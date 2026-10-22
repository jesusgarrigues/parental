"""One logical family, several AdGuard APIs. Remote writes are journalled and verified."""
import copy
import ipaddress
import json
import time


class GroupError(RuntimeError):
    def __init__(self, message, states=None):
        super().__init__(message)
        self.states = states or []


def identity(client):
    values = []
    for value in client.get('ids', []):
        try:
            value = str(ipaddress.ip_network(value, strict=False))
        except ValueError:
            if len(value) == 17 and value.count(':') == 5:
                value = value.lower()
        values.append(value)
    return frozenset(values)


def equivalent(current, expected):
    for key, value in expected.items():
        actual = current.get(key)
        if key in ('ids', 'tags', 'blocked_services') and isinstance(value, list):
            if sorted(actual or []) != sorted(value):
                return False
        elif actual != value:
            return False
    return True


class Group:
    def __init__(self, app, clock=time.time):
        self.a, self.clock = app, clock
        app.DB.executescript('''
        CREATE TABLE IF NOT EXISTS ag_baselines(client TEXT,node TEXT,data TEXT,PRIMARY KEY(client,node));
        CREATE TABLE IF NOT EXISTS ag_cache(node TEXT,path TEXT,data TEXT,updated REAL,PRIMARY KEY(node,path));
        CREATE TABLE IF NOT EXISTS ag_health(node TEXT PRIMARY KEY,data TEXT);
        CREATE TABLE IF NOT EXISTS ag_client_health(client TEXT,node TEXT,data TEXT,PRIMARY KEY(client,node));
        CREATE TABLE IF NOT EXISTS ag_changes(id INTEGER PRIMARY KEY AUTOINCREMENT,client TEXT,kind TEXT,targets TEXT,results TEXT,status TEXT,created REAL);
        CREATE TABLE IF NOT EXISTS ag_event_sources(event_id INTEGER,node TEXT,query_key TEXT,occurred REAL,PRIMARY KEY(node,query_key));
        ''')
        app.DB.commit()

    @property
    def enabled(self):
        return bool(self.a.server_config().get('replicas'))

    def nodes(self, config=None):
        config = config or self.a.server_config()
        primary = {k: v for k, v in config.items() if k != 'replicas'}
        primary.update(id='origin', name=config.get('name') or 'Principal')
        return [primary] + [dict(n, demo=config.get('demo', False)) for n in config.get('replicas', [])]

    def call(self, node, path, payload=None, method=None):
        return self.a.api(path, payload, method, config=node)

    def status(self, node, **patch):
        row = self.a.DB.execute('SELECT data FROM ag_health WHERE node=?', (node['id'],)).fetchone()
        value = json.loads(row[0]) if row else {}
        value.update(patch)
        value.update(id=node['id'], name=node['name'], checked=self.clock())
        self.a.DB.execute('INSERT OR REPLACE INTO ag_health VALUES(?,?)', (node['id'], json.dumps(value)))
        if patch.get('client'):
            self.a.DB.execute('INSERT OR REPLACE INTO ag_client_health VALUES(?,?,?)',(patch['client'],node['id'],json.dumps(patch)))
        self.a.DB.commit()

    def read(self, node, path, stale=False):
        try:
            data = self.call(node, path)
            self.a.DB.execute('INSERT OR REPLACE INTO ag_cache VALUES(?,?,?,?)', (node['id'], path, json.dumps(data), self.clock()))
            self.status(node, reachable=True, error='', stale=False,last_success=self.clock())
            return data
        except Exception as error:
            self.status(node, reachable=False, error=self.error(error), stale=True)
            row = self.a.DB.execute('SELECT data FROM ag_cache WHERE node=? AND path=?', (node['id'], path)).fetchone()
            if stale and row:
                return json.loads(row[0])
            raise GroupError(node['name'] + ': no se pudo leer ' + path.split('?')[0]) from None

    @staticmethod
    def error(error):
        if isinstance(error, GroupError):
            return str(error)
        if getattr(error, 'detail', None):
            return error.detail['message']
        return 'La API no confirmó la operación. Revisa conexión, credenciales y diagnóstico.'

    def primary(self, path):
        return self.read(self.nodes()[0], path, stale=True)

    def globals(self, node):
        return self.a.global_config(node)

    def match(self, canonical, clients):
        key = identity(canonical)
        matches = [c for c in clients if key and identity(c) == key]
        if len(matches) != 1:
            raise GroupError('Cliente ausente o identificadores incompatibles: ' + canonical['name'])
        return copy.deepcopy(matches[0])

    def logical_name(self, local, origin_clients):
        candidates = [c['name'] for c in origin_clients if identity(c) and identity(c) == identity(local)]
        return candidates[0] if len(candidates) == 1 else None

    def baseline(self, client, node='origin'):
        row = self.a.DB.execute('SELECT data FROM ag_baselines WHERE client=? AND node=?', (client, node)).fetchone()
        return json.loads(row[0]) if row else None

    def save_baseline(self, client, node, value):
        self.a.DB.execute('INSERT OR REPLACE INTO ag_baselines VALUES(?,?,?)', (client, node, json.dumps(value)))
        self.a.DB.commit()

    def pending(self):
        return bool(self.a.DB.execute("SELECT 1 FROM ag_changes WHERE status='pending'").fetchone())

    def preflight(self, name, consistent=False):
        nodes = self.nodes()
        origin = self.read(nodes[0], 'clients')['clients']
        canonical = next((c for c in origin if c['name'] == name), None)
        if canonical is None:
            raise GroupError('Cliente desconocido en el origen')
        result, policy = [], None
        for node in nodes:
            try:
                local = self.match(canonical, self.read(node, 'clients')['clients'])
                globals_ = self.globals(node)
                base = (self.baseline(name, node['id']) or {}).get('base', local)
                effective = self.a.effective(base, globals_)
                signature = copy.deepcopy(effective)
                signature['blocked_services'] = sorted(signature['blocked_services'])
                if consistent and policy is not None and signature != policy:
                    raise GroupError('La configuración base del cliente difiere entre servidores; revisa los ajustes antes de conceder tiempo')
                policy = signature
                result.append((node, local, globals_, copy.deepcopy(base)))
                self.status(node, client=name, policy='coincide', error='')
            except Exception as error:
                self.status(node, client=name, policy='conflicto', error=self.error(error))
                raise GroupError(node['name'] + ': ' + self.error(error)) from None
        return result

    def reconcile(self):
        failures = []
        names = [r[0] for r in self.a.DB.execute('SELECT DISTINCT client FROM ag_baselines')]
        for name in names:
            active = [r[0] for r in self.a.DB.execute('SELECT service FROM leases WHERE client=? AND expires>?', (name, self.clock()))]
            complete = True
            for node in self.nodes():
                b = self.baseline(name, node['id'])
                if not b:
                    continue
                try:
                    clients = self.read(node, 'clients')['clients']
                    current = self.match(b['base'], clients)
                    globals_ = self.globals(node)
                    for key in self.a.POLICY_KEYS:
                        value = current.get(key)
                        known = [b['base'].get(key), b['applied'].get(key)]
                        if 'previous' in b:
                            known.append(b['previous'].get(key))
                        if value not in known:
                            if key in current:
                                b['base'][key] = copy.deepcopy(value)
                            else:
                                b['base'].pop(key, None)
                    updated = self.a.overlay(current, b, active, globals_)
                    b['previous'] = copy.deepcopy(b['applied'])
                    b['applied'] = updated
                    self.save_baseline(name, node['id'], b)
                    if not equivalent(current, updated):
                        self.call(node, 'clients/update', {'name': current['name'], 'data': updated})
                    confirmed = self.match(b['base'], self.read(node, 'clients')['clients'])
                    if not equivalent(confirmed, updated):
                        raise GroupError('La lectura posterior no confirma la política solicitada; revisa adguard-sync')
                    b.pop('previous', None)
                    self.save_baseline(name, node['id'], b)
                    self.status(node, client=name, operation='aplicado' if active else 'restaurado', error='')
                except Exception as error:
                    complete = False
                    self.status(node, client=name, operation='aplicación pendiente' if active else 'restauración pendiente', error=self.error(error))
                    failures.append(node['name'] + ': ' + self.error(error))
            if complete and not active:
                self.a.DB.execute('DELETE FROM ag_baselines WHERE client=?', (name,))
                self.a.DB.execute('DELETE FROM leases WHERE client=?', (name,))
                self.a.DB.commit()
                self.a.event(name, '', '', 'restored')
        if failures:
            raise GroupError('Cambios pendientes en el grupo. ' + '; '.join(failures), self.health())

    def permit(self, name, service, minutes, require_blocked=False, granted_by=''):
        if type(minutes) is not int or not 1 <= minutes <= 1440:
            raise ValueError('Duración entre 1 y 1440 minutos')
        if type(require_blocked) is not bool:
            raise ValueError('Origen del permiso inválido')
        if self.pending():
            raise GroupError('Hay ajustes pendientes en el grupo. Espera su confirmación antes de conceder tiempo.')
        self.reconcile()  # Complete any previous restoration before starting another grant.
        rows = self.preflight(name, consistent=True)
        existing = self.a.DB.execute('SELECT expires,granted_by FROM leases WHERE client=? AND service=?', (name, service)).fetchone()
        if require_blocked and existing and existing[0] > self.clock():
            raise ValueError('Ya existe un permiso. Actualiza los datos.')
        for node, current, globals_, base in rows:
            catalog = {s['id'] for s in self.read(node, 'blocked_services/all')['blocked_services']} | set(self.a.RESTRICTIONS)
            if service not in catalog:
                raise ValueError(node['name'] + ': servicio desconocido')
            effective = self.a.effective(base, globals_)
            enabled = effective['safe_search'].get('enabled', False) if service == '@safesearch' else effective[self.a.RESTRICTIONS[service]] if service in self.a.RESTRICTIONS else service in effective['blocked_services']
            if not enabled:
                raise ValueError(node['name'] + ': esta restricción no está activada en la configuración base')
        for node,current,globals_,base in rows:
            if not self.baseline(name,node['id']):
                self.save_baseline(name,node['id'],{'base':base,'applied':current})
        expires = self.clock() + minutes * 60
        self.a.DB.execute('INSERT OR REPLACE INTO leases(client,service,expires,granted_by) VALUES(?,?,?,?)', (name, service, expires, granted_by))
        self.a.DB.commit()
        try:
            self.reconcile()
        except GroupError:
            # Compensate all destinations. A lost response can still have changed an AdGuard.
            if existing:
                self.a.DB.execute('UPDATE leases SET expires=?,granted_by=? WHERE client=? AND service=?', (*existing, name, service))
            else:
                self.a.DB.execute('DELETE FROM leases WHERE client=? AND service=?', (name, service))
            self.a.DB.commit()
            try:
                self.reconcile()
            except GroupError:
                raise GroupError('Concesión no confirmada. Restauración pendiente en algún AdGuard; no vuelvas a conceder hasta resolverla.', self.health()) from None
            raise GroupError('No se confirmó la concesión en todos los AdGuard. Se ha restaurado el estado previo.', self.health()) from None
        return {'ok': True, 'status': 'confirmed', 'expires': expires, 'servers': self.health()}

    def journal(self, client, kind, targets):
        identifier = self.a.DB.execute("INSERT INTO ag_changes(client,kind,targets,results,status,created) VALUES(?,?,?,'{}','pending',?)", (client, kind, json.dumps(targets), self.clock())).lastrowid
        self.a.DB.commit()
        self.apply_change(identifier)

    def apply_change(self, identifier):
        row = self.a.DB.execute('SELECT client,kind,targets,results FROM ag_changes WHERE id=?', (identifier,)).fetchone()
        name, kind, raw, previous = row
        targets, results = json.loads(raw), json.loads(previous)
        failures = []
        configs = {n['id']: n for n in self.nodes()}
        for key, target in targets.items():
            if results.get(key, {}).get('state') == 'confirmed':
                continue
            node = configs.get(key)
            if not node:
                failures.append('Servidor ya no configurado')
                continue
            results[key] = {'state': 'sending'}
            self.a.DB.execute('UPDATE ag_changes SET results=? WHERE id=?', (json.dumps(results), identifier))
            self.a.DB.commit()
            try:
                if kind == 'global':
                    for field, value in target['desired'].items():
                        self.write_global(node, field, value)
                    if not equivalent(self.globals(node), target['desired']):
                        raise GroupError('Lectura global sin confirmar')
                else:
                    clients = self.read(node, 'clients')['clients']
                    desired=copy.deepcopy(target['desired'])
                    baseline=self.baseline(name,key) if kind=='client' else None
                    if baseline:
                        active=[r[0] for r in self.a.DB.execute('SELECT service FROM leases WHERE client=? AND expires>?',(name,self.clock()))]
                        desired=self.a.overlay(desired,baseline,active,self.globals(node))
                        baseline['previous']=copy.deepcopy(baseline['applied'])
                        baseline['applied']=desired
                        self.save_baseline(name,key,baseline)
                    current = next((c for c in clients if c['name'] == desired['name']), None)
                    if current is None or not equivalent(current, desired):
                        if kind == 'add':
                            if current is not None:
                                raise GroupError('El nombre existe con otros identificadores')
                            self.call(node, 'clients/add', desired)
                        else:
                            self.call(node, 'clients/update', {'name': current['name'] if current else target['before']['name'], 'data': desired})
                    confirmed = next((c for c in self.read(node, 'clients')['clients'] if c['name'] == target['desired']['name']), {})
                    if not equivalent(confirmed, desired):
                        raise GroupError('La lectura del cliente no confirma el guardado')
                results[key] = {'state': 'confirmed'}
                self.status(node, client=name, operation='ajustes confirmados', error='')
            except Exception as error:
                results[key] = {'state': 'pending', 'error': self.error(error)}
                self.status(node, client=name, operation='ajustes pendientes', error=self.error(error))
                failures.append(node['name'] + ': ' + self.error(error))
            self.a.DB.execute('UPDATE ag_changes SET results=? WHERE id=?', (json.dumps(results), identifier))
            self.a.DB.commit()
        if failures:
            raise GroupError('Guardado parcial; se reintentará sin ampliar permisos. ' + '; '.join(failures), self.health())
        self.a.DB.execute("UPDATE ag_changes SET status='confirmed' WHERE id=?", (identifier,))
        self.a.DB.execute("DELETE FROM ag_changes WHERE status='confirmed' AND id NOT IN (SELECT id FROM ag_changes ORDER BY id DESC LIMIT 100)")
        self.a.DB.commit()

    def retry_changes(self):
        failures = []
        for (identifier,) in self.a.DB.execute("SELECT id FROM ag_changes WHERE status='pending' ORDER BY id").fetchall():
            try:
                self.apply_change(identifier)
            except GroupError as error:
                failures.append(str(error))
        if failures:
            raise GroupError('; '.join(failures), self.health())

    def write_global(self, node, key, value):
        current = self.globals(node)
        if current[key] == value:
            return
        if key == 'filtering_enabled':
            self.call(node, 'filtering/config', {'enabled': value, 'interval': self.read(node, 'filtering/status')['interval']})
        elif key in ('parental_enabled', 'safebrowsing_enabled'):
            self.call(node, key.split('_')[0] + ('/enable' if value else '/disable'), {})
        elif key == 'safe_search':
            self.call(node, 'safesearch/settings', value, 'PUT')
        else:
            self.call(node, 'blocked_services/update', value, 'PUT')

    def save_global(self, patch):
        if self.pending():
            raise GroupError('Ya hay un guardado pendiente. Espera su confirmación.')
        # Preflight every destination; no mutation until all reads succeed.
        targets = {}
        for node in self.nodes():
            self.globals(node)
            targets[node['id']] = {'desired': patch}
        self.journal('', 'global', targets)
        self.reconcile()

    def save_client(self, name, patch, add=False):
        if self.pending():
            raise GroupError('Ya hay un guardado pendiente. Espera su confirmación.')
        targets, bases = {}, []
        if add:
            for node in self.nodes():
                clients = self.read(node, 'clients')['clients']
                if any(c['name'] == patch['name'] or identity(c) & identity(patch) for c in clients):
                    raise ValueError(node['name'] + ': nombre o identificadores ya asignados')
                targets[node['id']] = {'before': {}, 'desired': patch}
        else:
            rows = self.preflight(name)
            for node, current, globals_, base in rows:
                b = self.baseline(name, node['id'])
                target = copy.deepcopy(base)
                target.update(patch)
                self.a.validate_client(target)
                if b and target['name'] != base['name']:
                    raise ValueError('Finaliza los permisos antes de renombrar el cliente')
                desired = copy.deepcopy(current)
                desired.update(patch)
                if b:
                    b['base'] = target
                    active = [r[0] for r in self.a.DB.execute('SELECT service FROM leases WHERE client=? AND expires>?', (name, self.clock()))]
                    desired = self.a.overlay(desired, b, active, globals_)
                    bases.append((node['id'], b))
                targets[node['id']] = {'before': current, 'desired': target}
        # Base edits are durable too; expiry always derives its policy from the latest approved base.
        for key, b in bases:
            self.save_baseline(name, key, b)
        self.journal(name, 'add' if add else 'client', targets)
        self.reconcile()

    def health(self):
        states = []
        for node in self.nodes():
            row = self.a.DB.execute('SELECT data FROM ag_health WHERE node=?', (node['id'],)).fetchone()
            states.append({'id': node['id'], 'name': node['name'], **(json.loads(row[0]) if row else {'reachable': None, 'operation': 'sin comprobar'})})
        return states

    def clients_status(self):
        nodes,result,peers=self.nodes(),{},{}
        origin=self.read(nodes[0],'clients',stale=True)['clients']
        for node in nodes:
            try: peers[node['id']]=self.read(node,'clients',stale=True)['clients']
            except GroupError: peers[node['id']]=[]
        for client in origin:
            result[client['name']]=[]
            for node in nodes:
                status=next(s for s in self.health() if s['id']==node['id'])
                row=self.a.DB.execute('SELECT data FROM ag_client_health WHERE client=? AND node=?',(client['name'],node['id'])).fetchone()
                operation=json.loads(row[0]).get('operation','') if row else ''
                try:
                    local=self.match(client,peers[node['id']])
                    result[client['name']].append({'id':node['id'],'name':node['name'],'client_name':local['name'],'matched':True,'reachable':status.get('reachable'),'operation':operation})
                except GroupError:
                    result[client['name']].append({'id':node['id'],'name':node['name'],'matched':False,'reachable':status.get('reachable'),'operation':'cliente sin vincular o servidor inaccesible'})
        return result

    def poll(self):
        origin = self.read(self.nodes()[0], 'clients', stale=True)['clients']
        failures, statuses = [], []
        for node in self.nodes():
            try:
                self.a.poll(config=node, node_id=node['id'], origin_clients=origin)
                self.status(node, querylog=self.a.LAST_POLL, error='')
                statuses.append(dict(self.a.LAST_POLL, server=node['name']))
            except Exception as error:
                self.status(node, querylog={'status': 'error'}, error=self.error(error))
                failures.append(node['name'])
        self.a.LAST_POLL = {'status': 'partial' if failures else 'ok', 'servers': statuses, 'unavailable': failures, 'message': 'Registros leídos por servidor; los clientes y avisos se agrupan.'}
        if failures:
            raise GroupError('No se pudo leer la actividad de: ' + ', '.join(failures))

    def observe(self, event_id, node, key, occurred):
        self.a.DB.execute('INSERT OR IGNORE INTO ag_event_sources VALUES(?,?,?,?)', (event_id, node, key, occurred))
        self.a.DB.execute('DELETE FROM ag_event_sources WHERE event_id NOT IN (SELECT id FROM events)')
        self.a.DB.commit()

    def decorate_events(self, events):
        for item in events:
            rows = self.a.DB.execute('SELECT node,COUNT(*),MAX(occurred) FROM ag_event_sources WHERE event_id=? GROUP BY node', (item['id'],)).fetchall()
            if rows:
                item.update(attempts=sum(r[1] for r in rows), last_seen=max(r[2] for r in rows), servers=[r[0] for r in rows],server_names=[next((n['name'] for n in self.nodes() if n['id']==r[0]),r[0]) for r in rows])
        return events

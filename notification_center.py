"""Durable, scoped notifications. Receiving or reading never grants access."""
import base64, hashlib, ipaddress, json, logging, os, re, time
from pathlib import Path
from urllib.parse import urlsplit
import auth, blocked_alerts

GROUP_WINDOW = 86400

# Push services identify the sender by this contact. Apple rejects the whole VAPID JWT (403 BadJwtToken)
# when it names no real domain, e.g. localhost or mDNS ".local" names, so the default is a public origin.
# py_vapid only accepts an https contact without a path, so the default is the bare project host.
DEFAULT_CONTACT = 'https://github.com'
RESERVED_SUFFIXES = ('localhost', 'local', 'invalid', 'test', 'example', 'internal', 'lan', 'home', 'arpa', 'localdomain')


def public_host(host):
    host = (host or '').lower().rstrip('.')
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    labels = host.split('.')
    return (len(labels) >= 2 and all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in labels)
            and not labels[-1].isdigit() and labels[-1] not in RESERVED_SUFFIXES and labels[-2:] != ['example', 'com'])


def vapid_contact(value):
    """Return a contact Apple, Google and Mozilla accept: mailto: or https: with a real public domain."""
    value = (value or '').strip()
    if not value:
        return DEFAULT_CONTACT
    if value.lower().startswith('mailto:'):
        address = value[7:]
        if re.fullmatch(r'[^@\s:/]+@[^@\s]+', address) and public_host(address.rsplit('@', 1)[1]):
            return value
    elif value.lower().startswith('https://'):
        url = urlsplit(value)
        # Origin only: py_vapid rejects https contacts with a path, port, query or credentials.
        if public_host(url.hostname) and value.rstrip('/') == 'https://' + url.netloc and url.netloc == url.hostname:
            return value.rstrip('/')
    raise ValueError('WEB_PUSH_CONTACT debe ser mailto:correo@dominio-real o https://dominio-real sin ruta')


def provider_error(error):
    """Short, non-sensitive reason for a rejected Push: status code plus the provider's reason token only."""
    response = getattr(error, 'response', None)
    code = getattr(response, 'status_code', None)
    if not code:
        return None, 'push_network_or_configuration'
    reason = ''
    try:
        data = json.loads((getattr(response, 'text', '') or '')[:1000])
        candidate = data.get('reason') if isinstance(data, dict) else None
        if isinstance(candidate, str) and re.fullmatch(r'[A-Za-z]{1,40}', candidate):
            reason = ':' + candidate
    except (ValueError, TypeError):
        pass
    return code, 'push_provider_' + str(code) + reason


def device_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{16,80}', value):
        raise ValueError('Dispositivo de avisos inválido')
    return value


def subscription(value):
    if value is None: return None
    if not isinstance(value, dict): raise ValueError('Suscripción Push inválida')
    endpoint = value.get('endpoint')
    if not isinstance(endpoint, str) or len(endpoint) > 3072: raise ValueError('Destino Push inválido')
    url = urlsplit(endpoint)
    if (url.scheme != 'https' or url.hostname not in ('web.push.apple.com', 'fcm.googleapis.com', 'updates.push.services.mozilla.com')
            or url.username or url.password or url.port not in (None, 443) or url.fragment):
        raise ValueError('Proveedor Push no admitido; usa Safari, Chrome o Firefox')
    keys = value.get('keys')
    if not isinstance(keys, dict): raise ValueError('Claves Push inválidas')
    try:
        for name, size in (('auth', 16), ('p256dh', 65)):
            raw = keys[name]
            if not isinstance(raw, str) or not re.fullmatch(r'[A-Za-z0-9_-]{20,100}={0,2}', raw): raise ValueError()
            decoded = base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4))
            if len(decoded) != size: raise ValueError()
        from cryptography.hazmat.primitives.asymmetric import ec
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), decoded)
    except (ValueError, KeyError): raise ValueError('Claves Push inválidas') from None
    return {'endpoint': endpoint, 'keys': {k: keys[k] for k in ('auth', 'p256dh')}}


class Center:
    def __init__(self, folder, clock=time.time, sender=None, contact=None):
        self.clock, self.sender = clock, sender or self.send
        try:
            self.contact = vapid_contact(os.getenv('WEB_PUSH_CONTACT', '') if contact is None else contact)
        except ValueError as error:
            logging.warning('Avisos: %s; se usa el contacto predeterminado %s', error, DEFAULT_CONTACT)
            self.contact = DEFAULT_CONTACT
        self.keyfile = Path(folder) / 'web-push.pem'
        with auth.LOCK:
            auth.DB.executescript('''
            CREATE TABLE IF NOT EXISTS alert_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,source TEXT NOT NULL,kind TEXT NOT NULL,client TEXT NOT NULL,service TEXT NOT NULL,created REAL NOT NULL,detail TEXT NOT NULL,read_at REAL,UNIQUE(user_id,source));
            CREATE TABLE IF NOT EXISTS alert_devices(id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,subscription TEXT,created REAL NOT NULL,updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS alert_deliveries(alert_id INTEGER NOT NULL,device TEXT NOT NULL,status TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,due REAL NOT NULL,error TEXT NOT NULL DEFAULT '',PRIMARY KEY(alert_id,device));
            ''')
            auth.DB.execute('INSERT OR IGNORE INTO alert_meta VALUES(?,?)', ('started', str(clock())))
            self.started = float(auth.DB.execute("SELECT value FROM alert_meta WHERE key='started'").fetchone()[0])
            auth.DB.commit()
        self.migrate_groups()
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        if not self.keyfile.exists():
            key = ec.generate_private_key(ec.SECP256R1())
            data = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
            try:
                fd = os.open(self.keyfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'wb') as stream: stream.write(data)
            except FileExistsError: pass
        key = serialization.load_pem_private_key(self.keyfile.read_bytes(), password=None)
        self.public_key = base64.urlsafe_b64encode(key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)).rstrip(b'=').decode()

    def migrate_groups(self):
        """Add immutable 24h groups without deleting old alerts or changing requests."""
        with auth.LOCK:
            auth.DB.execute('CREATE TABLE IF NOT EXISTS alert_groups(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,client TEXT NOT NULL,service TEXT NOT NULL,created REAL NOT NULL,read_at REAL)')
            if 'group_id' not in [r[1] for r in auth.DB.execute('PRAGMA table_info(alerts)')]:
                auth.DB.execute('ALTER TABLE alerts ADD COLUMN group_id INTEGER')
            auth.DB.execute('CREATE INDEX IF NOT EXISTS alert_groups_lookup ON alert_groups(user_id,client,service,created)')
            auth.DB.execute('CREATE INDEX IF NOT EXISTS alerts_group ON alerts(group_id)')
            for row in auth.DB.execute("SELECT * FROM alerts WHERE kind='blocked' AND group_id IS NULL ORDER BY created,id").fetchall():
                group = self.group(row['user_id'],row['client'],row['service'],row['created'])
                auth.DB.execute('UPDATE alerts SET group_id=? WHERE id=?',(group['id'],row['id']))
                if row['read_at'] is not None:
                    auth.DB.execute('UPDATE alert_groups SET read_at=COALESCE(read_at,?) WHERE id=?',(row['read_at'],group['id']))
            auth.DB.execute("UPDATE alerts SET read_at=(SELECT read_at FROM alert_groups WHERE id=alerts.group_id) WHERE group_id IN (SELECT id FROM alert_groups WHERE read_at IS NOT NULL)")
            auth.DB.commit()

    def group(self, uid, client, service, created):
        row = auth.DB.execute('SELECT * FROM alert_groups WHERE user_id=? AND client=? AND service=? AND created<=? AND created>? ORDER BY created DESC LIMIT 1',(uid,client,service,created,created-GROUP_WINDOW)).fetchone()
        if row is None:
            gid=auth.DB.execute('INSERT INTO alert_groups(user_id,client,service,created) VALUES(?,?,?,?)',(uid,client,service,created)).lastrowid
            row=auth.DB.execute('SELECT * FROM alert_groups WHERE id=?',(gid,)).fetchone()
        return row

    def grouped(self, items):
        groups={}
        for item in items:
            if item['kind']!='blocked' or item.get('group_id') is None: continue
            groups.setdefault(item['group_id'],[]).append(item)
        result=[]
        for gid,members in groups.items():
            group=auth.DB.execute('SELECT * FROM alert_groups WHERE id=?',(gid,)).fetchone()
            if not group: continue
            latest=max(members,key=lambda x:(x['created'],x['id']))
            details=[json.loads(i['detail']) for i in members]
            value=self.public(latest)
            value.update(group_id=gid,first_seen=group['created'],last_seen=max(max(i['created'] for i in members),max(d.get('last_seen',0) for d in details)),expires=group['created']+GROUP_WINDOW,read_at=group['read_at'],is_new=group['read_at'] is None and group['created']<=self.clock()<group['created']+GROUP_WINDOW,attempts=sum(max(1,int(d.get('attempts',1))) for d in details),records=len(members))
            result.append(value)
        return sorted(result,key=lambda x:(x['is_new'],x['last_seen']),reverse=True)

    def user(self, uid):
        row = auth.DB.execute('SELECT * FROM users WHERE id=? AND active=1', (uid,)).fetchone()
        return auth.public(row) if row else None

    def visible(self, user, item):
        if item['kind'] == 'signup':  # New-account requests are for administrators only (#98).
            return bool(user and user['role'] == 'admin')
        return bool(user and user['role'] in ('admin', 'responsable') and (item['kind']=='test' or user['role'] == 'admin' or item['client'] in user['clients']))

    def current(self, user, item):
        if not self.visible(user, item): return False
        if item['kind']=='test': return True
        if item['kind'] == 'signup':
            return bool(auth.DB.execute('SELECT 1 FROM signups WHERE user_id=?', (json.loads(item['detail'])['user_id'],)).fetchone())
        if item['kind'] == 'request':
            rid = json.loads(item['detail'])['request_id']
            row = auth.DB.execute("SELECT status,created,client FROM requests WHERE id=?", (rid,)).fetchone()
            return bool(row and row['status'] == 'pending' and row['created'] > self.clock()-86400 and row['client'] == item['client'])
        pref = blocked_alerts.preferences(user)
        chosen = pref['preferences']['clients'].get(item['client'], pref['preferences'])
        service = item['service']
        selected = service in chosen['protections'] if service.startswith('@') else chosen['mode'] == 'all' or (chosen['mode'] == 'selected' and service in chosen['services'])
        return selected and item['created'] > pref['updated']

    def sync(self, events):
        now, reasons, made = self.clock(), {}, 0
        with auth.LOCK:
            users = [auth.public(r) for r in auth.DB.execute("SELECT * FROM users WHERE active=1 AND role IN ('admin','responsable')")]
            for user in users:
                config = blocked_alerts.preferences(user)
                evaluated,excluded=blocked_alerts.evaluate(user,events,config,now=now)
                eligible = {e['id'] for e in evaluated}
                auth.DB.execute('INSERT OR REPLACE INTO alert_meta VALUES(?,?)', ('scan:'+str(user['id']),json.dumps({'time':now,'events_in_scope':sum(self.visible(user,e) for e in events),'eligible':len(eligible),'excluded':{k:v for k,v in excluded.items() if k!='not_blocked_or_outside_scope'}})))
                for event in events:
                    if event['kind'] != 'blocked' or not self.visible(user, event): continue
                    if event['time'] < self.started or event['id'] not in eligible:
                        reasons['history_preferences_or_cooldown'] = reasons.get('history_preferences_or_cooldown', 0) + 1
                        continue
                    source = 'block:' + hashlib.sha256(json.dumps([event[k] for k in ('time','client','service','domain')], ensure_ascii=False).encode()).hexdigest()
                    made += self.insert(user, source, 'blocked', event['client'], event['service'], event['time'], {'domain': event['domain'],'attempts':event.get('attempts',1),'last_seen':event.get('last_seen',event['time'])})
                if user['role'] == 'admin':
                    for row in auth.DB.execute('SELECT s.user_id,s.created,u.username FROM signups s JOIN users u ON u.id=s.user_id').fetchall():
                        if row['created'] >= self.started:
                            made += self.insert(user, 'signup:'+str(row['user_id']), 'signup', '', '', row['created'], {'user_id':row['user_id'],'username':row['username']})
                for request in auth.requests_for(user):
                    if request['status'] == 'pending' and request['created'] >= self.started:
                        made += self.insert(user, 'request:'+str(request['id']), 'request', request['client'], request['service'], request['created'], {'request_id':request['id'],'username':request['username'],'minutes':request['minutes']})
            auth.DB.execute('INSERT OR REPLACE INTO alert_meta VALUES(?,?)', ('scan', json.dumps({'time':now,'events':len(events),'created':made,'excluded':reasons})))
            # Bound retained history, without truncating the unread inbox count at a display limit.
            old = now-30*86400
            auth.DB.execute('DELETE FROM alert_deliveries WHERE alert_id IN (SELECT id FROM alerts WHERE created<?)', (old,))
            auth.DB.execute('DELETE FROM alerts WHERE created<?', (old,))
            auth.DB.execute('DELETE FROM alert_groups WHERE id NOT IN (SELECT group_id FROM alerts WHERE group_id IS NOT NULL)')
            auth.DB.commit()

    def insert(self, user, source, kind, client, service, created, detail):
        old=auth.DB.execute('SELECT id,detail FROM alerts WHERE user_id=? AND source=?',(user['id'],source)).fetchone()
        if old:
            if kind=='blocked':
                previous=json.loads(old['detail'])
                detail['attempts']=max(previous.get('attempts',1),detail.get('attempts',1))
                detail['last_seen']=max(previous.get('last_seen',created),detail.get('last_seen',created))
                auth.DB.execute('UPDATE alerts SET detail=? WHERE id=?',(json.dumps(detail,ensure_ascii=False),old['id']))
            return 0
        group=self.group(user['id'],client,service,created) if kind=='blocked' else None
        result = auth.DB.execute('INSERT INTO alerts(user_id,source,kind,client,service,created,detail,group_id,read_at) VALUES(?,?,?,?,?,?,?,?,?)', (user['id'],source,kind,client,service,created,json.dumps(detail,ensure_ascii=False),group['id'] if group else None,group['read_at'] if group else None))
        if result.rowcount:
            for device in auth.DB.execute('SELECT id FROM alert_devices WHERE user_id=?', (user['id'],)):
                auth.DB.execute("INSERT OR IGNORE INTO alert_deliveries VALUES(?,?,'pending',0,?,'')", (result.lastrowid,device['id'],self.clock()))
        return result.rowcount

    def register(self, user, body):
        device = device_id(body.get('device'))
        sub = subscription(body.get('subscription'))
        with auth.LOCK:
            old = auth.DB.execute('SELECT * FROM alert_devices WHERE id=?', (device,)).fetchone()
            if old and old['user_id'] != user['id']: raise auth.Forbidden('Este dispositivo pertenece a otra sesión; cierra sesión primero')
            moved = None
            if sub:
                endpoint = sub['endpoint']
                for row in auth.DB.execute('SELECT id,user_id,subscription FROM alert_devices WHERE subscription IS NOT NULL').fetchall():
                    if json.loads(row['subscription'])['endpoint'] == endpoint and row['id'] != device:
                        if row['user_id'] != user['id']:
                            raise ValueError('Este navegador ya tiene otro dispositivo de avisos; desactívalo antes de activar otro')
                        moved = row['id']  # Same browser and account with a new local id (storage cleared): keep its queue.
            if not old and not moved and auth.DB.execute('SELECT COUNT(*) FROM alert_devices WHERE user_id=?',(user['id'],)).fetchone()[0]>=20:
                raise ValueError('Límite de 20 dispositivos de avisos; desactiva uno antes de añadir otro')
            if moved:
                if old:
                    auth.DB.execute('DELETE FROM alert_deliveries WHERE device=?', (moved,))
                else:
                    auth.DB.execute('UPDATE alert_deliveries SET device=? WHERE device=?', (device, moved))
                auth.DB.execute('DELETE FROM alert_devices WHERE id=?', (moved,))
            # A page load may briefly see no browser subscription; only Activar avisos, disconnect or an
            # expired-subscription response change a stored one, so Push is never dropped silently.
            stored = json.dumps(sub) if sub else old['subscription'] if old else None
            auth.DB.execute('INSERT OR REPLACE INTO alert_devices VALUES(?,?,?,?,?)', (device,user['id'],stored,old['created'] if old else self.clock(),self.clock()))
            if sub and (not old or old['subscription']!=json.dumps(sub)):
                auth.DB.execute("UPDATE alert_deliveries SET status='pending',attempts=0,due=?,error='' WHERE device=? AND status='failed' AND alert_id IN (SELECT id FROM alerts WHERE read_at IS NULL AND created>?)",(self.clock(),device,self.clock()-3600))
            auth.DB.commit()
        return {'ok':True,'push':bool(stored),'public_key':self.public_key}

    def disconnect(self, user, device):
        device_id(device)
        with auth.LOCK:
            row = auth.DB.execute('SELECT user_id FROM alert_devices WHERE id=?', (device,)).fetchone()
            if row and row['user_id'] != user['id']: raise auth.Forbidden('Dispositivo ajeno')
            auth.DB.execute('DELETE FROM alert_deliveries WHERE device=?', (device,))
            auth.DB.execute('DELETE FROM alert_devices WHERE id=?', (device,));auth.DB.commit()
        return {'ok':True}

    def snapshot(self, user, device=None):
        with auth.LOCK:
            items = [dict(r) for r in auth.DB.execute('SELECT * FROM alerts WHERE user_id=? ORDER BY id DESC', (user['id'],)) if self.visible(user, r)]
            groups=self.grouped(items)
            unread=len({g['client'] for g in groups if g['is_new']})
            requests = auth.requests_for(user)
            pending = sum(r['status'] == 'pending' for r in requests)
            found = auth.DB.execute('SELECT * FROM alert_devices WHERE id=? AND user_id=?', (device,user['id'])).fetchone() if device else None
            candidates, diagnostic = [], {'registered':bool(found),'push':bool(found and found['subscription'])}
            if found:
                for item in items:
                    delivery = auth.DB.execute('SELECT * FROM alert_deliveries WHERE alert_id=? AND device=?', (item['id'],device)).fetchone()
                    if delivery and self.current(user,item) and item['read_at'] is None and delivery['status'] in ('pending','retry','sending') and delivery['due'] <= self.clock() and item['created'] > self.clock()-3600:
                        candidates.append(self.public(item))
                last = auth.DB.execute('SELECT status,attempts,error,due FROM alert_deliveries WHERE device=? ORDER BY alert_id DESC LIMIT 1', (device,)).fetchone()
                if last: diagnostic['last_delivery'] = dict(last)
            scan = auth.DB.execute('SELECT value FROM alert_meta WHERE key=?',('scan:'+str(user['id']),)).fetchone()
            diagnostic['scan'] = json.loads(scan[0]) if scan else {'status':'Todavía no se han recogido avisos para esta cuenta.'}
            return {'items':[self.public(i) for i in items[:200]],'groups':groups[:200],'unread_groups':sum(g['is_new'] for g in groups),'unread':unread,'pending_requests':pending,'badge':unread+pending,'requests':requests,'deliveries':candidates[:20], 'diagnostic':diagnostic,'public_key':self.public_key}

    def public(self, item):
        detail = json.loads(item['detail'])
        target = {'kind':'request','id':detail['request_id']} if item['kind']=='request' else {'kind':'users'} if item['kind']=='signup' else {'kind':'client','client':item['client'],'service':item['service']}
        target['alert_id'] = item['id']
        if item['kind']=='test': target=None
        return {k:item[k] for k in ('id','kind','client','service','created','read_at')} | {'detail':detail,'target':target}

    def read(self, user, body):
        identifier = body.get('id')
        client = body.get('client')
        if client is not None and (not isinstance(client,str) or not client or identifier is not None): raise ValueError('Cliente de avisos inválido')
        if identifier is not None and (type(identifier) is not int or identifier<=0): raise ValueError('Aviso inválido')
        with auth.LOCK:
            items = [dict(r) for r in auth.DB.execute('SELECT * FROM alerts WHERE user_id=?', (user['id'],)) if self.visible(user,r)]
            if identifier is not None and not any(i['id']==identifier for i in items): raise auth.Forbidden('Aviso no disponible para esta cuenta')
            if client is not None and not any(i['client']==client and i['kind']=='blocked' for i in items): raise auth.Forbidden('Cliente fuera de ámbito')
            selected=[i for i in items if (identifier is None or i['id']==identifier) and (client is None or (i['kind']=='blocked' and i['client']==client))]
            gids={i['group_id'] for i in selected if i.get('group_id') is not None}
            for gid in gids:
                auth.DB.execute('UPDATE alert_groups SET read_at=COALESCE(read_at,?) WHERE id=? AND user_id=?',(self.clock(),gid,user['id']))
                auth.DB.execute('UPDATE alerts SET read_at=COALESCE(read_at,?) WHERE group_id=? AND user_id=?',(self.clock(),gid,user['id']))
            for item in selected:
                auth.DB.execute('UPDATE alerts SET read_at=COALESCE(read_at,?) WHERE id=?', (self.clock(),item['id']))
            auth.DB.commit()
        return {'ok':True}

    def delivery(self, user, body):
        device, identifier, action = device_id(body.get('device')), body.get('id'), body.get('status')
        if type(identifier) is not int or identifier<=0 or action not in ('claim','accepted','failed'): raise ValueError('Resultado de aviso inválido')
        with auth.LOCK:
            found = auth.DB.execute('SELECT * FROM alert_devices WHERE id=? AND user_id=?', (device,user['id'])).fetchone()
            item = auth.DB.execute('SELECT * FROM alerts WHERE id=? AND user_id=?', (identifier,user['id'])).fetchone()
            if not found or not item or not self.current(user,item) or found['subscription']: raise auth.Forbidden('Entrega no disponible para esta cuenta o canal')
            job = auth.DB.execute('SELECT * FROM alert_deliveries WHERE alert_id=? AND device=?', (identifier,device)).fetchone()
            if not job: raise ValueError('Entrega desconocida')
            if action == 'claim':
                if item['read_at'] is not None or job['status'] not in ('pending','retry','sending') or job['due']>self.clock() or job['attempts']>=5: return {'claimed':False}
                auth.DB.execute("UPDATE alert_deliveries SET status='sending',attempts=attempts+1,due=? WHERE alert_id=? AND device=?", (self.clock()+60,identifier,device))
            else:
                if job['status'] != 'sending': return {'ok':True}
                self.finish(identifier,device,action=='accepted',job['attempts'],'browser_rejected')
            auth.DB.commit()
        return {'claimed':True} if action=='claim' else {'ok':True}

    def finish(self, identifier, device, accepted, attempts, error):
        status = 'accepted' if accepted else 'failed' if attempts>=5 else 'retry'
        auth.DB.execute('UPDATE alert_deliveries SET status=?,due=?,error=? WHERE alert_id=? AND device=?', (status,self.clock()+min(300,10*2**attempts),'' if accepted else error,identifier,device))

    def payload(self, user, item):
        value = self.public(item)
        title = 'Nueva solicitud de '+value['detail']['username'] if item['kind']=='request' else item['client']+' · '+item['service']
        body = item['client']+' · '+str(value['detail']['minutes'])+' minutos' if item['kind']=='request' else 'Servicio bloqueado. Pulsa para revisar y autorizar tiempo.'
        if item['kind']=='test': title,body='Parental · Prueba desde el servidor','Este aviso se ha enviado desde Docker mediante Web Push.'
        if item['kind']=='signup': title,body='Nueva cuenta pendiente: '+value['detail']['username'],'Ha entrado con Authentik. Revísala en Usuarios y roles para activarla o rechazarla.'
        return {'title':title,'body':body,'target':value['target'],'tag':'parental-alert-'+str(item['id']),'badge_count':self.snapshot(user)['badge']}

    def test(self, user, body):
        auth.require(user,'admin','responsable')
        device=device_id(body.get('device'))
        with auth.LOCK:
            row=auth.DB.execute('SELECT * FROM alert_devices WHERE id=? AND user_id=? AND subscription IS NOT NULL',(device,user['id'])).fetchone()
            if not row: raise ValueError('Activa primero Push en este dispositivo')
            identifier=auth.DB.execute("INSERT INTO alerts(user_id,source,kind,client,service,created,detail) VALUES(?,?,'test','','',?,'{}')",(user['id'],'test:'+str(time.time_ns()),self.clock())).lastrowid
            auth.DB.execute("INSERT INTO alert_deliveries VALUES(?,?,'pending',0,?,'')",(identifier,device,self.clock()+10))
            auth.DB.commit()
        return {'ok':True}

    def send(self, sub, payload):
        from pywebpush import webpush
        import requests
        class NoRedirectSession(requests.Session):
            def request(self,*args,**kwargs):
                kwargs['allow_redirects']=False
                return super().request(*args,**kwargs)
        with NoRedirectSession() as session:
            # Urgency high: Android otherwise defers normal-priority Web Push while the phone is idle (Doze).
            response = webpush(subscription_info=sub,data=json.dumps(payload,ensure_ascii=False),vapid_private_key=str(self.keyfile),vapid_claims={'sub':self.contact},ttl=3600,timeout=10,headers={'Urgency':'high'},requests_session=session)
            if response.status_code not in (200,201,202): raise ValueError('push_provider_rejected')

    def dispatch(self):
        # Send outside the shared database/AdGuard locks. Never log endpoint, keys or response bodies.
        for _ in range(20):
            with auth.LOCK:
                jobs = auth.DB.execute("SELECT j.*,d.user_id,d.subscription FROM alert_deliveries j JOIN alert_devices d ON d.id=j.device WHERE d.subscription IS NOT NULL AND j.status IN ('pending','retry','sending') AND j.due<=? ORDER BY j.alert_id LIMIT 50", (self.clock(),)).fetchall()
                job = None
                for candidate in jobs:
                    item = auth.DB.execute('SELECT * FROM alerts WHERE id=?', (candidate['alert_id'],)).fetchone()
                    user = self.user(candidate['user_id'])
                    if not item or item['user_id']!=candidate['user_id'] or item['read_at'] is not None or not self.current(user,item) or item['created']<self.clock()-3600 or candidate['attempts']>=5:
                        auth.DB.execute("UPDATE alert_deliveries SET status='skipped' WHERE alert_id=? AND device=?", (candidate['alert_id'],candidate['device']))
                        continue
                    job = candidate; break
                if not job: auth.DB.commit();return
                attempts = job['attempts']+1
                auth.DB.execute("UPDATE alert_deliveries SET status='sending',attempts=?,due=? WHERE alert_id=? AND device=?", (attempts,self.clock()+60,job['alert_id'],job['device']))
                payload = self.payload(user,item);auth.DB.commit()
            accepted, code, error_code = False, None, ''
            try: self.sender(json.loads(job['subscription']),payload);accepted=True
            except Exception as error:
                code, error_code = provider_error(error)
                logging.warning('Avisos: Push rechazado (%s)', error_code)
            with auth.LOCK:
                self.finish(job['alert_id'],job['device'],accepted,attempts,error_code)
                if code in (404,410):
                    auth.DB.execute('UPDATE alert_devices SET subscription=NULL WHERE id=?', (job['device'],))
                    auth.DB.execute("UPDATE alert_deliveries SET status='failed',error='push_subscription_expired' WHERE device=? AND status!='accepted'", (job['device'],))
                auth.DB.commit()

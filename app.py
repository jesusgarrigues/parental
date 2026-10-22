import logging, socket, ssl
from collections import deque
import base64, copy, hashlib, hmac, ipaddress, json, os, sqlite3, threading, time, urllib.error, urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from http.cookies import CookieError, SimpleCookie
import auth
import re
import nintendo
import oidc
import blocked_alerts
import notification_center
import adguard_group
import sys
import uuid
import security
from datetime import datetime

ROOT = Path(__file__).parent


CSP = security.content_security_policy((ROOT / 'index.html').read_text())
DATA = Path(os.getenv('DATA_DIR', '/data'))
DATA.mkdir(parents=True, exist_ok=True)
auth.init(DATA)
ALERTS = notification_center.Center(DATA)
AUTHENTIK = oidc.Connector(DATA)
NINTENDO = nintendo.Connector(DATA)
TOKEN = os.environ.get('APP_TOKEN', '')
LOCK = threading.RLock()
DB = sqlite3.connect(DATA / 'state.db', check_same_thread=False)
DB.executescript(
    '''CREATE TABLE IF NOT EXISTS leases (client TEXT, service TEXT, expires REAL, PRIMARY KEY(client,service));
CREATE TABLE IF NOT EXISTS baselines (client TEXT PRIMARY KEY, config TEXT);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, time REAL, client TEXT, service TEXT, domain TEXT, kind TEXT);
CREATE TABLE IF NOT EXISTS seen (key TEXT PRIMARY KEY, time REAL);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS client_appearance (identity TEXT PRIMARY KEY, icon TEXT NOT NULL);
'''
)
# Who granted each temporary permit (#99). Older rows keep an empty author.
if 'granted_by' not in [row[1] for row in DB.execute('PRAGMA table_info(leases)')]:
    DB.execute("ALTER TABLE leases ADD COLUMN granted_by TEXT NOT NULL DEFAULT ''")
    DB.commit()
LAST_ERROR = ''
LAST_POLL = {'status': 'waiting', 'message': 'Todavía no se ha leído el registro de consultas.'}
DIAGNOSTICS = deque(maxlen=100)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
SAFE_KEYS = ('enabled', 'bing', 'duckduckgo', 'ecosia', 'google', 'pixabay', 'yandex', 'youtube')
BOOL_KEYS = (
    'use_global_settings',
    'filtering_enabled',
    'parental_enabled',
    'safebrowsing_enabled',
    'safesearch_enabled',
    'use_global_blocked_services',
    'ignore_querylog',
    'ignore_statistics',
    'upstreams_cache_enabled',
)
CLIENT_KEYS = set(BOOL_KEYS) | {
    'name',
    'ids',
    'safe_search',
    'blocked_services_schedule',
    'blocked_services',
    'upstreams',
    'tags',
    'upstreams_cache_size',
}
RESTRICTIONS = {
    '@filtering': 'filtering_enabled',
    '@parental': 'parental_enabled',
    '@safebrowsing': 'safebrowsing_enabled',
    '@safesearch': 'safe_search',
}
POLICY_KEYS = (
    'use_global_settings',
    'filtering_enabled',
    'parental_enabled',
    'safebrowsing_enabled',
    'safe_search',
    'safesearch_enabled',
    'use_global_blocked_services',
    'blocked_services',
    'blocked_services_schedule',
)
APPEARANCE_CATALOG = json.loads((ROOT / 'assets/appearance-catalog.json').read_text())
DEVICE_ICONS = tuple(item['id'] for item in APPEARANCE_CATALOG['devices'])
SERVICE_LOGOS = json.loads((ROOT / 'assets/service-catalog.json').read_text())
SERVICE_ARTWORK = json.loads((ROOT / 'assets/service-artwork.json').read_text())
STATIC_ASSETS = {
    '/assets/appearance-catalog.js': ('assets/appearance-catalog.js', 'application/javascript'),
    '/assets/authentik.js': ('assets/authentik.js', 'application/javascript'),
    '/assets/identity.js': ('assets/identity.js', 'application/javascript'),
    '/assets/blocked-notifications.js': ('assets/blocked-notifications.js', 'application/javascript'),
    '/assets/notification-center.js': ('assets/notification-center.js', 'application/javascript'),
    '/assets/notifications.js': ('assets/notifications.js', 'application/javascript'),
    '/assets/notification-targets.js': ('assets/notification-targets.js', 'application/javascript'),
    '/assets/app-badges.js': ('assets/app-badges.js', 'application/javascript'),
    '/assets/service-logos.js': ('assets/service-logos.js', 'application/javascript'),
    '/assets/parental.css': ('assets/parental.css', 'text/css'),
    '/assets/app.css': ('assets/app.css', 'text/css'),
    '/assets/app-shell.js': ('assets/app-shell.js', 'application/javascript'),
    '/assets/parental-icon.png': ('assets/parental-icon.png', 'image/png'),
    '/assets/client-settings.js': ('assets/client-settings.js', 'application/javascript'),
    '/assets/fonts/InterVariable.woff2': ('assets/fonts/InterVariable.woff2', 'font/woff2'),
    **{'/assets/icons/' + icon + '.svg': ('assets/icons/' + icon + '.svg', 'image/svg+xml') for icon in DEVICE_ICONS},
    **{
        '/assets/avatars/' + avatar + '.svg': ('assets/avatars/' + avatar + '.svg', 'image/svg+xml')
        for avatar in auth.AVATARS
        if avatar
    },
    **{
        '/assets/licenses/' + name + '.txt': ('assets/licenses/' + name + '.txt', 'text/plain')
        for name in ('inter', 'lucide', 'hostlists-registry', 'parental-artwork', 'simple-icons', 'dashboard-icons')
    },
}


def appearance_identity(client, provider='adguard'):
    # AdGuard has no stable client UUID: an unchanged set of identifiers survives a rename.
    identifiers = sorted(set(client.get('ids') or [client['name']])) if provider == 'adguard' else [client['key']]
    return provider + ':' + hashlib.sha256(json.dumps(identifiers, ensure_ascii=False).encode()).hexdigest()


def automatic_icon(client, provider='adguard'):
    if provider == 'nintendo':
        return 'gamepad-2'
    name = client['name'].lower()
    hinted = sorted(
        ((hint, item['id']) for item in APPEARANCE_CATALOG['devices'] for hint in item.get('hints', [])),
        key=lambda x: len(x[0]),
        reverse=True,
    )
    model = next((icon for hint, icon in hinted if hint in name), None)
    kinds = {
        'device_phone': 'smartphone',
        'device_tablet': 'tablet',
        'device_tv': 'tv',
        'device_router': 'router',
        'device_printer': 'printer',
        'device_gameconsole': 'gamepad-2',
    }
    compatible = {
        'device_phone': {'iphone', 'iphone-classic'},
        'device_tablet': {'ipad', 'ipad-pro'},
        'device_tv': {
            'apple-tv',
            'fire-tv',
            'fire-tv-stick',
            'fire-tv-lite',
            'fire-tv-4k',
            'fire-tv-4k-max',
            'fire-tv-cube',
        },
        'device_router': {'wifi-ap', 'mesh', 'network-switch', 'firewall'},
    }
    for tag in client.get('tags', []):
        if tag in kinds:
            return model if model in compatible.get(tag, set()) else kinds[tag]
    if model:
        return model
    name = client['name'].lower()
    for pattern, icon in (
        (r'ipad|tablet', 'tablet'),
        (r'iphone|android|móvil|movil|phone', 'smartphone'),
        (r'macbook|laptop|portátil|portatil', 'laptop'),
        (r'imac|ordenador|desktop|\bpc\b', 'monitor'),
        (r'switch|xbox|playstation|consola', 'gamepad-2'),
        (r'televisi|\btv\b', 'tv'),
        (r'router|wifi', 'router'),
        (r'impresora|printer', 'printer'),
        (r'altavoz|speaker', 'speaker'),
        (r'auriculares|headphone', 'headphones'),
        (r'reloj|watch', 'watch'),
    ):
        if re.search(pattern, name):
            return icon
    return 'monitor' if 'device_pc' in client.get('tags', []) else 'server'


def attach_appearance(client, provider='adguard'):
    row = DB.execute(
        'SELECT icon FROM client_appearance WHERE identity=?', (appearance_identity(client, provider),)
    ).fetchone()
    client['ui_icon'] = row[0] if row else None
    client['ui_auto_icon'] = automatic_icon(client, provider)
    return client


def save_client_icon(user, client, icon):
    if not isinstance(client, str) or not isinstance(icon, str) or icon not in (*DEVICE_ICONS, 'auto'):
        raise ValueError('Icono o cliente inválido')
    auth.grant(user, client)
    provider = 'nintendo' if client.startswith('nintendo:') else 'adguard'
    devices = NINTENDO.devices().get('devices', []) if provider == 'nintendo' else api('clients')['clients']
    device = next((d for d in devices if d.get('key' if provider == 'nintendo' else 'name') == client), None)
    if device is None:
        raise ValueError('Cliente desconocido')
    identity = appearance_identity(device, provider)
    if icon == 'auto':
        DB.execute('DELETE FROM client_appearance WHERE identity=?', (identity,))
    else:
        DB.execute('INSERT OR REPLACE INTO client_appearance VALUES (?,?)', (identity, icon))
    DB.commit()
    auth.audit(user, 'client_icon', {'client': client, 'icon': icon})
    return {'ok': True, 'icon': None if icon == 'auto' else icon}


def demo_reset():
    global DEMO_CLIENTS, DEMO_GLOBAL
    defaults = dict(
        use_global_settings=True,
        filtering_enabled=True,
        parental_enabled=True,
        safebrowsing_enabled=True,
        safe_search={k: True for k in SAFE_KEYS},
        use_global_blocked_services=True,
        blocked_services=[],
        blocked_services_schedule={'time_zone': 'Local'},
        upstreams=[],
        tags=[],
        ignore_querylog=False,
        ignore_statistics=False,
        upstreams_cache_enabled=False,
        upstreams_cache_size=0,
    )
    DEMO_CLIENTS = [
        dict(copy.deepcopy(defaults), name='iMac de Emma', ids=['192.168.1.20']),
        dict(copy.deepcopy(defaults), name='iMac de Martín', ids=['192.168.1.21']),
    ]
    DEMO_GLOBAL = {
        'filtering_enabled': True,
        'parental_enabled': True,
        'safebrowsing_enabled': True,
        'safe_search': {k: True for k in SAFE_KEYS},
        'blocked_services': {'ids': ['youtube', 'tiktok'], 'schedule': {'time_zone': 'Local'}},
        'protection_enabled': True,
    }


demo_reset()


def server_config():
    saved = DB.execute("SELECT value FROM settings WHERE key='server'").fetchone()
    return (
        json.loads(saved[0])
        if saved
        else {
            'url': os.getenv('ADGUARD_URL', ''),
            'username': os.getenv('ADGUARD_USER', ''),
            'password': os.getenv('ADGUARD_PASSWORD', ''),
            'demo': os.getenv('DEMO', 'false').lower() == 'true',
        }
    )


def public_server():
    c = server_config()
    public = {k: v for k, v in c.items() if k not in ('password', 'replicas')}
    public['has_password'] = bool(c.get('password'))
    public['replicas'] = [
        {k: v for k, v in node.items() if k != 'password'} | {'has_password': bool(node.get('password'))}
        for node in c.get('replicas', [])
    ]
    return public


class AdGuardError(RuntimeError):
    def __init__(self, endpoint, category, message, status=None):
        self.detail = {
            'endpoint': endpoint.split('?')[0],
            'category': category,
            'message': message,
            'http_status': status,
        }
        super().__init__(message)


def diagnostic(endpoint, ok, started, error=None):
    item = {
        'time': time.time(),
        'endpoint': endpoint.split('?')[0],
        'ok': ok,
        'duration_ms': round((time.monotonic() - started) * 1000),
    }
    if error:
        item.update(error.detail)
    DIAGNOSTICS.append(item)
    if error:
        logging.warning(
            'AdGuard endpoint=%s category=%s status=%s: %s',
            item['endpoint'],
            item['category'],
            item.get('http_status'),
            item['message'],
        )


def network_error(endpoint, error, config=None):
    reason = getattr(error, 'reason', error)
    if isinstance(error, urllib.error.HTTPError):
        code = error.code
        message = {
            401: 'AdGuard rechaza el usuario o contraseña (HTTP 401).',
            403: 'Acceso denegado por AdGuard o su proxy (HTTP 403).',
            404: 'No se encuentra la API (HTTP 404). Comprueba URL, puerto y ruta del proxy.',
        }.get(code, 'AdGuard o su proxy devolvió HTTP ' + str(code) + '.')
        if code in (400, 409, 422):
            try:
                raw = error.read(4096).decode('utf-8', errors='replace').strip()
                try:
                    value = json.loads(raw)
                    detail = value.get('message') or value.get('error') if isinstance(value, dict) else ''
                except ValueError:
                    detail = raw if '<' not in raw else ''
                if isinstance(detail, str) and detail:
                    for key in ('password', 'username'):
                        secret = (config or {}).get(key)
                        if secret:
                            detail = detail.replace(secret, '[oculto]')
                    detail = re.sub(r'(?i)(password|token|secret|authorization)\s*[:=]\s*\S+', r'\1=[oculto]', detail)
                    message += ' ' + detail[:300]
            except Exception:
                pass
        return AdGuardError(endpoint, 'http', message, code)
    if isinstance(reason, ssl.SSLCertVerificationError):
        return AdGuardError(
            endpoint,
            'tls',
            'No se puede verificar el certificado HTTPS. Usa un certificado confiable o instala su CA en el contenedor.',
        )
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return AdGuardError(
            endpoint,
            'timeout',
            'La conexión ha superado 10 segundos. Comprueba red, IP, puerto y firewall desde el contenedor.',
        )
    if isinstance(reason, socket.gaierror):
        return AdGuardError(endpoint, 'dns', 'No se puede resolver el nombre del servidor desde el contenedor.')
    if isinstance(reason, ConnectionRefusedError):
        return AdGuardError(
            endpoint, 'connection_refused', 'El servidor rechaza la conexión. Comprueba el puerto web de AdGuard.'
        )
    return AdGuardError(
        endpoint,
        'network',
        'No se puede alcanzar el servidor desde el contenedor. Comprueba IP, puerto, red Docker y firewall.',
    )


def api(path, payload=None, method=None, config=None):
    if config is None and payload is None and globals().get('ADGUARDS') and ADGUARDS.enabled:
        return ADGUARDS.primary(path)
    cfg = config or server_config()
    if cfg.get('demo'):
        if path == 'clients':
            return {
                'clients': copy.deepcopy(DEMO_CLIENTS),
                'auto_clients': [{'name': 'Tablet invitada', 'ip': '192.168.1.50', 'source': 'rdns'}],
                'supported_tags': ['device_pc', 'device_phone', 'user_child'],
            }
        if path == 'status':
            return {'version': 'demo', 'protection_enabled': DEMO_GLOBAL['protection_enabled']}
        if path == 'blocked_services/all':
            return {
                'blocked_services': [
                    {'id': s, 'name': n}
                    for s, n in [
                        ('youtube', 'YouTube'),
                        ('tiktok', 'TikTok'),
                        ('twitch', 'Twitch'),
                        ('facebook', 'Facebook'),
                        ('instagram', 'Instagram'),
                        ('netflix', 'Netflix'),
                        ('discord', 'Discord'),
                    ]
                ]
            }
        if path == 'blocked_services/get':
            return copy.deepcopy(DEMO_GLOBAL['blocked_services'])
        if path == 'blocked_services/update':
            DEMO_GLOBAL['blocked_services'] = copy.deepcopy(payload)
            return {}
        if path == 'filtering/status':
            return {'enabled': DEMO_GLOBAL['filtering_enabled'], 'interval': 24}
        if path == 'filtering/config':
            DEMO_GLOBAL['filtering_enabled'] = payload['enabled']
            return {}
        if path == 'safesearch/status':
            return copy.deepcopy(DEMO_GLOBAL['safe_search'])
        if path == 'safesearch/settings':
            DEMO_GLOBAL['safe_search'] = copy.deepcopy(payload)
            return {}
        for category, field in [('parental', 'parental_enabled'), ('safebrowsing', 'safebrowsing_enabled')]:
            if path == category + '/status':
                return {'enabled': DEMO_GLOBAL[field]}
            if path in (category + '/enable', category + '/disable'):
                DEMO_GLOBAL[field] = path.endswith('/enable')
                return {}
        if path.startswith('querylog'):
            return {'data': []}
        if path == 'clients/update':
            for i, c in enumerate(DEMO_CLIENTS):
                if c['name'] == payload['name']:
                    DEMO_CLIENTS[i] = copy.deepcopy(payload['data'])
                    return {}
            raise ValueError('Cliente desconocido')
        if path == 'clients/add':
            DEMO_CLIENTS.append(copy.deepcopy(payload))
            return {}
        raise ValueError('Endpoint no simulado: ' + path)
    base = cfg['url'].rstrip('/')
    if not base:
        raise AdGuardError(path, 'configuration', 'Configura la URL del servidor en la pantalla Servidor.')
    url = base + ('/' if base.endswith('/control') else '/control/') + path
    credentials = base64.b64encode((cfg['username'] + ':' + cfg['password']).encode()).decode()
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode() if payload is not None else None,
        method=method or ('POST' if payload is not None else 'GET'),
        headers={'Authorization': 'Basic ' + credentials, 'Content-Type': 'application/json'},
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            raw = response.read()
            try:
                result = json.loads(raw) if raw else {}
            except (ValueError, UnicodeError):
                raise AdGuardError(
                    path,
                    'response',
                    'El servidor devuelve contenido que no es JSON. La URL puede apuntar a una página de login o al puerto equivocado.',
                )
        diagnostic(path, True, started)
        return result
    except AdGuardError as error:
        diagnostic(path, False, started, error)
        raise
    except (urllib.error.URLError, OSError) as error:
        safe = network_error(path, error, cfg)
        diagnostic(path, False, started, safe)
        raise safe from None


def global_config(config=None):
    fetch = (lambda path: api(path, config=config)) if config is not None else api
    return {
        'filtering_enabled': fetch('filtering/status')['enabled'],
        'parental_enabled': fetch('parental/status')['enabled'],
        'safebrowsing_enabled': fetch('safebrowsing/status')['enabled'],
        'safe_search': fetch('safesearch/status'),
        'blocked_services': fetch('blocked_services/get'),
        'protection_enabled': fetch('status')['protection_enabled'],
    }


def effective(client, global_values):
    result = {
        key: copy.deepcopy(client.get(key, False))
        for key in ('filtering_enabled', 'parental_enabled', 'safebrowsing_enabled')
    }
    result['safe_search'] = copy.deepcopy(
        client.get('safe_search', {'enabled': client.get('safesearch_enabled', False)})
    )
    if client.get('use_global_settings', True):
        for key in (*result.keys(),):
            result[key] = copy.deepcopy(global_values[key])
    inherited = client.get('use_global_blocked_services', True)
    result['blocked_services'] = copy.deepcopy(
        global_values['blocked_services']['ids'] if inherited else client.get('blocked_services', [])
    )
    result['blocked_services_schedule'] = copy.deepcopy(
        global_values['blocked_services'].get('schedule', {})
        if inherited
        else client.get('blocked_services_schedule', {})
    )
    result['protection_enabled'] = global_values['protection_enabled']
    return result


def baseline_for(name):
    if globals().get('ADGUARDS') and ADGUARDS.enabled:
        return ADGUARDS.baseline(name)
    row = DB.execute('SELECT config FROM baselines WHERE client=?', (name,)).fetchone()
    if not row:
        return None
    b = json.loads(row[0])
    if 'base' not in b:  # Upgrade leases created by the first release.
        c = next(c for c in api('clients')['clients'] if c['name'] == name)
        base = copy.deepcopy(c)
        base.update(
            blocked_services=b['blocked_services'], use_global_blocked_services=b['use_global_blocked_services']
        )
        b = {'base': base, 'applied': c}
    return b


def save_baseline(name, b):
    DB.execute('INSERT OR REPLACE INTO baselines VALUES(?,?)', (name, json.dumps(b)))
    DB.commit()


def event(client, service, domain='', kind='blocked', occurred=None):
    cursor = DB.execute(
        'INSERT INTO events(time,client,service,domain,kind) VALUES(?,?,?,?,?)',
        (time.time() if occurred is None else occurred, client, service, domain, kind),
    )
    DB.execute('DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 500)')
    DB.commit()
    return cursor.lastrowid


def overlay(current, baseline, active, globals_):
    updated = copy.deepcopy(current)
    base = baseline['base']
    for key in POLICY_KEYS:
        if key in base:
            updated[key] = copy.deepcopy(base[key])
        else:
            updated.pop(key, None)
    eff = effective(base, globals_)
    services = {s for s in active if not s.startswith('@')}
    if services:
        updated['use_global_blocked_services'] = False
        updated['blocked_services'] = sorted(set(eff['blocked_services']) - services)
        updated['blocked_services_schedule'] = eff['blocked_services_schedule']
    if any(s in RESTRICTIONS for s in active):
        updated['use_global_settings'] = False
        for key in ('filtering_enabled', 'parental_enabled', 'safebrowsing_enabled', 'safe_search'):
            updated[key] = copy.deepcopy(eff[key])
        for s in active:
            if s not in RESTRICTIONS:
                continue
            key = RESTRICTIONS[s]
            if key == 'safe_search':
                updated[key]['enabled'] = False
                updated['safesearch_enabled'] = False
            else:
                updated[key] = False
    return updated


def reconcile():
    if ADGUARDS.enabled:
        errors = []
        for action in (ADGUARDS.retry_changes, ADGUARDS.reconcile):
            try:
                action()
            except adguard_group.GroupError as error:
                errors.append(str(error))
        if errors:
            raise adguard_group.GroupError('; '.join(errors), ADGUARDS.health())
        return
    rows = DB.execute('SELECT client FROM baselines').fetchall()
    if not rows:
        return
    clients = {c['name']: c for c in api('clients')['clients']}
    globals_ = global_config()
    failures = []
    for (name,) in rows:
        try:
            if name not in clients:
                raise ValueError('Cliente eliminado o renombrado: ' + name)
            b = baseline_for(name)
            current = clients[name]
            # Adopt external policy edits instead of restoring a stale permanent setting.
            for key in POLICY_KEYS:
                if current.get(key) != b['applied'].get(key) and (
                    'previous' not in b or current.get(key) != b['previous'].get(key)
                ):
                    if key in current:
                        b['base'][key] = copy.deepcopy(current[key])
                    else:
                        b['base'].pop(key, None)
            active = [
                r[0] for r in DB.execute('SELECT service FROM leases WHERE client=? AND expires>?', (name, time.time()))
            ]
            updated = overlay(current, b, active, globals_)
            # Record expected result before the remote write: retries remain idempotent.
            previous = copy.deepcopy(b['applied'])
            b['applied'] = updated
            save_baseline(name, b)
            try:
                if updated != current:
                    api('clients/update', {'name': name, 'data': updated})
            except Exception:
                # A failed request may have succeeded remotely. Keep both versions as candidates.
                b['previous'] = previous
                save_baseline(name, b)
                raise
            b.pop('previous', None)
            save_baseline(name, b)
            if not active:
                DB.execute('DELETE FROM leases WHERE client=?', (name,))
                DB.execute('DELETE FROM baselines WHERE client=?', (name,))
                event(name, '', '', 'restored')
        except Exception as e:
            failures.append(str(e))
    DB.commit()
    if failures:
        raise RuntimeError('; '.join(failures))


def permit(name, service, minutes, require_blocked=False, granted_by=''):
    if ADGUARDS.enabled:
        return ADGUARDS.permit(name, service, minutes, require_blocked, granted_by)
    if type(minutes) is not int or not 1 <= minutes <= 1440:
        raise ValueError('Duración entre 1 y 1440 minutos')
    services = {s['id'] for s in api('blocked_services/all')['blocked_services']} | set(RESTRICTIONS)
    if service not in services:
        raise ValueError('Restricción desconocida')
    clients = {c['name']: c for c in api('clients')['clients']}
    if name not in clients:
        raise ValueError('Da de alta este cliente en AdGuard antes de crear un permiso')
    c = clients[name]
    if type(require_blocked) is not bool:
        raise ValueError('Origen del permiso inválido')
    if require_blocked:
        current = effective(c, global_config())
        active = DB.execute(
            'SELECT 1 FROM leases WHERE client=? AND service=? AND expires>?', (name, service, time.time())
        ).fetchone()
        blocked = (
            current['safe_search'].get('enabled', False)
            if service == '@safesearch'
            else current[RESTRICTIONS[service]] if service in RESTRICTIONS else service in current['blocked_services']
        )
        if active or not blocked:
            raise ValueError('La restricción ya no está activa o ya existe un permiso. Actualiza los datos.')
    b = baseline_for(name) or {'base': copy.deepcopy(c), 'applied': copy.deepcopy(c)}
    eff = effective(b['base'], global_config())
    enabled = (
        eff['safe_search'].get('enabled', False)
        if service == '@safesearch'
        else eff[RESTRICTIONS[service]] if service in RESTRICTIONS else service in eff['blocked_services']
    )
    if not enabled:
        raise ValueError('Esta restricción no está activada en la configuración base')
    save_baseline(name, b)
    DB.execute(
        'INSERT OR REPLACE INTO leases(client,service,expires,granted_by) VALUES(?,?,?,?)',
        (name, service, time.time() + minutes * 60, granted_by),
    )
    DB.commit()
    reconcile()


def validate_schedule(s):
    if not isinstance(s, dict) or set(s) - {'time_zone', 'sun', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat'}:
        raise ValueError('Horario inválido')
    zone = s.get('time_zone', 'Local')
    if zone != 'Local':
        try:
            ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError('Zona horaria inválida') from None
    for day, r in s.items():
        if day == 'time_zone':
            continue
        if not isinstance(r, dict) or set(r) - {'start', 'end'}:
            raise ValueError('Intervalo inválido')
        a, z = r.get('start'), r.get('end')
        if type(a) is not int or type(z) is not int or not 0 <= a < z <= 86400000 or a % 60000 or z % 60000:
            raise ValueError('Intervalo horario inválido')


def validate_client(c):
    if not isinstance(c, dict):
        raise ValueError('Configuración inválida')
    if not isinstance(c.get('name'), str) or not c['name'].strip():
        raise ValueError('Nombre obligatorio')
    if not c.get('ids'):
        raise ValueError('Indica al menos un identificador')
    for key in BOOL_KEYS:
        if key in c and type(c[key]) is not bool:
            raise ValueError('Valor inválido: ' + key)
    for key in ('ids', 'upstreams', 'tags', 'blocked_services'):
        if key in c and (not isinstance(c[key], list) or any(not isinstance(x, str) or not x for x in c[key])):
            raise ValueError('Lista inválida: ' + key)
    if 'upstreams_cache_size' in c and (type(c['upstreams_cache_size']) is not int or c['upstreams_cache_size'] < 0):
        raise ValueError('Caché inválida')
    if 'safe_search' in c:
        validate_safe(c['safe_search'])
    if 'blocked_services_schedule' in c:
        validate_schedule(c['blocked_services_schedule'])
    if 'blocked_services' in c:
        validate_services(c['blocked_services'])


def validate_safe(s):
    if not isinstance(s, dict) or set(s) - set(SAFE_KEYS) or any(type(v) is not bool for v in s.values()):
        raise ValueError('Búsqueda segura inválida')


def validate_services(ids):
    catalog = {s['id'] for s in api('blocked_services/all')['blocked_services']}
    if not isinstance(ids, list) or any(not isinstance(s, str) or s not in catalog for s in ids):
        raise ValueError('Servicio desconocido')


def save_client(name, patch, add=False):
    if not isinstance(patch, dict) or set(patch) - CLIENT_KEYS:
        raise ValueError('Campo de cliente no admitido')
    clients = api('clients')['clients']
    current = next((c for c in clients if c['name'] == name), None)
    if not add and not current:
        raise ValueError('Cliente desconocido')
    if ADGUARDS.enabled:
        target = copy.deepcopy((baseline_for(name) or {}).get('base', current) or {})
        target.update(patch)
        validate_client(target)
        return ADGUARDS.save_client(name, target if add else patch, add)
    b = baseline_for(name) if not add else None
    target = copy.deepcopy(b['base'] if b else current or {})
    target.update(patch)
    validate_client(target)
    if any(c['name'] == target['name'] and (add or c['name'] != name) for c in clients):
        raise ValueError('El nombre ya existe')
    if b and target['name'] != name:
        raise ValueError('Finaliza los permisos activos antes de renombrar el cliente')
    if b:
        nonpolicy = copy.deepcopy(current)
        for key, value in patch.items():
            if key not in POLICY_KEYS:
                nonpolicy[key] = copy.deepcopy(value)
        if nonpolicy != current:
            api('clients/update', {'name': name, 'data': nonpolicy})
        b['base'] = target
        save_baseline(name, b)
        reconcile()
    else:
        api('clients/add' if add else 'clients/update', target if add else {'name': name, 'data': target})


def save_global(patch):
    allowed = {'filtering_enabled', 'parental_enabled', 'safebrowsing_enabled', 'safe_search', 'blocked_services'}
    if not isinstance(patch, dict) or set(patch) - allowed:
        raise ValueError('Campo global no admitido')
    for key in ('filtering_enabled', 'parental_enabled', 'safebrowsing_enabled'):
        if key in patch and type(patch[key]) is not bool:
            raise ValueError('Valor global inválido')
    if 'safe_search' in patch:
        validate_safe(patch['safe_search'])
    if 'blocked_services' in patch:
        value = patch['blocked_services']
        if not isinstance(value, dict) or set(value) - {'ids', 'schedule'}:
            raise ValueError('Servicios globales inválidos')
        validate_services(value.get('ids'))
        validate_schedule(value.get('schedule', {}))
    if ADGUARDS.enabled:
        return ADGUARDS.save_global(patch)
    current = global_config()
    completed = []
    try:
        for key, value in patch.items():
            if value == current[key]:
                continue
            if key == 'filtering_enabled':
                api('filtering/config', {'enabled': value, 'interval': api('filtering/status')['interval']})
            elif key in ('parental_enabled', 'safebrowsing_enabled'):
                api(key.split('_')[0] + ('/enable' if value else '/disable'), {})
            elif key == 'safe_search':
                api('safesearch/settings', value, 'PUT')
            else:
                api('blocked_services/update', value, 'PUT')
            completed.append(key)
    except Exception as e:
        raise RuntimeError(
            'Guardado global incompleto. Recarga para revisar lo aplicado: ' + ', '.join(completed)
        ) from e
    reconcile()


def save_server(body, test_only=False):
    allowed = {'url', 'username', 'password', 'demo', 'name', 'replicas', 'sync_policy_ack'}
    if not isinstance(body, dict) or set(body) - allowed:
        raise ValueError('Servidor inválido')
    old = server_config()
    cfg = {key: body.get(key, old.get(key, '')) for key in ('url', 'username', 'password', 'demo')}
    if cfg['password'] == '' and old.get('password'):
        cfg['password'] = old['password']
    cfg['name'] = body.get('name', old.get('name', 'Principal'))
    cfg['sync_policy_ack'] = body.get('sync_policy_ack', old.get('sync_policy_ack', False))
    if type(cfg['demo']) is not bool or type(cfg['sync_policy_ack']) is not bool:
        raise ValueError('Servidor inválido')

    def validate(node):
        if (
            any(not isinstance(node.get(k), str) or len(node[k]) > 4096 for k in ('url', 'username', 'password'))
            or not isinstance(node.get('name'), str)
            or not node['name'].strip()
            or len(node['name']) > 60
        ):
            raise ValueError('Nombre, URL o credenciales del servidor inválidos')
        parsed = urlsplit(node['url'])
        if not cfg['demo'] and (
            parsed.scheme not in ('http', 'https')
            or not parsed.hostname
            or parsed.username
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError('Indica una URL HTTP/HTTPS sin credenciales incrustadas')
        if parsed.hostname:
            parsed.port

    validate(cfg)
    raw = body.get('replicas', old.get('replicas', []))
    if not isinstance(raw, list) or len(raw) > 3:
        raise ValueError('Se admiten hasta tres réplicas por grupo')
    saved = {n['id']: n for n in old.get('replicas', [])}
    replicas = []
    used = {'origin'}
    urls = {cfg['url'].rstrip('/')}
    for node in raw:
        if not isinstance(node, dict) or set(node) - {'id', 'name', 'url', 'username', 'password'}:
            raise ValueError('Réplica inválida')
        key = node.get('id') or 'node-' + uuid.uuid4().hex[:16]
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,48}', key) or key in used:
            raise ValueError('Identificador de réplica inválido o duplicado')
        used.add(key)
        previous = saved.get(key, {})
        result = {k: node.get(k, previous.get(k, '')) for k in ('name', 'url', 'username', 'password')}
        if result['password'] == '' and previous.get('password'):
            result['password'] = previous['password']
        result['id'] = key
        validate(result)
        if result['url'].rstrip('/') in urls:
            raise ValueError('Cada servidor debe tener una URL distinta')
        urls.add(result['url'].rstrip('/'))
        replicas.append(result)
    cfg['replicas'] = replicas
    if replicas and cfg['demo']:
        raise ValueError('Para conectar réplicas desactiva el modo demostración')
    if replicas and not test_only and not cfg['sync_policy_ack']:
        raise ValueError(
            'Confirma que adguard-sync no copia clientes, servicios ni ajustes generales que gestionará Parental'
        )
    active = (
        DB.execute('SELECT 1 FROM baselines').fetchone()
        or DB.execute('SELECT 1 FROM ag_baselines').fetchone()
        or ADGUARDS.pending()
    )
    topology = lambda config: [(n['id'], n['url'], n.get('demo', False)) for n in ADGUARDS.nodes(config)]
    if not test_only and active and (not old.get('replicas') or topology(old) != topology(cfg)):
        raise ValueError(
            'Finaliza los permisos y restauraciones pendientes antes de añadir, quitar o cambiar servidores'
        )
    versions = []
    failures = []
    for node in ADGUARDS.nodes(cfg):
        try:
            status = api('status', config=node)
            api('clients', config=node)
            if test_only:
                for endpoint in (
                    'filtering/status',
                    'parental/status',
                    'safebrowsing/status',
                    'safesearch/status',
                    'blocked_services/get',
                    'blocked_services/all',
                ):
                    api(endpoint, config=node)
            versions.append(node['name'] + ': ' + status.get('version', ''))
        except Exception as error:
            failures.append(node['name'] + ': ' + ADGUARDS.error(error))
    if failures:
        raise adguard_group.GroupError('Conexión no confirmada. ' + '; '.join(failures))
    if not test_only:
        DB.execute("INSERT OR REPLACE INTO settings VALUES('server',?)", (json.dumps(cfg),))
        if topology(old) != topology(cfg):
            DB.execute('DELETE FROM ag_cache')
            DB.execute('DELETE FROM ag_health')
            DB.execute('DELETE FROM ag_client_health')
        if old.get('url') != cfg['url']:
            DB.execute('DELETE FROM seen')
            DB.execute('DELETE FROM events')
            DB.execute('DELETE FROM ag_event_sources')
        DB.commit()
    return {'ok': True, 'version': '; '.join(versions), 'servers': ADGUARDS.health() if not test_only else []}


def client_identity(q, clients):
    info = q.get('client_info') or {}
    if info.get('name') in {c['name'] for c in clients}:
        return info['name']
    value = q.get('client_id') or q.get('client', '')
    for c in clients:
        if value in c.get('ids', []) or q.get('client', '') in c.get('ids', []):
            return c['name']
    matches = []
    for c in clients:
        for identifier in c.get('ids', []):
            try:
                network = ipaddress.ip_network(identifier, strict=False)
                if ipaddress.ip_address(q.get('client', '')) in network:
                    matches.append((network.prefixlen, c['name']))
            except ValueError:
                pass
    return max(matches)[1] if matches else value or 'Desconocido'


def poll(config=None, node_id=None, origin_clients=None):
    global LAST_POLL
    if config is None and ADGUARDS.enabled:
        return ADGUARDS.poll()
    fetch = (lambda path: api(path, config=config)) if config is not None else api
    clients = fetch('clients')['clients']
    reasons = {
        'FilteredSafeBrowsing': '@safebrowsing',
        'FilteredParental': '@parental',
        'FilteredSafeSearch': '@safesearch',
        'SafeSearch': '@safesearch',
        'FilteredBlackList': '@filtering',
    }
    data = fetch('querylog?limit=500')['data']
    counts = {}
    recognized = 0
    unknown = 0
    catalog = {item['id'] for item in fetch('blocked_services/all')['blocked_services']}
    for q in reversed(data):
        reason = q.get('reason', '')
        counts[reason] = counts.get(reason, 0) + 1
        service = (
            q.get('service_name') if reason in ('FilteredBlockedService', 'BlockedService') else reasons.get(reason)
        )
        if not isinstance(service, str) or not service:
            continue
        if not service.startswith('@') and service not in catalog:
            unknown += 1
            continue
        recognized += 1
        key = (
            q.get('time', '') + '|' + q.get('client', '') + '|' + q.get('question', {}).get('name', '') + '|' + service
        )
        query_key = key
        key = (node_id or 'origin') + '|' + key
        if DB.execute('SELECT 1 FROM seen WHERE key=?', (key,)).fetchone():
            continue
        try:
            occurred = datetime.fromisoformat(q['time'].replace('Z', '+00:00')).timestamp()
        except (ValueError, KeyError, AttributeError):
            unknown += 1
            continue
        if occurred > time.time() + 30:
            unknown += 1
            continue
        DB.execute('INSERT INTO seen VALUES(?,?)', (key, time.time()))
        client = client_identity(q, clients)
        if origin_clients is not None:
            local = next((c for c in clients if c['name'] == client), None)
            client = ADGUARDS.logical_name(local, origin_clients) if local else None
            if not client:
                unknown += 1
                continue
        recent = DB.execute(
            'SELECT id FROM events WHERE client=? AND service=? AND kind=? AND time BETWEEN ? AND ?',
            (client, service, 'blocked', occurred - 300, occurred + 300),
        ).fetchone()
        event_id = (
            recent[0] if recent else event(client, service, q.get('question', {}).get('name', ''), occurred=occurred)
        )
        ADGUARDS.observe(event_id, node_id or 'origin', query_key, occurred)
    DB.execute('DELETE FROM seen WHERE time<?', (time.time() - 86400,))
    DB.commit()
    LAST_POLL = {
        'status': 'ok',
        'time': time.time(),
        'queries': len(data),
        'recognized': recognized,
        'unknown': unknown,
        'reasons': counts,
        'window_full': len(data) >= 500,
        'excluded_clients': [c['name'] for c in clients if c.get('ignore_querylog')],
        'message': (
            'Ventana de 500 consultas completa; con mucho tráfico pueden faltar intentos entre lecturas.'
            if len(data) >= 500
            else 'Registro leído. Sin consultas de un dispositivo no se pueden detectar sus intentos.'
        ),
    }


def notification_settings(user):
    auth.require(user, 'admin', 'responsable')
    info = api('clients')['clients']
    clients = [c for c in info if user['role'] == 'admin' or c['name'] in user['clients']]
    result = blocked_alerts.preferences(user, {c['name'] for c in clients})
    result['clients'] = [{'name': c['name'], 'ignore_querylog': c.get('ignore_querylog', False)} for c in clients]
    result['services'] = api('blocked_services/all')['blocked_services']
    return result


def save_notification_settings(user, body):
    context = notification_settings(user)
    return blocked_alerts.save(
        user, body, {s['id'] for s in context['services']}, {c['name'] for c in context['clients']}
    )


def state():
    info = api('clients')
    g = global_config()
    bases = {c['name']: (baseline_for(c['name']) or {}).get('base', c) for c in info['clients']}
    return {
        'clients': info['clients'],
        'auto_clients': info.get('auto_clients', []),
        'supported_tags': info.get('supported_tags', []),
        'base_clients': bases,
        'adguard_clients': ADGUARDS.clients_status() if ADGUARDS.enabled else {},
        'adguard_servers': ADGUARDS.health() if ADGUARDS.enabled else [],
        'effective': {c['name']: effective(c, g) for c in info['clients']},
        'base_effective': {n: effective(c, g) for n, c in bases.items()},
        'services': api('blocked_services/all')['blocked_services'],
        'global_config': g,
        'leases': [
            dict(zip(('client', 'service', 'expires', 'granted_by'), r))
            for r in DB.execute('SELECT client,service,expires,granted_by FROM leases')
        ],
        'events': ADGUARDS.decorate_events(
            [
                dict(zip(('id', 'time', 'client', 'service', 'domain', 'kind'), r))
                for r in DB.execute('SELECT * FROM events ORDER BY id DESC LIMIT 100')
            ]
        ),
        'error': LAST_ERROR,
        'server': public_server(),
        'demo': server_config()['demo'],
    }


def scoped_state(user):
    data = state()
    allowed = {c['name'] for c in data['clients'] if user['role'] == 'admin' or c['name'] in user['clients']}
    data['clients'] = [c for c in data['clients'] if c['name'] in allowed]
    for client in data['clients']:
        attach_appearance(client)
    for key in ('base_clients', 'effective', 'base_effective'):
        data[key] = {k: v for k, v in data[key].items() if k in allowed}
    data['adguard_clients'] = {
        name: value for name, value in data.get('adguard_clients', {}).items() if name in allowed
    }
    if user['role'] != 'admin':
        data['adguard_servers'] = []
    data['leases'] = [l for l in data['leases'] if l['client'] in allowed]
    data['events'] = [e for e in data['events'] if e['client'] in allowed] if user['role'] != 'solicitante' else []
    if user['role'] != 'admin':
        data['server'] = {}
        data['auto_clients'] = []
        data['error'] = ''
        if user['role'] != 'responsable' or not user['edit_policy']:
            data['global_config'] = {'protection_enabled': data['global_config']['protection_enabled']}
            data['clients'] = [{k: c[k] for k in ('name', 'ids', 'ui_icon', 'ui_auto_icon')} for c in data['clients']]
            data['base_clients'] = {
                name: {
                    k: v
                    for k, v in c.items()
                    if k in ('name', 'ids', 'use_global_settings', 'use_global_blocked_services', 'ignore_querylog')
                }
                for name, c in data['base_clients'].items()
            }
    allowed_events = [
        dict(zip(('id', 'time', 'client', 'service', 'domain', 'kind'), row))
        for row in DB.execute('SELECT * FROM events ORDER BY id DESC LIMIT 500')
        if row[2] in allowed
    ]
    preferences = (
        blocked_alerts.preferences(user, allowed)
        if user['role'] in ('admin', 'responsable')
        else {'preferences': blocked_alerts.defaults(), 'updated': 0}
    )
    data['notification_events'] = blocked_alerts.eligible(user, allowed_events, preferences)
    data['notification_cursor'] = max((e['id'] for e in allowed_events), default=0)
    data['notification_revision'] = preferences['updated']
    data['requests'] = auth.requests_for(user)
    return data


def nintendo_state(user, force=False):
    data = dict(NINTENDO.devices(force=force))
    data['devices'] = [d for d in data.get('devices', []) if user['role'] == 'admin' or d['key'] in user['clients']]
    with LOCK:
        for device in data['devices']:
            attach_appearance(device, 'nintendo')
            for key in ('pending_operation', 'last_operation'):
                if device.get(key):
                    device[key] = dict(device[key], actor=operation_actor(device[key]['operation_id']))
    return data


def operation_actor(operation_id):
    """Name of the adult behind a Nintendo operation: direct ids embed the user, request ids the reviewer."""
    kind, _, rest = operation_id.partition(':')
    with auth.LOCK:
        if kind in ('direct', 'cancel', 'settings'):
            uid = rest.split(':', 1)[0]
            row = (
                auth.DB.execute('SELECT username FROM users WHERE id=?', (int(uid),)).fetchone()
                if uid.isdigit()
                else None
            )
        elif kind == 'request' and rest.isdigit():
            row = auth.DB.execute(
                'SELECT u.username FROM requests r JOIN users u ON u.id=r.reviewer WHERE r.id=?', (int(rest),)
            ).fetchone()
        else:
            row = None
    return row['username'] if row else ''


def device_catalog():
    devices = []
    errors = []
    try:
        devices.extend({'key': c['name'], 'name': c['name'], 'provider': 'adguard'} for c in api('clients')['clients'])
    except Exception:
        security.log_failure('Catálogo de dispositivos: lectura de AdGuard', logging.WARNING)
        errors.append('No se han podido leer los clientes de AdGuard')
    ns = NINTENDO.devices()
    devices.extend({'key': d['key'], 'name': d['name'], 'provider': 'nintendo'} for d in ns.get('devices', []))
    if ns.get('error'):
        errors.append(ns['error'])
    return {'devices': devices, 'errors': errors}


def operation_key(user, body, kind='direct'):
    value = body.get('operation_id', '')
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,80}', value):
        raise ValueError('Identificador de operación inválido. Recarga el panel.')
    return kind + ':' + str(user['id']) + ':' + value


def grant_access(client, service, minutes, operation_id=None, extend_bedtime=False, granted_by=''):
    if client.startswith('nintendo:'):
        if service != '@nintendo':
            raise ValueError('Servicio Nintendo inválido')
        if not operation_id:
            raise ValueError('Falta identificador de operación')
        return NINTENDO.grant(client, minutes, operation_id, extend_bedtime=extend_bedtime)
    if extend_bedtime:
        raise ValueError('La ampliación del horario de descanso solo se admite para Nintendo')
    permit(client, service, minutes, granted_by=granted_by)
    return {'status': 'confirmed', 'ok': True}


def refresh_nintendo_requests():
    NINTENDO.refresh_pending()
    with auth.LOCK:
        rows = auth.DB.execute(
            "SELECT id FROM requests WHERE client LIKE 'nintendo:%' AND status='approved_pending'"
        ).fetchall()
        for row in rows:
            op = NINTENDO.operation_status('request:' + str(row['id']))
            # A crash before the connector persisted its ledger must never
            # trigger an automatic replay of an additive Nintendo grant.
            status = (
                'approval_error'
                if not op
                else (
                    'approved'
                    if op.get('status') == 'confirmed'
                    else (
                        'approval_closed'
                        if op.get('status') == 'superseded'
                        else 'approval_error' if op.get('status') == 'failed' else None
                    )
                )
            )
            if status:
                auth.DB.execute('UPDATE requests SET status=? WHERE id=?', (status, row['id']))
                auth.audit(None, 'nintendo_request_synced', {'id': row['id'], 'status': status})
        auth.DB.commit()


def validate_request(client, service):
    if client.startswith('nintendo:'):
        if service != '@nintendo':
            raise ValueError('Servicio Nintendo inválido')
        return NINTENDO.validate(client, 5)
    clients = {c['name']: c for c in api('clients')['clients']}
    if client not in clients:
        raise ValueError('Cliente desconocido')
    base = (baseline_for(client) or {}).get('base', clients[client])
    eff = effective(base, global_config())
    enabled = (
        eff['safe_search'].get('enabled', False)
        if service == '@safesearch'
        else eff[RESTRICTIONS[service]] if service in RESTRICTIONS else service in eff['blocked_services']
    )
    if not enabled:
        raise ValueError('Esta restricción no está activada')


def worker():
    global LAST_ERROR
    while True:
        with LOCK:
            try:
                reconcile()
                LAST_ERROR = ''
            except AdGuardError as e:
                LAST_ERROR = str(e)
            except Exception:
                LAST_ERROR = 'Fallo al sincronizar AdGuard. Revisa el diagnóstico del servidor.'
                security.log_failure('Error interno de sincronización (sin datos de credenciales)')
        with LOCK:
            try:
                poll()
            except Exception:
                LAST_ERROR = LAST_ERROR or 'No se pudo leer toda la actividad de AdGuard. Revisa el diagnóstico.'
                security.log_failure('Registro de consultas de AdGuard', logging.WARNING)
        try:
            refresh_nintendo_requests()
        except Exception:
            security.log_failure('Nintendo: no se ha podido actualizar el estado de confirmación', logging.WARNING)
        try:
            with LOCK:
                events = [
                    dict(zip(('id', 'time', 'client', 'service', 'domain', 'kind'), row))
                    for row in DB.execute('SELECT * FROM events ORDER BY id DESC LIMIT 500')
                ]
            with LOCK:
                ADGUARDS.decorate_events(events)
            ALERTS.sync(events)
        except Exception:
            security.log_failure('Avisos: fallo de recogida; consulta el diagnóstico de avisos', logging.WARNING)
        time.sleep(10)


def push_worker():
    while True:
        try:
            ALERTS.dispatch()
        except Exception:
            security.log_failure('Avisos: fallo de envío Push (sin credenciales ni destinos)', logging.WARNING)
        time.sleep(5)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def end_headers(self):
        # Every response, including redirects and errors, refuses framing and foreign scripts.
        self.send_header('Content-Security-Policy', CSP)
        self.send_header('X-Frame-Options', 'DENY')
        super().end_headers()

    def respond(self, status, data, mime='application/json'):
        content = json.dumps(data, ensure_ascii=False).encode() if mime == 'application/json' else data
        try:
            self.send_response(status)
            self.send_header(
                'Content-Type',
                mime
                + (
                    '; charset=utf-8'
                    if mime.startswith('text/')
                    or mime
                    in ('application/json', 'application/manifest+json', 'application/javascript', 'image/svg+xml')
                    else ''
                ),
            )
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.end_headers()
            self.wfile.write(content)
        except (BrokenPipeError, ConnectionResetError):
            logging.info('El navegador o proxy cerró la solicitud antes de recibir la respuesta')

    def cookie_token(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get('Cookie', ''))
        except CookieError:
            return ''
        return cookie['session'].value if 'session' in cookie else ''

    def oidc_binding(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get('Cookie', ''))
        except CookieError:
            return ''
        return cookie[oidc.COOKIE].value if oidc.COOKIE in cookie else ''

    def oidc_redirect(self, target, binding=None, token=None, clear=False):
        self.send_response(303)
        self.send_header('Location', target)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        if binding is not None:
            self.send_header(
                'Set-Cookie', oidc.COOKIE + '=' + binding + '; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=600'
            )
        if clear:
            self.send_header('Set-Cookie', oidc.COOKIE + '=; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=0')
        if token:
            self.send_header(
                'Set-Cookie', 'session=' + token + '; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=43200'
            )
        self.end_headers()

    def oidc_start_response(self, url, binding):
        self.send_response(200)
        self.send_header(
            'Set-Cookie', oidc.COOKIE + '=' + binding + '; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=600'
        )
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(json.dumps({'url': url}).encode())

    def user(self):
        return auth.session(self.cookie_token())

    def session_response(self, token, csrf, user):
        self.send_response(200)
        secure = '; Secure' if self.headers.get('X-Forwarded-Proto', '').lower() == 'https' else ''
        self.send_header(
            'Set-Cookie', 'session=' + token + '; Path=/; HttpOnly; SameSite=Strict; Max-Age=43200' + secure
        )
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(json.dumps({'user': user, 'csrf': csrf}).encode())

    def body(self):
        length = int(self.headers.get('Content-Length', '0'))
        if not 0 < length <= 131072:
            raise ValueError('Solicitud inválida')
        result = json.loads(self.rfile.read(length))
        if not isinstance(result, dict):
            raise ValueError('Solicitud inválida')
        return result

    def do_GET(self):
        assets = {
            '/': ('index.html', 'text/html'),
            '/manifest.webmanifest': ('manifest.webmanifest', 'application/manifest+json'),
            '/sw.js': ('sw.js', 'application/javascript'),
            '/apple-touch-icon.png': ('apple-touch-icon.png', 'image/png'),
            '/favicon.png': ('favicon.png', 'image/png'),
            '/icon.svg': ('icon.svg', 'image/svg+xml'),
            '/icon-192.png': ('icon-192.png', 'image/png'),
            '/icon-512.png': ('icon-512.png', 'image/png'),
        }
        assets.update(STATIC_ASSETS)
        asset_path = urlsplit(self.path).path
        service = asset_path.removeprefix('/assets/services/').removesuffix('.svg')
        if asset_path == '/assets/services/' + service + '.svg' and service in SERVICE_ARTWORK:
            return self.respond(200, SERVICE_ARTWORK[service].encode(), 'image/svg+xml')
        if asset_path in assets:
            file, mime = assets[asset_path]
            return self.respond(200, (ROOT / file).read_bytes(), mime)
        if self.path == '/api/auth/status':
            return self.respond(
                200, {'configured': auth.configured(), 'authentik': auth.configured() and AUTHENTIK.login_enabled()}
            )
        if asset_path in ('/api/auth/oidc/start', oidc.CALLBACK):
            try:
                query = parse_qs(urlsplit(self.path).query)
                if asset_path == '/api/auth/oidc/start':
                    url, binding = AUTHENTIK.begin(query.get('target', ['/'])[0], ip=self.client_address[0])
                    return self.oidc_redirect(url, binding=binding)
                if any(len(values) != 1 for values in query.values()) or query.get('error'):
                    raise oidc.OIDCError('Authentik canceló el acceso o devolvió una respuesta inválida')
                result = AUTHENTIK.callback(
                    query.get('state', [''])[0], query.get('code', [''])[0], self.oidc_binding()
                )
                return self.oidc_redirect(result['target'], token=result.get('token'), clear=not result['link'])
            except (oidc.OIDCError, auth.Forbidden) as error:
                import html

                content = (
                    '<!doctype html><html lang="es"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Parental · Authentik</title><link rel="stylesheet" href="/assets/parental.css"><main style="max-width:560px;margin:48px auto;padding:24px"><h1>No se pudo completar el acceso</h1><p>'
                    + html.escape(str(error))
                    + '</p><a href="/">Volver a Parental</a></main></html>'
                ).encode()
                return self.respond(400, content, 'text/html')
            except Exception:
                security.log_failure('Authentik: fallo interno al completar el acceso')
                return self.respond(
                    502, {'error': 'No se pudo completar Authentik. Vuelve a Parental e inicia de nuevo'}
                )
        user = self.user()
        if not user:
            return self.respond(401, {'error': 'Inicia sesión para continuar'})
        try:
            if self.path == '/api/me':
                return self.respond(200, {'user': user, 'csrf': user['csrf']})
            if asset_path == '/api/me/alerts':
                query = parse_qs(urlsplit(self.path).query)
                return self.respond(200, ALERTS.snapshot(user, query.get('device', [None])[0]))
            if self.path == '/api/me/notifications':
                with LOCK:
                    return self.respond(200, notification_settings(user))
            if self.path == '/api/me/authentik':
                return self.respond(200, AUTHENTIK.status(user, self.cookie_token(), self.oidc_binding()))
            if self.path == '/api/authentik/config':
                auth.require(user, 'admin')
                return self.respond(200, AUTHENTIK.public_config())
            if self.path in ('/api/diagnostics', '/api/server', '/api/users', '/api/audit'):
                auth.require(user, 'admin')
                if self.path == '/api/diagnostics':
                    return self.respond(
                        200, {'entries': list(DIAGNOSTICS), 'last_error': LAST_ERROR, 'querylog': LAST_POLL}
                    )
                if self.path == '/api/server':
                    return self.respond(200, public_server())
                if self.path == '/api/users':
                    linked = {r['user_id'] for r in auth.DB.execute('SELECT user_id FROM external_identities')}
                    pending = auth.pending_signups()
                    return self.respond(
                        200,
                        {
                            'users': [
                                dict(
                                    auth.public(r),
                                    authentik_linked=r['id'] in linked,
                                    signup_pending=r['id'] in pending,
                                    signup_created=pending.get(r['id']),
                                    has_password=bool(r['password']),
                                )
                                for r in auth.DB.execute('SELECT * FROM users')
                            ]
                        },
                    )
                if self.path == '/api/audit':
                    return self.respond(
                        200,
                        {
                            'entries': [
                                dict(r)
                                for r in auth.DB.execute(
                                    'SELECT a.*,u.username FROM audit a LEFT JOIN users u ON a.user_id=u.id ORDER BY a.id DESC LIMIT 200'
                                )
                            ]
                        },
                    )
            if self.path == '/api/requests':
                return self.respond(200, {'requests': auth.requests_for(user)})
            if self.path == '/api/nintendo/config':
                auth.require(user, 'admin')
                return self.respond(200, NINTENDO.public_config())
            if self.path in ('/api/nintendo/state', '/api/nintendo/state?refresh=1'):
                return self.respond(200, nintendo_state(user, force=self.path.endswith('?refresh=1')))
            if self.path == '/api/devices':
                auth.require(user, 'admin')
                return self.respond(200, device_catalog())
            if self.path == '/api/state':
                with LOCK:
                    return self.respond(200, scoped_state(user))
            self.respond(404, {'error': 'No encontrado'})
        except nintendo.NintendoError as e:
            self.respond(502, {'error': str(e)})
        except oidc.OIDCError as e:
            self.respond(400, {'error': str(e)})
        except auth.Forbidden as e:
            self.respond(403, {'error': str(e)})
        except AdGuardError as e:
            self.respond(502, {'error': str(e), 'diagnostic': e.detail})
        except Exception:
            security.log_failure('GET ' + urlsplit(self.path).path)
            self.respond(502, {'error': 'No se puede leer AdGuard. Revisa el diagnóstico en Servidor.'})

    def do_POST(self):
        try:
            body = self.body()
            if self.path == '/api/auth/setup':
                auth.bootstrap(body.get('token', ''), TOKEN, body['username'], body['password'])
                token, csrf, user = auth.login(body['username'], body['password'], self.client_address[0])
                return self.session_response(token, csrf, user)
            if self.path == '/api/auth/login':
                token, csrf, user = auth.login(body['username'], body['password'], self.client_address[0])
                return self.session_response(token, csrf, user)
            user = self.user()
            if not user:
                return self.respond(401, {'error': 'Inicia sesión para continuar'})
            if not hmac.compare_digest(self.headers.get('X-CSRF-Token', ''), user['csrf']):
                raise auth.Forbidden('Sesión inválida. Vuelve a entrar')
            if self.path == '/api/me/alerts/device':
                return self.respond(200, ALERTS.register(user, body))
            if self.path == '/api/me/alerts/disconnect':
                return self.respond(200, ALERTS.disconnect(user, body.get('device')))
            if self.path == '/api/me/alerts/read':
                return self.respond(200, ALERTS.read(user, body))
            if self.path == '/api/me/alerts/result':
                return self.respond(200, ALERTS.delivery(user, body))
            if self.path == '/api/me/alerts/test':
                return self.respond(200, ALERTS.test(user, body))
            if self.path == '/api/me/notifications':
                with LOCK:
                    return self.respond(200, save_notification_settings(user, body))
            if self.path == '/api/me/authentik/start':
                url, binding = AUTHENTIK.begin(
                    '/?view=settings', user, self.cookie_token(), body.get('password'), self.client_address[0]
                )
                return self.oidc_start_response(url, binding)
            if self.path in ('/api/me/authentik/confirm', '/api/me/authentik/cancel'):
                return self.respond(
                    200,
                    AUTHENTIK.confirm(
                        user,
                        body.get('confirmation'),
                        self.cookie_token(),
                        self.oidc_binding(),
                        cancel=self.path.endswith('/cancel'),
                    ),
                )
            if self.path == '/api/me/authentik/unlink':
                return self.respond(200, AUTHENTIK.unlink(user, body, self.client_address[0]))
            if self.path == '/api/authentik/config':
                return self.respond(200, AUTHENTIK.save(user, body, self.client_address[0]))
            if self.path == '/api/authentik/test':
                return self.respond(200, AUTHENTIK.test(user))
            if self.path == '/api/auth/logout':
                if body.get('device'):
                    ALERTS.disconnect(user, body['device'])
                auth.logout(self.cookie_token())
                return self.respond(200, {'ok': True})
            if self.path == '/api/me/avatar':
                if set(body) - {'avatar'}:
                    raise ValueError('Solo puedes cambiar tu cara desde este formulario')
                return self.respond(200, {'user': auth.save_avatar(user, body)})
            if self.path == '/api/users/avatar':
                auth.require(user, 'admin')
                return self.respond(200, {'user': auth.save_avatar(user, body)})
            if self.path == '/api/client/icon':
                with LOCK:
                    return self.respond(200, save_client_icon(user, body['client'], body['icon']))
            if self.path == '/api/nintendo/operation/close':
                auth.grant(user, body['client'])
                if not str(body['client']).startswith('nintendo:'):
                    raise ValueError('Consola Nintendo inválida')
                result = NINTENDO.close_tracking(
                    body['client'],
                    body['operation_id'],
                    acknowledge_uncertain=body.get('acknowledge_uncertain', False),
                    expected_daily_extra_minutes=body['expected_daily_extra_minutes'],
                    expected_bedtime=body['expected_bedtime'],
                )
                auth.audit(
                    user,
                    'nintendo_tracking_closed',
                    {'client': body['client'], 'operation_id': body['operation_id'], 'status': result['status']},
                )
                return self.respond(200, result)
            if self.path == '/api/nintendo/policy':
                auth.grant(user, body['client'], edit=True)
                op = operation_key(user, body, 'settings')
                result = NINTENDO.save_policy(body['client'], body['patch'], body['revision'], op)
                auth.audit(
                    user, 'nintendo_policy', {'client': body['client'], 'operation_id': op, 'status': result['status']}
                )
                return self.respond(200, result)
            if self.path.startswith('/api/nintendo/'):
                auth.require(user, 'admin')
                if self.path == '/api/nintendo/login/begin':
                    return self.respond(200, NINTENDO.begin_login())
                if self.path == '/api/nintendo/login/complete':
                    result = NINTENDO.complete_login(
                        body['state_id'], body['response_url'], body.get('timezone', 'Europe/Madrid')
                    )
                    auth.audit(user, 'nintendo_connected', {})
                    return self.respond(200, result)
                if self.path == '/api/nintendo/disconnect':
                    NINTENDO.disconnect()
                    auth.audit(user, 'nintendo_disconnected', {})
                    return self.respond(200, {'ok': True})
                return self.respond(404, {'error': 'No encontrado'})
            if self.path == '/api/users':
                return self.respond(200, {'user': auth.save_user(user, body)})
            if self.path == '/api/users/signup/reject':
                return self.respond(200, auth.reject_signup(user, body))
            if self.path in ('/api/global', '/api/server', '/api/server/test', '/api/client/add'):
                auth.require(user, 'admin')
            if self.path == '/api/server/test':
                return self.respond(200, save_server(body, True))
            if self.path == '/api/request/withdraw':
                auth.withdraw(user, body['id'])
                return self.respond(200, {'ok': True})
            if self.path == '/api/permit':
                auth.grant(user, body['client'], body['minutes'])
                auth.bedtime_option(body, body['client'])
            if self.path == '/api/cancel':
                auth.grant(user, body['client'])
            if self.path == '/api/client':
                auth.grant(user, body['client'], edit=True)
            if self.path == '/api/request':
                auth.require(user, 'solicitante')
                auth.scoped(user, body['client'])
            if self.path == '/api/request/review':
                auth.require(user, 'admin', 'responsable')
                if type(body.get('id')) is not int or body['id'] <= 0:
                    raise ValueError('Identificador de solicitud inválido')
            if self.path == '/api/request' and str(body.get('client', '')).startswith('nintendo:'):
                if type(body.get('minutes')) is not int or body['minutes'] not in (5, 10, 15, 20, 25, 30, 40, 60):
                    raise ValueError('Selecciona 5, 10, 15, 20, 25, 30, 40 o 60 minutos de tiempo extra')
                return self.respond(200, {'ok': True, 'id': auth.request_access(user, body, validate_request)})
            if self.path == '/api/request/review':
                with auth.LOCK:
                    requested = auth.DB.execute('SELECT client FROM requests WHERE id=?', (body['id'],)).fetchone()
                if requested and requested['client'].startswith('nintendo:'):
                    result = auth.review(
                        user,
                        body,
                        lambda c, s, m, extend_bedtime=False: grant_access(
                            c,
                            s,
                            m,
                            'request:' + str(body['id']),
                            extend_bedtime=extend_bedtime,
                            granted_by=user['username'],
                        ),
                    ) or {'ok': True}
                    if result.get('status') == 'failed':
                        return self.respond(
                            409, dict(result, error=result.get('message', 'Nintendo ha rechazado el cambio'))
                        )
                    return self.respond(200, result)
            if self.path in ('/api/permit', '/api/cancel') and str(body.get('client', '')).startswith('nintendo:'):
                if body.get('service') != '@nintendo':
                    raise ValueError('Servicio Nintendo inválido')
                op = operation_key(user, body, 'cancel' if self.path == '/api/cancel' else 'direct')
                extend_bedtime = auth.bedtime_option(body, body['client']) if self.path == '/api/permit' else False
                cancel_options = {}
                if self.path == '/api/cancel':
                    cancel_all = body.get('cancel_all_today', False)
                    if type(cancel_all) is not bool:
                        raise ValueError('Confirmación de retirada inválida')
                    if cancel_all:
                        if 'expected_extra_minutes' not in body or 'expected_bedtime' not in body:
                            raise ValueError('Actualiza el tiempo extra antes de confirmar la retirada')
                        cancel_options = {
                            'cancel_all_today': True,
                            'expected_extra_minutes': body['expected_extra_minutes'],
                            'expected_bedtime': body['expected_bedtime'],
                        }
                auth.audit(
                    user,
                    'nintendo_operation_requested',
                    {
                        'client': body['client'],
                        'minutes': body.get('minutes'),
                        'operation_id': op,
                        'kind': self.path,
                        'extend_bedtime': extend_bedtime,
                        **cancel_options,
                    },
                )
                result = (
                    NINTENDO.grant(body['client'], body['minutes'], op, extend_bedtime=extend_bedtime)
                    if self.path == '/api/permit'
                    else NINTENDO.cancel(body['client'], op, **cancel_options)
                )
                auth.audit(user, 'nintendo_operation_result', {'operation_id': op, 'status': result['status']})
                if result.get('status') == 'failed':
                    return self.respond(
                        409, dict(result, error=result.get('message', 'Nintendo ha rechazado el cambio'))
                    )
                return self.respond(200, result)
            with LOCK:
                result = {'ok': True}
                if self.path == '/api/permit':
                    permit(
                        body['client'],
                        body['service'],
                        body['minutes'],
                        body.get('from_blocked_event', False),
                        granted_by=user['username'],
                    )
                elif self.path == '/api/cancel':
                    DB.execute(
                        'UPDATE leases SET expires=0 WHERE client=? AND service=?', (body['client'], body['service'])
                    )
                    DB.commit()
                    reconcile()
                elif self.path == '/api/client':
                    save_client(body['client'], body['patch'])
                elif self.path == '/api/client/add':
                    save_client('', body['patch'], True)
                elif self.path == '/api/global':
                    save_global(body['patch'])
                elif self.path == '/api/server':
                    result = save_server(body)
                elif self.path == '/api/request':
                    if str(body.get('client', '')).startswith('nintendo:'):
                        if type(body.get('minutes')) is not int or body['minutes'] not in (
                            5,
                            10,
                            15,
                            20,
                            25,
                            30,
                            40,
                            60,
                        ):
                            raise ValueError('Selecciona 5, 10, 15, 20, 25, 30, 40 o 60 minutos de tiempo extra')
                    result = {'ok': True, 'id': auth.request_access(user, body, validate_request)}
                elif self.path == '/api/request/review':
                    result = auth.review(
                        user,
                        body,
                        lambda c, s, m, extend_bedtime=False: grant_access(
                            c,
                            s,
                            m,
                            'request:' + str(body['id']),
                            extend_bedtime=extend_bedtime,
                            granted_by=user['username'],
                        ),
                    ) or {'ok': True}
                else:
                    return self.respond(404, {'error': 'No encontrado'})
                if self.path not in ('/api/request', '/api/request/review'):
                    auth.audit(
                        user,
                        self.path,
                        {'client': body.get('client'), 'service': body.get('service'), 'minutes': body.get('minutes')},
                    )
                if result.get('status') == 'failed':
                    return self.respond(
                        409, dict(result, error=result.get('message', 'Nintendo ha rechazado el cambio'))
                    )
                self.respond(200, result)
        except adguard_group.GroupError as e:
            if user['role'] == 'admin':
                self.respond(502, {'error': str(e), 'servers': e.states})
            else:
                message = 'No se confirmó el cambio en todos los AdGuard.'
                self.respond(502, {'error': message + ' Pide al administrador que revise el grupo.'})
        except nintendo.NintendoError as e:
            data = {'error': str(e)}
            if self.path in ('/api/permit', '/api/cancel', '/api/nintendo/policy') and 'op' in locals():
                try:
                    operation = NINTENDO.operation_status(op)
                    data['operation_started'] = operation is not None
                    if operation:
                        data['operation'] = operation
                except Exception:
                    pass
            self.respond(502, data)
        except auth.Forbidden as e:
            self.respond(403, {'error': str(e)})
        except AdGuardError as e:
            self.respond(502, {'error': str(e), 'diagnostic': e.detail})
        except (ValueError, KeyError, TypeError) as e:
            self.respond(400, {'error': str(e)})
        except Exception:
            security.log_failure('POST ' + urlsplit(self.path).path)
            self.respond(
                502, {'error': 'La operación no se pudo completar. Revisa solicitudes y permisos antes de reintentar.'}
            )


ADGUARDS = adguard_group.Group(sys.modules[__name__])


if __name__ == '__main__':
    if len(TOKEN) < 16:
        raise SystemExit('Configura APP_TOKEN con al menos 16 caracteres')
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=push_worker, daemon=True).start()
    ThreadingHTTPServer(('0.0.0.0', 8080), Handler).serve_forever()

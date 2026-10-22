"""End-to-end HTTP smoke test for CI, using a local simulated AdGuard."""
import http.cookiejar, json, os, secrets, subprocess, tempfile, time, urllib.error, urllib.request
from pathlib import Path

initial=secrets.token_urlsafe(32)
env=dict(os.environ,DATA_DIR=tempfile.mkdtemp(),DEMO='true',APP_TOKEN=initial)
# Keep the HTTP/session/CSRF layer real; simulate only the remote Nintendo API.
launcher="""import threading
from datetime import datetime
from zoneinfo import ZoneInfo
from http.server import ThreadingHTTPServer
import app, nintendo
from test_nintendo import FakeBackend
app.NINTENDO.close()
original_permit=app.permit
def scoped_group_failure(name,service,minutes,*args,**kwargs):
    if minutes==19:
        import adguard_group
        raise adguard_group.GroupError('private-other-client', [{'client':'private-other-client','querylog':{'excluded_clients':['private-other-client']}}])
    return original_permit(name,service,minutes,*args,**kwargs)
app.permit=scoped_group_failure
fixed_time=datetime(2026,9,4,18,0,tzinfo=ZoneInfo('Europe/Madrid')).timestamp()
def fake_backend():
    backend=FakeBackend()
    backend.mode='next_step'
    original_confirm=backend.confirm
    async def confirm(device_id,minutes,**kwargs):
        result=await original_confirm(device_id,minutes,**kwargs)
        if minutes==15 and backend.extra==75:raise TimeoutError('private-test-token')
        return result
    backend.confirm=confirm
    return backend
app.NINTENDO=nintendo.Connector(app.DATA,backend_factory=fake_backend,clock=lambda:fixed_time)
threading.Thread(target=app.worker,daemon=True).start()
ThreadingHTTPServer(('127.0.0.1',8080),app.Handler).serve_forever()
"""
process=subprocess.Popen(['python','-c',launcher],env=env)

class Client:
    def __init__(self): self.opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()));self.csrf=''
    def call(self,path,body=None,expected=200):
        headers={'Content-Type':'application/json','X-CSRF-Token':self.csrf}
        req=urllib.request.Request('http://127.0.0.1:8080/api/'+path,data=json.dumps(body).encode() if body is not None else None,headers=headers)
        try:
            with self.opener.open(req,timeout=5) as response: code=response.status;data=json.load(response)
        except urllib.error.HTTPError as e:code=e.code;data=json.load(e)
        assert code==expected,(path,code,data)
        if 'csrf' in data:self.csrf=data['csrf']
        return data

try:
    admin=Client()
    for _ in range(50):
        try:admin.call('auth/status');break
        except OSError:time.sleep(.1)
    else:raise RuntimeError('App did not start')
    admin.call('auth/setup',{'token':initial,'username':'admin','password':'admin-password-1234'})
    for name,role in [('emma','solicitante'),('parent','responsable'),('observer','observador')]:admin.call('users',{'username':name,'password':name+'-password-1234','role':role,'clients':['iMac de Emma'],'max_minutes':60})
    child=Client();child.call('auth/login',{'username':'emma','password':'emma-password-1234'})
    changed=child.call('me/avatar',{'avatar':'face-08'})
    assert changed['user']['avatar']=='face-08' and changed['user']['role']=='solicitante'
    assert child.call('me')['user']['avatar']=='face-08'
    child.call('me/avatar',{'avatar':'face-99'},400)
    child.call('me/avatar',{'avatar':'face-08','role':'admin'},400)
    child.call('users/avatar',{'user_id':1,'avatar':'face-04'},403)
    state=child.call('state');assert [c['name'] for c in state['clients']]==['iMac de Emma'];assert state['events']==[];assert state['server']=={}
    child.call('permit',{'client':'iMac de Emma','service':'youtube','minutes':20},403)
    child.call('global',{'patch':{}},403)
    child.call('request',{'client':'iMac de Martín','service':'youtube','minutes':20},403)
    rid=child.call('request',{'client':'iMac de Emma','service':'youtube','minutes':20,'reason':'Vídeo de clase'})['id']
    assert admin.call('state')['leases']==[]
    parent=Client();parent.call('auth/login',{'username':'parent','password':'parent-password-1234'})
    private_error=parent.call('permit',{'client':'iMac de Emma','service':'youtube','minutes':19},502)
    assert 'private-other-client' not in json.dumps(private_error) and 'servers' not in private_error
    admin_error=admin.call('permit',{'client':'iMac de Emma','service':'youtube','minutes':19},502)
    assert admin_error['servers'][0]['client']=='private-other-client'
    alert_device='http-device-1234567890'
    admin.call('me/alerts/device',{'device':alert_device})
    assert admin.call('me/alerts?device='+alert_device)['diagnostic']['registered']
    parent.call('me/alerts/device',{'device':alert_device},403)
    child.call('me/alerts/read',{'id':999999},403)
    parent.call('me/alerts/device',{'device':'other-device-1234567890','subscription':{'endpoint':'https://127.0.0.1/private','keys':{}}},400)
    admin.csrf='invalid';admin.call('me/alerts/read',{'id':None},403);admin.call('me')
    admin.call('me/alerts/disconnect',{'device':alert_device})
    assert not admin.call('me/alerts?device='+alert_device)['diagnostic']['registered']
    prefs=parent.call('me/notifications');assert [c['name'] for c in prefs['clients']]==['iMac de Emma']
    config=prefs['preferences'];config['mode']='selected';config['services']=['youtube']
    parent.call('me/notifications',config)
    assert parent.call('me/notifications')['preferences']['services']==['youtube']
    assert admin.call('me/notifications')['preferences']['mode']=='all'
    child.call('me/notifications',expected=403);child.call('me/notifications',config,403)
    parent.call('me/notifications',dict(config,clients={'iMac de Martín':{'mode':'all','services':[],'protections':[]}}),403)
    parent.call('me/notifications',dict(config,services=['unknown']),400)
    parent.csrf='invalid';parent.call('me/notifications',config,403);parent.call('me')
    parent.call('request/review',{'id':rid,'decision':'approve','minutes':15})
    granted=child.call('state');assert len(granted['leases'])==1;assert granted['requests'][0]['status']=='approved'
    assert granted['leases'][0]['granted_by']=='parent' and granted['requests'][0]['reviewer_name']=='parent'
    observer=Client();observer.call('auth/login',{'username':'observer','password':'observer-password-1234'})
    observer.call('cancel',{'client':'iMac de Emma','service':'youtube'},403)
    parent.call('client',{'client':'iMac de Emma','patch':{'parental_enabled':False}},403)
    admin.call('client',{'client':'iMac de Martín','patch':{'use_global_blocked_services':False,'blocked_services':['youtube'],'blocked_services_schedule':{'time_zone':'Local'}}})
    persisted=admin.call('state')
    assert persisted['base_clients']['iMac de Martín']['blocked_services']==['youtube']
    assert not persisted['base_clients']['iMac de Martín']['use_global_blocked_services']
    assert persisted['effective']['iMac de Martín']['blocked_services']==['youtube']
    admin.call('client',{'client':'iMac de Martín','patch':{'blocked_services':['unknown-service']}},400)
    child.call('client/icon',{'client':'iMac de Emma','icon':'tv'},403)
    observer.call('client/icon',{'client':'iMac de Emma','icon':'tv'},403)
    parent.call('client/icon',{'client':'iMac de Martín','icon':'tv'},403)
    parent.call('client/icon',{'client':'iMac de Emma','icon':'tv'})
    assert child.call('state')['clients'][0]['ui_icon']=='tv'
    parent.call('client/icon',{'client':'iMac de Emma','icon':'auto'})
    assert child.call('state')['clients'][0]['ui_icon'] is None
    child.call('nintendo/login/begin',{},403)
    login=admin.call('nintendo/login/begin',{})
    connected=admin.call('nintendo/login/complete',{'state_id':login['state_id'],'response_url':'npf54789bef://auth#state=oauth-state&session_token_code=private-test-code','timezone':'Europe/Madrid'})
    assert connected['configured'] and connected['devices'][0]['key']=='nintendo:ABC'
    assert 'private-test-code' not in json.dumps(connected)
    child_user=next(u for u in admin.call('users')['users'] if u['username']=='emma')
    parent_user=next(u for u in admin.call('users')['users'] if u['username']=='parent')
    for user in (child_user,parent_user):admin.call('users',dict(user,clients=['iMac de Emma','nintendo:ABC']))
    child.call('auth/login',{'username':'emma','password':'emma-password-1234'})
    parent.call('auth/login',{'username':'parent','password':'parent-password-1234'})
    assert [d['key'] for d in child.call('nintendo/state')['devices']]==['nintendo:ABC']
    child.call('permit',{'client':'nintendo:ABC','service':'@nintendo','minutes':20,'extend_bedtime':True,'operation_id':secrets.token_hex(16)},403)
    native_id=child.call('request',{'client':'nintendo:ABC','service':'@nintendo','minutes':20,'extend_bedtime':True})['id']
    requested=next(r for r in parent.call('requests')['requests'] if r['id']==native_id)
    assert requested['status']=='pending' and requested['extend_bedtime']
    parent.call('request/review',{'id':native_id,'decision':'approve','minutes':20,'extend_bedtime':False})
    approved=next(r for r in child.call('requests')['requests'] if r['id']==native_id)
    assert approved['status']=='approved' and not approved['approved_extend_bedtime']
    state=child.call('nintendo/state?refresh=1')
    assert state['devices'][0]['extra_minutes']==20
    assert state['devices'][0]['effective_bedtime']=='21:00'
    assert state['devices'][0]['last_operation']['confirmation_with_bedtime'] is False
    assert state['devices'][0]['last_operation']['actor']=='parent'
    assert next(r for r in child.call('requests')['requests'] if r['id']==native_id)['reviewer_name']=='parent'
    child.call('nintendo/policy',{'client':'nintendo:ABC','patch':{},'revision':'rev','operation_id':secrets.token_hex(16)},403)
    parent.call('nintendo/policy',{'client':'nintendo:ABC','patch':{},'revision':'rev','operation_id':secrets.token_hex(16)},403)
    forty_id=child.call('request',{'client':'nintendo:ABC','service':'@nintendo','minutes':40})['id']
    partial=parent.call('request/review',{'id':forty_id,'decision':'approve','minutes':40})
    assert partial['status']=='pending' and partial['confirmed_minutes']==30
    for _ in range(120):
        progress=child.call('nintendo/state?refresh=1')['devices'][0]['last_operation']
        if progress['status']=='confirmed':break
        time.sleep(.25)
    else:raise AssertionError('Composed 40-minute approval never finished')
    assert progress['confirmed_minutes']==40
    approved=next(r for r in child.call('requests')['requests'] if r['id']==forty_id)
    assert approved['status']=='approved' and approved['approved_minutes']==40
    assert child.call('nintendo/state')['devices'][0]['extra_minutes']==60
    assert child.call('nintendo/state')['devices'][0]['effective_bedtime']=='21:00'
    uncertain=parent.call('permit',{'client':'nintendo:ABC','service':'@nintendo','minutes':15,'operation_id':secrets.token_hex(16)})
    assert uncertain['status']=='pending' and not uncertain['last_step_acknowledged']
    close_body={'client':'nintendo:ABC','operation_id':uncertain['operation_id'],'acknowledge_uncertain':True,'expected_daily_extra_minutes':75,'expected_bedtime':'21:00'}
    child.call('nintendo/operation/close',close_body,403)
    observer.call('nintendo/operation/close',close_body,403)
    closed=parent.call('nintendo/operation/close',close_body)
    assert closed['status']=='superseded'
    assert parent.call('nintendo/operation/close',close_body)['status']=='superseded'
    recovered=child.call('nintendo/state?refresh=1')['devices'][0]
    assert recovered['extra_minutes']==75 and recovered['pending_operation'] is None
    assert recovered['last_read_at']>0
    assert observer.call('nintendo/state')['devices']==[]
    for path in ('manifest.webmanifest','sw.js','icon-192.png','icon-512.png','apple-touch-icon.png','favicon.png','assets/app.css','assets/parental.css','assets/app-shell.js','assets/client-settings.js','assets/identity.js','assets/notifications.js','assets/service-logos.js','assets/fonts/InterVariable.woff2','assets/icons/tv.svg','assets/avatars/face-08.svg','assets/services/youtube.svg','assets/services/manus.svg'):
        with urllib.request.urlopen('http://127.0.0.1:8080/'+path) as response:assert response.status==200
    import struct
    for path,size in (('icon-192.png',192),('icon-512.png',512),('apple-touch-icon.png',180),('favicon.png',64)):
        with urllib.request.urlopen('http://127.0.0.1:8080/'+path+'?v=parental-1') as response:
            assert response.headers.get_content_type()=='image/png'
            icon=response.read();assert icon[:8]==b'\x89PNG\r\n\x1a\n' and struct.unpack('>II',icon[16:24])==(size,size)
    # Security headers on the page, the API and errors; the CSP allows exactly the inline script served.
    import base64, hashlib, re
    with urllib.request.urlopen('http://127.0.0.1:8080/') as response:
        page=response.read().decode();csp=response.headers['Content-Security-Policy']
        assert response.headers['X-Frame-Options']=='DENY',response.headers
    for directive in ("default-src 'self'","frame-ancestors 'none'","object-src 'none'","base-uri 'none'","connect-src 'self'"):assert directive in csp,csp
    assert 'unsafe-inline' not in csp.split('script-src',1)[1].split(';',1)[0],csp
    for code in re.findall(r'<script>(.*?)</script>',page,re.S):
        assert "'sha256-"+base64.b64encode(hashlib.sha256(code.encode()).digest()).decode()+"'" in csp,csp
    try:urllib.request.urlopen('http://127.0.0.1:8080/api/me')
    except urllib.error.HTTPError as error:assert error.headers['Content-Security-Policy']==csp and error.headers['X-Frame-Options']=='DENY'
    with urllib.request.urlopen('http://127.0.0.1:8080/manifest.webmanifest') as response:
        manifest=json.loads(response.read());assert manifest['name']=='Parental' and manifest['id']=='/' and manifest['display']=='standalone'
    child.call('auth/logout',{});child.call('me',expected=401)
    print('HTTP smoke passed: scoped roles, mandatory approval, durable 40-minute composition, optional Nintendo bedtime, policy permissions, session logout and PWA assets')
finally:process.terminate();process.wait(timeout=5)

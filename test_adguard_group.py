"""Two independent AdGuard contracts; real leases, journal, matching and expiry."""
import copy
import json
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
import os, tempfile
os.environ.setdefault('DATA_DIR',tempfile.mkdtemp())
os.environ.setdefault('DEMO','true')
import app
import adguard_group


class GroupTests(unittest.TestCase):
    def setUp(self):
        self.old_group=app.ADGUARDS
        self.original=app.api
        self.tables=('leases','baselines','events','seen','settings','ag_baselines','ag_cache','ag_health','ag_client_health','ag_changes','ag_event_sources')
        for table in self.tables:app.DB.execute('DELETE FROM '+table)
        app.DB.commit();app.demo_reset()
        self.now=time.time();self.writes=[];self.offline=set();self.lost=set();self.reject=set();self.ignore=set();self.queries={'origin':[],'node-replica':[]}
        self.clients={key:copy.deepcopy(app.DEMO_CLIENTS) for key in self.queries}
        self.values={key:copy.deepcopy(app.DEMO_GLOBAL) for key in self.queries}
        self.cfg={'name':'Principal','url':'http://primary.test','username':'admin','password':'secret-primary','demo':False,'sync_policy_ack':True,'replicas':[{'id':'node-replica','name':'Réplica','url':'http://replica.test:8053','username':'other','password':'secret-replica'}]}
        app.DB.execute("INSERT OR REPLACE INTO settings VALUES('server',?)",(json.dumps(self.cfg),));app.DB.commit()
        app.ADGUARDS=adguard_group.Group(app,clock=lambda:self.now)
        self.mock=patch.object(app,'api',side_effect=self.api);self.mock.start()

    def tearDown(self):
        self.mock.stop();app.ADGUARDS=self.old_group
        for table in self.tables:app.DB.execute('DELETE FROM '+table)
        app.DB.commit();app.demo_reset()

    def api(self,path,payload=None,method=None,config=None):
        if config is None:return self.original(path,payload,method)
        key=config.get('id','origin');key='node-replica' if 'replica.test' in config.get('url','') else key
        if key in self.offline:raise OSError('private-secret-host')
        clients=self.clients[key];g=self.values[key]
        if path=='clients':return {'clients':copy.deepcopy(clients),'auto_clients':[]}
        if path=='status':return {'version':'v0.107.test','protection_enabled':g['protection_enabled']}
        if path=='blocked_services/all':return {'blocked_services':[{'id':x,'name':x} for x in ('youtube','tiktok','twitch')]}
        if path=='blocked_services/get':return copy.deepcopy(g['blocked_services'])
        if path=='safesearch/status':return copy.deepcopy(g['safe_search'])
        if path=='filtering/status':return {'enabled':g['filtering_enabled'],'interval':24}
        if path=='parental/status':return {'enabled':g['parental_enabled']}
        if path=='safebrowsing/status':return {'enabled':g['safebrowsing_enabled']}
        if path.startswith('querylog'):return {'data':copy.deepcopy(self.queries[key])}
        self.writes.append((key,path,copy.deepcopy(payload)))
        if key in self.reject:raise OSError('private-token')
        if key in self.ignore:return {}
        if path=='clients/update':
            index=next(i for i,c in enumerate(clients) if c['name']==payload['name']);clients[index]=copy.deepcopy(payload['data'])
        elif path=='clients/add':clients.append(copy.deepcopy(payload))
        elif path=='filtering/config':g['filtering_enabled']=payload['enabled']
        elif path=='safesearch/settings':g['safe_search']=copy.deepcopy(payload)
        elif path=='blocked_services/update':g['blocked_services']=copy.deepcopy(payload)
        elif path.startswith('parental/'):g['parental_enabled']=path.endswith('/enable')
        elif path.startswith('safebrowsing/'):g['safebrowsing_enabled']=path.endswith('/enable')
        else:raise AssertionError(path)
        if key in self.lost:
            self.lost.remove(key);raise TimeoutError('private-lost-response')
        return {}

    def grant(self,service='youtube',minutes=20):
        return app.permit('iMac de Emma',service,minutes,granted_by='adult')

    def expire(self):
        self.now+=86400;app.reconcile()

    def test_single_client_both_servers_and_absolute_expiry(self):
        result=self.grant();expires=result['expires']
        self.assertEqual(result['status'],'confirmed')
        for clients in self.clients.values():self.assertEqual(clients[0]['blocked_services'],['tiktok'])
        self.now+=100;app.reconcile();self.assertEqual(app.DB.execute('SELECT expires FROM leases').fetchone()[0],expires)
        self.assertEqual(len(app.state()['clients']),2)
        self.expire()
        for clients in self.clients.values():self.assertTrue(clients[0]['use_global_blocked_services'])
        self.assertEqual(app.DB.execute('SELECT COUNT(*) FROM leases').fetchone()[0],0)

    def test_match_uses_ids_with_alias_and_refuses_name_collision(self):
        self.clients['node-replica'][0]['name']='Emma réplica'
        self.grant();self.assertEqual(self.clients['node-replica'][0]['name'],'Emma réplica')
        self.expire();self.clients['node-replica'][0]['ids']=['192.168.99.99']
        with self.assertRaises(adguard_group.GroupError):self.grant()

    def test_missing_replica_client_and_policy_difference_do_not_mutate(self):
        self.clients['node-replica'].pop(0)
        with self.assertRaises(adguard_group.GroupError):self.grant()
        self.assertEqual(self.writes,[])
        self.clients['node-replica']=copy.deepcopy(self.clients['origin'])
        self.values['node-replica']['blocked_services']['ids']=['youtube']
        with self.assertRaises(adguard_group.GroupError):self.grant()
        self.assertEqual(self.writes,[])

    def test_unreachable_preflight_never_grants_only_one(self):
        self.offline.add('node-replica')
        with self.assertRaises(adguard_group.GroupError):self.grant()
        self.assertEqual(self.writes,[]);self.assertEqual(app.DB.execute('SELECT COUNT(*) FROM leases').fetchone()[0],0)

    def test_partial_write_is_compensated_and_never_reported_success(self):
        self.reject.add('node-replica')
        with self.assertRaisesRegex(adguard_group.GroupError,'estado previo'):self.grant()
        for clients in self.clients.values():self.assertTrue(clients[0]['use_global_blocked_services'])
        self.assertEqual(app.DB.execute('SELECT COUNT(*) FROM leases').fetchone()[0],0)

    def test_uncertain_write_is_restored_on_both(self):
        self.lost.add('node-replica')
        with self.assertRaises(adguard_group.GroupError):self.grant()
        for clients in self.clients.values():self.assertTrue(clients[0]['use_global_blocked_services'])

    def test_read_confirmation_detects_ignored_write(self):
        self.ignore.add('node-replica')
        with self.assertRaises(adguard_group.GroupError):self.grant()
        self.assertTrue(self.clients['origin'][0]['use_global_blocked_services'])

    def test_restart_and_recovery_after_expiry_never_replays_time(self):
        self.grant();self.offline.add('node-replica');self.now+=1300
        with self.assertRaises(adguard_group.GroupError):app.reconcile()
        self.assertTrue(self.clients['origin'][0]['use_global_blocked_services'])
        self.assertTrue(app.DB.execute('SELECT 1 FROM ag_baselines').fetchone())
        app.ADGUARDS=adguard_group.Group(app,clock=lambda:self.now)
        self.offline.clear();app.reconcile()
        self.assertTrue(self.clients['node-replica'][0]['use_global_blocked_services'])
        self.assertFalse(app.DB.execute('SELECT 1 FROM leases').fetchone())

    def test_overlapping_grants_and_policy_edit_restore_latest_base(self):
        self.grant();self.grant('tiktok',30)
        app.save_client('iMac de Emma',{'use_global_blocked_services':False,'blocked_services':['youtube','twitch'],'ignore_statistics':True})
        self.expire()
        for clients in self.clients.values():
            self.assertEqual(clients[0]['blocked_services'],['youtube','twitch']);self.assertTrue(clients[0]['ignore_statistics'])

    def test_global_save_verified_on_both_and_retry_is_persistent(self):
        self.reject.add('node-replica')
        with self.assertRaises(adguard_group.GroupError):app.save_global({'parental_enabled':False})
        self.assertTrue(app.ADGUARDS.pending());self.assertFalse(self.values['origin']['parental_enabled'])
        app.ADGUARDS=adguard_group.Group(app,clock=lambda:self.now);self.reject.clear();app.reconcile()
        self.assertFalse(app.ADGUARDS.pending());self.assertFalse(self.values['node-replica']['parental_enabled'])

    def test_client_save_retry_after_expiry_uses_permanent_policy(self):
        self.grant();self.reject.add('node-replica')
        with self.assertRaises(adguard_group.GroupError):app.save_client('iMac de Emma',{'ignore_statistics':True})
        self.now+=1300;self.reject.clear();app.reconcile()
        for clients in self.clients.values():
            self.assertTrue(clients[0]['use_global_blocked_services']);self.assertTrue(clients[0]['ignore_statistics'])

    def test_external_unrelated_edit_survives_restore(self):
        self.grant();self.clients['node-replica'][0]['ignore_querylog']=True
        self.expire();self.assertTrue(self.clients['node-replica'][0]['ignore_querylog'])

    def test_server_credentials_redacted_and_replica_ack_required(self):
        public=app.public_server();self.assertNotIn('secret-',json.dumps(public));self.assertNotIn('password',public['replicas'][0])
        with self.assertRaisesRegex(ValueError,'adguard-sync'):app.save_server({'sync_policy_ack':False})
        self.grant()
        with self.assertRaises(ValueError):app.save_server({'replicas':[]})
        app.save_server({'password':'new-secret'})
        self.assertNotIn('new-secret',json.dumps(app.public_server()))

    def test_two_querylogs_one_client_one_event_with_provenance(self):
        q={'time':datetime.fromtimestamp(self.now,timezone.utc).isoformat(),'client':'192.168.1.20','reason':'FilteredBlockedService','service_name':'youtube','question':{'name':'accounts.youtube.com'}}
        self.queries={key:[q] for key in self.queries};app.poll();events=app.state()['events']
        self.assertEqual(len(events),1);self.assertEqual(events[0]['client'],'iMac de Emma')
        app.ADGUARDS.decorate_events(events);self.assertEqual(events[0]['attempts'],2)
        self.assertEqual(set(events[0]['servers']),{'origin','node-replica'})
        app.poll();self.assertEqual(app.DB.execute('SELECT COUNT(*) FROM ag_event_sources').fetchone()[0],2)

    def test_replica_querylog_failure_does_not_lose_origin_queries(self):
        q={'time':datetime.fromtimestamp(self.now,timezone.utc).isoformat(),'client':'192.168.1.20','reason':'FilteredBlockedService','service_name':'youtube','question':{'name':'youtube.com'}}
        self.queries['origin']=[q];self.offline.add('node-replica')
        with self.assertRaises(adguard_group.GroupError):app.poll()
        self.assertEqual(app.DB.execute('SELECT COUNT(*) FROM events').fetchone()[0],1)
        self.assertNotIn('private-',json.dumps(app.ADGUARDS.health()))

    def test_origin_cache_preserves_scoped_clients_but_blocks_new_grants(self):
        app.state();self.offline.add('origin')
        state=app.state();self.assertEqual(len(state['clients']),2)
        self.assertFalse(state['adguard_servers'][0]['reachable'])
        with self.assertRaises(adguard_group.GroupError):self.grant()

if __name__=='__main__':unittest.main()

@unittest.skipUnless(os.getenv('CI'),'Requires loopback listeners in CI')
class GroupHTTPTests(unittest.TestCase):
    """The same two-node coordinator through real HTTP APIs and per-node Basic auth."""
    api=GroupTests.api
    grant=GroupTests.grant
    expire=GroupTests.expire

    def setUp(self):
        GroupTests.setUp(self)
        self.mock.stop()
        import base64,threading
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        parent=self
        class NodeHandler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):self.run_api()
            def do_POST(self):self.run_api()
            def do_PUT(self):self.run_api()
            def run_api(self):
                key=self.server.node_id
                secret='admin:secret-primary' if key=='origin' else 'other:secret-replica'
                expected='Basic '+base64.b64encode(secret.encode()).decode()
                if self.headers.get('Authorization')!=expected:
                    self.send_response(401);self.end_headers();return
                payload=json.loads(self.rfile.read(int(self.headers['Content-Length']))) if self.headers.get('Content-Length') else None
                try:
                    value=parent.api(self.path.removeprefix('/control/'),payload,self.command,config={'id':key})
                    raw=json.dumps(value).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(raw)
                except Exception:
                    self.send_response(503);self.end_headers()
        self.servers=[]
        for key in ('origin','node-replica'):
            server=ThreadingHTTPServer(('127.0.0.1',0),NodeHandler);server.node_id=key
            self.servers.append(server);threading.Thread(target=server.serve_forever,daemon=True).start()
        self.cfg['url']='http://127.0.0.1:'+str(self.servers[0].server_address[1])
        self.cfg['replicas'][0]['url']='http://127.0.0.1:'+str(self.servers[1].server_address[1])
        app.DB.execute("UPDATE settings SET value=? WHERE key='server'",(json.dumps(self.cfg),));app.DB.commit()

    def tearDown(self):
        for server in self.servers:server.shutdown();server.server_close()
        GroupTests.tearDown(self)

    def test_independent_http_auth_write_readback_and_expiry(self):
        result=self.grant();self.assertEqual(result['status'],'confirmed')
        for node in app.ADGUARDS.nodes():
            client=app.api('clients',config=node)['clients'][0]
            self.assertNotIn('youtube',client['blocked_services'])
        self.expire()
        for node in app.ADGUARDS.nodes():self.assertTrue(app.api('clients',config=node)['clients'][0]['use_global_blocked_services'])

    def test_http_rejection_is_visible_and_compensated(self):
        self.reject.add('node-replica')
        with self.assertRaises(adguard_group.GroupError):self.grant()
        self.assertTrue(self.clients['origin'][0]['use_global_blocked_services'])

import base64, copy, json, tempfile, time, unittest
from pathlib import Path
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
import auth, notification_center as alerts


class CenterFixture(unittest.TestCase):
    def setUp(self):
        self.old=auth.DB;self.folder=tempfile.TemporaryDirectory();auth.init(self.folder.name)
        self.now=time.time();self.sent=[]
        self.center=alerts.Center(self.folder.name,clock=lambda:self.now,sender=lambda sub,payload:self.sent.append(payload))
        self.admin=auth.save_user(None,{'username':'admin','password':'test-password-1234','role':'admin'},bootstrap=True)
        self.parent=auth.save_user(self.admin,{'username':'parent','password':'test-password-1234','role':'responsable','clients':['TV']})
        self.child=auth.save_user(self.admin,{'username':'child','password':'test-password-1234','role':'solicitante','clients':['TV']})
        self.device='device-1234567890123456'
        self.center.register(self.admin,{'device':self.device})

    def tearDown(self):
        auth.DB.close();auth.DB=self.old;self.folder.cleanup()

    def event(self,identifier=1,client='TV',service='youtube',when=None):
        return {'id':identifier,'time':self.now if when is None else when,'client':client,'service':service,'domain':'accounts.youtube.com','kind':'blocked'}

    def fresh(self):
        self.now+=10;self.center.sync([self.event()]);return self.center.snapshot(self.admin,self.device)['items'][0]

    def sub(self,endpoint='https://web.push.apple.com/example'):
        key=ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(serialization.Encoding.X962,serialization.PublicFormat.UncompressedPoint)
        return {'endpoint':endpoint,'keys':{'auth':base64.urlsafe_b64encode(b'a'*16).rstrip(b'=').decode(),'p256dh':base64.urlsafe_b64encode(key).rstrip(b'=').decode()}}



class CenterTests(CenterFixture):
    def test_old_history_not_replayed(self):
        self.center.sync([self.event(when=self.now-20)])
        self.assertEqual(self.center.snapshot(self.admin,self.device)['unread'],0)

    def test_first_event_not_lost_and_unread_survives_11_minutes(self):
        item=self.fresh();self.now+=660
        data=self.center.snapshot(self.admin,self.device)
        self.assertEqual(data['unread'],1);self.assertEqual(data['deliveries'][0]['id'],item['id'])
        self.assertEqual(auth.DB.execute('SELECT COUNT(*) FROM requests').fetchone()[0],0)

    def test_failure_retries_without_marking_read_and_claim_is_atomic(self):
        item=self.fresh();body={'device':self.device,'id':item['id'],'status':'claim'}
        self.assertTrue(self.center.delivery(self.admin,body)['claimed'])
        self.assertFalse(self.center.delivery(self.admin,body)['claimed'])
        self.center.delivery(self.admin,dict(body,status='failed'))
        self.assertEqual(self.center.snapshot(self.admin,self.device)['deliveries'],[])
        self.now+=21;self.assertTrue(self.center.delivery(self.admin,body)['claimed'])
        self.center.delivery(self.admin,dict(body,status='accepted'))
        data=self.center.snapshot(self.admin,self.device);self.assertEqual(data['unread'],1);self.assertEqual(data['deliveries'],[])

    def test_restart_and_repeated_sources_not_duplicated(self):
        item=self.fresh();event=self.event();self.center.sync([event])
        again=alerts.Center(self.folder.name,clock=lambda:self.now)
        self.assertEqual(again.public_key,self.center.public_key)
        again.sync([event]);self.assertEqual(again.snapshot(self.admin)['unread'],1)
        self.assertEqual(again.snapshot(self.admin)['items'][0]['id'],item['id'])
        self.assertEqual(Path(self.folder.name,'web-push.pem').stat().st_mode&0o777,0o600)

    def test_selection_cooldown_and_generic_dns(self):
        self.now+=10;self.center.sync([self.event(),self.event(2,service='@filtering'),self.event(3,when=self.now+10)])
        self.assertEqual(self.center.snapshot(self.admin)['unread'],1)

    def test_roles_scope_and_other_users_read(self):
        item=self.fresh();self.assertEqual(self.center.snapshot(self.parent)['unread'],1)
        self.assertEqual(self.center.snapshot(self.child)['unread'],0)
        with self.assertRaises(auth.Forbidden):self.center.read(self.parent,{'id':item['id']})
        with self.assertRaises(auth.Forbidden):self.center.delivery(self.child,{'device':self.device,'id':item['id'],'status':'claim'})
        self.parent['clients']=[];self.assertEqual(self.center.snapshot(self.parent)['unread'],0)

    def test_read_idempotent_and_does_not_grant(self):
        item=self.fresh();self.center.read(self.admin,{'id':item['id']});self.center.read(self.admin,{'id':item['id']})
        self.assertEqual(self.center.snapshot(self.admin)['unread'],0)
        self.assertEqual(auth.DB.execute('SELECT COUNT(*) FROM requests').fetchone()[0],0)

    def test_requests_counted_once_in_combined_badge(self):
        self.now+=10
        auth.DB.execute("INSERT INTO requests(user_id,client,service,minutes,status,created) VALUES(?,?,'youtube',10,'pending',?)",(self.child['id'],'TV',self.now));auth.DB.commit()
        self.center.sync([self.event()]);data=self.center.snapshot(self.admin,self.device)
        self.assertEqual(data['pending_requests'],1);self.assertEqual(data['unread'],1);self.assertEqual(data['badge'],2)
        self.assertEqual(len(data['deliveries']),2)
        self.center.read(self.admin,{'id':None});self.assertEqual(self.center.snapshot(self.admin)['badge'],1)

    def test_push_server_delivery_without_browser_and_no_foreground_duplicate(self):
        self.center.register(self.admin,{'device':self.device,'subscription':self.sub()});self.fresh()
        self.center.dispatch();self.assertEqual(len(self.sent),1);self.assertEqual(self.sent[0]['badge_count'],1)
        self.center.dispatch();self.assertEqual(len(self.sent),1)
        item=self.center.snapshot(self.admin,self.device)['items'][0]
        with self.assertRaises(auth.Forbidden):self.center.delivery(self.admin,{'device':self.device,'id':item['id'],'status':'claim'})

    def test_push_failure_and_retry_redacts_private_exception(self):
        def failed(*args):raise RuntimeError('private-endpoint-secret')
        self.center.sender=failed;self.center.register(self.admin,{'device':self.device,'subscription':self.sub()});self.fresh();self.center.dispatch()
        data=self.center.snapshot(self.admin,self.device);self.assertEqual(data['diagnostic']['last_delivery']['status'],'retry')
        self.assertNotIn('private-endpoint-secret',json.dumps(data))
        self.now+=21;self.center.sender=lambda sub,payload:self.sent.append(payload);self.center.dispatch();self.assertEqual(len(self.sent),1)

    def test_expired_subscription_is_disabled(self):
        class Gone(Exception): response=type('Response',(),{'status_code':410})()
        def gone(*args):raise Gone()
        self.center.sender=gone;self.center.register(self.admin,{'device':self.device,'subscription':self.sub()});self.fresh();self.center.dispatch()
        data=self.center.snapshot(self.admin,self.device);self.assertFalse(data['diagnostic']['push']);self.assertEqual(data['diagnostic']['last_delivery']['error'],'push_subscription_expired')

    def test_logout_disconnect_cancels_pending_device_push(self):
        self.center.register(self.admin,{'device':self.device,'subscription':self.sub()});self.fresh()
        self.center.disconnect(self.admin,self.device);self.center.dispatch();self.assertEqual(self.sent,[])

    def test_permissions_revalidated_before_push(self):
        self.center.register(self.admin,{'device':self.device,'subscription':self.sub()});self.fresh()
        auth.DB.execute('UPDATE users SET active=0 WHERE id=?',(self.admin['id'],));auth.DB.commit()
        self.center.dispatch();self.assertEqual(self.sent,[])

    def test_endpoint_validation_prevents_private_and_redirect_destinations(self):
        for endpoint in ('http://web.push.apple.com/test','https://127.0.0.1/api','https://web.push.apple.com.evil.test/x','https://user@web.push.apple.com/x','https://web.push.apple.com:123/x'):
            with self.assertRaises(ValueError):alerts.subscription(self.sub(endpoint))
        for endpoint in ('https://web.push.apple.com/test','https://fcm.googleapis.com/wp/test','https://updates.push.services.mozilla.com/wpush/test'):
            self.assertEqual(alerts.subscription(self.sub(endpoint))['endpoint'],endpoint)

    def test_devices_cannot_be_taken_over(self):
        with self.assertRaises(auth.Forbidden):self.center.register(self.parent,{'device':self.device})
        with self.assertRaises(auth.Forbidden):self.center.disconnect(self.parent,self.device)

    def test_vapid_contact_rejects_names_apple_refuses(self):
        self.assertEqual(alerts.vapid_contact(''),alerts.DEFAULT_CONTACT)
        for good in ('mailto:padres@gmail.com','https://parental.midominio.es','mailto:admin@casa.example.org.es'):
            self.assertEqual(alerts.vapid_contact(good),good)
        self.assertEqual(alerts.vapid_contact('https://midominio.es/'),'https://midominio.es')
        try:from py_vapid import _check_sub
        except ImportError:_check_sub=None
        if _check_sub:  # Everything accepted here must also pass the signing library's own check.
            for good in (alerts.DEFAULT_CONTACT,'mailto:padres@gmail.com','https://parental.midominio.es'):self.assertTrue(_check_sub(alerts.vapid_contact(good)),good)
        for bad in ('mailto:admin@parental.local','mailto:admin@localhost','https://localhost','https://192.168.1.2','mailto:a@nas.lan','http://midominio.es','mailto:a@example.com','admin@gmail.com','https://user:pw@midominio.es','https://github.com/jesusgarrigues/parental','https://midominio.es:8443'):
            with self.assertRaises(ValueError,msg=bad):alerts.vapid_contact(bad)
        self.assertEqual(alerts.Center(self.folder.name,contact='mailto:admin@parental.local').contact,alerts.DEFAULT_CONTACT)

    def test_provider_reason_is_recorded_without_private_details(self):
        class Rejected(Exception):
            response=type('Response',(),{'status_code':403,'text':'{"reason":"BadJwtToken","endpoint":"private-endpoint-secret"}'})()
        def rejected(*args):raise Rejected('private-endpoint-secret')
        self.center.sender=rejected;self.center.register(self.admin,{'device':self.device,'subscription':self.sub()});self.fresh()
        with self.assertLogs(level='WARNING') as logs:self.center.dispatch()
        data=self.center.snapshot(self.admin,self.device)
        self.assertEqual(data['diagnostic']['last_delivery']['error'],'push_provider_403:BadJwtToken')
        self.assertNotIn('private-endpoint-secret',json.dumps(data)+''.join(logs.output))

    def test_page_load_without_browser_subscription_keeps_push(self):
        self.center.register(self.admin,{'device':self.device,'subscription':self.sub()})
        self.assertTrue(self.center.register(self.admin,{'device':self.device,'subscription':None})['push'])
        self.fresh();self.center.dispatch();self.assertEqual(len(self.sent),1)

    def test_same_browser_new_local_id_moves_queue_but_not_across_users(self):
        sub=self.sub();self.center.register(self.admin,{'device':self.device,'subscription':sub});self.fresh()
        other='device-abcdefghijklmnop'
        self.assertTrue(self.center.register(self.admin,{'device':other,'subscription':sub})['push'])
        self.assertFalse(self.center.snapshot(self.admin,self.device)['diagnostic']['registered'])
        self.center.dispatch();self.assertEqual(len(self.sent),1)
        with self.assertRaises(ValueError):self.center.register(self.parent,{'device':'device-zzzzzzzzzzzzzzzz','subscription':sub})

    def test_pending_signup_notifies_admins_only_until_resolved(self):
        self.now+=10
        uid=auth.DB.execute("INSERT INTO users(username,password,role,clients,edit_policy,max_minutes,active,avatar,email) VALUES('nueva',NULL,'solicitante','[]',0,120,0,'','')").lastrowid
        auth.DB.execute('INSERT INTO signups VALUES(?,?)',(uid,self.now));auth.DB.commit()
        self.center.register(self.admin,{'device':self.device,'subscription':self.sub()})
        parent_device='device-parent-123456789'
        self.center.register(self.parent,{'device':parent_device,'subscription':self.sub('https://fcm.googleapis.com/wp/parent')})
        self.center.sync([]);self.center.dispatch()
        self.assertEqual(len(self.sent),1);self.assertIn('nueva',self.sent[0]['title']);self.assertEqual(self.sent[0]['target']['kind'],'users')
        self.assertEqual(self.center.snapshot(self.parent,parent_device)['items'],[])
        auth.DB.execute('DELETE FROM signups WHERE user_id=?',(uid,));auth.DB.commit()
        self.center.sync([]);self.assertFalse(self.center.current(self.admin,self.center.snapshot(self.admin)['items'][0]|{'detail':json.dumps({'user_id':uid})}))

    def test_real_encryption_vapid_and_redirects_disabled(self):
        try:import requests, pywebpush
        except ImportError:self.skipTest('requests installed in CI')
        from unittest.mock import patch
        captured=[]
        def send(session,request,**kwargs):
            captured.append((request,kwargs));response=requests.Response();response.status_code=201;response._content=b'';return response
        with patch.object(requests.Session,'send',send):self.center.send(self.sub(),{'title':'Parental','body':'Test'})
        self.assertTrue(captured[0][0].body);self.assertIn('authorization',{k.lower():v for k,v in captured[0][0].headers.items()})
        self.assertFalse(captured[0][1]['allow_redirects'])
        headers={k.lower():v for k,v in captured[0][0].headers.items()}
        self.assertEqual(headers['urgency'],'high');self.assertEqual(self.center.contact,alerts.DEFAULT_CONTACT)
        self.assertEqual(captured[0][1]['timeout'],10)


class GroupedBadgeTests(CenterFixture):
    def test_463_records_four_clients_two_requests_badge_six(self):
        self.now+=20
        for i in range(463):
            self.center.insert(self.admin,'example:'+str(i),'blocked','TV'+str(i%4),'youtube' if i%3 else 'tiktok',self.now+i,{'domain':'youtube.com'})
        # The timestamps are all past/current and remain in a single immutable window per service.
        self.now+=500
        for i in range(2):auth.DB.execute("INSERT INTO requests(user_id,client,service,minutes,status,created) VALUES(?,?,'youtube',10,'pending',?)",(self.child['id'],'TV',self.now))
        auth.DB.commit();data=self.center.snapshot(self.admin)
        self.assertEqual(data['unread'],4);self.assertEqual(data['pending_requests'],2);self.assertEqual(data['badge'],6)
        self.assertEqual(sum(g['records'] for g in data['groups']),463)

    def test_window_not_extended_and_old_history_still_visible(self):
        item=self.fresh();start=self.now
        self.now+=3600
        self.center.sync([self.event(2)])
        group=self.center.snapshot(self.admin)['groups'][0]
        self.assertEqual(group['first_seen'],start);self.assertEqual(group['records'],2)
        self.now=start+86400
        data=self.center.snapshot(self.admin);self.assertEqual(data['unread'],0);self.assertEqual(len(data['groups']),1)
        self.center.sync([self.event(3)])
        data=self.center.snapshot(self.admin);self.assertEqual(data['unread'],1);self.assertEqual(len(data['groups']),2)

    def test_read_group_and_repetitions_remain_read_service_new_can_count(self):
        item=self.fresh();self.center.read(self.admin,{'id':item['id']});self.now+=600
        self.center.sync([self.event(2)])
        data=self.center.snapshot(self.admin);self.assertEqual(data['unread'],0);self.assertEqual(data['groups'][0]['records'],2)
        self.center.sync([self.event(3,service='tiktok')]);self.assertEqual(self.center.snapshot(self.admin)['unread'],1)
        self.center.read(self.admin,{'client':'TV'});self.assertEqual(self.center.snapshot(self.admin)['unread'],0)
        with self.assertRaises(auth.Forbidden):self.center.read(self.parent,{'client':'Other TV'})

    def test_migration_of_existing_alerts_is_idempotent_and_keeps_history(self):
        self.now+=20
        for i in range(10):auth.DB.execute("INSERT INTO alerts(user_id,source,kind,client,service,created,detail) VALUES(?,?,'blocked','TV','youtube',?,'{}')",(self.admin['id'],'legacy:'+str(i),self.now-90000+i))
        auth.DB.commit();self.center.migrate_groups();self.center.migrate_groups()
        data=self.center.snapshot(self.admin)
        self.assertEqual(data['unread'],0);self.assertEqual(len(data['groups']),1);self.assertEqual(data['groups'][0]['records'],10)
        self.assertEqual(auth.DB.execute('SELECT COUNT(*) FROM alerts').fetchone()[0],10)

    def test_late_query_joins_current_group_without_a_second_badge(self):
        item=self.fresh()
        self.center.insert(self.admin,'late-dns','blocked','TV','youtube',self.now-10,{'domain':'youtube.com'})
        auth.DB.commit()
        data=self.center.snapshot(self.admin)
        self.assertEqual(len(data['groups']),1);self.assertEqual(data['unread'],1)

    def test_existing_event_details_update_after_notification_collection_window(self):
        self.now+=10;event=self.event();self.center.sync([event]);first=self.now
        self.now+=200;event.update(attempts=12,last_seen=self.now,server_names=['Principal','Réplica'])
        self.center.sync([event]);data=self.center.snapshot(self.admin)
        self.assertEqual(len(data['groups']),1);self.assertEqual(data['groups'][0]['attempts'],12)
        self.assertEqual(data['groups'][0]['first_seen'],first);self.assertEqual(data['groups'][0]['last_seen'],self.now)
        self.assertEqual(data['groups'][0]['detail']['servers'],['Principal','Réplica'])

if __name__=='__main__':unittest.main()

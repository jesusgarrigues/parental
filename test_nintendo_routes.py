"""Role boundaries and approval state for the Nintendo HTTP integration."""
import json, os, sqlite3, tempfile, unittest
from pathlib import Path
from unittest.mock import Mock, patch
os.environ.setdefault('DATA_DIR',tempfile.mkdtemp())
os.environ.setdefault('DEMO','true')
import app, auth

class NintendoRoutes(unittest.TestCase):
    def setUp(self):
        auth.init(tempfile.mkdtemp())
        self.admin=auth.bootstrap('setup','setup','admin','password-admin123')
        self.child=auth.save_user(self.admin,{'username':'child','password':'password-child123','role':'solicitante','clients':['nintendo:switch1']})
        self.parent=auth.save_user(self.admin,{'username':'parent','password':'password-parent123','role':'responsable','clients':['nintendo:switch1'],'max_minutes':30})
    def handler(self,user,path,body):
        h=object.__new__(app.Handler);h.path=path;h.body=lambda:body;h.user=lambda:dict(user,csrf='csrf');h.headers={'X-CSRF-Token':'csrf'};h.respond=Mock();return h
    def test_child_cannot_grant_native_time(self):
        h=self.handler(self.child,'/api/permit',{'client':'nintendo:switch1','service':'@nintendo','minutes':20,'operation_id':'a'*32})
        with patch.object(app.NINTENDO,'grant') as grant:h.do_POST();grant.assert_not_called()
        self.assertEqual(h.respond.call_args.args[0],403)
    def test_forty_and_sixty_requests_require_adult_approval_of_the_whole_amount(self):
        for minutes in (40,60):
            h=self.handler(self.child,'/api/request',{'client':'nintendo:switch1','service':'@nintendo','minutes':minutes})
            with patch.object(app.NINTENDO,'validate'),patch.object(app.NINTENDO,'grant') as grant:h.do_POST();grant.assert_not_called()
            self.assertEqual(h.respond.call_args.args[0],200)
            rid=h.respond.call_args.args[1]['id']
            approve=self.handler(self.parent,'/api/request/review',{'id':rid,'decision':'approve','minutes':minutes})
            with patch.object(app.NINTENDO,'grant') as grant:approve.do_POST();grant.assert_not_called()
            self.assertEqual(approve.respond.call_args.args[0],400)
            self.assertEqual(next(r for r in auth.requests_for(self.child) if r['id']==rid)['status'],'pending')
            approve=self.handler(self.admin,'/api/request/review',{'id':rid,'decision':'approve','minutes':minutes})
            with patch.object(app.NINTENDO,'grant',return_value={'status':'pending'}) as grant:approve.do_POST()
            grant.assert_called_once_with('nintendo:switch1',minutes,'request:'+str(rid),extend_bedtime=False)
    def test_policy_requires_scope_and_permission_to_edit_permanent_rules(self):
        body={'client':'nintendo:switch1','operation_id':'c'*32,'revision':'rev','patch':{}}
        for user in (self.child,self.parent):
            h=self.handler(user,'/api/nintendo/policy',body)
            with patch.object(app.NINTENDO,'save_policy') as save:h.do_POST();save.assert_not_called()
            self.assertEqual(h.respond.call_args.args[0],403)
        parent=auth.save_user(self.admin,{'id':self.parent['id'],'edit_policy':True})
        h=self.handler(parent,'/api/nintendo/policy',body)
        with patch.object(app.NINTENDO,'save_policy',return_value={'status':'confirmed'}) as save:h.do_POST()
        save.assert_called_once_with('nintendo:switch1',{},'rev','settings:'+str(parent['id'])+':'+'c'*32)
        h=self.handler(parent,'/api/nintendo/policy',dict(body,client='nintendo:other'))
        with patch.object(app.NINTENDO,'save_policy') as save:h.do_POST();save.assert_not_called()
        self.assertEqual(h.respond.call_args.args[0],403)
    def test_parent_scope_enforced_before_cloud_mutation(self):
        h=self.handler(self.parent,'/api/permit',{'client':'nintendo:switch2','service':'@nintendo','minutes':20,'operation_id':'a'*32})
        with patch.object(app.NINTENDO,'grant') as grant:h.do_POST();grant.assert_not_called()
        self.assertEqual(h.respond.call_args.args[0],403)
    def test_pending_native_operation_not_reported_active(self):
        h=self.handler(self.parent,'/api/permit',{'client':'nintendo:switch1','service':'@nintendo','minutes':20,'operation_id':'a'*32})
        with patch.object(app.NINTENDO,'grant',return_value={'status':'pending','message':'Pendiente'}) as grant:h.do_POST()
        self.assertEqual(grant.call_args.args[2],'direct:'+str(self.parent['id'])+':'+'a'*32)
        self.assertEqual(grant.call_args.kwargs,{'extend_bedtime':False})
        self.assertEqual(h.respond.call_args.args[1]['status'],'pending')
    def test_no_request_collision_with_client_operation_ids(self):
        h=self.handler(self.parent,'/api/permit',{'client':'nintendo:switch1','service':'@nintendo','minutes':20,'operation_id':'request:1'})
        with patch.object(app.NINTENDO,'grant') as grant:h.do_POST();grant.assert_not_called()
        self.assertEqual(h.respond.call_args.args[0],400)
    def test_credentials_config_admin_only(self):
        h=self.handler(self.child,'/api/nintendo/login/begin',{})
        with patch.object(app.NINTENDO,'begin_login') as login:h.do_POST();login.assert_not_called()
        self.assertEqual(h.respond.call_args.args[0],403)
    def test_child_nintendo_request_and_approval_pending(self):
        with patch.object(app,'validate_request'):
            rid=auth.request_access(self.child,{'client':'nintendo:switch1','service':'@nintendo','minutes':20},app.validate_request)
        h=self.handler(self.parent,'/api/request/review',{'id':rid,'decision':'approve','minutes':20})
        with patch.object(app.NINTENDO,'grant',return_value={'status':'pending','message':'Pendiente'}) as grant:h.do_POST()
        grant.assert_called_once_with('nintendo:switch1',20,'request:'+str(rid),extend_bedtime=False)
        self.assertEqual(auth.requests_for(self.child)[0]['status'],'approved_pending')
        h.do_POST();self.assertEqual(h.respond.call_args.args[0],400)
    def test_invalid_nintendo_duration_before_approval(self):
        rid=auth.request_access(self.child,{'client':'nintendo:switch1','service':'@nintendo','minutes':20},lambda c,s:None)
        with self.assertRaises(ValueError):auth.review(self.parent,{'id':rid,'decision':'approve','minutes':19},Mock())
        self.assertEqual(auth.requests_for(self.child)[0]['status'],'pending')
    def test_consoles_filtered_by_assignment(self):
        fake={'configured':True,'devices':[{'key':'nintendo:switch1','name':'Emma'},{'key':'nintendo:switch2','name':'Private'}]}
        with patch.object(app.NINTENDO,'devices',return_value=fake):result=app.nintendo_state(self.child)
        self.assertEqual([d['name'] for d in result['devices']],['Emma'])
    def test_cancel_restricted_before_remote_action(self):
        h=self.handler(self.child,'/api/cancel',{'client':'nintendo:switch1','service':'@nintendo','operation_id':'b'*32})
        with patch.object(app.NINTENDO,'cancel') as cancel:h.do_POST();cancel.assert_not_called()
        self.assertEqual(h.respond.call_args.args[0],403)
    def test_explicit_cancel_all_scoped_to_adult_and_current_displayed_bonus(self):
        body={'client':'nintendo:switch1','service':'@nintendo','operation_id':'b'*32,
              'cancel_all_today':True,'expected_extra_minutes':5,'expected_bedtime':'20:00'}
        h=self.handler(self.parent,'/api/cancel',body)
        with patch.object(app.NINTENDO,'cancel',return_value={'status':'confirmed'}) as cancel:h.do_POST()
        cancel.assert_called_once_with('nintendo:switch1','cancel:'+str(self.parent['id'])+':'+'b'*32,
                                      cancel_all_today=True,expected_extra_minutes=5,expected_bedtime='20:00')
        self.assertEqual(h.respond.call_args.args[0],200)
        for user,client in ((self.child,'nintendo:switch1'),(self.parent,'nintendo:switch2')):
            h=self.handler(user,'/api/cancel',dict(body,client=client))
            with patch.object(app.NINTENDO,'cancel') as cancel:h.do_POST();cancel.assert_not_called()
            self.assertEqual(h.respond.call_args.args[0],403)
    def test_cancel_all_requires_boolean_and_displayed_budget(self):
        base={'client':'nintendo:switch1','service':'@nintendo','operation_id':'b'*32}
        for extra in ({'cancel_all_today':'true'},{'cancel_all_today':1},{'cancel_all_today':True},
                      {'cancel_all_today':True,'expected_extra_minutes':5}):
            h=self.handler(self.parent,'/api/cancel',dict(base,**extra))
            with patch.object(app.NINTENDO,'cancel') as cancel:h.do_POST();cancel.assert_not_called()
            self.assertEqual(h.respond.call_args.args[0],400)
    def test_failed_preflight_does_not_invent_an_uncertain_operation(self):
        h=self.handler(self.parent,'/api/permit',{'client':'nintendo:switch1','service':'@nintendo','minutes':20,'operation_id':'a'*32})
        with patch.object(app.NINTENDO,'grant',side_effect=app.nintendo.NintendoError('No hay datos actuales')),patch.object(app.NINTENDO,'operation_status',return_value=None):h.do_POST()
        self.assertEqual(h.respond.call_args.args[0],503)
        self.assertIs(h.respond.call_args.args[1]['operation_started'],False)
    def test_recorded_pending_operation_not_disguised_as_unsent(self):
        h=self.handler(self.parent,'/api/permit',{'client':'nintendo:switch1','service':'@nintendo','minutes':20,'operation_id':'a'*32})
        pending={'status':'pending','message':'Sin confirmación'}
        with patch.object(app.NINTENDO,'grant',side_effect=app.nintendo.NintendoError('Error de lectura')),patch.object(app.NINTENDO,'operation_status',return_value=pending):h.do_POST()
        self.assertIs(h.respond.call_args.args[1]['operation_started'],True)
        self.assertEqual(h.respond.call_args.args[1]['operation']['status'],'pending')
    def test_request_ids_cannot_alias_canonical_operation_id(self):
        rid=auth.request_access(self.child,{'client':'nintendo:switch1','service':'@nintendo','minutes':20},lambda c,s:None)
        for bad_id in (str(rid).zfill(2),float(rid),True,-1):
            h=self.handler(self.parent,'/api/request/review',{'id':bad_id,'decision':'approve','minutes':20})
            with patch.object(app.NINTENDO,'grant') as grant:h.do_POST();grant.assert_not_called()
            self.assertEqual(h.respond.call_args.args[0],400)
        self.assertEqual(auth.requests_for(self.child)[0]['status'],'pending')
        with self.assertRaises(ValueError):auth.withdraw(self.child,str(rid))
    def test_failed_native_approval_is_http_error(self):
        rid=auth.request_access(self.child,{'client':'nintendo:switch1','service':'@nintendo','minutes':20},lambda c,s:None)
        h=self.handler(self.parent,'/api/request/review',{'id':rid,'decision':'approve','minutes':20})
        with patch.object(app.NINTENDO,'grant',return_value={'status':'failed','message':'Horario de descanso'}):h.do_POST()
        self.assertEqual(h.respond.call_args.args[0],409)
        self.assertEqual(auth.requests_for(self.child)[0]['status'],'approval_error')
    def test_nintendo_request_works_when_adguard_is_unavailable(self):
        h=self.handler(self.child,'/api/request',{'client':'nintendo:switch1','service':'@nintendo','minutes':20})
        with patch.object(app,'api',side_effect=app.AdGuardError('clients','connection','Sin conexión')),patch.object(app.NINTENDO,'validate') as validate:h.do_POST()
        self.assertEqual(h.respond.call_args.args[0],200)
        validate.assert_called_once_with('nintendo:switch1',5)
        rid=h.respond.call_args.args[1]['id']
        h=self.handler(self.parent,'/api/request/review',{'id':rid,'decision':'approve','minutes':20})
        with patch.object(app,'api',side_effect=app.AdGuardError('clients','connection','Sin conexión')),patch.object(app.NINTENDO,'grant',return_value={'status':'confirmed'}):h.do_POST()
        self.assertEqual(h.respond.call_args.args[0],200)
        self.assertEqual(auth.requests_for(self.child)[0]['status'],'approved')
    def test_worker_resolves_approval_without_replaying_cloud_write(self):
        for state,expected in (({'status':'confirmed'},'approved'),({'status':'failed'},'approval_error'),({'status':'superseded'},'approval_closed'),(None,'approval_error'),({'status':'pending'},'approved_pending')):
            rid=auth.request_access(self.child,{'client':'nintendo:switch1','service':'@nintendo','minutes':20},lambda c,s:None)
            auth.review(self.parent,{'id':rid,'decision':'approve','minutes':20},lambda c,s,m,**kw:{'status':'pending'})
            with patch.object(app.NINTENDO,'refresh_pending'),patch.object(app.NINTENDO,'operation_status',return_value=state),patch.object(app.NINTENDO,'grant') as grant:
                app.refresh_nintendo_requests();grant.assert_not_called()
            row=auth.DB.execute('SELECT status FROM requests WHERE id=?',(rid,)).fetchone()
            self.assertEqual(row['status'],expected)

    def test_closing_tracking_requires_adult_and_assigned_console_without_remote_write(self):
        body={'client':'nintendo:switch1','operation_id':'direct:1:test','acknowledge_uncertain':True,'expected_daily_extra_minutes':75,'expected_bedtime':'20:00'}
        observer=auth.save_user(self.admin,{'username':'observer','password':'password-observer123','role':'observador','clients':['nintendo:switch1']})
        for user in (self.child,observer):
            h=self.handler(user,'/api/nintendo/operation/close',body)
            with patch.object(app.NINTENDO,'close_tracking') as close:h.do_POST();close.assert_not_called()
            self.assertEqual(h.respond.call_args.args[0],403)
        h=self.handler(self.parent,'/api/nintendo/operation/close',dict(body,client='nintendo:other'))
        with patch.object(app.NINTENDO,'close_tracking') as close:h.do_POST();close.assert_not_called()
        self.assertEqual(h.respond.call_args.args[0],403)
        h=self.handler(self.parent,'/api/nintendo/operation/close',body)
        with patch.object(app.NINTENDO,'close_tracking',return_value={'status':'superseded'}) as close,patch.object(app.NINTENDO,'grant') as grant,patch.object(app.NINTENDO,'cancel') as cancel:
            h.do_POST();grant.assert_not_called();cancel.assert_not_called()
            close.assert_called_once_with('nintendo:switch1','direct:1:test',acknowledge_uncertain=True,expected_daily_extra_minutes=75,expected_bedtime='20:00')
        self.assertEqual(h.respond.call_args.args[0],200)

    def test_immediate_superseded_approval_is_closed_not_marked_granted(self):
        rid=auth.request_access(self.child,{'client':'nintendo:switch1','service':'@nintendo','minutes':20},lambda c,s:None)
        result=auth.review(self.parent,{'id':rid,'decision':'approve'},lambda c,s,m,**kw:{'status':'superseded'})
        self.assertEqual(result['status'],'superseded')
        self.assertEqual(auth.DB.execute('SELECT status FROM requests WHERE id=?',(rid,)).fetchone()['status'],'approval_closed')
    def test_parent_can_explicitly_extend_bedtime_in_direct_grant(self):
        h=self.handler(self.parent,'/api/permit',{'client':'nintendo:switch1','service':'@nintendo','minutes':20,'operation_id':'a'*32,'extend_bedtime':True})
        with patch.object(app.NINTENDO,'grant',return_value={'status':'confirmed'}) as grant:h.do_POST()
        grant.assert_called_once_with('nintendo:switch1',20,'direct:'+str(self.parent['id'])+':'+'a'*32,extend_bedtime=True)
        self.assertEqual(h.respond.call_args.args[0],200)
        row=auth.DB.execute("SELECT detail FROM audit WHERE action='nintendo_operation_requested' ORDER BY id DESC LIMIT 1").fetchone()
        self.assertIs(json.loads(row['detail'])['extend_bedtime'],True)
    def test_invalid_bedtime_options_do_not_mutate_nintendo(self):
        for value in ('false','true',1,0,None,[]):
            h=self.handler(self.parent,'/api/permit',{'client':'nintendo:switch1','service':'@nintendo','minutes':20,'operation_id':'a'*32,'extend_bedtime':value})
            with patch.object(app.NINTENDO,'grant') as grant:h.do_POST();grant.assert_not_called()
            self.assertEqual(h.respond.call_args.args[0],400)
            h=self.handler(self.child,'/api/request',{'client':'nintendo:switch1','service':'@nintendo','minutes':20,'extend_bedtime':value})
            with patch.object(app.NINTENDO,'validate') as validate:h.do_POST();validate.assert_not_called()
            self.assertEqual(h.respond.call_args.args[0],400)
        self.assertEqual(auth.requests_for(self.child),[])
    def test_requesting_bedtime_never_grants_it(self):
        h=self.handler(self.child,'/api/request',{'client':'nintendo:switch1','service':'@nintendo','minutes':20,'extend_bedtime':True})
        with patch.object(app.NINTENDO,'validate'),patch.object(app.NINTENDO,'grant') as grant:h.do_POST();grant.assert_not_called()
        self.assertEqual(h.respond.call_args.args[0],200)
        row=auth.requests_for(self.child)[0]
        self.assertEqual(row['status'],'pending')
        self.assertIs(row['extend_bedtime'],True)
        self.assertIsNone(row['approved_extend_bedtime'])
        forbidden=self.handler(self.child,'/api/request/review',{'id':row['id'],'decision':'approve','extend_bedtime':True})
        with patch.object(app.NINTENDO,'grant') as grant:forbidden.do_POST();grant.assert_not_called()
        self.assertEqual(forbidden.respond.call_args.args[0],403)
        self.assertEqual(auth.requests_for(self.child)[0]['status'],'pending')
    def test_adult_bedtime_decision_is_distinct_from_request(self):
        for requested,approved in ((True,False),(False,True),(True,True),(False,False)):
            rid=auth.request_access(self.child,{'client':'nintendo:switch1','service':'@nintendo','minutes':20,'extend_bedtime':requested},lambda c,s:None)
            h=self.handler(self.parent,'/api/request/review',{'id':rid,'decision':'approve','minutes':15,'extend_bedtime':approved})
            with patch.object(app.NINTENDO,'grant',return_value={'status':'confirmed'}) as grant:h.do_POST()
            grant.assert_called_once_with('nintendo:switch1',15,'request:'+str(rid),extend_bedtime=approved)
            row=auth.requests_for(self.child)[0]
            self.assertIs(row['extend_bedtime'],requested)
            self.assertIs(row['approved_extend_bedtime'],approved)
            self.assertEqual(row['status'],'approved')
    def test_bedtime_is_not_implicitly_approved(self):
        rid=auth.request_access(self.child,{'client':'nintendo:switch1','service':'@nintendo','minutes':20,'extend_bedtime':True},lambda c,s:None)
        h=self.handler(self.parent,'/api/request/review',{'id':rid,'decision':'approve'})
        with patch.object(app.NINTENDO,'grant',return_value={'status':'confirmed'}) as grant:h.do_POST()
        grant.assert_called_once_with('nintendo:switch1',20,'request:'+str(rid),extend_bedtime=False)
        self.assertIs(auth.requests_for(self.child)[0]['approved_extend_bedtime'],False)
    def test_invalid_approval_option_keeps_request_pending(self):
        rid=auth.request_access(self.child,{'client':'nintendo:switch1','service':'@nintendo','minutes':20},lambda c,s:None)
        h=self.handler(self.parent,'/api/request/review',{'id':rid,'decision':'approve','extend_bedtime':'true'})
        with patch.object(app.NINTENDO,'grant') as grant:h.do_POST();grant.assert_not_called()
        self.assertEqual(h.respond.call_args.args[0],400)
        self.assertEqual(auth.requests_for(self.child)[0]['status'],'pending')
    def test_non_nintendo_cannot_extend_bedtime(self):
        assigned=auth.save_user(self.admin,{'id':self.child['id'],'clients':['computer']})
        for value in (True,'false','true',0,None):
            validate=Mock()
            with self.assertRaises(ValueError):auth.request_access(assigned,{'client':'computer','service':'youtube','minutes':20,'extend_bedtime':value},validate)
            validate.assert_not_called()
        h=self.handler(self.admin,'/api/permit',{'client':'computer','service':'youtube','minutes':20,'extend_bedtime':True})
        with patch.object(app,'permit') as grant:h.do_POST();grant.assert_not_called()
        self.assertEqual(h.respond.call_args.args[0],400)
        rid=auth.request_access(assigned,{'client':'computer','service':'youtube','minutes':20,'extend_bedtime':False},lambda c,s:None)
        permit=Mock()
        with self.assertRaises(ValueError):auth.review(self.admin,{'id':rid,'decision':'approve','extend_bedtime':True},permit)
        permit.assert_not_called()
        self.assertEqual(auth.requests_for(assigned)[0]['status'],'pending')
        auth.review(self.admin,{'id':rid,'decision':'approve','extend_bedtime':False},permit)
        permit.assert_called_once_with('computer','youtube',20)
        self.assertIsNone(auth.requests_for(assigned)[0]['approved_extend_bedtime'])
    def test_bedtime_schema_upgrade_preserves_existing_requests(self):
        folder=Path(tempfile.mkdtemp())
        previous=sqlite3.connect(folder/'accounts.db')
        previous.execute('CREATE TABLE requests(id INTEGER PRIMARY KEY,user_id INTEGER,client TEXT,service TEXT,minutes INTEGER,reason TEXT,status TEXT,created REAL,reviewer INTEGER,reviewed REAL,approved_minutes INTEGER)')
        previous.execute("INSERT INTO requests(id,user_id,client,service,minutes,status) VALUES(1,1,'nintendo:switch1','@nintendo',20,'pending')")
        previous.commit();previous.close()
        auth.init(folder)
        row=auth.DB.execute('SELECT * FROM requests WHERE id=1').fetchone()
        self.assertEqual(row['status'],'pending')
        self.assertEqual(row['minutes'],20)
        self.assertEqual(row['extend_bedtime'],0)
        self.assertIsNone(row['approved_extend_bedtime'])
        auth.init(folder)
        self.assertEqual(auth.DB.execute('SELECT count(*) FROM requests').fetchone()[0],1)

if __name__=='__main__':unittest.main()

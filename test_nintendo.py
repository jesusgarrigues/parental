import asyncio
import copy
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.parse import urlencode
from datetime import datetime
from zoneinfo import ZoneInfo

import nintendo


class FakeBackend:
    def __init__(self):
        self.reads = self.grants = self.cancels = self.restores = self.closed = 0
        self.confirms = 0
        self.confirm_mode = 'success'
        self.confirm_to = '21:20'
        self.read_after_update = None
        self.extra = 0
        self.mode = 'success'
        self.read_error = None
        self.restore_error = None
        self.available = True
        self.complete_on = None
        self.policy_writes = 0
        self.confirm_bedtime_flags = []
        self.device = {
            'id': 'ABC', 'key': 'nintendo:ABC', 'name': 'Switch de Martín', 'model': 'Switch',
            'used_minutes': 60, 'remaining_minutes': 0, 'limit_minutes': 60,
            'extra_minutes': 0, 'bedtime': '21:00', 'forced_termination': True,
            'last_sync': 1700000000, 'console_sync_pending': False,
            'available': True, 'can_grant': True,
            'base_bedtime': '21:00', 'effective_bedtime': '21:00',
            'daily_extra_minutes': 0, 'bedtime_extra_minutes': 0,
            'restrictions_suspended': False,
        }
        reg = {'timeToPlayInOneDay': {'enabled': True, 'limitTime': 60},
               'bedtime': {'enabled': True, 'endingTime': {'hour':21,'minute':0}, 'startingTime':{'hour':6,'minute':0}}}
        self.device['native_policy'] = {'timerMode':'DAILY','restrictionMode':'FORCED_TERMINATION',
            'dailyRegulations':copy.deepcopy(reg),'eachDayOfTheWeekRegulations':{d:copy.deepcopy(reg) for d in ('monday','tuesday','wednesday','thursday','friday','saturday','sunday')}}
        self.device['policy_revision'] = nintendo._policy_revision(self.device['native_policy'])

    async def begin_login(self):
        self.begin_on = asyncio.get_running_loop()
        return 'https://accounts.nintendo.com/connect/1.0.0/authorize?' + urlencode({
            'state': 'oauth-state', 'redirect_uri': 'npf54789bef://auth',
            'session_token_code_challenge': 'verifier-challenge',
        })

    async def complete_login(self, response_url, timezone):
        self.complete_on = asyncio.get_running_loop()
        return {'session_token': 'secret-session-token', 'account_id': 'private-account', 'timezone': timezone}

    async def restore(self, credentials):
        self.restores += 1
        if self.restore_error:
            raise self.restore_error

    async def snapshot(self):
        self.reads += 1
        if self.read_error:
            raise self.read_error
        device = copy.deepcopy(self.device)
        device['extra_minutes'] = self.extra
        device['daily_extra_minutes'] = self.extra
        device['available'] = self.available
        return [device]

    async def grant(self, device_id, minutes):
        self.grants += 1
        if self.mode == 'next_step':
            if self.read_after_update:
                self.read_after_update()
            return {'json': {'status': 'TO_ADDED', 'nextStepDetail': {'estimatedBedtimeChanges': {
                'from': {'hour': 21, 'minute': 0},
                'to': {'hour': int(self.confirm_to[:2]), 'minute': int(self.confirm_to[3:])},
            }}}}
        if self.mode == 'rejected':
            return {'json': {'status': 'OVERTIME_ERROR'}}
        if self.mode == 'unknown':
            self.extra += minutes
            return {'json': {'status': 'UNKNOWN'}}
        if self.mode == 'delayed':
            return {'json': {'status': 'SUCCESS'}}
        self.extra += minutes
        if self.mode == 'timeout':
            raise TimeoutError('secret-token-in-unsafe-sdk-error')
        return {'json': {'status': 'TO_ADDED'}}

    async def confirm(self, device_id, minutes, *, with_bedtime):
        self.confirms += 1
        self.confirm_bedtime_flags.append(with_bedtime)
        if self.confirm_mode != 'delayed':
            self.extra += minutes
            if with_bedtime:
                self.device['bedtime'] = self.device['effective_bedtime'] = self.confirm_to
        if self.confirm_mode == 'timeout':
            raise TimeoutError('private-confirm-token')
        if self.confirm_mode == 'conflict':
            self.device['effective_bedtime'] = self.device['bedtime'] = '22:00'
        return {'json': {'status': 'SUCCESS'}}

    async def cancel(self, device_id):
        self.cancels += 1
        self.extra = 0
        self.device['bedtime'] = self.device['effective_bedtime'] = self.device['base_bedtime']
        return {'json': {'status': 'TO_CANCELED'}}

    async def policy(self, device_id, regulations):
        self.policy_writes += 1
        if self.mode != 'delayed':
            self.device['native_policy'] = copy.deepcopy(regulations)
            self.device['policy_revision'] = nintendo._policy_revision(regulations)
        if self.mode == 'timeout': raise TimeoutError('unsafe-policy-token')
        return {'json': {}}

    async def close(self):
        self.closed += 1


class ConnectorTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.backend = FakeBackend()
        self.now = 1788516000.0
        self.connector = self.make_connector()

    def make_connector(self):
        return nintendo.Connector(self.folder.name, backend_factory=lambda: self.backend, clock=lambda: self.now)

    def tearDown(self):
        self.connector.close()
        self.folder.cleanup()

    def connect(self):
        state = self.connector.begin_login()
        return self.connector.complete_login(state['state_id'], 'npf54789bef://auth#state=oauth-state&session_token_code=secret-code')

    def grant(self, operation_id='direct:1:example', minutes=20, extend_bedtime=False):
        return self.connector.grant('nintendo:ABC', minutes, operation_id, extend_bedtime=extend_bedtime)

    def test_not_configured_without_sdk(self):
        self.assertFalse(self.connector.public_config()['configured'])
        self.assertEqual(self.connector.devices()['devices'], [])
        with self.assertRaises(nintendo.NintendoError):
            self.grant()

    def test_interactive_verifier_keeps_same_backend_and_loop(self):
        data = self.connect()
        self.assertTrue(data['configured'])
        self.assertIs(self.backend.begin_on, self.backend.complete_on)
        self.assertEqual(self.backend.restores, 0)
        self.assertEqual(data['devices'][0]['name'], 'Switch de Martín')
        self.assertNotIn('secret', json.dumps(data))
        self.assertNotIn('private-account', json.dumps(self.connector.public_config()))

    def test_credentials_atomic_private_and_restored_after_restart(self):
        self.connect()
        path = Path(self.folder.name) / 'nintendo.json'
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(path.read_text())['session_token'], 'secret-session-token')
        self.assertEqual(list(Path(self.folder.name).glob('.nintendo-*')), [])
        self.connector.close()
        self.connector = self.make_connector()
        self.assertTrue(self.connector.devices()['configured'])
        self.assertEqual(self.backend.restores, 1)

    def test_oauth_state_scheme_and_expiration_checked_before_auth(self):
        state = self.connector.begin_login()
        for response in ('npf54789bef://auth#state=wrong&session_token_code=a',
                         'https://attacker.test/#state=oauth-state&session_token_code=a',
                         'npf54789bef://auth#session_token_code=a'):
            with self.assertRaises(nintendo.NintendoError):
                self.connector.complete_login(state['state_id'], response)
        self.assertIsNone(self.backend.complete_on)
        self.now += 601
        with self.assertRaisesRegex(nintendo.NintendoError, 'caducado'):
            self.connector.complete_login(state['state_id'], 'npf54789bef://auth#state=oauth-state&session_token_code=a')
        self.assertGreater(self.backend.closed, 0)

    def test_timezone_checked_and_login_state_used_once(self):
        state = self.connector.begin_login()
        response = 'npf54789bef://auth#state=oauth-state&session_token_code=a'
        with self.assertRaises(nintendo.NintendoError):
            self.connector.complete_login(state['state_id'], response, 'invalid/timezone')
        self.connector.complete_login(state['state_id'], response)
        with self.assertRaises(nintendo.NintendoError):
            self.connector.complete_login(state['state_id'], response)

    def test_confirmed_grant_idempotent_and_persisted(self):
        self.connect()
        result = self.grant()
        self.assertEqual(result['status'], 'confirmed')
        self.assertEqual(self.grant()['status'], 'confirmed')
        self.assertEqual(self.backend.grants, 1)
        self.assertEqual(self.backend.extra, 20)
        self.connector.close()
        self.connector = self.make_connector()
        self.assertEqual(self.grant()['status'], 'confirmed')
        self.assertEqual(self.backend.grants, 1)
        self.assertEqual(self.connector.devices()['devices'][0]['last_operation']['operation_id'], result['operation_id'])

    def test_duplicate_operation_cannot_change_target_or_duration(self):
        self.connect()
        self.grant()
        with self.assertRaises(ValueError):
            self.grant(minutes=10)
        with self.assertRaises(ValueError):
            self.connector.grant('nintendo:OTHER', 20, 'direct:1:example')
        self.assertEqual(self.backend.grants, 1)

    def test_next_step_confirms_budget_without_approving_bedtime(self):
        self.connect()
        self.backend.mode = 'next_step'
        result = self.grant()
        self.assertEqual(result['status'], 'confirmed')
        self.assertFalse(result['confirmation_with_bedtime'])
        self.assertEqual(self.backend.extra, 20)
        self.assertEqual(self.backend.device['bedtime'], '21:00')
        self.assertEqual(self.grant()['status'], 'confirmed')
        self.assertEqual(self.backend.grants, 1)
        self.assertEqual(self.backend.confirms, 1)
        self.assertEqual(self.backend.confirm_bedtime_flags, [False])

    def test_explicit_extension_verified_in_both_dimensions_and_persisted(self):
        self.connect()
        self.backend.mode = 'next_step'
        result = self.grant(extend_bedtime=True)
        self.assertEqual(result['status'], 'confirmed')
        self.assertTrue(result['extend_bedtime'])
        self.assertEqual(result['stage'], 'complete')
        self.assertEqual(self.backend.confirms, 1)
        self.assertEqual(self.backend.extra, 20)
        self.assertEqual(self.backend.device['bedtime'], '21:20')
        self.connector.close()
        self.connector = self.make_connector()
        self.assertEqual(self.grant(extend_bedtime=True)['status'], 'confirmed')
        self.assertEqual(self.backend.confirms, 1)
        with self.assertRaises(ValueError):
            self.grant(extend_bedtime=False)

    def test_bedtime_only_extension_does_not_require_extra_daily_delta(self):
        self.connect()
        self.backend.extra = 40
        self.backend.device['used_minutes'] = 40
        self.backend.mode = 'next_step'
        async def bedtime_only(device_id, minutes, *, with_bedtime):
            self.backend.confirms += 1
            self.backend.device['effective_bedtime'] = self.backend.device['bedtime'] = '21:20'
            return {'json': {'status': 'TO_ADDED'}}
        self.backend.confirm = bedtime_only
        self.assertEqual(self.grant(extend_bedtime=True)['status'], 'confirmed')
        self.assertEqual(self.backend.extra, 40)
        self.assertEqual(self.backend.device['effective_bedtime'], '21:20')

    def test_wrong_base_limit_readback_never_claims_confirmed(self):
        self.connect()
        original = self.backend.grant
        async def conflict(device_id, minutes):
            result = await original(device_id, minutes)
            self.backend.device['limit_minutes'] = 120
            return result
        self.backend.grant = conflict
        self.assertEqual(self.grant()['status'], 'failed')

    def test_ledger_survives_crash_before_remote_call_without_resending(self):
        self.connect()
        device = self.connector.validate('nintendo:ABC', 20)
        self.connector._insert('direct:1:example', device, 20, 'grant', extend_bedtime=True)
        self.connector.close()
        self.connector = self.make_connector()
        self.assertEqual(self.grant(extend_bedtime=True)['status'], 'pending')
        self.assertEqual(self.backend.grants, 0)

    def test_timestamp_units_normalized_without_exposing_invalid_dates(self):
        self.connect()
        self.backend.device['last_sync'] = 1700000000000
        self.assertEqual(self.connector.devices(force=True)['devices'][0]['last_sync'], 1700000000)
        self.backend.device['last_sync'] = float('nan')
        self.assertIsNone(self.connector.devices(force=True)['devices'][0]['last_sync'])

    def test_optional_extension_does_not_confirm_when_not_needed(self):
        self.connect()
        self.assertEqual(self.grant(extend_bedtime=True)['status'], 'confirmed')
        self.assertEqual(self.backend.confirms, 0)
        self.assertEqual(self.backend.device['bedtime'], '21:00')

    def test_small_overrun_does_not_prevent_a_new_daily_bonus(self):
        self.connect()
        self.backend.device['used_minutes'] = 61
        self.assertEqual(self.grant()['status'], 'confirmed')
        self.assertEqual(self.backend.extra, 20)

    def test_bonus_that_cannot_cover_overrun_is_rejected_before_write(self):
        self.connect()
        self.backend.device['used_minutes'] = 80
        with self.assertRaises(nintendo.NintendoError):
            self.grant()
        self.assertEqual(self.backend.grants, 0)

    def test_approved_bedtime_extension_after_curfew_is_limited_from_now(self):
        self.connect()
        self.now = datetime(2026, 9, 4, 21, 50, tzinfo=ZoneInfo('Europe/Madrid')).timestamp()
        self.backend.mode = 'next_step'
        self.backend.confirm_to = '22:10'
        async def confirm(device_id, minutes, *, with_bedtime):
            self.backend.confirms += 1
            self.backend.extra = 20  # Clock shifted 70 minutes, daily budget only 20.
            self.backend.device['bedtime'] = self.backend.device['effective_bedtime'] = '22:10'
            return {'json': {'status': 'TO_ADDED'}}
        self.backend.confirm = confirm
        self.assertEqual(self.grant(extend_bedtime=True)['status'], 'confirmed')
        self.assertEqual(self.backend.confirms, 1)

    def test_late_proposal_exceeding_approved_play_window_is_not_confirmed(self):
        self.connect()
        self.now = datetime(2026, 9, 4, 21, 50, tzinfo=ZoneInfo('Europe/Madrid')).timestamp()
        self.backend.mode = 'next_step'
        self.backend.confirm_to = '22:20'
        self.assertEqual(self.grant(extend_bedtime=True)['status'], 'failed')
        self.assertEqual(self.backend.confirms, 0)

    def test_unknown_confirmation_outcome_never_retries(self):
        self.connect()
        self.backend.mode = 'next_step'
        self.backend.confirm_mode = 'timeout'
        result = self.grant(extend_bedtime=True)
        self.assertEqual(result['status'], 'pending')
        self.assertEqual(result['stage'], 'confirm_sent')
        self.assertNotIn('private-confirm', result['message'])
        self.assertEqual(self.grant(extend_bedtime=True)['status'], 'pending')
        self.connector.close()
        self.connector = self.make_connector()
        self.assertEqual(self.grant(extend_bedtime=True)['status'], 'pending')
        self.assertEqual(self.backend.grants, 1)
        self.assertEqual(self.backend.confirms, 1)

    def test_delayed_bedtime_confirmation_reads_only(self):
        self.connect()
        self.backend.mode = 'next_step'
        self.backend.confirm_mode = 'delayed'
        self.assertEqual(self.grant(extend_bedtime=True)['status'], 'pending')
        self.backend.extra = 20
        self.backend.device['effective_bedtime'] = self.backend.device['bedtime'] = '21:20'
        self.connector.refresh_pending()
        self.assertEqual(self.connector.operation_status('direct:1:example')['status'], 'confirmed')
        self.assertEqual(self.backend.confirms, 1)

    def test_changed_settings_between_update_and_confirm_are_not_confirmed(self):
        self.connect()
        self.backend.mode = 'next_step'
        self.backend.read_after_update = lambda: self.backend.device.update(limit_minutes=30)
        result = self.grant(extend_bedtime=True)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(self.backend.confirms, 0)

    def test_incomplete_or_excessive_bedtime_estimate_is_never_confirmed(self):
        self.connect()
        self.backend.mode = 'next_step'
        self.backend.confirm_to = '22:00'
        self.assertEqual(self.grant(extend_bedtime=True)['status'], 'failed')
        self.assertEqual(self.backend.confirms, 0)

    def test_wrong_readback_bedtime_does_not_claim_confirmed(self):
        self.connect()
        self.backend.mode = 'next_step'
        self.backend.confirm_mode = 'conflict'
        self.assertEqual(self.grant(extend_bedtime=True)['status'], 'superseded')
        self.assertEqual(self.backend.confirms, 1)

    def test_hidden_alarms_and_alarm_only_mode_do_not_block_budget_management(self):
        self.connect()
        self.backend.device['restrictions_suspended'] = True
        self.backend.device['alarms_enabled'] = False
        self.backend.device.update(limit_minutes=0, used_minutes=0)
        self.backend.extra = 5
        self.assertEqual(self.grant(extend_bedtime=True)['status'], 'confirmed')
        self.assertEqual(self.backend.extra, 25)
        self.backend.device['restrictions_suspended'] = False
        self.backend.device['forced_termination'] = False
        self.assertEqual(self.grant('direct:1:alarm-only')['status'], 'confirmed')
        self.assertEqual(self.backend.grants, 2)

    def test_nighttime_request_validation_does_not_mutate_or_reject_just_time(self):
        self.connect()
        self.backend.device['bedtime_remaining_minutes'] = 0
        self.assertEqual(self.connector.validate('nintendo:ABC', 20)['key'], 'nintendo:ABC')
        self.backend.mode='next_step'
        self.assertEqual(self.grant()['status'],'confirmed')
        self.assertEqual(self.backend.confirm_bedtime_flags,[False])
        self.assertEqual(self.backend.device['bedtime'],'21:00')

    def test_bedtime_flag_strict_boolean(self):
        self.connect()
        for value in (1, 0, None, 'true'):
            with self.assertRaises(ValueError):
                self.grant(extend_bedtime=value)
        self.assertEqual(self.backend.grants, 0)

    def test_login_completion_cannot_replace_account_with_new_pending_operation(self):
        self.connect()
        login = self.connector.begin_login()
        self.backend.mode = 'timeout'
        self.grant()
        with self.assertRaises(nintendo.NintendoError):
            self.connector.complete_login(login['state_id'], 'npf54789bef://auth#state=oauth-state&session_token_code=other')

    def test_known_rejection_is_failed(self):
        self.connect()
        self.backend.mode = 'rejected'
        self.assertEqual(self.grant()['status'], 'failed')
        self.assertEqual(self.backend.extra, 0)

    def test_delayed_readback_reconciles_without_resending(self):
        self.connect()
        self.backend.mode = 'delayed'
        self.assertEqual(self.grant()['status'], 'pending')
        with self.assertRaisesRegex(nintendo.NintendoError, 'pendiente'):
            self.grant('direct:1:other')
        self.backend.extra = 20
        self.connector.refresh_pending()
        self.assertEqual(self.connector.operation_status('direct:1:example')['status'], 'confirmed')
        self.assertEqual(self.backend.grants, 1)

    def test_timeout_never_retries_even_if_remote_write_was_applied(self):
        self.connect()
        self.backend.mode = 'timeout'
        result = self.grant()
        self.assertEqual(result['status'], 'pending')
        self.assertNotIn('secret-token', result['message'])
        self.assertEqual(self.backend.extra, 20)
        self.assertEqual(self.grant()['status'], 'pending')
        device = self.connector.devices(force=True)['devices'][0]
        self.assertFalse(device['can_grant'])
        self.assertFalse(device['can_cancel'])
        self.assertEqual(device['pending_operation']['operation_id'], result['operation_id'])
        self.connector.close()
        self.connector = self.make_connector()
        self.assertEqual(self.grant()['status'], 'pending')
        with self.assertRaises(nintendo.NintendoError):
            self.grant('direct:1:new')
        self.assertEqual(self.backend.grants, 1)

    def test_unknown_success_shape_not_trusted(self):
        self.connect()
        self.backend.mode = 'unknown'
        self.assertEqual(self.grant()['status'], 'pending')
        self.connector.devices(force=True)
        self.assertEqual(self.connector.operation_status('direct:1:example')['status'], 'pending')

    def test_invalid_scope_duration_and_unlimited_never_mutate(self):
        self.connect()
        for minutes in (0, -1, 4, 31, 120, 10.0, True, '20'):
            with self.assertRaises(ValueError):
                self.connector.validate('nintendo:ABC', minutes)
        with self.assertRaises(ValueError):
            self.connector.validate('nintendo:MISSING', 20)
        self.backend.extra = -1
        with self.assertRaises(nintendo.NintendoError):
            self.grant()
        self.assertEqual(self.backend.grants, 0)

    def test_failed_fresh_read_prevents_writes_and_disables_stale_cards(self):
        self.connect()
        self.backend.read_error = RuntimeError('sensitive-cookie-token')
        result = self.connector.devices(force=True)
        self.assertNotIn('sensitive', json.dumps(result))
        self.assertFalse(result['devices'][0]['available'])
        self.assertFalse(self.connector.devices()['devices'][0]['can_grant'])
        with self.assertRaises(nintendo.NintendoError):
            self.grant()
        self.assertEqual(self.backend.grants, 0)
        self.assertIsNone(self.connector.operation_status('direct:1:example'))

    def test_read_cache_20_seconds(self):
        self.connect()
        first = self.backend.reads
        self.connector.devices()
        self.connector.devices()
        self.assertEqual(self.backend.reads, first)
        self.now += 21
        self.connector.devices()
        self.assertEqual(self.backend.reads, first + 1)

    def close_tracking(self, operation_id='direct:1:example', **overrides):
        options={'acknowledge_uncertain':True,'expected_daily_extra_minutes':self.backend.extra,
                 'expected_bedtime':self.backend.device['effective_bedtime']}
        options.update(overrides)
        return self.connector.close_tracking('nintendo:ABC',operation_id,**options)

    def test_official_app_higher_bonus_closes_accepted_tracking_without_claiming_grant(self):
        self.connect();self.backend.extra=45;self.backend.device.update(limit_minutes=0,used_minutes=40)
        self.backend.mode='delayed'
        pending=self.grant(minutes=15)
        self.assertEqual(pending['status'],'pending');self.assertTrue(pending['last_step_acknowledged'])
        self.backend.extra=75
        device=self.connector.devices(force=True)['devices'][0]
        self.assertIsNone(device['pending_operation'])
        self.assertEqual(device['daily_extra_minutes'],75)
        self.assertEqual(device['last_operation']['status'],'superseded')
        self.assertEqual(device['last_operation']['confirmed_minutes'],0)
        self.assertTrue(device['can_grant'])
        self.assertEqual(self.grant(minutes=15)['status'],'superseded')
        self.assertEqual(self.backend.grants,1)
        self.connector.close();self.connector=self.make_connector();self.connector.refresh_pending()
        self.assertEqual(self.backend.grants,1)

    def test_lagging_baseline_after_ack_stays_pending_then_exact_target_confirms(self):
        self.connect();self.backend.mode='delayed';self.grant()
        self.now+=120;self.connector.refresh_pending()
        pending=self.connector.operation_status('direct:1:example')
        self.assertEqual(pending['status'],'pending')
        self.assertEqual(pending['expected_daily_extra_minutes'],20)
        self.assertTrue(pending['last_step_acknowledged'])
        self.assertEqual(pending['native_response_status'],'SUCCESS')
        self.backend.extra=20;self.connector.refresh_pending()
        self.assertEqual(self.connector.operation_status('direct:1:example')['status'],'confirmed')
        self.assertEqual(self.backend.grants,1)

    def test_lost_response_higher_read_never_automatically_confirms_or_closes(self):
        self.connect();self.backend.mode='timeout';self.grant();self.backend.extra=75
        device=self.connector.devices(force=True)['devices'][0]
        self.assertEqual(device['pending_operation']['status'],'pending')
        self.assertFalse(device['pending_operation']['last_step_acknowledged'])
        self.assertFalse(device['can_grant']);self.assertEqual(self.backend.grants,1)

    def test_official_cancel_below_baseline_closes_accepted_grant(self):
        self.connect();self.backend.extra=5;self.backend.mode='delayed';self.grant(minutes=15)
        self.backend.extra=0;self.connector.refresh_pending()
        self.assertEqual(self.connector.operation_status('direct:1:example')['status'],'superseded')
        self.assertEqual(self.backend.grants,1)

    def test_close_uncertain_tracking_is_read_only_durable_and_idempotent(self):
        self.connect();self.backend.mode='timeout';self.grant()
        result=self.close_tracking()
        self.assertEqual(result['status'],'superseded');self.assertEqual(result['stage'],'closed_by_user')
        self.assertEqual(self.backend.extra,20)
        self.assertEqual((self.backend.grants,self.backend.confirms,self.backend.cancels,self.backend.policy_writes),(1,0,0,0))
        self.connector.close();self.connector=self.make_connector();self.connector.refresh_pending()
        self.assertEqual(self.close_tracking()['status'],'superseded')
        self.assertTrue(self.connector.devices()['devices'][0]['can_grant'])
        self.grant('direct:1:another')
        self.close_tracking()  # retry old close never closes a newer pending grant
        self.assertEqual(self.connector.operation_status('direct:1:another')['status'],'pending')
        self.assertEqual(self.backend.grants,2)

    def test_closing_tracking_requires_acknowledgement_and_fresh_matching_state(self):
        self.connect();self.backend.mode='timeout';self.grant()
        for value in (False,'true',1,None):
            with self.assertRaises(ValueError):self.close_tracking(acknowledge_uncertain=value)
        with self.assertRaises(nintendo.NintendoError):self.close_tracking(expected_daily_extra_minutes=0)
        with self.assertRaises(nintendo.NintendoError):self.close_tracking(expected_bedtime='20:00')
        self.backend.read_error=TimeoutError('private-token')
        with self.assertRaises(nintendo.NintendoError):self.close_tracking()
        self.assertEqual(self.connector.operation_status('direct:1:example')['status'],'pending')
        self.assertEqual((self.backend.grants,self.backend.cancels),(1,0))

    def test_close_tracking_cannot_target_another_console_or_account(self):
        self.connect();self.backend.mode='timeout';self.grant()
        with self.assertRaises(ValueError):self.connector.close_tracking('nintendo:other','direct:1:example',acknowledge_uncertain=True,expected_daily_extra_minutes=20,expected_bedtime='21:00')
        self.connector._db.execute("UPDATE operations SET account_hash='another-account'");self.connector._db.commit()
        with self.assertRaises(ValueError):self.close_tracking()
        self.assertEqual(self.backend.grants,1)

    def test_safe_cancel_own_single_grant_is_durable(self):
        self.connect()
        self.grant()
        self.assertTrue(self.connector.devices()['devices'][0]['can_cancel'])
        result = self.connector.cancel('nintendo:ABC', 'cancel:1:example')
        self.assertEqual(result['status'], 'confirmed')
        self.assertEqual(self.backend.extra, 0)
        self.assertTrue(self.connector.operation_status('direct:1:example')['cancelled'])
        self.assertEqual(self.connector.cancel('nintendo:ABC', 'cancel:1:example')['status'], 'confirmed')
        self.assertEqual(self.backend.cancels, 1)

    def test_cancel_external_time_requires_explicit_all_today_confirmation(self):
        self.connect()
        self.backend.extra = 10
        self.grant()
        self.assertTrue(self.connector.devices()['devices'][0]['can_cancel'])
        with self.assertRaises(nintendo.NintendoError):
            self.connector.cancel('nintendo:ABC', 'cancel:1:example')
        self.assertEqual(self.backend.cancels, 0)

    def test_cancel_official_app_bonus_with_zero_daily_limit(self):
        self.backend.device.update(limit_minutes=0, used_minutes=0, alarms_enabled=False)
        self.backend.extra = 5
        self.connect()
        self.assertTrue(self.connector.devices()['devices'][0]['can_cancel'])
        result = self.connector.cancel('nintendo:ABC', 'cancel:1:official', cancel_all_today=True,
                                       expected_extra_minutes=5, expected_bedtime='21:00')
        self.assertEqual(result['status'], 'confirmed')
        self.assertEqual(self.backend.extra, 0)
        self.assertEqual(self.backend.device['limit_minutes'], 0)
        self.assertEqual(self.backend.cancels, 1)
        self.connector.cancel('nintendo:ABC', 'cancel:1:official', cancel_all_today=True,
                              expected_extra_minutes=5, expected_bedtime='21:00')
        self.assertEqual(self.backend.cancels, 1)

    def test_cancel_external_bonus_refuses_changed_minutes_or_bedtime(self):
        self.backend.extra = 5
        self.connect()
        for extra, bedtime in ((10, '21:00'), (5, '21:20')):
            with self.assertRaisesRegex(nintendo.NintendoError, 'ha cambiado'):
                self.connector.cancel('nintendo:ABC', 'cancel:1:stale', cancel_all_today=True,
                                      expected_extra_minutes=extra, expected_bedtime=bedtime)
        self.assertEqual(self.backend.cancels, 0)
        self.assertIsNone(self.connector.operation_status('cancel:1:stale'))

    def test_cancel_without_daily_limit_and_with_infinite_extra(self):
        self.backend.device['limit_minutes'] = -1
        self.backend.extra = -1
        self.connect()
        result = self.connector.cancel('nintendo:ABC', 'cancel:1:infinite', cancel_all_today=True,
                                       expected_extra_minutes=-1, expected_bedtime='21:00')
        self.assertEqual(result['status'], 'confirmed')
        self.assertEqual(self.backend.extra, 0)

    def test_cancel_all_marks_multiple_app_grants_removed(self):
        self.connect()
        self.grant('direct:1:first')
        self.grant('direct:1:second')
        self.connector.cancel('nintendo:ABC', 'cancel:1:both', cancel_all_today=True,
                              expected_extra_minutes=40, expected_bedtime='21:00')
        for operation in ('direct:1:first', 'direct:1:second'):
            self.assertTrue(self.connector.operation_status(operation)['cancelled'])

    def test_uncertain_cancel_is_never_resent(self):
        self.backend.extra = 5
        self.connect()
        original = self.backend.cancel
        async def timeout_after_write(device_id):
            await original(device_id)
            raise TimeoutError('unsafe-response')
        self.backend.cancel = timeout_after_write
        options = {'cancel_all_today': True, 'expected_extra_minutes': 5, 'expected_bedtime': '21:00'}
        self.assertEqual(self.connector.cancel('nintendo:ABC', 'cancel:1:timeout', **options)['status'], 'pending')
        self.connector.refresh_pending()
        self.assertEqual(self.connector.cancel('nintendo:ABC', 'cancel:1:timeout', **options)['status'], 'pending')
        self.assertEqual(self.backend.cancels, 1)

    def test_cancel_all_rejects_invalid_confirmation_before_write(self):
        self.backend.extra = 5
        self.connect()
        for option in ('true', 1, None):
            with self.assertRaises(ValueError):
                self.connector.cancel('nintendo:ABC', 'cancel:1:invalid', cancel_all_today=option)
        for extra in (None, True, '5', -2, 0, float('nan')):
            with self.assertRaises((ValueError, nintendo.NintendoError)):
                self.connector.cancel('nintendo:ABC', 'cancel:1:invalid', cancel_all_today=True,
                                      expected_extra_minutes=extra, expected_bedtime='21:00')
        self.assertEqual(self.backend.cancels, 0)

    def test_cancel_rechecks_remote_delta_even_if_button_was_visible(self):
        self.connect()
        self.grant()
        self.assertTrue(self.connector.devices()['devices'][0]['can_cancel'])
        self.backend.extra += 5
        with self.assertRaises(nintendo.NintendoError):
            self.connector.cancel('nintendo:ABC', 'cancel:1:example')
        self.assertEqual(self.backend.cancels, 0)

    def test_disconnect_blocks_pending_but_next_day_expires(self):
        self.connect()
        self.backend.mode = 'timeout'
        self.grant()
        with self.assertRaises(nintendo.NintendoError):
            self.connector.disconnect()
        with self.assertRaises(nintendo.NintendoError):
            self.connector.begin_login()
        self.now += 86400
        self.assertFalse(self.connector.disconnect()['configured'])
        self.assertFalse((Path(self.folder.name) / 'nintendo.json').exists())
        self.assertEqual(self.connector.operation_status('direct:1:example')['status'], 'failed')

    def test_parallel_duplicate_grants_send_once(self):
        self.connect()
        results, errors = [], []
        def run():
            try:
                results.append(self.grant())
            except Exception as error:
                errors.append(error)
        threads = [threading.Thread(target=run) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 4)
        self.assertTrue(all(result['status'] == 'confirmed' for result in results))
        self.assertEqual(self.backend.grants, 1)

    def test_ledger_committed_before_remote_write(self):
        self.connect()
        original = self.backend.grant
        async def inspect(device_id, minutes):
            self.assertEqual(self.connector._db.execute('SELECT status FROM operations').fetchone()[0], 'pending')
            return await original(device_id, minutes)
        self.backend.grant = inspect
        self.assertEqual(self.grant()['status'], 'confirmed')

    def test_zero_daily_limit_existing_five_can_add_fifteen_with_incomplete_estimate(self):
        self.connect()
        self.backend.device.update(limit_minutes=0, used_minutes=0, bedtime='20:00',
                                   base_bedtime='20:00', effective_bedtime='20:00')
        details = ({}, {'estimatedBedtimeChanges': None},
                   {'estimatedBedtimeChanges': {'from': {'hour':20}, 'to': {'hour':20}}},
                   {'estimatedBedtimeChanges': {'from': {'hour':20,'minute':0}, 'to': {'hour':20,'minute':0}}})
        for index, detail in enumerate(details):
            for extend in (False, True):
                with self.subTest(detail=detail, extend=extend):
                    self.backend.extra=5
                    async def proposal(device_id, minutes):
                        self.backend.grants+=1
                        return {'json': {'status':'TO_ADDED', 'nextStepDetail':detail}}
                    self.backend.grant=proposal
                    result=self.grant(f'direct:1:shape-{index}-{extend}', minutes=15, extend_bedtime=extend)
                    self.assertEqual(result['status'],'confirmed')
                    self.assertFalse(result['confirmation_with_bedtime'])
                    self.assertEqual(self.backend.extra,20)
                    self.assertEqual(self.backend.device['bedtime'],'20:00')
        self.assertEqual(self.backend.confirm_bedtime_flags,[False]*8)

    def test_unknown_update_with_next_step_never_sends_a_confirmation(self):
        self.connect()
        async def proposal(*args): return {'json': {'status':'SOMETHING_NEW', 'nextStepDetail':{}}}
        self.backend.grant=proposal
        self.assertEqual(self.grant()['status'],'pending')
        self.assertEqual(self.backend.confirms,0)

    def test_composed_grant_preserves_bedtime_from_previous_verified_step(self):
        self.connect();self.backend.confirm_to='21:30'
        async def proposal(device_id,minutes):
            self.backend.grants+=1
            detail={'estimatedBedtimeChanges':{'from':{'hour':21,'minute':0},'to':{'hour':21,'minute':30}}} if self.backend.grants==1 else {}
            return {'json':{'status':'TO_ADDED','nextStepDetail':detail}}
        self.backend.grant=proposal
        self.assertEqual(self.grant(minutes=40,extend_bedtime=True)['confirmed_minutes'],30)
        self.connector.refresh_pending();self.connector.refresh_pending()
        result=self.connector.operation_status('direct:1:example')
        self.assertEqual(result['status'],'confirmed')
        self.assertEqual(result['confirmed_minutes'],40)
        self.assertEqual(self.backend.extra,40)
        self.assertEqual(self.backend.device['effective_bedtime'],'21:30')
        self.assertEqual(self.backend.confirm_bedtime_flags,[True,False,False])

    def test_failed_fresh_read_after_next_step_is_unsent_not_uncertain(self):
        self.connect();self.backend.mode='next_step'
        self.backend.read_after_update=lambda:setattr(self.backend,'read_error',TimeoutError('private-token'))
        result=self.grant()
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['stage'],'confirmation_received')
        self.assertEqual(self.backend.confirms,0)
        self.assertNotIn('private-token',result['message'])
        self.backend.read_error=None;self.backend.read_after_update=None
        self.assertEqual(self.grant('direct:1:new')['status'],'confirmed')

    def test_budget_only_confirm_timeout_remains_uncertain_after_restart(self):
        self.connect();self.backend.mode='next_step';self.backend.confirm_mode='timeout'
        result=self.grant()
        self.assertEqual(result['status'],'pending')
        self.assertEqual(result['stage'],'confirm_sent')
        self.assertEqual(self.backend.device['bedtime'],'21:00')
        self.connector.close();self.connector=self.make_connector()
        self.connector.refresh_pending()
        self.assertEqual(self.grant()['status'],'pending')
        self.assertEqual(self.backend.grants,1)
        self.assertEqual(self.backend.confirms,1)

    def test_explicit_confirm_conflict_is_failed_and_not_replayed(self):
        self.connect();self.backend.mode='next_step'
        class Conflict(Exception): status_code=409
        async def reject(*args,**kwargs):raise Conflict()
        self.backend.confirm=reject
        self.assertEqual(self.grant()['status'],'failed')
        self.assertEqual(self.backend.extra,0)
        self.assertEqual(self.grant()['status'],'failed')
        self.assertEqual(self.backend.grants,1)

    def test_upgrade_releases_only_old_known_unsent_confirmation(self):
        self.connect()
        device=self.connector.validate('nintendo:ABC',15)
        self.connector._insert('direct:1:old',device,15,'grant',extend_bedtime=True)
        self.connector._finish('direct:1:old','pending','Nintendo ha enviado una ampliación del horario incompleta. El resultado es incierto y no se reenviará la operación.')
        self.connector.close();self.connector=self.make_connector()
        self.assertEqual(self.connector.operation_status('direct:1:old')['status'],'failed')
        self.assertEqual(self.backend.grants,0)
        self.assertEqual(self.backend.confirms,0)
        self.assertEqual(self.grant('direct:1:new',minutes=15)['status'],'confirmed')

    def test_restart_after_receiving_next_step_never_sends_unfinished_confirmation(self):
        self.connect()
        device=self.connector.validate('nintendo:ABC',15)
        self.connector._insert('direct:1:old',device,15,'grant')
        self.connector._db.execute("UPDATE operations SET stage='confirmation_received'")
        self.connector._db.commit()
        self.connector.close();self.connector=self.make_connector()
        self.connector.refresh_pending()
        self.assertEqual(self.connector.operation_status('direct:1:old')['status'],'failed')
        self.assertEqual(self.backend.grants,0)
        self.assertEqual(self.backend.confirms,0)

    def test_sixty_is_one_native_grant_and_consumed_bonus_can_be_extended_again(self):
        self.connect()
        self.assertEqual(self.grant(minutes=60)['confirmed_minutes'], 60)
        self.backend.device['used_minutes'] = 120
        self.assertEqual(self.grant('direct:1:again', minutes=60)['status'], 'confirmed')
        self.assertEqual(self.backend.extra, 120)
        self.assertEqual(self.backend.grants, 2)

    def test_forty_is_one_approval_three_verified_steps_without_rounding_up(self):
        self.connect()
        result = self.grant(minutes=40)
        self.assertEqual((result['status'], result['confirmed_minutes'], result['steps_total']), ('pending', 30, 3))
        self.assertEqual(self.backend.grants, 1)
        self.connector.devices(force=True)  # browser GET never sends a step
        self.assertEqual(self.backend.grants, 1)
        self.connector.refresh_pending()
        self.assertEqual(self.connector.operation_status('direct:1:example')['confirmed_minutes'], 35)
        self.connector.refresh_pending()
        result = self.connector.operation_status('direct:1:example')
        self.assertEqual((result['status'], result['confirmed_minutes']), ('confirmed', 40))
        self.assertEqual(self.backend.extra, 40)
        self.assertEqual(self.backend.grants, 3)
        self.grant(minutes=40)
        self.assertEqual(self.backend.grants, 3)

    def test_forty_plan_survives_restart_and_resumes_only_unsent_steps(self):
        self.connect()
        self.grant(minutes=40)
        self.connector.close(); self.connector = self.make_connector()
        self.connector.refresh_pending(); self.connector.refresh_pending()
        self.assertEqual(self.backend.grants, 3)
        self.assertEqual(self.connector.operation_status('direct:1:example')['status'], 'confirmed')

    def test_uncertain_second_step_blocks_further_steps_even_after_restart(self):
        self.connect(); self.grant(minutes=40)
        self.backend.mode = 'timeout'
        self.connector.refresh_pending()
        self.assertEqual(self.backend.extra, 35)
        self.assertEqual(self.backend.grants, 2)
        self.connector.close(); self.connector = self.make_connector()
        for _ in range(3): self.connector.refresh_pending()
        result = self.connector.operation_status('direct:1:example')
        self.assertEqual((result['status'], result['confirmed_minutes']), ('pending', 30))
        self.assertEqual(self.backend.grants, 2)

    def test_delayed_step_must_be_read_back_before_next_step(self):
        self.connect(); self.backend.mode = 'delayed'
        self.grant(minutes=40)
        self.connector.refresh_pending()
        self.assertEqual(self.backend.grants, 1)
        self.backend.extra = 30; self.backend.mode = 'success'
        self.connector.refresh_pending()
        self.assertEqual(self.backend.grants, 2)
        self.assertEqual(self.backend.extra, 35)

    def test_changed_budget_between_steps_stops_with_partial_progress(self):
        self.connect(); self.grant(minutes=40)
        self.backend.extra = 45
        self.connector.refresh_pending()
        result = self.connector.operation_status('direct:1:example')
        self.assertEqual((result['status'], result['confirmed_minutes']), ('superseded', 30))
        self.assertEqual(self.backend.grants, 1)
        self.assertEqual(self.backend.extra, 45)

    def test_native_expiry_prevents_continuing_a_plan(self):
        self.backend.device['extra_expires_at'] = self.now + 10
        self.connect(); self.grant(minutes=40)
        self.now += 11; self.connector.refresh_pending()
        self.assertEqual(self.backend.grants, 1)
        self.assertEqual(self.connector.operation_status('direct:1:example')['status'], 'failed')

    def test_clock_shift_alone_never_confirms_an_added_daily_budget(self):
        self.connect(); self.backend.mode = 'delayed'
        self.grant()
        self.backend.device['bedtime_extra_minutes'] = 20
        self.connector.refresh_pending()
        self.assertEqual(self.connector.operation_status('direct:1:example')['status'], 'pending')

    def test_cancel_bedtime_only_permission_without_fabricating_daily_minutes(self):
        self.backend.device.update(has_extra=True, bedtime_extra_minutes=30, effective_bedtime='21:30', bedtime='21:30')
        self.connect()
        self.assertTrue(self.connector.devices()['devices'][0]['can_cancel'])
        result = self.connector.cancel('nintendo:ABC','cancel:1:night',cancel_all_today=True,
                                       expected_extra_minutes=0,expected_bedtime='21:30')
        self.assertEqual(result['status'],'confirmed')

    def test_existing_pending_legacy_operation_is_not_reissued_after_upgrade(self):
        self.connect(); self.backend.mode = 'delayed'; self.grant()
        self.connector._db.execute("UPDATE operations SET model_version=1 WHERE operation_id='direct:1:example'");self.connector._db.commit()
        self.connector.close(); self.connector = self.make_connector()
        self.backend.extra = 20; self.connector.refresh_pending()
        self.assertEqual(self.connector.operation_status('direct:1:example')['status'], 'confirmed')
        self.assertEqual(self.backend.grants, 1)

    def policy_patch(self, mode='DAILY'):
        return {'timer_mode':mode,'forced_termination':True,
                'daily':{'limit_minutes':0,'bedtime':'20:00','morning':'06:00'},
                'week':{d:{'limit_minutes':i*15,'bedtime':'21:00','morning':'07:00'} for i,d in enumerate(('monday','tuesday','wednesday','thursday','friday','saturday','sunday'))}}

    def save_policy(self, patch=None, revision=None, operation='settings:1:test'):
        return self.connector.save_policy('nintendo:ABC',patch or self.policy_patch(),
                                          revision or self.backend.device['policy_revision'],operation)

    def test_zero_daily_policy_is_explicit_and_verified_without_changing_week(self):
        self.connect();week=copy.deepcopy(self.backend.device['native_policy']['eachDayOfTheWeekRegulations'])
        self.assertEqual(self.save_policy()['status'],'confirmed')
        self.assertEqual(self.backend.device['native_policy']['dailyRegulations']['timeToPlayInOneDay']['limitTime'],0)
        self.assertEqual(self.backend.device['native_policy']['eachDayOfTheWeekRegulations'],week)
        self.assertEqual(self.backend.policy_writes,1)
        self.save_policy();self.assertEqual(self.backend.policy_writes,1)

    def test_weekly_policy_sets_seven_days_and_preserves_common_regulation(self):
        self.connect();common=copy.deepcopy(self.backend.device['native_policy']['dailyRegulations'])
        self.assertEqual(self.save_policy(self.policy_patch('EACH_DAY_OF_THE_WEEK'))['status'],'confirmed')
        self.assertEqual(self.backend.device['native_policy']['dailyRegulations'],common)
        self.assertEqual(self.backend.device['native_policy']['eachDayOfTheWeekRegulations']['sunday']['timeToPlayInOneDay']['limitTime'],90)

    def test_policy_writes_refuse_stale_revision_and_existing_extra(self):
        self.connect()
        with self.assertRaises(nintendo.NintendoError): self.save_policy(revision='stale')
        self.backend.extra=5
        with self.assertRaises(nintendo.NintendoError): self.save_policy()
        self.assertEqual(self.backend.policy_writes,0)
        self.assertEqual(self.backend.cancels,0)

    def test_unknown_policy_response_is_verified_by_target_read_without_replay(self):
        self.connect();self.backend.mode='timeout'
        self.assertEqual(self.save_policy()['status'],'confirmed')
        self.connector.refresh_pending();self.save_policy()
        self.assertEqual(self.backend.policy_writes,1)

    def test_lagging_policy_does_not_claim_saved_or_resend_after_restart(self):
        self.connect();self.backend.mode='delayed'
        self.assertEqual(self.save_policy()['status'],'pending')
        self.connector.close();self.connector=self.make_connector();self.connector.refresh_pending()
        self.assertEqual(self.backend.policy_writes,1)
        self.assertEqual(self.connector.operation_status('settings:1:test')['status'],'pending')

    def test_invalid_policy_does_not_create_or_send_an_operation(self):
        self.connect()
        for field,value in (('timer_mode','other'),('forced_termination','true'),('week',{})):
            patch=self.policy_patch('EACH_DAY_OF_THE_WEEK');patch[field]=value
            with self.assertRaises(ValueError):self.save_policy(patch)
        patch=self.policy_patch();patch['daily']['limit_minutes']=True
        with self.assertRaises(ValueError):self.save_policy(patch)
        self.assertEqual(self.backend.policy_writes,0)
        self.assertIsNone(self.connector.operation_status('settings:1:test'))

    def test_explicit_policy_conflict_does_not_block_future_operations_as_uncertain(self):
        self.connect()
        class Conflict(Exception): status_code=409
        async def reject(*args):raise Conflict()
        self.backend.policy=reject
        self.assertEqual(self.save_policy()['status'],'failed')
        self.assertEqual(self.grant()['status'],'confirmed')

    def test_policy_verification_accepts_equivalent_disabled_json(self):
        self.connect();original=self.backend.policy
        async def normalized(device_id,regulations):
            result=await original(device_id,regulations)
            self.backend.device['native_policy']['dailyRegulations']['timeToPlayInOneDay'].pop('limitTime',None)
            self.backend.device['policy_revision']=nintendo._policy_revision(self.backend.device['native_policy'])
            return result
        self.backend.policy=normalized
        patch=self.policy_patch();patch['daily']['limit_minutes']=-1
        self.assertEqual(self.save_policy(patch)['status'],'confirmed')

    def test_bedtime_flag_and_stage_committed_before_confirm(self):
        self.connect()
        self.backend.mode = 'next_step'
        original = self.backend.confirm
        async def inspect(device_id, minutes, *, with_bedtime):
            row = self.connector._db.execute('SELECT extend_bedtime,stage,ack FROM operations').fetchone()
            self.assertEqual(tuple(row), (1, 'confirm_sent', 0))
            return await original(device_id, minutes, with_bedtime=with_bedtime)
        self.backend.confirm = inspect
        self.assertEqual(self.grant(extend_bedtime=True)['status'], 'confirmed')


class SDKAdapterTests(unittest.TestCase):
    def test_sdk_confirmation_passes_explicit_bedtime_choice(self):
        class Api:
            calls=[]
            async def async_confirm_extra_playing_time(self,*args,**kwargs):
                self.calls.append((args,kwargs));return {'json':{'status':'TO_ADDED'}}
        backend=nintendo._NintendoBackend();backend.api=Api()
        asyncio.run(backend.confirm('ABC',15,with_bedtime=False))
        asyncio.run(backend.confirm('ABC',15,with_bedtime=True))
        self.assertEqual(backend.api.calls,[(('ABC',15),{'with_bedtime':False}),
                                          (('ABC',15),{'with_bedtime':True})])

    def test_additional_time_api_never_calls_automatic_bedtime_method(self):
        class Api:
            calls = []
            async def async_update_extra_playing_time(self, *args, **kwargs):
                self.calls.append((args, kwargs))
                return {'json': {'status': 'SUCCESS', 'nextStepDetail': {'bedtime': True}}}
            async def async_confirm_extra_playing_time(self, *args, **kwargs):
                raise AssertionError('Must never confirm bedtime expansion')
        backend = nintendo._NintendoBackend()
        backend.api = Api()
        asyncio.run(backend.grant('ABC', 20))
        asyncio.run(backend.cancel('ABC'))
        self.assertEqual(backend.api.calls, [(('ABC', 20), {}), (('ABC',), {'cancel': True})])

    def test_explicit_device_update_errors_are_not_swallowed(self):
        class Api:
            async def async_get_account_devices(self):
                return {'json': {'ownedDevices': [{'deviceId': 'ABC'}]}}
        class Device:
            @classmethod
            def from_device_response(cls, raw, api):
                return cls()
            async def update(self, now):
                raise TimeoutError('unsafe-HTTP-body')
        backend = nintendo._NintendoBackend()
        backend.api, backend._Device = Api(), Device
        with self.assertRaises(TimeoutError):
            asyncio.run(backend.snapshot())

    def test_real_sdk_bedtime_only_payload_and_hidden_alarms(self):
        try:
            from pynintendoparental.device import Device
        except ImportError:
            self.skipTest('Pinned SDK is installed in Docker and CI')
        class Api:
            _tz = 'Europe/Madrid'
            visibility = 'VISIBLE'
            async def async_get_account_devices(self):
                return {'json': {'ownedDevices': [{'deviceId': 'ABC', 'label': 'Switch de Martín',
                    'parentalControlSettingState': {'updatedAt': 1700000000000}}]}}
            def owned(self):
                return {'deviceId': 'ABC', 'device': {'platformGeneration': 'P00',
                    'alarmSetting': {'visibility': self.visibility},
                    'synchronizedParentalControlSetting': {'synchronizedAt': 1700000000000},
                    'extraPlayingTime': {'bedtime': {'endTime': {'hour': 21, 'minute': 20}},
                        'inOneDay': None, 'expiresAt': 1900000000000}},
                    'parentalControlSettingState': {'synchronizationStatus': 'SYNCHRONIZED'}}
            async def async_get_account_device(self, device_id):
                return {'json': {'ownedDevice': self.owned()}}
            async def async_get_device_daily_summaries(self, device_id):
                return {'json': {'dailySummaries': [{'date': datetime.now(ZoneInfo(self._tz)).strftime('%Y-%m-%d'), 'playingTime': 60}]}}
            async def async_get_device_monthly_summaries(self, device_id):
                return {'json': {'available': []}}
            async def async_get_device_parental_control_setting(self, device_id):
                return {'json': {'parentalControlSetting': {
                    'playTimerRegulations': {'restrictionMode': 'FORCED_TERMINATION', 'timerMode': 'DAILY',
                        'dailyRegulations': {'timeToPlayInOneDay': {'enabled': True, 'limitTime': 60},
                            'bedtime': {'enabled': True, 'endingTime': {'hour': 21, 'minute': 0},
                                'startingTime': {'hour': 6, 'minute': 0}}}},
                    'whitelistedApplicationList': []}, 'ownedDevice': self.owned()}}
        backend = nintendo._NintendoBackend()
        backend.api, backend._Device = Api(), Device
        snapshot = asyncio.run(backend.snapshot())[0]
        self.assertEqual(snapshot['daily_extra_minutes'], 0)
        self.assertEqual(snapshot['bedtime_extra_minutes'], 20)
        self.assertEqual(snapshot['extra_minutes'], 0)
        self.assertEqual(snapshot['budget_remaining_minutes'], 0)
        self.assertTrue(snapshot['has_extra'])
        self.assertEqual(snapshot['bedtime'], '21:20')
        self.assertEqual(snapshot['last_sync'], 1700000000)
        self.assertTrue(snapshot['can_grant'])
        backend.api.visibility = 'INVISIBLE'
        hidden = asyncio.run(backend.snapshot())[0]
        self.assertFalse(hidden['alarms_enabled'])
        self.assertTrue(hidden['can_grant'])
        self.assertIsNotNone(hidden['remaining_minutes'])

    def test_snapshot_zero_limit_five_official_minutes_with_hidden_or_missing_alarm(self):
        class Api:
            visibility = 'INVISIBLE'
            async def async_get_account_devices(self):
                return {'json': {'ownedDevices': [{'deviceId': 'ABC', 'label': 'Switch #1'}]}}
            async def async_get_device_parental_control_setting(self, device_id):
                device = {'extraPlayingTime': {'inOneDay': {'duration': 5}}}
                if self.visibility is not None:
                    device['alarmSetting'] = {'visibility': self.visibility}
                return {'json': {'parentalControlSetting': {'playTimerRegulations': {}},
                                 'ownedDevice': {'device': device}}}
        class Device:
            device_id, name, model = 'ABC', 'Switch #1', 'Switch'
            forced_termination_mode = True
            extra = {}
            daily_summaries = []
            @classmethod
            def from_device_response(cls, raw, api): return cls()
            async def update(self, now): pass
            def _parse_parental_control_setting(self, pcs, now): pass
            def _get_today_regulation(self, now):
                return {'timeToPlayInOneDay': {'enabled': True, 'limitTime': 0},
                        'bedtime': {'enabled': True, 'endingTime': {'hour': 20, 'minute': 0}}}
        backend = nintendo._NintendoBackend()
        backend.api, backend._Device = Api(), Device
        for visibility, expected in (('INVISIBLE', False), ('VISIBLE', True), (None, None), ('UNKNOWN', None)):
            backend.api.visibility = visibility
            snapshot = asyncio.run(backend.snapshot())[0]
            self.assertEqual(snapshot['limit_minutes'], 0)
            self.assertEqual(snapshot['extra_minutes'], 5)
            self.assertEqual(snapshot['alarms_enabled'], expected)
            self.assertTrue(snapshot['can_grant'])
            self.assertIsNotNone(snapshot['remaining_minutes'])


class SafeErrorTests(unittest.TestCase):
    def test_update_required_asks_for_new_image_not_reconnect(self):
        class HttpException(Exception):
            status_code, error_code = 400, 'update_required'
        message = nintendo._safe_error(HttpException('HTTP 400: secreto (update_required)'))
        self.assertIn('Actualiza la imagen Docker', message)
        self.assertNotIn('secreto', message)

    def test_other_400_still_asks_to_reconnect(self):
        class HttpException(Exception):
            status_code, error_code = 400, 'invalid_grant'
        self.assertIn('Vuelve a conectar', nintendo._safe_error(HttpException()))


if __name__ == '__main__':
    unittest.main()

"""Direct Nintendo Switch parental controls, with durable additive operations.

The HTTP app owns authorization. This module owns credentials, OAuth state,
fresh Nintendo reads and the rule that an uncertain additive write is never
sent a second time. Permanent limits stay unchanged. Today's bedtime may be
extended only when the approval explicitly includes that option.
"""

import asyncio
import copy
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import secrets
import security
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


MINUTES = (5, 10, 15, 20, 25, 30, 40, 60)
CACHE_SECONDS = 20
LOGIN_SECONDS = 600
_REJECTED = {
    'NO_EFFECT': 'Nintendo no permite añadir más tiempo hoy.',
    'OVERTIME_ERROR': 'El tiempo solicitado excede el horario de descanso.',
    'DURING_LATE_NIGHT_ERROR': 'Nintendo mantiene el bloqueo nocturno.',
    'FAILED': 'Nintendo ha rechazado la operación.',
}


class NintendoError(RuntimeError):
    """A message safe for the browser and application logs."""


def _safe_error(error):
    # Never stringify an SDK exception: it can contain HTTP bodies or tokens.
    if isinstance(error, NintendoError):
        return str(error)
    status = getattr(error, 'status_code', None)
    name = type(error).__name__
    # Nintendo rejects outdated app versions; reconnecting the account does not help.
    if getattr(error, 'error_code', None) == 'update_required':
        return 'Nintendo exige una versión más reciente de la integración. Actualiza la imagen Docker de Parental.'
    if status in (400, 401, 403) or name in ('InvalidSessionTokenException', 'InvalidOAuthConfigurationException'):
        return 'Nintendo ha rechazado la sesión. Vuelve a conectar la cuenta.'
    if isinstance(error, (TimeoutError, asyncio.TimeoutError)) or 'Timeout' in name:
        return 'Nintendo no ha respondido a tiempo. Comprueba la conexión del servidor.'
    if name in ('ClientConnectorError', 'ClientConnectorDNSError', 'ClientConnectionError', 'ClientSSLError') or isinstance(error, OSError):
        return 'No se puede conectar con Nintendo desde este servidor.'
    if isinstance(error, ImportError):
        return 'La imagen no incluye la integración Nintendo. Actualiza la imagen Docker.'
    if status == 429:
        return 'Nintendo ha limitado las consultas. Espera antes de volver a intentarlo.'
    if isinstance(status, int):
        return f'Nintendo ha respondido con HTTP {status}.'
    return 'No se ha podido obtener una respuesta válida de Nintendo.'


def _number(value, *, nullable=False, minimum=0):
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < minimum:
        raise NintendoError('Nintendo ha devuelto datos incompletos; no se puede conceder tiempo.')
    return value


def _response_payload(response):
    if not isinstance(response, dict) or not isinstance(response.get('json'), dict):
        raise NintendoError('Nintendo ha devuelto una respuesta incompleta.')
    return response['json']


def _time_minutes(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{2}:\d{2}', value):
        raise NintendoError('Nintendo no ha enviado un horario de descanso válido.')
    hour, minute = (int(part) for part in value.split(':'))
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise NintendoError('Nintendo no ha enviado un horario de descanso válido.')
    return hour * 60 + minute


def _api_time(value):
    if not isinstance(value, dict) or type(value.get('hour')) is not int or type(value.get('minute')) is not int:
        raise NintendoError('Nintendo ha enviado una ampliación del horario incompleta.')
    result = f"{value['hour']:02d}:{value['minute']:02d}"
    _time_minutes(result)
    return result


def _timestamp(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        return None
    return value / 1000 if value >= 100000000000 else value


def _revision(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _policy_revision(value):
    # Nintendo may omit disabled values instead of returning explicit nulls.
    # Verify the effective policy, not an incidental JSON representation.
    def regulation(raw):
        raw = raw or {}
        timer, night = raw.get('timeToPlayInOneDay', {}), raw.get('bedtime', {})
        return {'limit': timer.get('limitTime') if timer.get('enabled') else None,
                'bedtime': night.get('endingTime') if night.get('enabled') else None,
                'morning': night.get('startingTime') if night.get('enabled') else None}
    return _revision({'mode':value.get('timerMode'),'restriction':value.get('restrictionMode'),
                      'daily':regulation(value.get('dailyRegulations')),
                      'week':{day:regulation(raw) for day,raw in value.get('eachDayOfTheWeekRegulations',{}).items()}})


def _policy_patch(patch, current):
    if not isinstance(patch, dict) or set(patch) != {'timer_mode', 'forced_termination', 'daily', 'week'}:
        raise ValueError('Configuración Nintendo incompleta.')
    mode = patch['timer_mode']
    if mode not in ('DAILY', 'EACH_DAY_OF_THE_WEEK') or type(patch['forced_termination']) is not bool:
        raise ValueError('Modo Nintendo inválido.')
    def regulation(value, previous):
        if not isinstance(value, dict) or set(value) != {'limit_minutes', 'bedtime', 'morning'}:
            raise ValueError('Introduce presupuesto, hora tope e inicio de la mañana.')
        limit = value['limit_minutes']
        if type(limit) is not int or not -1 <= limit <= 360:
            raise ValueError('El presupuesto habitual debe estar entre 0 y 360 minutos; -1 significa sin límite.')
        bedtime, morning = value['bedtime'], value['morning']
        if bedtime is not None:
            if not 16 * 60 <= _time_minutes(bedtime) <= 23 * 60 or not 5 * 60 <= _time_minutes(morning) <= 9 * 60:
                raise ValueError('La hora tope debe estar entre 16:00 y 23:00 y la mañana entre 05:00 y 09:00.')
        result = copy.deepcopy(previous)
        result['timeToPlayInOneDay'] = {'enabled': limit >= 0, 'limitTime': limit if limit >= 0 else None}
        result['bedtime'] = {'enabled': bedtime is not None,
                            'endingTime': {'hour': _time_minutes(bedtime)//60, 'minute': _time_minutes(bedtime)%60} if bedtime else None,
                            'startingTime': {'hour': _time_minutes(morning)//60, 'minute': _time_minutes(morning)%60} if bedtime else previous.get('bedtime', {}).get('startingTime', {'hour': 6, 'minute': 0})}
        return result
    result = copy.deepcopy(current)
    result['timerMode'] = mode
    result['restrictionMode'] = 'FORCED_TERMINATION' if patch['forced_termination'] else 'ALARM'
    if mode == 'DAILY':
        result['dailyRegulations'] = regulation(patch['daily'], current.get('dailyRegulations', {}))
    else:
        names = {'monday','tuesday','wednesday','thursday','friday','saturday','sunday'}
        if not isinstance(patch['week'], dict) or set(patch['week']) != names:
            raise ValueError('Completa los siete días de la semana.')
        previous = current.get('eachDayOfTheWeekRegulations', {})
        result['eachDayOfTheWeekRegulations'] = {day: regulation(patch['week'][day], previous.get(day, {})) for day in names}
    return result


class _NintendoBackend:
    """SDK adapter. All its objects stay on one persistent asyncio loop."""

    def __init__(self):
        self.session = self.auth = self.api = None
        self.timezone = 'Europe/Madrid'

    async def _open(self):
        import aiohttp
        from pynintendoparental import Authenticator
        from pynintendoparental.api import Api
        from pynintendoparental.device import Device
        # These libraries log raw account data and HTTP bodies in debug mode.
        # Keep third-party logs private and use our sanitized diagnostics.
        for name in ('pynintendoauth', 'pynintendoparental'):
            logger = logging.getLogger(name)
            logger.handlers = [logging.NullHandler()]
            logger.propagate = False
        self._Authenticator, self._Api, self._Device = Authenticator, Api, Device
        if self.session is None:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15), trust_env=True)

    async def begin_login(self):
        await self._open()
        self.auth = self._Authenticator(client_session=self.session)
        return self.auth.login_url

    async def complete_login(self, response_url, timezone):
        await self.auth.async_complete_login(response_url)
        self.timezone = timezone
        self.api = self._Api(self.auth, timezone, 'es-ES')
        return {'session_token': self.auth.session_token, 'account_id': self.auth.account_id, 'timezone': timezone}

    async def restore(self, credentials):
        await self._open()
        self.auth = self._Authenticator(session_token=credentials['session_token'], client_session=self.session)
        await self.auth.async_complete_login(use_session_token=True)
        if str(self.auth.account_id) != str(credentials['account_id']):
            raise NintendoError('La cuenta Nintendo no coincide con la conexión guardada. Vuelve a conectarla.')
        self.timezone = credentials['timezone']
        self.api = self._Api(self.auth, self.timezone, 'es-ES')

    async def snapshot(self):
        payload = _response_payload(await self.api.async_get_account_devices())
        owned = payload.get('ownedDevices')
        if not isinstance(owned, list):
            raise NintendoError('Nintendo no ha enviado una lista válida de consolas.')
        result = []
        now = datetime.now(ZoneInfo(self.timezone))
        for raw in owned:
            # NintendoParental._get_devices catches per-device errors. Use an
            # explicit Device.update so incomplete reads abort the operation.
            device = self._Device.from_device_response(raw, self.api)
            await device.update(now=now)
            if getattr(device, 'stats_update_failed', False):
                raise NintendoError('Nintendo no ha podido calcular los tiempos actuales de la consola.')
            pcs = _response_payload(await self.api.async_get_device_parental_control_setting(device.device_id))
            settings = pcs.get('parentalControlSetting')
            if not isinstance(settings, dict) or not isinstance(settings.get('playTimerRegulations'), dict):
                raise NintendoError('Nintendo no ha enviado la configuración actual de la consola.')
            device._parse_parental_control_setting(copy.deepcopy(pcs), now)
            regulation = device._get_today_regulation(now)
            if not isinstance(regulation, dict) or not isinstance(regulation.get('timeToPlayInOneDay'), dict):
                raise NintendoError('No se ha podido obtener el límite diario de Nintendo.')
            timer = regulation['timeToPlayInOneDay']
            if not isinstance(timer.get('enabled'), bool):
                raise NintendoError('Nintendo no ha enviado el estado del límite diario.')
            limit = _number(timer.get('limitTime'), minimum=0) if timer['enabled'] else -1
            owned_device = pcs.get('ownedDevice', {}).get('device', {})
            if not isinstance(owned_device, dict) or 'extraPlayingTime' not in owned_device:
                raise NintendoError('Nintendo no ha enviado el estado del tiempo extra.')
            extra_raw = owned_device['extraPlayingTime']
            daily_extra = 0
            if extra_raw is not None:
                if not isinstance(extra_raw, dict):
                    raise NintendoError('Nintendo ha enviado un tiempo extra incompleto.')
                in_one_day = extra_raw.get('inOneDay')
                if in_one_day is not None:
                    if not isinstance(in_one_day, dict):
                        raise NintendoError('Nintendo ha enviado un tiempo extra diario incompleto.')
                    daily_extra = -1 if in_one_day.get('isInfinity') is True else _number(in_one_day.get('duration'))
            summaries = device.daily_summaries
            if not isinstance(summaries, list):
                raise NintendoError('Nintendo no ha enviado el uso diario de la consola.')
            used = 0
            for summary in summaries:
                if not isinstance(summary, dict) or not isinstance(summary.get('date'), str):
                    raise NintendoError('Nintendo ha enviado un registro diario incompleto.')
                if summary['date'] == now.strftime('%Y-%m-%d'):
                    used = _number(summary.get('playingTime') or 0)
                    break
            bedtime_setting = regulation.get('bedtime', {})
            bedtime = None
            if bedtime_setting.get('enabled'):
                ending = bedtime_setting.get('endingTime')
                if not isinstance(ending, dict):
                    raise NintendoError('Nintendo no ha enviado el horario de descanso.')
                bedtime = _api_time(ending)
            base_bedtime = bedtime
            bedtime_extra = 0
            if extra_raw and extra_raw.get('bedtime'):
                if not bedtime:
                    raise NintendoError('El tiempo extra de Nintendo no coincide con el horario de descanso actual.')
                effective = _api_time(extra_raw['bedtime'].get('endTime'))
                bedtime_extra = (_time_minutes(effective) - _time_minutes(bedtime)) % 1440
                bedtime = effective
            # A clock extension is not a daily budget extension. Preserve the
            # two native dimensions instead of adding bedtime minutes to play.
            extra = daily_extra
            remaining = None if limit == -1 or daily_extra == -1 else max(0, limit + daily_extra - used)
            bedtime_remaining = None
            if bedtime:
                bedtime_minutes = _time_minutes(bedtime)
                end = now.replace(hour=bedtime_minutes // 60, minute=bedtime_minutes % 60, second=0, microsecond=0)
                if end <= now and bedtime_minutes < 360 and now.hour >= 6:
                    end += timedelta(days=1)
                bedtime_remaining = max(0, int((end - now).total_seconds() / 60))
            playable = min(remaining, bedtime_remaining) if remaining is not None and bedtime_remaining is not None else remaining
            alarm = owned_device.get('alarmSetting', device.extra.get('alarmSetting', {}))
            visibility = alarm.get('visibility') if isinstance(alarm, dict) else None
            alarms_enabled = {'VISIBLE': True, 'INVISIBLE': False}.get(visibility)
            # Alarm visibility is informational. The official app can manage
            # today's extra time even when alarms are hidden or unavailable.
            # Do not turn that state into a local rejection of a cloud write.
            grant_reason = ('No hay un límite diario activo; no se puede ampliar un presupuesto sin límite.' if limit < 0
                            else 'El tiempo extra de hoy ya es ilimitado en Nintendo.' if extra < 0 else '')
            bedtime_start = _api_time(bedtime_setting['startingTime']) if bedtime_setting.get('enabled') and bedtime_setting.get('startingTime') else None
            sync = pcs.get('ownedDevice', {}).get('parentalControlSettingState', {}).get('synchronizationStatus')
            result.append({
                'id': str(device.device_id), 'key': 'nintendo:' + str(device.device_id),
                'name': str(raw.get('label') or device.name or 'Nintendo Switch'), 'model': str(device.model),
                'used_minutes': used, 'remaining_minutes': remaining, 'limit_minutes': limit,
                'extra_minutes': extra, 'bedtime': bedtime,
                'base_bedtime': base_bedtime, 'effective_bedtime': bedtime,
                'bedtime_start': bedtime_start, 'bedtime_extra_minutes': bedtime_extra,
                'daily_extra_minutes': daily_extra, 'bedtime_remaining_minutes': bedtime_remaining,
                'budget_remaining_minutes': -1 if limit == -1 or daily_extra == -1 else remaining, 'playable_minutes_now': playable,
                'has_extra': extra_raw is not None,
                'extra_kind': 'both' if daily_extra and bedtime_extra else 'bedtime_only' if bedtime_extra else 'daily' if daily_extra else 'none',
                'extra_expires_at': _timestamp(extra_raw.get('expiresAt')) if extra_raw else None,
                'timer_mode': settings['playTimerRegulations'].get('timerMode'),
                'weekly_limits': copy.deepcopy(settings['playTimerRegulations'].get('eachDayOfTheWeekRegulations', {})),
                'daily_regulation': copy.deepcopy(settings['playTimerRegulations'].get('dailyRegulations', {})),
                'native_policy': {key: copy.deepcopy(value) for key, value in settings['playTimerRegulations'].items()
                                  if key in ('timerMode', 'restrictionMode', 'dailyRegulations', 'eachDayOfTheWeekRegulations')},
                'forced_termination': bool(device.forced_termination_mode),
                'alarms_enabled': alarms_enabled,
                'grant_unavailable_reason': grant_reason,
                'last_sync': _timestamp(owned_device.get('synchronizedParentalControlSetting', {}).get('synchronizedAt')),
                'console_sync_pending': sync != 'SYNCHRONIZED', 'available': True,
                'can_grant': limit >= 0 and extra >= 0,
            })
            result[-1]['policy_revision'] = _policy_revision(result[-1]['native_policy'])
        return result

    async def grant(self, device_id, minutes):
        # Never use Device.add_extra_time: it confirms withBedtime=True.
        return await self.api.async_update_extra_playing_time(device_id, minutes)

    async def cancel(self, device_id):
        return await self.api.async_update_extra_playing_time(device_id, cancel=True)

    async def confirm(self, device_id, minutes, *, with_bedtime):
        return await self.api.async_confirm_extra_playing_time(device_id, minutes, with_bedtime=with_bedtime)

    async def policy(self, device_id, regulations):
        return await self.api.async_update_play_timer(device_id, regulations)

    async def close(self):
        if self.session is not None:
            await self.session.close()
            self.session = None


class Connector:
    """Synchronous thread-safe facade used by the standard-library HTTP app."""

    def __init__(self, folder, *, backend_factory=None, clock=None):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self._path = self.folder / 'nintendo.json'
        self._factory = backend_factory or _NintendoBackend
        self._clock = clock or time.time
        self._lock = threading.RLock()
        self._loop = self._thread = self._backend = None
        self._logins = {}
        self._cache, self._updated, self._attempted, self._last_error = [], 0, 0, ''
        self._credentials = None
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text())
                self._check_credentials(data)
                self._credentials = data
                os.chmod(self._path, 0o600)
            except (OSError, ValueError, KeyError, TypeError, NintendoError):
                self._last_error = 'La conexión Nintendo guardada no es válida. Vuelve a conectar la cuenta.'
        db_path = self.folder / 'nintendo-operations.db'
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        os.chmod(db_path, 0o600)
        self._db.row_factory = sqlite3.Row
        self._db.execute('''CREATE TABLE IF NOT EXISTS operations (
            operation_id TEXT PRIMARY KEY, device_key TEXT NOT NULL,
            kind TEXT NOT NULL, minutes INTEGER NOT NULL,
            baseline REAL, expected REAL, status TEXT NOT NULL,
            message TEXT NOT NULL, account_hash TEXT NOT NULL,
            day TEXT NOT NULL, ack INTEGER NOT NULL DEFAULT 0,
            source_operation TEXT, cancelled INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL, updated_at REAL NOT NULL)''')
        # Additive migrations preserve earlier uncertainty and credentials.
        columns = {row['name'] for row in self._db.execute('PRAGMA table_info(operations)')}
        for column, definition in (
            ('extend_bedtime', 'INTEGER NOT NULL DEFAULT 0'),
            ('stage', "TEXT NOT NULL DEFAULT 'update_sent'"),
            ('baseline_bedtime', 'TEXT'), ('base_bedtime', 'TEXT'),
            ('expected_bedtime', 'TEXT'), ('baseline_daily_extra', 'REAL'),
            ('baseline_limit', 'REAL'), ('baseline_suspended', 'INTEGER'),
            ('baseline_forced', 'INTEGER'), ('night_only', 'INTEGER NOT NULL DEFAULT 0'),
            ('model_version', 'INTEGER NOT NULL DEFAULT 1'), ('expected_daily', 'REAL'),
            ('chunks', 'TEXT'), ('chunk_index', 'INTEGER NOT NULL DEFAULT 0'),
            ('confirmed_minutes', 'INTEGER NOT NULL DEFAULT 0'), ('native_expires_at', 'REAL'),
            ('expected_policy', 'TEXT'), ('policy_payload_hash', 'TEXT'),
            ('confirmation_with_bedtime', 'INTEGER'),
            ('native_response_status', 'TEXT'), ('native_response_at', 'REAL'),
        ):
            if column not in columns:
                self._db.execute(f'ALTER TABLE operations ADD COLUMN {column} {definition}')
        self._db.commit()
        # Older versions left a known, unfinished two-step request uncertain
        # when parsing its bedtime estimate failed. No confirm was sent in
        # those cases. Release ONLY that evidence-backed case, never a lost
        # update or confirm response. New interrupted pre-confirm stages are
        # also safe to close without sending anything on restart.
        self._db.execute('''UPDATE operations SET status='failed',message=?,updated_at=?
            WHERE status='pending' AND kind='grant' AND ack=0 AND (
                stage='confirmation_received' OR (stage='update_sent' AND
                (message LIKE 'Nintendo ha enviado una ampliación del horario incompleta.%'
                 OR message LIKE 'Nintendo no ha enviado un horario de descanso válido.%')))''',
            ('Nintendo pidió una confirmación que no llegó a enviarse. No se ha reenviado la operación; puedes iniciar una nueva concesión.', self._clock()))
        self._db.commit()

    @staticmethod
    def _check_credentials(data):
        if not isinstance(data, dict) or not isinstance(data.get('session_token'), str) or not data['session_token']:
            raise NintendoError('No se ha recibido una sesión Nintendo válida.')
        if not isinstance(data.get('account_id'), str) or not data['account_id']:
            raise NintendoError('Nintendo no ha identificado la cuenta.')
        try:
            ZoneInfo(data['timezone'])
        except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError) as error:
            raise NintendoError('La zona horaria no es válida.') from error

    def _run(self, coroutine):
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(target=self._loop.run_forever, name='nintendo-connector', daemon=True)
            self._thread.start()
        async def bounded():
            return await asyncio.wait_for(coroutine, timeout=45)
        return asyncio.run_coroutine_threadsafe(bounded(), self._loop).result(timeout=50)

    def _save(self, credentials):
        self._check_credentials(credentials)
        data = {key: credentials[key] for key in ('session_token', 'account_id', 'timezone')}
        temp = self.folder / ('.nintendo-' + secrets.token_hex(12))
        try:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump(data, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, self._path)
        finally:
            if temp.exists():
                temp.unlink()
        self._credentials = data

    def _account(self):
        if self._credentials is None:
            return ''
        return hashlib.sha256(self._credentials['account_id'].encode()).hexdigest()

    def _today(self):
        timezone = self._credentials['timezone'] if self._credentials else 'Europe/Madrid'
        return datetime.fromtimestamp(self._clock(), ZoneInfo(timezone)).strftime('%Y-%m-%d')

    def public_config(self):
        with self._lock:
            return {'configured': self._credentials is not None,
                    'timezone': self._credentials['timezone'] if self._credentials else 'Europe/Madrid',
                    'error': self._last_error, 'updated_at': self._updated,
                    'allowed_minutes': list(MINUTES)}

    def _close_backend(self, backend):
        if backend is not None:
            try:
                self._run(backend.close())
            except Exception:
                pass

    def _pending(self, device_key=None):
        # A daily permission cannot survive Nintendo's next local calendar day.
        self._db.execute("UPDATE operations SET status='failed',message=?,updated_at=? WHERE status='pending' AND day<>?", (
            'No se pudo confirmar el permiso antes de finalizar el día; no se ha reenviado.', self._clock(), self._today()))
        self._db.commit()
        query = "SELECT * FROM operations WHERE status='pending'"
        params = []
        if device_key is not None:
            query += ' AND device_key=? AND account_hash=?'
            params = [device_key, self._account()]
        return self._db.execute(query, params).fetchall()

    def begin_login(self):
        with self._lock:
            if self._pending():
                raise NintendoError('Hay una operación pendiente. Espera a que se resuelva antes de cambiar la conexión Nintendo.')
            for state in self._logins.values():
                self._close_backend(state['backend'])
            self._logins.clear()
            backend = self._factory()
            try:
                login_url = self._run(backend.begin_login())
                query = parse_qs(urlparse(login_url).query)
                oauth_state = query['state'][0]
                redirect = query['redirect_uri'][0]
                if urlparse(login_url).scheme != 'https' or urlparse(login_url).hostname != 'accounts.nintendo.com':
                    raise NintendoError('Nintendo no ha enviado una dirección de acceso válida.')
                state_id = secrets.token_urlsafe(32)
                self._logins[state_id] = {'backend': backend, 'expires': self._clock() + LOGIN_SECONDS,
                                          'oauth_state': oauth_state, 'redirect': redirect}
                return {'login_url': login_url, 'state_id': state_id, 'expires_in': LOGIN_SECONDS}
            except Exception as error:
                self._close_backend(backend)
                raise NintendoError(_safe_error(error)) from None

    def complete_login(self, state_id, response_url, timezone='Europe/Madrid'):
        with self._lock:
            # A grant can be created while the user is on Nintendo's login page.
            if self._pending():
                raise NintendoError('Hay una operación pendiente. No se cambiará la cuenta Nintendo hasta resolverla.')
            state = self._logins.get(state_id) if isinstance(state_id, str) else None
            if not state or state['expires'] <= self._clock():
                if state:
                    self._logins.pop(state_id, None)
                    self._close_backend(state['backend'])
                raise NintendoError('El enlace de acceso ha caducado. Inicia la conexión Nintendo otra vez.')
            try:
                ZoneInfo(timezone)
            except (ValueError, TypeError, ZoneInfoNotFoundError):
                raise NintendoError('La zona horaria no es válida.') from None
            if not isinstance(response_url, str) or len(response_url) > 8192:
                raise NintendoError('Pega el enlace de respuesta del acceso Nintendo.')
            parsed = urlparse(response_url)
            expected = urlparse(state['redirect'])
            query = parse_qs(parsed.fragment or parsed.query)
            if (parsed.scheme, parsed.netloc, parsed.path) != (expected.scheme, expected.netloc, expected.path):
                raise NintendoError('El enlace no es la respuesta de acceso Nintendo esperada.')
            if query.get('state') != [state['oauth_state']] or not query.get('session_token_code'):
                raise NintendoError('La respuesta no corresponde al acceso iniciado. Abre el enlace generado otra vez.')
            self._logins.pop(state_id, None)
            backend = state['backend']
            try:
                credentials = self._run(backend.complete_login(response_url, timezone))
                self._save(credentials)
                self._close_backend(self._backend)
                self._backend = backend
                self._cache, self._updated, self._attempted, self._last_error = [], 0, 0, ''
                result = self.devices(force=True)
                result['timezone'] = timezone
                return result
            except Exception as error:
                self._close_backend(backend)
                raise NintendoError(_safe_error(error)) from None

    def _ensure_backend(self):
        if self._credentials is None:
            raise NintendoError('Conecta primero tu cuenta Nintendo.')
        if self._backend is None:
            backend = self._factory()
            try:
                self._run(backend.restore(self._credentials))
                self._backend = backend
            except Exception:
                self._close_backend(backend)
                raise

    def _read(self):
        self._attempted = self._clock()
        self._ensure_backend()
        devices = self._run(self._backend.snapshot())
        if not isinstance(devices, list):
            raise NintendoError('Nintendo no ha enviado una lista válida de consolas.')
        seen = set()
        for device in devices:
            if not isinstance(device, dict) or not isinstance(device.get('id'), str) or not device['id']:
                raise NintendoError('Nintendo ha enviado una consola sin identificar.')
            if device.get('key') != 'nintendo:' + device['id'] or device['key'] in seen:
                raise NintendoError('Nintendo ha enviado una lista de consolas inconsistente.')
            seen.add(device['key'])
            for key in ('used_minutes', 'remaining_minutes', 'limit_minutes', 'extra_minutes'):
                _number(device.get(key), nullable=key in ('remaining_minutes', 'extra_minutes'), minimum=-1 if key in ('limit_minutes', 'extra_minutes') else 0)
            if 'daily_extra_minutes' in device:
                _number(device['daily_extra_minutes'], minimum=-1)
            if device.get('available') is not True:
                raise NintendoError('La consola Nintendo no tiene datos actuales; no se puede conceder tiempo.')
            device['last_sync'] = _timestamp(device.get('last_sync'))
        self._cache, self._updated, self._last_error = copy.deepcopy(devices), self._clock(), ''
        self._reconcile(devices)
        return devices

    def _decorate(self, devices):
        result = copy.deepcopy(devices)
        for device in result:
            device['last_read_at'] = self._updated
            pending = self._pending(device['key'])
            device['pending_operation'] = self._result(pending[0]) if pending else None
            device['can_grant'] = bool(device.get('can_grant')) and not pending
            device['can_cancel'] = self._has_extra(device) and not pending
            latest = self._db.execute('''SELECT * FROM operations WHERE device_key=?
                AND account_hash=? AND day=? ORDER BY created_at DESC,rowid DESC LIMIT 1''',
                (device['key'], self._account(), self._today())).fetchone()
            device['last_operation'] = self._result(latest) if latest else None
        return result

    def devices(self, force=False):
        with self._lock:
            if self._credentials is None:
                return {'configured': False, 'devices': [], 'error': self._last_error, 'updated_at': 0}
            if force or not self._attempted or self._clock() - self._attempted >= CACHE_SECONDS:
                try:
                    self._read()
                except Exception as error:
                    self._last_error = _safe_error(error)
            visible = copy.deepcopy(self._cache)
            if self._last_error:
                for device in visible:
                    device.update(available=False, can_grant=False, can_cancel=False)
            return {'configured': True, 'devices': self._decorate(visible), 'error': self._last_error, 'updated_at': self._updated}

    def _validate(self, device_key, minutes, *, allow_pending=False, for_cancel=False):
        if type(minutes) is not int or minutes not in MINUTES:
            raise ValueError('Selecciona una concesión de 5, 10, 15, 20, 25, 30, 40 o 60 minutos.')
        if not isinstance(device_key, str) or not device_key.startswith('nintendo:'):
            raise ValueError('Selecciona una consola Nintendo válida.')
        try:
            devices = self._read()
        except Exception as error:
            raise NintendoError(_safe_error(error)) from None
        device = next((item for item in devices if item['key'] == device_key), None)
        if device is None:
            raise ValueError('Esta consola no está vinculada a la cuenta Nintendo conectada.')
        if not for_cancel and (device['limit_minutes'] < 0 or device['extra_minutes'] is None or device['extra_minutes'] < 0):
            raise NintendoError(device.get('grant_unavailable_reason') or 'La consola no tiene un límite diario activo compatible con tiempo extra.')
        if not allow_pending and self._pending(device_key):
            raise NintendoError('Hay una concesión pendiente en esta consola. No se añadirá más tiempo hasta resolverla.')
        return device

    def validate(self, device_key, minutes):
        with self._lock:
            return self._decorate([self._validate(device_key, minutes)])[0]

    def _preflight(self, device, minutes, extend_bedtime):
        daily = device.get('daily_extra_minutes', device['extra_minutes'])
        budget_after = device['limit_minutes'] + daily + minutes - device['used_minutes']
        if budget_after <= 0:
            raise NintendoError('El tiempo solicitado no alcanza para superar el uso ya registrado. Elige una duración mayor dentro de tu límite de aprobación.')
        # A budget need not fit entirely before bedtime. Nintendo supports
        # confirming ONLY the daily budget with withBedtime=False; the hard
        # cutoff remains active. The UI explains the usable window rather
        # than forcing a parent to authorize a bedtime change.

    @staticmethod
    def _operation_id(operation_id):
        if not isinstance(operation_id, str) or not re.fullmatch(r'[A-Za-z0-9:_-]{1,150}', operation_id):
            raise ValueError('Identificador de operación no válido.')

    def _result(self, row):
        return {'operation_id': row['operation_id'], 'device_key': row['device_key'],
                'kind': row['kind'], 'minutes': row['minutes'], 'status': row['status'],
                'message': row['message'], 'cancelled': bool(row['cancelled']),
                'extend_bedtime': bool(row['extend_bedtime']), 'stage': row['stage'],
                'confirmed_minutes': row['confirmed_minutes'] if row['model_version'] == 2 else (row['minutes'] if row['status'] == 'confirmed' and row['kind'] == 'grant' else 0),
                'steps_total': len(json.loads(row['chunks'])) if row['chunks'] else 1,
                'steps_confirmed': row['chunk_index'] if row['model_version'] == 2 else int(row['status'] == 'confirmed'),
                'night_only': bool(row['night_only']),
                'confirmation_with_bedtime': None if row['confirmation_with_bedtime'] is None else bool(row['confirmation_with_bedtime']),
                'last_step_acknowledged': bool(row['ack']),
                'baseline_daily_extra_minutes': row['baseline_daily_extra'] if row['baseline_daily_extra'] is not None else row['baseline'],
                'expected_daily_extra_minutes': row['expected_daily'] if row['model_version'] == 2 else row['expected'],
                'baseline_bedtime': row['baseline_bedtime'], 'expected_bedtime': row['expected_bedtime'],
                'native_response_status': row['native_response_status'], 'native_response_at': row['native_response_at'],
                'waiting_seconds': max(0, int(self._clock() - row['created_at'])),
                'created_at': row['created_at'], 'updated_at': row['updated_at']}

    def operation_status(self, operation_id):
        with self._lock:
            self._operation_id(operation_id)
            row = self._db.execute('SELECT * FROM operations WHERE operation_id=?', (operation_id,)).fetchone()
            return self._result(row) if row else None

    def _finish(self, operation_id, status, message, *, ack=None):
        if ack is None:
            self._db.execute('UPDATE operations SET status=?,message=?,updated_at=? WHERE operation_id=?', (status, message, self._clock(), operation_id))
        else:
            self._db.execute('UPDATE operations SET status=?,message=?,ack=?,updated_at=? WHERE operation_id=?', (status, message, int(ack), self._clock(), operation_id))
        self._db.commit()
        return self.operation_status(operation_id)

    def _reconcile(self, devices):
        by_key = {device['key']: device for device in devices}
        for row in self._pending():
            if row['account_hash'] != self._account():
                continue
            if row['day'] != self._today() or (row['native_expires_at'] and self._clock() >= row['native_expires_at']):
                self._finish(row['operation_id'], 'failed', 'No se pudo confirmar el permiso antes de finalizar el día; no se ha reenviado.')
                continue
            device = by_key.get(row['device_key'])
            if not device or device.get('available') is not True:
                continue
            if row['kind'] == 'policy':
                if device.get('policy_revision') == row['expected_policy']:
                    self._finish(row['operation_id'], 'confirmed', 'Configuración habitual confirmada por Nintendo.')
                continue
            extra = device.get('extra_minutes')
            # Read-after-write can lag. An ACK plus exactly the expected total
            # confirms it. A lost response has no remote operation identifier;
            # even a matching total cannot safely be attributed to this write.
            if row['ack']:
                baseline_limit = row['baseline_limit']
                current_base = device.get('base_bedtime', device.get('bedtime'))
                if ((baseline_limit is not None and device['limit_minutes'] != baseline_limit)
                        or (row['base_bedtime'] != current_base)
                        or (row['baseline_forced'] is not None and bool(device.get('forced_termination')) != bool(row['baseline_forced']))):
                    self._finish(row['operation_id'], 'failed', 'La configuración actual de Nintendo entra en conflicto con el permiso. No se reenviará la operación ni se modificarán las restricciones permanentes.')
                    continue
            bedtime = device.get('effective_bedtime', device.get('bedtime'))
            daily = device.get('daily_extra_minutes', extra)
            expected_daily = row['expected_daily'] if row['model_version'] == 2 else row['expected']
            baseline_daily = row['baseline_daily_extra'] if row['baseline_daily_extra'] is not None else row['baseline']
            # A later cloud value can supersede an accepted additive request.
            # Do NOT label that as our grant confirmed or continue its plan:
            # another controller may have changed today's bonus. An old read
            # (the baseline) still waits, as does any lost write response.
            if row['kind'] == 'grant' and row['ack'] and (
                    daily == -1 or (daily is not None and expected_daily is not None and daily > expected_daily)
                    or (daily is not None and baseline_daily is not None and daily < baseline_daily)
                    or bedtime not in (row['baseline_bedtime'], row['expected_bedtime'])):
                self._finish(row['operation_id'], 'superseded',
                             'El estado de Nintendo ha cambiado y ya no coincide con esta concesión. Seguimiento cerrado sin reenviar minutos; se muestra el presupuesto actual de Nintendo.')
                continue
            if row['kind'] == 'grant' and row['stage'] == 'ready_next' and (
                    daily != expected_daily or bedtime != row['expected_bedtime']):
                self._finish(row['operation_id'], 'superseded',
                             'Nintendo ha cambiado el permiso tras un paso confirmado. Se han detenido los pasos restantes; revisa el presupuesto actual antes de conceder más.')
                continue
            daily_matches = (device.get('daily_extra_minutes', extra) == row['expected_daily']) if row['model_version'] == 2 else extra == row['expected']
            if row['ack'] and daily_matches and bedtime == row['expected_bedtime']:
                if row['model_version'] == 2 and row['kind'] == 'grant':
                    chunks = json.loads(row['chunks'])
                    count = row['chunk_index'] + 1
                    confirmed = row['confirmed_minutes'] + chunks[row['chunk_index']]
                    self._db.execute('UPDATE operations SET chunk_index=?,confirmed_minutes=?,ack=0,stage=? WHERE operation_id=?',
                                     (count, confirmed, 'ready_next' if count < len(chunks) else 'complete', row['operation_id']))
                    self._db.commit()
                    if count < len(chunks):
                        self._finish(row['operation_id'], 'pending', f'{confirmed} de {row["minutes"]} minutos confirmados. Preparando el siguiente paso aprobado.')
                        continue
                message = 'Tiempo extra confirmado en Nintendo.' if row['kind'] == 'grant' else 'Tiempo extra cancelado en Nintendo.'
                if row['kind'] == 'grant' and row['confirmation_with_bedtime'] == 0:
                    message = 'Tiempo extra confirmado en Nintendo; se conserva la hora tope de hoy.'
                if row['kind'] == 'grant' and row['night_only']:
                    message = 'Nintendo ha confirmado la ampliación de la hora tope; el presupuesto diario se muestra por separado.'
                self._finish(row['operation_id'], 'confirmed', message)
                if row['kind'] == 'cancel':
                    self._db.execute('''UPDATE operations SET cancelled=1 WHERE
                        device_key=? AND account_hash=? AND day=? AND kind='grant'
                        AND status='confirmed' AND created_at<=?''',
                        (row['device_key'], row['account_hash'], row['day'], row['created_at']))
                    self._db.commit()

    def refresh_pending(self):
        with self._lock:
            if self._credentials and self._pending():
                self.devices(force=True)
                # ready_next is a new, already-approved step, never a resend.
                for row in list(self._pending()):
                    if row['model_version'] == 2 and row['stage'] == 'ready_next':
                        self._advance(row)
            return [self._result(row) for row in self._db.execute('SELECT * FROM operations WHERE status=?', ('pending',)).fetchall()]

    def close_tracking(self, device_key, operation_id, *, acknowledge_uncertain,
                       expected_daily_extra_minutes, expected_bedtime):
        """Close local tracking after an adult checks Nintendo; NEVER mutate it."""
        with self._lock:
            if acknowledge_uncertain is not True:
                raise ValueError('Confirma que has revisado Nintendo y que cerrar el seguimiento no retira ni añade tiempo.')
            self._operation_id(operation_id)
            _number(expected_daily_extra_minutes, minimum=-1)
            if expected_bedtime is not None:
                _time_minutes(expected_bedtime)
            row = self._db.execute('SELECT * FROM operations WHERE operation_id=?', (operation_id,)).fetchone()
            if not row or row['device_key'] != device_key or row['account_hash'] != self._account():
                raise ValueError('El seguimiento no corresponde a esta consola y cuenta.')
            if row['status'] != 'pending':
                return self._result(row)  # idempotent, without closing a newer operation
            device = self._validate(device_key, 5, for_cancel=True, allow_pending=True)
            current = self._db.execute('SELECT * FROM operations WHERE operation_id=?', (operation_id,)).fetchone()
            if current['status'] != 'pending':
                return self._result(current)
            if (device.get('daily_extra_minutes', device['extra_minutes']) != expected_daily_extra_minutes
                    or device.get('effective_bedtime', device.get('bedtime')) != expected_bedtime):
                raise NintendoError('El tiempo o la hora tope han cambiado. Actualiza y revisa Nintendo antes de cerrar el seguimiento.')
            self._db.execute("UPDATE operations SET stage='closed_by_user' WHERE operation_id=?", (operation_id,))
            return self._finish(operation_id, 'superseded',
                                'Seguimiento cerrado por un responsable. No se ha confirmado esta concesión ni se ha añadido, retirado o reenviado tiempo. Se conserva el estado actual de Nintendo.')

    def _existing(self, operation_id, device_key, minutes, kind, extend_bedtime=False):
        self._operation_id(operation_id)
        row = self._db.execute('SELECT * FROM operations WHERE operation_id=?', (operation_id,)).fetchone()
        if row:
            if row['device_key'] != device_key or row['minutes'] != minutes or row['kind'] != kind or bool(row['extend_bedtime']) != extend_bedtime:
                raise ValueError('El identificador de operación ya corresponde a otra acción.')
            return self._result(row)
        return None

    def _insert(self, operation_id, device, minutes, kind, source=None, extend_bedtime=False):
        now = self._clock()
        baseline = device['extra_minutes']
        expected = baseline + minutes if kind == 'grant' else 0
        try:
            self._db.execute('BEGIN IMMEDIATE')
            if self._db.execute("SELECT 1 FROM operations WHERE device_key=? AND account_hash=? AND status='pending'", (device['key'], self._account())).fetchone():
                raise NintendoError('Hay otra operación pendiente en esta consola.')
            self._db.execute('''INSERT INTO operations
                (operation_id,device_key,kind,minutes,baseline,expected,status,message,
                 account_hash,day,source_operation,created_at,updated_at,extend_bedtime,
                 baseline_bedtime,base_bedtime,expected_bedtime,baseline_daily_extra,
                 baseline_limit,baseline_suspended,baseline_forced)
                VALUES(?,?,?,?,?,?,'pending',?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (operation_id, device['key'], kind, minutes, baseline, expected,
                 'Operación enviada; esperando confirmación de Nintendo.', self._account(), self._today(), source, now, now,
                 int(extend_bedtime), device.get('effective_bedtime', device.get('bedtime')), device.get('base_bedtime', device.get('bedtime')),
                 device.get('base_bedtime', device.get('bedtime')) if kind == 'cancel' else device.get('effective_bedtime', device.get('bedtime')),
                 device.get('daily_extra_minutes', baseline), device['limit_minutes'],
                 int(bool(device.get('restrictions_suspended'))), int(bool(device.get('forced_termination')))))
            chunks = [30, 5, 5] if minutes == 40 else [minutes]
            self._db.execute('''UPDATE operations SET model_version=2,chunks=?,expected_daily=?,native_expires_at=? WHERE operation_id=?''',
                             (json.dumps(chunks), device.get('daily_extra_minutes', baseline) + chunks[0] if kind == 'grant' else 0,
                              device.get('extra_expires_at'), operation_id))
            self._db.commit()  # full versioned plan durable BEFORE any write
        except Exception:
            self._db.rollback()
            raise

    def _confirm_bedtime(self, operation_id, device, minutes, payload):
        row = self._db.execute('SELECT * FROM operations WHERE operation_id=?', (operation_id,)).fetchone()
        # Receiving nextStepDetail means updateExtraPlayingTime has NOT
        # finished. Confirming the budget and extending bedtime are separate
        # choices. Record this distinction before a read or a process crash.
        self._db.execute("UPDATE operations SET stage='confirmation_received',ack=0,updated_at=? WHERE operation_id=?", (self._clock(), operation_id))
        self._db.commit()
        detail = payload.get('nextStepDetail')
        changes = detail.get('estimatedBedtimeChanges') if isinstance(detail, dict) else None
        from_time = to_time = None
        if row['extend_bedtime'] and isinstance(changes, dict):
            try:
                from_time, to_time = _api_time(changes.get('from')), _api_time(changes.get('to'))
            except NintendoError:
                # An unparseable optional estimate cannot authorize a clock
                # change. We can still confirm the budget with bedtime=False.
                pass
        with_bedtime = bool(from_time and to_time and from_time != to_time)
        try:
            return self._send_confirmation(row, device, minutes, from_time, to_time, with_bedtime)
        except Exception as error:
            current = self._db.execute('SELECT stage FROM operations WHERE operation_id=?', (operation_id,)).fetchone()
            if current['stage'] != 'confirmation_received':
                raise  # A confirm MAY have been sent: keep the uncertain result.
            return self._finish(operation_id, 'failed', _safe_error(error) + ' No se ha enviado la confirmación ni se ha ampliado el descanso.')

    def _send_confirmation(self, row, device, minutes, from_time, to_time, with_bedtime):
        operation_id = row['operation_id']
        # A fresh read between API calls prevents confirming against policy
        # or extra time changed by another parent meanwhile.
        fresh = next((item for item in self._read() if item['key'] == device['key']), None)
        keys = ('daily_extra_minutes', 'limit_minutes', 'forced_termination')
        if (not fresh or any(fresh.get(key) != device.get(key) for key in keys)
                or fresh.get('base_bedtime', fresh.get('bedtime')) != row['base_bedtime']
                or fresh.get('effective_bedtime', fresh.get('bedtime')) != row['baseline_bedtime']):
            return self._finish(operation_id, 'failed', 'Las restricciones han cambiado durante la aprobación. No se ha enviado la confirmación.')
        self._preflight(fresh, minutes, bool(row['extend_bedtime']))
        if not with_bedtime:
            expected = fresh.get('daily_extra_minutes', fresh['extra_minutes']) + minutes
            self._db.execute('''UPDATE operations SET stage='confirm_sent',expected=?,expected_daily=?,
                expected_bedtime=?,night_only=0,confirmation_with_bedtime=0,ack=0,updated_at=? WHERE operation_id=?''',
                (expected, expected, row['baseline_bedtime'], self._clock(), operation_id))
            self._db.commit()  # durable BEFORE confirming; never resend on timeout
            return _response_payload(self._run(self._backend.confirm(device['id'], minutes, with_bedtime=False)))
        if fresh.get('effective_bedtime', fresh.get('bedtime')) != from_time:
            return self._finish(operation_id, 'failed', 'El horario propuesto por Nintendo no coincide con el actual. No se ha confirmado la ampliación.')
        extension = (_time_minutes(to_time) - _time_minutes(from_time)) % 1440
        now = datetime.fromtimestamp(self._clock(), ZoneInfo(self._credentials['timezone']))
        scheduled = now.replace(hour=_time_minutes(from_time)//60, minute=_time_minutes(from_time)%60, second=0, microsecond=0)
        if now.hour < 6 and scheduled.hour >= 6:
            scheduled -= timedelta(days=1)
        elif now.hour >= 6 and scheduled.hour < 6 and scheduled <= now:
            scheduled += timedelta(days=1)
        proposed_end = scheduled + timedelta(minutes=extension)
        usable_extension = (proposed_end - max(scheduled, now)).total_seconds() / 60
        if not extension or not 0 < usable_extension <= minutes:
            return self._finish(operation_id, 'failed', 'La ampliación propuesta por Nintendo excede los minutos aprobados. No se ha confirmado.')
        start = fresh.get('bedtime_start')
        if start and _time_minutes(to_time) < 360 and _time_minutes(to_time) >= _time_minutes(start):
            return self._finish(operation_id, 'failed', 'El horario propuesto entra en conflicto con el inicio del siguiente día. No se ha confirmado.')
        daily_budget = max(0, fresh['limit_minutes'] + fresh.get('daily_extra_minutes', fresh['extra_minutes']) - fresh['used_minutes'])
        night_only = daily_budget >= minutes
        expected = fresh.get('daily_extra_minutes', fresh['extra_minutes']) + (0 if night_only else minutes)
        self._db.execute('''UPDATE operations SET stage='confirm_sent',expected_bedtime=?,
            expected=?,expected_daily=?,night_only=?,confirmation_with_bedtime=1,ack=0,updated_at=? WHERE operation_id=?''',
            (to_time, expected, expected, int(night_only), self._clock(), operation_id))
        self._db.commit()  # includes the explicit flag BEFORE second mutation
        return _response_payload(self._run(self._backend.confirm(device['id'], minutes, with_bedtime=True)))

    def _mutate(self, operation_id, device, minutes, kind):
        try:
            response = self._run(self._backend.grant(device['id'], minutes) if kind == 'grant' else self._backend.cancel(device['id']))
            payload = _response_payload(response)
            if payload.get('status') in _REJECTED:
                return self._finish(operation_id, 'failed', _REJECTED[payload['status']])
            if payload.get('nextStepDetail') is not None:
                if payload.get('status') not in ('SUCCESS', 'TO_ADDED'):
                    return self._finish(operation_id, 'pending', 'Nintendo ha enviado una confirmación con un estado desconocido. No se reenviará la operación.')
                if kind != 'grant':
                    return self._finish(operation_id, 'failed', 'Nintendo pide confirmar un cambio adicional para cancelar. No se ha autorizado ese cambio.')
                confirmed_payload = self._confirm_bedtime(operation_id, device, minutes, payload)
                if 'operation_id' in confirmed_payload:
                    return confirmed_payload
                payload = confirmed_payload
                if payload.get('nextStepDetail') is not None:
                    return self._finish(operation_id, 'pending', 'Nintendo ha solicitado otra confirmación inesperada. No se reenviará la operación.')
            status = payload.get('status')
            if status in _REJECTED:
                return self._finish(operation_id, 'failed', _REJECTED[status])
            accepted = ('SUCCESS', 'TO_ADDED') if kind == 'grant' else ('SUCCESS', 'TO_CANCELED')
            if status not in accepted:
                return self._finish(operation_id, 'pending', 'Nintendo no ha confirmado el resultado. La operación no se reenviará automáticamente.')
            self._db.execute('UPDATE operations SET native_response_status=?,native_response_at=? WHERE operation_id=?',
                             (status, _timestamp(payload.get('updatedAt')), operation_id))
            self._finish(operation_id, 'pending', 'Nintendo ha aceptado la operación; esperando que la lectura confirme el tiempo.', ack=True)
        except Exception as error:
            status = str(getattr(error, 'status', ''))
            if status in _REJECTED:
                return self._finish(operation_id, 'failed', _REJECTED[status])
            if getattr(error, 'status_code', None) in (400, 401, 403, 404, 409, 422):
                return self._finish(operation_id, 'failed', _safe_error(error) + ' Nintendo ha rechazado la operación.')
            # Network failures and unrecognised responses can happen AFTER a
            # write. Never report them as safely retryable failures.
            return self._finish(operation_id, 'pending', _safe_error(error) + ' El resultado es incierto y no se reenviará la operación.')
        try:
            self._read()
        except Exception:
            security.log_failure('Nintendo: lectura posterior a la operación', logging.WARNING)
        result = self.operation_status(operation_id)
        latest = next((item for item in self._cache if item['key'] == device['key']), device)
        result['console_sync_pending'] = bool(latest.get('console_sync_pending'))
        return result

    def grant(self, device_key, minutes, operation_id, *, extend_bedtime=False):
        with self._lock:
            if type(extend_bedtime) is not bool:
                raise ValueError('La opción de ampliar el descanso debe ser verdadera o falsa.')
            prior = self._existing(operation_id, device_key, minutes, 'grant', extend_bedtime)
            if prior:
                return prior
            device = self._validate(device_key, minutes)
            self._preflight(device, minutes, extend_bedtime)
            self._insert(operation_id, device, minutes, 'grant', extend_bedtime=extend_bedtime)
            return self._mutate(operation_id, device, 30 if minutes == 40 else minutes, 'grant')

    def save_policy(self, device_key, patch, revision, operation_id):
        with self._lock:
            payload_hash = _revision(patch)
            prior = self._existing(operation_id, device_key, 0, 'policy')
            if prior:
                row = self._db.execute('SELECT policy_payload_hash FROM operations WHERE operation_id=?', (operation_id,)).fetchone()
                if row['policy_payload_hash'] != payload_hash:
                    raise ValueError('El identificador ya corresponde a otros ajustes.')
                return prior
            device = self._validate(device_key, 5, for_cancel=True)
            if not isinstance(device.get('native_policy'), dict) or revision != device.get('policy_revision'):
                raise NintendoError('Los ajustes han cambiado en Nintendo. Actualiza antes de guardar.')
            target = _policy_patch(patch, device['native_policy'])
            if self._has_extra(device):
                raise NintendoError('Retira primero la ampliación de hoy antes de cambiar los ajustes habituales. No se retirará automáticamente.')
            self._insert(operation_id, device, 0, 'policy')
            self._db.execute("UPDATE operations SET stage='policy_sent',expected_policy=?,policy_payload_hash=? WHERE operation_id=?",
                             (_policy_revision(target), payload_hash, operation_id));self._db.commit()
            try:
                _response_payload(self._run(self._backend.policy(device['id'], target)))
                self._finish(operation_id, 'pending', 'Ajustes enviados; esperando la lectura de Nintendo.', ack=True)
            except Exception as error:
                rejected = getattr(error, 'status_code', None) in (400, 401, 403, 404, 409, 422)
                self._finish(operation_id, 'failed' if rejected else 'pending', _safe_error(error) + (' Nintendo ha rechazado los ajustes.' if rejected else ' No se reenviarán los ajustes automáticamente.'))
            self.devices(force=True)
            return self.operation_status(operation_id)

    def _advance(self, row):
        sent = False
        try:
            fresh = next((d for d in self._read() if d['key'] == row['device_key']), None)
            current = self._db.execute('SELECT * FROM operations WHERE operation_id=?', (row['operation_id'],)).fetchone()
            if current['status'] != 'pending' or current['stage'] != 'ready_next':
                return self._result(current)
            if (not fresh or row['day'] != self._today() or
                    fresh.get('daily_extra_minutes', fresh.get('extra_minutes')) != row['expected_daily'] or
                    fresh.get('effective_bedtime', fresh.get('bedtime')) != row['expected_bedtime'] or
                    fresh.get('base_bedtime', fresh.get('bedtime')) != row['base_bedtime'] or
                    fresh['limit_minutes'] != row['baseline_limit'] or
                    bool(fresh.get('forced_termination')) != bool(row['baseline_forced'])):
                raise NintendoError('Los ajustes o el tiempo extra han cambiado antes del siguiente paso.')
            chunks = json.loads(row['chunks'])
            minutes = chunks[row['chunk_index']]
            self._preflight(fresh, row['minutes'] - row['confirmed_minutes'], bool(row['extend_bedtime']))
            self._db.execute('''UPDATE operations SET stage='update_sent',ack=0,expected_daily=?,expected=?,baseline_daily_extra=?,
                baseline_bedtime=?,confirmation_with_bedtime=NULL,night_only=0,updated_at=? WHERE operation_id=?''',
                             (row['expected_daily'] + minutes, row['expected_daily'] + minutes, row['expected_daily'],
                              row['expected_bedtime'], self._clock(), row['operation_id']))
            self._db.commit()  # crash after this point must never repeat this step
            sent = True
            return self._mutate(row['operation_id'], fresh, minutes, 'grant')
        except Exception as error:
            return self._finish(row['operation_id'], 'pending' if sent else 'failed', f'{row["confirmed_minutes"]} de {row["minutes"]} minutos confirmados. ' + _safe_error(error))

    @staticmethod
    def _has_extra(device):
        extra = device.get('extra_minutes')
        return device.get('available') is True and (bool(device.get('has_extra')) or (extra is not None and (extra > 0 or extra == -1)) or device.get('bedtime_extra_minutes', 0) > 0)

    def _cancel_source(self, device):
        if not device.get('available') or device.get('extra_minutes') is None or device.get('extra_minutes', 0) <= 0:
            return None
        return self._db.execute('''SELECT * FROM operations WHERE
            device_key=? AND account_hash=? AND day=? AND kind='grant'
            AND status='confirmed' AND cancelled=0 AND baseline=0 AND expected=?
            ORDER BY created_at DESC LIMIT 1''',
            (device['key'], self._account(), self._today(), device['extra_minutes'])).fetchone()

    def cancel(self, device_key, operation_id, *, cancel_all_today=False, expected_extra_minutes=None, expected_bedtime=None):
        with self._lock:
            if type(cancel_all_today) is not bool:
                raise ValueError('La confirmación de retirar todo el tiempo debe ser verdadera o falsa.')
            if cancel_all_today:
                _number(expected_extra_minutes, minimum=-1)
                if expected_extra_minutes != -1 and expected_extra_minutes < 0:
                    raise ValueError('No hay tiempo extra que retirar.')
                if expected_bedtime is not None:
                    _time_minutes(expected_bedtime)
            prior = self._existing(operation_id, device_key, 0, 'cancel')
            if prior:
                return prior
            device = self._validate(device_key, 5, for_cancel=True)
            if not self._has_extra(device):
                raise NintendoError('Nintendo no tiene tiempo extra para retirar hoy.')
            source = self._cancel_source(device)
            if cancel_all_today:
                if device['extra_minutes'] != expected_extra_minutes or device.get('effective_bedtime', device.get('bedtime')) != expected_bedtime:
                    raise NintendoError('El tiempo extra o el horario ha cambiado en Nintendo. Actualiza los datos y confirma de nuevo qué quieres retirar.')
            elif source is None:
                raise NintendoError('Nintendo retira todo el tiempo extra del día, incluido el concedido desde la app oficial. Confirma expresamente que quieres retirarlo todo.')
            self._insert(operation_id, device, 0, 'cancel', source['operation_id'] if source else None)
            return self._mutate(operation_id, device, 0, 'cancel')

    def disconnect(self):
        with self._lock:
            if self._pending():
                raise NintendoError('Hay una operación pendiente. No se puede eliminar la conexión hasta resolverla.')
            self._close_backend(self._backend)
            self._backend = None
            for state in self._logins.values():
                self._close_backend(state['backend'])
            self._logins.clear()
            self._path.unlink(missing_ok=True)
            self._credentials = None
            self._cache, self._updated, self._attempted, self._last_error = [], 0, 0, ''
            return self.public_config()

    def close(self):
        """Release resources on application shutdown; credentials are retained."""
        with self._lock:
            self._close_backend(self._backend)
            self._backend = None
            for state in self._logins.values():
                self._close_backend(state['backend'])
            self._logins.clear()
            self._db.close()
            if self._loop is not None:
                self._loop.call_soon_threadsafe(self._loop.stop)
                self._thread.join(timeout=2)
                self._loop.close()
                self._loop = self._thread = None

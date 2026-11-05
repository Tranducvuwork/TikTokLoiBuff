"""Persist the number of shared proxies this machine should lease."""
import json
import os
import socket
import threading
import urllib.request
import inspect
from pathlib import Path

CONFIG_PATH = Path.home() / '.auto-like-cmt' / 'machine_proxy_allocation.json'
POOL_PATH = Path.home() / '.auto-like-cmt' / 'local_pool_data.json'
_lock = threading.RLock()
SERVER_URL = 'http://127.0.0.1:19802/api/client/machine-proxy-allocation'
CAPTCHA_PATH = Path.home() / '.auto-like-cmt' / 'pool_captcha_settings.json'

def get_captcha_settings():
    import tiktok_reply as tt
    try:
        enabled = bool(json.loads(CAPTCHA_PATH.read_text(encoding='utf-8')).get('enabled', True))
    except (OSError, ValueError):
        enabled = True
    return {'ok': True, 'enabled': enabled, 'solver_ready': tt.has_captcha_key()}

def set_captcha_settings(enabled):
    if not isinstance(enabled, bool):
        return {'ok': False, 'error': 'Trạng thái captcha không hợp lệ.'}
    with _lock:
        CAPTCHA_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = CAPTCHA_PATH.with_suffix('.tmp')
        temporary.write_text(json.dumps({'enabled': enabled}), encoding='utf-8')
        os.replace(temporary, CAPTCHA_PATH)
    return get_captcha_settings()

def server_config(count=None):
    payload = None if count is None else json.dumps({'count': count}).encode('utf-8')
    request = urllib.request.Request(SERVER_URL, data=payload, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=3) as response:
        result = json.load(response)
    if result.get('ok') and (not isinstance(result.get('count'), int) or not isinstance(result.get('total'), int) or not result.get('machine')):
        raise ValueError('Server allocation response is missing fields')
    return result


def pool_total():
    try:
        rows = json.loads(POOL_PATH.read_text(encoding='utf-8')).get('proxies', [])
        return len(rows) if isinstance(rows, list) else 0
    except (OSError, ValueError, TypeError):
        return 0


def get_config():
    total = pool_total()
    fallback = max(1, total) if total else 21
    with _lock:
        try:
            count = json.loads(CONFIG_PATH.read_text(encoding='utf-8')).get('count', fallback)
            if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                count = fallback
            elif total > 3 and count <= 3:
                count = min(total, fallback)
        except (OSError, ValueError, TypeError, AttributeError):
            count = fallback
    return {'ok': True, 'machine': socket.gethostname(), 'count': count,
            'total': total, 'saved': CONFIG_PATH.exists(), 'server_saved': False}


def set_config(value):
    if isinstance(value, bool):
        return {'ok': False, 'error': 'Nhập số proxy nguyên dương.'}
    try:
        count = int(value)
    except (ValueError, TypeError, OverflowError):
        return {'ok': False, 'error': 'Nhập số proxy nguyên dương.'}
    if str(value).strip() != str(count) or count < 1:
        return {'ok': False, 'error': 'Nhập số proxy nguyên dương.'}
    try:
        with _lock:
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            temporary = CONFIG_PATH.with_suffix('.tmp')
            temporary.write_text(json.dumps({'count': count}), encoding='utf-8')
            os.replace(temporary, CONFIG_PATH)
            return get_config()
    except OSError as error:
        return {'ok': False, 'error': 'Không ghi được cấu hình proxy trên máy: ' + str(error)}


def install():
    from client.app.bridge import Bridge
    Bridge.get_machine_proxy_allocation = lambda self: get_config()
    Bridge.set_machine_proxy_allocation = lambda self, count: set_config(count)
    Bridge.get_captcha_balance = lambda self: get_captcha_balance()
    Bridge.get_pool_captcha_settings = lambda self: get_captcha_settings()
    Bridge.set_pool_captcha_settings = lambda self, enabled: set_captcha_settings(enabled)
    from client.app.runner import Runner
    if getattr(Runner, '_machine_proxy_limit_installed', False):
        return
    import atexit
    previous = Runner.run
    session = {'rows': [], 'limit': None, 'release': None}
    session_lock = threading.RLock()
    def clear_session():
        with session_lock:
            rows, release = session['rows'], session['release']
            session['rows'] = []
            session['limit'] = None
            if rows and release:
                try:
                    parameters = inspect.signature(release).parameters.values()
                    accepts_rows = any(p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.VAR_POSITIONAL) for p in parameters)
                except (TypeError, ValueError):
                    accepts_rows = False
                if accepts_rows: release(rows)
                else: release()
    atexit.register(clear_session)
    Bridge.clear_machine_proxy_session = lambda self: clear_session()
    original_save = Bridge.set_machine_proxy_allocation
    def save_count(self, count):
        result = original_save(self, count)
        if result.get('ok') and session['limit'] != result['count']:
            clear_session()
        return result
    Bridge.set_machine_proxy_allocation = save_count
    def run(self, accounts, proxies, opts, *args, **kwargs):
        limit = getattr(self, '_run_proxy_limit', None) or get_config()['count']
        proxies = list(dict.fromkeys(proxies))[:limit]
        acquire, release = self._proxy_acquire, self._proxy_release
        if acquire:
            try:
                parameters = inspect.signature(acquire).parameters
                supports_stop = 'stop_event' in parameters or any(
                    parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())
            except (TypeError, ValueError):
                supports_stop = False
            def retained(desired, **options):
                with session_lock:
                    if session['limit'] != limit:
                        clear_session()
                    if not session['rows']:
                        rows = acquire(limit, **options) if supports_stop else acquire(limit)
                        session.update(rows=list(rows or [])[:limit], limit=limit, release=release)
                    return list(session['rows'])
            self._proxy_acquire = retained
            self._proxy_release = lambda *args, **kwargs: None
        try:
            return previous(self, accounts, proxies, opts, *args, **kwargs)
        finally:
            self._proxy_acquire, self._proxy_release = acquire, release
    Runner.run = run
    Runner._machine_proxy_limit_installed = True

def get_captcha_balance():
    import requests
    from omo_captcha import load_omo_key, OMO_BASE
    from decimal import Decimal, ROUND_FLOOR
    key = load_omo_key()
    if not key: return {'ok': False}
    try:
        r = requests.post(OMO_BASE + '/getBalance', json={'clientKey': key}, timeout=12)
        r.raise_for_status()
        d = r.json()
        if d.get('errorId') != 0: return {'ok': False}
        if key.startswith('PKG_'):
            quantity = d.get('quantity')
            if quantity is None: return {'ok': False}
            return {'ok': True, 'remaining': max(0, int(quantity)), 'estimated': False}
        balance = Decimal(str(d.get('balance') or 0)) + Decimal(str(d.get('voucherBalance') or 0))
        count = max(0, int((balance / Decimal('0.0006')).to_integral_value(rounding=ROUND_FLOOR)))
        return {'ok': True, 'remaining': count, 'balance': str(balance), 'estimated': True}
    except Exception: return {'ok': False}

"""Keep validated browser sessions across runs without waiting for server reports."""
import json
import os
import threading
import time
from pathlib import Path

CACHE_PATH = Path.home() / '.auto-like-cmt' / 'browser_cookie_cache.json'
_lock = threading.RLock()
_write_lock = threading.Lock()
_generation = 0
_cached_data = {}
_cached_stamp = None
_dirty = False
_writer_started = False


def flush():
    global _dirty, _cached_stamp
    # Serialize writers, but keep browser threads out of JSON/disk work.
    # Dirty stays set until this exact generation is safely on disk.
    with _write_lock:
        with _lock:
            if not _dirty:
                return
            data = dict(_cached_data)
            generation = _generation
        temporary = CACHE_PATH.with_name(f'{CACHE_PATH.name}.{threading.get_ident()}.tmp')
        try:
            CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
            os.replace(temporary, CACHE_PATH)
            with _lock:
                info = CACHE_PATH.stat()
                _cached_stamp = (str(CACHE_PATH), info.st_mtime_ns, info.st_size)
                _dirty = _generation != generation
        except OSError:
            pass
        finally:
            temporary.unlink(missing_ok=True)


def _ensure_writer():
    global _writer_started
    if _writer_started:
        return
    with _lock:
        if _writer_started:
            return
        _writer_started = True
        import atexit
        atexit.register(flush)
        def _loop():
            while True:
                time.sleep(3.0)
                try:
                    flush()
                except Exception:
                    pass
        threading.Thread(target=_loop, daemon=True, name='browser-cookie-cache-writer').start()


def _key(account):
    account_id = account.get('id')
    if account_id is not None:
        return 'id:' + str(account_id)
    username = str(account.get('login_user') or account.get('username') or '').strip().lstrip('@').casefold()
    return 'user:' + username if username else ''


def _read():
    global _cached_data, _cached_stamp
    if _dirty and isinstance(_cached_data, dict) and _cached_data:
        return _cached_data
    try:
        info = CACHE_PATH.stat()
        stamp = (str(CACHE_PATH), info.st_mtime_ns, info.st_size)
        if stamp == _cached_stamp:
            return _cached_data
        data = json.loads(CACHE_PATH.read_text(encoding='utf-8'))
        _cached_data = data if isinstance(data, dict) else {}
        _cached_stamp = stamp
        return _cached_data
    except (OSError, ValueError):
        return _cached_data if isinstance(_cached_data, dict) else {}


_STRIP_COOKIE_NAMES = frozenset({
    '_waftokenid',
    'perf_feed_cache',
})


def _sanitize_cookie_str(cookie: str) -> str:
    if not isinstance(cookie, str) or not cookie:
        return ''
    by_name = {}
    for part in cookie.split(';'):
        part = part.strip()
        if not part or '=' not in part:
            continue
        k, v = part.split('=', 1)
        k = k.strip()
        if not k or k in _STRIP_COOKIE_NAMES or k.startswith('_waf'):
            continue
        by_name[k] = f'{k}={v.strip()}'
    return '; '.join(by_name.values())


def restore(account):
    key = _key(account)
    if not key:
        return False
    with _lock:
        saved = _read().get(key, {})
        cookie = saved.get('cookie') if isinstance(saved, dict) else None
        if not isinstance(cookie, str) or not cookie:
            return False
        account['cookie'] = _sanitize_cookie_str(cookie)
        return True


def save(account, cookie, immediate=True):
    global _cached_data, _cached_stamp, _dirty, _generation
    key = _key(account)
    cookie = _sanitize_cookie_str(cookie)
    if not key or not cookie:
        return
    with _lock:
        data = _read()
        if isinstance(data.get(key), dict) and data[key].get('cookie') == cookie:
            return
        if not isinstance(_cached_data, dict) or not _cached_data:
            _cached_data = dict(data)
        _cached_data[key] = {'cookie': cookie, 'saved_at': time.time()}
        _generation += 1
        _dirty = True
    if immediate:
        flush()
    else:
        _ensure_writer()


def _capture(runner, bot, account):
    if getattr(bot, '_driver_unresponsive', False):
        return
    d = getattr(bot, 'driver', None)
    if d is None:
        return
    client_config = getattr(getattr(d, 'command_executor', None), '_client_config', None)
    old_timeout = getattr(client_config, 'timeout', None)
    try:
        if client_config is not None:
            client_config.timeout = 2.5
        import bot_login
        cookie = bot_login.harvest_cookie(bot)
        if cookie and any(part.strip().split('=', 1)[0] in ('sessionid', 'sessionid_ss', 'sid_tt')
                          and part.partition('=')[2] for part in cookie.split(';')):
            save(account, cookie, immediate=False)
            account['cookie'] = cookie
    except Exception:
        runner._log('[cookie] Không lưu được phiên vào đĩa; lần chạy này vẫn tiếp tục.')
    finally:
        if client_config is not None and old_timeout is not None and not getattr(bot, '_driver_unresponsive', False):
            try:
                client_config.timeout = old_timeout
            except Exception:
                pass


def install():
    from client.app.runner import Runner
    if getattr(Runner, '_disk_cookie_cache_installed', False):
        return
    original_run_one = Runner._run_one
    original_save = Runner._save_fresh_cookie
    original_relogin = Runner._try_relogin

    def run_one(self, acc, *args, **kwargs):
        if restore(acc):
            self._log('[cookie] Nạp phiên đã lưu; kiểm tra cookie trước khi đăng nhập lại.')
        return original_run_one(self, acc, *args, **kwargs)

    def save_fresh(self, bot, acc):
        try:
            return original_save(self, bot, acc)
        finally:
            _capture(self, bot, acc)

    def relogin(self, bot, acc, name):
        result = original_relogin(self, bot, acc, name)
        if result:
            _capture(self, bot, acc)
        return result

    Runner._run_one = run_one
    Runner._save_fresh_cookie = save_fresh
    Runner._try_relogin = relogin
    Runner._disk_cookie_cache_installed = True

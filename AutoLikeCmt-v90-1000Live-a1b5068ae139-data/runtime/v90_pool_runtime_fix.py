import json
import os
import shutil
import threading
import time
import uuid
from pathlib import Path

ROOT = Path.home() / '.auto-like-cmt'
LOCK = threading.RLock()

def read_pool():
    return json.loads((ROOT / 'local_pool_data.json').read_text(encoding='utf-8-sig'))

def delete_local(ids=None, status=None):
    with LOCK:
        import local_account_results as store
        store.flush()
        data = read_pool()
        selected = {str(x) for x in ids or []}
        removed = [a for a in data.get('accounts', []) if
                   (str(a.get('id')) in selected if status is None else str(a.get('status', '')).lower() == status)]
        removed_ids = {str(a.get('id')) for a in removed}
        data['accounts'] = [a for a in data.get('accounts', []) if str(a.get('id')) not in removed_ids]
        for name in ('local_pool_data.json', 'pool.json'):
            path = ROOT / name
            if path.exists():
                shutil.copy2(path, str(path) + '.backup-delete-' + time.strftime('%Y%m%d-%H%M%S'))
            temp = ROOT / (name + '.' + str(uuid.uuid4()) + '.tmp')
            temp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
            os.replace(temp, path)
        with store.LOCK:
            for account_id in removed_ids:
                store.PENDING.pop(account_id, None)
            for bridge in store.BRIDGES:
                bridge._pool = data
        cache_path = ROOT / 'browser_cookie_cache.json'
        if cache_path.exists():
            cache = json.loads(cache_path.read_text(encoding='utf-8-sig'))
            for account_id in removed_ids:
                cache.pop('id:' + account_id, None)
            temp = cache_path.with_suffix('.delete.tmp')
            temp.write_text(json.dumps(cache), encoding='utf-8')
            os.replace(temp, cache_path)
        return {'ok': True, 'deleted': len(removed)}


def import_local(lines, account_type='totp', section='accounts'):
    with LOCK:
        import local_account_results as store
        store.flush()
        data = read_pool()
        rows = list(data.get(section, []))
        max_id = max([int(a.get('id') or 0) for a in data.get('accounts', []) + data.get('comment_accounts', [])] + [6000])
        by_login = {}
        for i, a in enumerate(rows):
            lu = str(a.get('login_user') or a.get('user') or a.get('email') or '').strip().lower()
            if lu:
                by_login[lu] = i
        added = 0
        for raw in str(lines or '').splitlines():
            raw = raw.strip()
            if not raw:
                continue
            parts = [p.strip() for p in raw.split('|')]
            if len(parts) < 2 or parts[0].lower() in ('user', 'username', 'login_user'):
                continue
            login_user = parts[0]
            password = parts[1]
            email = ''
            mail_pass = ''
            totp_secret = ''
            if len(parts) >= 4 and '@' in parts[2]:
                email = parts[2]
                mail_pass = parts[3]
                if len(parts) >= 5:
                    totp_secret = parts[4]
            elif len(parts) >= 3:
                if '@' in parts[2]:
                    email = parts[2]
                else:
                    totp_secret = parts[2]
            if not email and '@' in login_user:
                email = login_user
            key = login_user.lower()
            if key in by_login:
                rec = rows[by_login[key]]
                rec['password'] = password
                if email:
                    rec['email'] = email
                if mail_pass:
                    rec['mail_pass'] = mail_pass
                if totp_secret:
                    rec['totp_secret'] = totp_secret
            else:
                max_id += 1
                rec = {
                    'id': max_id,
                    'username': login_user if login_user.startswith('@') else '',
                    'login_user': login_user,
                    'password': password,
                    'totp_secret': totp_secret,
                    'email': email,
                    'mail_pass': mail_pass,
                    'cookie': '',
                    'status': 'unknown',
                }
                by_login[key] = len(rows)
                rows.append(rec)
                added += 1
        data[section] = rows
        for name in ('local_pool_data.json', 'pool.json'):
            path = ROOT / name
            temp = ROOT / (name + '.' + str(uuid.uuid4()) + '.tmp')
            temp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
            os.replace(temp, path)
        with store.LOCK:
            for bridge in store.BRIDGES:
                bridge._pool = data
        return {'ok': True, 'added': added, 'count': added, 'total': len(rows)}


_GROUP=threading.local()
def set_proxy_group(rows=None):
    _GROUP.rows=rows if rows is None or callable(rows) else list(rows)

def _slot_key(p):
    if not isinstance(p, dict):
        return (None, None)
    user = str(p.get('user') or '').strip()
    if user:
        return (p.get('host'), p.get('port'), user)
    return (p.get('host'), p.get('port'))

def choose_available_proxy(candidates,loads,cursor,cap=2):
    if not candidates:return None,cursor
    ordered=candidates[cursor%len(candidates):]+candidates[:cursor%len(candidates)]
    usable=[p for p in ordered if loads.get(_slot_key(p),0)<cap and loads.get((p.get('host'),p.get('port')),0)<cap]
    if not usable:return None,cursor
    chosen=min(usable,key=lambda p:loads.get(_slot_key(p),0)+loads.get((p.get('host'),p.get('port')),0))
    return chosen,(candidates.index(chosen)+1)%len(candidates)

def install():
    import tiktok_reply as tt
    from client.app.bridge import Bridge
    original_start = tt.TikTokLikeBot.start_browser
    original_log = tt.TikTokLikeBot.log
    # Runner writes its requested proxy after start_browser; the allocator may
    # have picked another endpoint. Always report/blacklist the actual session.
    def last_proxy(bot):
        return getattr(bot, '_v90_actual_proxy', None) or getattr(bot, '_v90_requested_proxy', None)
    def set_last_proxy(bot, value):
        bot._v90_requested_proxy = value
    tt.TikTokLikeBot._last_proxy = property(last_proxy, set_last_proxy)
    failed = {}
    proxy_lock = threading.RLock()

    def log(bot, message):
        if any(marker in str(message) for marker in ('browser_network_error', 'login page network error', '⛔ 403')):
            actual = getattr(bot, '_v90_actual_proxy', None)
            if actual:
                endpoint = _slot_key(actual)
                with proxy_lock:
                    failed[endpoint] = time.monotonic() + 60
                tt.blacklist_proxy(actual, ttl=60)
                original_log(bot, f"[proxy] Tạm loại {actual.get('host')}:{actual.get('port')}; kiểm tra lại sau 60 giây trước khi đưa vào pool.")
        return original_log(bot, message)

    loads={}
    cursor=[0]
    original_close=tt.TikTokLikeBot.close
    # Only recent probes are used; report contains IDs/endpoints, never credentials.
    report=Path(r'C:\Users\PC\Downloads\tooltiktok\proxy-check-20261012.json')
    try:
        if time.time()-report.stat().st_mtime<300:
            checks={str(r.get('id')):r for r in json.loads(report.read_text(encoding='utf-8'))}
            for row in read_pool().get('proxies',[]):
                check=checks.get(str(row.get('id')), {})
                if check.get('internet',{}).get('result')!='ok' and check.get('tiktok',{}).get('result')!='ok' and check:
                    candidate=tt.parse_proxy(str(row.get('raw') or row.get('address') or ''))
                    if candidate:tt.blacklist_proxy(candidate,ttl=60)
    except (OSError,ValueError):pass

    def free(bot):
        with proxy_lock:
            endpoint=getattr(bot,'_v90_reserved_endpoint',None)
            if endpoint is not None:
                loads[endpoint]=max(0,loads.get(endpoint,0)-1)
                bot._v90_reserved_endpoint=None

    def start(bot, headless=False, session_idx=None, proxy=None, **kwargs):
        free(bot)
        if proxy is None:
            bot._v90_actual_proxy=None
            bot._v90_requested_proxy=None
            return original_start(bot,headless=headless,session_idx=session_idx,proxy=None,**kwargs)
        announced=False
        while True:
            bot._check_stop()
            with proxy_lock:
                now=time.monotonic()
                raw_rows=getattr(_GROUP,'rows',None)
                if callable(raw_rows):
                    raw_rows=raw_rows()
                if raw_rows is None:
                    raw_rows=[str(row.get('raw') or row.get('address') or '') for row in read_pool().get('proxies',[])]
                candidates=[];seen=set()
                for raw in raw_rows:
                    candidate=tt.parse_proxy(raw)
                    if not candidate:continue
                    endpoint=_slot_key(candidate)
                    key=tt._proxy_key(candidate)
                    if key not in seen and failed.get(endpoint,0)<=now and not tt._proxy_blocked(candidate):
                        seen.add(key);candidates.append(candidate)
                chosen,cursor[0]=choose_available_proxy(candidates,loads,cursor[0],cap=4)
                if chosen is not None:
                    endpoint=_slot_key(chosen)
                    loads[endpoint]=loads.get(endpoint,0)+1
                    bot._v90_reserved_endpoint=endpoint
                    bot._v90_actual_proxy=chosen
                    break
            if not announced:
                original_log(bot, '[proxy] Chờ slot hoặc proxy phục hồi; giữ acc trong lượt, bấm Dừng để hủy.')
                announced=True
            bot.stop_event.wait(.2)
        if tt._proxy_key(chosen)!=tt._proxy_key(proxy):
            original_log(bot,f"[proxy] Phân phối proxy {chosen.get('host')}:{chosen.get('port')}; tối đa 4 browser/địa chỉ.")
        try:
            result=original_start(bot,headless=headless,session_idx=session_idx,proxy=chosen,**kwargs)
            bot._last_proxy=chosen
            return result
        except Exception:
            free(bot)
            raise

    def close(bot,*args,**kwargs):
        try:return original_close(bot,*args,**kwargs)
        finally:free(bot)

    tt.TikTokLikeBot.log = log
    tt.TikTokLikeBot.start_browser = start
    tt.TikTokLikeBot.close = close

    def snapshot(self):
        self._pool = read_pool()
        return {'ok': True, 'accounts': [{k: a.get(k) for k in ('id', 'username', 'login_user', 'status')} for a in self._pool.get('accounts', [])],
                'proxies': [local_proxy_pool.public_row(row) for row in self._pool.get('proxies', [])]}

    def refresh(self, *args, **kwargs):
        snapshot(self)
        return {'ok': True, 'accounts': len(self._pool.get('accounts', [])), 'proxies': len(self._pool.get('proxies', []))}

    def delete(self, ids=None, status=None):
        result = delete_local(ids, status)
        snapshot(self)
        return result

    def import_accounts_fn(self, lines, account_type='totp'):
        result = import_local(lines, account_type=account_type, section='accounts')
        snapshot(self)
        return result

    def import_comment_accounts_fn(self, lines, account_type='totp'):
        result = import_local(lines, account_type=account_type, section='comment_accounts')
        snapshot(self)
        return result

    Bridge.local_pool_snapshot = snapshot
    Bridge.get_accounts = snapshot
    Bridge.local_delete_accounts = delete
    Bridge.import_accounts = import_accounts_fn
    Bridge.import_comment_accounts = import_comment_accounts_fn
    Bridge.refresh_pool = refresh
    import local_proxy_pool
    local_proxy_pool.install()

    try:
        import webview
        _orig_create_window = getattr(webview, 'create_window', None)
        if callable(_orig_create_window) and not getattr(webview, '_v93_title_patched', False):
            def _create_window_v93(title, *a, **kw):
                if isinstance(title, str) and 'Auto-Like-Cmt' in title:
                    title = 'Auto-Like-Cmt v94'
                return _orig_create_window(title, *a, **kw)
            webview.create_window = _create_window_v93
            webview._v93_title_patched = True
    except Exception:
        pass


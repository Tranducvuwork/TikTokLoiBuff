
_retry_pool_stamp=None
_retry_pool_accounts={}
def _retry_proxy_plan(account_id):
    global _retry_pool_stamp, _retry_pool_accounts
    import json
    from pathlib import Path
    p=Path.home()/'.auto-like-cmt'/'local_pool_data.json'
    try:
        stamp=p.stat().st_mtime_ns
        if stamp!=_retry_pool_stamp:
            data=json.loads(p.read_text(encoding='utf-8-sig'))
            _retry_pool_accounts={str(a.get('id')):a for a in data.get('accounts',[]) if a.get('_check_change_proxy')}
            _retry_pool_stamp=stamp
        return _retry_pool_accounts.get(str(account_id),{})
    except (OSError,ValueError):return {}

def _alternate_check_proxies(rows,index,plan,parse_proxy):
    import random
    if not rows or not plan:return list(rows or []),index
    original=parse_proxy(rows[index%len(rows)]) or {}
    original_endpoint=f"{original.get('host','')}:{original.get('port','')}"
    forbidden=set(plan.get('_check_failed_proxy_endpoints') or [])
    candidates=[]
    for raw in rows:
        parsed=parse_proxy(raw) or {}
        endpoint=f"{parsed.get('host','')}:{parsed.get('port','')}"
        if parsed and endpoint not in forbidden and endpoint!=original_endpoint:candidates.append(raw)
    random.shuffle(candidates)
    return candidates,0

# -*- coding: utf-8 -*-
"""Check live account pool — dùng chung cho client (khách) và admin tool.

Cookie được kiểm tra trực tiếp qua TikTok HTTP API, không mở Chrome. Chrome chỉ
được tạo khi cookie thiếu/chết và cần login user/pass, captcha hoặc 2FA; cookie
mới sau login tiếp tục được xác nhận bằng HTTP API trước khi báo LIVE.

Mọi request dùng cùng proxy với lượt chạy thật. Lỗi mạng/IP chỉ tạo UNKNOWN và
được đổi proxy có giới hạn; chỉ credentials sai hoặc cookie-only hết hạn mới DIE.
Tách khỏi admin_tool.py để giữ UI và logic check độc lập.
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

CHECK_THREADS = 8
MAX_CHECK_THREADS = 12
DIRECT_MAX_THREADS = 1
PER_PROXY_THREADS = 1
# Chỉ đổi tối đa 3 proxy khi lỗi thực sự thuộc mạng/IP.
MAX_ALT_PROXIES = 3

# --- Monkey patch undetected_chromedriver to eliminate multiprocessing pipe hang ---
try:
    import sys as _sys
    import subprocess as _subprocess
    import undetected_chromedriver as _uc
    try:
        import undetected_chromedriver.dprocess as _dprocess
        def _safe_start_detached(executable, *args, **kwargs):
            cflags = (0x00000008 | 0x00000200) if _sys.platform == "win32" else 0
            p = _subprocess.Popen(
                [executable, *args],
                stdin=_subprocess.PIPE,
                stdout=_subprocess.PIPE,
                stderr=_subprocess.PIPE,
                creationflags=cflags
            )
            _dprocess.REGISTERED.append(p.pid)
            return p.pid
        _dprocess.start_detached = _safe_start_detached
        _dprocess._start_detached = _safe_start_detached
    except Exception:
        pass

    _orig_uc_init = _uc.Chrome.__init__
    def _safe_uc_init(self, *args, **kwargs):
        kwargs["use_subprocess"] = True
        return _orig_uc_init(self, *args, **kwargs)
    _uc.Chrome.__init__ = _safe_uc_init
except Exception:
    pass


def _attempt(acc: dict, cookies, log_fn, prefix: str, proxy,
             pause_event=None, stop_event=None, bots_reg=None,
             headless=True) -> tuple[bool, str, bool]:
    """Hiện browser: xác nhận cookie bằng Chrome; ẩn: dùng HTTP API."""
    if not headless:
        import tiktok_reply
        if not _gate(pause_event, stop_event, log_fn, prefix):
            return False, "", False
        if not cookies:
            return False, "", False
        bot = tiktok_reply.TikTokLikeBot(
            log_fn, stop_event or threading.Event(), prefix=prefix)
        if bots_reg is not None:
            bots_reg.append(bot)
        try:
            if not _gate(pause_event, stop_event, log_fn, prefix):
                return False, "", False
            bot.start_browser(headless=False, session_idx=acc["id"], proxy=proxy)
            if stop_event is not None and stop_event.is_set():
                return False, "", False
            if bot.inject_cookies(cookies):
                ok, username = bot.check_live_api()
                if ok and username:
                    log_fn(f"[check] {prefix} Chrome xác nhận LIVE {username}")
                    return True, username, False
            # Use the existing HTTP classifier when browser validation is
            # inconclusive, so network failures never become evidence of DIE.
            log_fn(f"[check] {prefix} Chrome chưa xác nhận; kiểm tra API bổ sung.")
        finally:
            try:
                bot.close()
            except Exception:
                pass
            if bots_reg is not None and bot in bots_reg:
                bots_reg.remove(bot)
        if not _gate(pause_event, stop_event, log_fn, prefix):
            return False, "", False
    if not _gate(pause_event, stop_event, log_fn, prefix):
        return False, "", False
    if not cookies:
        return False, "", False
    from client.app import tiktok_session_api
    ok, username, proxy_blocked, note = tiktok_session_api.check_live(
        cookies, proxy=proxy)
    if proxy_blocked or any(token in str(note).lower() for token in ('proxyerror', 'sslerror', 'timeout', 'connectionerror')):
        acc['_check_proxy_failed'] = True
        proxy_blocked = True
    if note:
        kind = "proxy" if proxy_blocked else "cookie"
        log_fn(f"[check] {prefix} API {kind}: {note}")
    return ok, username, proxy_blocked



def _gate(pause_event, stop_event, log, prefix) -> bool:
    """Chờ khi pause. Trả False nếu bị STOP (caller bỏ qua acc, không báo DIE)."""
    if stop_event is not None and stop_event.is_set():
        return False
    if pause_event is not None:
        while not pause_event.wait(timeout=0.3):
            if stop_event is not None and stop_event.is_set():
                return False
    return True


def _relogin_once(acc, log_fn, proxy, headless, stop_event, bots_reg):
    """Re-login; trả ok, cookie, username, banned, thiếu credentials, lý do lỗi."""
    import tiktok_reply
    import bot_login
    prefix = acc.get("username") or f"acc{acc['id']}"
    network_failure=[False]
    original_log=log_fn
    def checked_log(message):
        if _is_proxy_error(str(message)) or 'browser_network_error' in str(message):
            network_failure[0]=True
        original_log(message)
    log_fn=checked_log
    bot = tiktok_reply.TikTokLikeBot(log_fn, stop_event or threading.Event(),
                                     prefix=prefix)
    if bots_reg is not None:
        bots_reg.append(bot)
    if stop_event is not None and stop_event.is_set():
        if bots_reg is not None and bot in bots_reg:
            bots_reg.remove(bot)
        return False, "", "", False, False, "stopped"
    try:
        bot.start_browser(headless=headless, session_idx=acc["id"] % 100,
                          proxy=proxy)
        email = (acc.get("email") or "").strip()
        totp_secret = (acc.get("totp_secret") or "").strip()
        mail_pass = (acc.get("mail_pass") or "").strip()
        credentials_incomplete = False
        if not email and "@" in totp_secret:
            email = totp_secret
            totp_secret = ""
            credentials_incomplete = not mail_pass
            log_fn(
                f"[check] {prefix} phát hiện email từ dữ liệu server cũ"
                + (" nhưng thiếu mail password." if credentials_incomplete
                   else "."))
        login_user = (
            acc.get("login_user") or acc.get("username") or prefix)
        ok, cookie, uname = bot_login.auto_login_with_email_fallback(
            bot, login_user, email,
            acc["password"],
            totp_secret,
            log_fn=log_fn,
            mail_pass=mail_pass,
            retry_server_error_in_place=False)
        return (
            ok, cookie, uname, getattr(bot, "last_banned", False),
            credentials_incomplete,
            ('proxy' if not ok and network_failure[0] else getattr(bot, "last_login_failure", "") or "unknown"))
    finally:
        try:
            bot.close()
        except Exception:
            pass
        if bots_reg is not None and bot in bots_reg:
            bots_reg.remove(bot)


def _check_one_impl(acc: dict, log_fn, proxies=None, proxy_idx=0, pause_event=None,
               stop_event=None, bots_reg=None, headless=True) -> dict:
    """Check một account; chỉ DIE khi có bằng chứng credentials không hợp lệ."""
    prefix = acc.get("username") or f"acc{acc['id']}"
    stopped = {"id": acc["id"], "username": "", "status": "stopped"}
    unknown = {
        "id": acc["id"], "username": acc.get("username") or "",
        "status": "unknown",
    }
    try:
        import random
        import tiktok_reply
        retry_plan=_retry_proxy_plan(acc['id'])
        if retry_plan:
            proxies,proxy_idx=_alternate_check_proxies(proxies,proxy_idx,retry_plan,tiktok_reply.parse_proxy)
            if not proxies:
                log_fn(f'[check] {prefix} chưa có proxy khác phù hợp; giữ cookie và UNKNOWN.')
                return unknown
            log_fn(f'[check] {prefix} dùng proxy khác theo yêu cầu trong pool.')

        if not _gate(pause_event, stop_event, log_fn, prefix):
            return stopped
        cookies = tiktok_reply.parse_cookies(acc.get("cookie") or "")
        password = acc.get("password") or ""
        if retry_plan.get('_check_refresh_cookie'):
            if not password:
                log_fn(f'[check] {prefix} thiếu mật khẩu; chưa làm mới được cookie, giữ cookie cũ.')
                return unknown
            cookies=[]
            log_fn(f'[check] {prefix} đăng nhập lại lấy cookie mới trên proxy khác; giữ bản cookie cũ đến khi có kết quả.')
        if not cookies and not password:
            log_fn(f"[check] acc{acc['id']} DIE — không có cookie hoặc password.")
            return {"id": acc["id"], "username": "", "status": "die"}

        proxy = _proxy_for(proxies, proxy_idx) if proxies else None
        remaining_indices = []
        if proxies and len(proxies) > 1:
            available = [
                index for index in range(len(proxies))
                if index != proxy_idx % len(proxies)
            ]
            remaining_indices = random.sample(
                available, min(MAX_ALT_PROXIES, len(available)))

        ok = False
        uname = ""
        proxy_blocked = False
        if cookies:
            try:
                ok, uname, proxy_blocked = _attempt(
                    acc, cookies, log_fn, prefix, proxy,
                    pause_event, stop_event, bots_reg, headless)
            except Exception as exc:
                if not _is_proxy_error(str(exc)):
                    raise
                proxy_blocked = True
                log_fn(f"[check] {prefix} lỗi proxy → đổi proxy.")

            while not ok and proxy_blocked and remaining_indices:
                index = remaining_indices.pop(0)
                if not _gate(pause_event, stop_event, log_fn, prefix):
                    return stopped
                proxy = _proxy_for(proxies, index)
                log_fn(f"[check] {prefix} lỗi mạng → đổi proxy {proxy.get('host','?')}:{proxy.get('port','?')}; còn {len(remaining_indices)} lượt thay thế.")
                try:
                    ok, uname, proxy_blocked = _attempt(
                        acc, cookies, log_fn, prefix, proxy,
                        pause_event, stop_event, bots_reg, headless)
                except Exception as exc:
                    if _is_proxy_error(str(exc)):
                        proxy_blocked = True
                        continue
                    raise
            if not ok and proxy_blocked:
                log_fn(f"[check] acc{acc['id']} UNKNOWN — đã thử hết proxy thay thế; giữ cookie.")
                return unknown
            if ok and uname:
                return {
                    "id": acc["id"], "username": uname, "status": "live",
                }

        if password:
            # Không có cookie thì đi thẳng vào login, tránh mở một browser rỗng
            # rồi đóng ngay như luồng cũ.
            log_fn(f"[check] {prefix} cookie thiếu/chết → re-login user/pass...")
            retry_indices = list(remaining_indices)
            retry_total = len(retry_indices)
            while True:
                if not _gate(pause_event, stop_event, log_fn, prefix):
                    return stopped
                try:
                    (ok2, cookie, uname, banned, credentials_incomplete,
                     failure) = _relogin_once(
                        acc, log_fn, proxy, headless, stop_event, bots_reg)
                except Exception as exc:
                    failure = (
                        "proxy" if _is_proxy_error(str(exc)) else "technical")
                    ok2, cookie, uname = False, "", ""
                    banned = credentials_incomplete = False
                    log_fn(
                        f"[check] {prefix} re-login lỗi {failure}: "
                        f"{str(exc)[:100]}")

                if failure == "stopped" or (
                        stop_event is not None and stop_event.is_set()):
                    return stopped
                if banned:
                    log_fn(
                        f"[check] ⛔ acc{acc['id']} ({prefix}) BANNED.")
                    return {
                        "id": acc["id"], "username": "", "status": "banned",
                    }
                if ok2 and cookie:
                    # Keep the newly saved session even if API confirmation fails.
                    unknown = dict(unknown, cookie=cookie)
                    fresh_cookies = tiktok_reply.parse_cookies(cookie)
                    api_ok, api_uname, api_proxy_blocked = _attempt(
                        acc, fresh_cookies, log_fn, prefix, proxy,
                        pause_event, stop_event, bots_reg, headless)
                    if api_ok and api_uname:
                        if uname and api_uname.lstrip('@').lower()!=uname.lstrip('@').lower():
                            log_fn(f'[check] {prefix} phiên mới không khớp tài khoản đăng nhập; giữ cookie cũ.')
                            return dict(unknown, cookie=acc.get('cookie') or '')
                        import scout_cookie_cache
                        scout_cookie_cache.save(acc,cookie)
                        log_fn(f'[cookie] {prefix} đã xác nhận phiên mới; cập nhật cache và lưu cookie vào pool.')
                        return {
                            "id": acc["id"],
                            "username": api_uname,
                            "status": "live",
                            "cookie": cookie,
                        }
                    failure = "proxy" if api_proxy_blocked else "technical"
                    log_fn(
                        f"[check] acc{acc['id']} đăng nhập được nhưng "
                        f"HTTP API chưa xác nhận LIVE ({failure}).")
                if credentials_incomplete:
                    log_fn(
                        f"[check] acc{acc['id']} UNKNOWN — server cũ đã làm "
                        "mất mail password; cần migrate và nạp lại account.")
                    return unknown
                if failure != "proxy":
                    break
                if not retry_indices:
                    break
                index = retry_indices.pop(0)
                proxy = _proxy_for(proxies, index)
                log_fn(
                    f"[check] {prefix} lỗi {failure} → proxy "
                    f"{proxy['host'] if proxy else '?'} "
                    f"({retry_total - len(retry_indices)}/"
                    f"{retry_total})...")

            if failure == "credentials":
                log_fn(
                    f"[check] acc{acc['id']} DIE — username và email đều "
                    "không đăng nhập được.")
                return {"id": acc["id"], "username": "", "status": "die"}
            log_fn(
                f"[check] acc{acc['id']} UNKNOWN — chưa đủ căn cứ kết luận DIE "
                f"({failure}).")
            return unknown

        # Cookie-only: xác nhận thêm đúng một lần trên proxy đã hoạt động.
        if not _gate(pause_event, stop_event, log_fn, prefix):
            return stopped
        log_fn(f"[check] {prefix} xác nhận lại cookie...")
        ok, uname, proxy_blocked = _attempt(
            acc, cookies, log_fn, prefix, proxy,
            pause_event, stop_event, bots_reg, headless)
        if ok and uname:
            return {"id": acc["id"], "username": uname, "status": "live"}
        if proxy_blocked:
            return unknown
        log_fn(f"[check] acc{acc['id']} DIE — cookie không còn hợp lệ.")
        return {"id": acc["id"], "username": "", "status": "die"}
    except Exception as exc:
        if stop_event is not None and stop_event.is_set():
            return stopped
        log_fn(
            f"[check] acc{acc['id']} UNKNOWN — lỗi kỹ thuật: "
            f"{str(exc)[:100]}")
        return unknown


def _check_one(acc, log_fn, *args, **kwargs):
    current = dict(acc)
    result = _check_one_impl(current, log_fn, *args, **kwargs)
    return result

def _is_proxy_error(msg: str) -> bool:
    """Lỗi mạng/proxy (tunnel, timeout, refused...) — không phải acc hỏng."""
    m = (msg or "").lower()
    return any(k in m for k in (
        "tunnel", "err_proxy", "err_connection", "err_name_not_resolved",
        "connection refused", "net::err", "proxyerror",
        "connecttimeout", "connection timed out", "read timed out",
    ))


def _proxy_for(proxies, idx: int):
    """Proxy thô → dict cho bot (round-robin giống runner). Lỗi parse → None."""
    import tiktok_reply
    p = proxies[idx % len(proxies)]
    if isinstance(p, dict):
        raw = p.get("raw") or p.get("address") or ""
    else:
        raw = str(p or "")
    raw = raw.strip()
    if not raw:
        return None
    try:
        return tiktok_reply.parse_proxy(raw)
    except Exception:
        return None


def check_all(accounts: list[dict], log_fn, progress_fn=None,
              proxies: list[str] | None = None, threads: int | None = None,
              pause_event=None, stop_event=None, bots_reg=None,
              headless=True) -> list[dict]:
    """Check danh sách acc (đa luồng, qua proxy nếu có).

    proxies: danh sách proxy thô của pool — phân bổ round-robin giống runner
    (acc thứ i dùng proxy thứ i % n) → check trong điều kiện giống chạy thật.
    threads: số browser đồng thời (UI chọn; None = CHECK_THREADS mặc định).
    pause_event: set = chạy, clear = tạm dừng (chờ giữa các acc).
    stop_event: set = DỪNG HẲN — acc chưa bắt đầu bị bỏ qua (status='stopped'),
    browser đang mở bị caller đóng qua bots_reg (MỌI bot đều đăng ký).
    headless: False = hiện cửa sổ browser để xem check (debug).
    progress_fn(done, total, result) gọi sau MỖI acc — caller đẩy lên server
    real-time. Chạy BLOCKING — gọi trong thread nền.
    """
    results: list[dict] = []
    total = len(accounts)
    done = 0
    requested = max(1, int(threads or CHECK_THREADS))
    if not total:
        return results
    workers = min(requested, total)
    log_fn(f"[check] {total} account · {workers} luồng (yêu cầu {requested}).")
    log_fn("[check] Chế độ: Chrome hiển thị." if not headless else "[check] Chế độ: HTTP API, không mở Chrome cho cookie.")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {}
        for i, a in enumerate(accounts):
            futs[pool.submit(_check_one, a, log_fn, proxies, i, pause_event,
                             stop_event, bots_reg, headless)] = a
        for fut in as_completed(futs):
            acc = futs[fut]
            try:
                r = fut.result()
            except Exception as e:
                if stop_event is not None and stop_event.is_set():
                    r = {"id": acc["id"], "username": "", "status": "stopped"}
                else:
                    log_fn(f"[check] acc{acc['id']} lỗi kỹ thuật: {str(e)[:100]}")
                    r = {
                        "id": acc["id"], "username": "",
                        "status": "unknown",
                    }
            results.append(r)
            done += 1
            if r["status"] == "stopped":
                log_fn(f"[check] ⏹️ {done}/{total} acc{r['id']} bỏ qua (đã DỪNG).")
            else:
                log_fn(f"[check] {done}/{total} → acc{r['id']} {r['status'].upper()}"
                       f" {r['username'] or '(chưa có tên)'}")
            if progress_fn:
                progress_fn(done, total, r)
    return results

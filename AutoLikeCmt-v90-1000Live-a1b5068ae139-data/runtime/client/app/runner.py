import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from types import SimpleNamespace


def _cookie_str_to_list(raw):
    result = []
    for part in raw.split(';'):
        part = part.strip()
        if '=' not in part:
            continue
        name, value = part.split('=', 1)
        name = name.strip()
        if not name:
            continue
        result.append({
            'name': name,
            'value': value.strip(),
            'domain': '.tiktok.com',
            'path': '/',
            'secure': True,
            'httpOnly': name in ('sessionid', 'sessionid_ss', 'sid_tt', 'sid_guard'),
        })
    return result


class RunStats:
    def __init__(self, target=0):
        self.target = int(target or 0)
        self.lock = threading.RLock()
        self.total_likes = 0
        self.total_done = 0
        self.total_ok = 0
        self.total_fail = 0
        self.stopped_at_target = False
        self.per_link = {}
        self._results = []

    @property
    def ok(self):
        return self.total_likes

    @ok.setter
    def ok(self, v):
        self.total_likes = v

    @property
    def done(self):
        return self.total_done

    @done.setter
    def done(self, v):
        self.total_done = v

    @property
    def acc_ok(self):
        return self.total_ok

    @acc_ok.setter
    def acc_ok(self, v):
        self.total_ok = v

    def reached_target(self):
        if self.target <= 0:
            return False
        with self.lock:
            return self.total_likes >= self.target

    def add_result(self, link_idx, ok, likes=1, complete=None):
        with self.lock:
            self.total_done += 1
            if ok:
                self.total_ok += 1
                self.total_likes += max(0, likes)
            else:
                self.total_fail += 1
            self._results.append({
                'link_idx': link_idx,
                'ok': ok,
                'likes': likes,
                'complete': complete,
            })

    def snapshot(self):
        with self.lock:
            return {
                'total_likes': self.total_likes,
                'total_done': self.total_done,
                'total_ok': self.total_ok,
                'total_fail': self.total_fail,
                'target': self.target,
                'reached': self.reached_target(),
            }


class Runner:
    _explicit_pool_scheduler = False
    _scout_handoff_installed = False
    _delivery_browser_limit_v55 = False
    _delivery_link_rotation_ = False
    _delivery_rotation_ = False
    _disk_cookie_cache_installed = False
    _machine_proxy_limit_installed = False

    def __init__(self, bridge, log_fn=None):
        self.bridge = bridge
        self.log_fn = log_fn or bridge._log
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.pause_event.set()
        self.stats = RunStats()
        self.account_results = []
        self._proxy_pool = []
        self._proxy_idx = 0
        self._proxy_lock = threading.Lock()
        self._proxy_acquire = None
        self._proxy_release = None
        self._reuse_pool = False
        self._worker_local = threading.local()

    def _log(self, msg):
        if self.log_fn:
            self.log_fn(msg)

    log = _log

    def _proxy_acquire_default(self, count=1, stop_event=None):
        with self._proxy_lock:
            result = []
            for _ in range(count):
                if not self._proxy_pool:
                    break
                proxy = self._proxy_pool[self._proxy_idx % len(self._proxy_pool)]
                self._proxy_idx += 1
                result.append(proxy)
            return result

    def _close_reused_bots(self):
        wl = getattr(self, '_worker_local', None)
        if wl is None:
            return
        bot = getattr(wl, 'bot', None)
        if bot is not None:
            try:
                bot.close()
            except Exception:
                pass
            wl.bot = None

    def _report_account_link(self, acc_no, acc_name, link, like_type, vid_ok, cmt_liked, cmt_found,
                             vip_n=0, cmt_failure=''):
        status = 'ok' if (vid_ok or cmt_liked > 0) else 'fail'
        self.account_results.append({
            'acc_no': acc_no,
            'acc_name': acc_name,
            'link': link,
            'like_type': like_type,
            'vid_ok': vid_ok,
            'cmt_liked': cmt_liked,
            'cmt_found': cmt_found,
            'status': status,
            'cmt_failure': cmt_failure,
        })

    def _record_result(self, acc, link_idx, ok, likes=1, complete=None):
        self.stats.add_result(link_idx, ok, likes=likes, complete=complete)

    def _save_fresh_cookie(self, acc, cookie):
        pass

    def _try_relogin(self, bot, acc, proxy):
        return False

    def _log_summary(self, opts):
        snap = self.stats.snapshot()
        self._log(f'Hoàn tất. Tim: {snap["total_likes"]}, OK: {snap["total_ok"]}, Lỗi: {snap["total_fail"]}')

    def _do_one_link(self, bot, acc, link_idx, link, opts,
                     extract_comment_id=None, scout_observe=None, scout_mode=False, **kw):
        try:
            import tiktok_reply as tt
            like_type = getattr(opts, 'like_type', 'comment')
            vid_ok = False
            cmt_liked = 0
            cmt_found = 0
            acc_name = acc.get('username') or acc.get('login_user') or f"acc{acc.get('id')}"
            target_text = getattr(opts, 'target_text', '')
            target_texts = getattr(opts, 'target_texts', [])
            find_mode = getattr(opts, 'find_mode', 'all')

            bot._check_stop()
            bot.open_link(link)

            if scout_mode and scout_observe:
                current_url = ''
                try:
                    current_url = bot.driver.current_url if bot.driver else ''
                except Exception:
                    pass
                aweme_id = tt.extract_aweme_id(link) or tt.extract_aweme_id(current_url)
                comments = bot.find_target_comments(
                    target_text, find_mode=find_mode
                ) if hasattr(bot, 'find_target_comments') else []
                scout_observe(link_idx, acc_name, link, current_url, comments)
                return True

            if like_type in ('video', 'both'):
                try:
                    bot.like_video()
                    vid_ok = True
                except Exception as e:
                    self._log(f'  Like video lỗi: {e}')

            if like_type in ('comment', 'both'):
                aweme_id = tt.extract_aweme_id(link)
                if not aweme_id:
                    try:
                        current_url = bot.driver.current_url if bot.driver else link
                        aweme_id = tt.extract_aweme_id(current_url)
                    except Exception:
                        pass

                gate = getattr(self, '_scout_match_gate', None)
                cached_comments = None
                if gate and aweme_id:
                    cached_comments = gate.peek_ready(aweme_id, target_text)
                if not cached_comments and aweme_id:
                    import tiktok_reply as _tt
                    cache_key = (str(aweme_id), _tt._norm_handle(target_text))
                    hit = _tt._COMMENT_CACHE.get(cache_key)
                    if hit and hit.get('comments'):
                        cached_comments = [dict(c) for c in hit['comments']]

                if cached_comments:
                    try:
                        results = bot.api_digg_comments(aweme_id, cached_comments)
                        if isinstance(results, list):
                            cmt_liked = sum(1 for r in results if r[0])
                            cmt_found = len(results)
                        elif isinstance(results, dict):
                            cmt_liked = results.get('liked', 0)
                            cmt_found = results.get('found', 0)
                    except Exception as e:
                        self._log(f'  API digg lỗi: {e}')
                else:
                    try:
                        if hasattr(bot, 'find_and_digg_comments'):
                            result = bot.find_and_digg_comments(
                                target_text, find_mode=find_mode,
                                autocap=getattr(opts, 'autocap', True),
                            )
                        else:
                            result = bot.api_digg_comments(
                                aweme_id or link, [],
                            )
                        if isinstance(result, dict):
                            cmt_liked = result.get('liked', 0)
                            cmt_found = result.get('found', 0)
                        elif isinstance(result, (list, tuple)):
                            cmt_liked = sum(1 for r in result if r[0]) if result and isinstance(result[0], tuple) else len(result)
                            cmt_found = len(result)
                        else:
                            cmt_liked = int(result or 0)
                            cmt_found = cmt_liked
                    except Exception as e:
                        self._log(f'  Like comment lỗi: {e}')

            ok = vid_ok or cmt_liked > 0
            likes = max(cmt_liked, int(vid_ok))
            self.stats.add_result(link_idx, ok, likes=likes)
            self._report_account_link(0, acc_name, link, like_type, vid_ok, cmt_liked, cmt_found)
            return ok
        except Exception as e:
            self._log(f'  Link lỗi: {e}')
            self.stats.add_result(link_idx, False, likes=0)
            return False

    def _run_one(self, account, index, total, proxies, opts,
                 bot_class, parse_proxy, parse_cookies, extract_comment_id,
                 pick_proxy=None, blacklist_proxy=None,
                 _proxy_key=None, is_proxy_blocked=None,
                 scout_mode=False, scan_only=False, scan_round=0,
                 scout_observe=None):
        acc_name = account.get('username') or account.get('login_user') or f"acc{account.get('id')}"
        prefix = f'[{acc_name}]'
        self._log(f'{prefix} Bắt đầu... ({index}/{total})')

        reuse = self._reuse_pool and not scan_only
        wl = getattr(self, '_worker_local', None)
        bot = getattr(wl, 'bot', None) if reuse and wl else None
        bot_reused = bot is not None and getattr(bot, 'driver', None) is not None
        created_bot = False

        try:
            if not bot_reused:
                bot = bot_class(self._log, self.stop_event, prefix=prefix)
                created_bot = True

                proxy = None
                if pick_proxy and proxies:
                    proxy = pick_proxy(proxies)
                elif proxies:
                    raw = proxies[(index - 1) % len(proxies)]
                    if isinstance(raw, dict):
                        proxy = parse_proxy(raw.get('raw') or raw.get('address') or '')
                    elif isinstance(raw, str):
                        proxy = parse_proxy(raw)

                bot.start_browser(
                    headless=getattr(opts, 'headless', True),
                    session_idx=index,
                    proxy=proxy,
                    debug=getattr(opts, 'debug', False),
                    assetless=not scan_only,
                    compact=not scan_only,
                )

            bot.prefix = prefix
            bot._expected_account_username = acc_name
            bot._expected_login_user = account.get('login_user', '')

            cookie = account.get('cookie', '')
            if cookie:
                if bot_reused:
                    bot.reset_account_session()
                if isinstance(cookie, str):
                    cookie = _cookie_str_to_list(cookie)
                bot.login_with_cookies(cookie)
            else:
                login_user = account.get('login_user') or account.get('email') or ''
                password = account.get('password', '')
                if login_user and password:
                    ok = self._try_relogin(bot, account, prefix)
                    if not ok:
                        self._log(f'{prefix} Login thất bại, bỏ qua.')
                        return
                else:
                    self._log(f'{prefix} Không có cookie/password, bỏ qua.')
                    return

            links = [l.strip() for l in (getattr(opts, 'links', []) or []) if l.strip()]

            for link_idx, link in enumerate(links):
                if self.stop_event.is_set():
                    break
                if self.stats.reached_target():
                    break
                while not self.pause_event.is_set() and not self.stop_event.is_set():
                    time.sleep(0.3)

                self._do_one_link(
                    bot, account, link_idx, link, opts,
                    extract_comment_id=extract_comment_id,
                    scout_observe=scout_observe,
                    scout_mode=scan_only,
                )

                if getattr(bot, 'last_digg_expired', False):
                    self._log(f'{prefix} Phiên hết hạn, chuyển acc tiếp.')
                    break
                if getattr(bot, 'last_session_invalid', False):
                    self._log(f'{prefix} Phiên không hợp lệ, chuyển acc.')
                    break

            self._log(f'{prefix} Hoàn tất.')

            if reuse and wl is not None and not self.stop_event.is_set():
                wl.bot = bot
                created_bot = False

        except RuntimeError as e:
            if 'dừng' in str(e).lower() or 'stop' in str(e).lower():
                self._log(f'{prefix} Đã dừng.')
            else:
                self._log(f'{prefix} Lỗi: {e}')
        except Exception as e:
            import traceback as _tb
            self._log(f'{prefix} Lỗi: {e}')
            self._log(_tb.format_exc())
        finally:
            if created_bot and bot:
                try:
                    bot.close()
                except Exception:
                    pass

    def run(self, accounts, proxies, opts, done_fn=None):
        import tiktok_reply as tt

        self.stop_event.clear()
        self.pause_event.set()
        self.stats = RunStats(target=getattr(opts, 'target_count', 0))
        self.account_results = []
        self._proxy_pool = proxies or []

        links = [l.strip() for l in (getattr(opts, 'links', []) or [])
                 if l.strip() and 'vt.tiktok.com/....' not in l]
        if not links:
            self._log('Chưa nhập link.')
            return

        threads_count = max(1, int(getattr(opts, 'threads', 1)))
        self._log(f'Bắt đầu chạy với {len(links)} link, {len(accounts)} account, {threads_count} luồng...')

        with ThreadPoolExecutor(max_workers=threads_count) as pool:
            futures = []
            for i, acc in enumerate(accounts):
                if self.stop_event.is_set() or self.stats.reached_target():
                    break
                futures.append(pool.submit(
                    self._run_one, acc, i + 1, len(accounts), proxies, opts,
                    tt.TikTokLikeBot, tt.parse_proxy, tt.parse_cookies, tt.extract_comment_id,
                ))
            for f in as_completed(futures):
                try:
                    f.result()
                except Exception as e:
                    self._log(f'Worker lỗi: {e}')

        self._log_summary(opts)
        if done_fn:
            done_fn()

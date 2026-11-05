from proxy_wait_timeout import bounded_wait, wait as proxy_wait
"""API scout/pool scheduling: browser count is independent of proxy count."""
import threading
import time
import inspect
import json
import re
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

POOL_METADATA_PATH = Path.home() / '.auto-like-cmt' / 'local_pool_data.json'


def proxy_lease_limit():
    """Use the user-selected budget on this machine, independent of browsers."""
    try:
        import machine_proxy_config
        return machine_proxy_config.get_config()['count']
    except ImportError:
        pass
    try:
        rows = json.loads(POOL_METADATA_PATH.read_text(encoding='utf-8')).get('proxies', [])
        total = len(rows) if isinstance(rows, list) else 0
    except (OSError, ValueError, TypeError):
        total = 0
    return max(1, total // 2) if total else 1


def _prioritize_accounts(accounts, links, extract_aweme_id):
    items = list(accounts)
    if not items or not any(isinstance(a, dict) for a in items):
        return items
    target_videos = {
        str(v) for v in (extract_aweme_id(link) for link in (links or ()) if str(link or '').strip()) if v
    }
    vid_done, vid_partial, vid_failed = set(), set(), set()
    any_confirmed, any_zero, any_unread, full_any = set(), set(), set(), set()
    run_counts = {}
    max_chg = {}
    last_zero = {}
    log_path = Path(__file__).resolve().parent / 'logs' / 'digg-verification.jsonl'
    try:
        if log_path.exists():
            with log_path.open('r', encoding='utf-8') as fp:
                for line in fp:
                    if not line.strip():
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    uname = str(rec.get('account') or '').strip().lstrip('@').lower()
                    if not uname:
                        continue
                    req = int(rec.get('requested') or 0)
                    if not str(rec.get('aweme_id') or '').isdigit():
                        continue
                    conf = int(rec.get('confirmed') or 0)
                    chg = int(rec.get('changed_confirmed') or 0)
                    opp = int(rec.get('explicit_opposite') or 0)
                    unr = int(rec.get('unread') or 0)
                    ns = int(rec.get('not_sent') or 0)
                    if chg > max_chg.get(uname, 0):
                        max_chg[uname] = chg
                    if req >= 5 and conf >= req:
                        full_any.add(uname)
                    if chg > 0 or conf > 0:
                        run_counts[uname] = run_counts.get(uname, 0) + 1
                        last_zero[uname] = False
                        any_confirmed.add(uname)
                        any_zero.discard(uname)
                        any_unread.discard(uname)
                    elif req > 0 and opp > 0 and unr == 0:
                        run_counts[uname] = run_counts.get(uname, 0) + 1
                        last_zero[uname] = True
                        if uname not in any_confirmed:
                            any_zero.add(uname)
                    elif req > 0 and (unr == req or ns == req):
                        if uname not in any_confirmed and uname not in any_zero:
                            any_unread.add(uname)
                    aweme = str(rec.get('aweme_id') or '')
                    if target_videos and aweme in target_videos and req > 0:
                        if conf >= req:
                            vid_done.add(uname)
                            vid_partial.discard(uname)
                            vid_failed.discard(uname)
                        elif conf > 0:
                            if uname not in vid_done:
                                vid_partial.add(uname)
                            vid_failed.discard(uname)
                        elif opp > 0 and unr == 0 and uname not in vid_done and uname not in vid_partial:
                            vid_failed.add(uname)
    except OSError:
        pass

    try:
        import scout_cookie_cache
        for a in items:
            if isinstance(a, dict):
                scout_cookie_cache.restore(a)
    except Exception:
        pass
    idx_map = {id(a): idx for idx, a in enumerate(items)}

    def _priority(a):
        if not isinstance(a, dict):
            return (0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
        ck = str(a.get('cookie') or '').strip()
        ck_low = ck.lower()
        has_sess = ('sessionid=' in ck or 'sid_tt=' in ck)
        aid = int(a.get('id') or 0)
        has_pw = bool(str(a.get('password') or '').strip())
        has_totp = bool(str(a.get('totp_secret') or '').strip())
        email_dom = str(a.get('email') or '').strip().lower().split('@')[-1] if '@' in str(a.get('email') or '') else ''
        can_auto_2fa = has_totp or email_dom in ('somoj.com', 'punkproof.com', 'tiffincrane.com', 'maxxspace.com', 'comfythings.com')
        # Always run cookie-ready accounts first (0) so 10 threads never hit TikTok's login rate-limit wall;
        # then no-cookie accounts that have TOTP/Mail.tm (1); then no-cookie accounts without auto-2FA (2).
        has_ck = 0 if has_sess else (1 if (has_pw and can_auto_2fa) else 2)
        is_eu_idc = ('eu-ttp2' in ck_low or 'no1a' in ck_low or 'us-eastred' in ck_low)
        m_sg = re.search(r'sid_guard=[^%|]+(?:%7C|\|)(\d{10})', ck)
        sg_ts = int(m_sg.group(1)) if m_sg else 0
        sg_day = sg_ts // 86400 if sg_ts else 0
        m_cc = re.search(r'store-country-code=([^;|]+)', ck_low)
        cc = m_cc.group(1).strip() if m_cc else ''
        idx = idx_map.get(id(a), 0)
        uname = str(a.get('username') or a.get('login_user') or '').strip().lstrip('@').lower()
        status_str = str(a.get('status') or 'live').strip().lower()
        rc = run_counts.get(uname, 0)
        m_chg = max_chg.get(uname, 0)
        l_zero = last_zero.get(uname, False)
        is_unconfirmed = (status_str in ('unknown', 'die', 'expired', 'invalid') and uname not in any_confirmed)
        is_stale_batch = 0 if (sg_day >= 20732 or uname in any_confirmed) else 1
        region_tier = 1 if is_eu_idc else 0
        if uname in any_confirmed or (sg_day >= 20732 and (6000 <= aid < 7500 or cc in ('in', 'sa', 'br', 'pk', 'bd', 'eg', 'ar'))):
            cohort_tier = 0
        elif sg_day >= 20732 and not (aid >= 8500 and cc == 'vn'):
            cohort_tier = 1
        elif sg_day >= 20732:
            cohort_tier = 2
        else:
            cohort_tier = 3
        if is_unconfirmed:
            sub = 6
        elif uname in vid_done:
            sub = 7
        elif (m_chg >= 2 or uname in full_any) and not l_zero and uname not in vid_failed:
            sub = 1
        elif (m_chg >= 1 or uname in any_confirmed) and uname not in vid_failed:
            sub = 2
        elif rc == 0 and uname not in vid_failed:
            sub = 3
        elif (uname in vid_partial or uname in any_confirmed):
            sub = 4
        else:
            sub = 5
        return (
            has_ck,
            1 if uname in vid_done else 0,
            1 if is_unconfirmed else 0,
            sub,
            is_stale_batch,
            cohort_tier,
            region_tier,
            1 if (l_zero and m_chg == 0) else 0,
            -m_chg,
            -sg_day,
            rc,
            idx,
            -aid,
        )

    ordered = sorted(items, key=_priority)
    actionable = [a for a in ordered if not isinstance(a, dict) or _priority(a)[0] < 2]
    if actionable:
        ordered = actionable
    seen_unames = set()
    unique_first, dups_last = [], []
    for a in ordered:
        if not isinstance(a, dict):
            unique_first.append(a)
            continue
        u = str(a.get('username') or a.get('login_user') or '').strip().lstrip('@').lower()
        if u and u in seen_unames:
            dups_last.append(a)
        else:
            if u:
                seen_unames.add(u)
            unique_first.append(a)
    return unique_first + dups_last


def install():
    from client.app.runner import Runner, RunStats
    import tiktok_reply as tt
    if getattr(Runner, '_explicit_pool_scheduler', False):
        return
    previous = Runner.run

    def run(self, accounts, proxies, opts, done_fn=None):
        if not opts.api_like or opts.debug or opts.api_capture or opts.job != 'like' or opts.like_type != 'comment':
            return previous(self, accounts, proxies, opts, done_fn)
        accounts = _prioritize_accounts(accounts, getattr(opts, 'links', None), tt.extract_aweme_id)
        self.stop_event.clear()
        self.pause_event.set()
        workers = min(max(1, int(opts.threads or 1)), len(accounts))
        stats = self.stats = RunStats(target=opts.target_count)
        self.account_results = []
        self._scout_match_gate = tt._ScoutMatchGate(min(2, workers, len(accounts)))
        self._reuse_pool = False
        self._worker_local = threading.local()
        self._timing_started_at = self._scout_started_at = time.monotonic()
        self._like_started_at = 0.0
        self._scout_elapsed = self._like_elapsed = self._run_elapsed = 0.0
        self._timing_phase = 'scout'
        tt.clear_comment_cache()
        seen = {}
        best = {}
        seen_lock = threading.Lock()
        leased = False
        run_proxy_limit = getattr(self, "_run_proxy_limit", None) or proxy_lease_limit()

        @bounded_wait(self._log)
        def acquire(desired):
            nonlocal leased
            while not self.stop_event.is_set():
                if self._proxy_acquire:
                    lease_count = run_proxy_limit
                    try:
                        parameters = inspect.signature(self._proxy_acquire).parameters
                    except (TypeError, ValueError):
                        parameters = {}
                    supports_stop = 'stop_event' in parameters or any(
                        parameter.kind == inspect.Parameter.VAR_KEYWORD
                        for parameter in parameters.values())
                    rows = list((self._proxy_acquire(lease_count, stop_event=self.stop_event)
                                 if supports_stop else self._proxy_acquire(lease_count)) or [])
                    leased = True
                    if len(rows) > lease_count:
                        rows = rows[:lease_count]
                else:
                    rows = list(proxies)
                usable = []
                keys = set()
                for raw in rows:
                    parsed = tt.parse_proxy(raw)
                    key = tt._proxy_key(parsed)
                    if parsed and key not in keys and not tt._proxy_blocked(parsed):
                        keys.add(key)
                        usable.append(raw)
                if usable or (not proxies and not self._proxy_acquire):
                    return usable
                self._log('[pool] Chờ proxy khả dụng; giữ nguyên số Chrome đã chọn.')
                release()
                if proxy_wait(self.stop_event, 3):
                    break
            return None

        def release():
            nonlocal leased
            if leased and self._proxy_release:
                self._proxy_release()
            leased = False

        def target_for(index):
            targets = list(opts.target_texts or [])
            return (targets[min(index, len(targets) - 1)] if targets else opts.target_text).strip()

        links = [link.strip() for link in opts.links if link.strip()]
        # Short share URLs have no post ID until a scout follows the redirect.
        # Keep that association local to this run and to the exact input link.
        link_videos = {link: tt.extract_aweme_id(link) for link in links}

        def observe(index, name, link, current_url, comments):
            video = tt.extract_aweme_id(link) or tt.extract_aweme_id(current_url)
            if not video:
                return
            with seen_lock:
                for link_index, configured in enumerate(links):
                    configured_video = link_videos[configured]
                    if configured_video:
                        if configured_video != video:
                            continue
                    elif configured != str(link or '').strip():
                        continue
                    else:
                        link_videos[configured] = video
                        self._log(f'[scout] Đã nhận diện link rút gọn: bài {video}.')
                    if not comments:
                        continue
                    target = target_for(link_index)
                    unique = list({str(row['cid']): dict(row) for row in comments if row.get('cid')}.values())
                    key = (str(video), tt._norm_handle(target))
                    seen[(index, video)] = len(unique)
                    if len(unique) > len(best.get(key, [])):
                        best[key] = unique
                        self._log(f'[scout] Hook cao nhất video {video}: {len(unique)} CID (scout {index}).')

        scout_proxy_lock=threading.Lock()
        scout_proxy_endpoints=set()
        proxy_epoch=None
        def invoke(account, index, rows, scout=False, round_number=0):
            if self.stop_event.is_set() or stats.reached_target():
                return
            worker_rows = proxy_epoch.snapshot() if proxy_epoch is not None and not scout else list(rows)
            if worker_rows:
                start = (index - 1 + (round_number - 1 if scout else 0)) % len(worker_rows)
                worker_rows = worker_rows[start:] + worker_rows[:start]
            selected_endpoint=[None]
            def worker_pick_proxy(raw_lines, default_scheme='http', avoid=None):
                if proxy_epoch is not None and not scout:
                    raw_lines = proxy_epoch.snapshot()
                    import v90_pool_runtime_fix
                    v90_pool_runtime_fix.set_proxy_group(proxy_epoch.snapshot)
                with scout_proxy_lock:
                    for raw in raw_lines:
                        parsed=tt.parse_proxy(raw,default_scheme)
                        if not parsed or tt._proxy_key(parsed) in (avoid or set()) or tt._proxy_blocked(parsed):continue
                        endpoint=(parsed.get('host'),parsed.get('port'))
                        if scout and endpoint in scout_proxy_endpoints:continue
                        if scout:
                            if selected_endpoint[0] is not None:scout_proxy_endpoints.discard(selected_endpoint[0])
                            scout_proxy_endpoints.add(endpoint)
                        selected_endpoint[0]=endpoint
                        return parsed
                return None
            class NetworkAwareBot(tt.TikTokLikeBot):
                def log(bot,message):
                    if 'browser_network_error' in str(message) or 'login page network error' in str(message):
                        actual=getattr(bot, '_v90_actual_proxy', None)
                        if actual is not None:
                            tt.blacklist_proxy(actual,ttl=60)
                        elif selected_endpoint[0] is not None:
                            for raw in worker_rows:
                                parsed=tt.parse_proxy(raw)
                                if parsed and (parsed.get('host'),parsed.get('port'))==selected_endpoint[0]:
                                    tt.blacklist_proxy(parsed,ttl=60)
                                    break
                    return super().log(message)
            import v90_pool_runtime_fix
            v90_pool_runtime_fix.set_proxy_group(proxy_epoch.snapshot if proxy_epoch is not None and not scout else worker_rows)
            try:
                return self._run_one(
                    account, index, len(accounts), worker_rows, opts,
                    NetworkAwareBot, tt.parse_proxy, tt.parse_cookies, tt.extract_comment_id,
                    pick_proxy=worker_pick_proxy, blacklist_proxy=tt.blacklist_proxy,
                    _proxy_key=tt._proxy_key, is_proxy_blocked=tt._proxy_blocked,
                    scout_mode=scout, scan_only=scout, scan_round=round_number,
                    scout_observe=observe if scout else None)
            finally:
                v90_pool_runtime_fix.set_proxy_group(None)
                if scout:
                    with scout_proxy_lock:scout_proxy_endpoints.discard(selected_endpoint[0])
                if proxy_epoch is not None and not scout:
                    proxy_epoch.complete()

        try:
            if not workers or not links:
                return stats
            scout_count = min(2, workers, len(accounts))
            scout_candidates = [
                a for a in accounts
                if isinstance(a, dict) and ('sessionid=' in str(a.get('cookie') or '') or 'sid_tt=' in str(a.get('cookie') or ''))
            ]
            if len(scout_candidates) < scout_count:
                scout_candidates = list(accounts)
            scouts = list(enumerate(scout_candidates[:scout_count], 1))
            scout_proxies = acquire(scout_count)
            if scout_proxies is None:
                return stats
            self._log(f'[scout] {scout_count} Chrome riêng; pool sau scout: {workers} Chrome theo lựa chọn.')
            for round_number in range(1, min(3, len(scout_proxies) or 1) + 1):
                missing = [(index, account) for index, account in scouts
                           if any(not seen.get((index, link_videos[link])) for link in links)]
                if not missing or self.stop_event.is_set():
                    break
                if round_number>1:
                    if any(best.values()):break
                    self._log(f'[scout] Nhóm trước không lấy được CID; lấy nhóm proxy mới cho lượt {round_number}/3.')
                    rotate = getattr(self, '_proxy_rotate200', None)
                    if rotate:
                        scout_proxies = rotate()
                    else:
                        release()
                        scout_proxies = acquire(scout_count)
                    if not scout_proxies or self.stop_event.is_set():
                        break
                with ThreadPoolExecutor(max_workers=scout_count) as executor:
                    futures = [executor.submit(invoke, account, index, scout_proxies, True, round_number)
                               for index, account in missing]
                    for future in as_completed(futures):
                        try:
                            future.result()
                        except Exception as error:
                            self._log(f'[scout] Lỗi tác vụ: {type(error).__name__}; giữ kiểm tra CID trước khi chuyển pool.')
            self._scout_elapsed = time.monotonic() - self._scout_started_at
            release()
            if self.stop_event.is_set():
                return stats
            if not any(best.values()):
                self._log('[scout] Không scout nào tìm được CID mục tiêu; chưa mở pool.')
                return stats
            successful = len({index for (index, video), count in seen.items() if count > 0})
            self._log(f'[scout] Đã đối chiếu {scout_count} tài khoản; {successful} scout có CID. Chọn hook cao nhất, chuyển pool.')
            # All scout futures have completed and ScoutBot.finally closed their browsers.
            # Replace merged intermediate data with the winning single snapshot.
            tt.clear_comment_cache()
            for (video, target), comments in best.items():
                tt.merge_comment_cache(video, target, [dict(row) for row in comments])
                self._log(f'[scout] Chốt hook cao nhất video {video}: {len(comments)} CID.')
            # The scout has now resolved shortened URLs, so history can match
            # their actual video and put already completed accounts last.
            accounts = _prioritize_accounts(accounts, links, tt.extract_aweme_id)
            self._timing_phase = 'like'
            self._like_started_at = time.monotonic()
            self._reuse_pool = True
            self._log(f'[pool] Scout đã đóng; bắt đầu {workers} Chrome ẩn theo ô Số trình duyệt. Số proxy không giảm số Chrome.')
            from rolling_workers import run_rolling
            rows = acquire(workers)
            if rows is not None:
                from proxy_epoch_200 import ProxyEpoch
                proxy_epoch=ProxyEpoch(rows,getattr(self,'_proxy_rotate200',None))
                self._worker_local = threading.local()
                self._log(f'[pool] {workers} Chrome / {len(rows)} proxy; luồng xong nhận acc tiếp ngay.')
                run_rolling(enumerate(accounts, 1), workers,
                    lambda item: invoke(item[1], item[0], rows),
                    lambda: self.stop_event.is_set() or stats.reached_target(),
                    lambda error: self._log(f'[pool] Lỗi tác vụ: {type(error).__name__}.'))
                if not self.stop_event.is_set() and not stats.reached_target():
                    target_vids = {str(v) for v in (tt.extract_aweme_id(l) for l in links) if v}
                    done_unames = set()
                    log_path = Path(__file__).resolve().parent / 'logs' / 'digg-verification.jsonl'
                    try:
                        if log_path.exists() and target_vids:
                            with log_path.open('r', encoding='utf-8') as fp:
                                for line in fp:
                                    if not line.strip():
                                        continue
                                    rec = json.loads(line)
                                    if str(rec.get('aweme_id') or '') in target_vids:
                                        req = int(rec.get('requested') or 0)
                                        conf = int(rec.get('confirmed') or 0)
                                        if req > 0 and conf >= req:
                                            done_unames.add(str(rec.get('account') or '').strip().lstrip('@').lower())
                    except Exception:
                        pass
                    retry_accounts = [
                        a for a in accounts
                        if isinstance(a, dict)
                        and str(a.get('username') or a.get('login_user') or '').strip().lstrip('@').lower() not in done_unames
                    ]
                    if 0 < len(retry_accounts) < len(accounts):
                        try:
                            self._close_reused_bots()
                        except Exception:
                            pass
                        self._worker_local = threading.local()
                        self._log(f'[pool] ♻️ Chạy vét bù {len(retry_accounts)} tài khoản chưa lên đủ tim bằng trình duyệt/proxy mới...')
                        run_rolling(enumerate(retry_accounts, 1), min(workers, len(retry_accounts)),
                            lambda item: invoke(item[1], item[0], rows),
                            lambda: self.stop_event.is_set() or stats.reached_target(),
                            lambda error: self._log(f'[pool] Lỗi tác vụ vét bù: {type(error).__name__}.'))
            if stats.reached_target():
                stats.stopped_at_target = True
            self._log_summary(opts)
            return stats
        finally:
            try:
                self._close_reused_bots()
                release()
            finally:
                self._reuse_pool = False
                self._run_elapsed = time.monotonic() - self._timing_started_at
                if self._like_started_at:
                    self._like_elapsed = time.monotonic() - self._like_started_at
                self._timing_phase = 'idle'
                if done_fn:
                    done_fn()

    run_lock = threading.Lock()
    def guarded_run(self, *args, **kwargs):
        with run_lock:
            return run(self, *args, **kwargs)
    Runner.run = guarded_run
    Runner._explicit_pool_scheduler = True

"""Bounded verification, stage timing and a 60-second run counter."""
from collections import defaultdict, deque
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import uuid

LOG_DIR = Path(__file__).resolve().parent / 'logs'
_file_lock = threading.Lock()
_WORKER = threading.local()


def _append(name, value):
    try:
        with _file_lock:
            LOG_DIR.mkdir(exist_ok=True)
            path = LOG_DIR / name
            if path.exists() and path.stat().st_size > 10 * 1024 * 1024:
                path.replace(path.with_suffix('.previous.jsonl'))
            with path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(value, ensure_ascii=True) + '\n')
    except OSError:
        pass


class PerformanceMeter:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.RLock()
        self.reset(0)

    def reset(self, workers):
        with self.lock:
            self.started = self.clock()
            self.ended = None
            self.run_id = uuid.uuid4().hex
            self.workers = int(workers)
            self.enabled = False
            self.counts = defaultdict(int)
            self.stages = defaultdict(lambda: [0, 0.0, 0.0])
            self.events = deque()
            self.credited = set()
            self.first_minute = 0
            self.paused_at = None
            self.pause_seconds = 0.0

    def pause(self, paused):
        with self.lock:
            if paused and self.paused_at is None:
                self.paused_at = self.clock()
            elif not paused and self.paused_at is not None:
                self.pause_seconds += self.clock() - self.paused_at
                self.paused_at = None

    def stage(self, name, elapsed):
        with self.lock:
            row = self.stages[name]
            row[0] += 1
            row[1] += max(0.0, elapsed)
            row[2] = max(row[2], elapsed)

    def record(self, account, video, new_cids, summary):
        now = self.clock()
        with self.lock:
            fresh_cids = set()
            for cid in new_cids:
                key = (account, video, cid)
                if key not in self.credited:
                    self.credited.add(key)
                    fresh_cids.add(cid)
            fresh = len(fresh_cids)
            self.counts['new_likes'] += fresh
            if fresh:
                self.events.append((now, fresh))
                if now - self.started < 60:
                    self.first_minute += fresh
            for name in ('already_present', 'unread', 'explicit_opposite', 'not_sent'):
                self.counts[name] += int(summary.get(name, 0))
            self.counts['already_present'] += len(new_cids) - fresh
            return fresh_cids

    def finish(self):
        with self.lock:
            self.ended = self.clock()
        _append('performance-v95.jsonl', dict(event='run_finished', **self.snapshot()))

    def snapshot(self):
        with self.lock:
            now = self.ended if self.ended is not None else self.clock()
            elapsed = max(0.0, now - self.started)
            while self.events and now - self.events[0][0] >= 60:
                self.events.popleft()
            paused = self.pause_seconds
            if self.paused_at is not None:
                paused += max(0.0, now - self.paused_at)
            active = max(0.0, elapsed - paused)
            return dict(
                run_id=self.run_id, workers=self.workers, elapsed_seconds=round(elapsed, 3),
                active_seconds=round(active, 3), paused=self.paused_at is not None,
                new_likes=self.counts['new_likes'],
                new_likes_last_60s=sum(n for _, n in self.events),
                first_60s_new_likes=self.first_minute, first_60s_complete=elapsed >= 60,
                target_150_first_60s_met=self.workers == 10 and self.first_minute >= 150,
                average_new_likes_per_minute=round(self.counts['new_likes'] * 60 / elapsed, 2) if elapsed else 0,
                already_present=self.counts['already_present'], unread=self.counts['unread'],
                explicit_opposite=self.counts['explicit_opposite'], not_sent=self.counts['not_sent'],
                verification='account_state_0_to_1',
                stages={name: dict(calls=v[0], total_seconds=round(v[1], 3),
                                  mean_seconds=round(v[1] / v[0], 3), max_seconds=round(v[2], 3))
                        for name, v in self.stages.items() if v[0]},
            )


METER = PerformanceMeter()


def _state(value):
    if isinstance(value, bool):
        return int(value)
    if type(value) is int and value in (0, 1):
        return value
    if isinstance(value, str) and value in ('0', '1'):
        return int(value)
    return None


def read_states(bot, aweme_id, comments, max_pages=6):
    """Fetch each parent once per wave; missing fields stay unknown."""
    groups = {}
    all_wanted = set()
    for row in comments:
        cid = str(row.get('cid') or '')
        if not cid:
            continue
        all_wanted.add(cid)
        parent = str(row.get('parent_cid') or '')
        if parent in ('0', cid):
            parent = ''
        groups.setdefault(parent, {'wanted': set(), 'cursor': 0, 'seen': {0}, 'count': 0, 'retries': 0})['wanted'].add(cid)
    states = {}
    started = time.monotonic()
    try:
        for _ in range(max_pages):
            bot._check_stop()
            active = [(parent, group) for parent, group in groups.items()
                      if group['wanted'] - states.keys() and group['cursor'] is not None]
            if not active:
                break
            requests = []
            ts_ms = int(time.time() * 1000)
            for idx_req, (parent, group) in enumerate(active):
                query = dict(cursor=group['cursor'], count=20, aid=1988, _t=ts_ms + idx_req)
                if parent:
                    query.update(aweme_id=str(aweme_id), item_id=str(aweme_id), comment_id=parent)
                    path = '/api/comment/list/reply/'
                else:
                    query['aweme_id'] = str(aweme_id)
                    path = '/api/comment/list/'
                requests.append((path, query))
            try:
                payloads = bot._tiktok_signed_get_batch(requests, timeout_sec=3.0)
            except Exception:
                break
            for index, (_, group) in enumerate(active):
                data = payloads[index] if isinstance(payloads, list) and index < len(payloads) else None
                prev_cursor = group['cursor'] or 0
                if not isinstance(data, dict):
                    if group.get('retries', 0) < 1:
                        group['retries'] = group.get('retries', 0) + 1
                    else:
                        group['cursor'] = None
                    continue
                code = data.get('status_code')
                if code is None:
                    code = data.get('statusCode')
                has_comments_list = isinstance(data.get('comments'), list)
                if not has_comments_list and (type(code) not in (int, str) or code not in (0, '0')):
                    if group.get('retries', 0) < 1:
                        group['retries'] = group.get('retries', 0) + 1
                    else:
                        group['cursor'] = None
                    continue
                group['cursor'] = None
                group['retries'] = 0
                comments_list = data.get('comments') or []
                group['count'] += len(comments_list)
                for row in comments_list:
                    if not isinstance(row, dict):
                        continue
                    cid = str(row.get('cid') or '')
                    state = _state(row.get('user_digged'))
                    if cid in all_wanted and state is not None and (cid not in states or state == 1):
                        states[cid] = state
                    for rep in (row.get('reply_comment') or []):
                        if not isinstance(rep, dict):
                            continue
                        rcid = str(rep.get('cid') or '')
                        rstate = _state(rep.get('user_digged'))
                        if rcid in all_wanted and rstate is not None and (rcid not in states or rstate == 1):
                            states[rcid] = rstate
                cursor = data.get('cursor')
                total = int(data.get('total') or 0)
                has_more = data.get('has_more') in (1, True, '1')
                if type(cursor) in (int, str):
                    try:
                        cursor = int(cursor)
                    except ValueError:
                        continue
                    if cursor >= 0 and cursor not in group['seen']:
                        if has_more or (cursor > prev_cursor and group['count'] < total):
                            group['seen'].add(cursor)
                            group['cursor'] = cursor
        return states
    finally:
        METER.stage('state_read', time.monotonic() - started)


def verified_send(bot, original, aweme_id, comments, digg_type=1, concurrency=8):
    comments = list(comments)
    if not comments:
        return []
    bot._check_stop()
    if getattr(_WORKER, 'active', False):
        _WORKER.seen = True
        _WORKER.requested += len(comments)
    if not getattr(bot, '_api_identity_confirmed', False):
        # Reused pool workers inject cookies directly. Their identity was
        # previously checked by cookie_api_session.send, inside this wrapper.
        from cookie_api_session import identity_ok, _sync_page_identity
        identity_started = time.monotonic()
        try:
            ok, name = bot.check_live_api()
            bot.last_login_username = name or ''
            bot._api_identity_confirmed = bool(ok and identity_ok(bot))
            if bot._api_identity_confirmed:
                _sync_page_identity(bot)
        except Exception:
            bot._api_identity_confirmed = False
        finally:
            METER.stage('verify_identity', time.monotonic() - identity_started)
        if not bot._api_identity_confirmed:
            METER.record('', str(aweme_id), (), {'not_sent': len(comments)})
            return [(False, -4, 'Chưa xác nhận phiên đúng acc; chưa gửi') for _ in comments]
    video = str(aweme_id or '')
    if not video.isdigit():
        return [(False, -4, 'ID video chưa hợp lệ; chưa gửi') for _ in comments]
    desired = 1 if digg_type == 1 else 0
    rows = [(False, -4, 'CID rỗng hoặc trùng; chưa gửi') for _ in comments]
    pending, seen, existing = [], set(), set()
    for index, row in enumerate(comments):
        cid = str(row.get('cid') or '')
        if not cid or cid in seen:
            continue
        seen.add(cid)
        pending.append(index)
    if pending:
        bot._check_stop()
        selected = [comments[i] for i in pending]
        sent = list(original(bot, video, selected, digg_type=digg_type, concurrency=concurrency))
        unverified_indices = [
            pos for pos, r in enumerate(sent)
            if pos < len(selected)
            and r[0]
            and r[2] != 'confirmed_user_digged=1'
            and r[1] not in (-3, 8, 429)
        ]
        after = {}
        if unverified_indices and not getattr(bot, 'last_session_invalid', False):
            unverified_rows = [selected[pos] for pos in unverified_indices]
            after = read_states(bot, video, unverified_rows)
        for position, index in enumerate(pending):
            cid = str(comments[index]['cid'])
            result = sent[position] if position < len(sent) else (False, -4, 'Thiếu phản hồi')
            if result[1] in (8, 429):
                rows[index] = result
            elif result[0] and result[2] == 'confirmed_user_digged=1':
                rows[index] = (True, 0, 'confirmed_user_digged=1' if desired else 'confirmed_removed')
            elif result[1] == -3:
                rows[index] = (False, -3, result[2] or 'unconfirmed_user_digged=0')
            elif not result[0]:
                rows[index] = result
            elif after.get(cid) == desired:
                rows[index] = (True, 0, 'confirmed_user_digged=1' if desired else 'confirmed_removed')
            elif after.get(cid) == 1 - desired:
                rows[index] = (False, -3, 'unconfirmed_user_digged=0')
            else:
                rows[index] = (False, -4, 'Trạng thái sau chưa đọc được; không tự gửi lại')
    new_cids = {str(comments[i]['cid']) for i, row in enumerate(rows)
                if row[0] and row[2] == 'confirmed_user_digged=1'}
    summary = dict(requested=len(comments), confirmed=sum(bool(row[0]) for row in rows),
                   changed_confirmed=len(new_cids), already_present=len(existing),
                   unread=sum(row[1] == -4 for row in rows),
                   explicit_opposite=sum(row[1] == -3 for row in rows),
                   not_sent=len(comments) - len(pending) - len(existing), before_unknown=0)
    account = str(getattr(bot, 'last_login_username', '') or '').lstrip('@').casefold()
    account_key = hashlib.sha256(account.encode()).hexdigest()[:16]
    credited = METER.record(account_key, video, new_cids, summary)
    duplicates = new_cids - credited
    if duplicates:
        rows = [(True, 0, 'already_present')
                if row[2] == 'confirmed_user_digged=1' and str(comments[i].get('cid')) in duplicates
                else row for i, row in enumerate(rows)]
        existing.update(duplicates)
        new_cids = credited
        summary.update(changed_confirmed=len(new_cids), already_present=len(existing))
    bot.last_digg_verification = summary
    bot._last_verified_confirmed = int(summary.get('confirmed', 0))
    if summary.get('unread', 0) > 0:
        bot._needs_page_reload = True
        bot._consecutive_invalid_status = getattr(bot, '_consecutive_invalid_status', 0) + 1
    if summary.get('confirmed', 0) == 0:
        bot._needs_page_reload = True
        bot._consecutive_zero_accounts = getattr(bot, '_consecutive_zero_accounts', 0) + 1
    else:
        bot._consecutive_invalid_status = 0
        bot._consecutive_zero_accounts = 0
    if getattr(_WORKER, 'active', False):
        _WORKER.new_likes += len(new_cids)
        _WORKER.satisfied += summary['confirmed']
    if account and (summary['confirmed'] > 0 or (summary['explicit_opposite'] > 0 and summary['unread'] == 0)):
        _append('digg-verification.jsonl', dict(summary, account=account, aweme_id=video,
                                               logged_at=time.time(), run_id=METER.run_id))
    bot.log(f"[v94] Tim thật +{len(new_cids)}; đã có {len(existing)}; "
            f"chưa đọc được {summary['unread']}; tim ảo bị loại {summary['explicit_opposite']}.")
    return rows


def install():
    import tiktok_reply as tt
    from client.app.runner import Runner, RunStats
    from client.app.bridge import Bridge
    cls = tt.TikTokLikeBot
    if getattr(cls, '_performance_v95', False):
        return
    def timed(name, original):
        @wraps(original)
        def call(*args, **kwargs):
            started = time.monotonic()
            try:
                return original(*args, **kwargs)
            finally:
                METER.stage(name, time.monotonic() - started)
        return call
    start_browser = cls.start_browser
    @wraps(start_browser)
    def start(self, *args, **kwargs):
        kwargs['http_debug'] = bool(kwargs.get('debug') or os.environ.get('ALC_HTTP_TRACE') == '1')
        return start_browser(self, *args, **kwargs)
    cls.start_browser = start
    for name in ('start_browser', 'reset_account_session', 'login_with_cookies', 'open_link'):
        setattr(cls, name, timed(name, getattr(cls, name)))
    original_send = cls.api_digg_comments
    @wraps(original_send)
    def send(self, *args, **kwargs):
        started = time.monotonic()
        try:
            return verified_send(self, original_send, *args, **kwargs)
        finally:
            METER.stage('verified_batch', time.monotonic() - started)
    cls.api_digg_comments = send
    original_link = Runner._do_one_link
    @wraps(original_link)
    def link(self, *args, **kwargs):
        _WORKER.active, _WORKER.seen = True, False
        _WORKER.new_likes = _WORKER.satisfied = _WORKER.requested = 0
        return original_link(self, *args, **kwargs)
    Runner._do_one_link = link
    original_add = RunStats.add_result
    @wraps(original_add)
    def add(self, link_idx, ok, likes=1, complete=None):
        if getattr(_WORKER, 'active', False) and getattr(_WORKER, 'seen', False):
            # The compiled runner uses `last_like_count or 1` on success.
            # Override that fallback with the measured delta, including zero.
            likes = _WORKER.new_likes
            if _WORKER.requested > 0 and likes <= 0:
                ok = False
            complete = bool(_WORKER.requested and _WORKER.satisfied == _WORKER.requested)
            _WORKER.active = False
        return original_add(self, link_idx, ok, likes=likes, complete=complete)
    RunStats.add_result = add
    original_report = Runner._report_account_link
    @wraps(original_report)
    def report(self, acc_no, acc_name, link, like_type, vid_ok, cmt_liked, cmt_found,
               vip_n=0, cmt_failure=''):
        if getattr(_WORKER, 'active', False) and getattr(_WORKER, 'seen', False):
            cmt_liked = _WORKER.new_likes
        return original_report(self, acc_no, acc_name, link, like_type, vid_ok,
                               cmt_liked, cmt_found, vip_n=vip_n, cmt_failure=cmt_failure)
    Runner._report_account_link = report
    original_run = Runner.run
    @wraps(original_run)
    def run(self, accounts, proxies, opts, *args, **kwargs):
        METER.reset(min(max(1, int(getattr(opts, 'threads', 1))), len(accounts)))
        METER.enabled = bool(getattr(opts, 'api_like', False)
                             and getattr(opts, 'like_type', '') == 'comment'
                             and getattr(opts, 'job', 'like') == 'like')
        try:
            return original_run(self, accounts, proxies, opts, *args, **kwargs)
        finally:
            METER.finish()
    Runner.run = run
    original_progress = Bridge.get_progress
    @wraps(original_progress)
    def progress(self, *args, **kwargs):
        result = original_progress(self, *args, **kwargs)
        if METER.enabled and isinstance(result, dict) and result.get('api_like') and result.get('job', 'like') == 'like':
            result = dict(result, performance=METER.snapshot())
        return result
    Bridge.get_progress = progress
    for name, paused in (('pause_run', True), ('resume_run', False)):
        original = getattr(Bridge, name)
        def wrap(original, paused):
            @wraps(original)
            def change(*args, **kwargs):
                result = original(*args, **kwargs)
                if not (isinstance(result, dict) and result.get('ok') is False):
                    METER.pause(paused)
                return result
            return change
        setattr(Bridge, name, wrap(original, paused))
    cls._performance_v95 = True
    if os.environ.get('ALC_V95_BENCHMARK') == '1':
        def benchmark():
            # Worker threads import tiktok_reply too. Let this module finish
            # importing before starting a threaded run, avoiding its import lock.
            while getattr(getattr(tt, '__spec__', None), '_initializing', False):
                time.sleep(0.05)
            import importlib.util
            path = Path(__file__).resolve().parents[2] / 'diagnostics/v95/live_benchmark.py'
            spec = importlib.util.spec_from_file_location('v95_live_benchmark', str(path))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            os._exit(0 if module.run() else 1)
        threading.Thread(target=benchmark, daemon=True, name='v95-live-benchmark').start()
    if os.environ.get('ALC_V95_SELFTEST') == '1':
        import importlib.util
        path = Path(__file__).resolve().parents[2] / 'diagnostics/v95/embedded_checks.py'
        spec = importlib.util.spec_from_file_location('v95_embedded_checks', str(path))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        os._exit(0 if module.run() else 1)

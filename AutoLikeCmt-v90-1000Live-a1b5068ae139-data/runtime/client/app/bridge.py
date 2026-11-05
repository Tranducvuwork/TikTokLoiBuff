import json
import os
import platform
import psutil
import shutil
import threading
import time
import uuid
from pathlib import Path

ROOT = Path.home() / '.auto-like-cmt'

def _read_pool():
    p = ROOT / 'local_pool_data.json'
    if not p.exists():
        p = ROOT / 'pool.json'
    if not p.exists():
        return {'accounts': [], 'proxies': []}
    return json.loads(p.read_text(encoding='utf-8-sig'))

def _write_pool(data):
    for name in ('local_pool_data.json', 'pool.json'):
        path = ROOT / name
        temp = ROOT / (name + '.' + str(uuid.uuid4()) + '.tmp')
        temp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        os.replace(temp, path)

class _LicenseStub:
    def __init__(self):
        self.pool = {}
    def is_licensed(self): return True
    def startup(self): return True
    def refresh_access(self): return True
    def _try_refresh(self): return True
    def _start_heartbeat(self): pass
    def _do_heartbeat(self): pass
    def run_done(self, *a, **k): pass
    def report_account_results(self, results, *a, **k): pass
    def refresh_pool(self): return {}

class Bridge:
    def __init__(self):
        self.lic = _LicenseStub()
        self._pool = _read_pool()
        self.lic.pool = self._pool
        self._lock = threading.RLock()
        self._window = None
        self._on_top = False
        self._running = False
        self._paused = False
        self._stop_event = threading.Event()
        self._logs = []
        self._log_cursor = 0
        self._results = []
        self._progress = {'done': 0, 'total': 0, 'digged': 0}
        self._live_check_running = False
        self._live_check_paused = False
        self._run_opts = None

    def get_status(self):
        accs = self._pool.get('accounts', [])
        live = sum(1 for a in accs if str(a.get('status', '')).lower() == 'live')
        die = sum(1 for a in accs if str(a.get('status', '')).lower() == 'die')
        banned = sum(1 for a in accs if str(a.get('status', '')).lower() == 'banned')
        unknown = len(accs) - live - die - banned
        proxies = self._pool.get('proxies', [])
        hint = self._compute_thread_hint()
        return {
            'ok': True,
            'version': 'v95',
            'edition': 'VIP',
            'total': len(accs),
            'live': live,
            'die': die,
            'banned': banned,
            'unknown': unknown,
            'proxies': len(proxies),
            'pool_summary': {
                'total': len(accs), 'live': live, 'die': die,
                'banned': banned, 'unknown': unknown,
                'accounts': len(accs), 'proxies': len(proxies),
            },
            'thread_hint': hint,
        }

    def _compute_thread_hint(self):
        try:
            ram_gb = psutil.virtual_memory().total / (1024**3)
            cpus = os.cpu_count() or 4
            suggested = min(int(ram_gb // 1.5), cpus * 2, 12)
            return {
                'suggested': max(1, suggested),
                'ram_gb': round(ram_gb, 1),
                'cpus': cpus,
            }
        except Exception:
            return {'suggested': 4, 'ram_gb': 8, 'cpus': 4}

    def get_thread_hint(self):
        return self._compute_thread_hint()

    def resume_saved_activation(self):
        act_file = ROOT / 'remembered-activation.bin'
        if act_file.exists() and act_file.stat().st_size > 0:
            return {'ok': True, 'pool_summary': self.get_status().get('pool_summary', {})}
        return {'ok': False, 'needs_key': True}

    def login(self, key):
        act_file = ROOT / 'remembered-activation.bin'
        act_file.write_bytes(key.encode('utf-8')[:256])
        return {'ok': True}

    def logout(self):
        act_file = ROOT / 'remembered-activation.bin'
        if act_file.exists():
            act_file.unlink()
        return {'ok': True}

    def sync_state(self):
        return {'ok': True, 'synced': True}

    def toggle_on_top(self):
        self._on_top = not self._on_top
        if self._window:
            self._window.on_top = self._on_top
        return {'ok': True, 'on_top': self._on_top}

    def get_accounts(self):
        with self._lock:
            self._pool = _read_pool()
            accs = self._pool.get('accounts', [])
            return {
                'ok': True,
                'accounts': [{k: a.get(k) for k in ('id', 'username', 'login_user', 'status')} for a in accs],
                'proxies': [{k: p.get(k) for k in ('id', 'address', 'status', 'test_status', 'note') if k in p} for p in self._pool.get('proxies', [])],
            }

    def local_pool_snapshot(self):
        return self.get_accounts()

    def refresh_pool(self):
        with self._lock:
            self._pool = _read_pool()
        return {'ok': True, 'accounts': len(self._pool.get('accounts', [])), 'proxies': len(self._pool.get('proxies', []))}

    def import_accounts(self, lines, account_type='totp'):
        from v90_pool_runtime_fix import import_local
        result = import_local(lines, account_type=account_type, section='accounts')
        with self._lock:
            self._pool = _read_pool()
        return result

    def import_comment_accounts(self, lines, account_type='totp'):
        from v90_pool_runtime_fix import import_local
        result = import_local(lines, account_type=account_type, section='comment_accounts')
        with self._lock:
            self._pool = _read_pool()
        return result

    def local_delete_accounts(self, ids=None, status=None):
        return self.local_delete_proxy(ids, status)

    def local_delete_proxy(self, ids=None, status=None):
        from v90_pool_runtime_fix import delete_local
        result = delete_local(ids, status)
        with self._lock:
            self._pool = _read_pool()
        return result

    def delete_dead(self):
        return self.local_delete_proxy(status='die')

    def delete_banned(self):
        return self.local_delete_proxy(status='banned')

    def delete_unknown(self):
        return self.local_delete_proxy(status='unknown')

    def get_comment_accounts(self):
        with self._lock:
            self._pool = _read_pool()
            accs = self._pool.get('comment_accounts', [])
            return {
                'ok': True,
                'accounts': [{k: a.get(k) for k in ('id', 'username', 'login_user', 'status')} for a in accs],
            }

    def delete_comment_accounts(self, ids=None, status=None):
        from v90_pool_runtime_fix import delete_local
        with self._lock:
            data = _read_pool()
            selected = {str(x) for x in ids or []}
            ca = data.get('comment_accounts', [])
            if status:
                removed = [a for a in ca if str(a.get('status', '')).lower() == status]
            else:
                removed = [a for a in ca if str(a.get('id')) in selected]
            removed_ids = {str(a.get('id')) for a in removed}
            data['comment_accounts'] = [a for a in ca if str(a.get('id')) not in removed_ids]
            _write_pool(data)
            self._pool = data
        return {'ok': True, 'deleted': len(removed)}

    def get_proxysach_status(self):
        proxies = self._pool.get('proxies', [])
        return {
            'ok': True,
            'active': len(proxies),
            'configured': len(proxies),
            'manual': 0,
            'endpoints': [{
                'id': p.get('id'),
                'address': p.get('address', ''),
                'status': p.get('status', 'unknown'),
                'test_status': p.get('test_status', ''),
                'note': p.get('note', ''),
            } for p in proxies],
        }

    def check_proxies(self):
        return {'ok': True, 'started': True}

    def get_proxy_test_status(self):
        return {'ok': True, 'running': False, 'results': []}

    def add_manual_proxies(self, lines):
        with self._lock:
            data = _read_pool()
            proxies = data.get('proxies', [])
            max_id = max([int(p.get('id') or 0) for p in proxies] + [0])
            added = 0
            for raw in str(lines or '').splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                max_id += 1
                proxies.append({'id': max_id, 'address': raw, 'raw': raw, 'status': 'unknown', 'test_status': '', 'note': ''})
                added += 1
            data['proxies'] = proxies
            _write_pool(data)
            self._pool = data
        return {'ok': True, 'added': added}

    def _make_run_opts(self, opts):
        from types import SimpleNamespace
        links = [l.strip() for l in (opts.get('links') or []) if l.strip() and 'vt.tiktok.com/....' not in l]
        return SimpleNamespace(
            links=links,
            threads=opts.get('threads', 1),
            target_count=opts.get('target_count', 0),
            target_text=opts.get('target_text', ''),
            target_texts=opts.get('target_texts', []),
            find_mode=opts.get('find_mode', 'all'),
            like_type=opts.get('like_type', 'comment'),
            api_like=opts.get('api_like', False),
            autocap=opts.get('autocap', True),
            headless=opts.get('headless', True),
            debug=opts.get('debug', False),
            random_accounts=opts.get('random_accounts', False),
            job=opts.get('job', 'like'),
            api_capture=opts.get('api_capture', False),
        )

    def start_run(self, opts):
        if self._running:
            return {'ok': False, 'error': 'Đang chạy'}

        from client.app.runner import Runner

        run_opts = self._make_run_opts(opts)
        if not run_opts.links:
            return {'ok': False, 'error': 'Chưa nhập link.'}

        self._running = True
        self._paused = False
        self._logs = []
        self._results = []
        self._progress = {'done': 0, 'total': 0, 'digged': 0}
        self._run_opts = run_opts

        runner = Runner(self, log_fn=self._log)
        self._runner = runner
        self._stop_event = runner.stop_event

        accs = self._pool.get('accounts', [])
        live_accs = [a for a in accs if str(a.get('status', '')).lower() in ('live', 'unknown', '?', '')]
        proxies = self._pool.get('proxies', [])
        self._progress['total'] = len(live_accs)

        self._log(f'[debug] live_accs={len(live_accs)}, proxies={len(proxies)}, api_like={run_opts.api_like}, like_type={run_opts.like_type}, job={run_opts.job}, links={len(run_opts.links)}')

        def worker():
            try:
                self._run_and_report(live_accs, proxies, run_opts)
            except Exception as e:
                import traceback
                self._log(f'Lỗi tổng: {e}')
                self._log(traceback.format_exc())
            finally:
                self._running = False

        t = threading.Thread(target=worker, daemon=True, name='bridge-run-worker')
        self._runner_thread = t
        t.start()
        return {'ok': True}

    def _run_and_report(self, accounts, proxies, opts):
        runner = self._runner
        runner.run(accounts, proxies, opts)

    def stop_run(self, force=False):
        if force:
            self._stop_event.set()
            if hasattr(self, '_runner'):
                self._runner.stop_event.set()
            self._running = False
            return {'ok': True, 'forced': True}
        self._stop_event.set()
        if hasattr(self, '_runner'):
            self._runner.stop_event.set()
        return {'ok': True}

    def pause_run(self):
        self._paused = True
        if hasattr(self, '_runner'):
            self._runner.pause_event.clear()
        return {'ok': True}

    def resume_run(self):
        self._paused = False
        if hasattr(self, '_runner'):
            self._runner.pause_event.set()
        return {'ok': True}

    def get_progress(self):
        runner = getattr(self, '_runner', None)
        snap = runner.stats.snapshot() if runner else {}
        accs = self._pool.get('accounts', [])
        proxies = self._pool.get('proxies', [])
        live = sum(1 for a in accs if str(a.get('status', '')).lower() == 'live')
        die = sum(1 for a in accs if str(a.get('status', '')).lower() == 'die')
        banned = sum(1 for a in accs if str(a.get('status', '')).lower() == 'banned')
        run_opts = getattr(self, '_run_opts', None)
        return {
            'running': self._running,
            'paused': self._paused,
            'done': snap.get('total_done', self._progress.get('done', 0)),
            'total': self._progress.get('total', 0),
            'ok': snap.get('total_likes', 0),
            'acc_ok': snap.get('total_ok', 0),
            'target': snap.get('target', 0),
            'job': getattr(run_opts, 'job', 'like') if run_opts else 'like',
            'api_like': getattr(run_opts, 'api_like', False) if run_opts else False,
            'like_type': getattr(run_opts, 'like_type', 'comment') if run_opts else 'comment',
            'live_checking': self._live_check_running,
            'live_paused': self._live_check_paused,
            'solver_ready': True,
            'pool_dirty': True,
            'pool_summary': {
                'accounts': len(accs), 'proxies': len(proxies),
                'total': len(accs), 'live': live, 'die': die,
                'banned': banned, 'unknown': len(accs) - live - die - banned,
            },
        }

    def get_logs(self, cursor=0):
        cursor = int(cursor or 0)
        logs = self._logs[cursor:]
        return {
            'ok': True,
            'logs': logs,
            'next': len(self._logs),
            'results': self._results,
            'running': self._running,
        }

    def _log(self, msg):
        ts = time.strftime('%H:%M:%S')
        self._logs.append(f'[{ts}] {msg}')
        if self._window:
            try:
                self._window.evaluate_js(f'appendLog({json.dumps(self._logs[-1])})')
            except Exception:
                pass

    def start_live_check(self, ids=None, threads=4, headless=True):
        if self._live_check_running:
            return {'ok': False, 'error': 'Đang check'}
        self._live_check_running = True
        self._live_check_paused = False

        def worker():
            try:
                import tiktok_reply as tt
                accs = self._pool.get('accounts', [])
                if ids:
                    id_set = set(str(x) for x in ids)
                    accs = [a for a in accs if str(a.get('id')) in id_set]
                self._log(f'Bắt đầu check {len(accs)} account...')
                for i, acc in enumerate(accs):
                    if not self._live_check_running:
                        break
                    name = acc.get('username') or acc.get('login_user') or f"acc{acc.get('id')}"
                    cookie = acc.get('cookie', '')
                    if not cookie:
                        continue
                    try:
                        import cookie_api_session
                        result = cookie_api_session.check_cookie_live(cookie)
                        new_status = 'live' if result else 'die'
                    except Exception:
                        new_status = 'unknown'
                    acc['status'] = new_status
                    self._log(f'[{i+1}/{len(accs)}] {name}: {new_status}')
                with self._lock:
                    _write_pool(self._pool)
                self._log('Check live hoàn tất.')
            except Exception as e:
                self._log(f'Check live lỗi: {e}')
            finally:
                self._live_check_running = False

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        return {'ok': True}

    def stop_live_check(self):
        self._live_check_running = False
        return {'ok': True}

    def pause_live_check(self):
        self._live_check_paused = True
        return {'ok': True}

    def resume_live_check(self):
        self._live_check_paused = False
        return {'ok': True}

    def is_live_check_running(self):
        return {'running': self._live_check_running, 'paused': self._live_check_paused}

    def get_captcha_balance(self):
        try:
            from omo_captcha import load_omo_key, load_cap_provider
            provider = load_cap_provider()
            key = load_omo_key()
            return {'ok': True, 'provider': provider, 'has_key': bool(key), 'ready': bool(key)}
        except Exception:
            return {'ok': True, 'provider': 'omo', 'has_key': False, 'ready': False}

    def check_update(self):
        return {'ok': True, 'has_update': False}

    def install_update(self):
        return {'ok': False, 'error': 'Cập nhật không khả dụng trên macOS.'}

    def get_comment_config(self):
        cfg_path = ROOT / 'comment_config.json'
        if cfg_path.exists():
            return json.loads(cfg_path.read_text(encoding='utf-8'))
        return {'templates': [], 'phrases': []}

    def save_comment_config(self, config):
        cfg_path = ROOT / 'comment_config.json'
        cfg_path.write_text(json.dumps(config, ensure_ascii=False), encoding='utf-8')
        return {'ok': True}

    def start_comment_run(self, opts):
        return {'ok': False, 'error': 'Comment run chưa sẵn sàng trên macOS.'}

    def start_comment_live_check(self, ids=None, threads=4, headless=True):
        return self.start_live_check(ids, threads, headless)

    def start_comment_account_action(self, action, ids=None, headless=False, debug=False):
        return {'ok': False, 'error': 'Chức năng này chưa sẵn sàng trên macOS.'}

    def stop_comment_account_action(self):
        return {'ok': True}

    def get_auto_comment_handoff(self):
        return {'ok': True, 'data': {}}

    def import_auto_comment_handoff(self, raw_json=''):
        return {'ok': False, 'error': 'Chưa hỗ trợ.'}

    def _machine_id(self):
        return platform.node() or 'mac-local'

    def get_machine_proxy_allocation(self):
        p = ROOT / 'machine_proxy_allocation.json'
        if p.exists():
            data = json.loads(p.read_text(encoding='utf-8'))
            data.setdefault('ok', True)
            data.setdefault('machine', self._machine_id())
            data['count'] = int(data.get('count', 3))
            return data
        return {'ok': True, 'count': 3, 'machine': self._machine_id()}

    def set_machine_proxy_allocation(self, count):
        count = int(count)
        p = ROOT / 'machine_proxy_allocation.json'
        data = {'ok': True, 'count': count, 'machine': self._machine_id()}
        p.write_text(json.dumps(data), encoding='utf-8')
        return data

    def get_pool_captcha_settings(self):
        p = ROOT / 'pool_captcha_settings.json'
        data = {}
        if p.exists():
            raw = json.loads(p.read_text(encoding='utf-8'))
            if isinstance(raw, dict):
                data = raw
            else:
                data = {'enabled': bool(raw)}
        return {
            'ok': True,
            'provider': data.get('provider', 'omo'),
            'enabled': data.get('enabled', data.get('autocap', True)),
            'solver_ready': True,
        }

    def set_pool_captcha_settings(self, enabled=True):
        p = ROOT / 'pool_captcha_settings.json'
        data = {'enabled': bool(enabled), 'provider': 'omo'}
        p.write_text(json.dumps(data), encoding='utf-8')
        return {'ok': True}

    def clear_machine_proxy_session(self):
        return {'ok': True}

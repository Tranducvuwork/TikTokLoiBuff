"""Keep the UI-selected check worker count independent of proxy count."""
import functools
import inspect
import json
from pathlib import Path

def pool_proxies():
    import machine_proxy_config as cfg
    pool = json.loads(Path(cfg.POOL_PATH).read_text(encoding='utf-8-sig'))
    rows = pool.get('proxies', [])
    if not isinstance(rows, list):
        raise ValueError('Danh sách proxy trong pool không hợp lệ.')
    result = []
    seen = set()
    for row in rows:
        raw = (row.get('raw') or row.get('address') or '') if isinstance(row, dict) else row
        raw = str(raw or '').strip()
        import tiktok_reply as tt
        if raw and raw not in seen and not tt._proxy_blocked(tt.parse_proxy(raw)):
            seen.add(raw)
            result.append(raw)
    if not result:
        raise ValueError('Pool chưa có proxy để check tài khoản.')
    return result

def install():
    from client.app.bridge import Bridge
    import admin_check_accounts as checker
    if getattr(Bridge, '_check_threads_unlimited', False):
        return
    selected = {'threads': None, 'headless': True}
    original_all = checker.check_all
    signature = inspect.signature(original_all)
    @functools.wraps(original_all)
    def check_all(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        if selected['threads'] is not None:
            bound.arguments['threads'] = selected['threads']
        # The bridge can pass only two leased proxies. Always use the entire
        # local pool for check, independent of the buff allocation setting.
        bound.arguments['proxies'] = pool_proxies()
        bound.arguments['headless'] = selected['headless']
        log_fn = bound.arguments.get('log_fn')
        if log_fn:
            mode = 'check ẩn qua API' if selected['headless'] else 'Chrome hiển thị'
            log_fn(f"[check] Dùng toàn bộ {len(bound.arguments['proxies'])} proxy trong pool; {bound.arguments.get('threads') or 8} luồng, {mode}, không giới hạn theo proxy.")
        return original_all(*bound.args, **bound.kwargs)
    checker.check_all = check_all
    def wrap_start(original):
        sig = inspect.signature(original)
        @functools.wraps(original)
        def start(*args, **kwargs):
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            selected['threads'] = max(1, int(bound.arguments.get('threads') or 8))
            selected['headless'] = bool(bound.arguments.get('headless', True))
            return original(*bound.args, **bound.kwargs)
        return start
    for name in ('start_live_check', 'start_comment_live_check'):
        if hasattr(Bridge, name):
            setattr(Bridge, name, wrap_start(getattr(Bridge, name)))
    Bridge._check_threads_unlimited = True

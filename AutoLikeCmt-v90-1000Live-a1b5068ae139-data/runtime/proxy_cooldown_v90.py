import json
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path

ROOT = Path.home() / '.auto-like-cmt'
PATH = ROOT / 'proxy-cooldowns-v90.json'
LOCK = threading.RLock()

def read():
    try:
        return json.loads(PATH.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}

def probe(proxy):
    from local_proxy_pool import request_via_proxy
    code, status = request_via_proxy(proxy, 'https://www.tiktok.com/')
    return code == 0 and 200 <= status < 400


def install():
    import tiktok_reply as tt
    original = tt._proxy_blocked
    original_blacklist = tt.blacklist_proxy
    cache = {'stamp': None, 'keys': {}}

    def quarantine(proxy, ttl=60):
        original_blacklist(proxy, ttl=ttl)
        if not proxy:
            return
        # Persist pool IDs only; never write proxy passwords to diagnostic files.
        try:
            pool = json.loads((ROOT / 'local_pool_data.json').read_text(encoding='utf-8-sig'))
            target_key = tt._proxy_key(proxy)
            endpoint = (proxy.get('host'), str(proxy.get('port')))
            has_auth = bool(proxy.get('user') or proxy.get('password'))
            with LOCK:
                plan = read()
                for row in pool.get('proxies', []):
                    parsed = tt.parse_proxy(str(row.get('raw') or row.get('address') or ''))
                    if not parsed:
                        continue
                    matched = (
                        tt._proxy_key(parsed) == target_key
                        if has_auth
                        else (parsed.get('host'), str(parsed.get('port'))) == endpoint
                    )
                    if matched:
                        id = str(row.get('id'))
                        until = max(float(plan.get(id, {}).get('until', 0)), time.time() + ttl)
                        plan[id] = {'until': until, 'reason': 'runtime_proxy_failure', 'generation': str(uuid.uuid4())}
                temp = ROOT / ('proxy-cooldowns-' + str(uuid.uuid4()) + '.tmp')
                temp.write_text(json.dumps(plan), encoding='utf-8')
                os.replace(temp, PATH)
                cache['stamp'] = None
        except (OSError, ValueError, TypeError):
            # The original blacklist remains in effect if persistence fails.
            return

    tt.blacklist_proxy = quarantine

    def paused(proxy):
        if not proxy:
            return False
        with LOCK:
            try:
                stamp = (PATH.stat().st_mtime_ns, (ROOT / 'local_pool_data.json').stat().st_mtime_ns)
                if cache['stamp'] != stamp:
                    plan = read()
                    pool = json.loads((ROOT / 'local_pool_data.json').read_text(encoding='utf-8-sig'))
                    keys = {}
                    for row in pool.get('proxies', []):
                        if str(row.get('id')) in plan:
                            parsed = tt.parse_proxy(str(row.get('raw') or row.get('address') or ''))
                            if parsed:
                                keys[tt._proxy_key(parsed)] = True
                    cache.update(stamp=stamp, keys=keys)
                return tt._proxy_key(proxy) in cache['keys']
            except (OSError, ValueError):
                return False

    tt._proxy_blocked = lambda proxy: paused(proxy) or original(proxy)

    def recheck():
        while True:
            time.sleep(3)
            try:
                with LOCK:
                    plan = read()
                    due = [(id, dict(item)) for id, item in plan.items() if float(item.get('until', 0)) <= time.time()]
                if not due:
                    continue
                pool = json.loads((ROOT / 'local_pool_data.json').read_text(encoding='utf-8-sig'))
                by_id = {str(row.get('id')): row for row in pool.get('proxies', [])}
                for id, tested in due:
                    row = by_id.get(id)
                    parsed = tt.parse_proxy(str(row.get('raw') or row.get('address') or '')) if row else None
                    healthy = probe(parsed) if parsed else False
                    with LOCK:
                        current = read()
                        if current.get(id) != tested or float(current[id].get('until', 0)) > time.time():
                            continue
                        if healthy or row is None:
                            current.pop(id, None)
                        else:
                            current[id] = {'until': time.time() + 60, 'reason': 'recheck_failed'}
                        temp = ROOT / ('proxy-cooldowns-' + str(uuid.uuid4()) + '.tmp')
                        temp.write_text(json.dumps(current), encoding='utf-8')
                        os.replace(temp, PATH)
            except (OSError, ValueError):
                continue

    threading.Thread(target=recheck, daemon=True, name='proxy-cooldown-recheck').start()

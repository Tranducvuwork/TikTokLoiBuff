"""Authoritative local proxy statuses and permanent user-requested deletion."""
import hashlib
import json
import os
import subprocess
import threading
import time
import uuid
import urllib.request
import urllib.error
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

ROOT = Path.home() / '.auto-like-cmt'
LOCK = threading.RLock()
RUNNING = False

def raw(row):
    return str(row.get('raw') or row.get('address') or '').strip()

def identity(row):
    return hashlib.sha256(raw(row).encode('utf-8')).hexdigest()

def read(name, default):
    try: return json.loads((ROOT / name).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError): return default

def write(name, value):
    temporary = ROOT / (name + '.' + str(uuid.uuid4()) + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, ROOT / name)

def save_pool(data):
    for name in ('local_pool_data.json', 'pool.json'): write(name, data)
    import local_account_results as store
    for bridge in store.BRIDGES: bridge._pool = data

def state(row):
    test = row.get('test_status')
    return test.get('state', 'untested') if isinstance(test, dict) else 'untested'

def public_row(row):
    import tiktok_reply as tt
    try: parsed = tt.parse_proxy(raw(row), row.get('proxy_type') or 'http')
    except (ValueError, TypeError): parsed = None
    address = f"{parsed['host']}:{parsed['port']}" if parsed else 'Proxy không hợp lệ'
    return {'id': row.get('id'), 'address': address,
            'proxy_type': row.get('proxy_type') or (parsed or {}).get('scheme', 'http'),
            'test_status': row.get('test_status') if isinstance(row.get('test_status'), dict) else {'state':'untested'}}

def snapshot():
    with LOCK:
        rows = read('local_pool_data.json', {}).get('proxies', [])
        summary = {s: sum(state(row) == s for row in rows) for s in ('untested', 'testing', 'live', 'blocked', 'dead')}
        return {'ok':True, 'enabled':False, 'details':[], 'manual_details':[public_row(row) for row in rows],
                'manual_proxies':len(rows), 'total_proxies':len(rows), 'test_running':RUNNING, 'test_summary':summary}

def request_via_proxy(proxy, url):
    # Use the application's relay and Python TLS, avoiding Windows Schannel.
    from proxy_relay import ProxyRelay, Socks5Relay
    relay_type = Socks5Relay if str(proxy.get('scheme','http')).startswith('socks') else ProxyRelay
    relay = relay_type(proxy['host'], int(proxy['port']), proxy.get('user',''), proxy.get('password',''))
    try:
        port = relay.start()
        handler = urllib.request.ProxyHandler({'http':f'http://127.0.0.1:{port}', 'https':f'http://127.0.0.1:{port}'})
        opener = urllib.request.build_opener(handler)
        with opener.open(url, timeout=8) as response: return 0, response.status
    except urllib.error.HTTPError as error: return 0, error.code
    except (OSError, ValueError, urllib.error.URLError): return -1, 0
    finally: relay.close()

def probe(row):
    import tiktok_reply as tt
    try: proxy = tt.parse_proxy(raw(row), row.get('proxy_type') or 'http')
    except (ValueError, TypeError): proxy = None
    if not proxy: return {'state':'dead', 'note':'Định dạng proxy không hợp lệ', 'checked_at':time.time()}
    def request(url): return request_via_proxy(proxy, url)
    code, http = request('https://www.tiktok.com/')
    if code == 0 and 200 <= http < 400: status, note = 'live', 'Kết nối TikTok thành công'
    elif http == 403: status, note = 'blocked', 'Proxy kết nối được; TikTok trả 403'
    else:
        internet_code, internet_http = request('https://example.com/')
        if internet_code == 0 and 200 <= internet_http < 400:
            status, note = 'blocked', f'Internet hoạt động; TikTok lỗi HTTP {http}'
        else: status, note = 'dead', f'Lỗi kết nối hoặc xác thực proxy (HTTP {http})'
    return {'state':status, 'note':note, 'checked_at':time.time(), 'http':http}

def test_all():
    global RUNNING
    import local_account_results as store
    with LOCK, store.LOCK:
        if RUNNING: return {'ok':True, 'count':len(read('local_pool_data.json', {}).get('proxies', []))}
        store.flush()
        data = read('local_pool_data.json', {})
        rows = [dict(row) for row in data.get('proxies', [])]
        for row in data.get('proxies', []): row['test_status']={'state':'testing'}
        save_pool(data)
        RUNNING = True
    def run():
        global RUNNING
        def check(row):
            try: result = probe(row)
            except Exception: result = {'state':'dead','note':'Không kiểm tra được kết nối', 'checked_at':time.time()}
            with LOCK, store.LOCK:
                store.flush()
                current = read('local_pool_data.json', {})
                for saved in current.get('proxies', []):
                    if str(saved.get('id')) == str(row.get('id')) and identity(saved) == identity(row):
                        saved['test_status']=result
                        saved['status']=result['state']; saved['state']=result['state']
                save_pool(current)
        try:
            with ThreadPoolExecutor(max_workers=8) as executor: list(executor.map(check, rows))
        finally:
            with LOCK: RUNNING=False
    threading.Thread(target=run, daemon=True, name='local-proxy-test').start()
    return {'ok':True, 'count':len(rows)}

def delete(id=None, mode=None):
    import local_account_results as store
    with LOCK, store.LOCK:
        store.flush()
        data = read('local_pool_data.json', {})
        rows = data.get('proxies', [])
        removed = [row for row in rows if (mode == 'all' or (mode == 'dead' and state(row) == 'dead') or
            (mode is None and id is not None and str(row.get('id')) == str(id)))]
        keys = {identity(row) for row in removed}
        deleted = set(read('deleted-proxy-keys.json', [])) | keys
        # Write tombstones first: packaged defaults can never silently restore deletions.
        write('deleted-proxy-keys.json', sorted(deleted))
        import tiktok_reply as tt
        runtime_keys = set(read('deleted-proxy-runtime-keys.json', []))
        runtime_keys.update(tt._proxy_key(tt.parse_proxy(raw(row))) for row in removed)
        write('deleted-proxy-runtime-keys.json', sorted(runtime_keys))
        data['proxies'] = [row for row in rows if identity(row) not in keys]
        save_pool(data)
        cooldown = read('proxy-cooldowns-v90.json', {})
        for row in removed: cooldown.pop(str(row.get('id')), None)
        write('proxy-cooldowns-v90.json', cooldown)
        lines = [raw(row) for row in data['proxies']]
        temporary = ROOT / ('proxies.' + str(uuid.uuid4()) + '.tmp')
        temporary.write_text('\n'.join(lines), encoding='utf-8')
        os.replace(temporary, ROOT / 'proxies.txt')
    return {'ok':True, 'deleted':len(removed)}


def add(lines):
    import local_account_results as store
    import tiktok_reply as tt
    if isinstance(lines, str): lines = lines.splitlines()
    if not isinstance(lines, (list, tuple)):
        return {'ok':False, 'error':'Danh sách proxy không hợp lệ.'}
    parsed_rows=[]
    for line in lines:
        line=str(line).strip()
        if not line: continue
        try: parsed=tt.parse_proxy(line)
        except (ValueError,TypeError): parsed=None
        if not parsed:
            return {'ok':False, 'error':'Có proxy sai định dạng; chưa thêm dòng nào.'}
        parsed_rows.append((line,parsed))
    with LOCK, store.LOCK:
        store.flush()
        data=read('local_pool_data.json', {})
        rows=data.setdefault('proxies', [])
        known={raw(row) for row in rows}
        next_id=max([int(row.get('id',0)) for row in rows] or [0])+1
        added=[]
        for line,parsed in parsed_rows:
            if line in known: continue
            row={'id':next_id,'raw':line,'address':line,
                 'proxy_type':parsed.get('scheme','http'),'status':'unknown',
                 'state':'unknown','note':'','test_status':{'state':'untested'}}
            next_id+=1
            known.add(line)
            rows.append(row)
            added.append((row,parsed))
        # Explicit re-addition overrides prior deletion markers.
        deleted=set(read('deleted-proxy-keys.json', []))
        deleted.difference_update(identity(row) for row,_ in added)
        runtime_deleted=set(read('deleted-proxy-runtime-keys.json', []))
        runtime_deleted.difference_update(tt._proxy_key(parsed) for _,parsed in added)
        write('deleted-proxy-keys.json', sorted(deleted))
        write('deleted-proxy-runtime-keys.json', sorted(runtime_deleted))
        save_pool(data)
        temp=ROOT/('proxies.'+str(uuid.uuid4())+'.tmp')
        temp.write_text('\n'.join(raw(row) for row in rows),encoding='utf-8')
        os.replace(temp, ROOT/'proxies.txt')
    return {'ok':True,'added':len(added),'total':len(rows)}

def install():
    from client.app.bridge import Bridge
    import tiktok_reply as tt
    previous = tt._proxy_blocked
    def blocked(proxy):
        return tt._proxy_key(proxy) in read('deleted-proxy-runtime-keys.json', []) or previous(proxy)
    tt._proxy_blocked = blocked
    Bridge.get_proxysach_status = lambda self: snapshot()
    Bridge.get_proxy_test_status = lambda self: snapshot()
    Bridge.check_proxies = lambda self: test_all()
    Bridge.local_delete_proxy = lambda self, id=None, mode=None: delete(id, mode)

    Bridge.add_manual_proxies = lambda self, lines: add(lines)

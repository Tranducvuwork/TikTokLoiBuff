"""Read-only OmoCaptcha package status, refreshed outside the UI thread."""
import json
import threading
import time
import urllib.request
from pathlib import Path

_lock = threading.Lock()
_busy = False
_next = 0
_status = {'ok': False, 'text': 'OmoCaptcha: đang kiểm tra…'}

def fetch(key):
    request = urllib.request.Request(
        'https://api.omocaptcha.com/v2/getMyPackages',
        data=json.dumps({'clientKey': key}).encode(),
        headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=12) as response:
        data = json.load(response)
    if data.get('errorId') != 0:
        return {'ok': False, 'text': 'OmoCaptcha: không đọc được gói'}
    packages = data.get('packages', [])
    matching = [p for p in packages if key in (p.get('token'), p.get('packageToken'), p.get('clientKey'))]
    if not matching:
        matching = packages if len(packages) == 1 else []
    if len(matching) != 1:
        return {'ok': False, 'text': 'OmoCaptcha: chưa xác định được gói của key'}
    package = matching[0]
    remaining = int(package['remaining'])
    active = bool(package.get('isActive'))
    return {'ok': active and remaining > 0, 'remaining': remaining,
            'expires': package.get('dateEnd', ''),
            'text': 'OmoCaptcha: còn ' + f'{remaining:,}'.replace(',', '.') + ' lượt' +
                    ('' if active else ' · gói hết hạn')}

def read():
    global _busy, _next
    with _lock:
        if not _busy and time.monotonic() >= _next:
            _busy = True
            threading.Thread(target=_refresh, daemon=True).start()
        return dict(_status)

def _refresh():
    global _busy, _next, _status
    try:
        import omo_captcha
        key = omo_captcha.load_omo_key()
        result = fetch(key) if key else {'ok': False, 'text': 'OmoCaptcha: chưa có key'}
    except Exception:
        result = {'ok': False, 'text': 'OmoCaptcha: chưa cập nhật được số lượt'}
    with _lock:
        _status = result
        _next = time.monotonic() + (60 if result.get('ok') else 30)
        _busy = False

def install():
    import omo_captcha
    # Resolve shipped settings explicitly even when Nuitka supplies the module.
    omo_captcha.OMO_KEY_FILE = Path(__file__).with_name('omo_key.txt')
    omo_captcha.OMO_PROVIDER_FILE = Path(__file__).with_name('captcha_provider.txt')
    read()

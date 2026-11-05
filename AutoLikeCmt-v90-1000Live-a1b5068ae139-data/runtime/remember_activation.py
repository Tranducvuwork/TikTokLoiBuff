"""Remember a successfully validated key on this machine."""
import json, os, sys
from pathlib import Path

STORE = Path.home() / '.auto-like-cmt' / 'remembered-activation.bin'

def _use_dpapi():
    return sys.platform == 'win32'

def crypt(data, decrypt=False):
    if not _use_dpapi():
        import base64
        if decrypt:
            return base64.b85decode(data)
        return base64.b85encode(data)
    import ctypes
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [('size',wintypes.DWORD),('data',ctypes.POINTER(ctypes.c_ubyte))]
    source=ctypes.create_string_buffer(data)
    incoming=Blob(len(data),ctypes.cast(source,ctypes.POINTER(ctypes.c_ubyte)))
    outgoing=Blob()
    api=ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    if not api(ctypes.byref(incoming),None,None,None,None,1,ctypes.byref(outgoing)):
        raise ctypes.WinError()
    try:return ctypes.string_at(outgoing.data,outgoing.size)
    finally:ctypes.windll.kernel32.LocalFree(outgoing.data)

def save(key):
    STORE.parent.mkdir(parents=True,exist_ok=True)
    temp=STORE.with_suffix('.tmp')
    temp.write_bytes(crypt(json.dumps({'key':key}).encode('utf-8')))
    os.replace(temp,STORE)

def load():
    if not STORE.exists():return ''
    return json.loads(crypt(STORE.read_bytes(),True).decode('utf-8')).get('key','')

def install():
    from client.app.bridge import Bridge
    if getattr(Bridge,'_remember_activation_installed',False):return
    original=Bridge.login
    def login(self,key):
        result=original(self,key)
        if isinstance(result,dict) and result.get('ok') is True:
            try:save(key)
            except Exception:
                result=dict(result,remember_error='Đã kích hoạt nhưng không lưu được key trên máy. Hãy kiểm tra quyền ghi dữ liệu.')
        return result
    def resume(self):
        try:
            if self.lic.is_licensed():return {'ok':True,'restored':True}
            key=load()
            if not key:return {'ok':False,'needs_key':True}
            return login(self,key)
        except Exception:
            return {'ok':False,'error':'Không khôi phục được kích hoạt đã lưu trên máy. Hãy thử kích hoạt lại.'}
    Bridge.login=login
    Bridge.resume_saved_activation=resume
    Bridge._remember_activation_installed=True

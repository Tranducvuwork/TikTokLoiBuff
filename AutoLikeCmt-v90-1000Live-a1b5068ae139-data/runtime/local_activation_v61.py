"""Owner-requested local shared-key activation, without an online license server."""
import hashlib, hmac, json, os
import local_account_results
from pathlib import Path
from remember_activation import save, load

EXPECTED = hashlib.sha256(b'TTK-6PJ3-4EUU-54YJ-HQY4').hexdigest()
ROOT=Path.home()/'.auto-like-cmt'

def valid(key):
    return isinstance(key,str) and hmac.compare_digest(hashlib.sha256(key.strip().encode()).hexdigest(),EXPECTED)

def pool():
    path=ROOT/'local_pool_data.json'
    return json.loads(path.read_text(encoding='utf-8-sig'))

def summary(data):
    return {'accounts':len(data.get('accounts',[])),'proxies':len(data.get('proxies',[]))}

def install():
    from client.app.bridge import Bridge
    if getattr(Bridge,'_local_activation_v61',False):return
    previous_init=Bridge.__init__

    def bind(self):
        data=pool();self._pool=data;self.lic.pool=data
        def licensed():
            try:return valid(load())
            except Exception:return False
        self.lic.is_licensed=licensed
        self.lic.startup=licensed
        self.lic.refresh_access=licensed
        self.lic._try_refresh=licensed
        self.lic._start_heartbeat=lambda:None
        self.lic._do_heartbeat=lambda:None
        self.lic.run_done=lambda *a,**k:None
        self.lic.report_account_results=lambda results,*a,**k:local_account_results.apply_results(self,results)
        self.lic.refresh_pool=lambda:pool()
        return data

    def initialize(self,*args,**kwargs):
        previous_init(self,*args,**kwargs)
        bind(self)

    def login(self,key):
        if not valid(key):return {'ok':False,'error':'Key không đúng với bản v61 này.'}
        try:
            save(key.strip())
            data=bind(self)
            return {'ok':True,'is_vip':True,'pool_summary':summary(data),'local_activation':True}
        except Exception:
            return {'ok':False,'error':'Không lưu được kích hoạt cục bộ. Kiểm tra quyền ghi dữ liệu Windows.'}

    def resume(self):
        if os.environ.get('ALC_SHOW_KEY_ON_OPEN') == '1': return {'ok':False,'needs_key':True}
        try:
            if valid(load()):
                data=bind(self)
                return {'ok':True,'is_vip':True,'pool_summary':summary(data),'local_activation':True}
        except Exception:pass
        return {'ok':False,'needs_key':True}

    def refresh(self):
        data=bind(self)
        return {'ok':True,'pool_summary':summary(data)}

    Bridge.__init__=initialize
    Bridge.login=login
    Bridge.resume_saved_activation=resume
    Bridge.refresh_pool=refresh
    from client.app.runner import Runner
    def save_cookie(self,bot,account):
        import scout_cookie_cache
        scout_cookie_cache._capture(self,bot,account)
    def local_relogin(self,bot,account,name):
        has_existing_cookie=bool((account.get('cookie') or '').strip())
        phase=getattr(self,'_timing_phase','')
        if (phase=='scout' or (phase in ('like','') and has_existing_cookie)) and not os.environ.get('ALC_FORCE_RELOGIN'):
            bot.last_login_failure='cookie_unconfirmed'
            self._log(f'[relogin] Bỏ qua nhanh {name} (API chưa xác nhận phiên); chưa đăng nhập lại, không kết luận cookie hết hạn.')
            return False
        import bot_login,scout_cookie_cache,mailtm
        password=account.get('password') or ''
        if not password:
            bot.last_login_failure='credentials'
            self._log('[relogin] Không có mật khẩu; bỏ qua tài khoản.')
            return False
        username=account.get('login_user') or account.get('username') or name
        email=account.get('email') or ''
        secret=account.get('totp_secret') or account.get('2fa') or ''
        if not secret and email and not mailtm.is_supported_mailtm_address(email):
            bot.last_login_failure='2fa'
            self._log(f'[relogin] Bỏ qua {name}: email không hỗ trợ đọc OTP tự động và chưa có khóa 2FA TOTP.')
            return False
        req_px=getattr(bot,'_v90_requested_proxy',None) or getattr(bot,'_v90_actual_proxy',None) or {'host':'127.0.0.1','port':80}
        if getattr(bot,'driver',None) is not None and (getattr(bot,'_browser_account_count',0)>0 or getattr(bot,'_did_auto_login',False)):
            try:
                bot.close()
            except Exception:
                pass
            bot.driver=None
        ok,cookie,actual=False,'',''
        for login_try in range(2):
            if getattr(bot,'driver',None) is None:
                try:
                    bot.start_browser(headless=True,proxy=req_px)
                except Exception:
                    return False
            else:
                try:
                    bot.reset_account_session()
                except Exception:
                    pass
            bot._did_auto_login=True
            ok,cookie,actual=bot_login.auto_login_with_email_fallback(
                bot,username,email,password,secret,log_fn=self._log,
                mail_pass=account.get('mail_pass') or '')
            if ok and cookie:
                break
            if getattr(bot,'last_login_failure','')=='proxy' and login_try==0:
                self._log(f'[relogin] 🔄 Đổi proxy sạch và thử đăng nhập lại cho {name}...')
                continue
            break
        if not ok or not cookie:return False
        expected=str(account.get('username') or '').strip().lstrip('@').casefold()
        observed=str(actual or '').strip().lstrip('@').casefold()
        if observed and '@' not in observed and expected and observed != expected:
            self._log(f'[relogin] ℹ️ Cập nhật username mới từ phiên đăng nhập: @{expected} → @{observed}')
        account['cookie']=cookie
        account['status']='live'
        if actual and '@' not in str(actual):
            clean_actual = str(actual).strip().lstrip('@')
            account['username']='@'+clean_actual
            bot._expected_account_username=clean_actual.lower()
            bot.last_login_username='@'+clean_actual
        bot._api_identity_confirmed=True
        bot._needs_page_reload=True
        scout_cookie_cache.save(account,cookie)
        try:
            local_account_results.apply_results(None,{'id':account.get('id'),'username':account.get('username'),'status':'live','cookie':cookie})
        except Exception:
            pass
        self._log('[cookie] Đã lưu cookie mới trên máy, không gửi lên server.')
        return True
    Runner._try_relogin=local_relogin
    Runner._save_fresh_cookie=save_cookie
    Bridge._local_activation_v61=True

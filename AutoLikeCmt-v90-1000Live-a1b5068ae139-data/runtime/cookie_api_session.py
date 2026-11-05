"""Validate current cookie identity once per account; sync in-page m345.h.appContext without page reload."""
import hashlib
import re

_RESOLVED_SHORT_LINKS: dict[str, str] = {}
_RESOLVED_AWEME_IDS: dict[str, str] = {}

SYNC_IDENTITY_SCRIPT = r"""
const [newCsrf, newUidStr, newUname, newRegion, newWid] = arguments;
try {
  let csrf = String(newCsrf || '');
  if (!csrf && document.cookie) {
    const m = document.cookie.match(/(?:^|;\s*)tt_csrf_token=([^;]+)/);
    if (m) {
      try { csrf = decodeURIComponent(m[1]); } catch (_) { csrf = m[1]; }
    }
  }
  const uidStr = String(newUidStr || '');
  const uname = String(newUname || '').replace(/^@/, '');
  const region = String(newRegion || '').toUpperCase();
  const wid = String(newWid || '');
  window.__alc_identity = {csrf, uidStr, uname, region, wid};

  function applyToCtx(ctx) {
    if (!ctx || typeof ctx !== 'object') return;
    const id = window.__alc_identity || {};
    if (id.csrf) ctx.csrfToken = id.csrf;
    if (id.wid) ctx.wid = id.wid;
    if (id.uidStr) ctx.odinId = id.uidStr;
    else if (id.wid) ctx.odinId = id.wid;
    if (id.region) {
      ctx.region = id.region;
      ctx.priorityRegion = id.region;
    }
    if (!ctx.user || typeof ctx.user !== 'object') {
      ctx.user = {region: id.region || ctx.region || 'VN', ftcUser: false};
    } else if (id.region) {
      ctx.user.region = id.region;
    }
    if (id.uidStr) ctx.user.uid = id.uidStr;
    if (id.uname) ctx.user.uniqueId = id.uname;
  }

  const chunks = window.__LOADABLE_LOADED_CHUNKS__;
  let req = null;
  if (chunks && typeof chunks.push === 'function') {
    chunks.push([['alc_sync_' + Date.now()], {}, function(r) { req = r; }]);
  }
  const m345 = req && req.m && req.m[345146] ? req(345146) : null;
  const h = (m345 && m345.h) || window.__alc_http_client;
  if (h) {
    window.__alc_http_client = h;
    if (!h.__alc_hooked_setAppContext && typeof h.setAppContext === 'function') {
      const origSet = h.setAppContext.bind(h);
      h.setAppContext = function(e) {
        if (e && typeof e === 'object') applyToCtx(e);
        const res = origSet(e);
        if (this.appContext && typeof this.appContext === 'object') applyToCtx(this.appContext);
        return res;
      };
      h.__alc_hooked_setAppContext = true;
    }
    if (h.verify && typeof h.verify.checkVerify === 'function' && !h.verify.__alc_hooked_verify) {
      h.verify.checkVerify = async function() { return 0; };
      h.verify.__alc_hooked_verify = true;
    }
    if (h.appContext && typeof h.appContext === 'object' && h.appContext.wid) {
      applyToCtx(h.appContext);
    }
  }
  try { delete window.__alc_comment_mutations; } catch (_) {}
} catch (_) {}
"""


def _sync_page_identity(bot):
    d = getattr(bot, "driver", None)
    if d is None or not hasattr(d, "execute_script"):
        return
    csrf = str(getattr(bot, "_current_csrf_token", "") or "")
    uid_str = str(getattr(bot, "_current_uid_str", "") or "")
    uname = str(getattr(bot, "last_login_username", "") or "").lstrip("@")
    region = str(getattr(bot, "_current_region", "") or "").upper()
    salt = int(getattr(bot, "_wid_salt", 0) or 0)
    seed_str = f"wid:{uid_str}:{uname}:{csrf[:16]}:{salt}"
    wid_num = int(hashlib.sha256(seed_str.encode("utf-8")).hexdigest()[:15], 16) % (10 ** 17)
    wid = "74" + str(wid_num).zfill(17)
    try:
        d.execute_script(SYNC_IDENTITY_SCRIPT, csrf, uid_str, uname, region, wid)
    except Exception:
        pass


def identity_ok(bot):
    expected = str(getattr(bot, "prefix", "") or "").strip("[] @").lower()
    expected = str(getattr(bot, '_expected_account_username', '') or expected).lstrip('@').lower()
    expected_login = str(getattr(bot, '_expected_login_user', '') or '').strip("[] @").lower()
    actual = str(getattr(bot, "last_login_username", "") or "").lstrip("@").lower()
    if not actual:
        return False
    if re.fullmatch(r"[a-z0-9._]{2,40}", expected) and actual != expected:
        if expected_login and re.fullmatch(r"[a-z0-9._]{2,40}", expected_login) and actual == expected_login:
            bot._expected_account_username = actual
            return True
        bot.log("[session] Username phiên không khớp acc; không gửi API bằng phiên này.")
        return False
    return True


def install():
    import tiktok_reply as tt
    cls = tt.TikTokLikeBot
    if getattr(cls, '_account_session_isolation_v91', False):
        return
    orig_extract = getattr(tt, 'extract_aweme_id', None)

    if callable(orig_extract):
        def extract_aweme_id_cached(url: str):
            res = orig_extract(url)
            if res:
                return res
            return _RESOLVED_AWEME_IDS.get(str(url or "").strip())

        tt.extract_aweme_id = extract_aweme_id_cached

    orig_inject = cls.inject_cookies
    orig_check_live = cls.check_live_api
    login = cls.login_with_cookies
    reset = cls.reset_account_session
    api = cls.api_digg_comments
    open_link = cls.open_link

    def _clean_and_extract_cookies(self, cookies):
        self._api_identity_confirmed = False
        self._current_csrf_token = ""
        self._current_region = ""
        self._wid_salt = 0
        if not isinstance(cookies, (list, tuple)):
            return cookies
        by_name = {}
        seed_parts = []
        for c in cookies:
            if not isinstance(c, dict):
                continue
            n = str(c.get("name") or "").strip()
            v = str(c.get("value") or "").strip()
            if n == "tt_csrf_token" and v:
                self._current_csrf_token = v
            elif n == "store-country-code" and v:
                self._current_region = v.upper()
            if n in ("sessionid", "sid_tt", "uid_tt") and v:
                seed_parts.append(f"{n}={v}")
            if not n or n in ("_waftokenid", "perf_feed_cache") or n.startswith("_waf"):
                continue
            by_name[n] = c
        if "odin_tt" not in by_name and seed_parts:
            odin_val = hashlib.sha512(("odin_tt:" + ";".join(seed_parts)).encode("utf-8")).hexdigest()
            by_name["odin_tt"] = {
                "name": "odin_tt",
                "value": odin_val,
                "domain": ".tiktok.com",
                "path": "/",
                "secure": True,
                "httpOnly": False,
            }
        return list(by_name.values())

    def inject(self, cookies):
        cookies = _clean_and_extract_cookies(self, cookies)
        return orig_inject(self, cookies)

    def check_live(self, *args, **kwargs):
        ok, uname = orig_check_live(self, *args, **kwargs)
        if ok:
            self.last_login_username = uname or ""
            self._api_identity_confirmed = bool(identity_ok(self))
            if self._api_identity_confirmed:
                _sync_page_identity(self)
        else:
            self._api_identity_confirmed = False
        return ok, uname

    def navigate(self, link, *args, **kwargs):
        raw_link = str(link or '').strip()
        target_link = _RESOLVED_SHORT_LINKS.get(raw_link, raw_link)
        self._last_target_link = target_link
        d = getattr(self, 'driver', None)
        is_scout = callable(getattr(self, '_scout_publish', None))
        need_reload = bool(getattr(self, '_needs_page_reload', False))
        reused_cnt = getattr(self, '_reused_page_count', 0)
        if (
            d is not None
            and not is_scout
            and not getattr(self, 'last_proxy_blocked', False)
            and not need_reload
            and reused_cnt < 18
        ):
            try:
                cur_url = str(getattr(d, 'current_url', '') or '')
                cur_id = orig_extract(cur_url) if callable(orig_extract) else None
                target_id = tt.extract_aweme_id(target_link)
                if cur_id and target_id and str(cur_id) == str(target_id):
                    self._reused_page_count = reused_cnt + 1
                    _sync_page_identity(self)
                    if callable(getattr(self, 'is_login_wall', None)) and self.is_login_wall():
                        self.close_login_modal()
                    return
            except Exception:
                pass
        self._needs_page_reload = False
        self._reused_page_count = 0
        try:
            if d is not None:
                d.execute_script('delete window.__alc_comment_mutations; delete window.__alc_http_client;')
        except Exception:
            pass
        result = open_link(self, target_link, *args, **kwargs)
        _sync_page_identity(self)
        try:
            cur = str(getattr(self.driver, 'current_url', '') or '') if getattr(self, 'driver', None) else ''
            resolved_id = orig_extract(cur) if callable(orig_extract) else None
            if resolved_id and raw_link:
                _RESOLVED_AWEME_IDS[raw_link] = resolved_id
                clean_url = cur.split('?')[0]
                if 'tiktok.com' in clean_url and 'robots.txt' not in clean_url:
                    _RESOLVED_SHORT_LINKS[raw_link] = clean_url
                    self._last_target_link = clean_url
        except Exception:
            pass
        return result

    def cookie(self, cookies):
        cookies = _clean_and_extract_cookies(self, cookies)
        ok = login(self, cookies)
        self._api_identity_confirmed = bool(ok and identity_ok(self))
        if self._api_identity_confirmed:
            _sync_page_identity(self)
        return self._api_identity_confirmed

    def clear(self, *a, **k):
        self._api_identity_confirmed = False
        self._expected_account_username = ''
        self._current_csrf_token = ''
        self._current_uid_str = ''
        self._current_region = ''
        self._wid_salt = 0
        cnt = getattr(self, '_browser_account_count', 0)
        if (
            getattr(self, '_consecutive_invalid_status', 0) >= 1
            or getattr(self, '_consecutive_zero_accounts', 0) >= 2
            or cnt >= 20
        ):
            self._consecutive_invalid_status = 0
            self._consecutive_zero_accounts = 0
            self._browser_account_count = 0
            self._reused_page_count = 0
            self._needs_page_reload = False
            try:
                self.close()
            except Exception:
                pass
            self.driver = None
            return False
        return reset(self, *a, **k)

    def send(self, aweme_id, comments, digg_type=1, concurrency=8):
        self._check_stop()
        if not getattr(self, "_api_identity_confirmed", False):
            ok, name = self.check_live_api()
            self.last_login_username = name or ""
            self._api_identity_confirmed = bool(ok and identity_ok(self))
        if not self._api_identity_confirmed:
            return [(False, -1, "Chưa xác nhận phiên đúng acc; chưa gửi API") for _ in comments]
        self._browser_account_count = getattr(self, '_browser_account_count', 0) + 1
        _sync_page_identity(self)
        return api(self, aweme_id, comments, digg_type=digg_type, concurrency=concurrency)

    cls.inject_cookies = inject
    cls.check_live_api = check_live
    cls.open_link = navigate
    cls.login_with_cookies = cookie
    cls.reset_account_session = clear
    cls.api_digg_comments = send
    cls._account_session_isolation_v91 = True

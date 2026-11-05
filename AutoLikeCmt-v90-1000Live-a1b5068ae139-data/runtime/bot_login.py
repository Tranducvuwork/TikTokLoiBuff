# -*- coding: utf-8 -*-
"""Auto re-login TikTok bằng user/pass + TOTP — dùng chung cho client & admin.

Luồng: login user/pass → solve captcha (llm/omo qua handle_captcha của bot) →
tự sinh mã 2FA từ seed TOTP (pyotp) → chờ login xong → harvest cookie mới.

Trả (ok, cookie_str, username). KHÔNG tự đóng browser — caller quyết định
(re-login xong thường tiếp tục chạy trên chính browser đó).
"""
import time

LOGIN_URL = "https://www.tiktok.com/login/phone-or-email/email"

_UID_SEL = (
    "input[name='username'], "
    "input[placeholder*='Email' i], "
    "input[placeholder*='TikTok ID' i], "
    "input[placeholder*='email' i], "
    "input[placeholder*='username' i], "
    "input[autocomplete='webauthn'], "
    "input[autocomplete='username']"
)
_PW_SEL = (
    "input[type='password'], input[name='password'], "
    "input[placeholder*='Mật khẩu' i], input[placeholder*='Password' i]"
)
_2FA_SEL = (
    "input[autocomplete='one-time-code'], "
    "input[placeholder*='6'], "
    "input[placeholder*='chữ số'], "
    "input[placeholder*='digit' i], "
    "[class*='DivCodeInputContainer'] input, "
    "[class*='DivLoginContainer'] input[type='text'], "
    "[id='loginContainer'] input[type='text'], "
    "input[inputmode='numeric'], "
    "input[type='tel']"
)
_ERR_SEL = ("[data-e2e='login-error-msg'], [class*='LoginError'], "
            "[class*='login-error'], [type='error'][aria-live='assertive']")
_LOGGED_IN_SEL = "[data-e2e='profile-icon']"
_POST_OTP_TRANSITION_TIMEOUT = 20.0
_MAX_EMAIL_OTP_ATTEMPTS = 2


def _totp_key(secret: str) -> bytes:
    """Read a Base32 seed or an authenticator URI without changing saved data."""
    import base64
    import re
    from urllib.parse import urlsplit, parse_qs, unquote
    s = (secret or "").strip()
    if s.lower().startswith("otpauth://"):
        uri = urlsplit(s)
        if uri.netloc.lower() != "totp":
            raise ValueError("Unsupported OTP type")
        s = parse_qs(uri.query).get("secret", [""])[0]
    else:
        s = unquote(s)
        s = re.sub(r"^(?:TOTP|OTP|SECRET)\s*[:=]\s*", "", s, flags=re.I)
    s = re.sub(r"[\s-]+", "", s).upper().rstrip("=")
    if not s or not re.fullmatch(r"[A-Z2-7]+", s):
        raise ValueError("Invalid Base32 seed")
    key = base64.b32decode(s + "=" * (-len(s) % 8), casefold=True)
    if not key:
        raise ValueError("Empty OTP seed")
    return key


def _totp_now(secret: str, log, stop_event=None) -> str:
    """TOTP 6 số; chờ cửa sổ mới nhưng vẫn thoát ngay khi force stop."""
    try:
        key = _totp_key(secret)
    except (ValueError, TypeError):
        log("[relogin] ❌ Khóa 2FA sai định dạng Base32. Kiểm tra khóa bí mật "
            "hoặc URI otpauth; không nhập mã OTP 6 số vào ô khóa. "
            "Giữ account/cookie và dừng, không đổi proxy.")
        return ""

    interval = 30
    digits = 6
    remaining = interval - (int(time.time()) % interval)
    if remaining < 8:
        log(f"[relogin] TOTP còn {remaining}s — chờ cửa sổ mới...")
        wait = remaining + 1.2
        if stop_event is not None:
            if stop_event.wait(wait):
                return ""
        else:
            time.sleep(wait)

    import hmac, hashlib, struct
    counter = int(time.time()) // interval
    msg = struct.pack(">Q", counter)
    h = hmac.new(key, msg, hashlib.sha1).digest()
    offset = h[-1] & 0x0F
    binary = struct.unpack(">I", h[offset:offset + 4])[0] & 0x7FFFFFFF
    code = str(binary % (10 ** digits)).zfill(digits)

    left = interval - (int(time.time()) % interval)
    log(f"[relogin] Đã sinh mã TOTP (còn {left}s trong cửa 30s)")
    return code


def _find_login_input(d, selector):
    """Read actual rendered inputs, without Selenium's visibility atom."""
    return d.execute_script("""
        return Array.from(document.querySelectorAll(arguments[0])).find(el => {
            const r = el.getBoundingClientRect();
            const s = getComputedStyle(el);
            return !el.disabled && !el.readOnly && r.width > 0 && r.height > 0
                && s.display !== 'none' && s.visibility !== 'hidden';
        }) || null;
    """, selector)


def _type_when_ready(d, selector, text, timeout, log, field) -> bool:
    """Focus + CDP insertText — React login không nhận JS value=."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    try:
        el = WebDriverWait(d, timeout, poll_frequency=0.2).until(
            lambda dr: _find_login_input(dr, selector))
    except Exception as error:
        log(f"[relogin] ⚠️ Chưa lấy được ô {field} sau {timeout}s ({type(error).__name__})")
        return False
    if el is None:
        log(f"[relogin] ⚠️ Không thấy ô {field} sau {timeout}s")
        return False
    digits = field == "2FA"
    for attempt in range(3):
        try:
            el = _find_login_input(d, selector)
            if el is None:
                return False
            d.execute_script("arguments[0].focus(); arguments[0].click();", el)
            if digits:
                d.execute_script(
                    "var el=arguments[0],val=arguments[1];"
                    "var set=Object.getOwnPropertyDescriptor("
                    "window.HTMLInputElement.prototype,'value').set;"
                    "el.focus(); el.click();"
                    "set.call(el,'');"
                    "el.dispatchEvent(new Event('input',{bubbles:true}));"
                    "set.call(el,val);"
                    "el.dispatchEvent(new InputEvent('input',{bubbles:true,data:val,inputType:'insertText'}));"
                    "el.dispatchEvent(new Event('change',{bubbles:true}));",
                    el, str(text))
                time.sleep(0.1)
                if d.execute_script("return arguments[0].value || '';", el) != str(text):
                    for ch in str(text):
                        d.execute_cdp_cmd("Input.dispatchKeyEvent", {
                            "type": "char", "text": ch, "unmodifiedText": ch,
                            "key": ch})
                        time.sleep(0.05)
            elif attempt != 1:
                from selenium.webdriver.common.keys import Keys
                el.click()
                el.send_keys(Keys.CONTROL, 'a')
                el.send_keys(Keys.BACKSPACE)
                el.send_keys(text)
            else:
                d.execute_cdp_cmd("Input.dispatchKeyEvent", {
                    "type": "keyDown", "key": "a", "code": "KeyA",
                    "windowsVirtualKeyCode": 65, "modifiers": 2})
                d.execute_cdp_cmd("Input.dispatchKeyEvent", {
                    "type": "keyUp", "key": "a", "code": "KeyA",
                    "windowsVirtualKeyCode": 65, "modifiers": 2})
                d.execute_cdp_cmd("Input.insertText", {"text": text})
            time.sleep(0.15)
            el = _find_login_input(d, selector) or el
            got = d.execute_script("return arguments[0].value || '';", el)
            if got == text or (digits and len(got) >= 6):
                return True
            el = _find_login_input(d, selector) or el
        except Exception:
            pass
        time.sleep(0.2)
    log(f"[relogin] ⚠️ Ô {field} không nhận giá trị (React).")
    return False

def _submit(d, log) -> bool:
    """Nút Đăng nhập (user/pass) — KHÔNG dùng cho bước 2FA."""
    from selenium.webdriver.support.ui import WebDriverWait
    try:
        WebDriverWait(d, 6).until(lambda dr: dr.execute_script("""
            var b = document.querySelector("[data-e2e='login-button']")
                 || document.querySelector("button[type='submit']");
            if (!b) return false;
            var u = document.querySelector("input[name='username']");
            var p = document.querySelector("input[type='password']");
            if (u && p && u.value && p.value) {
              return !b.disabled && b.getAttribute('aria-disabled') !== 'true';
            }
            return false;
        """))
        clicked = d.execute_script("""
            var b = document.querySelector("[data-e2e='login-button']")
                 || document.querySelector("button[type='submit']");
            if (!b || b.disabled || b.getAttribute('aria-disabled') === 'true') return false;
            b.click();
            return true;
        """)
        return bool(clicked)
    except Exception:
        log("[relogin] ⚠️ Nút Đăng nhập không bật.")
        return False


def _2fa_state(d) -> dict:
    """Moi bien the 2FA: 1 o 6 so, 6 o 1 so, loginContainer, tel, email OTP."""
    return d.execute_script(r"""
        const out = {kind:'', inputs:[], btn:null, hint:''};
        const blob = ((document.body&&document.body.innerText)||'') + ' ' +
                     ((document.documentElement&&document.documentElement.innerHTML)||'');
        const t = blob.toLowerCase();
        const hintHit = /xác minh 2|2 bước|two[- ]step|authenticator|one[- ]time|verification code|mã gồm 6|6 chữ số|6-digit|nhập mã|enter code|sms code|email code/.test(t);
        const skip = (el) => {
          if (!el || el.disabled) return true;
          const ty = (el.type||'').toLowerCase();
          if (ty === 'password' || ty === 'hidden' || ty === 'checkbox' || ty === 'submit') return true;
          const n = (el.name||'')+(el.id||'')+(el.autocomplete||'')+(el.placeholder||'');
          if (/user|email|pass|search|captcha/i.test(n) && !/code|otp|digit|token|verify/i.test(n)) return true;
          const r = el.getBoundingClientRect();
          if (r.width < 4 || r.height < 4) return true;
          return false;
        };
        const nodes = [...document.querySelectorAll(
          "input, [contenteditable='true'], [role='textbox']")];
        const cands = nodes.filter(el => !skip(el));
        const otpish = cands.filter(el => {
          const meta = ((el.autocomplete||'')+' '+(el.placeholder||'')+' '+
            (el.getAttribute('aria-label')||'')+' '+(el.className||'')+' '+
            (el.maxLength||'')+' '+(el.inputMode||'')+' '+(el.type||'')).toLowerCase();
          return /one-time|otp|digit|code|token|verify|tel|numeric|6/.test(meta)
              || el.maxLength === 1 || el.maxLength === 6 || el.maxLength === 8;
        });
        const six = cands.filter(el => el.maxLength === 1 ||
          ((el.placeholder||'').length <= 1 && (el.inputMode||'').toLowerCase()==='numeric'));
        let pick = [];
        if (otpish.length === 6 || six.length >= 4) {
          pick = (otpish.length === 6 ? otpish : six).slice(0, 6);
          out.kind = 'slots';
        } else if (otpish.length) {
          pick = [otpish[0]];
          out.kind = 'single';
        } else if (hintHit && cands.length) {
          const vis = cands.filter(el => {
            const r = el.getBoundingClientRect();
            return r.width > 20 && r.height > 10;
          });
          pick = vis.length ? [vis[0]] : [cands[0]];
          out.kind = 'fallback';
        }
        out.inputs = pick;
        out.hint = (hintHit ? 'hint' : '') + (out.kind ? ':'+out.kind : '');
        const btns = [...document.querySelectorAll(
          "button[type='submit'], button[class*='TwoStep'], button[class*='StyledTwo'], #loginContainer button, [class*='DivLoginContainer'] button, form button")];
        out.btn = btns.find(b => {
          const lab = ((b.innerText||'')+' '+(b.getAttribute('aria-label')||'')).toLowerCase();
          return /tiếp|next|continue|verify|xác nhận|submit|confirm/.test(lab) || b.type==='submit';
        }) || btns[0] || null;
        return {kind: out.kind, hint: out.hint,
                n: pick.length,
                hasBtn: !!out.btn};
    """) or {}


def _2fa_inputs(d) -> list:
    st = _2fa_state(d)
    n = int(st.get("n") or 0)
    if n <= 0:
        return []
    els = d.execute_script(r"""
        const skip = (el) => {
          if (!el || el.disabled) return true;
          const ty = (el.type||'').toLowerCase();
          if (ty === 'password' || ty === 'hidden' || ty === 'checkbox' || ty === 'submit') return true;
          const n = (el.name||'')+(el.id||'')+(el.autocomplete||'')+(el.placeholder||'');
          if (/user|email|pass|search|captcha/i.test(n) && !/code|otp|digit|token|verify/i.test(n)) return true;
          const r = el.getBoundingClientRect();
          return r.width < 4 || r.height < 4;
        };
        const nodes = [...document.querySelectorAll("input, [contenteditable='true']")];
        const cands = nodes.filter(el => !skip(el));
        const otpish = cands.filter(el => {
          const meta = ((el.autocomplete||'')+' '+(el.placeholder||'')+' '+
            (el.getAttribute('aria-label')||'')+' '+(el.className||'')+' '+
            (el.maxLength||'')+' '+(el.inputMode||'')+' '+(el.type||'')).toLowerCase();
          return /one-time|otp|digit|code|token|verify|tel|numeric|6|chữ số/.test(meta)
              || el.maxLength === 1 || el.maxLength === 6;
        });
        const six = cands.filter(el => el.maxLength === 1);
        if (otpish.length === 6 || six.length >= 4) return (otpish.length===6?otpish:six).slice(0,6);
        if (otpish.length) return [otpish[0]];
        const blob = ((document.body&&document.body.innerText)||'').toLowerCase();
        if (/xác minh 2|2 bước|authenticator|mã gồm 6|6 chữ số|verification code/.test(blob) && cands.length)
          return [cands[0]];
        return [];
    """)
    return els or []


def _fill_otp(d, code: str, log) -> bool:
    """1 o hoac 6 o. Native setter + insertText + send_keys."""
    from selenium.webdriver.common.keys import Keys
    code = "".join(c for c in str(code) if c.isdigit())[:6]
    if len(code) != 6:
        log("[relogin] ⚠️ Mã TOTP không đủ 6 số.")
        return False
    els = _2fa_inputs(d)
    if not els:
        log("[relogin] ⚠️ Không thấy ô OTP (mọi biến thể).")
        return False
    kind = "slots" if len(els) >= 4 else "single"
    log(f"[relogin] 2FA form={kind} n={len(els)}")
    try:
        d.execute_script("""
            const els = arguments[0], code = arguments[1];
            function setVal(el, val) {
              el.focus(); el.click();
              const proto = Object.getOwnPropertyDescriptor(
                  window.HTMLInputElement.prototype, 'value');
              if (proto && proto.set) proto.set.call(el, val);
              else el.value = val;
              el.dispatchEvent(new Event('input', {bubbles:true}));
              el.dispatchEvent(new InputEvent('input', {
                  bubbles:true, data:val, inputType:'insertText'}));
              el.dispatchEvent(new Event('change', {bubbles:true}));
            }
            if (els.length >= 4) {
              for (let i = 0; i < Math.min(els.length, code.length); i++)
                setVal(els[i], code[i]);
            } else {
              setVal(els[0], code);
            }
            return true;
        """, els, code)
    except Exception as e:
        log(f"[relogin] setVal: {e}")
    got = ""
    try:
        if kind == "slots":
            got = "".join((e.get_attribute("value") or "") for e in els)
        else:
            got = els[0].get_attribute("value") or ""
    except Exception:
        pass
    got = "".join(c for c in got if c.isdigit())
    if got != code:
        try:
            els[0].click()
            d.execute_cdp_cmd("Input.insertText", {"text": code if kind == "single" else ""})
            if kind == "slots":
                for i, ch in enumerate(code):
                    if i >= len(els):
                        break
                    try:
                        els[i].click()
                        d.execute_cdp_cmd("Input.insertText", {"text": ch})
                    except Exception:
                        pass
        except Exception:
            pass
        try:
            if kind == "slots":
                got = "".join((e.get_attribute("value") or "") for e in els)
            else:
                got = els[0].get_attribute("value") or ""
        except Exception:
            pass
        got = "".join(c for c in got if c.isdigit())
    if got != code:
        try:
            els[0].send_keys(Keys.CONTROL, "a")
            els[0].send_keys(code)
            got = "".join(c for c in (els[0].get_attribute("value") or "") if c.isdigit())
            if kind == "slots" and len(got) < 6:
                got = "".join((e.get_attribute("value") or "") for e in els)
                got = "".join(c for c in got if c.isdigit())
        except Exception:
            pass
    ok = got == code or (kind == "slots" and len(got) >= 6)
    log(f"[relogin] Kiểm tra ô OTP: {'OK' if ok else 'SAI/NHÂN'}")
    return ok


def _submit_2fa(d, log) -> bool:
    """Bấm đúng một lần nút xác minh; tránh gửi cùng OTP hai request."""
    result = d.execute_script("""
        const inputs = [...document.querySelectorAll('input')].filter(el => {
          const ty = (el.type||'').toLowerCase();
          return ty !== 'password' && ty !== 'hidden' && ty !== 'checkbox';
        });
        let digits = '';
        for (const el of inputs) digits += (el.value||'').replace(/\\D/g,'');
        digits = digits.slice(0, 8);
        const btns = [...document.querySelectorAll(
          "button[type='submit'], button, [role='button']")];
        const b = btns.find(x => {
          const lab = ((x.innerText||'')+' '+(x.getAttribute('aria-label')||'')).toLowerCase();
          return /tiếp|next|continue|verify|xác nhận|confirm|submit/.test(lab);
        }) || document.querySelector("form button[type='submit']")
           || document.querySelector("#loginContainer button")
           || btns.find(x => x.type === 'submit');
        if (!b) return 'no-btn';
        if (digits.length < 6) return 'badlen:'+digits;
        b.disabled = false;
        b.removeAttribute('disabled');
        try {
          b.dispatchEvent(new MouseEvent('mousedown', {bubbles:true}));
          b.dispatchEvent(new MouseEvent('mouseup', {bubbles:true}));
          b.click();
          return 'ok';
        } catch (clickError) {
          const form = b.closest('form');
          if (!form || !form.requestSubmit) return 'click-failed';
          try {
            form.requestSubmit(b);
            return 'ok';
          } catch (submitError) {
            return 'submit-failed';
          }
        }
    """)
    log(f"[relogin] Submit 2FA → {result}")
    return result == "ok"


def _click_send_email_code(d, log) -> str:
    """Bấm một bước 2FA: ưu tiên Gửi mã / Gửi lại mã, sau đó chọn Email."""
    try:
        return d.execute_script(r"""
            if (location.pathname.includes('/login/phone-or-email') && !document.querySelector('.idv-container')) {
              return 'wait';
            }
            const bodyText = ((document.body && document.body.innerText) || '').toLowerCase();
            if (/gửi lại mã\s*\(?\d+\s*s|resend code\s*\(?\d+\s*s/i.test(bodyText)) {
              return 'sent';
            }
            const textOf = (el) => ((el.innerText||el.textContent||'') + ' ' +
              (el.getAttribute('aria-label')||'') + ' ' + (el.getAttribute('title')||'')).trim();
            const vis = (el) => {
              if (!el) return false;
              const r = el.getBoundingClientRect();
              const s = getComputedStyle(el);
              return r.width > 8 && r.height > 8 && s.display !== 'none' && s.visibility !== 'hidden';
            };
            const nodes = [...document.querySelectorAll(
              "button, a, [role='button'], [role='radio'], [role='option'], label, div, span, p"
            )].filter(vis);
            function clickLab(re, disallowCountdown) {
              const el = nodes.find(x => {
                if (x.disabled || x.getAttribute('aria-disabled') === 'true') return false;
                const t = textOf(x).toLowerCase();
                if (/email hoặc tiktok id|email or username/i.test(t)) return false;
                if (disallowCountdown && /\b\d+\s*s\b/.test(t)) return false;
                return re.test(t) && t.length < 80;
              });
              if (!el) return false;
              try {
                el.scrollIntoView({block:'center'});
                el.dispatchEvent(new MouseEvent('mousedown', {bubbles:true}));
                el.dispatchEvent(new MouseEvent('mouseup', {bubbles:true}));
                el.click();
              } catch(e) { return false; }
              return true;
            }
            // Màn IDV “Xác minh đó là bạn”: listener nằm trên cả hàng,
            // không nằm ở div chữ Email bên trong.
            const idvItems = [...document.querySelectorAll(
              ".idv-container [class*='pc-home-item-']"
            )].filter(el => vis(el) && /(^|\s)email(\s|$)|thư điện tử/i.test(textOf(el)));
            const idvRow = idvItems.find(el =>
              /pc-home-item-/i.test(el.className || '') &&
              !/content|icon|desc|arrow/i.test(el.className || ''));
            // Prefer clicking an active Send/Resend code button first before assuming already sent.
            if (clickLab(/gửi mã|gửi lại|resend|send code|get code|nhận mã|gửi tới/, true)) return 'sent';
            if (idvRow) {
              try {
                idvRow.scrollIntoView({block:'center'});
                idvRow.dispatchEvent(new MouseEvent('mousedown', {bubbles:true}));
                idvRow.dispatchEvent(new MouseEvent('mouseup', {bubbles:true}));
                idvRow.click();
                return 'method';
              } catch(e) {}
            }
            if (clickLab(/qua email|gửi.*email|send.*email|email.*code|thư điện tử/, false)) return 'method';
            if (clickLab(/xác minh bằng cách khác|verify another way|try another way|phương thức khác/, false)) return 'method';
            if (/nhập mã được gửi đến|enter the code sent to/i.test(bodyText)) {
              return 'sent';
            }
            return 'none';
        """) or "none"
    except Exception as e:
        log(f"[relogin] ⚠️ Click gửi mã email: {str(e)[:80]}")
        return "err"



def _dismiss_app_gate(d, log):
    """Popup Mở TikTok + overlay TUX (z-index 3001) nuốt click."""
    try:
        hid = d.execute_script("""
            var n = 0;
            document.querySelectorAll(
              "[data-e2e='instagram-modal'], [data-e2e='instagram-modal-close'], "
              + "[class*='DivPopup'], [class*='DivMask'], "
              + "[data-testid*='tux-web-canary'][data-testid$='modal-overlay'], "
              + "[class*='tux-modal__overlay']"
            ).forEach(function(el) {
              el.style.setProperty('display', 'none', 'important');
              el.style.setProperty('pointer-events', 'none', 'important');
              n++;
            });
            var c = document.querySelector("[data-e2e='instagram-modal-close']");
            if (c) { try { c.click(); } catch (e) {} }
            return n;
        """)
        if hid:
            log(f"[relogin] Ẩn popup/overlay ({hid} node).")
    except Exception:
        pass


_TIKTOK_SERVER_ERR = (
    "máy chủ nội bộ", "sự cố nhỏ với máy chủ", "lỗi máy chủ", "internal server",
    "thử lại sau", "try again later", "đạt số lần thử tối đa", "maximum number of attempts",
    "too many", "quá nhiều", "quá thường xuyên", "too frequently",
)
_TIKTOK_CREDENTIAL_ERR = (
    "người dùng không tồn tại", "user doesn't exist", "user does not exist",
    "không tìm thấy tài khoản", "couldn't find this account",
    "mật khẩu không đúng", "incorrect password", "wrong password",
)


def _login_failure_kind(message: str) -> str:
    text = (message or "").casefold()
    if any(phrase in text for phrase in _TIKTOK_SERVER_ERR):
        return "proxy"
    if any(phrase in text for phrase in _TIKTOK_CREDENTIAL_ERR):
        return "credentials"
    return "technical"

# Acc bị ĐÌNH CHỈ vĩnh viễn — 100% die, không retry, không xoay proxy.
_TIKTOK_BAN_PHRASES = ("đã bị đình chỉ", "đình chỉ", "suspended", "banned",
                       "vô hiệu hóa", "deactivated")


def _is_banned(d) -> bool:
    """Trang hiện text 'Tài khoản của bạn đã bị đình chỉ' (thường ngay sau
    bước 2FA) → acc die vĩnh viễn."""
    from selenium.webdriver.common.by import By
    try:
        text = (d.find_element(By.TAG_NAME, "body").text or "").lower()
        return any(p in text for p in _TIKTOK_BAN_PHRASES)
    except Exception:
        return False


def _page_error_text(d) -> str:
    """Text lỗi TikTok hiển thị trên trang (không qua selector lỗi chuẩn).

    Lỗi -202 'Lỗi máy chủ nội bộ' TikTok render bằng i18n text thường —
    phải quét body text, selector login-error không bắt được."""
    from selenium.webdriver.common.by import By
    try:
        text = (d.find_element(By.TAG_NAME, "body").text or "").lower()
        for phrase in _TIKTOK_SERVER_ERR:
            if phrase in text:
                return phrase
    except Exception:
        pass
    return ""


def _logged_in(bot) -> bool:
    """Yêu cầu cookie session và bằng chứng đăng nhập hiện trên trang."""
    from selenium.webdriver.common.by import By
    try:
        cookies = bot.driver.get_cookies()
        if not any(c.get("name") in ("sessionid", "sessionid_ss", "sid_tt")
                   and c.get("value") for c in cookies):
            return False
        if any(e.is_displayed() for e in
               bot.driver.find_elements(By.CSS_SELECTOR, _LOGGED_IN_SEL)):
            return True
        return bool(bot.get_username())
    except Exception:
        return False


_STRIP_COOKIE_NAMES = frozenset({
    "_waftokenid",
    "perf_feed_cache",
    "tt_ticket_guard_has_set_public_key",
    "csrf_session_id",
})


def harvest_cookie(bot) -> str:
    """Đọc cookie từ browser → chuỗi 'name=value; ...' (định dạng parse_cookies)."""
    try:
        by_name = {}
        for c in bot.driver.get_cookies():
            name = str(c.get("name") or "").strip()
            if not name or name in _STRIP_COOKIE_NAMES or name.startswith("_waf"):
                continue
            by_name[name] = f"{name}={c.get('value', '')}"
        return "; ".join(by_name.values())
    except Exception:
        return ""


def _login_advanced(bot):
    try:
        return _logged_in(bot) or bool(_2fa_inputs(bot.driver))
    except Exception:
        return False


def _credentials_or_next_step(bot, username, password, log):
    """Type username and password on the login form, then submit."""
    d = bot.driver
    def advanced():
        return _login_advanced(bot)
    for selector, text, timeout, field in (
            (_UID_SEL, username, 15, 'username'),
            (_PW_SEL, password, 8, 'password')):
        if not _type_when_ready(d, selector, text, timeout, log, field):
            return advanced()
    if advanced():
        return True
    submitted = _submit(d, log)
    return submitted or advanced()


def _choose_totp_method(d):
    try:
        return bool(d.execute_script(r"""
            const pattern = /authenticator|authentication app|ứng dụng xác thực|ứng dụng tạo mã/i;
            const nodes = [...document.querySelectorAll(
                "button, [role='button'], [role='radio'], .idv-container [class*='pc-home-item-']")];
            const target = nodes.find(el => {
                const r = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                const text = (el.innerText || el.textContent || '').trim();
                return r.width > 8 && r.height > 8 && style.visibility !== 'hidden'
                    && style.display !== 'none' && text.length < 160 && pattern.test(text);
            });
            if (!target) return false;
            target.scrollIntoView({block: 'center'});
            target.click();
            return true;
        """))
    except Exception:
        return False


def auto_login(bot, username: str, password: str, totp_secret: str,
               log_fn=None, mail_address: str = "", mail_pass: str = "",
               retry_server_error_in_place: bool = True) -> tuple[bool, str, str]:
    """Login. 2FA = TOTP seed hoặc OTP Mail.tm (mail_address)."""
    from selenium.webdriver.common.by import By
    import tiktok_reply
    import uuid
    flow_id = uuid.uuid4().hex[:8]
    sink = log_fn or (lambda _m: None)
    log = lambda message: sink(f'[flow:{flow_id}] {message}')
    stop_event = getattr(bot, "stop_event", None)
    from mail_credentials import resolve
    original_seed = totp_secret
    totp_secret, mail_pass = resolve(mail_address, totp_secret, mail_pass)
    if original_seed != totp_secret:
        log("[relogin] Khôi phục mật khẩu Mail.tm từ dữ liệu 4 cột; lấy OTP email, không dùng seed TOTP.")

    def _abort():
        bot.last_login_failure = "stopped"
        log("[relogin] ⏹️ Đã dừng đăng nhập.")
        return False, "", ""

    def _wait(seconds: float) -> bool:
        if stop_event is not None:
            return stop_event.wait(seconds)
        time.sleep(seconds)
        return False

    if stop_event is not None and stop_event.is_set():
        return _abort()
    bot.last_login_failure = ""
    if totp_secret:
        try:
            _totp_key(totp_secret)
        except Exception:
            bot.last_login_failure = "2fa"
            log("[relogin] Khóa 2FA sai định dạng; bỏ sớm bước login để giải phóng Chrome pool.")
            return False, "", ""
    d = getattr(bot, "driver", None)
    if not d:
        bot.last_login_failure = "technical"
        return False, "", ""

    def _dump(reason):
        """Fail → dump URL/title/text + screenshot vào logs/login_fail/."""
        try:
            bot._dump_login_fail(reason, folder="login_fail")
        except Exception:
            pass

    def _rotate_flagged_login_proxy():
        actual_px = getattr(bot, "_v90_actual_proxy", None)
        if actual_px:
            try:
                tiktok_reply.blacklist_proxy(actual_px, ttl=90)
            except Exception:
                pass
        try:
            bot.close()
        except Exception:
            pass
        bot.driver = None

    try:
        bot.pause_network_blocks(True)
    except Exception:
        pass
    try:
        d.execute_cdp_cmd("Network.clearBrowserCookies", {})
        d.execute_cdp_cmd("Storage.clearDataForOrigin", {
            "origin": "https://www.tiktok.com",
            "storageTypes": "all",
        })
    except Exception:
        pass
    ok_submit = False
    for attempt in range(3):
        if stop_event is not None and stop_event.is_set():
            return _abort()
        if attempt > 0 and _login_advanced(bot):
            ok_submit = True
            break
        try:
            d.get(LOGIN_URL)
            page_error = d.execute_script("""
                const text = (document.body?.innerText || '');
                const error = text.match(/ERR_[A-Z_]+/);
                return location.protocol === 'chrome-error:' ||
                    document.querySelector('#main-frame-error') ?
                    (error ? error[0] : 'browser_network_error') : '';
            """)
            if page_error:
                bot.last_login_failure = 'proxy'
                log(f'[relogin] Trang login lỗi mạng/proxy: {page_error}; dừng sớm, giữ tài khoản và cookie.')
                _dump('login page network error')
                try:
                    bot.close()
                except Exception:
                    pass
                return False, '', ''
        except Exception:
            bot.last_login_failure = 'proxy'
            log('[relogin] Không tải được trang login; dừng sớm, giữ tài khoản và cookie.')
            _dump('login navigation failed')
            try:
                bot.close()
            except Exception:
                pass
            return False, '', ''
        try:
            from selenium.webdriver.support.ui import WebDriverWait
            WebDriverWait(d, 10, poll_frequency=0.2).until(
                lambda dr: (stop_event is not None and stop_event.is_set()) or _find_login_input(dr, _UID_SEL))
        except Exception:
            pass
        if stop_event is not None and stop_event.is_set():
            return _abort()
        try:
            from selenium.webdriver.common.by import By as _By
            tabs = d.find_elements(_By.CSS_SELECTOR, "[data-e2e='email-tab']")
            if tabs:
                d.execute_script("arguments[0].click();", tabs[0])
                time.sleep(0.3)
        except Exception:
            pass
        if stop_event is not None and stop_event.is_set():
            return _abort()
        credentials_ready = _credentials_or_next_step(bot, username, password, log)
        if stop_event is not None and stop_event.is_set():
            return _abort()
        if credentials_ready:
            ok_submit = True
            break
        _dump('username/password form unavailable')
        if attempt < 2:
            log(f"[relogin] 🔄 Login chưa ổn định, thử lại (lần {attempt + 1}/2)...")
        if _wait(2):
            return _abort()
    if not ok_submit:
        bot.last_login_failure = "technical"
        log("[relogin] ❌ Không nhập được user/pass.")
        return False, "", ""

    # 2. Vòng chờ: captcha / chọn email / OTP / thành công / lỗi
    sent_2fa = ""
    sent_2fa_at = 0.0
    otp_attempts = 0
    email_code_requested = False
    email_code_requested_at = 0.0
    email_request_attempts = 0
    server_err_retries = 0
    login_budget = 90.0 if mail_address else 45.0
    login_deadline = time.monotonic() + login_budget
    while time.monotonic() < login_deadline:
        if _wait(0.25):
            return _abort()
        if _logged_in(bot):
            uname = ""
            for _ in range(3):
                try:
                    uname = bot.get_username() or ""
                except Exception:
                    uname = ""
                if uname:
                    break
                if _wait(1.5):
                    return _abort()
            log(f"[relogin] ✅ Login lại thành công {uname or username}.")
            return True, harvest_cookie(bot), uname or username

        if tiktok_reply.captcha_present(d):
            title = ""
            try:
                title = (d.find_element(By.CSS_SELECTOR,
                    "[class*='DivTitleContainer']").text or "")
            except Exception:
                pass
            if "xác minh 2" not in title.lower() and "2 bước" not in title.lower():
                if not bot.handle_captcha():
                    if _wait(1):
                        return _abort()
                continue

        _dismiss_app_gate(d, log)
        if _is_banned(d):
            bot.last_banned = True
            bot.last_login_failure = "banned"
            log("[relogin] ⛔ TÀI KHOẢN BỊ ĐÌNH CHỈ — BANNED, không retry.")
            return False, "", ""

        if totp_secret and not _2fa_inputs(d):
            if _choose_totp_method(d):
                log('[relogin] Đã chọn ứng dụng xác thực TOTP.')
                if _wait(1):
                    return _abort()
        # Chỉ lấy OTP email nếu account không có seed TOTP.
        if mail_address and not totp_secret and not email_code_requested and not _2fa_inputs(d):
            try:
                body = (d.execute_script(
                    "return (document.body && document.body.innerText || '').toLowerCase();"
                ) or "")
            except Exception:
                body = ""
            if any(k in body for k in (
                    "xác minh", "2 bước", "two-step", "verify", "thư điện tử", "gửi mã", "nhận mã",
                    "send code", "get code", "verification code")):
                request_started = time.time()
                sent = _click_send_email_code(d, log)
                if sent != "wait":
                    if email_request_attempts >= 3:
                        bot.last_login_failure = "2fa"
                        log("[relogin] Không chuyển được bước nhận mã email sau 3 lần thử — bỏ qua tài khoản, chuyển acc tiếp theo.")
                        _dump("email method did not advance")
                        return False, "", ""
                    email_request_attempts += 1
                    log(f"[relogin] Yêu cầu mã qua email → {sent}")
                    email_code_requested = sent == "sent"
                    if email_code_requested:
                        email_code_requested_at = request_started
                    if sent in ("sent", "method"):
                        if _wait(2.5):
                            return _abort()

        twofa = _2fa_inputs(d)
        if twofa:
            st = _2fa_state(d)
            log(f"[relogin] Trang 2FA ({st.get('hint') or st.get('kind') or 'otp'}).")
            err_txt = ""
            try:
                err_txt = (d.execute_script("""
                    return [...document.querySelectorAll(
                      "[aria-live='assertive'], [role='alert'], "
                      + "[data-e2e*='error'], [class*='Error'], [class*='error']"
                    )].filter(e => {
                      const r = e.getBoundingClientRect();
                      const s = getComputedStyle(e);
                      return r.width > 0 && r.height > 0
                        && s.display !== 'none' && s.visibility !== 'hidden';
                    }).map(e => e.innerText || e.textContent || '')
                      .filter(Boolean).join(' ');
                """) or "").lower()
            except Exception:
                pass

            ignored = []
            rejected = any(k in err_txt for k in (
                "không chính xác", "incorrect", "sai", "invalid",
                "hết hạn", "expired", "quá nhiều lần", "too many attempts"))
            if sent_2fa:
                if rejected:
                    if totp_secret or not mail_address or otp_attempts >= _MAX_EMAIL_OTP_ATTEMPTS:
                        bot.last_login_failure = "2fa"
                        log("[relogin] ❌ TikTok từ chối OTP — dừng account này.")
                        _dump("TikTok từ chối OTP")
                        return False, "", ""
                    log("[relogin] TikTok báo mã sai/hết hạn — yêu cầu OTP email mới.")
                    ignored = [sent_2fa]
                    sent_2fa = ""
                    sent_2fa_at = 0.0
                    email_code_requested = False
                elif time.monotonic() - sent_2fa_at < _POST_OTP_TRANSITION_TIMEOUT:
                    if 3.0 <= time.monotonic() - sent_2fa_at <= 6.0:
                        _submit_2fa(d, log)
                    if _wait(2):
                        return _abort()
                    continue
                else:
                    bot.last_login_failure = "2fa"
                    log("[relogin] ❌ TikTok giữ nguyên trang 2FA sau 20s — "
                        "OTP không được chấp nhận, dừng thay vì chờ lặp.")
                    _dump("trang 2FA không chuyển sau khi submit")
                    return False, "", ""

            code = ""
            if totp_secret:
                code = _totp_now(totp_secret, log, stop_event)
                if not code:
                    if stop_event is not None and stop_event.is_set():
                        return _abort()
                    bot.last_login_failure = "2fa"
                    log("[relogin] ❌ Không sinh được mã TOTP (seed sai?) → fail.")
                    return False, "", ""
            elif mail_address:
                if not email_code_requested:
                    request_started = time.time()
                    sent = _click_send_email_code(d, log)
                    if sent == "wait":
                        continue
                    cur_url = ""
                    try:
                        cur_url = d.current_url or ""
                    except Exception:
                        pass
                    if sent == "none" and "/login/2sv/totp" in cur_url:
                        bot.last_login_failure = "2fa"
                        log("[relogin] ❌ Tài khoản yêu cầu mã TOTP (Ứng dụng xác thực) nhưng không có khóa 2FA — bỏ qua.")
                        _dump("totp required without secret")
                        return False, "", ""
                    if email_request_attempts >= 3:
                        bot.last_login_failure = "2fa"
                        log("[relogin] Không chuyển được bước nhận mã email sau 3 lần thử — bỏ qua tài khoản, chuyển acc tiếp theo.")
                        _dump("email method did not advance")
                        return False, "", ""
                    email_request_attempts += 1
                    log(f"[relogin] Nút nhận mã email → {sent}")
                    email_code_requested = sent == "sent"
                    if email_code_requested:
                        email_code_requested_at = request_started
                    if sent in ("sent", "method"):
                        if _wait(2.5):
                            return _abort()
                    if not email_code_requested:
                        continue
                try:
                    import mailtm
                    log(f"[relogin] 📬 Lấy OTP Mail.tm {mail_address}...")
                    code = ""
                    for wait_slice in (22, 25, 25):
                        if tiktok_reply.captcha_present(d):
                            bot.handle_captcha()
                            _click_send_email_code(d, log)
                        code = mailtm.wait_otp(
                            mail_address, mail_pass or "MailTM@",
                            timeout=wait_slice, log=log, ignore_codes=ignored,
                            stop_event=stop_event,
                            not_before=email_code_requested_at or None)
                        if code or (stop_event is not None and stop_event.is_set()):
                            break
                        if _logged_in(bot):
                            break
                        resent = _click_send_email_code(d, log)
                        if resent in ("sent", "method"):
                            log(f"[relogin] 🔄 Yêu cầu gửi lại mã email → {resent}")
                except Exception as e:
                    if stop_event is not None and stop_event.is_set():
                        return _abort()
                    bot.last_login_failure = "mail"
                    log(f"[relogin] ❌ Mail.tm: {str(e)[:120]}")
                    return False, "", ""
                if not code:
                    bot.last_login_failure = "mail"
                    log("[relogin] ❌ Không thấy OTP mới trong Mail.tm.")
                    return False, "", ""
            else:
                bot.last_login_failure = "2fa"
                log("[relogin] ❌ Cần 2FA nhưng không có TOTP / Mail.tm.")
                return False, "", ""
            log("[relogin] 🔑 Nhập mã 2FA.")
            if not _fill_otp(d, code, log):
                bot.last_login_failure = "technical"
                log("[relogin] ❌ Không điền đủ OTP vào form.")
                return False, "", ""
            if not _submit_2fa(d, log):
                bot.last_login_failure = "technical"
                log("[relogin] ❌ Không submit được form OTP.")
                return False, "", ""
            sent_2fa = code
            sent_2fa_at = time.monotonic()
            otp_attempts += 1
            if _wait(1.5):
                return _abort()
            continue

        page_err = _page_error_text(d)
        if page_err and not twofa:
            if retry_server_error_in_place and server_err_retries < 1:
                server_err_retries += 1
                log(f"[relogin] ⏳ TikTok chặn tạm thời ('{page_err}') — "
                    f"chờ 8s thử lại (lần {server_err_retries}/1)...")
                if _wait(8):
                    return _abort()
                ok_submit = False
                for attempt in range(3):
                    if stop_event is not None and stop_event.is_set():
                        return _abort()
                    if attempt > 0 and _login_advanced(bot):
                        ok_submit = True
                        break
                    d.get(LOGIN_URL)
                    if _credentials_or_next_step(bot, username, password, log):
                        ok_submit = True
                        break
                    if _wait(2):
                        return _abort()
                if ok_submit:
                    sent_2fa = ""
                    continue
            bot.last_login_failure = "proxy"
            log("[relogin] ❌ TikTok vẫn chặn login (IP/proxy bị flag) — "
                "acc KHÔNG bị sai pass, thử lại sau hoặc đổi proxy.")
            _dump("TikTok chặn login (IP/proxy flag)")
            _rotate_flagged_login_proxy()
            return False, "", ""

        errs = [e for e in d.find_elements(By.CSS_SELECTOR, _ERR_SEL)
                if e.is_displayed()]
        if errs and not twofa:
            msg = (errs[0].text or "login error")[:80]
            bot.last_login_failure = _login_failure_kind(msg)
            log(f"[relogin] ❌ TikTok báo lỗi: {msg}")
            _dump(f"lỗi form login: {msg}")
            if bot.last_login_failure == "proxy":
                _rotate_flagged_login_proxy()
            return False, "", ""
    bot.last_login_failure = "timeout"
    log(f"[relogin] ❌ Timeout {login_budget:g}s — chuyển tài khoản tiếp theo.")
    _dump(f"timeout {login_budget:g}s")
    return False, "", ""


def auto_login_with_email_fallback(
        bot, username: str, email: str, password: str, totp_secret: str,
        log_fn=None, mail_pass: str = "",
        retry_server_error_in_place: bool = True) -> tuple[bool, str, str]:
    """Account có email luôn đăng nhập bằng email; username chỉ dùng khi không có."""
    log = log_fn or (lambda _m: None)
    username = (username or "").strip()
    email = (email or "").strip()
    if "@" in email:
        login_kind, login_id = "email", email
    elif username:
        login_kind, login_id = "username", username.lstrip("@")
    else:
        return False, "", ""

    log(f"[relogin] 🔑 Thử đăng nhập bằng {login_kind}.")
    return auto_login(
        bot, login_id, password, totp_secret,
        log_fn=log, mail_address=email, mail_pass=mail_pass,
        retry_server_error_in_place=retry_server_error_in_place)

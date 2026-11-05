"""VIP local comment-account maintenance: private account and avatar."""
from __future__ import annotations

import base64
import json
import threading
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

_ACTIVE_LOCK = threading.Lock()
_ACTIVE_BOTS = set()

_PROFILE_STEP_PAUSE_SECONDS = 1.0
_PROFILE_FILE_SETTLE_SECONDS = 1.0
_PROFILE_SAVE_SETTLE_SECONDS = 2.0
_PROFILE_SUCCESS_HOLD_SECONDS = 1.0


def stop_actions() -> None:
    """Hard-kill mọi browser hồ sơ ở thread nền để không treo UI."""
    with _ACTIVE_LOCK:
        bots = list(_ACTIVE_BOTS)
        _ACTIVE_BOTS.clear()

    def kill(bot) -> None:
        try:
            bot._hard_kill()
            bot.driver = None
        except Exception:
            pass

    for bot in bots:
        threading.Thread(target=kill, args=(bot,), daemon=True).start()


def _track(bot, add: bool) -> None:
    with _ACTIVE_LOCK:
        (_ACTIVE_BOTS.add if add else _ACTIVE_BOTS.discard)(bot)



def _wait_unless_stopped(stop_event, seconds: float) -> bool:
    """Chờ có thể ngắt ngay bằng nút Dừng."""
    if stop_event:
        return not stop_event.wait(seconds)
    time.sleep(seconds)
    return True



def _click_text(driver, patterns: tuple[str, ...]) -> bool:
    try:
        return bool(driver.execute_script(r"""
            const pats = arguments[0].map(x => x.toLowerCase());
            const visible = e => {
              const r=e.getBoundingClientRect(), s=getComputedStyle(e);
              return r.width>8 && r.height>8 && s.display!=='none' && s.visibility!=='hidden';
            };
            const nodes=[...document.querySelectorAll(
              'button,[role="button"],label,div,span')].filter(visible);
            const el=nodes.find(e => {
              const t=(e.innerText||e.textContent||'').trim().toLowerCase();
              return t.length<80 && pats.some(p => t===p || t.includes(p));
            });
            if(!el) return false;
            (el.closest('button,[role="button"]')||el).click();
            return true;
        """, list(patterns)))
    except Exception:
        return False



def _trusted_pointer_click(driver, element) -> bool:
    """Phát pointer thật và chỉ nhận khi browser ghi nhận click isTrusted."""
    marker = str(time.time_ns())
    try:
        point = driver.execute_script(r"""
            const element = arguments[0], marker = arguments[1];
            element.scrollIntoView({block:'center', inline:'center'});
            const rect = element.getBoundingClientRect();
            window.__alcTrustedClickProbe = {marker, fired:false};
            addEventListener('click', event => {
              const probe = window.__alcTrustedClickProbe;
              if (probe && probe.marker === marker)
                probe.fired = event.isTrusted;
            }, {capture:true, once:true});
            return {
              x: rect.left + rect.width / 2,
              y: rect.top + rect.height / 2
            };
        """, element, marker)
        if not point:
            return False
    except Exception:
        return False

    def fired() -> bool:
        try:
            return bool(driver.execute_script(
                "const p=window.__alcTrustedClickProbe;"
                "return !!p && p.marker===arguments[0] && p.fired;", marker))
        except Exception:
            return False

    try:
        from selenium.webdriver.common.action_chains import ActionChains
        ActionChains(driver).move_to_element(element).pause(.15).click().perform()
        if fired():
            return True
    except Exception:
        pass
    try:
        x, y = float(point["x"]), float(point["y"])
        driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
            "type": "mouseMoved", "x": x, "y": y,
        })
        driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
            "type": "mousePressed", "x": x, "y": y,
            "button": "left", "buttons": 1, "clickCount": 1,
        })
        driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
            "type": "mouseReleased", "x": x, "y": y,
            "button": "left", "buttons": 0, "clickCount": 1,
        })
        return fired()
    except Exception:
        return False


def _click_crop_register(driver) -> bool:
    """Tìm mọi nút crop hợp lệ rồi bấm bằng pointer trusted."""
    wanted = {"đăng ký", "apply", "áp dụng", "confirm", "xác nhận",
              "done", "hoàn tất", "submit"}
    try:
        buttons = driver.execute_script(r"""
            const wanted = new Set(arguments[0]);
            const visible = el => {
              const r = el.getBoundingClientRect(), s = getComputedStyle(el);
              return r.width > 8 && r.height > 8 && s.display !== 'none'
                && s.visibility !== 'hidden';
            };
            const enabled = el => !el.disabled
              && el.getAttribute('aria-disabled') !== 'true';
            const text = el => String(el.innerText || el.textContent || '')
              .replace(/\s+/g, ' ').trim().toLowerCase();
            const all = [...document.querySelectorAll(
              'button,[role="button"]')].filter(el =>
                visible(el) && enabled(el));

            // Luôn tìm toàn document. Bản cũ bỏ qua nhánh này nếu selector
            // footer bắt trúng một nút không liên quan như Cancel/Save.
            const named = all.filter(el => wanted.has(text(el))).reverse();
            const cropSurface = [...document.querySelectorAll(
              'canvas,input[type="range"],[role="slider"],'
              + '[class*="Crop"],[class*="crop"]')].some(visible);
            const footer = all.filter(el => el.closest(
              '[class*="DivFooterContainer"],[class*="FooterContainer"]'));
            const ordered = cropSurface
              ? [...named, ...footer.reverse()] : named;
            return ordered.filter((el, index) =>
              ordered.indexOf(el) === index);
        """, list(wanted)) or []
        for button in buttons:
            if (button.is_displayed() and button.is_enabled()
                    and button.get_attribute("aria-disabled") != "true"
                    and _trusted_pointer_click(driver, button)):
                deadline = time.time() + 3
                while time.time() < deadline:
                    try:
                        if (not button.is_displayed() or not button.is_enabled()
                                or button.get_attribute("aria-disabled") == "true"):
                            return True
                    except Exception:
                        return True
                    time.sleep(.1)
    except Exception:
        pass
    return False


def _crop_button_snapshot(driver) -> str:
    """Mô tả ngắn các nút đang hiện để chẩn đoán selector, không chứa dữ liệu acc."""
    try:
        rows = driver.execute_script(r"""
            const visible = el => {
              const r = el.getBoundingClientRect(), s = getComputedStyle(el);
              return r.width > 8 && r.height > 8 && s.display !== 'none'
                && s.visibility !== 'hidden';
            };
            return [...document.querySelectorAll('button,[role="button"]')]
              .filter(visible).slice(-16).map(el => ({
                tag: el.tagName.toLowerCase(),
                text: String(el.innerText || el.textContent || '')
                  .replace(/\s+/g, ' ').trim().slice(0, 60),
                e2e: String(el.getAttribute('data-e2e') || '').slice(0, 60),
                disabled: !!el.disabled
                  || el.getAttribute('aria-disabled') === 'true',
                cls: String(el.className || '').split(/\s+/).slice(0, 2)
                  .join('.').slice(0, 80)
              }));
        """) or []
        return json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    except Exception as error:
        return f"snapshot error: {str(error)[:100]}"


def _click_profile_save(driver) -> bool:
    """Bấm đúng Save trong modal bằng pointer trusted."""
    selector = (
        "[data-e2e='edit-profile-popup'] "
        "[data-e2e='edit-profile-save']")
    try:
        buttons = driver.find_elements("css selector", selector)
        if not buttons:
            button = driver.execute_script(r"""
                const visible = el => {
                  const r=el.getBoundingClientRect(), s=getComputedStyle(el);
                  return r.width>0 && r.height>0 && s.display!=='none'
                    && s.visibility!=='hidden';
                };
                const dialogs=[...document.querySelectorAll(
                  '[data-e2e="edit-profile-popup"]')].filter(visible);
                const dialog=dialogs.at(-1);
                if(!dialog) return null;
                return [...dialog.querySelectorAll(
                  '[data-e2e="edit-profile-save"]')].find(el =>
                  visible(el) && !el.disabled
                  && el.getAttribute('aria-disabled')!=='true') || null;
            """)
            buttons = [button] if button else []
        for button in buttons:
            if (button.is_displayed() and button.is_enabled()
                    and button.get_attribute("aria-disabled") != "true"
                    and _trusted_pointer_click(driver, button)):
                return True
    except Exception:
        pass
    return False


def _install_profile_ui_observer(driver) -> None:
    """Ghi lại toast DOM ngắn ngủi xuất hiện sau thao tác Save."""
    driver.execute_script(r"""
        window.__alcProfileUiText = [];
        if (window.__alcProfileUiObserver) window.__alcProfileUiObserver.disconnect();
        window.__alcProfileUiObserver = new MutationObserver(mutations => {
          for (const mutation of mutations) {
            for (const node of mutation.addedNodes) {
              const text = String(node.innerText || node.textContent || '').trim();
              if (text) window.__alcProfileUiText.push(text.slice(0, 500));
            }
          }
          if (window.__alcProfileUiText.length > 100)
            window.__alcProfileUiText.splice(0, 50);
        });
        window.__alcProfileUiObserver.observe(document.documentElement, {
          childList: true, subtree: true
        });
    """)


def _profile_updated_toast(driver) -> str:
    """Trả nội dung toast thành công đã thấy trên UI, kể cả toast đã biến mất."""
    try:
        return str(driver.execute_script(r"""
            const success = /hồ sơ đã được cập nhật|đã cập nhật hồ sơ|profile (?:has been )?updated/i;
            const saved = window.__alcProfileUiText || [];
            for (let i = saved.length - 1; i >= 0; i--) {
              if (success.test(saved[i])) return saved[i];
            }
            const selectors = [
              '[role="alert"]', '[role="status"]', '[data-e2e*="toast"]',
              '[class*="Toast"]', '[class*="toast"]'
            ].join(',');
            for (const node of document.querySelectorAll(selectors)) {
              const text = String(node.innerText || node.textContent || '').trim();
              if (success.test(text)) return text;
            }
            return '';
        """) or "")
    except Exception:
        return ""


def _profile_error_message(driver) -> str:
    """Lấy lỗi TikTok đang hiển thị trong form, không lấy dữ liệu field."""
    try:
        return str(driver.execute_script(r"""
            const error = /không thể|không khả dụng|không hợp lệ|hãy chậm lại|thử lại sau|đã xảy ra lỗi|quá nhanh|cannot|can't|couldn't|not available|invalid|too fast|try again|something went wrong|failed/i;
            const line = raw => String(raw || '').split(/\n+/)
              .map(x => x.replace(/\s+/g, ' ').trim())
              .filter(x => x && x.length <= 220 && error.test(x))
              .sort((a, b) => a.length - b.length)[0] || '';
            const dialogs = [...document.querySelectorAll(
              '[data-e2e="edit-profile-popup"]')].filter(el => {
                const r = el.getBoundingClientRect(), s = getComputedStyle(el);
                return r.width > 0 && r.height > 0 && s.display !== 'none'
                  && s.visibility !== 'hidden';
              });
            const dialog = dialogs.at(-1);
            if (dialog) {
              const matches = [...dialog.querySelectorAll(
                '[role="alert"],[role="status"],[aria-live],'
                + '[data-e2e*="error"],[class*="Error"],[class*="error"],'
                + 'span,p,div')].map(el => line(el.innerText || el.textContent))
                .filter(Boolean).sort((a, b) => a.length - b.length);
              if (matches.length) return matches[0];
            }
            const saved = window.__alcProfileUiText || [];
            for (let i = saved.length - 1; i >= 0; i--) {
              const match = line(saved[i]);
              if (match) return match;
            }
            return '';
        """) or "")
    except Exception:
        return ""


def _profile_modal_open(driver) -> bool:
    try:
        return any(element.is_displayed() for element in driver.find_elements(
            "css selector", "[data-e2e='edit-profile-popup']"))
    except Exception:
        return False


def _find_profile_avatar_input(driver, timeout: float = 15,
                               stop_event=None):
    """Chỉ lấy file input sau khi form Sửa hồ sơ thật sự xuất hiện."""
    selector = (
        "[data-e2e='edit-profile-avatar-edit-icon'] input[type='file'],"
        "[data-e2e='edit-profile-avatar'] input[type='file'],"
        "input[type='file'][accept*='image'],"
        "input[type='file'][accept*='.jpg']")
    deadline = time.time() + timeout
    while time.time() < deadline and not (
            stop_event and stop_event.is_set()):
        try:
            popups = driver.find_elements(
                "css selector", "[data-e2e='edit-profile-popup']")
            for popup in reversed(popups):
                if not popup.is_displayed():
                    continue
                files = popup.find_elements("css selector", selector)
                if files:
                    return files[0]
        except Exception:
            pass
        time.sleep(.2)
    return None


def _profile_avatar_state(driver) -> dict:
    """Nhận diện avatar đang hiển thị trên trang hồ sơ, không gọi API."""
    selectors = (
        "[data-e2e='user-avatar'] img,"
        "img[data-e2e='user-avatar'],"
        "[data-e2e='profile-icon'] img,"
        "[data-e2e='user-avatar']"
    )
    try:
        for element in driver.find_elements("css selector", selectors):
            if not element.is_displayed():
                continue
            identity = driver.execute_script(r"""
                const host = arguments[0];
                const img = host.tagName === 'IMG' ? host : host.querySelector('img');
                if (!img || !img.complete || !img.naturalWidth) return '';
                try {
                  const url = new URL(img.currentSrc || img.src, location.href);
                  return url.pathname.split('~', 1)[0];
                } catch (_) {
                  return img.currentSrc || img.src || '';
                }
            """, element) or ""
            try:
                image = element.screenshot_as_png
            except Exception:
                image = b""
            if identity or image:
                return {"identity": str(identity), "image": image}
    except Exception:
        pass
    return {"identity": "", "image": b""}


def _profile_avatar_changed(before: dict, after: dict) -> bool:
    """So sánh avatar đã render; ưu tiên object path, fallback ảnh chụp element."""
    old_identity = before.get("identity") or ""
    new_identity = after.get("identity") or ""
    if old_identity and new_identity:
        return old_identity != new_identity
    old_image = before.get("image") or b""
    new_image = after.get("image") or b""
    return bool(old_image and new_image and old_image != new_image)


def _install_profile_debug_trace(bot, log) -> None:
    """Cài recorder DOM/CDP thụ động; không sửa fetch, XHR hay request."""
    driver = bot.driver
    bot._profile_trace_requests = {}
    bot._profile_update_result = None
    bot._profile_upload_result = None
    try:
        driver.execute_cdp_cmd("Network.enable", {})
        driver.get_log("performance")  # bỏ request login trước pha profile
    except Exception:
        pass
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": r"""
        (() => {
          if (window.__alcProfileActionTraceInstalled) return;
          window.__alcProfileActionTraceInstalled = true;
          window.__alcProfileActionTrace = [];
          const push = event => {
            const raw = event.target;
            if (!(raw instanceof Element)) return;
            const target = raw.closest(
              'button,[role="button"],input,[data-e2e*="profile"],'
              + '[class*="Modal"],[class*="modal"]');
            if (!target) return;
            const row = {
              at: Math.round(performance.now()),
              type: event.type,
              trusted: event.isTrusted,
              tag: target.tagName.toLowerCase(),
              e2e: target.getAttribute('data-e2e') || '',
              role: target.getAttribute('role') || '',
              disabled: !!target.disabled,
              ariaDisabled: target.getAttribute('aria-disabled') || '',
              text: String(target.innerText || target.textContent || '')
                .trim().replace(/\s+/g, ' ').slice(0, 70)
            };
            if (target instanceof HTMLInputElement && target.type === 'file') {
              row.files = target.files ? target.files.length : 0;
              row.bytes = target.files && target.files[0]
                ? target.files[0].size : 0;
            }
            window.__alcProfileActionTrace.push(row);
            if (window.__alcProfileActionTrace.length > 300)
              window.__alcProfileActionTrace.splice(0, 150);
          };
          ['pointerdown','mousedown','mouseup','click','input','change','submit']
            .forEach(type => addEventListener(type, push, true));
        })();
        """
    })
    log("🐞 Trace thụ động đã bật: DOM event + CDP Network; "
        "fetch/XHR vẫn nguyên bản.")
    bot._profile_trace_enabled = True


def _request_field_names(request: dict) -> str:
    data = str(request.get("postData") or "")
    if not data:
        return ""
    names = []
    try:
        parsed = json.loads(data)
        if isinstance(parsed, dict):
            names = [str(key) for key in parsed]
    except (TypeError, ValueError):
        names = [key for key, _value in parse_qsl(data, keep_blank_values=True)]
    clean = sorted({name[:40] for name in names if name})
    return ",".join(clean[:12])


def _trace_request(request: dict) -> bool:
    method = str(request.get("method") or "GET").upper()
    if method not in {"POST", "PUT", "PATCH"}:
        return False
    url = str(request.get("url") or "").lower()
    data = str(request.get("postData") or "").lower()
    return ("/api/upload/image/" in url
            or "/api/update/profile/" in url
            or "avatar_uri" in data)


def _flush_profile_debug_trace(bot, log, phase: str) -> bool:
    """Drain action + Network events, gắn nhãn SELENIUM/MANUAL để so trực tiếp."""
    driver = getattr(bot, "driver", None)
    if not driver:
        return False
    alive = True
    try:
        rows = driver.execute_script(
            "const a=window.__alcProfileActionTrace||[];"
            "window.__alcProfileActionTrace=[];return a;") or []
        for row in rows:
            target = row.get("e2e") or row.get("role") or row.get("tag") or "?"
            state = "disabled" if row.get("disabled") or row.get("ariaDisabled") == "true" else "enabled"
            trusted = "trusted" if row.get("trusted") else "UNTRUSTED"
            detail = f" files={row.get('files')} bytes={row.get('bytes')}" if "files" in row else ""
            text = str(row.get("text") or "")[:50]
            log(f"[trace {phase} ui] {row.get('type')} {trusted} "
                f"{target} {state} “{text}”{detail}")
    except Exception:
        alive = False

    try:
        entries = driver.get_log("performance")
    except Exception:
        return False
    requests = getattr(bot, "_profile_trace_requests", {})
    for entry in entries:
        try:
            message = json.loads(entry["message"])["message"]
            event = message.get("method")
            params = message.get("params") or {}
            request_id = str(params.get("requestId") or "")
            if event == "Network.requestWillBeSent":
                request = params.get("request") or {}
                if not _trace_request(request):
                    continue
                parsed = urlsplit(str(request.get("url") or ""))
                label = f"{parsed.netloc}{parsed.path}"
                fields = _request_field_names(request)
                requests[request_id] = {
                    "method": str(request.get("method") or "GET").upper(),
                    "label": label[:160],
                    "fields": fields,
                    "phase": phase,
                    "at": float(params.get("timestamp") or 0),
                    "kind": (
                        "upload" if "/api/upload/image/" in label
                        else "profile" if "/api/update/profile/" in label
                        else "request"),
                }
                suffix = f" fields={fields}" if fields else ""
                log(f"[trace {phase} net] → {requests[request_id]['method']} "
                    f"{requests[request_id]['label']}{suffix}")
            elif event == "Network.responseReceived" and request_id in requests:
                item = requests[request_id]
                response = params.get("response") or {}
                elapsed = max(0, round(
                    (float(params.get("timestamp") or 0) - item["at"]) * 1000))
                log(f"[trace {item['phase']} net] ← {item['method']} "
                    f"{int(response.get('status') or 0)} {elapsed}ms {item['label']}")
                item["response_received"] = True
            elif event == "Network.loadingFinished" and request_id in requests:
                item = requests.pop(request_id)
                if item.get("response_received"):
                    try:
                        raw = driver.execute_cdp_cmd(
                            "Network.getResponseBody", {"requestId": request_id})
                        body = str(raw.get("body") or "")
                        if raw.get("base64Encoded"):
                            body = base64.b64decode(body).decode(
                                "utf-8", errors="replace")
                        body = body.lstrip("\ufeff").strip()
                        kind = item["kind"]
                        if not body:
                            encoded = int(params.get("encodedDataLength") or 0)
                            log(f"[trace {item['phase']} {kind} result] "
                                f"response body rỗng bytes={encoded}; "
                                "dùng trạng thái hiển thị trên form")
                            continue
                        payload = json.loads(body)
                        if not isinstance(payload, dict):
                            raise ValueError("response không phải object")
                        code = payload.get("status_code")
                        message_text = str(
                            payload.get("status_msg")
                            or payload.get("message") or "")[:100]
                        if kind == "upload":
                            data = payload.get("data")
                            data = data if isinstance(data, dict) else {}
                            has_uri = bool(
                                data.get("uri") or data.get("avatar_uri")
                                or data.get("image_uri"))
                            bot._profile_upload_result = {
                                "status_code": code,
                                "status_msg": message_text,
                                "has_uri": has_uri,
                            }
                            uri_state = "yes" if has_uri else "no"
                            log(f"[trace {item['phase']} upload result] "
                                f"status_code={code} status_msg=“{message_text}” "
                                f"avatar_uri={uri_state}")
                        elif kind == "profile":
                            bot._profile_update_result = {
                                "status_code": code,
                                "status_msg": message_text,
                            }
                            log(f"[trace {item['phase']} profile result] "
                                f"status_code={code} status_msg=“{message_text}”")
                    except Exception as error:
                        log(f"[trace {item['phase']} {item['kind']} result] "
                            f"không đọc được response JSON: {str(error)[:100]}")
            elif event == "Network.loadingFailed" and request_id in requests:
                item = requests.pop(request_id)
                reason = str(params.get("errorText") or "failed")[:80]
                log(f"[trace {item['phase']} net] × {item['method']} "
                    f"{item['label']} {reason}")
        except (KeyError, TypeError, ValueError):
            continue
    bot._profile_trace_requests = requests
    return alive


def _start_manual_profile_trace(bot, log, stop_event) -> None:
    if getattr(bot, "_profile_trace_streaming", False):
        return
    bot._profile_trace_streaming = True
    log("🐞 Pha MANUAL: thao tác lại trong browser; event/request mới sẽ "
        "được log để so với pha SELENIUM.")

    def stream():
        failures = 0
        while not (stop_event and stop_event.is_set()):
            if _flush_profile_debug_trace(bot, log, "MANUAL"):
                failures = 0
            else:
                failures += 1
                if failures >= 6:
                    break
            time.sleep(.5)
        _flush_profile_debug_trace(bot, log, "MANUAL")
        bot._profile_trace_streaming = False

    threading.Thread(target=stream, daemon=True).start()


def _open_authenticated(acc: dict, proxy, headless: bool, stop_event, log,
                        debug: bool = False, fresh_login: bool = False):
    import bot_login
    import tiktok_reply
    bot = tiktok_reply.TikTokLikeBot(
        log, stop_event or threading.Event(),
        prefix=acc.get("username") or f"acc{acc.get('id')}")
    bot.start_browser(
        headless=headless, session_idx=int(acc.get("id") or 0) % 100,
        proxy=proxy, compact=True, debug=debug, http_debug=False)
    password = acc.get("password") or ""
    use_fresh_login = bool(fresh_login and password)
    logged = False
    if use_fresh_login:
        log("🔐 Đăng nhập mới bằng tài khoản/mật khẩu; không nạp cookie cũ.")
    else:
        if fresh_login:
            log("⚠ Acc không có mật khẩu; dùng cookie hiện có để đăng nhập.")
        cookies = tiktok_reply.parse_cookies(acc.get("cookie") or "")
        logged = bool(cookies and bot.login_with_cookies(cookies))
    if not logged and password:
        email = (acc.get("email") or "").strip()
        login_user = (
            acc.get("login_user") or acc.get("username") or "")
        try:
            ok, cookie, uname = bot_login.auto_login_with_email_fallback(
                bot, login_user, email, password,
                acc.get("totp_secret") or "",
                log_fn=log, mail_pass=acc.get("mail_pass") or "")
        finally:
            try:
                bot.pause_network_blocks(False)
            except Exception:
                pass
        if ok:
            acc["cookie"] = cookie
            if uname:
                acc["username"] = uname
            logged = True
    return bot, logged


def _success(acc: dict, changed: bool = True) -> dict:
    return {
        "ok": True,
        "changed": changed,
        "cookie": acc.get("cookie", ""),
        "username": acc.get("username", ""),
    }


def _failure(acc: dict, error: str) -> dict:
    return {
        "ok": False,
        "error": error,
        "cookie": acc.get("cookie", ""),
        "username": acc.get("username", ""),
    }


def _complete_avatar_change(acc: dict, stop_event, log, keep_open: bool) -> dict:
    """Giữ profile hiện thêm một nhịp ở chế độ chạy thật rồi mới đóng browser."""
    if not keep_open:
        log(f"⏳ Đổi avatar thành công; giữ hồ sơ thêm "
            f"{_PROFILE_SUCCESS_HOLD_SECONDS:g} giây trước khi đóng...")
        _wait_unless_stopped(stop_event, _PROFILE_SUCCESS_HOLD_SECONDS)
    return _success(acc)


def set_private(acc: dict, proxy=None, headless=True, stop_event=None,
                log=None) -> dict:
    """Bật Private account; selector name ổn định, text EN/VI chỉ là fallback."""
    log = log or (lambda _m: None)
    bot = None
    try:
        bot, logged = _open_authenticated(acc, proxy, headless, stop_event, log)
        if not logged:
            return _failure(acc, "login fail")
        _track(bot, True)
        d = bot.driver
        d.get("https://www.tiktok.com/setting")
        selector = "input[name='private_account_item'][type='checkbox']"
        deadline = time.time() + 25
        switch = None
        while time.time() < deadline and not (stop_event and stop_event.is_set()):
            found = d.find_elements("css selector", selector)
            if not found:
                found = d.execute_script(r"""
                    const cells=[...document.querySelectorAll(
                      '[data-testid="tux-web-list-cell"]')];
                    const cell=cells.find(x =>
                      /private account|tài khoản riêng tư/i.test(
                        x.innerText||x.textContent||''));
                    return cell ? [...cell.querySelectorAll(
                      'input[type="checkbox"]')] : [];
                """) or []
            if found:
                switch = found[0]
                break
            time.sleep(.5)
        if switch is None:
            return _failure(acc, "không thấy Private account/Tài khoản riêng tư")
        if switch.is_selected():
            return _success(acc, changed=False)

        d.execute_script(r"""
            const input=arguments[0];
            const host=input.closest(
              '[data-testid="tux-web-interaction-container"]');
            const indicator=host && host.querySelector(
              '[class*="indicator__container"]');
            (indicator||host||input).click();
        """, switch)
        time.sleep(1)
        current = d.find_elements("css selector", selector)
        if current and current[0].is_selected():
            return _success(acc)

        # React-controlled checkbox: native setter + bubbling events.
        target = current[0] if current else switch
        d.execute_script(r"""
            const input=arguments[0];
            const setter=Object.getOwnPropertyDescriptor(
              HTMLInputElement.prototype,'checked').set;
            setter.call(input,true);
            input.dispatchEvent(new Event('input',{bubbles:true}));
            input.dispatchEvent(new Event('change',{bubbles:true}));
        """, target)
        deadline = time.time() + 8
        while time.time() < deadline:
            current = d.find_elements("css selector", selector)
            if current and current[0].is_selected():
                return _success(acc)
            time.sleep(.4)
        return _failure(acc, "đã click nhưng switch vẫn chưa bật")
    except Exception as e:
        return _failure(acc, str(e)[:140])
    finally:
        if bot:
            _track(bot, False)
            bot.close()




def change_avatar(acc: dict, avatar_path: str = "", proxy=None, headless=True,
                  stop_event=None, log=None, debug=False) -> dict:
    """Thao tác Edit profile và xác nhận avatar đổi hoàn toàn qua giao diện."""
    log = log or (lambda _m: None)
    path = Path(avatar_path).resolve() if avatar_path else None
    if not path or not path.is_file():
        return _failure(acc, "không tải được avatar từ kho VPS")
    bot = None
    keep_open = False
    trace_log = log if debug else (lambda _m: None)
    try:
        bot, logged = _open_authenticated(
            acc, proxy, headless, stop_event, log, debug=debug,
            fresh_login=True)
        if not logged:
            return _failure(acc, "login fail")
        _track(bot, True)
        keep_open = bool(debug)
        d = bot.driver
        try:
            _install_profile_debug_trace(bot, trace_log)
            if debug:
                log("🐞 Pha SELENIUM: tool bắt đầu thao tác đổi avatar.")
        except Exception as error:
            if debug:
                log(f"⚠️ Không bật được trace thụ động: {str(error)[:100]}")
        uname = (acc.get("username") or bot.get_username() or "").lstrip("@")
        if not uname:
            return _failure(acc, "không lấy được username")
        d.get(f"https://www.tiktok.com/@{uname}")
        deadline = time.time() + 20
        edit = None
        while time.time() < deadline and not (stop_event and stop_event.is_set()):
            found = d.find_elements(
                "css selector", "[data-e2e='edit-profile-entrance']")
            if found:
                edit = found[0]
                break
            time.sleep(.5)
        before_avatar = _profile_avatar_state(d)
        if not before_avatar.get("identity") and not before_avatar.get("image"):
            return _failure(acc, "không đọc được avatar hiện tại trên hồ sơ")
        log(f"⏳ Chờ {_PROFILE_STEP_PAUSE_SECONDS:g} giây trước khi mở Sửa hồ sơ...")
        if not _wait_unless_stopped(
                stop_event, _PROFILE_STEP_PAUSE_SECONDS):
            return _failure(acc, "đã dừng")
        if edit:
            try:
                edit.click()
            except Exception:
                d.execute_script("arguments[0].click();", edit)
        elif not _click_text(d, ("edit profile", "sửa hồ sơ")):
            return _failure(acc, "không thấy Edit profile/Sửa hồ sơ")

        upload = _find_profile_avatar_input(
            d, timeout=15, stop_event=stop_event)
        if upload is None:
            return _failure(acc, "form Sửa hồ sơ chưa mở hoặc không có input avatar")
        log(f"⏳ Chờ {_PROFILE_STEP_PAUSE_SECONDS:g} giây trước khi chọn ảnh...")
        if not _wait_unless_stopped(
                stop_event, _PROFILE_STEP_PAUSE_SECONDS):
            return _failure(acc, "đã dừng")
        upload.send_keys(str(path))
        log(f"🖼 Đã chọn file, chờ {_PROFILE_FILE_SETTLE_SECONDS:g} giây "
            "để TikTok dựng modal ảnh...")
        if not _wait_unless_stopped(
                stop_event, _PROFILE_FILE_SETTLE_SECONDS):
            return _failure(acc, "đã dừng")
        log("🖼 Đang chờ nút Đăng ký trong modal ảnh...")
        deadline = time.time() + 20
        applied = False
        while time.time() < deadline and not (stop_event and stop_event.is_set()):
            if _click_crop_register(d):
                applied = True
                log("✅ Đã bấm Đăng ký ảnh.")
                break
            time.sleep(.5)
        if not applied:
            if debug:
                log(f"🐞 Crop candidates: {_crop_button_snapshot(d)}")
            return _failure(acc, "không thấy/bấm được nút Đăng ký ảnh")
        log(f"⏳ Chờ {_PROFILE_SAVE_SETTLE_SECONDS:g} giây để TikTok hoàn tất "
            "ảnh trước khi Lưu hồ sơ...")
        if not _wait_unless_stopped(
                stop_event, _PROFILE_SAVE_SETTLE_SECONDS):
            return _failure(acc, "đã dừng")
        try:
            _install_profile_ui_observer(d)
        except Exception:
            pass

        log("💾 Đang chờ nút Lưu hồ sơ...")
        deadline = time.time() + 20
        saved = False
        while time.time() < deadline and not (stop_event and stop_event.is_set()):
            if _click_profile_save(d):
                saved = True
                log("💾 Đã bấm Lưu như thao tác thủ công.")
                break
            time.sleep(.5)
        if not saved:
            if stop_event and stop_event.is_set():
                return _failure(acc, "đã dừng")
            return _failure(acc, "không thấy/bấm được nút Save/Lưu")

        # Mỗi thao tác Save chỉ gửi một lần. Retry khi TikTok đã giới hạn tài
        # khoản chỉ tạo thêm request và có thể kéo dài thời gian bị chặn.
        deadline = time.time() + 20
        toast = ""
        modal_open = True
        while time.time() < deadline and not (stop_event and stop_event.is_set()):
            failure = None
            if getattr(bot, "_profile_trace_enabled", False):
                _flush_profile_debug_trace(bot, trace_log, "SELENIUM")
                upload_result = getattr(bot, "_profile_upload_result", None)
                if (upload_result
                        and upload_result.get("status_code") not in (None, 0, "0")):
                    upload_code = upload_result.get("status_code")
                    upload_message = (
                        upload_result.get("status_msg")
                        or "TikTok từ chối file ảnh")
                    failure = (
                        upload_code,
                        upload_message,
                        f"TikTok từ chối upload ảnh ({upload_code}): "
                        f"{upload_message}",
                    )
                result = getattr(bot, "_profile_update_result", None)
                if (failure is None and result
                        and result.get("status_code") not in (None, 0, "0")):
                    code = result.get("status_code")
                    message_text = (
                        result.get("status_msg") or "TikTok từ chối cập nhật")
                    if str(code) == "3002284":
                        clean_message = message_text.rstrip().rstrip(".")
                        error = (
                            "TikTok đang giới hạn cập nhật hồ sơ "
                            f"(3002284): {clean_message}. Không tự thử lại.")
                    else:
                        error = f"TikTok từ chối Save ({code}): {message_text}"
                    failure = (code, message_text, error)
            if failure is None:
                error_text = _profile_error_message(d)
                if error_text:
                    failure = (
                        None,
                        error_text,
                        f"TikTok báo trên form: {error_text}",
                    )

            if failure:
                return _failure(acc, failure[2])

            toast = toast or _profile_updated_toast(d)
            modal_open = _profile_modal_open(d)
            if toast and not modal_open:
                break
            time.sleep(.2)
        if modal_open:
            error_text = _profile_error_message(d)
            if error_text:
                return _failure(acc, f"TikTok báo trên form: {error_text}")
            return _failure(acc, "đã bấm Lưu nhưng form Sửa hồ sơ chưa tự đóng")
        if not toast:
            return _failure(acc, "form đã đóng nhưng không thấy toast Hồ sơ đã được cập nhật")
        log(f"✅ TikTok xác nhận trên giao diện: {toast[:100]}")

        # Modal đã đóng an toàn. Ưu tiên avatar cập nhật ngay trên trang; chỉ F5
        # sau xác nhận UI nếu ảnh vẫn còn cache.
        after_avatar = _profile_avatar_state(d)
        if _profile_avatar_changed(before_avatar, after_avatar):
            log("✅ Avatar đã đổi trên trang hồ sơ.")
            return _complete_avatar_change(acc, stop_event, log, keep_open)
        log("🔄 Đang tải lại hồ sơ để bỏ cache avatar...")
        d.refresh()
        deadline = time.time() + 20
        while time.time() < deadline and not (stop_event and stop_event.is_set()):
            after_avatar = _profile_avatar_state(d)
            if _profile_avatar_changed(before_avatar, after_avatar):
                log("✅ Avatar đã đổi trên trang hồ sơ sau khi tải lại.")
                return _complete_avatar_change(acc, stop_event, log, keep_open)
            time.sleep(.5)
        return _failure(acc, "TikTok báo đã cập nhật nhưng avatar trên hồ sơ chưa đổi")
    except Exception as e:
        return _failure(acc, str(e)[:140])
    finally:
        stopped = bool(stop_event and stop_event.is_set())
        if (bot and keep_open and not stopped
                and getattr(bot, "_profile_trace_enabled", False)):
            _flush_profile_debug_trace(bot, log, "SELENIUM")
            _start_manual_profile_trace(bot, log, stop_event)
        elif bot:
            _track(bot, False)
            bot.close()

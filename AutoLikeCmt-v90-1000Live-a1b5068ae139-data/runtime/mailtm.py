"""Mail.tm — POST /token rồi GET /messages với Bearer. Acc comment 2FA qua mail."""
from __future__ import annotations

from datetime import datetime
import json
import re
import threading
import time
import urllib.error
import urllib.request

API = "https://api.mail.tm"
_OTP_RE = re.compile(r"(?<![#0-9a-zA-Z])(\d{6})(?![0-9a-zA-Z])")
_RATE_LOCK = threading.Lock()
_LAST_REQ_AT = 0.0
_MIN_REQ_INTERVAL = 0.22


class MailTmStopped(RuntimeError):
    pass


def _raise_if_stopped(stop_event) -> None:
    if stop_event is not None and stop_event.is_set():
        raise MailTmStopped("Đã dừng chờ Mail.tm.")


def _wait(stop_event, seconds: float) -> None:
    if stop_event is not None:
        if stop_event.wait(seconds):
            raise MailTmStopped("Đã dừng chờ Mail.tm.")
    else:
        time.sleep(seconds)


def _throttle(stop_event) -> None:
    global _LAST_REQ_AT
    with _RATE_LOCK:
        now = time.monotonic()
        wait_s = _MIN_REQ_INTERVAL - (now - _LAST_REQ_AT)
        if wait_s > 0:
            _LAST_REQ_AT = now + wait_s
        else:
            _LAST_REQ_AT = now
    if wait_s > 0:
        _wait(stop_event, wait_s)


def _req(method: str, path: str, token: str = "", body: dict | None = None,
         timeout=20, stop_event=None):
    data = None
    headers = {"Accept": "application/ld+json, application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(API + path, data=data, headers=headers, method=method)
    last_err = None
    for attempt in range(7):
        _raise_if_stopped(stop_event)
        _throttle(stop_event)
        try:
            request_timeout = min(float(timeout), 3.0) if stop_event else timeout
            with urllib.request.urlopen(req, timeout=request_timeout) as r:
                raw = r.read().decode("utf-8", errors="replace")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")[:240]
            if e.code == 429:
                _wait(stop_event, 1.2 * (attempt + 1))
                last_err = RuntimeError(f"mail.tm 429 {path}")
                continue
            if e.code == 401:
                raise RuntimeError("mail.tm 401 Unauthorized — token/pass MailTM sai") from e
            if e.code == 404:
                raise RuntimeError(f"mail.tm 404 {path} — account/message không tồn tại") from e
            raise RuntimeError(f"mail.tm {e.code} {path}: {err}") from e
        except urllib.error.URLError as e:
            last_err = e
            _wait(stop_event, 1)
    if last_err:
        raise RuntimeError(str(last_err))
    return {}


def list_domains(stop_event=None) -> list[str]:
    """GET /domains — domain active, không private."""
    data = _req("GET", "/domains", stop_event=stop_event) or {}
    members = data.get("hydra:member") or data.get("member") or []
    out = []
    for d in members:
        name = (d.get("domain") or "").strip().lower()
        if not name:
            continue
        if d.get("isActive") is False:
            continue
        if d.get("isPrivate") is True:
            continue
        out.append(name)
    return out


_KNOWN_MAILTM_DOMAINS = {"somoj.com", "punkproof.com", "tiffincrane.com", "maxxspace.com"}
_NON_MAILTM_DOMAINS = {
    "hotmail.com", "outlook.com", "live.com", "msn.com", "gmail.com", "yahoo.com",
    "icloud.com", "megacloudx.com", "mailharb.com", "lunarbytehub.com", "trustlark.com",
}


def is_supported_mailtm_address(address: str) -> bool:
    addr = (address or "").strip().lower()
    if "@" not in addr:
        return False
    host = addr.split("@")[-1].strip()
    if not host or host in _NON_MAILTM_DOMAINS:
        return False
    return True


def ensure_account(address: str, password: str, log=None,
                   stop_event=None) -> None:
    """POST /accounts nếu token 401. Domain phải nằm GET /domains."""
    log = log or (lambda _m: None)
    addr = (address or "").strip()
    pw = (password or "").strip() or "MailTM@"
    host = addr.split("@")[-1].lower() if "@" in addr else ""
    if host in _NON_MAILTM_DOMAINS:
        raise RuntimeError(f"Email @{host} không thuộc Mail.tm (cần Cookie hoặc 2FA TOTP)")
    domains = list_domains(stop_event=stop_event)
    if host and domains and host not in domains:
        log(f"[mail.tm] domain {host} không trong GET /domains: {', '.join(domains[:8])}")
        raise RuntimeError(f"Domain @{host} không hỗ trợ tạo mới trên Mail.tm")
    try:
        _req("POST", "/accounts", body={"address": addr, "password": pw},
             stop_event=stop_event)
        log(f"[mail.tm] POST /accounts OK {addr}")
    except RuntimeError as e:
        msg = str(e).lower()
        if "422" in msg or "exist" in msg or "already" in msg:
            return
        if "400" in msg:
            return
        raise


def token(address: str, password: str, log=None, stop_event=None) -> str:
    """POST /token. 401 → POST /accounts rồi token lại."""
    log = log or (lambda _m: None)
    addr = (address or "").strip()
    pw = (password or "").strip() or "MailTM@"
    host = addr.split("@")[-1].lower() if "@" in addr else ""
    if host in _NON_MAILTM_DOMAINS:
        raise RuntimeError(f"Email @{host} không thuộc Mail.tm (cần Cookie hoặc 2FA TOTP)")
    try:
        r = _req(
            "POST", "/token", body={"address": addr, "password": pw},
            stop_event=stop_event)
    except RuntimeError as e:
        if "401" in str(e) and host not in _KNOWN_MAILTM_DOMAINS:
            ensure_account(addr, pw, log=log, stop_event=stop_event)
            r = _req(
                "POST", "/token", body={"address": addr, "password": pw},
                stop_event=stop_event)
        else:
            raise
    t = (r or {}).get("token") or ""
    if not t:
        raise RuntimeError("mail.tm không trả token (sai email/pass MailTM?)")
    return t


def _otp_from_text(*parts: str) -> str:
    blob = "\n".join(p or "" for p in parts)
    m = _OTP_RE.search(blob)
    return m.group(1) if m else ""


def _message_timestamp(message: dict) -> float | None:
    raw = str(message.get("createdAt") or message.get("updatedAt") or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def wait_otp(address: str, password: str, timeout: int = 90, log=None,
             ignore_codes=None, stop_event=None,
             not_before: float | None = None) -> str:
    """Lấy OTP mới sau thời điểm yêu cầu; stop_event ngắt ngay vòng chờ."""
    log = log or (lambda _m: None)
    skip = {str(x) for x in (ignore_codes or []) if x}
    _raise_if_stopped(stop_event)
    tok = token(address, password, log=log, stop_event=stop_event)
    log(f"[mail.tm] token OK {address}")
    deadline = time.time() + max(20, int(timeout))
    seen = set()
    next_heartbeat = time.time() + 15
    while time.time() < deadline:
        _raise_if_stopped(stop_event)
        try:
            listing = _req(
                "GET", "/messages", token=tok, stop_event=stop_event) or {}
        except MailTmStopped:
            raise
        except Exception as e:
            if "429" in str(e):
                _wait(stop_event, 2.0)
                continue
            raise
        hydra = listing.get("hydra:member") or listing.get("member") or []
        if isinstance(listing, list):
            hydra = listing
        for m in hydra:
            if not_before is not None:
                sent_at = _message_timestamp(m)
                if sent_at is not None and sent_at < not_before - 15:
                    continue
            mid = str(m.get("id") or "")
            intro = m.get("intro") or ""
            subj = m.get("subject") or ""
            sender = str((m.get("from") or {}).get("address") or "").lower()
            subj_low = subj.lower()
            if "newsletter@" in sender or "security alert" in subj_low or "new login" in subj_low or "đăng nhập mới" in subj_low:
                continue
            code = _otp_from_text(subj, intro)
            if not code and mid and mid not in seen:
                seen.add(mid)
                try:
                    full = _req(
                        "GET", f"/messages/{mid}", token=tok,
                        stop_event=stop_event) or {}
                except MailTmStopped:
                    raise
                except Exception:
                    full = {}
                text = full.get("text") or ""
                code = _otp_from_text(subj, intro, text)
            if code and code not in skip:
                log(f"[mail.tm] OTP {code} từ {address}")
                return code
        now = time.time()
        if now >= next_heartbeat:
            remaining = max(0, int(deadline - now))
            log(f"[mail.tm] vẫn chờ OTP {address} ({remaining}s còn lại)")
            next_heartbeat = now + 15
        _wait(stop_event, 1.5)  # < 8 QPS; dừng được ngay
    log(f"[mail.tm] hết giờ, không thấy OTP cho {address}")
    return ""

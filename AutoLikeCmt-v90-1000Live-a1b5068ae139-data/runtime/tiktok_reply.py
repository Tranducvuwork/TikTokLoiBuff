"""
TikTok Like Tool  -  Tim video hoặc tim comment, chạy hàng loạt nhiều tài khoản.
"""

import json
import os
import queue
import random
import re
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from tkinter import ttk, scrolledtext, messagebox, filedialog
from urllib.parse import parse_qs, unquote, urlparse

import undetected_chromedriver as uc
from selenium import webdriver
from selenium.webdriver.edge.options import Options as EdgeOptions
from selenium.webdriver.edge.service import Service as EdgeService
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException
from webdriver_manager.microsoft import EdgeChromiumDriverManager

from proxy_relay import ProxyRelay, Socks5Relay
from omo_captcha import (
    OmocaptchaClient, check_omo_key, load_omo_key, save_omo_key,
    load_cap_provider, save_cap_provider,
)


def recommend_thread_count(requested: int, total: int) -> tuple[int, str]:
    """Respect the explicit worker count; only available jobs limit it."""
    req = max(1, int(requested or 1))
    count = max(0, int(total or 0))
    workers = min(req, count)
    note = (f"Chạy {workers}/{req} luồng: chỉ có {count} account."
            if workers < req else f"Chạy đủ {workers} luồng theo số đã chọn.")
    return workers, note


def browser_grid_rect(
        slot: int, screen_width: int, screen_height: int,
        screen_left: int = 0, screen_top: int = 0) -> tuple[int, int, int, int, int]:
    """Return a compact five-column, two-row browser tile.

    The final value is the number of unique slots available on this screen.
    A four-pixel gutter keeps adjacent Chrome borders independently draggable.
    """
    width = max(1, int(screen_width or 0))
    height = max(1, int(screen_height or 0))
    gutter = 4
    columns = max(1, min(5, width // 200))
    rows = 2 if height >= 480 else 1
    capacity = columns * rows
    normalized = max(0, int(slot or 0)) % capacity
    column = normalized % columns
    row = normalized // columns
    tile_width = max(1, (width - gutter * (columns - 1)) // columns)
    tile_height = max(1, (height - gutter * (rows - 1)) // rows)
    x = int(screen_left or 0) + column * (tile_width + gutter)
    y = int(screen_top or 0) + row * (tile_height + gutter)
    return x, y, tile_width, tile_height, capacity



# Tắt tiếng ồn log của urllib3 khi driver chết giữa chừng: mỗi thao tác d.get /
# find_elements trên session đã đóng retry 3 lần + in "WARNING: Retrying after
# connection broken ... connection refused" → spam tràn console, che log thật.
# Các exception thực vẫn được code bắt qua try/except nên không mất thông tin lỗi.
import logging as _logging
_logging.getLogger("urllib3").setLevel(_logging.ERROR)
_logging.getLogger("selenium").setLevel(_logging.ERROR)


# ── Ẩn cửa sổ console đen trên Windows ─────────────────────────────────────
# CREATE_NO_WINDOW: ẩn cửa sổ console cho subprocess con. Dùng riêng cho từng
# lệnh (vd taskkill) — KHÔNG monkey-patch subprocess.Popen toàn cục, vì
# undetected_chromedriver khởi động Chrome bằng Popen với cờ DETACHED_PROCESS
# và CREATE_NO_WINDOW xung đột với nó → uc retry lặp, mở nhiều cửa sổ.
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


ACCOUNTS_FILE = Path(__file__).parent / "accounts.json"
PROXIES_FILE  = Path(__file__).parent / "proxies.txt"
LLM_KEY_FILE  = Path(__file__).parent / "llm_key.txt"
LLM_CFG_FILE  = Path(__file__).parent / "llm_api.txt"


# ── LLM-backed CAPTCHA solver (OpenAI-compatible API + tiktok-captcha-solver) ─
# Thay "bộ não" EulerStream/SadCaptcha (trả phí) bằng 1 LLM vision (kimi/gpt/...).
# Phần kéo chuột / click vẫn dùng SeleniumSolver của tiktok-captcha-solver — ta
# chỉ gán solver.client = LLMApiClient (cùng giao diện puzzle/rotate/shapes/icon).
DEFAULT_LLM_BASE  = "https://llm.konfuse.me/v1"
DEFAULT_LLM_MODEL = "kimi-k2.6"
# DEFAULT_LLM_BASE  = "https://generativelanguage.googleapis.com/v1beta/interactions"
# DEFAULT_LLM_MODEL = "gemini-2.5-flash"


def load_llm_key() -> str:
    if LLM_KEY_FILE.exists():
        return LLM_KEY_FILE.read_text(encoding="utf-8").strip()
    return ""


def save_llm_key(key: str):
    LLM_KEY_FILE.write_text(key.strip(), encoding="utf-8")


def load_llm_config() -> tuple[str, str]:
    """Trả (base_url, model). Đọc từ llm_api.txt (2 dòng) hoặc dùng default."""
    base, model = DEFAULT_LLM_BASE, DEFAULT_LLM_MODEL
    if LLM_CFG_FILE.exists():
        lines = [l.strip() for l in LLM_CFG_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
        if len(lines) >= 1 and lines[0]:
            base = lines[0]
        if len(lines) >= 2 and lines[1]:
            model = lines[1]
    return base, model


def save_llm_config(base: str, model: str):
    LLM_CFG_FILE.write_text(f"{base}\n{model}\n", encoding="utf-8")


def _b64_to_data_url(b64: str) -> str:
    """Đoán mime từ magic bytes rồi ghép data URL (LLM cần prefix data:image/...)."""
    s = (b64 or "").lstrip()
    head = s[:5]
    if head.startswith("/9j/"):
        mime = "image/jpeg"
    elif head.startswith("iVBOR"):
        mime = "image/png"
    elif head.startswith("R0lGOD"):
        mime = "image/gif"
    elif head.startswith("UklGR"):
        mime = "image/webp"
    else:
        mime = "image/png"
    return f"data:{mime};base64,{s}"


def _stitch_rotate_composite(outer_b64: str, inner_b64: str) -> str:
    """Ghép ảnh ROTATE TikTok (outer ring + inner) thành 1 ảnh PNG base64.

    ROTATE v1: outer = vòng ring có khía/vết cắt; inner = ảnh tròn cần xoay sao
    cho khía của nó khớp vết cắt trên ring. LLM yếu (flash) xử lý 2 ảnh RIÊNG lẻ
    thì không nhìn được mối quan hệ tương đối → chỉ đoán bừa góc "tròn" (90/45).
    Ghép side-by-side + overlay inner LÊN giữa outer để LLM thấy 1 bức ảnh duy
    nhất, dễ align. Trả PNG base64 (format mà mọi LLM vision đều đọc tốt nhất).
    """
    import base64, io
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return ""   # thiếu Pillow → caller fallback gửi 2 ảnh riêng như cũ
    try:
        outer = Image.open(io.BytesIO(base64.b64decode(outer_b64))).convert("RGBA")
        inner = Image.open(io.BytesIO(base64.b64decode(inner_b64))).convert("RGBA")
    except Exception:
        return ""

    # Layout: [outer có inner overlay ở giữa] | [inner gốc] — LLM nhìn cả 2 cùng lúc.
    ow, oh = outer.size
    iw, ih = inner.size
    inner_on_outer = outer.copy()
    # Overlay inner thu nhỏ vào tâm outer (không che ring) để thấy lệch.
    scale = min(ow, oh) * 0.55 / max(iw, ih)
    small = inner.resize((max(1, int(iw * scale)), max(1, int(ih * scale))))
    cx, cy = (ow - small.width) // 2, (oh - small.height) // 2
    # Vẽ vòng tròn outline quanh inner overlay làm điểm tham chiếu.
    draw = ImageDraw.Draw(inner_on_outer)
    draw.ellipse([cx, cy, cx + small.width, cy + small.height],
                 outline=(255, 0, 0, 255), width=3)
    inner_on_outer.alpha_composite(small, (cx, cy))

    # Ghép ngang: [overlay] | [inner gốc] với 16px gap + nhãn.
    gap = 16
    w = ow + gap + iw
    h = max(oh, ih)
    canvas = Image.new("RGB", (w, h), (24, 24, 24))
    canvas.paste(inner_on_outer.convert("RGB"), (0, (h - oh) // 2))
    canvas.paste(inner.convert("RGB"), (ow + gap, (h - ih) // 2))

    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


class LLMApiClient:
    """ApiClient-compatible: thay vì gọi SadCaptcha, gọi LLM vision giải CAPTCHA.

    4 method puzzle/rotate/shapes/icon có cùng chữ ký + kiểu trả về như ApiClient
    gốc (trả các *CaptchaResponse) để SeleniumSolver dùng được mà không sửa.
    """

    def __init__(self, api_key: str, base_url: str = DEFAULT_LLM_BASE,
                 model: str = DEFAULT_LLM_MODEL, timeout: int = 120, log_fn=None):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        # log_fn: in ra UI log của tool. Mặc định silent để không phá chỗ gọi ngoài GUI.
        self._log = log_fn or (lambda msg: None)

    def _dump_image(self, kind: str, b64: str) -> str:
        """Lưu ảnh captcha gửi cho LLM ra disk để debug 'giải sai'.

        Việc biết ảnh LLM THỰC SỰ nhận giúp phân biệt: ảnh hỏng/méo (vấn đề fetch)
        vs LLM nhìn đúng nhưng trả sai (vấn đề model/prompt). Trả tên file đã lưu.
        """
        try:
            import base64
            d = Path(__file__).parent / "logs" / "captcha_debug"
            d.mkdir(parents=True, exist_ok=True)
            ts = time.strftime("%H%M%S")
            raw = base64.b64decode(b64)
            # Đoán đuôi file từ magic bytes
            if raw[:3] == b"\xff\xd8\xff":
                ext = "jpg"
            elif raw[:8] == b"\x89PNG\r\n\x1a\n":
                ext = "png"
            elif raw[:6] in (b"GIF87a", b"GIF89a"):
                ext = "gif"
            else:
                ext = "png"
            fp = d / f"{ts}_{kind}.{ext}"
            fp.write_bytes(raw)
            return fp.name
        except Exception:
            return ""

    # ── Lõi: gọi chat completion với ảnh → trả text content ─────────────────
    def _chat(self, prompt: str, images_b64: list[str], max_tokens: int = 16000) -> str:
        import requests
        content = [{"type": "text", "text": prompt}]
        for b64 in images_b64:
            content.append({"type": "image_url",
                            "image_url": {"url": _b64_to_data_url(b64)}})
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": max_tokens,
            "temperature": 0.2,
        }
        # Reasoning model (kimi/gemini-3/o1) "nghĩ thầm" hàng nghìn token TRƯỚC khi
        # ra content. max_tokens áp dụng cho reasoning+content gộp chung → nếu quá
        # nhỏ, reasoning bị cắt giữa chừng (finish_reason=length), content rỗng →
        # solver không có JSON → fail. Phải cho max_tokens đủ lớn (~16k) để reasoning
        # kết thúc tự nhiên rồi model mới kịp viết content JSON.
        # reasoning_effort=low giúp bớt lan man. Proxy không hỗ trợ → 400, retry bỏ.
        _use_reasoning_effort = any(
            k in self.model for k in ("gemini-3", "gemini-2.5", "o1", "o3", "o4",
                                      "kimi", "k2"))
        if _use_reasoning_effort and getattr(self, "_rej_reasoning_effort", False):
            _use_reasoning_effort = False
        if _use_reasoning_effort:
            payload["reasoning_effort"] = "low"
        r = requests.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"},
            json=payload, timeout=self.timeout,
        )
        # Nếu 400 do reasoning_effort không được hỗ trợ → cache + retry không field
        if r.status_code == 400 and _use_reasoning_effort and \
                "reasoning_effort" in r.text:
            self._rej_reasoning_effort = True
            payload.pop("reasoning_effort", None)
            r = requests.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Content-Type": "application/json"},
                json=payload, timeout=self.timeout,
            )
        if r.status_code != 200:
            self._log(f"[captcha] LLM HTTP {r.status_code}: {r.text[:160]}")
            raise RuntimeError(f"LLM HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
        msg = data["choices"][0]["message"]
        finish = data["choices"][0].get("finish_reason", "")
        text = msg.get("content") or ""
        reasoning = msg.get("reasoning_content") or ""
        if not text and not reasoning:
            raise RuntimeError("LLM trả response rỗng")
        # Cảnh báo khi reasoning bị cắt giữa chừng (content rỗng + finish=length):
        # _extract_json sẽ không tìm thấy JSON → solver fail. Log rõ để user biết
        # nguyên nhân là model lan man, không phải bug code.
        if not text and finish == "length":
            self._log(f"[captcha] ⚠️ Reasoning bị cắt ({len(reasoning)} ký tự, "
                      f"finish=length) — model lan man, thử lại...")
        self._log(f"[captcha] LLM trả (content={len(text)} ký tự, "
                  f"reasoning={len(reasoning)} ký tự, finish={finish})")
        return text + "\n" + reasoning

    @staticmethod
    def _extract_json(text: str) -> dict:
        """Tìm object JSON ĐÁP ÁN trong text (LLM đôi khi kèm reasoning dài).

        Reasoning model (kimi) thường viết JSON VÍ DỤ sai ở giữa chuỗi suy nghĩ rồi
        JSON đáp án thật ở cuối → lấy "JSON đầu tiên hợp lệ" sẽ trúng ví dụ sai.
        Chiến lược theo thứ tự ưu tiên:
          1) ```json {...}```  — code fence, tin cậy nhất (model chủ ý đánh dấu)
          2) {...} non-greedy, thử từ CUỐI lên ĐẦU: đáp án thật thường nằm cuối
             reasoning; JSON ví dụ sai nằm giữa. Ưu tiên match ngắn (1 dòng).
          3) match có nesting 1 lớp (icon/shapes chứa list point).
        """
        import json, re
        text = text.strip()
        # 1) Code fence ```json {...}``` — lấy cái cuối cùng (nhiều fence thì cái
        #    cuối thường là đáp án đã chốt, cái đầu là ví dụ minh họa).
        fence = list(re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S))
        for m in reversed(fence):
            try:
                return json.loads(m.group(1))
            except Exception:
                continue
        # 2) Thu thập tất cả match {...} (không nest + nest 1 lớp), thử parse,
        #    giữ lại những cái hợp lệ. Ưu tiên match CUỐI CÙNG hợp lệ.
        candidates = []
        for pat in (r"\{[^{}]*\}",                   # không nest
                    r"\{[^{}]*\{[^{}]*\}[^{}]*\}"):  # nest 1 lớp (proportionalPoints)
            for m in re.finditer(pat, text, re.S):
                try:
                    candidates.append((m.start(), json.loads(m.group(0))))
                except Exception:
                    continue
        if candidates:
            # Sort theo vị trí giảm dần → lấy match hợp lệ cuối cùng trong text.
            candidates.sort(key=lambda x: x[0], reverse=True)
            return candidates[0][1]
        raise ValueError(f"không tìm thấy JSON hợp lệ trong {len(text)} ký tự")

    # ── 4 loại CAPTCHA (cùng giao diện với ApiClient gốc) ───────────────────
    def puzzle(self, puzzle_b64: str, piece_b64: str):
        from tiktok_captcha_solver.models import PuzzleCaptchaResponse
        f1 = self._dump_image("puzzle_bg", puzzle_b64)
        f2 = self._dump_image("puzzle_piece", piece_b64)
        self._log(f"[captcha] Loại PUZZLE. Ảnh đã lưu: bg={f1} piece={f2}")
        prompt = (
            "This is a slide-puzzle. Image 1 is the background "
            "with a notch/gap cut out. Image 2 is the puzzle piece that must slide "
            "HORIZONTALLY from the left edge into that gap. Estimate the horizontal "
            "position of the gap as a PROPORTION of image 1's width "
            "(0.0 = far left, 1.0 = far right). Reply ONLY with JSON: "
            '{"slide_x_proportion": <number>}. No explanation.'
        )
        res = self._extract_json(self._chat(prompt, [puzzle_b64, piece_b64]))
        val = float(res["slide_x_proportion"])
        self._log(f"[captcha] PUZZLE → slide_x_proportion={val}")
        return PuzzleCaptchaResponse(slide_x_proportion=val)

    def rotate(self, outer_b64: str, inner_b64: str):
        from tiktok_captcha_solver.models import RotateCaptchaResponse
        f1 = self._dump_image("rotate_outer", outer_b64)
        f2 = self._dump_image("rotate_inner", inner_b64)
        # Ghép outer+inner thành 1 ảnh để LLM nhìn được quan hệ tương đối giữa
        # khía trên ring và vết trên inner (lỗi cũ: gửi 2 ảnh riêng → flash chỉ
        # đoán góc "tròn" 90/45/270). Fallback 2 ảnh riêng nếu Pillow thiếu.
        composite = _stitch_rotate_composite(outer_b64, inner_b64)
        if composite:
            fc = self._dump_image("rotate_composite", composite)
            self._log(f"[captcha] Loại ROTATE (composite). Ảnh: {fc} (outer={f1} inner={f2})")
            images = [composite]
            img_desc = (
                "the single combined image below. LEFT half: the OUTER ring (a "
                "circular frame with notches/cutouts around its edge) with the INNER "
                "image overlaid in its CENTER inside a RED circle. RIGHT half: the "
                "INNER image alone. Compare the notches on the outer ring against the "
                "pattern/ridges on the inner image"
            )
        else:
            self._log(f"[captcha] Loại ROTATE (2 ảnh). outer={f1} inner={f2} (không ghép được)")
            images = [outer_b64, inner_b64]
            img_desc = (
                "Image 1: the OUTER ring (a circular frame with notches/cutouts). "
                "Image 2: the INNER image. Compare the notches on the ring against "
                "the pattern/ridges on the inner image"
            )
        # Fix A: yêu cầu số THẬP PHÂN + cấm làm tròn về bội 45/90 — flash bias
        # snap về cardinal angles khi được phép trả integer.
        prompt = (
            f"This is a rotate puzzle challenge. You see {img_desc}. "
            "The INNER image must be ROTATED CLOCKWISE so its pattern aligns with "
            "the notches on the outer ring. Estimate the exact rotation angle in "
            "degrees as a DECIMAL number in the range 0.0 to 360.0. "
            "IMPORTANT: Do NOT round to multiples of 45 or 90. Give a precise value "
            "like 137.5, 23.0, or 311.5 — the alignment is rarely an exact cardinal "
            "angle. Reply ONLY with JSON: {\"angle\": <decimal>}. No explanation."
        )
        res = self._extract_json(self._chat(prompt, images))
        angle = int(round(float(res["angle"])))
        self._log(f"[captcha] ROTATE → angle={angle} (raw={res.get('angle')})")
        return RotateCaptchaResponse(angle=angle)

    def shapes(self, image_b64: str):
        from tiktok_captcha_solver.models import ShapesCaptchaResponse
        f = self._dump_image("shapes", image_b64)
        self._log(f"[captcha] Loại SHAPES. Ảnh đã lưu: {f}")
        prompt = (
            "This is a 'shapes' puzzle challenge. There are exactly TWO matching points "
            "to click on the image. Give their positions as PROPORTIONS of the image "
            "width/height (0.0-1.0). Reply ONLY with JSON: "
            '{"point_one_proportion_x": <n>, "point_one_proportion_y": <n>, '
            '"point_two_proportion_x": <n>, "point_two_proportion_y": <n>}. '
            "No explanation."
        )
        res = self._extract_json(self._chat(prompt, [image_b64]))
        self._log(f"[captcha] SHAPES → raw={res}")
        return ShapesCaptchaResponse(
            point_one_proportion_x=float(res["point_one_proportion_x"]),
            point_one_proportion_y=float(res["point_one_proportion_y"]),
            point_two_proportion_x=float(res["point_two_proportion_x"]),
            point_two_proportion_y=float(res["point_two_proportion_y"]),
        )

    def icon(self, challenge_text: str, image_b64: str):
        from tiktok_captcha_solver.models import IconCaptchaResponse, ProportionalPoint
        f = self._dump_image("icon", image_b64)
        # challenge_text có thể rỗng khi selector .captcha_verify_bar trả "" —
        # LLM không biết phải click gì → giải bừa. Log rõ để phát hiện.
        self._log(f"[captcha] Loại ICON. challenge='{challenge_text}' ảnh={f}")
        if not challenge_text or not challenge_text.strip():
            self._log("[captcha] ⚠️ challenge_text RỖNG — LLM không biết tìm gì, sẽ giải bừa.")
        prompt = (
            f"This is a icon puzzle challenge. The challenge is: \"{challenge_text}\". "
            "Click EVERY object in the image that matches the challenge. Give each "
            "click position as PROPORTIONS of image width/height (0.0-1.0). Reply ONLY "
            'with JSON: {"proportionalPoints": [{"proportionX": <n>, "proportionY": <n>}, ...]}. '
            "No explanation."
        )
        res = self._extract_json(self._chat(prompt, [image_b64]))
        self._log(f"[captcha] ICON → raw={res}")
        resp = IconCaptchaResponse(proportional_points=[])
        for point in res.get("proportionalPoints", []):
            resp.proportional_points.append(ProportionalPoint(
                proportion_x=float(point["proportionX"]),
                proportion_y=float(point["proportionY"]),
            ))
        return resp


def has_captcha_key() -> bool:
    """True nếu provider hiện tại (llm/omo) có key → bật được tự giải."""
    return bool(load_omo_key()) if load_cap_provider() == "omo" else bool(load_llm_key())


# ── CAPTCHA detect + solve (luồng chạy chính: cookie login → like/comment) ───
# Selector khớp TIKTOK HIỆN TẠI: container captcha nằm thẳng trong DOM chính
# (captcha-verify-container / captcha-disable-scroll — đúng bộ selector mà
# chính thư viện tiktok-captcha-solver dùng để detect), cộng bộ cũ dự phòng.
# Bộ selector RỘNG: TikTok có nhiều biến thể container captcha (v1/v2, iframe
# verification-va, id/class khác nhau theo phiên bản trang). Bỏ sót 1 biến thể
# = tool không biết đang có captcha → báo nhầm 'không có nút comment'.
# An toàn false-positive nhờ is_displayed() trong captcha_present().
CAPTCHA_SEL = ("div.captcha-verify-container, .captcha-disable-scroll, "
               "iframe[src*='captcha'], iframe[src*='verify'], "
               "[class*='captcha_verify'], [id='captcha-verify'], "
               "[class*='secsdk-captcha'], [id*='captcha'], "
               "[class*='captcha-container'], [id*='verify']:not([id*='verified']), "
               "#tiktok-verify-ele, #captcha-verify-container-main-page, "
               "#captcha_slide_button, #captcha_close_button")
CAPTCHA_CHALLENGE_SEL = (
    ".captcha-disable-scroll [data-testid='whirl-inner-img'], "
    ".captcha-verify-container > div > div > div > img.cap-absolute, "
    ".captcha-disable-scroll img.captcha_verify_img_slide, "
    ".captcha-verify-container #captcha-verify-image, "
    ".captcha-disable-scroll .verify-captcha-submit-button, "
    ".captcha-verify-container .cap-relative button.cap-w-full")
# Serialize mọi lượt giải: solver điều khiển chuột thật trên driver —
# 2 luồng giải cùng lúc sẽ click nhầm browser của nhau.
_captcha_solve_lock = threading.Lock()

# Cache comment theo (aweme_id, target_user) — chia sẻ mọi bot trong process.
# Acc đầu quét xong lưu lại; acc sau bỏ qua API/harvest (~tiết kiệm 30-60s/acc).
# Sống SUỐT BUỔI CHẠY (runner clear khi bắt đầu run mới); TTL 6h chỉ là lưới
# an toàn nếu app treo nhiều ngày không restart.
_COMMENT_CACHE: dict = {}
_COMMENT_CACHE_TTL = 6 * 3600
# Lock theo key: nhiều acc vào CÙNG lúc (cache còn rỗng) từng cùng harvest
# song song — 5 browser cuộn 1 post (chậm + đáng ngờ). Acc đầu giữ lock quét,
# acc sau ĐỢI rồi nhận cache xong.
_COMMENT_CACHE_LOCKS: dict = {}
_cc_locks_guard = threading.Lock()
_VIP_ASK_CACHE: dict = {}
_VIP_ASK_CACHE_TTL = 30 * 60


def _comment_cache_lock(key) -> threading.Lock:
    with _cc_locks_guard:
        return _COMMENT_CACHE_LOCKS.setdefault(key, threading.Lock())

def clear_comment_cache() -> None:
    """Xóa CID đã harvest trước một lượt chạy mới."""
    with _cc_locks_guard:
        _COMMENT_CACHE.clear()
        _COMMENT_CACHE_LOCKS.clear()


def merge_comment_cache(video_id: str, username: str,
                        comments: list[dict]) -> dict:
    """Gộp CID từ nhiều scout vào một cache chung, không làm mất record cũ."""
    key = (str(video_id or ""), _norm_handle(username))
    if not key[0] or not key[1]:
        return {"total": 0, "added": 0, "rounds": 0}
    lock = _comment_cache_lock(key)
    with lock:
        previous = _COMMENT_CACHE.get(key) or {}
        merged = {}
        for row in list(previous.get("comments") or []) + list(comments or []):
            cid = str(row.get("cid") or "")
            if not cid:
                continue
            current = merged.get(cid)
            if current is None:
                merged[cid] = dict(row)
                continue
            for field in (
                    "parent_cid", "text", "username", "position",
                    "scroll_top", "root"):
                value = row.get(field)
                if value not in (None, ""):
                    current[field] = value
            if row.get("already_liked"):
                current["already_liked"] = True
        old_total = len({
            str(row.get("cid") or "")
            for row in previous.get("comments") or []
            if row.get("cid")
        })
        rounds = int(previous.get("rounds") or 0) + 1
        rows = list(merged.values())
        _COMMENT_CACHE[key] = {
            "ts": time.time(),
            "comments": rows,
            "rounds": rounds,
            "added": max(0, len(rows) - old_total),
        }
        return {
            "total": len(rows),
            "added": max(0, len(rows) - old_total),
            "rounds": rounds,
        }


def import_comment_handoff_cache(data: dict) -> int:
    """Nạp JSON Auto Cmt đã validate vào cache tìm comment của Auto tim."""
    loaded = 0
    now = time.time()
    for video in list((data or {}).get("videos") or []):
        link = str(video.get("video_url") or "")
        video_id = str(video.get("video_id") or "") or extract_aweme_id(link)
        if not video_id:
            continue
        grouped = {}
        for item in list(video.get("comments") or []):
            text = " ".join(str(item.get("text") or "").split())
            if not text:
                continue
            username = _norm_handle(item.get("username") or "") or "_auto_cmt"
            grouped.setdefault(username, []).append({
                "cid": "",
                "text": text,
                "username": username,
                "position": max(0, int(item.get("position") or 0)),
                "scroll_top": max(0, int(item.get("scroll_top") or 0)),
                "root": str(item.get("root") or ""),
            })
        for username, comments in grouped.items():
            _COMMENT_CACHE[(video_id, username)] = {
                "ts": now,
                "comments": comments,
                "rounds": 1,
                "added": len(comments),
            }
            loaded += len(comments)
    return loaded


_TAB_CLICK_JS = """
    const bar = document.querySelector("[data-testid='tux-web-tab-bar']");
    const nodes = bar
      ? [...bar.querySelectorAll('[role="tab"],button,div,span,p')]
      : [...document.querySelectorAll("[data-testid='tux-web-tab-bar']")];
    for (const t of nodes) {
        const txt = (t.innerText || t.textContent || '').trim();
        if (txt.length > 24) continue;
        if (/^(b[ìi]nh lu[ậa]n|comments?)$/i.test(txt)) {
            t.click();
            return true;
        }
    }
    return false;
"""


def click_comment_tab(driver) -> bool:
    """Click tab 'Bình luận/Comment' trong tab-bar UI mới. Trả True nếu click."""
    try:
        return bool(driver.execute_script(_TAB_CLICK_JS))
    except Exception:
        return False


def captcha_present(driver) -> bool:
    """True nếu captcha TikTok đang trong DOM và không display:none.

    KHÔNG dùng Selenium is_displayed(): cửa sổ off-screen/occluded trả False
    dù captcha đang hiện → skip 2–3 challenge rồi mới giải. Check CSS
    display/visibility/size — khớp headless giả.
    """
    try:
        return bool(driver.execute_script("""
            const sel = arguments[0];
            const els = document.querySelectorAll(sel);
            for (const el of els) {
                const st = getComputedStyle(el);
                if (st.display === 'none' || st.visibility === 'hidden') continue;
                if (parseFloat(st.opacity || '1') === 0) continue;
                const r = el.getBoundingClientRect();
                if (r.width >= 8 && r.height >= 8) return true;
                if (el.querySelector('canvas, img, iframe')) return true;
            }
            return false;
        """, CAPTCHA_SEL))
    except Exception:
        try:
            return bool(driver.find_elements(By.CSS_SELECTOR, CAPTCHA_SEL))
        except Exception:
            return False
def _captcha_challenge_signature(driver) -> str:
    """Fingerprint a visible challenge the installed Selenium solver supports."""
    try:
        return str(driver.execute_script("""
            const nodes = [...document.querySelectorAll(arguments[0])];
            const visible = nodes.filter((el) => {
                const st = getComputedStyle(el);
                if (st.display === 'none' || st.visibility === 'hidden') return false;
                if (parseFloat(st.opacity || '1') === 0) return false;
                const r = el.getBoundingClientRect();
                return r.width >= 8 && r.height >= 8;
            });
            return visible.map((el) => [
                el.tagName,
                el.getAttribute('data-testid') || '',
                el.getAttribute('src') || '',
                Math.round(el.getBoundingClientRect().width),
                Math.round(el.getBoundingClientRect().height)
            ].join('|')).join('::');
        """, CAPTCHA_CHALLENGE_SEL) or "")
    except Exception:
        return ""


def _wait_captcha_transition(driver, timeout: float = 4.0) -> tuple[str, str]:
    """Return (gone|ready|stalled, signature) after TikTok updates its modal."""
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        if not captcha_present(driver):
            return "gone", ""
        signature = _captcha_challenge_signature(driver)
        if signature:
            return "ready", signature
        if time.monotonic() >= deadline:
            return "stalled", ""
        time.sleep(0.25)




_PRESS_HOLD_TARGET_JS = r"""
const phrases = /press\s*(?:and|&)\s*hold|please\s*wait|nhấn\s+và\s+giữ|ấn\s+và\s+giữ/i;
const roots = [document];
const nodes = [];
for (let index = 0; index < roots.length; index += 1) {
  const root = roots[index];
  for (const element of root.querySelectorAll('*')) {
    nodes.push(element);
    if (element.shadowRoot) roots.push(element.shadowRoot);
  }
}
let best = null;
let bestScore = -1;
for (const element of nodes) {
  const text = [
    element.innerText,
    element.textContent,
    element.getAttribute && element.getAttribute('aria-label'),
    element.getAttribute && element.getAttribute('title'),
    element.value
  ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
  const marker = [
    element.id,
    typeof element.className === 'string' ? element.className : '',
    element.getAttribute && element.getAttribute('role')
  ].filter(Boolean).join(' ');
  if (!phrases.test(text) && !/press.?hold|verify.?button/i.test(marker)) continue;
  const rect = element.getBoundingClientRect();
  const style = getComputedStyle(element);
  if (rect.width < 40 || rect.height < 16 ||
      style.display === 'none' || style.visibility === 'hidden' ||
      Number(style.opacity || 1) === 0) continue;
  const interactive = element.matches(
    'button, [role="button"], input[type="button"], input[type="submit"], ' +
    '[draggable="true"], [class*="button"], [class*="Button"], ' +
    '[class*="slider"], [class*="Slider"]'
  );
  let score = interactive ? 200 : 0;
  if (/please\s*wait/i.test(text)) score += 80;
  if (/press\s*(?:and|&)\s*hold|nhấn\s+và\s+giữ|ấn\s+và\s+giữ/i.test(text)) {
    score += 60;
  }
  if (/press.?hold|verify.?button/i.test(marker)) score += 50;
  if (rect.height > 140 || rect.width > 700) score -= 100;
  if (score > bestScore) {
    best = element;
    bestScore = score;
  }
}
return best;
"""


def solve_press_and_hold_captcha(
        driver, stop_event=None, hold_seconds: float = 8.0) -> bool:
    """Solve TikTok's iframe/shadow-DOM press-and-hold verification."""
    switch = driver.switch_to
    switch.default_content()

    def find_target(depth: int = 0):
        try:
            target = driver.execute_script(_PRESS_HOLD_TARGET_JS)
            if target is not None:
                return target
        except Exception:
            pass
        if depth >= 3:
            return None
        try:
            frames = driver.find_elements(By.CSS_SELECTOR, "iframe")
        except Exception:
            frames = []
        for frame in frames:
            try:
                switch.frame(frame)
                target = find_target(depth + 1)
                if target is not None:
                    return target
            except Exception:
                pass
            try:
                switch.parent_frame()
            except Exception:
                switch.default_content()
        return None

    target = find_target()
    if target is None:
        switch.default_content()
        return False
    held = False
    try:
        driver.execute_script(
            "arguments[0].scrollIntoView({block:'center',inline:'center'});",
            target)
        ActionChains(driver).move_to_element(target).pause(
            random.uniform(0.35, 0.7)).click_and_hold().perform()
        held = True
        deadline = time.monotonic() + max(1.0, float(hold_seconds))
        while time.monotonic() < deadline:
            if stop_event is not None and stop_event.is_set():
                break
            time.sleep(0.2)
    finally:
        if held:
            try:
                ActionChains(driver).release().perform()
            except Exception:
                pass
        switch.default_content()
    return True


def _stub_foreign_solver_engines():
    """Cắm stub 'nodriver' + 'playwright' vào sys.modules TRƯỚC khi import
    tiktok_captcha_solver — CHỈ trên exe (Nuitka).

    __init__ của package import cả launcher/playwrightsolver/asyncplaywrightsolver
    → kéo nodriver + playwright, hai engine ta KHÔNG dùng (chỉ dùng SeleniumSolver).
    Trên exe, module nodriver/cdp/network.py (200KB) fail 'SyntaxError: encoding
    problem: utf-8' khi load → nguyên nhân 'Có CAPTCHA nhưng không có solver'.
    sys.modules được check ĐẦU TIÊN trong import → stub chặn arm import thật.
    Máy dev (chạy python) không stub — giữ behavior gốc đầy đủ.
    """
    import sys
    if not _is_frozen_build():
        return
    import types

    def _mk(name):
        m = types.ModuleType(name)
        # __getattr__ module-level (PEP 562): mọi attribute chưa định nghĩa
        # (vd nodriver.Browser trong annotation của launcher.py) trả dummy —
        # stub chỉ cần cho import chạy qua, không bao giờ được gọi thật.
        m.__getattr__ = lambda attr: type(attr, (), {})
        sys.modules[name] = m
        return m

    if "nodriver" not in sys.modules:
        _mk("nodriver")
        _mk("nodriver.cdp")
    if "playwright" not in sys.modules:
        pw = _mk("playwright")
        sync_api = _mk("playwright.sync_api")
        async_api = _mk("playwright.async_api")
        # Tên mà playwrightsolver/asyncplaywrightsolver from-import ở top-level.
        for api in (sync_api, async_api):
            api.FloatRect = object
            api.Page = object
            api.expect = None
            api.TimeoutError = TimeoutError
        sync_api.sync_playwright = None
        async_api.async_playwright = None
        pw.sync_api = sync_api
        pw.async_api = async_api


def _is_frozen_build() -> bool:
    """True khi chạy từ .exe Nuitka (có __compiled__ ở module main)."""
    import sys
    if getattr(sys, "frozen", False):
        return True
    main_mod = sys.modules.get("__main__")
    return main_mod is not None and hasattr(main_mod, "__compiled__")


def make_captcha_solver(driver, _unused_key: str = "", log_fn=None):
    """Tạo SeleniumSolver. Client tùy provider: 'omo' → OmocaptchaClient,
    'llm' (mặc định) → LLMApiClient. Trả None nếu thiếu key/thư viện.

    log_fn: hàm ghi log ra UI — truyền self.log của bot để client in được loại
    captcha + kết quả trả về, giúp chẩn đoán 'giải sai'.
    """
    _stub_foreign_solver_engines()

    def _why(msg):
        # Ghi traceback đầy đủ ra file — log UI cắt ngắn, file giữ nguyên vẹn
        # để chẩn đoán chính xác file/dòng gây SyntaxError trên exe.
        import traceback
        try:
            from pathlib import Path as _P
            d = _P(__file__).parent / "logs"
            d.mkdir(exist_ok=True)
            (d / "solver_error.txt").write_text(
                f"{msg}\n---\n{traceback.format_exc()}", encoding="utf-8")
        except Exception:
            pass
        if log_fn:
            log_fn(f"[captcha] ❌ Tạo solver fail: {msg}")

    if load_cap_provider() == "omo":
        omo_key = load_omo_key()
        if omo_key:
            try:
                from tiktok_captcha_solver import SeleniumSolver  # type: ignore
                solver = SeleniumSolver(driver, "omo", mouse_step_size=2)
                solver.client = OmocaptchaClient(omo_key, log_fn=log_fn)
                return solver
            except Exception as e:
                _why(f"omo path: {type(e).__name__}: {str(e)[:100]}")
        # Fallback chéo: cấu hình provider=omo nhưng thiếu omo key (vd web
        # admin đổi provider mà quên key) → dùng LLM key nếu có, đừng bỏ trắng.
        llm_key = load_llm_key()
        if llm_key:
            try:
                from tiktok_captcha_solver import SeleniumSolver  # type: ignore
                base, model = load_llm_config()
                s = SeleniumSolver(driver, "llm", mouse_step_size=2)
                s.client = LLMApiClient(llm_key, base, model, log_fn=log_fn)
                if log_fn:
                    log_fn("[captcha] ⚠️ Thiếu omo key — fallback sang LLM.")
                return s
            except Exception as e:
                _why(f"llm fallback: {type(e).__name__}: {str(e)[:100]}")
        if not omo_key and not llm_key:
            _why("KHÔNG có key nào (omo + llm đều rỗng)")
        return None
    # provider=llm (mặc định) — thiếu LLM key thì fallback chéo sang omo key
    # (trường hợp web admin để Bộ giải=LLM mà chỉ có omo key — từng gây
    # 'Có CAPTCHA nhưng không có solver' bỏ captcha trắng).
    key = load_llm_key()
    if not key:
        omo_key = load_omo_key()
        if omo_key:
            try:
                from tiktok_captcha_solver import SeleniumSolver  # type: ignore
                solver = SeleniumSolver(driver, "omo", mouse_step_size=2)
                solver.client = OmocaptchaClient(omo_key, log_fn=log_fn)
                if log_fn:
                    log_fn("[captcha] ⚠️ Thiếu LLM key — fallback sang Omocaptcha.")
                return solver
            except Exception as e:
                _why(f"omo fallback: {type(e).__name__}: {str(e)[:100]}")
        _why("provider=llm nhưng LLM key rỗng và omo key cũng rỗng/thất bại")
        return None
    try:
        from tiktok_captcha_solver import SeleniumSolver  # type: ignore
        base, model = load_llm_config()
        solver = SeleniumSolver(driver, "llm", mouse_step_size=2)
        solver.client = LLMApiClient(key, base, model, log_fn=log_fn)
        return solver
    except Exception as e:
        _why(f"llm path: {type(e).__name__}: {str(e)[:100]}")
        return None


def load_proxies() -> str:
    if PROXIES_FILE.exists():
        return PROXIES_FILE.read_text(encoding="utf-8")
    return ""


def save_proxies(text: str):
    PROXIES_FILE.write_text(text, encoding="utf-8")


def _norm_scheme(scheme: str) -> str:
    """Chuẩn hóa scheme về 'http' hoặc 'socks5' (gộp socks4/socks5h → socks5)."""
    s = (scheme or "http").lower()
    if s.startswith("socks"):
        return "socks5"
    return "http"


def parse_proxy(raw: str, default_scheme: str = "http"):
    """
    Hỗ trợ các định dạng:
      ip:port
      ip:port:user:pass
      socks5://ip:port
      socks5://user:pass@ip:port
      http://user:pass@ip:port
      https://user:pass@ip:port
    default_scheme: dùng cho dòng KHÔNG có tiền tố (do dropdown UI quyết định).
    Trả về dict: {scheme, host, port, user, password} hoặc None nếu trống/sai.
    """
    raw = raw.strip()
    if not raw:
        return None
    # Có scheme (http:// socks5:// ...)
    if "://" in raw:
        p = urlparse(raw)
        return {
            "scheme": _norm_scheme(p.scheme),
            "host": p.hostname or "",
            "port": p.port or 80,
            "user": unquote(p.username or ""),
            "password": unquote(p.password or ""),
        }
    scheme = _norm_scheme(default_scheme)
    # Dạng ip:port hoặc ip:port:user:pass
    parts = raw.split(":")
    if len(parts) == 2:
        return {"scheme": scheme, "host": parts[0], "port": int(parts[1]),
                "user": "", "password": ""}
    if len(parts) == 4:
        return {"scheme": scheme, "host": parts[0], "port": int(parts[1]),
                "user": parts[2], "password": parts[3]}
    return None


_PROXY_BL_TTL = 60  # Temporary retry delay for a failed authenticated proxy session.
_proxy_bl: dict[str, float] = {}
_proxy_bl_lock = threading.Lock()
_proxy_rr = 0


def _proxy_key(p: dict | None) -> str:
    if not p:
        return ""
    import hashlib
    identity = [str(p.get(k) or '') for k in ('scheme', 'host', 'port', 'user', 'password')]
    return hashlib.sha256(json.dumps(identity).encode('utf-8')).hexdigest()


def blacklist_proxy(p: dict | None, ttl: float = _PROXY_BL_TTL) -> None:
    k = _proxy_key(p)
    if not k:
        return
    with _proxy_bl_lock:
        _proxy_bl[k] = time.time() + ttl


def _proxy_blocked(p: dict | None) -> bool:
    k = _proxy_key(p)
    if not k:
        return False
    with _proxy_bl_lock:
        until = _proxy_bl.get(k, 0)
        if until <= time.time():
            _proxy_bl.pop(k, None)
            return False
        return True

def clear_proxy_blacklist(raw_lines: list[str]) -> None:
    """Bỏ cooldown cục bộ chỉ cho endpoint vừa được provider đổi IP."""
    keys = set()
    for raw in raw_lines:
        try:
            proxy = parse_proxy(raw)
        except (TypeError, ValueError):
            proxy = None
        key = _proxy_key(proxy)
        if key:
            keys.add(key)
    if not keys:
        return
    with _proxy_bl_lock:
        for key in keys:
            _proxy_bl.pop(key, None)


def pick_proxy(raw_lines: list[str], default_scheme: str = "http",
               avoid: set | None = None) -> dict | None:
    """Proxy chua blacklist. Round-robin toan cuc."""
    global _proxy_rr
    parsed = []
    for raw in raw_lines:
        p = parse_proxy(raw, default_scheme)
        if p:
            parsed.append(p)
    if not parsed:
        return None
    avoid = avoid or set()
    n = len(parsed)
    with _proxy_bl_lock:
        start = _proxy_rr
        _proxy_rr += 1
    for i in range(n):
        p = parsed[(start + i) % n]
        k = _proxy_key(p)
        if k in avoid:
            continue
        if not _proxy_blocked(p):
            return p
    return None


def detect_chrome_major(chrome_path: str) -> int | None:
    """Đọc version chính (major) của Chrome từ metadata file trên Windows.

    undetected_chromedriver hay cảnh báo 'could not detect version_main' khi
    không tự dò được → có thể tải nhầm driver. Truyền major vào uc.Chrome()
    để khớp đúng driver và tắt cảnh báo.
    """
    if not chrome_path or not Path(chrome_path).exists():
        return None
    # macOS: run the binary with --version
    if sys.platform == "darwin":
        try:
            completed = subprocess.run(
                [chrome_path, "--version"],
                capture_output=True, text=True, timeout=5,
            )
            match = re.search(r"(\d+)\.\d+\.\d+", completed.stdout or "")
            if match:
                return int(match.group(1))
        except Exception:
            pass
        return None
    # Cách 1: Windows file version qua win32
    try:
        import win32api  # type: ignore
        info = win32api.GetFileVersionInfo(chrome_path, "\\")
        ms = info["FileVersionMS"]
        return ms >> 16
    except Exception:
        pass
    # Cách 2: thư mục con dạng "124.0.6367.60" cạnh chrome.exe
    try:
        parent = Path(chrome_path).parent
        for child in parent.iterdir():
            name = child.name
            if child.is_dir() and name[:3].isdigit() and "." in name:
                return int(name.split(".")[0])
    except Exception:
        pass
    return None


def _chromedriver_major(executable_path: str) -> int | None:
    """Đọc major từ chính chromedriver mà không gọi mạng."""
    if not executable_path or not Path(executable_path).is_file():
        return None
    try:
        completed = subprocess.run(
            [executable_path, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=_CREATE_NO_WINDOW,
        )
        match = re.search(r"\bChromeDriver\s+(\d+)\.", completed.stdout or "")
        return int(match.group(1)) if match else None
    except Exception:
        return None


def _cached_uc_driver_path(chrome_major: int | None) -> str:
    """Dùng lại driver UC đã patch nếu đúng major, tránh tải lại qua DNS."""
    try:
        _driver_name = "chromedriver.exe" if sys.platform == "win32" else "chromedriver"
        local_cand = Path(__file__).parent / _driver_name
        if local_cand.is_file():
            local_major = _chromedriver_major(str(local_cand))
            if not chrome_major or local_major == chrome_major:
                return str(local_cand)
        patcher = uc.Patcher(version_main=chrome_major)
        executable_path = str(patcher.executable_path)
        if not patcher.is_binary_patched(executable_path):
            return ""
        driver_major = _chromedriver_major(executable_path)
        if chrome_major and driver_major != chrome_major:
            return ""
        return executable_path
    except Exception:
        return ""


def _is_dns_resolution_error(error: BaseException) -> bool:
    """Nhận diện lỗi DNS xuyên qua URLError/OSError wrapper."""
    pending = [error]
    seen = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        message = str(current).lower()
        if "getaddrinfo failed" in message or "name resolution" in message:
            return True
        reason = getattr(current, "reason", None)
        cause = getattr(current, "__cause__", None)
        context = getattr(current, "__context__", None)
        pending.extend(
            item for item in (reason, cause, context)
            if isinstance(item, BaseException))
    return False


def find_chrome_binary() -> str:
    if sys.platform == "darwin":
        candidates = [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/Applications/Google Chrome Beta.app/Contents/MacOS/Google Chrome Beta"),
            Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
            Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
            Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ]
    else:
        candidates = [
            Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
            Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
            Path.home() / r"AppData\Local\Google\Chrome\Application\chrome.exe",
            Path(r"C:\Program Files\Google\Chrome Beta\Application\chrome.exe"),
            Path(r"C:\Program Files\Chromium\Application\chrome.exe"),
            Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        ]
    for p in candidates:
        if p.exists():
            return str(p)
    if sys.platform != "darwin":
        try:
            import winreg
            for sub in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
                        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"):
                try:
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, sub) as k:
                        exe = winreg.QueryValueEx(k, "")[0]
                        if exe and Path(exe).exists():
                            return exe
                except OSError:
                    pass
        except ImportError:
            pass
    return ""




# --------------------------------------------------------------------------- #
# Account storage
# --------------------------------------------------------------------------- #
def _normalize(v):
    if isinstance(v, str):
        return {"cookie": v, "username": "", "status": "?"}
    return {
        "cookie": v.get("cookie", ""),
        "username": v.get("username", ""),
        "status": v.get("status", "?"),
    }


def load_accounts() -> dict:
    if ACCOUNTS_FILE.exists():
        try:
            raw = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
            return {k: _normalize(val) for k, val in raw.items()}
        except Exception:
            return {}
    return {}


def save_accounts(data: dict):
    ACCOUNTS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Cookie parsing
# --------------------------------------------------------------------------- #
_STRIP_COOKIE_NAMES = frozenset({
    "_waftokenid",
    "perf_feed_cache",
    "tt_ticket_guard_has_set_public_key",
    "csrf_session_id",
})


def _is_ephemeral_waf_cookie(name: str) -> bool:
    n = (name or "").strip()
    return not n or n in _STRIP_COOKIE_NAMES or n.startswith("_waf")


def parse_cookies(raw: str):
    raw = raw.strip()
    if not raw:
        return []
    if raw.startswith("[") or raw.startswith("{"):
        data = json.loads(raw)
        if isinstance(data, dict) and "cookies" in data:
            data = data["cookies"]
        elif isinstance(data, dict):
            data = [data]
        by_name = {}
        for c in data:
            name = (c.get("name") or "").strip()
            value = c.get("value")
            if _is_ephemeral_waf_cookie(name):
                continue
            by_name[name] = {
                "name": name,
                "value": value,
                "domain": c.get("domain", ".tiktok.com"),
                "path": c.get("path", "/"),
            }
        return list(by_name.values())
    by_name = {}
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if _is_ephemeral_waf_cookie(name):
            continue
        by_name[name] = {
            "name": name,
            "value": value.strip(),
            "domain": ".tiktok.com",
            "path": "/",
        }
    return list(by_name.values())


def cookie_identity(raw: str) -> str:
    """Trích khóa định danh của 1 cookie để phát hiện trùng tài khoản.

    Ưu tiên 'sessionid' (mỗi phiên đăng nhập TikTok là duy nhất). Nếu không có,
    thử 'sid_tt' / 'sessionid_ss'. Cuối cùng fallback = chuỗi cookie đã chuẩn hóa.
    Trả về "" nếu không parse được (coi như không xác định — không gộp trùng).
    """
    try:
        cookies = parse_cookies(raw)
    except Exception:
        return ""
    by_name = {c["name"]: (c.get("value") or "") for c in cookies}
    for key in ("sessionid", "sessionid_ss", "sid_tt"):
        if by_name.get(key):
            return f"{key}={by_name[key]}"
    # Fallback: tập name=value đã sắp xếp (bỏ qua khác biệt về thứ tự/khoảng trắng)
    if cookies:
        return ";".join(sorted(f"{c['name']}={c.get('value') or ''}" for c in cookies))
    return raw.strip()


def dedupe_accounts(accounts: dict) -> tuple[dict, int]:
    """Loại bỏ tài khoản có cookie trùng (cùng sessionid). Giữ bản đầu tiên,
    ưu tiên giữ bản có username/status đã biết. Trả về (dict_mới, số_đã_xóa)."""
    seen: dict[str, str] = {}   # identity -> tên account giữ lại
    result: dict = {}
    removed = 0
    def _rank(acc):
        # Điểm cao hơn = đáng giữ hơn (đã có username, status Live)
        score = 0
        if acc.get("username"):
            score += 2
        if acc.get("status") == "Live":
            score += 1
        return score
    for name, acc in accounts.items():
        ident = cookie_identity(acc.get("cookie", ""))
        if not ident:
            result[name] = acc          # không xác định → luôn giữ
            continue
        if ident in seen:
            kept_name = seen[ident]
            # Nếu bản mới "tốt" hơn bản đã giữ → thay thế
            if _rank(acc) > _rank(result[kept_name]):
                result[kept_name] = acc
            removed += 1
        else:
            seen[ident] = name
            result[name] = acc
    return result, removed


def extract_comment_id(url: str):
    try:
        qs = parse_qs(urlparse(url).query)
        ids = qs.get("comment_id") or qs.get("reply_id")
        return ids[0] if ids else None
    except Exception:
        return None


def extract_aweme_id(url: str):
    """Trích aweme_id (ID post) = segment số cuối trong path của URL TikTok.

    VD: https://www.tiktok.com/@user/photo/7619214151662963989?...  ->  7619214151662963989
    """
    try:
        for seg in reversed(urlparse(url).path.strip("/").split("/")):
            if seg.isdigit():
                return seg
    except Exception:
        return None
    return None


def _short_text(text: str, limit: int = 40) -> str:
    """Rút gọn nội dung comment cho log (1 dòng, tối đa `limit` ký tự)."""
    t = " ".join((text or "").split())
    return t if len(t) <= limit else t[:limit] + "…"


def _fold_text(s: str) -> str:
    """NFKD, bỏ dấu/format ẩn để khớp chữ Unicode stylized với DOM."""
    raw = s or ""
    mapped = []
    for ch in raw:
        cp = ord(ch)
        if 0x1F150 <= cp <= 0x1F169:
            mapped.append(chr(ord("a") + cp - 0x1F150))
        elif 0x1F170 <= cp <= 0x1F189:
            mapped.append(chr(ord("a") + cp - 0x1F170))
        elif 0x1F1E6 <= cp <= 0x1F1FF:
            mapped.append(chr(ord("a") + cp - 0x1F1E6))
        elif ch in ("đ", "Đ"):
            mapped.append("d")
        else:
            mapped.append(ch)
    text = unicodedata.normalize("NFKD", "".join(mapped))
    text = "".join(
        ch for ch in text
        if unicodedata.category(ch) not in ("Mn", "Cf")
    )
    return " ".join(text.split()).casefold()


def _alnum_fold(s: str) -> str:
    """Chuẩn hóa chữ + số sau khi fold Unicode để bỏ ký tự trang trí (⋆, emoji)."""
    return re.sub(r"[^\w]+", "", _fold_text(s), flags=re.UNICODE).strip("_")


def _norm_handle(s: str) -> str:
    """Chuẩn hóa handle/username để so khớp: bỏ @, khoảng trắng, về thường."""
    return (s or "").lstrip("@").strip().lower()


def _user_matches(user: dict, uname: str) -> bool:
    """User trong comment có khớp target không. So cả unique_id lẫn nickname
    (bỏ dấu @, không phân biệt hoa thường, hỗ trợ chữ kiểu Unicode/emoji) —
    vì user có thể nhập tên hiển thị thay vì handle, hoặc API trả unique_id rỗng."""
    if not uname:
        return False
    u_norm = _norm_handle(uname)
    u_fold = _fold_text(u_norm)
    u_alnum = _alnum_fold(u_norm)
    for field in ("unique_id", "nickname"):
        raw_val = user.get(field) or ""
        if not raw_val:
            continue
        v_norm = _norm_handle(raw_val)
        if v_norm == u_norm:
            return True
        if u_fold and _fold_text(v_norm) == u_fold:
            return True
        v_alnum = _alnum_fold(v_norm)
        if u_alnum and v_alnum:
            if v_alnum == u_alnum:
                return True
            if len(u_alnum) >= 4 and v_alnum.startswith(u_alnum):
                return True
    return False


def _network_block_patterns(
        compact: bool, assetless: bool = False,
        captcha_images: bool = False) -> list[str]:
    """Chặn payload nặng nhưng giữ JavaScript và tùy chọn ảnh CAPTCHA."""
    patterns = [
        "*://*.doubleclick.net/*",
        "*://*.google-analytics.com/*",
        "*://*.googletagmanager.com/*",
        "*onelink.me/*",
        "*snssdk1180.onelink.me/*",
        "*://*.appsflyer.com/*",
        "*webcast/feed*",
        "*webcast/room*",
    ]
    if compact or assetless:
        patterns.extend([
            "*://*/*.mp4*", "*://*/*.m3u8*", "*://*/*.mpd*",
            "*://*/*.webm*", "*://*/*.mov*", "*://*/*.mp3*",
            "*://*/*.m4a*", "*://*/*.aac*", "*://*/*.wav*",
            "*://*/*.flac*", "*mime_type=video*", "*mime_type=audio*",
            "*play_addr*", "*video/tos*", "*aweme/v1/play*",
            "*music/play*",
        ])
    if assetless and not captcha_images:
        patterns.extend([
            "*://*/*.jpg*", "*://*/*.jpeg*", "*://*/*.png*",
            "*://*/*.webp*", "*://*/*.avif*", "*://*/*.gif*",
            "*://*/*.bmp*", "*://*/*.svg*", "*://*/*.ico*",
            "*://*/favicon*", "*://*/apple-touch-icon*",
        ])
    if assetless:
        patterns.extend([
            "*://*/*.woff*", "*://*/*.woff2*", "*://*/*.ttf*",
            "*://*/*.otf*", "*://*/*.eot*", "*://*/*.css*",
        ])
    return patterns


def kill_all_bot_browsers() -> dict:
    """Kill và xác nhận mọi Chrome/Edge dùng profile tạm của ứng dụng."""
    summary = {"killed": 0, "remaining": 0}
    if os.name != "nt":
        return summary
    command = (
        "$seen=@{}; "
        "for($attempt=0; $attempt -lt 4; $attempt++){ "
        "$owned=@(Get-CimInstance Win32_Process | Where-Object { "
        "($_.Name -eq 'chrome.exe' -or $_.Name -eq 'msedge.exe') "
        "-and $_.CommandLine "
        "-and $_.CommandLine -like '*tiktok_profile_*' }); "
        "$roots=@($owned | Where-Object { "
        "$_.CommandLine -notmatch '(^|\\s)--type=' }); "
        "foreach($proc in $roots){ "
        "$seen[$proc.ProcessId]=$true; "
        "Stop-Process -Id $proc.ProcessId -Force "
        "-ErrorAction SilentlyContinue }; "
        "foreach($proc in $owned){ "
        "Stop-Process -Id $proc.ProcessId -Force "
        "-ErrorAction SilentlyContinue }; "
        "Start-Sleep -Milliseconds 250 }; "
        "$remaining=@(Get-CimInstance Win32_Process | Where-Object { "
        "($_.Name -eq 'chrome.exe' -or $_.Name -eq 'msedge.exe') "
        "-and $_.CommandLine "
        "-and $_.CommandLine -like '*tiktok_profile_*' "
        "-and $_.CommandLine -notmatch '(^|\\s)--type=' }).Count; "
        "Write-Output ('killed=' + $seen.Count + "
        "';remaining=' + $remaining)"
    )
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile",
             "-NonInteractive", "-Command", command],
            capture_output=True, text=True, timeout=15,
            creationflags=_CREATE_NO_WINDOW)
        match = re.search(
            r"killed=(\d+);remaining=(\d+)",
            completed.stdout or "")
        if match:
            summary["killed"] = int(match.group(1))
            summary["remaining"] = int(match.group(2))
        elif completed.returncode:
            summary["remaining"] = -1
    except Exception:
        summary["remaining"] = -1
    return summary


# --------------------------------------------------------------------------- #
# Automation core
# --------------------------------------------------------------------------- #
class _UnresponsiveDriverStub:
    """Stub thay thế WebDriver bị treo sau khi _hard_kill() để Runner._run_one
    (đọc driver.current_url) và close() thoát tức thì không chờ HTTP timeout."""
    _is_unresponsive_stub = True

    def __init__(self, current_url: str = ""):
        self.current_url = str(current_url or "")

    def get_cookies(self):
        return []

    def execute_script(self, *args, **kwargs):
        return None

    def execute_async_script(self, *args, **kwargs):
        return None

    def find_elements(self, *args, **kwargs):
        return []

    def quit(self):
        return None


class TikTokLikeBot:
    def __init__(self, log_fn, stop_event, prefix: str = ""):
        # prefix (vd "acc3" / "@user") tự chèn vào đầu mỗi dòng log của bot này —
        # để khi chạy đa luồng, mọi dòng đều gắn tài khoản, không bị lẫn.
        self._raw_log = log_fn
        self.prefix = prefix
        self.stop_event = stop_event
        self.driver = None
        self._browser_grid_slot = None
        # Kết quả tim của lần like_comment gần nhất (runner đọc để đếm + log).
        self.last_like_count = 0            # số comment đã tim (nhánh @user)
        self.last_proxy_blocked = False     # 403: TikTok chặn IP proxy (acc không die)
        self.last_liked: list = []          # [{author, text}] chi tiết comment
        self.last_like_failure = ""
        self.last_captcha_blocked = False
        # True khi digg API trả status_code=8 (session ghi hết hạn) — runner
        # đọc cờ này để DỪNG acc luôn thay vì cố tim hết các link còn lại.
        self.last_digg_expired = False
        self.last_session_invalid = False
        self.last_shadow_banned = False
        self.last_login_username = ""
        self.last_batch_complete = None
        self.last_comment_blocked = False
        self.last_comment_rate_limited = False
        self.last_comment_submission = {}
        self._http_debug = False
        self._http_seen = set()

    def log(self, msg: str):
        if self.prefix and not msg.lstrip().startswith("["):
            msg = f"[{self.prefix}] {msg}"
        self._raw_log(msg)
        if self._http_debug:
            self._flush_http_logs()

    def _check_stop(self):
        if self.stop_event.is_set():
            raise RuntimeError("Đã dừng theo yêu cầu người dùng.")

    @staticmethod
    def _rsleep(base: float, spread: float = 0.4):
        """Sleep ngẫu nhiên quanh 'base' để hành vi giống người, tránh pattern máy.

        Thời gian thực = base × hệ số ngẫu nhiên trong [1-spread, 1+spread].
        Dùng cho các thao tác GIẢ NGƯỜI (click, scroll, chuyển link), KHÔNG dùng
        cho sleep chờ kỹ thuật (page load) để không làm chậm/kém ổn định.
        """
        lo = max(0.0, base * (1 - spread))
        hi = base * (1 + spread)
        time.sleep(random.uniform(lo, hi))

    # UC mặc định xóa/tải lại chromedriver ở mỗi lần tạo Chrome. Giữ binary đã
    # patch theo major để batch hàng nghìn account không phụ thuộc DNS Google.
    _uc_init_lock = threading.Lock()
    _uc_driver_paths: dict[int, str] = {}
    _browser_grid_lock = threading.Lock()
    _browser_grid_slots: dict[int, int] = {}

    @classmethod
    def _claim_browser_grid_slot(cls, capacity: int, preferred: int = 0) -> int:
        capacity = max(1, int(capacity or 1))
        with cls._browser_grid_lock:
            slot = next(
                (candidate for candidate in range(capacity)
                 if not cls._browser_grid_slots.get(candidate)),
                max(0, int(preferred or 0)) % capacity,
            )
            cls._browser_grid_slots[slot] = (
                cls._browser_grid_slots.get(slot, 0) + 1)
            return slot

    def _release_browser_grid_slot(self) -> None:
        with type(self)._browser_grid_lock:
            slot = getattr(self, "_browser_grid_slot", None)
            if slot is None:
                return
            self._browser_grid_slot = None
            count = type(self)._browser_grid_slots.get(slot, 0)
            if count <= 1:
                type(self)._browser_grid_slots.pop(slot, None)
            else:
                type(self)._browser_grid_slots[slot] = count - 1

    _VIEWPORT_POOL = [
        (900, 820), (960, 800), (880, 840), (920, 780),
    ]

    _MOBILE_UA_POOL = [
        "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/151.0.0.0 Mobile Safari/537.36",
        "Mozilla/5.0 (Linux; Android 12; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/150.0.0.0 Mobile Safari/537.36",
        "Mozilla/5.0 (Linux; Android 14; K) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/151.0.7922.171 Mobile Safari/537.36",
    ]
    _MOBILE_VIEWPORT_POOL = [
        (390, 700), (375, 680), (360, 720), (412, 736),
    ]

    def start_browser(
            self, headless=False, session_idx: int = None, proxy: dict = None,
            mobile: bool = False, debug: bool = False,
            compact: bool = False, http_debug: bool = True,
            assetless: bool = False, captcha_images: bool = False):
        self._check_stop()
        # Desktop dùng UA native để khớp Client Hints/platform/version thật.
        # Worker API không thao tác DOM: viewport nhỏ và chỉ giữ HTML/JS/API.
        if mobile:
            ua = random.choice(self._MOBILE_UA_POOL)
            vw, vh = random.choice(self._MOBILE_VIEWPORT_POOL)
            vw += random.randint(-4, 4)
            vh += random.randint(-8, 8)
        else:
            ua = ""
            if assetless:
                vw, vh = 640, 480
            else:
                vw, vh = random.choice(
                    [(980, 760), (1020, 780), (960, 800)]
                    if compact else self._VIEWPORT_POOL)
        if headless:
            vw, vh = max(vw, 1440), max(vh, 1000)
        self._compact_browser = bool(compact)
        self._assetless_browser = bool(assetless)
        self._captcha_images_enabled = bool(captcha_images)
        proxy_label = f" proxy={proxy['host']}:{proxy['port']}" if proxy else ""

        chrome_path = find_chrome_binary()
        if sys.platform == "darwin":
            edge_path = next(
                (str(p) for p in [
                    Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
                ] if p.exists()), ""
            )
        else:
            edge_path = next(
                (str(p) for p in [
                    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
                    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
                ] if p.exists()), ""
            )

        # Unique temp profile dir — browser fingerprint khác nhau mỗi lần
        profile_dir = tempfile.mkdtemp(prefix="tiktok_profile_")
        # Ghi nhận trước khi uc.Chrome() chạy: Force Stop vẫn tìm được Chrome
        # nếu driver treo giữa lúc khởi tạo và chưa gán vào self.driver.
        self._profile_dir = profile_dir

        def _lean_chrome(opts):
            if http_debug:
                opts.set_capability("goog:loggingPrefs", {"performance":"ALL"})
            """Giảm renderer/disk; assetless chỉ dùng cho worker API không có UI."""
            opts.add_argument("--disable-gpu")
            opts.add_argument("--mute-audio")
            opts.add_argument("--autoplay-policy=user-gesture-required")
            opts.add_argument("--disable-sync")
            opts.add_argument("--disable-component-update")
            opts.add_argument("--disable-default-apps")
            opts.add_argument("--no-first-run")
            opts.add_argument("--no-default-browser-check")
            opts.add_argument("--no-pings")
            opts.add_argument("--disable-breakpad")
            opts.add_argument("--disable-crash-reporter")
            opts.add_argument("--disk-cache-size=1048576")
            opts.add_argument("--media-cache-size=1")
            prefs = {
                "credentials_enable_service": False,
                "profile.password_manager_enabled": False,
                "profile.password_manager_leak_detection": False,
            }
            if assetless and not captcha_images:
                opts.add_argument("--blink-settings=imagesEnabled=false")
                prefs["profile.managed_default_content_settings.images"] = 2
            if assetless:
                opts.add_argument("--disable-remote-fonts")
                opts.add_argument("--disable-extensions")
                opts.add_argument("--disable-notifications")
                opts.add_argument("--disable-print-preview")
                opts.add_argument("--disable-translate")
                opts.add_argument("--force-device-scale-factor=" + ("1" if headless else "0.5"))
                opts.add_argument(
                    "--disable-features=MediaRouter,GlobalMediaControls,"
                    "OptimizationHints,AutofillServerCommunication")
                prefs["profile.default_content_setting_values.notifications"] = 2
            if prefs:
                opts.add_experimental_option("prefs", prefs)

        def _block_heavy_media(driver):
            """Chặn media; worker API vẫn giữ ảnh để solver đọc CAPTCHA."""
            urls = _network_block_patterns(
                compact, assetless, captcha_images=captcha_images)
            self._blocked_urls = urls
            try:
                driver.execute_cdp_cmd("Network.enable", {})
                driver.execute_cdp_cmd("Network.setBlockedURLs", {"urls": urls})
            except Exception:
                pass
            try:
                driver.execute_cdp_cmd("Emulation.setAutoplayPolicy", {
                    "autoplayPolicy": "document-user-activation-required"
                })
            except Exception:
                pass


        def _apply_proxy(opts, is_uc=True):
            if not proxy:
                return
            host, port = proxy["host"], proxy["port"]
            user, pw  = proxy.get("user", ""), proxy.get("password", "")
            scheme    = _norm_scheme(proxy.get("scheme", "http"))

            if scheme == "socks5":
                if user and pw:
                    # Chrome KHONG ho tro auth cho SOCKS5 -> dung local relay:
                    # Chrome noi HTTP toi 127.0.0.1:<port>, relay bat tay SOCKS5
                    # (co user/pass) len upstream roi tunnel.
                    relay = Socks5Relay(host, port, user, pw)
                    local_port = relay.start()
                    self._proxy_relay = relay
                    opts.add_argument(f"--proxy-server=http://127.0.0.1:{local_port}")
                else:
                    # SOCKS5 khong auth -> Chrome ho tro native
                    opts.add_argument(f"--proxy-server=socks5://{host}:{port}")
            elif user and pw:
                # HTTP proxy co auth: uc KHONG nap duoc extension proxy-auth
                # -> Chrome dung IP goc. Thay bang local relay HTTP tu chen auth.
                relay = ProxyRelay(host, port, user, pw)
                local_port = relay.start()
                self._proxy_relay = relay
                opts.add_argument(f"--proxy-server=http://127.0.0.1:{local_port}")
            else:
                opts.add_argument(f"--proxy-server=http://{host}:{port}")

        if chrome_path and "edge" not in chrome_path.lower():
            self.log(f"Khởi động trình duyệt...{proxy_label}")
            opts = uc.ChromeOptions()
            opts.page_load_strategy = "eager"
            opts.add_argument(f"--window-size={vw},{vh}")
            if ua:
                opts.add_argument(f"--user-agent={ua}")
            opts.add_argument(f"--user-data-dir={profile_dir}")
            opts.add_argument("--lang=vi-VN")
            opts.binary_location = chrome_path
            _apply_proxy(opts)
            _lean_chrome(opts)
            # HEADLESS GIẢ: Chrome headless thật (--headless=new) bị TikTok
            # fingerprint phát hiện → phục vụ trang khác (không nút comment,
            # de2e rỗng) dù debug chạy tốt. Thay vào đó: Chrome cửa sổ THẬT
            # (render giống hệt debug) nhưng đẩy cửa sổ ra ngoài màn hình —
            # người dùng không thấy, TikTok thấy browser thật.
            if headless:
                opts.add_argument("--window-position=-32000,-32000")
                opts.add_argument("--disable-backgrounding-occluded-windows")
                opts.add_argument("--disable-renderer-backgrounding")
                opts.add_argument("--disable-background-timer-throttling")
                opts.add_argument("--disable-features=CalculateNativeWinOcclusion")
            if debug:
                opts.add_argument("--auto-open-devtools-for-tabs")
                debug_prefs = dict(opts.experimental_options.get("prefs", {}))
                debug_prefs["devtools.preferences.currentDockState"] = '"undocked"'
                opts.add_experimental_option("prefs", debug_prefs)
                opts.set_capability("goog:loggingPrefs", {"performance": "ALL"})
            major = detect_chrome_major(chrome_path)
            driver_key = int(major or 0)
            with TikTokLikeBot._uc_init_lock:
                driver_path = TikTokLikeBot._uc_driver_paths.get(
                    driver_key, "")
                if not driver_path or not Path(driver_path).is_file():
                    driver_path = _cached_uc_driver_path(major)
                for attempt in range(3):
                    try:
                        kwargs = {
                            "options": opts,
                            "headless": False,
                            "version_main": major,
                            "use_subprocess": True,
                        }
                        if driver_path:
                            kwargs["driver_executable_path"] = driver_path
                        self._check_stop()
                        self.driver = uc.Chrome(**kwargs)
                        patched_path = str(
                            getattr(self.driver.patcher, "executable_path", "")
                            or driver_path)
                        if patched_path:
                            TikTokLikeBot._uc_driver_paths[
                                driver_key] = patched_path
                        break
                    except Exception as error:
                        if not _is_dns_resolution_error(error):
                            raise
                        if attempt >= 2:
                            raise RuntimeError(
                                "DNS không phân giải được máy chủ tải "
                                "ChromeDriver sau 3 lần thử") from error
                        self.log(
                            "⚠️ DNS tải ChromeDriver lỗi; thử lại sau "
                            f"{attempt + 1} giây.")
                        time.sleep(attempt + 1)
        elif edge_path:
            self.log(f"Khởi động Edge...{proxy_label}")
            opts = EdgeOptions()
            opts.page_load_strategy = "eager"
            opts.add_argument(f"--window-size={vw},{vh}")
            if ua:
                opts.add_argument(f"--user-agent={ua}")
            opts.add_argument(f"--user-data-dir={profile_dir}")
            opts.add_argument("--lang=vi-VN")
            opts.add_argument("--disable-blink-features=AutomationControlled")
            opts.add_experimental_option("excludeSwitches", ["enable-automation"])
            opts.add_experimental_option("useAutomationExtension", False)
            _lean_chrome(opts)
            if headless:
                opts.add_argument("--headless=new")
                opts.add_argument("--window-position=-32000,-32000")
                opts.add_argument("--disable-backgrounding-occluded-windows")
                opts.add_argument("--disable-renderer-backgrounding")
                opts.add_argument("--disable-background-timer-throttling")
                opts.add_argument("--disable-features=CalculateNativeWinOcclusion")
            if debug:
                opts.add_argument("--auto-open-devtools-for-tabs")
                debug_prefs = dict(opts.experimental_options.get("prefs", {}))
                debug_prefs["devtools.preferences.currentDockState"] = '"undocked"'
                opts.add_experimental_option("prefs", debug_prefs)
                opts.set_capability("goog:loggingPrefs", {"performance": "ALL"})
            _apply_proxy(opts, is_uc=False)
            self._check_stop()
            service = EdgeService(EdgeChromiumDriverManager().install())
            self.driver = webdriver.Edge(service=service, options=opts)
        else:
            raise RuntimeError(
                "Không tìm thấy Chrome hoặc Edge. "
                "Hãy cài Chrome tại https://www.google.com/chrome/ rồi thử lại."
            )
        # Bound the HTTP command wait as well as the browser page timeout.
        # Disable urllib3 automatic retries on local chromedriver loopback so a
        # wedged renderer never multiplies WebDriver timeouts by 4x (1 + 3 retries).
        self._driver_unresponsive = False
        executor = getattr(self.driver, 'command_executor', None)
        config = getattr(executor, '_client_config', None)
        if config is not None:
            config.timeout = 25
        conn = getattr(executor, '_conn', None)
        if conn is not None:
            try:
                from urllib3.util.retry import Retry
                no_retry = Retry(total=False, connect=0, read=0, redirect=False)
            except Exception:
                no_retry = False
            if isinstance(getattr(conn, 'connection_pool_kw', None), dict):
                conn.connection_pool_kw['retries'] = no_retry
            pools = getattr(conn, 'pools', None)
            if pools is not None:
                try:
                    with getattr(pools, 'lock', threading.RLock()):
                        for pool in list(getattr(pools, '_container', {}).values()):
                            pool.retries = no_retry
                except Exception:
                    pass
        self.driver.set_page_load_timeout(45)
        _block_heavy_media(self.driver)
        try:
            if headless:
                self.driver.set_window_rect(
                    x=-32000, y=-32000, width=vw, height=vh)
            else:
                try:
                    screen = self.driver.execute_script("""
                        return {
                          left: Number(screen.availLeft || 0),
                          top: Number(screen.availTop || 0),
                          width: Number(screen.availWidth || 1920),
                          height: Number(screen.availHeight || 1080)
                        };
                    """) or {}
                    screen_left = int(screen.get("left") or 0)
                    screen_top = int(screen.get("top") or 0)
                    screen_width = int(screen.get("width") or 1920)
                    screen_height = int(screen.get("height") or 1080)
                except Exception:
                    screen_left, screen_top = 0, 0
                    screen_width, screen_height = 1920, 1080
                _, _, _, _, capacity = browser_grid_rect(
                    0, screen_width, screen_height,
                    screen_left, screen_top)
                slot = self._claim_browser_grid_slot(
                    capacity, int(session_idx or 0))
                self._browser_grid_slot = slot
                x, y, win_w, win_h, _ = browser_grid_rect(
                    slot, screen_width, screen_height,
                    screen_left, screen_top)
                try:
                    self.driver.set_window_rect(
                        x=x, y=y, width=win_w, height=win_h)
                except Exception:
                    self.driver.set_window_size(win_w, win_h)
                    self.driver.set_window_position(x, y)
        except Exception:
            pass
        if mobile or assetless:
            try:
                self.driver.execute_cdp_cmd(
                    "Emulation.setDeviceMetricsOverride", {
                        "width": vw,
                        "height": vh,
                        "deviceScaleFactor": 1 if headless else (0.5 if assetless else 2),
                        "mobile": bool(mobile),
                        "screenWidth": vw,
                        "screenHeight": vh,
                    })
                if mobile:
                    self.driver.execute_cdp_cmd(
                        "Emulation.setTouchEmulationEnabled",
                        {"enabled": True, "maxTouchPoints": 5})
            except Exception as e:
                self.log(f"⚠️ Không giảm được độ phân giải browser: {e}")
            if mobile:
                self._install_web_comment_gate()
        self._install_comment_hook(capture_lists=True)
        self._install_media_killer()
        if debug and http_debug:
            self._enable_http_debug()

    def _install_web_comment_gate(self):
        """Chan protocol tiktok:// + an popup Mo app de comment sheet web mo."""
        d = self.driver
        try:
            d.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": r"""
(function(){
  const kill = (u) => /tiktok:|snssdk|onelink\.me|appsflyer|af_dp=/i.test(String(u||''));
  const loc = window.location;
  try {
    const raw = loc.assign.bind(loc);
    loc.assign = function(u){ if (kill(u)) return; return raw(u); };
    const rawr = loc.replace.bind(loc);
    loc.replace = function(u){ if (kill(u)) return; return rawr(u); };
  } catch(e) {}
  try {
    const wo = window.open;
    window.open = function(u,n,f){ if (kill(u)) return null; return wo.call(window,u,n,f); };
  } catch(e) {}
  document.addEventListener('click', function(e){
    const a = e.target && e.target.closest && e.target.closest('a,[data-e2e="play-side-comment"],[data-e2e="instagram-modal-button"],[data-e2e="bottom-banner-open"]');
    if (!a) return;
    const href = a.getAttribute('href') || a.getAttribute('data-href') || '';
    if (kill(href) || /open.?tiktok|mở tiktok/i.test((a.innerText||'')+' '+(a.getAttribute('aria-label')||''))) {
      e.preventDefault(); e.stopImmediatePropagation();
      try { a.removeAttribute('href'); a.removeAttribute('target'); } catch(_){}
    }
  }, true);
})();
"""})
        except Exception:
            pass

    def _dismiss_open_app_cta(self):
        try:
            self.driver.execute_script("""
              document.querySelectorAll(
                "[data-e2e='instagram-modal'],[data-e2e='instagram-modal-close'],"
                +"[data-e2e='launch-popup-close'],[data-e2e='bottom-banner-open'],"
                +"[class*='DivPopup'],[class*='DivMask'],"
                +"[data-testid*='tux-web-canary'][data-testid$='modal-overlay'],"
                +"[class*='tux-modal__overlay'],.tux-modal__overlay-AbELNe"
              ).forEach(function(el){
                el.style.setProperty('display','none','important');
                el.style.setProperty('pointer-events','none','important');
                el.setAttribute('aria-hidden','true');
              });
              var c=document.querySelector("[data-e2e='instagram-modal-close'],[data-e2e='launch-popup-close']");
              if(c) try{c.click();}catch(e){}
            """)
        except Exception:
            pass

    def pause_network_blocks(self, on: bool):
        """Tắt block URL khi vào /login (form emotion cần font/chunk). Buff bật lại."""
        urls = [] if on else getattr(self, "_blocked_urls", None)
        if urls is None and not on:
            return
        try:
            self.driver.execute_cdp_cmd("Network.setBlockedURLs",
                                        {"urls": urls or []})
        except Exception:
            pass

    def _enable_http_debug(self):
        """Debug: đo từng fetch/XHR (TikTok API) — method status ms size path."""
        self._http_debug = True
        self._http_seen = set()
        try:
            self.driver.execute_cdp_cmd("Network.enable", {})
        except Exception:
            pass
        try:
            self.driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": r"""
                (function(){
                  if (window.__tt_http_hooked) return;
                  window.__tt_http_hooked = true;
                  window.__tt_http_logs = [];
                  function push(rec){
                    try { window.__tt_http_logs.push(rec); } catch(e){}
                    if (window.__tt_http_logs.length > 400)
                      window.__tt_http_logs.splice(0, 200);
                  }
                  function shortUrl(u){
                    try {
                      const x = new URL(u, location.href);
                      return x.pathname + (x.search ? x.search.slice(0, 80) : '');
                    } catch(e) { return String(u).slice(0, 120); }
                  }
                  function interesting(u){
                    u = String(u||'');
                    if (/\/v1\/list(\?|$)|monitor_web|monitor_browser|slardar|webid|ttwid|abtest|privacy_headers|global-footer|notice\/multi|inbox\/notice|webcast\/|wallet_api|screen_time|community_notes|prefetch\/explore|cookie-privacy|business\/suite|compliance\/settings|share\/settings|popup\/|pns_|goofy-captcha\/.*captcha\.js/i.test(u))
                      return false;
                    return /\/api\/comment\/|\/api\/item\/|\/api\/user\/|\/aweme\/v1\/commit\/user|\/commit\/user|\/passport\/|\/captcha\/|omocaptcha|konfuse|openai|generativelanguage|\/api\/comment\/digg/i.test(u);
                  }
                  const ofetch = window.fetch;
                  window.fetch = function(){
                    const a = arguments;
                    const u = (typeof a[0]==='string') ? a[0] : (a[0] && a[0].url) || '';
                    const m = (a[1] && a[1].method) || (a[0] && a[0].method) || 'GET';
                    const t0 = performance.now();
                    const p = ofetch.apply(this, a);
                    if (interesting(u)) {
                      p.then(function(r){
                        const ms = Math.round(performance.now()-t0);
                        push({k: t0+':'+u.slice(-40), m: m, s: r.status, ms: ms,
                              u: shortUrl(u), n: +(r.headers.get('content-length')||0)});
                      }).catch(function(e){
                        push({k: t0+':err', m: m, s: 0, ms: Math.round(performance.now()-t0),
                              u: shortUrl(u), err: String(e).slice(0,80)});
                      });
                    }
                    return p;
                  };
                  const xo = XMLHttpRequest.prototype.open;
                  const xs = XMLHttpRequest.prototype.send;
                  XMLHttpRequest.prototype.open = function(method, url){
                    this.__tt_m = method; this.__tt_u = url;
                    return xo.apply(this, arguments);
                  };
                  XMLHttpRequest.prototype.send = function(){
                    const xhr = this;
                    const u = xhr.__tt_u || '';
                    if (interesting(u)) {
                      const t0 = performance.now();
                      xhr.addEventListener('loadend', function(){
                        push({k: t0+':x'+u.slice(-40), m: xhr.__tt_m||'GET',
                              s: xhr.status, ms: Math.round(performance.now()-t0),
                              u: shortUrl(u), n: (xhr.responseText||'').length});
                      });
                    }
                    return xs.apply(this, arguments);
                  };
                })();
                """})
            self._raw_log("[http] Debug HTTP: log fetch/XHR TikTok + solver (ms, status, path).")
        except Exception as e:
            self._raw_log(f"[http] Không gắn hook: {e}")

    def _flush_http_logs(self):
        if not self._http_debug or not self.driver:
            return
        try:
            rows = self.driver.execute_script(
                "var a=window.__tt_http_logs||[]; window.__tt_http_logs=[]; return a;") or []
        except Exception:
            return
        for r in rows:
            key = r.get("k")
            if key in self._http_seen:
                continue
            self._http_seen.add(key)
            ms = r.get("ms") or 0
            st = r.get("s") if r.get("s") is not None else "?"
            method = (r.get("m") or "GET")[:6]
            path = (r.get("u") or "")[:110]
            n = r.get("n") or 0
            err = r.get("err")
            extra = f" {n}B" if n else ""
            if err:
                extra = f" ERR {err}"
            line = f"[http {ms}ms {method} {st}] {path}{extra}"
            if self.prefix:
                line = f"[{self.prefix}] {line}"
            self._raw_log(line)

    def _install_comment_hook(self, capture_lists: bool = True):
        """Thu publish luôn; chỉ thu comment list khi browser không compact."""
        try:
            self.driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": r"""
                window.__tt_comment_bodies = [];
                window.__tt_comment_urls = [];
                window.__tt_comment_dropped = 0;
                window.__tt_comment_nodes = Object.create(null);
                window.__tt_comment_capture_stats = {
                  rootResponses: 0, replyResponses: 0,
                  total: null, cursor: null, hasMore: null
                };
                (function(){
                  const captureLists = __CAPTURE_LISTS__;
                  const isPublish = (url, method) =>
                    String(method || 'GET').toUpperCase() === 'POST'
                    && /\/api\/comment\/(?:publish|create)(?:\/|\?|$)/i.test(String(url || ''));
                  const savePost = (url, status, body) => {
                    try {
                      window.__tt_comment_posts.push({
                        url: String(url || ''),
                        status: Number(status || 0),
                        body: String(body || '').slice(0, 500000)
                      });
                      if (window.__tt_comment_posts.length > 20)
                        window.__tt_comment_posts.splice(0, 10);
                    } catch (_) {}
                  };
                  const isList = (url) =>
                    /\/api\/comment\/list(?:\/reply)?(?:\/|\?|$)/i.test(String(url || ''));
                  const saveList = (url, body) => {
                    try {
                      const rawUrl = String(url || '');
                      const rawBody = String(body || '');
                      const replyUrl = /\/api\/comment\/list\/reply(?:\/|\?|$)/i
                        .test(rawUrl);
                      const stats = window.__tt_comment_capture_stats;
                      if (replyUrl) stats.replyResponses++;
                      else stats.rootResponses++;
                      try {
                        let data = JSON.parse(rawBody);
                        if (data && data.data && typeof data.data === 'object')
                          data = data.data;
                        const parentFromUrl = (() => {
                          try {
                            return new URL(rawUrl, location.href)
                              .searchParams.get('comment_id') || '';
                          } catch (_) { return ''; }
                        })();
                        const cleanCid = value => {
                          const s = String(value || '').trim();
                          return (s && s !== '0') ? s : '';
                        };
                        const keep = (node, parentCid) => {
                          if (!node || !node.cid) return;
                          const cid = String(node.cid);
                          const previous = window.__tt_comment_nodes[cid] || {};
                          const inferredParent = cleanCid(parentCid)
                            || cleanCid(node.reply_id)
                            || cleanCid(node.reply_to_reply_id)
                            || cleanCid(previous.parent_cid);
                          const user = node.user || {};
                          window.__tt_comment_nodes[cid] = {
                            cid,
                            parent_cid: inferredParent === cid ? '' : inferredParent,
                            reply_comment_total: Number(
                              node.reply_comment_total || previous.reply_comment_total || 0),
                            text: String(node.text || previous.text || ''),
                            user: {
                              unique_id: String(user.unique_id
                                || (previous.user && previous.user.unique_id) || ''),
                              nickname: String(user.nickname
                                || (previous.user && previous.user.nickname) || '')
                            },
                            user_digged: Boolean(
                              node.user_digged || previous.user_digged)
                          };
                        };
                        const comments = data.comments || [];
                        for (const comment of comments) {
                          keep(comment, replyUrl ? parentFromUrl : '');
                          for (const reply of (comment.reply_comment || []))
                            keep(reply, comment.cid || parentFromUrl);
                        }
                        if (!replyUrl && data.total !== undefined) {
                          stats.total = data.total;
                          stats.cursor = data.cursor;
                          stats.hasMore = data.has_more;
                        }
                      } catch (_) {}
                      window.__tt_comment_urls.push(rawUrl);
                      window.__tt_comment_bodies.push(rawBody.slice(0, 500000));
                      if (window.__tt_comment_bodies.length > 80) {
                        const dropped = window.__tt_comment_bodies.length - 40;
                        window.__tt_comment_dropped += dropped;
                        window.__tt_comment_bodies.splice(0, dropped);
                        window.__tt_comment_urls.splice(0, dropped);
                      }
                    } catch (_) {}
                  };
                  const of = window.fetch;
                  window.fetch = function(...args){
                    const url = (typeof args[0] === 'string')
                      ? args[0] : (args[0] && args[0].url) || '';
                    const method = (args[1] && args[1].method)
                      || (args[0] && args[0].method) || 'GET';
                    const promise = of.apply(this, args);
                    if (captureLists && isList(url)) {
                      promise.then(response => { try {
                        response.clone().text().then(body =>
                          saveList(url, body)).catch(() => {});
                      } catch (_) {} });
                    }
                    if (isPublish(url, method)) {
                      promise.then(response => { try {
                        response.clone().text().then(body =>
                          savePost(url, response.status, body)).catch(() => {});
                      } catch (_) {} }).catch(error =>
                        savePost(url, 0, JSON.stringify({transport_error: String(error)})));
                    }
                    return promise;
                  };
                  const open = XMLHttpRequest.prototype.open;
                  const send = XMLHttpRequest.prototype.send;
                  XMLHttpRequest.prototype.open = function(method, url){
                    this.__alc_method = method;
                    this.__alc_url = url;
                    return open.apply(this, arguments);
                  };
                  XMLHttpRequest.prototype.send = function(){
                    if (captureLists && isList(this.__alc_url)) {
                      this.addEventListener('loadend', () =>
                        saveList(this.__alc_url, this.responseText));
                    }
                    if (isPublish(this.__alc_url, this.__alc_method)) {
                      this.addEventListener('loadend', () =>
                        savePost(this.__alc_url, this.status, this.responseText));
                    }
                    return send.apply(this, arguments);
                  };
                })();
                """.replace("__CAPTURE_LISTS__",
                            "true" if capture_lists else "false")})
        except Exception as e:
            self.log(f"⚠️ Không cài được comment hook: {e}")

    def _install_media_killer(self):
        """Chặn autoplay và xóa media khỏi DOM ngoài luồng CAPTCHA."""
        hard = "true" if (
            getattr(self, "_compact_browser", False)
            or getattr(self, "_assetless_browser", False)) else "false"
        source = r"""
        (function(){
          const HARD = __HARD__;
          function isCaptcha(el){
            try{
              return !!(el.closest && el.closest(
                '.captcha-verify-container,.captcha-disable-scroll,[id*="captcha"]'));
            }catch(e){ return false; }
          }
          if(HARD){
            try{
              const rawPlay=HTMLMediaElement.prototype.play;
              HTMLMediaElement.prototype.play=function(){
                if(!isCaptcha(this)){
                  try{ this.pause(); this.muted=true; }catch(e){}
                  return Promise.resolve();
                }
                return rawPlay.apply(this,arguments);
              };
            }catch(e){}
          }
          function sweep(){
            document.querySelectorAll('video,audio').forEach(function(m){
              if(isCaptcha(m)) return;
              try{
                m.muted=true; m.volume=0;
                if(HARD){ m.pause(); m.removeAttribute('src'); m.load&&m.load(); }
              }catch(e){}
            });
          }
          function arm(){
            sweep();
            try{
              new MutationObserver(sweep).observe(
                document.documentElement,{childList:true,subtree:true});
            }catch(e){}
          }
          if(document.readyState==='loading')
            document.addEventListener('DOMContentLoaded',arm);
          else arm();
        })();
        """.replace("__HARD__", hard)
        try:
            self.driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument", {"source": source})
        except Exception as e:
            self.log(f"⚠️ Không cài media killer: {e}")

    def _reset_comment_hook(self):
        """Xoá response, URL và bộ đếm debug của comment hook."""
        try:
            self.driver.execute_script("""
                window.__tt_comment_bodies = [];
                window.__tt_comment_urls = [];
                window.__tt_comment_dropped = 0;
                window.__tt_comment_nodes = Object.create(null);
                window.__tt_comment_capture_stats = {
                    rootResponses: 0, replyResponses: 0,
                    total: null, cursor: null, hasMore: null
                };
                window.__tt_comment_posts = [];
            """)
        except Exception:
            pass

    def verify_proxy_ip(self) -> str:
        """Mở api.ipify.org để lấy IP thoát thực tế. Trả IP hoặc '' nếu thất bại.

        Dùng để xác minh browser thực sự đi qua proxy (không lộ IP gốc).
        Extension proxy-auth cần vài trăm ms để khởi tạo, nên thử tối đa 2 lần.
        """
        d = self.driver
        for attempt in range(2):
            try:
                d.get("https://api.ipify.org?format=json")
                body = WebDriverWait(d, 2, poll_frequency=0.1).until(
                    lambda driver: driver.find_element(By.TAG_NAME, "body").text.strip())
                try:
                    ip = json.loads(body).get("ip", "")
                except Exception:
                    ip = body if body and body.count(".") == 3 else ""
                if ip:
                    return ip
            except Exception:
                pass
            time.sleep(1)
        return ""

    def check_live_api(self):
        """Passport trên origin TikTok; robots.txt thay homepage nhiều MB."""
        d = self.driver
        if not getattr(self, "_cdp_network_enabled", False):
            try:
                d.execute_cdp_cmd("Network.enable", {})
                self._cdp_network_enabled = True
            except Exception:
                pass
        cur = (d.current_url or "")
        if "tiktok.com" not in cur:
            try:
                d.get("https://www.tiktok.com/robots.txt")
            except Exception:
                return False, ""
        script = r"""
        const done = arguments[arguments.length - 1];
        const paths = ['/passport/web/account/info/?aid=1459',
                       '/api/user/detail/?aid=1988'];
        const username = obj => {
          if (!obj || typeof obj !== 'object') return '';
          const name = obj.unique_id || obj.username;
          if (typeof name === 'string' && name.trim()) return name;
          for (const child of Object.values(obj)) {
            const found = username(child); if (found) return found;
          }
          return '';
        };
        (async () => {
          for (const path of paths) {
            const control = new AbortController();
            const timer = setTimeout(() => control.abort(), 4000);
            try {
              const response = await fetch(path, {credentials:'include',
                signal:control.signal, cache:'no-store'});
              if (!response.ok) continue;
              const text = await response.text();
              const body = JSON.parse(text);
              const code = body.status_code ?? body.error_code ?? body.data?.error_code;
              if (code !== undefined && code !== null &&
                  (typeof code === 'boolean' || String(code).trim() !== '0')) continue;
              if (username(body)) return text;
            } catch (_) {} finally { clearTimeout(timer); }
          }
          return '';
        })().then(done).catch(() => done(''));
        """
        try:
            d.set_script_timeout(10)
            body = d.execute_async_script(script)
        except Exception:
            body = ""
        if not body:
            return False, ""
        try:
            data = json.loads(body)
        except Exception:
            return False, ""
        status_code = data.get("status_code")
        if status_code is not None and status_code != 0:
            return False, ""
        def _walk(obj):
            if isinstance(obj, dict):
                uid = obj.get("unique_id") or obj.get("username")
                if uid and isinstance(uid, str) and uid.strip():
                    return "@" + uid.lstrip("@")
                for v in obj.values():
                    r = _walk(v)
                    if r:
                        return r
            elif isinstance(obj, list):
                for it in obj:
                    r = _walk(it)
                    if r:
                        return r
            return ""
        def _walk_uid(obj):
            if isinstance(obj, dict):
                for k in ("user_id_str", "user_id", "uid", "id"):
                    val = obj.get(k)
                    if val is not None and str(val).strip().isdigit() and len(str(val).strip()) >= 8:
                        return str(val).strip()
                for v in obj.values():
                    r = _walk_uid(v)
                    if r:
                        return r
            elif isinstance(obj, list):
                for it in obj:
                    r = _walk_uid(it)
                    if r:
                        return r
            return ""
        uname = _walk(data)
        uid_str = _walk_uid(data)
        if uid_str:
            self._current_uid_str = uid_str
        if uname:
            return True, uname
        return False, ""

    def inject_cookies(self, cookies) -> bool:
        # One browser round trip instead of one per cookie. Session validation
        # remains in login_with_cookies; unsupported CDP falls back unchanged.
        cookies = list(cookies)
        payloads = []
        for cookie in cookies:
            name = cookie.get('name')
            domain = str(cookie.get('domain') or '.tiktok.com').lstrip('.')
            if not name or not (domain == 'tiktok.com' or domain.endswith('.tiktok.com')):
                continue
            payloads.append({
                'name': name, 'value': cookie.get('value') or '',
                'domain': '.' + domain, 'path': cookie.get('path') or '/',
                'secure': True, 'httpOnly': bool(cookie.get('httpOnly', False)),
            })
        if not payloads:
            return False
        try:
            self.driver.execute_cdp_cmd('Network.setCookies', {'cookies': payloads})
            return True
        except Exception:
            return self._inject_cookies_individually(cookies)

    def _inject_cookies_individually(self, cookies) -> bool:
        """Nạp cookie CDP; trả False nếu không cookie nào được Chrome nhận."""
        d = self.driver
        try:
            d.execute_cdp_cmd("Network.enable", {})
        except Exception:
            pass
        try:
            cur = d.current_url or ""
            if "tiktok.com" not in cur:
                d.get("https://www.tiktok.com/robots.txt")
        except Exception:
            pass
        skipped = 0
        loaded = 0
        for c in cookies:
            name, value = c.get("name"), c.get("value")
            if not name:
                skipped += 1
                continue
            domain = (c.get("domain") or ".tiktok.com").lstrip(".")
            if "tiktok" not in domain:
                skipped += 1
                continue
            payload = {
                "name": name,
                "value": value or "",
                "domain": "." + domain if not str(c.get("domain") or "").startswith(".") else (c.get("domain") or ".tiktok.com"),
                "path": c.get("path") or "/",
                "secure": True,
                "httpOnly": bool(c.get("httpOnly", False)),
            }
            try:
                response = d.execute_cdp_cmd("Network.setCookie", payload)
                if (isinstance(response, dict)
                        and response.get("success") is False):
                    skipped += 1
                else:
                    loaded += 1
            except Exception:
                skipped += 1
        if skipped:
            self.log(f"Nạp cookie CDP ({skipped} cookie bị bỏ qua).")
        return loaded > 0

    def reset_account_session(self) -> bool:
        """Xóa phiên cũ nhưng giữ nguyên page/runtime TikTok để đổi account nhanh."""
        d = self.driver
        if d is None or getattr(d, "_is_unresponsive_stub", False):
            return False
        self._driver_unresponsive = False
        client_config = getattr(getattr(d, "command_executor", None), "_client_config", None)
        if client_config is not None:
            try:
                client_config.timeout = 25
            except Exception:
                pass
        try:
            d.execute_cdp_cmd("Network.clearBrowserCookies", {})
            d.execute_script("""
                try { delete window.__alc_comment_mutations; } catch (_) {}
                window.__tt_comment_bodies = [];
                window.__tt_comment_nodes = Object.create(null);
            """)
        except Exception:
            return False
        self.last_proxy_blocked = False
        self.last_digg_expired = False
        self.last_session_invalid = False
        self.last_shadow_banned = False
        self.last_login_username = ""
        self.last_like_count = 0
        self.last_liked = []
        self.last_like_failure = ""
        self.last_captcha_blocked = False
        return True

    def _dump_login_fail(self, reason: str, folder: str = "cookie_fail"):
        """Login cookie fail → log bối cảnh + screenshot."""
        d = self.driver
        info = {}
        try:
            info = d.execute_script("""
                const out = {};
                out.title = document.title || '';
                out.loginBtn = !!document.querySelector(
                    "[data-e2e='top-login-button'], a[href*='/login']");
                out.captcha = !!document.querySelector(
                    ".captcha-verify-container, [id*='captcha-verify']");
                out.text = (document.body ? document.body.innerText : '')
                    .replace(/\\s+/g, ' ').trim().slice(0, 120);
                return out;
            """) or {}
        except Exception:
            pass
        try:
            url = (d.current_url or "")[:90]
        except Exception:
            url = "?"
        self.log(f"❌ Cookie không đăng nhập được — {reason}. "
                 f"URL={url} title={info.get('title', '')!r:.60} "
                 f"loginBtn={info.get('loginBtn')} captcha={info.get('captcha')} "
                 f"text={info.get('text', '')!r:.80}")
        try:
            from pathlib import Path as _P
            _d = _P(__file__).parent / "logs" / folder
            _d.mkdir(parents=True, exist_ok=True)
            _d.joinpath(f"fail_{int(time.time())}_{(self.prefix or 'acc')}.png") \
                .write_bytes(d.get_screenshot_as_png())
        except Exception:
            pass

    def dump_comment_debug(self, reason: str = "panel"):
        """Ghi logs/comment_debug: png + diag.json + snippet.html. Tra ve path."""
        d = self.driver
        if d is None:
            return ""
        try:
            from pathlib import Path as _P
            stamp = time.strftime("%Y%m%d-%H%M%S")
            tag = re.sub(r"[^a-zA-Z0-9._-]+", "_", (self.prefix or "acc"))[:40]
            folder = _P(__file__).resolve().parent / "logs" / "comment_debug" / f"{stamp}_{tag}"
            folder.mkdir(parents=True, exist_ok=True)
            diag = d.execute_script(r"""
                const out = {};
                out.url = location.href;
                out.title = document.title || '';
                out.ua = navigator.userAgent;
                out.vw = innerWidth; out.vh = innerHeight;
                out.de2e = [...document.querySelectorAll('[data-e2e]')]
                  .map(e => e.getAttribute('data-e2e'));
                out.playSide = !!document.querySelector("[data-e2e='play-side-comment']");
                out.commentIcon = !!document.querySelector("[data-e2e='comment-icon']");
                out.tuxOpen = !!document.querySelector('.tux-base-dialog--open');
                out.appCta = !![...document.querySelectorAll('button,a,p,span')]
                  .some(e => /mở tiktok|open tiktok/i.test((e.innerText||'').slice(0,40)));
                out.clsComment = [...document.querySelectorAll('[class]')]
                  .flatMap(e => [...e.classList])
                  .filter(s => /comment|Comment|tux-dialog|CommentSheet/i.test(s))
                  .filter((v,i,a) => a.indexOf(v)===i).slice(0,40);
                out.text = (document.body.innerText||'').replace(/\s+/g,' ').trim().slice(0,400);
                out.hookBodies = (window.__tt_comment_bodies||[]).length;
                return out;
            """) or {}
            diag["reason"] = reason
            (folder / "diag.json").write_text(
                json.dumps(diag, ensure_ascii=False, indent=2), encoding="utf-8")
            html = d.execute_script(
                "return document.documentElement.outerHTML") or ""
            (folder / "page.html").write_text(html[:800000], encoding="utf-8", errors="replace")
            (folder / "shot.png").write_bytes(d.get_screenshot_as_png())
            self.log(f"DEBUG panel → {folder.resolve()}")
            return str(folder)
        except Exception as e:
            self.log(f"DEBUG dump lỗi: {e}")
            return ""
    def login_with_cookies(self, cookies):
        """CDP cookie + passport. False = session chet (runner relogin)."""
        self.last_login_username = ""
        if not self.inject_cookies(cookies):
            self.log("Cookie chưa được Chrome nhận; không kết luận account hết hạn.")
            return False
        expected = {str(c.get("name")): str(c.get("value") or "")
                    for c in cookies if c.get("name") in
                    ("sessionid", "sessionid_ss", "sid_tt") and c.get("value")}
        try:
            # WebDriver.get_cookies() only reads the current page's domain.
            # A new browser starts on about:blank, even after CDP injection.
            try:
                stored = self.driver.execute_cdp_cmd('Network.getCookies', {
                    'urls': ['https://www.tiktok.com/', 'https://www.tiktok.com/login/']
                })['cookies']
            except Exception:
                from urllib.parse import urlsplit
                host = urlsplit(self.driver.current_url or '').hostname or ''
                if host != 'tiktok.com' and not host.endswith('.tiktok.com'):
                    self.driver.get('https://www.tiktok.com/robots.txt')
                stored = self.driver.get_cookies()
            actual = {str(c.get("name")): str(c.get("value") or "")
                      for c in stored}
            if expected and not any(actual.get(k) == v for k, v in expected.items()):
                self.log("Cookie session chưa nạp đúng vào browser; giữ nguyên dữ liệu đã lưu.")
                return False
        except Exception:
            pass
        ok, uname = self.check_live_api()
        if not ok:
            stop = getattr(self, "stop_event", None)
            if stop is not None and stop.wait(0.25):
                return False
            if stop is None:
                time.sleep(0.25)
            ok, uname = self.check_live_api()
        if ok:
            self.last_login_username = uname or ""
            if uname:
                self.log(f"Cookie OK {uname}")
            return True
        self.log("Cookie chưa được API xác nhận sau 2 lần; có thể do phiên hết hạn hoặc proxy/API lỗi.")
        return False

    def is_login_wall(self) -> bool:
        """TikTok đẩy modal bắt login (login-modal) — session THỰC TẾ đã chết
        dù cookie còn đọc được trang. Khi modal này hiện, mọi click action
        (comment/like) đều trượt hoặc bật lại modal → cần DIE acc, không retry.
        """
        try:
            return any(e.is_displayed() for e in self.driver.find_elements(
                By.CSS_SELECTOR, "[data-e2e='login-modal'], [class*='LoginContainer']"))
        except Exception:
            return False

    def close_login_modal(self):
        """Đóng login-modal nếu đang hiện (để DOM sạch cho thao tác sau)."""
        try:
            for btn in self.driver.find_elements(
                    By.CSS_SELECTOR, "[data-e2e='modal-close-inner-button'], "
                                     "[data-e2e='login-modal'] [class*='close']"):
                if btn.is_displayed():
                    btn.click()
                    self._rsleep(0.5)
                    return True
        except Exception:
            pass
        return False

    def handle_captcha(self, uid: str = "") -> bool:
        """Detect and solve supported CAPTCHA chains without retrying an empty shell.

        Returns True when a CAPTCHA wrapper was observed, including terminal
        provider/TikTok failures; False means no CAPTCHA was present.
        """
        uid = uid or (self.prefix or "")
        if not hasattr(self, "_bot_captcha_lock"):
            self._bot_captcha_lock = threading.Lock()
        with self._bot_captcha_lock:
            self.last_captcha_blocked = False

            def _terminal(message: str) -> bool:
                self.last_captcha_blocked = True
                self.log(message)
                return True

            solved_any = False
            unchanged_failures = 0
            for round_ in range(6):
                if self.stop_event.is_set():
                    return True
                if not captcha_present(self.driver):
                    if solved_any:
                        self.log(f"[{uid}] ✅ Hết captcha ({round_} lượt).")
                    return solved_any
                if solve_press_and_hold_captcha(
                        self.driver, self.stop_event, hold_seconds=8):
                    self.log(
                        f"[{uid}] 🖱️ Đã giữ nút xác minh; "
                        "chờ TikTok kiểm tra...")
                    state, _ = _wait_captcha_transition(
                        self.driver, timeout=6.0)
                    if state == "gone":
                        return True
                    if state == "stalled":
                        return _terminal(
                            f"[{uid}] ⛔ Khung CAPTCHA còn nhưng đề đã biến mất; "
                            "TikTok chưa xác minh hoặc đang báo lỗi. Dừng account.")
                    solved_any = True
                    continue

                state, before_signature = _wait_captcha_transition(
                    self.driver, timeout=4.0)
                if state == "gone":
                    return True
                if state == "stalled":
                    return _terminal(
                        f"[{uid}] ⛔ Khung CAPTCHA còn nhưng không có đề "
                        "puzzle/rotate/shapes để giải. Không gọi OMO lại; "
                        "dừng account.")

                try:
                    solver = make_captcha_solver(
                        self.driver, log_fn=self.log)
                    if solver is None:
                        return _terminal(
                            f"[{uid}] ⚠️ Có CAPTCHA nhưng không có solver "
                            f"(thiếu key solver hoặc thư viện) → dừng account.")
                    self.log(
                        f"[{uid}] 🤖 Tự giải CAPTCHA qua "
                        f"{load_cap_provider().upper()} (lượt {round_ + 1})...")
                    solver.solve_captcha_if_present(
                        captcha_detect_timeout=8, retries=1)
                    state, after_signature = _wait_captcha_transition(
                        self.driver, timeout=6.0)
                    if state == "gone":
                        return True
                    if state == "stalled":
                        return _terminal(
                            f"[{uid}] ⛔ OMO đã gửi đáp án nhưng TikTok chỉ còn "
                            "khung xác minh rỗng/lỗi. Dừng để không trừ tiền tiếp.")
                    if after_signature != before_signature:
                        solved_any = True
                        unchanged_failures = 0
                        self.log(
                            f"[{uid}] ↻ TikTok đã chuyển sang đề CAPTCHA kế tiếp.")
                        continue
                    unchanged_failures += 1
                    if unchanged_failures >= 2:
                        return _terminal(
                            f"[{uid}] ⛔ Cùng một đề CAPTCHA bị từ chối 2 lần; "
                            "dừng account thay vì gọi OMO liên tục.")
                    self.log(
                        f"[{uid}] ⚠️ Đề CAPTCHA vẫn nguyên sau đáp án; "
                        "thử lại đúng một lần...")
                except Exception as e:
                    err = str(e)
                    low = err.lower()
                    self.log(f"[{uid}] ⚠️ Tự giải lỗi ({err[:120]})")
                    if "neither puzzle, shapes, or rotate" in low:
                        state, _ = _wait_captcha_transition(
                            self.driver, timeout=3.0)
                        if state == "gone":
                            return True
                        if state == "stalled":
                            return _terminal(
                                f"[{uid}] ⛔ TikTok chỉ còn khung CAPTCHA rỗng; "
                                "không gọi OMO lại, dừng account.")
                    if any(k in low for k in (
                            "internal server error", "omo http 5",
                            "error_job_status")):
                        return _terminal(
                            f"[{uid}] ⛔ Dịch vụ giải CAPTCHA đang lỗi; "
                            "dừng retry account này.")
                    if any(k in low for k in (
                            "package_exhausted", "error_zero_balance",
                            "insufficient", "invalid key", "error_key")):
                        llm_key = load_llm_key()
                        if llm_key and load_cap_provider() == "omo":
                            try:
                                from tiktok_captcha_solver import SeleniumSolver  # type: ignore
                                base, model = load_llm_config()
                                fallback = SeleniumSolver(
                                    self.driver, "llm", mouse_step_size=2)
                                fallback.client = LLMApiClient(
                                    llm_key, base, model, log_fn=self.log)
                                self.log(
                                    f"[{uid}] 🤖 OMO hết gói → fallback LLM...")
                                fallback.solve_captcha_if_present(
                                    captcha_detect_timeout=8, retries=1)
                                state, _ = _wait_captcha_transition(
                                    self.driver, timeout=6.0)
                                if state == "gone":
                                    return True
                            except Exception as e2:
                                self.log(
                                    f"[{uid}] ⚠️ LLM fallback: "
                                    f"{str(e2)[:100]}")
                        return _terminal(
                            f"[{uid}] ⛔ Solver hết gói — captcha còn, "
                            "không retry OMO.")
                time.sleep(0.8)
            return _terminal(
                f"[{uid}] ⚠️ CAPTCHA chưa hết sau 6 đề khác nhau; "
                "dừng account.")

    def get_username(self) -> str:
        import re
        d = self.driver

        # Cách 1: đọc từ script tag __UNIVERSAL_DATA_FOR_REHYDRATION__ (TikTok nhúng vào mọi page)
        try:
            val = d.execute_script("""
                try {
                    const el = document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__');
                    if (el) {
                        const data = JSON.parse(el.textContent);
                        const scope = data.__DEFAULT_SCOPE__ || data || {};
                        const appCtx = scope['webapp.app-context'] || {};
                        if (appCtx.user && appCtx.user.uniqueId) {
                            return String(appCtx.user.uniqueId);
                        }
                        const str = JSON.stringify(scope);
                        const m = str.match(/"uniqueId":"([^"]+)"/);
                        if (m) return m[1];
                    }
                } catch(e) {}
                return null;
            """)
            if val and isinstance(val, str):
                return "@" + val
        except Exception:
            pass

        # Cách 2: tìm uniqueId trong các state object phổ biến
        try:
            val = d.execute_script("""
                try {
                    const keys = ['__INITIAL_STATE__','__STORE__','__SSR_DATA__',
                                  'TIKTOK_SSR_DATA','__APP_STATE__'];
                    for (const k of keys) {
                        if (!window[k]) continue;
                        const m = JSON.stringify(window[k]).match(/"uniqueId":"([^"]+)"/);
                        if (m) return m[1];
                    }
                } catch(e) {}
                return null;
            """)
            if val and isinstance(val, str):
                return "@" + val
        except Exception:
            pass

        # Cách 3: đọc href từ thẻ <a> trong DivMainNavContainer (cấu trúc: h2 > div > a > button > div)
        try:
            for el in d.find_elements(By.CSS_SELECTOR,
                                      "[class*='DivMainNavContainer'] a, [class*='MainNavContainer'] a"):
                href = (el.get_attribute("href") or "").split("?")[0].rstrip("/")
                m = re.search(r'/@([^/]+)$', href)
                if m and "." not in m.group(1):
                    return "@" + m.group(1)
        except Exception:
            pass

        # Cách 4: click thẻ <a> chứa "Profile" → đọc URL → quay lại
        try:
            btn = d.find_element(
                By.XPATH,
                "//*[contains(@class,'TUXButton-label') and contains(text(),'Profile')]"
            )
            # Leo lên tìm thẻ <a> gần nhất
            link = btn.find_element(By.XPATH, "ancestor::a[1]")
            href = (link.get_attribute("href") or "").split("?")[0].rstrip("/")
            m = re.search(r'/@([^/]+)$', href)
            if m and "." not in m.group(1):
                return "@" + m.group(1)
            # Nếu không có href thì click rồi đọc URL
            d.execute_script("arguments[0].click();", link)
            time.sleep(0.8)
            url = d.current_url.split("?")[0].rstrip("/")
            m = re.search(r'/@([^/]+)$', url)
            if m and "." not in m.group(1):
                d.back()
                time.sleep(0.5)
                return "@" + m.group(1)
        except Exception:
            pass

        return ""

    def open_link(self, link):
        self.last_proxy_blocked = False
        self._driver_unresponsive = False
        d = getattr(self, "driver", None)
        if d is not None:
            client_config = getattr(getattr(d, "command_executor", None), "_client_config", None)
            if client_config is not None:
                try:
                    client_config.timeout = 25
                except Exception:
                    pass
        if link:
            self._last_known_url = str(link).strip()
            if "robots.txt" not in str(link):
                self._last_target_link = str(link).strip()
        self.log(f"Mở: {link}")
        self.driver.get(link)
        try:
            WebDriverWait(self.driver, 6).until(
                lambda dr: dr.execute_script(
                    "return document.readyState") in ("interactive", "complete"))
        except TimeoutException:
            pass
        # 403 = TikTok chặn IP proxy (trang từ chối truy cập) — đánh dấu để
        # caller bỏ qua acc này (acc KHÔNG die), đổi proxy sẽ vào bình thường.
        try:
            import navigation_health
            nav = navigation_health.inspect_navigation(self.driver)
            if nav.get("blocked"):
                self.last_proxy_blocked = True
                self.log("⛔ 403 — TikTok chặn IP proxy (acc không die, cần đổi proxy).")
                return
        except Exception:
            try:
                body = (self.driver.find_element(By.TAG_NAME, "body").text or "")[:300]
                if "403 Forbidden" in body or "truy cập bị từ chối" in body.lower():
                    self.last_proxy_blocked = True
                    self.log("⛔ 403 — TikTok chặn IP proxy (acc không die, cần đổi proxy).")
                    return
            except Exception:
                pass
        self.handle_captcha()
        try:
            self.driver.execute_script("""
                document.querySelectorAll('video,audio').forEach(function(m){
                    try{ m.pause(); m.muted=true; m.removeAttribute('src');
                         m.load && m.load(); }catch(e){}
                });
            """)
        except Exception:
            pass

    def like_video(self):
        d = self.driver
        like_btn = None
        for sel in [
            # Mobile web: nút tim nằm trong <div data-e2e='play-side-like'>
            # (KHÔNG phải button/span → các selector 'button[data-e2e*="like"]'
            #  bên dưới không khớp, phải thêm riêng cho mobile).
            "[data-e2e='play-side-like']",
            "[data-e2e='like-icon']",
            "[data-e2e='browse-like-icon']",
            "[data-e2e='video-like-button']",
            "button[data-e2e*='like']",
            "span[data-e2e*='like']",
        ]:
            try:
                like_btn = WebDriverWait(d, 8).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, sel))
                )
                break
            except TimeoutException:
                pass
        if not like_btn:
            self.log("❌ Không tìm thấy nút tim video.")
            return False
        d.execute_script("arguments[0].scrollIntoView({block:'center'});", like_btn)
        self._rsleep(0.4)   # dừng lại "ngắm" trước khi bấm, như người thật

        # Trạng thái TRƯỚC click. Nếu đã like sẵn (aria-pressed=true) thì coi là
        # thành công luôn — tránh bấm lần 2 gỡ tim.
        if self._is_video_liked(like_btn):
            self.log("✅ Video đã được tim từ trước.")
            return True

        d.execute_script("arguments[0].click();", like_btn)
        self._rsleep(0.6)

        # XÁC MINH THẬT: click JS trên phần tử wrapper có thể không trigger event
        # (hoặc TikTok chặn do cookie chết/chưa login thật/cần captcha) → nút KHÔNG
        # đổi sang trạng thái đã-like. Chỉ báo thành công khi aria-pressed/class
        # thực sự chuyển. Chờ tối đa ~2s vì UI cập nhật bất đồng bộ.
        for _ in range(8):
            if self._is_video_liked(like_btn):
                self.log("✅ Đã tim video.")
                return True
            self._rsleep(0.25)
        self.log("❌ Bấm tim nhưng trạng thái không đổi (có thể acc lỗi/cookie chết).")
        return False

    def _is_video_liked(self, like_btn):
        """True nếu nút tim video đang ở trạng thái ĐÃ like.

        TikTok đánh dấu bằng aria-pressed='true' trên nút (hoặc phần tử con), và
        đổi màu SVG sang đỏ (fill rgb(254,44,85) — màu tim của TikTok). Kiểm tra
        cả hai cho bền: aria-pressed trước, fallback sang màu fill của SVG.
        """
        try:
            return bool(self.driver.execute_script("""
                const el = arguments[0];
                const cand = [el, ...el.querySelectorAll('*')];
                for (const n of cand) {
                    const ap = n.getAttribute && n.getAttribute('aria-pressed');
                    if (ap === 'true') return true;
                }
                // Fallback: SVG path fill = màu tim đỏ của TikTok
                const svgs = el.querySelectorAll('svg path, svg');
                for (const s of svgs) {
                    const f = getComputedStyle(s).fill || '';
                    if (f.includes('254, 44, 85') || f.includes('255, 44, 85')) return true;
                }
                // Fallback cuối: class chứa từ khóa trạng thái đã-like
                for (const n of cand) {
                    const c = (n.className && n.className.baseVal !== undefined)
                              ? n.className.baseVal : (n.className || '');
                    if (/active|liked|is-liked/i.test(String(c))) return true;
                }
                return false;
            """, like_btn))
        except Exception:
            return False

    def open_comment_panel(self):
        """Click nút comment để mở panel nếu chưa mở. Thử tối đa 3 lần."""
        d = self.driver
        import comment_panel_wait as panel_wait

        _PANEL = (
            "[class*='DivCommentListContainer'], [class*='CommentListContainer'], "
            "[data-e2e='comment-list'], [data-e2e='browse-comment-list'], "
            "[data-e2e='comment-container'], [data-e2e='comments-list'], "
            "[class*='DivCommentMain'], [class*='CommentMainContainer'], "
            "[class*='CommentSheet'], [class*='commentListContainer'], "
            "[class*='DivCommentList'], "
            ".tux-base-dialog--open [class*='Comment'], "
            ".tux-dialog__content-container [class*='omment'], "
            "[class*='DivCommentPanel'], [class*='CommentPanel']"
        )
        _BTNS = [
            "[data-e2e='play-side-comment']",
            "[data-e2e='comment-icon']",
            "[data-e2e='browse-comment-icon']",
            "[data-e2e='video-comment']",
            "[data-e2e='comment-count']",
            "[data-e2e='comments']",
            "[class*='SectionActionBarContainer'] [class*='ButtonActionItemV1'] button[aria-label*='omment']",
            "[class*='SectionActionBarContainer'] button[aria-label*='omment']",
            "[data-e2e='comment-count']",
            # Biến thể trang photo: nút comment mang data-e2e='comments'
            # (log chẩn đoán từng bắt được de2e:["comments"] mà click trượt).
            "[data-e2e='comments']",
            # Action bar (trang photo): KHÔNG lấy mọi button — nút đầu là
            # "Bạn có thể thích" (like video) → click nhầm like mà panel không
            # mở. Chỉ nhận nút có label/text chứa 'comment/Bình luận'.
            "[class*='SectionActionBarContainer'] [class*='ButtonActionItemV1'] button[aria-label*='omment']",
            "[class*='SectionActionBarContainer'] button[aria-label*='omment']",
            # Label UI tiếng Việt 'Bình luận' — không chứa 'comment'.
            "[class*='SectionActionBarContainer'] button[aria-label*='Bình luận'], "
            "[class*='SectionActionBarContainer'] button[aria-label*='luận']",
            "button[aria-label*='omment']",
        ]

        # Avoid duplicate selectors and return as soon as the UI is ready.
        _BTNS = list(dict.fromkeys(_BTNS))

        def _wait_comment_ui():
            self._check_stop()
            try:
                d.execute_async_script(r"""
                const selectors=arguments[0],done=arguments[arguments.length-1];
                const visible=()=>Array.from(document.querySelectorAll(selectors)).some(
                    element=>{const r=element.getBoundingClientRect(),s=getComputedStyle(element);
                        return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden';});
                if(visible()){done(true);return;}
                let settled=false,observer,timer,poll;
                const finish=value=>{if(settled)return;settled=true;
                    observer?.disconnect();clearTimeout(timer);clearInterval(poll);done(value);};
                observer=new MutationObserver(()=>{if(visible())finish(true);});
                observer.observe(document.documentElement,{subtree:true,childList:true,attributes:true});
                timer=setTimeout(()=>finish(false),2000);
                poll=setInterval(()=>{if(visible())finish(true);},100);
                if(visible())finish(true);
                """, _PANEL + ', ' + ', '.join(_BTNS))
            except Exception:
                # Retain the old maximum settling time if async observation fails.
                self.stop_event.wait(2)
            self._check_stop()

        def _click_mobile_btn(parent):
            """Tap nút action mobile mở bottom-sheet mà KHÔNG bị navigate.

            2 vấn đề với bản cũ (chỉ gọi .click()):
            1. TikTok mobile mở comment-sheet qua handler pointer/touch —
               .click() thuần (chỉ phát MouseEvent) thường BỊ BỎ QUA → sheet
               không bay lên (đó là lý do panel lúc mở lúc không).
            2. preventDefault cũ query 'a' trong CON của nút, nhưng data-e2e
               nằm trên chính <a> → không chặn được navigation thực sự.

            Fix: phát chuỗi sự kiện thật (touch→pointer→mouse→click, bubbles)
            để gọi đúng handler TikTok đang lắng nghe + gắn preventDefault
            capture-phase trên element và MỌI <a> tổ tiên (kể cả chính nó).
            """
            d.execute_script(
                "(function(el){"
                # 0. Cuộn nút vào giữa viewport — tránh bị overlay che/off-screen.
                "try{el.scrollIntoView({block:'center',inline:'center'});}catch(e){}"
                # 1. Chặn navigation trên element + mọi <a> tổ tiên (kể cả chính nó).
                "var bn=function(e){try{e.preventDefault();}catch(_){}};"
                "var p=el;"
                "while(p&&p.tagName){"
                "if(p.tagName.toLowerCase()==='a')"
                "p.addEventListener('click',bn,{capture:true,once:true});"
                "p=p.parentElement;}"
                # 2. Tạo toạ độ tâm nút cho các sự kiện.
                "var r=el.getBoundingClientRect();"
                "var cx=r.left+r.width/2,cy=r.top+r.height/2;"
                "var base={bubbles:true,cancelable:true,clientX:cx,clientY:cy,"
                "button:0,pointerId:1,pointerType:'touch'};"
                "var fire=function(type,init){"
                "var C=window[type];"
                "if(!C&&type.indexOf('pointer')===0)C=window.MouseEvent;"
                "if(!C)return;"
                "try{el.dispatchEvent(new C(type,init));}catch(e){}};"
                # 3. Touch (ưu tiên) — nhiều handler TikTok chỉ nghe touchend.
                "try{"
                "var t=new Touch({identifier:1,target:el,clientX:cx,clientY:cy});"
                "el.dispatchEvent(new TouchEvent('touchstart',{bubbles:true,"
                "cancelable:true,touches:[t],targetTouches:[t],changedTouches:[t]}));"
                "el.dispatchEvent(new TouchEvent('touchend',{bubbles:true,"
                "cancelable:true,touches:[],targetTouches:[],changedTouches:[t]}));"
                "}catch(e){}"
                # 4. Pointer + mouse + click — bao phủ các handler kiểu còn lại.
                "fire('pointerdown',base);"
                "fire('mousedown',base);"
                "fire('pointerup',base);"
                "fire('mouseup',base);"
                "fire('click',base);"
                "})", parent)

        # The comment UI can be usable before unrelated page assets finish.
        # Keep the same wait budget and all existing click/reload fallbacks.
        def _comment_ui_or_page_ready(driver):
            self._check_stop()
            return driver.execute_script(r"""
                const selectors = arguments[0];
                return Array.from(document.querySelectorAll(selectors)).some(el => {
                    const rect=el.getBoundingClientRect(), style=getComputedStyle(el);
                    return rect.width>0 && rect.height>0 &&
                           style.display!=='none' && style.visibility!=='hidden';
                });
            """, _PANEL + ', ' + ', '.join(_BTNS))
        try:
            WebDriverWait(d, 10, poll_frequency=0.2).until(_comment_ui_or_page_ready)
        except TimeoutException:
            pass

        url_before = (d.current_url or "").split("?")[0]

        def _reload_page():
            """Reload trang hiện tại + đợi nút comment xuất hiện."""
            try:
                d.get(d.current_url)
                WebDriverWait(d, 6, poll_frequency=0.2).until(_comment_ui_or_page_ready)
            except Exception:
                pass

        def _try_clicks(reload_between=True):
            """2 vòng click (fresh + reload). Trả True nếu panel mở."""
            # Trang photo load lần ĐẦU (qua redirect vt.tiktok.com) thường render
            # bản nghèo KHÔNG nút comment (de2e rỗng) — RELOAD 1 lần là hiện đầy đủ
            # (verify bằng debug: lần 1 fail, reload vào ngay).
            for round_ in range(2):
                self._check_stop()
                if round_ == 1:
                    if not reload_between:
                        return False
                    self.log("↻ Trang chưa có nút comment — reload thử lại...")
                    _reload_page()
                clicked = set()
                for attempt in range(3):
                    if panel_wait.visible(d, _PANEL):
                        return True   # panel đã mở — không cần log
                    if attempt > 0:
                        _wait_comment_ui()

                    # UI tab-bar mới — click tab theo text (xem click_comment_tab).
                    if click_comment_tab(d):
                        if panel_wait.wait_visible(self, _PANEL, 4):
                            return True
                    # Query all variants together. A hidden panel is not ready,
                    # and nested icons referring to one button are clicked once.
                    candidates = panel_wait.buttons(d, ', '.join(_BTNS))
                    for btn in candidates:
                        identity = getattr(btn, 'id', None) or id(btn)
                        if identity in clicked:
                            continue
                        clicked.add(identity)
                        self._check_stop()
                        try:
                            marker = btn.get_attribute('data-e2e') or ''
                            _click_mobile_btn(btn) if 'play-side-' in marker else \
                                d.execute_script("arguments[0].click();", btn)
                        except Exception:
                            continue
                        if panel_wait.wait_visible(self, _PANEL, 3):
                            return True
                        break
                # Round 1 hết lượt mà CHƯA thấy nút nào → đừng cố nữa, lên reload.
                if not any(d.find_elements(By.CSS_SELECTOR, sel) for sel in _BTNS):
                    continue
            return False

        if _try_clicks():
            return True

        # ── CAPTCHA chèn ngang: TikTok có thể đẩy captcha SAU khi click comment
        # (chặn đúng thao tác) — lúc này trang không còn nút comment (de2e rỗng).
        # Detect lại bằng bộ selector rộng rồi giải.
        # Selector mình vẫn có thể sót biến thể → thêm BLIND: gọi thẳng thư
        # viện solver (nó detect theo cách riêng, khớp captcha nó giải được).
        had_captcha = False
        if captcha_present(d):
            self.log("⚠️ Panel fail vì CAPTCHA đang chèn ngang → giải rồi thử lại...")
            self.handle_captcha()
            had_captcha = True
        else:
            # Blind check: detect bằng CHÍNH thư viện solver trước (4s) — chỉ
            # giải + reload khi THẬT SỰ có captcha. Bản trước gọi solve rồi
            # coi như có captcha → ép reload oan → reload sinh ra trang bản
            # nghèo không nút comment (đã gặp hàng loạt sau blind check).
            solver = make_captcha_solver(d, log_fn=self.log)
            if solver:
                try:
                    self.log("🤖 Blind check captcha (thư viện tự dò)...")
                    if solver.captcha_is_present(captcha_detect_timeout=4):
                        solver.solve_captcha_if_present(
                            captcha_detect_timeout=4, retries=1)
                        had_captcha = True
                except Exception:
                    pass
        # Sau khi giải captcha: trang thường KHÔNG tự hồi phục nút comment —
        # reload để render lại đầy đủ rồi chạy LẠI toàn bộ vòng click
        # (trước đây chỉ click 1 lần rồi bỏ → acc kết thúc 0 cmt oan).
        if had_captcha and not captcha_present(d):
            self.log("↻ Đã giải captcha — reload trang rồi thử panel lại...")
            _reload_page()
            # Vòng 2 không reload nữa (vừa reload ở trên) — chỉ click.
            if _try_clicks(reload_between=False):
                return True

        # ── Trang photo bị TikTok phục vụ BẢN NGHÈO (không có nút comment nào
        # — log chẩn đoán de2e rỗng): đổi URL /photo/ → /video/ (cùng aweme_id)
        # lấy UI video chuẩn có nút comment. Đã gặp ở phiên đáng ngờ sau captcha.
        try:
            cur = d.current_url or ""
            if "/photo/" in cur and not d.find_elements(By.CSS_SELECTOR, _PANEL):
                self.log("↻ Trang photo không có nút comment — thử bản video...")
                d.get(cur.split("?")[0].replace("/photo/", "/video/"))
                WebDriverWait(d, 15).until(
                    lambda dr: dr.execute_script(
                        "return document.readyState") == "complete")
                _wait_comment_ui()
                if _try_clicks(reload_between=False):
                    return True
        except Exception:
            pass

        # ── Dump chẩn đoán: phân biệt "click không mở" vs "mở nhưng detection sai"
        try:
            url_after = (d.current_url or "").split("?")[0]
            navigated = url_after != url_before
            diag = d.execute_script("""
            const out = {};
            out.de2e = [...document.querySelectorAll("[data-e2e]")]
                .map(e=>e.getAttribute("data-e2e"))
                .filter(s=>s&&s.toLowerCase().includes("comment"));
            out.cls = [...document.querySelectorAll("[class]")]
                .map(e=>[...e.classList].join(" "))
                .filter(s=>/comment/i.test(s) && !/icon|button|action|play-side/i.test(s))
                .slice(0,15);
            out.iframes = [...document.querySelectorAll("iframe")]
                .map(f=>(f.src||"").split("/")[2]||"").filter(Boolean).slice(0,5);
            // Bối cảnh trang: title + text đầu trang — nhận diện login-wall /
            // trang rỗng / redirect lạ mà bộ selector comment không thấy gì.
            out.title = document.title || '';
            out.text = (document.body ? document.body.innerText : '')
                .replace(/\\s+/g, ' ').trim().slice(0, 150);
            out.buttons = [...document.querySelectorAll('button')]
                .map(b => (b.innerText || b.getAttribute('aria-label') || '')
                    .trim()).filter(Boolean).slice(0, 12);
            return JSON.stringify(out);
            """)
            self.log(
                f"⚠️ Không mở được panel comment. "
                f"navigate={'CÓ (URL đổi → click sai đích)' if navigated else 'không'}. "
                f"data-e2e comment: {diag}")
            # Screenshot trang đang lỗi — mắt thường thấy ngay là gì
            # (login wall / trang trắng / panel thật nhưng selector trượt).
            try:
                from pathlib import Path as _P
                _d = _P(__file__).parent / "logs" / "panel_fail"
                _d.mkdir(parents=True, exist_ok=True)
                _d.joinpath(f"panel_fail_{int(time.time())}.png") \
                    .write_bytes(d.get_screenshot_as_png())
            except Exception:
                pass
        except Exception:
            pass

        self.log("⚠️ Không mở được panel comment.")
        return False

    def _get_comment_scroll_target(self):
        """Container overflow trong panel comment (UI sharing/dialog moi)."""
        d = self.driver
        el = d.execute_script("""
            const sels = [
              "[class*='DivCommentListContainer']","[class*='CommentListContainer']",
              "[data-e2e='comment-list']","[class*='DivCommentMain']",
              ".tux-base-dialog--open","[class*='tux-dialog__content']",
              "[class*='CommentSheet']","[class*='DivCommentPanel']"
            ];
            const seen = new Set();
            const roots = [];
            sels.forEach(s => document.querySelectorAll(s).forEach(e => {
              if (!seen.has(e)) { seen.add(e); roots.push(e); }
            }));
            function walk(n, acc) {
              if (!n) return;
              const st = getComputedStyle(n);
              if (/(auto|scroll)/.test(st.overflowY) && n.scrollHeight > n.clientHeight + 20)
                acc.push(n);
              for (const c of n.children) walk(c, acc);
            }
            let best = null, bh = 0;
            roots.forEach(r => {
              const acc = [];
              walk(r, acc);
              acc.forEach(n => {
                if (n.scrollHeight > bh) { bh = n.scrollHeight; best = n; }
              });
            });
            return best;
        """)
        return el

    def like_comment(self, target="", link="", max_scroll=30, find_first=False,
                     api_mode=False):
        if not api_mode:
            self.handle_captcha()
        d = self.driver
        self.last_like_count = 0
        self.last_found_count = 0
        self.last_liked = []
        self.last_like_failure = ""
        self.last_batch_complete = None

        aweme_id = extract_aweme_id(link) or extract_aweme_id(d.current_url) or ""
        if link and "robots.txt" not in str(link):
            if not getattr(self, "_last_target_link", ""):
                self._last_target_link = str(link).strip()
        cached_comments = []
        if not target.strip():
            comment_id = extract_comment_id(d.current_url) or extract_comment_id(link)
            if comment_id:
                self.log(f"Tìm comment cid={comment_id} trên panel...")
                try:
                    self.open_comment_panel()
                except Exception:
                    pass
                if self._like_by_id(comment_id):
                    self.last_like_count = 1
                    return True
                self.log("❌ Không tìm thấy comment qua ID trên UI.")
                return False
            now = time.time()
            for (video_id, _username), hit in list(_COMMENT_CACHE.items()):
                if (video_id == str(aweme_id)
                        and now - hit.get("ts", 0) < _COMMENT_CACHE_TTL):
                    cached_comments.extend(dict(c) for c in hit.get("comments", []))
            if not cached_comments:
                self.log(
                    "❌ Link chưa có cache comment. Chạy auto-comment trước "
                    "hoặc nhập @user.")
                return False
            target = "@cache"
            self.log(
                f"♻️ Dùng {len(cached_comments)} comment đã cache từ auto-comment.")

        target = target.strip()
        if not target.startswith("@"):
            target = "@" + target
        target_norm = target.lstrip("@").lower()
        mode_lbl = "comment đầu tiên" if find_first else "TẤT CẢ comment"
        action_lbl = "cache CID rồi tim bằng request đã ký" if api_mode else "cuộn panel, bấm tim"
        self.log(f"Tìm {mode_lbl} của @{target_norm} ({action_lbl})...")

        cache_key = (str(aweme_id), target_norm) if aweme_id else None
        comments = None
        if cached_comments:
            comments = cached_comments
        elif cache_key:
            lock = _comment_cache_lock(cache_key)
            got = lock.acquire(timeout=2)
            while not got:
                self._check_stop()
                got = lock.acquire(timeout=2)
            try:
                hit = _COMMENT_CACHE.get(cache_key)
                fresh = hit and (time.time() - hit["ts"]) < _COMMENT_CACHE_TTL
                if fresh and hit.get("rounds", 0) >= 1:
                    comments = [dict(c) for c in hit["comments"]]
                    self.log(
                        f"♻️ Cache {len(comments)} CID comment — bỏ qua bước "
                        "cuộn lấy danh sách.")
                else:
                    comments = self.harvest_comments_by_scroll(
                        target_norm, stop_at_first=find_first)
                    if comments:
                        _COMMENT_CACHE[cache_key] = {
                            "ts": time.time(),
                            "comments": [dict(c) for c in comments],
                            "rounds": 1,
                            "added": len(comments)}
                        self.log(f"📦 Scout đã cache {len(comments)} CID của @{target_norm}.")
            finally:
                lock.release()
        else:
            comments = self.harvest_comments_by_scroll(
                target_norm, stop_at_first=find_first)

        if comments:
            liked = 0
            if api_mode:
                selected = [
                    comment for comment in comments
                    if str(comment.get("cid") or "")
                ]
                seen_cids = set()
                unique = []
                for comment in selected:
                    key = (str(comment.get("cid") or ""),
                           str(comment.get("parent_cid") or ""))
                    if key not in seen_cids:
                        seen_cids.add(key)
                        unique.append(comment)
                selected = unique
                # A deep link identifies the exact CID even when an author is supplied.
                requested_cid = extract_comment_id(link)
                if requested_cid:
                    selected = [c for c in selected if str(c.get("cid") or "") == str(requested_cid)]
                    if not selected:
                        self.last_like_failure = "CID trong link không thuộc danh sách comment của user đã chọn"
                        self.log("Chưa gửi: " + self.last_like_failure)
                        return False
                if find_first:
                    selected = selected[:1]
                def _send_direct(rows, concurrency):
                    self._check_stop()
                    return self.api_digg_comments(
                        aweme_id, list(rows), digg_type=1,
                        concurrency=concurrency)

                concurrency = min(8, max(4, len(selected)))
                self.log(
                    f"↻ Gửi {len(selected)} CID, tối đa "
                    f"{concurrency} request song song...")
                started = time.perf_counter()
                results = _send_direct(
                    selected, concurrency=concurrency)
                retry_count = 0
                session_invalid = any(result[1] == 8 for result in results)
                def _retryable(result):
                    if result[0] or result[1] == 8:
                        return False
                    reason = str(result[2] or "").lower()
                    if result[1] == 429 or any(token in reason for token in (
                            "too many", "rate limit", "quá nhiều", "429")):
                        return False
                    if result[1] != -1:
                        return False
                    return any(token in reason for token in (
                        "failed to fetch", "timeout", "deadline",
                        "webpack runtime", "module loader",
                        "mutation comment/digg", "invalid_status",
                        "missing_status", "chưa xác nhận kết quả digg", "chưa gửi: comment context",
                    ))
                initial_retry_indices = [
                    index for index, result in enumerate(results)
                    if _retryable(result)
                ]
                if not session_invalid and 0 < len(initial_retry_indices) <= 3 and any(r[0] for r in results):
                    self.log(
                        f"↻ Retry nhanh {len(initial_retry_indices)} CID lỗi song song...")
                    time.sleep(0.15)
                    retry_results = _send_direct(
                        [selected[index] for index in initial_retry_indices],
                        concurrency=len(initial_retry_indices))
                    retry_count += len(initial_retry_indices)
                    session_invalid = any(result[1] == 8 for result in retry_results)
                    for index, result in zip(initial_retry_indices, retry_results):
                        results[index] = result
                elif not session_invalid and len(initial_retry_indices) > 3:
                    self._session_page_loaded = False
                    self._needs_page_reload = True
                    self._consecutive_invalid_status = getattr(self, "_consecutive_invalid_status", 0) + 1

                status_counts = {}
                reason_counts = {}
                for comment, result in zip(selected, results):
                    ok, api_code, reason = result
                    status_counts[api_code] = (
                        status_counts.get(api_code, 0) + 1)
                    if not ok:
                        reason_key = " ".join(str(reason or "không rõ").split())[:100]
                        reason_counts[reason_key] = (
                            reason_counts.get(reason_key, 0) + 1)
                    if ok and reason != 'already_present':
                        liked += 1
                        self.last_liked.append({
                            "author": (
                                comment.get("username") or target_norm),
                            "text": comment.get("text") or "",
                        })
                elapsed = time.perf_counter() - started
                codes = ", ".join(
                    f"{code}:{count}"
                    for code, count in sorted(status_counts.items()))
                ver = getattr(self, "last_digg_verification", None)
                ver_note = ""
                if isinstance(ver, dict) and liked > 0:
                    new_cnt = int(ver.get("changed_confirmed", 0) or 0)
                    old_cnt = int(ver.get("already_present", 0) or 0)
                    unk_cnt = int(ver.get("before_unknown", 0) or 0)
                    ver_note = f", tim mới +{new_cnt}, đã có sẵn {old_cnt}"
                    if unk_cnt > 0:
                        ver_note += f", trước chưa rõ {unk_cnt}"
                self.log(
                    f"⚡ Server xác nhận {liked}/{len(selected)} CID trong "
                    f"{elapsed:.1f}s ({concurrency} song song, "
                    f"retry {retry_count}"
                    f"{', status ' + codes if codes else ''}{ver_note}).")
                if reason_counts:
                    details = "; ".join(
                        f"{count}× {reason}"
                        for reason, count in list(reason_counts.items())[:3])
                    self.last_like_failure = details
                    self.log(f"↳ Lỗi cuối: {details}")
                self.last_found_count = len(selected)
                self.last_like_count = liked
                satisfied = sum(1 for result in results if result[0])
                self.last_batch_complete = bool(
                    selected and satisfied == len(selected))
                transport_failed = sum(1 for result in results if not result[0] and result[1] not in (0, -3))
                if selected and transport_failed < max(1, len(selected) // 2):
                    self._consecutive_invalid_status = 0
                elif selected and transport_failed >= max(1, len(selected) // 2):
                    self._session_page_loaded = False
                    self._needs_page_reload = True
                    if any(r[1] == -1 for r in results) and len(initial_retry_indices) <= 3:
                        self._consecutive_invalid_status = getattr(self, "_consecutive_invalid_status", 0) + 1
                if satisfied:
                    return True
                self.log("❌ Có CID nhưng chưa có tim được server xác nhận.")
                return False

            for comment in comments:
                preview = _short_text(comment.get("text") or "")
                snippet = (comment.get("text") or "").strip()[:48]
                try:
                    self.open_comment_panel()
                except Exception:
                    pass
                position = max(0, int(comment.get("position") or 0))
                scroll_top = max(0, int(comment.get("scroll_top") or 0))
                ok = bool(snippet) and self._ui_like_by_snippet(
                    snippet, position=position, scroll_top=scroll_top)
                self.log(
                    f'{"✅" if ok else "⚠️"} '
                    f'{"Tim UI" if ok else "Không bấm được tim UI"} '
                    f'"{preview}" của @{target_norm}.')
                if ok:
                    liked += 1
                    self.last_liked.append({
                        "author": comment.get("username") or target_norm,
                        "text": comment.get("text") or ""})
                if find_first and liked:
                    break
            self.last_found_count = len(comments)
            self.last_like_count = liked
            if liked:
                return True
            self.log("⚠️ Harvest có comment nhưng không bấm tim được → cuộn DOM.")


        # Chờ comment panel load lần đầu
        try:
            WebDriverWait(d, 10).until(lambda dr: len(dr.find_elements(
                By.CSS_SELECTOR,
                "[class*='CommentItemContainer'], [data-e2e='comment-list-item'], "
                "[class*='DivCommentListContainer'] > div")) > 0)
        except TimeoutException:
            self.log("⚠️ Chờ comment load timeout, tiếp tục...")

        use_href = target.strip().startswith("@")

        def _climb_to_comment(el):
            """Leo lên DOM tìm comment container gần nhất."""
            try:
                return el.find_element(By.XPATH,
                    "ancestor::*[contains(@class,'CommentItemWrapper') "
                    "or contains(@class,'CommentItem') "
                    "or @data-e2e='comment-list-item'][1]")
            except Exception:
                try:
                    return el.find_element(By.XPATH, "../../../..")
                except Exception:
                    return None

        def find_by_href():
            """Tìm qua a[href='/@username'] — dùng khi nhập @user."""
            links = d.find_elements(By.CSS_SELECTOR,
                f"a[href='/@{target_norm}'], a[href^='/@{target_norm}?']")
            for link in links:
                item = _climb_to_comment(link)
                if item:
                    return item
            return None

        def find_by_text():
            """Tìm qua nội dung span.TUXText — dùng khi nhập text comment."""
            try:
                spans = d.find_elements(By.XPATH,
                    f"//span[contains(@class,'TUXText') and "
                    f"contains(translate(normalize-space(.),"
                    f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ ÀÁÂÃÈÉÊÌÍÒÓÔÕÙÚĂĐĨŨƠƯẠẢẤẦẨẪẬẮẰẲẴẶẸẺẼẾỀỂỄỆỈỊỌỎỐỒỔỖỘỚỜỞỠỢỤỦỨỪỬỮỰỲỴÝỶỸ',"
                    f"'abcdefghijklmnopqrstuvwxyz àáâãèéêìíòóôõùúăđĩũơưạảấầẩẫậắằẳẵặẹẻẽếềểễệỉịọỏốồổỗộớờởỡợụủứừửữựỳỵýỷỹ'),"
                    f"'{target_norm}')]")
                for span in spans:
                    item = _climb_to_comment(span)
                    if item:
                        return item
            except Exception:
                pass
            return None

        def _expand_replies():
            """Click tất cả nút 'View X replies' / 'Xem X câu trả lời'."""
            clicked = False
            # Selector chính xác từ DOM: DivViewRepliesContainer bên trong DivViewMoreRepliesOptionsContainer
            for sel in [
                "[class*='DivViewRepliesContainer']",
                "[class*='DivViewMoreRepliesOptionsContainer'] > div",
                "[class*='DivViewMoreRepliesOptionsContainer']",
                "[class*='DivViewMoreRepliesWrapper']",
            ]:
                btns = d.find_elements(By.CSS_SELECTOR, sel)
                for btn in btns:
                    try:
                        d.execute_script("arguments[0].click();", btn)
                        clicked = True
                    except Exception:
                        pass
                if clicked:
                    time.sleep(0.5)
                    break
            # Fallback text: "View X replies" / "Xem X câu trả lời"
            if not clicked:
                try:
                    spans = d.find_elements(By.XPATH,
                        "//*[contains(@class,'TUXText') and ("
                        "contains(text(),'replies') or contains(text(),'Xem') or "
                        "contains(text(),'câu trả lời') or contains(text(),'View')"
                        ")]")
                    for span in spans:
                        try:
                            d.execute_script("arguments[0].click();", span)
                            clicked = True
                        except Exception:
                            pass
                    if clicked:
                        time.sleep(0.5)
                except Exception:
                    pass

        no_new = 0
        prev_count = 0
        found_item = None

        for i in range(max_scroll):
            try:
                self._check_stop()

                # Mở reply đang thu gọn trước khi tìm
                _expand_replies()

                found_item = find_by_href() if use_href else find_by_text()
                if not found_item:
                    found_item = find_by_text() if use_href else find_by_href()
                if found_item:
                    break

                # Đếm comment — dùng nhiều selector vì class thay đổi theo loại post
                cur_count = len(d.find_elements(By.CSS_SELECTOR,
                    "[class*='CommentItemContainer'], [data-e2e='comment-list-item'], "
                    "[class*='DivCommentItemWrapper'], "
                    "[class*='DivCommentListContainer'] > div, "
                    "[class*='CommentListContainer'] > div"))

                # Tìm lại container mỗi vòng
                scroll_container = self._get_comment_scroll_target()
                # Cuộn ngẫu nhiên (600-1000px) thay vì đúng 800px — người thật
                # không cuộn đều tăm tắp.
                dy = random.randint(600, 1000)
                if scroll_container:
                    d.execute_script(
                        f"arguments[0].scrollTop += {dy};"
                        "arguments[0].dispatchEvent(new Event('scroll'));",
                        scroll_container)
                else:
                    d.execute_script(f"window.scrollBy(0, {dy});")
                self._rsleep(0.75)   # nghỉ ngẫu nhiên để nội dung load + giống người
                # Chỉ log tiến trình cuộn mỗi 5 vòng — tránh spam log khi đa luồng.
                if (i + 1) % 5 == 0:
                    self.log(f"...cuộn ({i + 1}/{max_scroll}), {cur_count} cmt")

                if cur_count == prev_count:
                    no_new += 1
                    if no_new >= 3:
                        self.log("Không tải thêm comment, dừng cuộn.")
                        break
                else:
                    no_new = 0
                prev_count = cur_count

            except WebDriverException as e:
                self.log(f"⚠️ Lỗi kết nối: {e.msg.splitlines()[0]}")
                if link:
                    self._handle_retry(link)
                    self.open_comment_panel()
                else:
                    return False

        if not found_item:
            self.log("❌ Không tìm thấy comment.")
            return False
        return self._click_like_in_item(found_item)


    def _find_comment_item_by_id(self, comment_id: str):
        d = self.driver
        cid = str(comment_id or "")
        if not cid:
            return None
        el = d.execute_script("""
            const cid = arguments[0];
            const nodes = document.querySelectorAll(
              "[class*='DivCommentItemWrapper'],[class*='CommentItemContainer']," +
              "[data-e2e='comment-list-item'],[class*='CommentItem']");
            for (const n of nodes) {
              const blob = ((n.outerHTML||'') + ' ' + (n.id||'') + ' ' +
                (n.getAttribute('data-comment-id')||'') + ' ' +
                (n.getAttribute('data-id')||'')).toString();
              if (blob.indexOf(cid) >= 0) return n;
            }
            return null;
        """, cid)
        if el is not None:
            return el
        for attr in [f"[data-comment-id='{cid}']",
                     f"[id*='{cid}']",
                     f"[data-id='{cid}']"]:
            els = d.find_elements(By.CSS_SELECTOR, attr)
            if els:
                return els[0]
        return None

    def _like_by_id(self, comment_id: str):
        cid = str(comment_id or "")
        item = self._find_comment_item_by_id(cid)
        if item is None:
            return False
        return self._click_like_in_item(item)

    def _handle_retry(self, link: str):
        """Vào lại link nếu trang bị lỗi mạng."""
        self.log("🔄 Tải lại trang...")
        self.driver.get(link)
        time.sleep(4)

    # ── API path: gọi mutationFn của chính webapp TikTok ─────────────────
    def api_digg_comments(
            self, aweme_id: str, comments: list[dict],
            digg_type: int = 1, concurrency: int = 6):
        """Tim nhiều CID trong page context, có nhịp nghỉ chống drop tim."""
        targets = []
        for comment in comments:
            cid_val = str(comment.get("cid") or "").strip()
            if not cid_val:
                continue
            parent_val = str(comment.get("parent_cid") or "").strip()
            if parent_val in ("0", cid_val):
                parent_val = ""
            targets.append({
                "cid": cid_val,
                "parentCid": parent_val,
            })
        if not aweme_id or not targets:
            return []
        limit = max(1, min(8, int(concurrency or 8)))
        script = r"""
        const awemeId = String(arguments[0] || '');
        const targets = JSON.parse(arguments[1] || '[]');
        const diggType = Number(arguments[2] || 1);
        const concurrency = Math.max(1, Math.min(8, Number(arguments[3] || 1)));
        const done = arguments[arguments.length - 1];

        (async () => {
          const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
          function resolveMutations() {
            if (window.__alc_comment_mutations && window.__alc_comment_mutations.root) {
              return window.__alc_comment_mutations;
            }
            const chunks = window.__LOADABLE_LOADED_CHUNKS__;
            if (!chunks || typeof chunks.push !== 'function') {
              throw new Error('TikTok webpack runtime chưa sẵn sàng');
            }
            let requireFn = null;
            chunks.push([
              ['alc_comment_api_' + Date.now()],
              {},
              function(r) { requireFn = r; }
            ]);
            if (!requireFn || !requireFn.m) {
              throw new Error('Không lấy được TikTok module loader');
            }
            for (const [moduleId, factory] of Object.entries(requireFn.m)) {
              const source = Function.prototype.toString.call(factory);
              if (!source.includes('/api/comment/digg/') ||
                  !source.includes('postCommentLike') ||
                  !source.includes('postReplyLike')) {
                continue;
              }
              const exports = requireFn(moduleId);
              let root = null;
              let reply = null;
              for (const value of Object.values(exports || {})) {
                if (typeof value !== 'function' || value.length !== 0) continue;
                let configs;
                try { configs = value(); } catch (_) { continue; }
                if (!Array.isArray(configs)) continue;
                for (const config of configs) {
                  const mutation = config && config.mutation;
                  const rootEntry = mutation && mutation.postCommentLike;
                  const replyEntry = mutation && mutation.postReplyLike;
                  if (rootEntry && typeof rootEntry.mutationFn === 'function') {
                    root = rootEntry.mutationFn;
                  }
                  if (replyEntry && typeof replyEntry.mutationFn === 'function') {
                    reply = replyEntry.mutationFn;
                  }
                }
              }
              if (root && reply) {
                window.__alc_comment_mutations = {
                  root: root,
                  reply: reply,
                  moduleId: moduleId
                };
                return window.__alc_comment_mutations;
              }
            }
            throw new Error('Không tìm thấy mutation comment/digg của TikTok');
          }

function inspectDiggResponse(response) {
  const objects = [];
  const shapes = [];
  const seen = new Set();
  function visit(value, path, depth) {
    if (depth > 3 || value == null) return;
    if (typeof value === 'string') {
      if (value.length <= 65536) {
        try { visit(JSON.parse(value), path + '.json', depth + 1); } catch (_) {}
      }
      return;
    }
    if (typeof value !== 'object' || seen.has(value)) return;
    seen.add(value);
    objects.push(value);
    const keys = Object.keys(value).slice(0, 12).map(key =>
      /^[A-Za-z_][A-Za-z0-9_]{0,39}$/.test(key) ? key : '[key]');
    shapes.push(path + ':' + keys.join(','));
    for (const key of ['data', 'body', 'response']) {
      if (Object.prototype.hasOwnProperty.call(value, key)) {
        visit(value[key], path + '.' + key, depth + 1);
      }
    }
  }
  visit(response, 'root', 0);
  const codes = [];
  const rawStatuses = [];
  let malformed = false;
  for (const object of objects) {
    for (const key of ['statusCode', 'status_code']) {
      if (!Object.prototype.hasOwnProperty.call(object, key)) continue;
      const raw = object[key];
      const rawType = raw === null ? 'null' : typeof raw;
      const safeValue = typeof raw === 'number' || typeof raw === 'boolean'
        ? String(raw)
        : typeof raw === 'string' && /^-?\d{1,16}(?:\.\d{1,8})?$/.test(raw.trim())
          ? JSON.stringify(raw.trim()) : rawType;
      rawStatuses.push(key + '=' + safeValue + '(' + rawType + ')');
      // Empty wrapper fields are placeholders; a nested explicit code is still required.
      if (raw == null) continue;
      const valid = typeof raw === 'number'
        ? Number.isSafeInteger(raw) && raw >= 0
        : typeof raw === 'string' && /^\d+$/.test(raw.trim()) &&
          Number.isSafeInteger(Number(raw));
      if (valid) codes.push(Number(raw));
      else malformed = true;
    }
  }
  // A wrapper HTTP status cannot prove that the mutation succeeded.
  const http = objects.map(object => object.status).find(status =>
    Number.isInteger(status) && status >= 100 && status <= 599);
  const unique = [...new Set(codes)];
  const code = !malformed && unique.length === 1 ? unique[0] : -1;
  const kind = unique.length > 1 ? 'conflicting_status'
    : malformed ? 'invalid_status' : code < 0 ? 'missing_status' : 'api_status';
  const type = response === null ? 'null' : Array.isArray(response) ? 'array' : typeof response;
  // Only structural metadata is logged: no cookies, tokens, response values or URLs.
  const diagnostic = ('kind=' + kind + ';type=' + type +
    (http !== undefined ? ';http=' + http : '') +
    ';rawStatus=' + rawStatuses.join('|') +
    ';shape=' + shapes.join('|')).slice(0, 600);
  return {code, kind, diagnostic};
}

          try {
            const chunks = window.__LOADABLE_LOADED_CHUNKS__;
            let req = null;
            if (chunks && typeof chunks.push === 'function') {
              chunks.push([['alc_ctx_' + Date.now()], {}, function(r) { req = r; }]);
            }
            const m345 = req && req.m && req.m[345146] ? req(345146) : null;
            const h = (m345 && m345.h) || window.__alc_http_client;
            if (h) {
              window.__alc_http_client = h;
              function applyCtx(ctx) {
                if (!ctx || typeof ctx !== 'object') return;
                const id = window.__alc_identity || {};
                let csrf = String(id.csrf || '');
                if (!csrf && document.cookie) {
                  const m = document.cookie.match(/(?:^|;\s*)tt_csrf_token=([^;]+)/);
                  if (m) { try { csrf = decodeURIComponent(m[1]); } catch (_) { csrf = m[1]; } }
                }
                if (csrf) ctx.csrfToken = csrf;
                if (id.wid) ctx.wid = String(id.wid);
                if (id.uidStr) ctx.odinId = String(id.uidStr);
                else if (id.wid) ctx.odinId = String(id.wid);
                if (id.region) {
                  ctx.region = String(id.region);
                  ctx.priorityRegion = String(id.region);
                }
                if (!ctx.user || typeof ctx.user !== 'object') {
                  ctx.user = {region: id.region || ctx.region || 'VN', ftcUser: false};
                } else if (id.region) {
                  ctx.user.region = String(id.region);
                }
                if (id.uidStr) ctx.user.uid = String(id.uidStr);
                if (id.uname) ctx.user.uniqueId = String(id.uname).replace(/^@/, '');
              }
              if (!h.__alc_hooked_setAppContext && typeof h.setAppContext === 'function') {
                const origSet = h.setAppContext.bind(h);
                h.setAppContext = function(e) {
                  if (e && typeof e === 'object') applyCtx(e);
                  const res = origSet(e);
                  if (this.appContext && typeof this.appContext === 'object') applyCtx(this.appContext);
                  return res;
                };
                h.__alc_hooked_setAppContext = true;
              }
              if (h.verify && typeof h.verify.checkVerify === 'function' && !h.verify.__alc_hooked) {
                h.verify.checkVerify = async function() { return 0; };
                h.verify.__alc_hooked = true;
              }
              if (h.appContext && typeof h.appContext === 'object' && !h.appContext.wid) {
                h.appContext = undefined;
              }
              if (!h.appContext && typeof h.handleAppContext === 'function') {
                try { await h.handleAppContext(); } catch (_) {}
              }
              if (h.appContext && typeof h.appContext === 'object') {
                applyCtx(h.appContext);
              }
            }
          } catch (_) {}
          let mutations = resolveMutations();
          const results = new Array(targets.length);
          let cursor = 0;
          let blockedCode = 0;
          async function worker(workerIdx) {
            if (workerIdx > 0) await sleep(workerIdx * 40);
            let localCount = 0;
            while (true) {
              const index = cursor++;
              if (index >= targets.length) return;
              if (localCount > 0) await sleep(35);
              localCount++;
              const target = targets[index];
              if (blockedCode) {
                results[index] = {cid: String(target.cid || ''), statusCode: blockedCode,
                  skipped: true, error: 'Bỏ qua: phiên hết hạn hoặc bị giới hạn'};
                continue;
              }
              const rawParent = String(target.parentCid || '').trim();
              const parentCid = (rawParent && rawParent !== '0' && rawParent !== String(target.cid || '').trim())
                ? rawParent : '';
              const payload = {
                itemId: awemeId,
                cid: String(target.cid || ''),
                like: diggType === 1
              };
              if (parentCid) payload.parentCid = parentCid;
              try {
                let inspected = null;
                for (let attempt = 0; attempt < 2; attempt++) {
                  if (attempt > 0) await sleep(120);
                  let requestTimer;
                  let response;
                  try {
                    response = await Promise.race([
                      (async () => {
                        const fn = parentCid ? mutations.reply : mutations.root;
                        return await fn(payload);
                      })().catch(async err => {
                        const msg = String(err && (err.message || err) || '');
                        if (msg.includes('is not a function')) {
                          try { delete window.__alc_comment_mutations; } catch (_) {}
                          await sleep(180);
                          try {
                            mutations = resolveMutations();
                            const fn2 = parentCid ? mutations.reply : mutations.root;
                            return await fn2(payload);
                          } catch (_) {}
                          const csrf = (window.__alc_identity && window.__alc_identity.csrf) || '';
                          const url = '/api/comment/digg/?aid=1988&app_name=tiktok_web&device_platform=web_pc'
                            + '&aweme_id=' + encodeURIComponent(awemeId)
                            + '&cid=' + encodeURIComponent(payload.cid)
                            + '&digg_type=' + encodeURIComponent(String(diggType));
                          const r = await fetch(url, {
                            method: 'POST',
                            credentials: 'include',
                            headers: csrf ? {'tt-csrf-token': csrf, 'x-secsdk-csrf-token': '000100000001' + csrf} : {}
                          });
                          return await r.json();
                        }
                        throw err;
                      }),
                      new Promise((_, reject) => {
                        requestTimer = setTimeout(
                          () => reject(new Error('digg request timeout')), 4000);
                      })
                    ]);
                  } finally {
                    clearTimeout(requestTimer);
                  }
                  inspected = inspectDiggResponse(response);
                  if (inspected.code >= 0) break;
                }
                if (inspected.code === 8 || inspected.code === 429) blockedCode = inspected.code;
                results[index] = {
                  cid: payload.cid,
                  statusCode: inspected.code,
                  moduleId: mutations.moduleId,
                  diagnostic: inspected.diagnostic,
                  skipped: false,
                  error: inspected.code < 0
                    ? 'Chưa xác nhận kết quả digg (' + inspected.kind + ')'
                    : inspected.code === 0 ? '' : 'TikTok trả mã digg ' + inspected.code
                };
              } catch (error) {
                results[index] = {
                  cid: payload.cid,
                  statusCode: -1,
                  requestError: true,
                  error: String(
                    error && (error.message || error) || error).slice(0, 200)
                };
              }
            }
          }
          const workers = Math.min(concurrency, targets.length);
          await Promise.all(Array.from({length: workers}, (_, idx) => worker(idx)));
          return results;
        })().then(done).catch(error => done(targets.map(target => ({
          cid: String(target.cid || ''),
          statusCode: -1,
          moduleError: true,
          error: String(error && (error.message || error) || error).slice(0, 200)
        }))));
        """

        def _call():
            try:
                # Each worker gets 4 seconds per CID. Account for the entire
                # queue, so later CIDs aren't discarded after the old 12s cap.
                waves = (len(targets) + limit - 1) // limit
                self.driver.set_script_timeout(max(15, waves * 4 + 5))
                return self.driver.execute_async_script(
                    script, str(aweme_id),
                    json.dumps(targets, ensure_ascii=False),
                    int(digg_type), limit)
            except Exception as error:
                reason = str(error)[:200]
                return [
                    {
                        "cid": target["cid"],
                        "statusCode": -1,
                        "scriptError": True,
                        "error": reason,
                    }
                    for target in targets
                ]

        def _module_missing(rows):
            return (
                isinstance(rows, list)
                and len(rows) == len(targets)
                and bool(rows)
                and all(bool(row.get("moduleError")) for row in rows)
            )

        def _runtime_missing(rows):
            return _module_missing(rows) and all(
                "webpack runtime" in str(row.get("error") or "").lower()
                for row in rows)

        rows = _call()
        for _ in range(8):
            if not _module_missing(rows):
                break
            time.sleep(0.25)
            rows = _call()
        if _module_missing(rows):
            if _runtime_missing(rows):
                try:
                    self.driver.refresh()
                    WebDriverWait(self.driver, 8).until(
                        lambda driver: driver.execute_script(
                            "return document.readyState === 'complete' && "
                            "!!window.__LOADABLE_LOADED_CHUNKS__"))
                except Exception:
                    pass
            # Mutation comment/digg nằm trong chunk tải lười của panel comment.
            # Refresh trang không đủ; phải mở panel để webpack nạp đúng module.
            try:
                self.log('[api] Module chưa sẵn sàng; chế độ API không mở panel.')
            except Exception:
                pass
            for _ in range(8):
                rows = _call()
                if not _module_missing(rows):
                    break
                time.sleep(0.25)

        diagnostics = {}
        invalid_status_count = 0
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict) and row.get("statusCode") != 0 and row.get("diagnostic"):
                    summary = str(row["diagnostic"])[:600]
                    diagnostics[summary] = diagnostics.get(summary, 0) + 1
                    if "invalid_status" in summary or row.get("statusCode") == -1:
                        invalid_status_count += 1
        if invalid_status_count > 0:
            self._needs_page_reload = True
            self._consecutive_invalid_status = getattr(self, "_consecutive_invalid_status", 0) + 1
        elif isinstance(rows, list) and any(isinstance(r, dict) and r.get("statusCode") == 0 for r in rows):
            self._consecutive_invalid_status = 0
        for summary, count in list(diagnostics.items())[:3]:
            self.log(f"🔎 Digg response ({count} CID): {summary}")
        normalized = []
        for index, target in enumerate(targets):
            row = rows[index] if (
                isinstance(rows, list) and index < len(rows)
                and isinstance(rows[index], dict)) else {}
            raw_code = row.get("statusCode")
            try:
                code = int(raw_code) if (
                    not isinstance(raw_code, bool) and
                    ((isinstance(raw_code, int) and raw_code >= 0) or
                     (isinstance(raw_code, str) and raw_code.strip().isdigit()))
                ) else -1
            except (ValueError, TypeError, OverflowError):
                code = -1
            if code == 0:
                result = (
                    True, 0, f"TikTok module {row.get('moduleId', '?')}")
            elif code == 8:
                self.last_digg_expired = True
                self.last_session_invalid = True
                result = (False, code, "bỏ qua do phiên hết hạn" if row.get("skipped")
                          else "acc hết hạn đăng nhập ghi")
            else:
                error = str(row.get("error") or "")[:160]
                result = (
                    False, code, error or "TikTok từ chối comment/digg")
            normalized.append(result)
        return normalized

    def api_digg_comment(
            self, aweme_id: str, cid: str, digg_type: int = 1,
            parent_cid: str = ""):
        """Biến thể một CID, giữ contract cho caller đơn lẻ."""
        if not aweme_id or not cid:
            return False, -1, "thiếu aweme_id/cid"
        results = self.api_digg_comments(
            aweme_id,
            [{"cid": str(cid), "parent_cid": str(parent_cid or "")}],
            digg_type=digg_type, concurrency=1)
        return results[0] if results else (
            False, -1, "response module không hợp lệ")

    @staticmethod
    def _is_valid_comment_api_payload(data) -> bool:
        if not isinstance(data, dict):
            return False
        if isinstance(data.get("comments"), list):
            return True
        return data.get("status_code") in (0, "0", None) and (
            "has_more" in data or "total" in data
        )

    def _scout_peer_ready_rows(self, aweme_id: str, username: str = ""):
        if not aweme_id:
            return None
        peek = getattr(self, "_scout_peek_ready", None)
        if not callable(peek):
            return None
        try:
            uname = _norm_handle(username or getattr(self, "_current_scan_uname", "") or "")
            rows = peek(str(aweme_id), uname)
            return rows if rows else None
        except Exception:
            return None

    def _mark_driver_unresponsive(self, fallback_url: str = "") -> None:
        """Đánh dấu tab trình duyệt bị treo ở tầng WebDriver, hạ timeout và
        nếu là ScoutBot thì đóng chromedriver ngay để không treo luồng."""
        self._driver_unresponsive = True
        self._consecutive_api_failures = max(
            int(getattr(self, "_consecutive_api_failures", 0) or 0), 2
        )
        url = str(
            fallback_url
            or getattr(self, "_last_known_url", "")
            or getattr(self, "_last_target_link", "")
            or ""
        )
        if url:
            self._last_known_url = url
        is_scout = callable(getattr(self, "_scout_publish", None))
        d = getattr(self, "driver", None)
        if d is not None and is_scout:
            client_config = getattr(
                getattr(d, "command_executor", None), "_client_config", None
            )
            if client_config is not None:
                try:
                    client_config.timeout = 1.5
                except Exception:
                    pass
        if is_scout:
            try:
                self._hard_kill()
            except Exception:
                pass
            self.driver = _UnresponsiveDriverStub(url)

    def _tiktok_signed_get(self, path: str, query: dict, timeout_sec: float = 4.5):
        """Gọi GET API nội bộ TikTok qua webpack httpClient (tự ký X-Bogus/X-Gnarly)."""
        self._last_signed_get_ok = False
        if getattr(self, "_driver_unresponsive", False):
            return None
        d = self.driver
        if d is None or getattr(d, "_is_unresponsive_stub", False) or self.stop_event.is_set():
            return None
        script = r"""
        const path = String(arguments[0] || '');
        const query = JSON.parse(arguments[1] || '{}');
        const timeoutMs = Math.max(800, Number(arguments[2] || 4500));
        const done = arguments[arguments.length - 1];
        (async () => {
          function getHttp() {
            if (window.__alc_http_client) return window.__alc_http_client;
            const chunks = window.__LOADABLE_LOADED_CHUNKS__;
            if (!chunks || typeof chunks.push !== 'function') return null;
            let req = null;
            try {
              chunks.push([['alc_http_' + Date.now()], {}, function(r) { req = r; }]);
            } catch (_) {}
            if (!req || !req.m) return null;
            try {
              const m345 = req(345146);
              if (m345 && m345.h && typeof m345.h.get === 'function') {
                window.__alc_http_client = m345.h;
                return m345.h;
              }
            } catch (_) {}
            for (const id of Object.keys(req.m)) {
              try {
                const src = Function.prototype.toString.call(req.m[id]);
                if (!src.includes('baseUrlType') && !src.includes('interceptors')) continue;
                const mod = req(id);
                if (mod && mod.h && typeof mod.h.get === 'function' && typeof mod.h.post === 'function') {
                  window.__alc_http_client = mod.h;
                  return mod.h;
                }
              } catch (_) {}
            }
            return null;
          }
          function recordNodes(payload, parentFromQuery) {
            try {
              if (!payload || typeof payload !== 'object') return;
              if (!window.__tt_comment_nodes) window.__tt_comment_nodes = Object.create(null);
              const cleanCid = v => {
                const s = String(v || '').trim();
                return (s && s !== '0') ? s : '';
              };
              const keep = (node, parentCid) => {
                if (!node || !node.cid) return;
                const cid = String(node.cid);
                const prev = window.__tt_comment_nodes[cid] || {};
                const inferredParent = cleanCid(parentCid)
                  || cleanCid(node.reply_id)
                  || cleanCid(node.reply_to_reply_id)
                  || cleanCid(prev.parent_cid);
                const user = node.user || {};
                window.__tt_comment_nodes[cid] = {
                  cid,
                  parent_cid: inferredParent === cid ? '' : inferredParent,
                  reply_comment_total: Number(node.reply_comment_total || prev.reply_comment_total || 0),
                  text: String(node.text || prev.text || ''),
                  user: {
                    unique_id: String(user.unique_id || (prev.user && prev.user.unique_id) || ''),
                    nickname: String(user.nickname || (prev.user && prev.user.nickname) || '')
                  },
                  user_digged: Boolean(node.user_digged || prev.user_digged)
                };
              };
              const pCid = cleanCid(parentFromQuery);
              for (const c of (payload.comments || [])) {
                keep(c, pCid);
                for (const r of (c.reply_comment || [])) {
                  keep(r, c.cid || pCid);
                }
              }
            } catch (_) {}
          }
          const http = getHttp();
          let timer = null;
          try {
            let raw = null;
            if (http) {
              try {
                if (http.appContext && typeof http.appContext === 'object' && !http.appContext.wid) {
                  http.appContext = undefined;
                }
                if (!http.appContext && typeof http.handleAppContext === 'function') {
                  await http.handleAppContext();
                }
                const ctx = http.appContext;
                const id = window.__alc_identity || {};
                if (ctx && typeof ctx === 'object') {
                  if (id.csrf) ctx.csrfToken = String(id.csrf);
                  if (id.uidStr) ctx.odinId = String(id.uidStr);
                  if (!ctx.user || typeof ctx.user !== 'object') {
                    ctx.user = {region: ctx.region || 'VN', ftcUser: false};
                  }
                  if (id.uidStr) ctx.user.uid = String(id.uidStr);
                  if (id.uname) ctx.user.uniqueId = String(id.uname).replace(/^@/, '');
                }
              } catch (_) {}
              if (!window.__alc_inflight_gets) window.__alc_inflight_gets = Object.create(null);
              const reqKey = path + '?' + JSON.stringify(query || {});
              let p = (query && query._t) ? null : window.__alc_inflight_gets[reqKey];
              if (!p) {
                p = Promise.resolve(
                  http.get(path, {query: query, headers: {'cache-control': 'no-cache', 'pragma': 'no-cache'}, baseUrlType: 2})
                ).then(res => {
                  const d = (res && res.data && typeof res.data === 'object' && Array.isArray(res.data.comments)) ? res.data : res;
                  recordNodes(d, query && query.comment_id ? query.comment_id : '');
                  return res;
                }).finally(() => {
                  if (window.__alc_inflight_gets) delete window.__alc_inflight_gets[reqKey];
                });
                if (!(query && query._t)) window.__alc_inflight_gets[reqKey] = p;
              }
              raw = await Promise.race([
                p,
                new Promise((_, rej) => { timer = setTimeout(() => rej(new Error('timeout')), timeoutMs); })
              ]);
            } else {
              const qs = new URLSearchParams();
              for (const [k, v] of Object.entries(query || {})) {
                if (v !== undefined && v !== null) qs.set(k, String(v));
              }
              const ctrl = new AbortController();
              timer = setTimeout(() => ctrl.abort(), timeoutMs);
              const resp = await fetch(path + '?' + qs.toString(), {credentials: 'include', cache: 'no-store', signal: ctrl.signal});
              const txt = await resp.text();
              raw = txt ? JSON.parse(txt) : null;
            }
            if (!raw || typeof raw !== 'object') return null;
            const data = (raw.data && typeof raw.data === 'object' && Array.isArray(raw.data.comments))
              ? raw.data : raw;
            recordNodes(data, query && query.comment_id ? query.comment_id : '');
            return JSON.stringify(data);
          } catch (_) {
            return null;
          } finally {
            if (timer) clearTimeout(timer);
          }
        })().then(done).catch(() => done(null));
        """
        try:
            # A page timer can stop when Chromium stalls. Bound the WebDriver
            # async command too, rather than relying only on Promise.race.
            eff_timeout = max(0.8, float(timeout_sec or 4.5))
            client_config = getattr(getattr(d, "command_executor", None), "_client_config", None)
            old_transport_timeout = getattr(client_config, "timeout", None)
            transport_failed = False
            try:
                if client_config is not None:
                    client_config.timeout = max(5.0, eff_timeout + 3.0)
                d.set_script_timeout(max(3.0, eff_timeout + 2.0))
                raw_json = d.execute_async_script(
                    script, str(path), json.dumps(query, ensure_ascii=False), int(eff_timeout * 1000)
                )
            except Exception as exc:
                cls_name = type(exc).__name__.lower()
                msg = str(exc or "").lower()
                is_script_err = (
                    "scripttimeout" in cls_name
                    or "javascript" in cls_name
                    or "script timeout" in msg
                    or "javascript error" in msg
                )
                is_transport_err = not is_script_err and (
                    isinstance(exc, (TimeoutError, ConnectionError, OSError))
                    or any(
                        t in cls_name
                        for t in (
                            "readtimeout",
                            "maxretry",
                            "protocolerror",
                            "remotedisconnected",
                            "invalidsessionid",
                            "nosuchwindow",
                        )
                    )
                    or any(
                        t in msg
                        for t in (
                            "read timed out",
                            "connection aborted",
                            "connection reset",
                            "forcibly closed",
                            "remotedisconnected",
                            "max retries exceeded",
                            "invalid session id",
                            "chrome not reachable",
                            "target window already closed",
                            "disconnected:",
                        )
                    )
                )
                if is_transport_err:
                    transport_failed = True
                    self._driver_unresponsive = True
                    marker = getattr(self, "_mark_driver_unresponsive", None)
                    if callable(marker):
                        marker()
                return None
            finally:
                is_scout = callable(getattr(self, "_scout_publish", None))
                if client_config is not None and not (transport_failed and is_scout):
                    try:
                        client_config.timeout = max(25.0, float(old_transport_timeout or 25.0))
                    except Exception:
                        client_config.timeout = 25
            if not raw_json:
                return None
            parsed = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
            self._last_signed_get_ok = self._is_valid_comment_api_payload(parsed)
            return parsed
        except Exception:
            return None

    def _tiktok_signed_get_batch(self, items: list, timeout_sec: float = 4.5) -> list:
        """Gọi song song nhiều GET API nội bộ TikTok trong 1 lượt execute_async_script."""
        if not items:
            return []
        if (
            len(items) == 1
            or "_tiktok_signed_get" in getattr(self, "__dict__", {})
            or getattr(type(self), "_tiktok_signed_get", None) is not getattr(TikTokLikeBot, "_tiktok_signed_get", None)
        ):
            return [self._tiktok_signed_get(p, q, timeout_sec=timeout_sec) for p, q in items]
        if getattr(self, "_driver_unresponsive", False):
            return [None] * len(items)
        d = self.driver
        if d is None or getattr(d, "_is_unresponsive_stub", False) or self.stop_event.is_set():
            return [None] * len(items)
        payload_items = [{"path": str(p), "query": dict(q or {})} for p, q in items]
        script = r"""
        const items = JSON.parse(arguments[0] || '[]');
        const timeoutMs = Math.max(800, Number(arguments[1] || 3500));
        const done = arguments[arguments.length - 1];
        (async () => {
          function getHttp() {
            if (window.__alc_http_client) return window.__alc_http_client;
            const chunks = window.__LOADABLE_LOADED_CHUNKS__;
            if (!chunks || typeof chunks.push !== 'function') return null;
            let req = null;
            try {
              chunks.push([['alc_http_' + Date.now()], {}, function(r) { req = r; }]);
            } catch (_) {}
            if (!req || !req.m) return null;
            try {
              const m345 = req(345146);
              if (m345 && m345.h && typeof m345.h.get === 'function') {
                window.__alc_http_client = m345.h;
                return m345.h;
              }
            } catch (_) {}
            for (const id of Object.keys(req.m)) {
              try {
                const src = Function.prototype.toString.call(req.m[id]);
                if (!src.includes('baseUrlType') && !src.includes('interceptors')) continue;
                const mod = req(id);
                if (mod && mod.h && typeof mod.h.get === 'function' && typeof mod.h.post === 'function') {
                  window.__alc_http_client = mod.h;
                  return mod.h;
                }
              } catch (_) {}
            }
            return null;
          }
          function recordNodes(payload, parentFromQuery) {
            try {
              if (!payload || typeof payload !== 'object') return;
              if (!window.__tt_comment_nodes) window.__tt_comment_nodes = Object.create(null);
              const cleanCid = v => {
                const s = String(v || '').trim();
                return (s && s !== '0') ? s : '';
              };
              const keep = (node, parentCid) => {
                if (!node || !node.cid) return;
                const cid = String(node.cid);
                const prev = window.__tt_comment_nodes[cid] || {};
                const inferredParent = cleanCid(parentCid)
                  || cleanCid(node.reply_id)
                  || cleanCid(node.reply_to_reply_id)
                  || cleanCid(prev.parent_cid);
                const user = node.user || {};
                window.__tt_comment_nodes[cid] = {
                  cid,
                  parent_cid: inferredParent === cid ? '' : inferredParent,
                  reply_comment_total: Number(node.reply_comment_total || prev.reply_comment_total || 0),
                  text: String(node.text || prev.text || ''),
                  user: {
                    unique_id: String(user.unique_id || (prev.user && prev.user.unique_id) || ''),
                    nickname: String(user.nickname || (prev.user && prev.user.nickname) || '')
                  },
                  user_digged: Boolean(node.user_digged || prev.user_digged)
                };
              };
              const pCid = cleanCid(parentFromQuery);
              for (const c of (payload.comments || [])) {
                keep(c, pCid);
                for (const r of (c.reply_comment || [])) {
                  keep(r, c.cid || pCid);
                }
              }
            } catch (_) {}
          }
          const http = getHttp();
          if (http) {
            try {
              if (http.appContext && typeof http.appContext === 'object' && !http.appContext.wid) {
                http.appContext = undefined;
              }
              if (!http.appContext && typeof http.handleAppContext === 'function') {
                await http.handleAppContext();
              }
              const ctx = http.appContext;
              const id = window.__alc_identity || {};
              if (ctx && typeof ctx === 'object') {
                if (id.csrf) ctx.csrfToken = String(id.csrf);
                if (id.uidStr) ctx.odinId = String(id.uidStr);
                if (!ctx.user || typeof ctx.user !== 'object') {
                  ctx.user = {region: ctx.region || 'VN', ftcUser: false};
                }
                if (id.uidStr) ctx.user.uid = String(id.uidStr);
                if (id.uname) ctx.user.uniqueId = String(id.uname).replace(/^@/, '');
              }
            } catch (_) {}
          }
          const out = new Array(items.length).fill(null);
          let cursor = 0;
          async function fetchOne(item) {
            const path = String(item.path || '');
            const query = item.query || {};
            let timer = null;
            try {
              let raw = null;
              if (http) {
                const p = Promise.resolve(
                  http.get(path, {query: query, headers: {'cache-control': 'no-cache', 'pragma': 'no-cache'}, baseUrlType: 2})
                );
                raw = await Promise.race([
                  p,
                  new Promise((_, rej) => { timer = setTimeout(() => rej(new Error('timeout')), timeoutMs); })
                ]);
              } else {
                const qs = new URLSearchParams();
                for (const [k, v] of Object.entries(query)) {
                  if (v !== undefined && v !== null) qs.set(k, String(v));
                }
                const ctrl = new AbortController();
                timer = setTimeout(() => ctrl.abort(), timeoutMs);
                const resp = await fetch(path + '?' + qs.toString(), {credentials: 'include', cache: 'no-store', signal: ctrl.signal});
                const txt = await resp.text();
                raw = txt ? JSON.parse(txt) : null;
              }
              if (!raw || typeof raw !== 'object') return null;
              const data = (raw.data && typeof raw.data === 'object' && Array.isArray(raw.data.comments))
                ? raw.data : raw;
              recordNodes(data, query && query.comment_id ? query.comment_id : '');
              return data;
            } catch (_) {
              return null;
            } finally {
              if (timer) clearTimeout(timer);
            }
          }
          async function worker() {
            while (true) {
              const idx = cursor++;
              if (idx >= items.length) return;
              out[idx] = await fetchOne(items[idx]);
            }
          }
          const workers = Math.min(10, items.length);
          await Promise.all(Array.from({length: workers}, () => worker()));
          return JSON.stringify(out);
        })().then(done).catch(() => done(null));
        """
        try:
            eff_timeout = max(0.8, float(timeout_sec or 3.5))
            waves = (len(items) + 9) // 10
            total_timeout = max(eff_timeout, waves * eff_timeout * 0.7 + 1.0)
            client_config = getattr(getattr(d, "command_executor", None), "_client_config", None)
            old_transport_timeout = getattr(client_config, "timeout", None)
            try:
                if client_config is not None:
                    client_config.timeout = max(6.0, total_timeout + 3.0)
                d.set_script_timeout(max(3.5, total_timeout + 2.0))
                raw_json = d.execute_async_script(
                    script, json.dumps(payload_items, ensure_ascii=False), int(eff_timeout * 1000)
                )
            finally:
                if client_config is not None:
                    try:
                        client_config.timeout = max(25.0, float(old_transport_timeout or 25.0))
                    except Exception:
                        client_config.timeout = 25
            if not raw_json:
                return [None] * len(items)
            parsed = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
            if isinstance(parsed, list) and len(parsed) == len(items):
                self._last_signed_get_ok = any(self._is_valid_comment_api_payload(p) for p in parsed)
                return parsed
            return [None] * len(items)
        except Exception:
            return [None] * len(items)

    def _cid_digged_status(self, aweme_id: str, cid: str):
        """Trả True/False nếu xác định được cid đã/chưa like; None nếu không rõ."""
        if not aweme_id or not cid:
            return None
        cid = str(cid)
        cursor = 0
        tried = set()
        for _ in range(5):
            if cursor in tried:
                break
            tried.add(cursor)
            data = self._tiktok_signed_get(
                "/api/comment/list/",
                {"aid": 1988, "aweme_id": str(aweme_id), "count": 20, "cursor": cursor},
            )
            if not isinstance(data, dict):
                return None
            for c in (data.get("comments") or []):
                if str(c.get("cid")) == cid:
                    return bool(c.get("user_digged"))
                for r in (c.get("reply_comment") or []):
                    if str(r.get("cid")) == cid:
                        return bool(r.get("user_digged"))
            if not data.get("has_more"):
                break
            next_cursor = data.get("cursor")
            if next_cursor is None:
                break
            cursor = int(next_cursor)
        return None

    def already_liked_api(self, aweme_id: str, cid: str) -> bool:
        """Kiểm tra comment đã like chưa qua API comment/list (tránh double-like)."""
        return self._cid_digged_status(aweme_id, cid) is True

    def find_cid_by_user_api(self, aweme_id: str, username: str) -> str:
        """Tìm cid của comment gần nhất của @username qua /api/comment/list/."""
        uname = _norm_handle(username)
        if not aweme_id or not uname:
            return ""
        cursor = 0
        tried = set()
        for _ in range(8):
            if cursor in tried:
                break
            tried.add(cursor)
            data = self._tiktok_signed_get(
                "/api/comment/list/",
                {"aid": 1988, "aweme_id": str(aweme_id), "count": 20, "cursor": cursor},
            )
            if not isinstance(data, dict):
                break
            for c in (data.get("comments") or []):
                if _user_matches(c.get("user") or {}, uname):
                    return str(c.get("cid") or "")
                for r in (c.get("reply_comment") or []):
                    if _user_matches(r.get("user") or {}, uname):
                        return str(r.get("cid") or "")
            if not data.get("has_more"):
                break
            next_cursor = data.get("cursor")
            if next_cursor is None:
                break
            cursor = int(next_cursor)
        return ""

    def _fetch_replies_api(self, aweme_id: str, comment_id: str,
                           start_cursor: int = 0) -> list:
        """Lấy TẤT CẢ reply của 1 comment gốc qua /api/comment/list/reply/ (đã ký)."""
        self._last_signed_get_ok = False
        if not aweme_id or not comment_id:
            return []
        out = []
        seen_cids = set()
        cursor = int(start_cursor or 0)
        tried = set()
        for _ in range(15):
            self._check_stop()
            if self._scout_peer_ready_rows(aweme_id):
                self._last_signed_get_ok = True
                break
            if cursor in tried:
                break
            tried.add(cursor)
            data = self._tiktok_signed_get(
                "/api/comment/list/reply/",
                {
                    "aid": 1988,
                    "aweme_id": str(aweme_id),
                    "item_id": str(aweme_id),
                    "comment_id": str(comment_id),
                    "count": 20,
                    "cursor": cursor,
                },
            )
            if not self._is_valid_comment_api_payload(data):
                if not out:
                    self._last_signed_get_ok = False
                break
            self._last_signed_get_ok = True
            replies = data.get("comments") or []
            for r in replies:
                rcid = str(r.get("cid") or "")
                if rcid and rcid not in seen_cids:
                    seen_cids.add(rcid)
                    out.append(r)
            next_cursor = data.get("cursor")
            has_more = bool(data.get("has_more"))
            total = int(data.get("total") or 0)
            if has_more and next_cursor is not None and int(next_cursor) != cursor:
                cursor = int(next_cursor)
                continue
            if not has_more and next_cursor is not None and int(next_cursor) > cursor and len(out) < total:
                cursor = int(next_cursor)
                continue
            break
        return out

    def _fetch_replies_api_batch(self, aweme_id: str, comment_ids: list) -> dict:
        """Lấy song song TẤT CẢ reply của nhiều comment gốc qua /api/comment/list/reply/."""
        self._last_signed_get_ok = False
        if not aweme_id or not comment_ids:
            return {}
        unique_cids = []
        seen_root = set()
        for cid in comment_ids:
            s = str(cid or "").strip()
            if s and s not in seen_root:
                seen_root.add(s)
                unique_cids.append(s)
        if not unique_cids:
            return {}
        if (
            len(unique_cids) <= 1
            or "_fetch_replies_api" in getattr(self, "__dict__", {})
            or "_tiktok_signed_get" in getattr(self, "__dict__", {})
            or getattr(type(self), "_fetch_replies_api", None) is not getattr(TikTokLikeBot, "_fetch_replies_api", None)
            or getattr(type(self), "_tiktok_signed_get", None) is not getattr(TikTokLikeBot, "_tiktok_signed_get", None)
        ):
            out_map = {}
            any_ok = False
            for cid in unique_cids:
                out_map[cid] = self._fetch_replies_api(aweme_id, cid, start_cursor=0)
                if getattr(self, "_last_signed_get_ok", False):
                    any_ok = True
            self._last_signed_get_ok = any_ok
            return out_map

        out_map = {cid: [] for cid in unique_cids}
        seen_map = {cid: set() for cid in unique_cids}
        cursor_map = {cid: 0 for cid in unique_cids}
        tried_map = {cid: set() for cid in unique_cids}
        active_cids = list(unique_cids)
        any_batch_ok = False

        for _ in range(15):
            if not active_cids:
                break
            self._check_stop()
            if self._scout_peer_ready_rows(aweme_id):
                self._last_signed_get_ok = True
                break
            req_cids = []
            requests = []
            for cid in active_cids:
                cur = cursor_map[cid]
                if cur in tried_map[cid]:
                    continue
                tried_map[cid].add(cur)
                req_cids.append(cid)
                requests.append((
                    "/api/comment/list/reply/",
                    {
                        "aid": 1988,
                        "aweme_id": str(aweme_id),
                        "item_id": str(aweme_id),
                        "comment_id": str(cid),
                        "count": 20,
                        "cursor": cur,
                    },
                ))
            if not requests:
                break
            results = self._tiktok_signed_get_batch(requests)
            round_ok = False
            next_active = []
            for cid, data in zip(req_cids, results):
                cur = cursor_map[cid]
                if not self._is_valid_comment_api_payload(data):
                    continue
                round_ok = True
                any_batch_ok = True
                replies = data.get("comments") or []
                seen = seen_map[cid]
                out = out_map[cid]
                for r in replies:
                    rcid = str(r.get("cid") or "")
                    if rcid and rcid not in seen:
                        seen.add(rcid)
                        out.append(r)
                next_cursor = data.get("cursor")
                has_more = bool(data.get("has_more"))
                total = int(data.get("total") or 0)
                if has_more and next_cursor is not None and int(next_cursor) != cur:
                    cursor_map[cid] = int(next_cursor)
                    next_active.append(cid)
                elif not has_more and next_cursor is not None and int(next_cursor) > cur and len(out) < total:
                    cursor_map[cid] = int(next_cursor)
                    next_active.append(cid)
            if not round_ok:
                break
            active_cids = next_active

        self._last_signed_get_ok = any_batch_ok
        return out_map

    def _list_exhausted(self) -> bool:
        """True nếu response /comment/list MỚI NHẤT (không phải reply) báo has_more=0
        → đã cuộn hết comment gốc. Dùng để không dừng cuộn sớm với acc chậm.

        Chỉ xét response của endpoint comment/list gốc (có trường 'total'); response
        reply cũng có has_more nhưng của riêng 1 comment, không phản ánh toàn trang.
        """
        try:
            return bool(self.driver.execute_script("""
                const bodies = window.__tt_comment_bodies||[];
                for(let i=bodies.length-1; i>=0; i--){
                    let d; try{ d=JSON.parse(bodies[i]); }catch(e){ continue; }
                    // response list gốc có 'total'; reply endpoint không có.
                    if(d && d.total !== undefined){
                        return d.has_more === 0 || d.has_more === false;
                    }
                }
                return false;
            """))
        except Exception:
            return False

    def _comment_panel_open(self) -> bool:
        """Chi True khi co comment item that — khong tin tux/login dialog."""
        try:
            return bool(self.driver.execute_script("""
                return !!document.querySelector(
                    "[data-e2e='comment-list-item'],"
                    + "[class*='DivCommentItemWrapper'],"
                    + "[class*='DivCommentItemContainer'],"
                    + "[class*='CommentItemContainer']");
            """))
        except Exception:
            return False

    def _tap_comment_control(self, el):
        d = self.driver
        try:
            d.execute_script("""
              var el=arguments[0];
              var a=el.closest('a')||el;
              a.removeAttribute('href'); a.removeAttribute('target');
              a.setAttribute('href','javascript:void(0)');
            """, el)
        except Exception:
            pass
        try:
            box = el.rect
            x = int(box["x"] + box["width"] / 2)
            y = int(box["y"] + box["height"] / 2)
            d.execute_cdp_cmd("Input.dispatchMouseEvent", {
                "type": "mousePressed", "x": x, "y": y,
                "button": "left", "clickCount": 1})
            d.execute_cdp_cmd("Input.dispatchMouseEvent", {
                "type": "mouseReleased", "x": x, "y": y,
                "button": "left", "clickCount": 1})
        except Exception:
            pass
        d.execute_script(
            "(function(el){"
            "try{el.scrollIntoView({block:'center',inline:'center'});}catch(e){}"
            "var bn=function(e){try{e.preventDefault();}catch(_){}};"
            "var p=el;"
            "while(p&&p.tagName){"
            "if(p.tagName.toLowerCase()==='a')"
            "p.addEventListener('click',bn,{capture:true,once:true});"
            "p=p.parentElement;}"
            "var r=el.getBoundingClientRect();"
            "var cx=r.left+r.width/2,cy=r.top+r.height/2;"
            "var base={bubbles:true,cancelable:true,clientX:cx,clientY:cy,"
            "button:0,pointerId:1,pointerType:'touch'};"
            "var fire=function(type,init){"
            "var C=window[type];"
            "if(!C&&type.indexOf('pointer')===0)C=window.MouseEvent;"
            "if(!C)return;"
            "try{el.dispatchEvent(new C(type,init));}catch(e){}};"
            "try{"
            "var t=new Touch({identifier:1,target:el,clientX:cx,clientY:cy});"
            "el.dispatchEvent(new TouchEvent('touchstart',{bubbles:true,"
            "cancelable:true,touches:[t],targetTouches:[t],changedTouches:[t]}));"
            "el.dispatchEvent(new TouchEvent('touchend',{bubbles:true,"
            "cancelable:true,touches:[],targetTouches:[],changedTouches:[t]}));"
            "}catch(e){}"
            "fire('pointerdown',base); fire('mousedown',base);"
            "fire('pointerup',base); fire('mouseup',base); fire('click',base);"
            "})", el)

    def _try_open_comment_panel(self, got_any_fn) -> bool:
        """Khong click play-side-comment (deep-link onelink / mo app)."""
        d = self.driver
        sels = (
            "[data-e2e='comment-icon']",
            "[data-e2e='browse-comment']",
            "[data-e2e='comments']",
            "[data-e2e='browse-comment-icon']",
        )
        aweme_id = extract_aweme_id((d.current_url if d else "") or "")
        is_scout = callable(getattr(self, "_scout_publish", None))
        max_attempts = 3 if is_scout else 8
        for _ in range(max_attempts):
            if self.stop_event.is_set() or self._scout_peer_ready_rows(aweme_id):
                return False
            if captcha_present(d):
                self.log("CAPTCHA luc mo panel -> giai ngay...")
                self.handle_captcha()
            self._dismiss_open_app_cta()
            if self._comment_panel_open():
                return True
            click_comment_tab(d)
            clicked = False
            for sel in sels:
                els = d.find_elements(By.CSS_SELECTOR, sel)
                if not els:
                    continue
                try:
                    self._tap_comment_control(els[0])
                    clicked = True
                except Exception:
                    pass
                break
            if not clicked:
                self.open_comment_panel()
            for _ in range(10):
                if self.stop_event.is_set() or self._scout_peer_ready_rows(aweme_id):
                    return False
                if captcha_present(d):
                    self.handle_captcha()
                if self._comment_panel_open():
                    return True
                time.sleep(0.3)
        return self._comment_panel_open()

    def harvest_comments_by_scroll(self, username: str,
                                   stop_at_first: bool = False) -> list:
        """Thu comment của @username bằng cách để TRANG tự gọi API rồi hook response.

        Mở panel comment + cuộn hết (bấm cả nút 'Xem thêm' reply). Hook fetch đã cài
        ở _install_comment_hook gom mọi response comment/list vào
        window.__tt_comment_bodies. Parse các body đó → lấy comment+reply của user.
        Đây là đường ĐÁNG TIN nhất vì response do trang phát ra đã được ký hợp lệ
        (khác với gọi API thủ công luôn bị trả rỗng). Trả list [{"cid","text"}].
        """
        d = self.driver
        if d is None or self.stop_event.is_set():
            return []
        uname = _norm_handle(username)
        self._current_scan_uname = uname
        # uname rỗng: vẫn cuộn để đổ hook (VIP auto-comment hỏi giá).
        self._reset_comment_hook()
        self._scroll_round = 0
        self._no_panel_rounds = 0
        self._synced_from_peer_scout = False
        cur_url = ""
        if not getattr(self, "_driver_unresponsive", False):
            try:
                cur_url = str(d.current_url or "")
            except Exception:
                self._mark_driver_unresponsive()
        if not cur_url:
            cur_url = str(getattr(self, "_last_known_url", "") or getattr(self, "_last_target_link", "") or "")
        else:
            self._last_known_url = cur_url
        aweme_id_fast = extract_aweme_id(cur_url)
        if uname and aweme_id_fast:
            if not getattr(self, "_driver_unresponsive", False):
                try:
                    WebDriverWait(d, 5).until(
                        lambda dr: dr.execute_script("return !!window.__LOADABLE_LOADED_CHUNKS__")
                    )
                except Exception:
                    pass
            self.log(f"⚡ Quét nhanh comment gốc + reply ẩn của @{uname} qua API đã ký...")
            fast_found = self.find_all_comments_by_user_api(
                aweme_id_fast, uname, stop_at_first=stop_at_first
            )
            if fast_found:
                publish = getattr(self, "_scout_publish", None)
                if callable(publish) and not getattr(self, "_synced_from_peer_scout", False):
                    try:
                        publish(
                            aweme_id_fast,
                            uname,
                            fast_found,
                            authoritative=(getattr(self, "_consecutive_api_failures", 0) == 0),
                        )
                    except TypeError:
                        publish(aweme_id_fast, uname, fast_found)
                    except Exception:
                        pass
                if not getattr(self, "_synced_from_peer_scout", False):
                    self.log(f"✅ Scout API khớp {len(fast_found)} CID của @{uname} — chuyển pool gửi API ngay.")
                return fast_found
            if self.stop_event.is_set():
                return []
            peer_rows = self._scout_peer_ready_rows(aweme_id_fast, uname)
            if peer_rows:
                self.log(f"✅ Đồng bộ {len(peer_rows)} CID từ scout đã khớp API — chuyển pool gửi API ngay.")
                return peer_rows
            if callable(getattr(self, "_scout_publish", None)) and (
                getattr(self, "_driver_unresponsive", False)
                or getattr(self, "_consecutive_api_failures", 0) >= 2
            ):
                self.log("⚠️ Tab scout này bị nghẽn API comment — đóng sớm để nhường scout đối ứng / chuyển pool.")
                return []

        def _got_any() -> bool:
            try:
                return bool(self.driver.execute_script(
                    "return (window.__tt_comment_bodies||[]).length > 0;"))
            except Exception:
                return False

        # Chờ nút comment-icon render (photo/video post lazy-load ~2-4s). Đây là
        # bước hay bất ổn khi đa luồng (trang render chậm) → chờ tới 20s.
        try:
            WebDriverWait(d, 20).until(
                lambda dr: self.stop_event.is_set()
                or getattr(self, "_driver_unresponsive", False)
                or bool(self._scout_peer_ready_rows(aweme_id_fast, uname))
                or dr.find_elements(
                    By.CSS_SELECTOR, "[data-e2e='comment-icon'], "
                    "[data-e2e='browse-comment'], [data-e2e='play-side-comment'], "
                    "[data-testid='tux-web-tab-bar'], [data-e2e='comments']"))
        except Exception:
            pass
        if self.stop_event.is_set():
            return []
        peer_rows = self._scout_peer_ready_rows(aweme_id_fast, uname)
        if peer_rows:
            self.log(f"✅ Đồng bộ {len(peer_rows)} CID từ scout đã khớp API — chuyển pool gửi API ngay.")
            return peer_rows

        opened = self._try_open_comment_panel(_got_any)
        peer_rows = self._scout_peer_ready_rows(aweme_id_fast, uname)
        if peer_rows:
            self.log(f"✅ Đồng bộ {len(peer_rows)} CID từ scout đã khớp API — chuyển pool gửi API ngay.")
            return peer_rows
        if not opened and self.is_login_wall():
            self.close_login_modal()
            self.log("⛔ TikTok đẩy modal bắt login — session acc đã chết.")
            return []
        if not opened and not callable(getattr(self, "_scout_publish", None)):
            try:
                cur = d.current_url or ""
                if "/photo/" in cur:
                    self.log("↻ Trang photo không có nút comment — thử bản video...")
                    d.get(cur.split("?")[0].replace("/photo/", "/video/"))
                else:
                    self.log("↻ Panel chưa render item — tải lại 1 lần...")
                    d.get(cur or d.current_url)
                try:
                    WebDriverWait(d, 12).until(
                        lambda dr: dr.find_elements(
                            By.CSS_SELECTOR, "[data-e2e='comment-icon'], "
                            "[data-e2e='browse-comment'], [data-e2e='play-side-comment'], "
                            "[data-testid='tux-web-tab-bar'], [data-e2e='comments']"))
                except TimeoutException:
                    pass
                opened = self._try_open_comment_panel(_got_any)
            except Exception:
                pass
        if self.stop_event.is_set() or self.driver is not d:
            return []
        if not opened:
            self.log("⚠️ Không mở được panel comment để thu (thử fallback UI).")
            path = self.dump_comment_debug("harvest_no_panel")
            if path:
                self.log(f"Gui file: {path}\\diag.json va shot.png")
            if not _got_any():
                return []
        elif not _got_any() and not self._comment_panel_open():
            path = self.dump_comment_debug("opened_but_zero_items")
            if path:
                self.log(f"Panel gia (0 item). Gui file: {path}\\diag.json")

        max_loops = 10 if stop_at_first else 20
        scroll_js = r"""
        const items = document.querySelectorAll(
            "[class*='CommentItemContainer'],[class*='DivCommentItemContainer']," +
            "[class*='DivCommentItemWrapper'],[class*='CommentItemWrapper']," +
            "[class*='CommentObjectWrapper'],[data-e2e='comment-list-item']," +
            "[class*='CommentItem']");
        function scrollNode(p){
            const st = getComputedStyle(p);
            const ok = (/(auto|scroll)/.test(st.overflowY) || p.scrollHeight > p.clientHeight + 20);
            if(ok && p.scrollHeight > p.clientHeight + 8){
                const step = Math.max(420, Math.floor(p.clientHeight * 0.78));
                p.scrollTop = Math.min(p.scrollHeight, p.scrollTop + step);
                try { p.dispatchEvent(new Event('scroll', {bubbles:true})); } catch(_){}
                return true;
            }
            return false;
        }
        if(items.length){
            let p = items[items.length-1];
            while(p && p !== document.body){
                scrollNode(p);
                p = p.parentElement;
            }
        } else {
            const roots = document.querySelectorAll(
                "[class*='DivCommentListContainer'],[class*='CommentListContainer']," +
                "[class*='DivCommentMain'],[class*='comment-list']," +
                ".tux-base-dialog--open,[class*='tux-dialog__content']," +
                "[class*='CommentSheet']");
            let n = 0;
            roots.forEach(r => {
                const st = getComputedStyle(r);
                if (/(auto|scroll)/.test(st.overflowY) && r.scrollHeight > r.clientHeight+20) {
                    const step = Math.max(420, Math.floor(r.clientHeight * 0.78));
                    r.scrollTop = Math.min(r.scrollHeight, r.scrollTop + step); n++;
                }
                [...r.querySelectorAll('*')].forEach(c => {
                    const cs = getComputedStyle(c);
                    if (/(auto|scroll)/.test(cs.overflowY) && c.scrollHeight > c.clientHeight+20) {
                        const step = Math.max(420, Math.floor(c.clientHeight * 0.78));
                        c.scrollTop = Math.min(c.scrollHeight, c.scrollTop + step);
                    }
                });
            });
        }
        let expandFound = 0;
        document.querySelectorAll('p,span,button').forEach(e=>{
            const t=(e.textContent||'').trim().toLowerCase();
            if(t.length>40) return;
            const isMore = /câu trả lời|xem thêm|view more|repl|thêm \d/.test(t);
            const isHide = /^ẩn|hide/.test(t);
            if(isMore && !isHide){
                expandFound++;
                const child = [...e.querySelectorAll('p,span,button')].some(n => {
                    const text = (n.textContent||'').trim().toLowerCase();
                    return text.length <= 40 && /câu trả lời|xem thêm|view more|repl|thêm \d/.test(text) && !/^ẩn|hide/.test(text);
                });
                if(child) return;
                const target = e.closest('button,[role=button]') || e;
                const pending = window.__tt_reply_clicks || (window.__tt_reply_clicks = new WeakMap());
                if(Date.now() - (pending.get(target)||0) < 600) return;
                pending.set(target,Date.now());
                try{ target.click(); }catch(_){}
            }
        });
        // 3) Chẩn đoán: trạng thái panel trả về cho Python log định kỳ.
        window.__scroll_diag = {
            items: items.length,
            expand: expandFound,
            tabs: document.querySelectorAll("[data-testid='tux-web-tab-bar']").length,
            scrollables: (()=>{ let n=0, p = items.length ? items[items.length-1] : null;
                while(p && p !== document.body){
                    const st = getComputedStyle(p);
                    if(/(auto|scroll)/.test(st.overflowY) && p.scrollHeight > p.clientHeight+10) n++;
                    p = p.parentElement; }
                return n; })()
        };
        """
        last_count = -1
        stagnant = 0
        patience = 10
        empty_rounds = 0
        diag = None
        for _ in range(max_loops):
            # CAPTCHA chèn ngang giữa lúc cuộn — không check sẽ thu 0 comment
            # rồi ĐÓNG browser bỏ acc oan. Check mỗi vòng (rẻ) + blind solver:
            # để chính thư viện solver tự detect theo cách của nó, không phụ
            # thuộc bộ selector của mình (có biến thể vẫn sót).
            if captcha_present(d):
                self.log("⚠️ CAPTCHA xuất hiện giữa lúc thu comment → giải...")
                self.handle_captcha()
                if captcha_present(d):
                    break  # giải không xong — thoát, đừng cuộn tiếp vô ích
            # Panel có thể CHƯA MỞ dù hook có data: TikTok fetch comment ngầm
            # ngay khi load trang (panel đóng vẫn bắt được 11 comment trang 1).
            # Cuộn lúc đó = cuộn trong chân không → không bao giờ load trang 2.
            # Mỗi vòng: DOM chưa có item → click lại tab Bình luận cho tới khi
            # panel thật sự render. Không mở được sau 5 vòng → RELOAD 1 lần lấy
            # phiên mới (một số phiên TikTok cố tình không render panel); vẫn
            # không được sau 10 vòng → thoát sớm, đừng cuộn hụt tiếp.
            try:
                has_items = d.execute_script("""
                    return !!document.querySelector(
                        "[class*='DivCommentItemWrapper'],[class*='CommentItemContainer']," +
                        "[class*='DivCommentItemContainer'],[data-e2e='comment-list-item']");
                """)
                if not has_items:
                    no_panel_rounds = getattr(self, "_no_panel_rounds", 0) + 1
                    self._no_panel_rounds = no_panel_rounds
                    click_comment_tab(d)
                    if no_panel_rounds == 4:
                        self.log("⚠️ Panel chưa có item — click tab tiếp, không dừng sớm.")
                else:
                    self._no_panel_rounds = 0
            except Exception:
                pass
            try:
                d.execute_script(scroll_js)
            except Exception:
                pass
            self._check_stop()
            time.sleep(0.45)
            if self._http_debug:
                self._flush_http_logs()
            try:
                res = d.execute_script("""
                    const nodes = Object.values(window.__tt_comment_nodes || {});
                    const stats = window.__tt_comment_capture_stats || {};
                    const scroll = window.__scroll_diag || {};
                    let roots = 0, replies = 0;
                    for (const node of nodes) {
                        if (String(node.parent_cid || '')) replies++;
                        else roots++;
                    }
                    return {
                        records: nodes,
                        unique: nodes.length,
                        rootUnique: roots,
                        replyUnique: replies,
                        rootResponses: Number(stats.rootResponses || 0),
                        replyResponses: Number(stats.replyResponses || 0),
                        retained: (window.__tt_comment_bodies || []).length,
                        dropped: Number(window.__tt_comment_dropped || 0),
                        total: stats.total ?? null,
                        cursor: stats.cursor ?? null,
                        hasMore: stats.hasMore ?? null,
                        items: Number(scroll.items || 0),
                        expand: Number(scroll.expand || 0),
                        tabs: Number(scroll.tabs || 0),
                        scrollables: Number(scroll.scrollables || 0)
                    };
                """)
                cnt = res.get("unique", 0) if isinstance(res, dict) else 0
                diag = res if isinstance(res, dict) else None
                # Log every 5 vòng để phân biệt: TikTok total, số CID thực
                # nhận, reply đã nhận, cursor và số response bị buffer drop.
                if diag is not None and getattr(self, "_scroll_round", 0) % 5 == 0:
                    self.log(
                        f"🔎 Cuộn: hooked={diag['unique']} "
                        f"api-root={diag['rootResponses']} "
                        f"total={diag['total']} cursor={diag['cursor']} "
                        f"more={diag['hasMore']} root={diag['rootUnique']} "
                        f"reply={diag['replyUnique']} "
                        f"items={diag['items']} nút-reply={diag['expand']} "
                        f"buffer={diag['retained']} drop={diag['dropped']}")
                self._scroll_round = getattr(self, "_scroll_round", 0) + 1
            except Exception:
                cnt = last_count
                diag = None
            publish = getattr(self, '_scout_publish', None)
            if publish and isinstance(diag, dict):
                matches = []
                for node in diag.get('records') or []:
                    if not isinstance(node, dict) or not _user_matches(node.get('user') or {}, uname):
                        continue
                    matches.append({
                        'cid': str(node.get('cid') or ''),
                        'parent_cid': str(node.get('parent_cid') or node.get('reply_id') or node.get('reply_to_reply_id') or ''),
                        'text': (node.get('text') or '').strip(),
                        'already_liked': bool(node.get('user_digged')),
                    })
                ready = publish(extract_aweme_id(d.current_url or ''), uname, matches)
                if ready:
                    self.log(f'✅ Scout khớp {len(ready)} CID đã thu — chốt ngay, chuyển pool gửi API.')
                    return ready
            # Chưa có comment nào (panel load chậm khi đa luồng) → KHÔNG tính là
            # "đứng yên đã xong". Chờ tối đa 15 vòng (~20s) cho panel kịp load; chỉ
            # bỏ cuộc nếu thật sự không có gì (video bị tắt comment / panel lỗi).
            if not cnt:
                empty_rounds += 1
                if empty_rounds >= 30:
                    break
                continue
            empty_rounds = 0
            if cnt == last_count:
                stagnant += 1
                expand = (diag or {}).get("expand", 1) if isinstance(diag, dict) else 1
                if stagnant >= 4 and expand == 0 and self._list_exhausted():
                    break
                if stagnant == patience:
                    self.log("⏳ Comment tải chậm — tiếp tục chờ/cuộn, chưa kết luận hết.")
            else:
                stagnant = 0
                last_count = cnt

        # Bộ tích lũy giữ record tối thiểu theo CID nên không mất kết quả khi
        # raw-response buffer phải loại body cũ để giới hạn RAM.
        try:
            records = d.execute_script(
                "return Object.values(window.__tt_comment_nodes||{});") or []
        except Exception:
            records = []
        found, seen = [], set()
        scanned = 0

        def _take(node, parent_cid: str = "") -> bool:
            nonlocal scanned
            cid = str(node.get("cid") or "")
            if not cid or cid in seen:
                return False
            seen.add(cid)
            scanned += 1
            inferred_parent = str(
                parent_cid
                or node.get("parent_cid")
                or node.get("reply_id")
                or node.get("reply_to_reply_id")
                or "")
            if inferred_parent in ("0", cid):
                inferred_parent = ""
            if _user_matches(node.get("user") or {}, uname):
                found.append({
                    "cid": cid,
                    "parent_cid": inferred_parent,
                    "text": (node.get("text") or "").strip(),
                    "already_liked": bool(node.get("user_digged")),
                })
                return stop_at_first
            return False

        if records:
            for node in records:
                if isinstance(node, dict) and _take(
                        node, parent_cid=str(node.get("parent_cid") or "")):
                    break
        else:
            try:
                bodies = d.execute_script(
                    "return window.__tt_comment_bodies||[];") or []
            except Exception:
                bodies = []
            stop = False
            for body in bodies:
                try:
                    data = json.loads(body)
                except Exception:
                    continue
                for comment in (data.get("comments") or []):
                    root_cid = str(comment.get("cid") or "")
                    if _take(comment):
                        stop = True
                        break
                    for reply in (comment.get("reply_comment") or []):
                        if _take(reply, parent_cid=root_cid):
                            stop = True
                            break
                    if stop:
                        break
                if stop:
                    break
        if isinstance(diag, dict):
            self.log(
                f"📊 API comment cuối: total={diag['total']} "
                f"cursor={diag['cursor']} more={diag['hasMore']} "
                f"root={diag['rootUnique']} reply={diag['replyUnique']} "
                f"response-root={diag['rootResponses']} "
                f"response-reply={diag['replyResponses']} "
                f"buffer={diag['retained']} drop={diag['dropped']}")
        reply_hits = sum(1 for item in found if item.get("parent_cid"))
        self.log(
            f"ℹ️ Hook thu {scanned} comment từ trang → khớp {len(found)} "
            f"của @{uname} ({reply_hits} reply).")
        if uname and (not found or not stop_at_first):
            aweme_id = extract_aweme_id(d.current_url or "")
            if aweme_id:
                self.log(
                    "↻ Quét bổ sung toàn bộ comment gốc + reply ẩn qua API đã ký...")
                api_found = self.find_all_comments_by_user_api(
                    aweme_id, uname, stop_at_first=stop_at_first)
                if api_found:
                    for item in api_found:
                        cid_val = str(item.get("cid") or "")
                        if cid_val and cid_val not in seen:
                            seen.add(cid_val)
                            found.append(item)
                            if stop_at_first:
                                break
        return found

    def find_all_comments_by_user_api(self, aweme_id: str, username: str,
                                      stop_at_first: bool = False) -> list:
        """Tìm comment của @username trên video qua /api/comment/list/ đã ký."""
        uname = _norm_handle(username)
        self._current_scan_uname = uname
        self._consecutive_api_failures = 0
        if not aweme_id or not uname:
            return []
        found = []
        seen = set()
        cursor = 0
        tried_cursors = set()
        total_scanned = 0
        reply_scanned = 0

        def _consume(node, parent_cid: str = "") -> bool:
            """Duyệt một root/reply; giữ parent CID cho mutation tim reply."""
            nonlocal total_scanned
            total_scanned += 1
            if _user_matches(node.get("user") or {}, uname):
                cid = str(node.get("cid") or "")
                if cid and cid not in seen:
                    seen.add(cid)
                    inferred_parent = str(
                        parent_cid
                        or node.get("reply_id")
                        or node.get("reply_to_reply_id")
                        or "").strip()
                    if inferred_parent in ("0", cid):
                        inferred_parent = ""
                    found.append({
                        "cid": cid,
                        "parent_cid": inferred_parent,
                        "text": (node.get("text") or "").strip(),
                        "already_liked": bool(node.get("user_digged")),
                    })
                    if stop_at_first:
                        return True
            return False

        need_root_pages = True
        try:
            hook_state = self.driver.execute_script(
                "return {nodes: Object.values(window.__tt_comment_nodes || {}), "
                "stats: window.__tt_comment_capture_stats || {}};"
            )
        except Exception:
            hook_state = None
        if isinstance(hook_state, dict):
            h_nodes = hook_state.get("nodes") or []
            h_stats = hook_state.get("stats") or {}
            reply_counts = {}
            root_nodes = []
            for n in h_nodes:
                if not isinstance(n, dict):
                    continue
                p_cid = str(n.get("parent_cid") or "").strip()
                if p_cid and p_cid != "0":
                    reply_counts[p_cid] = reply_counts.get(p_cid, 0) + 1
                else:
                    root_nodes.append(n)
            for n in h_nodes:
                if isinstance(n, dict):
                    if _consume(n, parent_cid=str(n.get("parent_cid") or "")):
                        return found
            pending_root_cids = []
            for r_node in root_nodes:
                r_cid = str(r_node.get("cid") or "")
                r_total = int(r_node.get("reply_comment_total") or 0)
                if r_cid and r_total > reply_counts.get(r_cid, 0):
                    pending_root_cids.append(r_cid)
            if not stop_at_first and len(pending_root_cids) > 1:
                for idx in range(0, len(pending_root_cids), 12):
                    self._check_stop()
                    peer_rows = self._scout_peer_ready_rows(aweme_id, uname)
                    if peer_rows:
                        self._synced_from_peer_scout = True
                        self.log(f"✅ Đồng bộ {len(peer_rows)} CID từ scout đã khớp API — dừng quét trùng.")
                        return peer_rows
                    if getattr(self, "_driver_unresponsive", False):
                        self.log("⚠️ Tab trình duyệt không phản hồi API comment — bỏ qua quét API trên tab này.")
                        break
                    chunk_cids = pending_root_cids[idx:idx + 12]
                    batch_map = self._fetch_replies_api_batch(aweme_id, chunk_cids)
                    if not getattr(self, "_last_signed_get_ok", True):
                        self._consecutive_api_failures += 1
                        if getattr(self, "_driver_unresponsive", False) or self._consecutive_api_failures >= 2:
                            self.log("⚠️ API comment không phản hồi — bỏ qua quét API trên tab này.")
                            break
                    else:
                        self._consecutive_api_failures = 0
                    for r_cid in chunk_cids:
                        replies = batch_map.get(r_cid) or []
                        reply_scanned += len(replies)
                        for rep in replies:
                            if _consume(rep, parent_cid=r_cid):
                                return found
            else:
                for r_cid in pending_root_cids:
                    self._check_stop()
                    peer_rows = self._scout_peer_ready_rows(aweme_id, uname)
                    if peer_rows:
                        self._synced_from_peer_scout = True
                        self.log(f"✅ Đồng bộ {len(peer_rows)} CID từ scout đã khớp API — dừng quét trùng.")
                        return peer_rows
                    if getattr(self, "_driver_unresponsive", False):
                        self.log("⚠️ Tab trình duyệt không phản hồi API comment — bỏ qua quét API trên tab này.")
                        break
                    replies = self._fetch_replies_api(aweme_id, r_cid, start_cursor=0)
                    if not getattr(self, "_last_signed_get_ok", True):
                        self._consecutive_api_failures += 1
                        if getattr(self, "_driver_unresponsive", False) or self._consecutive_api_failures >= 2:
                            self.log("⚠️ API comment không phản hồi — bỏ qua quét API trên tab này.")
                            break
                    else:
                        self._consecutive_api_failures = 0
                    reply_scanned += len(replies)
                    for rep in replies:
                        if _consume(rep, parent_cid=r_cid):
                            return found
            if root_nodes and h_stats.get("cursor") is not None:
                if h_stats.get("hasMore") in (0, False):
                    need_root_pages = False
                else:
                    cursor = int(h_stats.get("cursor") or 0)

        max_pages = (10 if stop_at_first else 40) if need_root_pages else 0
        for _ in range(max_pages):
            self._check_stop()
            peer_rows = self._scout_peer_ready_rows(aweme_id, uname)
            if peer_rows:
                self._synced_from_peer_scout = True
                self.log(f"✅ Đồng bộ {len(peer_rows)} CID từ scout đã khớp API — dừng quét trùng.")
                return peer_rows
            if getattr(self, "_driver_unresponsive", False) or self._consecutive_api_failures >= 2:
                break
            if cursor in tried_cursors:
                break
            tried_cursors.add(cursor)
            data = self._tiktok_signed_get(
                "/api/comment/list/",
                {"aid": 1988, "aweme_id": str(aweme_id), "count": 20, "cursor": cursor},
            )
            if not self._is_valid_comment_api_payload(data):
                self._consecutive_api_failures += 1
                break
            self._consecutive_api_failures = 0
            page_hidden_cids = []
            for c in (data.get("comments") or []):
                root_cid = str(c.get("cid") or "")
                if _consume(c, parent_cid=""):
                    return found
                embedded = c.get("reply_comment") or []
                for node in embedded:
                    if _consume(node, parent_cid=root_cid):
                        return found
                total_reply = int(c.get("reply_comment_total") or 0)
                has_hidden = (total_reply > len(embedded)
                              or bool(c.get("thread_has_more"))
                              or (total_reply > 0 and not embedded))
                if has_hidden and root_cid:
                    if stop_at_first:
                        self._check_stop()
                        peer_rows = self._scout_peer_ready_rows(aweme_id, uname)
                        if peer_rows:
                            self._synced_from_peer_scout = True
                            self.log(f"✅ Đồng bộ {len(peer_rows)} CID từ scout đã khớp API — dừng quét trùng.")
                            return peer_rows
                        replies = self._fetch_replies_api(
                            aweme_id, root_cid, start_cursor=0)
                        if not getattr(self, "_last_signed_get_ok", True):
                            self._consecutive_api_failures += 1
                            if getattr(self, "_driver_unresponsive", False) or self._consecutive_api_failures >= 2:
                                self.log("⚠️ API comment không phản hồi — bỏ qua quét API trên tab này.")
                                break
                        else:
                            self._consecutive_api_failures = 0
                        reply_scanned += len(replies)
                        for node in replies:
                            if _consume(node, parent_cid=root_cid):
                                return found
                    else:
                        page_hidden_cids.append(root_cid)
            if page_hidden_cids and not getattr(self, "_driver_unresponsive", False) and self._consecutive_api_failures < 2:
                for idx in range(0, len(page_hidden_cids), 12):
                    self._check_stop()
                    peer_rows = self._scout_peer_ready_rows(aweme_id, uname)
                    if peer_rows:
                        self._synced_from_peer_scout = True
                        self.log(f"✅ Đồng bộ {len(peer_rows)} CID từ scout đã khớp API — dừng quét trùng.")
                        return peer_rows
                    chunk_cids = page_hidden_cids[idx:idx + 12]
                    if len(chunk_cids) == 1:
                        replies_map = {chunk_cids[0]: self._fetch_replies_api(aweme_id, chunk_cids[0], start_cursor=0)}
                    else:
                        replies_map = self._fetch_replies_api_batch(aweme_id, chunk_cids)
                    if not getattr(self, "_last_signed_get_ok", True):
                        self._consecutive_api_failures += 1
                        if getattr(self, "_driver_unresponsive", False) or self._consecutive_api_failures >= 2:
                            self.log("⚠️ API comment không phản hồi — bỏ qua quét API trên tab này.")
                            break
                    else:
                        self._consecutive_api_failures = 0
                    for r_cid in chunk_cids:
                        replies = replies_map.get(r_cid) or []
                        reply_scanned += len(replies)
                        for node in replies:
                            if _consume(node, parent_cid=r_cid):
                                return found
            if getattr(self, "_driver_unresponsive", False) or self._consecutive_api_failures >= 2:
                break
            if not data.get("has_more"):
                break
            next_cursor = data.get("cursor")
            if next_cursor is None or int(next_cursor) == cursor:
                break
            cursor = int(next_cursor)
        peer_rows = self._scout_peer_ready_rows(aweme_id, uname)
        if peer_rows and (getattr(self, "_consecutive_api_failures", 0) > 0 or len(peer_rows) >= len(found)):
            self._synced_from_peer_scout = True
            self.log(f"✅ Đồng bộ {len(peer_rows)} CID từ scout đã khớp API — dừng quét trùng.")
            return peer_rows
        self.log(f"ℹ️ Comment từ API: {total_scanned} comment "
                 f"({reply_scanned} reply ẩn) → khớp {len(found)} của @{uname}.")
        if not found and total_scanned:
            self.log("⚠️ API cursor cũng không khớp comment nào của target.")
        return found

    def prefetch_comments_for_run(self, links, target: str,
                                  find_first: bool = False) -> int:
        """No-op: /api/comment/list từ homepage luôn body rỗng (thiếu chữ ký
        video context). Cache được điền trong like_comment SAU open_link."""
        return 0

    def _ui_like_by_snippet(
            self, snippet: str, position: int = 0,
            scroll_top: int | None = None) -> bool:
        """Tìm comment theo nội dung rồi kích hoạt nút tim native."""
        needle = _fold_text(snippet)
        if not needle:
            return False
        d = self.driver
        try:
            scroll_target = self._get_comment_scroll_target()
            if scroll_target is not None:
                target_top = max(0, int(scroll_top or 0))
                d.execute_script(
                    "arguments[0].scrollTop=arguments[1];",
                    scroll_target, target_top)
            elif position:
                for _ in range(max(0, int(position)) // 8):
                    self._scroll_comment_list(520)
                    time.sleep(0.15)
            def _expand_visible_replies():
                try:
                    return int(d.execute_script(r"""
                        let clicked = 0;
                        document.querySelectorAll(
                          "[class*='DivViewRepliesContainer'],"
                          + "[class*='DivViewMoreRepliesOptionsContainer'],"
                          + "[class*='DivViewMoreRepliesWrapper']"
                        ).forEach(el => {
                          const text = (el.textContent || '').trim().toLowerCase();
                          if (/ẩn|hide/.test(text)) return;
                          try { el.click(); clicked++; } catch (_) {}
                        });
                        return clicked;
                    """) or 0)
                except Exception:
                    return 0
            expanded = False
            if _expand_visible_replies():
                expanded = True
                time.sleep(.4)
            for round_index in range(32):
                items = d.find_elements(
                    By.CSS_SELECTOR,
                    "[class*='DivCommentItemWrapper'], "
                    "[class*='CommentItemContainer'], "
                    "[data-e2e='comment-list-item']")
                matches = []
                for item in items:
                    try:
                        folded = _fold_text(item.text or "")
                        if needle in folded:
                            matches.append((len(folded), item))
                    except Exception:
                        continue
                for _length, item in sorted(matches, key=lambda pair: pair[0]):
                    try:
                        used = bool(d.execute_script(
                            "return arguments[0].dataset.alcHandoffLiked==='1';",
                            item))
                        if used or not self._click_like_in_item(item):
                            continue
                        d.execute_script(
                            "arguments[0].dataset.alcHandoffLiked='1';", item)
                        return True
                    except Exception:
                        continue
                if not expanded and round_index % 4 == 3:
                    expanded = bool(_expand_visible_replies())
                    if expanded:
                        time.sleep(.4)
                self._scroll_comment_list(460)
                time.sleep(0.25)
        except Exception:
            pass
        return False

    def _click_like_in_item(self, item):
        d = self.driver
        d.execute_script("arguments[0].scrollIntoView({block:'center'});", item)
        self._rsleep(0.5)   # cuộn tới comment rồi khựng lại như đang đọc
        like_btn = None
        for sel in [
            # Selector chính xác từ DOM thực tế
            "[class*='DivLikeContainer']",
            # Fallback data-e2e
            "[data-e2e='comment-like-icon']",
            "[data-e2e='like-icon']",
            "span[data-e2e='comment-like']",
            "button[aria-label*='like' i]",
            "span[aria-label*='like' i]",
        ]:
            btns = item.find_elements(By.CSS_SELECTOR, sel)
            if btns:
                like_btn = btns[0]
                break
        if not like_btn:
            self.log("❌ Không tìm thấy nút tim trong comment.")
            return False

        # Kiểm tra đã tim chưa qua color của SVG
        # Đã tim: color="var(--ui-text-primary-display)"
        # Chưa tim: color="var(--ui-text-3)"
        try:
            svg = like_btn.find_element(By.CSS_SELECTOR, "svg")
            color = svg.get_attribute("color") or ""
            if "ui-text-primary-display" in color:
                self.log("💙 Comment đã được tim rồi, bỏ qua.")
                return True
        except Exception:
            pass

        if not self._js_click(like_btn):
            return False
        self._rsleep(0.8)
        self.log("✅ Đã tim comment.")
        return True

    # ── VIP auto-comment (hỏi giá) ────────────────────────────────────────
    _PRICE_ASK_RE = re.compile(
        r"(xin\s*gi[aáàảạã]|h[oỏ]i\s*gi[aáàảạã]|cho\s*gi[aáàảạã]|"
        r"gi[aáàảạã]\s*(bao|bn|bnhiu|nhi[eê]u|n[aà]o|sao|v[aậ]y|z[iị]|ạ|shop|"
        r"t[uúùủụũ]i|qu[aầầẩẫậ]n|[aáàảạã]o|v[aá]y|d[eé]p|size|m[aã]u)|"
        r"bao\s*nhi[eê]u|bao\s*nhiu|bnhiu|bn\s*(v[aậ]y|z[iị]|ạ|shop)|"
        r"charm\s*(bao|bn|gi[aá])|c[oò]\s*gi[aáàảạã]|"
        r"pass\s*(bao|bn)|sale\s*kh[oô]ng|c[oò]n\s*h[aà]ng|"
        r"ship\s*(bao|bn)|gi[aáàảạã]\s*\?)",
        re.IGNORECASE,
    )
    # Cụm thêm (mỗi dòng 1 cụm). UI VIP có thể đẩy vào extra_phrases.
    _PRICE_ASK_EXTRA: tuple[str, ...] = (
        "mk xin", "em xin", "cho em xin", "xin ạ", "xin shop",
        "giá sao", "giá bn", "giá z", "giá k", "gia bn", "gia z",
        "bao nhieu", "bao nhìu", "bn z", "bn k", "bn ạ",
        "pass k", "pass ko", "ib giá", "ib gia", "inb giá",
        "còn k", "còn ko", "con hang", "order k",
    )

    @classmethod
    def is_price_ask_comment(cls, text: str, extra_phrases=None) -> bool:
        t = (text or "").strip()
        if not t or len(t) > 220:
            return False
        low = t.casefold()
        if cls._PRICE_ASK_RE.search(t):
            return True
        extras = list(cls._PRICE_ASK_EXTRA)
        if extra_phrases:
            extras.extend(str(x).strip() for x in extra_phrases if str(x).strip())
        return any(x.casefold() in low for x in extras)


    _VIP_COMMENT_TEMPLATES = (
        "𝐗𝐨𝐚́ [ 𝟮 𝗰𝗵𝘂̛̃ 𝐎𝐎 đầu ] , 𝗴𝗵𝗶𝗶 𝘃𝗮̀𝗼 𝘀𝗼̣̂𝗽𝗽 𝗽𝗶̀𝗶 𝗹𝗮̀ 𝗿𝗮 𝗻𝗵𝗲́ 𝗮̣ 🌸 {code}",
        "𝐗𝐨𝐚́[𝐜𝐡𝐮̛̃ 𝐎𝐎 đầu ], ghii vào sộpp pìi ạ 🦊 {code}",
        "𝗗𝗮́𝗻 𝗺:;𝗮̃ 𝘃𝗮̀𝗼 𝘀hop;be[ 𝘅𝗼𝗮́ 2 𝗰𝗵𝘂̛̃ OO đầu ] ạ 🌷 {code}",
        "𝘾𝙪𝙤̂́𝙞𝙞 𝙘𝙖̂𝙪 𝙘𝙤́ 𝙢;𝙖̃𝙖 , 𝙭𝙤𝙖́ [ 𝙘𝙝𝙪̛̃ 𝙊𝙊 đầu ] 𝙧𝙤̂̀𝙞 𝙙𝙖́𝙣 𝙢𝙖̃𝙖 𝙫𝙖̀𝙤 𝙨𝙝𝙤𝙥;𝙗𝙚 𝙡𝙖̀ 𝙧𝙖 𝙖̣ 🌷 {code}",
        "Bai ☂️Tiểu sử @{username} số {number} coá ạ",
    )
    _VIP_COMMENT_REACTIONS = ("😊", "❤️")

    _OUR_STYLE_MARKERS = (
        "shop//bii", "𝘀hop//bii", "sộppi", "shoppii", "sộpp pìi", "sộpp pìi",
        "𝘀𝗼̣̂𝗽𝗽", "shop;be", "𝙨𝙝𝙤𝙥;𝙗𝙚", "2 chữ oo", "𝟮 𝗰𝗵𝘂̛̃", "𝐨𝐨",
        "oo đầu", "𝐱𝟎'𝐚", "cuối câu có m", "𝙘𝙪𝙤̂́𝙞𝙞", "𝗱𝗮́𝗻 𝗺", "xoá[", "𝘅𝗼𝗮́",
        "ghii vào", "𝗴𝗵𝗶𝗶", "tiểu sử @ngọc vi số",
    )

    @staticmethod
    def _strip_invisibles(text: str) -> str:
        out = []
        for ch in text or "":
            o = ord(ch)
            if ch in ("￶", "\u3000", "\u200b", "\u200c", "\u200d", "\ufeff"):
                continue
            if 0x2000 <= o <= 0x200F or 0x2028 <= o <= 0x202F:
                continue
            out.append(ch)
        return "".join(out)

    @classmethod
    def is_our_style_reply(cls, text: str) -> bool:
        t = cls._strip_invisibles(text or "").lower()
        if len(t) < 8:
            return False
        return any(m.lower() in t for m in cls._OUR_STYLE_MARKERS)

    @classmethod
    def normalize_shop_codes(cls, text: str) -> list[str]:
        """Lấy mọi mã 3-3-3 theo thứ tự, bỏ OO và mã trùng."""
        value = unicodedata.normalize(
            "NFKC", cls._strip_invisibles(text or "")).upper()
        found = re.findall(
            r"(?:^|[^A-Z0-9])(?:OO)?"
            r"([A-Z0-9]{3}-[A-Z0-9]{3}-[A-Z0-9]{3})"
            r"(?=$|[^A-Z0-9])",
            value)
        return list(dict.fromkeys(found))

    @classmethod
    def normalize_shop_code(cls, code: str) -> str:
        """Tương thích luồng một mã; trả mã 3-3-3 đầu tiên."""
        codes = cls.normalize_shop_codes(code)
        return codes[0] if codes else ""

    @classmethod
    def display_shop_codes(cls, text: str) -> list[str]:
        """Lấy mọi mã để đăng, giữ tiền tố OO riêng của từng mã."""
        value = unicodedata.normalize(
            "NFKC", cls._strip_invisibles(text or "")).upper()
        found = re.findall(
            r"(?:^|[^A-Z0-9])((?:OO)?"
            r"[A-Z0-9]{3}-[A-Z0-9]{3}-[A-Z0-9]{3})"
            r"(?=$|[^A-Z0-9])",
            value)
        return list(dict.fromkeys(found))

    @classmethod
    def display_shop_code(cls, code: str) -> str:
        """Tương thích luồng một mã; trả mã hiển thị đầu tiên."""
        codes = cls.display_shop_codes(code)
        return codes[0] if codes else ""

    @classmethod
    def extract_shop_codes(cls, text: str) -> list[str]:
        """Lấy mọi mã 3-3-3 trong một reply."""
        return cls.normalize_shop_codes(text)

    @classmethod
    def extract_numbers(cls, text: str) -> list[str]:
        """Lấy number đứng sau từ 'số'; number độc lập với shop code."""
        value = unicodedata.normalize(
            "NFKC", cls._strip_invisibles(text or "")).casefold()
        found = re.findall(r"\bsố\s*(\d+)\b", value)
        return list(dict.fromkeys(found))

    @staticmethod
    def _join_values(values: list[str], noun: str) -> str:
        if not values:
            return ""
        return f" và {noun} ".join(values)

    def format_vip_reply(self, code: str, numbers: str = "",
                         bio_username: str = "",
                         template: str | None = None,
                         templates: list | None = None,
                         kind: str = "") -> str:
        """Điền code và number độc lập; mỗi bên hỗ trợ nhiều giá trị."""
        display_codes = self.display_shop_codes(code)
        number_values = re.findall(r"\d+", str(numbers or ""))
        number_values = list(dict.fromkeys(number_values))
        requested_padding = "￶ " * str(code or "").count("￶")
        code_text = requested_padding + self._join_values(
            display_codes, "mã") if display_codes else ""
        number_text = self._join_values(number_values, "số")
        username = str(bio_username or "").strip().lstrip("@")
        pool = [str(t) for t in (templates or []) if t]
        if not pool:
            pool = list(self._VIP_COMMENT_TEMPLATES)
        if template:
            pool = [str(template)]

        def compatible(value: str) -> bool:
            has_code = "{code}" in value
            has_number = "{number}" in value or "<number>" in value
            has_username = "{username}" in value
            if has_code and not code_text:
                return False
            if has_number and not number_text:
                return False
            if has_username and not username:
                return False
            if kind == "code" and not has_code:
                return False
            if kind == "number" and not has_number:
                return False
            return True

        prepared = []
        for value in pool:
            if not any(marker in value for marker in
                       ("{code}", "{number}", "<number>")):
                value = value.rstrip() + (
                    " {code}" if code_text else " {number}")
            if compatible(value):
                prepared.append(value)
        if not prepared:
            return ""
        last = getattr(self, "_last_vip_template", "")
        choices = [value for value in prepared if value != last] or prepared
        chosen = choices[0] if template else random.choice(choices)
        self._last_vip_template = chosen
        return chosen.replace("{code}", code_text).replace(
            "{number}", number_text).replace("<number>", number_text).replace(
                "{username}", username)

    def _comment_wrapper(self, node):
        d = self.driver
        try:
            return d.execute_script("""
                const el = arguments[0];
                return el && el.closest
                    ? el.closest("[class*='DivCommentObjectWrapper']")
                    : null;
            """, node)
        except Exception:
            return None

    def _js_click(self, el) -> bool:
        d = self.driver
        if not d or el is None:
            return False
        try:
            return bool(d.execute_script("""
                const el = arguments[0];
                if (!el) return false;
                try { el.scrollIntoView({block:'center', inline:'nearest'}); } catch (e) {}
                const r = el.getBoundingClientRect();
                const x = r.left + Math.min(r.width / 2, 12);
                const y = r.top + Math.min(r.height / 2, 8);
                const hit = document.elementFromPoint(x, y);
                const target = (hit && el.contains(hit)) ? hit : el;
                const opts = {bubbles:true, cancelable:true, view:window, clientX:x, clientY:y, button:0};
                for (const type of ['pointerdown','mousedown','pointerup','mouseup','click']) {
                    try { target.dispatchEvent(new MouseEvent(type, opts)); } catch (e) {}
                }
                try { target.click(); } catch (e) {}
                return true;
            """, el))
        except Exception:
            return False

    def _scroll_comment_list(self, delta: int = 520) -> None:
        d = self.driver
        if not d:
            return
        try:
            d.execute_script("""
                const delta = arguments[0];
                const roots = [
                    document.querySelector("[data-e2e='comment-list']"),
                    document.querySelector("[class*='DivCommentListContainer']"),
                    document.querySelector("[class*='CommentListContainer']"),
                    document.querySelector("[class*='DivCommentMain']"),
                    document.querySelector("[class*='CommentSheet']")
                ].filter(Boolean);
                let moved = false;
                for (const root of roots) {
                    const st = getComputedStyle(root);
                    if ((/(auto|scroll)/.test(st.overflowY) || root.scrollHeight > root.clientHeight + 20)) {
                        root.scrollTop += delta;
                        moved = true;
                    }
                    for (const child of root.querySelectorAll('*')) {
                        const cs = getComputedStyle(child);
                        if (/(auto|scroll)/.test(cs.overflowY) && child.scrollHeight > child.clientHeight + 20) {
                            child.scrollTop += delta;
                            moved = true;
                        }
                    }
                }
                if (!moved) window.scrollBy(0, delta);

            """, delta)
        except Exception:
            pass

    def _attach_captured_comment_ids(self, items: list[dict]) -> list[dict]:
        """Ghép CID/replies từ response list vào các comment đã thấy trên DOM."""
        if not items or not self.driver:
            return items
        try:
            bodies = self.driver.execute_script(
                "return (window.__tt_comment_bodies || []).slice();") or []
        except Exception:
            return items
        if not isinstance(bodies, list):
            return items
        by_key = {}
        by_text = {}
        for raw in bodies:
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(payload.get("data"), dict):
                payload = payload["data"]
            for node in payload.get("comments") or []:
                if str(node.get("reply_id") or "0") not in ("", "0"):
                    continue
                text_key = _fold_text(node.get("text") or "")
                cid = str(node.get("cid") or "")
                if not text_key or not cid:
                    continue
                user = node.get("user") or {}
                username = _norm_handle(
                    user.get("unique_id") or user.get("nickname") or "")
                value = {
                    "cid": cid,
                    "replies": list(node.get("reply_comment") or []),
                }
                by_key[(username, text_key)] = value
                by_text.setdefault(text_key, value)
        for item in items:
            text_key = _fold_text(item.get("text") or "")
            username = _norm_handle(item.get("username") or "")
            match = by_key.get((username, text_key)) or by_text.get(text_key)
            if not match:
                continue
            item["cid"] = match["cid"]
            if match["replies"]:
                item["replies"] = match["replies"]
        return items

    def harvest_price_ask_comments(self, extra_phrases=None) -> list:
        """Quét comment hỏi giá trực tiếp từ DOM của browser hiện tại."""
        d = self.driver
        if not d:
            return []
        found = {}
        unchanged = 0
        for scroll_round in range(18):
            before = len(found)
            try:
                scroll_top = int(d.execute_script("""
                    const roots = [...document.querySelectorAll(
                        "[data-e2e='comment-list'],[class*='DivCommentListContainer'],"
                        + "[class*='CommentListContainer'],[class*='DivCommentMain']")];
                    const hit = roots.find(x => x.scrollHeight > x.clientHeight + 20);
                    return hit ? hit.scrollTop : 0;
                """) or 0)
            except Exception:
                scroll_top = 0
            for dom_index, content in enumerate(d.find_elements(
                    By.CSS_SELECTOR, '[data-e2e="comment-level-1"]')):
                try:
                    text = " ".join((content.text or "").split())
                    if not text or not self.is_price_ask_comment(
                            text, extra_phrases):
                        continue
                    wrapper = self._comment_wrapper(content)
                    if wrapper is None:
                        continue
                    author = ""
                    try:
                        href = wrapper.find_element(
                            By.CSS_SELECTOR,
                            '[data-e2e="comment-username-1"] a[href*="/@"]'
                        ).get_attribute("href") or ""
                        author = href.rsplit("/@", 1)[-1].split("?", 1)[0]
                    except Exception:
                        pass
                    replies = []
                    for reply in wrapper.find_elements(
                            By.CSS_SELECTOR, '[data-e2e="comment-level-2"]'):
                        reply_text = " ".join((reply.text or "").split())
                        if not reply_text:
                            continue
                        username = ""
                        try:
                            href = d.execute_script("""
                                const el = arguments[0];
                                const a = el.closest("[class*='DivCommentItemWrapper'], [class*='CommentItem']")
                                    && el.closest("[class*='DivCommentItemWrapper'], [class*='CommentItem']")
                                         .querySelector("a[href*='/@']");
                                return a ? a.href : '';
                            """, reply) or ""
                            username = href.rsplit("/@", 1)[-1].split("?", 1)[0]
                        except Exception:
                            pass
                        replies.append({
                            "text": reply_text,
                            "user": {"unique_id": username},
                        })
                    key = (_norm_handle(author), _fold_text(text))
                    found.setdefault(key, {
                        "cid": "",
                        "text": text,
                        "username": _norm_handle(author),
                        "position": scroll_round * 20 + dom_index,
                        "scroll_top": scroll_top,
                        "replies": replies,
                    })
                except Exception:
                    continue
            if unchanged >= 4:
                break
            self._scroll_comment_list(540)
            time.sleep(random.uniform(0.5, 0.9))
        return self._attach_captured_comment_ids(list(found.values()))

    def _thread_reply_info(
            self, parent_cid: str, embedded: list,
            own_username: str = "") -> tuple[list[str], set[str], set[str]]:
        """Trả mã acc khác, mã của mình và bio number của mình."""
        nodes = list(embedded or [])
        other_codes = []
        other_seen = set()
        own_codes = set()
        own_numbers = set()
        own_norm = _norm_handle(own_username)
        for node in nodes:
            text = (node.get("text") or "").strip()
            if not self.is_our_style_reply(text):
                continue
            shop_codes = self.extract_shop_codes(text)
            numbers = self.extract_numbers(text)
            if not shop_codes and not numbers:
                continue
            user = node.get("user") or {}
            is_own = bool(own_norm and any(
                _norm_handle(user.get(field) or "") == own_norm
                for field in ("unique_id", "nickname")))
            if is_own:
                own_codes.update(shop_codes)
                own_numbers.update(numbers)
            else:
                for shop_code in shop_codes:
                    if shop_code not in other_seen:
                        other_seen.add(shop_code)
                        other_codes.append(shop_code)
        return other_codes, own_codes, own_numbers

    def _find_root_wrapper(self, root_text: str, timeout: float = 18):
        d = self.driver
        wanted = _fold_text(root_text)
        if not d or not wanted:
            return None
        deadline = time.time() + timeout
        while time.time() < deadline:
            for content in d.find_elements(
                    By.CSS_SELECTOR, '[data-e2e="comment-level-1"]'):
                try:
                    shown = _fold_text(content.text or "")
                    if shown == wanted or (wanted and wanted in shown):
                        wrapper = self._comment_wrapper(content)
                        if wrapper is not None:
                            return wrapper
                except Exception:
                    continue
            self._scroll_comment_list(460)
            time.sleep(0.45)
        return None

    def _type_into_editor(self, editor, text: str) -> bool:
        d = self.driver
        if not d or editor is None:
            return False
        try:
            self._js_click(editor)
            d.execute_script("""
                const el = arguments[0];
                el.focus();
                document.execCommand('selectAll', false, null);
            """, editor)
            d.execute_cdp_cmd("Input.insertText", {"text": text})
            typed = " ".join((editor.get_attribute("innerText")
                              or editor.text or "").split())
            wanted = " ".join(text.split())
            if wanted and wanted in typed:
                return True
            d.execute_script("""
                const el = arguments[0];
                const value = arguments[1];
                el.focus();
                document.execCommand('selectAll', false, null);
                document.execCommand('insertText', false, value);
                el.dispatchEvent(new InputEvent('input', {bubbles:true, data:value, inputType:'insertText'}));
            """, editor, text)
            typed = " ".join((editor.get_attribute("innerText")
                              or editor.text or "").split())
            return bool(wanted) and wanted in typed
        except Exception:
            return False

    def _wait_comment_post_response(self, timeout: float = 12) -> dict | None:
        """Đợi response `/api/comment/publish` đã được page hook ghi lại."""
        deadline = time.time() + max(0.1, timeout)
        while time.time() < deadline:
            try:
                rows = self.driver.execute_script("""
                    const rows = window.__tt_comment_posts || [];
                    window.__tt_comment_posts = [];
                    return rows;
                """)
                if isinstance(rows, list) and rows:
                    record = rows[-1] if isinstance(rows[-1], dict) else None
                    if record:
                        self._last_comment_publish_url = str(
                            record.get("url") or "")
                    return record
            except Exception:
                pass
            time.sleep(0.2)
        return None

    @staticmethod
    def _comment_post_outcome(record: dict) -> tuple[bool, str, str]:
        """Trả accepted, comment_id, mô tả ngắn từ response publish."""
        http_status = int(record.get("status") or 0)
        if http_status < 200 or http_status >= 300:
            return False, "", f"HTTP {http_status or 'transport error'}"
        try:
            payload = json.loads(str(record.get("body") or ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            return False, "", "response không phải JSON"
        if not isinstance(payload, dict):
            return False, "", "response JSON không hợp lệ"
        data = payload.get("data")
        status_code = payload.get("status_code")
        if status_code is None and isinstance(data, dict):
            status_code = data.get("status_code")
        message = (
            payload.get("status_msg") or payload.get("message")
            or payload.get("description") or "")
        if isinstance(data, dict):
            message = (
                data.get("status_msg") or data.get("message")
                or data.get("description") or message)
        if status_code not in (None, 0):
            return False, "", str(message or f"status_code={status_code}")[:120]
        if str(message).casefold() in ("error", "failed", "failure"):
            return False, "", str(message)[:120]

        def _find_id(value):
            if isinstance(value, dict):
                for key in ("cid", "comment_id"):
                    found = value.get(key)
                    if found not in (None, ""):
                        return str(found)
                for child in value.values():
                    found = _find_id(child)
                    if found:
                        return found
            elif isinstance(value, list):
                for child in value:
                    found = _find_id(child)
                    if found:
                        return found
            return ""

        comment_id = _find_id(payload)
        if (status_code is None and not comment_id
                and str(message).casefold() not in ("success", "ok")):
            return False, "", "response thiếu dấu hiệu publish thành công"
        return True, comment_id, str(message or "status_code=0")[:120]

    @staticmethod
    def _comment_rejection_kind(detail: str) -> str:
        """Phân loại lỗi publish để không retry lỗi TikTok đã kết luận."""
        folded = _fold_text(str(detail or ""))
        if any(token in folded for token in (
                "binh luan qua nhanh", "commenting too fast",
                "comment too fast", "too many comments", "rate limit",
                "too frequent")):
            return "rate_limit"
        if any(token in folded for token in (
                "filtered", "under review", "kiem duyet", "vi pham",
                "community guidelines")):
            return "filtered"
        if any(token in folded for token in (
                "transport error", "network error", "timeout",
                "response khong phai json", "response json khong hop le",
                "response thieu dau hieu")):
            return "transport"
        return "rejected"

    def post_comment_signed(self, text: str, reply_cid: str,
                            aweme_id: str = "") -> bool:
        """Gửi reply qua request được SDK của trang ký trong session hiện tại."""
        d = self.driver
        text = " ".join((text or "").split())
        reply_cid = str(reply_cid or "")
        aweme_id = str(aweme_id or extract_aweme_id(
            (d.current_url if d else "") or "") or "")
        self.last_comment_rate_limited = False
        self.last_comment_submission = {
            "accepted": False,
            "comment_id": "",
            "root_cid": reply_cid,
            "self_visible": None,
            "public_visible": None,
            "visibility": "not_accepted",
            "rejection": "",
            "detail": "chưa gửi",
        }
        if not d or not text or not reply_cid or not aweme_id:
            self.last_comment_submission["detail"] = (
                "thiếu session, video hoặc comment gốc")
            self.log("⚠️ Không xác định được comment gốc để gửi reply.")
            return False
        script = r"""
            const [awemeId, replyId, text, done] = arguments;
            try {
              const sources = (window.__tt_comment_urls || []).slice().reverse();
              const source = sources.find(raw => {
                try {
                  const u = new URL(raw, location.href);
                  return /\/api\/comment\/list(?:\/|\?)/i.test(u.pathname + '?')
                    && !/\/reply\//i.test(u.pathname)
                    && (!u.searchParams.get('aweme_id')
                        || u.searchParams.get('aweme_id') === String(awemeId));
                } catch (_) { return false; }
              });
              if (!source) {
                done({status:0, body:'', error:'missing comment context'});
                return;
              }
              const url = new URL(source, location.href);
              url.pathname = '/api/comment/publish/';
              [
                'X-Bogus', 'X-Dynosaur', 'X-Gnarly', '_signature', 'msToken',
                'cursor', 'count', 'item_id', 'comment_id', 'sort_type',
                'insert_ids', 'whale_cut_token', 'cut_version', 'rcFT'
              ].forEach(key => url.searchParams.delete(key));
              const params = {
                aid:'1988',
                app_name:'tiktok_web',
                channel:'tiktok_web',
                device_platform:'web_pc',
                aweme_id:String(awemeId),
                text:String(text),
                reply_id:String(replyId),
                reply_to_reply_id:'0',
                text_extra:'[]',
                from_page:'user',
                user_is_login:'true',
                focus_state:'true',
                is_page_visible:'true',
                is_fullscreen:'false',
                browser_online:String(navigator.onLine),
                cookie_enabled:String(navigator.cookieEnabled),
                history_len:String(history.length),
                screen_width:String(screen.width),
                screen_height:String(screen.height),
                tz_name:Intl.DateTimeFormat().resolvedOptions().timeZone || ''
              };
              Object.entries(params).forEach(([key, value]) =>
                url.searchParams.set(key, value));
              const xhr = new XMLHttpRequest();
              xhr.open('POST', url.toString(), true);
              xhr.withCredentials = true;
              xhr.setRequestHeader(
                'content-type', 'application/x-www-form-urlencoded');
              xhr.onloadend = () => done({
                status:Number(xhr.status || 0),
                body:String(xhr.responseText || '')
              });
              xhr.onerror = () => done({
                status:0, body:'', error:'network error'
              });
              xhr.send('');
            } catch (error) {
              done({status:0, body:'', error:String(error)});
            }
        """
        last_detail = ""
        for attempt in range(2):
            try:
                record = d.execute_async_script(
                    script, aweme_id, reply_cid, text)
            except Exception as exc:
                record = {
                    "status": 0,
                    "body": "",
                    "error": f"{type(exc).__name__}: {str(exc)[:80]}",
                }
            record = record if isinstance(record, dict) else {}
            accepted, comment_id, detail = self._comment_post_outcome(record)
            last_detail = str(record.get("error") or detail)
            if accepted:
                self.last_comment_submission = {
                    "accepted": True,
                    "comment_id": comment_id,
                    "root_cid": reply_cid,
                    "self_visible": None,
                    "public_visible": None,
                    "visibility": "accepted_unverified",
                    "rejection": "",
                    "detail": detail,
                }
                cid_note = f" · mã {comment_id}" if comment_id else ""
                self.log(
                    f"✅ TikTok API đã nhận reply{cid_note} · "
                    "chưa xác minh hiển thị công khai.")
                return True
            rejection = self._comment_rejection_kind(last_detail)
            if rejection == "rate_limit":
                self.last_comment_rate_limited = True
            retryable = (
                rejection == "transport"
                or int(record.get("status") or 0) == 0)
            if attempt == 0 and retryable:
                time.sleep(0.8)
                continue
            break
        rejection = self._comment_rejection_kind(last_detail)
        self.last_comment_submission.update({
            "visibility": "not_accepted",
            "rejection": rejection,
            "detail": last_detail[:120],
        })
        self.log(f"❌ Không gửi được reply: {last_detail[:120]}")
        return False

    def post_comment(self, text: str, reply_cid: str = "",
                     root_text: str = "") -> bool:
        """Đăng reply UI; phân biệt API accepted, self-visible và public."""
        d = self.driver
        if not d or not (text or "").strip():
            return False
        self.last_comment_submission = {
            "accepted": False,
            "comment_id": "",
            "root_cid": reply_cid,
            "self_visible": False,
            "public_visible": None,
            "detail": "chưa gửi",
        }
        try:
            if not self.open_comment_panel():
                self.log("⚠️ UI comment: không mở được panel.")
                return False
            for closer in d.find_elements(
                    By.CSS_SELECTOR,
                    "[class*='DivReplyCommentEditorContainer'] svg"):
                try:
                    if closer.is_displayed():
                        self._js_click(closer)
                except Exception:
                    continue

            wrapper = self._find_root_wrapper(root_text)
            if wrapper is None:
                self.log(
                    f"⚠️ UI comment: không tìm thấy comment gốc: "
                    f"{' '.join((root_text or '').split())[:80]!r}.")
                return False

            def _matching_reply_count() -> int:
                wanted = _fold_text(text)
                count = 0
                for el in wrapper.find_elements(
                        By.CSS_SELECTOR, '[data-e2e="comment-level-2"]'):
                    shown = _fold_text(
                        el.get_attribute("textContent") or el.text or "")
                    if shown == wanted:
                        count += 1
                return count

            before_count = _matching_reply_count()
            reply_button = wrapper.find_element(
                By.CSS_SELECTOR, '[data-e2e="comment-reply-1"]')
            self._js_click(reply_button)

            def _visible_input(_driver):
                try:
                    el = wrapper.find_element(
                        By.CSS_SELECTOR,
                        '[data-e2e="comment-input"] [contenteditable="true"]')
                    return el if el.is_displayed() else False
                except Exception:
                    return False

            editor = WebDriverWait(d, 8).until(_visible_input)
            if not self._type_into_editor(editor, text):
                self.log("⚠️ UI comment: không nhập được nội dung vào ô reply.")
                return False
            time.sleep(random.uniform(0.35, 0.7))

            post = WebDriverWait(d, 8).until(
                lambda _driver: next((
                    el for el in wrapper.find_elements(
                        By.CSS_SELECTOR, '[data-e2e="comment-post"]')
                    if el.is_displayed() and not el.get_attribute("disabled")),
                    False))
            try:
                d.execute_script("window.__tt_comment_posts = [];")
            except Exception:
                pass
            self._js_click(post)
            response = self._wait_comment_post_response(timeout=12)
            if response:
                accepted, comment_id, detail = self._comment_post_outcome(response)
                if not accepted:
                    self.last_comment_submission = {
                        "accepted": False,
                        "comment_id": comment_id,
                        "root_cid": reply_cid,
                        "detail": detail,
                    }
                    self.log(f"❌ TikTok từ chối reply: {detail}")
                    return False
                try:
                    WebDriverWait(d, 6).until(
                        lambda _driver: _matching_reply_count() > before_count)
                    self_visible = True
                except TimeoutException:
                    self_visible = False
                self.last_comment_submission = {
                    "accepted": True,
                    "comment_id": comment_id,
                    "root_cid": reply_cid,
                    "self_visible": self_visible,
                    "public_visible": None,
                    "detail": detail,
                }
                cid_note = f" cid={comment_id}" if comment_id else ""
                self.log(
                    f"✅ Đã gửi reply{cid_note} · "
                    f"{'đã thấy trong phiên' if self_visible else 'đang đồng bộ'}.")
                return True

            try:
                WebDriverWait(d, 4).until(
                    lambda _driver: _matching_reply_count() > before_count)
                self.last_comment_submission = {
                    "accepted": None,
                    "comment_id": "",
                    "root_cid": reply_cid,
                    "self_visible": True,
                    "public_visible": None,
                    "detail": "không bắt được response publish",
                }
                self.log("⚠️ Đã thấy reply mới nhưng chưa lấy được mã.")
                return True
            except TimeoutException:
                remaining = " ".join(
                    (editor.get_attribute("innerText") or editor.text or "").split())
                state = "EDITOR_CLEARED" if not remaining else "EDITOR_NOT_CLEARED"
                self.last_comment_submission.update({
                    "detail": f"không bắt được response publish; {state}",
                })
                self.log(
                    f"❌ COMMENT_UNCONFIRMED · {state} · không tính thành công.")
                return False
        except Exception as e:
            self.log(f"⚠️ UI comment fail: {type(e).__name__}: {str(e)[:120]}")
            return False

    def auto_reply_price_asks(self, code: str, replies_min: int = 3,
                             replies_max: int = 4, extra_phrases=None,
                             templates=None, numbers: str = "",
                             bio_username: str = "",
                             diagnostic_probe: bool = False) -> int:
        """Quét hỏi giá rồi gửi code và bio number bằng session hiện tại."""
        self.last_vip_comments = []
        self.last_vip_threads = []
        self.last_comment_blocked = False
        self.last_comment_rate_limited = False
        self.last_comment_probe = None
        own_username = self.get_username() or ""
        display_codes = self.display_shop_codes(code)
        requested_numbers = list(dict.fromkeys(
            re.findall(r"\d+", str(numbers or ""))))
        try:
            self.open_comment_panel()
        except Exception:
            pass
        asks = self.harvest_price_ask_comments(extra_phrases)
        self.log(f"🔎 Acc này thấy {len(asks)} comment hỏi giá trên DOM.")
        aweme_id = extract_aweme_id(
            (self.driver.current_url if self.driver else "") or "") or ""
        if aweme_id:
            grouped = {}
            for item in asks:
                username = _norm_handle(item.get("username") or "")
                if username:
                    grouped.setdefault(username, []).append({
                        "cid": "",
                        "text": item.get("text") or "",
                        "username": username,
                        "position": int(item.get("position") or 0),
                        "scroll_top": int(item.get("scroll_top") or 0),
                    })
            for username, comments in grouped.items():
                key = (str(aweme_id), username)
                with _comment_cache_lock(key):
                    _COMMENT_CACHE[key] = {
                        "ts": time.time(),
                        "comments": comments,
                        "rounds": 1,
                        "added": len(comments),
                    }
            if grouped:
                self.log(
                    f"📦 Cache vị trí + username của {sum(map(len, grouped.values()))} "
                    f"comment thuộc {len(grouped)} user để chạy auto tim.")
        if not asks:
            self.log("ℹ️ VIP: không thấy comment hỏi giá.")
            return 0
        n = 0
        consecutive_failures = 0
        abort_commenting = False
        seen_codes = set()
        probe_pending = bool(diagnostic_probe)
        for c in asks:
            cid = str(c.get("cid") or "")
            harvested, own_codes, own_numbers = self._thread_reply_info(
                cid, c.get("replies") or [], own_username)
            fresh = [x for x in harvested if x not in seen_codes]
            seen_codes.update(fresh)
            if fresh:
                self.log("👀 Mã người khác (tham khảo, KHÔNG dùng đăng): "
                         + ", ".join(fresh))
            missing_codes = [
                value for value in display_codes
                if self.normalize_shop_code(value) not in own_codes
            ]
            missing_numbers = [
                value for value in requested_numbers
                if value not in own_numbers
            ]
            if not missing_codes and not missing_numbers:
                self.log(
                    f"ℹ️ Chính @{own_username.lstrip('@')} đã reply đủ "
                    "code/number trong thread này — bỏ qua.")
                continue
            code_input = ", ".join(missing_codes)
            number_input = ", ".join(missing_numbers)
            lo = max(1, int(replies_min or 3))
            hi = max(lo, int(replies_max or lo))
            kinds = []
            if missing_codes:
                kinds.append("code")
            if missing_numbers and bio_username:
                kinds.append("number")
            target = max(random.randint(lo, hi), len(kinds))
            expected_total = target + 1
            preview = _short_text(c.get("text") or "", 36)
            posted_here = []
            for sequence_index in range(expected_total):
                if sequence_index == 0:
                    body = random.choice(self._VIP_COMMENT_REACTIONS)
                else:
                    reply_index = sequence_index - 1
                    kind = (
                        kinds[reply_index]
                        if reply_index < len(kinds) else "")
                    body = self.format_vip_reply(
                        code_input, number_input, bio_username,
                        templates=templates, kind=kind)
                if not body:
                    self.log("⚠️ Không có mẫu phù hợp cho code/number đã nhập.")
                    continue
                ok = self.post_comment_signed(
                    body, reply_cid=cid, aweme_id=aweme_id)
                if ok:
                    consecutive_failures = 0
                    n += 1
                    posted_here.append(body)
                    self.last_vip_comments.append(body)
                else:
                    consecutive_failures += 1
                    if self.last_comment_rate_limited:
                        self.last_comment_blocked = True
                        abort_commenting = True
                        self.log(
                            "⛔ TikTok báo bình luận quá nhanh; dừng toàn bộ "
                            "account trong lượt này để account/IP nghỉ.")
                        break
                    if consecutive_failures >= 2:
                        self.last_comment_blocked = True
                        abort_commenting = True
                        self.log(
                            "⛔ TikTok không nhận hoặc không xác nhận 2 reply "
                            "liên tiếp; dừng toàn bộ account để tránh gửi lặp.")
                        break
                # Nhịp cũ ~1,2 giây tạo burst rõ ràng. Giãn request đã nhận như
                # thao tác người; lỗi transport chỉ chờ ngắn để thử lần kế.
                self._rsleep(8.0 if ok else 3.5, spread=0.35)
            if probe_pending and not abort_commenting:
                probe_text = "Đồ đẹp quá"
                self.log(
                    f"🧪 CONTROL_PROBE: đăng thêm {probe_text!r} để phân biệt "
                    "lọc nội dung với shadow account/IP.")
                probe_ok = self.post_comment_signed(
                    probe_text, reply_cid=cid, aweme_id=aweme_id)
                self.last_comment_probe = {
                    "text": probe_text,
                    "ok": bool(probe_ok),
                    "root": c.get("text") or "",
                    "submission": dict(
                        getattr(self, "last_comment_submission", None) or {}),
                }
                self.log(
                    "🧪 CONTROL_PROBE "
                    + ("API ĐÃ NHẬN; chưa xác minh hiển thị công khai."
                       if probe_ok else "THẤT BẠI; account/IP hoặc luồng đăng đang bị chặn."))
                probe_pending = False
                self._rsleep(8.0, spread=0.35)
            if posted_here:
                self.last_vip_threads.append({
                    "root": c.get("text") or "",
                    "position": c.get("position") or 0,
                    "scroll_top": c.get("scroll_top") or 0,
                    "comments": posted_here,
                })
                self.log(
                    f"💬 API nhận {len(posted_here)}/{expected_total} reply "
                    f"(1 icon + {target} tin nhắn) vào comment gốc: "
                    f"{preview} · public chưa xác minh.")
            if abort_commenting:
                return n
        return n



    def close(self):
        # Chạy cả khi driver chưa kịp gán: profile riêng vẫn nhận diện được
        # Chrome bị tách khỏi chromedriver trong lúc khởi động.
        self._hard_kill()
        if self.driver:
            # uc 3.5.5 trên Windows: driver.quit() chặn main thread đợi Chrome đóng,
            # đôi khi vướng "OSError: WinError 6 handle invalid" → UI treo ở trạng
            # thái "đang dừng". Kill process chromedriver + chrome CON trực tiếp cho
            # nhanh: session invalid ngay → các d.get/find_elements đang block ném
            # exception → worker thoát → UI reset được.
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None
        self._release_browser_grid_slot()
        # Dong local proxy relay neu co
        relay = getattr(self, "_proxy_relay", None)
        if relay:
            try:
                relay.close()
            except Exception:
                pass
            self._proxy_relay = None
        # Dọn profile tạm
        profile = getattr(self, "_profile_dir", None)
        if profile:
            try:
                import shutil
                shutil.rmtree(profile, ignore_errors=True)
            except Exception:
                pass
            self._profile_dir = None

    def _hard_kill(self):
        """Kill đúng cây driver và mọi Chrome/Edge dùng profile riêng của bot."""
        try:
            proc = getattr(
                getattr(getattr(self, "driver", None), "service", None),
                "process", None)
            pid = getattr(proc, "pid", None)
            if pid:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                    creationflags=_CREATE_NO_WINDOW)
        except Exception:
            pass

        # undetected-chromedriver có thể khởi động Chrome tách khỏi cây
        # chromedriver; taskkill /T khi đó không chạm tới cửa sổ còn sống.
        # Profile được tạo riêng cho từng bot là ownership marker an toàn:
        # chỉ kill chrome.exe/msedge.exe có đúng --user-data-dir này.
        profile = str(getattr(self, "_profile_dir", "") or "")
        if os.name != "nt" or not profile:
            return
        try:
            env = os.environ.copy()
            env["ALC_PROFILE_DIR"] = os.path.abspath(profile)
            command = (
                "$needle=$env:ALC_PROFILE_DIR; "
                "Get-CimInstance Win32_Process | "
                "Where-Object { "
                "($_.Name -eq 'chrome.exe' -or $_.Name -eq 'msedge.exe') "
                "-and $_.CommandLine "
                "-and $_.CommandLine.Contains($needle) } | "
                "ForEach-Object { "
                "Stop-Process -Id $_.ProcessId -Force "
                "-ErrorAction SilentlyContinue }"
            )
            subprocess.run(
                ["powershell.exe", "-NoLogo", "-NoProfile",
                 "-NonInteractive", "-Command", command],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, env=env,
                creationflags=_CREATE_NO_WINDOW)
        except Exception:
            pass


    def find_target_comments(self, target_text="", find_mode="all",
                               max_pages=8):
        d = self.driver
        if d is None or self.stop_event.is_set():
            return []
        try:
            cur_url = str(d.current_url or "")
        except Exception:
            return []
        aweme_id = extract_aweme_id(cur_url)
        if not aweme_id:
            return []
        uname = _norm_handle(target_text) if target_text else ""
        if uname:
            return self.find_all_comments_by_user_api(
                aweme_id, uname,
                stop_at_first=(find_mode == "first"))
        found = []
        seen = set()
        cursor = 0
        tried = set()
        for _ in range(max_pages):
            if self.stop_event.is_set():
                break
            if cursor in tried:
                break
            tried.add(cursor)
            data = self._tiktok_signed_get(
                "/api/comment/list/",
                {"aid": 1988, "aweme_id": str(aweme_id),
                 "count": 20, "cursor": cursor},
            )
            if not isinstance(data, dict):
                break
            for c in (data.get("comments") or []):
                cid = str(c.get("cid") or "")
                if not cid or cid in seen:
                    continue
                seen.add(cid)
                found.append({
                    "cid": cid,
                    "parent_cid": "",
                    "text": (c.get("text") or "").strip(),
                })
                for r in (c.get("reply_comment") or []):
                    rcid = str(r.get("cid") or "")
                    if rcid and rcid not in seen:
                        seen.add(rcid)
                        found.append({
                            "cid": rcid,
                            "parent_cid": cid,
                            "text": (r.get("text") or "").strip(),
                        })
            if not data.get("has_more"):
                break
            next_cursor = data.get("cursor")
            if next_cursor is None:
                break
            cursor = int(next_cursor)
        if found:
            self.log(f"[scout] Tìm thấy {len(found)} CID trên video {aweme_id}.")
        return found


# --------------------------------------------------------------------------- #
# GUI
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Import Dialog — Auto-login bằng id|pass|2fa, lấy cookie
# --------------------------------------------------------------------------- #
class ImportDialog(tk.Toplevel):
    def __init__(self, parent: "App"):
        super().__init__(parent)
        self.parent = parent
        self.title("Import Acc → Cookie")
        self.geometry("640x520")
        self.resizable(True, True)
        self._captcha_lock = threading.Lock()
        # Điều khiển job login:
        #   _stop_event  set  -> dừng hẳn (không nhận acc mới + ngắt acc đang chạy)
        #   _pause_event set  -> ĐANG CHẠY; clear -> TẠM DỪNG (dùng ngược để wait() rẻ)
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()   # mặc định: đang chạy (không pause)
        self._bots = set()        # các bot đang sống — để đóng khi Dừng
        self._bots_lock = threading.Lock()
        self._alive = True        # False khi dialog đã đóng → chặn thao tác widget chết
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self):
        """Đóng dialog → dừng job + đóng browser đang chạy để tránh mồ côi."""
        self._alive = False       # chặn mọi callback after() thao tác widget đã hủy
        self._stop_event.set()
        self._pause_event.set()
        with self._bots_lock:
            for bot in list(self._bots):
                try:
                    bot.stop_event.set()
                    bot.close()
                except Exception:
                    pass
        self.destroy()

    def _build_ui(self):
        pad = {"padx": 10, "pady": 4}

        ttk.Label(self, text="Danh sách acc — mỗi dòng: username|pass|2fa  hoặc  username|pass|2fa|proxy:") \
            .pack(anchor="w", **pad)
        self.acc_txt = scrolledtext.ScrolledText(self, height=10, wrap="none")
        self.acc_txt.pack(fill="both", expand=True, **pad)

        ctrl = ttk.Frame(self)
        ctrl.pack(fill="x", **pad)
        ttk.Label(ctrl, text="Số luồng:").pack(side="left")
        self.threads_var = tk.IntVar(value=4)
        ttk.Spinbox(ctrl, from_=1, to=16, width=5,
                    textvariable=self.threads_var).pack(side="left", padx=4)
        self.headless_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(ctrl, text="Headless", variable=self.headless_var).pack(side="left", padx=6)
        # Mobile emulation — đồng bộ với cửa sổ chính (check live / chạy). Bản web
        # mobile của TikTok có DOM ổn định hơn, login qua mobile đôi khi dễ hơn.
        self.mobile_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(ctrl, text="Mobile", variable=self.mobile_var).pack(side="left", padx=6)
        # Tự giải CAPTCHA qua LLM vision (kimi/gpt/... — OpenAI-compatible). Cần có
        # key + api ở cửa sổ chính. Bỏ check → gặp CAPTCHA thì chờ giải thủ công.
        self.autocap_var = tk.BooleanVar(value=has_captcha_key())
        ttk.Checkbutton(ctrl, text="Tự giải CAPTCHA (LLM)",
                        variable=self.autocap_var).pack(side="left", padx=6)
        self.start_btn = ttk.Button(ctrl, text="▶ Bắt đầu", command=self.start)
        self.start_btn.pack(side="right")

        # ── Thanh log + nút điều khiển Pause/Stop ────────────────────────────
        log_head = ttk.Frame(self)
        log_head.pack(fill="x", **pad)
        ttk.Label(log_head, text="Log:").pack(side="left")
        self.stop_btn = ttk.Button(log_head, text="■ Dừng", command=self.stop_job,
                                   state="disabled")
        self.stop_btn.pack(side="right")
        self.pause_btn = ttk.Button(log_head, text="⏸ Tạm dừng", command=self.toggle_pause,
                                    state="disabled")
        self.pause_btn.pack(side="right", padx=(0, 4))

        self.log_area = scrolledtext.ScrolledText(self, height=7, wrap="word", state="disabled")
        self.log_area.pack(fill="x", **pad)

    def log(self, msg):
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        # Ghi vào file log của App chính
        try:
            self.parent._log_file.write(line + "\n")
        except Exception:
            pass
        def _do():
            if not self._alive:          # dialog đã đóng → widget đã hủy
                return
            try:
                self.log_area.configure(state="normal")
                self.log_area.insert("end", line + "\n")
                self.log_area.see("end")
                self.log_area.configure(state="disabled")
            except tk.TclError:
                pass                     # widget bị hủy giữa chừng — bỏ qua
        if not self._alive:
            return
        try:
            self.after(0, _do)
        except (tk.TclError, RuntimeError):
            pass

    def test_proxy(self):
        proxies = [p.strip() for p in self.proxy_txt.get("1.0", "end").splitlines() if p.strip()]
        if not proxies:
            messagebox.showwarning("Trống", "Chưa nhập proxy nào.", parent=self)
            return
        threading.Thread(target=self._test_proxy_job, args=(proxies,), daemon=True).start()

    def _test_proxy_job(self, proxies):
        scheme = self.parent._proxy_scheme()
        for raw in proxies:
            proxy = parse_proxy(raw, scheme)
            if not proxy:
                self.log(f"[proxy] \u274c Parse th\u1ea5t b\u1ea1i: {raw}")
                continue
            host = proxy['host']
            port = proxy['port']
            user = proxy.get('user', '')
            pw = proxy.get('password', '')
            ps = proxy.get('scheme', 'http')
            self.log(f"[proxy] Ki\u1ec3m tra {ps}://{host}:{port}...")
            try:
                if user and pw:
                    proxy_url = f"{ps}://{user}:{pw}@{host}:{port}"
                else:
                    proxy_url = f"{ps}://{host}:{port}"
                proxies_dict = {"http": proxy_url, "https": proxy_url}
                r = requests.get("https://api.ipify.org?format=json",
                                 proxies=proxies_dict, timeout=10)
                try:
                    ip = r.json().get("ip", r.text.strip())
                except Exception:
                    ip = r.text.strip()
                self.log(f"[proxy] \u2705 OK \u2014 IP th\u1ef1c t\u1ebf: {ip}")
            except Exception as e:
                self.log(f"[proxy] \u274c L\u1ed7i: {e}")

    def _parse_list(self):
        """
        Format: username|pass|2fa|email|passemail|date...
        Chỉ quan tâm: username (0), pass (1), 2fa (2), email (3).
        Proxy lấy từ ô Proxy ở cửa sổ chính (round-robin).
        """
        entries = []
        for i, line in enumerate(self.acc_txt.get("1.0", "end").splitlines()):
            line = line.strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 2 and parts[0] and parts[1]:
                uid   = parts[0]
                pw    = parts[1]
                fa    = parts[2] if len(parts) > 2 else ""
                email = parts[3] if len(parts) > 3 else ""
                proxy = self.parent._get_proxy(i)
                # Giữ dòng gốc (raw) để log ra list acc thất bại cho việc retry
                entries.append((uid, pw, fa, email, proxy, line))
        return entries

    def start(self):
        entries = self._parse_list()
        if not entries:
            messagebox.showwarning("Trống", "Vui lòng nhập danh sách acc.", parent=self)
            return
        # Reset trạng thái điều khiển cho phiên mới
        self._stop_event.clear()
        self._pause_event.set()
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.pause_btn.configure(state="normal", text="⏸ Tạm dừng")
        n = self.threads_var.get()
        threading.Thread(target=self._job, args=(entries, n), daemon=True).start()

    # ── Điều khiển Stop / Pause ───────────────────────────────────────────
    def stop_job(self):
        """Dừng hẳn: không nhận acc mới, đóng mọi browser đang chạy."""
        self._stop_event.set()
        self._pause_event.set()   # bỏ pause để các luồng đang chờ thoát ra
        self.log("■ Đang dừng — đóng các phiên đang chạy...")
        # Đóng bot đang sống để cắt WebDriverWait/sleep ngay lập tức
        with self._bots_lock:
            for bot in list(self._bots):
                try:
                    bot.stop_event.set()
                    bot.close()
                except Exception:
                    pass
        self.stop_btn.configure(state="disabled")
        self.pause_btn.configure(state="disabled")

    def toggle_pause(self):
        """Tạm dừng / tiếp tục. Pause chỉ chặn acc MỚI, acc đang chạy vẫn xong."""
        if self._pause_event.is_set():
            # đang chạy -> chuyển sang pause
            self._pause_event.clear()
            self.pause_btn.configure(text="▶ Tiếp tục")
            self.log("⏸ Đã tạm dừng — acc đang chạy sẽ hoàn tất, không mở acc mới.")
        else:
            # đang pause -> tiếp tục
            self._pause_event.set()
            self.pause_btn.configure(text="⏸ Tạm dừng")
            self.log("▶ Tiếp tục.")

    def _wait_if_paused(self):
        """Chặn tại đây khi đang pause; trả False nếu bị stop trong lúc chờ."""
        while not self._pause_event.wait(timeout=0.3):
            if self._stop_event.is_set():
                return False
        return not self._stop_event.is_set()

    # ── CAPTCHA pause (serialize vì nhiều luồng cùng gặp) ─────────────────
    # ── CAPTCHA: tự giải qua LLM, fallback thủ công ────────────────────────
    def _try_auto_captcha(self, uid, driver) -> bool:
        """Cố giải CAPTCHA tự động qua LLM vision. Trả True nếu đã gọi solver xong
        (caller chờ trang xử lý rồi kiểm tra lại). Trả False khi tắt tính năng, thiếu
        key/thư viện, hoặc solver ném lỗi → caller fallback giải thủ công.

        Serialize qua _captcha_lock vì solver dùng ActionChains chuột (race sẽ hỏng).
        Retry vài lần: LLM đôi khi trả JSON lệch hoặc giải sai, TikTok vẫn giữ
        captcha → thử lại.
        """
        if not self.autocap_var.get():
            return False
        if not has_captcha_key():
            self.log(f"[{uid}] ⚠️ Không có key CAPTCHA → không tự giải được.")
            return False
        with self._captcha_lock:
            for attempt in range(3):
                try:
                    solver = make_captcha_solver(driver, log_fn=self.log)
                    if solver is None:
                        self.log(f"[{uid}] ⚠️ Chưa cài tiktok-captcha-solver → thủ công.")
                        return False
                    self.log(f"[{uid}] 🤖 Tự giải CAPTCHA qua {load_cap_provider().upper()} (lần {attempt + 1})...")
                    solver.solve_captcha_if_present(captcha_detect_timeout=10, retries=1)
                    # Nếu captcha biến mất → giải đúng, thoát luôn.
                    if not captcha_present(driver):
                        self.log(f"[{uid}] ✅ LLM đã giải xong CAPTCHA.")
                        return True
                    self.log(f"[{uid}] ⚠️ CAPTCHA vẫn còn — LLM giải sai? Thử lại...")
                    time.sleep(2)
                except Exception as e:
                    self.log(f"[{uid}] ⚠️ Tự giải Lỗi ({str(e)[:120]}) → thử lại...")
                    time.sleep(2)
            self.log(f"[{uid}] ⚠️ LLM giải không xong sau 3 lần → thủ công.")
            return False

    def _wait_captcha(self, uid):
        with self._captcha_lock:
            if not self._alive:
                return
            ev = threading.Event()
            try:
                self.after(0, lambda: (
                    messagebox.showinfo(
                        "CAPTCHA", f"[{uid}] Giải CAPTCHA xong thì bấm OK.", parent=self),
                    ev.set()
                ))
            except (tk.TclError, RuntimeError):
                return
            ev.wait(timeout=300)

    # ── 2FA code ──────────────────────────────────────────────────────────
    def _get_2fa_code(self, uid: str, secret: str) -> str:
        if not secret:
            return ""
        secret = secret.strip().upper().replace(" ", "")
        if len(secret) == 6 and secret.isdigit():
            return secret
        try:
            import pyotp
            # Base32 cần padding đúng (bội số 8)
            padding = (8 - len(secret) % 8) % 8
            padded = secret + "=" * padding
            code = pyotp.TOTP(padded).now()
            self.log(f"[{uid}] TOTP → {code}")
            return code
        except Exception as e:
            self.log(f"[{uid}] ❌ pyotp lỗi: {e} | secret={secret[:8]}...")
            return ""

    _2FA_INPUT_SEL = (
        "input[autocomplete='one-time-code'], "
        "input[placeholder*='6'], input[placeholder*='chữ số'], "
        "input[placeholder*='digit' i], input[inputmode='numeric'], "
        "input[type='tel'], input[maxlength='1'], input[maxlength='6'], "
        "[class*='DivCodeInputContainer'] input, "
        "#loginContainer input, [class*='DivLoginContainer'] input, "
        "[class*='CodeInput'] input, [name*='code' i], [id*='otp' i]"
    )

    def _fresh_2fa_input(self, d):
        """Re-tìm ô 2FA tươi để tránh stale element (React render lại ô sau mỗi lần nhập)."""
        try:
            els = d.find_elements(By.CSS_SELECTOR, self._2FA_INPUT_SEL)
            for el in els:
                try:
                    if el.is_displayed() or el.get_attribute("type") == "tel":
                        return el
                except Exception:
                    continue
            return els[0] if els else None
        except Exception:
            return None

    def _enter_2fa_code(self, d, inp_el, code: str, uid: str):
        """Nhập mã 2FA vào ô OTP 6-chữ của TikTok rồi submit.

        Ô 2FA TikTok là 1 <input type='tel'> do React controlled (state quản lý).
        Gán el.value trực tiếp KHÔNG trigger React onChange (React override value
        setter) → state rỗng → nút 'Tiếp' vẫn disabled. Phải dùng native setter của
        HTMLInputElement.prototype rồi dispatch event 'input' thì React mới nhận:
          1) re-tìm ô tươi (chống stale element — React render lại sau submit);
          2) set value qua native setter, dispatch 'input'/'change' (bubbles) → React onChange;
          3) nếu value vẫn rỗng/sai → fallback gõ từng số + send_keys;
          4) submit, rồi chờ kết quả (login xong / lỗi / ô biến mất) trước khi trả.
        """
        code = str(code).strip()

        def _native_set(el, full: str) -> bool:
            """Set OTP: native setter + CDP từng số (ô tel 6-slot)."""
            ok = d.execute_script(
                "var el=arguments[0],val=arguments[1];"
                "el.focus(); el.click();"
                "var set=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;"
                "set.call(el,'');"
                "el.dispatchEvent(new Event('input',{bubbles:true}));"
                "set.call(el,val);"
                "el.dispatchEvent(new InputEvent('input',{bubbles:true,data:val,inputType:'insertText'}));"
                "el.dispatchEvent(new Event('change',{bubbles:true}));"
                "return el.value===val;",
                el, full)
            if ok:
                return True
            try:
                d.execute_cdp_cmd("Input.insertText", {"text": full})
            except Exception:
                pass
            return (el.get_attribute("value") or "") == full

        el = self._fresh_2fa_input(d) or inp_el
        try:
            if el and _native_set(el, code):
                self.log(f"[{uid}] ✅ Đã set mã 2FA qua native setter.")
                if self._submit_2fa(d, uid):
                    return True
        except Exception as e:
            self.log(f"[{uid}] ⚠️ Native setter lỗi: {str(e)[:80]}")

        # 2) Fallback: gõ từng số qua JS keydown/input/keyup (giống bấm phím).
        el = self._fresh_2fa_input(d) or el
        try:
            d.execute_script("arguments[0].focus(); arguments[0].click();", el)
        except Exception:
            pass
        for ch in code:
            try:
                d.execute_script(
                    "var el=arguments[0],v=arguments[1];"
                    "el.dispatchEvent(new KeyboardEvent('keydown',{key:v,code:'Digit'+v,bubbles:true}));"
                    "el.dispatchEvent(new InputEvent('input',{bubbles:true,inputType:'insertText',data:v}));"
                    "el.dispatchEvent(new KeyboardEvent('keyup',{key:v,code:'Digit'+v,bubbles:true}));",
                    el, ch)
                time.sleep(random.uniform(0.05, 0.18))
            except Exception:
                pass
        if self._submit_2fa(d, uid):
            return True

        # 3) Fallback: send_keys thuần (Selenium keystroke — bypass JS event).
        self.log(f"[{uid}] ⚠️ JS không nhận mã — thử send_keys.")
        el = self._fresh_2fa_input(d) or el
        try:
            el.clear()
            el.send_keys(code)
            time.sleep(0.5)
            if self._submit_2fa(d, uid):
                return True
        except Exception:
            pass
        return False

    def _submit_2fa(self, d, uid: str) -> bool:
        """Chờ nút Tiếp (StyledTwoStepButton) hết disabled rồi click."""
        try:
            try:
                for c in d.find_elements(By.CSS_SELECTOR,
                        "[data-e2e='instagram-modal-close'], [class*='IClose']"):
                    if c.is_displayed():
                        d.execute_script("arguments[0].click();", c)
            except Exception:
                pass
            WebDriverWait(d, 8).until(lambda dr: dr.execute_script("""
                var inputs=[...document.querySelectorAll('input')].filter(el=>{
                  var ty=(el.type||'').toLowerCase();
                  return ty!=='password'&&ty!=='hidden'&&ty!=='checkbox';
                });
                var digits='';
                for (const el of inputs) digits += (el.value||'').replace(/\\D/g,'');
                var b=document.querySelector("button[class*='StyledTwoStepButton'], button[type='submit'], #loginContainer button, form button");
                if(!b) return false;
                if(digits.replace(/\\D/g,'').length>=6){
                  b.disabled=false; b.removeAttribute('disabled'); return true;
                }
                return !b.disabled;
            """))
            d.execute_script("""
                var b=document.querySelector("button[class*='StyledTwoStepButton'], button[type='submit']");
                if(b){ b.disabled=false; b.removeAttribute('disabled'); b.click(); }
            """)
            return True
        except Exception:
            return False

    _RATE_LIMIT_TEXT = "quá thường xuyên"

    @staticmethod
    def _visible_text_contains(d, needle: str) -> bool:
        """Needle có trong text VISIBLE trên trang."""
        try:
            # XPath: text() visible — is_displayed() lọc element bị ẩn.
            els = d.find_elements(By.XPATH,
                f"//*[contains(text(),{json.dumps(needle, ensure_ascii=False)})]")
            return any(e.is_displayed() for e in els)
        except Exception:
            return False

    @staticmethod
    def _is_logged_in(d) -> bool:
        """Phát hiện đăng nhập thành công qua nhiều dấu hiệu (bền hơn 1 selector).

        Selector UI của TikTok đổi liên tục, nên chỉ dựa vào profile-icon/nav-upload
        dễ bị miss → vòng lặp login treo hết timeout. Ở đây kết hợp:
          1) cookie sessionid  (đáng tin nhất — có nghĩa server đã cấp session)
          2) selector UI phổ biến (profile-icon / nav-upload / avatar)
          3) đã rời khỏi trang /login (không còn ô password)
        """
        # 1) Cookie sessionid — dấu hiệu chắc chắn nhất
        try:
            for c in d.get_cookies():
                if c.get("name") == "sessionid" and c.get("value"):
                    return True
        except Exception:
            pass
        # 2) Selector UI
        try:
            if d.find_elements(By.CSS_SELECTOR,
                    "[data-e2e='profile-icon'], [data-e2e='nav-upload'], "
                    "[data-e2e='nav-profile'], [class*='DivHeaderRightContainer'] img"):
                return True
        except Exception:
            pass
        # 3) Đã rời trang login (không còn ô password) VÀ không phải trang verify/2fa
        try:
            url = d.current_url
            if "login" not in url and "verify" not in url and "2fa" not in url:
                if not d.find_elements(By.CSS_SELECTOR, "input[type='password']"):
                    # Chỉ tính khi đang ở domain tiktok (tránh trang trống about:blank)
                    if "tiktok.com" in url:
                        return True
        except Exception:
            pass
        return False

    # ── Login 1 tài khoản ─────────────────────────────────────────────────
    def _login_one(self, uid: str, pw: str, fa: str, email: str = "",
                   proxy: dict = None, session_idx: int = 0) -> str:
        """Trả về 'ok' | 'rate_limit' | 'fail' | 'stopped'."""
        # Chờ nếu đang pause; nếu bị stop thì bỏ qua acc này
        if not self._wait_if_paused():
            return "stopped"

        # Dùng stop_event CHUNG của dialog để nút Dừng cắt được vòng lặp login
        bot = TikTokLikeBot(self.log, self._stop_event)
        with self._bots_lock:
            self._bots.add(bot)
        try:
            bot.start_browser(headless=self.headless_var.get(),
                              session_idx=session_idx, proxy=proxy,
                              mobile=self.mobile_var.get())
            d = bot.driver

            # Xác minh proxy TRƯỚC khi login — nếu không dùng proxy sẽ lộ IP gốc.
            # Cảnh báo ngay để người dùng biết session này không được proxy che.
            if proxy:
                exit_ip = bot.verify_proxy_ip()
                if exit_ip:
                    self.log(f"[{uid}] 🌐 IP thoát: {exit_ip} (proxy {proxy['host']}:{proxy['port']})")
                else:
                    self.log(f"[{uid}] ⚠️ Không xác minh được IP proxy — có thể đang dùng IP gốc!")
            else:
                self.log(f"[{uid}] ⚠️ KHÔNG có proxy — dùng IP gốc của máy.")

            def _type_when_ready(selector: str, text: str, timeout: int, field: str) -> bool:
                try:
                    el = WebDriverWait(d, timeout).until(
                        lambda dr: next((e for e in dr.find_elements(By.CSS_SELECTOR, selector)
                                         if e.is_displayed()), None))
                except TimeoutException:
                    self.log(f"[{uid}] ⚠️ Không tìm thấy ô {field} sau {timeout}s")
                    return False
                if el is None:
                    self.log(f"[{uid}] ⚠️ Không tìm thấy ô {field} sau {timeout}s")
                    return False
                js = r"""
                const el = arguments[0], val = arguments[1];
                const proto = window.HTMLInputElement.prototype;
                const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                el.focus();
                setter.call(el, '');
                el.dispatchEvent(new Event('input', {bubbles:true}));
                setter.call(el, val);
                el.dispatchEvent(new InputEvent('input', {bubbles:true, data: val, inputType:'insertText'}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
                return el.value;
                """
                for _ in range(4):
                    try:
                        if d.execute_script(js, el, text) == text:
                            return True
                        el.send_keys(text)
                        if (el.get_attribute("value") or "") == text:
                            return True
                        el = next((e for e in d.find_elements(By.CSS_SELECTOR, selector)
                                   if e.is_displayed()), el)
                    except WebDriverException:
                        pass
                    time.sleep(0.25)
                self.log(f"[{uid}] ⚠️ Nhập ô {field} không ổn định (web load chậm?)")
                return False

            def _do_login(login_id: str):
                """Nhập login_id + pw rồi submit. Trả True nếu submit thành công."""
                d.get("https://www.tiktok.com/login/phone-or-email/email")
                try:
                    WebDriverWait(d, 20).until(
                        lambda dr: dr.execute_script("return document.readyState") == "complete"
                    )
                except TimeoutException:
                    self.log(f"[{uid}] ⚠️ Trang login load chậm (>20s), vẫn thử tiếp...")

                uid_sel = (
                    "input[name='username'], input[placeholder*='Email' i], "
                    "input[placeholder*='TikTok ID' i], "
                    "input[placeholder*='email' i], "
                    "input[autocomplete='webauthn'], input[autocomplete='username']"
                )
                if not _type_when_ready(uid_sel, login_id, 15, "username"):
                    return False
                if not _type_when_ready(
                        "input[type='password'], input[name='password'], "
                        "input[placeholder*='Mật khẩu' i], "
                        "input[placeholder*='Password' i]",
                        pw, 8, "password"):
                    return False
                try:
                    WebDriverWait(d, 6).until(lambda dr: dr.execute_script("""
                        var u=document.querySelector("input[name='username']");
                        var p=document.querySelector("input[type='password']");
                        var b=document.querySelector("[data-e2e='login-button'],button[type='submit']");
                        if(!b) return false;
                        if(u&&p&&u.value&&p.value){ b.disabled=false; b.removeAttribute('disabled'); return true; }
                        return !b.disabled;
                    """))
                    d.execute_script("""
                        var b=document.querySelector("[data-e2e='login-button'],button[type='submit']");
                        if(b){ b.disabled=false; b.removeAttribute('disabled'); b.click(); }
                    """)
                    return True
                except TimeoutException:
                    return False

            def _login_with_retry(login_id: str, label: str) -> bool:
                """Bọc _do_login với retry ở mức flow.

                _do_login đã tự reload trang mỗi lần (d.get) + retry gõ 3 lần bên
                trong, NHƯNG nếu typing/submit vẫn fail do proxy chậm thì chỉ gọi
                1 lần rồi bỏ qua → phiên treo 90s chờ vô vọng. Hàm này thử lại
                tối đa 3 lần ở mức flow trước khi báo fail hẳn.
                """
                for attempt in range(3):
                    if self._stop_event.is_set():
                        return False
                    if _do_login(login_id):
                        return True
                    if attempt < 2:
                        self.log(f"[{uid}] 🔄 {label} chưa ổn định, tải lại + thử lại (lần {attempt + 2}/3)...")
                        time.sleep(2)
                return False

            # Thử username trước (retry khi web load chậm / gõ không ổn định)
            self.log(f"[{uid}] Đăng nhập bằng username...")
            if not _login_with_retry(uid, "Username"):
                self.log(f"[{uid}] ❌ Không nhập được thông tin sau nhiều lần thử.")
                return "fail"

            # Vòng chờ: xử lý CAPTCHA / 2FA / thành công / fallback email
            _2fa_sent = False
            _last_2fa_code = ""
            _captcha_cooldown = 0
            _tried_email = False
            # Đếm số tick ngay sau khi submit 2FA — tránh phát hiện lỗi ngay lập tức
            # khi trang đang navigate (false positive → nhập lại mã → stale element).
            _2fa_grace = 0
            _login_err_sel = (
                "[data-e2e='login-error-msg'], "
                "[class*='LoginError'], [class*='login-error'], "
                "[class*='tiktok'][class*='error']"
            )
            for _ in range(90):
                try:
                    # Người dùng bấm Dừng → thoát ngay
                    if self._stop_event.is_set():
                        return "stopped"
                    time.sleep(1)
                    # Kiểm tra đăng nhập xong — dùng helper đa dấu hiệu để break ngay,
                    # tránh treo hết 90s khi selector UI không khớp.
                    if self._is_logged_in(d):
                        break

                    # Fallback email nếu username bị lỗi
                    if not _tried_email and email and d.find_elements(By.CSS_SELECTOR, _login_err_sel):
                        self.log(f"[{uid}] ❌ Username thất bại → thử email: {email}")
                        _tried_email = True
                        _2fa_sent = False
                        _last_2fa_code = ""
                        _login_with_retry(email, "Email")
                        time.sleep(1)
                        continue

                    # CAPTCHA: thử tự giải qua LLM trước, không được thì thủ công
                    if _captcha_cooldown > 0:
                        _captcha_cooldown -= 1
                    elif captcha_present(d):
                        _2fa_sent = False
                        _last_2fa_code = ""
                        if self._try_auto_captcha(uid, d):
                            _captcha_cooldown = 4   # giải xong, cho trang xử lý
                        else:
                            self.log(f"[{uid}] ⚠️ CAPTCHA — chờ giải thủ công...")
                            self._wait_captcha(uid)
                            _captcha_cooldown = 6
                        continue

                    # 2FA
                    on_2fa_page = (
                        "verify" in d.current_url or "2fa" in d.current_url
                        or not d.find_elements(By.CSS_SELECTOR, "input[type='password']")
                    )
                    twofa = d.find_elements(By.CSS_SELECTOR,
                        "input[placeholder*='6'], "
                        "input[inputmode='numeric'], "
                        "input[type='tel'], "
                        "#loginContainer input[type='text']") if on_2fa_page else []

                    # Phát hiện lỗi sau khi đã gửi mã — CHỊ check khi hết grace period.
                    # Grace period = vài giây đầu sau submit, trang đang navigate, text lỗi
                    # cũ vẫn còn → check sớm sẽ false-positive ("mã sai" ảo) → nhập lại mã
                    # mới lên element đã stale → treo. Phải chờ trang tĩnh lại mới check.
                    if _2fa_sent and twofa:
                        if _2fa_grace > 0:
                            _2fa_grace -= 1
                        else:
                            # Escape hatch: login có thể đã thành công ngay submit
                            # → ô 2FA đã biến mất nhưng twofa[] còn giữ element stale.
                            # Phải check login TRƯỚC khi kết luận lỗi.
                            if self._is_logged_in(d):
                                break
                            err = d.find_elements(By.XPATH,
                                "//*[contains(text(),'không chính xác') or "
                                "contains(text(),'invalid') or contains(text(),'incorrect') or "
                                "contains(text(),'wrong') or contains(text(),'expired')]")
                            # Chỉ tính lỗi khi text VISIBLE (element ẩn cũng match → ảo)
                            err = [e for e in err if e.is_displayed()]
                            if err:
                                # Tạo mã mới — nếu vẫn giống mã cũ thì chờ TOTP window tiếp.
                                # Mã TOTP mới chỉ hợp lệ sau khi server sang window mới — nhập
                                # ngay mã vừa đổi sẽ bị TikTok báo sai (clock skew), nên chờ
                                # thêm vài giây cho server bắt kịp trước khi retry.
                                new_code = self._get_2fa_code(uid, fa)
                                if new_code and new_code != _last_2fa_code:
                                    self.log(f"[{uid}] ⚠️ Mã sai, thử mã mới: {new_code}")
                                    _2fa_sent = False
                                else:
                                    # Cùng window 30s — chờ đến khi code đổi (tối đa 30s).
                                    # PHẢI check login giữa chừng: 2FA có thể đã thành công
                                    # ngay submit đầu, nếu không check sẽ nhập mã mới sai lên
                                    # trang đã logged-in → stale element.
                                    self.log(f"[{uid}] ⏳ Chờ TOTP window mới...")
                                    for _ in range(30):
                                        time.sleep(1)
                                        if self._is_logged_in(d):
                                            break
                                        if self._stop_event.is_set():
                                            break
                                        c = self._get_2fa_code(uid, fa)
                                        if c and c != _last_2fa_code:
                                            self.log(f"[{uid}] Mã mới: {c}")
                                            _2fa_sent = False
                                            break

                    # Rate limit sau 2FA — CHỈ check khi hết grace period (trang đã tĩnh)
                    # VÀ text lỗi phải VISIBLE (page_source chứa cả element ẩn → khớp
                    # text cũ từ lần fail trước → false positive, báo rate-limit trong
                    # khi login đã thành công). Re-check _is_logged_in làm escape hatch.
                    if _2fa_sent and _2fa_grace == 0:
                        if self._is_logged_in(d):
                            break
                        if self._visible_text_contains(d, self._RATE_LIMIT_TEXT):
                            self.log(f"[{uid}] 🚫 Rate limit sau 2FA — bỏ qua.")
                            return "rate_limit"

                    if not _2fa_sent and twofa:
                        code = self._get_2fa_code(uid, fa)
                        if code:
                            self.log(f"[{uid}] Nhập mã 2FA: {code}")
                            self._enter_2fa_code(d, twofa[0], code, uid)
                            _2fa_sent = True
                            _last_2fa_code = code
                            # Grace period: trong ~5s đầu sau submit, KHÔNG phát hiện lỗi
                            # (trang đang navigate → false positive → nhập lại → stale element).
                            _2fa_grace = 5
                        else:
                            self.log(f"[{uid}] ⚠️ Cần nhập 2FA thủ công (60s)...")
                            time.sleep(60)
                            _2fa_sent = True

                except WebDriverException as e:
                    msg = e.msg or ""
                    if any(k in msg for k in ("invalid session id", "no such window",
                                               "disconnected", "not connected")):
                        self.log(f"[{uid}] ⚠️ Browser đóng: {msg.splitlines()[0]}")
                        break
                    # stale element / timeout / v.v. → bỏ qua, tiếp tục vòng lặp

            # Lưu cookie nếu thành công
            if self._is_logged_in(d):
                cookies_raw = json.dumps(d.get_cookies(), ensure_ascii=False)
                # Lưu qua parent (App luôn sống) để cookie vẫn được ghi kể cả khi
                # dialog Import đã đóng giữa chừng.
                self.parent.after(0, lambda u=uid, c=cookies_raw: self._save_cookie(u, c))
                return "ok"
            else:
                self.log(f"[{uid}] ❌ Đăng nhập thất bại.")
                return "fail"

        except Exception as e:
            self.log(f"[{uid}] ❌ Lỗi: {e}")
            return "fail"
        finally:
            with self._bots_lock:
                self._bots.discard(bot)
            bot.close()

    def _save_cookie(self, uid: str, cookie_raw: str):
        accs = self.parent._accounts
        uname = f"@{uid}"
        # Tìm acc đã tồn tại theo username → cập nhật thay vì tạo mới
        existing = next((k for k, v in accs.items() if v.get("username") == uname), None)
        name = existing or self.parent._next_name()
        action = "Cập nhật" if existing else "Lưu mới"
        accs[name] = {"cookie": cookie_raw, "username": uname, "status": "Live"}
        save_accounts(accs)
        self.parent._refresh_tree()
        self.log(f"[{uid}] ✅ {action} cookie ({name}).")

    def _job(self, entries, n_threads):
        total = len(entries)
        ok = fail = rate = stopped = 0
        failed_lines = []   # dòng gốc các acc thất bại/rate-limit → để retry
        lock = threading.Lock()

        def _run(uid, pw, fa, email, proxy, raw, idx):
            nonlocal ok, fail, rate, stopped
            result = self._login_one(uid, pw, fa, email, proxy, idx)
            with lock:
                if result == "ok":
                    ok += 1
                elif result == "rate_limit":
                    rate += 1
                    failed_lines.append(raw)   # rate-limit cũng nên retry sau
                elif result == "stopped":
                    stopped += 1
                    return   # bị dừng: không in progress
                else:
                    fail += 1
                    failed_lines.append(raw)
                done = ok + fail + rate
                self.log(
                    f"[{done}/{total}] ✅ {ok} thành công  "
                    f"❌ {fail} thất bại  🚫 {rate} rate limit"
                )

        with ThreadPoolExecutor(max_workers=n_threads) as ex:
            futures = [ex.submit(_run, uid, pw, fa, email, proxy, raw, idx)
                       for idx, (uid, pw, fa, email, proxy, raw) in enumerate(entries)]
            for f in as_completed(futures):
                try:
                    f.result()
                except Exception as e:
                    self.log(f"❌ Lỗi không mong muốn: {e}")

        if self._stop_event.is_set():
            self.log(
                f"■ Đã dừng — ✅ {ok} thành công  ❌ {fail} thất bại  "
                f"🚫 {rate} rate limit  ⏭ {stopped} bỏ qua"
            )
        else:
            self.log(
                f"━━ Hoàn tất {total} tài khoản — "
                f"✅ {ok} thành công  ❌ {fail} thất bại  🚫 {rate} rate limit ━━"
            )

        # Ghi danh sách acc thất bại ra file để người dùng copy retry
        if failed_lines:
            self._write_failed(failed_lines)

        if self._alive:
            try:
                self.after(0, self._reset_controls)
            except (tk.TclError, RuntimeError):
                pass

    def _write_failed(self, failed_lines):
        """Ghi các dòng acc thất bại ra logs/failed_accounts_<time>.txt + log ra UI."""
        try:
            log_dir = Path(__file__).parent / "logs"
            log_dir.mkdir(exist_ok=True)
            ts = time.strftime("%Y-%m-%d_%H-%M-%S")
            fpath = log_dir / f"failed_accounts_{ts}.txt"
            fpath.write_text("\n".join(failed_lines) + "\n", encoding="utf-8")
            self.log(f"📄 Đã lưu {len(failed_lines)} acc thất bại → {fpath.name}")
            self.log("── DANH SÁCH ACC THẤT BẠI (copy để retry) ──")
            for line in failed_lines:
                self.log(f"   {line}")
            self.log("─────────────────────────────────────────────")
        except Exception as e:
            self.log(f"⚠️ Không ghi được file acc thất bại: {e}")

    def _reset_controls(self):
        """Trả UI về trạng thái sẵn sàng sau khi job kết thúc/dừng."""
        if not self._alive:
            return
        try:
            self.run_btn.configure(state="normal")
            self.run_sel_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
            self.pause_btn.configure(state="disabled", text="⏸ Tạm dừng")
        except tk.TclError:
            pass


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("TikTok Like Tool")
        try:
            sw = int(self.winfo_screenwidth())
            sh = int(self.winfo_screenheight())
        except Exception:
            sw, sh = 1920, 1080
        ww, hh = min(960, sw - 40), min(820, sh - 80)
        x, y = max(0, (sw - ww) // 2), max(0, (sh - hh) // 2)
        self.geometry(f"{ww}x{hh}+{x}+{y}")
        self.minsize(min(860, sw - 40), min(640, sh - 80))
        self.log_queue = queue.Queue()
        self.stop_event = threading.Event()
        # pause_event: set = đang chạy; clear = tạm dừng (dùng ngược để wait() rẻ)
        self.pause_event = threading.Event()
        self.pause_event.set()
        self.worker = None
        self._alive = True   # False khi cửa sổ đóng → chặn callback thao tác widget chết
        self._debug_bots = []   # browser giữ mở ở chế độ Debug — đóng khi thoát App
        self._accounts = load_accounts()
        self._save_lock = threading.Lock()
        self._tree_map: dict[str, str] = {}

        # Log file — tạo mới mỗi session theo timestamp
        log_dir = Path(__file__).parent / "logs"
        log_dir.mkdir(exist_ok=True)
        ts = time.strftime("%Y-%m-%d_%H-%M-%S")
        self._log_file = open(log_dir / f"{ts}.log", "a", encoding="utf-8", buffering=1)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_ui()
        self.log(f"Log file: {self._log_file.name}")
        self.after(100, self._drain_log)

    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}
        frm = ttk.Frame(self)
        frm.pack(fill="both", expand=True, padx=8, pady=6)
        frm.rowconfigure(2, weight=1)
        frm.columnconfigure(0, weight=1)

        # ══════════════════════════════════════════════════════════════════
        # VÙNG 1: Tài khoản
        # ══════════════════════════════════════════════════════════════════
        acct_frame = ttk.LabelFrame(frm, text="Tài khoản")
        acct_frame.grid(row=0, column=0, sticky="ew", **pad)
        acct_frame.columnconfigure(0, weight=1)

        # Bảng
        tree_wrap = ttk.Frame(acct_frame)
        tree_wrap.grid(row=0, column=0, sticky="ew", padx=4, pady=(4, 2))
        tree_wrap.columnconfigure(0, weight=1)

        cols = ("stt", "username", "cookie", "status")
        self.tree = ttk.Treeview(tree_wrap, columns=cols, show="headings", height=6)
        self.tree.heading("stt",      text="#")
        self.tree.heading("username", text="Username")
        self.tree.heading("cookie",   text="Cookie")
        self.tree.heading("status",   text="Status")
        self.tree.column("stt",      width=36,  anchor="center", stretch=False)
        self.tree.column("username", width=160, anchor="w")
        self.tree.column("cookie",   width=340, anchor="w")
        self.tree.column("status",   width=80,  anchor="center", stretch=False)
        vsb = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.grid(row=0, column=0, sticky="ew")
        vsb.grid(row=0, column=1, sticky="ns")

        # Thanh nút (hàng trên) + ô cookie (hàng dưới) — tách 2 hàng để không tràn
        bottom_bar = ttk.Frame(acct_frame)
        bottom_bar.grid(row=1, column=0, sticky="ew", padx=4, pady=(2, 4))
        bottom_bar.columnconfigure(0, weight=1)

        btn_left = ttk.Frame(bottom_bar)
        btn_left.grid(row=0, column=0, sticky="w", pady=(0, 4))
        ttk.Button(btn_left, text="Import Acc",    command=self.open_import_dialog).pack(side="left", padx=2)
        ttk.Button(btn_left, text="Import Cookie", command=self.import_cookies_file).pack(side="left", padx=2)
        ttk.Button(btn_left, text="Check Live",    command=self.check_live_selected).pack(side="left", padx=2)
        ttk.Button(btn_left, text="Check Tất cả", command=self.check_live_all).pack(side="left", padx=2)
        ttk.Separator(btn_left, orient="vertical").pack(side="left", fill="y", padx=6)
        ttk.Button(btn_left, text="Xóa trùng",    command=self.remove_duplicates).pack(side="left", padx=2)
        ttk.Button(btn_left, text="Xóa đã chọn",  command=self.delete_account).pack(side="left", padx=2)

        cookie_bar = ttk.Frame(bottom_bar)
        cookie_bar.grid(row=1, column=0, sticky="ew")
        cookie_bar.columnconfigure(0, weight=1)
        self.cookie_entry = ttk.Entry(cookie_bar)
        self.cookie_entry.insert(0, "Dán cookie vào đây...")
        self.cookie_entry.configure(foreground="gray")
        self.cookie_entry.bind("<FocusIn>",  lambda e: (self.cookie_entry.get() == "Dán cookie vào đây..." and
                               (self.cookie_entry.delete(0, "end"), self.cookie_entry.configure(foreground=""))))
        self.cookie_entry.bind("<FocusOut>", lambda e: (not self.cookie_entry.get() and
                               (self.cookie_entry.insert(0, "Dán cookie vào đây..."), self.cookie_entry.configure(foreground="gray"))))
        self.cookie_entry.grid(row=0, column=0, sticky="ew")
        ttk.Button(cookie_bar, text="Lưu cookie", command=self.save_account).grid(row=0, column=1, padx=(4, 0))

        self._refresh_tree()

        # ══════════════════════════════════════════════════════════════════
        # VÙNG 2: Proxy (trái) | Thao tác (phải)
        # ══════════════════════════════════════════════════════════════════
        mid = ttk.Frame(frm)
        mid.grid(row=1, column=0, sticky="ew", **pad)
        mid.columnconfigure(0, weight=1)
        mid.columnconfigure(1, weight=2)

        # ── Proxy ─────────────────────────────────────────────────────────
        proxy_frame = ttk.LabelFrame(mid, text="Proxy  (ip:port | ip:port:user:pass | socks5://...)")
        proxy_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        proxy_frame.columnconfigure(0, weight=1)

        self.proxy_txt = scrolledtext.ScrolledText(proxy_frame, height=5, wrap="none", width=30)
        self.proxy_txt.insert("1.0", load_proxies())
        self.proxy_txt.grid(row=0, column=0, columnspan=2, sticky="ew", padx=4, pady=(4, 2))
        self.proxy_txt.bind("<<Modified>>", self._on_proxy_modified)

        proxy_btn = ttk.Frame(proxy_frame)
        proxy_btn.grid(row=1, column=0, columnspan=2, sticky="ew", padx=4, pady=(0, 4))
        # Dropdown loại proxy — áp dụng cho dòng KHÔNG có tiền tố (socks5:// / http://)
        ttk.Label(proxy_btn, text="Loại:").pack(side="left")
        self.proxy_type_var = tk.StringVar(value="http")
        ttk.Combobox(proxy_btn, textvariable=self.proxy_type_var, width=8,
                     state="readonly", values=["http", "socks5"]).pack(side="left", padx=(2, 0))
        # 2 nút xuống hàng dưới để panel proxy hẹp không tràn
        proxy_btn2 = ttk.Frame(proxy_frame)
        proxy_btn2.grid(row=2, column=0, columnspan=2, sticky="ew", padx=4, pady=(0, 4))
        ttk.Button(proxy_btn2, text="Lưu proxy", command=self._save_proxy_manual).pack(side="left", padx=2)
        ttk.Button(proxy_btn2, text="Test proxy", command=self._test_proxy).pack(side="left", padx=2)

        # ── LLM API (để tự giải CAPTCHA) ─────────────────────────────────────
        # 2 ô: API key + base_url|model. Lưu ra llm_key.txt / llm_api.txt.
        base, model = load_llm_config()
        llm_row = ttk.Frame(proxy_frame)
        llm_row.grid(row=3, column=0, columnspan=2, sticky="ew", padx=4, pady=(2, 2))
        llm_row.columnconfigure(1, weight=1)
        ttk.Label(llm_row, text="🤖 LLM key:").grid(row=0, column=0, sticky="w")
        self.llm_key_entry = ttk.Entry(llm_row, show="•")
        self.llm_key_entry.insert(0, load_llm_key())
        self.llm_key_entry.grid(row=0, column=1, sticky="ew", padx=(4, 0))
        ttk.Button(llm_row, text="Lưu key", command=self._save_llm_key).grid(row=0, column=2, padx=4)
        llm_row2 = ttk.Frame(proxy_frame)
        llm_row2.grid(row=4, column=0, columnspan=2, sticky="ew", padx=4, pady=(0, 4))
        llm_row2.columnconfigure(1, weight=1)
        ttk.Label(llm_row2, text="API:").grid(row=0, column=0, sticky="w")
        self.llm_api_entry = ttk.Entry(llm_row2)
        self.llm_api_entry.insert(0, f"{base}|{model}")
        self.llm_api_entry.grid(row=0, column=1, sticky="ew", padx=(4, 0))
        ttk.Button(llm_row2, text="Lưu API",
                   command=self._save_llm_api).grid(row=0, column=2, padx=4)

        # ── CAPTCHA solver: chọn provider + key omocaptcha ────────────────────
        cap_row = ttk.Frame(proxy_frame)
        cap_row.grid(row=5, column=0, columnspan=2, sticky="ew", padx=4, pady=(2, 2))
        cap_row.columnconfigure(1, weight=1)
        ttk.Label(cap_row, text="Solver:").grid(row=0, column=0, sticky="w")
        self.cap_provider_var = tk.StringVar(value=load_cap_provider())
        ttk.Combobox(cap_row, textvariable=self.cap_provider_var, width=8,
                     state="readonly", values=["llm", "omo"]).grid(
                     row=0, column=1, sticky="w", padx=(4, 0))
        ttk.Button(cap_row, text="Lưu solver",
                   command=self._save_cap_provider).grid(row=0, column=2, padx=4)
        omo_row = ttk.Frame(proxy_frame)
        omo_row.grid(row=6, column=0, columnspan=2, sticky="ew", padx=4, pady=(0, 4))
        omo_row.columnconfigure(1, weight=1)
        ttk.Label(omo_row, text="🔑 Omo key:").grid(row=0, column=0, sticky="w")
        self.omo_key_entry = ttk.Entry(omo_row, show="•")
        self.omo_key_entry.insert(0, load_omo_key())
        self.omo_key_entry.grid(row=0, column=1, sticky="ew", padx=(4, 0))
        ttk.Button(omo_row, text="Lưu key", command=self._save_omo_key).grid(
                   row=0, column=2, padx=4)
        ttk.Button(omo_row, text="Kiểm tra", command=self._check_omo_key).grid(
                   row=0, column=3, padx=4)

        # ── Thao tác ──────────────────────────────────────────────────────
        action_frame = ttk.LabelFrame(mid, text="Thao tác")
        action_frame.grid(row=0, column=1, sticky="nsew")
        action_frame.columnconfigure(1, weight=1)

        mode_row = ttk.Frame(action_frame)
        mode_row.grid(row=0, column=0, columnspan=2, sticky="w", padx=6, pady=(6, 2))
        ttk.Label(mode_row, text="Chế độ:").pack(side="left")
        self.mode_var = tk.StringVar(value="video")
        ttk.Radiobutton(mode_row, text="Tim video",   variable=self.mode_var,
                        value="video",   command=self._on_mode_change).pack(side="left", padx=8)
        ttk.Radiobutton(mode_row, text="Tim comment", variable=self.mode_var,
                        value="comment", command=self._on_mode_change).pack(side="left")

        # Link — hỗ trợ NHIỀU link, mỗi dòng 1 link. Mỗi acc lần lượt chạy hết.
        ttk.Label(action_frame, text="Link:").grid(row=1, column=0, sticky="nw", padx=6, pady=(4, 0))
        self.link_txt = scrolledtext.ScrolledText(action_frame, height=3, wrap="none")
        self.link_txt.insert("1.0", "https://vt.tiktok.com/....")
        self.link_txt.grid(row=1, column=1, sticky="ew", padx=(0, 6), pady=(4, 0))

        # Ghi chú cho ô Link (mỗi dòng 1 link)
        ttk.Label(action_frame, text="(Nhiều link: mỗi dòng 1 link. Link có ?comment_id=… → tự gọi API)",
                  foreground="gray").grid(row=2, column=1, sticky="w", padx=(0, 6), pady=(0, 2))

        # Target comment — label + ô nhập cùng hàng
        self.target_label = ttk.Label(action_frame, text="@user / comment:")
        self.target_label.grid(row=3, column=0, sticky="w", padx=6)
        self.target_entry = ttk.Entry(action_frame)
        self.target_entry.grid(row=3, column=1, sticky="ew", padx=(0, 6), pady=2)

        # Điều khiển
        ctrl = ttk.Frame(action_frame)
        ctrl.grid(row=4, column=0, columnspan=2, sticky="ew", padx=6, pady=(6, 6))
        self.headless_var = tk.BooleanVar(value=False)
        self.headless_chk = ttk.Checkbutton(ctrl, text="Headless", variable=self.headless_var)
        self.headless_chk.pack(side="left")
        self.mobile_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(ctrl, text="Mobile", variable=self.mobile_var).pack(side="left", padx=(6, 0))
        # Debug: 1 luồng, không headless, mở DevTools (F12), GIỮ browser mở để inspect
        self.debug_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(ctrl, text="🐞 Debug (hiện browser)", variable=self.debug_var,
                        command=self._on_debug_toggle).pack(side="left", padx=(6, 0))
        ttk.Label(ctrl, text="  Luồng:").pack(side="left")
        self.threads_var = tk.IntVar(value=1)
        self.threads_spin = ttk.Spinbox(ctrl, from_=1, to=9999, width=5,
                                        textvariable=self.threads_var)
        self.threads_spin.pack(side="left", padx=(2, 0))
        self.stop_btn = ttk.Button(ctrl, text="■ Dừng", command=self.on_stop, state="disabled")
        self.stop_btn.pack(side="right", padx=(4, 0))
        self.pause_btn = ttk.Button(ctrl, text="⏸ Tạm dừng", command=self.on_pause,
                                    state="disabled")
        self.pause_btn.pack(side="right", padx=(4, 0))
        self.run_btn = ttk.Button(ctrl, text="▶ Chạy tất cả", command=self.on_run)
        self.run_btn.pack(side="right", padx=(4, 0))
        self.run_sel_btn = ttk.Button(ctrl, text="▶ Chạy đã chọn", command=self.on_run_selected)
        self.run_sel_btn.pack(side="right")

        # ══════════════════════════════════════════════════════════════════
        # VÙNG 3: Nhật ký
        # ══════════════════════════════════════════════════════════════════
        log_frame = ttk.LabelFrame(frm, text="Nhật ký")
        log_frame.grid(row=2, column=0, sticky="nsew", **pad)
        log_frame.rowconfigure(1, weight=1)
        log_frame.columnconfigure(0, weight=1)

        log_toolbar = ttk.Frame(log_frame)
        log_toolbar.grid(row=0, column=0, sticky="ew", padx=4, pady=(4, 0))
        ttk.Label(log_toolbar, text="Tìm:").pack(side="left")
        self.log_filter_var = tk.StringVar()
        ttk.Entry(log_toolbar, textvariable=self.log_filter_var, width=28).pack(
            side="left", fill="x", expand=True, padx=4)
        ttk.Button(log_toolbar, text="Xóa log", command=self._clear_log).pack(side="right")

        self.log_area = scrolledtext.ScrolledText(log_frame, height=10, wrap="word", state="disabled")
        self.log_area.grid(row=1, column=0, sticky="nsew", padx=4, pady=(2, 4))

    def _on_mode_change(self):
        is_comment = self.mode_var.get() == "comment"
        state = "normal" if is_comment else "disabled"
        self.target_entry.configure(state=state)
        self.target_label.configure(foreground="" if is_comment else "gray")

    def _on_debug_toggle(self):
        """Debug bật → ép 1 luồng + tắt headless (cần thấy browser để inspect),
        khóa 2 ô đó lại. Tắt → mở khóa."""
        if self.debug_var.get():
            self.headless_var.set(False)
            self.threads_var.set(1)
            self.headless_chk.configure(state="disabled")
            self.threads_spin.configure(state="disabled")
            self.log("🐞 Chế độ Debug: 1 luồng, mở DevTools, GIỮ browser sau khi chạy.")
        else:
            self.headless_chk.configure(state="normal")
            self.threads_spin.configure(state="normal")

    def _save_llm_key(self):
        save_llm_key(self.llm_key_entry.get().strip())
        self.log("🔑 Đã lưu LLM key." if self.llm_key_entry.get().strip()
                 else "🔑 Đã xóa LLM key.")

    def _save_llm_api(self):
        """Ô API nhập dạng 'base_url|model'. Tách ra lưu 2 file riêng."""
        raw = self.llm_api_entry.get().strip()
        if not raw:
            return
        parts = raw.split("|")
        base = parts[0].strip() if parts[0].strip() else DEFAULT_LLM_BASE
        model = parts[1].strip() if len(parts) > 1 and parts[1].strip() else DEFAULT_LLM_MODEL
        save_llm_config(base, model)
        self.log(f"🤖 Đã lưu API: {base} | model={model}")

    def _save_cap_provider(self):
        """Lưu provider giải CAPTCHA: 'llm' (vision) hoặc 'omo' (omocaptcha)."""
        p = self.cap_provider_var.get()
        save_cap_provider(p)
        self.autocap_var.set(has_captcha_key())
        self.log(f"🧩 Solver CAPTCHA = {p.upper()}"
                 + ("" if has_captcha_key() else " (⚠️ chưa có key)"))

    def _save_omo_key(self):
        save_omo_key(self.omo_key_entry.get().strip())
        self.autocap_var.set(has_captcha_key())
        self.log("🔑 Đã lưu Omo key." if self.omo_key_entry.get().strip()
                 else "🔑 Đã xóa Omo key.")

    def _check_omo_key(self):
        """Kiểm tra key omocaptcha live/die qua getAccountInfo (chạy nền)."""
        key = self.omo_key_entry.get().strip() or load_omo_key()
        if not key:
            self.log("🔑 Chưa nhập Omo key.")
            return
        self.log("⏳ Đang kiểm tra Omo key...")
        def worker():
            ok, msg = check_omo_key(key)
            self.log(f"{'✅' if ok else '❌'} Omo key: {msg}")
        threading.Thread(target=worker, daemon=True).start()

    _proxy_save_after = None

    def _on_proxy_modified(self, _event=None):
        self.proxy_txt.edit_modified(False)
        if self._proxy_save_after:
            self.after_cancel(self._proxy_save_after)
        self._proxy_save_after = self.after(1500, self._save_proxy_auto)

    def _save_proxy_auto(self):
        save_proxies(self.proxy_txt.get("1.0", "end").rstrip())
        self._proxy_save_after = None

    def _save_proxy_manual(self):
        save_proxies(self.proxy_txt.get("1.0", "end").rstrip())
        self.log("Đã lưu proxy.")

    # ── Tree helpers ──────────────────────────────────────────────────────
    def _sorted_names(self):
        def key(s):
            return int(s[3:]) if s.startswith("acc") and s[3:].isdigit() else float("inf")
        return sorted(self._accounts.keys(), key=key)

    def _refresh_tree(self):
        if not self._alive:
            return
        self.tree.delete(*self.tree.get_children())
        self._tree_map.clear()
        for i, name in enumerate(self._sorted_names(), 1):
            acc = self._accounts[name]
            cookie_trim = acc["cookie"][:35].replace("\n", " ") + "..."
            iid = self.tree.insert("", "end", values=(
                i,
                acc["username"] or name,
                cookie_trim,
                acc["status"],
            ))
            self._tree_map[iid] = name

    def _set_status(self, name: str, status: str, username: str = None):
        with self._save_lock:
            if name not in self._accounts:
                return
            self._accounts[name]["status"] = status
            if username is not None:
                self._accounts[name]["username"] = username
            save_accounts(self._accounts)
        if self._alive:
            try:
                self.after(0, self._refresh_tree)
            except (tk.TclError, RuntimeError):
                pass

    def _proxy_scheme(self) -> str:
        """Loại proxy mặc định do dropdown chọn (áp cho dòng không có tiền tố)."""
        try:
            return self.proxy_type_var.get()
        except Exception:
            return "http"

    def _get_proxy(self, idx: int = 0):
        """Proxy chua 403-blacklist (TTL 15 phut)."""
        lines = [l.strip() for l in self.proxy_txt.get("1.0", "end").splitlines() if l.strip()]
        if not lines:
            return None
        return sys.modules[__name__].pick_proxy(lines, self._proxy_scheme())

    def _test_proxy(self):
        proxies = [l.strip() for l in self.proxy_txt.get("1.0", "end").splitlines() if l.strip()]
        if not proxies:
            messagebox.showwarning("Trống", "Chưa nhập proxy nào.")
            return
        threading.Thread(target=self._test_proxy_job, args=(proxies,), daemon=True).start()

    def _test_proxy_job(self, proxies):
        scheme = self._proxy_scheme()
        for raw in proxies:
            proxy = parse_proxy(raw, scheme)
            if not proxy:
                self.log(f"[proxy] \u274c Parse th\u1ea5t b\u1ea1i: {raw}")
                continue
            host = proxy['host']
            port = proxy['port']
            user = proxy.get('user', '')
            pw = proxy.get('password', '')
            ps = proxy.get('scheme', 'http')
            self.log(f"[proxy] Ki\u1ec3m tra {ps}://{host}:{port}...")
            try:
                if user and pw:
                    proxy_url = f"{ps}://{user}:{pw}@{host}:{port}"
                else:
                    proxy_url = f"{ps}://{host}:{port}"
                proxies_dict = {"http": proxy_url, "https": proxy_url}
                r = requests.get("https://api.ipify.org?format=json",
                                 proxies=proxies_dict, timeout=10)
                try:
                    ip = r.json().get("ip", r.text.strip())
                except Exception:
                    ip = r.text.strip()
                self.log(f"[proxy] \u2705 OK \u2014 IP th\u1ef1c t\u1ebf: {ip}")
            except Exception as e:
                self.log(f"[proxy] \u274c L\u1ed7i: {e}")

    def open_import_dialog(self):
        ImportDialog(self)

    def _reset_buttons(self):
        self.pause_event.set()
        if not self._alive:
            return
        try:
            self.run_btn.configure(state="normal")
            self.run_sel_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
            self.pause_btn.configure(state="disabled", text="⏸ Tạm dừng")
        except tk.TclError:
            pass

    def on_stop(self):
        self.stop_event.set()
        self.pause_event.set()
        self.stop_btn.configure(state="disabled")
        self.pause_btn.configure(state="disabled")
        self.log("⛔ Đang dừng — acc đang chạy sẽ hoàn tất rồi dừng hẳn.")

    def on_pause(self):
        """Tạm dừng / tiếp tục. Pause chỉ chặn acc MỚI, acc đang chạy vẫn hoàn tất."""
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.pause_btn.configure(text="▶ Tiếp tục")
            self.log("⏸ Đã tạm dừng — acc đang chạy sẽ hoàn tất, không mở acc mới.")
        else:
            self.pause_event.set()
            self.pause_btn.configure(text="⏸ Tạm dừng")
            self.log("▶ Tiếp tục.")

    def _wait_if_paused(self) -> bool:
        """Chặn khi đang pause; trả False nếu bị stop trong lúc chờ (bỏ qua acc)."""
        while not self.pause_event.wait(timeout=0.3):
            if self.stop_event.is_set():
                return False
        return not self.stop_event.is_set()

    # ── Account CRUD ──────────────────────────────────────────────────────
    def _next_name(self):
        i = 1
        while f"acc{i}" in self._accounts:
            i += 1
        return f"acc{i}"

    def save_account(self):
        cookie_raw = self.cookie_entry.get().strip()
        if not cookie_raw or cookie_raw == "Dán cookie vào đây...":
            messagebox.showwarning("Thiếu cookie", "Vui lòng dán cookie vào ô trước.")
            return
        name = self._next_name()
        self._accounts[name] = {"cookie": cookie_raw, "username": "", "status": "?"}
        save_accounts(self._accounts)
        self._refresh_tree()
        self.cookie_entry.delete(0, "end")
        self.cookie_entry.insert(0, "Dán cookie vào đây...")
        self.cookie_entry.configure(foreground="gray")
        self.log(f"✅ Đã lưu '{name}'.")

    def import_cookies_file(self):
        """Import cookie hàng loạt từ file. Mỗi dòng = 1 cookie (chuỗi ; hoặc JSON
        1 dòng), HOẶC toàn file là 1 JSON array các cookie. Tự bỏ cookie trùng
        (cùng sessionid) so với danh sách hiện có và trùng lẫn nhau trong file."""
        path = filedialog.askopenfilename(
            title="Chọn file cookie",
            filetypes=[("Text/JSON", "*.txt *.json"), ("Tất cả", "*.*")],
        )
        if not path:
            return
        try:
            content = Path(path).read_text(encoding="utf-8").strip()
        except Exception as e:
            messagebox.showerror("Lỗi đọc file", str(e))
            return
        if not content:
            messagebox.showwarning("Trống", "File không có nội dung.")
            return

        # Tách thành từng cookie thô. Nếu cả file là 1 JSON array cookie → coi là 1 acc.
        raw_cookies = []
        try:
            data = json.loads(content)
            if isinstance(data, list) and data and isinstance(data[0], dict):
                raw_cookies = [content]          # 1 array cookie = 1 tài khoản
            elif isinstance(data, list):
                raw_cookies = [json.dumps(x, ensure_ascii=False) for x in data]
            else:
                raw_cookies = [content]
        except Exception:
            # Không phải JSON → mỗi dòng không rỗng là 1 cookie
            raw_cookies = [ln.strip() for ln in content.splitlines() if ln.strip()]

        # Tập identity đã có để chống trùng
        existing = {cookie_identity(a.get("cookie", "")) for a in self._accounts.values()}
        existing.discard("")
        added = dup = bad = 0
        for raw in raw_cookies:
            ident = cookie_identity(raw)
            if not ident:
                bad += 1
                continue
            if ident in existing:
                dup += 1
                continue
            existing.add(ident)
            name = self._next_name()
            self._accounts[name] = {"cookie": raw, "username": "", "status": "?"}
            added += 1
        save_accounts(self._accounts)
        self._refresh_tree()
        self.log(f"📥 Import cookie: +{added} mới, {dup} trùng bỏ qua, {bad} không hợp lệ.")
        messagebox.showinfo("Import cookie",
                            f"Thêm mới: {added}\nTrùng (bỏ qua): {dup}\nKhông hợp lệ: {bad}")

    def remove_duplicates(self):
        """Quét danh sách hiện có, gộp/xóa các tài khoản cookie trùng nhau."""
        if not self._accounts:
            messagebox.showinfo("Trống", "Chưa có tài khoản nào.")
            return
        new_accounts, removed = dedupe_accounts(self._accounts)
        if removed == 0:
            messagebox.showinfo("Xóa trùng", "Không tìm thấy cookie trùng.")
            return
        if not messagebox.askyesno("Xác nhận",
                                   f"Tìm thấy {removed} tài khoản trùng cookie. Xóa chúng?"):
            return
        self._accounts = new_accounts
        save_accounts(self._accounts)
        self._refresh_tree()
        self.log(f"🧹 Đã xóa {removed} tài khoản trùng cookie.")

    def delete_account(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showwarning("Chưa chọn", "Vui lòng chọn tài khoản cần xóa.")
            return
        names = [self._tree_map[iid] for iid in sel if iid in self._tree_map]
        if not messagebox.askyesno("Xác nhận xóa", f"Xóa {len(names)} tài khoản?"):
            return
        for name in names:
            del self._accounts[name]
        save_accounts(self._accounts)
        self._refresh_tree()
        self.log(f"Đã xóa {len(names)} tài khoản.")

    # ── Check Live ────────────────────────────────────────────────────────
    def check_live_selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showwarning("Chưa chọn", "Vui lòng chọn tài khoản cần kiểm tra.")
            return
        names = [self._tree_map[iid] for iid in sel if iid in self._tree_map]
        self._start_worker(self._check_job, names)

    def check_live_all(self):
        if not self._accounts:
            messagebox.showinfo("Trống", "Chưa có tài khoản nào.")
            return
        self._start_worker(self._check_job, self._sorted_names())

    def _check_one(self, name, idx, total):
        if not self._wait_if_paused():
            return
        self.log(f"[{idx}/{total}] Kiểm tra {name}...")
        acc = self._accounts.get(name)
        if not acc:
            return
        bot = TikTokLikeBot(self.log, self.stop_event, prefix=name)
        try:
            proxy = self._get_proxy(idx - 1)
            bot.start_browser(headless=self.headless_var.get(), session_idx=idx - 1,
                              proxy=proxy)
            cookies = parse_cookies(acc["cookie"])
            bot.inject_cookies(cookies)
            ok, username = bot.check_live_api()
            if ok and username:
                self._set_status(name, "Live", username)
                self.log(f"  [{name}] ✅ Live ({username})")
            else:
                self._set_status(name, "Offline")
                self.log(f"  [{name}] ❌ Offline"
                         + ("" if ok else "")
                         + (" (passport OK nhưng không có unique_id)" if ok and not username else ""))
        except Exception as e:
            self._set_status(name, "Lỗi")
            self.log(f"  [{name}] ❌ Lỗi: {e}")
        finally:
            bot.close()

    def _check_job(self, names):
        total = len(names)
        n = min(self.threads_var.get(), total)
        with ThreadPoolExecutor(max_workers=n) as ex:
            futures = {ex.submit(self._check_one, name, idx, total): name
                       for idx, name in enumerate(names, 1)}
            for f in as_completed(futures):
                if self.stop_event.is_set():
                    break
                try:
                    f.result()
                except Exception as e:
                    self.log(f"❌ {futures[f]}: {e}")
        self.after(0, self._reset_buttons)
        self.log("Check Live hoàn tất.")

    # ── Run ───────────────────────────────────────────────────────────────
    def _get_run_params(self):
        """Validate và trả về (links, mode, target) hoặc None nếu thiếu.

        links: danh sách link (mỗi dòng 1 link, bỏ dòng trống/placeholder)."""
        raw = self.link_txt.get("1.0", "end")
        links = [l.strip() for l in raw.splitlines()
                 if l.strip() and not l.strip().startswith("https://vt.tiktok.com/....")]
        mode = self.mode_var.get()
        target = self.target_entry.get().strip() if mode == "comment" else ""
        if not links:
            messagebox.showwarning("Thiếu dữ liệu", "Vui lòng nhập ít nhất 1 link.")
            return None
        return links, mode, target

    def on_run(self):
        if self.worker and self.worker.is_alive():
            return
        if not self._accounts:
            messagebox.showwarning("Trống", "Chưa có tài khoản nào.")
            return
        params = self._get_run_params()
        if not params:
            return
        self._start_worker(self._run_job, self._sorted_names(), *params)

    def on_run_selected(self):
        if self.worker and self.worker.is_alive():
            return
        sel = self.tree.selection()
        if not sel:
            messagebox.showwarning("Chưa chọn", "Vui lòng chọn tài khoản cần chạy.")
            return
        names = [self._tree_map[iid] for iid in sel if iid in self._tree_map]
        params = self._get_run_params()
        if not params:
            return
        self._start_worker(self._run_job, names, *params)

    def _start_worker(self, fn, *args):
        if self.worker and self.worker.is_alive():
            messagebox.showwarning("Đang chạy", "Vui lòng chờ tác vụ hiện tại hoàn tất.")
            return
        self.stop_event.clear()
        self.pause_event.set()
        self.run_btn.configure(state="disabled")
        self.run_sel_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.pause_btn.configure(state="normal", text="⏸ Tạm dừng")
        self.worker = threading.Thread(target=fn, args=args, daemon=True)
        self.worker.start()

    def _rotate_browser(self, bot, name, idx, cookies, used: set, debug: bool):
        """403: blacklist proxy hien tai, doi proxy, mo lai Chrome, login."""
        old = getattr(bot, "_last_proxy", None)
        if old:
            blacklist_proxy(old)
            used.add(_proxy_key(old))
            self.log(f"  [{name}] 🚫 Blacklist {_proxy_key(old)} 15 phut.")
        nxt = self._get_proxy_avoid(used)
        if not nxt:
            self.log(f"  [{name}] ⏭️ Het proxy sach (403).")
            return False
        try:
            bot.close()
        except Exception:
            pass
        self.log(f"  [{name}] 🔄 403 → proxy {nxt.get('host')}:{nxt.get('port')}")
        bot.last_proxy_blocked = False
        bot.start_browser(headless=self.headless_var.get(), session_idx=idx - 1,
                          proxy=nxt, mobile=self.mobile_var.get(), debug=debug)
        bot._last_proxy = nxt
        used.add(_proxy_key(nxt))
        return bool(cookies and bot.login_with_cookies(cookies))

    def _get_proxy_avoid(self, used: set):
        lines = [l.strip() for l in self.proxy_txt.get("1.0", "end").splitlines() if l.strip()]
        if not lines:
            return None
        return sys.modules[__name__].pick_proxy(lines, self._proxy_scheme(), avoid=used)

    def _do_one_link(self, bot, name, link, mode, target, li, nlinks):
        """Xử lý 1 link cho 1 acc (đã login sẵn). Trả True nếu thành công."""
        tag = f"link {li}/{nlinks}" if nlinks > 1 else "link"
        bot.open_link(link)
        if getattr(bot, "last_proxy_blocked", False):
            return "403"
        if mode == "video":
            ok = bot.like_video()
            self.log(f"  [{name}] {'✅' if ok else '❌'} Tim video ({tag}).")
            return ok
        if not extract_comment_id(link) and not (target or "").strip():
            if not bot.open_comment_panel():
                self.log(f"  [{name}] ⚠️ Bỏ qua ({tag}): không mở được panel comment.")
                return False
        ok = bot.like_comment(target, link=link)
        self.log(f"  [{name}] {'✅' if ok else '❌'} Tim comment ({tag}).")
        return ok

    def _run_one(self, name, idx, total, links, mode, target, stats, lock):
        if not self._wait_if_paused():
            return
        time.sleep(max(0, (idx - 1) * 0.4))
        if not self._wait_if_paused():
            return
        self.log(f"── [{idx}/{total}] {name} ──")
        acc = self._accounts.get(name)
        if not acc:
            return
        debug = self.debug_var.get()
        bot = TikTokLikeBot(self.log, self.stop_event, prefix=name)
        used: set[str] = set()
        try:
            proxy = self._get_proxy(idx - 1)
            bot.start_browser(headless=self.headless_var.get(), session_idx=idx - 1,
                              proxy=proxy, mobile=self.mobile_var.get(), debug=debug)
            bot._last_proxy = proxy
            if proxy:
                used.add(_proxy_key(proxy))
            cookies = parse_cookies(acc["cookie"])
            if not bot.login_with_cookies(cookies):
                if getattr(bot, "last_proxy_blocked", False):
                    if not self._rotate_browser(bot, name, idx, cookies, used, debug):
                        return
                else:
                    self.log(f"  [{name}] Cookie het — thu re-login user/pass...")
                    try:
                        import bot_login
                        pw = acc.get("password") or ""
                        totp = acc.get("totp_secret") or acc.get("2fa") or ""
                        user = acc.get("login_user") or acc.get("username") or name
                        if not pw:
                            self.log(f"  [{name}] ❌ Đăng nhập thất bại (khong co pass).")
                            self._set_status(name, "Offline")
                            return
                        ok_rl, cookie, uname = bot_login.auto_login(
                            bot, user, pw, totp, log_fn=self.log)
                        if not (ok_rl and cookie):
                            self.log(f"  [{name}] ❌ Re-login that bai.")
                            self._set_status(name, "Offline")
                            return
                        acc["cookie"] = cookie
                        cookies = parse_cookies(cookie)
                        if uname:
                            acc["username"] = uname.lstrip("@")
                        self.log(f"  [{name}] Re-login OK {uname or ''}")
                    except Exception as e:
                        self.log(f"  [{name}] ❌ Re-login loi: {e}")
                        self._set_status(name, "Offline")
                        return
            if not acc["username"]:
                uname = bot.get_username()
                self._set_status(name, "Live", uname)
            else:
                self._set_status(name, "Live")
            nlinks = len(links)
            for li, link in enumerate(links, 1):
                if self.stop_event.is_set():
                    break
                if li > 1:
                    bot._rsleep(2.5, spread=0.5)
                ok = self._do_one_link(bot, name, link, mode, target, li, nlinks)
                rotates = 0
                while ok == "403" and rotates < 4:
                    rotates += 1
                    if not self._rotate_browser(bot, name, idx, cookies, used, debug):
                        ok = False
                        break
                    ok = self._do_one_link(bot, name, link, mode, target, li, nlinks)
                if ok == "403":
                    self.log(f"  [{name}] ⏭️ Bỏ qua (link {li}) — het proxy (403).")
                    ok = False
                with lock:
                    stats[li]["ok"] += 1 if ok else 0
                    stats[li]["done"] += 1
        except Exception as e:
            first_line = str(e).splitlines()[0]
            self.log(f"  [{name}] ❌ Lỗi: {first_line}")
        finally:
            if debug:
                # Debug: GIỮ browser mở để tự inspect element (F12). Người dùng
                # đóng cửa sổ browser bằng tay khi xong.
                self._debug_bots.append(bot)
                self.log(f"  [{name}] 🐞 Debug: browser vẫn mở — tự inspect rồi đóng tay.")
            else:
                bot.close()

    def _run_job(self, names, links, mode, target):
        total = len(names)
        n, note = recommend_thread_count(self.threads_var.get(), total)
        _COMMENT_CACHE.clear()
        if note:
            self.log(f"ℹ️ {note}")
        self.log(f"Chạy {total} tài khoản × {len(links)} link với {n} luồng...")
        # Thống kê theo link: {link_index: {ok, done}} — cập nhật thread-safe.
        stats = {li: {"ok": 0, "done": 0} for li in range(1, len(links) + 1)}
        lock = threading.Lock()
        with ThreadPoolExecutor(max_workers=n) as ex:
            futures = {ex.submit(self._run_one, name, idx, total, links,
                                 mode, target, stats, lock): name
                       for idx, name in enumerate(names, 1)}
            for f in as_completed(futures):
                if self.stop_event.is_set():
                    break
                try:
                    f.result()
                except Exception as e:
                    first_line = str(e).splitlines()[0]
                    self.log(f"❌ {futures[f]}: {first_line}")
        self.after(0, self._reset_buttons)
        self._log_summary(links, mode, stats)

    def _log_summary(self, links, mode, stats):
        """In tổng kết cuối: mỗi link X/Y acc thành công."""
        kind = "video" if mode == "video" else "comment"
        self.log("━━━━━━━━━━ KẾT QUẢ ━━━━━━━━━━")
        total_ok = total_done = 0
        for li in range(1, len(links) + 1):
            s = stats.get(li, {"ok": 0, "done": 0})
            total_ok += s["ok"]
            total_done += s["done"]
            label = f"Link {li}" if len(links) > 1 else "Link"
            self.log(f"  {label}: đã tim {kind}  {s['ok']}/{s['done']} acc")
        if len(links) > 1:
            self.log(f"  TỔNG: {total_ok}/{total_done} lượt thành công")
        self.log("━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    def _clear_log(self):
        self.log_area.configure(state="normal")
        self.log_area.delete("1.0", "end")
        self.log_area.configure(state="disabled")

    def _on_close(self):
        self._alive = False
        self.stop_event.set()      # báo worker dừng để không thao tác widget nữa
        self.pause_event.set()
        # Đóng các browser đang giữ mở ở chế độ Debug để không mồ côi
        for bot in self._debug_bots:
            try:
                bot.close()
            except Exception:
                pass
        try:
            self._log_file.close()
        except Exception:
            pass
        self.destroy()

    # ── Log ───────────────────────────────────────────────────────────────
    def log(self, msg):
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        self.log_queue.put(line)
        try:
            self._log_file.write(line + "\n")
        except Exception:
            pass

    def _drain_log(self):
        if not self._alive:
            return
        try:
            while not self.log_queue.empty():
                msg = self.log_queue.get()
                self.log_area.configure(state="normal")
                self.log_area.insert("end", msg + "\n")
                self.log_area.see("end")
                self.log_area.configure(state="disabled")
            self.after(100, self._drain_log)
        except tk.TclError:
            pass


if __name__ == "__main__":
    App().mainloop()

# Persist validated sessions locally for subsequent scout batches.
import scout_cookie_cache
scout_cookie_cache.install()

# Timings contain no usernames, credentials, cookies, or target URLs.
def _install_performance_timing():
    import functools
    for method_name in ('start_browser', 'verify_proxy_ip', 'login_with_cookies',
                        'open_link', 'api_digg_comments'):
        original = getattr(TikTokLikeBot, method_name, None)
        if original is None:
            continue
        def wrap(function, label):
            @functools.wraps(function)
            def timed(self, *args, **kwargs):
                started = time.monotonic()
                try:
                    return function(self, *args, **kwargs)
                finally:
                    try:
                        if not (label == 'api_digg_comments' and getattr(self, '_in_per_cid_batch', False)):
                            self.log(f'[perf] {label}: {time.monotonic() - started:.2f}s')
                    except Exception:
                        pass
            return timed
        setattr(TikTokLikeBot, method_name, wrap(original, method_name))
_install_performance_timing()

class _ScoutMatchGate:
    def __init__(self, expected):
        self.expected = expected
        self.lock = threading.Lock()
        self.snapshots = {}
        self.ready = {}

    def publish(self, scout, video, username, rows, authoritative=False):
        key = (str(video), _norm_handle(username))
        if not key[0] or not key[1]:
            return None
        unique = {}
        for row in rows or []:
            cid = str(row.get('cid') or '')
            if cid:
                parent = str(row.get('parent_cid') or '')
                if parent in ('0', cid):
                    parent = ''
                copied = dict(row, cid=cid, parent_cid=parent)
                unique[(cid, parent)] = copied
        with self.lock:
            snapshots = self.snapshots.setdefault(key, {})
            snapshots[scout] = unique
            if authoritative and unique:
                current = self.ready.get(key) or []
                if len(unique) >= len(current):
                    self.ready[key] = list(unique.values())
                return [dict(row) for row in self.ready[key]]
            if key in self.ready:
                current = self.ready[key]
                if len(unique) > len(current):
                    self.ready[key] = list(unique.values())
                return [dict(row) for row in self.ready[key]]
            if len(snapshots) < self.expected:
                return None
            best_snap = max(snapshots.values(), key=len, default={})
            if best_snap:
                self.ready[key] = list(best_snap.values())
                return [dict(row) for row in self.ready[key]]
            return None

    def peek_ready(self, video, username=''):
        v = str(video or '')
        u = _norm_handle(username or '')
        if not v:
            return None
        with self.lock:
            if u:
                rows = self.ready.get((v, u))
                return [dict(row) for row in rows] if rows else None
            for (vid, _), rows in self.ready.items():
                if vid == v and rows:
                    return [dict(row) for row in rows]
            return None



def _install_scout_handoff():
    from client.app.runner import Runner
    from types import SimpleNamespace
    def _replace_ns(obj, **kw):
        d = dict(vars(obj)); d.update(kw); return SimpleNamespace(**d)
    if getattr(Runner, '_scout_handoff_installed', False):
        return
    original = Runner._run_one
    original_run = Runner.run
    def run(self, accounts, proxies, opts, *values, **options):
        self._scout_match_gate = _ScoutMatchGate(min(2, len(accounts)))
        self._active_scout_bots = {}
        return original_run(self, accounts, proxies, opts, *values, **options)
    Runner.run = run
    def run_one(self, *args, **kwargs):
        if not kwargs.get('scan_only', False):
            factory = args[5]
            hidden = bool(args[4].headless)
            class PoolBot(factory):
                def start_browser(self, headless=False, *values, **options):
                    return super().start_browser(hidden, *values, **options)
            values = list(args)
            values[5] = PoolBot
            return original(self, *values, **kwargs)
        # Scout browsers stay separate and respect the user's hidden setting. Pool options
        # and its browser count pass through unchanged for every later account.
        account = args[0]
        scout_idx = args[1]
        opts_arg = args[4]
        factory = args[5]
        bots = []
        runner = self
        hidden = bool(opts_arg.headless)
        active_map = getattr(runner, '_active_scout_bots', None)
        if not isinstance(active_map, dict):
            active_map = {}
            runner._active_scout_bots = active_map
        class ScoutBot(factory):
            def __init__(self, *values, **options):
                super().__init__(*values, **options)
                self._scout_close_lock = threading.RLock()
                self._scout_closed = False
                self._scout_session_validated = False
                self._scout_cookie_captured = False
                bots.append(self)
                active_map[scout_idx] = self
                gate = runner._scout_match_gate
                def _publish(video, target, rows, authoritative=False):
                    res = gate.publish(
                        scout_idx, video, target, rows, authoritative=authoritative)
                    if authoritative and res:
                        links_cnt = len([l for l in (getattr(opts_arg, 'links', None) or []) if str(l).strip()])
                        if links_cnt <= 1:
                            def _release_wedged_peers():
                                for idx, peer in list(active_map.items()):
                                    if idx != scout_idx and not getattr(peer, '_scout_closed', False):
                                        try:
                                            peer._mark_driver_unresponsive()
                                        except Exception:
                                            pass
                            t = threading.Timer(0.3, _release_wedged_peers)
                            t.daemon = True
                            t.start()
                    return res
                self._scout_publish = _publish
                self._scout_peek_ready = getattr(gate, 'peek_ready', None)
            def start_browser(self, headless=False, *values, **options):
                return super().start_browser(hidden, *values, **options)
            def login_with_cookies(self, *values, **options):
                result = super().login_with_cookies(*values, **options)
                self._scout_session_validated = bool(result)
                if (
                    self._scout_session_validated
                    and self.driver is not None
                    and not getattr(self, '_driver_unresponsive', False)
                    and not isinstance(self.driver, _UnresponsiveDriverStub)
                ):
                    scout_cookie_cache._capture(runner, self, account)
                    self._scout_cookie_captured = True
                return result
            def close(self):
                with self._scout_close_lock:
                    if self._scout_closed:
                        return
                    active_map.pop(scout_idx, None)
                    if (
                        self._scout_session_validated
                        and not getattr(self, '_scout_cookie_captured', False)
                        and not getattr(self, '_driver_unresponsive', False)
                        and self.driver is not None
                        and not isinstance(self.driver, _UnresponsiveDriverStub)
                    ):
                        scout_cookie_cache._capture(runner, self, account)
                        self._scout_cookie_captured = True
                    try:
                        return super().close()
                    finally:
                        self._scout_closed = True
        values = list(args)
        values[4] = _replace_ns(values[4], debug=False)
        values[5] = ScoutBot
        try:
            return original(self, *values, **kwargs)
        finally:
            active_map.pop(scout_idx, None)
            for bot in bots:
                bot.close()
    Runner._run_one = run_one
    Runner._scout_handoff_installed = True
_install_scout_handoff()

import pool_scheduler
pool_scheduler.install()

import machine_proxy_config
machine_proxy_config.install()

# Local captcha settings survive extraction and executable upgrades.
def _load_runtime_overrides():
    import importlib.util
    from pathlib import Path
    rt_dir = Path(__file__).parent
    for mod_name in ('mailtm', 'bot_login', 'omo_captcha', 'proxy_relay', 'admin_check_accounts', 'comment_account_tools'):
        mod_path = rt_dir / f'{mod_name}.py'
        if mod_path.exists():
            spec = importlib.util.spec_from_file_location(mod_name, str(mod_path))
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            spec.loader.exec_module(mod)
_load_runtime_overrides()

def _install_local_captcha():
    from pathlib import Path
    import omo_captcha
    import captcha_status
    from client.app.bridge import Bridge
    root = Path.home() / '.auto-like-cmt' / 'captcha-v25'
    root.mkdir(parents=True, exist_ok=True)
    import shutil
    defaults = Path(__file__).parent / 'captcha-defaults'
    for name in ('omo_key.txt', 'captcha_provider.txt'):
        target = root / name
        source = defaults / name
        if source.exists() and (not target.exists() or not target.read_text(encoding='utf-8').strip()):
            shutil.copy2(source, target)
    omo_captcha.OMO_KEY_FILE = root / 'omo_key.txt'
    omo_captcha.OMO_PROVIDER_FILE = root / 'captcha_provider.txt'
    def get_captcha_balance(self):
        key = omo_captcha.load_omo_key()
        if not key:
            return {'ok': False, 'error': 'Chưa có API key'}
        try:
            return captcha_status.fetch(key)
        except Exception:
            return {'ok': False, 'error': 'Chưa cập nhật được số captcha'}
    Bridge.get_captcha_balance = get_captcha_balance
_install_local_captcha()

import account_status
account_status.install()

import mail_import
mail_import.install()

import proxy_link_rotation
proxy_link_rotation.install()

import browser_limit_v55
browser_limit_v55.install()

import check_threads_unlimited
check_threads_unlimited.install()

import local_account_results
local_account_results.install()

import cookie_api_session
cookie_api_session.install()

import api_pacing
api_pacing.install()

import native_comment_context
native_comment_context.install()

import digg_transport_trace
digg_transport_trace.install()

import v90_pool_runtime_fix
v90_pool_runtime_fix.install()

import proxy_cooldown_v90
proxy_cooldown_v90.install()

import active_run_timing
active_run_timing.install()

import performance_v95
performance_v95.install()


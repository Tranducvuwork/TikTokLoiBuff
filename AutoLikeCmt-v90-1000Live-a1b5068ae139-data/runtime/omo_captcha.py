# -*- coding: utf-8 -*-
"""Omocaptcha CAPTCHA solver — client cùng giao diện ApiClient của tiktok-captcha-solver.

Thay "bộ não" LLM vision bằng API omocaptcha.com (trả phí theo job, chính xác hơn).
Luồng: createTask → poll getTaskResult mỗi 2s → đọc solution.

Provider chọn qua captcha_provider.txt ("llm" | "omo"), key lưu ở omo_key.txt.
"""
import base64
import io
import json
import time
from pathlib import Path

import requests

OMO_BASE = "https://api.omocaptcha.com/v2"
OMO_KEY_FILE = Path(__file__).parent / "omo_key.txt"
OMO_PROVIDER_FILE = Path(__file__).parent / "captcha_provider.txt"
POLL_INTERVAL = 2   # docs: đợi 2s giữa các lần getTaskResult
POLL_TIMEOUT = 120  # bỏ cuộc sau 2 phút


# ── Lưu trữ key + provider ──────────────────────────────────────────────────
def load_omo_key() -> str:
    if OMO_KEY_FILE.exists():
        return OMO_KEY_FILE.read_text(encoding="utf-8").strip()
    return ""


def save_omo_key(key: str):
    OMO_KEY_FILE.write_text(key.strip(), encoding="utf-8")


def load_cap_provider() -> str:
    """"llm" (mặc định) hoặc "omo"."""
    if OMO_PROVIDER_FILE.exists():
        v = OMO_PROVIDER_FILE.read_text(encoding="utf-8").strip().lower()
        if v in ("llm", "omo"):
            return v
    return "llm"


def save_cap_provider(provider: str):
    OMO_PROVIDER_FILE.write_text(provider.strip().lower(), encoding="utf-8")


def check_omo_key(key: str) -> tuple[bool, str]:
    """Kiểm tra key live/die qua getAccountInfo. Trả (ok, mô tả số dư hoặc lỗi)."""
    try:
        r = requests.post(f"{OMO_BASE}/getAccountInfo",
                          json={"clientKey": key}, timeout=15)
        d = r.json()
    except Exception as e:
        return False, f"Lỗi kết nối: {str(e)[:120]}"
    if d.get("errorId") != 0:
        return False, f"DIE — {d.get('errorCode')}: {d.get('errorDescription')}"
    bal = float(d.get("balance") or 0) + float(d.get("voucherBalance") or 0)
    return True, (f"LIVE — {d.get('username')} | dư ${d.get('balance', '0')} "
                  f"(+KM ${d.get('voucherBalance', '0')}) = ${bal:.2f}")


def _image_size(b64: str) -> tuple[int, int]:
    """Kích thước gốc của ảnh base64 — omocaptcha cần widthView/heightView (px
    hiển thị trên web). Ảnh solver lấy từ DOM nên ≈ kích thước hiển thị."""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(base64.b64decode(b64)))
        return img.size
    except Exception:
        return 340, 212  # kích thước captcha TikTok phổ biến


class OmocaptchaClient:
    """ApiClient-compatible: puzzle/rotate/shapes/icon → task omocaptcha.
    Trả đúng các *CaptchaResponse để SeleniumSolver dùng không cần sửa."""

    def __init__(self, api_key: str, log_fn=None):
        self.api_key = api_key
        self._log = log_fn or (lambda msg: None)

    def _dump_image(self, kind: str, b64: str) -> str:
        """Lưu ảnh challenge ra logs/captcha_debug — để test lại thủ công
        (script test_omo_manual) và chẩn đoán 'giải sai'."""
        import base64 as _b64
        from datetime import datetime
        try:
            d = Path(__file__).parent / "logs" / "captcha_debug"
            d.mkdir(parents=True, exist_ok=True)
            name = f"omo_{kind}_{datetime.now():%H%M%S}.png"
            (d / name).write_bytes(_b64.b64decode(b64))
            return name
        except Exception:
            return ""

    # ── HTTP + polling ──────────────────────────────────────────────────────
    def _post(self, endpoint: str, payload: dict) -> dict:
        r = requests.post(f"{OMO_BASE}/{endpoint}", json=payload, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"omo HTTP {r.status_code}: {r.text[:160]}")
        return r.json()

    def _solve_task(self, task: dict) -> dict:
        """createTask → poll getTaskResult tới khi ready. Trả solution dict."""
        d = self._post("createTask", {"clientKey": self.api_key, "task": task})
        if d.get("errorId") != 0:
            raise RuntimeError(f"omo createTask: {d.get('errorCode')} "
                               f"{d.get('errorDescription')}")
        task_id = d.get("taskId")
        self._log(f"[captcha] omo task={task_id} ({task['type']})")
        deadline = time.time() + POLL_TIMEOUT
        while time.time() < deadline:
            time.sleep(POLL_INTERVAL)
            d = self._post("getTaskResult",
                           {"clientKey": self.api_key, "taskId": task_id})
            if d.get("errorId") != 0:
                raise RuntimeError(f"omo {d.get('errorCode')}: "
                                   f"{d.get('errorDescription')}")
            if d.get("status") == "ready":
                return d.get("solution") or {}
        raise RuntimeError("omo timeout: không có kết quả sau "
                           f"{POLL_TIMEOUT}s")

    # ── 4 loại CAPTCHA (cùng giao diện với ApiClient gốc) ───────────────────
    def puzzle(self, puzzle_b64: str, piece_b64: str):
        """Slider: gửi ảnh nền (có gap), solution.end.x → tỉ lệ trượt."""
        from tiktok_captcha_solver.models import PuzzleCaptchaResponse
        f = self._dump_image("puzzle", puzzle_b64)
        w, _ = _image_size(puzzle_b64)
        self._log(f"[captcha] Loại PUZZLE. Ảnh đã lưu: {f}")
        sol = self._solve_task({
            "type": "TiktokSliderWebTask",
            "imageBase64": puzzle_b64,
            "widthView": w,
        })
        val = min(max(float(sol["end"]["x"]) / w, 0.0), 1.0)
        self._log(f"[captcha] PUZZLE → slide_x={val:.3f} (end.x={sol['end']['x']})")
        return PuzzleCaptchaResponse(slide_x_proportion=val)

    def rotate(self, outer_b64: str, inner_b64: str):
        """Rotate: docs yêu cầu [inner, outer]. solution.rotate 0-1 → độ.

        Docs omocaptcha: targetX = (trackWidth − buttonWidth) × (1 − rotate)
        — slider phải KÉO TỚI vị trí (1−rotate), tức góc quay NGHỊCH của tỉ
        lệ rotate (5 lần test thực tế rotate*360 sai 100% → đổi sang 1−r)."""
        from tiktok_captcha_solver.models import RotateCaptchaResponse
        f1 = self._dump_image("rotate_outer", outer_b64)
        f2 = self._dump_image("rotate_inner", inner_b64)
        self._log(f"[captcha] Loại ROTATE. Ảnh: {f1} {f2}")
        sol = self._solve_task({
            "type": "TiktokRotateWebTask",
            "imageBase64s": [inner_b64, outer_b64],
        })
        val = int(round((1.0 - float(sol["rotate"])) * 360)) % 360
        self._log(f"[captcha] ROTATE → angle={val} (1−rotate="
                  f"{1.0 - float(sol['rotate']):.2f})")
        return RotateCaptchaResponse(angle=val)

    def shapes(self, image_b64: str):
        """Chọn 2 hình giống nhau: pointA/B (px) → tỉ lệ theo w/h."""
        from tiktok_captcha_solver.models import ShapesCaptchaResponse
        f = self._dump_image("shapes", image_b64)
        w, h = _image_size(image_b64)
        self._log(f"[captcha] Loại SHAPES. Ảnh đã lưu: {f}")
        sol = self._solve_task({
            "type": "TiktokSelectObjectWebTask",
            "imageBase64": image_b64,
            "widthView": w,
            "heightView": h,
            "question": "Select the two matching objects.",
        })
        a, b = sol["pointA"], sol["pointB"]
        self._log(f"[captcha] SHAPES → A=({a['x']},{a['y']}) B=({b['x']},{b['y']})")
        return ShapesCaptchaResponse(
            point_one_proportion_x=a["x"] / w, point_one_proportion_y=a["y"] / h,
            point_two_proportion_x=b["x"] / w, point_two_proportion_y=b["y"] / h,
        )

    def icon(self, challenge_text: str, image_b64: str):
        """Icon/3D object: question = challenge_text, pointA/B → tỉ lệ."""
        from tiktok_captcha_solver.models import IconCaptchaResponse, ProportionalPoint
        f = self._dump_image("icon", image_b64)
        w, h = _image_size(image_b64)
        self._log(f"[captcha] Loại ICON (challenge='{challenge_text}'). Ảnh: {f}")
        sol = self._solve_task({
            "type": "Tiktok3DSelectObjectWebTask",
            "imageBase64": image_b64,
            "widthView": w,
            "heightView": h,
            "question": challenge_text or "Select the matching objects.",
        })
        resp = IconCaptchaResponse(proportional_points=[])
        for p in (sol.get("pointA"), sol.get("pointB")):
            if p:
                resp.proportional_points.append(ProportionalPoint(
                    proportion_x=float(p["x"]) / w,
                    proportion_y=float(p["y"]) / h,
                ))
        self._log(f"[captcha] ICON → {len(resp.proportional_points)} điểm")
        return resp

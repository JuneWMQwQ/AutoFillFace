# -*- coding: utf-8 -*-
"""
captcha_ocr.py — 验证码识别模块

用 mss 按固定区域截图，Windows 系统自带 OCR（Windows.Media.Ocr）识别图中字符。
- 零第三方 OCR 依赖：不依赖 onnxruntime / ddddocr（那套在这台机器上 DLL 崩溃）。
- 截图区域为相对"鼠标所在显示器左上角"的像素坐标（与屏幕预览一致）。
"""
import asyncio
import io
import threading

import mss
from PIL import Image

_ocr_engine = None
_ocr_lock = threading.Lock()
_loop = None


def _get_loop():
    """全局事件循环（winrt 异步需要）。"""
    global _loop
    if _loop is None:
        _loop = asyncio.new_event_loop()
        threading.Thread(target=_loop.run_forever, daemon=True).start()
    return _loop


def _get_engine():
    """惰性创建 Windows OCR 引擎（线程安全）。"""
    global _ocr_engine
    if _ocr_engine is None:
        with _ocr_lock:
            if _ocr_engine is None:
                from winrt.windows.globalization import Language
                from winrt.windows.media.ocr import OcrEngine
                eng = None
                try:
                    eng = OcrEngine.try_create_from_language(Language("zh-Hans-CN"))
                except Exception:
                    eng = None
                if eng is None:
                    eng = OcrEngine.try_create_from_user_profile_languages()
                _ocr_engine = eng
    return _ocr_engine


def _recognize_bytes(img_bytes: bytes) -> str:
    """把 PNG/JPEG 字节交给 Windows OCR，返回识别文本。"""
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

    async def _run():
        stream = InMemoryRandomAccessStream()
        writer = DataWriter(stream)
        writer.write_bytes(img_bytes)
        await writer.store_async()
        writer.detach_stream()
        stream.seek(0)

        from winrt.windows.graphics.imaging import BitmapDecoder
        decoder = await BitmapDecoder.create_async(stream)
        bitmap = await decoder.get_software_bitmap_async()

        engine = _get_engine()
        if engine is None:
            return ""
        result = await engine.recognize_async(bitmap)
        return "".join(line.text for line in result.lines)

    fut = asyncio.run_coroutine_threadsafe(_run(), _get_loop())
    try:
        return (fut.result(timeout=15) or "").strip()
    except Exception:
        return ""


def recognize_region(region: dict, monitor: dict | None = None) -> str:
    """截取验证码区域并识别，返回识别文本（可能为空字符串）。

    region: {"left", "top", "width", "height"}，像素坐标。
            left/top 为相对鼠标所在显示器左上角；monitor 提供后自动加上显示器偏移。
    monitor: screen.capture_bgr 返回的显示器信息；为 None 时不加偏移。
    """
    if not region or not (region.get("width") and region.get("height")):
        return ""
    left = int(region.get("left", 0))
    top = int(region.get("top", 0))
    if monitor:
        left += int(monitor.get("left", 0))
        top += int(monitor.get("top", 0))
    grab_region = {
        "left": left,
        "top": top,
        "width": int(region.get("width", 0)),
        "height": int(region.get("height", 0)),
    }
    with mss.mss() as sct:
        shot = sct.grab(grab_region)
    # mss 输出 BGRA，按 BGRX 读成 RGB
    img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    # 放大 2 倍提高识别率（验证码一般较小）
    if img.width < 200:
        img = img.resize((img.width * 2, img.height * 2), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return _recognize_bytes(buf.getvalue())


def copy_to_clipboard(text: str) -> bool:
    """把识别结果复制到剪贴板（可选，失败不影响主流程）。"""
    try:
        import pyperclip
        pyperclip.copy(text)
        return True
    except Exception:
        return False

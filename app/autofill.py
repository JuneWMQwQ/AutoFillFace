# -*- coding: utf-8 -*-
"""
autofill.py — 自动填充编排

流程：截屏 → 本地登录页模板匹配（ORB）→ 人脸验证 → 按记录坐标点击填入 → 执行宏。
不再依赖视觉模型。
"""
import time

from pynput import keyboard, mouse

import captcha_ocr
import screen


class FillResult:
    def __init__(self, ok: bool, stage: str, message: str, entry: dict | None = None,
                 candidates: list = None, similarity: float | None = None):
        self.ok = ok
        self.stage = stage
        self.message = message
        self.entry = entry
        self.candidates = candidates or []
        self.similarity = similarity


def _click(px, py, delay: float) -> None:
    m = mouse.Controller()
    m.position = (int(px), int(py))
    time.sleep(delay)
    m.click(mouse.Button.left, 1)
    time.sleep(delay)


def _type_text(text: str) -> None:
    k = keyboard.Controller()
    with k.pressed(keyboard.Key.ctrl):
        k.press("a"); k.release("a")
    time.sleep(0.1)
    k.type(text)


def _press(name: str) -> None:
    k = keyboard.Controller()
    key = getattr(keyboard.Key, name.lower(), None) if name.lower() in (
        "enter", "tab", "esc", "space", "backspace", "delete", "down", "up", "left", "right") else None
    if key is not None:
        k.press(key); k.release(key)
    else:
        k.type(name)


def run_macro(macro: str, monitor: dict, delay: float = 0.3) -> None:
    """执行自定义宏。每行一条命令：click x,y | type TEXT | key ENTER | sleep 0.5"""
    if not macro:
        return
    for line in macro.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            if line.startswith("click "):
                parts = line[6:].split(",")
                x = float(parts[0]) * monitor["width"] + monitor["left"]
                y = float(parts[1]) * monitor["height"] + monitor["top"]
                _click(x, y, delay)
            elif line.startswith("type "):
                _type_text(line[5:])
            elif line.startswith("key "):
                _press(line[4:])
            elif line.startswith("sleep "):
                time.sleep(float(line[6:]))
        except Exception:
            continue


def run_autofill(config, vault, face_engine, matcher_best,
                 notify=None, choose=None, face_verify: bool = True, face_ui=None) -> FillResult:
    """执行一次自动填充。matcher_best(screen_bgr) -> [(score, entry)]。
    face_ui: 可选对象，有 start()/end(ok) 控制人脸识别动画。"""
    def _notify(stage, text):
        if notify:
            try:
                notify(stage, text)
            except Exception:
                pass

    _notify("capture", "正在截取屏幕…")
    try:
        bgr, monitor = screen.capture_bgr(max_width=1600)
    except Exception as e:
        return FillResult(False, "capture", f"截屏失败：{e}")

    # ---- 本地模板匹配 ----
    _notify("search", "正在本地匹配登录页…")
    try:
        scored = matcher_best(bgr)
    except Exception as e:
        return FillResult(False, "search", f"匹配出错：{e}")
    if not scored:
        return FillResult(False, "search",
                          "未匹配到已保存的登录页。请先在管理器中为该页面新增服务（保存截图+标注坐标）")
    candidates = [e for _, e in scored]
    best_score = scored[0][0]

    # ---- 人脸验证 ----
    if face_verify:
        _notify("face", "正在打开摄像头…")
        if face_ui:
            try:
                face_ui.start()
            except Exception:
                pass
        try:
            import cv2
            _notify("face", "正在打开摄像头…")
            cap = cv2.VideoCapture(int(config.get("camera_index", 0)))
            if not cap.isOpened():
                if face_ui: face_ui.on_fail()
                return FillResult(False, "face", "无法打开摄像头，请检查设备与权限")
            _notify("face", "正在采集人脸帧（约1秒）…")
            frames = []
            for i in range(8):
                ok, frame = cap.read()
                if ok and frame is not None:
                    frames.append(frame)
                time.sleep(0.12)
            cap.release()
            _notify("face", f"采集到 {len(frames)} 帧，正在比对…")
            if not frames:
                if face_ui: face_ui.on_fail()
                return FillResult(False, "face", "摄像头未采集到画面")
            templates = face_engine.templates
            if not templates:
                if face_ui: face_ui.on_fail()
                return FillResult(False, "face", "尚未注册人脸，请先在管理器中注册")
            passed, sim, hit = face_engine.verify_from_frames(frames, templates)
            _notify("face", f"比对完成：相似度 {sim:.2f}")
            if not passed:
                if face_ui: face_ui.on_fail()
                return FillResult(False, "face",
                                  f"人脸验证未通过（相似度 {sim:.2f} < {face_engine.threshold:.2f}）",
                                  similarity=sim)
            _notify("face", f"人脸验证通过（相似度 {sim:.2f}）")
            if face_ui:
                try:
                    face_ui.on_success()
                except Exception:
                    pass
        except Exception as e:
            if face_ui:
                try: face_ui.on_fail()
                except Exception: pass
            return FillResult(False, "face", f"人脸验证出错：{e}")

    # ---- 选条目 ----
    entry = candidates[0]
    if len(candidates) > 1 and choose:
        picked = choose(candidates)
        if picked is None:
            return FillResult(False, "choose", "已取消填充", candidates=candidates)
        entry = picked

    # ---- 填充 ----
    delay = float(config.get("fill_delay", 0.35))
    try:
        user_c = entry.get("user_coord") or []
        pwd_c = entry.get("pwd_coord") or []
        captcha = entry.get("captcha") or {}
        cap_region = captcha.get("region") or {}
        cap_coord = captcha.get("coord") or []
        cap_enabled = bool(captcha and cap_region and cap_coord)
        cap_order = captcha.get("order", "after_pwd")
        cap_before = cap_enabled and cap_order == "before_pwd"
        cap_after = cap_enabled and cap_order == "after_pwd"

        def _fill_captcha():
            _notify("captcha", "正在截取验证码区域并识别…")
            code = captcha_ocr.recognize_region(cap_region, monitor)
            if not code:
                raise RuntimeError("验证码识别为空：区域坐标可能不准或图片不清晰，请重新配置该服务")
            _notify("captcha", f"识别到验证码「{code}」，正在输入…")
            captcha_ocr.copy_to_clipboard(code)
            _click(cap_coord[0] * monitor["width"] + monitor["left"],
                   cap_coord[1] * monitor["height"] + monitor["top"], delay)
            _type_text(code)
            time.sleep(0.2)

        # 用户名：点坐标 → 填用户名
        if user_c and entry.get("username"):
            _notify("fill", "正在填写用户名…")
            _click(user_c[0] * monitor["width"] + monitor["left"],
                   user_c[1] * monitor["height"] + monitor["top"], delay)
            _type_text(entry["username"])
            time.sleep(0.2)
        # 验证码（顺序：密码之前）
        if cap_before:
            _fill_captcha()
        # 密码：点坐标 → 填密码
        if pwd_c:
            _notify("fill", "正在填写密码…")
            _click(pwd_c[0] * monitor["width"] + monitor["left"],
                   pwd_c[1] * monitor["height"] + monitor["top"], delay)
            _type_text(entry["password"])
        else:
            _notify("fill", "该服务未标注密码坐标，将填入当前聚焦输入框（请确认光标在密码框内）…")
            time.sleep(1.2)
            _type_text(entry["password"])
        # 验证码（顺序：密码之后）
        if cap_after:
            _fill_captcha()
        # 宏
        if entry.get("macro"):
            _notify("fill", "正在执行自定义宏…")
            run_macro(entry["macro"], monitor, delay)
    except Exception as e:
        return FillResult(False, "fill", f"输入失败：{e}")

    _notify("done", f"已为「{entry.get('service')}」填入凭据（匹配度 {best_score:.2f}）")
    return FillResult(True, "done", "填充完成", entry=entry, similarity=best_score)

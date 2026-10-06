# -*- coding: utf-8 -*-
"""
main.py — 程序入口
启动管理界面 + 注册全局热键 Ctrl+Alt+M。
命令行参数：
  --selftest  无界面自检（模型加载/配置/密码库/视觉API连通性）
"""
import os
import sys

# 源码运行：确保 app 目录在 import 路径
if getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.dirname(sys.executable))
else:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import screen


def selftest() -> int:
    """无界面诊断：验证依赖、模型、配置与视觉 API。结果写入 selftest_report.txt。"""
    lines = ["===== AutoFillFace 自检 ====="]
    ok = True

    def out(text):
        lines.append(text)
        print(text)

    try:
        from config import Config, DATA_DIR
        cfg = Config()
        out(f"[ok] 配置目录: {DATA_DIR}")
        out(f"[ok] 配置: model={cfg.get('model')}, 热键={cfg.get('hotkey')}, "
            f"人脸阈值={cfg.get('face_threshold')}")
        out(f"[ok] API Key 已配置: {bool(cfg.get('api_key'))}")
    except Exception as e:
        out(f"[fail] 配置: {e}")
        ok = False

    try:
        from vault import Vault
        v = Vault()
        if os.path.exists(v.path):
            out(f"[ok] 密码库: {len(v.entries)} 条记录 ({os.path.getsize(v.path)} bytes)")
        else:
            out("[ok] 密码库: 空（尚未创建）")
    except Exception as e:
        out(f"[fail] 密码库: {e}")
        ok = False

    try:
        from face_engine import FaceEngine
        fe = FaceEngine(threshold=float(Config().get("face_threshold", 0.55)))
        out("[ok] 人脸引擎: 检测模型 + 识别模型加载成功")
        out(f"[ok] 人脸模板: {len(fe.templates)} 帧")
    except Exception as e:
        out(f"[fail] 人脸引擎: {e}")
        ok = False

    try:
        from vision import VisionClient
        cfg = Config()
        vc = VisionClient(cfg.get("api_key", ""), cfg.get("model", "glm-4.6v-flash"))
        if cfg.get("api_key"):
            out("[ok] 视觉模型配置就绪（自检不发请求，不消耗额度）")
        else:
            out("[warn] 视觉模型 API Key 未配置，屏幕识别不可用（请在界面设置页填写）")
    except Exception as e:
        out(f"[fail] 视觉客户端: {e}")
        ok = False

    try:
        import captcha_ocr
        eng = captcha_ocr._get_engine()
        if eng is not None:
            out("[ok] 验证码引擎: Windows 系统 OCR 加载成功（可识别验证码）")
        else:
            out("[fail] 验证码引擎: Windows 系统 OCR 不可用（系统缺少 OCR 语言包）")
            ok = False
    except Exception as e:
        out(f"[fail] 验证码引擎: {e}")
        ok = False

    try:
        screen.set_dpi_awareness()
        bgr, mon = screen.capture_bgr(max_width=640)
        out(f"[ok] 截屏: {mon['width']}x{mon['height']} -> {bgr.shape[1]}x{bgr.shape[0]}")
    except Exception as e:
        out(f"[fail] 截屏: {e}")
        ok = False

    out("===== 自检" + ("通过" if ok else "存在失败项") + " =====")

    # 写入报告文件（源码/打包两种位置都尝试）
    try:
        if getattr(sys, "frozen", False):
            report_path = os.path.join(os.path.dirname(sys.executable), "selftest_report.txt")
        else:
            report_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "selftest_report.txt")
        with open(report_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        out(f"[ok] 报告已写入: {report_path}")
    except Exception as e:
        out(f"[warn] 报告写入失败: {e}")

    # 打包成 --noconsole 后 print 不可见，改用弹窗展示
    if getattr(sys, "frozen", False):
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            if ok:
                messagebox.showinfo("AutoFillFace 自检", "全部检查通过，程序可正常使用。")
            else:
                messagebox.showerror("AutoFillFace 自检", "存在失败项，详见 selftest_report.txt")
            root.destroy()
        except Exception:
            pass
    return 0 if ok else 1


def main() -> None:
    if "--selftest" in sys.argv:
        sys.exit(selftest())

    screen.set_dpi_awareness()

    from config import Config
    from face_engine import FaceEngine
    from hotkey import HotkeyManager
    from ui import App
    from vault import Vault

    cfg = Config()
    vault = Vault()
    face = FaceEngine(threshold=float(cfg.get("face_threshold", 0.55)))

    app = App(cfg, vault, face)

    hotkey = HotkeyManager(cfg.get("hotkey", "<ctrl>+<alt>+m"), on_trigger=app.handle_hotkey)
    if not hotkey.start():
        app._log("警告：全局热键注册失败（可能被其他程序占用）")

    app._log("程序已启动。密码库/人脸注册请在上方页签操作。")
    app._log("在任何登录页按 Ctrl+Alt+M 即可自动识别并填充。")

    def on_close():
        hotkey.stop()
        app.destroy()

    app.protocol("WM_DELETE_WINDOW", on_close)
    app.mainloop()


if __name__ == "__main__":
    main()

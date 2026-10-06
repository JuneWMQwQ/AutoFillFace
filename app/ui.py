"""
ui.py — CustomTkinter 现代化深色界面
三个页签：密码库 / 人脸 / 设置，底部为运行日志。
"""
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

import cv2
from PIL import Image, ImageDraw, ImageTk
import customtkinter as ctk

import matcher
import screen
from autofill import run_autofill

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

BG = "#1a1a1a"
CARD = "#2b2b2b"
ACCENT = "#1f6aa5"
DANGER = "#a51f2e"


class FaceScanOverlay:
    """人脸识别动画：0.9s 黑圆→胶囊→圆角方块，之后播放 faceid 帧序列（30fps）。"""

    def __init__(self, app):
        self.app = app
        self.tl = None
        self.canvas = None
        self.photo = None
        self.frames = []   # faceid 帧 PhotoImage 列表
        self._frames_loaded = False
        self._faceid_playing = False

    def _resource_dir(self):
        import sys
        if getattr(sys, "frozen", False):
            return sys._MEIPASS
        return os.path.dirname(os.path.abspath(__file__))

    def _load_frames(self):
        import glob
        import numpy as np
        d = os.path.join(self._resource_dir(), "faceid")
        files = sorted(glob.glob(os.path.join(d, "FaceID*.png")))
        try:
            self.app._log(f"查找 faceid 帧：{d}，找到 {len(files)} 个")
        except Exception:
            pass
        R = 24
        # 预生成圆角 mask（200x200）
        mask_img = Image.new("L", (200, 200), 0)
        ImageDraw.Draw(mask_img).rounded_rectangle([0, 0, 199, 199], radius=R, fill=255)
        mask_arr = np.array(mask_img)   # 255=保留, 0=透明
        for f in files:
            img = Image.open(f).convert("RGB")
            img = img.resize((200, 200), Image.LANCZOS)
            arr = np.array(img)
            # 圆角外像素设为透明色 (15,15,15)
            arr[mask_arr == 0] = (15, 15, 15)
            self.frames.append(ImageTk.PhotoImage(Image.fromarray(arr)))

    def _ease(self, t):
        return 1 - (1 - t) ** 3

    def start(self):
        self.app.after(0, self._show)

    def _show(self):
        self.tl = tk.Toplevel(self.app)
        self.tl.overrideredirect(True)
        self.tl.attributes("-topmost", True)
        W = H = 200
        sw = self.tl.winfo_screenwidth()
        sh = self.tl.winfo_screenheight()
        # 屏幕顶部中间（灵动岛位置）
        y = 16
        self.tl.geometry(f"{W}x{H}+{sw//2 - W//2}+{y}")
        # 背景透明：把 #0f0f0f 设为透明色，只显示黑色形状
        self.tl.configure(bg="#0f0f0f")
        try:
            self.tl.attributes("-transparentcolor", "#0f0f0f")
        except Exception:
            pass
        self.canvas = tk.Canvas(self.tl, width=W, height=H, bg="#0f0f0f",
                                highlightthickness=0, bd=0)
        self.canvas.pack()
        self.W = W
        self.H = H
        self.t0 = time.time()
        self.finished = False
        self.tl.lift()
        self.tl.update_idletasks()
        self._tick()
        # 帧加载放到变形动画之后（不阻塞窗口显示）
        if not self._frames_loaded:
            self.tl.after(50, self._do_load_frames)

    def _do_load_frames(self):
        try:
            self._load_frames()
            self._frames_loaded = True
            try:
                self.app._log(f"faceid 帧加载完成：{len(self.frames)} 帧")
            except Exception:
                pass
        except Exception as e:
            try:
                import traceback
                self.app._log(f"faceid 帧加载失败：{e}\n{traceback.format_exc()}")
            except Exception:
                pass

    def _tick(self):
        if self.finished or self.tl is None or not self.tl.winfo_exists():
            return
        el = (time.time() - self.t0) / 0.80
        if el >= 1:
            self._render_shape(180, 150, 24)
            # 变形结束，立刻开始播 faceid 绿色扫描（边识别边播）
            if not self._faceid_playing:
                self._faceid_playing = True
                self._play_sound()
                self._play_frames()
            return
        if el < 0.40:
            p = self._ease(el / 0.40)
            w = 60 + (180 - 60) * p
            h = 60
            cr = 30
        else:
            p = self._ease((el - 0.40) / 0.60)
            w = 180
            h = 60 + (150 - 60) * p
            cr = 30 - (30 - 24) * p
        self._render_shape(w, h, cr)
        self.tl.after(16, self._tick)

    def _play_sound(self):
        try:
            import ctypes
            path = os.path.join(self._resource_dir(), "faceid", "faceid.m4a")
            if not os.path.exists(path):
                return
            # 用 MCI 异步播放 m4a
            ctypes.windll.winmm.mciSendStringW('close snd', None, 0, None)
            ctypes.windll.winmm.mciSendStringW(
                f'open "{path}" type mpegvideo alias snd', None, 0, None)
            ctypes.windll.winmm.mciSendStringW('play snd from 0', None, 0, None)
        except Exception:
            pass

    def on_success(self):
        """人脸验证通过：等 faceid 帧播完后画绿勾（不缩小）。"""
        def _do():
            if self.tl is None:
                return
            # faceid 已经在变形结束后开始播了，等它播完画勾
            delay = max(0, len(self.frames) * 50 - int((time.time() - self.t0) * 1000) - 800)
            self.tl.after(max(0, delay), self._show_check)
        self.app.after(0, _do)

    def _show_check(self):
        if self.finished or self.tl is None:
            return
        self.finished = True
        # faceid 最后两帧是空帧，直接在上面画勾
        cx, cy = self.W // 2, self.H // 2
        self._anim_polyline([(cx-28, cy+2), (cx-8, cy+22), (cx+30, cy-18)],
                            "#50e678", width=8)
        self.tl.after(900, self._close)

    def on_fail(self):
        """人脸验证失败：直接在空帧上画红叉。"""
        def _do():
            self.finished = True
            if self.tl is None:
                return
            # 直接跳到最后一帧（空帧），画红叉
            if hasattr(self, "_img_item") and self._img_item is not None and self.frames:
                self.canvas.itemconfig(self._img_item, image=self.frames[-1])
            cx, cy = self.W // 2, self.H // 2
            self._anim_line((cx-22, cy-22), (cx+22, cy+22), "#ff5a5a", width=8)
            self._anim_line((cx+22, cy-22), (cx-22, cy+22), "#ff5a5a", width=8)
            self.tl.after(900, self._close)
        self.app.after(0, _do)

    def _anim_line(self, p0, p1, color, width=8, steps=6):
        """从 p0 逐步画到 p1。"""
        item = self.canvas.create_line(p0[0], p0[1], p0[0], p0[1],
                                       fill=color, width=width, tags="shape")
        for i in range(1, steps + 1):
            t = i / steps
            ex = p0[0] + (p1[0] - p0[0]) * t
            ey = p0[1] + (p1[1] - p0[1]) * t
            self.tl.after(i * 22, lambda ex=ex, ey=ey:
                          self.canvas.coords(item, p0[0], p0[1], ex, ey))

    def _anim_polyline(self, points, color, width=8, steps=6):
        """逐段画折线（绿勾用）。"""
        item = self.canvas.create_line(points[0][0], points[0][1], points[0][0], points[0][1],
                                       fill=color, width=width, smooth=True, tags="shape")
        delay = 0
        for seg in range(len(points) - 1):
            p0 = points[seg]
            p1 = points[seg + 1]
            for i in range(1, steps + 1):
                t = i / steps
                ex = p0[0] + (p1[0] - p0[0]) * t
                ey = p0[1] + (p1[1] - p0[1]) * t
                coords = [points[0][0], points[0][1]]
                for s in range(1, seg + 1):
                    coords += [points[s][0], points[s][1]]
                coords += [ex, ey]
                delay += 22
                self.tl.after(delay, lambda c=coords:
                              self.canvas.coords(item, *c))

    def _play_frames(self):
        """30fps 播放 faceid 帧序列（圆角遮罩已加好，保持窗口透明）。"""
        if self.finished or self.tl is None:
            return
        if not self.frames:
            # 帧还在加载，100ms 后重试
            self.tl.after(100, self._play_frames)
            return
        self.fi_idx = 0
        self._next_frame()

    def _next_frame(self):
        if self.finished or self.tl is None or not self.tl.winfo_exists():
            return
        if not hasattr(self, "_img_item") or self._img_item is None:
            self._img_item = self.canvas.create_image(0, 0, anchor="nw", image=self.frames[0])
        else:
            self.canvas.itemconfig(self._img_item, image=self.frames[self.fi_idx])
        self.fi_idx += 1
        if self.fi_idx < len(self.frames):
            self.tl.after(50, self._next_frame)
        else:
            self.fi_idx = len(self.frames) - 1

    def _render_shape(self, w, h, cr):
        self.canvas.delete("shape")
        cx = self.W / 2
        x0 = cx - w / 2
        y0 = 20
        x1 = x0 + w
        y1 = y0 + h
        cr = min(cr, w / 2, h / 2)
        self.canvas.create_rectangle(x0+cr, y0, x1-cr, y1, fill="#000", outline="", tags="shape")
        self.canvas.create_rectangle(x0, y0+cr, x0+cr, y1-cr, fill="#000", outline="", tags="shape")
        self.canvas.create_rectangle(x1-cr, y0+cr, x1, y1-cr, fill="#000", outline="", tags="shape")
        self.canvas.create_oval(x0, y0, x0+2*cr, y0+2*cr, fill="#000", outline="", tags="shape")
        self.canvas.create_oval(x1-2*cr, y0, x1, y0+2*cr, fill="#000", outline="", tags="shape")
        self.canvas.create_oval(x0, y1-2*cr, x0+2*cr, y1, fill="#000", outline="", tags="shape")
        self.canvas.create_oval(x1-2*cr, y1-2*cr, x1, y1, fill="#000", outline="", tags="shape")

    def end(self, ok: bool):
        def _do():
            self.finished = True
            if self.tl is None:
                return
            self.canvas.delete("all")
            cx, cy = self.W // 2, 95
            self.canvas.create_rectangle(30, 20, 170, 170, fill="#000", outline="", tags="shape")
            self.canvas.create_oval(30, 20, 78, 68, fill="#000", outline="", tags="shape")
            self.canvas.create_oval(122, 20, 170, 68, fill="#000", outline="", tags="shape")
            self.canvas.create_oval(30, 122, 78, 170, fill="#000", outline="", tags="shape")
            self.canvas.create_oval(122, 122, 170, 170, fill="#000", outline="", tags="shape")
            if ok:
                self._anim_polyline([(cx-28, cy+2), (cx-8, cy+22), (cx+30, cy-18)],
                                    "#50e678", width=8)
            else:
                self._anim_line((cx-22, cy-22), (cx+22, cy+22), "#ff5a5a", width=8)
                self._anim_line((cx+22, cy-22), (cx-22, cy+22), "#ff5a5a", width=8)
            self.tl.after(1000, self._close)
        self.app.after(0, _do)

    def _close(self):
        try:
            self.tl.destroy()
        except Exception:
            pass
        self.tl = None

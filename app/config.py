# -*- coding: utf-8 -*-
"""
config.py — 配置管理
数据目录：EXE 运行时取 EXE 所在目录；源码运行时取项目根目录。
写入的文件：settings.json（明文，仅 API Key / 图床 Token 等非密码信息）
"""
import json
import os
import sys

APP_NAME = "AutoFillFace"


def get_data_dir() -> str:
    """返回数据目录：优先 EXE 所在目录，回退源码项目根目录。"""
    if getattr(sys, "frozen", False):  # PyInstaller 打包
        return os.path.dirname(sys.executable)
    # 源码运行：项目根目录（本文件的上两级）
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


DATA_DIR = get_data_dir()

DEFAULTS = {
    "api_key": "",
    "model": "glm-4.6v-flash",
    "vision_endpoint": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
    "picui_token": "",  # PICUI 图床主 token（可选，请在界面设置页填写，勿提交到仓库）
    "picui_base": "https://v2.picui.cn",
    "hotkey": "<ctrl>+<alt>+m",   # 全局热键
    "face_threshold": 0.45,       # ArcFace 余弦相似度阈值
    "camera_index": 0,            # 摄像头索引
    "vision_enabled": True,       # 是否启用视觉模型识别（关闭则只做人脸+填充）
    "fill_delay": 0.35,           # 点击字段后的等待秒数
}


class Config:
    def __init__(self, path: str | None = None):
        self.path = path or os.path.join(DATA_DIR, "settings.json")
        self.data = dict(DEFAULTS)
        self.load()

    def load(self) -> None:
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                for k, v in saved.items():
                    self.data[k] = v
            except Exception:
                pass  # 配置损坏则用默认

    def save(self) -> None:
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def get(self, key: str, default=None):
        return self.data.get(key, DEFAULTS.get(key) if default is None else default)

    def set(self, key: str, value) -> None:
        self.data[key] = value
        self.save()

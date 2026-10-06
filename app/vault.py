# -*- coding: utf-8 -*-
"""
vault.py — 本地密码库（单用户）

存储：vault.bin，整个 JSON 用 Windows DPAPI（CryptProtectData）加密。
只有当前 Windows 用户账户能解密 —— 不需要主密码，不落明文。
"""
import ctypes
import json
import os
import re
import time
import uuid
from ctypes import wintypes
from difflib import SequenceMatcher

from config import DATA_DIR


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi_encrypt(data: bytes) -> bytes:
    """用当前 Windows 用户 DPAPI 密钥加密数据。"""
    buf = ctypes.create_string_buffer(data)
    blob_in = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DATA_BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(blob_in), "AutoFillFace", None, None, None, 0, ctypes.byref(blob_out)
    ):
        raise OSError(f"DPAPI 加密失败，错误码 {ctypes.get_last_error()}")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _dpapi_decrypt(data: bytes) -> bytes:
    blob_in = _DATA_BLOB(len(data), ctypes.cast(
        ctypes.create_string_buffer(data), ctypes.POINTER(ctypes.c_char)))
    blob_out = _DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    ):
        raise OSError(f"DPAPI 解密失败，错误码 {ctypes.get_last_error()}")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


class Vault:
    """密码库：{entries: [{id, service, url, username, password, note, created, updated}]}"""

    def __init__(self, path: str | None = None):
        self.path = path or os.path.join(DATA_DIR, "vault.bin")
        self.entries: list[dict] = []
        self.load()

    def load(self) -> None:
        if not os.path.exists(self.path):
            self.entries = []
            return
        try:
            raw = _dpapi_decrypt(open(self.path, "rb").read())
            data = json.loads(raw.decode("utf-8"))
            self.entries = data.get("entries", [])
        except Exception:
            self.entries = []  # 无法解密（换用户/损坏）→ 视为空，不覆盖原文件
        # 兼容旧版：snapshot 单值字段迁移为 snapshots 列表
        for e in self.entries:
            if "snapshot" in e and "snapshots" not in e:
                e["snapshots"] = [e["snapshot"]] if e.get("snapshot") else []
                e.pop("snapshot", None)
            e.setdefault("snapshots", [])
            e.setdefault("user_coord", [])
            e.setdefault("pwd_coord", [])
            e.setdefault("macro", "")
            e.setdefault("captcha", None)  # 验证码配置：None 或 {region, coord, order}

    def save(self) -> None:
        data = {"version": 1, "entries": self.entries}
        enc = _dpapi_encrypt(json.dumps(data, ensure_ascii=False).encode("utf-8"))
        with open(self.path, "wb") as f:
            f.write(enc)

    # ---------- CRUD ----------
    def add(self, service: str, url: str, username: str, password: str, note: str = "",
            snapshots: list | None = None,
            user_coord: tuple | None = None, pwd_coord: tuple | None = None,
            macro: str = "", captcha: dict | None = None) -> dict:
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        entry = {
            "id": uuid.uuid4().hex[:12],
            "service": service.strip(),
            "url": url.strip(),
            "username": username.strip(),
            "password": password,
            "note": note.strip(),
            "snapshots": snapshots or [],    # 登录页截图文件列表（多组适配）
            "user_coord": list(user_coord) if user_coord else [],   # [x, y] 比例坐标
            "pwd_coord": list(pwd_coord) if pwd_coord else [],      # [x, y] 比例坐标
            "macro": macro,                # 自定义宏（文本命令）
            "captcha": captcha,            # 验证码配置：{region:{left,top,width,height}, coord:[x,y], order:"before_pwd"|"after_pwd"}
            "created": now,
            "updated": now,
        }
        self.entries.append(entry)
        self.save()
        return entry

    def update(self, entry_id: str, **fields) -> bool:
        for e in self.entries:
            if e["id"] == entry_id:
                for k in ("service", "url", "username", "password", "note",
                          "snapshots", "user_coord", "pwd_coord", "macro", "captcha"):
                    if k in fields:
                        e[k] = fields[k]
                e["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
                self.save()
                return True
        return False

    def delete(self, entry_id: str) -> bool:
        before = len(self.entries)
        self.entries = [e for e in self.entries if e["id"] != entry_id]
        if len(self.entries) != before:
            self.save()
            return True
        return False

    # ---------- 匹配 ----------
    @staticmethod
    def _norm(s: str) -> str:
        """归一化：小写、去空格与常见符号。"""
        return re.sub(r"[\s\.\-_/\\:()\[\]（）·]", "", s.lower())

    @staticmethod
    def _score(entry: dict, text: str) -> float:
        """计算 entry 与识别文本的相关度（0~1）。"""
        text = Vault._norm(text)
        if not text:
            return 0.0
        parts = [Vault._norm(entry.get("service", "")),
                 Vault._norm(entry.get("url", ""))]
        parts = [p for p in parts if p]
        best = 0.0
        for p in parts:
            if not p:
                continue
            if p in text:
                best = max(best, 1.0)
            else:
                best = max(best, SequenceMatcher(None, p, text).ratio())
        # 服务名通常较短，正文较长：全包含时给足权重
        return best

    def search(self, text: str, top_n: int = 5) -> list[tuple[float, dict]]:
        """按识别文本检索，返回 [(score, entry)]，降序。"""
        scored = [(self._score(e, text), e) for e in self.entries]
        scored.sort(key=lambda x: x[0], reverse=True)
        return [(s, e) for s, e in scored if s > 0 and s > scored[0][0] * 0.75][:top_n] if scored else []

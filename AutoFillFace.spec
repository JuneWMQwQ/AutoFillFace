# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files, collect_all

datas = [('assets/models', 'assets/models'), ('faceid', 'faceid')]
datas += collect_data_files('customtkinter')

# 验证码识别：Windows 系统 OCR（winrt），需收集全部 winrt 包的原生 pyd
_wrt_datas, _wrt_binaries, _wrt_hidden = collect_all('winrt')
datas += _wrt_datas
binaries = _wrt_binaries
hiddenimports = _wrt_hidden

# winrt 的 _winrt_*.pyd 是延迟加载的，PyInstaller 分析不到，必须显式收进 binaries
import glob as _glob, os as _os
import importlib.util as _ilu
_winrt_dir = list(_ilu.find_spec("winrt").submodule_search_locations)[0]
for _pyd in _glob.glob(_os.path.join(_winrt_dir, "_winrt*.pyd")):
    binaries.append((_pyd, "winrt"))
for _pkg in ("winrt.windows.media.ocr", "winrt.windows.globalization",
             "winrt.windows.storage.streams", "winrt.windows.foundation",
             "winrt.windows.foundation.collections", "winrt.windows.graphics.imaging"):
    try:
        _d, _b, _h = collect_all(_pkg)
        datas += _d
        binaries = binaries + _b
        hiddenimports = hiddenimports + _h
    except Exception:
        pass


a = Analysis(
    ['app/main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='AutoFillFace',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['AutoFillFace.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='AutoFillFace',
)

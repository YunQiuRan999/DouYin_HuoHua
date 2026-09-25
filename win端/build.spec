# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：抖音续火花控制台（Win 二合一：本地引擎 + 远程客户端）。

构建命令（项目根目录）：
    pyinstaller build.spec --noconfirm

产物：dist/DouyinFireConsole/DouyinFireConsole.exe
运行时：exe 同级自动生成 config.json（本地引擎配置）与 data/（账号/登录态/日志）。
"""
from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = []

# Playwright（含驱动）、python-dotenv、tzdata（时区数据）
for package in ("playwright", "dotenv", "tzdata"):
    data, binary, hidden = collect_all(package)
    datas += data
    binaries += binary
    hiddenimports += hidden

# 白色毛玻璃样式
datas += [("qss", "qss")]

# 与运行无关、被环境预装且被 hooks 意外带入的第三方包（体积优化）
# 注意：certifi/chardet/charset_normalizer 是 requests 依赖，click/h11 是 uvicorn 依赖，不可排除
EXCLUDES = [
    "numpy", "matplotlib", "PIL", "IPython", "jedi", "parso",
    "prompt_toolkit", "pyreadline3", "yaml", "contourpy",
    "kiwisolver", "dateutil", "wcwidth", "pandas", "scipy", "sklearn",
    "sympy", "pytz", "six", "requests-toolbelt", "PyQt5", "tkinter",
    "tests", "pytest",
]

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DouyinFireConsole",
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
    icon="app.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="DouyinFireConsole",
)

# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller 打包配置 —— ACVD 鼠标瞄准

===============================================================================
一、这个文件解决什么问题
===============================================================================

源码模式靠「解释器 + .py 文件」运行。给别人用的版本不能要求对方装 Python，
也不能假设对方把游戏放在和你一样的路径（形如 D:\某人的目录 这类硬编码
在别人机器上是死路）。所以打成一个自包含的 EXE：**路径全部相对 EXE 自己**。

===============================================================================
二、三处不能动的地方（动了就打不出能用的 EXE）
===============================================================================

【1】pathex 必须同时包含项目根和 tools/

    项目根（ROOT）→ 让 `import acvd.xxx` 找得到（acvd 是有 __init__.py 的包）
    tools/        → 让 `import mapper`、`import panel` 找得到
                    它们是**顶层模块**，因为 tools/ 没有 __init__.py，不是包。
                    写成 `import tools.mapper` 会在打包后直接 ImportError。

【2】hiddenimports 必须用 collect_submodules('acvd') 全量收集

    exe_entry.py 对 mapper / panel 是函数体内的延迟 import（为了不让
    启动器一上来就加载整个手柄与内核栈），PyInstaller 的静态分析会漏掉。
    acvd 内部同样有大量按需 import（比如 core 里按 ctrl 选择控制器的分支）。
    显式收集是唯一可靠的办法——漏一个模块，EXE 启动时才炸，报
    ModuleNotFoundError，而且现场已经不在了。

【3】vgamepad 的 ViGEmClient.dll 必须随包携带

    vgamepad 用 ctypes 调 LoadLibrary 加载这个 DLL。PyInstaller 不会分析
    ctypes 的字符串参数，所以不会自动带。漏了它，启动就报
    「找不到 ViGEmClient.dll」，而玩家会以为是自己没装 ViGEmBus。

【4】acvd 的 .py 源码也要打进 dist（本文件最后一段）

    这不是为了给人看，是为了 `acvd/core/guard.py`。分层守卫用 ast 解析
    **源代码文件**来做架构检查（禁止 controller import threading 之类的
    规矩）。PyInstaller 默认只把 .py 编译进 pyz，磁盘上没有 .py 文件，
    guard 就会报「文件不存在（层被删掉了？）」然后拒绝启动引擎。
    所以要把源码原样复制进 dist/_internal/acvd/，让 guard 的
    `os.path.dirname(__file__) + 文件名` 能找到。

===============================================================================
三、想改打包行为，从这里下手
===============================================================================

  想加文件/图标          → EXE(...) 里的 icon=，或往 datas 里 append
  想改程序名             → EXE(name=) 和 COLLECT(name=) 两处要一致
  想要单文件（慢启动）   → exclude_binaries=False 并把 a.binaries/datas 塞进 EXE
                           （不推荐：常驻程序每次启动解压 20+MB，白等几秒）
  想减小体积             → 往 excludes 里加确认用不到的库；
                           但**不要**排除 numpy 除非确定不用 legacy 内核
  想换入口               → Analysis([...]) 里的路径，入口见 tools/exe_entry.py
"""

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, \
    collect_submodules

import glob
import os

# 项目根 = 本 spec 文件所在目录（tools/）的上一级。
# 不要写死绝对路径：这个 spec 要能跟着仓库一起被任何人打包。
#
# ⚠ 用 SPECPATH 而不是 __file__：PyInstaller 6.x 用 exec() 执行 spec，
# __file__ 没有定义，会直接 NameError。SPECPATH 是 PyInstaller 注入的
# 「spec 文件所在目录」，等价且可靠。
ROOT = os.path.dirname(SPECPATH)
TOOLS = SPECPATH

# ---- vgamepad：DLL 是运行时 ctypes 加载的，必须显式收集（见「不能动 3」）----
datas = []
binaries = []
try:
    datas += collect_data_files("vgamepad")
    binaries += collect_dynamic_libs("vgamepad")
except Exception as e:                       # pragma: no cover
    print(f"[spec] 收集 vgamepad 资源失败: {e}")

# ---- acvd 源码：给 core/guard.py 做 AST 检查用（见「不能动 4」）----
# 只收 .py，跳过 __pycache__；保留原始相对目录结构，
# 这样 _internal/acvd/core/types.py 与源码树一一对应，guard 的相对路径逻辑
# 不需要任何改动就能工作。
_src_count = 0
for _py in glob.glob(os.path.join(ROOT, "acvd", "**", "*.py"), recursive=True):
    if "__pycache__" in _py:
        continue
    _rel = os.path.relpath(_py, ROOT)                  # acvd/core/guard.py
    datas.append((_py, os.path.dirname(_rel)))         # → _internal/acvd/core
    _src_count += 1
print(f"[spec] 打包 acvd 源码 {_src_count} 个（供分层守卫做 AST 检查）")

# ---- 顶层入口 + 整个 acvd 包（见「不能动 2」）----
# keymap_ui / find_camera 同样要列进去：它们只被 exe_entry 在函数体内
# 延迟 import，静态分析看不到，漏了会 ImportError：
#   keymap_ui   → ACVD.exe keys 炸
#   find_camera → ACVD.exe locate 炸（首次定位相机地址，不能缺）
hidden = ["mapper", "panel", "keymap_ui", "find_camera",
          "exe_entry"] + collect_submodules("acvd")

a = Analysis(
    [os.path.join(TOOLS, "exe_entry.py")],
    pathex=[ROOT, TOOLS],                    # 见「不能动 1」
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 排除确认用不到的大件。
    # 注意：numpy **不要**排除 —— acvd/autoloc.py（legacy 内核的自动定位）
    # 依赖它。core 内核用不到，但 legacy 回退路径需要，留着更稳。
    excludes=[
        "pandas", "matplotlib", "scipy", "PIL",
        "pytest", "setuptools", "pip",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,          # onedir：二进制放依赖目录，启动不用解压
    name="ACVD",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                      # UPX 压缩会触发杀软误报，宁可大点
    console=True,                   # mapper 需要控制台看日志/报错
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ACVD",
)

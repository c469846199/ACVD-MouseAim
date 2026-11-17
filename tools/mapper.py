"""
tools/mapper.py — 常驻键鼠 → 虚拟手柄映射服务（唯一输入源）

## 架构

    键盘 + 鼠标 ──→ 【本程序】──→ 虚拟手柄(XInput slot0) ──→ Xenia

    * Xenia 只认这一个虚拟手柄，配置一次到位，改键位不用重启 Xenia
    * 带【焦点判断】：只有 Xenia 在前台时才处理输入，你打字不会影响游戏
    * 鼠标闭环：直接在本程序内完成，输出右摇杆
    * 测试通过管道 takeover 接管右摇杆，键盘照常可用

## 焦点

    Xenia 不在前台 → 所有输入忽略，手柄保持中立（不推任何轴）

## 鼠标模型（在 mapper 内实现）

    tgt_yaw/tgt_pitch  目标方向角，来自鼠标 dx/dy 的 atan 反投影，无限累积
    aim_yaw/aim_pitch  从内存读到的火控圈当前角度
    err                两者之差（yaw 做 wrap180），摇杆 = clamp(err / 满杆角度)
    FOV                每帧从内存动态读取（冲刺时引擎会改），f 随之重算

## 无外部准星

    跨进程绘制的准星永远追不上引擎渲染帧（16ms 窗口），高速时必然视觉滞后，
    所以默认不画。游戏原生火控圈就是唯一的准星，摇杆推得准它自然跟上。
    调试需要时可加 --crosshair。

    进场景后由「自动定位」接管：后台静默预扫候选，玩家一动摇杆就用
    那次输入做隐式验证，全程不需要按 F1。F1 保留为手动兜底。

## 键位（默认，可用 --map 指定 JSON）

    A=Space  B=C  X=R  Y=V
    LB=Tab   RB=F  LT=Q/MouseLeft  RT=E/MouseRight  LS=Shift  RS=MouseMiddle
    START=X  BACK=Z
    WASD=左摇杆   方向键=右摇杆（未开鼠标闭环时生效）

## 管道

    status / takeover on|off / r <x> <y> / l <x> <y> / btn <mask>
    lt/rt <v> / center / reload / mouse on|off / recenter
    fov <deg> | fov live | fov info / freeze on|off / quit
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import math
import os
import queue
import sys
import threading
import time

def app_root() -> str:
    """程序根目录 —— 所有配置、日志、缓存都放在这里。

    两种运行方式必须都能用：
      * 源码运行（python tools\\mapper.py）：取 tools/ 的上一级，即项目根目录。
      * 打包成 EXE：取 EXE 自己的目录。

    为什么不能统一用 __file__（PyInstaller 的坑）：
      PyInstaller 会把脚本解压到临时目录（%TEMP%\\_MEIxxxx）再执行，
      此时 __file__ 指向那个临时目录。如果拿它算配置路径，玩家改的
      keymap.json / tune.json 会在进程退出时随临时目录一起被删掉，
      表现为「改完配置重启就丢，而且根本找不到文件在哪」。
      所以冻结运行时必须改用 sys.executable（EXE 自己的位置）。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    here = os.path.abspath(__file__)                 # .../tools/mapper.py
    return os.path.dirname(os.path.dirname(here))    # .../（项目根）


# 源码运行时把项目根目录加进 sys.path，让 `import acvd.*` 能找到包。
# 打包后 acvd 已经随 EXE 打进去了，不需要也不应该再插入源码路径
# （插入了在别人机器上就是个不存在的目录，纯噪音）。
if not getattr(sys, "frozen", False):
    sys.path.insert(0, app_root())

from acvd.focus import FocusWatcher
from acvd.keyhook import KeyHook
from acvd.mousehook import MouseHook
from acvd.pad import VirtualPad, detect_slots
from acvd.pipe import PipeClient, PipeServer
from acvd.rawmouse import RawMouse

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
user32.GetCursorPos.restype = wt.BOOL
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.SetCursorPos.restype = wt.BOOL
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.GetWindowRect.restype = wt.BOOL
user32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.GetClientRect.restype = wt.BOOL
user32.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
user32.ClientToScreen.restype = wt.BOOL
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.IsWindowVisible.restype = wt.BOOL

VK_LBUTTON, VK_RBUTTON, VK_MBUTTON = 0x01, 0x02, 0x04
# 光标差值模式的单帧位移上限（像素）：125Hz 下 400px ≈ 50000px/s
CURSOR_MAX_STEP = 400

# 记录每个键「开始按住」的时间，用于检测卡键
_PRESS_SINCE: dict[int, float] = {}
_UP_STREAK: dict[int, int] = {}     # 连续判定松开的帧数（防误清）

DEFAULT_MAP = {
    "a": 0x20, "b": 0x43, "x": 0x52, "y": 0x56,
    "lb": 0x09,
    "rb": 0x46,                  # RB = F
    # LT/RT：键盘键 + 鼠标左右键（字符串表示鼠标键，走 WH_MOUSE_LL）
    "lt": [0x51, "left"],        # Q 或 鼠标左键
    "rt": [0x45, "right"],       # E 或 鼠标右键
    "ls": [0xA0, 0xA1],          # LeftShift / RightShift
    "rs": "middle",              # RS = 鼠标中键
    "start": 0x58, "back": 0x5A,
    "left_up": 0x57, "left_down": 0x53,
    "left_left": 0x41, "left_right": 0x44,
    "right_up": 0x26, "right_down": 0x28,
    "right_left": 0x25, "right_right": 0x27,
    # 手柄十字键（D-Pad）。**必须登记在这里**：load_user_keymap 只接受
    # 已存在的键，keymap.json 里写 "dpdown" 否则会被当「未知项」丢掉。
    # 默认不绑（空列表），想用就在 keymap.json 里写。
    "dpup": [], "dpdown": [], "dpleft": [], "dpright": [],
}

BTN = {
    "dpup": 0x0001, "dpdown": 0x0002, "dpleft": 0x0004, "dpright": 0x0008,
    "start": 0x0010, "back": 0x0020, "ls": 0x0040, "rs": 0x0080,
    "lb": 0x0100, "rb": 0x0200,
    "a": 0x1000, "b": 0x2000, "x": 0x4000, "y": 0x8000,
}

# 热键必须避开 xenia-edge 硬编码的快捷键（源码 EmulatorWindow::OnKeyDown）：
#   F3 Profiler / F4 GPU trace / F5 清缓存 / F6 显示配置 / F7 性能设置 /
#   F8 调试设置 / F11 全屏 / F12 截图 / Esc 上下文菜单 / Pause 调试器 /
#   Cancel 宿主调试器 / Ctrl+O 打开游戏 / 小键盘 * + -
# Edge 只看 virtual_key，加修饰键躲不掉，只能换键。
HOTKEYS = {
    "calibrate": 0x70,   # F1
    "recenter": 0x71,    # F2
    "pause": 0x78,       # F9（只停鼠标闭环，键盘/按钮照常）
    "halt": 0x79,        # F10（全部暂停：手柄整份中性，键盘也不发）
    "quit": 0x91,        # ScrollLock
}
HOTKEY_LABEL = {
    "calibrate": "重新对齐", "recenter": "归中",
    "pause": "暂停鼠标", "halt": "全部暂停", "quit": "退出",
}

# 用户键位文件（可用 tools\rebind.py 图形化生成，不用手写 JSON）
USER_KEYMAP = os.path.join(
    app_root(), "keymap.json")

# 手感参数文件（死区 / 满杆角度 / 俯仰限位 / 平滑），支持 // 注释
TUNE_PATH = os.path.join(
    app_root(), "tune.json")
TUNING = {
    "dead": 0.0,             # 死区（度）：误差小于它就完全松杆
    "full_stick_angle": 10.0,  # 满杆角度（度）：误差达到它就顶满摇杆
    "pitch_limit": 68.0,     # 火控圈俯仰物理限位（度）
    "smooth": 1.0,           # 输出平滑（1.0=直出，越小越柔）
    "stick_floor": 0.12,     # 反死区：鼠标刚动过时，最小推杆幅度（吃掉游戏死区）
    "curve_gamma": 1.0,      # 响应曲线指数：1.0=线性（旧行为）；
    #                          >1 时「误差越小推力越小」按幂律衰减，
    #                          等效于满杆角随距离动态变大 —— 接近目标时自然
    #                          减速，不靠死区硬截断。试 2.0 / 3.0。
    "input_window": 0.15,    # 反死区生效窗口（秒）：最后一次鼠标移动后的时间
    "speed_boost": 0.30,     # 快甩加成：鼠标速度达到 speed_ref 时额外加的推力
    "speed_ref": 1500.0,     # 参考速度（像素/秒），达到它就算"甩"
    "speed_gamma": 1.5,      # 速度曲线指数：>1 只有真甩动才加成，慢速微调不受影响
    "speed_tau": 0.06,       # 速度平滑时间常数（秒），防止单帧抖动影响加成
    "rate_max": 54.0,        # 满杆时火控圈的角速度（度/秒，实测）
    "ff_gain": 1.0,          # 角速度前馈增益：1.0 = 鼠标转多快准星就转多快
    "game_deadzone": 0.44,   # 游戏自身摇杆死区（实测 ~42%），输出前用来补偿
    "err_dead": 0.25,        # 角度死带（度）：误差小于它就不输出，防止持续爬行
    "dead_px": 16.0,         # 摇杆回中死区（**屏幕像素**）：应设成火控圈半径。
    #                          目标进圈就不用再动，所以「圈内即到位」；
    #                          准星上那个绿圈就是按它画的，二者永远一致。
    #                          同时也是「摇杆真回中」的条件 —— 否则游戏认为
    #                          一直在瞄准，连开镜都进不去（实测）。
    "cross_alpha": 200,      # 准星透明度 0~255（只看视觉，不影响控制）
    "cam_tau": 0.0,          # 逆模型补偿（秒）：把相机 150ms 滞后反推回去。
    #                          0 = 与旧控制律逐位一致；实测机体→相机 116ms，
    #                          建议 0.15。只影响"运动中提前收力"，稳态无偏。
    "cam_lp": 0.04,          # 角速度低通（秒）：相机按 60fps 写、我们 125Hz 读，
    #                          差分有锯齿，靠它平均掉；同时压住读数噪声。
    "button_hold": 0.025,    # 按键最短保持（秒）：必须 > 模拟器采样周期 16.7ms
    "pool_cap": 4000.0,      # 位移积压上限（像素）：超过就丢弃最老的
}


def load_tune(path=TUNE_PATH) -> int:
    """读取 tune.json 覆盖手感参数，返回生效条数。"""
    if not path or not os.path.exists(path):
        return 0
    try:
        data = _load_jsonc(path)
    except Exception as e:
        print(f"! tune.json 解析失败({e})，用内置默认值", flush=True)
        return 0
    n = 0
    for k, v in data.items():
        if k in TUNING:
            try:
                TUNING[k] = float(v)
                n += 1
            except Exception:
                print(f"! tune.json 项 {k!r} 不是数字，已忽略", flush=True)
        else:
            print(f"! tune.json 里有未知项 {k!r}，已忽略", flush=True)
    return n


def wrap180(d):
    return (d + 180.0) % 360.0 - 180.0


def clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


# 丢输入诊断计数器（管道 stats 命令读取，stats reset 清零）
STATS = {
    "frames": 0,        # 输出循环跑了多少帧
    "slow": 0,          # 帧间隔 > 20ms 的次数（线程被卡）
    "max_gap_ms": 0.0,  # 最大帧间隔
    "dxsum": 0,         # 鼠标位移累计（像素）
    "dxmax": 0,         # 单帧最大位移
    "clip": 0,          # 疑似被屏幕边界截断的帧数
    "steps": 0,         # 闭环执行次数
    "padupd": 0,        # 虚拟手柄更新次数
    "latch": 0,         # 被「最短保持」救下来继续按住的次数
    "raw_events": 0,    # Raw Input 收到并累加的原始事件数
    "drop": 0,          # 熔断丢弃的像素数（积压超过 pool_cap）
    "focus_flips": 0,   # focus 状态翻转次数（丢整段输入的真凶指标）
    "unfocused": 0,     # 处于失焦状态的帧数
    "pad_err": 0,       # 手柄输出异常次数
    "released": 0,      # 被强制松开的卡键次数
}


def stat_reset():
    for k in STATS:
        if k.startswith("_"):
            continue          # 内部标志（如 _raw_ok）不能被清零
        STATS[k] = 0


# 绿准星记录器：把闭环内部量按时间写进 CSV，事后不用猜
TRACE_DUMP_PATH = os.path.join(
    app_root(),
    "mapper_stall.txt")

TRACE_PATH = os.path.join(
    app_root(),
    "cross_trace.csv")
REC = {"on": False, "fh": None, "n": 0, "t0": 0.0, "every": 3}


def rec_open() -> str:
    if REC["on"]:
        return f"已在记录（{REC['n']} 行）"
    try:
        new = not os.path.exists(TRACE_PATH) or os.path.getsize(TRACE_PATH) == 0
        REC["fh"] = open(TRACE_PATH, "a", encoding="utf-8", buffering=1)
        if new:
            REC["fh"].write("t,aim_yaw,tgt_yaw,err_y,err_p,f,fov,ch_x,ch_y"
                            ",R_x,R_y,src,focused\n")
        REC["on"] = True
        REC["n"] = 0
        REC["t0"] = time.perf_counter()
        return f"开始记录 -> {TRACE_PATH}"
    except Exception as e:
        REC["on"] = False
        return f"记录启动失败: {e}"


def rec_close() -> str:
    was = REC["on"]
    REC["on"] = False
    try:
        if REC["fh"]:
            REC["fh"].close()
    except Exception:
        pass
    REC["fh"] = None
    return f"已停止记录（本次 {REC['n']} 行）" if was else "本来就没在记录"


class State:
    def __init__(self):
        self.lock = threading.Lock()
        # 键盘
        self.k_lx = self.k_ly = 0.0
        self.k_rx = self.k_ry = 0.0
        self.k_btn = 0
        self.k_lt = self.k_rt = 0.0
        # 管道覆盖
        self.takeover = False
        self.frozen = False       # 冻结摇杆输出（准星照常更新，用于校准验证）
        self.paused = False       # F9：暂停鼠标闭环（键盘/按钮照常）
        self.halt = False         # 面板「全部暂停」：手柄整份输出为中性，
        #                           键盘映射也不发 —— 切去打字时游戏完全不受控
        self.force = False        # F10：强制映射（忽略焦点判断）
        self.o_rx = self.o_ry = 0.0
        self.o_lx = self.o_ly = None
        self.o_btn = None
        self.o_lt = self.o_rt = None
        # 鼠标闭环
        self.mouse_on = False
        self.m_rx = self.m_ry = 0.0
        self.cross = None          # 准星窗口（main 里创建后挂上）
        self.cross_on = False      # 绿色准星开关（管道 cross on|off 实时切换）


def attach_xenia():
    """按前缀连接 Xenia 进程，兼容各版本不同的 exe 名。

    双路查找（顺序不能反）：
      1. 进程枚举（_find_pid_by_prefix）—— 正常情况走这条，最稳
      2. 窗口反查（_find_pid_by_window_title）—— 枚举失败时的退路

    为什么必须有第二条路（打包成 EXE 后实测）：
      PyInstaller 产物里 EnumProcesses / Toolhelp32 / WMI 三条路都只能看到
      19 个左右的进程，xenia_edge 不在其中；但同一进程里 OpenProcess 能成功、
      EnumWindows 能看到 xenia 的窗口。也就是说**枚举列表被裁剪，对象本身可访问**。
      只靠枚举的话，EXE 版会永远「找不到 Xenia」，表现为 base=0、ok=0、不接管。

    注意不要反过来只用窗口反查：窗口不存在时（Xenia 刚启动、无窗口）
    反查会失败，而枚举在那个时刻仍然有效。
    """
    from acvd.winmem import Process
    from acvd.focus import _find_pid_by_prefix, _find_pid_by_window_title

    pid = _find_pid_by_prefix("xenia")
    if pid is None:
        # 传 "xenia" 这个公共开头：Xenia 各分支的窗口标题都以此开头
        # （Xenia-edge / Xenia-canary-netplay），而匹配函数内部会排除
        # 「xenia 后跟空格」的干扰窗口（比如浏览器标题里提到 Xenia）。
        pid = _find_pid_by_window_title("xenia")
    if pid is None:
        raise RuntimeError("找不到 Xenia 进程（进程枚举与窗口标题匹配都失败）")
    return Process.attach(pid)


class ButtonLatch:
    """按键最短保持：把短按延长到 button_hold 秒。

    为什么需要：mapper 以 125Hz 采样，但 xenia-edge 只按自己的帧率
    （~60Hz）去 poll 手柄。一次 10ms 的快速点按，我们的循环能抓到，
    Edge 那一帧可能刚好没采样到，于是这次按键就"丢"了。
    按住至少 40ms 就能保证 Edge 至少看到两次，短按不再丢。
    """

    def __init__(self, hold: float = 0.040):
        self.hold = hold
        self._until: dict = {}

    def _bit(self, key: str, mask_bit: int, active: bool, now: float) -> int:
        if active:
            self._until[key] = now + self.hold
            return mask_bit
        end = self._until.get(key, 0.0)
        if now < end:
            STATS["latch"] += 1
            return mask_bit
        return 0

    def mask(self, value: int) -> int:
        now = time.perf_counter()
        out = 0
        for name, bit in BTN.items():
            out |= self._bit(name, bit, bool(value & bit), now)
        return out

    def axis(self, key: str, value: float) -> float:
        now = time.perf_counter()
        if value >= 0.5:
            self._until[key] = now + self.hold
            return value
        if now < self._until.get(key, 0.0):
            STATS["latch"] += 1
            return 1.0
        return 0.0

    def clear(self):
        self._until.clear()


def _strip_json_comments(s: str) -> str:
    """去掉 // 行注释和 /* */ 块注释，字符串里的内容不动。

    标准 JSON 不允许注释，但键位文件是给人看的，允许写中文说明。
    """
    out = []
    i, n = 0, len(s)
    in_str = False
    while i < n:
        c = s[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(s[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and s[i + 1] == "/":
            while i < n and s[i] not in "\r\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and s[i + 1] == "*":
            i += 2
            while i + 1 < n and not (s[i] == "*" and s[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _load_jsonc(path):
    """读一个允许 // 注释的 JSON 文件。"""
    with open(path, encoding="utf-8") as f:
        return json.loads(_strip_json_comments(f.read()))


def load_user_keymap(path, keymap, hotkeys) -> int:
    """把 keymap.json 合并进 keymap / hotkeys，返回成功应用的条目数。

    文件里可以用人类可读的名字（"space" / "mouse_left" / "f1"），
    也兼容旧的十六进制 VK 数字。热键用 hotkey_calibrate 之类的键名。
    """
    from acvd.keynames import to_vk

    if not path or not os.path.exists(path):
        return 0
    data = _load_jsonc(path)

    def norm(v):
        if isinstance(v, (list, tuple)):
            return [to_vk(x) for x in v]
        return to_vk(v)

    applied = 0
    for k, v in data.items():
        try:
            if k.startswith("hotkey_"):
                name = k[len("hotkey_"):]
                if name in hotkeys:
                    if v is None:
                        continue
                    hotkeys[name] = norm(v)
                    applied += 1
            elif k in keymap:
                keymap[k] = norm(v)
                applied += 1
            else:
                print(f"! keymap.json 里有未知项 {k!r}，已忽略", flush=True)
        except Exception as e:
            print(f"! keymap.json 项 {k!r} 无效（{e}），已忽略", flush=True)
    return applied


def hotkey_help() -> str:
    from acvd.keynames import vk_name
    order = [("calibrate", "重新对齐"), ("recenter", "归中"),
             ("pause", "暂停鼠标"), ("halt", "全部暂停"), ("quit", "退出")]
    parts = [f"{vk_name(HOTKEYS[k])}={label}" for k, label in order if k in HOTKEYS]
    return "  ".join(parts)


class Tee:
    """把 stdout 同时写到控制台和日志文件（控制台不会空白，日志可回溯）。"""

    def __init__(self, path, stream):
        self._f = open(path, "a", encoding="utf-8", buffering=1)
        self._s = stream

    def write(self, s):
        for x in (self._s, self._f):
            try:
                x.write(s)
            except Exception:
                pass
        return len(s)

    def flush(self):
        for x in (self._s, self._f):
            try:
                x.flush()
            except Exception:
                pass

    def isatty(self):
        try:
            return self._s.isatty()
        except Exception:
            return False


SINGLE_INSTANCE_MUTEX = "Global\\ACVD_Mapper_Single_Instance"
_mutex_handle = None


def acquire_single_instance() -> bool:
    """用命名互斥量保证只有一个 mapper。已存在则返回 False。"""
    global _mutex_handle
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    h = kernel32.CreateMutexW(None, 0, SINGLE_INSTANCE_MUTEX)
    err = ctypes.get_last_error()
    if not h:
        return True          # 创建失败就别拦，免得完全起不来
    _mutex_handle = h
    ERROR_ALREADY_EXISTS = 183
    if err == ERROR_ALREADY_EXISTS:
        return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--map", default=None, help="额外键位文件（可选，默认读 keymap.json）")
    ap.add_argument("--hz", type=float, default=125)
    ap.add_argument("--mouse", action="store_true",
                    help="（已废弃，保留兼容）鼠标闭环改为进场景后按 F1 启用")
    ap.add_argument("--fov", type=float, default=None,
                    help="兜底水平 FOV（度）。正常由内存动态读取，读不到才用它")
    ap.add_argument("--crosshair", action="store_true",
                    help="启动时显示绿色准星（也可用管道命令 cross on|off 切换）")
    # --engine 参数保留但已无选项：旧内核（扫描火控圈那套）已彻底移除，
    # 现在只有四层内核这一条路径。保留这个参数是为了让旧脚本/快捷方式
    # 里的 `--engine core` 不至于报「unrecognized arguments」。
    ap.add_argument("--engine", choices=("core",), default="core",
                    help="瞄准内核（只有一个：core）")
    a = ap.parse_args()

    if not acquire_single_instance():
        print("! 已经有一个 mapper 在运行了（管道 acvd_pad 被它占用）。",
              flush=True)
        print("  请先关掉旧的（ScrollLock 或任务管理器），再启动这个。",
              flush=True)
        return 2


    # 日志同时落盘，方便事后回溯（控制台依旧正常显示）
    try:
        log_path = os.path.join(app_root(), "mapper.out.log")
        sys.stdout = Tee(log_path, sys.stdout)
        sys.stderr = Tee(log_path, sys.stderr)
        print(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} mapper 启动 =====",
              flush=True)
    except Exception:
        pass

    keymap = dict(DEFAULT_MAP)
    nt = load_tune()
    if nt:
        print(f"手感: {TUNE_PATH}（{nt} 项）dead={TUNING['dead']} "
              f"fullstick={TUNING['full_stick_angle']}", flush=True)
    else:
        print(f"手感: 默认 dead={TUNING['dead']} "
              f"fullstick={TUNING['full_stick_angle']}"
              f"（想调就建 tune.json，或管道命令 dead 0.2）", flush=True)
    n = load_user_keymap(USER_KEYMAP, keymap, HOTKEYS)
    if n:
        print(f"键位: {USER_KEYMAP}（{n} 项）", flush=True)
    else:
        print(f"键位: 默认（没有 {os.path.basename(USER_KEYMAP)}，"
              f"跑 python tools\\rebind.py 可图形化改键）", flush=True)
    if a.map and os.path.exists(a.map):
        keymap.update(_load_jsonc(a.map))
        print(f"额外键位: {a.map}", flush=True)

    print("创建虚拟手柄 ...", flush=True)
    raw = RawMouse()
    raw_ok = raw.start()
    STATS["_raw_ok"] = raw_ok
    print(f"Raw Input 累积器: {'已注册' if raw_ok else '注册失败（退回光标差值）'} "
          f"hwnd=0x{(raw.hwnd or 0):X}", flush=True)
    pad = VirtualPad()
    st = State()
    st.cross_on = True    # 绿准星默认常开：它是诊断仪表，不是装饰
    fw = FocusWatcher(exe_prefix="xenia")     # 兼容 xenia_canary 与 xenia_canary_new_xma_alpha_v2
    fw.is_focused()          # 预热
    print(f"ready  XInput 槽位 {detect_slots()}  Xenia hwnd {hex(fw.hwnd_of()) or None}",
          flush=True)
    print(f"  热键: {hotkey_help()}", flush=True)
    print("        （F7/F8/F11/F12 被 xenia-edge 占用：F7 性能面板 F8 调试设置 "
          "F11 全屏 F12 截图）", flush=True)
    print("  鼠标闭环：固定相机反馈，进关卡后自动接管", flush=True)
    print("  改键位: python tools\\rebind.py   （写入 keymap.json）", flush=True)

    # 只吞 mapper 自己的热键（F1/F2/F9/F10/ScrollLock），键位映射用的
    # WASD 那些照常透传。否则 F10 会漏给前台窗口，触发「激活菜单栏」，
    # 之后每个按键都变成无效菜单项、系统逐键响铃。
    hook = KeyHook(swallow=lambda: set(HOTKEYS.values()))
    hook.start()
    mhook = MouseHook()
    if not mhook.start():
        print("! 鼠标钩子启动失败（LT/RT/RS 的鼠标键不可用）", flush=True)

    mouse = None
    proc = None
    pad_lock = threading.Lock()
    pad_q: queue.Queue = queue.Queue(maxsize=1)

    def pad_worker():
        """独立线程做真正的手柄输出：ViGEm 卡住也只卡这里，不影响主循环。

        同时承担「断流保险」：主循环一旦不再喂数据（换关卡时 ReadProcessMemory
        可能阻塞数秒），虚拟手柄会永久保持最后一帧的推力——那就是机甲在新
        场景出生瞬间自己乱飞的成因。所以这里自己盯着数据流，超过 150ms 没有
        新帧就把右摇杆强制回中。它不读内存、不依赖任何锁，主循环冻住它照跑。
        """
        nonlocal pad
        last_feed = time.perf_counter()
        cut = False
        while running:
            try:
                item = pad_q.get(timeout=0.02)
            except queue.Empty:
                if not cut and time.perf_counter() - last_feed > 0.15:
                    cut = True
                    try:
                        with pad_lock:
                            pad.right(0.0, 0.0)
                            pad.update()
                        STATS["cut"] = STATS.get("cut", 0) + 1
                        print("! 主循环 150ms 没有输出 → 右摇杆已强制回中"
                              "（防止跨场景带推力）", flush=True)
                    except Exception:
                        pass
                continue
            if item is None:
                break
            cut = False
            last_feed = time.perf_counter()
            lx, ly, rx, ry, lt, rt, btn = item
            try:
                with pad_lock:
                    pad.left(lx, ly)
                    pad.right(rx, ry)
                    pad.triggers(lt, rt)
                    pad.set_buttons(btn)
                    pad.update()
                STATS["padupd"] += 1
                STATS["_pad_fail"] = 0
            except Exception as e:
                fails = STATS.get("_pad_fail", 0) + 1
                STATS["_pad_fail"] = fails
                STATS["pad_err"] = STATS.get("pad_err", 0) + 1
                if fails <= 3 or fails % 100 == 0:
                    print(f"! 手柄输出异常(第{fails}次): {e}", flush=True)
                if fails >= 20:
                    print("! 手柄连续异常，重建虚拟手柄 ...", flush=True)
                    try:
                        with pad_lock:
                            pad.close()
                            pad = VirtualPad()
                        print("✓ 虚拟手柄已重建（Edge 里需重新确认槽位）", flush=True)
                    except Exception as e2:
                        print(f"! 重建失败: {e2}", flush=True)
                    STATS["_pad_fail"] = 0
    running = True
    auto = None      # 自动定位线程（main 末尾创建；管道命令可能先到，先占位）
    engine = None    # 四层内核（main 末尾创建）
    eproxy = None    # 把内核包装成旧 MouseAim 的接口，前端代码不用改

    def handle(line: str) -> str:
        nonlocal running, mouse
        p = line.split()
        if not p:
            return "empty"
        c = p[0].lower()
        try:
            if c == "dzscan":
                # 在后台线程里跑，避免阻塞管道
                def _dzscan():
                    if mouse is None:
                        print("[死区] 内核实例未就绪", flush=True)
                        return
                    mags = [0.03, 0.05, 0.07, 0.09, 0.12, 0.15,
                            0.18, 0.22, 0.28, 0.35, 0.50, 0.70, 1.00]
                    rows = []

                    def yaw_now():
                        v = mouse._read()
                        return None if v is None else v[0]

                    with st.lock:
                        st.paused = False
                        st.takeover = True
                        st.o_rx = st.o_ry = 0.0
                    time.sleep(0.4)
                    print("\n[死区] 开始测量（视角会转动）", flush=True)
                    for mag in mags:
                        with st.lock:
                            st.o_rx, st.o_ry = 0.0, 0.0
                        time.sleep(0.35)
                        a = yaw_now()
                        with st.lock:
                            st.o_rx, st.o_ry = mag, 0.0
                        time.sleep(0.60)
                        b = yaw_now()
                        with st.lock:
                            st.o_rx, st.o_ry = 0.0, 0.0
                        time.sleep(0.30)
                        if a is None or b is None:
                            print(f"[死区]   杆量 {mag:.2f} 读取失败", flush=True)
                            continue
                        rate = (b - a) / 0.60
                        rows.append((mag, rate))
                        print(f"[死区]   杆量 {mag:4.2f} -> {rate:+7.2f} °/s",
                              flush=True)
                    with st.lock:
                        st.takeover = False
                        st.o_rx = st.o_ry = 0.0
                    full = next((r for m, r in rows if m >= 0.99), None)
                    if full and abs(full) > 0.2:
                        print(f"[死区] 满杆 {full:+.1f}°/s", flush=True)
                        dead = None
                        for m, r in rows:
                            pct = 100.0 * r / full
                            tag = ""
                            if abs(r) < 0.5 and dead is None:
                                dead = m
                                tag = "  ← 不动"
                            print(f"[死区]   {m:4.2f} -> {pct:5.1f}%{tag}",
                                  flush=True)
                        if dead is not None:
                            print(f"[死区] 结论：游戏死区约 {dead*100:.0f}% 行程",
                                  flush=True)
                    print("[死区] 测量结束", flush=True)

                threading.Thread(target=_dzscan, daemon=True).start()
                return "dzscan started"

            if c == "status":
                with st.lock:
                    # 输出源与接管状态：**以 engine 为准**。
                    #
                    # 这里修过一个大坑：以前用 `st.takeover` 决定显示哪个摇杆
                    # 变量，但 st.takeover 只在初始化时置 False，之后再没被
                    # 改过 —— 于是 status 永远显示 "takeover=off" 和
                    # 恒为 0 的 m_rx/m_ry。排查时据此判断「没接管、不推杆」
                    # 全都是错的，白绕一大圈（实际是 halt=on 拦掉的）。
                    #
                    # 内核模式下真实的输出是 engine.stick()（它读看门狗），
                    # 真实的接管状态是 engine.is_tracking()。
                    if engine is not None:
                        rx, ry = engine.stick()
                        tracking = engine.is_tracking()
                    elif st.takeover:
                        rx, ry = st.o_rx, st.o_ry
                        tracking = True
                    elif st.mouse_on:
                        rx, ry = st.m_rx, st.m_ry
                        tracking = False
                    else:
                        rx, ry = st.k_rx, st.k_ry
                        tracking = False
                    chx = chy = 0.0
                    errd = fovd = ""
                    if mouse is not None:
                        chx, chy = mouse.ch_x, mouse.ch_y
                        errd = f" err=({mouse.err_y:+.2f},{mouse.err_p:+.2f})"
                        if mouse.aim_cont is not None and mouse.tgt_yaw is not None:
                            errd += (f" yaw=(aim {mouse.aim_cont:+.0f}"
                                     f" tgt {mouse.tgt_yaw:+.0f}"
                                     f" resync {mouse.resync})")
                        fovd = (f" fov={mouse.FOV:.1f} hfov={mouse.hfov:.1f}"
                                f" f={mouse.f:.0f} live={'1' if mouse.fov_live else '0'}")
                    winpos = ""
                    if st.cross is not None and st.cross.hwnd:
                        rr = wt.RECT()
                        vis = user32.IsWindowVisible(st.cross.hwnd)
                        if user32.GetWindowRect(st.cross.hwnd, ctypes.byref(rr)):
                            winpos = (f" win=({rr.left},{rr.top}) vis={vis} "
                                      f"raw={st.cross.raw_ok}/{st.cross.raw_total}")
                        else:
                            winpos = " win=ERR"
                    else:
                        winpos = " win=None"
                    # 位移通道真实状态（不要和准星窗口的 raw 混淆）
                    src = "cursor"
                    if mouse is not None and mouse.raw is not None:
                        if mouse.raw_ever and mouse.raw_dead <= 600:
                            src = "raw"
                    winpos += (f" insrc={src}"
                               f" rawok={1 if raw_ok else 0}"
                               f" rawever={1 if (mouse and mouse.raw_ever) else 0}")
                    autod = ""
                    if engine is not None:
                        autod = " " + engine.status()
                    elif auto is not None:
                        autod = " " + auto.status()
                    return (f"focused={fw.is_focused()} "
                            f"L=({st.k_lx:+.2f},{st.k_ly:+.2f}) R=({rx:+.3f},{ry:+.3f}) "
                            f"btn=0x{st.k_btn:04X} lt={st.k_lt:.2f} rt={st.k_rt:.2f} "
                            f"tracking={'on' if tracking else 'off'} "
                            f"takeover={'on' if st.takeover else 'off'} "
                            f"paused={'on' if st.paused else 'off'} "
                            f"halt={'on' if st.halt else 'off'} "
                            f"force={'on' if st.force else 'off'} "
                            f"mouse={'on' if st.mouse_on else 'off'} "
                            f"ch=({chx:.0f},{chy:.0f}){errd}{fovd}{winpos}{autod}")
            if c == "auto":
                arg = p[1].lower() if len(p) > 1 else "status"
                if engine is not None:
                    if arg == "off":
                        engine.set_enabled(False)
                        return "内核已关闭（F1 仍可手动重定位）"
                    if arg == "on":
                        engine.set_enabled(True)
                        return "内核已恢复"
                    return engine.status()
                if auto is None:
                    return "auto 未启用（本版本已无自动定位，用 calib 重新对齐）"
                if arg == "off":
                    auto.suspend("off")
                    return "auto off（手动 F1 仍可用）"
                if arg == "on":
                    auto.resume()
                    return "auto on"
                return auto.status()
            if c == "takeover":
                with st.lock:
                    st.takeover = (len(p) > 1 and p[1].lower() == "on")
                    if not st.takeover:
                        st.o_rx = st.o_ry = 0.0
                return f"takeover={'on' if st.takeover else 'off'}"
            if c == "rhold":
                # 标定用：推杆保持 N 秒后回中（自动 takeover）
                x, y, secs = float(p[1]), float(p[2]), float(p[3])
                with st.lock:
                    st.takeover = True
                    st.o_rx, st.o_ry = x, y
                time.sleep(secs)
                with st.lock:
                    st.o_rx = st.o_ry = 0.0
                return f"rhold {x} {y} {secs}s"
            if c == "r":
                with st.lock:
                    st.o_rx, st.o_ry = float(p[1]), float(p[2])
                return f"R=({st.o_rx:+.3f},{st.o_ry:+.3f})"
            if c == "l":
                with st.lock:
                    st.o_lx, st.o_ly = float(p[1]), float(p[2])
                return f"L=({st.o_lx:+.3f},{st.o_ly:+.3f})"
            if c == "btn":
                with st.lock:
                    st.o_btn = int(p[1], 0)
                    # 管道发按键自动绕过焦点判断，否则后台时按键会被清零
                    st.takeover = True
                return f"btn=0x{st.o_btn:04X}"
            if c == "lt":
                with st.lock:
                    st.o_lt = float(p[1])
                return f"LT={st.o_lt:.2f}"
            if c == "rt":
                with st.lock:
                    st.o_rt = float(p[1])
                return f"RT={st.o_rt:.2f}"
            if c == "mouse":
                # 旧内核时代的「手动开启鼠标闭环」。四层内核自己管状态机
                # （看 is_tracking()），不需要外部创建 MouseAim，所以这个
                # 命令退化成只报告当前状态，不再重建任何对象。
                arg = p[1].lower() if len(p) > 1 else ""
                with st.lock:
                    if arg == "off":
                        st.mouse_on = False
                    elif engine is not None:
                        st.mouse_on = engine.is_tracking()
                    else:
                        st.mouse_on = bool(mouse is not None)
                return f"mouse={'on' if st.mouse_on else 'off'}"
            if c == "fov":
                if mouse is None:
                    return "mouse off"
                arg = p[1].lower() if len(p) > 1 else ""
                if arg in ("live", "auto", "mem"):
                    mouse.set_fov_live()
                    return (f"fov=live value={mouse.FOV:.2f} hfov={mouse.hfov:.1f} "
                            f"f={mouse.f:.0f}")
                if arg in ("info", "?"):
                    src = mouse.fovsrc
                    info = src.stats() if src is not None else "(no source)"
                    return (f"fov={mouse.FOV:.2f} hfov={mouse.hfov:.1f} f={mouse.f:.0f} "
                            f"live={'1' if mouse.fov_live else '0'} "
                            f"override={src.override if src is not None else None} "
                            f"{info}")
                v = float(arg)
                f = mouse.set_fov(v)
                return f"fov={v} (fixed) hfov={mouse.hfov:.1f} f={f:.1f}"
            if c in ("dead", "fullstick", "pitchlim", "smooth", "floor", "curve",
                     "inputwindow", "speedboost", "speedref", "speedtau", "speedgamma",
                     "ffgain", "ratemax", "gamedz", "errdead", "deadpx",
                     "hold", "cap", "crossalpha", "camtau", "camlp"):
                if not p or len(p) < 2:
                    return " ".join(f"{k}={v}" for k, v in TUNING.items())
                key = {"dead": "dead", "fullstick": "full_stick_angle",
                       "pitchlim": "pitch_limit", "smooth": "smooth",
                       "floor": "stick_floor", "curve": "curve_gamma",
                       "inputwindow": "input_window",
                       "speedboost": "speed_boost", "speedref": "speed_ref",
                       "speedtau": "speed_tau", "speedgamma": "speed_gamma",
                       "ffgain": "ff_gain", "ratemax": "rate_max", "gamedz": "game_deadzone",
                       "errdead": "err_dead", "deadpx": "dead_px",
                       "crossalpha": "cross_alpha",
                       "camtau": "cam_tau", "camlp": "cam_lp",
                       "hold": "button_hold", "cap": "pool_cap"}[c]
                TUNING[key] = float(p[1])
                if c == "hold":
                    latch.hold = TUNING["button_hold"]
                # 逆模型参数不走 Cfg：它们是控制律的时间常数，改完直接生效。
                # tau=0 就是旧行为，所以调它等于拨一个音量旋钮，不换逻辑。
                if c in ("camtau", "camlp") and engine is not None:
                    engine.set_v2(tau=TUNING["cam_tau"], lp=TUNING["cam_lp"])
                    return (f"cam_tau={TUNING['cam_tau']:.3f}s "
                            f"cam_lp={TUNING['cam_lp']:.3f}s "
                            f"（0 = 不补偿，等同旧控制律）")
                attr = {"dead": "DEAD", "fullstick": "FULL_STICK_ANGLE",
                        "pitchlim": "PITCH_LIMIT", "smooth": "SMOOTH"}.get(c)
                if mouse is not None and attr:
                    setattr(mouse, attr, TUNING[key])
                push_cfg()        # 内核参数热更新，不重启也能试手感
                return " ".join(f"{k}={v}" for k, v in TUNING.items())
            if c == "cross":
                arg = p[1].lower() if len(p) > 1 else ""
                if arg == "on":
                    st.cross_on = True
                    if st.cross is None:
                        try:
                            from acvd.overlay import Crosshair
                            cross = Crosshair()
                            if cross.start():
                                st.cross = cross
                                if mouse is not None:
                                    mouse.cross = cross
                                if getattr(st, "hider", None) is None:
                                    from acvd.cursor import CursorHider
                                    h = CursorHider()
                                    h.start()
                                    st.hider = h
                        except Exception as e:
                            return f"cross on failed: {e}"
                else:
                    st.cross_on = False
                    if st.cross is not None and st.cross.hwnd:
                        try:
                            user32.ShowWindow(st.cross.hwnd, 0)
                        except Exception:
                            pass
                return f"cross={'on' if st.cross_on else 'off'}"
            if c == "calib":
                # 旧内核这里是「扫描火控圈地址 + 推杆验证」，四层内核没有
                # 地址可扫（相机地址固定），语义收敛为「把准星重新锚到当前
                # 视角」。换关后世界角原点变了，按一下即可。
                if engine is not None:
                    engine.request_anchor()
                    return "已重新对齐坐标"
                return "内核未启动"
            if c in ("pause", "force"):
                arg = p[1].lower() if len(p) > 1 else "toggle"
                with st.lock:
                    cur = st.paused if c == "pause" else st.force
                    new = (not cur) if arg == "toggle" else (arg in ("on", "1", "true"))
                    if c == "pause":
                        st.paused = new
                    else:
                        st.force = new
                return f"{c}={'on' if new else 'off'}"
            if c == "halt":
                # 面板「全部暂停」：比 F9 更彻底 —— 手柄整份置中性、键盘映射
                # 也不发。切去聊天/桌面打字时游戏完全不响应，回来一键恢复。
                arg = p[1].lower() if len(p) > 1 else "toggle"
                with st.lock:
                    new = ((not st.halt) if arg == "toggle"
                           else (arg in ("on", "1", "true")))
                    st.halt = new
                    if new:
                        st.k_lx = st.k_ly = st.k_rx = st.k_ry = 0.0
                        st.k_lt = st.k_rt = 0.0
                        st.k_btn = 0
                        st.o_rx = st.o_ry = 0.0
                return f"halt={'on' if new else 'off'}"
            if c == "recal":
                # 同上：没有地址缓存需要清，缓存清理是针对火控圈扫描的。
                # 保留这条命令只为不让人按了没反应。
                if engine is not None:
                    engine.request_anchor()
                    return "已强制重新对齐"
                return "内核未启动"
            if c == "rec":
                arg = p[1].lower() if len(p) > 1 else ""
                if arg == "on":
                    return rec_open()
                if arg == "off":
                    return rec_close()
                return (f"rec={'on' if REC['on'] else 'off'} rows={REC['n']} "
                        f"path={TRACE_PATH}")
            if c == "watch":
                # 诊断「绿准星乱跑」：连续采样看 f / err / 准星坐标 的波动范围。
                # 用法: watch 3
                if mouse is None:
                    return "mouse off"
                secs = float(p[1]) if len(p) > 1 else 3.0
                n = max(3, int(secs / 0.1))
                fs, eys, chx, chy = [], [], [], []
                for _ in range(n):
                    fs.append(mouse.f)
                    eys.append(mouse.err_y)
                    chx.append(mouse.ch_x)
                    chy.append(mouse.ch_y)
                    time.sleep(0.1)
                return (f"watch {secs:.1f}s n={n}  "
                        f"f={min(fs):.0f}~{max(fs):.0f}(Δ{max(fs)-min(fs):.0f})  "
                        f"errY={min(eys):+.1f}~{max(eys):+.1f}  "
                        f"chX={min(chx):.0f}~{max(chx):.0f}(Δ{max(chx)-min(chx):.0f})  "
                        f"chY={min(chy):.0f}~{max(chy):.0f}  "
                        f"fov={mouse.FOV:.1f} live={'1' if mouse.fov_live else '0'}")
            if c == "drift":
                # 诊断「准星自己动」：N 秒内完全不碰鼠标，看火控圈角度飘多少，
                # 同时报告这期间有没有收到位移事件。用法: drift 3
                if mouse is None:
                    return "mouse off"
                secs = float(p[1]) if len(p) > 1 else 3.0
                v0 = mouse._read()
                if v0 is None:
                    return "read fail"
                STATS["raw_events"] = 0
                dx0 = STATS["dxsum"]
                time.sleep(secs)
                v1 = mouse._read()
                if v1 is None:
                    return "read fail after"
                return (f"drift {secs:.1f}s  Δyaw={wrap180(v1[0] - v0[0]):+.3f}° "
                        f"Δpitch={v1[1] - v0[1]:+.3f}°  "
                        f"raw_events={STATS['raw_events']} "
                        f"dx={STATS['dxsum'] - dx0}  "
                        f"err=({mouse.err_y:+.2f},{mouse.err_p:+.2f})")
            if c == "focus":
                if raw_ok:
                    fg = user32.GetForegroundWindow()
                    wpid = wt.DWORD(0)
                    if fg:
                        user32.GetWindowThreadProcessId(fg, ctypes.byref(wpid))
                    buf = ctypes.create_unicode_buffer(128)
                    if fg:
                        user32.GetWindowTextW(fg, buf, 128)
                    return (f"mapper_pid={fw.pid} mapper_hwnd=0x{(fw.hwnd or 0):X} "
                            f"foreground=0x{(fg or 0):X} fg_pid={wpid.value} "
                            f"focused={fw.is_focused()} title={buf.value[:40]!r}")
                return "raw not ok"
            if c == "rawtest":
                # 诊断：取走 Raw 池并报告，用来确认 WM_INPUT 到底有没有到
                dx, dy, n, absn = raw.drain()
                st_raw = raw.stats()
                return (f"raw drained dx={dx} dy={dy} events={n} absolute={absn} "
                        f"wm_input={st_raw.get('wm_input')} bad_read={st_raw.get('bad_read')} "
                        f"not_mouse={st_raw.get('not_mouse')} "
                        f"zero_move={st_raw.get('zero_move')} "
                        f"parse_err={st_raw.get('parse_err')} "
                        f"last_err={st_raw.get('last_err')!r} "
                        f"hwnd=0x{(raw.hwnd or 0):X} ok={raw.ok}")
            if c == "stats":
                if len(p) > 1 and p[1].lower() == "reset":
                    stat_reset()
                    return "stats reset"
                f = STATS["frames"] or 1
                avg_ms = 1000.0 * 1.0 / a.hz
                return (f"frames={STATS['frames']} steps={STATS['steps']} "
                        f"padupd={STATS['padupd']} slow20ms={STATS['slow']} "
                        f"max_gap={STATS['max_gap_ms']:.1f}ms "
                        f"raw={STATS['raw_events']} drop={STATS['drop']} "
                        f"dxsum={STATS['dxsum']} dxmax={STATS['dxmax']} clip={STATS['clip']} "
                        f"latch={STATS['latch']} released={STATS.get('released', 0)} hold={TUNING['button_hold']*1000:.0f}ms "
                        f"focus_flips={STATS['focus_flips']} unfocused={STATS['unfocused']} "
                        f"raw_ok={STATS.get('_raw_ok')} "
                        f"target={avg_ms:.0f}ms")
            if c == "aim":
                # 诊断用：精确读当前火控圈角度（比 status 的整数角度细）
                if mouse is None:
                    return "mouse off"
                v = mouse._read()
                if v is None:
                    return "read fail"
                return (f"addr=0x{mouse.addr:012X} yaw={v[0]:+.4f} pitch={v[1]:+.4f} "
                        f"cont={mouse.aim_cont if mouse.aim_cont is not None else v[0]:+.4f} "
                        f"err=({mouse.err_y:+.3f},{mouse.err_p:+.3f}) "
                        f"R=({mouse.out_x:+.3f},{mouse.out_y:+.3f})")
            if c == "freeze":
                with st.lock:
                    st.frozen = (len(p) > 1 and p[1].lower() == "on")
                return f"freeze={'on' if st.frozen else 'off'}"
            if c == "recenter":
                if mouse:
                    mouse.recenter()
                return "recentered"
            if c == "center":
                with st.lock:
                    st.o_rx = st.o_ry = 0.0
                    st.o_lx = st.o_ly = None
                    st.o_btn = None
                    st.o_lt = st.o_rt = None
                    st.takeover = False
                return "centered"
            if c == "pin":
                if engine is None:
                    return "没有内核（旧引擎不支持）"
                arg = p[1].lower() if len(p) > 1 else "off"
                if arg in ("off", "0", "none"):
                    engine.set_hold(None)
                    return "pin off（已恢复闭环）"
                ry = float(p[2]) if len(p) > 2 else 0.0
                engine.set_hold(float(arg), ry)
                return f"pin rx={float(arg):.4f} ry={ry:.4f}（绕开闭环）"
            if c in ("dump", "trace"):
                if engine is None:
                    return "没有内核黑匣子（旧引擎）"
                n = engine.trace_len()
                if n == 0:
                    return "黑匣子是空的（还没开始闭环）"
                path = engine.dump_trace(os.path.join(
                    app_root(),
                    time.strftime("trace_%Y%m%d_%H%M%S.csv")))
                return f"已导出 {n} 帧（125Hz ≈ {n / 125:.0f}s） → {path}"
            if c == "dumpn":
                if engine is None:
                    return "没有内核黑匣子（旧引擎）"
                return str(engine.trace_len())
            if c == "reload":
                n = load_user_keymap(USER_KEYMAP, keymap, HOTKEYS)
                if a.map and os.path.exists(a.map):
                    keymap.update(_load_jsonc(a.map))
                    n += 1
                nt = load_tune()
                latch.hold = TUNING["button_hold"]
                if mouse is not None:
                    mouse.DEAD = TUNING["dead"]
                    mouse.FULL_STICK_ANGLE = TUNING["full_stick_angle"]
                    mouse.PITCH_LIMIT = TUNING["pitch_limit"]
                    mouse.SMOOTH = TUNING["smooth"]
                push_cfg()
                if engine is not None:
                    engine.set_v2(tau=TUNING.get("cam_tau", 0.0),
                                  lp=TUNING.get("cam_lp", 0.04))
                return (f"reloaded 键位{n}项 手感{nt}项  热键: {hotkey_help()}  "
                        f"dead={TUNING['dead']} fullstick={TUNING['full_stick_angle']}"
                        f" deadpx={TUNING.get('dead_px')}")
            if c in ("quit", "exit", "q"):
                running = False
                return "bye"
            return f"unknown: {c}"
        except Exception as e:
            return f"error: {e}"

    srv = PipeServer("acvd_pad", handle)
    srv.start()

    interval = 1.0 / a.hz

    relocating = [False]
    PREV_ADDR = [None]    # 旧内核遗留占位（火控圈地址概念已废弃）
    PREV_FOV = [None]     # 上次扫到的 FOV 地址（=相机 quat+0x20），用于同向校验

    # ================= 接管闭环 =================
    def ensure_crosshair():
        """创建绿准星窗口（幂等）。它只是诊断仪表：画的是「剩余误差的屏幕投影」。

        ⚠ 这段原先是塞在 enable_tracking() 里的 —— 那是旧内核的接管函数，
        新内核从重构那天起就不调用它，于是准星窗口再也没被创建过（面板
        一直显示 win=None）。所以改成 main 启动时独立创建，两条内核共用。
        """
        if not st.cross_on or st.cross is not None:
            return st.cross
        try:
            from acvd.overlay import Crosshair
            cross = Crosshair()
            if cross.start():
                st.cross = cross
                if mouse is not None:
                    mouse.cross = cross
                print("✓ 绿准星已显示（诊断仪表；面板/管道 cross off 可关）",
                      flush=True)
                return cross
            print("! 准星窗口创建失败（CreateWindowEx 返回 0）", flush=True)
        except Exception as e:
            print(f"! 准星创建失败: {e}", flush=True)
        return None
    def make_cfg():
        """把 tune.json 的手感参数打包成内核 Cfg。

        统一入口的意义：管道命令改完参数后能立刻 set_cfg 生效，
        不用重启 mapper 去验证一个数字。
        """
        return Cfg(
            sens_scale=TUNING.get("sens_scale", 1.0),
            full_stick_angle=TUNING["full_stick_angle"],
            pitch_limit=TUNING["pitch_limit"],
            smooth=TUNING["smooth"],
            game_deadzone=TUNING["game_deadzone"],
            dead_px=TUNING.get("dead_px", 2.0),
            curve_gamma=TUNING.get("curve_gamma", 1.0),
        )

    def push_cfg():
        """参数热更新：新 Cfg 交给 Composer，下一帧生效。"""
        if engine is None:
            return
        try:
            engine.set_cfg(make_cfg())
        except Exception as e:
            print(f"! 参数热更新失败: {e}", flush=True)

    # 视口几何缓存（0.5 秒）：GetClientRect 每帧调用是浪费，
    # 窗口尺寸不会每帧变。t 是上次采样时刻，v 是缓存的 View。
    _view_cache = {"t": 0.0, "v": None}

    def _view_now():
        """视口几何：从游戏窗口客户区算 16:9 内接区域。0.5 秒缓存一次。"""
        from acvd.core.types import View
        now = time.perf_counter()
        if _view_cache["v"] is not None and now - _view_cache["t"] < 0.5:
            return _view_cache["v"]
        v = _view_cache["v"] or View(float(user32.GetSystemMetrics(0)) / 2,
                                     float(user32.GetSystemMetrics(1)) / 2,
                                     float(user32.GetSystemMetrics(0)),
                                     float(user32.GetSystemMetrics(1)))
        try:
            hwnd = fw.hwnd_of()
            r = wt.RECT()
            if hwnd and user32.GetClientRect(hwnd, ctypes.byref(r)):
                cw, ch = r.right - r.left, r.bottom - r.top
                if cw >= 200 and ch >= 200:
                    pt = wt.POINT(0, 0)
                    user32.ClientToScreen(hwnd, ctypes.byref(pt))
                    vh = float(ch)
                    vw = vh * 16.0 / 9.0
                    if vw > cw:
                        vw = float(cw)
                        vh = vw * 9.0 / 16.0
                    v = View(cx=pt.x + cw // 2, cy=pt.y + ch // 2,
                             sw=vw, sh=vh, hfov=v.hfov)
        except Exception:
            pass
        _view_cache["v"] = v
        _view_cache["t"] = now
        return v

    def _read_delta():
        """鼠标增量：Raw Input 优先，拿不到退回光标差值。带单帧位移上限。

        ## 为什么失焦时直接返回 0 并且不钉光标

        以前这里无条件把光标 SetCursorPos 回窗口中心。后果是：玩家只要
        一切换窗口，光标立刻被拽回游戏里，**只能用 F10（全部暂停）逃生**
        —— 想回浏览器查个东西都不行，而按了 F10 就把整份手柄也停了，
        看起来就像「视角不动」。

        现在分两种：

            焦点在 Xenia  正常读增量 + 钉光标（闭环的既有行为，不变）
            焦点不在      返回 (0,0)，**不动光标**

        这样切窗口时光标是自由的，切回来自动恢复闭环，不需要按任何热键。

        ## 为什么不能只放开钉光标、仍然读位移

        光标差值那一路（fallback）是「当前光标 - 上次位置」。切窗口后玩家
        在浏览器里移动鼠标，这些位移会被当成视角输入灌进闭环，准星会乱飞。
        所以失焦时必须**两条路一起断**，不只是不钉光标。
        """
        if not fw.is_focused():
            # 失焦：不读、不钉。返回零增量让上游知道这一帧没有输入。
            #
            # 必须顺手 drain 掉 Raw Input 的积压：失焦期间玩家可能在别的
            # 窗口移动鼠标，这些事件攒在缓冲里；等切回来时如果一次性
            # 消费，视角会"暴冲"一下。丢掉它们才是对的。
            if raw_ok:
                try:
                    raw.drain()
                except Exception:
                    pass
            return 0, 0

        dx = dy = 0
        if raw_ok:
            rdx, rdy, nraw, _ = raw.drain()
            if nraw or rdx or rdy:
                STATS["raw_events"] += nraw
                dx, dy = rdx, rdy
        if not (dx or dy):
            p = wt.POINT()
            v = _view_now()
            if user32.GetCursorPos(ctypes.byref(p)):
                cdx = p.x - int(v.cx)
                cdy = p.y - int(v.cy)
                cdx = clamp(cdx, -CURSOR_MAX_STEP, CURSOR_MAX_STEP)
                cdy = clamp(cdy, -CURSOR_MAX_STEP, CURSOR_MAX_STEP)
                dx += cdx
                dy += cdy
        try:
            v = _view_now()
            user32.SetCursorPos(int(v.cx), int(v.cy))
        except Exception:
            pass
        return dx, dy

    class EngineProxy:
        """把四层内核包装成旧 MouseAim 的接口。

        前端（准星、面板、管道诊断）只认这些字段名，所以内核换掉之后
        前端一行都不用改 —— 这就是绞杀者模式的意义：新架构原地接管，
        旧代码逐步失效，而不是一次性推倒。
        """

        def __init__(self, eng):
            self.engine = eng
            self.dead = False        # 内核自己处理失效，这里永远是 False
            self.resync = 0
            self.raw = raw if raw_ok else None
            self.raw_ever = raw_ok
            self.raw_dead = 0
            self.aim_cont = None
            self.tgt_yaw = None
            self.out_x = self.out_y = 0.0

        # -- 内核快照映射成旧字段 --
        def _f(self):
            return self.engine.frame()

        @property
        def ch_x(self):
            f = self._f()
            return f.ch_x if f else 0.0

        @property
        def ch_y(self):
            f = self._f()
            return f.ch_y if f else 0.0

        @property
        def err_y(self):
            f = self._f()
            return f.err_y if f else 0.0

        @property
        def err_p(self):
            f = self._f()
            return f.err_p if f else 0.0

        @property
        def FOV(self):
            return self.engine.sensor.fov()

        @property
        def hfov(self):
            return _view_now().hfov

        @property
        def f(self):
            return _view_now().f

        @property
        def fov_live(self):
            return True

        @property
        def addr(self):
            from acvd.core.sensor import CAM_FOV_GUEST
            return CAM_FOV_GUEST

        def step(self, enabled: bool = True):
            rx, ry = self.engine.stick()
            if not enabled:
                return 0.0, 0.0
            self.out_x, self.out_y = rx, ry
            return rx, ry

        def recenter(self):
            """F2：把准星锚定到火控圈当前绝对坐标（换关后按一下即可）。"""
            self.engine.request_anchor()

        def _read(self):
            """诊断用：从最近一帧反推（不再直接读内存）。"""
            f = self._f()
            if f is None:
                return None
            return (0.0, 0.0)

        def set_view(self, hwnd):
            return True

    # ---- 进程连接状态（auto_get_proc 用）--------------------------------
    # _proc_next: 下次允许尝试连接的时刻（失败退避 2s / 成功限速 50ms）
    # _proc_pid : 上次成功连接的 pid，只在换 pid 时打印，避免刷屏
    _proc_next = [0.0]
    _proc_pid = [None]

    def auto_get_proc():
        """给采集线程用：句柄失效就重连 Xenia。

        ## 为什么有 50ms 限速

        Sensor 以 250Hz 调它。进程正在退出的那一瞬，采集线程、主循环、
        看门狗会同时发现 alive()==False，每个都去 attach 一次。实测一次
        退出刷了 427 行日志、泄漏 427 个句柄。所以「连上了也限速 50ms」，
        不只是连不上才退避。

        ## 为什么失败要 2 秒退避

        游戏没开的时候这是常态。不退避的话 250Hz × 每秒 250 次
        进程查找（每次要走一遍 EnumWindows）会把 CPU 吃满。
        """
        nonlocal proc
        now = time.perf_counter()
        if proc is not None and proc.alive():
            return proc
        if proc is not None:
            try:
                proc.close()        # 旧句柄已经失效，别攒着泄漏
            except Exception:
                pass
        proc = None
        if now < _proc_next[0]:
            return None
        try:
            proc = attach_xenia()
            _proc_next[0] = now + 0.05
            if proc.pid != _proc_pid[0]:
                _proc_pid[0] = proc.pid
                print(f"[自动] 已连接 Xenia pid={proc.pid}", flush=True)
            return proc
        except Exception:
            _proc_next[0] = now + 2.0
            return None

    def main_loop():
        """手柄输出循环（后台线程，独立于内核闭环）。"""
        nonlocal running, pad
        hk_prev = {k: False for k in HOTKEYS}
        last_loop = time.perf_counter()
        while running:
            t0 = time.perf_counter()
            gap_ms = (t0 - last_loop) * 1000.0
            last_loop = t0
            STATS["frames"] += 1
            STATS["_hb"] = time.perf_counter()
            if gap_ms > STATS["max_gap_ms"]:
                STATS["max_gap_ms"] = gap_ms
            if gap_ms > 20.0:
                STATS["slow"] += 1

            # ---- 热键（键位见 HOTKEYS，必须避开 Edge 的 F3~F8/F11/F12/Esc/Pause）----
            hk = {name: hook.is_down(vk) for name, vk in HOTKEYS.items()}

            if hk["recenter"] and not hk_prev["recenter"] and mouse is not None:
                try:
                    mouse.recenter()
                    print(f"\n[{HOTKEY_LABEL['recenter']}] 准星已锚定到火控圈当前坐标",
                          flush=True)
                except Exception:
                    pass

            if hk["pause"] and not hk_prev["pause"]:
                with st.lock:
                    st.paused = not st.paused
                print(f"\n[{HOTKEY_LABEL['pause']}] 映射"
                      f"{'已暂停' if st.paused else '已恢复'}", flush=True)

            # F1：语义收敛为「重新对齐坐标」（与 F2 等价）。
            # 旧内核这里会扫内存找火控圈地址并推杆验证，四层内核用固定相机，
            # 没有地址可扫 —— 保留这个键只为不让人按了没反应。
            if hk["calibrate"] and not hk_prev["calibrate"]:
                if engine is not None:
                    engine.request_anchor()
                    print("\n[重新对齐] 准星已锚定到当前视角", flush=True)

            # F10：全部暂停 / 恢复。旧内核那个「强制映射（无视焦点）」已经
            # 没有意义 —— 新内核根本不看焦点，输出的是手柄。F10 空出来正好
            # 给「打字/切窗口时彻底脱手」用，与面板顶部大按钮同一开关。
            if hk["halt"] and not hk_prev["halt"]:
                with st.lock:
                    st.halt = not st.halt
                    if st.halt:
                        st.k_lx = st.k_ly = st.k_rx = st.k_ry = 0.0
                        st.k_lt = st.k_rt = 0.0
                        st.k_btn = 0
                        st.o_rx = st.o_ry = 0.0
                print(f"\n[{HOTKEY_LABEL['halt']}] "
                      f"{'手柄已整份脱手（键盘也不发）' if st.halt else '已恢复映射'}",
                      flush=True)

            if hk["quit"] and not hk_prev["quit"]:
                print(f"\n[{HOTKEY_LABEL['quit']}] 退出", flush=True)
                running = False

            hk_prev = hk

            # F9 暂停：只停「鼠标闭环」的右摇杆输出，键盘/按钮/左摇杆照常工作。
            # 旧行为是把整份手柄状态清零，结果暂停后连菜单里的键盘都没法用，
            # 而暂停的真实目的只是「别让鼠标继续带视角」。
            # 暂停不再隐藏准星：下面会把它画成灰色钉在中心。
            # 「消失」会被当成故障，「变灰」一眼就是暂停。
            # 注意这里不能 continue：F9 只跳鼠标闭环，键盘/按钮照常（菜单里要能用）；
            # halt（面板全部暂停）更彻底，输入在下面统一清零。

            focused = fw.is_focused()
            take = st.takeover

            # ---- 四层内核：闭环开关由内核状态机决定，前端只跟着走 ----
            if engine is not None:
                try:
                    on = engine.is_tracking()
                except Exception:
                    on = False
                if on != st.mouse_on:
                    with st.lock:
                        st.mouse_on = on

            # 四层内核自己管接管状态（engine.is_tracking()），上面那段
            # 已经把结果同步进 st.mouse_on。旧内核的 AutoLocator 自动定位
            # 已移除 —— 它靠扫描 guest 堆找火控圈，而新内核用固定相机地址，
            # 没有可扫的东西。

            # 失焦 = 全部输入被清零，会表现为「整段输入消失」。
            # 计数以便区分「采样丢按」和「焦点翻转」。
            if focused != STATS.get("_focus_prev", focused):
                STATS["focus_flips"] += 1
                STATS["_focus_prev"] = focused
            if not focused:
                STATS["unfocused"] += 1

            # 鼠标池按「谁在读」互斥分工：闭环正常运行（接管中且未暂停）
            # 时由 Composer 独占消费；其余情况主循环丢弃积压，避免恢复暴冲。
            # 新内核不看焦点（输出是手柄，Xenia 后台照样收）；
            # 焦点只保留给旧内核键鼠回退路径。
            if raw_ok and not (st.mouse_on and not st.paused and not st.halt
                               and (engine is not None or focused)):
                raw.drain()

            # 焦点清零只属于旧内核键鼠路径。新内核输出的是手柄，
            # Xenia 后台照样收；玩家切屏自己按 F9 暂停。
            if engine is None and not focused and not take and not st.force:
                with st.lock:
                    st.k_lx = st.k_ly = st.k_rx = st.k_ry = 0.0
                    st.k_btn = 0
                    st.k_lt = st.k_rt = 0.0
                lx = ly = rx = ry = 0.0
                lt = rt = 0.0
                btn = 0
            else:
                sync_hook(hook, keymap)
                m = mp_poll(keymap, hook, mhook)
                with st.lock:
                    st.k_lx, st.k_ly = m[0], m[1]
                    st.k_rx, st.k_ry = m[2], m[3]
                    st.k_lt, st.k_rt = m[4], m[5]
                    st.k_btn = m[6]

                    cur_mouse = mouse
                    if take:
                        rx, ry = st.o_rx, st.o_ry
                    elif engine is not None:
                        # 四层内核：输出权交给内核（看门狗保证卡住时它返 0）。
                        # 关键：不能只在 TRACKING 时读 —— 内核在 READY 阶段的
                        # 主动短推（MODE_PULSE）也走这条通道，否则那一下永远
                        # 推不出去，验证就永远凑不够票。
                        # 新内核只认 F9 暂停，不认窗口焦点。
                        if st.paused:
                            rx, ry = 0.0, 0.0
                        else:
                            rx, ry = engine.stick()
                    elif st.mouse_on and cur_mouse is not None and not st.paused:
                        # 闭环异常不能让整个输出线程死掉：保底沿用上一帧
                        try:
                            mx, my = cur_mouse.step(enabled=focused)
                        except Exception as e:
                            print(f"! 鼠标闭环异常: {e}", flush=True)
                            mx, my = st.m_rx, st.m_ry
                        st.m_rx, st.m_ry = mx, my
                        # 火控圈地址失效（关卡切换）：明确停掉，别让闭环空转。
                        # 注意：这里已经在 with st.lock 内部，绝不能再次获取
                        # st.lock（普通 Lock 不可重入，二次获取 = 永久死锁，
                        # 表现为整个 mapper 卡死、热键全无反应）。
                        if getattr(cur_mouse, "dead", False):
                            st.mouse_on = False
                            PREV_ADDR[0] = None    # 别缓存死地址
                            print("\n[闭环] 火控圈地址已失效（关卡结束/切换），"
                                  "已自动停掉闭环并清除地址缓存。"
                                  "换好关卡后按 F1 重新对齐坐标。", flush=True)
                        if st.frozen:
                            rx, ry = 0.0, 0.0
                        else:
                            rx, ry = mx, my
                    else:
                        rx, ry = st.k_rx, st.k_ry
                    lx = st.o_lx if st.o_lx is not None else st.k_lx
                    ly = st.o_ly if st.o_ly is not None else st.k_ly
                    btn = st.o_btn if st.o_btn is not None else st.k_btn
                    lt = st.o_lt if st.o_lt is not None else st.k_lt
                    rt = st.o_rt if st.o_rt is not None else st.k_rt

            # halt（面板「全部暂停」）：整份手柄置中性，键盘映射也不发。
            # 光按 F9 只停鼠标闭环，键盘/按钮照旧往游戏里送 —— 切去聊天框
            # 打字时游戏还会被按到，这就是「后台积极响应」的来源。
            #
            # 例外：pin（固定输出调试）必须能穿过 halt —— 否则为了扫内存
            # 就得解除暂停，而解除暂停会锁住光标，等于为了取证打断用户。
            _pinned = (engine is not None
                       and getattr(engine.composer, "hold", None) is not None)
            if st.halt and not _pinned:
                lx = ly = rx = ry = 0.0
                lt = rt = 0.0
                btn = 0
                latch.clear()      # 连「最短保持」里挂着的 40ms 也立刻掐掉

            # 手柄输出走独立线程：只把目标状态丢进队列（满则覆盖旧值）。
            # 这样即使 ViGEm 驱动卡住，主循环/热键/状态查询依然活着。
            try:
                pad_q.put_nowait((lx, ly, rx, ry,
                                  latch.axis("lt", lt), latch.axis("rt", rt),
                                  latch.mask(btn)))
            except queue.Full:
                try:
                    pad_q.get_nowait()          # 丢掉最旧的，只保留最新
                except queue.Empty:
                    pass
                try:
                    pad_q.put_nowait((lx, ly, rx, ry,
                                      latch.axis("lt", lt), latch.axis("rt", rt),
                                      latch.mask(btn)))
                except queue.Full:
                    pass

            # 准星上的圈 = 闭环死区，两者用同一个数：玩家看到的就是停杆的边界
            if st.cross is not None:
                try:
                    if engine is not None:
                        st.cross.radius = float(engine.composer.cfg.dead_px)
                    st.cross.set_alpha(TUNING.get("cross_alpha", 200))
                except Exception:
                    pass

            # 绿准星 = 诊断仪表：只要闭环在跑就一直画出来，
            # 不因失焦隐藏（失焦时闭环本身已经停输出，位置会冻结在原地）。
            if (st.cross_on and st.mouse_on and not st.paused and not st.halt
                    and mouse is not None and st.cross is not None):
                try:
                    # 目标超出视野（误差 > HFOV/2）时准星变红提示
                    half_fov = mouse.hfov * 0.5
                    st.cross.dim = False
                    st.cross.overflow = (abs(mouse.err_y) > half_fov
                                         or abs(mouse.err_p) > half_fov)
                    st.cross.move_to(mouse.ch_x, mouse.ch_y)
                except Exception:
                    pass
            elif (st.cross_on and st.mouse_on and st.cross is not None
                  and st.cross.hwnd and (st.paused or st.halt)):
                # 暂停时**不要消失**，画成灰色钉在屏幕中心：
                # 「准星不见了」和「准星在中心变灰」是两种完全不同的信息，
                # 前者让人以为坏了，后者一眼就知道是暂停。
                try:
                    v = _view_now()
                    st.cross.dim = True
                    st.cross.overflow = False
                    st.cross.move_to(v.cx, v.cy)
                except Exception:
                    pass
            elif st.cross is not None and st.cross.hwnd:
                try:
                    user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
                    user32.ShowWindow(st.cross.hwnd, 0)   # 闭环没跑就不该骗人
                except Exception:
                    pass

            # 绿准星记录（诊断用，rec on 开启）
            if REC["on"] and mouse is not None:
                REC["every"] -= 1
                if REC["every"] <= 0:
                    REC["every"] = 3          # 125Hz / 3 ≈ 40Hz
                    try:
                        REC["fh"].write(
                            f"{time.perf_counter() - REC['t0']:.3f},"
                            f"{(mouse.aim_cont if mouse.aim_cont is not None else 0):.4f},"
                            f"{(mouse.tgt_yaw if mouse.tgt_yaw is not None else 0):.4f},"
                            f"{mouse.err_y:.4f},{mouse.err_p:.4f},"
                            f"{mouse.f:.1f},{mouse.FOV:.2f},"
                            f"{mouse.ch_x:.1f},{mouse.ch_y:.1f},"
                            f"{mouse.out_x:.3f},{mouse.out_y:.3f},"
                            f"{'cursor' if (mouse.raw is None or mouse.raw_dead > 60) else 'raw'},"
                            f"{1 if focused else 0}\n")
                        REC["n"] += 1
                    except Exception:
                        rec_close()

            # 新内核：接管中且未暂停就锁光标，不看焦点（输出是手柄）。
            # 旧内核回退路径仍按焦点恢复，避免切到别的窗口看不见鼠标。
            hider = getattr(st, "hider", None)
            if hider is not None:
                try:
                    want = (bool(st.mouse_on and not st.paused)
                            if engine is not None
                            else bool(focused and st.mouse_on and not st.paused))
                    hider.set_enabled(want)
                except Exception:
                    pass

            s = interval - (time.perf_counter() - t0)
            if s > 0:
                time.sleep(s)

    latch = ButtonLatch(TUNING["button_hold"])

    threading.Thread(target=pad_worker, daemon=True).start()
    def watchdog():
        """主循环停摆检测：超 3 秒没有心跳就把所有线程的栈打出来。

        进程级卡死时（热键、状态查询全无响应）这是唯一能留证据的手段：
        栈会直接指认卡在哪个调用里（ReadProcessMemory / pad.update / 锁）。
        """
        import faulthandler
        last = -1.0
        stall = 0
        while running:
            time.sleep(1.0)
            hb = STATS.get("_hb", 0.0)
            if hb == last:
                stall += 1
            else:
                stall = 0
                last = hb
            if stall == 3:
                print("\n!! 主循环已 3 秒无心跳，导出所有线程栈 ↓↓↓", flush=True)
                try:
                    with open(TRACE_DUMP_PATH, "a", encoding="utf-8") as fd:
                        fd.write(f"\n===== {time.strftime('%H:%M:%S')} 主循环停摆 =====\n")
                        faulthandler.dump_traceback(file=fd, all_threads=True)
                        fd.write("===== 结束 =====\n")
                    print(f"!! 线程栈已写入 {TRACE_DUMP_PATH}", flush=True)
                except Exception as e:
                    print(f"!! 导出线程栈失败: {e}", flush=True)
                print("!! ↑↑↑\n", flush=True)

    threading.Thread(target=watchdog, daemon=True).start()

    # ---- 四层内核（core）：装配 + 分层守卫 ----
    if a.engine == "core":
        try:
            from acvd.core import AimEngine, Cfg, V2Cfg, enforce
            enforce()      # 分层违规直接拒绝启动，绝不带着缝合怪跑起来
            engine = AimEngine(
                get_proc=auto_get_proc,
                get_delta=_read_delta,
                get_view=_view_now,
                cfg=make_cfg(),
                log=lambda s: print(s, flush=True),
                # 控制器：v1 = 直读相机（稳定版，当前默认）。
                # v2 = 逆模型去滞后，代码保留但**不再默认启用** ——
                # 实测在机体平移时准星有异常跳动，方向未定论前不放进默认路径。
                # 想试：把这里改成 "v2"，或以后接面板开关。
                ctrl="v1",
                v2=V2Cfg(tau=TUNING.get("cam_tau", 0.0),
                         lp=TUNING.get("cam_lp", 0.04)),
                # 新内核不检查焦点：输出的是手柄，Xenia 后台照样收。
                # 玩家要切屏自己按 F9 暂停，这才是唯一的输出开关。
                # halt 是面板上的「全部暂停」，语义与 F9 相同但更彻底
                # （连键盘映射一起掐断），两者任一为真都不锁光标、不推杆。
                is_paused=lambda: st.paused or st.halt,
            )
            eproxy = EngineProxy(engine)
            mouse = eproxy
            engine.start()
            ensure_crosshair()
            print("  内核：四层解耦（采集/决策/控制/输出）分层守卫已通过",
                  flush=True)
        except Exception as e:
            engine = None
            eproxy = None
            mouse = None
            print(f"! 内核启动失败：{e}", flush=True)
            print("! 四层内核是唯一内核（旧内核已移除），无法继续。", flush=True)
            return 5

    loop_thread = threading.Thread(target=main_loop, daemon=True)
    loop_thread.start()


    try:
        while running:
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        running = False
        time.sleep(0.3)
        srv.stop()
        hook.stop()
        try:
            mhook.stop()
        except Exception:
            pass
        try:
            if st.cross:
                st.cross.stop()
            if getattr(st, "hider", None):
                st.hider.stop()
        except Exception:
            pass
        with pad_lock:
            try:
                pad.center()
            except Exception:
                pass
        pad.close()
        print("mapper 退出", flush=True)
    return 0


def mp_poll(keymap, hook, mhook=None, stale_check=None):
    """读取按键状态：数字=VK 走键盘钩子，字符串=鼠标键走鼠标钩子。"""
    d = hook.is_down

    def any_down(name) -> bool:
        v = keymap[name]
        vals = v if isinstance(v, (list, tuple)) else [v]
        for x in vals:
            if isinstance(x, str):
                if mhook is not None and mhook.is_down(x):
                    return True
            else:
                if d(int(x)):
                    return True
        return False

    lx = ly = 0.0
    if any_down("left_up"):
        ly = 1.0
    if any_down("left_down"):
        ly = -1.0
    if any_down("left_left"):
        lx = -1.0
    if any_down("left_right"):
        lx = 1.0
    if lx and ly:
        lx *= 0.7071
        ly *= 0.7071
    rx = ry = 0.0
    if any_down("right_up"):
        ry = 1.0
    if any_down("right_down"):
        ry = -1.0
    if any_down("right_left"):
        rx = -1.0
    if any_down("right_right"):
        rx = 1.0
    if rx and ry:
        rx *= 0.7071
        ry *= 0.7071
    btn = 0
    # 十字键（dpup/dpdown/dpleft/dpright）的位在 BTN 里一直有，
    # 但以前这个循环从没遍历它们 —— 等于「输出通道建好了，没人去接」。
    # 补上之后，键盘就能产生 D-Pad 输入（keymap.json 里绑哪个键都行）。
    for name in ("a", "b", "x", "y", "lb", "rb", "ls", "rs", "start", "back",
                 "dpup", "dpdown", "dpleft", "dpright"):
        if any_down(name):
            btn |= BTN[name]
    lt = 1.0 if any_down("lt") else 0.0
    rt = 1.0 if any_down("rt") else 0.0
    return lx, ly, rx, ry, lt, rt, btn


def _gas_works() -> bool:
    """
    检测 GetAsyncKeyState 在本会话是否可用。
    如果它完全读不到按键（例如服务会话输入隔离），返回 False，
    此时不能拿它做交叉校验，否则会误清真实按下的键。
    """
    # 用一个「几乎不可能被按住」的键做探测：连续 3 次都返回 0 说明函数可用
    # （若函数不可用，通常也会一直返回 0 —— 所以这只能证明「不会误报按下」）
    # 因此这里采用保守策略：只有在 --gas-check 显式开启时才使用
    return False


_GAS_OK = False


def sync_hook(hook, keymap, guard: float = 1.2):
    """修正被漏掉的 keyup：钩子说按住，但物理上早就松了 → 强制松开。

    低级钩子在回调被拖慢时（本进程有内存扫描、FOV 全进程扫描等重活），
    Windows 会按 LowLevelHooksTimeout 丢弃事件，keyup 一丢就变成永久卡键。
    这里用 GetAsyncKeyState 做交叉校验：连续 3 帧都说没按才算松，
    避免因为读取抖动把真实按住的键误清。
    """
    now = time.perf_counter()
    keys = hook.down_keys()
    for vk in keys:
        _PRESS_SINCE.setdefault(vk, now)
    for vk in list(_PRESS_SINCE):
        if vk not in keys:
            _PRESS_SINCE.pop(vk, None)
    for vk in keys:
        if now - _PRESS_SINCE.get(vk, now) < guard:
            _UP_STREAK.pop(vk, None)
            continue
        if user32.GetAsyncKeyState(vk) & 0x8000:
            _UP_STREAK.pop(vk, None)
            continue
        n = _UP_STREAK.get(vk, 0) + 1
        _UP_STREAK[vk] = n
        if n >= 3:
            hook.force_release(vk)
            _UP_STREAK.pop(vk, None)
            _PRESS_SINCE.pop(vk, None)
            STATS["released"] = STATS.get("released", 0) + 1


if __name__ == "__main__":
    raise SystemExit(main())
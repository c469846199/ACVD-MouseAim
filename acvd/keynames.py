"""
acvd/keynames.py — 键名 <-> 虚拟键码（VK）互转

给人类看的名字（"space" / "mouse_left" / "f1" / "scrolllock"）和 Windows
虚拟键码之间互相转换，让 keymap.json 不需要写十六进制。

名字全部小写、不区分大小写；也接受 "0x41" 这种十六进制字符串。
"""

from __future__ import annotations

import ctypes

user32 = ctypes.WinDLL("user32", use_last_error=True)

_LETTERS = {chr(c): c - 32 for c in range(ord("a"), ord("z") + 1)}
_DIGITS = {str(d): ord(str(d)) for d in range(10)}
_FKEYS = {f"f{i}": 0x6F + i for i in range(1, 25)}

NAMED = {
    **_LETTERS,
    **_DIGITS,
    **_FKEYS,
    # 控制键
    "space": 0x20, "tab": 0x09, "enter": 0x0D, "return": 0x0D,
    "esc": 0x1B, "escape": 0x1B, "backspace": 0x08,
    "capslock": 0x14, "numlock": 0x90, "scrolllock": 0x91,
    "printscreen": 0x2C, "pause": 0x13, "apps": 0x5D,
    # 修饰键
    "shift": 0x10, "lshift": 0xA0, "rshift": 0xA1,
    "ctrl": 0x11, "control": 0x11, "lctrl": 0xA2, "rctrl": 0xA3,
    "alt": 0x12, "lalt": 0xA4, "ralt": 0xA5,
    "win": 0x5B, "lwin": 0x5B, "rwin": 0x5C,
    # 编辑键
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "insert": 0x2D, "delete": 0x2E, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22,
    # 小键盘
    "num0": 0x60, "num1": 0x61, "num2": 0x62, "num3": 0x63, "num4": 0x64,
    "num5": 0x65, "num6": 0x66, "num7": 0x67, "num8": 0x68, "num9": 0x69,
    "numadd": 0x6B, "numsub": 0x6D, "nummul": 0x6A, "numdiv": 0x6F,
    "numdot": 0x6E,
    # 标点
    ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF,
    "`": 0xC0, "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE,
    "semicolon": 0xBA, "plus": 0xBB, "comma": 0xBC, "minus": 0xBD,
    "period": 0xBE, "slash": 0xBF, "tilde": 0xC0, "backslash": 0xDC,
    "quote": 0xDE,
}

# 鼠标键（不是 VK，走鼠标钩子）
# 注意：这里**不能**收 "left"/"right"/"middle" 这种裸名字。
# 它们和方向键同名，to_vk("right") 会变成鼠标右键而不是 VK_RIGHT(0x27)，
# 结果 keymap 里 "right_right": "right" 会让「按鼠标右键 → 右摇杆推满 → 视角固定速度转」。
MOUSE = {
    "mouse_left": "left", "mouse_right": "right", "mouse_middle": "middle",
    "mouse_x1": "left", "mouse_x2": "right",
}

# VK -> 显示名（用于打印）
_DISPLAY: dict[int, str] = {}
for _name, _vk in NAMED.items():
    if _vk not in _DISPLAY or len(_name) < len(_DISPLAY[_vk]):
        _DISPLAY[_vk] = _name


def to_vk(value):
    """把 "space" / "0x20" / 32 统一成 VK 整数；鼠标名返回原字符串（内部用法）。"""
    if isinstance(value, int):
        return value
    s = str(value).strip()
    low = s.lower()
    if low in MOUSE:
        return MOUSE[low]
    if low in NAMED:
        return NAMED[low]
    if low.startswith("0x"):
        return int(low, 16)
    if low.isdigit():
        return int(low)
    raise ValueError(f"不认识的键名: {value!r}（可用 keynames.NAMES 里的名字）")


def vk_name(vk: int) -> str:
    """VK -> 最简显示名，未收录的显示成 0xNN。"""
    return _DISPLAY.get(vk, f"0x{vk:02X}")


def vk_of_mouse_name(name: str) -> str:
    """把 mouse_left 之类转成鼠标钩子用的 'left'。"""
    return MOUSE.get(name.lower(), name)


# 供 rebind 工具扫描：这些 VK 都算「可绑定」
SCAN_VK = list(range(0x08, 0x100))


def pressed_vks() -> set[int]:
    """当前按下的所有键（含鼠标左右中键，统一用 0x01/0x02/0x04 表示）。"""
    down = set()
    for vk in SCAN_VK:
        if user32.GetAsyncKeyState(vk) & 0x8000:
            down.add(vk)
    # GetAsyncKeyState 对鼠标键也要单独查（0x01..0x04 已在 SCAN_VK 之外）
    for vk in (0x01, 0x02, 0x04):
        if user32.GetAsyncKeyState(vk) & 0x8000:
            down.add(vk)
    return down


MOUSE_VK = {0x01: "left", 0x02: "right", 0x04: "middle"}

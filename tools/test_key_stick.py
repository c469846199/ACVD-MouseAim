"""
tools/test_key_stick.py — 验证键盘方向键驱动右摇杆的实际效果

关键问题：
    键盘模式下，keybind_right_thumb_left/right 是【离散】输入，
    只能输出「推到底」或「不推」，无法输出中间值。

    如果真是这样，闭环只能做 bang-bang 控制（到角度就松手），
    精度受限。

本脚本测：
    按住方向键 N 秒，火控圈转了多少度 → 得到角速度
    松开后是否立即停止 → 判断有没有惯性

需要 Xenia 在场景里（有火控圈）。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import math
import struct
import sys
import time

__import__("sys").path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from acvd.aim import AimTracker, read_aim
from acvd.winmem import Process

user32 = ctypes.WinDLL("user32", use_last_error=True)

VK_UP, VK_DOWN, VK_LEFT, VK_RIGHT = 0x26, 0x28, 0x25, 0x27
KEYEVENTF_KEYUP = 0x0002
INPUT_KEYBOARD = 1


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _INPUTunion(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("_pad", ctypes.c_byte * 24)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _INPUTunion)]


user32.SendInput.argtypes = [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]


def key_down(vk):
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.u.ki = KEYBDINPUT(vk, 0, 0, 0, 0)
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def key_up(vk):
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.u.ki = KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP, 0, 0)
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def main():
    proc = Process.attach("xenia_canary")
    t = AimTracker(proc)
    print("定位火控圈 ...")
    if not t.locate():
        print("✗ 定位失败（游戏在场景里吗？）")
        return 1
    v = t.read()
    print(f"火控圈 0x{t.addr:012X}  yaw={v[0]:+.2f}")

    def measure(vk, name, secs=1.0):
        before = t.read()[0]
        key_down(vk)
        time.sleep(secs)
        mid = t.read()[0]
        key_up(vk)
        time.sleep(0.5)
        after = t.read()[0]
        d_turn = (mid - before + 180) % 360 - 180
        d_stop = (after - mid + 180) % 360 - 180
        print(f"  按 {name} {secs}s: 转向 {d_turn:+8.2f}°  "
              f"松手后再转 {d_stop:+7.2f}°")
        return d_turn

    print("\n测试方向键 → 右摇杆:")
    print("（每个方向按 1 秒）")
    for vk, name in [(VK_RIGHT, "→ 右"), (VK_LEFT, "← 左"),
                     (VK_UP, "↑ 上"), (VK_DOWN, "↓ 下")]:
        time.sleep(0.3)
        measure(vk, name, 1.0)

    print("\n结论：")
    print("  若「转向」明显（几十度）→ 方向键可驱动火控圈")
    print("  若「松手后再转」明显 → 火控圈有惯性，闭环要提前松手")
    print("  键盘是离散输入，无法输出中间摇杆值 → 闭环只能 bang-bang")

    proc.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
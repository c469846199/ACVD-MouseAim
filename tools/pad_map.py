"""
tools/pad_map.py — 纯键盘 → 虚拟手柄映射（不读内存，不进游戏也能用）

用途：
    在菜单里导航、进训练场。不需要火控圈定位，启动即用。

键位（玩家键位表）：
    A=空格  B=C  X=鼠标左键  Y=鼠标右键
    LB=Tab  RB=3  LT=Q  RT=E   LS=Shift  RS=鼠标中键
    START=X  BACK=Z
    WASD = 左摇杆     方向键 = 十字键

热键：F12 退出

用法：
    python tools\\pad_daemon.py     （另一窗口）
    python tools\\pad_map.py
"""

from __future__ import annotations

import ctypes
import sys
import time

__import__("sys").path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))

from acvd.keyhook import KeyHook
from acvd.pipe import PipeClient

user32 = ctypes.WinDLL("user32", use_last_error=True)

VK_A = 0x20
VK_B = 0x43
VK_LB = 0x09
VK_RB = 0x33
VK_LT = 0x51
VK_RT = 0x45
VK_LS = 0x10
VK_START = 0x58
VK_BACK = 0x5A
VK_W, VK_S, VK_AA, VK_D = 0x57, 0x53, 0x41, 0x44
VK_UP, VK_DOWN, VK_LEFT, VK_RIGHT = 0x26, 0x28, 0x25, 0x27
VK_LBUTTON, VK_RBUTTON, VK_MBUTTON = 0x01, 0x02, 0x04

LOOP_HZ = 120


def main() -> int:
    try:
        cli = PipeClient("acvd_pad")
        cli.send("status")
    except OSError:
        print("✗ 守护进程没跑：python tools\\pad_daemon.py")
        return 1

    hook = KeyHook()
    if not hook.start():
        print("✗ 键盘钩子启动失败")
        return 1

    print("=" * 52)
    print(f" 纯键盘映射已启动  ({LOOP_HZ} Hz)")
    print(" 空格=A C=B 左键=X 右键=Y Tab=LB 3=RB Q=LT E=RT")
    print(" Shift=LS X=START Z=BACK WASD=移动 方向键=十字键")
    print(" F12 退出")
    print("=" * 52)

    interval = 1.0 / LOOP_HZ
    prev_btn = -1
    prev_l = (0.0, 0.0)
    prev_tr = (-1.0, -1.0)
    n = 0

    try:
        while True:
            t0 = time.perf_counter()
            if hook.is_down(0x7B):     # F12
                print("\nF12 退出")
                break

            d = hook.is_down
            lx = ly = 0.0
            if d(VK_W):
                ly = 1.0
            if d(VK_S):
                ly = -1.0
            if d(VK_AA):
                lx = -1.0
            if d(VK_D):
                lx = 1.0
            if lx and ly:
                lx *= 0.7071
                ly *= 0.7071

            btn = 0
            if d(VK_A):
                btn |= 0x1000
            if d(VK_B):
                btn |= 0x2000
            if d(VK_LB):
                btn |= 0x0100
            if d(VK_RB):
                btn |= 0x0200
            if d(VK_START):
                btn |= 0x0010
            if d(VK_BACK):
                btn |= 0x0020
            if d(VK_LS):
                btn |= 0x0040
            if d(VK_UP):
                btn |= 0x0001
            if d(VK_DOWN):
                btn |= 0x0002
            if d(VK_LEFT):
                btn |= 0x0004
            if d(VK_RIGHT):
                btn |= 0x0008
            if user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000:
                btn |= 0x4000
            if user32.GetAsyncKeyState(VK_RBUTTON) & 0x8000:
                btn |= 0x8000
            if user32.GetAsyncKeyState(VK_MBUTTON) & 0x8000:
                btn |= 0x0080

            lt = 1.0 if d(VK_LT) else 0.0
            rt = 1.0 if d(VK_RT) else 0.0

            if (lx, ly) != prev_l:
                cli.send(f"l {lx:.4f} {ly:.4f}")
                prev_l = (lx, ly)
            if (lt, rt) != prev_tr:
                cli.send(f"lt {lt:.2f}")
                cli.send(f"rt {rt:.2f}")
                prev_tr = (lt, rt)
            if btn != prev_btn:
                cli.send(f"btn 0x{btn:04X}")
                prev_btn = btn

            n += 1
            if n % 60 == 0:
                print(f"\r lx={lx:+.2f} ly={ly:+.2f} btn=0x{btn:04X} lt={lt:.0f} rt={rt:.0f}  ",
                      end="", flush=True)

            sleep = interval - (time.perf_counter() - t0)
            if sleep > 0:
                time.sleep(sleep)
    except KeyboardInterrupt:
        print("\nCtrl+C")
    finally:
        hook.stop()
        try:
            c = PipeClient("acvd_pad")
            c.send("l 0 0")
            c.send("lt 0.00")
            c.send("rt 0.00")
            c.send("btn 0x0000")
        except OSError:
            pass
        print("已释放")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
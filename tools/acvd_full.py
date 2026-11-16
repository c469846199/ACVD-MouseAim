"""
tools/acvd_full.py — ACVD 完整控制程序（键盘 + 鼠标 + 火控圈闭环）

## 架构（参照 Workbuddy 的 pad_bridge，但修正两处）

Workbuddy 的做法（有效部分，照搬）：
    GetAsyncKeyState 读键盘
    → vgamepad 虚拟手柄（全范围模拟摇杆 -32767..32767）

它的两个问题（本程序修正）：
    1. GetAsyncKeyState 在非交互会话读不到键 → 改用 WH_KEYBOARD_LL 钩子
    2. 闭环读 body（机体），有惯性会抖 → 改读 aim（火控圈）

## 输入映射（玩家给出的键位表）

    A=空格  B=C  X=鼠标左键  Y=鼠标右键
    LB=Tab  RB=3  LT=Q  RT=E   LS=Shift  RS=鼠标中键
    START=X  BACK=Z
    WASD = 左摇杆     方向键 = 十字键

## 关键设计：右摇杆

    右摇杆 X = 鼠标位移累积成 target_yaw，减去火控圈 aim_yaw，
              乘比例系数 → 输出 -1..1 的模拟摇杆值
              （全范围，不是开关量）

## 热键

    F8  准星归中    F9  暂停鼠标    F12 退出
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import math
import sys
import time

__import__("sys").path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))

from acvd.aim import AimTracker
from acvd.keyhook import KeyHook
from acvd.pipe import PipeClient
from acvd.winmem import Process

user32 = ctypes.WinDLL("user32", use_last_error=True)

# ---- 键位（玩家键位表）---------------------------------------------
VK_A = 0x20        # 空格 → A
VK_B = 0x43        # C    → B
VK_X = 0x10        # Shift→ 注意：键位表里 X=鼠标左键，Shift=LS
VK_Y = 0x52        # R    → 未在表里，暂留
VK_LB = 0x09       # Tab  → LB
VK_RB = 0x33       # 3    → RB
VK_LT = 0x51       # Q    → LT
VK_RT = 0x45       # E    → RT
VK_LS = 0x10       # Shift→ LS
VK_START = 0x58    # X    → START
VK_BACK = 0x5A     # Z    → BACK
VK_W, VK_S, VK_AA, VK_D = 0x57, 0x53, 0x41, 0x44
VK_UP, VK_DOWN, VK_LEFT, VK_RIGHT = 0x26, 0x28, 0x25, 0x27
VK_LBUTTON, VK_RBUTTON, VK_MBUTTON = 0x01, 0x02, 0x04

# ---- 闭环参数 -------------------------------------------------------
SENS_YAW = 0.10
ERR_DEAD = 0.15
MAX_LEAD = 50.0
K_P = 0.045
STICK_MIN = 0.0      # 现在有全范围，不需要最低值
SMOOTH = 0.25
LOOP_HZ = 250


def wrap180(d):
    return (d + 180.0) % 360.0 - 180.0


def clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    try:
        cli = PipeClient("acvd_pad")
        cli.send("status")
    except OSError:
        print("✗ 守护进程没跑：python tools\\pad_daemon.py")
        return 1

    proc = Process.attach("xenia_canary")
    print("定位火控圈 ...", flush=True)
    tracker = AimTracker(proc)
    if not tracker.locate():
        print("✗ 定位失败（游戏在场景里吗？）")
        return 1
    v = tracker.read()
    if not v:
        print("✗ 读不到初始值")
        return 1
    target_yaw = v[0]
    print(f"✓ 火控圈 0x{tracker.addr:012X}  aim={target_yaw:+.2f}°", flush=True)

    hook = KeyHook()
    if not hook.start():
        print("! 键盘钩子启动失败")
        return 1
    print("✓ 键盘钩子已启动", flush=True)

    pt = wt.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    saved = (pt.x, pt.y)
    center = (user32.GetSystemMetrics(0) // 2, user32.GetSystemMetrics(1) // 2)
    user32.SetCursorPos(*center)

    print("\n" + "=" * 56)
    print(" 完整控制已启动")
    print(" 空格=A C=B 左键=X 右键=Y Tab=LB 3=RB Q=LT E=RT")
    print(" Shift=LS X=START Z=BACK WASD=移动 方向键=十字键")
    print(" 鼠标=右摇杆(闭环)")
    print(" F8 归中 | F9 暂停鼠标 | F12 退出")
    print("=" * 56 + "\n", flush=True)

    last_out = 0.0
    paused = False
    frames = 0
    t0 = time.perf_counter()
    interval = 1.0 / LOOP_HZ
    prev_btn = -1
    prev_lx = prev_ly = 0.0
    prev_lt = prev_rt = -1.0

    try:
        while True:
            lt0 = time.perf_counter()

            if hook.is_down(0x77):
                v = tracker.read()
                if v:
                    target_yaw = v[0]
                    last_out = 0.0
                print(f"\n[F8] 归中 = {target_yaw:+.2f}°", flush=True)
                time.sleep(0.2)
            if hook.is_down(0x78):
                paused = not paused
                print(f"\n[F9] 鼠标{'暂停' if paused else '恢复'}", flush=True)
                time.sleep(0.2)
            if hook.is_down(0x7B):
                print("\n[F12] 退出", flush=True)
                break

            d = hook.is_down

            # 鼠标 → 目标角
            if not paused:
                p = wt.POINT()
                if user32.GetCursorPos(ctypes.byref(p)):
                    dx = p.x - center[0]
                    if dx:
                        target_yaw += dx * SENS_YAW
                    if dx or p.y != center[1]:
                        user32.SetCursorPos(*center)

            v = tracker.read()
            if v is None:
                time.sleep(0.02)
                continue
            aim_yaw, _ = v

            error = wrap180(target_yaw - aim_yaw)
            if abs(error) > MAX_LEAD:
                target_yaw = aim_yaw + MAX_LEAD * (1 if error > 0 else -1)
                error = wrap180(target_yaw - aim_yaw)

            if abs(error) < ERR_DEAD:
                desired = 0.0
            else:
                desired = clamp(K_P * error, -1.0, 1.0)
            out = last_out + (desired - last_out) * SMOOTH
            last_out = out

            # ---- 其余输入 ----
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
                btn |= 0x1000        # A
            if d(VK_B):
                btn |= 0x2000        # B
            if d(VK_LB):
                btn |= 0x0100        # LB
            if d(VK_RB):
                btn |= 0x0200        # RB
            if d(VK_START):
                btn |= 0x0010        # START
            if d(VK_BACK):
                btn |= 0x0020        # BACK
            if d(VK_LS):
                btn |= 0x0040        # LS
            if d(VK_UP):
                btn |= 0x0001
            if d(VK_DOWN):
                btn |= 0x0002
            if d(VK_LEFT):
                btn |= 0x0004
            if d(VK_RIGHT):
                btn |= 0x0008
            # 鼠标键
            if user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000:
                btn |= 0x4000        # X
            if user32.GetAsyncKeyState(VK_RBUTTON) & 0x8000:
                btn |= 0x8000        # Y
            if user32.GetAsyncKeyState(VK_MBUTTON) & 0x8000:
                btn |= 0x0080        # RS

            lt = 1.0 if d(VK_LT) else 0.0
            rt = 1.0 if d(VK_RT) else 0.0

            if not a.dry_run:
                cli.send(f"r {out:.4f} 0")
                if (lx, ly) != (prev_lx, prev_ly):
                    cli.send(f"l {lx:.4f} {ly:.4f}")
                    prev_lx, prev_ly = lx, ly
                if (lt, rt) != (prev_lt, prev_rt):
                    cli.send(f"lt {lt:.2f}")
                    cli.send(f"rt {rt:.2f}")
                    prev_lt, prev_rt = lt, rt
                if btn != prev_btn:
                    cli.send(f"btn 0x{btn:04X}")
                    prev_btn = btn

            frames += 1
            if frames % 25 == 0:
                print(f"\r aim={aim_yaw:+8.2f}° tgt={target_yaw:+8.2f}° "
                      f"err={error:+7.2f}° stick={out:+.3f} btn=0x{btn:04X}   ",
                      end="", flush=True)

            sleep = interval - (time.perf_counter() - lt0)
            if sleep > 0:
                time.sleep(sleep)

    except KeyboardInterrupt:
        print("\nCtrl+C")
    finally:
        hook.stop()
        user32.SetCursorPos(*saved)
        if not a.dry_run:
            try:
                c = PipeClient("acvd_pad")
                c.send("r 0 0")
                c.send("l 0 0")
                c.send("lt 0.00")
                c.send("rt 0.00")
                c.send("btn 0x0000")
            except OSError:
                pass
        dt = time.perf_counter() - t0
        print(f"\n运行 {dt:.1f}s，{frames} 帧，{frames/dt:.0f} Hz")
        proc.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
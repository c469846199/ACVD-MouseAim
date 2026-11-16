"""
tools/acvd_control.py — ACVD 鼠标控制主程序

## 架构

    键盘 + 鼠标 (acvd.input_layer)
         │
         ├─ 鼠标 X 位移 → target_yaw 累积
         │                    │
         │                    └→ error = wrap180(target - aim_yaw)  ← 读内存
         │                                        │
         │                                        └→ 右摇杆 X 输出
         │
         └─ 其余按键 → XInput 按钮位
                              │
                              └→ 管道 → pad_daemon（持有虚拟手柄）

## 键位（玩家指定）

    A=空格  B=C  X=Shift  Y=R    L1=Tab  R1=E  L2=左键  R2=右键
    L3=Ctrl  R3=中键  BACK=Z  START=X    WASD=左摇杆
    鼠标 XY = 右摇杆

## 热键

    F8  目标重置为当前火控圈朝向
    F9  暂停/恢复鼠标控制
    F12 退出

## 运行前提

    python tools\\pad_daemon.py        （先在另一个窗口启动）
    Xenia hid = "xinput"
"""

from __future__ import annotations

import argparse
import sys
import time

__import__("sys").path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))

from acvd.aim import AimTracker
from acvd.input_layer import InputLayer
from acvd.pipe import PipeClient
from acvd.winmem import Process

# ---- 闭环参数（可调） ------------------------------------------------
SENS_YAW = 0.12        # 鼠标每像素多少度
ERR_DEAD = 0.20        # 误差死区（度）
MAX_LEAD = 50.0        # 目标最大领先角
K_P = 0.05             # 比例系数
STICK_MIN = 0.45       # 游戏右摇杆死区约 0.42
SMOOTH = 0.30          # 输出平滑
LOOP_HZ = 250

# 虚拟手柄按钮位（与 daemon 的 _NAME_TO_BIT 对应）
BTN_START = 0x0010
BTN_BACK = 0x0020
BTN_LS = 0x0040
BTN_RS = 0x0080
BTN_LB = 0x0100
BTN_RB = 0x0200
BTN_A = 0x1000
BTN_B = 0x2000
BTN_X = 0x4000
BTN_Y = 0x8000


def wrap180(d: float) -> float:
    return (d + 180.0) % 360.0 - 180.0


def clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="不输出摇杆/按键")
    ap.add_argument("--hz", type=float, default=LOOP_HZ)
    ap.add_argument("--sens", type=float, default=SENS_YAW)
    a = ap.parse_args()

    try:
        cli = PipeClient("acvd_pad")
        cli.send("status")
    except OSError:
        print("✗ 虚拟手柄守护进程没在跑。先运行： python tools\\pad_daemon.py")
        return 1

    proc = Process.attach("xenia_canary")
    print("定位火控圈 ...", flush=True)
    tracker = AimTracker(proc)
    if not tracker.locate():
        print("✗ 定位失败（游戏是否在场景里？）")
        return 1
    v = tracker.read()
    if not v:
        print("✗ 读不到初始值")
        return 1
    target_yaw = v[0]
    print(f"✓ 火控圈 0x{tracker.addr:012X}  aim_yaw={target_yaw:+.2f}°", flush=True)

    layer = InputLayer()
    layer.start()

    import ctypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)

    print("\n" + "=" * 58)
    print(" 鼠标控制火控圈  已启动")
    print("  F8 重置目标 | F9 暂停鼠标 | F12 退出")
    print("=" * 58 + "\n", flush=True)

    last_out = 0.0
    paused = False
    frames = 0
    t0 = time.perf_counter()
    interval = 1.0 / a.hz
    prev_btn = -1

    try:
        while True:
            loop_t0 = time.perf_counter()

            if user32.GetAsyncKeyState(0x77) & 0x8000:      # F8
                v = tracker.read()
                if v:
                    target_yaw = v[0]
                    last_out = 0.0
                print(f"\n[F8] 目标重置 = {target_yaw:+.2f}°", flush=True)
                time.sleep(0.2)

            if user32.GetAsyncKeyState(0x78) & 0x8000:      # F9
                paused = not paused
                layer.enabled = not paused
                print(f"\n[F9] 鼠标{'已暂停' if paused else '已恢复'}", flush=True)
                time.sleep(0.2)

            if user32.GetAsyncKeyState(0x7B) & 0x8000:      # F12
                print("\n[F12] 退出", flush=True)
                break

            inp = layer.poll()
            v = tracker.read()
            if v is None:
                time.sleep(0.02)
                continue
            aim_yaw, _ = v

            if not paused and inp["mouse_dx"]:
                target_yaw += inp["mouse_dx"] * a.sens

            error = wrap180(target_yaw - aim_yaw)
            if abs(error) > MAX_LEAD:
                target_yaw = aim_yaw + MAX_LEAD * (1 if error > 0 else -1)
                error = wrap180(target_yaw - aim_yaw)

            if abs(error) < ERR_DEAD:
                desired = 0.0
            else:
                desired = clamp(K_P * error, -1.0, 1.0)
                if 0 < abs(desired) < STICK_MIN:
                    desired = STICK_MIN if desired > 0 else -STICK_MIN

            out = last_out + (desired - last_out) * SMOOTH
            last_out = out

            if not a.dry_run:
                # 摇杆
                cli.send(f"r {out:.3f} 0")
                # 左摇杆（WASD）
                if inp["lx"] or inp["ly"]:
                    cli.send(f"l {inp['lx']:.3f} {inp['ly']:.3f}")
                # 扳机
                cli.send(f"lt {inp['lt']:.2f}")
                cli.send(f"rt {inp['rt']:.2f}")
                # 按钮（变化时才发）
                if inp["buttons"] != prev_btn:
                    cli.send(f"btn 0x{inp['buttons']:04X}")
                    prev_btn = inp["buttons"]

            frames += 1
            if frames % 25 == 0:
                print(f"\r aim={aim_yaw:+8.2f}° tgt={target_yaw:+8.2f}° "
                      f"err={error:+7.2f}° stick={out:+.3f} "
                      f"btn=0x{inp['buttons']:04X}   ", end="", flush=True)

            sleep = interval - (time.perf_counter() - loop_t0)
            if sleep > 0:
                time.sleep(sleep)

    except KeyboardInterrupt:
        print("\nCtrl+C")
    finally:
        layer.stop()
        if not a.dry_run:
            try:
                c = PipeClient("acvd_pad")
                c.send("r 0 0")
                c.send("l 0 0")
                c.send("btn 0x0000")
            except OSError:
                pass
        dt = time.perf_counter() - t0
        print(f"\n运行 {dt:.1f}s，{frames} 帧，{frames/dt:.0f} Hz")
        proc.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
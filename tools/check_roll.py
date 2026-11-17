"""
tools/check_roll.py — 验证火控圈是否带 roll（横滚）分量

背景（玩家观察）：
    左右移动火控圈时，圈会「少许倾斜」。
    如果真是这样，火控圈的四元数就不是纯 yaw+pitch，还会带 roll。
    这可以当作「火控圈 vs 纯偏航机体」的强区分依据。

四元数分解：
    roll  = atan2(2*(w*z + x*y), 1 - 2*(y*y + z*z))
    纯偏航四元数满足 x=z=0，roll 恒为 0。

用法:
    python tools/check_roll.py --dir left --hold 1.5
"""

from __future__ import annotations

import argparse
import math
import struct
import sys
import time

__import__("sys").path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from acvd.pipe import PipeClient
from acvd.winmem import Process

CANDS = {
    "aim_A": 0x141109DC0,
    "aim_B": 0x1418CDE10,
    "aim_C": 0x1418CEC30,
    "cam":   0x1405F30F0,
}
DIRS = {"up": (0.0, 0.9), "down": (0.0, -0.9), "left": (-0.9, 0.0), "right": (0.9, 0.0)}


def decompose(proc, addr):
    raw = proc.read(addr, 16)
    if not raw or len(raw) < 16:
        return None
    x, y, z, w = struct.unpack(">4f", raw)
    n = x * x + y * y + z * z + w * w
    if abs(n - 1.0) > 0.02:
        return None
    # ZYX 顺序：yaw(Y) -> pitch(X) -> roll(Z)
    sinp = 2 * (w * x - y * z)
    pitch = math.degrees(math.asin(max(-1.0, min(1.0, sinp))))
    yaw = math.degrees(math.atan2(2 * (w * y + x * z), 1 - 2 * (y * y + z * z)))
    roll = math.degrees(math.atan2(2 * (w * z + x * y), 1 - 2 * (x * x + y * y)))
    return {"yaw": yaw, "pitch": pitch, "roll": roll,
            "x": x, "y": y, "z": z, "w": w}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="left", choices=list(DIRS))
    ap.add_argument("--hold", type=float, default=1.5)
    a = ap.parse_args()

    proc = Process.attach("xenia")
    cli = PipeClient("acvd_pad")

    before = {k: decompose(proc, v) for k, v in CANDS.items()}
    print("推杆前:")
    print(f"  {'名称':>7} {'yaw':>9} {'pitch':>8} {'roll':>8}   q=(x, y, z, w)")
    for k, d in before.items():
        if d:
            print(f"  {k:>7} {d['yaw']:+8.2f}° {d['pitch']:+7.2f}° {d['roll']:+7.2f}° "
                  f" ({d['x']:+.4f}, {d['y']:+.4f}, {d['z']:+.4f}, {d['w']:+.4f})")

    vx, vy = DIRS[a.dir]
    print(f"\n推 {a.dir} {a.hold}s ...")
    cli.send(f"rhold {vx} {vy} {a.hold}")
    time.sleep(0.4)

    after = {k: decompose(proc, v) for k, v in CANDS.items()}
    print("\n推杆后:")
    print(f"  {'名称':>7} {'yaw':>9} {'pitch':>8} {'roll':>8}   q=(x, y, z, w)")
    for k, d in after.items():
        if d:
            print(f"  {k:>7} {d['yaw']:+8.2f}° {d['pitch']:+7.2f}° {d['roll']:+7.2f}° "
                  f" ({d['x']:+.4f}, {d['y']:+.4f}, {d['z']:+.4f}, {d['w']:+.4f})")

    print("\n变化:")
    print(f"  {'名称':>7} {'Δyaw':>9} {'Δpitch':>9} {'Δroll':>9}   {'|z|变化':>10}")
    for k in CANDS:
        d0, d1 = before.get(k), after.get(k)
        if not d0 or not d1:
            continue
        dy = (d1["yaw"] - d0["yaw"] + 180) % 360 - 180
        print(f"  {k:>7} {dy:+8.2f}° {d1['pitch']-d0['pitch']:+8.2f}° "
              f"{d1['roll']-d0['roll']:+8.2f}°  {abs(d1['z'])-abs(d0['z']):+9.5f}")

    proc.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
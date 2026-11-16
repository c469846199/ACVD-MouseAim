"""
tools/auto_push_probe.py — 边推杆边高频读相机，验证输入是否到达游戏

先前失败的可能原因：
    a) 虚拟手柄推杆没被游戏接收（XInput 槽被占 / SDL 优先）
    b) 推杆后回中，相机又回位，导致 before/after 相同

本脚本边推杆边每 50ms 读一次相机，能直接看出「推杆期间相机是否在动」。

用法:
    python tools/auto_push_probe.py --dir up --hold 2
"""

from __future__ import annotations

import argparse
import math
import struct
import sys
import time

__import__("sys").path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from acvd.pad import VirtualPad, detect_slots
from acvd.winmem import Process

CAMS = [0x1405F30F0, 0x1418EE520]
DIRS = {"up": (0.0, 1.0), "down": (0.0, -1.0), "left": (-1.0, 0.0), "right": (1.0, 0.0)}


def q_of(proc, addr):
    raw = proc.read(addr, 16)
    if not raw or len(raw) < 16:
        return None
    x, y, z, w = struct.unpack(">4f", raw)
    n = x * x + y * y + z * z + w * w
    if abs(n - 1.0) > 0.02:
        return None
    yaw = math.degrees(math.atan2(2 * (w * y + x * z), 1 - 2 * (y * y + z * z)))
    pit = math.degrees(math.asin(max(-1.0, min(1.0, 2 * (w * x - y * z)))))
    return yaw, pit


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="up", choices=list(DIRS))
    ap.add_argument("--hold", type=float, default=2.0)
    ap.add_argument("--mag", type=float, default=0.9)
    a = ap.parse_args()

    proc = Process.attach("xenia_canary")
    print("推杆前 XInput 槽位:", detect_slots())

    pad = VirtualPad()
    time.sleep(0.15)
    print("创建后槽位:", detect_slots())

    pad.right(0, 0)
    pad.update()
    time.sleep(0.3)
    base = {c: q_of(proc, c) for c in CAMS}
    print("基线相机:", {hex(k): v for k, v in base.items()})

    vx, vy = DIRS[a.dir]
    print(f"\n推 {a.dir} ...")
    pad.right(vx * a.mag, vy * a.mag)
    pad.update()

    t0 = time.perf_counter()
    samples = []
    while time.perf_counter() - t0 < a.hold:
        samples.append({c: q_of(proc, c) for c in CAMS})
        time.sleep(0.05)

    pad.right(0, 0)
    pad.update()
    time.sleep(0.3)
    end = {c: q_of(proc, c) for c in CAMS}
    pad.close()

    for c in CAMS:
        vals = [s[c] for s in samples if s[c]]
        if not vals:
            print(f"0x{c:X}: 采样失败")
            continue
        yaws = [v[0] for v in vals]
        pits = [v[1] for v in vals]
        # 解缠
        uy = [yaws[0]]
        for v in yaws[1:]:
            d = v - uy[-1]
            while d > 180:
                d -= 360
            while d < -180:
                d += 360
            uy.append(uy[-1] + d)
        print(f"\n0x{c:X}  采样 {len(vals)} 次")
        print(f"  yaw   {uy[0]:+9.2f} → {uy[-1]:+9.2f}  摆动 {max(uy)-min(uy):7.2f}°")
        print(f"  pitch {pits[0]:+9.2f} → {pits[-1]:+9.2f}  摆动 {max(pits)-min(pits):7.2f}°")
        print(f"  基线→结束 Δpitch = {end[c][1]-base[c][1]:+.3f}°" if end[c] and base[c] else "")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
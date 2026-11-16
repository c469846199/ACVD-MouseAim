"""
tools/watch2.py — 修正角度环绕后的精确观测

上一版把 yaw 的 ±180 绕圈算成了 358°，导致所有候选的 yaw 范围都失真。
本版：
  * yaw/pitch 全部用「解缠」后的值（unwrap），绕圈不计为跳变
  * 同时输出 pitch 的真实变化范围 —— 火控圈的 pitch 远大于相机
  * 增加相邻帧的相关性：谁对推杆反应最快

用法:
    python tools/watch2.py --seconds 6
"""

from __future__ import annotations

import argparse
import math
import struct
import sys
import time

__import__("sys").path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from acvd.winmem import Process

CANDIDATES = {
    "cam_1":     0x1405F30F0,
    "cam_3":     0x1418EE520,
    "pureyaw_A": 0x1418EE490,
    "pureyaw_B": 0x1405F313C,
    "near1":     0x1405F314C,
    "near2":     0x1405F3150,
}


def read_quat(proc, addr):
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


def unwrap(vals):
    """把跨 ±180 的序列解缠成连续序列。"""
    if not vals:
        return []
    out = [vals[0]]
    for v in vals[1:]:
        d = v - out[-1]
        while d > 180:
            d -= 360
        while d < -180:
            d += 360
        out.append(out[-1] + d)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=6.0)
    ap.add_argument("--hz", type=float, default=40.0)
    a = ap.parse_args()

    proc = Process.attach("xenia")
    series = {k: [] for k in CANDIDATES}

    print(f"采样 {a.seconds}s @ {a.hz}Hz。请【左推一下 → 回中 → 右推一下 → 回中】")
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < a.seconds:
        for k, addr in CANDIDATES.items():
            v = read_quat(proc, addr)
            if v:
                series[k].append(v)
        time.sleep(1.0 / a.hz)

    print("\n" + "=" * 78)
    print(f"{'名称':>10} {'地址':>12} {'yaw摆动':>9} {'pitch摆动':>10} {'yaw最大步进':>10}")
    print("-" * 78)
    for k, addr in CANDIDATES.items():
        vals = series[k]
        if len(vals) < 5:
            print(f"{k:>10} 0x{addr:09X}   读不到")
            continue
        yaws = unwrap([v[0] for v in vals])
        pits = [v[1] for v in vals]
        steps = [abs(b - a) for a, b in zip(yaws, yaws[1:])]
        print(f"{k:>10} 0x{addr:09X} {max(yaws)-min(yaws):8.2f}° "
              f"{max(pits)-min(pits):9.2f}° {max(steps):9.2f}°")
    print("\n火控圈 = yaw 摆动大 + pitch 摆动明显 > 相机")
    proc.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
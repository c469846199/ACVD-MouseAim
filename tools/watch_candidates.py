"""
tools/watch_candidates.py — 高频采样候选四元数，找出「谁跟着右摇杆动」

判断火控圈的依据：
    * 右摇杆推动时，火控圈 yaw 立即响应
    * 机体（纯偏航四元数）跟随，但有惯性延迟
    * 相机在两者之后

所以火控圈应该表现为：对推杆反应最快、幅度最大、且没有 pitch 分量。

用法:
    python tools/watch_candidates.py --seconds 6
"""

from __future__ import annotations

import argparse
import math
import struct
import sys
import time

__import__("sys").path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from acvd.winmem import Process

# 待观测候选（来自周边扫描，yaw/pitch 为当前值）
CANDIDATES = {
    "cam_active_1": 0x1405F30F0,     # yaw +160.55 pitch +6.63  (含俯仰 = 相机)
    "cam_active_3": 0x1418EE520,     # yaw +160.55 pitch +6.63  (相机副本)
    "pure_yaw_A":   0x1418EE490,     # yaw +160.78 pitch  0.00  纯偏航
    "pure_yaw_B":   0x1405F313C,     # yaw +140.86 pitch  0.00  纯偏航
    "cam_near1":    0x1405F314C,     # yaw -179.51 pitch -4.41
    "cam_near2":    0x1405F3150,     # yaw -179.52 pitch +12.49
    "mystery_314":  0x1418EE784,     # yaw -178.86 pitch +2.29
}


def read_quat(proc, addr):
    raw = proc.read(addr, 16)
    if not raw or len(raw) < 16:
        return None
    x, y, z, w = struct.unpack(">4f", raw)
    n = x * x + y * y + z * z + w * w
    if abs(n - 1.0) > 0.01:
        return None
    yaw = math.degrees(math.atan2(2 * (w * y + x * z), 1 - 2 * (y * y + z * z)))
    pit = math.degrees(math.asin(max(-1.0, min(1.0, 2 * (w * x - y * z)))))
    return yaw, pit


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=6.0)
    ap.add_argument("--hz", type=float, default=30.0)
    a = ap.parse_args()

    proc = Process.attach("xenia")
    series = {k: [] for k in CANDIDATES}

    print(f"采样 {a.seconds}s @ {a.hz}Hz。请【快速推右摇杆并回中】，反复几次。")
    t0 = time.perf_counter()
    interval = 1.0 / a.hz
    while time.perf_counter() - t0 < a.seconds:
        for k, addr in CANDIDATES.items():
            v = read_quat(proc, addr)
            series[k].append(v)
        time.sleep(interval)

    print("\n================ 结果 ================")
    print(f"{'名称':>14} {'地址':>12} {'yaw范围':>10} {'pitch范围':>11} {'最大跳变':>9}")
    print("-" * 64)
    for k, addr in CANDIDATES.items():
        vals = [v for v in series[k] if v]
        if len(vals) < 3:
            print(f"{k:>14} 0x{addr:09X}   (读不到)")
            continue
        yaws = [v[0] for v in vals]
        pits = [v[1] for v in vals]
        jumps = [abs(b - a) for a, b in zip(yaws, yaws[1:])]
        jumps = [j for j in jumps if j < 180]      # 排除跨 ±180 的绕圈
        print(f"{k:>14} 0x{addr:09X} {max(yaws)-min(yaws):9.2f}° "
              f"{max(pits)-min(pits):10.2f}° {max(jumps) if jumps else 0:8.2f}°")

    print("\n说明：火控圈 = yaw范围大 + pitch范围接近0 + 跳变剧烈")
    proc.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""
tools/watch3.py — 双向推杆验证（左/右各推 3 秒，观察响应）

判据（火控圈一定满足）：
    推左 → yaw 单调减小；回中 → 停住；推右 → yaw 单调增大
    推上 → pitch 增大；推下 → pitch 减小

输出每个候选的「分阶段均值」，能一眼看出谁在跟随推杆方向。

用法:
    python tools/watch3.py --phase left
    python tools/watch3.py --phase right
    python tools/watch3.py --phase up
    python tools/watch3.py --phase down
"""

from __future__ import annotations

import argparse
import math
import os
import struct
import sys
import time

__import__("sys").path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from acvd.winmem import Process

CANDIDATES = {
    "cam_1":     0x1405F30F0,
    "pureyaw_A": 0x1418EE490,
    "near1":     0x1405F314C,
    "near2":     0x1405F3150,
    "near3":     0x1418EE784,
}

STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "state")


def read_q(proc, addr):
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
    ap.add_argument("--phase", required=True,
                    choices=["left", "right", "up", "down", "idle"])
    ap.add_argument("--seconds", type=float, default=3.0)
    ap.add_argument("--hz", type=float, default=50.0)
    a = ap.parse_args()

    proc = Process.attach("xenia_canary")
    series = {k: [] for k in CANDIDATES}
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < a.seconds:
        for k, addr in CANDIDATES.items():
            v = read_q(proc, addr)
            if v:
                series[k].append(v)
        time.sleep(1.0 / a.hz)

    os.makedirs(STATE_DIR, exist_ok=True)
    out_path = os.path.join(STATE_DIR, f"watch_{a.phase}.json")

    print(f"\n=== phase={a.phase}  ({len(series['cam_1'])} 帧) ===")
    print(f"{'名称':>10} {'yaw首':>9} {'yaw尾':>9} {'Δyaw':>8} "
          f"{'pitch首':>9} {'pitch尾':>9} {'Δpitch':>8}")
    print("-" * 70)
    result = {}
    for k in CANDIDATES:
        vals = series[k]
        if len(vals) < 3:
            print(f"{k:>10}   读不到")
            continue
        y0, p0 = vals[0]
        y1, p1 = vals[-1]
        dy = (y1 - y0 + 180) % 360 - 180
        dp = p1 - p0
        result[k] = {"dyaw": round(dy, 3), "dpitch": round(dp, 3),
                     "y0": round(y0, 2), "p0": round(p0, 2),
                     "y1": round(y1, 2), "p1": round(p1, 2)}
        print(f"{k:>10} {y0:8.2f}° {y1:8.2f}° {dy:+7.2f}° "
              f"{p0:8.2f}° {p1:8.2f}° {dp:+7.2f}°")

    import json
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=1)
    print(f"\n已保存 {out_path}")
    proc.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
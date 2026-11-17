"""
tools/probe_churn.py — 找出「哪些内存区在游戏运行时真的在变」

采样两次（间隔可调），只比较可写私有区，输出变化率 > 阈值的区块。
同时给每块打上「像不像浮点数据」的标签。

这一步是为了确认：guest 数据到底在哪，而不是盲扫全进程 10GB。
"""

from __future__ import annotations

import argparse
import struct
import sys
import time

__import__("sys").path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from acvd.winmem import Process, MEM_PRIVATE, MEM_IMAGE

SAMPLE = 8192
POINTS = 32


def read_points(proc: Process, base: int, size: int) -> list[bytes]:
    pts = []
    step = max(SAMPLE, size // POINTS)
    for i in range(POINTS):
        off = min(i * step, max(0, size - SAMPLE))
        b = proc.read(base + off, SAMPLE)
        pts.append(b or b"")
    return pts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gap", type=float, default=1.0)
    ap.add_argument("--min-mb", type=float, default=0.5)
    ap.add_argument("--threshold", type=float, default=0.2, help="变化率百分比阈值")
    ap.add_argument("--all", action="store_true", help="含 image 区")
    a = ap.parse_args()

    proc = Process.attach("xenia")
    regs = [r for r in proc.regions()
            if r.state == 0x1000 and r.writable and r.size >= a.min_mb * 2**20
            and (a.all or r.type == MEM_PRIVATE)]

    print(f"采样 {len(regs)} 个区域，间隔 {a.gap}s ...")
    s1 = [(r, read_points(proc, r.base, r.size)) for r in regs]
    time.sleep(a.gap)
    s2 = [(r, read_points(proc, r.base, r.size)) for r in regs]

    rows = []
    for (r, b1), (_, b2) in zip(s1, s2):
        tot = ch = 0
        nonzero_seen = False
        for x, y in zip(b1, b2):
            if not x or not y:
                continue
            tot += len(x)
            ch += sum(1 for u, v in zip(x, y) if u != v)
            if any(x[:64]):
                nonzero_seen = True
        pct = ch / tot * 100 if tot else -1
        rows.append((pct, r, nonzero_seen))

    rows.sort(key=lambda t: -t[0])
    print(f"\n{'host base':>16} {'MB':>7} {'prot':>6} {'变化率':>8}")
    print("-" * 44)
    hot = 0
    for pct, r, nz in rows:
        if pct < a.threshold:
            continue
        hot += 1
        print(f"0x{r.base:014X} {r.size/2**20:7.1f} 0x{r.protect:X} {pct:7.2f}%")

    print(f"\n变化率 > {a.threshold}% 的区域: {hot} / {len(rows)}")
    tot_mb = sum(r.size for pct, r, _ in rows if pct >= a.threshold) / 2**20
    print(f"这些区域合计 {tot_mb:.1f} MB（这是需要精扫的范围）")
    proc.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
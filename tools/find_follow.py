"""
tools/find_follow.py — 找「右摇杆驱动的火控圈角度」

游戏模型（已确认）：
    右摇杆 → 火控圈移动 → 机体/相机跟随
  所以火控圈是输入链最上游的量，推杆时它「单向、平滑、连续」变化。

方法（重点是速度）：
  用 numpy 向量化比对，20Hz 连续采样热区，记录每个 float32 的变化轨迹，
  然后按「单调性 + 平滑性」排序。

性能：239MB 热区，单帧读 + 比对约 0.3s（纯 Python 要 5s+）。

用法:
    python tools/find_follow.py --seconds 5 --hz 15
"""

from __future__ import annotations

import argparse
import sys
import time

import numpy as np

__import__("sys").path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from acvd.winmem import Process, MEM_PRIVATE

CHUNK = 16 * 1024 * 1024


def hot_regions(proc: Process, min_mb: float = 1.0, thresh: float = 2.0, gap: float = 0.4):
    regs = [r for r in proc.regions()
            if r.state == 0x1000 and r.writable and r.type == MEM_PRIVATE
            and r.size >= min_mb * 2**20]

    def sample(r, n=8):
        pts = []
        step = max(4096, r.size // n)
        for i in range(n):
            off = min(i * step, max(0, r.size - 4096))
            pts.append(proc.read(r.base + off, 4096) or b"")
        return pts

    s1 = [(r, sample(r)) for r in regs]
    time.sleep(gap)
    s2 = [(r, sample(r)) for r in regs]
    hot = []
    for (r, b1), (_, b2) in zip(s1, s2):
        tot = ch = 0
        for x, y in zip(b1, b2):
            if x and y and len(x) == len(y):
                tot += len(x)
                ch += sum(1 for u, v in zip(x, y) if u != v)
        if tot and ch / tot * 100 >= thresh:
            hot.append(r)
    return hot


def read_blocks(proc: Process, regions) -> list[tuple[int, np.ndarray]]:
    """返回 [(base_addr, float32 array)]，跳过不可读块。"""
    out = []
    for r in regions:
        pos = r.base
        while pos < r.end:
            w = min(CHUNK, r.end - pos)
            buf = proc.read(pos, w)
            if buf and len(buf) >= 4:
                n = (len(buf) // 4) * 4
                arr = np.frombuffer(buf[:n], dtype=np.float32)
                out.append((pos, arr))
            pos += w
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=5.0)
    ap.add_argument("--hz", type=float, default=15.0)
    ap.add_argument("--thresh", type=float, default=2.0)
    ap.add_argument("--min-mb", type=float, default=1.0)
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--min-range", type=float, default=0.02)
    ap.add_argument("--min-mono", type=float, default=0.85)
    a = ap.parse_args()

    proc = Process.attach("xenia")
    print("筛热区 ...")
    hot = hot_regions(proc, a.min_mb, a.thresh)
    total = sum(r.size for r in hot)
    print(f"  热区 {len(hot)} 个 / {total/2**20:.0f} MB")

    print("第一次读取（建立基准）...")
    t = time.perf_counter()
    prev = read_blocks(proc, hot)
    print(f"  {time.perf_counter()-t:.2f}s")

    # addr -> [首值, ...轨迹]
    traj: dict[int, list[float]] = {}
    frames = 0
    interval = 1.0 / a.hz
    t0 = time.perf_counter()
    print(f"采样 {a.seconds}s @ {a.hz}Hz —— 请持续按住右摇杆往一个方向")
    next_t = time.perf_counter()
    while time.perf_counter() - t0 < a.seconds:
        cur = read_blocks(proc, hot)
        if len(cur) != len(prev):
            prev = cur
            continue
        for (base, pa), (_, ca) in zip(prev, cur):
            diff = pa != ca
            nan_ok = ~(np.isnan(pa) | np.isnan(ca))
            idx = np.flatnonzero(diff & nan_ok)
            for i in idx:
                addr = base + int(i) * 4
                traj.setdefault(addr, [float(pa[i])]).append(float(ca[i]))
        prev = cur
        frames += 1
        next_t += interval
        sleep = next_t - time.perf_counter()
        if sleep > 0:
            time.sleep(sleep)

    print(f"  采样 {frames} 帧，{len(traj):,} 个地址有变化")

    results = []
    for addr, vals in traj.items():
        if len(vals) < 6:
            continue
        rng = max(vals) - min(vals)
        if rng < a.min_range:
            continue
        diffs = [b - x for x, b in zip(vals, vals[1:]) if b != x]
        if len(diffs) < 4:
            continue
        up = sum(1 for d in diffs if d > 0)
        mono = max(up, len(diffs) - up) / len(diffs)
        if mono < a.min_mono:
            continue
        d2 = [abs(c - 2 * b + x) for x, b, c in zip(vals, vals[1:], vals[2:])]
        smooth = sum(d2) / len(d2)
        results.append((mono, smooth, addr, vals, rng))

    results.sort(key=lambda t: (-t[0], t[1]))
    print(f"\n单调(>={a.min_mono})候选: {len(results)} 个\n")
    print(f"{'地址':>14} {'单调':>6} {'平滑':>9} {'幅度':>8}  首/中/尾")
    print("-" * 84)
    for mono, smooth, addr, vals, rng in results[: a.top]:
        n = len(vals)
        print(f"0x{addr:012X} {mono:6.2f} {smooth:9.5f} {rng:8.3f}  "
              f"{vals[0]:+.3f} / {vals[n//2]:+.3f} / {vals[-1]:+.3f}")

    proc.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
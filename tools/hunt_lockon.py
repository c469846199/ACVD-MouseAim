"""
tools/hunt_lockon.py — 找锁定圈角度

流程（全自动，不需要人工点 Next Scan）：
  1. 首次扫描：在 guest 直接映射区找所有「合理角度范围」的 f32
  2. 让右摇杆转动（pivot 或指定方向），制造锁定圈变化
  3. 收敛：只保留「变化量显著」的候选
  4. 再静止一段时间，剔除「不是锁定圈但在动」的噪声（生命值、计时器等）
  5. 输出候选，附带上下文结构体 dump

用法:
    python tools/hunt_lockon.py --dir right --dur 2
    python tools/hunt_lockon.py --calibrate     # 只热身，打印扫描耗时
"""

from __future__ import annotations

import os
import argparse
import os
import struct
import os
import subprocess
import os
import sys
import os
import time

__import__("sys").path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from acvd.winmem import Process, MEM_PRIVATE

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable

# 角度搜索范围：弧度 ±2π 覆盖所有可能；度数 ±720 也覆盖。
# 用宽松范围先粗筛，后续靠「变化」收敛。
HI = 8.0


def pad(*args: str):
    subprocess.run([PY, f"{ROOT}\\tools\\padctl.py", *args], cwd=ROOT, timeout=30,
                   capture_output=True)


class Hunter:
    def __init__(self, proc: Process):
        self.proc = proc
        self.regions = [r for r in proc.regions()
                        if r.state == 0x1000 and r.writable and r.type == MEM_PRIVATE
                        and r.base < 0x100000000]

    def _read_region(self, r, chunk=8 * 1024 * 1024):
        pos = r.base
        while pos < r.end:
            want = min(chunk, r.end - pos)
            buf = self.proc.read(pos, want)
            if buf:
                yield pos, buf
            pos += want

    def first_scan(self, lo: float, hi: float) -> dict[int, float]:
        """找所有 f32 在 [lo, hi] 的可写位置。"""
        out: dict[int, float] = {}
        for r in self.regions:
            for base, buf in self._read_region(r):
                n = len(buf) // 4
                for i in range(n):
                    off = i * 4
                    v = struct.unpack_from("<f", buf, off)[0]
                    if lo <= v <= hi:
                        out[base + off] = v
        return out

    def snapshot(self, cands: dict[int, float]) -> dict[int, float]:
        out = {}
        for a in cands:
            d = self.proc.read(a, 4)
            if d and len(d) == 4:
                out[a] = struct.unpack("<f", d)[0]
        return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="right", choices=["right", "left", "up", "down"])
    ap.add_argument("--dur", type=float, default=1.5)
    ap.add_argument("--lo", type=float, default=-HI)
    ap.add_argument("--hi", type=float, default=HI)
    ap.add_argument("--min-delta", type=float, default=0.02,
                    help="转动后变化量下限（弧度）")
    ap.add_argument("--top", type=int, default=40)
    a = ap.parse_args()

    proc = Process.attach("xenia_canary")
    h = Hunter(proc)
    print(f"[1/4] 首次扫描 f32 ∈ [{a.lo}, {a.hi}] ...")
    t0 = time.perf_counter()
    before = h.first_scan(a.lo, a.hi)
    print(f"      候选 {len(before):,} 个，耗时 {time.perf_counter()-t0:.2f}s")

    if not before:
        print("✗ 没有候选。检查：游戏是否在训练场？guest 映射是否变化？")
        return 1

    print(f"[2/4] 让右摇杆 {a.dir} 转动 {a.dur}s ...")
    vx, vy = {"right": (0.9, 0.0), "left": (-0.9, 0.0),
              "up": (0.0, 0.9), "down": (0.0, -0.9)}[a.dir]
    pad("r", str(vx), str(vy))
    time.sleep(a.dur)
    pad("center")
    time.sleep(0.3)

    print("[3/4] 比对变化 ...")
    after = h.snapshot(before)
    changed = {addr: (before[addr], after[addr])
               for addr in before
               if addr in after and abs(after[addr] - before[addr]) >= a.min_delta}
    print(f"      变化 {len(changed):,} 个")

    print("[4/4] 静止 1.5s，剔除「非锁定圈也在动」的噪声 ...")
    time.sleep(1.5)
    still = h.snapshot({k: v for k, v in changed.items()})
    quiet = {addr: changed[addr] for addr in changed
             if addr in still and abs(still[addr] - changed[addr][1]) < 0.005}
    moving = {addr: changed[addr] for addr in changed
              if addr in still and addr not in quiet}
    print(f"      静止后仍在变 {len(moving):,}  |  静止后稳定 {len(quiet):,}")

    def show(d: dict, title: str):
        print(f"\n=== {title} (共 {len(d)}) ===")
        rows = sorted(d.items(), key=lambda kv: -abs(kv[1][1] - kv[1][0]))
        for addr, (v0, v1) in rows[: a.top]:
            print(f"  0x{addr:08X}  {v0:+.6f} -> {v1:+.6f}  Δ={v1-v0:+.6f}")

    show(quiet, "稳定候选（最可能是锁定圈）")
    show(moving, "转动中有变但静止仍在动（可能是计时器/动画）")

    proc.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
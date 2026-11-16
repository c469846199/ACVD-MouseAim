"""
tools/find_literal.py — 按「已知数值」反查地址（CE 的 Exact Value 扫描）

画面上的 24661 / 10000 / 500 是火控 HUD 的读数，它们来自内存里的整数。
用精确值扫描找它们，比「差异比对」稳定得多：
  * 相机移动不影响整数读数
  * 命中数极少（通常个位数到几十）

用法:
    python tools/find_literal.py 24661
    python tools/find_literal.py 24661 --type u32 --top 20
    python tools/find_literal.py 10000 --type u16
"""

from __future__ import annotations

import argparse
import struct
import sys
import time

__import__("sys").path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from acvd.winmem import Process, MEM_PRIVATE

CHUNK = 8 * 1024 * 1024


def regions_of(proc: Process, min_mb: float = 0.5, include_image: bool = False):
    return [r for r in proc.regions()
            if r.state == 0x1000 and r.writable and r.size >= min_mb * 2**20
            and (include_image or r.type == MEM_PRIVATE)]


def scan_value(proc, regions, target: int, size: int, aligned: int = 2) -> list[tuple[int, bytes]]:
    """返回 (地址, 目标窗口字节)，值 == target。"""
    pack = {1: "<B", 2: "<H", 4: "<I", 8: "<Q"}[size]
    needle = struct.pack(pack, target)
    hits = []
    for r in regions:
        pos = r.base
        while pos < r.end:
            w = min(CHUNK, r.end - pos)
            buf = proc.read(pos, w)
            if buf:
                start = 0
                while True:
                    i = buf.find(needle, start)
                    if i < 0:
                        break
                    if i % aligned == 0:
                        hits.append((pos + i, buf[max(0, i - 16): i + 32]))
                    start = i + 1
            pos += w
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("value", type=int)
    ap.add_argument("--type", default="u32", choices=["u8", "u16", "u32", "u64"])
    ap.add_argument("--min-mb", type=float, default=0.5)
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--sweep", type=int, default=0, help="连续采样秒数，验证哪些命中在变化")
    a = ap.parse_args()

    size = {"u8": 1, "u16": 2, "u32": 4, "u64": 8}[a.type]
    proc = Process.attach("xenia_canary")
    regions = regions_of(proc, a.min_mb)
    print(f"扫描 {len(regions)} 个区域 / {sum(r.size for r in regions)/2**20:.0f} MB "
          f"查找 {a.type} == {a.value}")

    t0 = time.perf_counter()
    hits = scan_value(proc, regions, a.value, size)
    print(f"命中 {len(hits)} 个，耗时 {time.perf_counter()-t0:.2f}s\n")

    traj: dict[int, list[int]] = {}
    if a.sweep:
        print(f"连续采样 {a.sweep}s，观察哪些命中会变（火控读数会变，常量不会）...")
        traj = {addr: [] for addr, _ in hits}
        t0 = time.perf_counter()
        fmt = {1: "<B", 2: "<H", 4: "<I", 8: "<Q"}[size]
        while time.perf_counter() - t0 < a.sweep:
            for addr, _ in hits:
                d = proc.read(addr, size)
                if d and len(d) == size:
                    traj[addr].append(struct.unpack(fmt, d)[0])
            time.sleep(0.1)
        hits.sort(key=lambda h: -len(set(traj.get(h[0], []))))

    for addr, ctx in hits[: a.top]:
        vals = traj.get(addr, [])
        uniq = len(set(vals)) if vals else 0
        print(f"0x{addr:012X}" + (f"  变化{uniq}种值" if a.sweep else ""))
        if ctx:
            for off in range(0, len(ctx), 16):
                chunk = ctx[off:off + 16]
                if len(chunk) < 16:
                    continue
                hexs = " ".join(f"{b:02X}" for b in chunk)
                fs = struct.unpack("<4f", chunk)
                print(f"     {hexs} | " + " ".join(f"{v:+9.2f}" for v in fs))
        print()

    proc.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
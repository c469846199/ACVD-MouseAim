"""
tools/hunt_flag.py — 找软锁定标志位 / 目标 ID（正确版）

为什么不能「扫所有 0/1 字节」：
    内存里绝大部分字节都是 0 或 1（填充、布尔、小整数），
    全进程会产生几百万个候选，没法收敛。

正确做法（CE 的 Next Scan 思路，但自动化）：
    1. baseline：全量快照目标区域
    2. 你套住敌人 → 再全量快照 → 差集 = 值变化的位置
    3. 你移开敌人 → 再全量快照 → 差集再和上次取交
    4. 只有「两次都变化」的地址才可能是锁定标志

真正的优点是差集通常只有几千个，几次翻转就能收敛到几十个。

用法（严格按提示操作）：
    python tools/hunt_flag.py --snap lock0    # 移开敌人，跑到这里
    python tools/hunt_flag.py --snap lock1    # 套住敌人（画面出现锁定框），跑到这里
    python tools/hunt_flag.py --snap lock0s   # 再移开，跑到这里
    python tools/hunt_flag.py --report
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys
import time

__import__("sys").path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from acvd.winmem import Process, MEM_PRIVATE

STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "state")
SNAP_FILE = os.path.join(STATE_DIR, "flag_snaps.json")
CHUNK = 8 * 1024 * 1024


def active_regions(proc: Process, min_mb: float = 1.0):
    return [r for r in proc.regions()
            if r.state == 0x1000 and r.writable and r.type == MEM_PRIVATE
            and r.size >= min_mb * 2**20]


def snap(proc: Process, regions) -> dict[int, bytes]:
    """整块快照，存成 {base: bytes}。"""
    out = {}
    for r in regions:
        pos = r.base
        while pos < r.end:
            want = min(CHUNK, r.end - pos)
            b = proc.read(pos, want)
            if b:
                out[pos] = b
            pos += want
    return out


def diff_bases(a: dict[int, bytes], b: dict[int, bytes]) -> list[int]:
    """返回所有发生变化的 4 字节对齐地址。"""
    hits = []
    for base in a:
        ba, bb = a[base], b.get(base)
        if bb is None or len(ba) != len(bb):
            continue
        n = min(len(ba), len(bb)) // 4
        for i in range(n):
            off = i * 4
            if ba[off:off + 4] != bb[off:off + 4]:
                hits.append(base + off)
    return hits


def load() -> dict:
    if not os.path.exists(SNAP_FILE):
        return {}
    with open(SNAP_FILE, encoding="utf-8") as f:
        return json.load(f)


def save(d: dict):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(SNAP_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f)


def read_vals(proc: Process, addrs: list[int]) -> dict[int, bytes]:
    out = {}
    for a in addrs:
        d = proc.read(a, 4)
        if d and len(d) == 4:
            out[a] = d
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snap", choices=["lock0", "lock1", "lock0s"])
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--min-mb", type=float, default=1.0)
    ap.add_argument("--max-report", type=int, default=60)
    a = ap.parse_args()

    proc = Process.attach("xenia")
    regions = active_regions(proc, a.min_mb)
    total = sum(r.size for r in regions)
    print(f"区域 {len(regions)} 个，{total/2**20:.0f} MB")

    st = load()

    if a.report:
        cands = st.get("candidates", [])
        print(f"\n最终候选 {len(cands)} 个\n")
        for addr in cands[: a.max_report]:
            ctx = proc.read(addr - 24, 56)
            print(f"0x{addr:012X}")
            if not ctx:
                continue
            for off in range(0, 56, 16):
                chunk = ctx[off:off + 16]
                if len(chunk) < 16:
                    continue
                hexs = " ".join(f"{b:02X}" for b in chunk)
                fa = struct.unpack("<4f", chunk)
                fstr = " ".join(f"{v:+9.3f}" for v in fa)
                mark = " <<< 目标" if 16 <= off < 32 else ""
                print(f"  {off-24:+4d}: {hexs} | {fstr}{mark}")
            print()
        return 0

    if not a.snap:
        print(__doc__)
        return 2

    print("快照中 ...")
    t0 = time.perf_counter()
    cur = snap(proc, regions)
    print(f"  完成 {time.perf_counter()-t0:.2f}s")

    if a.snap == "lock0":
        st["lock0"] = {"bases": {str(k): v.hex() for k, v in cur.items()}}
        save(st)
        print("已保存 baseline。现在【套住敌人】，然后跑 --snap lock1")
        proc.close()
        return 0

    prev_key = "lock0" if a.snap == "lock1" else "lock1"
    if prev_key not in st:
        print(f"✗ 缺少 {prev_key}，按顺序来")
        return 1

    prev_bases = {int(k): bytes.fromhex(v) for k, v in st[prev_key]["bases"].items()}
    d = diff_bases(prev_bases, cur)
    print(f"与 {prev_key} 相比，变化位置 {len(d):,} 个")

    st[a.snap] = {"bases": {str(k): v.hex() for k, v in cur.items()}}
    st.setdefault("diffs", {})[a.snap] = d

    if a.snap == "lock1":
        save(st)
        print("已保存。现在【移开敌人】，然后跑 --snap lock0s")
    else:
        d1 = set(st["diffs"]["lock1"])
        both = [x for x in d if x in d1]
        st["candidates"] = sorted(both)
        save(st)
        print(f"两次都变化的位置: {len(both)} 个  → 这些就是候选")
        print("用 --report 查看上下文")

    proc.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
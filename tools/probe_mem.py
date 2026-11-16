"""
tools/probe_mem.py — 判别 Xenia 各内存区的用途

思路：
  * 静态页：内容长时间不变 → 代码 / 常量 / 空池
  * 动态页：两帧之间内容大量变化 → 活动数据（堆、栈）
  * 全零 / 全 0x80 开头 → 未使用
"""

from __future__ import annotations

import struct
import sys
import time

__import__("sys").path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from acvd.winmem import Process, MEM_PRIVATE, MEM_IMAGE, MEM_MAPPED

SAMPLE = 4096           # 每区抽 4KB 比较
SAMPLE_POINTS = 16      # 分散抽样点


def classify(buf: bytes) -> str:
    if not buf:
        return "unreadable"
    n = len(buf)
    zeros = buf.count(0)
    if zeros / n > 0.98:
        return "zero"
    # 0x80000000 重复
    if n >= 4 and buf[:4] == b"\x00\x00\x00\x80" and all(x == buf[i % 4] for i, x in enumerate(buf[:256])):
        return "negzero"
    # 看起来像浮点：大量值在合理范围
    valid = 0
    for v in struct.unpack(f"<{min(n//4, 256)}f", buf[: (min(n // 4, 256)) * 4]):
        if v == v and (abs(v) < 1e6 or v == float("inf")):
            valid += 1
    if valid > 200:
        return "floatish"
    return "binary"


def main() -> int:
    proc = Process.attach("xenia_canary")

    # 只看 >= 1MB 的已提交私有区 + image 区
    regs = [r for r in proc.regions()
            if r.state == 0x1000 and r.size >= 1024 * 1024
            and r.type in (MEM_PRIVATE, MEM_IMAGE)]

    print(f"候选区域 {len(regs)} 个\n")

    def snapshot(r):
        pts = []
        step = max(SAMPLE, r.size // SAMPLE_POINTS)
        for i in range(SAMPLE_POINTS):
            off = min(i * step, r.size - SAMPLE)
            b = proc.read(r.base + off, SAMPLE)
            pts.append(b if b else b"")
        return pts

    results = []
    for r in regs:
        s1 = snapshot(r)
        results.append((r, s1))

    time.sleep(0.5)
    print(f"{'host base':>16} {'MB':>8} {'prot':>6} {'type':>8} {'内容':>10} {'0.5s变化':>9}")
    print("-" * 78)
    for r, s1 in results:
        s2 = snapshot(r)
        diff = 0
        total = 0
        for a, b in zip(s1, s2):
            if not a or not b:
                continue
            total += len(a)
            diff += sum(1 for x, y in zip(a, b) if x != y)
        pct = (diff / total * 100) if total else -1
        kind = classify(s1[0] if s1 and s1[0] else b"")
        kind2 = classify(s2[0] if s2 and s2[0] else b"")
        types = {MEM_PRIVATE: "PRIVATE", MEM_IMAGE: "IMAGE", MEM_MAPPED: "MAPPED"}
        print(f"0x{r.base:014X} {r.size/2**20:8.1f} 0x{r.protect:X} {types.get(r.type,'?'):>8} "
              f"{kind:>10} {pct:8.2f}%" + ("  <== 动态" if pct > 1.0 else ""))
    proc.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""tools/enumerate_cameras.py — 列出所有「相机结构」候选

判据（严格）：
    +0x00  position 4f, w == 1.0, |x|或|z| > 50
    +0x10  quaternion 4f, 模长 ≈ 1
    +0x20  scale 4f, 前 3 项在 0.5~2.0
    +0x30  FOV == 45.0 (大端)

输出所有符合的地址，用于判断相机结构是否唯一。
"""

from __future__ import annotations

import math
import struct
import sys

import numpy as np

__import__("sys").path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from acvd.winmem import Process

FOV_SIG = struct.pack(">f", 45.0)
CH = 32 << 20


def scan_region(proc, base, size):
    hits = []
    pos = base
    while pos < base + size:
        w = min(CH, base + size - pos)
        buf = proc.read(pos, w)
        if buf:
            start = 0
            while True:
                i = buf.find(FOV_SIG, start)
                if i < 0:
                    break
                start = i + 1
                qoff = i - 0x20          # FOV 在 +0x30，四元数在 +0x10
                if qoff < 16 or qoff + 16 > len(buf):
                    continue
                try:
                    q = struct.unpack(">4f", buf[qoff:qoff + 16])
                    p3 = struct.unpack(">3f", buf[qoff - 16:qoff - 4])
                    pw = struct.unpack(">f", buf[qoff - 4:qoff])[0]
                    sc = struct.unpack(">4f", buf[qoff + 16:qoff + 32])
                except struct.error:
                    continue
                x, y, z, ww = q
                n = x * x + y * y + z * z + ww * ww
                if abs(n - 1.0) > 0.002:
                    continue
                if abs(pw - 1.0) > 1e-4:
                    continue
                if not (1.0 < max(abs(p3[0]), abs(p3[2])) < 20000.0):
                    continue
                if not all(0.5 < abs(v) < 2.0 for v in sc[:3]):
                    continue
                yaw = math.degrees(math.atan2(2 * (ww * y + x * z), 1 - 2 * (y * y + z * z)))
                pit = math.degrees(math.asin(max(-1.0, min(1.0, 2 * (ww * x - y * z)))))
                hits.append((pos + qoff, p3, yaw, pit))
        pos += w
    return hits


def main():
    proc = Process.attach("xenia")
    regs = [r for r in proc.regions() if r.readable and r.size >= 4096]
    print(f"扫描 {len(regs)} 个区域 ...")
    all_hits = []
    for r in regs:
        all_hits.extend(scan_region(proc, r.base, r.size))

    print(f"严格符合条件的相机结构: {len(all_hits)}\n")
    print(f"{'四元数地址':>14} {'位置':>30} {'yaw':>9} {'pitch':>8}")
    print("-" * 68)
    for addr, p3, yaw, pit in sorted(all_hits):
        print(f"0x{addr:012X} ({p3[0]:9.2f},{p3[1]:8.2f},{p3[2]:9.2f}) "
              f"{yaw:+8.2f}° {pit:+7.2f}°")

    proc.close()


if __name__ == "__main__":
    main()

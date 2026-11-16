"""tools/find_camera_all.py — FOV 锚点全进程扫描（不依赖固定区域）

重启后 Xenia 的内存布局完全变化，固定区域失效。
本脚本扫描所有可读区域，用「大端 45.0 + 前 0x20 处单位四元数 + 前 0x30 处合理坐标」定位相机。
"""

from __future__ import annotations

import struct
import sys
import time

__import__("sys").path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from acvd.winmem import Process

FOV_SIG = struct.pack(">f", 45.0)
CHUNK = 32 * 1024 * 1024


def quat_yp(q):
    x, y, z, w = q
    n = x * x + y * y + z * z + w * w
    if abs(n - 1.0) > 0.002:
        return None
    import math
    yaw = math.degrees(math.atan2(2 * (w * y + x * z), 1 - 2 * (y * y + z * z)))
    pit = math.degrees(math.asin(max(-1.0, min(1.0, 2 * (w * x - y * z)))))
    return yaw, pit


def scan_region(proc, base, size, out, budget_hits=200):
    pos = base
    while pos < base + size:
        w = min(CHUNK, base + size - pos)
        buf = proc.read(pos, w)
        if buf:
            start = 0
            while True:
                i = buf.find(FOV_SIG, start)
                if i < 0:
                    break
                start = i + 1
                qoff = i - 0x20
                if qoff < 16 or qoff + 16 > len(buf):
                    continue
                try:
                    q = struct.unpack(">4f", buf[qoff:qoff + 16])
                    px, py, pz = struct.unpack(">3f", buf[qoff - 16:qoff - 4])
                except struct.error:
                    continue
                yp = quat_yp(q)
                if yp is None:
                    continue
                if not (1.0 < abs(px) < 20000.0 and 1.0 < abs(pz) < 20000.0):
                    continue
                if not (-35.0 < yp[1] < 75.0):
                    continue
                out.append((pos + qoff, (px, py, pz), yp, pos))
                if len(out) >= budget_hits:
                    return True
        pos += w
    return False


def main():
    proc = Process.attach("xenia")
    regs = [r for r in proc.regions() if r.readable and r.size >= 1024 * 1024]
    print(f"扫描 {len(regs)} 个区域 / {sum(r.size for r in regs)/2**20:.0f} MB")

    out = []
    t0 = time.perf_counter()
    for r in regs:
        if scan_region(proc, r.base, r.size, out):
            print("  达到候选上限，提前结束")
            break
    print(f"耗时 {time.perf_counter()-t0:.1f}s，命中 {len(out)}\n")

    out.sort(key=lambda x: x[0])
    print(f"{'四元数地址':>14} {'所在区域':>14} {'位置':>28} {'yaw':>9} {'pitch':>8}")
    print("-" * 82)
    for qaddr, p3, yp, region_base in out[:40]:
        print(f"0x{qaddr:012X} 0x{region_base:10X} "
              f"({p3[0]:9.2f},{p3[1]:8.2f},{p3[2]:9.2f}) {yp[0]:+8.2f}° {yp[1]:+7.2f}°")

    # 统计所在区域
    from collections import Counter
    c = Counter(hex(r) for _, _, _, r in out)
    print(f"\n候选分布区域: {dict(c)}")

    proc.close()


if __name__ == "__main__":
    main()

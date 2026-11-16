"""
tools/find_spring_arm.py — 用「固定臂长」定位火控圈/相机对

依据（实测验证）：
    火控圈与相机之间的距离（弹簧臂长）始终稳定：
        条件 A 静止    22.092
        条件 B 推杆    22.140
        条件 C 移动    21.762
    而两者各自的 position 在 B/C 条件下都会大幅变化。

因此：在相机附近的变换结构中，距离恰为该臂长的那一对，
就是一个是火控圈、一个是相机。

用法：
    python tools/find_spring_arm.py [--arm 22.09] [--tol 0.6]
"""

from __future__ import annotations

import argparse
import math
import struct
import sys

import numpy as np

__import__("sys").path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from acvd.winmem import Process

REGION = (0x140100000, 0x92C0000)
CH = 32 << 20


def scan(proc):
    """所有世界坐标量级的变换结构 {(pos, quat_addr)}"""
    out = []
    base, size = REGION
    pos = base
    while pos < base + size:
        w = min(CH, base + size - pos)
        buf = proc.read(pos, w)
        if buf and len(buf) >= 0x40:
            n = (len(buf) // 4) * 4
            with np.errstate(invalid="ignore", over="ignore"):
                a = np.frombuffer(buf[:n], dtype=">f4").astype(np.float64)
            if len(a) >= 8:
                f = a.reshape(-1, 4)
                posw, quat = f[:-1], f[1:]
                m = min(len(posw), len(quat))
                posw, quat = posw[:m], quat[:m]
                norms = (quat * quat).sum(axis=1)
                px, py, pz = posw[:, 0], posw[:, 1], posw[:, 2]
                ok = ((np.abs(posw[:, 3] - 1.0) < 1e-4)
                      & ((np.abs(px) >= 50.0) | (np.abs(pz) >= 50.0))
                      & (np.abs(px) < 20000.0) & (np.abs(py) < 20000.0)
                      & (np.abs(pz) < 20000.0)
                      & (np.abs(norms - 1.0) < 0.002))
                for i in np.flatnonzero(ok):
                    q = quat[i]
                    x, y, z, ww = q
                    yaw = math.degrees(math.atan2(2 * (ww * y + x * z), 1 - 2 * (y * y + z * z)))
                    pit = math.degrees(math.asin(max(-1.0, min(1.0, 2 * (ww * x - y * z)))))
                    out.append((pos + int(i + 1) * 16,
                                (float(px[i]), float(py[i]), float(pz[i])),
                                yaw, pit,
                                abs(x) < 1e-3 and abs(z) < 1e-3))
        pos += w
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", type=float, default=0.0,
                    help="目标臂长；0 表示先列出所有近距离结构对")
    ap.add_argument("--tol", type=float, default=0.6)
    a = ap.parse_args()

    proc = Process.attach("xenia_canary")
    structs = scan(proc)
    print(f"变换结构 {len(structs)} 个")

    if a.arm <= 0:
        # 自动发现：找最近的一对（通常就是相机+火控圈）
        print("\n自动查找近距离结构对（距离 < 60）:")
        best = []
        for i in range(len(structs)):
            for j in range(i + 1, len(structs)):
                d = math.dist(structs[i][1], structs[j][1])
                if d < 60:
                    best.append((d, i, j))
        best.sort()
        for d, i, j in best[:20]:
            ai, aj = structs[i], structs[j]
            print(f"  {d:7.3f}  0x{ai[0]:012X}(yaw={ai[2]:+7.2f},pit={ai[3]:+6.2f}) "
                  f"<-> 0x{aj[0]:012X}(yaw={aj[2]:+7.2f},pit={aj[3]:+6.2f})")
        print("\n用 --arm <距离> 精确筛选")
        proc.close()
        return

    print(f"\n筛选距离 ≈ {a.arm} ± {a.tol} 的结构对:")
    found = 0
    for i in range(len(structs)):
        for j in range(i + 1, len(structs)):
            d = math.dist(structs[i][1], structs[j][1])
            if abs(d - a.arm) <= a.tol:
                found += 1
                ai, aj = structs[i], structs[j]
                print(f"  臂长 {d:7.3f}")
                print(f"    A 0x{ai[0]:012X} pos=({ai[1][0]:.2f},{ai[1][1]:.2f},{ai[1][2]:.2f}) "
                      f"yaw={ai[2]:+7.2f} pitch={ai[3]:+6.2f} pure={ai[4]}")
                print(f"    B 0x{aj[0]:012X} pos=({aj[1][0]:.2f},{aj[1][1]:.2f},{aj[1][2]:.2f}) "
                      f"yaw={aj[2]:+7.2f} pitch={aj[3]:+6.2f} pure={aj[4]}")
    print(f"\n共 {found} 对")

    proc.close()


if __name__ == "__main__":
    main()

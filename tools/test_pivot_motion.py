"""
tools/test_pivot_motion.py — 验证枢轴是否随机甲移动

问题（玩家提出）：
    之前测到「位置不变」，但那是在机体不动的情况下测的。
    无法区分：
        A. 枢轴是弹簧臂锚点，固定在机甲上 → 机甲移动时它跟着走
        B. 枢轴是世界绝对点 → 机甲移动时它不动

做法：
    连续采样火控圈/机体的 position，玩家在采样期间【前后左右移动机甲】，
    看谁跟着动、动多少。
"""

from __future__ import annotations

import math
import struct
import sys
import time

import numpy as np

__import__("sys").path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from acvd.winmem import Process

AIM = 0x1411E38C0
CAM = 0x1405F30F0
# 之前找到的机体候选（可能已变，脚本会重新读）
BODY_CANDIDATES = [
    0x1411E38C0,
    0x14217B010,
    0x14217BE30,
    0x1405F30F0,
]


def read_pos(proc, quat_addr):
    raw = proc.read(quat_addr - 0x10, 0x10)
    if not raw or len(raw) < 0x10:
        return None
    p3 = np.array(struct.unpack(">3f", raw[0:12]))
    w = struct.unpack(">f", raw[12:16])[0]
    if abs(w - 1.0) > 1e-3:
        return None
    return p3


def read_quat(proc, quat_addr):
    raw = proc.read(quat_addr, 0x10)
    if not raw or len(raw) < 16:
        return None
    q = np.array(struct.unpack(">4f", raw))
    n = float((q * q).sum())
    if abs(n - 1.0) > 0.002:
        return None
    x, y, z, w = q
    yaw = math.degrees(math.atan2(2 * (w * y + x * z), 1 - 2 * (y * y + z * z)))
    pit = math.degrees(math.asin(max(-1.0, min(1.0, 2 * (w * x - y * z)))))
    return yaw, pit


def main():
    proc = Process.attach("xenia_canary")

    addrs = {}
    for a in BODY_CANDIDATES:
        p = read_pos(proc, a)
        q = read_quat(proc, a)
        if p is not None and q is not None:
            addrs[a] = (p, q)
    print(f"可读结构 {len(addrs)} 个:")
    for a, (p, q) in addrs.items():
        print(f"  0x{a:012X} pos=({p[0]:.2f},{p[1]:.2f},{p[2]:.2f}) "
              f"yaw={q[0]:+.2f} pitch={q[1]:+.2f}")

    if not addrs:
        print("✗ 没有可读结构")
        return 1

    print(f"\n{'='*64}")
    print("请【前后左右移动机甲】（不是转视角，是走过去）")
    print("持续 10 秒")
    print(f"{'='*64}")
    for s in range(3, 0, -1):
        print(f"  {s}...", end="\r", flush=True)
        time.sleep(1)
    print("  采样中...          ", flush=True)

    tracks = {a: [] for a in addrs}
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 10:
        for a in addrs:
            p = read_pos(proc, a)
            if p is not None:
                tracks[a].append(p)
        time.sleep(0.05)

    print(f"  采 {len(next(iter(tracks.values())))} 帧\n")

    print("各结构的位置变化:")
    print(f"{'地址':>14} {'x 跨度':>10} {'y 跨度':>10} {'z 跨度':>10} {'总位移':>10}  判定")
    print("-" * 74)
    for a, pts in tracks.items():
        if len(pts) < 5:
            continue
        P = np.array(pts)
        span = P.max(axis=0) - P.min(axis=0)
        disp = float(np.linalg.norm(P[-1] - P[0]))
        if disp < 0.5:
            tag = "  没动（世界点？或未移动）"
        else:
            tag = f"  跟着移动了（位移 {disp:.1f}）"
        print(f"0x{a:012X} {span[0]:9.2f} {span[1]:9.2f} {span[2]:9.2f} {disp:9.2f}{tag}")

    print()
    print("判读：")
    print("  火控圈枢轴随机甲移动 → 它是弹簧臂锚点（不随机体旋转移动，但随机体位移）")
    print("  火控圈枢轴完全不动   → 它可能是世界固定点")
    print("  相机位置继续变化     → 相机有自己的穿模/弹簧逻辑")

    proc.close()


if __name__ == "__main__":
    main()

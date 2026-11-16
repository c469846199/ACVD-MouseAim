"""
tools/test_pivot_vs_camera.py — 区分「机甲平移」与「穿模拉近」两种位移

核心问题：
    火控圈和相机都随机甲平移而移动（已证实），
    那它们的内存位置差异在哪里？

假设：
    火控圈 position = 机甲上的固定点（随平移+旋转？）
    相机   position = 机甲上的点 + 弹簧臂 + 穿模保护（额外变化）

做法：三种条件下分别记录两者 position：
    条件 A：机甲静止 + 不转视角      （基线）
    条件 B：机甲静止 + 大幅上下推杆  （只测穿模效应）
    条件 C：机甲移动 + 不转视角      （只测平移效应）
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


def read_pos(proc, quat_addr):
    raw = proc.read(quat_addr - 0x10, 0x10)
    if not raw or len(raw) < 0x10:
        return None
    p3 = np.array(struct.unpack(">3f", raw[0:12]))
    w = struct.unpack(">f", raw[12:16])[0]
    if abs(w - 1.0) > 1e-3:
        return None
    return p3


def sample(proc, seconds, label):
    pts_a, pts_c = [], []
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < seconds:
        a = read_pos(proc, AIM)
        c = read_pos(proc, CAM)
        if a is not None:
            pts_a.append(a)
        if c is not None:
            pts_c.append(c)
        time.sleep(0.04)
    if not pts_a or not pts_c:
        print(f"  {label}: 采样失败")
        return
    A, C = np.array(pts_a), np.array(pts_c)
    span_a = float(np.linalg.norm(A.max(axis=0) - A.min(axis=0)))
    span_c = float(np.linalg.norm(C.max(axis=0) - C.min(axis=0)))
    # 两者相对位移（消除共同平移）
    n = min(len(A), len(C))
    rel = A[:n] - C[:n]
    span_rel = float(np.linalg.norm(rel.max(axis=0) - rel.min(axis=0)))
    dist = float(np.linalg.norm(A[:n] - C[:n], axis=1).mean())
    print(f"  {label:24} AIM位移={span_a:7.3f}  CAM位移={span_c:7.3f}  "
          f"相对位移={span_rel:7.3f}  臂长={dist:7.3f}")


def main():
    proc = Process.attach("xenia_canary")
    print("=== 三种条件下对比 AIM 与 CAM 的位置行为 ===\n")

    print("条件 A：机甲静止、不转动视角（基线 3 秒）")
    sample(proc, 3.0, "A 静止")

    print("\n条件 B：机甲静止、大幅上下推杆（8 秒）")
    print("       >>> 请保持机甲不动，只上下推右摇杆")
    for s in range(3, 0, -1):
        print(f"       {s}...", end="\r", flush=True)
        time.sleep(1)
    print("       采样中...      ")
    sample(proc, 8.0, "B 只推杆")

    print("\n条件 C：机甲移动、不转视角（8 秒）")
    print("       >>> 请只走动，不要碰右摇杆")
    for s in range(3, 0, -1):
        print(f"       {s}...", end="\r", flush=True)
        time.sleep(1)
    print("       采样中...      ")
    sample(proc, 8.0, "C 只移动")

    proc.close()


if __name__ == "__main__":
    main()

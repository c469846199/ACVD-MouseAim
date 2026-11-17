"""
tools/check_relative_roll.py — 验证火控圈相对相机是否是「平整 2D」

玩家推断：
    锁定圈在屏幕上必须保持平整（否则会拍扁、难以瞄准），
    所以它相对【相机】的 roll 应恒为 0，尽管它在世界空间是 3D 旋转。

验证方法：
    q_rel = q_cam^-1 * q_aim        （aim 相对 cam 的姿态）
    若 rose 恒为 0 => 火控圈的屏幕朝向由相机决定，是伪 2D
    若 roll 随 pitch 变化 => 世界空间固定朝向（会拍扁）

同时对比：
    q_rel_body = q_body^-1 * q_aim  （aim 相对 body）
"""

from __future__ import annotations

import math
import struct
import sys
import time

import numpy as np

__import__("sys").path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from acvd.pipe import PipeClient
from acvd.winmem import Process

AIM = 0x141109DC0
BODY = 0x1418BA150
CAM = 0x1405F30F0


def read_quat(proc, addr):
    """返回归一化的 (x, y, z, w) numpy float64。"""
    raw = proc.read(addr, 16)
    if not raw or len(raw) < 16:
        return None
    q = np.frombuffer(raw, dtype=">f4").astype(np.float64)
    n = math.sqrt(float((q * q).sum()))
    if n < 1e-6 or abs(n - 1.0) > 0.05:
        return None
    return q / n


def qmul(a, b):
    """四元数乘法 a*b（Hamilton，xyzw）。"""
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return np.array([
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
        aw * bw - ax * bx - ay * by - az * bz,
    ])


def qinv(q):
    return np.array([-q[0], -q[1], -q[2], q[3]])


def euler_zxy(q):
    """返回 (yaw, pitch, roll)，单位度。ZXY 顺序，适合游戏常用朝向。"""
    x, y, z, w = q
    sinp = 2 * (w * x - y * z)
    pitch = math.degrees(math.asin(max(-1.0, min(1.0, sinp))))
    yaw = math.degrees(math.atan2(2 * (w * y + x * z), 1 - 2 * (y * y + z * z)))
    roll = math.degrees(math.atan2(2 * (w * z + x * y), 1 - 2 * (x * x + y * y)))
    return yaw, pitch, roll


def main() -> int:
    proc = Process.attach("xenia")
    cli = PipeClient("acvd_pad")

    print("扫描推杆全过程，记录 aim/cam/body 的相对 roll\n")
    print(f"{'t':>5} {'camPitch':>9} {'aimPitch':>9} "
          f"{'roll(aim/cam)':>14} {'roll(aim/body)':>15} {'aimYaw-camYaw':>14}")
    print("-" * 74)

    rows = []
    cli.send("r 0 0.9")          # 先推到顶
    time.sleep(1.2)
    t0 = time.perf_counter()
    for i in range(22):
        qa, qc, qb = read_quat(proc, AIM), read_quat(proc, CAM), read_quat(proc, BODY)
        if qa is None or qc is None or qb is None:
            continue
        rel_cam = qmul(qinv(qc), qa)
        rel_body = qmul(qinv(qb), qa)
        ay, ap, ar = euler_zxy(qa)
        cy, cp, cr = euler_zxy(qc)
        _, _, r_cam = euler_zxy(rel_cam)
        _, _, r_body = euler_zxy(rel_body)
        dyaw = (ay - cy + 180) % 360 - 180
        rows.append((time.perf_counter() - t0, cp, ap, r_cam, r_body, dyaw))
        print(f"{rows[-1][0]:5.2f} {cp:8.2f}° {ap:8.2f}° "
              f"{r_cam:+13.3f}° {r_body:+14.3f}° {dyaw:+13.2f}°")
        if i == 10:
            cli.send("r 0 -0.9")   # 中途反向
        time.sleep(0.12)
    cli.send("r 0 0")
    time.sleep(0.2)

    if rows:
        rc = [r[3] for r in rows]
        rb = [r[4] for r in rows]
        print("\n统计：")
        print(f"  roll(aim/cam)  范围 {min(rc):+.4f} .. {max(rc):+.4f}  摆动 {max(rc)-min(rc):.4f}°")
        print(f"  roll(aim/body) 范围 {min(rb):+.4f} .. {max(rb):+.4f}  摆动 {max(rb)-min(rb):.4f}°")
        print()
        if max(rc) - min(rc) < 1.0:
            print("  ✓ aim 相对 cam 的 roll 近似恒定 → 锁定圈是「相对相机的平面」")
        else:
            print("  ✗ aim 相对 cam 的 roll 随 pitch 变化 → 世界空间固定朝向，会拍扁")

    proc.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
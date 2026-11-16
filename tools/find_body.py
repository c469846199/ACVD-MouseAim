"""
tools/find_body.py — 找机体朝向（纯偏航四元数）

依据（从玩家描述 + Workbuddy 的 locate.py 注释）：
    机体朝向是「纯偏航四元数」：x=0, z=0, |q|=1，只有 Y 轴旋转。
    火控圈不是纯偏航（带 pitch），相机也不是（带 pitch）。
    所以「纯偏航 + 模长 1」是机体的强特征。

    再加两条约束：
        * 机体的 yaw 应在火控圈 yaw 附近（锁定圈永远在机体前方）
        * 机体位置应在相机正前方（而不是场景道具）

用法:
    python tools/find_body.py
"""

from __future__ import annotations

import json
import math
import os
import struct
import sys

import numpy as np

__import__("sys").path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from acvd.winmem import Process

STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "state")
STATE_FILE = os.path.join(STATE_DIR, "aim_addr.json")
CHUNK = 16 * 1024 * 1024
CAM_REGION = (0x140100000, 0x92C0000)


def yaw_of(q):
    x, y, z, w = q[..., 0], q[..., 1], q[..., 2], q[..., 3]
    return np.degrees(np.arctan2(2 * (w * y + x * z), 1 - 2 * (y * y + z * z)))


def main() -> int:
    proc = Process.attach("xenia_canary")

    # 读取已记录的火控圈 yaw
    aim_yaw = None
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
        for a in st.get("aim", []):
            raw = proc.read(a, 16)
            if raw:
                x, y, z, w = struct.unpack(">4f", raw)
                if abs(x * x + y * y + z * z + w * w - 1.0) < 0.02:
                    aim_yaw = math.degrees(math.atan2(2 * (w * y + x * z),
                                                      1 - 2 * (y * y + z * z)))
                    break
    print(f"火控圈当前 yaw = {aim_yaw:+.2f}°" if aim_yaw is not None else "火控圈未记录")

    base, size = CAM_REGION
    hits = []
    pos = base
    while pos < base + size:
        w = min(CHUNK, base + size - pos)
        buf = proc.read(pos, w)
        if buf and len(buf) >= 16:
            n = (len(buf) // 4) * 4
            with np.errstate(invalid="ignore", over="ignore"):
                q = np.frombuffer(buf[:n], dtype=">f4").astype(np.float64).reshape(-1, 4)
            if len(q):
                norms = (q * q).sum(axis=1)
                # 纯偏航：x≈0, z≈0, |q|≈1
                pure = ((np.abs(norms - 1.0) < 0.002)
                        & (np.abs(q[:, 0]) < 1e-4)
                        & (np.abs(q[:, 2]) < 1e-4))
                idx = np.flatnonzero(pure)
                if len(idx):
                    ys = yaw_of(q[idx])
                    for k, i in enumerate(idx):
                        hits.append((pos + int(i) * 16, float(ys[k])))
        pos += w

    print(f"\n纯偏航四元数: {len(hits):,} 个")

    if aim_yaw is not None:
        near = [(a, y) for a, y in hits
                if abs((y - aim_yaw + 180) % 360 - 180) < 65.0]
        print(f"其中 yaw 在火控圈 ±65° 内: {len(near)} 个\n")
        near.sort(key=lambda t: abs((t[1] - aim_yaw + 180) % 360 - 180))
        print(f"{'地址':>12} {'yaw':>9} {'与火控圈差':>10}")
        print("-" * 38)
        for a, y in near[:30]:
            d = (y - aim_yaw + 180) % 360 - 180
            print(f"0x{a:010X} {y:+8.2f}° {d:+9.2f}°")
    else:
        print("没有火控圈参照，列出前 30 个纯偏航：")
        for a, y in hits[:30]:
            print(f"0x{a:010X} yaw={y:+8.2f}°")

    proc.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
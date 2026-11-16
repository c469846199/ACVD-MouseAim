"""
tools/find_camera_fov.py — 复用 Workbuddy 的 FOV 锚点法定位相机 / 瞄准框

原理（来自 Workbuddy 的 locate.py::find_camera_by_fov）：
    相机结构布局固定：
        +0x00  position  3 × f32   (大端)
        +0x0C  (padding)
        +0x10  quaternion 4 × f32  (大端, 模长≈1)
        +0x20  FOV        1 × f32  = 45.0 (大端 => 42 34 00 00)
    所以先在内存里找「大端 45.0」这个 4 字节常量，再校验：
        * 前 0x20 处是一个有效单位四元数（|q|≈1）
        * 再前面 16 字节是合理的世界坐标（1 < |v| < 20000）
        * 由四元数解出的 pitch 落在 -35°..75°
    这个锚点跨关卡稳定，因为 FOV 不随场景重建而变。

用法:
    python tools/find_camera_fov.py                 # 全进程找
    python tools/find_camera_fov.py --region 0x100010000 --size 0x3F00000
    python tools/find_camera_fov.py --json out.json  # 保存结果
"""

from __future__ import annotations

import argparse
import json
import math
import struct
import sys
import time

__import__("sys").path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from acvd.winmem import Process

FOV_SIG = struct.pack(">f", 45.0)       # 42 34 00 00
YAW_MIN, YAW_MAX = -35.0, 75.0          # pitch 合理范围


def yaw_of(x, y, z, w):
    return math.degrees(math.atan2(2.0 * (w * y + x * z), 1.0 - 2.0 * (y * y + z * z)))


def pitch_of(x, y, z, w):
    v = 2.0 * (w * x - y * z)
    v = max(-1.0, min(1.0, v))
    return math.degrees(math.asin(v))


def check_candidate(raw: bytes, i: int, base: int) -> dict | None:
    """i = FOV 签名在 raw 中的位置。返回描述 dict 或 None。"""
    qoff = i - 0x20
    if qoff < 16 or qoff + 16 > len(raw):
        return None
    try:
        x, y, z, w = struct.unpack(">4f", raw[qoff:qoff + 16])
        px, py, pz = struct.unpack(">3f", raw[qoff - 16:qoff - 4])
    except struct.error:
        return None
    # 四元数必须单位化
    if abs((x * x + y * y + z * z + w * w) - 1.0) > 0.01:
        return None
    # 位置必须合理
    if not all(1.0 < abs(v) < 20000.0 for v in (px, py, pz)):
        return None
    pitch = pitch_of(x, y, z, w)
    if not (YAW_MIN < pitch < YAW_MAX):
        return None
    return {
        "fov_addr": base + i,
        "quat_addr": base + qoff,
        "pos_addr": base + qoff - 16,
        "pos": (round(px, 3), round(py, 3), round(pz, 3)),
        "quat": tuple(round(v, 6) for v in (x, y, z, w)),
        "yaw": round(yaw_of(x, y, z, w), 3),
        "pitch": round(pitch, 3),
    }


def scan_region(proc, base: int, size: int, limit: int = 200) -> list[dict]:
    out = []
    chunk = 16 * 1024 * 1024
    pos = base
    while pos < base + size:
        w = min(chunk, base + size - pos)
        buf = proc.read(pos, w)
        if buf:
            start = 0
            while True:
                i = buf.find(FOV_SIG, start)
                if i < 0:
                    break
                c = check_candidate(buf, i, pos)
                if c:
                    out.append(c)
                    if len(out) >= limit:
                        return out
                start = i + 1
        pos += w
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", type=lambda s: int(s, 0), default=None)
    ap.add_argument("--size", type=lambda s: int(s, 0), default=None)
    ap.add_argument("--json", default=None)
    ap.add_argument("--min-mb", type=float, default=1.0)
    a = ap.parse_args()

    proc = Process.attach("xenia")

    if a.region is not None:
        targets = [(a.region, a.size or 0x10000000)]
    else:
        targets = [(r.base, r.size) for r in proc.regions()
                   if r.readable and r.size >= a.min_mb * 2**20]
    total = sum(s for _, s in targets)
    print(f"扫描 {len(targets)} 个区域 / {total/2**20:.0f} MB，查找大端 FOV=45.0")

    results = []
    t0 = time.perf_counter()
    for base, size in targets:
        results.extend(scan_region(proc, base, size))
    print(f"耗时 {time.perf_counter()-t0:.2f}s，通过校验的候选: {len(results)}\n")

    results.sort(key=lambda c: c["fov_addr"])
    for c in results[:40]:
        print(f"FOV 0x{c['fov_addr']:012X} | quat 0x{c['quat_addr']:012X} "
              f"| pos {c['pos']} | yaw {c['yaw']:+8.2f} pitch {c['pitch']:+7.2f}")

    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=1, ensure_ascii=False)
        print(f"\n已保存 {len(results)} 个候选到 {a.json}")

    proc.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
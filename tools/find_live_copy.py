"""
tools/find_live_copy.py — 在火控圈的多个副本中，找出真正「实时更新」的那个

背景：
    auto_locate_aim 找到 52/39 个副本，但它们更新频率不同。
    闭环读到旧副本会导致「推摇杆没反应 → 积分饱和 → 抖动」。

判据：
    推杆后，哪个副本的 yaw 变化最及时、最大。
    实时副本应该每帧都在变；滞后副本几天都不变或跳变。

用法：
    python tools/find_live_copy.py
    然后推杆（脚本会自动提示）
"""

from __future__ import annotations

import math
import struct
import sys
import threading
import time

import numpy as np

__import__("sys").path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from acvd.aim import locate_aim
from acvd.pipe import PipeClient
from acvd.winmem import Process


def read_quat(proc, addr):
    raw = proc.read(addr, 16)
    if not raw or len(raw) < 16:
        return None
    x, y, z, w = struct.unpack(">4f", raw)
    n = x * x + y * y + z * z + w * w
    if abs(n - 1.0) > 0.02:
        return None
    return math.degrees(math.atan2(2 * (w * y + x * z), 1 - 2 * (y * y + z * z)))


def main():
    proc = Process.attach("xenia_canary")
    print("定位 ...")
    info = locate_aim(proc)
    if not info:
        print("✗ 定位失败")
        return 1
    copies = info["copies"]
    print(f"副本 {len(copies)} 个")

    try:
        cli = PipeClient("acvd_pad")
    except OSError:
        print("✗ 守护进程没跑")
        return 1

    print(f"\n{'='*58}")
    print("脚本将自动推右摇杆 2 秒，然后停止")
    print("期间记录每个副本的 yaw 变化")
    print(f"{'='*58}")
    time.sleep(2)

    # 基线
    base = {a: read_quat(proc, a) for a in copies}

    # 开始推杆
    stop = threading.Event()

    def pusher():
        cli.send("r 0.9 0")
        time.sleep(2.0)
        cli.send("r 0 0")
        stop.set()

    threading.Thread(target=pusher, daemon=True).start()

    # 每 20ms 采样，记录变化次数与累计变化
    changes = {a: 0 for a in copies}
    total = {a: 0.0 for a in copies}
    last = dict(base)
    t0 = time.perf_counter()
    while not stop.is_set():
        for a in copies:
            v = read_quat(proc, a)
            if v is None or last.get(a) is None:
                continue
            d = (v - last[a] + 180) % 360 - 180
            if abs(d) > 0.001:
                changes[a] += 1
                total[a] += abs(d)
            last[a] = v
        time.sleep(0.02)

    time.sleep(0.4)
    print(f"\n{'地址':>16} {'变化次数':>9} {'累计变化':>10} {'最终yaw':>10}")
    print("-" * 52)
    rows = sorted(copies, key=lambda a: -changes[a])
    for a in rows:
        v = last.get(a)
        print(f"0x{a:012X} {changes[a]:9d} {total[a]:9.2f}° "
              f"{v:+9.2f}°" if v is not None else "")

    live = rows[0]
    print(f"\n★ 最实时的副本: 0x{live:012X}（变化 {changes[live]} 次）")

    proc.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
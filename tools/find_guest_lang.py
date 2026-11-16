"""tools/find_guest_lang.py — 在 Guest 内存找语言变量

目标：
    游戏把 XGetLanguage 的返回值（英文=1或4）存到 guest 某处，
    之后用它选择 UI 语言。

策略：
    1. 重点扫 0x82000000 / 0x83000000 附近（Xbox 360 guest 数据区）
    2. 找 4 字节值 == 1 或 4，且前后是小整数、看起来像配置结构的
    3. 输出候选及其上下文，供人工判断
"""

from __future__ import annotations

import sys

__import__("sys").path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))

from acvd.focus import _find_pid_by_prefix
from acvd.winmem import Process

# Xbox 360 guest 常见数据区
GUEST_RANGES = [
    (0x82000000, 0x100000),   # 1 MB @ 0x82000000
    (0x83000000, 0x100000),   # 1 MB @ 0x83000000
    (0x83100000, 0x100000),
    (0x82C00000, 0x100000),
    (0x8A000000, 0x100000),
]


def main():
    pid = _find_pid_by_prefix("xenia")
    if pid is None:
        raise SystemExit("找不到 Xenia 进程")
    p = Process.attach(pid)

    # guest 地址 -> host 地址：Xenia 有映射表，我们直接读 guest 地址
    for gbase, gsize in GUEST_RANGES:
        data = p.read(gbase, gsize)
        if not data or len(data) < 64:
            print(f"0x{gbase:08X}: 读不到（可能未映射）")
            continue
        print(f"0x{gbase:08X}: 读到 {len(data)/1024:.0f} KB")

        # 找值为 1 或 4 的 4 字节大端，且前后 8 字节都是小整数（<64）
        cands = []
        for i in range(0, len(data) - 48, 4):
            v = int.from_bytes(data[i:i + 4], "big")
            if v not in (1, 4):
                continue
            # 检查前后邻域是否是一串小整数（配置结构特征）
            neigh = []
            small_cnt = 0
            for j in range(-16, 20, 4):
                k = i + j
                if k < 0 or k + 4 > len(data):
                    continue
                nv = int.from_bytes(data[k:k + 4], "big")
                neigh.append(nv)
                if nv < 64:
                    small_cnt += 1
            if small_cnt >= len(neigh) - 1:
                cands.append((gbase + i, v, neigh))
        print(f"  小整数密集区候选 {len(cands)} 个")
        for addr, v, neigh in cands[:15]:
            print(f"    0x{addr:08X} = {v}  邻域 {neigh}")
        if cands:
            break
    p.close()


if __name__ == "__main__":
    raise SystemExit(main())

"""tools/hunt_host_lang.py — 在 Host 内存找语言值（04 00 00 00）

背景：
    Guest(PPC) 代码被 Xenia JIT 翻译成 Host(x64) 指令，
    语言值最终以 4 字节形式存在于 Host 内存某处。

策略：
    在 game heap (0x140100000) 里找值为 4 的位置，
    并检查邻域是否有 8/17（语言枚举）、或者是否被反复读取。
    输出候选地址 + 上下文，供人工判断。
"""

from __future__ import annotations

import sys

__import__("sys").path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))

from acvd.focus import _find_pid_by_prefix
from acvd.winmem import Process

HOST_HEAP = (0x140100000, 146800640)

# 额外扫几个大区域
REGIONS = [
    (0x140100000, 146800640),
    (0x1C0000000, 536870912),
    (0x1E0000000, 536870912),
]


def main():
    pid = _find_pid_by_prefix("xenia")
    if pid is None:
        raise SystemExit("找不到 Xenia 进程")
    p = Process.attach(pid)

    needle = (4).to_bytes(4, "little")      # 04 00 00 00
    needle_be = (4).to_bytes(4, "big")      # 00 00 00 04

    for base, size in REGIONS:
        try:
            data = p.read(base, size)
        except Exception as e:
            print(f"0x{base:012X}: 读取失败 {e}")
            continue
        if not data:
            print(f"0x{base:012X}: 空")
            continue
        print(f"\n=== 区域 0x{base:012X} ({len(data)/1024/1024:.0f} MB) ===")
        for pat, label in ((needle, "LE 04 00 00 00"), (needle_be, "BE 00 00 00 04")):
            cnt = data.count(pat)
            print(f"  {label}: {cnt} 处")

        # 找 LE 的 4，且前后 32 字节内含 8 或 17（语言枚举对）
        hits = []
        start = 0
        while len(hits) < 20:
            i = data.find(needle, start)
            if i < 0:
                break
            lo = max(0, i - 32)
            hi = min(len(data), i + 36)
            ctx = data[lo:hi]
            # 邻域里是否有 8 或 17（LE 4字节）
            has8 = (8).to_bytes(4, "little") in ctx
            has17 = (17).to_bytes(4, "little") in ctx
            if has8 or has17:
                hits.append((base + i, has8, has17, ctx))
            start = i + 4
        print(f"  邻域含 8 或 17 的候选: {len(hits)}")
        for addr, h8, h17, ctx in hits[:12]:
            ints = [int.from_bytes(ctx[j:j + 4], "little") for j in range(0, len(ctx) - 4, 4)]
            print(f"    0x{addr:012X}  has8={h8} has17={h17}  邻域LE32={ints[:12]}")

    p.close()


if __name__ == "__main__":
    raise SystemExit(main())

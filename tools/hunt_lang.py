"""tools/hunt_lang.py — 定位游戏的语言枚举变量

策略：
    1. 在 guest 内存里找「语言枚举表」特征：
       连续出现 1,2,3,4,5,6,7,8 这类小整数序列
    2. 找同时含 8 和 17 的位置（中文的两个 ID）
    3. 找 Language 字符串附近的数据

用法: python tools/hunt_lang.py
"""

from __future__ import annotations

import sys

__import__("sys").path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))

from acvd.focus import _find_pid_by_prefix
from acvd.winmem import Process

GUEST_REGIONS = [
    (0x140100000, 146800640),
    (0x1C0000000, 536870912),
    (0x1E0000000, 536870912),
]


def main():
    pid = _find_pid_by_prefix("xenia")
    if pid is None:
        raise SystemExit("找不到 Xenia 进程")
    p = Process.attach(pid)

    print("=== 1. 寻找含 8 和 17 相邻的位置（中文语言 ID 对）===")
    for base, size in GUEST_REGIONS:
        data = p.read(base, size)
        if not data:
            continue
        print(f"  区域 0x{base:012X} ({len(data)/1024/1024:.0f} MB)")
        # 4 字节大端
        found = []
        for i in range(0, len(data) - 64, 4):
            a = int.from_bytes(data[i:i + 4], "big")
            b = int.from_bytes(data[i + 4:i + 8], "big")
            # 8 后面紧跟 17，或 17 后面紧跟 8
            if (a == 8 and b == 17) or (a == 17 and b == 8):
                found.append(base + i)
                if len(found) >= 10:
                    break
        for addr in found:
            off = addr - base
            ctx = data[max(0, off - 16):off + 32]
            vals = [int.from_bytes(ctx[j:j + 4], "big") for j in range(0, len(ctx) - 4, 4)]
            print(f"    0x{addr:012X}: {vals}")

    p.close()


if __name__ == "__main__":
    raise SystemExit(main())

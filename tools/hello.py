"""tools/hello.py — 环境自检：确认能挂上 Xenia 并读到 guest 内存。"""

from __future__ import annotations

import sys

__import__("sys").path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))

from acvd.guest import GuestReader, dump_guest_map
from acvd.winmem import Process


def main() -> int:
    try:
        proc = Process.attach("xenia_canary")
    except Exception as e:
        print("✗ 附加失败:", e)
        print("  → 请确认 Xenia 已启动，且本脚本以管理员身份运行。")
        return 1
    print(f"✓ 已附加 xenia_canary (pid={proc.pid})")

    regs = proc.regions()
    print(f"✓ 内存区域 {len(regs)} 个，"
          f"已提交 {sum(r.size for r in regs if r.state == 0x1000) / 2**20:,.0f} MB")

    g = GuestReader.auto(proc)
    print(g)
    if g.mappings:
        m = g.mappings[0]
        peek = g.read(m.guest_base, 64)
        print(f"✓ guest 0x{m.guest_base:08X} 可读，首 64 字节: {peek.hex() if peek else 'None'}")
    else:
        print("! 未嗅探到 guest 映射，打印候选：")
        print(dump_guest_map(proc))

    proc.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
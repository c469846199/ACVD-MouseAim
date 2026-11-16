"""tools/find_profile.py — 在 Xenia 宿主内存定位 UserProfile / AccountInfo 结构

思路：
    账户名 "kuro" 存在 X_XAMACCOUNTINFO.gamertag 里（UTF-16），
    搜到它就能定位整个结构，再按偏移找 Language / Country 字段。

用法：
    python tools/find_profile.py kuro
"""

from __future__ import annotations

import sys

__import__("sys").path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))

from acvd.focus import _find_pid_by_prefix
from acvd.winmem import Process


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else "kuro"
    pid = _find_pid_by_prefix("xenia")
    if pid is None:
        raise SystemExit("找不到 Xenia 进程")
    p = Process.attach(pid)

    regions = [r for r in p.regions() if r.readable and r.size >= 4096]
    print(f"可读区域 {len(regions)} 个")

    # 账户名是 UTF-16LE
    needle_le = name.encode("utf-16-le")
    # 也可能是 UTF-16BE（Xbox 环境）
    needle_be = name.encode("utf-16-be")

    hits = []
    for r in regions:
        try:
            data = p.read(r.base, r.size)
        except Exception:
            continue
        if not data:
            continue
        for needle, kind in ((needle_le, "LE"), (needle_be, "BE")):
            start = 0
            while True:
                i = data.find(needle, start)
                if i < 0:
                    break
                hits.append((r.base + i, kind, r.base, data, i))
                start = i + 1
                if len(hits) > 60:
                    break
    p.close()

    print(f"找到 {len(hits)} 处匹配")
    for addr, kind, rbase, data, off in hits[:20]:
        # 打印前后 64 字节看结构
        lo = max(0, off - 32)
        hi = min(len(data), off + 64)
        ctx = data[lo:hi]
        txt = ''.join(chr(c) if 32 <= c < 127 else '.' for c in ctx)
        print(f"  0x{addr:012X} [{kind}]")
        print(f"    hex: {' '.join(f'{c:02X}' for c in ctx)}")
        print(f"    txt: {txt}")


if __name__ == "__main__":
    raise SystemExit(main())

"""tools/padctl.py — 向虚拟手柄守护进程发送指令。"""

from __future__ import annotations

import sys

__import__("sys").path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from acvd.pipe import PipeClient


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    try:
        client = PipeClient("acvd_pad")
        print(client.send(" ".join(argv)))
    except OSError as e:
        print(f"✗ {e}\n  守护进程没在跑？先运行: python tools/pad_daemon.py")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
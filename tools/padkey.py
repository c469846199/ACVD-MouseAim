"""
tools/padkey.py — 虚拟手柄按键工具（给玩家自己用，走常驻守护进程）

为什么不自己创建手柄：
    新建 VirtualPad() 会占新的 XInput 槽位（Xenia 只读槽 0），
    而且设备断连重连会让游戏自动暂停。
    所以必须发给常驻的 pad_daemon.py，由它转发。

先启动守护进程（只需一次）：
    python tools\\pad_daemon.py

用法:
    python tools/padkey.py a                  # 按一次 A
    python tools/padkey.py a a a              # 连按 3 次 A
    python tools/padkey.py down*5 a           # 下按 5 次，再按 A
    python tools/padkey.py start              # Start（暂停/继续）
    python tools/padkey.py --gap 0.8 a a      # 加长间隔
    python tools/padkey.py --stick 0.9 0 --secs 1.5   # 右摇杆推右 1.5 秒
    python tools/padkey.py status             # 查看守护进程状态

按键名:
    a b x y start back guide
    lb rb ls rs up down left right
"""

from __future__ import annotations

import argparse
import sys
import time

__import__("sys").path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from acvd.pipe import PipeClient

ALIASES = {
    "l1": "lb", "r1": "rb", "l2": "lt", "r2": "rt",
    "select": "back", "ok": "a", "confirm": "a", "cancel": "b",
}


def expand(tokens: list[str]) -> list[str]:
    out = []
    for t in tokens:
        if "*" in t:
            name, n = t.split("*", 1)
            out.extend([name] * int(n))
        else:
            out.append(t)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="虚拟手柄按键工具（走守护进程）")
    ap.add_argument("keys", nargs="*", help="按键序列，支持 name*n")
    ap.add_argument("--gap", type=float, default=0.50, help="两次按键之间秒数")
    ap.add_argument("--stick", nargs=2, type=float, metavar=("X", "Y"),
                    help="右摇杆方向 -1..1")
    ap.add_argument("--secs", type=float, default=1.0, help="摇杆保持秒数")
    ap.add_argument("--status", action="store_true", help="查看守护进程状态")
    a = ap.parse_args()

    try:
        cli = PipeClient("acvd_pad")
        cli.send("status")
    except OSError:
        print("✗ 守护进程没在跑。先运行： python tools\\pad_daemon.py")
        return 1

    if a.status or (not a.keys and not a.stick):
        print(cli.send("status"))
        return 0

    if a.stick:
        x, y = a.stick
        print(f"右摇杆 ({x:+.2f}, {y:+.2f}) 保持 {a.secs}s ...", flush=True)
        cli.send(f"rhold {x} {y} {a.secs}")
        print("回中", flush=True)

    keys = expand(a.keys)
    if keys:
        names = [ALIASES.get(k.lower(), k.lower()) for k in keys]
        print(f"发送 {len(keys)} 个按键，间隔 {a.gap}s: {' '.join(names)}", flush=True)
        print(cli.send(f"keys {a.gap} 0.10 {' '.join(names)}"), flush=True)
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
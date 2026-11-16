"""tools/gui.py — Xenia 菜单的命令行入口（实现在 acvd/xenia_ui.py）

    python -m tools.gui menu                  # 整棵菜单树
    python -m tools.gui menu --grep 槽位
    python -m tools.gui slot 1                # 只看槽位 1 的绑定
    python -m tools.gui assign "Xbox 360 Controller" 1
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from acvd.xenia_ui import assign_device, hwnd, slot_label, tree  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["menu", "slot", "assign", "hwnd"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--grep", default=None)
    a = ap.parse_args()

    if a.cmd == "hwnd":
        print(f"0x{hwnd():X}")
        return
    if a.cmd == "menu":
        for d, lab, cid, sub in tree():
            if a.grep and a.grep not in lab:
                continue
            print("  " * d + lab + (f"   [id {cid}]" if cid and cid > 0 else ""))
        return
    if a.cmd == "slot":
        n = int(a.args[0]) if a.args else 1
        print(f"槽位 {n} = {slot_label(n) or '（空）'}")
        return
    if a.cmd == "assign":
        dev = a.args[0] if a.args else "Xbox 360 Controller"
        n = int(a.args[1]) if len(a.args) > 1 else 1
        print("成功" if assign_device(dev, n) else "失败")


if __name__ == "__main__":
    main()

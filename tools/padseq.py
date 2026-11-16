"""tools/padseq.py — 按序列发送虚拟手柄按键，带充足间隔

用法:
    python tools/padseq.py down down down down down a
    python tools/padseq.py --gap 0.5 a a b
"""

from __future__ import annotations

import argparse
import sys
import time

__import__("sys").path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from acvd.pad import VirtualPad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("keys", nargs="+")
    ap.add_argument("--gap", type=float, default=0.45, help="两次按键之间的秒数")
    ap.add_argument("--hold", type=float, default=0.10, help="按下保持秒数")
    a = ap.parse_args()

    pad = VirtualPad()
    time.sleep(0.15)
    try:
        for i, k in enumerate(a.keys):
            k = k.lower()
            print(f"[{i+1}/{len(a.keys)}] {k}", flush=True)
            pad.press(k, a.hold)
            if i != len(a.keys) - 1:
                time.sleep(a.gap)
    finally:
        pad.close()
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
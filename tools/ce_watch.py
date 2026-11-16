"""tools/ce_watch.py — 仿 CE 的实时观察窗（控制台版）

用法:
    python -m tools.ce_watch 0x82A1B2C4 0x82A1B2C8 ...

每 1/20 秒打印一次，并标出「与上次相比发生变化」的列。
Ctrl+C 退出并打印变化统计。
"""

from __future__ import annotations

import argparse
import struct
import sys
import time

__import__("sys").path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from acvd.winmem import Process


def parse_addr(s: str) -> int:
    s = s.strip().replace("0x", "").replace(" ", "")
    return int(s, 16)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("addrs", nargs="+", help="宿主地址，十六进制")
    ap.add_argument("--dtype", default="f32", choices=["f32", "f64", "u32", "i32", "u16"])
    ap.add_argument("--hz", type=float, default=20.0)
    ap.add_argument("--seconds", type=float, default=0.0, help="0 = 无限")
    ap.add_argument("--proc", default="xenia_canary")
    a = ap.parse_args()

    fmts = {"f32": "<f", "f64": "<d", "u32": "<I", "i32": "<i", "u16": "<H"}
    sizes = {"f32": 4, "f64": 8, "u32": 4, "i32": 4, "u16": 2}
    fmt, size = fmts[a.dtype], sizes[a.dtype]
    addrs = [parse_addr(x) for x in a.addrs]

    proc = Process.attach(a.proc)
    print(" ".join(f"0x{x:08X}" for x in addrs))
    last = [None] * len(addrs)
    changes = [0] * len(addrs)
    t0 = time.perf_counter()
    try:
        while True:
            d = proc.read(addrs[0], size)  # 预热，避免首行乱序
            line = []
            for i, ad in enumerate(addrs):
                b = proc.read(ad, size)
                v = struct.unpack(fmt, b)[0] if b and len(b) == size else float("nan")
                if last[i] is not None and v != last[i]:
                    changes[i] += 1
                    line.append(f"{v:>12.5f}*")
                else:
                    line.append(f"{v:>12.5f} ")
                last[i] = v
            print("".join(line), end="\r", flush=True)
            if a.seconds and time.perf_counter() - t0 > a.seconds:
                break
            time.sleep(1.0 / a.hz)
    except KeyboardInterrupt:
        pass
    print()
    print("变化次数:", "  ".join(f"0x{ad:08X}={c}" for ad, c in zip(addrs, changes)))
    proc.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
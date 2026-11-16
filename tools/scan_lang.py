"""tools/scan_lang.py — 内存扫描：定位游戏存储的语言值

原理：
    新版 Xenia 的 XGetLanguage 返回固定值（英文=4 或 1），
    游戏会把它存到 guest 内存某处，用于后续 UI 语言判断。

用法：
    python tools/scan_lang.py snap  <文件>     # 拍快照
    python tools/scan_lang.py diff  <a> <b>    # 对比两份快照
    python tools/scan_lang.py find  <文件> <值> # 找出某值的所有位置
"""

from __future__ import annotations

import sys

__import__("sys").path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))

from acvd.focus import _find_pid_by_prefix
from acvd.winmem import Process

# guest 内存主要区域（Xenia 的 heap）
REGIONS = [
    (0x140100000, 146800640),   # 主 guest 堆 146.8 MB
    (0x1A0000000, 536870912),   # 512 MB
    (0x1C0000000, 536870912),
    (0x1E0000000, 536870912),
    (0x200000000, 536870912),
]


def attach():
    pid = _find_pid_by_prefix("xenia")
    if pid is None:
        raise SystemExit("找不到 Xenia 进程")
    return Process.attach(pid)


def cmd_snap(path: str):
    p = attach()
    import pickle
    data = {}
    for base, size in REGIONS:
        raw = p.read(base, size)
        if raw:
            data[base] = raw
            print(f"  0x{base:012X}  读到 {len(raw)/1024/1024:.1f} MB")
    p.close()
    with open(path, "wb") as f:
        pickle.dump(data, f)
    print(f"快照已保存: {path}  ({sum(len(v) for v in data.values())/1024/1024:.1f} MB)")


def cmd_diff(path_a: str, path_b: str):
    import pickle
    with open(path_a, "rb") as f:
        a = pickle.load(f)
    with open(path_b, "rb") as f:
        b = pickle.load(f)

    for base in a:
        if base not in b:
            continue
        ra, rb = a[base], b[base]
        n = min(len(ra), len(rb))
        # 按 4 字节对齐比较
        diffs = []
        for i in range(0, n - 4, 4):
            if ra[i:i + 4] != rb[i:i + 4]:
                va = int.from_bytes(ra[i:i + 4], "big")
                vb = int.from_bytes(rb[i:i + 4], "big")
                diffs.append((base + i, va, vb))
        print(f"=== 0x{base:012X}: {len(diffs)} 处 4 字节差异 ===")
        # 只显示小整数的差异（语言值通常是 0-20）
        small = [(addr, va, vb) for addr, va, vb in diffs
                 if va < 100 and vb < 100]
        print(f"  其中小整数差异 {len(small)} 处：")
        for addr, va, vb in small[:60]:
            print(f"    0x{addr:012X}: {va} -> {vb}")


def cmd_find(path: str, value: int):
    import pickle
    with open(path, "rb") as f:
        data = pickle.load(f)
    target = int(value).to_bytes(4, "big")
    print(f"=== 搜索值 {value}（大端 4 字节）===")
    total = 0
    for base, raw in data.items():
        idx = 0
        hits = []
        while True:
            i = raw.find(target, idx)
            if i < 0:
                break
            # 检查前后是否也是小整数（排除数据区里的偶然匹配）
            ctx = raw[max(0, i - 8):i + 12]
            hits.append((base + i, ctx))
            idx = i + 4
            if len(hits) > 500:
                break
        print(f"  0x{base:012X}: {len(hits)} 处")
        total += len(hits)
    print(f"  合计 {total} 处")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    cmd = sys.argv[1]
    if cmd == "snap":
        cmd_snap(sys.argv[2])
    elif cmd == "diff":
        cmd_diff(sys.argv[2], sys.argv[3])
    elif cmd == "find":
        cmd_find(sys.argv[2], int(sys.argv[3]))
    else:
        print(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

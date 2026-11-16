"""tools/hid_probe.py — 枚举 HID 设备并尝试读取 DS4 输入报告。"""

from __future__ import annotations

import sys
import time

__import__("sys").path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from acvd import hidapi


def main() -> int:
    paths = hidapi.enumerate_paths()
    print(f"共 {len(paths)} 个 HID 接口\n")
    targets = []
    for p in paths:
        try:
            with hidapi.Device(p) as d:
                info = d.info()
                is_ctrl = d.vid in (0x054C, 0x045E)
                tag = "  <== 手柄" if is_ctrl else ""
                if is_ctrl:
                    print(f"[{info}]{tag}\n    {p}\n")
                    targets.append((p, d.vid, d.pid))
        except OSError:
            continue

    if not targets:
        print("✗ 没找到任何手柄 HID 接口。")
        return 1

    for path, vid, pid in targets:
        if vid != 0x054C:
            continue
        print(f"\n=== 尝试读取 DS4: {path} ===")
        try:
            with hidapi.Device(path) as d:
                print("  info:", d.info())
                got = 0
                t0 = time.time()
                while time.time() - t0 < 3.0 and got < 5:
                    rep = d.read(78)
                    if rep is None:
                        time.sleep(0.01)
                        continue
                    got += 1
                    print(f"  report#{got} len={len(rep)}: {rep[:16].hex(' ')} ...")
                if got == 0:
                    print("  ✗ 3 秒内没读到报告（可能被 DS4Windows/Steam 占用，或蓝牙未连上）")
        except OSError as e:
            print(f"  ✗ 打开失败: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
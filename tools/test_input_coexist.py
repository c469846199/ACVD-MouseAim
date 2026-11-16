"""
tools/test_input_coexist.py — 验证「Xenia 键盘模式 + 虚拟手柄」能否同时工作

这是方案 A 的前提：
    hid = "xinput"        （虚拟手柄占 slot 0）
    keyboard_mode = 1     （键盘也作为 slot 0 的输入）

如果两者共存，那么：
    * 键盘按键能进游戏
    * 虚拟手柄推杆也能进游戏

测试方法：
    用相机 yaw 作为探针（它对输入最敏感）：
        1. 记录初始 yaw
        2. 虚拟手柄推杆 → 看 yaw 变不变
        3. 报告结果
    键盘部分需要人工确认（脚本会提示）。

前提：Xenia 已用 hid=xinput + keyboard_mode=1 重启，守护进程在跑。
"""

from __future__ import annotations

import math
import struct
import sys
import time

__import__("sys").path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from acvd.pipe import PipeClient
from acvd.winmem import Process

CAM = 0x1405F30F0


def cam_yaw(proc):
    raw = proc.read(CAM, 16)
    if not raw or len(raw) < 16:
        return None
    x, y, z, w = struct.unpack(">4f", raw)
    n = x * x + y * y + z * z + w * w
    if abs(n - 1.0) > 0.05:
        return None
    return math.degrees(math.atan2(2 * (w * y + x * z), 1 - 2 * (y * y + z * z)))


def main():
    proc = Process.attach("xenia_canary")
    try:
        cli = PipeClient("acvd_pad")
    except OSError:
        print("✗ 守护进程没跑：python tools\\pad_daemon.py")
        return 1

    y0 = cam_yaw(proc)
    if y0 is None:
        print("✗ 读不到相机（地址可能失效，游戏是否在场景里？）")
        return 1
    print(f"初始相机 yaw = {y0:+.3f}°  （探针地址 0x{CAM:X}）")

    print("\n[测试 1] 虚拟手柄推右摇杆 1.5 秒 ...")
    cli.send("rhold 0.9 0 1.5")
    time.sleep(0.3)
    y1 = cam_yaw(proc)
    if y1 is None:
        print("  ✗ 读不到")
        return 1
    d1 = (y1 - y0 + 180) % 360 - 180
    print(f"  推杆后 yaw = {y1:+.3f}°   Δ={d1:+.3f}°")
    print(f"  {'✓ 虚拟手柄生效' if abs(d1) > 0.5 else '✗ 虚拟手柄无效（被键盘模式覆盖？）'}")

    print("\n[测试 2] 请现在用键盘按 W 和空格（A 键），持续 3 秒 ...")
    for s in range(3, 0, -1):
        print(f"  {s}...", end="\r", flush=True)
        time.sleep(1)
    print("  你自己确认游戏画面有没有反应（移动/确认）      ")

    print("\n结论：")
    print("  若测试 1 通过 → 键盘模式与虚拟手柄可共存（方案 A 可行）")
    print("  若测试 1 失败 → 需方案 B（由脚本监听键盘并转虚拟手柄）")

    proc.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
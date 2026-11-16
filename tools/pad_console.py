"""
tools/pad_console.py — 常驻虚拟手柄命令行

启动后虚拟 X360 手柄保持存活，Xenia 即可识别。
在终端里输入指令控制摇杆/按键，用来恢复游戏。

指令:
    r <x> <y>       右摇杆（-1~1），例如  r 1 0
    l <x> <y>       左摇杆
    rt <v> / lt <v> 扳机（0~1）
    press a|b|x|y|start|back|lb|rb|ls|rs|up|down|left|right [秒]
    hold  <btn> / rel <btn>
    center          摇杆回中
    seq <btn> <n>   连按 n 次（间隔 0.25s），用于菜单确认
    pivot           右摇杆绕一圈（看见锁定圈转没转）
    slots           查看 XInput 槽位占用
    status          打印当前状态
    q               退出（手柄同时断开）
"""

from __future__ import annotations

import sys
import time

__import__("sys").path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from acvd.pad import VirtualPad, detect_slots


class State:
    def __init__(self):
        self.lx = self.ly = self.rx = self.ry = 0.0
        self.lt = self.rt = 0.0


def main() -> int:
    print("创建虚拟 Xbox 360 手柄 ...")
    pad = VirtualPad()
    st = State()
    print(f"✓ 已创建. XInput 槽位: {detect_slots()}")
    print("输入 help 查看指令。")

    def apply():
        pad.left(st.lx, st.ly)
        pad.right(st.rx, st.ry)
        pad.triggers(st.lt, st.rt)
        pad.update()

    apply()
    try:
        while True:
            try:
                line = input("pad> ").strip()
            except EOFError:
                break
            if not line:
                continue
            parts = line.split()
            cmd = parts[0].lower()
            try:
                if cmd == "q":
                    break
                elif cmd == "help":
                    print(__doc__)
                elif cmd == "r" and len(parts) >= 3:
                    st.rx, st.ry = float(parts[1]), float(parts[2]); apply()
                    print(f"右摇杆 ({st.rx:.2f}, {st.ry:.2f})")
                elif cmd == "l" and len(parts) >= 3:
                    st.lx, st.ly = float(parts[1]), float(parts[2]); apply()
                    print(f"左摇杆 ({st.lx:.2f}, {st.ly:.2f})")
                elif cmd == "rt" and len(parts) >= 2:
                    st.rt = float(parts[1]); apply(); print(f"RT {st.rt:.2f}")
                elif cmd == "lt" and len(parts) >= 2:
                    st.lt = float(parts[1]); apply(); print(f"LT {st.lt:.2f}")
                elif cmd == "center":
                    st.lx = st.ly = st.rx = st.ry = 0.0
                    st.lt = st.rt = 0.0
                    apply(); print("回中")
                elif cmd == "press" and len(parts) >= 2:
                    hold = float(parts[2]) if len(parts) >= 3 else 0.08
                    pad.press(parts[1], hold); print(f"按 {parts[1]}")
                elif cmd == "hold" and len(parts) >= 2:
                    pad.hold(parts[1]); print(f"按住 {parts[1]}")
                elif cmd == "rel" and len(parts) >= 2:
                    pad.release(parts[1]); print(f"松开 {parts[1]}")
                elif cmd == "seq" and len(parts) >= 3:
                    n = int(parts[2])
                    for _ in range(n):
                        pad.press(parts[1], 0.08)
                        time.sleep(0.17)
                    print(f"{parts[1]} x{n}")
                elif cmd == "pivot":
                    import math
                    print("右摇杆绕圈 3 秒，观察锁定圈")
                    t0 = time.perf_counter()
                    while time.perf_counter() - t0 < 3.0:
                        t = (time.perf_counter() - t0) * 2 * math.pi
                        st.rx, st.ry = math.cos(t) * 0.8, math.sin(t) * 0.8
                        apply()
                        time.sleep(0.01)
                    st.rx = st.ry = 0.0
                    apply()
                    print("完成")
                elif cmd == "slots":
                    print("XInput 槽位:", detect_slots())
                elif cmd == "status":
                    print(f"L=({st.lx:.2f},{st.ly:.2f}) R=({st.rx:.2f},{st.ry:.2f}) "
                          f"LT={st.lt:.2f} RT={st.rt:.2f} 槽位={detect_slots()}")
                else:
                    print("未知指令，输入 help")
            except Exception as e:
                print(f"错误: {e}")
    except KeyboardInterrupt:
        pass
    finally:
        st.lx = st.ly = st.rx = st.ry = 0.0
        st.lt = st.rt = 0.0
        apply()
        pad.close()
        print("\n手柄已断开")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
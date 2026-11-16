"""
tools/ds4d.py — DS4 → XInput 常驻桥接（单线程，无子线程）

踩坑记录：
  早期版本用 threading.Thread 在后台线程调 pad.update()，
  实测 XInput 端 packet number 冻结不增长（ViGEm 客户端对象非线程安全）。
  改为单线程：主循环「读 DS4 → 解析 → update」一气呵成。

另外：ViGEm 客户端对象与进程同生命周期，进程必须常驻，
    退出时必须 close()，否则残留「已连接但无数据」的僵尸设备。
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import sys
import time
import traceback

__import__("sys").path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from acvd.ds4_bridge import DS4Bridge
from acvd.pad import VirtualPad


class GP(ctypes.Structure):
    _fields_ = [("wButtons", wt.WORD), ("bLeftTrigger", wt.BYTE), ("bRightTrigger", wt.BYTE),
                ("sThumbLX", wt.SHORT), ("sThumbLY", wt.SHORT),
                ("sThumbRX", wt.SHORT), ("sThumbRY", wt.SHORT)]


class ST(ctypes.Structure):
    _fields_ = [("dwPacketNumber", wt.DWORD), ("Gamepad", GP)]


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hz", type=float, default=250.0)
    ap.add_argument("--seconds", type=float, default=0.0, help="0=无限")
    a = ap.parse_args()

    dll = ctypes.WinDLL("XInput1_4.dll")
    dll.XInputGetState.argtypes = [wt.DWORD, ctypes.POINTER(ST)]

    _log("打开 DS4 ...")
    b = DS4Bridge()
    if not b.find():
        _log("✗ 找不到 DS4")
        return 1
    b.connect()
    _log("✓ DS4 已连接")

    _log("创建虚拟 X360 手柄 ...")
    pad = VirtualPad()
    time.sleep(0.15)
    st = ST()
    _log(f"✓ 已创建  xinput={dll.XInputGetState(0, ctypes.byref(st))} "
         f"pkt={st.dwPacketNumber}")

    _log("进入单线程主循环")
    interval = 1.0 / a.hz
    pkts = fails = 0
    t0 = time.perf_counter()
    last_stat = t0
    last_state = None

    try:
        while True:
            raw = b.read_raw()
            if raw is None:
                fails += 1
                time.sleep(0.003)
                if fails % 1000 == 1:
                    _log(f"! DS4 读失败累计 {fails}")
                continue
            d = b.parse(raw)
            if d:
                lx, ly = d["lx"] * 0.9, d["ly"] * 0.9
                # ViGEm 会对「完全相同的值」去重，导致 XInput 端 packet number 不增长，
                # Xenia 会认为没有新输入（表现为手柄卡住不动）。
                # 每帧附加 ±1 LSB 的抖动强制产生变化；1/32767 的幅度对手感无影响。
                jitter = 1 if (pkts & 1) else -1
                pad.left_raw(int(lx * 32767) + jitter, int(ly * 32767) + jitter)
                pad.right_raw(int(d["rx"] * 32767) + jitter, int(d["ry"] * 32767) + jitter)
                pad.triggers(d["l2"], d["r2"])
                pad.update()
                pkts += 1
                last_state = d

            now = time.perf_counter()
            if now - last_stat >= 3.0:
                dll.XInputGetState(0, ctypes.byref(st))
                s = last_state or {}
                _log(f"心跳 pkts={pkts} fails={fails} "
                     f"X pkt={st.dwPacketNumber} LX={st.Gamepad.sThumbLX} "
                     f"LY={st.Gamepad.sThumbLY} | DS4 L=({s.get('lx',0):+.2f},{s.get('ly',0):+.2f})")
                last_stat = now

            if a.seconds and now - t0 >= a.seconds:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        _log("Ctrl+C")
    except BaseException:
        traceback.print_exc()
        raise
    finally:
        pad.close()
        b.stop()
        _log("已清理退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
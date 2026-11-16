"""
tools/pad_daemon.py — 虚拟手柄守护进程（常驻，命名管道 + 可选 DS4 桥接）

为什么不用 stdin 交互：后台任务拿不到交互式 stdin，会立即 EOF 退出。
所以改为守护进程 + 命名管道 \\\\.\\pipe\\acvd_pad。

用法:
    python tools\\pad_daemon.py              # 纯虚拟手柄（脚本控制）
    python tools\\pad_daemon.py --bridge     # 同时桥接 PS4 手柄（DS4 → XInput）

桥接模式说明：
    读 PS4 蓝牙 HID 报告，转成 X360 手柄状态。桥接生效时，
    物理手柄的输入会覆盖脚本推杆；两者可以共存（脚本写入优先给摇杆，
    物理手柄控制按键）。切回脚本控制用 `bridge off`。

管道指令见 PAD_CONTROL.md。
"""

from __future__ import annotations

import argparse
import math
import sys
import threading
import time

__import__("sys").path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))

from acvd.pad import VirtualPad, detect_slots
from acvd.pipe import PipeServer

try:
    import vgamepad as vg
    _XUSB = vg.XUSB_BUTTON
except Exception:
    _XUSB = None

# 位掩码 -> Xbox 按钮常量名
_BUTTON_BITS = [
    (0x1000, "XUSB_GAMEPAD_A"),
    (0x2000, "XUSB_GAMEPAD_B"),
    (0x4000, "XUSB_GAMEPAD_X"),
    (0x8000, "XUSB_GAMEPAD_Y"),
    (0x0010, "XUSB_GAMEPAD_START"),
    (0x0020, "XUSB_GAMEPAD_BACK"),
    (0x0400, "XUSB_GAMEPAD_GUIDE"),
    (0x0100, "XUSB_GAMEPAD_LEFT_SHOULDER"),
    (0x0200, "XUSB_GAMEPAD_RIGHT_SHOULDER"),
    (0x0040, "XUSB_GAMEPAD_LEFT_THUMB"),
    (0x0080, "XUSB_GAMEPAD_RIGHT_THUMB"),
    (0x0001, "XUSB_GAMEPAD_DPAD_UP"),
    (0x0002, "XUSB_GAMEPAD_DPAD_DOWN"),
    (0x0004, "XUSB_GAMEPAD_DPAD_LEFT"),
    (0x0008, "XUSB_GAMEPAD_DPAD_RIGHT"),
]
_ALL_BUTTONS = [name for _, name in _BUTTON_BITS]

# 用户可读名 -> 位掩码
_NAME_TO_BIT = {
    "a": 0x1000, "b": 0x2000, "x": 0x4000, "y": 0x8000,
    "start": 0x0010, "back": 0x0020, "guide": 0x0400,
    "lb": 0x0100, "rb": 0x0200, "l1": 0x0100, "r1": 0x0200,
    "ls": 0x0040, "rs": 0x0080,
    "up": 0x0001, "down": 0x0002, "left": 0x0004, "right": 0x0008,
}


def _button_names(mask: int) -> list[str]:
    return [name for bit, name in _BUTTON_BITS if mask & bit]


def _ds4_buttons(btn0: int, btn1: int) -> int:
    """
    DS4 报告字节 -> XInput 按钮位掩码。

    btn0: bit0=□ bit1=× bit2=○ bit3=△ bit4=L1 bit5=R1 bit6=L2 bit7=R2
    btn1: bit0=SHARE bit1=OPTIONS bit2=L3 bit3=R3 bit4=PS bit5=TOUCHPAD
    映射：×→A  ○→B  □→X  △→Y
    """
    m = 0
    if btn0 & 0x02: m |= 0x1000   # ×  -> A
    if btn0 & 0x04: m |= 0x2000   # ○  -> B
    if btn0 & 0x01: m |= 0x4000   # □  -> X
    if btn0 & 0x08: m |= 0x8000   # △  -> Y
    if btn0 & 0x10: m |= 0x0100   # L1 -> LB
    if btn0 & 0x20: m |= 0x0200   # R1 -> RB
    if btn1 & 0x20: m |= 0x0004   # TOUCHPAD -> DPAD_LEFT（临时，可改）
    if btn1 & 0x08: m |= 0x0080   # R3 -> RS
    if btn1 & 0x04: m |= 0x0040   # L3 -> LS
    if btn1 & 0x02: m |= 0x0010   # OPTIONS -> START
    if btn1 & 0x01: m |= 0x0020   # SHARE   -> BACK
    return m


class PadState:
    """
    双来源状态：
        *_pipe : 管道/脚本写入
        *_phys : 物理手柄（DS4 桥接）写入

    合并规则：
        按钮  = pipe OR phys      （两边都能按，互不覆盖）
        扳机  = max(pipe, phys)   （谁按得深用谁）
        摇杆  = 物理超过死区时用物理，否则用管道
                （避免手柄漂移把脚本推杆覆盖掉）
    """

    DEADZONE = 0.08

    def __init__(self):
        self.lx = self.ly = self.rx = self.ry = 0.0
        self.lt = self.rt = 0.0
        self.buttons = 0
        # 物理手柄
        self.p_present = False
        self.p_lx = self.p_ly = self.p_rx = self.p_ry = 0.0
        self.p_lt = self.p_rt = 0.0
        self.p_buttons = 0
        self.lock = threading.Lock()

    def set_phys(self, lx, ly, rx, ry, lt, rt, buttons):
        with self.lock:
            self.p_present = True
            self.p_lx, self.p_ly, self.p_rx, self.p_ry = lx, ly, rx, ry
            self.p_lt, self.p_rt = lt, rt
            self.p_buttons = buttons

    def clear_phys(self):
        with self.lock:
            self.p_present = False
            self.p_lx = self.p_ly = self.p_rx = self.p_ry = 0.0
            self.p_lt = self.p_rt = 0.0
            self.p_buttons = 0

    def merge(self):
        """返回合并后的 (lx, ly, rx, ry, lt, rt, buttons, source)。"""
        with self.lock:
            def axis(p_pipe, p_phys):
                if self.p_present and abs(p_phys) > self.DEADZONE:
                    return p_phys
                return p_pipe
            lx = axis(self.lx, self.p_lx)
            ly = axis(self.ly, self.p_ly)
            rx = axis(self.rx, self.p_rx)
            ry = axis(self.ry, self.p_ry)
            lt = max(self.lt, self.p_lt) if self.p_present else self.lt
            rt = max(self.rt, self.p_rt) if self.p_present else self.rt
            btn = self.buttons | (self.p_buttons if self.p_present else 0)
            src = ("phys+" if self.p_present else "") + "pipe"
            return lx, ly, rx, ry, lt, rt, btn, src


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bridge", action="store_true", help="同时桥接 PS4 手柄")
    args = ap.parse_args()

    print("[padd] 创建虚拟 Xbox 360 手柄 ...", flush=True)
    pad = VirtualPad()
    st = PadState()
    print(f"[padd] ready. XInput 槽位: {detect_slots()}", flush=True)

    # vgamepad 非线程安全：管道是多线程服务的，所有 pad 调用走同一把锁
    pad_lock = threading.Lock()

    def apply():
        lx, ly, rx, ry, lt, rt, buttons, _src = st.merge()
        with pad_lock:
            pad.left(lx, ly)
            pad.right(rx, ry)
            pad.triggers(lt, rt)
            if _XUSB is None:
                pad.update()
                return
            for bit, name in _BUTTON_BITS:
                try:
                    btn = getattr(_XUSB, name)
                except AttributeError:
                    continue
                if buttons & bit:
                    pad._pad.press_button(button=btn)
                else:
                    pad._pad.release_button(button=btn)
            pad.update()

    apply()

    def handle(line: str) -> str:
        parts = line.split()
        if not parts:
            return "empty"
        cmd = parts[0].lower()
        try:
            if cmd == "r" and len(parts) >= 3:
                st.rx, st.ry = float(parts[1]), float(parts[2]); apply()
                return f"R=({st.rx:.2f},{st.ry:.2f})"
            if cmd == "rhold":
                # rhold <x> <y> <seconds>：推杆保持指定秒数后回中
                x, y, secs = float(parts[1]), float(parts[2]), float(parts[3])
                with st.lock:
                    st.rx, st.ry = x, y
                apply()
                time.sleep(secs)
                with st.lock:
                    st.rx = st.ry = 0.0
                apply()
                return f"rhold {x} {y} {secs}s"
            if cmd == "l" and len(parts) >= 3:
                st.lx, st.ly = float(parts[1]), float(parts[2]); apply()
                return f"L=({st.lx:.2f},{st.ly:.2f})"
            if cmd == "rt":
                st.rt = float(parts[1]); apply(); return f"RT={st.rt:.2f}"
            if cmd == "lt":
                st.lt = float(parts[1]); apply(); return f"LT={st.lt:.2f}"
            if cmd == "center":
                st.lx = st.ly = st.rx = st.ry = st.lt = st.rt = 0.0
                apply(); return "centered"
            if cmd == "press":
                hold = float(parts[2]) if len(parts) >= 3 else 0.08
                bit = _NAME_TO_BIT.get(parts[1].lower())
                if bit is None:
                    return f"unknown button: {parts[1]}"
                with st.lock:
                    st.buttons |= bit
                apply()
                time.sleep(hold)
                with st.lock:
                    st.buttons &= ~bit
                apply()
                return f"pressed {parts[1]}"
            if cmd == "btn":
                # btn 0x1000  —— 直接设置管道按钮掩码
                mask = int(parts[1], 0)
                with st.lock:
                    st.buttons = mask
                apply()
                return f"btn=0x{st.buttons:04X}"
            if cmd == "seq":
                n = int(parts[2]) if len(parts) >= 3 else 1
                bit = _NAME_TO_BIT.get(parts[1].lower())
                if bit is None:
                    return f"unknown button: {parts[1]}"
                for _ in range(n):
                    with st.lock:
                        st.buttons |= bit
                    apply()
                    time.sleep(0.08)
                    with st.lock:
                        st.buttons &= ~bit
                    apply()
                    time.sleep(0.17)
                return f"{parts[1]} x{n}"
            if cmd == "keys":
                # keys <gap> <hold> <k1> <k2> ...
                gap = float(parts[1])
                hold = float(parts[2])
                names = parts[3:]
                for i, k in enumerate(names):
                    bit = _NAME_TO_BIT.get(k.lower())
                    if bit is None:
                        return f"unknown button: {k}"
                    with st.lock:
                        st.buttons |= bit
                    apply()
                    time.sleep(hold)
                    with st.lock:
                        st.buttons &= ~bit
                    apply()
                    if i != len(names) - 1:
                        time.sleep(gap)
                return f"sent {len(names)} keys gap={gap}"
            if cmd == "pivot":
                t0 = time.perf_counter()
                while time.perf_counter() - t0 < 3.0:
                    t = (time.perf_counter() - t0) * 2 * math.pi
                    with st.lock:
                        st.rx, st.ry = math.cos(t) * 0.8, math.sin(t) * 0.8
                    apply()
                    time.sleep(0.008)
                with st.lock:
                    st.rx = st.ry = 0.0
                apply()
                return "pivot done"
            if cmd == "status":
                br = "on" if bridge_state["on"] else "off"
                pk = bridge_state["packets"]
                return (f"L=({st.lx:.2f},{st.ly:.2f}) R=({st.rx:.2f},{st.ry:.2f}) "
                        f"LT={st.lt:.2f} RT={st.rt:.2f} btn=0x{st.buttons:04X} "
                        f"slots={detect_slots()} bridge={br}({pk})")
            if cmd == "slots":
                return f"slots={detect_slots()}"
            if cmd == "bridge":
                # bridge on | bridge off
                arg = parts[1].lower() if len(parts) > 1 else ""
                if arg == "on":
                    msg = ensure_ds4()
                    if bridge_state["ds4"] is None:
                        return msg
                    bridge_state["on"] = True
                    return f"bridge on — {msg}"
                if arg == "off":
                    bridge_state["on"] = False
                    st.clear_phys()
                    with st.lock:
                        st.lx = st.ly = st.rx = st.ry = 0.0
                        st.buttons = 0
                    apply()
                    return "bridge off"
                br = "on" if bridge_state["on"] else "off"
                return (f"bridge={br} packets={bridge_state['packets']} "
                        f"ds4={'connected' if bridge_state['ds4'] else 'none'}")
            return f"unknown: {cmd}"
        except Exception as e:
            return f"error: {e}"

    server = PipeServer("acvd_pad", handle)
    server.start()
    print("[padd] pipe ready: \\\\.\\pipe\\acvd_pad", flush=True)

    # ---- DS4 桥接 ---------------------------------------------------
    bridge_state = {"on": False, "packets": 0, "ds4": None}
    quit_evt = threading.Event()

    def ensure_ds4() -> str:
        """确保 DS4 已连接；返回状态描述。"""
        from acvd.ds4_bridge import DS4Bridge
        if bridge_state["ds4"] is not None:
            return "DS4 已连接"
        ds4 = DS4Bridge()
        path = ds4.find()
        if not path:
            return "未找到 DS4（检查蓝牙连接）"
        try:
            ds4.connect()
        except OSError as e:
            return f"DS4 打开失败: {e}"
        bridge_state["ds4"] = ds4
        return f"DS4 已连接 ({path[-20:]})"

    if args.bridge:
        msg = ensure_ds4()
        print(f"[padd] {msg}", flush=True)
        if bridge_state["ds4"] is not None:
            bridge_state["on"] = True
            print("[padd] DS4 桥接已启用", flush=True)

        def bridge_loop():
            misses = 0
            while not quit_evt.is_set():
                if not bridge_state["on"]:
                    time.sleep(0.1)
                    continue
                ds4 = bridge_state["ds4"]
                if ds4 is None:
                    time.sleep(0.2)
                    continue
                try:
                    raw = ds4.read_raw()
                except Exception:
                    raw = None
                if raw is None:
                    misses += 1
                    if misses > 300:
                        # 连续 300 次读不到（约 1.5s），视为手柄断开
                        st.clear_phys()
                        apply()
                        misses = 0
                    time.sleep(0.005)
                    continue
                misses = 0
                d = ds4.parse(raw)
                if not d:
                    time.sleep(0.005)
                    continue
                st.set_phys(d["lx"] * 0.9, d["ly"] * 0.9, d["rx"], d["ry"],
                            d["l2"], d["r2"], _ds4_buttons(d["btn0"], d["btn1"]))
                apply()
                bridge_state["packets"] += 1
                time.sleep(0.002)

        threading.Thread(target=bridge_loop, daemon=True).start()

    def on_line(line: str) -> bool:
        if line.strip().lower() in ("quit", "exit", "q"):
            quit_evt.set()
            return True
        return False

    # 额外挂一个 quit 监听（同一个 pipe 名，由 server 回调区分）
    server.on_quit = on_line

    try:
        quit_evt.wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
        st.lx = st.ly = st.rx = st.ry = st.lt = st.rt = 0.0
        apply()
        pad.close()
        print("[padd] 退出，手柄已断开", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
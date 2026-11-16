"""
pad.py — 虚拟 Xbox 360 手柄（ViGEmBus + vgamepad）

用途：Xenia 的 hid="xinput" 只认 XInput 设备，PS4 手柄（DirectInput）不被识别。
本模块创建 ViGEmBus 虚拟 X360 手柄，替代原手柄，既用于人工恢复游戏，
也用于伺服输出。

⚠ 槽位说明：ViGEm 虚拟手柄会占用一个 XInput 槽（通常是 0）。
   物理 XInput 手柄若插着，可能被挤到 1 号槽。PS4 手柄走 DirectInput，不受影响。
"""

from __future__ import annotations

import time

try:
    import vgamepad as vg
    _HAS_VG = True
except Exception:
    _HAS_VG = False

# XInput 按钮位 -> vgamepad 常量名
_MASK_TO_VG = {
    0x0001: "XUSB_GAMEPAD_DPAD_UP",
    0x0002: "XUSB_GAMEPAD_DPAD_DOWN",
    0x0004: "XUSB_GAMEPAD_DPAD_LEFT",
    0x0008: "XUSB_GAMEPAD_DPAD_RIGHT",
    0x0010: "XUSB_GAMEPAD_START",
    0x0020: "XUSB_GAMEPAD_BACK",
    0x0040: "XUSB_GAMEPAD_LEFT_THUMB",
    0x0080: "XUSB_GAMEPAD_RIGHT_THUMB",
    0x0100: "XUSB_GAMEPAD_LEFT_SHOULDER",
    0x0200: "XUSB_GAMEPAD_RIGHT_SHOULDER",
    0x1000: "XUSB_GAMEPAD_A",
    0x2000: "XUSB_GAMEPAD_B",
    0x4000: "XUSB_GAMEPAD_X",
    0x8000: "XUSB_GAMEPAD_Y",
}


class VirtualPad:
    def __init__(self):
        if not _HAS_VG:
            raise RuntimeError("vgamepad 未安装：pip install vgamepad")
        self._pad = vg.VX360Gamepad()
        self._last_mask = -1          # 强制首次提交
        # 触发器默认是 0，摇杆默认回中（XInputValue 的默认值就是 0 / 32767）
        self._pad.update()
        self._center()

    # ---- 基础 -------------------------------------------------------
    def _center(self):
        self._pad.left_joystick(0, 0)
        self._pad.right_joystick(0, 0)
        self._pad.left_trigger(value=0)
        self._pad.right_trigger(value=0)
        self._pad.update()

    def update(self):
        self._pad.update()

    def close(self):
        self._center()

    # ---- 摇杆（-1.0 ~ 1.0）-----------------------------------------
    def left(self, x: float, y: float):
        self._pad.left_joystick(x_value=_clamp_i(x), y_value=_clamp_i(y))

    def right(self, x: float, y: float):
        self._pad.right_joystick(x_value=_clamp_i(x), y_value=_clamp_i(y))

    def left_raw(self, x: int, y: int):
        """直接写 XInput 原始量程 [-32768, 32767]，用于加入 LSB 抖动。"""
        self._pad.left_joystick(x_value=max(-32768, min(32767, x)),
                                y_value=max(-32768, min(32767, y)))

    def right_raw(self, x: int, y: int):
        self._pad.right_joystick(x_value=max(-32768, min(32767, x)),
                                 y_value=max(-32768, min(32767, y)))

    def triggers(self, lt: float = 0.0, rt: float = 0.0):
        self._pad.left_trigger(value=_clamp_b(lt))
        self._pad.right_trigger(value=_clamp_b(rt))

    # ---- 按键 -------------------------------------------------------
    BUTTONS = {
        "a": "XUSB_GAMEPAD_A", "b": "XUSB_GAMEPAD_B", "x": "XUSB_GAMEPAD_X",
        "y": "XUSB_GAMEPAD_Y", "start": "XUSB_GAMEPAD_START",
        "back": "XUSB_GAMEPAD_BACK", "guide": "XUSB_GAMEPAD_GUIDE",
        "lb": "XUSB_GAMEPAD_LEFT_SHOULDER", "rb": "XUSB_GAMEPAD_RIGHT_SHOULDER",
        "ls": "XUSB_GAMEPAD_LEFT_THUMB", "rs": "XUSB_GAMEPAD_RIGHT_THUMB",
        "up": "XUSB_GAMEPAD_DPAD_UP", "down": "XUSB_GAMEPAD_DPAD_DOWN",
        "left": "XUSB_GAMEPAD_DPAD_LEFT", "right": "XUSB_GAMEPAD_DPAD_RIGHT",
    }

    def press(self, name: str, hold: float = 0.08):
        btn = getattr(vg.XUSB_BUTTON, self.BUTTONS[name])
        self._pad.press_button(button=btn)
        self._pad.update()
        time.sleep(hold)
        self._pad.release_button(button=btn)
        self._pad.update()

    def hold(self, name: str):
        self._pad.press_button(button=getattr(vg.XUSB_BUTTON, self.BUTTONS[name]))
        self._pad.update()

    def release(self, name: str):
        self._pad.release_button(button=getattr(vg.XUSB_BUTTON, self.BUTTONS[name]))
        self._pad.update()

    def set_buttons(self, mask: int):
        """按位掩码设置所有按钮。

        hid="any" 模式下 winkey 轮询读取按钮状态，必须每帧全量
        press/release（不能去重），否则旧版 Xenia 读不到按键。
        """
        for bit, name in _MASK_TO_VG.items():
            try:
                btn = getattr(vg.XUSB_BUTTON, name)
            except AttributeError:
                continue
            if mask & bit:
                self._pad.press_button(button=btn)
            else:
                self._pad.release_button(button=btn)


def _clamp_i(v: float) -> int:
    return int(max(-32768, min(32767, round(v * 32767))))


def _clamp_b(v: float) -> int:
    return int(max(0, min(255, round(v * 255))))


def detect_slots() -> list[int]:
    """列出当前被 XInput 占用的槽位。"""
    import ctypes
    import ctypes.wintypes as wt
    for dll in ("XInput1_4.dll", "XInput1_3.dll"):
        try:
            d = ctypes.WinDLL(dll)
            break
        except OSError:
            continue
    else:
        return []

    class GP(ctypes.Structure):
        _fields_ = [("wButtons", wt.WORD), ("bLeftTrigger", wt.BYTE), ("bRightTrigger", wt.BYTE),
                    ("sThumbLX", wt.SHORT), ("sThumbLY", wt.SHORT),
                    ("sThumbRX", wt.SHORT), ("sThumbRY", wt.SHORT)]

    class ST(ctypes.Structure):
        _fields_ = [("dwPacketNumber", wt.DWORD), ("Gamepad", GP)]

    d.XInputGetState.argtypes = [wt.DWORD, ctypes.POINTER(ST)]
    out = []
    for i in range(4):
        st = ST()
        if d.XInputGetState(i, ctypes.byref(st)) == 0:
            out.append(i)
    return out
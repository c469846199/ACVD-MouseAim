"""
acvd/rawmouse.py — Raw Input 鼠标位移累加器（绝不丢像素）

## 为什么不用 GetCursorPos

绝对光标位置有几个致命问题：
  * 光标会被屏幕边界截断 —— 卡顿一次积压几百像素，多出来的部分直接丢
  * Windows 加速曲线会改写位移（已关，但系统策略可能被其它程序改回来）
  * 每帧回写中心点会与系统移动光标互相干扰

## Raw Input 的做法

底层驱动事件（WM_INPUT）在**加速和截断之前**就把原始计数送过来。我们在
独立线程的消息循环里把每个 lLastX / lLastY 累加到 delta_pool，
主循环一次性取干净并清零。线程与主循环之间用锁保护，取走即清零，
所以任何一帧的位移都不会被下一帧覆盖或漏掉。

## 用法

    rm = RawMouse()
    rm.start()              # 注册（RIDEV_INPUTSINK，非焦点也能收到）
    dx, dy, n = rm.drain()  # 取走并清零
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WM_INPUT = 0x00FF
WM_DESTROY = 0x0002

RID_INPUT = 0x10000003
RIM_TYPEMOUSE = 0

RIDEV_REMOVE = 0x00000001
RIDEV_INPUTSINK = 0x00000100

HWND_MESSAGE = ctypes.c_void_p(-3)

MOUSE_MOVE_ABSOLUTE = 0x01
MOUSE_VIRTUAL_DESKTOP = 0x02

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [("usUsagePage", wt.USHORT),
                ("usUsage", wt.USHORT),
                ("dwFlags", wt.DWORD),
                ("hwndTarget", wt.HWND)]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [("dwType", wt.DWORD),
                ("dwSize", wt.DWORD),
                ("hDevice", wt.HANDLE),
                ("wParam", wt.WPARAM)]


class RAWMOUSE(ctypes.Structure):
    _fields_ = [("usFlags", wt.USHORT),
                ("ulButtons", wt.ULONG),
                ("ulRawButtons", wt.ULONG),
                ("lLastX", wt.LONG),
                ("lLastY", wt.LONG),
                ("ulExtraInformation", wt.ULONG)]


class RAWINPUT(ctypes.Structure):
    _fields_ = [("header", RAWINPUTHEADER),
                ("mouse", RAWMOUSE)]


assert ctypes.sizeof(RAWINPUTDEVICE) == 16, "RAWINPUTDEVICE 布局错误"
assert ctypes.sizeof(RAWINPUTHEADER) == 24, "RAWINPUTHEADER 布局错误"


class RawMouse:
    """独立线程消费 WM_INPUT，把原始位移累加进池子。"""

    _BUFSZ = 256

    def __init__(self):
        self._lock = threading.Lock()
        self._dx = 0
        self._dy = 0
        self._events = 0          # 累加过的原始事件数
        self._absolute = 0        # 绝对坐标事件（数位板/远程桌面）计数
        self._wm_input = 0        # 收到的 WM_INPUT 总数（诊断）
        self._bad_read = 0        # GetRawInputData 失败次数
        self._not_mouse = 0       # 非鼠标类型的消息
        self._zero_move = 0       # 位移为 0 的消息（纯按键 / 抖动）
        self._parse_err = 0       # 解析异常次数
        self._last_err = ""       # 最近一次解析失败的原因（诊断）
        self.ok = False
        self.hwnd = 0
        self._ready = threading.Event()
        self._wndproc_ref = None
        self._thread = None

    # ---- 生命周期 ----------------------------------------------------
    def start(self, timeout: float = 3.0) -> bool:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(timeout)
        return self.ok

    def _run(self):
        try:
            hinst = kernel32.GetModuleHandleW(None)
            cls_name = "acvd_rawmouse"

            # 注册窗口类（消息专用窗口，不可见）
            class WNDCLASSEXW(ctypes.Structure):
                _fields_ = [
                    ("cbSize", wt.UINT), ("style", wt.UINT),
                    ("lpfnWndProc", WNDPROC),
                    ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                    ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                    ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
                    ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR),
                    ("hIconSm", wt.HICON),
                ]

            self._wndproc_ref = WNDPROC(self._wndproc)
            wc = WNDCLASSEXW()
            wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
            wc.lpfnWndProc = self._wndproc_ref
            wc.hInstance = hinst
            wc.lpszClassName = cls_name
            user32.RegisterClassExW(ctypes.byref(wc))

            hwnd = user32.CreateWindowExW(
                0, cls_name, "acvd", 0, 0, 0, 0, 0,
                HWND_MESSAGE, None, hinst, None)
            if not hwnd:
                self._ready.set()
                return
            self.hwnd = hwnd

            rid = RAWINPUTDEVICE()
            rid.usUsagePage = 0x01          # Generic Desktop
            rid.usUsage = 0x02              # Mouse
            rid.dwFlags = RIDEV_INPUTSINK   # 非焦点也收
            rid.hwndTarget = hwnd
            if not user32.RegisterRawInputDevices(
                    ctypes.byref(rid), 1, ctypes.sizeof(RAWINPUTDEVICE)):
                self._ready.set()
                return

            self.ok = True
            self._ready.set()

            msg = wt.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            self._ready.set()

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_INPUT:
            with self._lock:
                self._wm_input += 1
            try:
                # 固定缓冲区一次读完，避免两次调用之间尺寸变化的坑
                buf = ctypes.create_string_buffer(self._BUFSZ)
                size = wt.UINT(self._BUFSZ)
                got = user32.GetRawInputData(
                    ctypes.c_void_p(lparam), RID_INPUT, buf,
                    ctypes.byref(size), ctypes.sizeof(RAWINPUTHEADER))
                if got == 0 or got == 0xFFFFFFFF:
                    with self._lock:
                        self._bad_read += 1
                        self._last_err = f"got={got} size={size.value}"
                    return 0
                ri = ctypes.cast(buf, ctypes.POINTER(RAWINPUT)).contents
                if ri.header.dwType != RIM_TYPEMOUSE:
                    with self._lock:
                        self._not_mouse += 1
                    return 0
                m = ri.mouse
                if m.usFlags & MOUSE_MOVE_ABSOLUTE:
                    with self._lock:
                        self._absolute += 1
                    return 0
                lx, ly = int(m.lLastX), int(m.lLastY)
                with self._lock:
                    if lx or ly:
                        self._dx += lx
                        self._dy += ly
                        self._events += 1
                    else:
                        self._zero_move += 1
            except Exception as e:
                with self._lock:
                    self._parse_err += 1
                    self._last_err = f"{type(e).__name__}: {e}"
            return 0
        if msg == WM_DESTROY:
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    # ---- 取走位移 ----------------------------------------------------
    def drain(self):
        """取走并清零累积位移，返回 (dx, dy, events, absolute_events)。"""
        with self._lock:
            dx, dy = self._dx, self._dy
            n, absn = self._events, self._absolute
            self._dx = self._dy = 0
            self._events = self._absolute = 0
        return dx, dy, n, absn

    def stats(self):
        with self._lock:
            return {"events": self._events, "dx": self._dx, "dy": self._dy,
                    "absolute": self._absolute, "ok": self.ok,
                    "wm_input": self._wm_input, "bad_read": self._bad_read,
                    "not_mouse": self._not_mouse, "zero_move": self._zero_move,
                    "parse_err": self._parse_err, "last_err": self._last_err}


# 先声明函数原型（ctypes 默认参数转换对 LPARAM 不够，显式标注更安全）
user32.GetRawInputData.argtypes = [
    ctypes.c_void_p, wt.UINT, ctypes.c_void_p,
    ctypes.POINTER(wt.UINT), wt.UINT]
user32.GetRawInputData.restype = wt.UINT
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.RegisterRawInputDevices.argtypes = [
    ctypes.POINTER(RAWINPUTDEVICE), wt.UINT, wt.UINT]
user32.RegisterRawInputDevices.restype = wt.BOOL
user32.RegisterClassExW.argtypes = [ctypes.c_void_p]
user32.RegisterClassExW.restype = wt.WORD
user32.CreateWindowExW.argtypes = [
    wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wt.HWND, wt.HMENU, wt.HINSTANCE, ctypes.c_void_p]
user32.CreateWindowExW.restype = wt.HWND

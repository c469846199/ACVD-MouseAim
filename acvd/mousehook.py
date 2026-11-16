"""
acvd/mousehook.py — 低级鼠标钩子（WH_MOUSE_LL）

为什么需要单独一个：
    WH_KEYBOARD_LL 只收键盘事件，鼠标按键收不到。
    WH_MOUSE_LL 能拿到全系统的鼠标按键事件，不受焦点限制。

用法：
    mh = MouseHook()
    mh.start()
    mh.is_down("left")     # "left" / "right" / "middle"
    mh.stop()
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WH_MOUSE_LL = 14
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208

LRESULT = ctypes.c_ssize_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wt.WPARAM, wt.LPARAM)

_BTN = {
    WM_LBUTTONDOWN: ("left", True),
    WM_LBUTTONUP: ("left", False),
    WM_RBUTTONDOWN: ("right", True),
    WM_RBUTTONUP: ("right", False),
    WM_MBUTTONDOWN: ("middle", True),
    WM_MBUTTONUP: ("middle", False),
}

user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD]
user32.SetWindowsHookExW.restype = wt.HANDLE
user32.CallNextHookEx.argtypes = [wt.HANDLE, ctypes.c_int, wt.WPARAM, wt.LPARAM]
user32.CallNextHookEx.restype = LRESULT
user32.UnhookWindowsHookEx.argtypes = [wt.HANDLE]
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
kernel32.GetModuleHandleW.restype = wt.HMODULE


class MouseHook:
    """全局低级鼠标钩子，维护按键按下状态。"""

    def __init__(self):
        self._down: set[str] = set()
        self._lock = threading.Lock()
        self._hook = None
        self._thread = None
        self._proc_ref = None
        self._ready = threading.Event()

    def start(self, timeout: float = 3.0) -> bool:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self._ready.wait(timeout)

    def _run(self):
        def handler(nCode, wParam, lParam):
            if nCode == 0:
                info = _BTN.get(int(wParam))
                if info is not None:
                    name, pressed = info
                    with self._lock:
                        if pressed:
                            self._down.add(name)
                        else:
                            self._down.discard(name)
            return user32.CallNextHookEx(None, nCode, wParam, lParam)

        self._proc_ref = HOOKPROC(handler)
        hmod = kernel32.GetModuleHandleW(None)
        self._hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._proc_ref, hmod, 0)
        if not self._hook:
            print(f"✗ 鼠标钩子失败 err={ctypes.get_last_error()}", flush=True)
            self._ready.set()
            return
        self._ready.set()

        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def is_down(self, name: str) -> bool:
        with self._lock:
            return name in self._down

    def clear(self):
        with self._lock:
            self._down.clear()

    def stop(self):
        if self._hook:
            user32.UnhookWindowsHookEx(self._hook)
            self._hook = None

"""
acvd/keyhook.py — 低级键盘钩子（WH_KEYBOARD_LL）

为什么不用 GetAsyncKeyState：
    后台进程读不到前台窗口的按键状态（Windows 输入隔离）。
    WH_KEYBOARD_LL 钩子能接收全系统的键盘事件，不受焦点限制。

用法：
    kh = KeyHook()
    kh.start()
    kh.is_down(vk)      # 某个键当前是否按下
    kh.stop()
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading
import time

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105

LRESULT = ctypes.c_ssize_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wt.WPARAM, wt.LPARAM)


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wt.DWORD),
                ("scanCode", wt.DWORD),
                ("flags", wt.DWORD),
                ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD]
user32.SetWindowsHookExW.restype = wt.HANDLE
user32.CallNextHookEx.argtypes = [wt.HANDLE, ctypes.c_int, wt.WPARAM, wt.LPARAM]
user32.CallNextHookEx.restype = LRESULT
user32.UnhookWindowsHookEx.argtypes = [wt.HANDLE]
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
kernel32.GetModuleHandleW.restype = wt.HMODULE


class KeyHook:
    """全局低级键盘钩子：维护按键状态，并可**吞掉指定键**不传给前台窗口。

    ## 为什么要能吞

    mapper 的热键（F1/F2/F9/F10/ScrollLock）以前只是被"看见"，按键本身照样
    传给前台窗口。F10 在 Windows 里是「激活菜单栏」：

        按 F10 → 前台进入菜单模式（叮）
        → 之后每个 WASD 都是无效菜单项 → 每键叮一声

    玩家听到的就是「恢复映射后一片系统警告音，像在错误的窗口里打错字」。
    F1（帮助）、F9（重算）同理。

    ## 注意顺序

    先更新 `_down`，再决定吞不吞。这样 mapper 的 `is_down()` 依旧能读到热键
    （低级钩子吞掉之后 GetAsyncKeyState 会返回 0，靠它轮询就瞎了）。
    """

    def __init__(self, swallow=None):
        self._down: set[int] = set()
        self._lock = threading.Lock()
        self._hook = None
        self._thread = None
        self._proc_ref = None
        self._ready = threading.Event()
        # 返回"要被吞掉的 vk 集合"的函数。用函数而不是集合：
        # 键位支持热重载，每次按键现取，换键立刻生效。
        self.swallow = swallow or (lambda: set())
        self.blocked = 0

    def start(self, timeout: float = 3.0) -> bool:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self._ready.wait(timeout)

    def _run(self):
        def handler(nCode, wParam, lParam):
            if nCode == 0:
                kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                vk = int(kb.vkCode)
                is_down_msg = wParam in (WM_KEYDOWN, WM_SYSKEYDOWN)
                is_up_msg = wParam in (WM_KEYUP, WM_SYSKEYUP)
                with self._lock:
                    if is_down_msg:
                        self._down.add(vk)
                    elif is_up_msg:
                        self._down.discard(vk)
                # 记录完状态再决定吞不吞（见类注释）
                try:
                    if vk in self.swallow():
                        if is_down_msg:
                            self.blocked += 1
                        return 1        # 不传给前台窗口
                except Exception:
                    pass                # 出任何问题都放行，绝不吞掉正常按键
            return user32.CallNextHookEx(None, nCode, wParam, lParam)

        self._proc_ref = HOOKPROC(handler)
        hmod = kernel32.GetModuleHandleW(None)
        self._hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc_ref, hmod, 0)
        if not self._hook:
            print(f"✗ SetWindowsHookExW 失败 err={ctypes.get_last_error()}", flush=True)
            self._ready.set()
            return
        self._ready.set()

        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def is_down(self, vk: int) -> bool:
        with self._lock:
            return vk in self._down

    def down_keys(self) -> set[int]:
        with self._lock:
            return set(self._down)

    def force_release(self, vk: int):
        """强制清除某个键的按下状态（修正漏收 KEYUP 的卡键）。"""
        with self._lock:
            self._down.discard(vk)

    def clear(self):
        with self._lock:
            self._down.clear()

    def stop(self):
        if self._hook:
            user32.UnhookWindowsHookEx(self._hook)
            self._hook = None
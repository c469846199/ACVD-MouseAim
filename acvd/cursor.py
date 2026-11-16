"""
acvd/cursor.py — 可靠的系统光标隐藏/恢复

## 为什么 ShowCursor(False) 不够

ShowCursor 是【进程级引用计数】，而且会被其他程序（游戏、其他工具）的
调用干扰。即使返回负值，光标也可能仍然可见。

## 可靠方案（双重保险）

1. ShowCursor(False) 多次，直到返回负值
2. 全局替换光标为 NULL（SetCursor 需要在消息循环里持续执行，或
   用 SetClassLongPtr 换掉窗口类的光标 —— 我们的覆盖层不是全屏，
   所以用「每帧 SetCursor(0)」+ 一个定时器线程）

实际最稳的做法：在自己的准星窗口里响应 WM_SETCURSOR 返回 TRUE，
并用 ClipCursor 把真实光标限制在准星窗口内（窗口透明，看不见光标）。

不过最实用的还是：ShowCursor 计数拉到足够负 + 每帧 SetCursor(NULL)。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading
import time

user32 = ctypes.WinDLL("user32", use_last_error=True)

user32.ShowCursor.argtypes = [wt.BOOL]
user32.ShowCursor.restype = ctypes.c_int
user32.SetCursor.argtypes = [ctypes.c_void_p]
user32.SetCursor.restype = ctypes.c_void_p


class CursorHider:
    """
    隐藏系统光标。

    use_timer=True 时起一个后台线程持续 SetCursor(NULL)，
    这样即使有任何程序调用 ShowCursor(True)，光标也立刻变回不可见。
    """

    def __init__(self, use_timer: bool = True):
        self.use_timer = use_timer
        self._saved_count = 0
        self._stop = threading.Event()
        self._thread = None
        self._enabled = False

    def start(self):
        if self.use_timer and self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
        self.set_enabled(True)
        return self._saved_count

    def set_enabled(self, on: bool):
        """只在需要时隐藏；切到别的窗口要恢复，否则全屏都看不见光标。"""
        if on == self._enabled:
            return
        self._enabled = on
        if on:
            n = user32.ShowCursor(False)
            guard = 0
            while n >= 0 and guard < 32:
                n = user32.ShowCursor(False)
                guard += 1
            self._saved_count = n
            user32.SetCursor(None)
        else:
            n = user32.ShowCursor(True)
            guard = 0
            while n < 0 and guard < 64:
                n = user32.ShowCursor(True)
                guard += 1

    def _loop(self):
        while not self._stop.is_set():
            if self._enabled:
                user32.SetCursor(None)
            time.sleep(0.008)

    def stop(self):
        self._stop.set()
        self._enabled = False
        if self._thread:
            self._thread.join(timeout=1.0)
        n = user32.ShowCursor(True)
        guard = 0
        while n < 0 and guard < 64:
            n = user32.ShowCursor(True)
            guard += 1
        return n

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *a):
        self.stop()
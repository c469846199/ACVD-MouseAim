"""
acvd/focus.py — 判断 Xenia 是否在前台

让映射只在游戏有焦点时生效：切换到其他窗口打字时，游戏不会乱动。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import time

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = wt.HWND
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowThreadProcessId.restype = wt.DWORD
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.IsWindowVisible.restype = wt.BOOL
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.GetWindowRect.restype = wt.BOOL

_ENUM_PROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
user32.EnumWindows.argtypes = [_ENUM_PROC, wt.LPARAM]
user32.EnumWindows.restype = wt.BOOL

kernel32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wt.HANDLE
kernel32.Process32First.argtypes = [wt.HANDLE, ctypes.c_void_p]
kernel32.Process32Next.argtypes = [wt.HANDLE, ctypes.c_void_p]
kernel32.CloseHandle.argtypes = [wt.HANDLE]


def _find_pid_by_prefix(prefix: str) -> int | None:
    TH32CS_SNAPPROCESS = 0x00000002

    class PE32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
            ("th32ProcessID", wt.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
            ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wt.DWORD), ("szExeFile", wt.CHAR * 260)]

    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == wt.HANDLE(-1).value:
        return None
    try:
        pe = PE32()
        pe.dwSize = ctypes.sizeof(pe)
        ok = kernel32.Process32First(snap, ctypes.byref(pe))
        while ok:
            name = pe.szExeFile.decode("mbcs", "replace").lower()
            if name.startswith(prefix):
                return pe.th32ProcessID
            ok = kernel32.Process32Next(snap, ctypes.byref(pe))
    finally:
        kernel32.CloseHandle(snap)
    return None


def _find_pid_by_window_title(title_prefix: str) -> int | None:
    """枚举窗口，按**标题开头**匹配，反查 pid。

    ## 为什么这条链可靠

    全程不碰进程枚举，每一步都只用「窗口对象」：

        EnumWindows                枚举窗口（与进程列表完全不同的内核对象）
        GetWindowTextW             取窗口标题
        GetWindowThreadProcessId   窗口 → pid

    实测：进程枚举只看到 19 个进程的 EXE 环境里，EnumWindows 照样能枚举
    26 个窗口并正确拿到 xenia 的 pid。

    ## 为什么用「开头匹配」而不是「包含匹配」

    Xenia 的窗口标题固定以 `Xenia-edge` 开头：

        Xenia-edge (edge@16aa48a96 on Nov  7 2026) | [4E4D0864 ...]

    而当前环境里恰好有个标题**包含** xenia 但不是它的窗口：

        Xenia 鼠标视角控制脚本方案 — DeepSeek Harness    ← 浏览器/Harness

    用 `in` 包含匹配会误中后者（实测发生，pid 拿到 22692 而不是 10776）。
    用 `startswith` 开头匹配就精确了：真目标是 `xenia-edge`，
    干扰项是 `xenia `（带空格），前缀不同。

    ## 局限

    进程必须有可见顶层窗口。Xenia 启动瞬间或无窗口时匹配不到，
    所以只在进程枚举失败时才走这条退路。
    """
    key = title_prefix.lower()
    hit = [0]

    def cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if buf.value.lower().startswith(key):
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value:
                hit[0] = pid.value
                return False            # 找到就停
        return True

    try:
        user32.EnumWindows(_ENUM_PROC(cb), 0)
    except Exception:
        return None
    return hit[0] or None


def _largest_visible_window(pid: int) -> int:
    best = [0, 0]   # [area, hwnd]

    def cb(hwnd, _lparam):
        wpid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value != pid or not user32.IsWindowVisible(hwnd):
            return True
        r = wt.RECT()
        if user32.GetWindowRect(hwnd, ctypes.byref(r)):
            area = (r.right - r.left) * (r.bottom - r.top)
            if area > best[0]:
                best[0], best[1] = area, hwnd
        return True

    ref = _ENUM_PROC(cb)
    user32.EnumWindows(ref, 0)
    return best[1]


class FocusWatcher:
    """
    追踪 Xenia 主窗口，判断其是否在前台。

    用法：
        fw = FocusWatcher()
        if fw.is_focused():   # 只在游戏有焦点时处理输入
            ...
    """

    def __init__(self, exe_prefix: str = "xenia_canary", refresh: float = 2.0):
        self.prefix = exe_prefix
        self.refresh = refresh
        self.pid: int | None = None
        self.hwnd: int = 0
        self._next_refresh = 0.0

    def _refresh(self):
        self.pid = _find_pid_by_prefix(self.prefix)
        if self.pid is None:
            # 进程枚举失败时退回「按窗口反查 pid」。
            #
            # 为什么需要这条退路（打包成 EXE 后实测）：
            #   在 PyInstaller 产物里，EnumProcesses / Toolhelp32 / WMI
            #   三条路都只能看到 19 个左右的进程，xenia_edge 恰好不在其中。
            #   但同一个进程里：
            #       EnumWindows        → 17 个窗口，xenia 的窗口在
            #       OpenProcess(pid)   → 成功，能读内存
            #   也就是说**枚举列表被裁剪了，对象本身可访问**。
            #   窗口枚举走的是与进程枚举完全不同的内核对象，不受这个裁剪影响，
            #   所以用它反查 pid 是可靠的回退。
            #
            # 代价：进程必须有可见顶层窗口。所以只在枚举失败时才用这条退路。
            # 标题前缀用 "xenia-edge"（带连字符），这是 xenia_edge.exe 的
            # 窗口标题固定开头；用 "xenia" 会误中标题以 "Xenia " 开头的
            # 其它窗口（实测把浏览器的「Xenia 鼠标视角控制脚本方案」认成了目标）。
            self.pid = _find_pid_by_window_title("xenia-edge")
        self.hwnd = _largest_visible_window(self.pid) if self.pid else 0
        self._next_refresh = time.perf_counter() + self.refresh

    def is_focused(self) -> bool:
        # 按「前台窗口属于 xenia 进程」判断，而不是句柄相等：
        # xenia-edge 有多个顶层窗口（主框、渲染面板…），前台那个往往不是
        # 面积最大的那个，用句柄比较会永远判成失焦，把整个闭环掐掉。
        now = time.perf_counter()
        if now >= self._next_refresh or not self.pid:
            self._refresh()
        if not self.pid:
            return False
        fg = user32.GetForegroundWindow()
        if not fg:
            return False
        wpid = wt.DWORD(0)
        user32.GetWindowThreadProcessId(fg, ctypes.byref(wpid))
        return wpid.value == self.pid

    def hwnd_of(self) -> int:
        if not self.hwnd:
            self._refresh()
        return self.hwnd
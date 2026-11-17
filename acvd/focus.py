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


def _exe_stem_of_pid(pid: int) -> str | None:
    """通过进程句柄查 exe 名（去 .exe 后缀，小写），拿不到返回 None。

    用 OpenProcess + QueryFullProcessImageNameW，**不走进程枚举** ——
    在「枚举列表被裁剪」的环境里（PyInstaller 产物实测只看到 19 个进程）
    这条链仍然可用。
    """
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        size = wt.DWORD(1024)
        buf = ctypes.create_unicode_buffer(1024)
        if not kernel32.QueryFullProcessImageNameW(h, 0, buf,
                                                   ctypes.byref(size)):
            return None
        base = buf.value.rsplit("\\", 1)[-1].lower()
        return base[:-4] if base.endswith(".exe") else base
    except Exception:
        return None
    finally:
        kernel32.CloseHandle(h)


def _find_pid_by_window_title(title_prefix: str) -> int | None:
    """枚举窗口，按标题开头匹配 → 反查 pid **并通过 exe 名二次确认**。

    ## 为什么这条链可靠

    全程不碰进程枚举，每一步都只用「窗口对象」：

        EnumWindows                枚举窗口（与进程列表完全不同的内核对象）
        GetWindowTextW             取窗口标题
        GetWindowThreadProcessId   窗口 → pid
        OpenProcess + QueryFullProcessImageNameW   pid → exe 名（校验）

    实测：进程枚举只看到 19 个进程的 EXE 环境里，EnumWindows 照样能枚举
    26 个窗口并正确拿到 xenia 的 pid。

    ## 为什么必须校验 exe 名（踩过的坑）

    只看窗口标题会误中。实测踩坑：桌面上有个**文件夹**叫 xenia-netplay，
    它的资源管理器窗口标题就是 `xenia-netplay`，pid 是 explorer.exe 的 ——
    标题匹配完全合法，但那是文件夹窗口，不是模拟器。

    所以拿到 pid 后再查一次它的 exe 名，必须同样以 key 开头才认。
    explorer 的 exe 名是 `explorer`，不匹配 `xenia`，被排除。

    ## 为什么用「开头匹配」而不是「包含匹配」

    当前环境里有个标题**包含** xenia 但不是它的窗口：

        Xenia 鼠标视角控制脚本方案 — DeepSeek Harness    ← 浏览器/Harness

    用 `in` 包含匹配会误中它（实测 pid 拿到 22692 而不是 10776）。
    开头匹配能排除：真目标标题是 `Xenia-edge` / `Xenia-canary-netplay`，
    干扰项是 `Xenia `（Xenia 后面跟空格），前缀不同。

    ## 为什么前缀参数不能写死

    Xenia 有几个不同分支，窗口标题各不相同：

        Xenia-edge (edge@16aa48a96 on Nov  7 2026)         ← Edge 分支
        Xenia-canary-netplay (netplay_canary_experimental@b5f6f6ed6 ...)  ← Netplay 分支

    所以调用方传 "xenia" 这个公共开头，本函数负责两件事：
    排除「xenia 后跟空格」的标题，以及校验 exe 名。

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
        low = buf.value.lower()
        # 第 1 关：标题必须以 key 开头，且紧跟字符不是空格 ——
        # Xenia 各分支用连字符连接（xenia-edge / xenia-canary-netplay），
        # 人写的标题通常是 "Xenia 鼠标视角..."（空格）。这一条把两者分开。
        if not (low.startswith(key) and len(low) > len(key)
                and not low[len(key)].isspace()):
            return True
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return True
        # 第 2 关：exe 名也要以 key 开头。
        # 排除「文件夹窗口标题恰好叫 xenia-netplay」这类干扰（见文档）。
        stem = _exe_stem_of_pid(pid.value)
        if stem is None or not stem.startswith(key):
            return True
        hit[0] = pid.value
        return False                    # 找到就停

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

    def __init__(self, exe_prefix: str = "xenia", refresh: float = 2.0):
        """追踪 Xenia 的主窗口。

        exe_prefix 用 "xenia" 这个公共前缀就够了 —— 各分支的 exe 名
        都以它开头（xenia_edge / xenia_canary / xenia_canary_netplay /
        xenia_canary_mousehook …）。不区分具体分支，也就不会因为
        换了一个构建版本就找不到进程。

        匹配走「前缀」而不是「全等」，见 _find_pid_by_prefix。
        """
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
            # 传 "xenia" 这个公共开头即可 —— _find_pid_by_window_title 内部
            # 会排除「xenia 后跟空格」的干扰窗口（比如浏览器标题里提到 Xenia），
            # 同时兼容各分支的标题（Xenia-edge / Xenia-canary-netplay）。
            self.pid = _find_pid_by_window_title("xenia")
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
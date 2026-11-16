"""
acvd/overlay.py — 准星覆盖层（稳定版，所有 Win32 调用都声明了 argtypes）

踩过的坑：
    1. 不声明 argtypes → 64 位句柄被截断成 32 位 int → OverflowError
    2. 窗口创建成功但 _paint 抛异常 → 什么都不画，且看不到任何报错
    3. 需要 UpdateWindow 触发首次 WM_PAINT

设计：
    * 独立线程跑窗口 + 消息循环
    * 主线程 update() 设置位置，窗口线程按需重绘
    * ColorKey 透明（黑色=透明）
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading
import time

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

if ctypes.sizeof(ctypes.c_void_p) != 8:
    raise RuntimeError("需要 64 位 Python")

# ---- 常量 -----------------------------------------------------------
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_POPUP = 0x80000000
WM_PAINT = 0x000F
WM_ERASEBKGND = 0x0014
WM_DESTROY = 0x0002
LWA_COLORKEY = 0x00000001
LWA_ALPHA = 0x00000002
NULL_BRUSH = 5          # GetStockObject(NULL_BRUSH)：Ellipse 只描边不填充
SW_SHOWNA = 8
HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040

# Raw Input
WM_INPUT = 0x00FF
RID_INPUT = 0x10000003
RIDEV_INPUTSINK = 0x00000100
HID_USAGE_PAGE_GENERIC = 0x01
HID_USAGE_GENERIC_MOUSE = 0x02


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [("usUsagePage", wt.USHORT), ("usUsage", wt.USHORT),
                ("dwFlags", wt.DWORD), ("hwndTarget", wt.HWND)]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [("dwType", wt.DWORD), ("dwSize", wt.DWORD),
                ("hDevice", wt.HANDLE), ("wParam", wt.WPARAM)]


class RAWMOUSE(ctypes.Structure):
    _fields_ = [("usFlags", wt.USHORT), ("usButtonFlags", wt.USHORT),
                ("usButtonData", wt.USHORT), ("ulRawButtons", wt.ULONG),
                ("lLastX", wt.LONG), ("lLastY", wt.LONG),
                ("ulExtraInformation", wt.ULONG)]


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [("hdc", wt.HDC), ("fErase", wt.BOOL), ("rcPaint", RECT),
                ("fRestore", wt.BOOL), ("fIncUpdate", wt.BOOL),
                ("rgbReserved", ctypes.c_byte * 32)]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class WNDCLASS(ctypes.Structure):
    _fields_ = [("style", wt.UINT),
                ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wt.HINSTANCE),
                ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE),
                ("hbrBackground", wt.HBRUSH),
                ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR)]


# ---- 全部声明 argtypes/restype --------------------------------------
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASS)]
user32.RegisterClassW.restype = wt.ATOM
user32.CreateWindowExW.argtypes = [
    wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wt.HWND, wt.HMENU, wt.HINSTANCE, ctypes.c_void_p]
user32.CreateWindowExW.restype = wt.HWND
user32.DestroyWindow.argtypes = [wt.HWND]
user32.DestroyWindow.restype = wt.BOOL
user32.SetLayeredWindowAttributes.argtypes = [wt.HWND, wt.DWORD,
                                              ctypes.c_ubyte, wt.DWORD]
user32.SetLayeredWindowAttributes.restype = wt.BOOL
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.ShowWindow.restype = wt.BOOL
user32.UpdateWindow.argtypes = [wt.HWND]
user32.UpdateWindow.restype = wt.BOOL
user32.InvalidateRect.argtypes = [wt.HWND, ctypes.c_void_p, wt.BOOL]
user32.InvalidateRect.restype = wt.BOOL
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wt.UINT]
user32.SetWindowPos.restype = wt.BOOL
user32.BeginPaint.argtypes = [wt.HWND, ctypes.POINTER(PAINTSTRUCT)]
user32.BeginPaint.restype = wt.HDC
user32.EndPaint.argtypes = [wt.HWND, ctypes.POINTER(PAINTSTRUCT)]
user32.EndPaint.restype = wt.BOOL
user32.FillRect.argtypes = [wt.HDC, ctypes.POINTER(RECT), wt.HBRUSH]
user32.FillRect.restype = ctypes.c_int
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.PeekMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT,
                                wt.UINT, wt.UINT]
user32.PeekMessageW.restype = wt.BOOL
user32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int

gdi32.CreateSolidBrush.argtypes = [wt.COLORREF]
gdi32.CreateSolidBrush.restype = wt.HBRUSH
gdi32.CreatePen.argtypes = [ctypes.c_int, ctypes.c_int, wt.COLORREF]
gdi32.CreatePen.restype = wt.HPEN
gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
gdi32.SelectObject.restype = wt.HGDIOBJ
gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
gdi32.DeleteObject.restype = wt.BOOL
gdi32.MoveToEx.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
gdi32.MoveToEx.restype = wt.BOOL
gdi32.LineTo.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int]
gdi32.LineTo.restype = wt.BOOL
gdi32.Ellipse.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int,
                          ctypes.c_int, ctypes.c_int]
gdi32.Ellipse.restype = wt.BOOL
gdi32.GetStockObject.argtypes = [ctypes.c_int]
gdi32.GetStockObject.restype = wt.HGDIOBJ

user32.RegisterRawInputDevices.argtypes = [ctypes.POINTER(RAWINPUTDEVICE),
                                           wt.UINT, wt.UINT]
user32.RegisterRawInputDevices.restype = wt.BOOL
user32.GetRawInputData.argtypes = [wt.HANDLE, wt.UINT, ctypes.c_void_p,
                                   ctypes.POINTER(wt.UINT), wt.UINT]
user32.GetRawInputData.restype = wt.UINT

kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
kernel32.GetModuleHandleW.restype = wt.HMODULE


class Crosshair:
    """可定位的准星覆盖窗口。

    画的不再是一个「像素级的点」，而是 **点 + 圈**：圈的半径就是闭环的死区
    （dead_px）。玩家用的是火控圈套人，不是拿点打人 —— 只要目标在圈里就
    不用再动，所以死区等于圈半径时，抖动消失而命中精度一点没丢。
    """

    SIZE = 560                 # 窗口要装得下最大的圈（半径可到 ~270）

    def __init__(self):
        self.hwnd = wt.HWND(0)
        self.x = 0
        self.y = 0
        self._stop = threading.Event()
        self._thread = None
        self._ready = threading.Event()
        self._paint_count = 0
        self._proc_ref = None
        # Raw Input 累积（不受屏幕边界限制，快速甩动不丢位移）
        self._raw_lock = threading.Lock()
        self._raw_dx = 0
        self._raw_dy = 0
        self.raw_ok = False
        self.raw_total = 0      # 累计收到的原始包位移绝对值，用于诊断
        self.overflow = False   # True = 目标超出视野，准星画成红色
        self.dim = False        # True = 映射暂停中，画成灰色（别再让人以为坏了）
        self.radius = 16.0      # 圈半径（像素）= 闭环死区，由 mapper 每帧同步
        self._alpha = 255       # 0~255，透明度；改它立刻重设分层属性

    def pop_delta(self):
        """取出并清零累积的原始鼠标位移。"""
        with self._raw_lock:
            d = (self._raw_dx, self._raw_dy)
            self._raw_dx = 0
            self._raw_dy = 0
            return d

    def start(self) -> bool:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(3.0)
        return bool(self.hwnd)

    def _run(self):
        self._proc_ref = WNDPROC(self._wndproc)
        wc = WNDCLASS()
        wc.style = 0
        wc.lpfnWndProc = self._proc_ref
        wc.lpszClassName = "acvd_crosshair_v3"
        wc.hInstance = kernel32.GetModuleHandleW(None)
        user32.RegisterClassW(ctypes.byref(wc))

        hinst = kernel32.GetModuleHandleW(None)
        hwnd = user32.CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_TOOLWINDOW,
            "acvd_crosshair_v3", "", WS_POPUP,
            100, 100, self.SIZE, self.SIZE, None, None, hinst, None)
        if not hwnd:
            self._ready.set()
            return
        self.hwnd = hwnd
        user32.SetLayeredWindowAttributes(hwnd, 0x00000000, self._alpha,
                                          LWA_COLORKEY | LWA_ALPHA)
        user32.ShowWindow(hwnd, SW_SHOWNA)
        user32.UpdateWindow(hwnd)

        # 这里**不能**再注册 Raw Input。
        # RegisterRawInputDevices 是「按进程按设备类」唯一的：后注册的会覆盖
        # 先注册的。准星窗口若注册 RIDEV_INPUTSINK，会把 RawMouse 的注册顶掉，
        # 导致位移累加器永远收不到 WM_INPUT，只能退回光标差值模式。
        # 准星本身不需要原始输入，交给 acvd/rawmouse.py 独占。
        self.raw_ok = False
        self._ready.set()

        msg = wt.MSG()
        last = 0.0
        while not self._stop.is_set():
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            now = time.perf_counter()
            if now - last > 0.03:
                user32.InvalidateRect(hwnd, None, False)
                user32.UpdateWindow(hwnd)
                last = now
            time.sleep(0.005)
        user32.DestroyWindow(hwnd)

    def set_alpha(self, a: float) -> None:
        """透明度 0~255。要与色键透明共存，必须 LWA_COLORKEY|LWA_ALPHA 同时开。"""
        a = int(max(0, min(255, a)))
        if a == self._alpha:
            return
        self._alpha = a
        if self.hwnd:
            try:
                user32.SetLayeredWindowAttributes(
                    self.hwnd, 0x00000000, a, LWA_COLORKEY | LWA_ALPHA)
            except Exception:
                pass

    def move_to(self, x: float, y: float):
        if not self.hwnd:
            return
        self.x, self.y = x, y
        user32.SetWindowPos(self.hwnd, wt.HWND(HWND_TOPMOST),
                            int(x) - self.SIZE // 2, int(y) - self.SIZE // 2,
                            self.SIZE, self.SIZE,
                            SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_INPUT:
            try:
                size = wt.UINT(0)
                hdr_sz = ctypes.sizeof(RAWINPUTHEADER)
                user32.GetRawInputData(wparam, RID_INPUT, None,
                                       ctypes.byref(size), hdr_sz)
                if size.value >= hdr_sz:
                    buf = ctypes.create_string_buffer(size.value)
                    got = user32.GetRawInputData(wparam, RID_INPUT, buf,
                                                 ctypes.byref(size), hdr_sz)
                    if got:
                        hdr = ctypes.cast(buf, ctypes.POINTER(RAWINPUTHEADER)).contents
                        if hdr.dwType == 0:      # RIM_TYPEMOUSE
                            # RAWMOUSE 在 header 之后，需要按偏移取
                            addr = ctypes.addressof(buf) + hdr_sz
                            mouse = ctypes.cast(addr, ctypes.POINTER(RAWMOUSE)).contents
                            if mouse.lLastX or mouse.lLastY:
                                with self._raw_lock:
                                    self._raw_dx += mouse.lLastX
                                    self._raw_dy += mouse.lLastY
                                    self.raw_total += abs(mouse.lLastX) + abs(mouse.lLastY)
            except Exception:
                pass
            return 0
        if msg == WM_PAINT:
            ps = PAINTSTRUCT()
            hdc = user32.BeginPaint(hwnd, ctypes.byref(ps))
            try:
                self._paint(hdc)
            except Exception:
                pass
            finally:
                user32.EndPaint(hwnd, ctypes.byref(ps))
            return 0
        if msg == WM_ERASEBKGND:
            return 1
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _paint(self, hdc):
        s = self.SIZE
        # 背景黑色 = ColorKey = 透明
        rc = RECT(0, 0, s, s)
        bg = gdi32.CreateSolidBrush(0x00000000)
        user32.FillRect(hdc, ctypes.byref(rc), bg)
        gdi32.DeleteObject(bg)

        c = s // 2
        if self.dim:
            color = 0x00808080          # 灰：映射暂停，准星不再跟随误差
        elif self.overflow:
            color = 0x000000FF          # 红：目标超出视野
        else:
            color = 0x0000FF00          # 绿：正常

        # 圈：半径 = 死区。目标进圈就不推杆，所以「圈内即到位」。
        # ⚠ Ellipse 会用**当前画刷**填充内部，而 BeginPaint 给的 DC 默认是
        # 白色画刷 —— 不换成 NULL_BRUSH 就会在圈里糊一块白，挡住游戏画面。
        r = int(max(2, min(self.radius, c - 6)))
        pen = gdi32.CreatePen(0, 1, color)
        old = gdi32.SelectObject(hdc, pen)
        oldb = gdi32.SelectObject(hdc, gdi32.GetStockObject(NULL_BRUSH))
        gdi32.Ellipse(hdc, c - r, c - r, c + r, c + r)
        gdi32.SelectObject(hdc, oldb)
        gdi32.SelectObject(hdc, old)
        gdi32.DeleteObject(pen)

        pen = gdi32.CreatePen(0, 2, color)
        old = gdi32.SelectObject(hdc, pen)
        gdi32.MoveToEx(hdc, c - 11, c, None)
        gdi32.LineTo(hdc, c - 3, c)
        gdi32.MoveToEx(hdc, c + 3, c, None)
        gdi32.LineTo(hdc, c + 11, c)
        gdi32.MoveToEx(hdc, c, c - 11, None)
        gdi32.LineTo(hdc, c, c - 3)
        gdi32.MoveToEx(hdc, c, c + 3, None)
        gdi32.LineTo(hdc, c, c + 11)
        gdi32.SelectObject(hdc, old)
        gdi32.DeleteObject(pen)
        self._paint_count += 1

    @property
    def paint_count(self):
        return self._paint_count

    def stop(self):
        self._stop.set()
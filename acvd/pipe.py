"""
pipe.py — 极简命名管道双端（Windows，ctypes）

服务端：每行是一条指令，返回一行结果。
客户端：CLI 一次发一行，打印返回。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading

k32 = ctypes.WinDLL("kernel32", use_last_error=True)

PIPE_ACCESS_DUPLEX = 0x00000003
PIPE_TYPE_BYTE = 0x00000000
PIPE_READMODE_BYTE = 0x00000000
PIPE_WAIT = 0x00000000
PIPE_UNLIMITED_INSTANCES = 255
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

k32.CreateNamedPipeW.restype = wt.HANDLE
k32.CreateNamedPipeW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD,
                                 wt.DWORD, wt.DWORD, ctypes.c_void_p]
k32.ConnectNamedPipe.argtypes = [wt.HANDLE, ctypes.c_void_p]
k32.DisconnectNamedPipe.argtypes = [wt.HANDLE]
k32.ReadFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
k32.WriteFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
k32.CreateFileW.restype = wt.HANDLE
k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.HANDLE]
k32.WaitNamedPipeW.argtypes = [wt.LPCWSTR, wt.DWORD]


def _pipe_path(name: str) -> str:
    return "\\\\.\\pipe\\" + name


class PipeServer:
    def __init__(self, name: str, handler, encoding: str = "utf-8"):
        self.path = _pipe_path(name)
        self.handler = handler
        self.encoding = encoding
        self.on_quit = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        # 用一个空连接唤醒阻塞的 ConnectNamedPipe（用自身名字，不写死）
        try:
            PipeClient(self.path.rsplit("\\", 1)[-1], timeout_ms=200).send("__stop__")
        except Exception:
            pass

    def _run(self):
        while not self._stop.is_set():
            h = k32.CreateNamedPipeW(self.path, PIPE_ACCESS_DUPLEX,
                                     PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
                                     PIPE_UNLIMITED_INSTANCES, 4096, 4096, 200, None)
            if not h or h == INVALID_HANDLE_VALUE:
                self._stop.wait(0.2)
                continue
            try:
                k32.ConnectNamedPipe(h, None)
            except Exception:
                pass
            # 每条连接交给独立线程，避免客户端排队时连不上
            threading.Thread(target=self._serve, args=(h,), daemon=True).start()

    def _serve(self, h):
        try:
            while True:
                line = _read_line(h, self.encoding)
                if line is None:
                    break
                line = line.strip()
                if not line:
                    continue
                if line == "__stop__":
                    self._stop.set()
                    return
                if self.on_quit and self.on_quit(line):
                    _write(h, b"bye\n")
                    self._stop.set()
                    return
                reply = self.handler(line)
                _write(h, (str(reply) + "\n").encode(self.encoding))
        finally:
            k32.DisconnectNamedPipe(h)
            k32.CloseHandle(h)


class PipeClient:
    def __init__(self, name: str, timeout_ms: int = 3000):
        self.path = _pipe_path(name)
        self.timeout = timeout_ms

    def send(self, line: str) -> str:
        k32.WaitNamedPipeW(self.path, self.timeout)
        h = k32.CreateFileW(self.path, 0xC0000000, 0, None, 3, 0, None)
        if not h or h == INVALID_HANDLE_VALUE:
            raise OSError(f"无法连接 {self.path}, err={ctypes.get_last_error()}")
        try:
            _write(h, (line + "\n").encode("utf-8"))
            reply = _read_line(h, "utf-8")
            return reply or ""
        finally:
            k32.CloseHandle(h)


def _read_line(h, enc: str) -> str | None:
    buf = bytearray()
    n = wt.DWORD(0)
    ch = ctypes.create_string_buffer(1)
    while True:
        ok = k32.ReadFile(h, ch, 1, ctypes.byref(n), None)
        if not ok or n.value == 0:
            return None
        if ch.raw[0:1] == b"\n":
            return buf.decode(enc, "replace")
        buf += ch.raw[0:1]
        if len(buf) > 8192:
            return None


def _write(h, data: bytes):
    n = wt.DWORD(0)
    k32.WriteFile(h, data, len(data), ctypes.byref(n), None)
"""
winmem.py — 64-bit Windows 进程内存访问（纯 ctypes，无第三方依赖）

设计要点：
  * 一次 VirtualQueryEx 枚举所有区域，缓存起来；AOB 扫描时按块读，避免逐字节 syscall。
  * 读失败不抛异常，返回 None / 短字节串，由调用方决定语义。
  * 所有地址用 Python int，打印时统一 0x%X。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
from dataclasses import dataclass
from typing import Iterator

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_VM_OPERATION = 0x0008

MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
MEM_FREE = 0x10000

PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01
PAGE_READONLY = 0x02
PAGE_READWRITE = 0x04
PAGE_WRITECOPY = 0x08
PAGE_EXECUTE = 0x10
PAGE_EXECUTE_READ = 0x20
PAGE_EXECUTE_READWRITE = 0x40
PAGE_EXECUTE_WRITECOPY = 0x80

MEM_PRIVATE = 0x20000
MEM_MAPPED = 0x40000
MEM_IMAGE = 0x1000000


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wt.DWORD),
        ("__alignment1", wt.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wt.DWORD),
        ("Protect", wt.DWORD),
        ("Type", wt.DWORD),
        ("__alignment2", wt.DWORD),
    ]


kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
kernel32.GetExitCodeProcess.restype = wt.BOOL
kernel32.VirtualQueryEx.argtypes = [
    wt.HANDLE, ctypes.c_void_p, ctypes.POINTER(MEMORY_BASIC_INFORMATION), ctypes.c_size_t
]
kernel32.VirtualQueryEx.restype = ctypes.c_size_t
kernel32.ReadProcessMemory.argtypes = [
    wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)
]
kernel32.ReadProcessMemory.restype = wt.BOOL
kernel32.WriteProcessMemory.argtypes = [
    wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)
]
kernel32.WriteProcessMemory.restype = wt.BOOL

assert ctypes.sizeof(MEMORY_BASIC_INFORMATION) == 48, "MBI layout wrong"


@dataclass(frozen=True)
class Region:
    base: int
    size: int
    state: int
    protect: int
    type: int

    @property
    def end(self) -> int:
        return self.base + self.size

    @property
    def readable(self) -> bool:
        if self.state != MEM_COMMIT:
            return False
        if self.protect & PAGE_GUARD or self.protect & PAGE_NOACCESS:
            return False
        return bool(self.protect & (PAGE_READONLY | PAGE_READWRITE | PAGE_WRITECOPY
                                    | PAGE_EXECUTE_READ | PAGE_EXECUTE_READWRITE
                                    | PAGE_EXECUTE_WRITECOPY | PAGE_EXECUTE))

    @property
    def writable(self) -> bool:
        return self.readable and bool(self.protect & (PAGE_READWRITE | PAGE_WRITECOPY
                                                      | PAGE_EXECUTE_READWRITE
                                                      | PAGE_EXECUTE_WRITECOPY))

    def __str__(self) -> str:
        kinds = {MEM_PRIVATE: "PRIVATE", MEM_IMAGE: "IMAGE", MEM_MAPPED: "MAPPED"}
        return (f"0x{self.base:012X}-0x{self.end:012X} {self.size/2**20:8.1f}MB "
                f"{kinds.get(self.type, '?'):7} prot=0x{self.protect:X}")


class Process:
    """按名字或 PID 打开一个进程。"""

    def __init__(self, pid: int, handle: int):
        self.pid = pid
        self._h = handle
        self._regions: list[Region] | None = None

    # ---- life cycle -------------------------------------------------
    @classmethod
    def attach(cls, name_or_pid: str | int,
               access: int = None) -> "Process":
        if access is None:
            access = PROCESS_QUERY_INFORMATION | PROCESS_VM_READ | PROCESS_VM_WRITE | PROCESS_VM_OPERATION
        pid = _resolve_pid(name_or_pid)
        if pid is None:
            from .errors import ProcessNotFound
            raise ProcessNotFound(f"进程不存在: {name_or_pid}")
        h = kernel32.OpenProcess(access, False, pid)
        if not h:
            err = ctypes.get_last_error()
            from .errors import AccessDenied
            raise AccessDenied(
                f"OpenProcess(pid={pid}) 失败, GetLastError={err} "
                f"({ctypes.FormatError(err)}) — 需要以管理员身份运行")
        return cls(pid, h)

    def close(self) -> None:
        if self._h:
            kernel32.CloseHandle(self._h)
            self._h = None

    def alive(self) -> bool:
        """进程是否还活着（句柄有效且未退出）。

        用于自动重连：Xenia 重启后旧句柄失效，ReadProcessMemory 全部返回
        失败，表现为「相机地址突然读不到」。有这个方法才能区分
        「换关卡了」和「模拟器关了」。
        """
        if not self._h:
            return False
        code = wt.DWORD(0)
        if not kernel32.GetExitCodeProcess(self._h, ctypes.byref(code)):
            return False
        return code.value == 259          # STILL_ACTIVE

    def __enter__(self): return self
    def __exit__(self, *a): self.close()

    # ---- memory -----------------------------------------------------
    def regions(self, refresh: bool = False) -> list[Region]:
        if self._regions is None or refresh:
            self._regions = list(self._enumerate())
        return self._regions

    def _enumerate(self) -> Iterator[Region]:
        addr = 0
        mbi = MEMORY_BASIC_INFORMATION()
        while True:
            got = kernel32.VirtualQueryEx(self._h, ctypes.c_void_p(addr),
                                          ctypes.byref(mbi), ctypes.sizeof(mbi))
            if not got:
                return
            size = int(mbi.RegionSize)
            if size <= 0:
                return
            yield Region(addr, size, mbi.State, mbi.Protect, mbi.Type)
            nxt = addr + size
            if nxt <= addr:
                return
            addr = nxt

    def read(self, addr: int, size: int) -> bytes | None:
        buf = ctypes.create_string_buffer(size)
        n = ctypes.c_size_t(0)
        ok = kernel32.ReadProcessMemory(self._h, ctypes.c_void_p(addr), buf, size,
                                        ctypes.byref(n))
        if not ok:
            return None
        return buf.raw[: n.value]

    def read_exact(self, addr: int, size: int) -> bytes:
        data = self.read(addr, size)
        if data is None or len(data) != size:
            from .errors import ReadFailed
            raise ReadFailed(f"读 0x{addr:X} 失败 (len={size}) err={ctypes.get_last_error()}")
        return data

    def write(self, addr: int, data: bytes) -> int:
        n = ctypes.c_size_t(0)
        ok = kernel32.WriteProcessMemory(self._h, ctypes.c_void_p(addr), data, len(data),
                                         ctypes.byref(n))
        if not ok:
            from .errors import WriteFailed
            raise WriteFailed(f"写 0x{addr:X} 失败 err={ctypes.get_last_error()}")
        return n.value

    # ---- typed helpers ----------------------------------------------
    def u8(self, a):   return int.from_bytes(self.read_exact(a, 1), "little")
    def u16(self, a):  return int.from_bytes(self.read_exact(a, 2), "little")
    def u32(self, a):  return int.from_bytes(self.read_exact(a, 4), "little")
    def u64(self, a):  return int.from_bytes(self.read_exact(a, 8), "little")
    def i32(self, a):  return int.from_bytes(self.read_exact(a, 4), "little", signed=True)

    def f32(self, a) -> float:
        import struct
        return struct.unpack("<f", self.read_exact(a, 4))[0]

    def f64(self, a) -> float:
        return float(ctypes.c_double.from_buffer_copy(self.read_exact(a, 8)).value)

    def ptr(self, a) -> int:
        return self.u64(a)


def _resolve_pid(name_or_pid: str | int) -> int | None:
    if isinstance(name_or_pid, int):
        return name_or_pid
    name = name_or_pid[:-4] if name_or_pid.lower().endswith(".exe") else name_or_pid
    found = _find_pid_by_name(name.lower())
    if found:
        return found[0]
    if name_or_pid.isdigit():
        return int(name_or_pid)
    return None


def _find_pid_by_name(name_lower: str) -> list[int]:
    """用 Toolhelp32 快照找进程，避免依赖 psutil。"""
    TH32CS_SNAPPROCESS = 0x00000002

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wt.DWORD),
            ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
            ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
            ("szExeFile", wt.CHAR * 260),
        ]

    kernel32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wt.HANDLE
    kernel32.Process32First.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32)]
    kernel32.Process32Next.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32)]

    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == wt.HANDLE(-1).value or not snap:
        return []
    out: list[int] = []
    try:
        pe = PROCESSENTRY32()
        pe.dwSize = ctypes.sizeof(pe)
        ok = kernel32.Process32First(snap, ctypes.byref(pe))
        while ok:
            exe = pe.szExeFile.decode("mbcs", "replace")
            if exe.lower() == name_lower or exe.lower() == name_lower + ".exe":
                out.append(pe.th32ProcessID)
            ok = kernel32.Process32Next(snap, ctypes.byref(pe))
    finally:
        kernel32.CloseHandle(snap)
    return out


def list_processes() -> list[tuple[int, str]]:
    TH32CS_SNAPPROCESS = 0x00000002

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wt.DWORD),
            ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
            ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
            ("szExeFile", wt.CHAR * 260),
        ]

    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    out = []
    pe = PROCESSENTRY32()
    pe.dwSize = ctypes.sizeof(pe)
    ok = kernel32.Process32First(snap, ctypes.byref(pe))
    while ok:
        out.append((pe.th32ProcessID, pe.szExeFile.decode("mbcs", "replace")))
        ok = kernel32.Process32Next(snap, ctypes.byref(pe))
    kernel32.CloseHandle(snap)
    return out
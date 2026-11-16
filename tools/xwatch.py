"""
tools/xwatch.py — 硬件数据断点（xwatch.c 的纯 Python 版，零编译）

## 它做什么

在目标进程的某个地址上装一个**硬件数据断点**（DR0），然后统计
「谁访问了它」——按触发指令的 Rip 分类计数：

    python tools/xwatch.py <pid> <地址> [长度 1|2|4|8] [w|rw] [毫秒]

    长度默认 4，模式默认 rw，时长默认 5000

输出：

    hit 0x7FF6...  1843      某条指令访问了 1843 次
    total 1843  threads 42  events 1851

## 为什么不用 xwatch.c

原版要 C 编译器。这个版本用 ctypes 直接调 Debug API，行为与原版逐条对应：

  · DebugActiveProcess 附加，DebugSetProcessKillOnExit(FALSE) —— 退出不杀进程
  · 首个 EXCEPTION_BREAKPOINT = 附加断点，此刻给**所有线程**设 DR0/DR7
  · EXCEPTION_SINGLE_STEP + Dr6 bit0 = 我们的断点，记录 Rip
  · 到时 DebugBreakProcess 让所有线程挂起 → 清 DR0 → drain 500ms → 分离
  · 非单步异常一律 DBG_EXCEPTION_NOT_HANDLED，交还进程自己处理

## 注意

x64 的 CONTEXT 要求 16 字节对齐，所以这里不用 ctypes 结构体，
而是手工分配对齐内存 + 按固定偏移读写（偏移见常量表）。

附加期间进程会略微变慢（每次命中两次上下文切换）。时长别开太长。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys
import time

k32 = ctypes.WinDLL("kernel32", use_last_error=True)

# 调试事件（顺序来自 WinNT.h，不能凭记忆写）
EXCEPTION_DEBUG_EVENT = 1
CREATE_THREAD_DEBUG_EVENT = 2
CREATE_PROCESS_DEBUG_EVENT = 3
EXIT_THREAD_DEBUG_EVENT = 4
EXIT_PROCESS_DEBUG_EVENT = 5
LOAD_DLL_DEBUG_EVENT = 6

EXCEPTION_BREAKPOINT = 0x80000003
EXCEPTION_SINGLE_STEP = 0x80000004

DBG_CONTINUE = 0x00010002
DBG_EXCEPTION_NOT_HANDLED = 0x80010001

# CONTEXT 偏移（x64 WinNT.h）
OFF_CONTEXT_FLAGS = 0x30
OFF_DR0 = 0x48
OFF_DR6 = 0x68
OFF_DR7 = 0x70
OFF_RIP = 0xF8
CONTEXT_SIZE = 1232

CONTEXT_AMD64 = 0x00100000
CONTEXT_CONTROL = CONTEXT_AMD64 | 0x1
CONTEXT_DEBUG_REGISTERS = CONTEXT_AMD64 | 0x10


class EXCEPTION_RECORD(ctypes.Structure):
    _fields_ = [("ExceptionCode", wt.DWORD),
                ("ExceptionFlags", wt.DWORD),
                ("ExceptionRecord", ctypes.c_void_p),
                ("ExceptionAddress", ctypes.c_void_p),
                ("NumberParameters", wt.DWORD),
                ("ExceptionInformation", ctypes.c_void_p * 15)]


class EXCEPTION_DEBUG_INFO(ctypes.Structure):
    _fields_ = [("ExceptionRecord", EXCEPTION_RECORD),
                ("dwFirstChance", wt.DWORD)]


class CREATE_THREAD_DEBUG_INFO(ctypes.Structure):
    _fields_ = [("hThread", ctypes.c_void_p),
                ("lpThreadLocalBase", ctypes.c_void_p),
                ("lpStartAddress", ctypes.c_void_p)]


class CREATE_PROCESS_DEBUG_INFO(ctypes.Structure):
    _fields_ = [("hFile", ctypes.c_void_p),
                ("hProcess", ctypes.c_void_p),
                ("hThread", ctypes.c_void_p),
                ("lpBaseOfImage", ctypes.c_void_p),
                ("dwDebugInfoFileOffset", wt.DWORD),
                ("nDebugInfoSize", wt.DWORD),
                ("lpThreadLocalBase", ctypes.c_void_p),
                ("lpStartAddress", ctypes.c_void_p),
                ("lpImageName", ctypes.c_void_p),
                ("fUnicode", wt.WORD)]


class _EvUnion(ctypes.Union):
    _fields_ = [("Exception", EXCEPTION_DEBUG_INFO),
                ("CreateThread", CREATE_THREAD_DEBUG_INFO),
                ("CreateProcess", CREATE_PROCESS_DEBUG_INFO),
                ("LoadDll", ctypes.c_byte * 24),
                ("pad", ctypes.c_byte * 160)]


class DEBUG_EVENT(ctypes.Structure):
    _fields_ = [("dwDebugEventCode", wt.DWORD),
                ("dwProcessId", wt.DWORD),
                ("dwThreadId", wt.DWORD),
                ("u", _EvUnion)]


k32.DebugActiveProcess.argtypes = [wt.DWORD]
k32.DebugActiveProcess.restype = wt.BOOL
k32.DebugActiveProcessStop.argtypes = [wt.DWORD]
k32.DebugSetProcessKillOnExit.argtypes = [wt.BOOL]
k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.OpenProcess.restype = wt.HANDLE
k32.DebugBreakProcess.argtypes = [wt.HANDLE]
k32.WaitForDebugEvent.argtypes = [ctypes.POINTER(DEBUG_EVENT), wt.DWORD]
k32.ContinueDebugEvent.argtypes = [wt.DWORD, wt.DWORD, wt.DWORD]
k32.GetThreadContext.argtypes = [wt.HANDLE, ctypes.c_void_p]
k32.SetThreadContext.argtypes = [wt.HANDLE, ctypes.c_void_p]
k32.OpenThread.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.OpenThread.restype = wt.HANDLE
k32.CloseHandle.argtypes = [wt.HANDLE]


class Ctx:
    """16 字节对齐的 CONTEXT 缓冲（GetThreadContext 有对齐要求）。"""

    def __init__(self):
        self._raw = ctypes.create_string_buffer(CONTEXT_SIZE + 16)
        base = ctypes.addressof(self._raw)
        self._off = (16 - base % 16) % 16
        self.ptr = ctypes.c_void_p(base + self._off)

    def u64(self, off: int) -> int:
        return ctypes.c_uint64.from_buffer(self._raw, self._off + off).value

    def set_u64(self, off: int, v: int) -> None:
        ctypes.c_uint64.from_buffer(self._raw, self._off + off).value = v

    def set_flags(self, v: int) -> None:
        ctypes.c_uint32.from_buffer(self._raw, self._off + OFF_CONTEXT_FLAGS).value = v

    def clear(self) -> None:
        ctypes.memset(self.ptr, 0, CONTEXT_SIZE)


def arm(h, addr: int, dr7: int, ctx: Ctx) -> bool:
    ctx.clear()
    ctx.set_flags(CONTEXT_DEBUG_REGISTERS)
    if not k32.GetThreadContext(h, ctx.ptr):
        return False
    ctx.set_u64(OFF_DR0, addr)
    ctx.set_u64(OFF_DR7, (ctx.u64(OFF_DR7) & ~0xF0003) | dr7)
    ctx.set_u64(OFF_DR6, 0)
    ctx.set_flags(CONTEXT_DEBUG_REGISTERS)
    return bool(k32.SetThreadContext(h, ctx.ptr))


def disarm(h, ctx: Ctx) -> None:
    ctx.clear()
    ctx.set_flags(CONTEXT_DEBUG_REGISTERS)
    if not k32.GetThreadContext(h, ctx.ptr):
        return
    ctx.set_u64(OFF_DR0, 0)
    ctx.set_u64(OFF_DR7, ctx.u64(OFF_DR7) & ~0xF0003)
    ctx.set_u64(OFF_DR6, 0)
    ctx.set_flags(CONTEXT_DEBUG_REGISTERS)
    k32.SetThreadContext(h, ctx.ptr)


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    pid = int(sys.argv[1], 0)
    addr = int(sys.argv[2], 0)
    ln = int(sys.argv[3]) if len(sys.argv) > 3 else 4
    rw = (sys.argv[4] if len(sys.argv) > 4 else "rw").lower() == "rw"
    ms = int(sys.argv[5]) if len(sys.argv) > 5 else 5000

    if ln not in (1, 2, 4, 8) or addr % ln:
        print("长度必须是 1/2/4/8，且地址按长度对齐")
        return 2
    lenbits = {1: 0, 2: 1, 4: 3, 8: 2}[ln]
    dr7 = 1 | ((3 if rw else 1) << 16) | (lenbits << 18)

    if not k32.DebugActiveProcess(pid):
        err = ctypes.get_last_error()
        print(f"DebugActiveProcess 失败 err={err}")
        print("  · 5 = 拒绝访问（Xenia 可能是管理员权限启动的，需同样提权）")
        return 1
    k32.DebugSetProcessKillOnExit(False)
    hproc = k32.OpenProcess(0x1F0FFF, False, pid)

    threads: dict[int, int] = {}
    ctx = Ctx()
    hits: dict[int, int] = {}
    total = 0
    armed = False
    t_start = time.perf_counter()
    stopping = False
    cleared = False
    drain_until = 0.0
    ev = DEBUG_EVENT()
    events = 0

    print(f"附加 pid={pid}  监视 0x{addr:X} len={ln} "
          f"{'rw' if rw else 'w'}  {ms}ms", flush=True)

    while True:
        now = time.perf_counter()
        if not stopping and armed and (now - t_start) * 1000 >= ms:
            stopping = True
            if hproc:
                k32.DebugBreakProcess(hproc)
        if cleared and now >= drain_until:
            break
        if not k32.WaitForDebugEvent(ctypes.byref(ev), 50):
            continue
        events += 1
        code = ev.dwDebugEventCode
        cont = DBG_CONTINUE
        if code == CREATE_PROCESS_DEBUG_EVENT:
            # 附加时主线程从这里来；漏了它就会少一个线程（通常是渲染线程）
            threads[ev.dwThreadId] = ev.u.CreateProcess.hThread
            if ev.u.CreateProcess.hFile:
                k32.CloseHandle(ev.u.CreateProcess.hFile)
        elif code == CREATE_THREAD_DEBUG_EVENT:
            threads[ev.dwThreadId] = ev.u.CreateThread.hThread
            if armed and not stopping and not cleared:
                arm(ev.u.CreateThread.hThread, addr, dr7, ctx)
        elif code == EXIT_THREAD_DEBUG_EVENT:
            threads.pop(ev.dwThreadId, None)
        elif code == EXIT_PROCESS_DEBUG_EVENT:
            break
        elif code == EXCEPTION_DEBUG_EVENT:
            ec = ev.u.Exception.ExceptionRecord.ExceptionCode
            if ec == EXCEPTION_BREAKPOINT and not armed:
                for h in list(threads.values()):
                    arm(h, addr, dr7, ctx)
                armed = True
                t_start = time.perf_counter()
                print(f"已给 {len(threads)} 个线程装上断点", flush=True)
            elif ec == EXCEPTION_BREAKPOINT and stopping and not cleared:
                for h in list(threads.values()):
                    disarm(h, ctx)
                cleared = True
                drain_until = time.perf_counter() + 0.5
            elif ec == EXCEPTION_SINGLE_STEP:
                h = threads.get(ev.dwThreadId)
                if h:
                    ctx.clear()
                    ctx.set_flags(CONTEXT_CONTROL | CONTEXT_DEBUG_REGISTERS)
                    if k32.GetThreadContext(h, ctx.ptr):
                        dr6 = ctx.u64(OFF_DR6)
                        if dr6 & 1 and not cleared:
                            rip = ctx.u64(OFF_RIP)
                            hits[rip] = hits.get(rip, 0) + 1
                            total += 1
                        ctx.set_u64(OFF_DR6, 0)
                        if cleared:
                            ctx.set_u64(OFF_DR0, 0)
                            ctx.set_u64(OFF_DR7,
                                        ctx.u64(OFF_DR7) & ~0xF0003)
                        ctx.set_flags(CONTEXT_DEBUG_REGISTERS)
                        k32.SetThreadContext(h, ctx.ptr)
            elif ec == EXCEPTION_BREAKPOINT:
                pass
            else:
                cont = DBG_EXCEPTION_NOT_HANDLED
        k32.ContinueDebugEvent(ev.dwProcessId, ev.dwThreadId, cont)

    k32.DebugActiveProcessStop(pid)

    print()
    for rip, n in sorted(hits.items(), key=lambda kv: -kv[1]):
        print(f"hit 0x{rip:X}  {n}")
    print(f"total {total}  threads {len(threads)}  events {events}")
    if not hits:
        print("\n没有命中。可能：")
        print("  · 这个地址在这段时间没被访问")
        print("  · 断点装在错误的地址（注意要 host 地址，不是 guest）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

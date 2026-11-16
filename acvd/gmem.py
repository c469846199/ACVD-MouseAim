"""acvd/gmem.py — 按 guest 地址读写 Xenia 内存（不再用宿主坐标）

## 为什么

    以前所有地址都是宿主地址（0x1405EA770 这类），它们是
        host = guest_base + guest_addr
    的结果。guest_base 由 Xenia 在自己的地址空间里选，理论上会变；而 guest 侧
    的地址（0x405EA770）才是游戏真正看到的、跨进程重启也稳定的坐标。

    实测（2026-11-10，pid 24320）：
        guest_base = 0x1_0000_0000
        校验：base + 0x82000000 = 0x182000000 处是 MZ/PE（360 标题映像固定装在这里）
        相机 host 0x1405EA770 → guest 0x405EA770（guest 64K 虚拟页堆）

## base 怎么找

    xenia-mcp 的做法：游戏映像固定加载在 guest 0x82000000，所以对候选 base
    检查 base + 0x82000000 处是不是可执行映像头（MZ + PE\0\0）。
    已知常见值 0x1_0000_0000，找不到就枚举区域反推。

## 用法

    gm = GuestMem.attach("xenia")
    gm.u32(0x405EA770)            # 读 4 字节
    gm.read(0x405EA770, 0x30)     # 读任意长度
    gm.host(0x405EA770)           # 换算成宿主地址（调试用）
"""
from __future__ import annotations

import ctypes
import struct
from ctypes import wintypes

from .focus import _find_pid_by_prefix
from .winmem import Process

k32 = ctypes.windll.kernel32

MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01


class MBI64(ctypes.Structure):
    """MEMORY_BASIC_INFORMATION 的 64 位布局（48 字节）。

    注意：不要用 winmem.regions()，它的结构体定义是错的（会给出 128TB 这种
    明显不合理的 RegionSize）。这里自己定义，已对真实进程验证。
    """

    _fields_ = [("BaseAddress", ctypes.c_ulonglong),
                ("AllocationBase", ctypes.c_ulonglong),
                ("AllocationProtect", wintypes.DWORD), ("_a1", wintypes.DWORD),
                ("RegionSize", ctypes.c_ulonglong),
                ("State", wintypes.DWORD), ("Protect", wintypes.DWORD),
                ("Type", wintypes.DWORD), ("_a2", wintypes.DWORD)]


def _regions(handle: int, limit: int = 0x7FFFFFFFFFFF):
    """枚举区域，产出 (base, size, state, protect, type)。"""
    addr = 0
    while addr < limit:
        m = MBI64()
        if not k32.VirtualQueryEx(ctypes.c_void_p(handle),
                                  ctypes.c_void_p(addr),
                                  ctypes.byref(m), ctypes.sizeof(m)):
            return
        yield (m.BaseAddress, m.RegionSize, m.State, m.Protect,
               m.AllocationBase)
        nxt = m.BaseAddress + m.RegionSize
        if nxt <= addr:
            return
        addr = nxt


class GuestMem:
    """guest 地址视图。所有 offset 都是游戏看到的地址。"""

    GUEST_IMAGE = 0x82000000        # 360 标题映像固定加载点
    KNOWN_BASE = 0x1_0000_0000      # xenia-mcp 记录、本项目实测一致

    def __init__(self, proc: Process, base: int):
        self.proc = proc
        self.base = base
        self._h = proc._h            # 复用同一条句柄

    # ---- 生命周期 ----------------------------------------------------
    @classmethod
    def attach(cls, prefix: str = "xenia") -> "GuestMem":
        pid = _find_pid_by_prefix(prefix)
        if not pid:
            raise RuntimeError(f"找不到 {prefix}* 进程")
        proc = Process.attach(pid)
        base = cls.find_base(proc)
        if base is None:
            proc.close()
            raise RuntimeError("找不到 guest 内存基址（base + 0x82000000 处无映像头）")
        return cls(proc, base)

    @classmethod
    def find_base(cls, proc: Process) -> int | None:
        """反推 guest 内存基址。先试已知值，再按区域枚举。"""
        def looks_like_image(b: int) -> bool:
            head = proc.read(b + cls.GUEST_IMAGE, 0x40)
            if not head or head[:2] != b"MZ":
                return False
            e = struct.unpack("<I", head[0x3C:0x40])[0]
            if e >= 0x1000:
                return False
            return proc.read(b + cls.GUEST_IMAGE + e, 4) == b"PE\x00\x00"

        if looks_like_image(cls.KNOWN_BASE):
            return cls.KNOWN_BASE

        # 兜底：区域基址减去映像偏移就是候选 base
        seen = set()
        for base, size, state, protect, alloc in _regions(proc._h):
            for cand in (base - cls.GUEST_IMAGE, alloc - cls.GUEST_IMAGE,
                         alloc - 0x40000000):
                if cand < 0 or cand in seen:
                    continue
                seen.add(cand)
                if looks_like_image(cand):
                    return cand
        return None

    # ---- 读 ----------------------------------------------------------
    def host(self, guest: int) -> int:
        return self.base + guest

    def read(self, guest: int, size: int) -> bytes | None:
        return self.proc.read(self.base + guest, size)

    def u32(self, guest: int) -> int | None:
        b = self.read(guest, 4)
        return struct.unpack("<I", b)[0] if b and len(b) == 4 else None

    def f32(self, guest: int) -> float | None:
        b = self.read(guest, 4)
        return struct.unpack(">f", b)[0] if b and len(b) == 4 else None

    def close(self) -> None:
        """只关自己手上的引用，不关共享句柄。"""
        try:
            self.proc.close()
        except Exception:
            pass


def selftest() -> None:
    """自检：base 正确、guest 读与宿主读一致。"""
    gm = GuestMem.attach("xenia")
    print(f"guest base = 0x{gm.base:012X}")
    head = gm.read(GuestMem.GUEST_IMAGE, 2)
    print(f"guest 0x82000000 首字节 = {head.hex() if head else '读不到'}（应为 4d5a）")
    for g in (0x405EA770, 0x4105DCC0):
        via_guest = gm.read(g, 4)
        via_host = gm.proc.read(gm.host(g), 4)
        same = via_guest == via_host
        print(f"  guest 0x{g:08X} (host 0x{gm.host(g):012X}): "
              f"{'一致' if same else '不一致'}  {via_guest.hex() if via_guest else '-'}")
    gm.close()


if __name__ == "__main__":
    selftest()

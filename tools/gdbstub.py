"""
tools/gdbstub.py — xenia GDBStub 客户端（纯 socket，不需要装 gdb）

## 为什么用它

前面用硬件断点抓到的是 **JIT 生成的 x64 代码**，每条 guest 内存访问都被
展开成「边界检查 + 字节交换 + load/store」，rip 永远落在基础设施里，
读不出游戏逻辑。

GDBStub 直接在 **guest（PowerPC）地址空间** 工作：

    读内存    m<addr>,<len>
    写内存    M<addr>,<len>:<hex>
    设断点    Z0(执行) / Z1(硬件) / Z2(写) / Z4(读)
    继续      c        单步 s
    读寄存器  g

拿到 PC 就是 PPC 指令地址，用 capstone 的 CS_ARCH_PPC 反汇编，
可读性和 JIT 代码不在一个量级。

## 前置

xenia 必须带 `--debug` 启动，且配置里 `gdbport` 非 0（已设为 1234）。

## 用法

    python tools/gdbstub.py probe                    # 连接并打印寄存器/能力
    python tools/gdbstub.py read 0x417AE470 64       # 读 guest 内存
    python tools/gdbstub.py wp 0x417AE474 4          # 设写断点，等触发，打印 PC
    python tools/gdbstub.py rp 0x417AE474 4          # 设读断点
    python tools/gdbstub.py bp 0x82001234            # 执行断点
"""

from __future__ import annotations

import socket
import struct
import sys
import time

HOST = "127.0.0.1"
PORT = 1234


class RSP:
    def __init__(self, host=HOST, port=PORT, timeout=10.0):
        self.s = socket.create_connection((host, port), timeout)
        self.s.settimeout(timeout)
        self._drain_ack()

    def _drain_ack(self):
        self.s.setblocking(False)
        try:
            while True:
                if not self.s.recv(4096):
                    break
        except BlockingIOError:
            pass
        except Exception:
            pass
        self.s.setblocking(True)
        self.s.settimeout(10.0)

    def _recv_packet(self):
        """读一个 $...#xx 包，返回 (data, kind)。kind: 'pkt' 或 'notify'。"""
        data = b""
        while True:
            ch = self.s.recv(1)
            if not ch:
                raise EOFError("连接被关闭")
            if ch == b"$":
                break
            # '+' / '-' ack 与噪声都跳过
        while True:
            ch = self.s.recv(1)
            if not ch:
                raise EOFError("包中途断开")
            if ch == b"#":
                break
            data += ch
        cs = self.s.recv(2)          # 校验和，暂不校验
        try:
            self.s.sendall(b"+")
        except Exception:
            pass
        txt = data.decode("ascii", "replace")
        return txt

    def cmd(self, payload: str) -> str:
        body = payload.encode("ascii")
        pkt = b"$" + body + b"#" + f"{sum(body) & 0xFF:02x}".encode()
        self.s.sendall(pkt)
        return self._recv_packet()

    # ---- 常用操作 -----------------------------------------------------
    def halt_reason(self):
        return self.cmd("?")

    def regs_raw(self):
        return self.cmd("g")

    def read_mem(self, addr: int, n: int) -> bytes | None:
        r = self.cmd(f"m{addr:x},{n:x}")
        if not r or r.startswith("E"):
            return None
        try:
            return bytes.fromhex(r)
        except ValueError:
            return None

    def write_mem(self, addr: int, data: bytes) -> str:
        return self.cmd(f"M{addr:x},{len(data):x}:{data.hex()}")

    def set_break(self, kind: str, addr: int, size: int = 4) -> str:
        """kind: 'Z0' 执行 / 'Z1' 硬件 / 'Z2' 写 / 'Z3' 读 / 'Z4' 访问"""
        k = {1: 1, 2: 2, 4: 4, 8: 8}.get(size, 4)
        return self.cmd(f"{kind},{addr:x},{k}")

    def remove_break(self, kind: str, addr: int, size: int = 4) -> str:
        """删除断点：GDB 用 z 代替 Z（z0/z1/z2/z4）。"""
        k = {1: 1, 2: 2, 4: 4, 8: 8}.get(size, 4)
        return self.cmd(f"z{kind[1]},{addr:x},{k}")

    def cont(self):
        return self.cmd("c")

    def step(self):
        return self.cmd("s")

    def wait_stop(self, secs: float = 30.0):
        """继续后等待停下来（'*' 或 'T' 包）。"""
        self.s.settimeout(secs)
        try:
            return self._recv_packet()
        finally:
            self.s.settimeout(10.0)


def parse_regs(hexstr: str, count: int = 40) -> dict[str, int]:
    """GDB 'g' 包：按字节序解析成一串 32 位/64 位寄存器。xenia 是 64 位。"""
    out = {}
    data = bytes.fromhex(hexstr) if hexstr else b""
    n = min(count, len(data) // 8)
    for i in range(n):
        chunk = data[i * 8:(i + 1) * 8]
        if len(chunk) == 8:
            out[f"r{i}" if i < 32 else f"x{i}"] = struct.unpack(">Q", chunk)[0]
    return out


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    op = sys.argv[1].lower()
    try:
        c = RSP()
    except Exception as e:
        print(f"连不上 {HOST}:{PORT} —— {e}")
        print("请确认：xenia 是用 --debug 启动的，配置里 gdbport = 1234")
        return 1

    stop = c.halt_reason()
    print(f"已连接  停止原因: {stop}")

    if op == "probe":
        q = c.cmd("qSupported")
        print(f"qSupported: {q[:300]}")
        g = c.regs_raw()
        print(f"寄存器包长度 {len(g)} 字符（{len(g) // 2} 字节）")
        regs = parse_regs(g, 40)
        for k, v in list(regs.items())[:16]:
            print(f"  {k:<4} = 0x{v:016X}")
        mem = c.read_mem(0x405EA750, 16)
        if mem:
            print(f"相机 quat @0x405EA750 = "
                  f"{[round(x, 4) for x in struct.unpack('>4f', mem)]}")
        else:
            print("读 0x405EA750 失败（或许需要先继续执行一帧）")
        return 0

    if op == "read":
        addr = int(sys.argv[2], 0)
        n = int(sys.argv[3], 0) if len(sys.argv) > 3 else 64
        b = c.read_mem(addr, n)
        if not b:
            print("读失败")
            return 1
        for i in range(0, len(b), 16):
            chunk = b[i:i + 16]
            hexs = " ".join(f"{x:02X}" for x in chunk)
            asc = "".join(chr(x) if 32 <= x < 127 else "." for x in chunk)
            print(f"0x{addr + i:08X}  {hexs:<48} {asc}")
        return 0

    if op in ("wp", "rp", "bp"):
        kind = {"wp": "Z2", "rp": "Z4", "bp": "Z0"}[op]
        addr = int(sys.argv[2], 0)
        size = int(sys.argv[3], 0) if len(sys.argv) > 3 else 4
        r = c.set_break(kind, addr, size)
        print(f"设断点 {kind} @0x{addr:X} size={size} → {r or '(空)'}")
        if r.startswith("E") or r == "":
            print("设失败（stub 可能不支持这类断点）")
            return 1
        print("继续执行，等待触发 …")
        stop = c.wait_stop(60)
        print(f"停止包: {stop}")
        g = c.regs_raw()
        regs = parse_regs(g, 40)
        pc = regs.get("r32") or regs.get("x32") or 0
        print(f"PC（PPC）≈ 0x{pc:08X}")
        for k, v in list(regs.items())[:8]:
            print(f"  {k:<4} = 0x{v:016X}")
        return 0

    print(f"未知操作 {op}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

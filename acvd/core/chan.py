"""
acvd/core/chan.py — 跨线程通信原语。

全系统只有两种跨线程传递方式，不允许出现第三种：

    Slot  单槽覆盖式信箱：永远只保留最新一份。
          写方 put() 只持锁几纳秒，读方 get() 同样 —— 双方都不会等对方干活。
          读慢了不会积压，而是直接读到更新的那一份（手柄输出要的就是这个语义）。

    Alarm 报警闩锁：只记"发生过几次"，不传数据。
          MEM_LOST / OUT_STALE 这类信号用它，避免为了一次报警去唤醒线程。

为什么不用 queue.Queue：
    队列会积压。一旦消费方卡住，队列里就攒着一堆过期的摇杆指令，
    恢复瞬间会连续输出历史值 —— 正是"把上一场景的推力带进下一场景"。
    单槽天然消灭这个隐患：任何时刻只有最新的一份是真值。
"""

from __future__ import annotations

import threading


class Slot:
    """单槽覆盖式信箱。"""

    __slots__ = ("_lock", "_val", "_seq")

    def __init__(self):
        self._lock = threading.Lock()
        self._val = None
        self._seq = 0

    def put(self, val) -> int:
        with self._lock:
            self._val = val
            self._seq += 1
            return self._seq

    def get(self):
        """返回 (最新值, 序号)。读方拿到的永远是一份完整对象。"""
        with self._lock:
            return self._val, self._seq

    def seq(self) -> int:
        """只看序号，不取值（判断"有没有更新"的最便宜方式）。"""
        return self._seq


class Alarm:
    """计数式报警闩锁：fire() 累加，take() 取走并清零。"""

    __slots__ = ("_lock", "_pending", "_total")

    def __init__(self):
        self._lock = threading.Lock()
        self._pending = 0
        self._total = 0

    def fire(self) -> None:
        with self._lock:
            self._pending += 1
            self._total += 1

    def take(self) -> int:
        with self._lock:
            n, self._pending = self._pending, 0
            return n

    def pending(self) -> int:
        return self._pending

    def total(self) -> int:
        return self._total

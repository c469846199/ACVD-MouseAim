"""
acvd/core/output.py — 硬件输出层（两个线程）

    Composer 125Hz：取 Snap + Cmd → 调绝对坐标控制器 → 发布一帧杆量
    Watchdog 独立：100ms 没新帧 → 强制回中

门控只看三件事实：指令是否 TRACKING、游戏进程是否还在、玩家是否按了 F9。
**不判断焦点**：我们输出的是手柄，Xenia 在后台照样收手柄输入；
焦点是旧内核给键鼠用的规则，留着只会把映射悄悄掐掉。
**不判断「推了杆怎么没反应」**：游戏屏蔽输入是正常且无害的。

「接管」= 指令 TRACKING **且** 游戏进程真在。少了后半句就会出现这种
事故：先开 mapper、模拟器还没启动，光标就被每帧 SetCursorPos 钉死在
屏幕中心，玩家除了按 F9 什么都干不了 —— 而当时根本没有可闭环的对象。

鼠标增量与光标回中每帧都做，与快照是否新鲜无关。快照过期只让
本帧不推杆（live=False），绝不吞掉鼠标位移、绝不松开光标。
"""

from __future__ import annotations

import threading
import time

from . import control
from .blackbox import Blackbox
from .chan import Alarm, Slot
from .types import MODE_OFF, MODE_TRACKING, Cfg, CtrlState, OutFrame

COMPOSE_HZ = 125.0
SNAP_STALE = 0.12           # 快照超过它就当这一帧没有观测
WATCHDOG_MS = 100.0         # 超过这么久没有新帧 → 强制回中
JUMP_M = 50.0               # 相机单帧位移超过它 = 跨关卡传送（不是移动）


class Composer(threading.Thread):
    def __init__(self, snap_slot: Slot, cmd_slot: Slot, out_slot: Slot,
                 get_delta, get_view, cfg: Cfg, log=print,
                 is_paused=None, anchor_alarm=None, game_alive=None,
                 state=None, step_fn=None, get_tau=None):
        super().__init__(name="composer", daemon=True)
        self.snap_slot = snap_slot
        self.cmd_slot = cmd_slot
        self.out_slot = out_slot
        self.get_delta = get_delta
        self.get_view = get_view
        self.cfg = cfg
        self.log = log
        self.is_paused = is_paused or (lambda: False)
        self.anchor_alarm = anchor_alarm
        # 「游戏进程还在」与 Sensor 用同一份事实（proc_alive），
        # 不另造判据：两处不一致就会一边显示没接管、一边把光标锁死。
        self.game_alive = game_alive or (lambda: True)

        self.running = True
        # 控制器可替换（V1 绝对环 / V2 逆模型）。默认就是 V1，
        # 装配层不传时行为与重构前逐位一致。
        self.state = state if state is not None else CtrlState()
        self.step_fn = step_fn or control.step
        self.get_tau = get_tau or (lambda: 0.0)
        # 调试用固定输出：设了它就绕开闭环，直接发这对杆量。
        # 用途是「在内存里找游戏到底读了哪个值」——发一个不常见的常数，
        # 扫一遍内存，改一个数再扫一遍，两次交集就是输入变量。
        self.hold: tuple[float, float] | None = None
        self.seq = 0
        self.mode = MODE_OFF
        self.frames = 0
        self._last_takeover = None
        self._last_pos = None
        # 125Hz 环形记录：抖动与 τ 都发生在 100ms 尺度，管道 10Hz 看不出来。
        # 只占内存、不落盘，出问题一条命令 dump。
        self.bb = Blackbox()

    def set_cfg(self, cfg: Cfg) -> None:
        self.cfg = cfg

    def stop(self) -> None:
        self.running = False

    def run(self) -> None:
        period = 1.0 / COMPOSE_HZ
        while self.running:
            t0 = time.perf_counter()
            try:
                self._tick()
            except Exception as e:
                self.log(f"[输出] 合成异常（已隔离）: {e}")
            dt = period - (time.perf_counter() - t0)
            if dt > 0:
                time.sleep(dt)

    def _tick(self) -> None:
        now = time.perf_counter()
        # F2 锚定请求：由主循环线程 fire，这里在拥有 CtrlState 的线程里消费。
        # 只置一个标志，实际锚定交给 control.step 的既有分支，不新增逻辑。
        if self.anchor_alarm is not None and self.anchor_alarm.take():
            self.state.anchor_pending = True
        cmd, _ = self.cmd_slot.get()
        snap, _ = self.snap_slot.get()
        mode = cmd.mode if cmd is not None else MODE_OFF
        paused = self.is_paused()
        # 接管 = 指令 TRACKING + 游戏进程真在。进程不在时不存在可闭环的对象，
        # 既不该锁光标，也不该在面板上谎称已接管（frame.mode 就是面板读的那份）。
        takeover = (mode == MODE_TRACKING) and bool(self.game_alive())
        self.mode = MODE_TRACKING if takeover else MODE_OFF
        if takeover != self._last_takeover:
            self._last_takeover = takeover
            self.log("[输出] 已接管鼠标（游戏进程在，闭环可跑）" if takeover
                     else "[输出] 未接管：游戏进程不在，鼠标完全交还给你")

        # 跨关卡传送：相机位置单帧跳变 > JUMP_M。世界角原点换了，旧 Target_Abs
        # 已经没有意义，此刻静默重锚（等同按一次 F2）——锚定发生在累加之前，
        # 所以这一帧不推杆，玩家进关打最速突击时不会被甩一下视角。
        # 同一关内正常移动单帧最多几米，不可能触发（这不是判据，是传送这一事实）。
        if snap is not None and snap.ok:
            p = snap.cam_pos
            # 全零/超界 = 加载中或撕裂读，不是「位置」这个事实，不参与判断。
            # 否则读到一个 (0,0,0) 的坏帧就会凭空触发一次重锚。
            valid = (all(abs(v) < 1.0e5 for v in p)
                     and any(abs(v) > 1.0e-6 for v in p))
            if valid and self._last_pos is not None:
                d2 = sum((p[i] - self._last_pos[i]) ** 2 for i in range(3))
                if d2 > JUMP_M * JUMP_M:
                    self.state.anchor_pending = True
                    self.log("[输出] 场景跳变（跨关卡）→ 静默重锚，本帧不推杆")
            if valid:
                self._last_pos = p

        # 鼠标每帧都读：get_delta() 同时消费 Raw Input 位移并把光标钉回中心。
        # 只要接管且玩家没按 F9，就绝不能跳过 —— 否则一次 0.7s 全堆扫描
        # （快照过期）就会让鼠标脱锁、位移被吞，而面板仍显示「已接管」。
        dx, dy = (self.get_delta() if takeover and not paused
                  else (0.0, 0.0))
        fresh = snap is not None and (now - snap.t) <= SNAP_STALE
        live = takeover and fresh and not paused
        if self.hold is not None:
            # 固定输出：绕开闭环（也绕开 dead / gamma / 补偿），
            # 只把这个常数送进手柄，用于内存特征扫描。
            hx, hy = self.hold
            stick = Stick(rx=hx, ry=hy, active=True,
                          ch_x=self.state.base.ch_x
                          if hasattr(self.state, "base") else 0.0,
                          ch_y=self.state.base.ch_y
                          if hasattr(self.state, "base") else 0.0)
        else:
            stick = self.step_fn(self.cfg, self.state, snap, dx, dy,
                                 now, self.get_view(), live=live)

        self.seq += 1
        self.frames += 1
        frame = OutFrame(seq=self.seq, t=now, mode=self.mode,
                         rx=stick.rx, ry=stick.ry, active=stick.active,
                         err_y=stick.err_y, err_p=stick.err_p,
                         ch_x=stick.ch_x, ch_y=stick.ch_y)
        self.out_slot.put(frame)

        # 黑匣子：每帧一行。字段都是这一帧真实发生的事，
        # 不含任何推断，所以事后分析不需要复现现场。
        self.bb.push(
            t=now, mode=self.mode, rx=stick.rx, ry=stick.ry,
            err_y=stick.err_y, err_p=stick.err_p,
            ch_x=stick.ch_x, ch_y=stick.ch_y,
            cam_yaw=snap.cam_yaw if snap is not None else 0.0,
            cam_pitch=snap.cam_pitch if snap is not None else 0.0,
            fov=snap.fov if snap is not None else 0.0,
            tau=float(self.get_tau()))


class Watchdog(threading.Thread):
    """独立看门狗：100ms 没收到新帧就强制回中。不读内存、不算数学。"""

    def __init__(self, out_slot: Slot, stale_alarm: Alarm, log=print):
        super().__init__(name="watchdog", daemon=True)
        self.out_slot = out_slot
        self.alarm = stale_alarm
        self.log = log
        self.running = True
        self._lock = threading.Lock()
        self._rx = 0.0
        self._ry = 0.0
        self._frame = None
        self.last_seq = -1
        self.last_t = time.perf_counter()
        self.cuts = 0

    def value(self) -> tuple[float, float]:
        with self._lock:
            return self._rx, self._ry

    def frame(self):
        return self._frame

    def stop(self) -> None:
        self.running = False

    def run(self) -> None:
        while self.running:
            frame, seq = self.out_slot.get()
            now = time.perf_counter()
            if frame is not None and seq != self.last_seq:
                self.last_seq = seq
                self.last_t = now
                with self._lock:
                    self._rx, self._ry = frame.rx, frame.ry
                self._frame = frame
            elif now - self.last_t > WATCHDOG_MS / 1000.0:
                if self._rx != 0.0 or self._ry != 0.0:
                    with self._lock:
                        self._rx = self._ry = 0.0
                    self.cuts += 1
                    self.log(f"! 输出层 {WATCHDOG_MS:.0f}ms 无新指令 → 强制回中"
                             f"（累计 {self.cuts} 次）")
                self.alarm.fire()
            time.sleep(0.002)

    def stats(self) -> str:
        return f"cuts={self.cuts} rx={self._rx:+.2f} ry={self._ry:+.2f}"

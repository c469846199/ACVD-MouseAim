"""
acvd/core/engine.py — 装配三层，对外只暴露一个门面。

    Sensor ──Snap──► Composer ──Out──► Watchdog

没有决策层：状态只有「开 / 关」一个布尔，由 enabled 表达，
在 set_enabled 时发布一条 Cmd。原来那套两态状态机是为「地址可能错」
服务的，地址已经固定，它没有存在理由了。

前端只认 start / stop / stick / frame / status / snapshot / set_enabled /
set_cfg / force_rescan / request_anchor / is_tracking，签名保持不变。
"""

from __future__ import annotations

import time

from . import control_v2
from .chan import Alarm, Slot
from .output import Composer, Watchdog
from .sensor import CAM_FOV_GUEST, CAM_QUAT_GUEST, Sensor
from .types import MODE_OFF, MODE_TRACKING, Cfg, Cmd


class AimEngine:
    def __init__(self, get_proc, get_delta, get_view, cfg: Cfg | None = None,
                 log=print, hz: float = 125.0,
                 is_paused=None, ctrl: str = "v1",
                 v2: control_v2.V2Cfg | None = None):
        self.log = log
        self._snap = Slot()
        self._cmd = Slot()
        self._out = Slot()
        self._stale = Alarm()
        self._anchor = Alarm()      # F2：把准星锚定到当前视角

        self.enabled = True
        self._cmd_seq = 0

        # 控制器选择：v1 = 直读相机（现状），v2 = 逆模型去滞后。
        # 两者接口逐字相同，装配层只换函数与状态对象，别的都不动。
        self.ctrl = ctrl
        self.v2cfg = v2 or control_v2.V2Cfg()
        if ctrl == "v2":
            v2cfg = self.v2cfg

            def _step(cfg, st, snap, dx, dy, now, view, live=True):
                return control_v2.step(cfg, st, snap, dx, dy, now, view,
                                       live=live, v2=v2cfg)
            state = control_v2.V2State()
        else:
            _step = None
            state = None

        self.sensor = Sensor(get_proc, self._snap, log=log)
        self.composer = Composer(self._snap, self._cmd, self._out,
                                 get_delta, get_view, cfg or Cfg(), log=log,
                                 is_paused=is_paused,
                                 anchor_alarm=self._anchor,
                                 game_alive=self.sensor.proc_alive,
                                 state=state, step_fn=_step,
                                 get_tau=lambda: self.v2cfg.tau)
        self.watchdog = Watchdog(self._out, self._stale, log=log)
        self._started = False

    # ---- 生命周期 -----------------------------------------------------
    def start(self) -> None:
        if self._started:
            return
        for t in (self.sensor, self.composer, self.watchdog):
            t.start()
        self._emit()
        self._started = True
        self.log("[引擎] 已启动（固定相机反馈 / 绝对坐标）")

    def stop(self) -> None:
        for t in (self.sensor, self.composer, self.watchdog):
            try:
                t.stop()
            except Exception:
                pass
        self._started = False

    # ---- 指令 ---------------------------------------------------------
    def _emit(self) -> None:
        self._cmd_seq += 1
        mode = MODE_TRACKING if self.enabled else MODE_OFF
        self._cmd.put(Cmd(seq=self._cmd_seq, t=time.perf_counter(),
                          mode=mode, reason="enabled" if self.enabled else "关闭"))

    def set_enabled(self, on: bool) -> None:
        on = bool(on)
        if on == self.enabled:
            return
        self.enabled = on
        self._emit()

    # ---- 设备写入点 ---------------------------------------------------
    def stick(self) -> tuple[float, float]:
        return self.watchdog.value()

    def frame(self):
        return self.watchdog.frame()

    def is_tracking(self) -> bool:
        """是否真的在闭环：开了、游戏进程还在。

        只认「进程存在」这一件事，不认「这一帧相机读到没有」——
        后者会随加载/撕裂读抖动，把状态翻来翻去。

        frame.mode 已经由 Composer 按同一份 proc_alive 判过了，这里的
        proc_alive 只是复述事实，不是第二条规则。
        """
        f = self.watchdog.frame()
        return bool(self.enabled and f is not None
                    and f.mode == MODE_TRACKING and self.sensor.proc_alive())

    def set_cfg(self, cfg: Cfg) -> None:
        self.composer.set_cfg(cfg)

    # ---- 黑匣子 -------------------------------------------------------
    def trace_len(self) -> int:
        """已记录多少帧（125Hz，20000 帧约 160 秒）。"""
        return len(self.composer.bb)

    def dump_trace(self, path=None) -> str:
        """把最近这段时间的 125Hz 闭环数据写成 CSV，返回路径。

        记的是「真正发出去的杆量 / 误差 / 相机角度 / FOV / 当时的 tau」，
        所以事后分析不需要复现现场，也不需要用户按节拍操作。
        """
        return self.composer.bb.dump(path)

    def set_v2(self, tau=None, lp=None, rate_max=None) -> None:
        """热改逆模型参数。tau=0 立刻退回 V1 行为（同一个对象，同一份状态）。

        不重建线程、不换状态对象：V2State 里的 Target_Abs 原样保留，
        所以调 tau 不会让目标角凭空跳一下。
        """
        c = self.v2cfg
        if tau is not None:
            c.tau = float(tau)
        if lp is not None:
            c.lp = max(float(lp), 1e-4)
        if rate_max is not None:
            c.rate_max = max(float(rate_max), 1.0)

    def set_hold(self, rx: float | None, ry: float = 0.0) -> None:
        """固定输出杆量（调试）。rx=None 恢复正常闭环。

        用途：在内存里定位「游戏真正读到的摇杆值」——把 rx 固定成一个
        不常见的常数，全堆扫一遍；换一个数再扫一遍，两次交集就是它。
        """
        self.composer.hold = None if rx is None else (float(rx), float(ry))

    def force_rescan(self) -> None:
        """F1：没有地址可重扫了，语义收敛为「重新对齐坐标」（等同 F2）。"""
        self._anchor.fire()

    def request_anchor(self) -> None:
        """F2：把准星锚定到当前视角的绝对坐标（下一帧生效）。

        换关后世界角原点变了，旧关卡的 Target_Abs 会让闭环满杆回头。
        玩家主动按一下即可。除此之外没有任何逻辑能改 Target_Abs。
        """
        self._anchor.fire()

    # ---- 诊断（前端沿用旧字段名）---------------------------------------
    _PANEL = {"TRACKING": "tracked", "OFF": "off"}

    def status(self) -> str:
        s = self.snapshot()
        st = self.sensor.stats()
        return (f"auto_state={self._PANEL.get(s['mode'], 'idle')} "
                f"auto_scene=0 auto_cand=1 auto_votes=0 "
                f"auto_cam=0x{CAM_QUAT_GUEST:012X} "
                f"auto_try={s['reads']} "
                f"auto_on={1 if self.enabled else 0} "
                f"engine_mode={s['mode']} "
                f"addr=0x{CAM_FOV_GUEST:012X} "
                f"base=0x{self.sensor.base():012X} "
                f"ctrl={self.ctrl} tau={self.v2cfg.tau:.3f} "
                f"fps={self.sensor.fps():.0f} "
                f"ok={st['reads'] - st['bad']} bad={st['bad']} "
                f"cut={s['cuts']} slow={st['slow']} max={st['max_ms']:.0f}ms")

    def snapshot(self) -> dict:
        f = self.frame()
        st = self.sensor.stats()
        return {
            "mode": f.mode if f else MODE_OFF,
            "rx": self.stick()[0],
            "ry": self.stick()[1],
            "err_y": f.err_y if f else 0.0,
            "err_p": f.err_p if f else 0.0,
            "ch_x": f.ch_x if f else 0.0,
            "ch_y": f.ch_y if f else 0.0,
            "addr": CAM_FOV_GUEST,
            "cand": 1,
            "votes": 0,
            "cam": CAM_QUAT_GUEST,
            "reason": "相机固定",
            "cuts": self.watchdog.cuts,
            "reads": st["reads"],
            "slow": st["slow"],
            "max_ms": st["max_ms"],
        }

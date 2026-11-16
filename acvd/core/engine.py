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
from .sensor import Sensor
from .types import MODE_OFF, MODE_TRACKING, Cfg, Cmd


def _load_cam_addr(path: str | None = None) -> tuple[int, int] | None:
    """读 cam_addr.json，返回 (quat_guest, fov_guest)。

    ## 为什么放在装配层

    采集层（sensor.py）被分层守卫限制，只许 import
    math/struct/threading/time + chan/types/gmem，读文件的 json/os
    会被拦。engine.py 是装配层（Rule 允许 None = 不限制依赖），
    所以配置读取放在这里，读到之后把结果传给 Sensor。

    ## 相机地址为什么需要外部覆盖

    地址由游戏加载时的内存分配顺序决定。同一台电脑同一个模拟器时稳定，
    但换模拟器版本、换电脑、加载时序不同就会移位 ——
    实测：Edge 是 0x405EA750，Netplay 分支配到 0x405F30E0 附近。

    tools/find_camera.py 扫出真实地址后写这个文件，启动时读一次。

    ## 容错

    文件不存在、格式错、数值越界 —— 一律返回 None（用内置默认值），
    绝不因为配置坏了导致启动失败。
    """
    import json
    import os
    import sys

    if path is None:
        # 源码模式：本文件在 <项目根>/acvd/core/，往上三级是项目根。
        # EXE 模式（PyInstaller onedir）：本文件在
        # <exe目录>/_internal/acvd/core/，往上三级是 _internal ——
        # 但 cam_addr.json 写在 exe 同级（用户能看见的位置），
        # 所以 frozen 时取 sys.executable 的目录。
        if getattr(sys, "frozen", False):
            root = os.path.dirname(os.path.abspath(sys.executable))
        else:
            root = os.path.dirname(os.path.dirname(
                os.path.dirname(os.path.abspath(__file__))))
        path = os.path.join(root, "cam_addr.json")
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        q = int(d["quat_guest"])
        fov = int(d.get("fov_guest", q + 0x20))
        if 0 < q < 0x1_0000_0000 and 0 < fov < 0x1_0000_0000:
            return q, fov
    except Exception:
        pass
    return None


class AimEngine:
    def __init__(self, get_proc, get_delta, get_view, cfg: Cfg | None = None,
                 log=print, hz: float = 125.0,
                 is_paused=None, ctrl: str = "v1",
                 v2: control_v2.V2Cfg | None = None,
                 cam_addr_path: str | None = None):
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

        # 相机地址：优先用 cam_addr.json 的覆盖值。
        # 没配置就用内置默认，行为与改造前完全一致。
        _cam = _load_cam_addr(cam_addr_path)
        if _cam is not None:
            log(f"[采集] 使用自定义相机地址 "
                f"quat=0x{_cam[0]:012X} fov=0x{_cam[1]:012X}"
                f"（cam_addr.json）")

        self.sensor = Sensor(get_proc, self._snap, log=log,
                             cam_quat=_cam[0] if _cam else None,
                             cam_fov=_cam[1] if _cam else None)
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
        # 地址用 sensor 上的实例值，不是模块常量 —— 否则 status 永远
        # 显示内置默认的 0x405EA750/0x405EA770，即使实际在用 cam_addr.json
        # 里配置的地址。排查时会误以为「配置没生效」。
        return (f"auto_state={self._PANEL.get(s['mode'], 'idle')} "
                f"auto_scene=0 auto_cand=1 auto_votes=0 "
                f"auto_cam=0x{self.sensor.cam_quat:012X} "
                f"auto_try={s['reads']} "
                f"auto_on={1 if self.enabled else 0} "
                f"engine_mode={s['mode']} "
                f"addr=0x{self.sensor.cam_fov:012X} "
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
            "addr": self.sensor.cam_fov,
            "cand": 1,
            "votes": 0,
            "cam": self.sensor.cam_quat,
            "reason": "相机固定",
            "cuts": self.watchdog.cuts,
            "reads": st["reads"],
            "slow": st["slow"],
            "max_ms": st["max_ms"],
        }

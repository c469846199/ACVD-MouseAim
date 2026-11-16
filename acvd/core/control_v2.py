"""
acvd/core/control_v2.py — 逆模型（去滞后）控制器

V1 的反馈是相机读数本身：`Error = Target_Abs − cam`。
但相机是**一阶低通**，它永远比真正的瞄准方向晚 τ≈0.15s。用晚到的读数
做反馈，等于往闭环里塞了一个 150ms 的滞后，阻尼比被压到 0.5~0.6，
只能靠加大 full_stick_angle（降增益）换稳定 —— 代价是整条链路变慢。

V2 做的事只有一件：把相机读数**反推回它追的那个东西**。

    相机是一阶低通：  τ·ċam + cam = aim
    移项即得：        aim = cam + τ·ċam

这是精确恒等式（对阶跃、斜坡、任意输入都成立），不是近似。所以我们
不需要找到火控圈的地址 —— 只需要相机角度和它的导数。

    fb_yaw   = cam_yaw   + τ · ω_yaw      （ω 由相邻帧差分 + 低通）
    fb_pitch = cam_pitch + τ · ω_pitch
    Error    = Target_Abs − fb

## FOV 在公式里的位置：没有位置

`cam_yaw / cam_pitch` 是世界角度，`τ·ω` 的量纲是「度/秒 × 秒 = 度」，
与视场**无关**。冲刺时 FOV 从 50 拉到 70、开镜从 50 压到 10，相机角度
本身不变，所以补偿量也不变。

FOV 只出现在三处，全部沿用 V1 的既有路径，V2 一行都不碰：
    1. 输入增益 k        （鼠标像素 → 世界角度）
    2. 死区 dead         （屏幕像素 → 角度）
    3. 满杆角 full_eff   （保持「滑同样的像素 = 同样的杆量」）
    4. 准星投影 f_px     （只画，不反馈）
所以「FOV 不应该进闭环」这条铁律在 V2 里仍然成立，而且更强：补偿项
连 FOV 这个符号都不出现。

## V2 什么时候退化（全部是连续的，没有开关）

    tau = 0                → 与 V1 逐位一致（回归测试断言）
    相机不转（ω≈0）        → 补偿项为 0，稳态与 V1 完全一致，无偏
    tau 估小（欠补偿）      → 阻尼不足，回到 V1 附近，仍收敛
    tau 估大（过补偿）      → 高速时提前收力，响应变钝，仍收敛
    相机不连续（换关/切镜）→ 差分无定义，本帧补偿置 0，一帧后自动恢复

没有任何分支会「切到另一套逻辑」，只有一个连续的 τ 旋钮。

## 显示与推力分开

    drive 用补偿后的 Error（提前收力，消超调）
    准星画在补偿**前**的 Error 上（画面是相机渲染的，准星必须和画面对齐）

稳态 ω=0 时两者恒等，所以这个分离只在运动中可见，且方向是对的：
画面还在往目标走，杆量先松 —— 这正是阻尼。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import control
from .types import Cfg, CtrlState, Snap, Stick, View

# 相机角速度的物理上限。实测满杆 ≈54°/s，这里给一倍余量：
# 超过 120°/s 只可能是游戏自己驱动的（过场轨道、锁定转向、瞬跳），
# 不是玩家推出来的。用它限幅，避免一次异常差分把杆量打到反向满杆。
RATE_MAX = 120.0


@dataclass
class V2Cfg:
    """逆模型参数。tau=0 时与 V1 完全一致。

    不冻结：tau 允许运行时热改（面板 / 管道 camtau），
    改字段立刻生效，不需要重建任何线程或闭包。
    """

    tau: float = 0.0        # 补偿时间常数（秒）。实测机体→相机 116ms，取 0.15 留余量
    lp: float = 0.04        # 角速度低通时间常数（秒）。相机按渲染帧更新，差分有锯齿
    rate_max: float = RATE_MAX   # 角速度限幅（°/s）
    jump_deg: float = 45.0  # 单帧角度跳变超过它 = 不连续（换关 / 切镜 / 相机被瞬移）


@dataclass
class V2State:
    """V1 状态的超集：Target_Abs 那套语义原封不动，只多几个导数记忆。"""

    base: CtrlState = field(default_factory=CtrlState)

    prev_yaw: float = 0.0
    prev_pitch: float = 0.0
    prev_t: float = 0.0
    omega_y: float = 0.0        # 低通后的 yaw 角速度（°/s）
    omega_p: float = 0.0
    primed: bool = False        # 是否已有可用的上一帧样本
    frame_id: int = -1          # 上一次更新导数时对应的渲染帧号

    # output.py 只认这一个名字（F2 手动锚定 / 换关重锚都写它），
    # 用属性转发，装配层不需要知道 V2 的存在。
    @property
    def anchor_pending(self) -> bool:
        return self.base.anchor_pending

    @anchor_pending.setter
    def anchor_pending(self, v: bool) -> None:
        self.base.anchor_pending = v
        # 锚定 = 世界角原点被重设（跨关）或玩家要求归位。无论哪种，
        # 上一帧样本都不再属于当前坐标系，导数必须重新起算。
        self.primed = False

    # 下面几个只为「外部诊断代码按 V1 的字段名访问」时不至于炸。
    # 它们不是新契约，只是转发到 base —— 语义与 CtrlState 完全一致。
    @property
    def abs_yaw(self) -> float:
        return self.base.abs_yaw

    @abs_yaw.setter
    def abs_yaw(self, v: float) -> None:
        self.base.abs_yaw = v

    @property
    def abs_pitch(self) -> float:
        return self.base.abs_pitch

    @abs_pitch.setter
    def abs_pitch(self, v: float) -> None:
        self.base.abs_pitch = v

    @property
    def err_y(self) -> float:
        return self.base.err_y

    @property
    def err_p(self) -> float:
        return self.base.err_p


def _reset(st: V2State, snap: Snap, now: float) -> None:
    st.prev_yaw = snap.cam_yaw
    st.prev_pitch = snap.cam_pitch
    st.prev_t = now
    st.omega_y = st.omega_p = 0.0
    st.primed = True
    st.frame_id = snap.frame_seq


def update_rate(st: V2State, snap: Snap, now: float, v2: V2Cfg) -> None:
    """用相机角度序列估计角速度，**只在游戏出了一帧新画面时更新**。

    为什么必须按帧：相机按渲染帧写（实测 p50 26ms ≈ 38fps，大场面 130ms），
    而我们 125~250Hz 去读。拿采样间隔（8ms）去微分一个 26ms 才变一次的量，
    会得到「0, 0, 3倍, 0, 0, 3倍」的锯齿，把 τ·ω 补偿污染成噪声。

    更关键的是：同一帧内重复更新会让 fb 漂移、杆量在游戏看到的那一帧里
    被改好几次，游戏只采样到其中一个（相位随机）—— 这本身就是抖动源。
    """
    fid = snap.frame_seq
    if st.frame_id == fid:
        # 同一帧内没有新观测：导数保持不动。
        # 只有长时间（>0.25s，远大于最慢帧 130ms）没有新帧，才认为相机
        # 真的停住了（静止时相机值不变，frame_seq 不会增），导数归零。
        if st.primed and (now - st.prev_t) > 0.25:
            st.omega_y = st.omega_p = 0.0
            st.prev_t = now
        return

    dt = now - st.prev_t
    if not st.primed or dt <= 0.0 or dt > 0.5:
        # 首帧 / 时钟异常 / 断流太久 —— 差分无意义，从零起算
        _reset(st, snap, now)
        return

    dyaw = control.wrap180(snap.cam_yaw - st.prev_yaw)
    dpit = snap.cam_pitch - st.prev_pitch
    if abs(dyaw) > v2.jump_deg or abs(dpit) > v2.jump_deg:
        # 单帧跳 45° 在机甲里不可能：这是换关、过场切镜，或相机被瞬间重定位。
        # 这一刻的「导数」是无穷大，没有物理意义，按 0 处理并重新起算。
        _reset(st, snap, now)
        return

    cap = v2.rate_max
    raw_y = control.clamp(dyaw / dt, -cap, cap)
    raw_p = control.clamp(dpit / dt, -cap, cap)

    # 现在 dt 是**真实帧间隔**（实测中位 26ms，掉帧 130ms），不是采样间隔，
    # 所以差分值本身就是当帧的真实角速度；低通只用来压读数噪声。
    a = 1.0 if v2.lp <= 1e-9 else min(dt / v2.lp, 1.0)
    st.omega_y += (raw_y - st.omega_y) * a
    st.omega_p += (raw_p - st.omega_p) * a
    st.frame_id = fid

    st.prev_yaw = snap.cam_yaw
    st.prev_pitch = snap.cam_pitch
    st.prev_t = now


def _predict_err(err_raw: float, adv: float, cap: float = 0.85) -> float:
    """预测误差 = 原始误差 − 超前量，且超前量**最多吃掉 85% 的误差**。

    为什么必须有这个上限：当 τ·ω 大于剩余误差时，只有两种结局，都很糟——

        A. 允许 err 跨零 → 满杆反推（「火控圈在过去的路上打反杆」）
        B. 禁止跨零直接归 0 → 松手靠滑行，可相机带着惯性冲过目标，
           到位后弹回来（「更大的回弹」）

    两种都是同一个根源：**补偿吃掉了超过 100% 的剩余误差**。
    物理上它不可能比"全部"还多——多出来的部分是 τ 估大了的虚高量。

    cap=0.85 的意思是：无论 τ·ω 多大，最多只认 85% 的误差是"已经被超前量
    覆盖掉的"。剩下 15% 永远留给闭环收敛，所以既不会反推，也不会松手太早。
    """
    if err_raw == 0.0:
        return 0.0
    lim = cap * abs(err_raw)
    if abs(adv) > lim:
        adv = math.copysign(lim, adv)
    return err_raw - adv


def step(cfg: Cfg, st: V2State, snap: Snap, dx: float, dy: float,
         now: float, view: View, live: bool = True,
         v2: V2Cfg | None = None) -> Stick:
    """与 control.step 的接口逐字一致，唯一的差别是反馈值多了一项 τ·ω。"""
    v2 = v2 or V2Cfg()

    # ---- FOV 折算：与 V1 完全相同，补偿项不参与 ----
    fov = snap.fov if snap is not None else 45.0
    half = math.tan(math.radians(max(fov, 1.0)) / 2.0)
    f_px = (view.sw / 2.0) / half
    per_px = math.degrees(1.0 / f_px)
    k = per_px * cfg.sens_scale
    dead = per_px * cfg.dead_px

    ref = math.tan(math.radians(control.REF_FOV) / 2.0)
    full_eff = max(cfg.full_stick_angle * (half / ref), 2.0)

    base = st.base

    # ---- 锚定：与 V1 相同，且必须发生在累加之前 ----
    if base.anchor_pending and snap is not None and snap.ok:
        base.abs_yaw = snap.cam_yaw
        base.abs_pitch = snap.cam_pitch
        base.anchor_pending = False

    # 角速度每帧都跟（只要观测有效）：与 live 无关。
    # 暂停期间玩家用手柄转视角，ω 照样要跟上；恢复那一刻补偿就是真实的，
    # 不会从 0 慢慢爬。live 只决定「推不推杆」，不决定「记不记得」。
    if snap is not None and snap.ok:
        update_rate(st, snap, now, v2)

    # ---- Target_Abs：只由鼠标改写，任何门控都无权跳过 ----
    base.abs_yaw = (base.abs_yaw + dx * k) % 360.0
    base.abs_pitch = base.abs_pitch - dy * k

    if not live or snap is None or not snap.ok:
        return control.neutral(base)

    # ---- 反推首端：aim = cam + τ·ω ----
    # 稳态 ω=0 时 fb 恒等于 cam，与 V1 逐位一致；运动中 fb 领先 cam，
    # 等于提前看见「相机还没追到的那一截」。
    # 超前量**不允许把误差推过零**（见 _predict_err），否则大幅移动的
    # 收尾段会反向推杆。
    adv_y = v2.tau * st.omega_y
    adv_p = v2.tau * st.omega_p
    raw_y = control.wrap180(base.abs_yaw - snap.cam_yaw)   # 画准星（对齐全画面）
    raw_p = base.abs_pitch - snap.cam_pitch
    err_y = _predict_err(raw_y, adv_y)      # 驱动杆量
    err_p = _predict_err(raw_p, adv_p)
    vis_y, vis_p = raw_y, raw_p

    ux = control.drive(err_y, cfg, dead, full_eff)
    uy = control.drive(err_p, cfg, dead, full_eff)
    base.out_x += (ux - base.out_x) * cfg.smooth
    base.out_y += (uy - base.out_y) * cfg.smooth
    base.err_y, base.err_p = vis_y, vis_p

    ex = control.clamp(vis_y, -control.DISPLAY_CLAMP, control.DISPLAY_CLAMP)
    ep = control.clamp(vis_p, -cfg.pitch_limit, cfg.pitch_limit)
    base.ch_x = control.clamp(view.cx + f_px * math.tan(math.radians(ex)),
                              14.0, view.sw - 14.0)
    base.ch_y = control.clamp(view.cy - f_px * math.tan(math.radians(ep)),
                              14.0, view.sh - 14.0)

    return Stick(rx=base.out_x, ry=base.out_y, err_y=vis_y, err_p=vis_p,
                 ch_x=base.ch_x, ch_y=base.ch_y, active=True)

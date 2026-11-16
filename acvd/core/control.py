"""
acvd/core/control.py — 绝对坐标控制器（纯函数）

    鼠标 dx ──线性累加──► Target_Abs（360 度滑轨上的绝对坐标）
    Error = wrap180(Target_Abs − cam_yaw)        # X 轴：环上最短差
    Error = Target_Abs_P − cam_pitch             # Y 轴：线性轴
    杆量 = 死区逆变换(clamp(Error / full_stick_angle))
    准星 = Error 的屏幕投影（只显示，绝不回写 Target_Abs）

Aim_Abs 两轴都取**固定相机**：yaw 走环，pitch 走线性轴。相机是镜像级
固定地址、每帧更新的真实反馈，不需要扫描/簇/判据。它相对首端量有一阶
滞后（τ≈0.15s），表现为阻尼比不足，靠 Cfg.full_stick_angle 一个参数压掉，
不是加规则。

三条不可越过的边界：
  1. Target_Abs 只由鼠标 dx/dy 改变；首次初始化把它锚到当前世界角，
     此后任何逻辑（Aim_Abs 变化、准星回中、断流、失焦、换关）都不碰它。
  2. 输入增益 = 当前 FOV 的投影逆映射 f_px，逐帧计算后线性累加：
     同一 FOV 下与鼠标速度、分帧方式无关；FOV 平滑拉伸时按路径积分，
     累加出的 Target_Abs 才是真实物理角度，不会积累固定系数的系统误差。
  3. game_deadzone 是执行器的物理逆变换，不是「判定」；没有角度死带、
     没有 stall、没有抗饱和、没有前馈、没有 resync、没有自校准。

FOV 只出现在两处：输入增益 k 与准星投影 f_px。闭环（Error → 杆量）
完全不含 FOV；Aim_Abs / cam_pitch 是世界角度量，火控圈距离从不进方程。
所以 FOV 平滑变化不会引起摇杆推力抖动，也不需要任何补偿。
"""

from __future__ import annotations

import math

from .types import Cfg, CtrlState, Snap, Stick, View

DISPLAY_CLAMP = 60.0            # 仅限制 tan() 的显示投影，不限制 Target_Abs
REF_FOV = 50.0                  # 满杆角度以这个 FOV 为基准来定义（°）


def wrap180(d: float) -> float:
    return (d + 180.0) % 360.0 - 180.0


def clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else (hi if v > hi else v)


def drive(err: float, cfg: Cfg, dead: float = 0.0,
          full: float | None = None) -> float:
    """误差 → 杆量。

    dead 以内**直接输出 0**（摇杆真正回中），dead 之外用死区逆变换：
    err→0 时 u→gdz，而游戏在 u=gdz 时角速度恰为 0，所以渐近收敛、不抖振。

    full 不传就用 cfg.full_stick_angle；step 会传按 FOV 折算后的值。

    curve_gamma 把线性响应变成幂律（见 Cfg 注释）：gamma=1 是旧行为，
    gamma>1 让小误差的推力急剧下降 —— 这就是「准星越近火控圈、杆量越小」，
    等效于满杆角随距离动态变大。它接管了死区原本的一半职责：
    以前靠 dead 硬截断，现在接近时曲线自己就把推力压下去了。

    为什么必须有这个 0：死区逆变换让 u 恒 ≥ game_deadzone，摇杆**永远
    回不到中心**。游戏据此认为「玩家一直在微调瞄准」，于是拒绝进入开镜
    状态（实测：不暂停就开不了镜，按住 F9 才行）。摇杆回中不是判据，
    是手柄松手的物理事实。
    """
    if err == 0.0 or abs(err) <= dead:
        return 0.0
    f = cfg.full_stick_angle if full is None else full
    t = min(abs(err) / max(f, 1e-6), 1.0)
    if cfg.curve_gamma != 1.0:
        t = t ** cfg.curve_gamma
    g = cfg.game_deadzone
    return math.copysign(g + (1.0 - g) * t, err)


def neutral(st: CtrlState, active: bool = False) -> Stick:
    """只归零输出。Target_Abs 原封不动。"""
    st.out_x = st.out_y = 0.0
    return Stick(active=active, ch_x=st.ch_x, ch_y=st.ch_y)


def step(cfg: Cfg, st: CtrlState, snap: Snap, dx: float, dy: float,
         now: float, view: View, live: bool = True) -> Stick:
    """live=False 表示本帧观测不可用、不推杆，但**鼠标位移照常累加**。

    Target_Abs 是玩家意图的绝对坐标，观测断流、过场动画、全堆扫描
    都不能把它吞掉 —— 否则同样的物理滑行距离会得到不同落点。
    """
    # ---- 当前 FOV 下的投影逆映射（输入增益 + 视觉直径共用）----
    # f_px 是当前透视焦距（像素）。FOV 变大 → 世界被推远 → 同一像素
    # 对应的真实角度变大，而同一角度误差的屏幕像素位移变小。
    fov = snap.fov if snap is not None else 45.0
    half = math.tan(math.radians(max(fov, 1.0)) / 2.0)
    f_px = (view.sw / 2.0) / half
    per_px = math.degrees(1.0 / f_px)
    k = per_px * cfg.sens_scale
    dead = per_px * cfg.dead_px        # 死区按像素折算，任何倍率下同一视觉精度

    # 满杆角度也按 FOV 折算，基准是 FOV=50° 下的 full_stick_angle。
    # 不做这层折算会怎样：开镜 FOV=10° 时每像素只有 0.0052°，12° 的误差
    # 要鼠标滑 2298px —— 比屏幕还宽，玩家「把鼠标推到屏幕外也顶不满」。
    # 折算之后，鼠标在屏幕上滑同样的距离，任何倍率下都给出同样的杆量。
    # 下限 2° 防止极端窄视场把增益放大到发抖。
    ref = math.tan(math.radians(REF_FOV) / 2.0)
    full_eff = max(cfg.full_stick_angle * (half / ref), 2.0)

    # 锚定：首次启动，或玩家按 F2 把准星放回火控圈当前坐标（换关时用）。
    # 这是 Target_Abs 唯一允许被外部改写的入口；闭环内部永不触碰。
    # 必须在累加之前做，否则锚定帧的鼠标位移会被覆盖掉。
    if st.anchor_pending and snap is not None and snap.ok:
        st.abs_yaw = snap.cam_yaw
        st.abs_pitch = snap.cam_pitch
        st.anchor_pending = False

    # ---- Target_Abs：只在这里被改写，任何门控都无权跳过 ----
    st.abs_yaw = (st.abs_yaw + dx * k) % 360.0
    st.abs_pitch = st.abs_pitch - dy * k

    if not live or snap is None or not snap.ok:
        return neutral(st)

    # ---- Error：Aim_Abs 变化只在这里体现，不反向吞噬目标 ----
    err_y = wrap180(st.abs_yaw - snap.cam_yaw)
    err_p = st.abs_pitch - snap.cam_pitch

    ux = drive(err_y, cfg, dead, full_eff)
    uy = drive(err_p, cfg, dead, full_eff)
    st.out_x += (ux - st.out_x) * cfg.smooth
    st.out_y += (uy - st.out_y) * cfg.smooth
    st.err_y, st.err_p = err_y, err_p

    # ---- 准星：Error 的视觉投影，直径随实时 FOV 同步缩放 ----
    # 限幅只作用于显示，绝不回写 Target_Abs。
    ex = clamp(err_y, -DISPLAY_CLAMP, DISPLAY_CLAMP)
    ep = clamp(err_p, -cfg.pitch_limit, cfg.pitch_limit)
    st.ch_x = clamp(view.cx + f_px * math.tan(math.radians(ex)),
                    14.0, view.sw - 14.0)
    st.ch_y = clamp(view.cy - f_px * math.tan(math.radians(ep)),
                    14.0, view.sh - 14.0)

    return Stick(rx=st.out_x, ry=st.out_y, err_y=err_y, err_p=err_p,
                 ch_x=st.ch_x, ch_y=st.ch_y, active=True)

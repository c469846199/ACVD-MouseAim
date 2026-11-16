"""
acvd/core/types.py — 层间唯一允许共享的东西：数据结构。

绝对坐标模型（2026-11-11 定稿，相机版）：
    Target_Abs 是鼠标意图在 360 度滑轨上的绝对坐标，只由鼠标 dx 线性累加。
    Aim_Abs 直接取**固定相机**的 yaw / pitch —— 它是关卡里唯一镜像级不动
    的读数（跨关、跨重启、跨配件都是同一个 guest 地址），既真实反映状态，
    又不需要任何「找地址」的动作。
    准星在屏幕上的位置只是 Error = Target_Abs − Aim_Abs 的视觉投影，
    显示限幅绝不回写 Target_Abs。
"""

from __future__ import annotations

from dataclasses import dataclass

# ---- 对外状态：只有两态 --------------------------------------------------
MODE_OFF = "OFF"                # 未接管 / 读不到 / 暂停，输出恒 0
MODE_TRACKING = "TRACKING"      # 闭环运行

# 兼容旧前端的字符串常量（不再由内核产生）
MODE_LOADING = "LOADING"
MODE_READY = "READY"
MODE_PULSE = "PULSE"


@dataclass(frozen=True)
class Snap:
    """采集层的一帧观测。ok=False = 这一帧没有有效反馈，输出当帧归零。

    只有相机：固定地址、镜像级不变。没有地址、没有副本、
    没有「目标结构」——那些东西运行时一个都不需要。
    """

    seq: int = 0
    t: float = 0.0
    ok: bool = False

    cam_yaw: float = 0.0        # Aim_Abs：视角绝对角（世界角环）
    cam_pitch: float = 0.0      # Aim_Abs_P：视角绝对俯仰（线性轴）
    cam_pos: tuple = (0.0, 0.0, 0.0)   # 相机世界位置：只用于识别「跨关卡传送」
    fov: float = 45.0           # 只驱动输入增益与准星投影，不进闭环

    # 帧率传感器：相机 quat 只会在**渲染帧**被游戏改写，所以「值发生变化」
    # 的时刻就是帧边界，不需要任何额外地址。
    # frame_seq 每次变化 +1；frame_dt 是帧间隔的 EMA（实测 p50 26ms ≈ 38fps，
    # 大场面掉到 130ms）。控制器据此「一帧只更新一次导数」，
    # 而不是拿 8ms 的采样间隔去差分一个 26ms 才变一次的量。
    frame_seq: int = 0
    frame_dt: float = 0.0167


@dataclass(frozen=True)
class Cmd:
    """决策层指令。Output 只认 mode。"""

    seq: int = 0
    t: float = 0.0
    mode: str = MODE_OFF
    reason: str = ""


@dataclass
class CtrlState:
    """控制器状态：一个滑轨坐标 + 上一帧输出。

    **abs_yaw / abs_pitch 是禁区**：除「锚定」（首次启动 / 玩家按 F2）
    和鼠标 dx/dy 外，任何逻辑都不得读写它们。Aim_Abs 变化只改变 Error，
    不改变 Target_Abs。
    """

    anchor_pending: bool = True   # 待锚定：首次启动，或 F2 手动锚定
    abs_yaw: float = 0.0        # Target_Abs X（360 度环）
    abs_pitch: float = 0.0      # Target_Abs Y（线性滑轨，不 clamp）

    out_x: float = 0.0
    out_y: float = 0.0
    err_y: float = 0.0
    err_p: float = 0.0
    ch_x: float = 0.0          # 准星：纯视觉投影
    ch_y: float = 0.0


@dataclass(frozen=True)
class View:
    """视口几何，只用于把 Error 投影成屏幕坐标。"""

    cx: float = 960.0
    cy: float = 540.0
    sw: float = 1920.0
    sh: float = 1080.0
    hfov: float = 45.0

    @property
    def f(self) -> float:
        import math
        return (self.sw / 2.0) / math.tan(math.radians(max(self.hfov, 1.0)) / 2.0)


@dataclass(frozen=True)
class Cfg:
    """仅 5 个参数，全部是物理量或手感，没有一个是「判定」。"""

    sens_scale: float = 1.0         # 手感倍率；真实灵敏度由实时 FOV 逆投影给出
    # 误差多少度给满杆。这是唯一的阻尼旋钮，也是「跟手 vs 稳」的唯一杠杆：
    #     ζ = 1 / (2√(K·τ))，K = 54/full，τ≈0.15s
    #     8°  → K=6.75  ζ=0.50  超调 1.67°（跟手最好，冲过头明显）
    #     12° → K=4.50  ζ=0.61  超调 0.92°
    #     24° → K=2.25  ζ=0.86  超调 0.05°（最稳，但小误差响应慢一倍）
    # 实际值以 tune.json 为准（mapper 启动时覆盖；管道 fullstick N 可热改）。
    full_stick_angle: float = 12.0
    game_deadzone: float = 0.44     # 执行器物理死区，用于补偿（不是判定）
    smooth: float = 1.0             # 输出平滑，1.0 = 不平滑
    pitch_limit: float = 70.0       # 仅用于准星显示限幅，绝不缩减 Target_Abs
    # 响应曲线指数：杆量 = gdz + (1-gdz) · (|err|/full)^gamma
    #
    #     gamma = 1.0  线性（旧行为）：误差 0.5° 时也有半杆的一半，冲过去
    #     gamma = 2.0  平方：小误差的推力急剧下降，接近目标自然减速
    #     gamma = 3.0  更慢，只在很近时才给足
    #
    # 这是「按需推杆」的数学形式：远处给满，近处只给一点点。
    # 它同时把死区的职责接管了一半 —— 以前靠 dead_px 强行截断，
    # 现在曲线本身就会在接近时把杆量压到很小。
    curve_gamma: float = 1.0

    # 「误差小到这个程度就把摇杆真正放回中心」——单位是**屏幕像素**，
    # 不是角度：开镜 FOV=10° 时 0.25° 等于 48 像素，用角度做死区会直接
    # 把狙击精度吃掉。按像素折算后，任何倍率下都停在同一视觉精度上。
    #
    # 取值原则：**它应该等于火控圈的半径**。玩家是拿圈套人，不是拿点打人；
    # 目标在圈内就不需要再动，所以死区 = 圈半径时，抖动归零而命中精度不变。
    # 准星画面上那个绿圈就是用这个值画的，二者永远一致。
    #
    # 没有它不仅会留下稳态误差，更要命的是：死区补偿让 u 恒 ≥0.44，
    # 摇杆**永远回不到中心**，游戏就把「一直有瞄准输入」当成玩家在微调，
    # 拒绝进入开镜状态 —— 实测表现就是「按住 F9 暂停才能开镜」。
    dead_px: float = 80.0


@dataclass
class Stick:
    rx: float = 0.0
    ry: float = 0.0
    err_y: float = 0.0
    err_p: float = 0.0
    ch_x: float = 0.0
    ch_y: float = 0.0
    active: bool = False


@dataclass(frozen=True)
class OutFrame:
    seq: int = 0
    t: float = 0.0
    mode: str = MODE_OFF
    rx: float = 0.0
    ry: float = 0.0
    active: bool = False
    err_y: float = 0.0
    err_p: float = 0.0
    ch_x: float = 0.0
    ch_y: float = 0.0

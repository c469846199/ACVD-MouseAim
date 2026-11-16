"""acvd/core — 绝对坐标瞄准引擎（固定相机版）。

    from acvd.core import AimEngine

层（严格单向依赖，由 acvd/core/guard.py 在启动时强制检查）：

    ① sensor.py   采集：每帧读固定相机的 yaw / pitch / FOV
    ② control.py  控制器（纯函数）
    ③ output.py   硬件输出（合成 + 看门狗）
       engine.py  装配层（开关 + 装配）

没有「定位层」也没有「决策状态机」：相机是镜像级固定地址，
不需要扫描、簇、缓存、判据；状态只有开/关一个布尔。
"""

from .control_v2 import V2Cfg
from .engine import AimEngine
from .guard import ArchViolation, enforce
from .types import Cfg, MODE_OFF, MODE_READY, MODE_TRACKING

__all__ = ["AimEngine", "Cfg", "V2Cfg", "MODE_OFF", "MODE_READY",
           "MODE_TRACKING", "enforce", "ArchViolation"]

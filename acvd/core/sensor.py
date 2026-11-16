"""
acvd/core/sensor.py — 数据采集层（线程 A）

只做一件事：每帧读**固定相机**的 yaw / pitch / FOV，发布 Snap。

为什么只有相机：
    相机 `0x405EA750`（FOV `0x405EA770`）是镜像级固定地址 —— 跨关卡、
    跨 Xenia 重启、跨火控配件都不变，且每帧更新。它真实反映「视角此刻
    朝哪」，是唯一既能当反馈、又不需要任何「找地址」动作的读数。

    没有扫描、没有簇、没有副本、没有缓存、没有定位请求。读不到就是读
    不到（游戏退出/加载中），输出层当帧归零，下一帧自然恢复。
"""

from __future__ import annotations

import math
import struct
import threading
import time

from ..gmem import GuestMem
from .chan import Slot
from .types import Snap

FAST_HZ = 250.0
SLOW_HZ = 50.0

# 镜像级固定地址（guest 空间）。穿刺实测：跨关、跨重启、跨配件都不变。
#
# ⚠ 这是 **guest 地址**，读之前必须加 guest base（0x1_0000_0000）换成宿主
# 地址。2026-11-12 修：此前这里直接把 guest 地址丢给 ReadProcessMemory，
# 读的是未映射的宿主 0x405EA750，于是快照永远 ok=False —— 面板显示
# 「已接管」，实际一帧都没推过杆，鼠标动而火控圈不动。
CAM_QUAT_GUEST = 0x405EA750
CAM_FOV_GUEST = 0x405EA770


# ============================================================================
# 相机结构解析（原先在 acvd/camera.py，这里内联）
#
# 为什么搬进来：camera.py 顶部 `import numpy as np`，而它只被一个 5 行的
# read_cam 薄封装用到、那个封装又只被本文件用到。为了读一个固定地址的
# 四元数，把 24MB 的 numpy 拖进依赖链不值得 —— 整个相机解析其实只有
# 下面这三十行纯 struct/math 代码，一个 numpy 函数都不需要。
#
# 内联之后依赖链变成：
#     sensor → gmem → winmem        （全部是 stdlib + ctypes）
# 而不是：
#     sensor → scene → camera → numpy
# ============================================================================

# 从 FOV 地址反推结构起点要退多少字节。
#
# 相机结构的相对布局（都以 FOV 地址为基准，实测）：
#     fov_addr - 0x30 .. -0x24   世界坐标 xyz（3 × f32）
#     fov_addr - 0x24 .. -0x20   w 校验位（必须 ≈1.0）
#     fov_addr - 0x20 .. -0x10   朝向四元数 xyz w（4 × f32）
#     fov_addr - 0x10 .. +0x00   缩放（4 × f32，前三个应在 0.5~2.0）
#     fov_addr + 0x00            视场角（f32）
#
# 所以一次读 0x30 字节，起点是 fov_addr - 0x30。
# ⚠ 这里踩过坑：内联早期误把起点写成 -0x10，于是把 FOV 字段（45.0）
# 当成了四元数的 x 分量，模长校验必然失败，读到的永远是 None ——
# 表现是 ok=0 / 摇杆不动 / 准星不出现。
_CAM_READ_BACK = 0x30


def _quat_to_yaw_pitch(q):
    """四元数 → (yaw, pitch)，单位化不成立时返回 None。

    正向白名单写法是刻意的：NaN 的所有比较都返回 False，写成
    「不合法就拒绝」的正向形式才能拦掉 NaN。反向写法（`if bad: return`）
    会放行 NaN，历史上因此出现过火控圈一直左漂的事故。
    """
    x, y, z, w = q
    n = x * x + y * y + z * z + w * w
    if not (abs(n - 1.0) <= 0.002):
        return None
    yaw = math.degrees(
        math.atan2(2 * (w * y + x * z), 1 - 2 * (y * y + z * z)))
    pit = math.degrees(
        math.asin(max(-1.0, min(1.0, 2 * (w * x - y * z)))))
    return yaw, pit


def read_cam(proc, fov_addr: int):
    """按 **FOV 地址** 读一个相机，返回 (yaw, pitch, pos) 或 None。

    参数是 `CAM_FOV_GUEST`（即 0x405EA770），不是四元数地址 ——
    函数内部会退到结构起点。调用方不要再自己减偏移。

    三重校验（w≈1、缩放范围、四元数模长）任一不成立就返回 None：
    地址固定不代表内容固定，换关/加载中这块内存可能是别的东西。
    """
    if not fov_addr:
        return None
    raw = proc.read(fov_addr - _CAM_READ_BACK, _CAM_READ_BACK)
    if not raw or len(raw) < _CAM_READ_BACK:
        return None
    try:
        pos = struct.unpack(">3f", raw[0:12])
        pw = struct.unpack(">f", raw[12:16])[0]
        q = struct.unpack(">4f", raw[16:32])
        sc = struct.unpack(">4f", raw[32:48])
    except struct.error:
        return None
    if abs(pw - 1.0) > 1e-3:
        return None
    if not all(abs(v) > 0.5 and abs(v) < 2.0 for v in sc[:3]):
        return None
    yp = _quat_to_yaw_pitch(q)
    if yp is None:
        return None
    return yp[0], yp[1], pos


class Sensor(threading.Thread):
    def __init__(self, get_proc, snap_slot: Slot, log=print):
        super().__init__(name="sensor", daemon=True)
        self.get_proc = get_proc
        self.snap_slot = snap_slot
        self.log = log

        self.running = True
        self._fov = 45.0
        self._seq = 0
        self._alive = False
        self._stats = {"reads": 0, "slow": 0, "max_ms": 0.0, "bad": 0}
        self._pid = None
        self._base = 0
        self._confirmed = False
        self._fails = 0
        self._next_probe = 0.0
        # 帧率传感器：相机值变化 = 渲染帧边界
        self._frame_seq = 0
        self._frame_dt = 1.0 / 60.0
        self._last_key = None
        self._last_change = 0.0

    def stats(self) -> dict:
        return dict(self._stats)

    def fov(self) -> float:
        """最近一次读到的实时 FOV（面板诊断用）。"""
        return self._fov

    def proc_alive(self) -> bool:
        """游戏进程在不在。供「显示是否真的在闭环」用，不是判据：
        进程在 = 可能读到相机（加载中只是暂时读不到），进程不在 = 必然读不到。
        """
        return self._alive

    def base(self) -> int:
        """当前使用的 guest 基址（面板诊断用）。"""
        return self._base

    def frame_dt(self) -> float:
        """最近的渲染帧周期（秒，EMA）。相机值变化间隔就是帧周期。"""
        return self._frame_dt

    def fps(self) -> float:
        return 1.0 / self._frame_dt if self._frame_dt > 1e-6 else 0.0

    def _host_base(self, proc) -> int:
        """guest → 宿主 的基址。

        **验证方式就是「读相机」本身**：base 对了，0x405EA750 处必然是一个
        单位四元数；base 错了必然读不到或读出垃圾。

        为什么不再用 MZ/PE 校验（2026-11-12 事故）：Xenia 的 guest 内存有
        多个映射视图，MZ/PE 在 **6 个不同 base** 上都成立；而且 mapper 比
        游戏早启动几秒时映像还没加载完，KNOWN_BASE 校验会失败 → 枚举撞上
        0x2C92F970000 这种假 base → 相机永远读不到 → 闭环空转、准星不显示。
        所以：先认 KNOWN_BASE，每 0.5s 用相机验证，读到就锁定；连续 10 次
        都失败才枚举，且枚举也只用「相机能读通」作为判据。
        """
        pid = getattr(proc, "pid", 0)
        if pid != self._pid:
            self._pid = pid
            self._base = GuestMem.KNOWN_BASE
            self._confirmed = False
            self._fails = 0
            self._next_probe = 0.0

        if self._confirmed:
            return self._base

        now = time.perf_counter()
        if now < self._next_probe:
            return self._base
        self._next_probe = now + 0.5

        if self._base_ok_now(proc, self._base):
            self._confirmed = True
            self._fails = 0
            self.log(f"[采集] guest base = 0x{self._base:012X}"
                     f"（相机读通验证）")
            return self._base

        self._fails += 1
        if self._fails >= 10:
            alt = self._scan_base(proc)
            if alt is not None:
                self._base = alt
                self._confirmed = True
                self._fails = 0
                self.log(f"[采集] guest base = 0x{alt:012X}"
                         f"（枚举 + 相机验证）")
        return self._base

    @staticmethod
    def _quat_plausible(raw) -> bool:
        """16 字节是不是一个像样的单位四元数。就是「相机读通」的判据。"""
        if not raw or len(raw) < 16:
            return False
        try:
            x, y, z, w = struct.unpack(">4f", raw)
        except struct.error:
            return False
        if not all(abs(v) <= 2.0 for v in (x, y, z, w)):
            return False
        n = x * x + y * y + z * z + w * w
        return abs(n - 1.0) <= 0.02

    def _base_ok_now(self, proc, base: int) -> bool:
        try:
            return self._quat_plausible(proc.read(base + CAM_QUAT_GUEST, 16))
        except Exception:
            return False

    def _scan_base(self, proc):
        """兜底：枚举候选 base，逐个用「相机能读通」验证。

        只在前 10 次 KNOWN_BASE 验证全失败时才跑一次（约 5 秒），
        不会每帧枚举。
        """
        try:
            from ..gmem import _regions
            img = GuestMem.GUEST_IMAGE
            seen = set()
            for base, _size, _st, _pr, alloc in _regions(proc._h):
                for cand in (base - img, alloc - img, alloc - 0x40000000):
                    if cand < 0 or cand in seen:
                        continue
                    seen.add(cand)
                    if self._base_ok_now(proc, cand):
                        return cand
        except Exception:
            pass
        return None

    def stop(self) -> None:
        self.running = False

    # ---- 主循环 -------------------------------------------------------
    def run(self) -> None:
        while self.running:
            t0 = time.perf_counter()
            hz = FAST_HZ
            try:
                hz = self._tick()
            except Exception as e:
                self.log(f"[采集] 异常（已隔离）: {e}")
                hz = SLOW_HZ
            dt = 1.0 / hz - (time.perf_counter() - t0)
            if dt > 0:
                time.sleep(dt)

    def _tick(self) -> float:
        proc = self.get_proc()
        self._alive = proc is not None
        if proc is None:
            self._publish(Snap(seq=self._next(), t=time.perf_counter(),
                               ok=False, fov=self._fov))
            return SLOW_HZ

        t0 = time.perf_counter()

        base = self._host_base(proc)
        cam = read_cam(proc, base + CAM_FOV_GUEST)
        if cam is None:                     # 撕裂读：重试一次
            cam = read_cam(proc, base + CAM_FOV_GUEST)
        if cam is None:
            self._stats["bad"] += 1
        else:
            self._tick_frame(cam)

        # 每帧读实时 FOV：只驱动输入增益与准星投影，不进闭环。
        # 下限必须够低：主菜单的世界地图/选关界面会把 FOV 压到 8.5° 做放大，
        # 原先卡 15.0 会把它当脏数据丢掉，于是继续拿旧的 45° 算焦距 ——
        # 投影与增益全错，准星就飘到屏幕外。
        raw = proc.read(base + CAM_FOV_GUEST, 4)
        if raw and len(raw) >= 4:
            v = struct.unpack(">f", raw)[0]
            if v == v and 5.0 <= v <= 130.0:
                self._fov = v

        dur = time.perf_counter() - t0
        self._stats["reads"] += 1
        self._stats["max_ms"] = max(self._stats["max_ms"], dur * 1000.0)
        if dur > 1.0 / FAST_HZ * 2:
            self._stats["slow"] += 1

        self._publish(Snap(
            seq=self._next(), t=time.perf_counter(), ok=cam is not None,
            cam_yaw=cam[0] if cam else 0.0,
            cam_pitch=cam[1] if cam else 0.0,
            cam_pos=cam[2] if cam else (0.0, 0.0, 0.0),
            fov=self._fov,
            frame_seq=self._frame_seq,
            frame_dt=self._frame_dt,
        ))
        return FAST_HZ

    def _tick_frame(self, cam) -> None:
        """相机 yaw/pitch 变了 = 游戏出了新的一帧。

        为什么要这个：相机按**渲染帧**写，我们按 250Hz 读 —— 同一个值会重复
        3~5 次。拿 8ms 的采样间隔去差分一个 26ms 才变一次的量，会得到
        「0, 0, 3 倍, 0, 0, 3 倍」的锯齿，V2 的角速度补偿就被这个锯齿污染。
        用真实帧间隔微分才是对的。

        静止时相机不变 → 不增 seq，控制器保持上一次的导数（而不是每帧重复
        做零差分）。长时间不变由控制器按超时归零。
        """
        key = (cam[0], cam[1])
        now = time.perf_counter()
        if self._last_key is not None and key != self._last_key:
            gap = now - self._last_change
            # 0.004~0.25s 之间才当正常帧间隔；超出（过场、加载、极小帧率）
            # 只记边界，不污染平均值
            if 0.004 < gap < 0.25:
                self._frame_dt += (gap - self._frame_dt) * 0.15
            self._last_change = now
            self._frame_seq += 1
        self._last_key = key
        if self._last_change == 0.0:
            self._last_change = now

    def _next(self) -> int:
        self._seq += 1
        return self._seq

    def _publish(self, snap: Snap) -> None:
        self.snap_slot.put(snap)

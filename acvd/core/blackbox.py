"""
acvd/core/blackbox.py — 125Hz 环形黑匣子

为什么要它：
    抖动、过冲、τ 这些东西全部发生在 100ms 尺度上。管道查一次 status 要
    几毫秒，只能 10Hz —— 那个分辨率下 5~10Hz 的振荡会直接混叠成噪声。

    所以把 Composer 每帧真正发出去的杆量、误差、相机角度，写进一个固定
    长度的环形缓冲（不落盘、不占 IO），出问题时一条命令 dump 成 CSV。

记录内容（每帧一行）：
    t        perf_counter 时间戳
    mode     TRACKING / OFF
    rx, ry   真正推出去的杆量（-1~1，已含平滑）
    err_y/p  视觉误差（Target_Abs − 相机）
    ch_x/y   准星屏幕坐标
    cam_yaw/pitch  本帧相机读数
    fov      本帧 FOV
    tau      本帧使用的补偿时间常数（辨识用）

容量 20000 帧 ≈ 125Hz × 160 秒，覆盖一整局绰绰有余。
"""

from __future__ import annotations

import csv
import os
import time
from collections import deque

FIELDS = ["t", "mode", "rx", "ry", "err_y", "err_p",
          "ch_x", "ch_y", "cam_yaw", "cam_pitch", "fov", "tau"]

DEFAULT_CAP = 20000


class Blackbox:
    def __init__(self, cap: int = DEFAULT_CAP):
        self.buf: deque = deque(maxlen=cap)

    def push(self, **kw) -> None:
        self.buf.append(kw)

    def __len__(self) -> int:
        return len(self.buf)

    def dump(self, path: str | None = None) -> str:
        """写 CSV，返回路径。空缓冲返回空串。"""
        if not self.buf:
            return ""
        if path is None:
            path = os.path.join(
                os.getcwd(),
                time.strftime("acvd_trace_%Y%m%d_%H%M%S.csv"))
        with open(path, "w", newline="", encoding="utf-8") as fh:
            wr = csv.writer(fh)
            wr.writerow(FIELDS)
            # 必须先快照：Composer 在另一个线程里持续 append，
            # 直接迭代 deque 会抛 "deque mutated during iteration"，
            # 导出的 CSV 会截断在半路（第一次 dump 就是这么坏的）。
            for r in list(self.buf):
                row = []
                for k in FIELDS:
                    v = r.get(k, "")
                    row.append(f"{v:.5f}" if isinstance(v, float) else v)
                wr.writerow(row)
        return path

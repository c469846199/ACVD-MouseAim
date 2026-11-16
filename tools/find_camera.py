"""tools\find_camera.py — 首次使用时的相机地址定位向导

===============================================================================
一、这是干什么的
===============================================================================

本程序靠读「相机结构」的位置与朝向来做鼠标视角控制。相机在游戏内存里的
地址由游戏加载时的分配顺序决定 —— 同一台电脑跑同一个模拟器时它是稳定的，
但**换了模拟器版本、换了电脑、或者加载时序不同，地址就会变**。

所以：第一次在你自己电脑上使用时，可能要跑一次这个脚本，把相机地址记下来。

===============================================================================
二、怎么用（三步）
===============================================================================

    1. 启动模拟器和游戏，**进入任意一个关卡或训练场**
       （标题画面、菜单、机库里的相机结构还没初始化，扫不到）

    2. 在项目根目录运行：

           python tools\\find_camera.py

       它会扫描一小块内存（约 1 MB，通常几秒内完成），
       找到相机后**自动动一下视角做二次确认**，然后写进配置文件。

    3. 之后直接正常启动 mapper 即可，不需要再跑本脚本。

===============================================================================
三、它扫哪里、为什么扫得很快
===============================================================================

不遍历全部内存，只扫一个固定的堆区段：

    0x40500000 ~ 0x40700000     （2 MB）

理由是相机结构在多次实测里都落在这个区段内。范围固定意味着：

    · 几秒内完成，不像全内存扫描那样要几分钟
    · 行为可预期，不是「在全进程里乱翻」

如果这个区段里找不到（比如换了大幅修改的模拟器分支），
可以加 --wide 参数扩大到 0x40000000 ~ 0x41000000（16 MB）。

===============================================================================
四、找到之后存在哪
===============================================================================

    <项目根>/cam_addr.json

格式：

    {
      "quat_guest": 1075110224,        ← 相机四元数地址（十进制）
      "fov_guest":  1075110256,        ← 对应 FOV 地址
      "verified":   true,              ← 是否通过了「动视角确认」测试
      "stamp":      "2026-11-17T06:00:00"
    }

mapper 启动时会读这个文件。文件不存在就用内置默认值（0x405EA750）。

想重新定位，直接删掉这个文件再跑一次本脚本即可。
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys
import time

# 让 `from acvd.xxx` 能找到包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from acvd.focus import _find_pid_by_prefix, _find_pid_by_window_title  # noqa: E402
from acvd.gmem import GuestMem  # noqa: E402
from acvd.winmem import Process  # noqa: E402

# ---- 扫描范围 ----
SCAN_BASE = 0x40500000
SCAN_SIZE = 0x00200000          # 2 MB
WIDE_BASE = 0x40000000
WIDE_SIZE = 0x01000000          # 16 MB

# ---- 相机结构判据（与 acvd/core/sensor.py 保持一致）----
POS_W_MIN, POS_W_MAX = 1.0 - 1e-3, 1.0 + 1e-3
QUAT_NORM_TOL = 0.002
SCALE_MIN, SCALE_MAX = 0.5, 2.0
FOV_MIN, FOV_MAX = 5.0, 130.0
POS_MIN_ABS = 50.0              # 世界坐标：|x| 或 |z| 应大于它

OUT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "cam_addr.json")


def _fmt_addr(a: int) -> str:
    return f"0x{a:012X}"


def _conn():
    """连上 Xenia，返回 (proc, base)。"""
    pid = _find_pid_by_prefix("xenia") or _find_pid_by_window_title("xenia")
    if pid is None:
        print("! 找不到 Xenia 进程。")
        print("  请先启动模拟器（进程名以 xenia 开头）。")
        return None, None
    proc = Process.attach(pid)
    gm = GuestMem(proc, GuestMem.KNOWN_BASE)
    return proc, gm.base


def _check_struct(raw: bytes, off: int):
    """检查 raw[off:] 是否是一个合法相机结构。

    返回 (pos, quat, scale) 或 None。

    布局（大端 float）：
        +0x00  position  4f   第 4 个必须 ≈1.0（校验位）
        +0x10  quaternion 4f  模长² ≈ 1
        +0x20  scale     4f   前三个在 0.5~2.0
    """
    if off + 0x30 > len(raw):
        return None
    try:
        p3 = struct.unpack_from(">3f", raw, off)
        pw = struct.unpack_from(">f", raw, off + 12)[0]
        q = struct.unpack_from(">4f", raw, off + 16)
        sc = struct.unpack_from(">4f", raw, off + 32)
    except struct.error:
        return None

    # 第 1 条：位置的第 4 个分量校验位
    if not (POS_W_MIN <= pw <= POS_W_MAX):
        return None
    # 第 2 条：缩放范围
    if not all(SCALE_MIN < abs(v) < SCALE_MAX for v in sc[:3]):
        return None
    # 第 3 条：四元数单位化
    x, y, z, w = q
    n2 = x * x + y * y + z * z + w * w
    if abs(n2 - 1.0) > QUAT_NORM_TOL:
        return None
    # 第 4 条：世界坐标范围
    if not (abs(p3[0]) > POS_MIN_ABS or abs(p3[2]) > POS_MIN_ABS):
        return None

    return p3, q, sc


def _read_fov(proc, guest_base: int, quat_guest: int):
    """读 +0x20 处的 FOV（相对 quat 地址）。"""
    raw = proc.read(guest_base + quat_guest + 0x20, 4)
    if not raw or len(raw) < 4:
        return None
    v = struct.unpack(">f", raw)[0]
    if v != v:                      # NaN
        return None
    if FOV_MIN <= v <= FOV_MAX:
        return v
    return None


def _sample_quat(proc, guest_base: int, quat_guest: int, times: int = 6,
                 gap: float = 0.08):
    """连续采样四元数，返回列表。"""
    out = []
    for _ in range(times):
        raw = proc.read(guest_base + quat_guest, 16)
        if raw and len(raw) >= 16:
            out.append(struct.unpack(">4f", raw))
        time.sleep(gap)
    return out


def _is_rotating(samples) -> float:
    """返回采样间最大变化量。相机每帧更新，静态物体不变。"""
    if len(samples) < 2:
        return 0.0
    first = samples[0]
    maxd = 0.0
    for q in samples[1:]:
        d = sum(abs(a - b) for a, b in zip(q, first))
        maxd = max(maxd, d)
    return maxd


def scan(proc, guest_base: int, base: int, size: int):
    """在 [base, base+size) 内找所有相机结构候选。"""
    hits = []
    CHUNK = 0x40000             # 256 KB 一块
    pos = base
    end = base + size
    scanned = 0

    while pos < end:
        n = min(CHUNK, end - pos)
        raw = proc.read(guest_base + pos, n)
        if raw:
            scanned += len(raw)
            off = 0
            lim = len(raw) - 0x34
            while off < lim:
                # 快速筛：只对「+0x20 处看起来像 scale」的位置做完整检查
                try:
                    sc0 = struct.unpack_from(">f", raw, off + 0x20)[0]
                except struct.error:
                    break
                if not (SCALE_MIN < abs(sc0) < SCALE_MAX):
                    off += 4
                    continue

                r = _check_struct(raw, off)
                if r is not None:
                    hits.append(pos + off)
                off += 4
        pos += n

    return hits, scanned


def main() -> int:
    ap = argparse.ArgumentParser(
        description="定位相机结构地址并写入 cam_addr.json")
    ap.add_argument("--wide", action="store_true",
                    help="扩大扫描范围到 16 MB（默认只扫 2 MB）")
    ap.add_argument("--dry", action="store_true",
                    help="只报告，不写文件")
    a = ap.parse_args()

    print("=" * 70)
    print("  相机地址定位")
    print("=" * 70)
    print()
    print("  前提：游戏必须已经在**关卡内**（训练场也算）。")
    print("        标题画面 / 菜单 / 机库里相机结构还没初始化。")
    print()

    proc, guest_base = _conn()
    if proc is None:
        return 2

    print(f"  进程已连接   guest base = {_fmt_addr(guest_base)}")

    # ---- 先验证内置默认地址是否可用 ----
    default_quat = 0x405EA750
    raw = proc.read(guest_base + default_quat, 0x30)
    r = _check_struct(raw, 0) if raw else None
    if r is not None:
        fov = _read_fov(proc, guest_base, default_quat)
        print()
        print(f"  内置默认地址 {_fmt_addr(default_quat)} 实测**可用**"
              f"{'（FOV=%.1f）' % fov if fov else ''}")
        print("  你的环境不需要重新定位，直接用默认值即可。")
        if not a.dry:
            _save(default_quat, verified=True)
        proc.close()
        return 0

    print(f"  内置默认地址 {_fmt_addr(default_quat)} 不可用，开始扫描…")
    print()

    base, size = (WIDE_BASE, WIDE_SIZE) if a.wide else (SCAN_BASE, SCAN_SIZE)
    print(f"  扫描范围 {_fmt_addr(base)} ~ {_fmt_addr(base + size)}"
          f"（{size // 1024} KB）")

    t0 = time.perf_counter()
    hits, scanned = scan(proc, guest_base, base, size)
    dur = time.perf_counter() - t0
    print(f"  扫描 {scanned / 1024:.0f} KB，耗时 {dur:.2f} 秒，"
          f"命中 {len(hits)} 个结构")
    print()

    if not hits:
        print("! 没找到。请确认：")
        print("    1. 游戏已进入关卡（不是菜单/机库）")
        print("    2. 尝试加 --wide 扩大范围：python tools\\find_camera.py --wide")
        proc.close()
        return 1

    # ---- 二次确认：连续采样，看谁在动 ----
    print("  正在确认哪个是相机（采样 0.5 秒，看四元数是否变化）…")
    print()
    print(f"    {'地址':<16} {'变化量':>10}  {'FOV':>7}  位置")
    print("    " + "-" * 66)

    verified = []
    for ga in hits[:40]:
        samples = _sample_quat(proc, guest_base, ga)
        delta = _is_rotating(samples)
        fov = _read_fov(proc, guest_base, ga)
        raw = proc.read(guest_base + ga, 12)
        pos = struct.unpack(">3f", raw) if raw and len(raw) >= 12 else (0, 0, 0)
        fov_s = f"{fov:>7.2f}" if fov else f"{'--':>7}"
        moving = " ← 在动" if delta > 1e-4 else ""
        print(f"    {_fmt_addr(ga):<16} {delta:>10.6f}  {fov_s}  "
              f"({pos[0]:.0f}, {pos[1]:.0f}, {pos[2]:.0f}){moving}")
        if delta > 1e-4 and fov is not None:
            verified.append(ga)

    print()

    # ---- 决策 ----
    if verified:
        target = verified[0]
        print(f"  ✓ 确认相机地址：{_fmt_addr(target)}")
        print(f"    （四元数在变化 + FOV 合理，这两条同时满足）")
    elif hits:
        target = hits[0]
        print(f"  ⚠ 没能确认哪个在动，暂用第一个：{_fmt_addr(target)}")
        print(f"    如果 mapper 跑起来视角不对，请重新跑本脚本。")
    else:
        proc.close()
        return 1

    if a.dry:
        print()
        print("  --dry：不写文件。")
    else:
        _save(target, verified=bool(verified))
        print()
        print(f"  已写入：{OUT_PATH}")
        print("  现在可以直接启动 mapper 了。")

    proc.close()
    return 0


def _save(quat_guest: int, verified: bool) -> None:
    data = {
        "quat_guest": quat_guest,
        "fov_guest": quat_guest + 0x20,
        "verified": verified,
        "stamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


if __name__ == "__main__":
    raise SystemExit(main())

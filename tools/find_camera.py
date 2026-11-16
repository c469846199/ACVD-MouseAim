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

def _project_root() -> str:
    """返回配置文件的落点目录。

    ## 为什么不能直接用 __file__ 往上走

    源码模式：本文件在 <项目根>/tools/，往上两级就是项目根 ✓

    EXE 模式（PyInstaller onedir）：本文件会被打进
    <exe目录>/_internal/find_camera.py，往上两级是 _internal ——
    而配置要写在 **exe 同级**（用户能看见、能删掉重定位的地方）。
    所以 frozen 时取 sys.executable 的目录。

    判断依据 `sys.frozen` 是 PyInstaller 注入的，源码模式没有这个属性。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


OUT_PATH = os.path.join(_project_root(), "cam_addr.json")


# ============================================================================
# Xenia 输入配置修正
#
# ## 为什么定位脚本要管这件事
#
# 相机地址对了、mapper 也在推杆，但游戏里「键盘鼠标全都没反应」——
# 这个坑实测踩过，根因在 Xenia 侧的输入后端选错：
#
#     hid = "any"            输入后端不确定，可能没选到 XInput 设备
#     keyboard_mode = 0/1/2  键盘模拟手柄，会和虚拟手柄抢同一个端口
#
# 正确配置是：
#
#     hid = "xinput"         只用 XInput，也就是我们创建的虚拟手柄
#     keyboard_mode = 0      键盘交给 mapper 处理，Xenia 自己不碰
#
# ## 为什么改完必须重启 Xenia
#
# Xenia 运行期间和退出时都会把内存里的 cvar 写回配置文件。所以在它
# 运行时改文件，改动会被覆盖（实测：改完启动，文件立刻变回 "any"）。
# 只能先关掉它。
# ============================================================================

def _find_xenia_config() -> str | None:
    """从运行中（或刚退出）的 Xenia 进程定位它的配置文件。

    优先用进程的 exe 路径 → 同目录找 *.config.toml。
    进程不在时，尝试常见位置。
    """
    candidates = []

    # 1. 从进程拿 exe 路径（最准）
    pid = _find_pid_by_prefix("xenia") or _find_pid_by_window_title("xenia")
    if pid is not None:
        import subprocess
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-Process -Id {pid} -EA SilentlyContinue).Path"],
                capture_output=True, text=True, timeout=10)
            exe = r.stdout.strip()
            if exe and os.path.exists(exe):
                d = os.path.dirname(exe)
                candidates.append(d)
        except Exception:
            pass

    # 2. 常见便携位置：本程序旁边、上一级
    root = _project_root()
    candidates += [root, os.path.dirname(root)]

    for d in candidates:
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for n in names:
            if n.endswith(".toml") and "config" in n.lower():
                return os.path.join(d, n)
    return None


def _read_text(p: str) -> str:
    with open(p, encoding="utf-8", errors="ignore") as f:
        return f.read()


def ensure_xenia_input(verbose: bool = True, auto_fix: bool = False) -> int:
    """检查（可选修正）Xenia 的输入配置。

    返回：
        0  配置可用（无需改动）
        1  已修正（需要重启 Xenia 生效）
        2  找不到配置文件
        3  修正失败 / 无法安全修正

    ## 判断标准：什么算「可用」

        hid
            "any"        ✓ 自动尝试所有后端，实测能识别 XInput 设备
            "xinput"     ✓ 明确指定，最直接
            "sdl"        ✓ 能识别 XInput 设备（Edge 版就用这个）
            "winkey"     ✗ 只用键盘模拟，收不到虚拟手柄
            "nop"        ✗ 禁用输入

        keyboard_mode
            0 (Disabled)    ✓ 键盘全权交给 mapper
            1 (Enabled)     ⚠ Xenia 自己把键盘映射成手柄，会和虚拟手柄
                              抢同一个端口，表现是「按键触发两次」
            2 (Passthrough) ⚠ 同上，且需要已连接手柄

    ## 为什么默认不自动改（auto_fix=False）

    实测踩过：hid="any" 在某些环境下本来就是能用的。擅自改成 "xinput"
    属于「在你不知道的地方改你的东西」，而且 Xenia 运行期间改文件会被
    它的内存值覆盖，看上去像改成功了其实没变。

    所以默认只**报告**，把判断权还给用户。确实需要改时加 --fix-input。

    ## 为什么改完必须重启 Xenia

    Xenia 运行期间和退出时都会把内存里的 cvar 写回配置文件。所以在它
    运行时改文件，改动会被覆盖（实测：改完启动，文件立刻变回 "any"）。
    """
    cfg = _find_xenia_config()
    if cfg is None:
        if verbose:
            print("  ⚠ 找不到 Xenia 的配置文件（.toml）")
            print("    如果是便携版，配置文件应该在 exe 同级目录。")
        return 2

    if verbose:
        print(f"  Xenia 配置: {cfg}")

    try:
        text = _read_text(cfg)
    except OSError as e:
        if verbose:
            print(f"  ⚠ 读不了配置文件: {e}")
        return 3

    import re

    m_hid = re.search(r'(?m)^\s*hid\s*=\s*"([^"]*)"', text)
    m_kb = re.search(r'(?m)^\s*keyboard_mode\s*=\s*(\d+)', text)

    cur_hid = m_hid.group(1) if m_hid else None
    cur_kb = int(m_kb.group(1)) if m_kb else None

    if verbose:
        print(f"  当前 hid           = {cur_hid!r}")
        print(f"  当前 keyboard_mode = {cur_kb}")

    HID_OK = ("any", "xinput", "sdl")
    hid_ok = cur_hid in HID_OK
    kb_ok = (cur_kb == 0)

    if hid_ok and kb_ok:
        if verbose:
            print("  ✓ 输入配置可用")
            if cur_hid == "any":
                print("    （hid=any 会自动选后端，实测能用；"
                      "想更明确可以改成 \"xinput\"）")
        return 0

    # ---- 有问题：先报告 ----
    if verbose:
        print()
        if not hid_ok:
            print(f"  ✗ hid = {cur_hid!r} 收不到虚拟手柄")
            print(f"    应该是 \"any\" / \"xinput\" / \"sdl\" 之一")
        if not kb_ok:
            print(f"  ⚠ keyboard_mode = {cur_kb}：Xenia 自己在把键盘映射成手柄，"
                  f"会和虚拟手柄抢端口")
            print(f"    建议改成 0（Disabled），键盘交给 mapper 处理")

    if not auto_fix:
        if verbose:
            print()
            print("  这是只读检查，没有改动配置。")
            print("  要自动修正，运行：")
            print("      python tools\\find_camera.py --fix-input")
            print("  （必须先完全关闭 Xenia，否则改动会被它的内存值覆盖）")
        return 3

    # ---- auto_fix：备份后改 ----
    pid = _find_pid_by_prefix("xenia") or _find_pid_by_window_title("xenia")
    if pid is not None:
        if verbose:
            print()
            print("  ! Xenia 正在运行，现在改会被它退出时的写回覆盖。")
            print("    请先完全关闭 Xenia 再运行本脚本。")
        return 3

    try:
        import shutil
        import time as _t
        bak = f"{cfg}.bak_{_t.strftime('%Y%m%d_%H%M%S')}"
        shutil.copy2(cfg, bak)
        if verbose:
            print(f"  已备份: {os.path.basename(bak)}")
    except OSError:
        bak = None

    new = text
    if m_hid and not hid_ok:
        new = re.sub(r'(?m)^(\s*hid\s*=\s*)"[^"]*"', r'\1"xinput"', new, count=1)
    if m_kb and not kb_ok:
        new = re.sub(r'(?m)^(\s*keyboard_mode\s*=\s*)\d+', r'\g<1>0', new, count=1)

    try:
        # 无 BOM UTF-8：Xenia 的 toml 解析器遇到 BOM 会把第一个键读成乱码
        with open(cfg, "w", encoding="utf-8", newline="\n") as f:
            f.write(new)
    except OSError as e:
        if verbose:
            print(f"  ⚠ 写不了配置文件: {e}")
        return 3

    if verbose:
        print()
        print("  已修正：")
        if not hid_ok:
            print(f'    hid:           {cur_hid!r} → "xinput"')
        if not kb_ok:
            print(f"    keyboard_mode: {cur_kb} → 0（键盘交给 mapper）")
        print()
        print("  ★ 现在启动 Xenia 才生效 ★")

    return 1


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

    **off 指向结构起点（position 的开头）**，不是四元数地址。
    布局（大端 float）：
        +0x00  position  4f   第 4 个必须 ≈1.0（校验位）
        +0x10  quaternion 4f  模长² ≈ 1
        +0x20  scale     4f   前三个在 0.5~2.0
        +0x30  FOV       1f   5~130

    ⚠ 调用方拿到结果后要把 off 换算成四元数地址（+0x10）再存，
    因为 sensor.py 的 cam_quat 参数指的是**四元数**的地址，
    不是结构起点。这个换算踩过坑：直接存起点会让 sensor 把
    position 当成四元数读，模长必然爆掉，表现为 ok=0。
    """
    if off + 0x34 > len(raw):
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
    """在 [base, base+size) 内找出所有相机结构。

    返回 (hits, scanned_bytes)，hits 里存的是**四元数地址**
    （结构起点 + 0x10），因为 sensor.py 的 cam_quat 参数要的是四元数
    的地址。存结构起点会让后续读取错位（踩过）。
    """
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
                    # off 是结构起点，四元数在 +0x10
                    hits.append(pos + off + 0x10)
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
    ap.add_argument("--fix-input", action="store_true",
                    help="只修正 Xenia 的输入配置（hid=xinput / 键盘不抢端口），"
                         "不做相机定位。键鼠完全没反应时先跑这条")
    a = ap.parse_args()

    print("=" * 70)
    print("  相机地址定位")
    print("=" * 70)
    print()

    # ---- 只修 Xenia 输入配置，不做定位 ----
    # 用途：键鼠完全没反应时先排除模拟器侧配置问题。
    # 这条路径**不需要游戏在关卡内**，甚至 Xenia 没启动也能修。
    if a.fix_input:
        print("  只检查 Xenia 输入配置（不定位相机）")
        print()
        rc = ensure_xenia_input(verbose=True, auto_fix=True)
        print()
        if rc == 1:
            print("  配置已修正。请**完全重启 Xenia**，然后进关卡重新运行：")
            print("      python tools\\find_camera.py")
        elif rc == 0:
            print("  配置本来就正确，无需改动。")
        return 0 if rc in (0, 1) else 1

    print("  前提：游戏必须已经在**关卡内**（训练场也算）。")
    print("        标题画面 / 菜单 / 机库里相机结构还没初始化。")
    print()

    proc, guest_base = _conn()
    if proc is None:
        # 连不上进程说明 Xenia 没启动。此时仍然可以帮忙修输入配置，
        # 免得用户以为「连不上就什么都做不了」。
        print()
        print("-" * 70)
        print("  虽然没连上 Xenia，仍可检查输入配置")
        print("-" * 70)
        ensure_xenia_input(verbose=True)
        return 2

    print(f"  进程已连接   guest base = {_fmt_addr(guest_base)}")

    # ---- 先验证内置默认地址是否可用 ----
    # 注意：default_quat 是**四元数**地址，而 _check_struct 期望的是
    # 结构起点，所以要退 0x10 再传。
    default_quat = 0x405EA750
    raw = proc.read(guest_base + default_quat - 0x10, 0x40)
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
        # 位置在 quat - 0x10 处（结构起点），不是 quat 本身
        raw = proc.read(guest_base + ga - 0x10, 12)
        pos = struct.unpack(">3f", raw) if raw and len(raw) >= 12 else (0, 0, 0)
        fov_s = f"{fov:>7.2f}" if fov else f"{'--':>7}"
        moving = " ← 在动" if delta > 1e-4 else ""
        print(f"    {_fmt_addr(ga):<16} {delta:>10.6f}  {fov_s}  "
              f"({pos[0]:.0f}, {pos[1]:.0f}, {pos[2]:.0f}){moving}")
        if delta > 1e-4 and fov is not None:
            verified.append(ga)

    print()

    # ---- 去重：同一相机会有多份副本（值完全相同）----
    # 只保留地址最小的那个，其余视为副本。
    # 为什么不去掉：副本本身也是合法结构，读哪个都一样；
    # 但输出里列一堆相同地址会让用户困惑。
    unique = []
    seen_vals = set()
    for ga in hits:
        s = _sample_quat(proc, guest_base, ga, times=1, gap=0)
        if not s:
            continue
        key = tuple(round(v, 5) for v in s[0])
        if key in seen_vals:
            continue
        seen_vals.add(key)
        unique.append(ga)

    if len(hits) > len(unique):
        print(f"  （{len(hits)} 个候选中，{len(hits) - len(unique)} 个是同一"
              f"结构的副本，值完全相同）")
        print()

    # ---- 决策 ----
    if verified:
        target = verified[0]
        print(f"  ✓ 确认相机地址：{_fmt_addr(target)}")
        print(f"    （四元数在变化 + FOV 合理，这两条同时满足）")
    elif hits:
        target = hits[0]
        fov = _read_fov(proc, guest_base, target)
        print(f"  相机地址：{_fmt_addr(target)}")
        if fov is not None:
            print(f"    ✓ 结构校验通过（FOV = {fov:.1f}°，四元数单位化，"
                  f"世界坐标有效）")
            print(f"    · 采样期间视角没有变化，所以没法做「动起来」的确认。")
            print(f"      这在静止/暂停时是正常的。")
        else:
            print(f"    ⚠ FOV 读不到，可能不是相机。")
            print(f"      如果 mapper 跑起来视角不对，请重跑本脚本。")
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

    proc.close()

    # ---- 顺带修正 Xenia 的输入配置 ----
    #
    # 相机地址对了还不够：Xenia 必须只收虚拟手柄（hid=xinput），
    # 并且不能用自己的键盘模拟（keyboard_mode=0）去抢同一个端口。
    # 这两条不对的表现就是「键鼠全都没反应」，而玩家根本想不到
    # 是模拟器侧的配置问题。所以定位脚本顺手把它修好。
    if not a.dry:
        print()
        print("-" * 70)
        print("  Xenia 输入配置检查")
        print("-" * 70)
        rc = ensure_xenia_input(verbose=True, auto_fix=False)
        print()

    print("  完成。检查完上面的提示后正常启动 mapper 即可。")
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

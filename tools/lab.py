"""tools/lab.py — ACVD 诊断工具包（一条命令替代一堆临时脚本）

设计原则
--------
1. **一条命令一个结论**。不要"先跑三个脚本、再自己看 csv"。
2. **测量必须自证**。读数看着不对要能立刻看出是"没输入"还是"真没动"。
3. **坐标一律 guest**（0x405EA770 这种）。宿主地址只在调试时换算。

命令
----
    state       当前状态：base / 相机 / 火控圈 / 臂长 / 是否在动
    slots       输入通路体检：XInput 槽位、Xenia 焦点、hid 配置
    input       实测三条通路哪条能驱动游戏（虚拟手柄 LS / RS / 键盘方向键）
    trace       触发式录制 + 运动学判定（**判"机体转/相机转"就用它**）
    diff        组内副本活性排名
    stick       推右摇杆（手动，一次性）
    hold-pad    占住手柄槽位不放（Ctrl+C 结束）

trace 的判据（详见 acvd/motion.py）
----------------------------------
    等效吊臂 R = 相机走过的路程 / 转过的弧度
        R ≈ 1~30m  → 相机被吊着绕一个固定中心走 → **机体在转**
        R < 1m     → 位置没怎么动，只有朝向变 → **相机原地转**
"""
from __future__ import annotations

import argparse
import csv
import ctypes
import math
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from acvd.aim import read_transform                       # noqa: E402
from acvd.focus import FocusWatcher, _largest_visible_window  # noqa: E402
from acvd.gmem import GuestMem                            # noqa: E402
from acvd.locate_struct import locate_pairs               # noqa: E402
from acvd.motion import Sample, analyze, trigger_index, wrap180  # noqa: E402
from acvd.pad import VirtualPad                           # noqa: E402
from acvd.scene import read_cam                           # noqa: E402
from acvd.xenia_ui import ensure_slot, slot_label, tree    # noqa: E402

ARM_IDEAL = 22.09
HZ = 200.0
PAD_NAME = "Xbox 360 Controller"
XENIA_CONFIG = os.path.expanduser("~/Documents/Xenia/xenia-edge.config.toml")

# Xenia [HID.Key] 默认绑定（键盘 → 右摇杆）
VK_RIGHT_THUMB = {"left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28}


# ---------------------------------------------------------------- 连接
class Session:
    def __init__(self) -> None:
        self.gm = GuestMem.attach("xenia")
        self.proc = self.gm.proc
        self.base = self.gm.base
        self.cam = 0
        self.aim = 0
        self.dist = 0.0
        self.copies = 0

    # ---- 找玩家那一簇 ----
    def group(self, limit: int = 200, pitch_tol: float = 70.0):
        """按「属性完全重复」分簇，取副本最多的那一簇 = 玩家基准。

        为什么不用 ARM_IDEAL 挑：那个常数会把「另一个也对着相机、但距离
        22.0m 的对象」当成候选（B2 那次就从 24.05m 翻到 22.03m，看起来像
        "地址变了"，其实是换了个东西）。

        为什么"副本最多"可信：玩家基准被渲染/物理/UI 等多个子系统各存一份，
        实测有 17~27 份完全重复（同坐标同 yaw）；零散对象通常 1~5 份。
        簇大小本身就是可信度证据。
        """
        pairs = locate_pairs(self.proc, limit=limit, pitch_tol=pitch_tol)
        if not pairs:
            raise RuntimeError("没有候选对（可能不在战斗场景）")
        cl = {}
        for p in pairs:
            k = (round(p["dist"], 1), round(p["dyaw"], 1), round(p["dpitch"], 1), p["cam"])
            cl.setdefault(k, []).append(p)
        ranked = sorted(cl.items(), key=lambda kv: (-len(kv[1]), kv[0][3]))
        key, items = ranked[0]
        items.sort(key=lambda x: x["addr"])          # 地址最小者，确定可复现
        self.cam = items[0]["cam"]
        self.aim = items[0]["addr"]
        self.dist = items[0]["dist"]
        self.copies = len(items)
        self.ratio = (len(items) / max(1, len(ranked[1][1]))) if len(ranked) > 1 else 99.0
        return items

    def read(self):
        c = read_cam(self.proc, self.cam)
        a = read_transform(self.proc, self.aim)
        return c, a

    def sample(self, t: float, mark: str = "") -> Sample | None:
        """读一帧。相机读不到才算失败；火控圈读不到只标记，不丢整帧。"""
        c = read_cam(self.proc, self.cam)
        if not c:
            return None
        a = read_transform(self.proc, self.aim)
        if a:
            return Sample(t, c[0], c[1], c[2][0], c[2][1], c[2][2],
                          a[1], a[2], a[0][0], a[0][2], mark=mark, aim_ok=True)
        return Sample(t, c[0], c[1], c[2][0], c[2][1], c[2][2],
                      mark=mark, aim_ok=False)

    def guest(self, host: int) -> int:
        return host - self.base


# ---------------------------------------------------------------- 焦点
def focus_game(verbose: bool = True) -> bool:
    """把 Xenia 主窗口抢到前台，返回是否成功。"""
    fw = FocusWatcher(exe_prefix="xenia")
    if fw.is_focused():
        return True
    hwnd = _largest_visible_window(fw.pid or 0)
    if not hwnd:
        fw._refresh()
        hwnd = fw.hwnd
    if not hwnd:
        if verbose:
            print("! 找不到 Xenia 窗口")
        return False
    u = ctypes.windll.user32
    u.ShowWindow(hwnd, 9)                      # SW_RESTORE
    u.BringWindowToTop(hwnd)
    u.keybd_event(0x12, 0, 0, 0)               # 按一下 Alt，绕过前台锁
    ok = u.SetForegroundWindow(hwnd)
    u.keybd_event(0x12, 0, 2, 0)
    time.sleep(0.35)
    fw._next_refresh = 0.0
    got = fw.is_focused()
    if verbose:
        print("抢焦点：" + ("成功" if got else "失败（游戏可能仍在后台）")
              + ("" if ok else "  [SetForegroundWindow 被拒]"))
    return got


def _hid_config() -> dict:
    out = {}
    try:
        with open(XENIA_CONFIG, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.startswith("hid =") or line.startswith("keyboard_passthrough") \
                        or line.startswith("right_stick_deadzone"):
                    k, _, v = line.partition("=")
                    out[k.strip()] = v.split("#")[0].strip().strip('"')
    except OSError:
        pass
    return out


# ---------------------------------------------------------------- 命令
def cmd_state(s: Session) -> None:
    items = s.group()
    print(f"guest base   = 0x{s.base:012X}")
    print(f"相机         = guest 0x{s.guest(s.cam):08X}")
    print(f"机体         = guest 0x{s.guest(s.aim):08X}")
    print(f"玩家组       = {s.copies} 个副本，臂长 {s.dist:.2f}m，"
          f"yaw差 {min(x['dyaw'] for x in items):.2f}°")
    c, a = s.read()
    if not c or not a:
        print("读取失败")
        return
    print(f"相机姿态     yaw={c[0]:+8.2f}  pitch={c[1]:+6.2f}  "
          f"pos=({c[2][0]:8.1f},{c[2][1]:6.1f},{c[2][2]:8.1f})")
    print(f"机体姿态     yaw={a[1]:+8.2f}  pitch={a[2]:+6.2f}  "
          f"pos=({a[0][0]:8.1f},{a[0][1]:6.1f},{a[0][2]:8.1f})")
    print(f"滞后角       {wrap180(a[1] - c[0]):+.2f}°")

    s1 = s.read()
    time.sleep(0.2)
    s2 = s.read()
    moved = "静止" if (s1[0] and s2[0] and abs(wrap180(s2[0][0] - s1[0][0])) < 0.02) else "在动"
    print(f"场景状态     {moved}")


def cmd_slots(s: Session | None = None) -> None:
    """输入通路体检（不推杆，只报告环境）。"""
    from acvd.pad import detect_slots
    print(f"XInput 已占槽位   {detect_slots() or '（无）'}")
    cfg = _hid_config()
    print(f"Xenia 配置        {cfg or '（读不到 ' + XENIA_CONFIG + '）'}")
    fw = FocusWatcher(exe_prefix="xenia")
    focused = fw.is_focused()
    print(f"Xenia 进程        pid {fw.pid}  窗口 0x{(fw.hwnd or 0):X}")
    print(f"Xenia 是否前台    {'是' if focused else '否'}")

    t0 = time.perf_counter()
    pad = VirtualPad()
    try:
        print("建虚拟手柄后，Xenia 菜单里的注册过程：")
        last = None
        while time.perf_counter() - t0 < 6.0:
            cur = (slot_label(1), slot_label(2))
            if cur != last:
                print(f"  {time.perf_counter() - t0:5.2f}s  槽位1={cur[0] or '空'}  "
                      f"槽位2={cur[1] or '空'}")
                last = cur
            time.sleep(0.2)
        print(f"XInput 槽位        {detect_slots() or '（无）'}")
        if PAD_NAME not in (slot_label(1) + slot_label(2)):
            print(f"→ Xenia 始终没把「{PAD_NAME}」登记进设备表。"
                  f"这不是槽位错，是**它根本没看见这个设备**。")
            print("  重启 Xenia 前先建好手柄（启动时枚举），或换 USB 物理手柄。")
        else:
            print(f"→ 已登记。槽位 1 = {slot_label(1) or '空'}")
    finally:
        pad.close()
    print(f"建/拆手柄耗时     {time.perf_counter() - t0:.2f}s")


def _probe_motion(s: Session, label: str, act, dur: float) -> bool:
    """跑一个动作，比较前后的相机 pos/yaw。返回是否真的动了。"""
    c0, _ = s.read()
    if not c0:
        print(f"  [{label}] 读不到相机")
        return False
    act()
    time.sleep(dur)
    c1, _ = s.read()
    d = math.dist(c0[2], c1[2])
    dy = abs(wrap180(c1[0] - c0[0]))
    hit = d > 0.4 or dy > 2.0
    print(f"  [{label}] 位移 {d:6.2f}m   yaw {dy:6.2f}°   → "
          + ("通了" if hit else "没反应"))
    return hit


def cmd_input(s: Session, use_keys: bool = True) -> None:
    """实测哪条通路能把输入送进游戏。"""
    focused = focus_game()
    s.group()

    pad = VirtualPad()
    try:
        time.sleep(0.8)
        print("虚拟手柄已建立，槽位分配：")
        ensure_slot(PAD_NAME, 1)
        print("逐条试探：")
        ok_ls = _probe_motion(s, "虚拟手柄 左摇杆前推",
                              lambda: (pad.left(0.0, 1.0), pad.update()), 1.3)
        pad.left(0.0, 0.0)
        pad.update()
        wait_still(s, timeout=5.0)      # 等惯性滑完，否则下面的位移全是余量
        ok_rs = _probe_motion(s, "虚拟手柄 右摇杆左推",
                              lambda: (pad.right(-1.0, 0.0), pad.update()), 1.0)
        pad.right(0.0, 0.0)
        pad.update()
    finally:
        time.sleep(0.2)
        pad.close()

    ok_key = False
    if use_keys:
        time.sleep(0.6)
        print("键盘通路（Xenia [HID.Key] 里右摇杆左右的默认绑定是方向键）：")
        ok_key = _probe_motion(s, "键盘 方向键←",
                               lambda: _key_hold(VK_RIGHT_THUMB["left"], 1.0), 0.6)

    print("\n结论：" + ("至少一条通路可用" if (ok_ls or ok_rs or ok_key)
                       else "全部没反应 → 见下"))
    if not ok_ls and not ok_rs and not ok_key:
        print("  * 虚拟手柄已尝试分配到槽位 1，检查上面那行「已分配/失败」。")
        print(f"  * 当前槽位 1 = {slot_label(1) or '空'}；"
              f"槽位 2 = {slot_label(2) or '空'}")
        print("  * 游戏没在战斗里（菜单/机库/暂停）也会全无反应——先抓一张画面看。")
        if not focused:
            print("  * 游戏不在前台（理论上不影响 SDL 手柄，但键盘通路会失效）。")


def _key_hold(vk: int, dur: float) -> None:
    u = ctypes.windll.user32
    u.keybd_event(vk, 0, 0, 0)
    time.sleep(dur)
    u.keybd_event(vk, 0, 2, 0)


def cmd_stick(x: float, y: float, dur: float) -> None:
    pad = VirtualPad()
    try:
        pad.right(x, y)
        pad.update()
        print(f"右摇杆 ({x:+.2f},{y:+.2f}) 保持 {dur:.2f}s")
        time.sleep(dur)
    finally:
        pad.right(0.0, 0.0)
        pad.update()
        time.sleep(0.05)
        pad.close()
    print("已回中")


def cmd_hold_pad(seconds: float) -> None:
    """占住一个手柄槽位不放，直到 Ctrl+C。

    用途已经变了：Xenia 支持热插拔（手柄/键盘随时能在 1P 部署与脱离），
    所以不需要"先占位再启动"。现在它的用途是**占坑测试**——
    先在游戏外占住一个槽位，再让游戏/手柄进来，观察用户位怎么分配。
    """
    pad = VirtualPad()
    print("手柄已占位并保持中板。Ctrl+C 结束。")
    t0 = time.perf_counter()
    try:
        while seconds <= 0 or time.perf_counter() - t0 < seconds:
            pad.set_buttons(0)
            pad.left(0.0, 0.0)
            pad.right(0.0, 0.0)
            pad.triggers(0.0, 0.0)
            pad.update()
            time.sleep(1.0 / 30)
    except KeyboardInterrupt:
        pass
    finally:
        pad.close()
        print("手柄已释放")


def cmd_diff(s: Session, rounds: int = 6, gap: float = 0.05) -> None:
    items = s.group()
    addrs = [(x["addr"], x["dist"]) for x in items]
    print(f"{len(addrs)} 个副本，{rounds} 轮 × {gap*1000:.0f}ms")
    snaps = []
    for _ in range(rounds):
        snaps.append([read_transform(s.proc, a) for a, _ in addrs])
        time.sleep(gap)

    print("\n guest        臂长    变化字数  最大幅度  判定")
    rows = []
    for i, (a, d) in enumerate(addrs):
        frames = [sn[i] for sn in snaps]
        if any(f is None for f in frames):
            rows.append((a, d, -1, 0.0))
            continue
        ch, mx = 0, 0.0
        for p, q in zip(frames, frames[1:]):
            if p[1] != q[1]:
                ch += 1
                mx = max(mx, abs(wrap180(p[1] - q[1])))
            if p[2] != q[2]:
                ch += 1
                mx = max(mx, abs(p[2] - q[2]))
        rows.append((a, d, ch, mx))
    for a, d, ch, mx in sorted(rows, key=lambda r: -r[2]):
        tag = "活" if ch >= rounds else ("半活" if ch else "停更")
        print(f" 0x{s.guest(a):08X}  {d:6.2f}  {ch:6d}  {mx:8.4f}  {tag}")


def _probe_input(s: Session, pad, seconds: float = 0.45) -> tuple[float, float]:
    """轻推一下右摇杆，返回 (yaw 变化度, 位移米)。用行为验证通路，不看菜单。

    菜单里的设备列表会滞后/不刷新（实测同一个手柄一次显示、一次不显示），
    拿"菜单里有没有它"当判据会得出"没识别"的假结论。推一下看画面动没动
    才是唯一可信的自证。
    """
    c0, _ = s.read()
    pad.right(-0.85, 0.0)
    pad.update()
    time.sleep(seconds)
    pad.right(0.0, 0.0)
    pad.update()
    time.sleep(0.25)
    c1, _ = s.read()
    if not c0 or not c1:
        return (0.0, 0.0)
    return (abs(wrap180(c1[0] - c0[0])), math.dist(c0[2], c1[2]))


def _wait_still_after(s: Session, pad) -> None:
    """先松手，再等相机停住。"""
    pad.right(0.0, 0.0)
    pad.left(0.0, 0.0)
    pad.update()
    wait_still(s, timeout=6.0)


# ---------------------------------------------------------------- trace
def wait_still(s: Session, speed_max: float = 0.06, hold: float = 0.5,
               timeout: float = 12.0) -> float | None:
    """等相机真的停住，返回稳定后的漂移速度（m/s）；超时返回 None。

    ⚠ 为什么必须等：实测事故（2026-11-10）——先用左摇杆把机体推到 35m/s，
    紧接着推右摇杆，相机在这 1.6s 里平移了 20m。那 20m 是**左摇杆的惯性**，
    不是右摇杆造成的。拿它去算"等效吊臂"会得出 15.28m 这种看着很合理、
    实际全是余量的假结论。
    """
    t_end = time.perf_counter() + timeout
    hist = []
    ok_since = None
    while time.perf_counter() < t_end:
        c, _ = s.read()
        now = time.perf_counter()
        if c:
            hist.append((now, c[2]))
            if len(hist) > 8:
                hist.pop(0)
            if len(hist) >= 6:
                (ta, pa), (tb, pb) = hist[0], hist[-1]
                dt = tb - ta
                sp = math.dist(pa, pb) / dt if dt > 1e-3 else 99.0
                if sp <= speed_max:
                    ok_since = ok_since or now
                    if now - ok_since >= hold:
                        return sp
                else:
                    ok_since = None
        time.sleep(0.02)
    return None


def _seg_speed(seg) -> float:
    if len(seg) < 2:
        return 0.0
    d = sum(math.dist(a.cam_xz, b.cam_xz) for a, b in zip(seg, seg[1:]))
    dt = seg[-1].t - seg[0].t
    return d / dt if dt > 1e-3 else 0.0


def cmd_cands(s: Session, limit: int = 60, top: int = 20) -> None:
    """把整组候选按「和相机的对齐程度」列出来，看判据到底唯不唯一。

    回答的问题是：**给我一台相机，我能不能唯一地锁定火控圈/机体？**
    如果第一名和第二名差很远（比如 0.3° vs 6°），判据就是硬的；
    如果一堆都在 1° 以内，那这个判据就是自欺欺人。
    """
    pairs = locate_pairs(s.proc, limit=limit, pitch_tol=70.0)
    if not pairs:
        print("没有候选对（不在战斗场景？）")
        return
    g = {}
    for p in pairs:
        g.setdefault(p["cam"], []).append(p)
    camfov, items = max(g.items(), key=lambda kv: len(kv[1]))
    c = read_cam(s.proc, camfov)
    if not c:
        print("相机读不到")
        return
    items.sort(key=lambda x: abs(x["dyaw"]) + abs(x["dpitch"]))

    print(f"相机 guest 0x{camfov - s.base:08X}  yaw={c[0]:+8.2f}  "
          f"pos=({c[2][0]:9.2f},{c[2][1]:7.2f},{c[2][2]:9.2f})   候选 {len(items)} 个\n")
    print("  名次  guest地址    距离m   Δyaw°   Δpitch°   坐标")
    for i, p in enumerate(items[:top], 1):
        q = read_transform(s.proc, p["addr"])
        pos = f"({q[0][0]:9.2f},{q[0][1]:7.2f},{q[0][2]:9.2f})" if q else "读不到"
        print(f"  {i:3d}   0x{p['addr'] - s.base:08X}  {p['dist']:6.2f}  "
              f"{p['dyaw']:+6.2f}  {p['dpitch']:+7.2f}   {pos}")

    print("\n  对齐度分布：")
    for lim in (0.5, 1.0, 3.0, 8.0, 15.0):
        n = sum(1 for p in items if abs(p["dyaw"]) < lim and abs(p["dpitch"]) < lim)
        print(f"    |Δyaw|<{lim:>4.1f}° 且 |Δpitch|<{lim:>4.1f}°  →  {n} 个")
    first, second = items[0], (items[1] if len(items) > 1 else None)
    if second:
        gap = abs(second["dyaw"]) - abs(first["dyaw"])
        print(f"\n  第一名 {abs(first['dyaw']):.2f}° / 第二名 {abs(second['dyaw']):.2f}°  "
              f"→ 间隔 {gap:.2f}°  "
              + ("（判据硬，可以唯一锁定）" if gap >= 1.0 else "（判据偏软，需要额外条件）"))


def cmd_clusters(s: Session, limit: int = 200, top: int = 12) -> None:
    """按「属性完全相同」把候选分簇，看谁是被最多子系统共享的那个。

    实测（A 关静止）：玩家基准那一簇 **27 份完全重复**（同坐标、同 yaw、
    同距离），只有地址不同。这说明它是被渲染/物理/UI 等多个子系统各存一份
    的"真身"，而零散对象通常只有 1 份。

    → 定位判据可以完全不依赖任何常数（ARM_IDEAL 那种），
      直接取「副本最多的那一簇」，并且用簇大小自证可信度。
    """
    pairs = locate_pairs(s.proc, limit=limit, pitch_tol=70.0)
    if not pairs:
        print("没有候选对（不在战斗场景？）")
        return
    groups = {}
    for p in pairs:
        k = (round(p["dist"], 1), round(p["dyaw"], 1), round(p["dpitch"], 1))
        groups.setdefault(k, []).append(p)
    rows = sorted(groups.items(), key=lambda kv: -len(kv[1]))

    print(f"候选 {len(pairs)} 个，分成 {len(rows)} 簇\n")
    print("  簇  副本数  距离m   Δyaw°   Δpitch°   相机           样本地址       坐标")
    for i, (k, items) in enumerate(rows[:top], 1):
        camset = {p["cam"] for p in items}
        cam0 = sorted(camset)[0] - s.base
        q = read_transform(s.proc, items[0]["addr"])
        pos = f"({q[0][0]:9.2f},{q[0][1]:7.2f},{q[0][2]:9.2f})" if q else "读不到"
        print(f"  {i:2d} {len(items):6d}  {k[0]:6.2f}  {k[1]:+6.2f}  {k[2]:+7.2f}   "
              f"0x{cam0:08X}({len(camset)})  0x{items[0]['addr'] - s.base:08X}  {pos}")

    best = rows[0]
    print(f"\n  最大簇：{len(best[1])} 份，距离 {best[0][0]}m，Δyaw {best[0][1]}°，"
          f"Δpitch {best[0][2]}°")
    if len(rows) > 1:
        ratio = len(best[1]) / max(1, len(rows[1][1]))
        print(f"  第二名 {len(rows[1][1])} 份 → 倍数 {ratio:.1f}×  "
              + ("（差距明显，可当唯一判据）" if ratio >= 3 else "（差距不够，需要再加条件）"))


def _seg_rate(seg) -> float:
    if len(seg) < 2:
        return 0.0
    tot = sum(abs(wrap180(b.cam_yaw - a.cam_yaw)) for a, b in zip(seg, seg[1:]))
    dt = seg[-1].t - seg[0].t
    return tot / dt if dt > 1e-3 else 0.0


# ---------------------------------------------------------------- 地址标尺
ADDRLOG = os.path.join(ROOT, "scratch", "addrlog.jsonl")


def cmd_mark(s: Session, label: str) -> None:
    """记录一份「地址指纹」，用来跨关卡/跨重启比对地址到底变不变。

    记的东西：base、相机 guest 地址、机体 guest 地址、两者差值、
    副本数、吊臂、yaw 差、定位耗时、双方世界坐标。
    只有地址加坐标一起记，才能区分"地址变了但结构还是同一个"和
    "根本换了一套结构"。
    """
    import json
    t0 = time.perf_counter()
    try:
        items = s.group()
    except RuntimeError as e:
        print(f"定位失败（{e}）——现在大概在菜单/换关途中，等进了关卡再打标")
        return
    locate_ms = (time.perf_counter() - t0) * 1000
    c, a = s.read()
    if not c or not a:
        print("读取失败，没记")
        return
    rec = {
        "ts": time.strftime("%H:%M:%S"),
        "label": label,
        "base": f"0x{s.base:012X}",
        "cam": f"0x{s.guest(s.cam):08X}",
        "mech": f"0x{s.guest(s.aim):08X}",
        "delta": f"0x{s.aim - s.cam:08X}",
        "copies": s.copies,
        "arm": round(s.dist, 3),
        "dyaw": round(min(x["dyaw"] for x in items), 3),
        "locate_ms": round(locate_ms, 1),
        "cam_pos": [round(v, 2) for v in c[2]],
        "cam_yaw": round(c[0], 2),
        "mech_pos": [round(v, 2) for v in a[0]],
        "mech_yaw": round(a[1], 2),
        # 整组候选地址：只记"选中的那一个"是没意义的——同组里有很多副本，
        # 选中哪一个由排序决定，会因为排序抖动而"看起来地址变了"。
        "addrs": sorted(f"0x{s.guest(x['addr']):08X}" for x in items),
        "cams": sorted({f"0x{s.guest(x['cam']):08X}" for x in items}),
    }
    os.makedirs(os.path.dirname(ADDRLOG), exist_ok=True)
    with open(ADDRLOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"[{label}] {rec['ts']}  base {rec['base']}  "
          f"相机 {rec['cam']}  机体 {rec['mech']}  差 {rec['delta']}")
    print(f"        {rec['copies']} 副本  吊臂 {rec['arm']}m  yaw差 {rec['dyaw']}°  "
          f"定位 {rec['locate_ms']}ms  机体坐标 {rec['mech_pos']}")


def cmd_marks() -> None:
    import json
    if not os.path.exists(ADDRLOG):
        print("还没有记录，先 lab mark <标签>")
        return
    rows = [json.loads(l) for l in open(ADDRLOG, encoding="utf-8") if l.strip()]
    if not rows:
        print("记录为空")
        return
    first = rows[0]
    print(f"{'标签':<6} {'时间':<9} {'相机':<11} {'机体':<11} {'差值':<11} "
          f"{'副本':>4} {'吊臂':>6} {'定位ms':>7}  机体坐标")
    for r in rows:
        print(f"{r['label']:<6} {r['ts']:<9} {r['cam']:<11} {r['mech']:<11} "
              f"{r['delta']:<11} {r['copies']:>4} {r['arm']:>6} {r['locate_ms']:>7}  "
              f"{tuple(r['mech_pos'])}")
    print()
    for f in ("cam", "mech", "delta"):
        same = len({r[f] for r in rows}) == 1
        print(f"  {f:<6} {'全部相同' if same else '有变化'}"
              f"  {sorted({r[f] for r in rows})}")
    print(f"  base   {'全部相同' if len({r['base'] for r in rows}) == 1 else '有变化'}"
          f"  {sorted({r['base'] for r in rows})}")
    print(f"  与首条对比："
          f"相机{'一致' if all(r['cam'] == first['cam'] for r in rows) else '不一致'}，"
          f"机体{'一致' if all(r['mech'] == first['mech'] for r in rows) else '不一致'}")

    # 整组候选地址做集合比对——单个"选中项"会因为排序抖动而假变
    with_set = [r for r in rows if "addrs" in r]
    if len(with_set) >= 2:
        print("\n  候选地址集合比对（这才是可信的指纹）：")
        for a, b in zip(with_set, with_set[1:]):
            sa, sb = set(a["addrs"]), set(b["addrs"])
            inter = sa & sb
            print(f"    {a['label']} → {b['label']}: "
                  f"{len(sa)} → {len(sb)} 个，共有 {len(inter)}，"
                  f"新增 {len(sb - sa)}，消失 {len(sa - sb)}")
            if inter:
                print(f"      共有: {sorted(inter)}")
        allsets = [set(r["addrs"]) for r in with_set]
        common = set.intersection(*allsets)
        print(f"    所有标记共有的地址 {len(common)} 个"
              + (f"：{sorted(common)}" if common else "（一个都没有）"))
    else:
        print("\n  （记录太少或没有存整组地址，无法做集合比对）")


def cmd_pitch(s: Session, mag: float = 0.9, dur: float = 1.2) -> None:
    """标定右摇杆的纵轴：推上去/推下去，各看俯仰怎么动。

    产出三个数：方向（推上 = 抬头还是低头）、稳态角速度（°/s）、
    以及按死区 0.44 反推的满杆角速度。俯仰是闭环必须的，但一直没测过。
    """
    s.group()
    print(f"相机 0x{s.guest(s.cam):08X}  火控圈 0x{s.guest(s.aim):08X}  "
          f"吊臂 {s.dist:.2f}m")
    base = wait_still(s)
    print("已静止" if base is not None else "! 等不到静止，结果可能受干扰")

    pad = VirtualPad()
    try:
        time.sleep(0.6)
        ensure_slot(PAD_NAME, 1, wait=4.0)
        for sign, label in ((1.0, "上推 y=+%.1f" % mag), (-1.0, "下推 y=%.1f" % -mag)):
            _wait_still_after(s, pad)
            c0, a0 = s.read()
            if not c0 or not a0:
                print("  读不到，跳过")
                continue
            rows = []
            t0 = time.perf_counter()
            pad.right(0.0, sign * mag)
            pad.update()
            while time.perf_counter() - t0 < dur:
                c, a = s.read()
                if c and a:
                    rows.append((time.perf_counter() - t0, a[2], c[1]))
                time.sleep(1.0 / HZ)
            pad.right(0.0, 0.0)
            pad.update()
            if len(rows) < 20:
                print(f"  {label}: 样本不足")
                continue
            dp_aim = rows[-1][1] - rows[0][1]
            dp_cam = rows[-1][2] - rows[0][2]
            span = rows[-1][0] - rows[0][0]
            print(f"  {label}  {span:.2f}s  火控圈俯仰 {dp_aim:+7.2f}°  "
                  f"相机俯仰 {dp_cam:+7.2f}°  "
                  f"平均 {abs(dp_aim) / span:5.2f}°/s  "
                  f"→ {'抬头' if dp_aim > 0 else '低头' if dp_aim < 0 else '不动'}")
        print("\n判读：满杆角速度 ≈ 实测 / (%.1f-%.2f)/(1-%.2f) = 实测 × %.2f"
              % (mag, 0.44, 0.44, (1 - 0.44) / max(1e-6, mag - 0.44)))
    finally:
        _wait_still_after(s, pad)
        pad.close()


def cmd_find_near(s: Session, x: float, z: float, radius: float = 3.0) -> None:
    """在某个水平坐标附近找 transform —— 用来把「圆心」落到具体对象上。

    用途：motion 判定出「相机绕某点画圆」之后，那个点就是机体。
    这里去内存里把站在那个点上的 transform 找出来，顺便看它的 yaw
    跟相机的 yaw 差多少（机体朝向 vs 视角朝向）。
    """
    from acvd.aim import _scan_transforms
    cam = read_cam(s.proc, s.cam)
    print(f"扫描 guest 堆（146MB，约 10s）…找 ({x:.1f}, {z:.1f}) 半径 {radius:.0f}m 内")
    hits = []
    for addr, pos, yaw, pit, pure in _scan_transforms(s.proc):
        d = math.hypot(pos[0] - x, pos[2] - z)
        if d <= radius:
            hits.append((d, addr, pos, yaw, pit, pure))
    hits.sort()
    print(f"命中 {len(hits)} 个")
    print("\n 距离    guest地址    pos(x,y,z)                 yaw      pitch  纯偏航  与相机yaw差")
    for d, addr, pos, yaw, pit, pure in hits[:30]:
        dy = wrap180(yaw - cam[0]) if cam else 0.0
        print(f" {d:5.2f}  0x{s.guest(addr):08X}  "
              f"({pos[0]:9.2f},{pos[1]:7.2f},{pos[2]:9.2f})  {yaw:+8.2f} {pit:+7.2f}  "
              f"{'是' if pure else '否':>4}  {dy:+8.2f}")


def cmd_trace(s: Session, dps: float, pre: float, post: float, out: str,
              push: str | None, timeout: float, tab: bool, want_focus: bool,
              shot: str | None = None) -> None:
    """触发式录制：等静止 → 推杆 → 触发回填 → 运动学判定。

    `--push 0.9,0,1.5` 让脚本自己下右摇杆指令（AI 无人值守用）；
    不给 --push 就是被动等外部输入触发（玩家手动推杆）。
    """
    if want_focus:
        focus_game()
    items = s.group()
    print(f"相机 guest 0x{s.guest(s.cam):08X}  机体 guest 0x{s.guest(s.aim):08X}  "
          f"{s.copies} 副本 吊臂 {s.dist:.2f}m")

    sx = sy = None
    push_dur = 0.0
    if push:
        parts = [p.strip() for p in push.split(",")]
        sx, sy = float(parts[0]), float(parts[1])
        push_dur = float(parts[2]) if len(parts) > 2 else 1.2

    pad = VirtualPad() if push else None
    if pad:
        time.sleep(0.8)
        ensure_slot(PAD_NAME, 1, wait=5.0)
        # 菜单可能滞后，用行为自证：推一下右摇杆看 yaw 变不变
        dy, dp = _probe_input(s, pad)
        if dy < 1.0 and dp < 0.25:
            print(f"自检：推右摇杆无反应（yaw {dy:.2f}°，位移 {dp:.2f}m），"
                  f"再试一次槽位分配", flush=True)
            ensure_slot(PAD_NAME, 1, wait=4.0)
            _wait_still_after(s, pad)
            dy, dp = _probe_input(s, pad)
        if dy < 1.0 and dp < 0.25:
            print(f"! 输入没进 1P（slot1={slot_label(1) or '空'}，"
                  f"slot2={slot_label(2) or '空'}）——录制无意义，先停。")
            print("  Xenia 的 Controllers 菜单里把「Xbox 360 Controller」"
                  "分到槽位 1，或重启 Xenia 前先建手柄。")
            pad.close()
            return
        print(f"自检通过：右摇杆让相机转了 {dy:.1f}°、位移 {dp:.2f}m", flush=True)
        _wait_still_after(s, pad)

    base = wait_still(s)
    if base is None:
        print("! 等不到静止（相机一直在动）——位移判据会被余量污染，仅作参考", flush=True)
    else:
        print(f"已静止（漂移 {base * 100:.2f} cm/s），开始录制", flush=True)

    samples: list[Sample] = []
    trig_t = None
    t0 = time.perf_counter()
    push_at, push_end = 0.30, 0.30 + push_dur
    pushed = released = False
    mode = f"脚本推杆 ({sx:+.2f},{sy:+.2f}) {push_dur:.1f}s" if push else "等你推右摇杆"
    print(f"触发条件：相机角速度 ≥ {dps:.0f}°/s（{mode}，最多等 {timeout:.0f}s）",
          flush=True)

    try:
        while True:
            t = time.perf_counter() - t0
            mark = ""
            if pad:
                if not pushed and t >= push_at:
                    pad.right(sx, sy)
                    pad.update()
                    pushed, mark = True, "push"
                elif pushed and not released and t >= push_end:
                    pad.right(0.0, 0.0)
                    pad.update()
                    released, mark = True, "release"

            smp = s.sample(t, mark)
            if smp:
                samples.append(smp)
                if mark:
                    print(f"  t={t:5.2f}s  {mark}", flush=True)
                    if mark == "release" and shot:
                        from acvd.screen import grab
                        print(f"  截图 {grab(shot, scale=2)[1]}", flush=True)

            if trig_t is None:
                idx = trigger_index(samples[-3:], dps) if len(samples) >= 3 else None
                if idx is not None:
                    trig_t = samples[-3:][idx].t
                    print(f"  t={trig_t:5.2f}s  **触发**（检测到转向）", flush=True)
                elif t > timeout:
                    break
            if trig_t is not None and t >= trig_t + post:
                break
            time.sleep(1.0 / HZ)
    finally:
        if pad:
            pad.right(0.0, 0.0)
            pad.update()
            pad.close()

    if trig_t is None:
        print(f"\n! {timeout:.0f}s 内没检测到转向。样本 {len(samples)} 个。")
        if samples:
            print(f"  相机 yaw 从 {samples[0].cam_yaw:+.2f} 到 {samples[-1].cam_yaw:+.2f}"
                  f"（净 {wrap180(samples[-1].cam_yaw - samples[0].cam_yaw):+.2f}°）")
        else:
            print("  一帧都没读到 → 相机地址失效（换关/堆回收）或进程已退出。")
            print("  重跑 state 重新定位，不要复用旧地址。")
        print("  → 输入通路也先自查：python -m tools.lab input")
        return

    win = [x for x in samples if trig_t - pre <= x.t <= trig_t + post]
    rep = analyze(win)

    # ---- 分段：推杆前 / 推杆中 / 松手后 ----
    marks = {x.mark: x.t for x in win if x.mark}
    t_push = marks.get("push")
    t_rel = marks.get("release")
    seg_pre = [x for x in win if t_push is None or x.t < t_push]
    seg_turn = [x for x in win
                if (t_push is None or x.t >= t_push) and (t_rel is None or x.t <= t_rel)]
    seg_post = [x for x in win if t_rel is not None and x.t > t_rel]

    print("\n  段            样本   速度m/s  角速度°/s  路程m   净转角°")
    for name, seg in (("推杆前(静止)", seg_pre), ("推杆中", seg_turn), ("松手后", seg_post)):
        if len(seg) < 3:
            continue
        print(f"  {name:<12} {len(seg):5d} {_seg_speed(seg):9.3f} {_seg_rate(seg):10.1f} "
              f"{sum(math.dist(a.cam_xz, b.cam_xz) for a, b in zip(seg, seg[1:])):7.2f} "
              f"{wrap180(seg[-1].cam_yaw - seg[0].cam_yaw):8.2f}")

    # 判据只取"推杆中"这一段：前后段是余量与收敛，混进来会污染半径
    rep_turn = analyze(seg_turn) if len(seg_turn) >= 20 else None
    print()
    for line in (rep_turn or rep).lines():
        print(line)
    if tab and len(win) > 10:
        _table(win)
    main_rep = rep_turn or rep
    print(f"\n判定：**{main_rep.verdict}**")
    if main_rep.detail:
        print(f"      {main_rep.detail}")
    if len(seg_pre) > 10 and _seg_speed(seg_pre) > 0.08:
        print(f"  ! 推杆前就在以 {_seg_speed(seg_pre):.2f} m/s 移动，"
              f"路程里含惯性余量，吊臂估值偏大")

    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["t", "cam_yaw", "cam_pitch", "cam_x", "cam_y", "cam_z",
                    "aim_yaw", "aim_pitch", "aim_x", "aim_z", "lead", "mark"])
        for x in win:
            w.writerow([f"{x.t:.4f}", f"{x.cam_yaw:.3f}", f"{x.cam_pitch:.3f}",
                        f"{x.cam_x:.3f}", f"{x.cam_y:.3f}", f"{x.cam_z:.3f}",
                        f"{x.aim_yaw:.3f}", f"{x.aim_pitch:.3f}",
                        f"{x.aim_x:.3f}", f"{x.aim_z:.3f}",
                        f"{wrap180(x.aim_yaw - x.cam_yaw):.3f}", x.mark])
    print(f"曲线已存 {out}（{len(win)} 行）")


def _table(win, step: float = 0.1) -> None:
    """把曲线打成 0.1s 一行的表，肉眼能看出形状。

    速率按"桶内逐样本增量求和 / 桶时长"算。早先版本拿桶首样本和上一桶首样本
    作差，会打出 303°/s、0°/s 这种一跳一跳的假数字。
    """
    print("\n   t    速率°/s   本段位移  累计位移  领先角  路程m")
    base = win[0]
    per = []
    for a, b in zip(win, win[1:]):
        dt = b.t - a.t
        if dt > 1e-4:
            per.append((b.t, abs(wrap180(b.cam_yaw - a.cam_yaw)), dt,
                        math.dist(a.cam_xz, b.cam_xz)))
    t = base.t
    path = 0.0
    i = 0
    while t < win[-1].t:
        r = 0.0
        d = 0.0
        dur = 0.0
        seg = 0.0
        while i < len(per) and per[i][0] < t + step:
            _, dy, dt, dp = per[i]
            r += dy
            dur += dt
            d += dp
            seg += dp
            i += 1
        if i == 0 or dur <= 1e-4:
            t += step
            continue
        path += seg
        x = per[i - 1][0]
        smp = min(win, key=lambda s: abs(s.t - x))
        print(f" {t:6.2f} {r / dur:8.1f} {d:9.2f} {math.dist(base.cam_xz, smp.cam_xz):9.2f} "
              f"{wrap180(smp.aim_yaw - smp.cam_yaw):+7.2f} {path:7.2f}"
              + ("  ←推杆" if smp.mark == "push" else
                 "  ←松手" if smp.mark == "release" else ""))
        t += step


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["state", "diff", "stick", "padtest", "input",
                                    "slots", "hold-pad", "trace", "probe-turn",
                                    "watch-turn", "menu", "assign", "near",
                                    "mark", "marks", "cands", "clusters", "pitch"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--stick", default="0.9,0")
    ap.add_argument("--dur", type=float, default=1.0)
    ap.add_argument("--out", default="scratch/trace.csv")
    ap.add_argument("--no-focus", action="store_true")
    ap.add_argument("--wait", type=float, default=90.0)
    ap.add_argument("--dps", type=float, default=25.0, help="触发角速度阈值 °/s")
    ap.add_argument("--pre", type=float, default=0.8, help="触发前回填秒数")
    ap.add_argument("--post", type=float, default=2.2, help="触发后继续录秒数")
    ap.add_argument("--push", default=None, help="x,y,dur 由脚本推右摇杆触发")
    ap.add_argument("--tab", action="store_true", help="打印逐 0.1s 曲线表")
    ap.add_argument("--shot", default=None, help="松手瞬间截一张图存到这里")
    a = ap.parse_args()

    if a.cmd == "hold-pad":
        cmd_hold_pad(a.dur)
        return
    if a.cmd == "slots":
        cmd_slots()
        return
    if a.cmd == "menu":
        for d, lab, cid, sub in tree():
            print("  " * d + lab + (f"   [id {cid}]" if cid and cid > 0 else ""))
        return
    if a.cmd == "marks":
        cmd_marks()
        return
    if a.cmd == "assign":
        from acvd.xenia_ui import assign_device
        dev = a.args[0] if a.args else PAD_NAME
        slot = int(a.args[1]) if len(a.args) > 1 else 1
        assign_device(dev, slot)
        return
    if a.cmd == "stick":
        vals = (a.args or ["0.9", "0", "0.8"])[:3]
        while len(vals) < 3:
            vals.append("0.8")
        cmd_stick(float(vals[0]), float(vals[1]), float(vals[2]))
        return

    s = Session()
    try:
        if a.cmd == "state":
            cmd_state(s)
        elif a.cmd == "diff":
            cmd_diff(s)
        elif a.cmd == "near":
            vals = [float(v) for v in (a.args or ["0", "0"])[:2]]
            r = float(a.args[2]) if len(a.args) > 2 else 3.0
            cmd_find_near(s, vals[0], vals[1], r)
        elif a.cmd == "pitch":
            cmd_pitch(s)
        elif a.cmd == "clusters":
            cmd_clusters(s)
        elif a.cmd == "cands":
            cmd_cands(s)
        elif a.cmd == "mark":
            cmd_mark(s, a.args[0] if a.args else "未命名")
        elif a.cmd in ("input", "padtest"):
            cmd_input(s, use_keys=(a.cmd == "input"))
        elif a.cmd in ("trace", "probe-turn"):
            cmd_trace(s, a.dps, a.pre, a.post, a.out, a.push,
                      a.wait if a.push is None else 20.0, a.tab, not a.no_focus,
                      a.shot)
        elif a.cmd == "watch-turn":
            cmd_trace(s, a.dps, a.pre, a.post, a.out, None, a.wait, a.tab,
                      not a.no_focus, a.shot)
    finally:
        s.gm.close()


if __name__ == "__main__":
    main()

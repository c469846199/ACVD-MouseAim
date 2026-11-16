"""绝对坐标内核自检（固定相机版）。跑法： python tools/test_core.py

覆盖：
  1. 分层守卫（无定位层、无决策层）
  2. Target_Abs 只由鼠标改变（相机自己转不吞噬目标、与速度/分帧无关）
  3. 实时 FOV 只影响输入增益与准星投影，不影响闭环推力
  4. 闭环在「相机一阶滞后」真实植物下收敛、超调小
  5. 快照过期仍读鼠标/锁光标/累加目标，只停推杆；无焦点门控
  6. F2 锚定、看门狗、层间接缝
"""
import inspect
import math
import os
import sys
import time

# 项目根 = 本文件所在目录（tools/）的上一级。
# 不写死绝对路径：这个测试要能跟着仓库被任何人跑。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from acvd.core import AimEngine, Cfg
from acvd.core.chan import Alarm, Slot
from acvd.core.control import drive, neutral, step, wrap180
from acvd.core.guard import check
from acvd.core.output import Composer, Watchdog
from acvd.core.sensor import (CAM_FOV_GUEST, CAM_QUAT_GUEST, Sensor)
from acvd.core.types import (MODE_OFF, MODE_TRACKING, Cmd, CtrlState, Snap,
                             View)


def _view():
    return View(cx=960.0, cy=540.0, sw=1920.0, sh=1080.0, hfov=45.0)


def _snap(yaw=10.0, pitch=0.0, fov=45.0, ok=True, pos=(0.0, 0.0, 0.0)):
    return Snap(t=time.perf_counter(), ok=ok, cam_yaw=yaw,
                cam_pitch=pitch, cam_pos=pos, fov=fov)


# ---------------------------------------------------------------- 守卫
def test_guard():
    assert not check(), f"分层被破坏: {check()}"
    import acvd.core as core
    import os
    d = os.path.dirname(core.__file__)
    assert not os.path.exists(os.path.join(d, "brain.py")), "决策层又回来了"
    print("守卫    OK  分层无违规，无定位层/决策层")


# ---------------------------------------------------------------- 控制器
def test_target_abs_only_mouse():
    """相机（Aim_Abs）自己转，绝不能反向吞噬 Target_Abs。"""
    cfg, st, v = Cfg(), CtrlState(), _view()
    step(cfg, st, _snap(yaw=10.0), 0, 0, 0.0, v)
    t0 = st.abs_yaw
    # 相机自己转了 80°（过场动画 / 锁定），鼠标没动
    for y in (30.0, -60.0, 170.0, -179.0):
        step(cfg, st, _snap(yaw=y), 0, 0, 0.01, v)
    assert st.abs_yaw == t0, f"相机转向吞噬了目标: {st.abs_yaw} != {t0}"
    print("控制器 OK  相机自己转 200° 不吞噬 Target_Abs")


def test_same_distance_same_target():
    """同样物理滑行距离 ⇒ 同样的 Target_Abs，与速度/分帧无关。"""
    cfg, v = Cfg(), _view()
    a, b = CtrlState(), CtrlState()
    step(cfg, a, _snap(), 0, 0, 0.0, v)
    step(cfg, b, _snap(), 0, 0, 0.0, v)
    base = a.abs_yaw
    for _ in range(50):                      # 慢：50 帧 × 10px
        step(cfg, a, _snap(), 10, 0, 0.0, v)
    step(cfg, b, _snap(), 500, 0, 0.0, v)    # 快：1 帧 500px
    da = wrap180(a.abs_yaw - base)
    db = wrap180(b.abs_yaw - base)
    assert abs(da - db) < 1e-9, f"速度改变了落点: {da:.6f} vs {db:.6f}"
    print("控制器 OK  同样滑行距离 ⇒ 同样 Target_Abs（与速度/分帧无关）")


def test_fov_dynamic_gain():
    """FOV 变大 → 同一像素对应更大角度（动态逆投影）。"""
    cfg, v = Cfg(), _view()
    a, b = CtrlState(), CtrlState()
    step(cfg, a, _snap(fov=45.0), 0, 0, 0.0, v)
    step(cfg, b, _snap(fov=70.0), 0, 0, 0.0, v)
    t0 = a.abs_yaw
    step(cfg, a, _snap(fov=45.0), 100, 0, 0.01, v)
    step(cfg, b, _snap(fov=70.0), 100, 0, 0.01, v)
    da = wrap180(a.abs_yaw - t0)
    db = wrap180(b.abs_yaw - t0)
    assert db > da * 1.3, f"FOV 没改变输入增益: {da:.4f} vs {db:.4f}"
    print("控制器 OK  实时 FOV 动态增益生效")


def test_fov_keeps_stick_feel():
    """同样的鼠标滑行（屏幕距离），任何 FOV 下都给出同样的杆量。

    这要求输入增益 k 与**满杆角度**都按 tan(FOV/2) 等比例缩放。
    只缩放 k（旧行为）的话，开镜 FOV=10° 要滑 2298px 才满杆 —— 比屏幕
    还宽，玩家把鼠标推到屏幕外也顶不满，表现就是「开镜后杆量特别低」。
    """
    v = _view()
    for fov in (70.0, 50.0, 30.0, 10.0):
        cfg, st = Cfg(), CtrlState()
        step(cfg, st, _snap(yaw=0.0, fov=fov), 0, 0, 0.0, v)      # 锚定
        r = step(cfg, st, _snap(yaw=0.0, fov=fov), 432, 0, 0.01, v)
        assert abs(r.rx - 1.0) < 0.02, \
            f"FOV={fov} 下 432px 没顶满，rx={r.rx:.3f}"

    # 小位移也要按同一比例：216px（半屏）≈ 半杆
    for fov in (50.0, 10.0):
        cfg, st = Cfg(), CtrlState()
        step(cfg, st, _snap(yaw=0.0, fov=fov), 0, 0, 0.0, v)
        r = step(cfg, st, _snap(yaw=0.0, fov=fov), 216, 0, 0.01, v)
        assert 0.65 < r.rx < 0.75, f"FOV={fov} 半程杆量异常 rx={r.rx:.3f}"
    print("控制器 OK  同一屏幕位移 ⇒ 同一杆量（各倍率手感一致）")


def test_converges_under_camera_lag():
    """真实植物：右摇杆驱动首端量，相机一阶滞后跟随 τ=0.15s。

    反馈取相机，所以系统是二阶的。默认 full_stick_angle=12° 必须
    不发散、超调可控（<20%）、最终停在目标上。
    """
    cfg, st, v = Cfg(), CtrlState(), _view()
    DT, TAU, RATE = 1.0 / 125.0, 0.15, 54.0
    st.anchor_pending = True
    A = C = 0.0
    T = 10.0
    step(cfg, st, _snap(yaw=0.0), 0, 0, 0.0, v)   # 锚定
    peak = 0.0
    for i in range(int(3.0 / DT)):
        err = wrap180(T - C)
        u = drive(err, cfg)
        w = 0.0 if abs(u) <= cfg.game_deadzone else \
            math.copysign(RATE * (abs(u) - cfg.game_deadzone)
                          / (1 - cfg.game_deadzone), u)
        A += w * DT
        C += (A - C) * (DT / TAU)
        # 把相机值喂回控制器（相机即 Aim_Abs）
        step(cfg, st, _snap(yaw=C), 0, 0, i * DT, v)
        peak = max(peak, A)
    assert abs(A - T) < 0.5, f"没收敛: A={A:.2f} T={T}"
    assert peak - T < T * 0.20, f"超调过大: {peak - T:.2f}°"
    print(f"控制器 OK  相机滞后下收敛 A={A:.2f}° 超调{peak - T:.2f}°")


def test_pitch_closes_loop():
    cfg, st, v = Cfg(), CtrlState(), _view()
    step(cfg, st, _snap(pitch=0.0), 0, 0, 0.0, v)
    cam = 0.0
    RATE = 54.0
    DT = 1.0 / 125.0
    for i in range(120):
        dy = -60 if i == 0 else 0            # 鼠标只推一次
        r = step(cfg, st, _snap(pitch=cam), 0, dy, i * DT, v)
        u = r.ry
        cam += (0.0 if abs(u) <= cfg.game_deadzone else
                math.copysign(RATE * (abs(u) - cfg.game_deadzone)
                              / (1 - cfg.game_deadzone), u)) * DT
    assert abs(st.err_p) < 3.0, f"俯仰没收敛: {st.err_p:.2f}"
    print("控制器 OK  俯仰绝对坐标闭环收敛")


def test_neutral_preserves_target():
    cfg, st, v = Cfg(), CtrlState(), _view()
    step(cfg, st, _snap(), 200, 0, 0.0, v)
    t = st.abs_yaw
    neutral(st)
    assert st.abs_yaw == t and st.out_x == 0.0
    print("控制器 OK  断流归零输出，Target_Abs 原封不动")


# ---------------------------------------------------------------- 输出层
def test_stale_snapshot_keeps_mouse_alive():
    """快照过期（加载/扫描/切场景）时：鼠标照读、光标照锁、目标照累加，
    只是不推杆。旧代码把 get_delta 一起跳过 → 鼠标脱锁 + 带不动火控圈。"""
    cfg, snap_slot, cmd_slot, out_slot = Cfg(), Slot(), Slot(), Slot()
    reads, delta = {"n": 0}, [0.0, 0.0]

    def get_delta():
        reads["n"] += 1
        return (delta[0], delta[1])

    paused = {"v": False}
    c = Composer(snap_slot, cmd_slot, out_slot, get_delta,
                 lambda: _view(), cfg, log=lambda *a: None,
                 is_paused=lambda: paused["v"])
    cmd_slot.put(Cmd(mode=MODE_TRACKING))
    snap_slot.put(_snap(yaw=10.0))
    c._tick()
    base = c.state.abs_yaw

    stale = Snap(t=time.perf_counter() - 1.0, ok=True, cam_yaw=10.0, fov=45.0)
    delta[0] = 100.0
    snap_slot.put(stale)
    n0 = reads["n"]
    c._tick()
    assert reads["n"] == n0 + 1, "快照过期时没读鼠标 → 光标脱锁"
    f1, _ = out_slot.get()
    assert f1.rx == 0.0 and f1.ry == 0.0, "过期快照不该推杆"
    assert c.state.abs_yaw > base, "鼠标位移被吞 → 火控圈带不动"
    delta[0] = 0.0

    paused["v"] = True
    held = c.state.abs_yaw
    n1 = reads["n"]
    c._tick()
    assert reads["n"] == n1, "暂停时不该继续锁光标"
    assert c.state.abs_yaw == held
    paused["v"] = False

    cmd_slot.put(Cmd(mode=MODE_OFF))
    held = c.state.abs_yaw
    c._tick()
    f3, _ = out_slot.get()
    assert f3.rx == 0.0 and c.state.abs_yaw == held

    assert "is_focused" not in inspect.signature(Composer.__init__).parameters, \
        "Composer 又接回了焦点门控"
    assert "is_focused" not in inspect.getsource(Composer._tick)
    print("输出层 OK  快照过期仍读鼠标/锁光标/累加目标，只停推杆；无焦点门控")


def test_main_loop_does_not_steal_raw_pool():
    import tools.mapper as mapper
    src = inspect.getsource(mapper)
    assert "not (st.mouse_on and not st.paused" in src, \
        "主循环和合成层没有按「谁在读」互斥，会抢鼠标池"
    assert "if engine is None and not focused and not take and not st.force:" in src, \
        "焦点清零又回到了新内核路径"
    print("前端    OK  鼠标池由合成层独占消费，新内核不受焦点影响")


# ---------------------------------------------------------------- 锚定 / 看门狗
def test_f2_anchor():
    cfg, snap_slot, cmd_slot, out_slot = Cfg(), Slot(), Slot(), Slot()
    anchor, delta = Alarm(), [0.0, 0.0]
    c = Composer(snap_slot, cmd_slot, out_slot,
                 lambda: (delta[0], delta[1]), lambda: _view(), cfg,
                 log=lambda *a: None, anchor_alarm=anchor)
    cmd_slot.put(Cmd(mode=MODE_TRACKING))
    snap_slot.put(_snap(yaw=10.0, pitch=2.0))
    c._tick()
    assert not c.state.anchor_pending

    delta[0] = 200.0
    snap_slot.put(_snap(yaw=10.0, pitch=2.0))
    c._tick()
    assert c.state.abs_yaw > 10.0, "鼠标没推动 Target_Abs"
    delta[0] = 0.0

    # 换关：世界角跳到 -120 / pitch -8，玩家按 F2
    snap_slot.put(_snap(yaw=-120.0, pitch=-8.0))
    anchor.fire()
    c._tick()
    assert abs(wrap180(c.state.abs_yaw - (-120.0))) < 1e-9, \
        f"F2 没把准星锚到当前视角: {c.state.abs_yaw}"
    assert abs(c.state.abs_pitch - (-8.0)) < 1e-9
    assert abs(c.state.err_y) < 1e-9 and abs(c.state.err_p) < 1e-9
    f, _ = out_slot.get()
    assert f.rx == 0.0 and f.ry == 0.0, "锚定帧不该推杆"

    delta[0] = 100.0
    snap_slot.put(_snap(yaw=-120.0, pitch=-8.0))
    c._tick()
    assert c.state.abs_yaw > -120.0, "锚定后鼠标失效了"
    print("准星    OK  F2 锚定到当前视角，Error 归零且后续鼠标正常")


def test_watchdog_forces_neutral():
    out_slot, alarm = Slot(), Alarm()
    w = Watchdog(out_slot, alarm, log=lambda *a: None)
    w._rx, w._ry = 0.9, -0.5
    w.last_t = time.perf_counter() - 10.0
    w.running = True
    import threading
    th = threading.Thread(target=w.run, daemon=True)
    th.start()
    time.sleep(0.25)
    w.stop()
    rx, ry = w.value()
    assert rx == 0.0 and ry == 0.0, f"看门狗没回中: {rx},{ry}"
    print("输出层 OK  看门狗 100ms 无新帧强制回中")


# ---------------------------------------------------------------- 接缝
def test_seams():
    for cls, need in ((Sensor, ("run", "stop", "stats", "fov")),
                      (Composer, ("run", "stop", "set_cfg", "_tick")),
                      (Watchdog, ("run", "stop", "value", "frame")),
                      (AimEngine, ("start", "stop", "stick", "frame",
                                   "status", "snapshot", "set_enabled",
                                   "set_cfg", "force_rescan",
                                   "request_anchor", "is_tracking"))):
        missing = [n for n in need if not hasattr(cls, n)]
        assert not missing, f"{cls.__name__} 缺 {missing}"
    assert CAM_FOV_GUEST == 0x405EA770 and CAM_QUAT_GUEST == 0x405EA750
    print("接缝    OK  采集/输出/门面接口齐全，相机地址固定")


def test_no_locate_layer_in_core():
    """core 里不许再出现任何「找地址」的痕迹。"""
    import os
    import acvd.core as core
    d = os.path.dirname(core.__file__)
    banned = ("locate_struct", "pierce", "scan_slot", "ScanResult",
              "invalidate_pierce", "request_probe", "request_scan")
    hits = []
    for name in os.listdir(d):
        if not name.endswith(".py"):
            continue
        src = open(os.path.join(d, name), encoding="utf-8").read()
        for b in banned:
            if b in src:
                hits.append(f"{name}:{b}")
    assert not hits, f"core 里仍有定位层痕迹: {hits}"
    print("架构    OK  core 内无扫描/簇/地址缓存")


def test_level_jump_reanchors_silently():
    """跨关卡（相机位置单帧跳 > 50m）：静默重锚，这一帧零推杆。

    旧行为是带着上一关的 Target_Abs 进新关，第一帧就满杆把视角甩回头，
    玩家一进关就被打断。重锚只是换原点，与 F2 同义，不是新判据。
    """
    cfg, snap_slot, cmd_slot, out_slot = Cfg(), Slot(), Slot(), Slot()
    c = Composer(snap_slot, cmd_slot, out_slot, lambda: (0.0, 0.0),
                 lambda: _view(), cfg, log=lambda *a: None)
    cmd_slot.put(Cmd(mode=MODE_TRACKING))
    snap_slot.put(_snap(yaw=10.0, pos=(900.0, 20.0, 400.0)))
    c._tick()
    assert abs(c.state.abs_yaw - 10.0) < 1e-6, "首帧没有锚定到相机"

    # 模拟「玩家在旧关卡攒下的鼠标账本」：目标偏离当前视角 60°
    c.state.abs_yaw = 70.0
    snap_slot.put(_snap(yaw=170.0, pos=(100.0, 0.0, 0.0)))   # 传送 ~800m
    c._tick()
    f, _ = out_slot.get()
    assert abs(f.rx) < 1e-6, f"换关瞬间推杆了 rx={f.rx}"
    assert abs(c.state.abs_yaw - 170.0) < 1e-6, "没有重锚到新关视角"

    # 同一关内正常移动（1 米）不许触发重锚
    c.state.abs_yaw = 200.0
    snap_slot.put(_snap(yaw=170.0, pos=(101.0, 0.0, 0.0)))
    c._tick()
    f2, _ = out_slot.get()
    assert abs(c.state.abs_yaw - 200.0) < 1e-6, "同关内移动误触发重锚"
    assert abs(f2.rx) > 0.5, "该推的时候没推"

    # 坏帧 (0,0,0) 不许触发重锚（加载中/撕裂读）
    c.state.abs_yaw = 210.0
    snap_slot.put(_snap(yaw=170.0, pos=(0.0, 0.0, 0.0)))
    c._tick()
    assert abs(c.state.abs_yaw - 210.0) < 1e-6, "全零位置被当成传送，误触发重锚"
    print("输出层 OK  跨关卡静默重锚零推杆；同关移动与坏帧都不误触")


def test_deadzone_returns_stick_to_center():
    """误差小于 dead_px 折算的角度时，右摇杆必须**真正为 0**。

    这不是「死区判定」，是手柄松手的物理事实：只要 u 恒 ≥ game_deadzone，
    游戏就认为玩家一直在微调瞄准，**拒绝进入开镜状态**（实测：不按 F9
    暂停根本开不了镜，远距离武器第一次开火即可开镜也进不去）。
    """
    cfg, st, v = Cfg(), CtrlState(), _view()
    step(cfg, st, _snap(yaw=10.0), 0, 0, 0.0, v)

    st.abs_yaw = 10.0 + 0.001           # 极小误差
    s = step(cfg, st, _snap(yaw=10.0), 0, 0, 0.0, v)
    assert s.rx == 0.0 and s.ry == 0.0, f"极小误差仍推杆 rx={s.rx:.4f}"

    # 开镜：FOV=10°，死区按像素折算也跟着缩小，仍须回中
    st.abs_yaw = 10.0 + 0.005
    s2 = step(cfg, st, _snap(yaw=10.0, fov=10.0), 0, 0, 0.0, v)
    assert s2.rx == 0.0, f"开镜倍率下摇杆没回中 rx={s2.rx:.4f}"

    # 超出死区必须给足推力 —— 不能借机把整条响应吃掉
    #（默认圈半径 80px ≈ 2.2°，所以用 5° 误差来跨出去）
    st.abs_yaw = 10.0 + 5.0
    s3 = step(cfg, st, _snap(yaw=10.0), 0, 0, 0.0, v)
    assert abs(s3.rx) > 0.4, f"超过死区却没推力 rx={s3.rx:.4f}"

    # dead_px=0 时退回旧行为（纯死区逆变换，摇杆永不为 0）
    c0 = Cfg(dead_px=0.0)
    st2 = CtrlState()
    step(c0, st2, _snap(yaw=10.0), 0, 0, 0.0, v)     # 先消费掉首次锚定
    st2.abs_yaw = 10.0 + 0.001
    s4 = step(c0, st2, _snap(yaw=10.0), 0, 0, 0.0, v)
    assert abs(s4.rx) > 0.4, "dead_px=0 时不该归零"
    print("控制器 OK  死区外推、死区内摇杆真回中（开镜的前提）")


def test_no_proc_no_cursor_lock():
    """游戏没开就启动 mapper：不许接管、不许锁光标、不许谎报已接管。

    真实事故：先开 mapper、模拟器还没启动，光标被钉死在屏幕中心，
    除了按 F9 玩家什么都点不了。根因是 Composer 只看指令模式、不看
    游戏进程在不在，于是每帧 SetCursorPos。进程一出现必须立刻恢复接管。
    """
    cfg, snap_slot, cmd_slot, out_slot = Cfg(), Slot(), Slot(), Slot()
    reads, alive = {"n": 0}, {"v": False}

    def get_delta():
        reads["n"] += 1
        return (50.0, 0.0)

    c = Composer(snap_slot, cmd_slot, out_slot, get_delta,
                 lambda: _view(), cfg, log=lambda *a: None,
                 game_alive=lambda: alive["v"])
    cmd_slot.put(Cmd(mode=MODE_TRACKING))
    snap_slot.put(_snap(yaw=10.0))

    c._tick()
    assert reads["n"] == 0, "游戏没开还在读鼠标 → 光标被钉在屏幕中心"
    f, _ = out_slot.get()
    assert f.mode == MODE_OFF, "游戏没开，面板却显示已接管"
    t = c.state.abs_yaw

    alive["v"] = True
    c._tick()
    assert reads["n"] == 1, "游戏起来了却不接管鼠标"
    f2, _ = out_slot.get()
    assert f2.mode == MODE_TRACKING, "游戏在跑，面板该显示已接管"
    assert c.state.abs_yaw != t, "接管后鼠标位移没进 Target_Abs"
    print("输出层 OK  进程不在不接管/不锁光标，进程出现即接管")


def test_tracking_requires_proc():
    """「在闭环」必须同时满足：开了 + 游戏进程还在。

    进程在但相机暂时读不到（加载中/撕裂读）不能翻状态 —— 否则
    面板会「未接管 / 已接管」来回跳。只有进程消失才判不在闭环。
    """
    class DeadProc:
        def read(self, a, n):
            return None

    e = AimEngine(get_proc=lambda: DeadProc(), get_delta=lambda: (0, 0),
                  get_view=lambda: _view(), cfg=Cfg(), log=lambda *a: None)
    e.start()
    time.sleep(0.35)
    assert e.is_tracking(), "进程在、只是读不到相机，不该翻成未接管"
    e.stop()

    e2 = AimEngine(get_proc=lambda: None, get_delta=lambda: (0, 0),
                   get_view=lambda: _view(), cfg=Cfg(), log=lambda *a: None)
    e2.start()
    time.sleep(0.35)
    assert not e2.is_tracking(), "游戏没开却报告在闭环 → 会锁住光标"
    e2.stop()
    print("门面    OK  只有进程消失才判未闭环，读不到不抖状态")


if __name__ == "__main__":
    test_guard()
    test_target_abs_only_mouse()
    test_same_distance_same_target()
    test_fov_dynamic_gain()
    test_fov_keeps_stick_feel()
    test_converges_under_camera_lag()
    test_pitch_closes_loop()
    test_neutral_preserves_target()
    test_deadzone_returns_stick_to_center()
    test_stale_snapshot_keeps_mouse_alive()
    test_level_jump_reanchors_silently()
    test_no_proc_no_cursor_lock()
    test_main_loop_does_not_steal_raw_pool()
    test_f2_anchor()
    test_watchdog_forces_neutral()
    test_seams()
    test_no_locate_layer_in_core()
    test_tracking_requires_proc()
    print("\n全部通过")

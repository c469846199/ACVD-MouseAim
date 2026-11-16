r"""
tools/launch.py — 一键启动器（给 .bat 快捷方式调用）

为什么把逻辑放这里而不是写在 .bat 里：
    cmd.exe 按系统 ANSI 代码页解析 .bat，文件里一旦有 UTF-8 中文，
    行会被从中间劈断，报"不是内部或外部命令"。而 Python 往控制台写
    中文走的是 WriteConsoleW，跟代码页无关，永远正确。
    所以：.bat 只保留 ASCII 的"跳板"，所有提示文字由本文件打印。

用法：
    python tools/launch.py core        四层内核（默认）
    python tools/launch.py legacy      旧内核（应急回退）
    python tools/launch.py stop        停止 mapper
    python tools/launch.py selftest    离线自检
    python tools/launch.py log         看实时日志
"""

import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAPPER = os.path.join(ROOT, "tools", "mapper.py")
PANEL = os.path.join(ROOT, "tools", "panel.py")

# ---- 封存闸门（2026-11-10）------------------------------------------------
# 这个项目已停止开发，改用同级的 acvd-openloop（开环鼠标映射）。
# 入口全部走这里，所以只要这个文件在，双击任何 .bat 都只会看到一行提示。
# 想临时启用：设环境变量 ACVD_UNFROZEN=1，或删掉 FROZEN.md。
_FROZEN = os.path.join(ROOT, "FROZEN.md")
if os.path.exists(_FROZEN) and not os.environ.get("ACVD_UNFROZEN"):
    print("=" * 60)
    print("  这个项目已于 2026-11-10 封存，不再开发。")
    print("  原因与保留结论见 FROZEN.md；开环替代方案：acvd-openloop")
    print("  临时启用：set ACVD_UNFROZEN=1")
    print("=" * 60)
    sys.exit(0)
CTL = os.path.join(ROOT, "tools", "ctl.py")
LOG = os.path.join(ROOT, "mapper.out.log")
sys.path.insert(0, ROOT)

LINE = "=" * 46
CREATE_NEW_CONSOLE = 0x00000010
SW_MINIMIZE = 6


def _say(*a):
    print(*a, flush=True)


def _probe(timeout_ms=800):
    """管道能不能连上 —— 也就是 mapper 在不在跑。"""
    try:
        from acvd.pipe import PipeClient
        return PipeClient("acvd_pad", timeout_ms=timeout_ms).send("status")
    except Exception:
        return None


def _start_mapper(engine: str) -> bool:
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = SW_MINIMIZE
    subprocess.Popen(
        [sys.executable, MAPPER, "--engine", engine],
        cwd=ROOT, startupinfo=si,
        creationflags=CREATE_NEW_CONSOLE,
        close_fds=True,
    )
    return True


def cmd_start(engine: str) -> int:
    tag = "四层解耦内核" if engine == "core" else "旧版内核（回退）"
    _say(LINE)
    _say(f"  ACVD 鼠标瞄准    内核：{tag}")
    _say(LINE)
    _say("")

    busy = _probe(600)
    if busy is not None:
        _say("! mapper 已经在跑了，不再启动第二个。")
        _say("  要换内核请先双击「停止.bat」。")
        _say("")
        return _start_panel()

    if engine == "core":
        try:
            from acvd.core.guard import check
            bad = check()
            if bad:
                _say("! 分层守卫发现违规，拒绝启动：")
                for b in bad:
                    _say("   " + b)
                return 3
            _say("分层守卫：通过（采集 / 决策 / 控制 / 输出 无跨层直连）")
        except Exception as e:
            _say(f"! 守卫检查异常：{e}")

    _say("")
    _say("启动 mapper（后台最小化窗口）...")
    _start_mapper(engine)

    for _ in range(60):
        time.sleep(0.25)
        if _probe(400) is not None:
            _say("✓ mapper 已就绪（管道 acvd_pad）")
            break
    else:
        _say("! 10 秒内没等到 mapper，去看那个最小化的控制台窗口。")
        return 4

    _say("")
    return _start_panel()


def _start_panel() -> int:
    _say("启动控制面板 ...")
    _say("（面板显示：等画面稳定 → 预扫候选 → 等你动杆 → 已接管）")
    _say("")
    try:
        subprocess.call([sys.executable, PANEL], cwd=ROOT)
    except KeyboardInterrupt:
        return 0
    _say("")
    _say("面板已关闭。mapper 仍在后台运行。")
    _say("要一起停掉请双击「停止.bat」。")
    return 0


def cmd_stop() -> int:
    _say("正在让 mapper 退出 ...")
    try:
        subprocess.call([sys.executable, CTL, "quit"], cwd=ROOT)
    except Exception as e:
        _say(f"! 停止失败：{e}")
        return 1
    return 0


def cmd_selftest() -> int:
    _say(LINE)
    _say("  离线自检（不影响游戏，不用开 Xenia）")
    _say(LINE)
    _say("")
    tests = [
        ("绝对坐标内核：分层守卫 / 控制器 / 两态 / 底层保护",
         "tools/test_core.py"),
    ]
    fails = 0
    for i, (name, rel) in enumerate(tests, 1):
        _say(f"[{i}/{len(tests)}] {name}")
        rc = subprocess.call([sys.executable, os.path.join(ROOT, rel)], cwd=ROOT)
        if rc != 0:
            fails += 1
            _say(f"    ✗ 失败（返回码 {rc}）")
        _say("")
    _say(LINE)
    if fails:
        _say(f"  {fails} 项失败，别用这个版本。")
    else:
        _say("  全部通过。")
    _say(LINE)
    return 0 if not fails else 5


def cmd_log() -> int:
    if not os.path.exists(LOG):
        _say("还没有日志文件（mapper 从未启动过？）")
        return 1
    _say("实时跟踪 mapper.out.log（Ctrl+C 退出，不影响 mapper）")
    _say("")
    try:
        subprocess.call([
            "powershell", "-NoProfile", "-Command",
            f"Get-Content -Path '{LOG}' -Tail 40 -Encoding UTF8 -Wait"])
    except KeyboardInterrupt:
        pass
    except Exception as e:
        _say(f"! 无法调用 PowerShell：{e}")
        return 1
    return 0


def main(argv) -> int:
    what = (argv[0] if argv else "core").lower()
    if what in ("core", "legacy"):
        return cmd_start(what)
    if what in ("stop", "quit"):
        return cmd_stop()
    if what in ("selftest", "test"):
        return cmd_selftest()
    if what in ("log", "logs"):
        return cmd_log()
    _say(__doc__)
    return 2


if __name__ == "__main__":
    code = main(sys.argv[1:])
    # 双击运行时留住窗口，让人看得见结果
    if sys.stdin and sys.stdin.isatty():
        try:
            input("\n按回车关闭窗口 ...")
        except Exception:
            pass
    raise SystemExit(code)

r"""
tools/exe_entry.py — 打包成 EXE 时的统一入口

## 为什么需要这个文件

源码模式靠 `launch.py` 工作，它的做法是：

    subprocess.Popen([sys.executable, "tools/mapper.py", ...])

也就是「拿 python 解释器去跑一个 .py 文件」。打包成 EXE 之后：

  * 目标机器上没有 Python 解释器（`sys.executable` 变成了 EXE 自己）
  * 也没有 `tools/mapper.py` 这个文件（都被塞进 EXE 里了）

所以那条路直接断掉。本文件替代它：**同一个 EXE 根据命令行参数决定
扮演哪个角色**，不再 spawn 解释器。

## 用法（对最终玩家来说）

    ACVD.exe               启动 mapper（常驻，后台最小化窗口）
    ACVD.exe panel         启动控制面板（GUI，可关，mapper 继续跑）
    ACVD.exe stop          让 mapper 优雅退出
    ACVD.exe legacy        用旧内核启动（应急回退）
    ACVD.exe --help        看这份说明

## 打包时的关键约束（改打包脚本时务必保持）

1. **mapper 与 panel 必须以顶层模块名导入**（`import mapper` / `import panel`），
   不是 `tools.mapper` —— 因为 `tools/` 目录没有 `__init__.py`，不是包。
   打包脚本要把 `tools/` 加进 `pathex`，让 PyInstaller 把它们当顶层模块收集。

2. **配置文件放在 EXE 旁边**，不放在 EXE 内部。玩家需要能直接编辑
   `keymap.json` / `tune.json`，打进 EXE 里就改不了了。
   路径解析见 `mapper.app_root()`。

3. **不要在这里读任何游戏内存**。本文件只做分发，真正的逻辑在
   `mapper.py`（输入/输出编排）和 `acvd/core/`（瞄准内核）里。
"""

from __future__ import annotations

import os
import sys

# 打包后本文件所在目录就是 EXE 目录；源码运行时是 tools/ 的上一级项目根。
# 这个函数同时给「找不到配置时给出可读提示」用。
def app_root() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    here = os.path.abspath(__file__)                  # .../tools/exe_entry.py
    return os.path.dirname(os.path.dirname(here))     # .../（项目根）


USAGE = """ACVD 鼠标瞄准（固定相机 · 绝对坐标内核）

  ACVD.exe            双击启动：后台起 mapper + 打开控制面板
  ACVD.exe mapper     只起 mapper（控制台常驻，看日志用）
  ACVD.exe panel      只打开控制面板
  ACVD.exe keys       打开按键映射（全键盘点选改键）
  ACVD.exe stop       停止 mapper
  ACVD.exe legacy     旧内核启动（应急回退）
  ACVD.exe --help     显示本说明

配置文件（与本程序同目录，可直接用记事本改）：
  keymap.json     键位与热键
  tune.json       手感参数
  aim_cam.json    相机地址缓存（自动生成，删掉也能重建）

依赖：ViGEmBus 驱动（虚拟手柄）。没装的话启动时会明确报错。
"""


def _say(*a):
    print(*a, flush=True)


def _send_pipe(cmd: str, timeout_ms: int = 2000):
    """给正在运行的 mapper 发一条管道命令。连不上返回 None。"""
    try:
        from acvd.pipe import PipeClient
        return PipeClient("acvd_pad", timeout_ms=timeout_ms).send(cmd)
    except Exception:
        return None


def _hide_own_console() -> None:
    """把本进程自带的控制台窗口藏起来。

    为什么需要：PyInstaller 打包时 console=True（mapper 要靠它显示日志），
    于是双击启动器时也会先弹一个黑框。但玩家双击是想要界面，不是想看
    启动脚本在黑框里滚日志 —— 那个黑框留着只会让人以为程序卡住了。

    日志没有丢：mapper 子进程有自己的控制台（最小化），且所有输出同时
    写进 mapper.out.log。真要看，从控制面板或直接开日志文件都能看。
    """
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)      # 0 = SW_HIDE
    except Exception:
        pass


def _spawn_mapper() -> bool:
    """后台起一个 mapper 子进程（自己的另一个实例，带 mapper 子命令）。

    为什么不直接在本进程里跑 mapper：mapper 是常驻阻塞的（while 循环 +
    125Hz 主循环），本进程得留着跑 GUI 主循环。两个都是死循环，必须分开
    进程，否则面板一开就把 mapper 卡死。

    控制台给最小化而不是隐藏：万一 mapper 启动就崩（比如没装 ViGEmBus），
    那个黑框里会有错误信息，玩家点开就能看到，也方便排查。
    """
    import subprocess
    CREATE_NEW_CONSOLE = 0x00000010
    SW_MINIMIZE = 6
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = SW_MINIMIZE
        subprocess.Popen(
            [sys.executable, "mapper"],
            cwd=app_root(),
            startupinfo=si,
            creationflags=CREATE_NEW_CONSOLE,
            close_fds=True,
        )
        return True
    except Exception as e:
        print(f"! 启动 mapper 失败：{e}", flush=True)
        return False


def run_diag() -> int:
    """排查用：把「找 Xenia 进程」这条链的每一步单独跑一遍并打印结果。

    为什么需要它：mapper 的 auto_get_proc() 用 try/except 包住了 attach_xenia()，
    失败只退避不报错 —— 这在正常运行下是对的（游戏没开时不该刷屏），
    但排障时就变成了「什么也不说，就是连不上」。这个子命令把每一步的
    真实返回值和异常都摊开，不用猜。
    """
    import traceback

    _say("=== 环境 ===")
    _say(f"frozen      = {getattr(sys, 'frozen', False)}")
    _say(f"executable  = {sys.executable}")
    _say(f"app_root()  = {app_root()}")
    _say(f"cwd         = {os.getcwd()}")

    _say("\n=== 进程枚举 ===")
    try:
        import ctypes
        import ctypes.wintypes as wt

        _say(f"sys.maxsize = {sys.maxsize}（32 位是 2147483647）")
        _say(f"platform = {sys.platform}, "
             f"ctypes.sizeof(c_void_p) = {ctypes.sizeof(ctypes.c_void_p)}")

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
        k32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
        k32.Process32First.argtypes = [wt.HANDLE, ctypes.c_void_p]
        k32.Process32Next.argtypes = [wt.HANDLE, ctypes.c_void_p]
        k32.CloseHandle.argtypes = [wt.HANDLE]

        class PE(ctypes.Structure):
            _fields_ = [
                ("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
                ("th32ProcessID", wt.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                ("th32ParentProcessID", wt.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
                ("szExeFile", wt.CHAR * 260)]

        _say(f"sizeof(PROCESSENTRY32) = {ctypes.sizeof(PE)}")

        snap = k32.CreateToolhelp32Snapshot(0x00000002, 0)
        _say(f"snapshot = {snap}  err={ctypes.get_last_error()}")

        pe = PE()
        pe.dwSize = ctypes.sizeof(PE)
        want = pe.dwSize
        ok = k32.Process32First(snap, ctypes.byref(pe))
        _say(f"Process32First = {ok}  err={ctypes.get_last_error()}")
        _say(f"调用后 dwSize = {pe.dwSize}（传入 {want}，"
             f"{'一致' if pe.dwSize == want else '被 API 改写过！'}）")

        total = 0
        xen_hits = []
        first_five, last_five = [], []
        stall_note = ""
        last_pid = None
        ok = True
        while ok and total < 5000:
            raw = bytes(pe.szExeFile)
            try:
                name = raw.split(b"\x00")[0].decode("mbcs", "replace")
            except Exception as e:
                name = f"<decode-{type(e).__name__}>"
            total += 1
            if total <= 5:
                first_five.append(f"{pe.th32ProcessID}:{name}")
            if "xenia" in name.lower():
                xen_hits.append(f"{pe.th32ProcessID}:{name}")
            if pe.th32ProcessID == last_pid and total > 1 and not stall_note:
                stall_note = f"第 {total} 个与上一个 pid 相同（{last_pid}）"
            last_pid = pe.th32ProcessID
            last_five.append(f"{pe.th32ProcessID}:{name}")
            last_five = last_five[-5:]

            ok = k32.Process32Next(snap, ctypes.byref(pe))
            if not ok:
                err = ctypes.get_last_error()
                # ERROR_NO_MORE_FILES = 18，是正常结束
                _say(f"Process32Next 返回 0，err={err}"
                     f"{'（ERROR_NO_MORE_FILES，正常结束）' if err == 18 else '（异常）'}")
                break
        k32.CloseHandle(snap)

        _say(f"\n共枚举 {total} 个进程（源码模式实测 252 左右）")
        _say(f"前 5 个: {first_five}")
        _say(f"末 5 个: {last_five}")
        _say(f"含 xenia: {xen_hits if xen_hits else '无 —— 这就是找不到的原因'}")
        if stall_note:
            _say(f"! {stall_note}")

        # ---- 第二种枚举方式：EnumProcesses（和 Toolhelp 完全不同的 API）----
        # 如果两者数量差很多，说明问题不在 Toolhelp 结构体，而在进程可见性
        _say("\n=== 交叉验证：EnumProcesses ===")
        try:
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            arr = (wt.DWORD * 4096)()
            needed = wt.DWORD()
            ok2 = psapi.EnumProcesses(ctypes.byref(arr),
                                      ctypes.sizeof(arr),
                                      ctypes.byref(needed))
            if ok2:
                cnt = needed.value // ctypes.sizeof(wt.DWORD)
                _say(f"EnumProcesses = {cnt} 个")
            else:
                _say(f"EnumProcesses 失败 err={ctypes.get_last_error()}")
        except Exception as e:
            _say(f"EnumProcesses 异常: {type(e).__name__}: {e}")
    except Exception:
        _say("! 枚举异常：\n" + traceback.format_exc())

    try:
        from acvd.focus import _find_pid_by_prefix
        pid = _find_pid_by_prefix("xenia")
        _say(f"_find_pid_by_prefix('xenia')      = {pid}")
    except Exception:
        _say("! _find_pid_by_prefix 异常：\n" + traceback.format_exc())

    try:
        from acvd.focus import _find_pid_by_window_title
        wpid = _find_pid_by_window_title("xenia-edge")
        _say(f"_find_pid_by_window_title(...)    = {wpid}"
             f"    ← 枚举失败时的实际生效路径")
    except Exception:
        _say("! _find_pid_by_window_title 异常：\n" + traceback.format_exc())

    try:
        from acvd.winmem import _resolve_pid
        _say(f"_resolve_pid('xenia')             = {_resolve_pid('xenia')}")
    except Exception:
        _say("! _resolve_pid 异常：\n" + traceback.format_exc())

    _say("\n=== 连接（走 attach_xenia 的真实逻辑）===")
    try:
        from acvd.winmem import Process
        from acvd.focus import _find_pid_by_prefix, _find_pid_by_window_title

        pid = _find_pid_by_prefix("xenia")
        if pid is None:
            pid = _find_pid_by_window_title("xenia-edge")
        if pid is None:
            _say("! 两条路都没找到 Xenia")
        else:
            p = Process.attach(pid)
            _say(f"Process.attach({pid}) = 成功, alive={p.alive()}")
            try:
                from acvd.gmem import GuestMem
                gm = GuestMem(p, GuestMem.KNOWN_BASE)
                q = gm.read(0x405EA750, 32)
                _say(f"相机 32 字节 = {q.hex() if q else None}")
                _say(f"相机可读 = {q is not None}")
            except Exception:
                _say("! 读相机异常：\n" + traceback.format_exc())
            p.close()
    except Exception:
        _say("! 连接异常：\n" + traceback.format_exc())

    return 0


def run_default() -> int:
    """双击时的默认行为：确保 mapper 在跑，然后打开控制面板。

    顺序很重要 —— 先确认 mapper 起来（管道能连上），再开面板；
    否则面板一上来就是一片「mapper 未运行」，像是坏了。

    控制台**在确认 mapper 就绪之后**才隐藏：如果 spawn 失败或等不到管道，
    错误信息还得让玩家看见（隐藏了就成了一闪而过的黑框，什么也排查不了）。
    """
    import time

    if _send_pipe("status", 900) is None:
        if not _spawn_mapper():
            _say("! 无法启动 mapper。请在命令行运行 ACVD.exe mapper 查看原因。")
            return 1
        # 等管道就绪（最多 10 秒，和 launch.py 的等待时长一致）
        for _ in range(40):
            time.sleep(0.25)
            if _send_pipe("status", 400) is not None:
                break
        else:
            _say("! 10 秒内没等到 mapper 就绪，请检查那个最小化的控制台窗口。")
            return 4

    _hide_own_console()          # 到这里启动器没用了，把黑框收掉
    return run_panel()


def run_mapper(engine: str = "core") -> int:
    """在本进程里直接跑 mapper。

    为什么要重写 sys.argv：mapper.main() 内部用 argparse 解析自己的参数
    （--engine / --hz / --fov / --crosshair 等）。如果我们不把 argv 收拾干净，
    它会把本入口的参数（比如 "start"）当成未知选项直接 argparse 报错退出。
    所以这里构造一份「只有它认识的东西」的 argv。
    """
    sys.argv = [sys.argv[0]] + (["--engine", engine] if engine else [])
    import mapper                      # 顶层模块名（见文件头「关键约束 1」）
    return mapper.main()


def run_panel() -> int:
    """在本进程里直接跑控制面板（tkinter GUI，阻塞到窗口关闭）。"""
    sys.argv = [sys.argv[0]]
    import panel
    return panel.main()


def run_keymap_ui() -> int:
    """打开按键映射窗口（独立进程，不依赖 mapper 是否在跑）。

    改键写的是 app_root()/keymap.json —— 和 mapper 读的同一份。
    改完会自动给 mapper 发 reload；mapper 没在跑就等它下次启动生效。
    """
    sys.argv = [sys.argv[0]]
    import keymap_ui
    return keymap_ui.main()


def run_stop() -> int:
    """让 mapper 优雅退出：发 quit，然后等管道消失确认真退了。"""
    import time
    if _send_pipe("status", 800) is None:
        _say("mapper 没在运行。")
        _say(f"  提示：配置目录 {app_root()}")
        return 1
    _say("正在让 mapper 退出 ...")
    _send_pipe("quit", 2000)
    for _ in range(60):               # 最多等 9 秒（60 × 150ms）
        time.sleep(0.15)
        if _send_pipe("status", 400) is None:
            _say("✓ mapper 已退出")
            return 0
    _say("! 9 秒内没有退出，可能卡在驱动里，请在任务管理器里结束它。")
    return 2


def main(argv) -> int:
    what = (argv[0] if argv else "start").lower()

    if what in ("-h", "--help", "/?", "help"):
        _say(USAGE)
        return 0
    if what in ("", "start"):
        return run_default()            # 双击：后台起 mapper + 开面板
    if what in ("mapper", "core"):
        return run_mapper("core")       # 只起 mapper（控制台常驻）
    if what == "legacy":
        return run_mapper("legacy")
    if what in ("panel", "gui"):
        return run_panel()
    if what in ("keys", "keymap", "bind"):
        return run_keymap_ui()
    if what in ("diag", "doctor", "check"):
        return run_diag()
    if what in ("stop", "quit", "exit"):
        return run_stop()

    _say(f"不认识的参数：{what}\n")
    _say(USAGE)
    return 2


if __name__ == "__main__":
    code = main(sys.argv[1:])
    # 双击运行时留住窗口，否则报错信息一闪而过，玩家什么也看不到。
    # isatty() 判断「是不是交互式控制台」：管道/重定向调用时不留。
    if sys.stdin and sys.stdin.isatty():
        try:
            input("\n按回车关闭窗口 ...")
        except Exception:
            pass
    raise SystemExit(code)

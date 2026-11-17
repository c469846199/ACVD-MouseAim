"""一键诊断：虚拟手柄是否被 Xenia 接收。

## 用途

当「游戏里键盘/鼠标完全没反应」时跑这个，它会按顺序验证：

    1. 虚拟手柄是否存在于 XInput（mapper 有没有成功创建）
    2. mapper 是否在推杆（强制推一次，看 XInput 值变不变）
    3. Xenia 进程是否在跑、用的哪个输入后端（hid 配置）
    4. Xenia 是否被识别为接收方

## 用法

    python tools\\diag_input.py

不需要游戏在关卡内，但 mapper 必须已启动。
"""
import ctypes
import ctypes.wintypes as wt
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from acvd.focus import _find_pid_by_prefix, _find_pid_by_window_title  # noqa: E402

xinput = ctypes.WinDLL("xinput1_4")


class XINPUT_STATE(ctypes.Structure):
    _fields_ = [
        ("dwPacketNumber", wt.DWORD),
        ("wButtons", wt.WORD),
        ("bLeftTrigger", wt.BYTE),
        ("bRightTrigger", wt.BYTE),
        ("sThumbLX", wt.SHORT),
        ("sThumbLY", wt.SHORT),
        ("sThumbRX", wt.SHORT),
        ("sThumbRY", wt.SHORT),
    ]


def read_pad(slot=0):
    st = XINPUT_STATE()
    if xinput.XInputGetState(slot, ctypes.byref(st)) == 0:
        return st
    return None


def main() -> int:
    print("=" * 70)
    print("  输入链路诊断")
    print("=" * 70)
    print()

    # ---- 1. 虚拟手柄是否存在 ----
    print("[1] 虚拟手柄（XInput 槽位）")
    found = False
    for i in range(4):
        st = read_pad(i)
        if st is not None:
            found = True
            print(f"    槽位 {i}: 存在  packet={st.dwPacketNumber} "
                  f"btn=0x{st.wButtons:04X} "
                  f"L=({st.sThumbLX:6d},{st.sThumbLY:6d}) "
                  f"R=({st.sThumbRX:6d},{st.sThumbRY:6d})")
    if not found:
        print("    ✗ 没有任何 XInput 设备")
        print("      → ViGEmBus 没装，或 mapper 没启动")
        return 1
    print()

    # ---- 2. mapper 是否在推杆 ----
    print("[2] mapper 推杆测试")
    try:
        from acvd.pipe import PipeClient
        cli = PipeClient("acvd_pad", timeout_ms=2000)
        st = cli.send("status")
        d = dict(kv.split("=", 1) for kv in st.split() if "=" in kv)
        print(f"    管道连接: OK")
        print(f"    halt      = {d.get('halt')}   ← on 的话手柄整份不发！")
        print(f"    paused    = {d.get('paused')}")
        print(f"    tracking  = {d.get('tracking')}")
        print(f"    ok/bad    = {d.get('ok')}/{d.get('bad')}")

        if d.get("halt") == "on":
            print()
            print("    ! halt=on（F10 全部暂停）：摇杆和按键都不会发出去")
            print("      解除：管道发 halt off，或在面板上点「全部暂停」")
    except Exception as e:
        print(f"    ✗ 管道无响应: {e}")
        print("      → mapper 没在运行")
        return 1

    print()
    print("    强制推杆 2 秒，观察 XInput 变化 ...")
    before = read_pad(0)
    cli.send("r 0.9 0.0")
    time.sleep(1.0)
    during = read_pad(0)
    cli.send("center")
    time.sleep(0.3)
    after = read_pad(0)

    if before and during:
        print(f"      推杆前 R=({before.sThumbRX:6d})  "
              f"推杆中 R=({during.sThumbRX:6d})  "
              f"回中后 R=({after.sThumbRX if after else 0:6d})")
        if during.sThumbRX != before.sThumbRX:
            print("      ✓ 虚拟手柄确实在输出 —— 系统层面没问题")
        else:
            print("      ✗ 推杆命令无效，手柄值没变")
    print()

    # ---- 3. Xenia 状态 ----
    print("[3] Xenia 接收方")
    pid = _find_pid_by_prefix("xenia") or _find_pid_by_window_title("xenia")
    if pid is None:
        print("    ✗ 找不到 Xenia 进程")
        return 1
    print(f"    pid = {pid}")
    import subprocess
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"(Get-Process -Id {pid}).MainWindowTitle"],
        capture_output=True, text=True)
    print(f"    窗口 = {r.stdout.strip()[:70]}")

    # 找配置文件看 hid
    exe_path = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"(Get-Process -Id {pid}).Path"],
        capture_output=True, text=True).stdout.strip()
    if exe_path:
        exe_dir = os.path.dirname(exe_path)
        print(f"    目录 = {exe_dir}")
        for name in os.listdir(exe_dir):
            if name.endswith(".toml") and "netplay" in name.lower() \
                    or name == "xenia-edge.config.toml":
                p = os.path.join(exe_dir, name)
                try:
                    with open(p, encoding="utf-8", errors="ignore") as f:
                        for line in f:
                            s = line.strip()
                            if s.startswith("hid ="):
                                print(f"    {name}: {s}")
                except OSError:
                    pass
    print()

    # ---- 4. 结论 ----
    print("=" * 70)
    print("  判读")
    print("=" * 70)
    if found and during and during.sThumbRX != before.sThumbRX:
        print("  虚拟手柄在正常输出。如果游戏里还是没反应，问题在 Xenia 侧：")
        print()
        print("    · 输入后端不对：配置文件 [HID] 段的 hid 要设成 \"xinput\"")
        print("      （Netplay 默认是 \"any\"，可能没选到 XInput 设备）")
        print("    · 改完必须**完全重启 Xenia** 才生效")
        print()
        print("    · Netplay 版还有 [HID.WinKey] 段把键盘映射成手柄，")
        print("      并且 keyboard_user_index = 0 会和虚拟手柄抢 0 号端口。")
        print("      必要时把 keyboard_mode 设为 0（Disabled）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

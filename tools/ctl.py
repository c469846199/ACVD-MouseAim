r"""
tools/ctl.py — 给 .bat 快捷方式用的管道控制客户端。

用法：
    python tools\ctl.py status        查状态
    python tools\ctl.py quit          优雅退出 mapper
    python tools\ctl.py auto off      关掉自动定位
    python tools\ctl.py auto on       打开
    python tools\ctl.py calib         手动重定位（等价 F1）

它只做一件事：连管道、发一条命令、把回显打出来。
连接失败就明说"没在运行"，不抛一堆栈吓人 —— 快捷方式是给玩家用的，
不是给调试用的。
"""

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from acvd.pipe import PipeClient  # noqa: E402


def send(cmd: str, timeout: int = 2000):
    """发一条命令。mapper 没在运行时返回 None。"""
    try:
        return PipeClient("acvd_pad", timeout_ms=timeout).send(cmd)
    except Exception:
        return None


def main(argv) -> int:
    cmd = " ".join(argv).strip() or "status"
    head = cmd.split()[0].lower()

    reply = send(cmd)
    if reply is None:
        print("mapper 没在运行（管道 acvd_pad 连不上）。")
        print("要先双击「启动.bat」。")
        return 1
    print(reply)

    if head in ("quit", "exit", "q"):
        for _ in range(60):
            time.sleep(0.15)
            if send("status", 400) is None:
                print("\n✓ mapper 已退出")
                return 0
        print("\n! 9 秒内没有退出，可能卡在驱动里，请手动关掉窗口")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

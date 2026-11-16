"""tools/padd.py — 常驻虚拟手柄守护（只建一次、保持不拆、固定在 Xenia 槽位 1）

## 为什么要常驻
Xenia 会把「输入设备突然消失」判定为自动暂停。之前每个实验各建/拆一次
VirtualPad，来回插拔就把游戏弄暂停了，之后推什么都像死了一样。
这个进程建一次设备就不再拆，后续实验通过 **文件命令** 下指令，
不再碰插拔，也不会抢焦点、不弹窗。

## 用法（后台静默）
    python -m tools.padd            # 一直跑，直到 scratch/padd.stop 出现
    python -m tools.padd --status   # 只读查询当前状态

## 下指令（写 scratch/padd.cmd，JSON，一行）
    {"right": [0.37, 0.0]}              # 设右摇杆并保持（不自动回中）
    {"left": [0, 0.5], "triggers": [0, 0]}
    {"buttons": 4096}                    # XInput 按钮掩码
    {"hold": 0.8}                        # 可选：与上面同一条，保持 N 秒后自动回中
    {"neutral": true}                    # 全回中
    {"status": true}                     # 只查询，不改状态

执行完会删掉 cmd，把结果写 scratch/padd.result（JSON）。
状态文件 scratch/padd.status 每 0.3s 刷新一次。

## 安全
- 只用 PostMessageW 发 WM_COMMAND 到 Xenia 菜单，**不 SetForegroundWindow**，
  不会因为用户正在玩别的游戏而抢焦点。
- 设备全程保持回中，除非收到命令；中立手柄不会干扰其它游戏。
- 退出（stop）才会拆设备 —— 所以不要随便停，停了 Xenia 可能自动暂停。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from acvd.pad import VirtualPad, detect_slots            # noqa: E402
from acvd.xenia_ui import ensure_slot, slot_label, tree   # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = ROOT / "scratch"
CMD = SCRATCH / "padd.cmd"
RESULT = SCRATCH / "padd.result"
STATUS = SCRATCH / "padd.status"
STOP = SCRATCH / "padd.stop"
DEVICE = "Xbox 360 Controller"
SLOT = 1


def _write(path: Path, obj: dict) -> None:
    SCRATCH.mkdir(exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def neutral(pad: VirtualPad) -> None:
    pad.left(0.0, 0.0)
    pad.right(0.0, 0.0)
    pad.triggers(0.0, 0.0)
    pad.set_buttons(0)
    pad.update()


def main() -> None:
    SCRATCH.mkdir(exist_ok=True)
    if "--status" in sys.argv:
        try:
            print(json.dumps(json.loads(STATUS.read_text(encoding="utf-8")),
                             ensure_ascii=False, indent=1))
        except Exception as e:
            print(f"状态文件读不了：{e}")
        return

    # 旧信号清掉，避免一启动就退
    for p in (STOP, CMD, RESULT):
        try:
            p.unlink()
        except FileNotFoundError:
            pass

    pad = VirtualPad()                       # ★ 全局仅此一次
    time.sleep(0.5)
    xinput = detect_slots()
    print(f"虚拟手柄已建立，XInput 槽位 {xinput}", flush=True)

    assigned = ensure_slot(DEVICE, SLOT, wait=6.0, verbose=True)
    label = slot_label(SLOT) or ""
    state = {"lx": 0.0, "ly": 0.0, "rx": 0.0, "ry": 0.0,
             "lt": 0.0, "rt": 0.0, "btn": 0}
    born = time.time()
    print(f"槽位 {SLOT} = {label or '(读不到)'}", flush=True)

    t_status = 0.0
    try:
        while True:
            now = time.time()
            if STOP.exists():
                print("收到停止信号", flush=True)
                break

            if CMD.exists():
                try:
                    c = json.loads(CMD.read_text(encoding="utf-8"))
                    res = {"ts": time.strftime("%H:%M:%S"), "cmd": c}
                    if c.get("neutral"):
                        neutral(pad)
                        state = dict.fromkeys(state, 0.0)
                        state["btn"] = 0
                        res["ok"] = "已回中"
                    elif not c.get("status"):
                        for key in ("lx", "ly", "rx", "ry", "lt", "rt", "btn"):
                            if key in c:
                                state[key] = c[key]
                        if "left" in c:
                            state["lx"], state["ly"] = c["left"][0], c["left"][1]
                        if "right" in c:
                            state["rx"], state["ry"] = c["right"][0], c["right"][1]
                        if "triggers" in c:
                            state["lt"], state["rt"] = c["triggers"][0], c["triggers"][1]
                        if "buttons" in c:
                            state["btn"] = c["buttons"]
                        pad.left(state["lx"], state["ly"])
                        pad.right(state["rx"], state["ry"])
                        pad.triggers(state["lt"], state["rt"])
                        pad.set_buttons(int(state["btn"]))
                        pad.update()
                        res["ok"] = "已应用"
                        if c.get("hold"):
                            time.sleep(float(c["hold"]))
                            neutral(pad)
                            state = dict.fromkeys(state, 0.0)
                            state["btn"] = 0
                            res["ok"] = f"已应用并回中（{c['hold']}s）"
                    res["state"] = dict(state)
                    _write(RESULT, res)
                except Exception as e:
                    _write(RESULT, {"error": str(e)})
                finally:
                    try:
                        CMD.unlink()
                    except FileNotFoundError:
                        pass

            if now - t_status > 0.3:
                t_status = now
                _write(STATUS, {
                    "pid": os.getpid(),
                    "alive": True,
                    "uptime_s": round(now - born, 1),
                    "xinput_slots": detect_slots(),
                    "slot1": slot_label(1),
                    "assigned": assigned and DEVICE in (slot_label(1) or ""),
                    "state": dict(state),
                })
            time.sleep(0.1)
    finally:
        neutral(pad)
        _write(STATUS, {
            "pid": os.getpid(), "alive": False,
            "uptime_s": round(time.time() - born, 1),
            "slot1": slot_label(1), "note": "设备已拆除，Xenia 可能自动暂停",
        })
        pad.close()
        print("已退出（设备拆除）", flush=True)


if __name__ == "__main__":
    main()

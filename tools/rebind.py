"""
tools/rebind.py — 交互式改键向导（不用懂 Python）

用法：
    1. 先关掉 mapper（ScrollLock 退出），否则改完要按 reload 才生效
    2. python tools\\rebind.py
    3. 按提示一个个按你要绑的键：回车=保留原样，d=删掉这个绑定
    4. 改完自动写入 keymap.json，重启 mapper 即生效

也可以只改某几项：python tools\\rebind.py a b lt
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from acvd.keynames import MOUSE_VK, vk_name  # noqa: E402
from acvd.keynames import pressed_vks  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEYMAP_PATH = os.path.join(ROOT, "keymap.json")

# 顺序 = 向导提问顺序。名 -> (中文说明, 默认值)
ACTIONS = [
    ("a", "A 键", "space"),
    ("b", "B 键", "c"),
    ("x", "X 键", "r"),
    ("y", "Y 键", "v"),
    ("lb", "LB（左肩）", "tab"),
    ("rb", "RB（右肩）", "f"),
    ("lt", "LT 扳机（可多键）", ["q", "mouse_left"]),
    ("rt", "RT 扳机（可多键）", ["e", "mouse_right"]),
    ("ls", "LS 按下（左摇杆）", ["lshift", "rshift"]),
    ("rs", "RS 按下（右摇杆）", "mouse_middle"),
    ("start", "START", "x"),
    ("back", "BACK", "z"),
    ("left_up", "左摇杆 前", "w"),
    ("left_down", "左摇杆 后", "s"),
    ("left_left", "左摇杆 左", "a"),
    ("left_right", "左摇杆 右", "d"),
    ("right_up", "方向键 上", "up"),
    ("right_down", "方向键 下", "down"),
    ("right_left", "方向键 左", "left"),
    ("right_right", "方向键 右", "right"),
    ("hotkey_calibrate", "热键：重新标定", "f1"),
    ("hotkey_recenter", "热键：归中", "f2"),
    ("hotkey_pause", "热键：暂停映射", "f9"),
    ("hotkey_force", "热键：强制映射", "f10"),
    ("hotkey_quit", "热键：退出 mapper", "scrolllock"),
]

MULTI = {"lt", "rt", "ls"}


def ask(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:
        return ""


def capture_one(what: str, timeout: float = 15.0):
    """等一个新按下的键，返回名字；超时返回 None。"""
    print(f"   请按【{what}】... （{int(timeout)} 秒内，Esc 取消）", end="", flush=True)
    prev = pressed_vks()
    t0 = time.time()
    while time.time() - t0 < timeout:
        now = pressed_vks()
        new = now - prev
        if new:
            vk = sorted(new)[0]
            if vk == 0x1B:            # Esc
                print(" 已取消")
                return None
            if vk in MOUSE_VK:
                name = f"mouse_{MOUSE_VK[vk]}"
            else:
                name = vk_name(vk)
            print(f" {name}")
            return name
        prev = now
        time.sleep(0.01)
    print(" 超时")
    return None


def main() -> int:
    only = set(sys.argv[1:])
    data = {}
    if os.path.exists(KEYMAP_PATH):
        try:
            data = json.load(open(KEYMAP_PATH, encoding="utf-8"))
        except Exception as e:
            print(f"! 现有 keymap.json 读取失败（将重写）：{e}")
            data = {}
    defaults = {key: default for key, _label, default in ACTIONS}

    print("=" * 62)
    print(" ACVD 键位向导")
    print("  直接按想要的新键即可；回车=不改；输入 d 回车=清空该绑定")
    print("=" * 62)

    for key, label, default in ACTIONS:
        if only and key not in only:
            continue
        cur = data.get(key, default)
        shown = cur if isinstance(cur, list) else [cur]
        print(f"\n[{key}] {label}   当前: {' + '.join(map(str, shown))}")
        ans = ask("  按回车保留 / d 清空 / 直接按 s 开始录键 > ")
        if ans.strip() == "":
            continue
        if ans.strip().lower() == "d":
            data[key] = None
            continue
        # 录一条或多条
        picked = []
        msg = f"{key} 的第一个键"
        while True:
            name = capture_one(msg)
            if name is None:
                break
            picked.append(name)
            if key not in MULTI:
                break
            more = ask("  再加一个键？(y/N) > ").strip().lower()
            if more != "y":
                break
            msg = f"{key} 的追加键"
        if picked:
            data[key] = picked if len(picked) > 1 else picked[0]
        else:
            print("  没有录到键，保留原样")

    if not data:
        print("\n没有做任何修改，keymap.json 未写入。")
        return 0
    header = (
        "// ===================================================================\n"
        "//  ACVD 键位文件（本文件由 tools\\rebind.py 生成，也可手改）\n"
        "//  改完生效：给 mapper 发管道命令 reload\n"
        "//  注意：F3~F8 / F11 / F12 / Esc / Pause 被 xenia-edge 占用，别绑\n"
        "// ===================================================================\n"
    )
    with open(KEYMAP_PATH, "w", encoding="utf-8", newline="\n") as f:
        f.write(header)
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"\n已写入 {KEYMAP_PATH}")
    print("重启 mapper 生效（或对 mapper 发 reload 管道命令）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

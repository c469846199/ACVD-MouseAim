r"""
tools/keymap_ui.py — 按键映射图形界面（全键盘 + 点击改键）

===============================================================================
一、为什么要有它
===============================================================================

`tools/rebind.py` 是命令行版：它按固定顺序一个个问「A 键绑什么？」，
用户必须知道功能名、按顺序回答、还要输入 y/N。对不熟悉这套术语的人不友好。

这个界面换个方向：**把物理键盘画出来，点哪个键就改哪个键**。
键盘上每个键会显示它当前绑定的功能，一目了然。

===============================================================================
二、数据流（先看这个，再看代码）
===============================================================================

    keymap.json          权威数据：{功能名: 物理键名 或 [物理键名...]}
        │                （例 "a": "space"，"lt": ["q","mouse_left"]）
        │  读
        ▼
    build_reverse()      反查表：{物理键名: [功能名...]}
        │                （例 "space": ["a"]）
        │  画
        ▼
    键盘 UI              每个键显示「物理键名 + 它承载的功能」
        │  用户点击
        ▼
    监听 → 新键 P       把该键承载的所有功能，从旧键改绑到 P
        │  写
        ▼
    keymap.json 落盘     保留原文件的注释与排版，只改受影响的行
        │
        ▼
    发 reload 通知 mapper（正在跑的话）

**为什么用「反查」而不是直接拿功能名画按钮**：
功能名是 "a"/"lt"/"left_up" 这种，用户看不出它在键盘哪里。
画物理键盘 + 标注功能，才符合「我要把 A 键从空格挪到 G」的直觉。

===============================================================================
三、状态机（与需求逐条对应，改之前先读这段）
===============================================================================

    IDLE
      ├─ 点击虚拟键 K（且 K 上有绑定）  → LISTENING(K)，K 高亮
      ├─ 点击虚拟键 K（K 上无绑定）      → 只变灰提示，不进监听
      └─ 关闭窗口                        → 退出

    LISTENING(K)
      ├─ 普通按键 P                      → 冲突检测 → 改绑 K→P → IDLE
      ├─ ESC                             → 取消，不写盘 → IDLE
      ├─ DEL                             → 清空 K 上所有绑定 → 立即写盘 → IDLE
      ├─ 点击另一个键 K2                 → 取消当前监听，选中 K2 → LISTENING(K2)
      └─ 关闭窗口                        → 取消，不写盘 → 退出

## 两条容易写错的优先级（改代码时务必保持）

【PRIORITY】ESC / DEL 的判断必须放在「普通按键捕获」之前，且命中后 return。
    否则 ESC 会被当成一个普通按键 P 去改绑，用户按 ESC 反而把键绑到了 Esc 上。

【CLEAR】DEL 是「立即写盘」，不是「标记待写」。
    用户按下 DEL 时看到状态栏提示，就认为已经生效了；如果攒着等关窗口才写，
    中途关窗口会让他以为清空了、实际没清。

===============================================================================
四、想改哪里
===============================================================================

  改键盘布局          → ROWS / NUMPAD_ROWS 两个常量
  改按键显示文字      → _label_of()
  改冲突判定规则      → find_conflicts()
  改写盘格式          → KeymapFile.save()（注释模板在那里）
  改监听采样频率      → POLL_MS（默认 30ms，比人手快，够用）
  加「功能列表视图」  → 建议新增一个窗口，不要塞进这个画布
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import tkinter as tk
from tkinter import ttk

POLL_MS = 30          # 监听轮询间隔（毫秒）
VK_ESC = 0x1B
VK_DEL = 0x2E


# =============================================================================
# 路径：与 mapper 保持一致 —— 配置永远在「程序根目录」
# =============================================================================
def app_root() -> str:
    """程序根目录。冻结后是 EXE 目录，源码模式是项目根。

    必须和 mapper.app_root() 用同一套规则，否则这里改的 keymap.json
    和 mapper 读的不是同一个文件 —— 表现为「改完没反应，重启也没变」。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    here = os.path.abspath(__file__)                   # .../tools/keymap_ui.py
    return os.path.dirname(os.path.dirname(here))      # .../


KEYMAP_PATH = os.path.join(app_root(), "keymap.json")


# =============================================================================
# keymap.json 读写：保留注释，只改受影响的行
# =============================================================================
class KeymapFile:
    """keymap.json 的读写封装。

    ## 为什么不用 json.dump 整文件重写

    keymap.json 允许 // 注释，玩家会在里面写「这把换成左 Shift 了」之类的备注，
    文件头还有一大段说明。json.load + json.dump 会把注释全丢掉。

    ## 但也不能逐行原地替换（踩过的坑）

    第一版是「逐行扫描、替换值、其余原样」，结果写出来的最后一项带了尾逗号：
        "hotkey_quit": "scrolllock",
    }
    JSON 不允许尾逗号 → 下次读直接解析失败 → data 变成空字典 →
    表现为「改完键位重启就全没了」。

    ## 现在的做法

        head      ← 第一个 { 之前的全部内容（文件头注释块），原样保留
        body      ← 按 data 顺序重新生成键值行，逗号只在非末项加
        tail      ← 最后一个 } 之后的内容，原样保留
        comments  ← 每个 key 的行尾注释，从原文件提取后重新挂回去

    这样既保住注释，又保证写出来的一定是合法 JSON。
    """

    def __init__(self, path: str = KEYMAP_PATH):
        self.path = path
        self.head = ""
        self.tail = ""
        self.comments: dict[str, str] = {}
        self.data: dict = {}
        self.load()

    def load(self) -> None:
        try:
            with io.open(self.path, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            text = "{\n}\n"
        if text.startswith("\ufeff"):          # 记事本另存为会加 BOM
            text = text.lstrip("\ufeff")

        i = text.find("{")
        j = text.rfind("}")
        if i < 0 or j < i:
            self.head, self.tail = "", ""
            body = ""
        else:
            self.head = text[:i + 1]
            self.tail = text[j + 1:]
            body = text[i + 1:j]

        # 行尾注释：key -> "   // 说明"
        self.comments = {}
        for line in body.splitlines():
            m = re.match(r'\s*"([a-z0-9_]+)"\s*:\s*.*?(\s*//.*)?$', line)
            if m and m.group(2):
                self.comments[m.group(1)] = m.group(2)

        stripped = re.sub(r"//[^\n]*", "", body)
        try:
            self.data = json.loads("{" + stripped + "}") if stripped.strip() else {}
        except Exception:
            self.data = {}

    def save(self) -> None:
        """按 data 重新生成 body，保留头部注释块与每行的行尾注释。"""
        keys = list(self.data.keys())
        lines = []
        for n, key in enumerate(keys):
            comma = "," if n < len(keys) - 1 else ""     # 末项不加逗号
            cmt = self.comments.get(key, "")
            lines.append(f'  "{key}": '
                         f'{json.dumps(self.data[key], ensure_ascii=False)}'
                         f'{comma}{cmt}')

        text = self.head.rstrip()
        if not text.endswith("{"):
            text += "\n{"
        text += "\n" + "\n".join(lines) + "\n}" + self.tail
        if not text.endswith("\n"):
            text += "\n"

        # 写前自检：必须能被重新解析，且内容与内存一致。
        # 宁可报错也不要写出一个下次读不进来的文件。
        chk = re.sub(r"//[^\n]*", "", text)
        i = chk.find("{")
        j = chk.rfind("}")
        parsed = json.loads(chk[i:j + 1])
        if parsed != self.data:
            raise ValueError("写盘自检失败：序列化结果与内存中的数据不一致")

        with io.open(self.path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        self.head = text[:text.find("{") + 1]
        self.tail = text[text.rfind("}") + 1:]

    # ---- 面向 UI 的操作 -------------------------------------------------
    def actions_of(self, key: str) -> list[str]:
        """反查：这个物理键承载了哪些功能。"""
        out = []
        for action, val in self.data.items():
            vals = val if isinstance(val, list) else [val]
            if key in [str(x) for x in vals]:
                out.append(action)
        return out

    def rebind(self, old_key: str, new_key: str) -> list[str]:
        """把 old_key 承载的所有功能改绑到 new_key。

        返回被改动的功能名列表。
        多键动作（如 lt = ["q","mouse_left"]）只替换这一个键，其余保留。
        """
        changed = []
        for action, val in list(self.data.items()):
            if isinstance(val, list):
                if old_key in val:
                    self.data[action] = [new_key if x == old_key else x
                                         for x in val]
                    changed.append(action)
            elif val == old_key:
                self.data[action] = new_key
                changed.append(action)
        return changed

    def clear(self, key: str) -> list[str]:
        """删除该物理键上的所有绑定（多键动作里只摘掉这一个键）。"""
        removed = []
        for action, val in list(self.data.items()):
            if isinstance(val, list):
                if key in val:
                    rest = [x for x in val if x != key]
                    if rest:
                        self.data[action] = rest
                    else:
                        del self.data[action]
                        removed.append(action)
                    if rest:
                        removed.append(action)
            elif val == key:
                del self.data[action]
                removed.append(action)
        return removed


# =============================================================================
# 键盘布局（想加键/挪键改这里）
# =============================================================================
# 每项：(显示文字, keymap 用的键名, 相对宽度)
ROWS = [
    [("Esc", "esc", 1.2), ("F1", "f1", 1), ("F2", "f2", 1), ("F3", "f3", 1),
     ("F4", "f4", 1), ("F5", "f5", 1), ("F6", "f6", 1), ("F7", "f7", 1),
     ("F8", "f8", 1), ("F9", "f9", 1), ("F10", "f10", 1), ("F11", "f11", 1),
     ("F12", "f12", 1)],
    [("`", "`", 1), ("1", "1", 1), ("2", "2", 1), ("3", "3", 1), ("4", "4", 1),
     ("5", "5", 1), ("6", "6", 1), ("7", "7", 1), ("8", "8", 1), ("9", "9", 1),
     ("0", "0", 1), ("-", "-", 1), ("=", "=", 1), ("Back", "backspace", 2)],
    [("Tab", "tab", 1.5), ("Q", "q", 1), ("W", "w", 1), ("E", "e", 1),
     ("R", "r", 1), ("T", "t", 1), ("Y", "y", 1), ("U", "u", 1), ("I", "i", 1),
     ("O", "o", 1), ("P", "p", 1), ("[", "[", 1), ("]", "]", 1),
     ("\\", "\\", 1.5)],
    [("Caps", "capslock", 1.8), ("A", "a", 1), ("S", "s", 1), ("D", "d", 1),
     ("F", "f", 1), ("G", "g", 1), ("H", "h", 1), ("J", "j", 1), ("K", "k", 1),
     ("L", "l", 1), (";", ";", 1), ("'", "'", 1), ("Enter", "enter", 2.2)],
    [("LShift", "lshift", 2.3), ("Z", "z", 1), ("X", "x", 1), ("C", "c", 1),
     ("V", "v", 1), ("B", "b", 1), ("N", "n", 1), ("M", "m", 1),
     (",", ",", 1), (".", ".", 1), ("/", "/", 1), ("RShift", "rshift", 2.2)],
    [("LCtrl", "lctrl", 1.4), ("LWin", "lwin", 1.2), ("LAlt", "lalt", 1.2),
     ("Space", "space", 6.2), ("RAlt", "ralt", 1.2), ("RWin", "rwin", 1.2),
     ("Menu", "apps", 1.2), ("RCtrl", "rctrl", 1.4)],
]

# 右侧独立区：方向键 + 小键盘 + 鼠标
SIDE_ROWS = [
    [("↑", "up", 1)],
    [("←", "left", 1), ("↓", "down", 1), ("→", "right", 1)],
    [],
    [("Num7", "num7", 1), ("Num8", "num8", 1), ("Num9", "num9", 1)],
    [("Num4", "num4", 1), ("Num5", "num5", 1), ("Num6", "num6", 1)],
    [("Num1", "num1", 1), ("Num2", "num2", 1), ("Num3", "num3", 1)],
    [("Num0", "num0", 2), ("Num.", "numdot", 1)],
    [],
    [("MouseL", "mouse_left", 1.6), ("MouseR", "mouse_right", 1.6)],
    [("MouseM", "mouse_middle", 1.6)],
    [("Scroll", "scrolllock", 1.6)],
]

# 功能名 -> 中文说明（状态栏用；没收录的显示原名）
ACTION_LABEL = {
    "a": "A 键", "b": "B 键", "x": "X 键", "y": "Y 键",
    "lb": "LB 左肩", "rb": "RB 右肩", "lt": "LT 左扳机", "rt": "RT 右扳机",
    "ls": "LS 左摇杆按下", "rs": "RS 右摇杆按下",
    "start": "START", "back": "BACK",
    "left_up": "左摇杆 前", "left_down": "左摇杆 后",
    "left_left": "左摇杆 左", "left_right": "左摇杆 右",
    "right_up": "右摇杆 上", "right_down": "右摇杆 下",
    "right_left": "右摇杆 左", "right_right": "右摇杆 右",
    "dpup": "十字键 上", "dpdown": "十字键 下",
    "dpleft": "十字键 左", "dpright": "十字键 右",
    "hotkey_calibrate": "热键·重定位", "hotkey_recenter": "热键·归中",
    "hotkey_pause": "热键·暂停鼠标", "hotkey_halt": "热键·全部暂停",
    "hotkey_force": "热键·强制映射", "hotkey_quit": "热键·退出",
}


def _label_of(action: str) -> str:
    return ACTION_LABEL.get(action, action)


def _vk_name_of(vk: int) -> str | None:
    """把捕获到的 VK 转成 keymap 用的键名。"""
    try:
        from acvd import keynames
    except Exception:
        return None
    if vk in keynames.MOUSE_VK:
        return f"mouse_{keynames.MOUSE_VK[vk]}"
    name = keynames.vk_name(vk)
    # vk_name 对未收录的键返回 "0xNN"，这种名字 keymap 也认，但不直观
    return name


# =============================================================================
# 主窗口
# =============================================================================
class KeymapUI:
    """全键盘改键界面。状态机见文件头「三」。"""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("按键映射")
        self.root.geometry("1120x560")
        self.root.minsize(900, 460)

        self.km = KeymapFile()

        # [STATE] 只有两个状态：IDLE / LISTENING
        self.state = "IDLE"
        self.selected: str | None = None     # 当前选中/监听的物理键
        self._prev_vks: set[int] = set()
        self._after_id = None

        self._build()
        self._refresh()

    # ---- 界面 -----------------------------------------------------------
    def _build(self):
        top = ttk.Frame(self.root, padding=(10, 8))
        top.pack(fill="x")
        ttk.Label(top, text="点击一个键 → 按新键改绑  ·  ESC 取消  ·  DEL 清除",
                  font=("Microsoft YaHei UI", 10)).pack(side="left")
        self.hint = tk.StringVar(value="就绪")
        ttk.Label(top, textvariable=self.hint, foreground="#0a7",
                  font=("Microsoft YaHei UI", 10, "bold")).pack(side="right")

        body = ttk.Frame(self.root, padding=(10, 0))
        body.pack(fill="both", expand=True)

        self.buttons: dict[str, tk.Button] = {}
        self._draw_keyboard(body)
        self._draw_side(body)

        bar = ttk.Frame(self.root, padding=(10, 6))
        bar.pack(fill="x")
        self.status = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.status, anchor="w",
                  font=("Microsoft YaHei UI", 9)).pack(side="left")
        ttk.Button(bar, text="重新载入", command=self._reload).pack(side="right")
        ttk.Button(bar, text="打开文件目录",
                   command=self._open_dir).pack(side="right", padx=(0, 6))

    def _draw_keyboard(self, parent):
        left = ttk.Frame(parent)
        left.pack(side="left", fill="both", expand=True)
        for row in ROWS:
            line = ttk.Frame(left)
            line.pack(fill="x", pady=1)
            for text, key, w in row:
                btn = tk.Button(
                    line, text=text, width=0, relief="raised", bd=1,
                    font=("Microsoft YaHei UI", 8),
                    command=lambda k=key: self.on_click(k))
                btn.pack(side="left", fill="both", expand=True,
                         padx=1, ipadx=int(6 * w), ipady=6)
                self.buttons[key] = btn

    def _draw_side(self, parent):
        side = ttk.Frame(parent, padding=(10, 0))
        side.pack(side="right", fill="y")
        for row in SIDE_ROWS:
            line = ttk.Frame(side)
            line.pack(fill="x", pady=1)
            for text, key, w in row:
                btn = tk.Button(
                    line, text=text, relief="raised", bd=1,
                    font=("Microsoft YaHei UI", 8),
                    command=lambda k=key: self.on_click(k))
                btn.pack(side="left", fill="both", expand=True,
                         padx=1, ipadx=int(10 * w), ipady=6)
                self.buttons[key] = btn

    # ---- 绘制刷新 -------------------------------------------------------
    def _refresh(self):
        """按当前 keymap 重画所有键：功能名 + 是否选中。"""
        for key, btn in self.buttons.items():
            actions = self.km.actions_of(key)
            base = self._key_caption(key)
            if actions:
                btn.configure(
                    text=f"{base}\n{'/'.join(_label_of(a) for a in actions)}",
                    bg="#dff3e0", activebackground="#c6ebc9")
            else:
                btn.configure(text=base, bg="#f4f4f4",
                              activebackground="#e8e8e8")
            if key == self.selected:
                btn.configure(bg="#ffe58a", activebackground="#ffd75e")

    def _key_caption(self, key: str) -> str:
        for row in ROWS + SIDE_ROWS:
            for text, k, _w in row:
                if k == key:
                    return text
        return key

    # ---- 事件：点击某个键 ------------------------------------------------
    def on_click(self, key: str):
        """[LISTEN] 监听状态机入口：IDLE -> LISTENING(K) -> IDLE"""
        if self.state == "LISTENING":
            # 监听中再点别的键：取消当前监听，改选这个（不写盘）
            self._stop_listen()
            self.status.set("已取消上一项监听")

        actions = self.km.actions_of(key)
        if not actions:
            self.selected = None
            self._refresh()
            self.hint.set("就绪")
            self.status.set(f"[{self._key_caption(key)}] 当前未绑定任何功能，"
                            f"先给别的键改绑过来才能点它")
            return

        self.selected = key
        self.state = "LISTENING"
        self._refresh()
        self.hint.set(f"请按下新按键…  [{self._key_caption(key)}]")
        self.status.set(f"正在改：{self._key_caption(key)} = "
                        f"{' / '.join(_label_of(a) for a in actions)}"
                        f"    （ESC 取消  ·  DEL 清除当前键位映射）")
        self._start_listen()

    # ---- 监听 -----------------------------------------------------------
    def _start_listen(self):
        from acvd.keynames import pressed_vks
        self._prev_vks = pressed_vks()
        self._poll()

    def _stop_listen(self):
        if self._after_id is not None:
            try:
                self.root.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None
        self.state = "IDLE"

    def _poll(self):
        """轮询捕获新按键。

        为什么用轮询而不是 tkinter 的 <Key> 绑定：
        mapper 的全局低级键盘钩子可能吞掉某些键（热键），而且窗口失焦时
        <Key> 收不到。pressed_vks() 直接问系统「现在哪些键是按下状态」，
        与焦点、与钩子是否吞键都无关，和 rebind.py 用的是同一套。
        """
        if self.state != "LISTENING":
            return
        from acvd.keynames import pressed_vks

        cur = pressed_vks()
        new = cur - self._prev_vks
        self._prev_vks = cur

        if new:
            vk = sorted(new)[0]

            # [PRIORITY] ESC / DEL 必须在普通按键之前判断，且命中后立刻 return。
            # 否则 ESC 会被当成一个普通新键去改绑，用户按 ESC 反而绑上 Esc。
            if vk == VK_ESC:
                # [ESCAPE] 只回滚 UI 状态，不碰内存映射表和配置文件
                self._stop_listen()
                self.selected = None
                self._refresh()
                self.hint.set("就绪")
                self.status.set("已取消，未做任何修改")
                return

            if vk == VK_DEL:
                # [CLEAR] 删除该物理键上的绑定，立即落盘；失败回滚 UI
                self._do_clear()
                return

            name = _vk_name_of(vk)
            if name:
                self._do_rebind(name)
                return

        self._after_id = self.root.after(POLL_MS, self._poll)

    # ---- 改绑 -----------------------------------------------------------
    def _do_rebind(self, new_key: str):
        old_key = self.selected
        if old_key is None:
            self._stop_listen()
            return

        # 冲突检测：新键已经承载了别的功能
        taken = self.km.actions_of(new_key)
        if taken and new_key != old_key:
            self.status.set(
                f"! [{self._key_caption(new_key)}] 已被 "
                f"{' / '.join(_label_of(a) for a in taken)} 占用，未修改。"
                f"先清掉它，或换一个键。")
            # 不退出监听，让用户直接换一个键再按
            return

        if new_key == old_key:
            self._stop_listen()
            self.selected = None
            self._refresh()
            self.hint.set("就绪")
            self.status.set("按了同一个键，未修改")
            return

        snapshot = json.loads(json.dumps(self.km.data))   # 用于失败回滚
        changed = self.km.rebind(old_key, new_key)
        if not changed:
            self._stop_listen()
            self.status.set("没有找到要改的绑定（可能已被清空）")
            return
        try:
            self.km.save()
        except Exception as e:
            self.km.data = snapshot                        # 回滚
            self.status.set(f"! 写盘失败，已回滚：{e}")
            return

        self._stop_listen()
        self.selected = None
        self._refresh()
        self.hint.set("就绪")
        moved = " / ".join(_label_of(a) for a in changed)
        self.status.set(
            f"✓ {moved}：{self._key_caption(old_key)} → "
            f"{self._key_caption(new_key)}（已写入 {os.path.basename(KEYMAP_PATH)}）")
        self._notify_mapper()

    # ---- 清除 -----------------------------------------------------------
    def _do_clear(self):
        """[CLEAR] 清空当前选中键上的所有绑定。

        需求要求「立即写盘」：用户看到状态栏提示就认为生效了，
        不能攒到关窗口才写（中途关窗口会让他以为清空了、实际没清）。
        """
        key = self.selected
        if key is None:
            self._stop_listen()
            return
        snapshot = json.loads(json.dumps(self.km.data))
        removed = self.km.clear(key)
        if not removed:
            self._stop_listen()
            self.selected = None
            self._refresh()
            self.hint.set("就绪")
            self.status.set(f"[{self._key_caption(key)}] 未绑定任何功能")
            return
        try:
            self.km.save()
        except Exception as e:
            self.km.data = snapshot                        # 回滚
            self.status.set(f"! 写盘失败，已回滚：{e}")
            return

        self._stop_listen()
        self.selected = None
        self._refresh()
        self.hint.set("就绪")
        names = " / ".join(_label_of(a) for a in removed)
        self.status.set(f"已清除 {names} 的映射（"
                        f"[{self._key_caption(key)}] 现在是未绑定）")
        self._notify_mapper()

    # ---- 杂项 -----------------------------------------------------------
    def _notify_mapper(self):
        """通知正在运行的 mapper 重载键位。没在跑就算了，不报错。"""
        try:
            from acvd.pipe import PipeClient
            PipeClient("acvd_pad", timeout_ms=600).send("reload")
            self.status.set(self.status.get() + "  ·  mapper 已重载")
        except Exception:
            self.status.set(self.status.get() + "  ·  mapper 未运行，重启后生效")

    def _reload(self):
        self._stop_listen()
        self.selected = None
        self.km.load()
        self._refresh()
        self.status.set(f"已重新载入 {KEYMAP_PATH}")

    def _open_dir(self):
        try:
            os.startfile(app_root())            # noqa: S606 (Windows only)
        except Exception as e:
            self.status.set(f"! 打不开目录：{e}")

    def on_close(self):
        """关闭窗口：取消监听，不写盘（已经写过的保留）。"""
        self._stop_listen()
        self.root.destroy()


def open_window(parent=None):
    """打开映射窗口。

    两种调用方式，决定了窗口的生命周期：

      open_window()           新建独立的 Tk 主窗口，阻塞到关窗（EXE 子命令用）
      open_window(parent)     挂成控制面板的子窗口，共用它的 mainloop
                              （panel 里点「按键映射」走这条）

    为什么不能永远用 Tk()：一个进程里开两个 Tk() 主循环会互相打架
    （第二个 mainloop 起来后第一个就卡住）。所以挂面板时只能用 Toplevel。
    """
    if parent is None:
        root = tk.Tk()
        ui = KeymapUI(root)
        root.protocol("WM_DELETE_WINDOW", ui.on_close)
        root.mainloop()
        return ui

    win = tk.Toplevel(parent)
    win.transient(parent)                 # 跟随父窗口最小化/关闭
    ui = KeymapUI(win)
    win.protocol("WM_DELETE_WINDOW", ui.on_close)
    return ui


def main() -> int:
    open_window(None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

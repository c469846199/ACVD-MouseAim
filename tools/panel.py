"""
tools/panel.py — mapper 图形控制面板（给不写代码的人用）

双击 start.bat 就会同时起 mapper 和这个面板。
面板只通过命名管道和 mapper 通信，崩了也不会影响映射本身。

按钮：
    全部暂停  = 顶部大按钮 / F10。手柄整份置中性、键盘映射也不发 —— 切去
                聊天框打字时游戏完全不受控；再按一下恢复。
    强制标定  = 按 F1（正常情况下用不到，相机地址固定，只做坐标对齐）
    自动定位  = 开关后台的「静默预扫 + 机会式验证」
    归中      = 按 F2（换关后把准星锚到当前视角）
    暂停鼠标  = 按 F9（只停鼠标闭环，键盘/按钮照常）
    准星      = 显示/隐藏绿色准星
    强制重标  = 忽略地址缓存，完整重扫（相当于 recal）
    退出映射  = 关闭 mapper 进程

状态区第一行会显示自动定位当前在干什么：
    等画面稳定 → 预扫候选 → 等你动杆（候选 N · 票 M/2）→ 已接管
这条线就是「为什么还没接管」的答案，不需要看日志。
"""

from __future__ import annotations

import os
import sys
import tkinter as tk
from tkinter import font as tkfont

# 源码运行时把项目根目录加进 sys.path，让 `import acvd.*` 能找到包。
# 打包成 EXE 后 acvd 已经随程序打进去，不需要也不该再插源码路径
# （在别人机器上那是个不存在的目录）。
if not getattr(sys, "frozen", False):
    _here = os.path.abspath(__file__)                 # .../tools/panel.py
    sys.path.insert(0, os.path.dirname(os.path.dirname(_here)))

from acvd.pipe import PipeClient  # noqa: E402

POLL_MS = 300


def send(cmd: str) -> str:
    """给 mapper 发一条管道命令，**永远返回字符串**。

    三个兜底层次缺一不可，少一个都会让面板在异常时崩掉：

      1. 管道连不上          → 返回「mapper 未运行」
      2. 管道连上但回 None   → 同样按未运行处理
         （PipeClient.send 在超时边界上可能返回 None。以前这里直接
          return None，调用方紧接着 `.startswith(...)` 就抛
          AttributeError —— Tk 的 after 回调里抛异常不会弹窗，
          表现为**面板窗口直接消失**，而 mapper 还活得好好的。
          这就是「GUI 没了但 mapper 还在」的真实原因。）
      3. 其它异常            → 带上异常类型，方便判断是超时还是断开
    """
    try:
        r = PipeClient("acvd_pad", timeout_ms=1500).send(cmd)
        return r if isinstance(r, str) else "mapper 未运行（无响应）"
    except Exception as e:
        return f"mapper 未运行（{e.__class__.__name__}）"


class Panel:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("ACVD 鼠标瞄准 · 控制面板")
        root.attributes("-topmost", True)
        root.resizable(False, False)

        mono = tkfont.Font(family="Consolas", size=10)
        big = tkfont.Font(family="Microsoft YaHei UI", size=11, weight="bold")
        huge = tkfont.Font(family="Microsoft YaHei UI", size=12, weight="bold")

        # ---- 顶部：全局暂停（比 F9 更彻底）------------------------------
        # F9 只停鼠标闭环，键盘/按钮照旧往游戏里送；去聊天框打字时游戏
        # 还会被按到。这个按钮把手柄整份置中性，一键就能脱手打字。
        self.btn_halt = tk.Button(root, text="⏸ 全部暂停（F10 · 打字/切窗口）",
                                  font=huge, command=lambda: self.act("halt"),
                                  bg="#c62828", fg="white",
                                  activebackground="#a01e1e",
                                  activeforeground="white",
                                  relief="raised", bd=2, pady=6)
        self.btn_halt.grid(row=0, column=0, columnspan=4, padx=8, pady=(8, 4),
                           sticky="we")

        head = tk.Label(root, text="状态", font=big, anchor="w")
        head.grid(row=1, column=0, columnspan=4, sticky="w", padx=10, pady=(4, 2))

        self.lbl_main = tk.Label(root, text="连接中 ...", font=mono, justify="left",
                                 anchor="w", fg="#0a7a0a")
        self.lbl_main.grid(row=2, column=0, columnspan=4, sticky="w", padx=10)

        self.lbl_state = tk.Label(root, text="", font=mono, justify="left",
                                  anchor="w", fg="#333")
        self.lbl_state.grid(row=3, column=0, columnspan=4, sticky="w", padx=10,
                            pady=(2, 8))

        btns = [
            ("强制标定 (F1)", lambda: self.act("calib")),
            ("自动定位 开/关", lambda: self.act("auto_toggle")),
            ("归中 (F2)", lambda: self.act("recenter")),
            ("暂停鼠标 (F9)", lambda: self.act("pause")),
            ("准星 开/关", lambda: self.act("cross_toggle")),
            ("强制重标定", lambda: self.act("recal")),
            # 按键映射用子窗口打开（Toplevel），不新起 Tk 主循环 ——
            # 一个进程里两个 Tk() mainloop 会互相卡死。
            # 改完写 keymap.json 并自动给 mapper 发 reload，不用重启。
            ("按键映射 …", self.open_keymap),
            ("退出映射", lambda: self.act("quit")),
        ]
        # 两列排布：旧的写法把 4 个按钮都塞进同一行，后两个会盖住前两个
        for i, (text, cb) in enumerate(btns):
            tk.Button(root, text=text, command=cb, width=16).grid(
                row=4 + i // 2, column=i % 2, padx=6, pady=4, sticky="we")

        # ---- 手感微调：改完立刻生效，不用改文件、不用重启 --------------
        # 这两个数字就是全部「跟手 vs 稳」的杠杆：
        #   满杆角小 → 快、容易冲过头；大 → 稳、微调黏
        #   回中死区大 → 更稳、精度损失几个像素；小 → 更准、容易被噪声惊动
        row_tune = 4 + len(btns) // 2
        pf = tk.Frame(root)
        pf.grid(row=row_tune, column=0, columnspan=4, padx=8, pady=(6, 2),
                sticky="we")
        tk.Label(pf, text="满杆角(°)", font=mono).grid(row=0, column=0, sticky="w")
        self.ent_full = tk.Entry(pf, width=6, font=mono, justify="center")
        self.ent_full.grid(row=0, column=1, padx=(4, 10), pady=2)
        tk.Label(pf, text="回中/圈半径(px)", font=mono).grid(row=0, column=2,
                                                             sticky="w")
        self.ent_dead = tk.Entry(pf, width=6, font=mono, justify="center")
        self.ent_dead.grid(row=0, column=3, padx=(4, 0), pady=2)

        tk.Label(pf, text="透明度(0~255)", font=mono).grid(row=1, column=0,
                                                           sticky="w")
        self.ent_alpha = tk.Entry(pf, width=6, font=mono, justify="center")
        self.ent_alpha.grid(row=1, column=1, padx=(4, 10), pady=2)
        tk.Label(pf, text="补偿τ(s)", font=mono).grid(row=1, column=2, sticky="w")
        self.ent_tau = tk.Entry(pf, width=6, font=mono, justify="center")
        self.ent_tau.grid(row=1, column=3, padx=(4, 0), pady=2)

        tk.Button(pf, text="应用", width=10,
                  command=self.apply_tune).grid(row=2, column=0, columnspan=4,
                                                sticky="we", padx=(4, 0), pady=(4, 0))

        self.lbl_msg = tk.Label(root, text="", font=mono, anchor="w",
                                fg="#555", wraplength=340, justify="left")
        self.lbl_msg.grid(row=row_tune + 1, column=0, columnspan=4,
                          sticky="w", padx=10, pady=(6, 8))

        self.cross_on = True
        self.halt = False
        self._load_tune()
        self.poll()

    def _load_tune(self):
        """把 mapper 当前的参数填进输入框（读不到就用默认值）。"""
        d = self.parse(send("fullstick"))
        pairs = ((self.ent_full, "full_stick_angle", "12.0"),
                 (self.ent_dead, "dead_px", "16.0"),
                 (self.ent_alpha, "cross_alpha", "200"),
                 (self.ent_tau, "cam_tau", "0.0"))
        for ent, key, dflt in pairs:
            ent.delete(0, "end")
            ent.insert(0, d.get(key, dflt))

    def apply_tune(self):
        try:
            full = float(self.ent_full.get())
            dead = float(self.ent_dead.get())
            alpha = float(self.ent_alpha.get())
            tau = float(self.ent_tau.get())
        except ValueError:
            self.lbl_msg.config(text="四个框都要填数字")
            return
        send(f"fullstick {full}")
        send(f"deadpx {dead}")
        send(f"crossalpha {alpha}")
        send(f"camtau {tau}")
        self.lbl_msg.config(
            text=f"已应用：满杆角 {full:g}° · 圈半径/死区 {dead:g}px · "
                 f"透明度 {alpha:g} · 补偿τ {tau:g}s"
                 + ("（0 = 不补偿）" if tau == 0 else ""))

    # ---- 动作 -------------------------------------------------------
    def act(self, what: str):
        if what == "halt":
            self.halt = not self.halt
            msg = send("halt " + ("on" if self.halt else "off"))
        elif what == "cross_toggle":
            self.cross_on = not self.cross_on
            msg = send("cross " + ("on" if self.cross_on else "off"))
        elif what == "auto_toggle":
            self.auto_on = not getattr(self, "auto_on", True)
            msg = send("auto on" if self.auto_on else "auto off")
        elif what == "quit":
            msg = send("quit")
            self.lbl_msg.config(text=msg)
            self.root.after(400, self.root.destroy)
            return
        else:
            msg = send(what)
        self.lbl_msg.config(text=msg[:160])
        self.poll()

    def open_keymap(self):
        """打开按键映射子窗口。

        用 Toplevel 挂在面板上，不新起 Tk() 主循环（会卡死）。
        子窗口里改键会直接写 keymap.json 并给 mapper 发 reload，
        所以这里不需要做任何后续处理 —— 改完即时生效。
        """
        try:
            import keymap_ui
            keymap_ui.open_window(self.root)
        except Exception as e:
            self.lbl_msg.config(text=f"打不开按键映射：{e.__class__.__name__}: {e}")

    # ---- 轮询 -------------------------------------------------------
    def parse(self, s: str) -> dict:
        out = {}
        for part in s.split():
            if "=" in part:
                k, _, v = part.partition("=")
                out[k] = v
        return out

    def poll(self):
        """每 300ms 刷一次状态。

        ## 为什么整个函数包在 try/except 里

        Tk 的 `after` 回调抛异常时**不会弹窗、不会打印到控制台**，
        只是静默终止这一条回调链 —— 表现就是「面板窗口凭空消失了，
        而 mapper 还在后台跑」。这个现象极难归因，所以这里兜住一切：
        任何异常都只写进状态栏，绝不让回调链断掉。

        关键的一步是**无论成功失败都要重新排下一次 poll**，
        否则一次异常就永久停止刷新（窗口还在，但数字再也不动）。
        """
        try:
            self._poll_once()
        except Exception as e:
            try:
                self.lbl_msg.config(
                    text=f"面板刷新出错（已自动忽略）：{e.__class__.__name__}: {e}")
            except Exception:
                pass
        finally:
            try:
                self.root.after(POLL_MS, self.poll)
            except Exception:
                pass

    def _poll_once(self):
        s = send("status")
        if s.startswith("mapper 未运行"):
            self.lbl_main.config(text="mapper 未运行", fg="#b00")
            self.lbl_state.config(text="请重新双击 ACVD.exe 启动")
            return

        d = self.parse(s)
        focused = d.get("focused") == "True"
        loop_on = d.get("mouse") == "on"
        paused = d.get("paused") == "on"
        halted = d.get("halt") == "on"
        forced = d.get("force") == "on"

        self.halt = halted
        if halted:
            self.btn_halt.config(text="▶ 恢复映射（游戏会重新接手）",
                                 bg="#2e7d32", activebackground="#1b5e20")
        else:
            self.btn_halt.config(text="⏸ 全部暂停（打字 / 切窗口）",
                                 bg="#c62828", activebackground="#a01e1e")

        color = "#0a7a0a" if (focused and loop_on and not paused and not halted) \
            else "#b06000"
        # 自动定位状态（A+B）：翻成人话，这是「为什么还没接管」的唯一答案
        astate = d.get("auto_state", "")
        acand = d.get("auto_cand", "0")
        avotes = d.get("auto_votes", "0")
        aon = d.get("auto_on", "1") == "1"
        self.auto_on = aon
        auto_txt = {
            "idle": "等游戏启动",
            "find_cam": "找相机中",
            "moving": "等画面稳定",
            "ready": "预扫候选",
            "armed": f"等你动杆（候选 {acand} · 票 {avotes}/2）",
            "locked": "已锁定",
            "tracked": "已接管",
            "off": "已关闭",
        }.get(astate, astate or "关闭")

        if halted:
            mode = "全部暂停（手柄已脱手）"
        elif loop_on and not paused:
            mode = "运行中"
        elif paused:
            mode = "已暂停"
        elif astate in ("armed", "ready", "moving", "find_cam"):
            mode = "待接管"
        else:
            mode = "未定位"
        self.lbl_main.config(
            text=f"闭环：{mode}    焦点：{'游戏' if focused else '其他窗口'}",
            fg=color)

        err = d.get("err", "?")
        fov = d.get("fov", "?")
        hfov = d.get("hfov", "?")
        f = d.get("f", "?")
        win = d.get("win", "")
        stop_txt = ("全部暂停 · 键盘也不发" if halted
                    else ("鼠标暂停（F9）" if paused else "运行"))
        self.lbl_state.config(
            text=(f"映射：{stop_txt}\n"
                  f"自动定位 {'开' if aon else '关'}：{auto_txt}\n"
                  f"误差 {err}\n"
                  f"FOV {fov}°（按屏幕宽度 {hfov}°） 焦距 {f}\n"
                  f"摇杆输出 R={d.get('R','?')}  强制={forced}\n"
                  f"准星窗口 {win}"))
        # 注意：这里**不**排下一次轮询 —— 排程统一由 poll() 的 finally 负责。
        # 两处都排会导致每轮指数增长地堆回调（300ms 变 150ms 变 75ms…），
        # 表现为面板越用越卡、CPU 越来越高。


def main() -> int:
    root = tk.Tk()
    Panel(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

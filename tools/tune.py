"""
tools/tune.py — 手感调参向导（死区 / 满杆角度 / 俯仰限位 / 平滑）

用法：
    python tools\\tune.py              # 交互式，逐项问你要改多少
    python tools\\tune.py dead=0.15    # 直接改一项（可写多项）

会同时做两件事：
    * 立刻通过管道把新值推给运行中的 mapper（不用重启）
    * 写回 tune.json，下次启动依然生效
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TUNE_PATH = os.path.join(ROOT, "tune.json")

# 名字 -> (中文说明, 推荐范围)
FIELDS = [
    ("dead", "死区（度）：越小越跟手；0 = 完全无死区", "0 ~ 0.5"),
    ("stick_floor", "反死区：鼠标刚动时的最小推杆幅度（吃掉游戏死区）", "0 ~ 0.3"),
    ("curve_gamma", "响应曲线：>1 小误差更细腻，<1 小误差更凶", "1.0 ~ 2.5"),
    ("speed_boost", "快甩加成：鼠标甩动时额外加的推力（0=不加）", "0 ~ 0.6"),
    ("speed_ref", "多快算「甩」（像素/秒）", "500 ~ 4000"),
    ("speed_gamma", "速度曲线：>1 只有真甩动才加成", "1.0 ~ 2.5"),
    ("ff_gain", "角速度前馈：1.0 = 鼠标转多快准星就转多快（治微调死区）", "0 ~ 1.5"),
    ("rate_max", "满杆角速度（度/秒）：前馈的换算基准，实测 54", "30 ~ 80"),
    ("game_deadzone", "游戏自身摇杆死区（实测 0.44），输出前做补偿", "0 ~ 0.6"),
    ("err_dead", "角度死带（度）：误差小于它就不输出，防止持续爬行", "0 ~ 1"),
    ("button_hold", "按键最短保持（秒）：必须 > 采样周期 0.0167，否则丢按", "0.02 ~ 0.05"),
    ("pool_cap", "位移积压上限（像素）：超过就丢弃最老的", "1000 ~ 8000"),
    ("input_window", "反死区生效窗口（秒）：鼠标停下多久后退回线性", "0.05 ~ 0.4"),
    ("full_stick_angle", "满杆角度（度）：越小转得越凶", "6 ~ 20"),
    ("pitch_limit", "俯仰限位（度）", "60 ~ 70"),
    ("smooth", "输出平滑：1.0 直出", "0.5 ~ 1.0"),
]

DEFAULTS = {"dead": 0.0, "full_stick_angle": 10.0, "pitch_limit": 68.0,
            "smooth": 1.0, "stick_floor": 0.12, "curve_gamma": 1.35,
            "input_window": 0.15, "speed_boost": 0.30, "speed_ref": 1500.0,
            "speed_tau": 0.06, "speed_gamma": 1.5, "ff_gain": 1.0, "rate_max": 54.0, "game_deadzone": 0.44, "err_dead": 0.25,
            "button_hold": 0.025, "pool_cap": 4000.0}

# tune.json 里的键名 -> mapper 管道命令名
CMD = {"dead": "dead", "full_stick_angle": "fullstick",
       "pitch_limit": "pitchlim", "smooth": "smooth",
       "stick_floor": "floor", "curve_gamma": "curve",
       "input_window": "inputwindow", "speed_boost": "speedboost",
       "speed_ref": "speedref", "speed_tau": "speedtau",
       "speed_gamma": "speedgamma", "ff_gain": "ffgain", "rate_max": "ratemax", "game_deadzone": "gamedz", "err_dead": "errdead", "button_hold": "hold",
       "pool_cap": "cap"}


def load():
    data = dict(DEFAULTS)
    if os.path.exists(TUNE_PATH):
        try:
            txt = open(TUNE_PATH, encoding="utf-8").read()
            # 去掉 // 注释后再解析
            out, i, n, s = [], 0, len(txt), False
            while i < n:
                c = txt[i]
                if s:
                    out.append(c)
                    s = c != '"'
                    i += 1
                    continue
                if c == '"':
                    s = True
                    out.append(c)
                    i += 1
                    continue
                if c == "/" and i + 1 < n and txt[i + 1] == "/":
                    while i < n and txt[i] not in "\r\n":
                        i += 1
                    continue
                out.append(c)
                i += 1
            data.update(json.loads("".join(out)))
        except Exception as e:
            print(f"! tune.json 读取失败，用默认值：{e}")
    return data


def send(cmd: str) -> str:
    try:
        from acvd.pipe import PipeClient
        return PipeClient("acvd_pad", timeout_ms=2000).send(cmd)
    except Exception as e:
        return f"（mapper 没在跑，稍后重启才生效：{e}）"


def main() -> int:
    data = load()
    args = [a for a in sys.argv[1:] if "=" in a]

    if args:
        for a in args:
            k, _, v = a.partition("=")
            if k not in DEFAULTS:
                print(f"! 不认识 {k}，可选：{', '.join(DEFAULTS)}")
                return 1
            data[k] = float(v)
    else:
        print("=" * 58)
        print(" ACVD 手感调参（直接回车 = 不改）")
        print("=" * 58)
        for key, label, rng in FIELDS:
            cur = data.get(key, DEFAULTS[key])
            ans = input(f"\n{key} = {cur}\n  {label}\n  范围 {rng} → 新值: ").strip()
            if not ans:
                continue
            try:
                data[key] = float(ans)
            except ValueError:
                print("  不是数字，跳过")

    # 写回带注释的 tune.json
    body = (
        "// ===================================================================\n"
        "//  ACVD 手感参数（本文件由 tools\\tune.py 生成，也可手改）\n"
        "//  改完生效：管道命令 reload\n"
        "// ===================================================================\n"
    )
    with open(TUNE_PATH, "w", encoding="utf-8", newline="\n") as f:
        f.write(body)
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print("\n--- 推给运行中的 mapper ---")
    for key, _label, _rng in FIELDS:
        print(" ", send(f"{CMD[key]} {data[key]}"))

    print(f"\n已写入 {TUNE_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

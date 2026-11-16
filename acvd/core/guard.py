"""
acvd/core/guard.py — 分层守卫：用代码检查架构，而不是靠自觉。

## 为什么需要它

    分层最容易被破坏的方式不是"想破坏"，而是"顺手"：
    某天为了省一次拷贝，在 controller 里写了个 time.perf_counter()；
    又某天为了少传一个参数，让 brain 直接去调 read_aim()。
    此后分层就只剩下注释里的四行字。

    所以把规则写成可执行的检查，在引擎启动时跑一次，违规直接抛异常
    拒绝启动。宁可起不来，也不要悄悄变成缝合怪。

## 检查方式

    基于 ast，不看注释和文档字符串（否则"禁止 import winmem"这句话
    本身就会触发警报）。只检查两件事：
      1. import 了谁（含函数体内的延迟 import）
      2. 代码里实际用到了哪些标识符
"""

from __future__ import annotations

import ast
import os
from dataclasses import dataclass


class ArchViolation(RuntimeError):
    """分层被破坏。这条消息会直接打在控制台上，不用翻日志。"""


# 任何层都可以用的标准库基础件（不构成依赖）
BASE_ALLOW = {"__future__", "dataclasses", "typing", "math"}


@dataclass(frozen=True)
class Rule:
    allow: set | None      # None = 不限制（只给装配层）
    why: str


RULES = {
    "types.py": Rule(set(), "契约层不依赖任何实现"),
    "chan.py": Rule({"threading"}, "只允许用锁与线程原语"),
    "control.py": Rule({".types"}, "纯函数：不许线程、不许时钟、不许 IO"),
    "control_v2.py": Rule({".types", ".control"},
                          "逆模型控制器：复用 V1 的纯函数，同样不许线程/时钟/IO"),
    "sensor.py": Rule({"math", "struct", "threading", "time",
                       ".chan", ".types", "..gmem"},
                      "采集层只读固定相机，不得依赖定位/决策/输出层"),
    "blackbox.py": Rule({"collections", "csv", "os", "time"},
                        "只做内存环形记录与 CSV 导出，不读内存、不碰硬件"),
    "output.py": Rule({"threading", "time", ".chan", ".types", ".control",
                       ".blackbox"},
                      "输出层不读内存，只做合成与看门狗"),
    "engine.py": Rule(None, "装配层允许引用全部层"),
}

# core 里一次都不许出现的标识符（设备写入点只在前端）
CORE_FORBIDDEN = {"ViGEm", "vigem", "XInputSetState", "SetCursorPos",
                  "ReadProcessMemory", "VirtualQueryEx", "OpenProcess"}


def _parse(path: str) -> ast.AST:
    with open(path, encoding="utf-8") as f:
        return ast.parse(f.read(), filename=path)


def _imports(tree: ast.AST) -> list[tuple[str, int]]:
    """返回 [(规范化模块名, 行号)]，含函数体内的延迟 import。"""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                out.append((a.name, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            prefix = "." * (node.level or 0)
            if node.module:
                out.append((prefix + node.module, node.lineno))
            else:
                # from . import control  →  ".control"
                for a in node.names:
                    out.append((prefix + a.name, node.lineno))
    return out


def _identifiers(tree: ast.AST) -> set[str]:
    """代码里实际出现的标识符（注释、docstring 不算）。"""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


def _allowed(module: str, allow: set | None) -> bool:
    if allow is None:
        return True
    head = module.split(".")[0] if not module.startswith(".") else module
    if head in BASE_ALLOW:
        return True
    for a in allow:
        if module == a or module.startswith(a + "."):
            return True
    return False


def check(core_dir: str | None = None) -> list[str]:
    """返回违规清单（空 = 通过）。"""
    core_dir = core_dir or os.path.dirname(os.path.abspath(__file__))
    problems: list[str] = []

    for name, rule in RULES.items():
        path = os.path.join(core_dir, name)
        if not os.path.exists(path):
            problems.append(f"{name}: 文件不存在（层被删掉了？）")
            continue
        try:
            tree = _parse(path)
        except SyntaxError as e:
            problems.append(f"{name}: 语法错误 {e}")
            continue

        for mod, line in _imports(tree):
            if not _allowed(mod, rule.allow):
                problems.append(
                    f"{name}:{line} 违规 import {mod!r} —— {rule.why}")

        if name != "sensor.py":
            hit = _identifiers(tree) & CORE_FORBIDDEN
            if hit:
                problems.append(
                    f"{name}: 使用了 {sorted(hit)} —— 读内存/写硬件只允许在"
                    " sensor.py 与前端 mapper")

    return problems


def enforce(core_dir: str | None = None) -> None:
    """启动时调用。有任何违规直接拒绝启动。"""
    problems = check(core_dir)
    if problems:
        raise ArchViolation(
            "分层守卫拦截，引擎拒绝启动：\n  " + "\n  ".join(problems))

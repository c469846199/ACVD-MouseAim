"""
tools/correlate.py — 交叉比对上下推的响应，找真正的旋转轴

为什么：
    四元数反解出的 yaw/pitch 对小量旋转不直观，
    但「上推为正、下推为负、幅度对称」是任何旋转轴都满足的硬约束。
    相机的 pitch 范围最大（因为视角范围大），火控圈次之，机体的 pitch 恒为 0。

判据：
    s_up = Δpitch(up) / Δpitch(down)，理想值约 -1（反向对称）
    再按幅度排序，幅度大的在前。
"""

from __future__ import annotations

import json
import os
import sys

__import__("sys").path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))

STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "state")


def load(name):
    p = os.path.join(STATE_DIR, name)
    if not os.path.exists(p):
        return {}
    with open(p, encoding="utf-8") as f:
        return {int(d["addr"], 16): d for d in json.load(f)}


def main() -> int:
    up = load("hunt_up.json")
    down = load("hunt_down.json")
    left = load("hunt_left.json")
    print(f"up={len(up)}  down={len(down)}  left={len(left)}")

    rows = []
    for addr in set(up) & set(down):
        du = up[addr]["dpitch"]
        dd = down[addr]["dpitch"]
        yawu = up[addr]["dyaw"]
        yawd = down[addr]["dyaw"]
        if abs(du) < 0.5 or abs(dd) < 0.5:
            continue
        ratio = du / dd
        sym = abs(ratio + 1.0)          # 0 = 完美反向对称
        amp = (abs(du) + abs(dd)) / 2
        # 只在 left 里也出现的，说明它横向也响应
        in_left = addr in left
        rows.append((sym, -amp, addr, du, dd, yawu, yawd, in_left))

    rows.sort(key=lambda t: (t[0], t[1]))
    print(f"\n同时出现在 up/down 的候选: {len(rows)}\n")
    print(f"{'地址':>12} {'对称误差':>9} {'幅度':>8} {'up':>8} {'down':>8} {'left':>6}")
    print("-" * 62)
    for sym, namp, addr, du, dd, yawu, yawd, in_left in rows[:35]:
        print(f"0x{addr:010X} {sym:9.3f} {-namp:8.2f} {du:+8.2f} {dd:+8.2f} "
              f"{'  yes' if in_left else '   -'}")

    print("\n说明：对称误差接近 0 = 上下推反向对称（真实旋转轴）")
    print("      幅度 40~95 的是相机级；in_left=yes 表示左右也响应")
    print("\n参考：相机 0x1405F30F0 的上下响应 = "
          f"{up.get(0x1405F30F0, {}).get('dpitch', '?')} / "
          f"{down.get(0x1405F30F0, {}).get('dpitch', '?')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
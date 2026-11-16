"""tools/xmcp.py — 直接驱动 xenia-mcp 的 stdio 客户端（不等 UI 信任）

为什么需要它：
    WorkBuddy 要在连接器页面点「信任」才会把 MCP 工具挂进当前会话。
    在那之前，可以用这个脚本直接按 JSON-RPC 驱动同一个 server ——
    功能完全一致（读内存 / 快照 / 差分 / 找特征 / 追指针）。

关键点：
    xenia_connect 是**会话级**的，必须和后续调用活在同一个进程里。
    所以这里一次启动、连续执行多条命令，而不是一条命令起一个进程。

用法（heredoc，一次进程内执行多条）：
    python tools/xmcp.py <<'EOF'
    {"tool": "xenia_connect", "args": {}}
    {"tool": "xenia_read_values", "args": {"address": "0x405EA750", "kind": "vec4"}}
    {"tool": "xenia_snapshot", "args": {"address": "0x405EA700", "size": 4096, "label": "cam"}}
    EOF

每条输出一行 JSON（或纯文本），失败会打印 error。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 仓库装在工作区级 tools/ 下（不只服务 acvd-mouse），也允许放在本项目 tools/ 里
_CANDS = [
    os.path.join(os.path.dirname(ROOT), "tools", "xenia-mcp", "dist", "src", "index.js"),
    os.path.join(ROOT, "tools", "xenia-mcp", "dist", "src", "index.js"),
]
INDEX = next((p for p in _CANDS if os.path.exists(p)), _CANDS[0])


class Client:
    def __init__(self, index: str = INDEX):
        if not os.path.exists(index):
            raise SystemExit(f"找不到 {index}（先 npm install && npm run build）")
        self.p = subprocess.Popen(
            ["node", index],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", bufsize=1)
        self.n = 0

    def _rpc(self, method: str, params: dict) -> dict:
        self.n += 1
        req = {"jsonrpc": "2.0", "id": self.n, "method": method, "params": params}
        self.p.stdin.write(json.dumps(req) + "\n")
        self.p.stdin.flush()
        while True:
            line = self.p.stdout.readline()
            if not line:
                raise RuntimeError("xenia-mcp 进程已退出")
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") != self.n:
                continue            # 通知（initialized 等），跳过
            return msg

    def start(self) -> None:
        self._rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "workbuddy-xmcp", "version": "1.0"},
        })
        try:
            self.p.stdin.write(
                json.dumps({"jsonrpc": "2.0",
                            "method": "notifications/initialized"}) + "\n")
            self.p.stdin.flush()
        except Exception:
            pass

    def call(self, name: str, args: dict) -> str:
        msg = self._rpc("tools/call", {"name": name, "arguments": args})
        if "error" in msg:
            return f"ERROR: {msg['error'].get('message', msg['error'])}"
        res = msg.get("result", {})
        parts = []
        for c in res.get("content", []):
            if c.get("type") == "text":
                parts.append(c.get("text", ""))
        text = "\n".join(parts) if parts else json.dumps(res, ensure_ascii=False)
        if res.get("isError"):
            text = "ERROR: " + text
        return text

    def close(self) -> None:
        try:
            self.p.stdin.close()
        except Exception:
            pass
        try:
            self.p.wait(timeout=3)
        except Exception:
            self.p.kill()


def main() -> int:
    lines = [ln.strip() for ln in sys.stdin.read().splitlines() if ln.strip()]
    if not lines:
        print(__doc__)
        return 2
    c = Client()
    c.start()
    rc = 0
    for i, ln in enumerate(lines, 1):
        try:
            job = json.loads(ln)
        except json.JSONDecodeError as e:
            print(f"[{i}] 不是合法 JSON: {e}")
            rc = 1
            continue
        tool = job.get("tool", "")
        args = job.get("args", {})
        print(f"\n=== [{i}] {tool} {json.dumps(args, ensure_ascii=False)} ===")
        try:
            out = c.call(tool, args)
        except Exception as e:
            out = f"EXC: {e}"
            rc = 1
        print(out[:8000])
    c.close()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

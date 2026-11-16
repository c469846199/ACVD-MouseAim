"""tools/mcpq.py — 直接调用 xenia-mcp 的工具（MCP over stdio）

为什么要它：xenia-mcp 有 find_pattern / regions / snapshot / diff / follow_pointers
这些现成能力，比每次现写 Python 扫描快得多。但它是 MCP 服务器（给 AI 助手用的），
这里实现最小客户端，用命令行就能调。

用法
----
    python -m tools.mcpq list
    python -m tools.mcpq call xenia_connect
    python -m tools.mcpq call xenia_read_values '{"guest_address":1080000000,"type":"f32","count":4}'
    python -m tools.mcpq call xenia_regions
    python -m tools.mcpq call xenia_find_pattern '{"start":1073741824,"end":1090000000,"pattern":"00 00 00 00"}'

参数是 JSON，字段名以 `list` 输出的 schema 为准。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

# xenia-mcp 是另一个独立项目，不在本仓库里。按以下顺序找：
#   1. 环境变量 XENIA_MCP（用户显式指定）
#   2. 与本项目同级的 xenia-mcp/dist/src/index.js
#   3. 项目根目录下的 xenia-mcp/dist/src/index.js
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.environ.get("XENIA_MCP") or next(
    (p for p in (
        os.path.join(os.path.dirname(_ROOT), "xenia-mcp", "dist", "src", "index.js"),
        os.path.join(_ROOT, "xenia-mcp", "dist", "src", "index.js"),
    ) if os.path.exists(p)),
    "")


class Client:
    def __init__(self) -> None:
        if not os.path.exists(SERVER):
            raise SystemExit(f"找不到 {SERVER}，先在 xenia-mcp 目录 npm run build")
        self.p = subprocess.Popen(
            ["node", SERVER],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8",
            bufsize=1)
        self._id = 0
        self._wait: dict = {}
        self._lock = threading.Lock()
        self._buf: dict = {}
        self._err = []
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()
        self._init()

    def _read(self) -> None:
        for line in self.p.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            mid = msg.get("id")
            if mid is not None:
                with self._lock:
                    self._buf[mid] = msg

    def _read_err(self) -> None:
        for line in self.p.stderr:
            self._err.append(line.rstrip())

    def _send(self, obj: dict) -> None:
        self.p.stdin.write(json.dumps(obj) + "\n")
        self.p.stdin.flush()

    def request(self, method: str, params: dict | None = None,
                timeout: float = 25.0) -> dict:
        self._id += 1
        mid = self._id
        self._send({"jsonrpc": "2.0", "id": mid, "method": method,
                    "params": params or {}})
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < timeout:
            with self._lock:
                msg = self._buf.pop(mid, None)
            if msg is not None:
                return msg
            time.sleep(0.01)
        raise TimeoutError(f"{method} 超时；stderr: {' | '.join(self._err[-3:])}")

    def notify(self, method: str) -> None:
        self._send({"jsonrpc": "2.0", "method": method})

    def _init(self) -> None:
        self.request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "acvd-probe", "version": "0.1"},
        })
        self.notify("notifications/initialized")

    def tools(self) -> list:
        r = self.request("tools/list")
        return r.get("result", {}).get("tools", [])

    def call(self, name: str, args: dict | None = None) -> dict:
        r = self.request("tools/call", {"name": name, "arguments": args or {}})
        if "error" in r:
            return {"error": r["error"]}
        res = r.get("result", {})
        texts = [c.get("text", "") for c in res.get("content", [])
                 if c.get("type") == "text"]
        out = {"text": "\n".join(texts)}
        if res.get("isError"):
            out["isError"] = True
        return out

    def close(self) -> None:
        try:
            self.p.terminate()
        except Exception:
            pass


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]
    c = Client()
    try:
        if cmd == "list":
            for t in c.tools():
                print(f"{t['name']:24} {t.get('description','')[:90]}")
            return
        if cmd == "call":
            name = sys.argv[2]
            args = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
            out = c.call(name, args)
            print(out.get("text") or json.dumps(out, ensure_ascii=False)[:2000])
            if out.get("isError"):
                sys.exit(1)
            return
        if cmd == "seq":
            # 一次连接里按顺序调多个工具（snapshot → diff 这类必须同一个会话）
            # step 可带 "delay": 秒数 —— 在该步**之前**等待。
            # 没有它时 snapshot → diff 是瞬时连发，实测恒为 0 word(s) changed。
            steps = json.loads(sys.argv[2])
            for st in steps:
                if st.get("delay"):
                    time.sleep(float(st["delay"]))
                nm = st["name"]
                out = c.call(nm, st.get("args") or {})
                print(f"--- {nm} ---")
                print(out.get("text") or json.dumps(out, ensure_ascii=False)[:2000])
            return
        print(f"未知命令 {cmd}")
    finally:
        c.close()


if __name__ == "__main__":
    main()

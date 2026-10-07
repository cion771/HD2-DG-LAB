"""模拟 DG-LAB 手机 App 的 WebSocket 客户端（联调用，没有手机也能测）。

流程：连接 -> 收发 bind 握手 -> 回传通道上限 -> 打印收到的全部指令。

用法：
    1. 先启动本程序（界面上会显示 clientId / ws 地址 / 二维码）
    2. python tools/fake_app.py --url ws://127.0.0.1:9999/<clientId>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import websockets  # noqa: E402

APP_ID = "11111111-2222-3333-4444-555555555555"


async def run(url: str, limit_a: int, limit_b: int) -> None:
    async with websockets.connect(url) as ws:
        print(f"[fake-app] 已连接 {url}")
        async for raw in ws:
            print(f"[fake-app] <- {raw}")
            try:
                data = json.loads(raw)
            except Exception:
                continue
            mtype = data.get("type")
            if mtype == "bind" and data.get("message") == "targetId":
                target = data.get("targetId") or APP_ID
                for payload in (
                    {"type": "bind", "clientId": APP_ID, "targetId": target, "message": "DGLAB"},
                    {"type": "bind", "clientId": APP_ID, "targetId": target, "message": "200"},
                    {"type": "msg", "clientId": APP_ID, "targetId": target,
                     "message": f"strength-0+0+{limit_a}+{limit_b}"},
                ):
                    await ws.send(json.dumps(payload))
                print(f"[fake-app] -> 已完成绑定，通道上限 A={limit_a} B={limit_b}")
            elif mtype == "heartbeat":
                await ws.send(json.dumps({
                    "type": "heartbeat", "clientId": APP_ID,
                    "targetId": data.get("targetId", APP_ID), "message": "200",
                }))


def main() -> None:
    ap = argparse.ArgumentParser(description="模拟 DG-LAB App（联调用）")
    ap.add_argument("--url", required=True, help="ws://127.0.0.1:9999/<clientId>")
    ap.add_argument("--limit-a", type=int, default=200)
    ap.add_argument("--limit-b", type=int, default=200)
    args = ap.parse_args()
    try:
        asyncio.run(run(args.url, args.limit_a, args.limit_b))
    except KeyboardInterrupt:
        print("\n[fake-app] 已退出")


if __name__ == "__main__":
    main()

"""模拟游戏内 Lua 桥（hd2_coyote_bridge）的 UDP 上报，用来在没有游戏时联调。

用法：
    # 1. 先启动控制器（config.json 里 source = "hook"，默认端口 47777）
    # 2. 再跑这个脚本，它会按脚本演一遍受伤 -> 肢体损伤 -> 阵亡 -> 复活
    python tools/fake_addon.py --port 47777

    # 单发一条，方便手工验证
    python tools/fake_addon.py --once --hp 0.5 --limbs 1,0,0
"""

from __future__ import annotations

import argparse
import json
import socket
import time

PROTOCOL_VERSION = 1


def send(sock: socket.socket, addr, payload: dict) -> None:
    sock.sendto(json.dumps(payload, separators=(",", ":")).encode("utf-8"), addr)
    print(f"[fake-addon] -> {payload}")


def main() -> None:
    ap = argparse.ArgumentParser(description="模拟 HD2 游戏内 Lua 桥的 UDP 上报")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=47777)
    ap.add_argument("--build", default="1.8.46015.0")
    ap.add_argument("--once", action="store_true", help="只发一帧")
    ap.add_argument("--hp", type=float, default=1.0, help="0~1，或配合 --hp-max 用绝对值")
    ap.add_argument("--hp-max", type=float, default=100.0)
    ap.add_argument("--limbs", default="0,0,0", help="逗号分隔，如 1,0,1")
    ap.add_argument("--dead", action="store_true")
    args = ap.parse_args()

    addr = (args.host, args.port)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    started = time.monotonic()

    def state(hp: float, limbs: str, dead: bool = False, bleeding: int = 0) -> dict:
        return {
            "v": PROTOCOL_VERSION, "ev": "state",
            "t": round(time.monotonic() - started, 3),
            "hp": round(hp * args.hp_max, 2), "hp_max": args.hp_max,
            "limbs": [int(x) for x in limbs.split(",") if x != ""],
            "bleeding": bleeding, "dead": 1 if dead else 0,
        }

    if args.once:
        send(sock, addr, state(args.hp, args.limbs, args.dead))
        return

    send(sock, addr, {"v": PROTOCOL_VERSION, "ev": "hello",
                      "build": args.build, "profile": "fake"})

    script = [
        (0.0, 1.00, "0,0,0", False, 0, "满血"),
        (1.0, 0.88, "0,0,0", False, 0, "挨了一发"),
        (2.0, 0.66, "1,0,0", True, 1, "左臂受伤 + 流血"),
        (3.5, 0.40, "1,1,0", True, 1, "躯干也受伤（低血量）"),
        (5.0, 0.00, "1,1,1", False, 0, "阵亡（血条清空）"),
        (7.0, 0.05, "0,0,0", False, 0, "被增援"),
        (8.0, 1.00, "0,0,0", False, 0, "恢复"),
    ]
    try:
        idx = 0
        while idx < len(script):
            at, hp, limbs, dead, bleed, label = script[idx]
            elapsed = time.monotonic() - started
            if elapsed < at:
                time.sleep(min(0.05, at - elapsed))
                continue
            print(f"[fake-addon] {at:4.1f}s {label}")
            send(sock, addr, state(hp, limbs, dead, bleed))
            nxt = script[idx + 1][0] if idx + 1 < len(script) else at + 1.5
            while time.monotonic() - started < nxt:
                send(sock, addr, state(hp, limbs, dead, bleed))  # 10Hz 状态流
                time.sleep(0.1)
            idx += 1
        time.sleep(1.0)
        send(sock, addr, {"v": PROTOCOL_VERSION, "ev": "bye"})
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

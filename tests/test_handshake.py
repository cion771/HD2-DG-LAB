"""DG-LAB Socket 设备联调测试：用真的 WebSocket 跑一遍握手与指令。

不需要手机、不需要硬件 —— 测试自己扮演 App。
"""

from __future__ import annotations

import asyncio
import json
import socket
import unittest

import websockets

from hd2coyote.device.dglab_socket import DGLabSocketDevice
from hd2coyote.events import FeedbackButton

APP_ID = "11111111-2222-3333-4444-555555555555"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class TestSocketDevice(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.events: list[object] = []
        self.port = free_port()
        self.dev = DGLabSocketDevice(host="127.0.0.1", port=self.port,
                                     on_event=self.events.append)
        self.dev.start()
        self.url = f"ws://127.0.0.1:{self.port}/{self.dev.client_id}"

    async def asyncTearDown(self) -> None:
        self.dev.stop()

    async def _bind(self, ws) -> str:
        first = json.loads(await asyncio.wait_for(ws.recv(), 2))
        self.assertEqual(first["type"], "bind")
        self.assertEqual(first["message"], "targetId")
        self.assertEqual(first["clientId"], self.dev.client_id)
        target = first["targetId"]
        await ws.send(json.dumps({"type": "bind", "clientId": APP_ID,
                                  "targetId": target, "message": "DGLAB"}))
        ack = json.loads(await asyncio.wait_for(ws.recv(), 2))
        self.assertEqual(ack["type"], "bind")
        self.assertEqual(ack["message"], "200")
        self.assertEqual(ack["targetId"], target)
        zeros = [json.loads(await asyncio.wait_for(ws.recv(), 2)) for _ in range(2)]
        self.assertEqual([z["message"] for z in zeros],
                         ["strength-1+2+0", "strength-2+2+0"])
        return target

    async def test_bind_then_strength(self) -> None:
        async with websockets.connect(self.url) as ws:
            target = await self._bind(ws)
            self.assertTrue(self.dev.connected)
            self.assertEqual(self.dev.app_count, 1)

            # App 回传通道上限
            await ws.send(json.dumps({"type": "msg", "clientId": APP_ID, "targetId": target,
                                      "message": "strength-0+0+120+80"}))
            await asyncio.sleep(0.3)
            limits = self.dev.limits
            self.assertEqual((limits.a, limits.b), (120, 80))

            # 服务端下发绝对强度（官方格式 strength-{通道}+{模式}+{值}）
            self.dev.set_strength(60, 40)
            got = [json.loads(await asyncio.wait_for(ws.recv(), 2))["message"] for _ in range(2)]
            self.assertEqual(got, ["strength-1+2+60", "strength-2+2+40"])

            self.dev.clear("A")
            cleared = json.loads(await asyncio.wait_for(ws.recv(), 2))["message"]
            self.assertEqual(cleared, "clear-1")

    async def test_wave_chunking_respects_frame_limit(self) -> None:
        async with websockets.connect(self.url) as ws:
            await self._bind(ws)
            self.dev.send_wave("B", ["0A0A0A0A64646464"] * 250)
            total = 0
            messages = 0
            while total < 250:
                raw = await asyncio.wait_for(ws.recv(), 3)
                self.assertLess(len(raw), 1950, "单帧超过 App 的 1950 字符上限")
                payload = json.loads(raw)
                self.assertTrue(payload["message"].startswith("pulse-B:"))
                units = json.loads(payload["message"][len("pulse-B:"):])
                self.assertLessEqual(len(units), 100)
                total += len(units)
                messages += 1
            self.assertEqual(total, 250)
            self.assertGreaterEqual(messages, 3)

    async def test_feedback_button_event(self) -> None:
        async with websockets.connect(self.url) as ws:
            target = await self._bind(ws)
            await ws.send(json.dumps({"type": "msg", "clientId": APP_ID, "targetId": target,
                                      "message": "feedback-3"}))
            await asyncio.sleep(0.2)
            self.assertTrue(any(isinstance(e, FeedbackButton) and e.index == 3
                                for e in self.events))

    async def test_heartbeat_reply(self) -> None:
        async with websockets.connect(self.url) as ws:
            target = await self._bind(ws)
            await ws.send(json.dumps({"type": "heartbeat", "clientId": APP_ID,
                                      "targetId": target, "message": ""}))
            reply = json.loads(await asyncio.wait_for(ws.recv(), 2))
            self.assertEqual(reply["type"], "heartbeat")
            self.assertEqual(reply["message"], "200")

    async def test_disconnect_marks_disconnected(self) -> None:
        async with websockets.connect(self.url) as ws:
            await self._bind(ws)
            self.assertTrue(self.dev.connected)
        await asyncio.sleep(0.4)
        self.assertFalse(self.dev.connected)


if __name__ == "__main__":
    unittest.main()

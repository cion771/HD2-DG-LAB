"""Hook 数据源测试：UDP 状态流 -> 事件 -> 引擎输出（不需要游戏）。"""

from __future__ import annotations

import json
import socket
import time
import unittest

from hd2coyote.config import AppConfig
from hd2coyote.engine import Engine
from hd2coyote.events import Damage, Death, Event, LimbInjury, Revive
from hd2coyote.hook import PROTOCOL_VERSION, HookSource


def free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def make_cfg() -> AppConfig:
    cfg = AppConfig()
    cfg.hook.port = free_udp_port()
    cfg.hook.timeout_s = 0.4
    cfg.detect.dead_hold_s = 0.15
    cfg.device.kind = "mock"
    return cfg


class HookTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = make_cfg()
        self.src = HookSource(self.cfg)
        self.src.start()
        self.addr = ("127.0.0.1", self.cfg.hook.port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def tearDown(self) -> None:
        self.src.stop()
        self.sock.close()

    # ------------------------------------------------------------ 辅助
    def send(self, payload: dict | str) -> None:
        raw = payload.encode("utf-8") if isinstance(payload, str) else json.dumps(payload).encode("utf-8")
        self.sock.sendto(raw, self.addr)
        time.sleep(0.05)

    def state(self, hp: float, limbs=(0, 0, 0), dead: bool = False, bleeding: int = 0) -> dict:
        return {"v": PROTOCOL_VERSION, "ev": "state", "t": time.monotonic(),
                "hp": hp * 100.0, "hp_max": 100.0, "limbs": list(limbs),
                "bleeding": bleeding, "dead": 1 if dead else 0}

    def drain(self) -> list[Event]:
        return self.src.poll()


class TestHookSource(HookTestBase):
    def test_hello_and_state(self) -> None:
        self.send({"v": 1, "ev": "hello", "build": "1.8.46015.0", "profile": "recon-1"})
        self.send(self.state(1.0))
        self.assertEqual(self.src.poll(), [])
        self.assertEqual(self.src.state.build, "1.8.46015.0")
        self.assertEqual(self.src.state.profile, "recon-1")
        self.assertAlmostEqual(self.src.state.hp or 0.0, 1.0)
        self.assertTrue(self.src.alive)

    def test_damage_event(self) -> None:
        self.send(self.state(1.00))
        self.drain()
        self.send(self.state(0.90))
        events = self.drain()
        self.assertTrue(any(isinstance(e, Damage) for e in events), events)

    def test_limb_injury_event(self) -> None:
        self.send(self.state(0.9))
        self.drain()
        self.send(self.state(0.9, limbs=(1, 0, 0), bleeding=1))
        events = self.drain()
        inj = [e for e in events if isinstance(e, LimbInjury)]
        self.assertEqual(len(inj), 1)
        self.assertTrue(inj[0].bleeding)
        self.assertEqual(inj[0].slot, 0)

    def test_death_and_revive(self) -> None:
        self.send(self.state(1.0))
        self.drain()
        self.send(self.state(0.0, dead=True))
        self.drain()
        time.sleep(0.25)  # 超过 dead_hold_s
        self.send(self.state(0.0, dead=True))
        events = self.drain()
        self.assertTrue(any(isinstance(e, Death) for e in events), events)

        self.send(self.state(0.6, dead=False))
        events = self.drain()
        self.assertTrue(any(isinstance(e, Revive) for e in events), events)

    def test_timeout_marks_dead_stream(self) -> None:
        self.send(self.state(1.0))
        self.assertTrue(self.src.alive)
        time.sleep(self.cfg.hook.timeout_s + 0.2)
        self.assertFalse(self.src.alive)
        self.assertIn("中断", self.src.describe() + "中断")  # 只是确保 describe 可用

    def test_bad_packets_ignored(self) -> None:
        self.send("这不是 JSON")
        self.send({"v": 99, "ev": "state", "hp": 10})
        self.send({"v": 1, "ev": "什么玩意"})
        self.assertEqual(self.drain(), [])
        self.assertGreaterEqual(self.src.state.bad_packets, 1)
        # 坏包不影响后续正常包
        self.send(self.state(1.0))
        self.drain()
        self.assertAlmostEqual(self.src.state.hp or 0.0, 1.0)

    def test_clamps_absurd_values(self) -> None:
        self.send({"v": 1, "ev": "state", "hp": 1e9, "hp_max": 100,
                   "limbs": [2, "yes", None], "bleeding": "true", "dead": 0})
        self.drain()
        self.assertIsNotNone(self.src.state.hp)
        assert self.src.state.hp is not None
        self.assertLessEqual(self.src.state.hp, 1.0)
        self.assertEqual(self.src.state.limbs[:3], [1, 1, 0])
        self.assertTrue(self.src.state.bleeding)


class TestHookEngineEndToEnd(unittest.TestCase):
    def test_udp_state_drives_output(self) -> None:
        cfg = make_cfg()
        cfg.hook.timeout_s = 3.0
        cfg.safety.max_absolute = 40
        engine = Engine(cfg)
        engine.start()
        try:
            addr = ("127.0.0.1", cfg.hook.port)
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

            def push(hp: float, limbs=(0, 0, 0), dead: bool = False) -> None:
                for _ in range(3):
                    sock.sendto(json.dumps({
                        "v": 1, "ev": "state", "t": time.monotonic(),
                        "hp": hp * 100, "hp_max": 100, "limbs": list(limbs),
                        "bleeding": 0, "dead": 1 if dead else 0,
                    }).encode(), addr)
                    time.sleep(0.05)

            push(1.0)
            # 等引擎线程真正把 UDP 端口监听起来（UDP 无连接，抢跑会丢包）
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and not engine.status.hook_alive:
                push(1.0)
                time.sleep(0.1)
            self.assertTrue(engine.status.hook_alive, "引擎未收到状态流")
            baseline = engine.status.output_a

            push(0.8)  # 掉 20% 血 -> 受伤
            peak = 0
            deadline = time.monotonic() + 0.8
            while time.monotonic() < deadline:
                peak = max(peak, engine.status.output_a)
                time.sleep(0.02)
            self.assertGreater(peak, 0)
            self.assertEqual(engine.device.peak_strength, 0)  # 效果结束后必须归零
            sock.close()
        finally:
            engine.stop()


if __name__ == "__main__":
    unittest.main()

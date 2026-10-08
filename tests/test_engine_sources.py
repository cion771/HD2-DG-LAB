"""引擎 ↔ 事件源 ↔ 惩罚累积的集成：多源、波形库优先、断流静音。

这些用例走真线程 / 真 UDP，因为要验证的正是「引擎循环里到底发生了什么」。
"""

from __future__ import annotations

import json
import logging
import socket
import time
import unittest

from hd2coyote import wave_lib, waves
from hd2coyote.config import AppConfig
from hd2coyote.engine import Engine
from hd2coyote.events import Damage, Death, LimbInjury

LOG = logging.getLogger("hd2coyote.tests.engine_sources")


def free_udp_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def wait_for(predicate, timeout: float = 3.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


class TestHttpOnlyEngine(unittest.TestCase):
    """不装游戏桥，只靠 HTTP 上报也能玩（别的游戏/直播工具/手机都能当事件源）。"""

    def setUp(self) -> None:
        cfg = AppConfig()
        cfg.device.kind = "mock"
        cfg.sources.enabled = ["http"]
        cfg.sources.http_host = "127.0.0.1"
        cfg.sources.http_port = 0                     # 让系统随便给个端口
        cfg.rules["damage"].base_pct = 20.0
        self.cfg = cfg
        self.engine = Engine(cfg, LOG)

    def tearDown(self) -> None:
        self.engine.stop()

    def test_http_event_reaches_the_device(self) -> None:
        self.engine.start()
        self.assertTrue(wait_for(lambda: self.engine.source("http") is not None))
        src = self.engine.source("http")
        assert src is not None
        self.assertIsNone(self.engine.hook)           # 没启用游戏桥
        self.engine.arm()
        src.push([Damage(severity=20.0, hp_before=1.0, hp_after=0.8)])
        self.assertTrue(wait_for(lambda: self.engine.device.peak_strength > 0),
                        "HTTP 事件没有传到设备")
        self.assertTrue(self.engine.status.detecting)
        names = [row["name"] for row in self.engine.status.sources]
        self.assertIn("http", names)

    def test_status_lists_every_source(self) -> None:
        self.engine.start()
        self.assertTrue(wait_for(lambda: bool(self.engine.status.sources)))
        row = self.engine.status.sources[0]
        for key in ("name", "label", "alive", "critical", "detail", "events"):
            self.assertIn(key, row)


class TestBridgeDeadman(unittest.TestCase):
    """游戏桥断流 = 玩家状态不可信：立刻静音，等它回来。"""

    def setUp(self) -> None:
        cfg = AppConfig()
        cfg.device.kind = "mock"
        cfg.source = "hook"
        cfg.sources.enabled = ["game_bridge"]
        cfg.hook.port = free_udp_port()
        cfg.hook.timeout_s = 0.5
        cfg.rules["damage"].base_pct = 20.0
        self.cfg = cfg
        self.port = cfg.hook.port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.engine = Engine(cfg, LOG)

    def tearDown(self) -> None:
        self.engine.stop()
        self.sock.close()

    def send(self, hp: float, **extra) -> None:
        packet = {"v": 1, "ev": "state", "t": time.time(), "hp": hp * 100.0,
                  "hp_max": 100.0, "limbs": [0, 0, 0], "bleeding": 0, "dead": 0}
        packet.update(extra)
        self.sock.sendto(json.dumps(packet).encode("utf-8"), ("127.0.0.1", self.port))

    def test_bridge_stream_mutes_then_recovers(self) -> None:
        self.engine.start()
        self.assertTrue(wait_for(lambda: self.engine.hook is not None), "桥没起来")
        self.engine.arm()
        self.send(1.00)
        time.sleep(0.15)
        self.send(0.70)                               # 掉血 → 触发伤害规则
        self.assertTrue(wait_for(lambda: self.engine.device.peak_strength > 0),
                        "游戏桥事件没有产生输出")
        self.assertTrue(self.engine.status.hook_alive)

        # 停发状态包 → 超过 timeout_s 之后引擎必须静音
        self.assertTrue(wait_for(lambda: self.engine.status.output_a == 0, timeout=4.0),
                        f"断流后没有静音（output_a={self.engine.status.output_a}）")
        self.assertFalse(self.engine.status.hook_alive)
        self.assertFalse(self.engine._effects, "断流后还留着效果")

        # 状态流回来 → 又能输出
        self.send(1.00)
        time.sleep(0.1)
        self.send(0.60)
        self.assertTrue(wait_for(lambda: self.engine.device.peak_strength > 0),
                        "状态流恢复后没有继续输出")
        self.assertTrue(self.engine.status.hook_alive)


class TestWaveLibraryPriority(unittest.TestCase):
    def test_library_wins_over_builtin_preset(self) -> None:
        cfg = AppConfig()
        cfg.device.kind = "mock"
        wave_lib.set_entry(cfg, "死亡测试", units=["1414141464646464"], default_ms=300)
        engine = Engine(cfg, LOG)
        units = engine._wave_units("死亡测试", 300.0)
        self.assertEqual(set(units), {"1414141464646464"})
        self.assertEqual(len(units), waves.units_for(300.0))

    def test_unknown_wave_falls_back_instead_of_raising(self) -> None:
        cfg = AppConfig()
        cfg.device.kind = "mock"
        engine = Engine(cfg, LOG)
        units = engine._wave_units("根本没有这个波形", 200.0)
        self.assertTrue(units)                        # 退回内置预设，不炸
        self.assertEqual(units, waves.build("ramp_up", 200.0))

    def test_test_pulse_uses_the_library(self) -> None:
        cfg = AppConfig()
        cfg.device.kind = "mock"
        wave_lib.set_entry(cfg, "试打用", units=["0A0A0A0A64646464"])
        engine = Engine(cfg, LOG)
        engine.device.start()
        engine.test_pulse(pct=5.0, ms=300.0, wave="试打用")
        self.assertEqual(engine.device.peak_strength, 0)   # 还没 tick，先不比较
        engine.tick(time.monotonic() + 0.01)
        self.assertGreater(engine.device.peak_strength, 0)


class TestRampIntegration(unittest.TestCase):
    def setUp(self) -> None:
        cfg = AppConfig()
        cfg.device.kind = "mock"
        cfg.ramp.enabled = True
        cfg.ramp.per_event = 2.0
        cfg.ramp.hp_missing_pct = 0.0
        cfg.ramp.ceiling_pct = 10.0
        cfg.ramp.apply_to = ["damage"]
        self.cfg = cfg
        self.engine = Engine(cfg, LOG)
        self.engine.device.start()
        self.engine.safety.arm()
        self.t0 = time.monotonic()
        self.engine._last_tick = self.t0

    def feed(self, event, offset: float = 0.0) -> None:
        now = self.t0 + offset
        self.engine.inject(event, now)
        self.engine._update_status(now)

    def damage(self, offset: float = 0.0) -> None:
        # severity 要大于 cfg.detect.damage_min_pct，否则规则层直接忽略
        self.feed(Damage(severity=10.0, hp_before=1.0, hp_after=0.9), offset)

    def test_bonus_accumulates_and_caps(self) -> None:
        self.damage(0.0)
        self.assertAlmostEqual(self.engine.status.ramp_pct, 2.0, places=3)
        for i in range(1, 9):
            self.damage(i * 0.01)
        self.assertAlmostEqual(self.engine.status.ramp_pct, 10.0, places=3)   # 封顶
        self.assertIn("上限", self.engine.status.ramp_detail)

    def test_death_resets_accumulation(self) -> None:
        self.damage(0.0)
        self.damage(0.01)
        self.assertGreater(self.engine.status.ramp_pct, 0.0)
        self.feed(Death(), 0.02)
        self.assertEqual(self.engine.status.ramp_pct, 0.0)

    def test_apply_to_filters_other_events(self) -> None:
        self.feed(LimbInjury(slot=0, bleeding=True), 0.0)
        self.assertEqual(self.engine.status.ramp_pct, 0.0)

    def test_bonus_is_added_to_the_output_pct(self) -> None:
        before = self.cfg.rules["damage"].base_pct
        self.damage(0.0)
        self.damage(0.01)
        self.damage(0.02)
        self.damage(0.03)
        effect = self.engine._effects[0]
        self.assertGreater(effect.action.pct, before)          # 累积真的加进去了

    def test_disabled_ramp_changes_nothing(self) -> None:
        self.cfg.ramp.enabled = False
        self.engine.ramp.cfg = self.cfg.ramp
        self.damage(0.0)
        self.assertEqual(self.engine.status.ramp_pct, 0.0)
        self.assertEqual(self.engine.status.ramp_detail, "关闭")


if __name__ == "__main__":
    unittest.main()

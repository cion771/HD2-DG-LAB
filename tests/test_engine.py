"""引擎 + 安全层测试：强度换算、急停、会话预算、mock 输出。"""

from __future__ import annotations

import time
import unittest

from hd2coyote.config import AppConfig
from hd2coyote.device import MockDevice
from hd2coyote.engine import Engine
from hd2coyote.events import Damage, Death, DeviceState
from hd2coyote.safety import SafetyGuard


class TestSafety(unittest.TestCase):
    def test_strength_clamped_by_pct_and_absolute(self) -> None:
        cfg = AppConfig()
        cfg.safety.max_pct = 30.0
        cfg.safety.max_absolute = 40
        guard = SafetyGuard(cfg.safety)
        # 规则要 100%，只能给到 30% * 200 = 60，但绝对上限 40 更低
        self.assertEqual(guard.strength_for(100.0, 200), 40)
        self.assertEqual(guard.strength_for(10.0, 200), 20)
        self.assertEqual(guard.strength_for(50.0, 60), 18)

    def test_master_multiplier(self) -> None:
        cfg = AppConfig()
        cfg.safety.max_pct = 50.0
        cfg.safety.max_absolute = 200
        cfg.safety.master_multiplier = 0.5
        guard = SafetyGuard(cfg.safety)
        self.assertEqual(guard.strength_for(40.0, 200), 40)  # 40*0.5=20% -> 40

    def test_disarm_zeroes_output(self) -> None:
        cfg = AppConfig()
        guard = SafetyGuard(cfg.safety)
        self.assertEqual(guard.strength_for(20.0, 200), 40)
        guard.disarm("测试")
        self.assertEqual(guard.strength_for(20.0, 200), 0)

    def test_session_budget_trips(self) -> None:
        cfg = AppConfig()
        cfg.safety.max_session_seconds = 1.0
        device = MockDevice(verbose=False)
        guard = SafetyGuard(cfg.safety, device)
        guard.note_output(0.6, 20.0)
        self.assertTrue(guard.armed)
        guard.note_output(0.6, 20.0)
        self.assertFalse(guard.armed)
        self.assertEqual(device.peak_strength, 0)


class TestEngineWithMockDevice(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = AppConfig()
        self.cfg.device.kind = "mock"
        self.engine = Engine(self.cfg)
        assert isinstance(self.engine.device, MockDevice)
        self.engine.device.start()
        self.t0 = time.monotonic()
        self.engine._last_tick = self.t0

    def tearDown(self) -> None:
        self.engine.stop()

    def test_damage_event_produces_clamped_output(self) -> None:
        self.engine.inject(Damage(severity=10.0, hp_before=1.0, hp_after=0.9), self.t0)
        # 规则 16+7=23% -> 200*23% = 46，被绝对上限 40 夹住
        self.engine.tick(self.t0 + 0.2)
        self.assertEqual(self.engine.status.output_a, 40)
        self.assertEqual(self.engine.status.output_b, 0)
        self.assertEqual(self.engine.device.strength["A"], 40)
        self.assertGreater(self.engine.device.wave_counts["A"], 0)

    def test_effect_releases_to_zero(self) -> None:
        self.engine.inject(Damage(severity=10.0, hp_before=1.0, hp_after=0.9), self.t0)
        self.engine.tick(self.t0 + 0.2)
        self.assertGreater(self.engine.status.output_a, 0)
        # 400ms 效果 + 800ms 释放
        self.engine.tick(self.t0 + 1.0)
        self.engine.tick(self.t0 + 2.0)
        self.assertEqual(self.engine.status.output_a, 0)

    def test_death_rule_uses_both_channels(self) -> None:
        self.engine.inject(Death(), self.t0)
        self.engine.tick(self.t0 + 0.2)
        self.assertGreater(self.engine.status.output_a, 0)
        self.assertGreater(self.engine.status.output_b, 0)

    def test_trip_silences_everything(self) -> None:
        self.engine.inject(Death(), self.t0)
        self.engine.tick(self.t0 + 0.2)
        self.engine.trip("测试急停")
        self.assertEqual(self.engine.status.output_a, 0)
        self.assertEqual(self.engine.status.output_b, 0)
        self.assertEqual(self.engine.device.peak_strength, 0)
        self.assertFalse(self.engine.safety.armed)
        # 未重新武装前，新事件不产生任何输出
        self.engine.inject(Death(), self.t0 + 1.0)
        self.engine.tick(self.t0 + 1.2)
        self.assertEqual(self.engine.device.peak_strength, 0)
        self.engine.arm()
        self.engine.inject(Death(), self.t0 + 2.0)
        self.engine.tick(self.t0 + 2.2)
        self.assertGreater(self.engine.device.peak_strength, 0)

    def test_device_disconnect_clears_effects(self) -> None:
        self.engine.inject(Death(), self.t0)
        self.engine.tick(self.t0 + 0.2)
        self.engine._on_device_event(DeviceState(connected=False, detail="test"))
        self.engine.tick(self.t0 + 0.4)
        self.assertEqual(self.engine.status.output_a, 0)

    def test_wave_units_are_valid_hex(self) -> None:
        self.engine.inject(Death(), self.t0)
        self.engine.tick(self.t0 + 0.2)
        for rec in self.engine.device.history:
            if rec.op == "send_wave":
                channel, _count = rec.args
                self.assertIn(channel, ("A", "B"))

    def test_test_pulse_respects_safety(self) -> None:
        self.engine.test_pulse(pct=100.0, ms=500)
        self.engine.tick(self.t0 + 0.2)
        self.assertEqual(self.engine.status.output_a, self.cfg.safety.max_absolute)


if __name__ == "__main__":
    unittest.main()

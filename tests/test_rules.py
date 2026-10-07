"""规则层测试：事件 -> 强度/波形/时长 的换算。"""

from __future__ import annotations

import unittest

from hd2coyote.config import AppConfig
from hd2coyote.events import Damage, Death, LimbInjury, LowHealth
from hd2coyote.rules import RuleEngine


class TestRules(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = AppConfig()
        self.rules = RuleEngine(self.cfg)

    def test_damage_scaling(self) -> None:
        # base 16 + per_10hp 7 * (10% / 10) = 23
        acts = self.rules.actions(Damage(severity=10.0, hp_before=1.0, hp_after=0.9), now=100.0)
        self.assertEqual(len(acts), 1)
        self.assertAlmostEqual(acts[0].pct, 23.0)
        self.assertEqual(acts[0].channel, "A")
        self.assertEqual(len(acts[0].units), 4)  # 400ms

    def test_damage_below_threshold_ignored(self) -> None:
        acts = self.rules.actions(Damage(severity=1.0, hp_before=1.0, hp_after=0.99), now=100.0)
        self.assertEqual(acts, [])

    def test_damage_cooldown(self) -> None:
        ev = Damage(severity=20.0, hp_before=1.0, hp_after=0.8)
        self.assertEqual(len(self.rules.actions(ev, now=100.0)), 1)
        # 冷却 400ms 内不再触发
        self.assertEqual(self.rules.actions(ev, now=100.2), [])
        self.assertEqual(len(self.rules.actions(ev, now=100.5)), 1)

    def test_limb_injury_and_bleeding_bonus(self) -> None:
        acts = self.rules.actions(LimbInjury(slot=1, bleeding=True, name="躯干"), now=200.0)
        self.assertAlmostEqual(acts[0].pct, 30 * 1.2)
        self.assertEqual(acts[0].channel, "both")
        # 同一槽位受冷却保护，不同槽位不受影响
        self.assertEqual(self.rules.actions(LimbInjury(slot=1, bleeding=False), now=200.1), [])
        self.assertEqual(len(self.rules.actions(LimbInjury(slot=2, bleeding=False), now=200.1)), 1)

    def test_disabled_rule(self) -> None:
        self.cfg.rules["death"].enabled = False
        self.assertEqual(self.rules.actions(Death(), now=300.0), [])

    def test_death_duration_clamped_by_safety(self) -> None:
        self.cfg.safety.max_event_ms = 2000.0
        acts = self.rules.actions(Death(), now=400.0)
        self.assertEqual(acts[0].duration_ms, 2000.0)
        self.assertEqual(len(acts[0].units), 20)

    def test_low_health_default_off(self) -> None:
        self.assertEqual(self.rules.actions(LowHealth(ratio=0.3), now=500.0), [])
        self.cfg.rules["low_health"].enabled = True
        self.assertEqual(len(self.rules.actions(LowHealth(ratio=0.3), now=500.0)), 1)

    def test_channel_default_and_alternate(self) -> None:
        self.cfg.device.channel = "B"
        self.cfg.rules["limb_injury"].channel = "default"
        acts = self.rules.actions(LimbInjury(slot=0), now=600.0)
        self.assertEqual(acts[0].channel, "B")

        self.cfg.rules["limb_injury"].channel = "alternate"
        self.cfg.rules["limb_injury"].cooldown_ms = 0
        first = self.rules.actions(LimbInjury(slot=0), now=700.0)[0].channel
        second = self.rules.actions(LimbInjury(slot=0), now=701.0)[0].channel
        self.assertNotEqual(first, second)

    def test_wave_preset_respected(self) -> None:
        self.cfg.rules["damage"].wave = "death"
        acts = self.rules.actions(Damage(severity=50.0, hp_before=1.0, hp_after=0.5), now=800.0)
        self.assertEqual(len(acts[0].units), 4)


if __name__ == "__main__":
    unittest.main()

"""惩罚累积：事件叠加 + 血量越低越强 + 长时间无事件回落 + 阵亡清零。

纯计算、注入时间戳，所以测试不需要 sleep。
"""

from __future__ import annotations

import unittest

from hd2coyote.config import RampConfig
from hd2coyote.events import Damage, Death, LimbInjury, LowHealth, Revive
from hd2coyote.ramp import PunishmentRamp, rule_name


class TestRuleName(unittest.TestCase):
    def test_maps_events_to_rule_keys(self) -> None:
        self.assertEqual(rule_name(Damage()), "damage")
        self.assertEqual(rule_name(LimbInjury()), "limb_injury")
        self.assertEqual(rule_name(Death()), "death")
        self.assertEqual(rule_name(Revive()), "revive")
        self.assertEqual(rule_name(LowHealth()), "low_health")


class TestRamp(unittest.TestCase):
    def make(self, **overrides) -> PunishmentRamp:
        values = dict(enabled=True, per_event=2.0, hp_missing_pct=20.0, ceiling_pct=25.0,
                      decay_after_s=4.0, decay_per_s=1.5, reset_on_death=True,
                      apply_to=["damage", "limb_injury"])
        values.update(overrides)
        return PunishmentRamp(RampConfig(**values))

    def test_disabled_never_adds(self) -> None:
        ramp = self.make(enabled=False)
        ramp.note_hp(0.0)
        ramp.on_event(Damage(severity=10.0), now=100.0)
        self.assertEqual(ramp.accumulated, 0.0)
        self.assertEqual(ramp.bonus(), 0.0)
        self.assertEqual(ramp.describe(), "关闭")

    def test_accumulates_per_event(self) -> None:
        ramp = self.make(per_event=2.0)
        for i in range(3):
            ramp.on_event(Damage(severity=10.0), now=100.0 + i)
        self.assertAlmostEqual(ramp.accumulated, 6.0)
        self.assertAlmostEqual(ramp.bonus(), 6.0)
        self.assertEqual(ramp.events, 3)

    def test_accumulation_is_capped(self) -> None:
        ramp = self.make(per_event=10.0, ceiling_pct=25.0)
        for i in range(10):
            ramp.on_event(Damage(severity=1.0), now=100.0 + i)
        self.assertAlmostEqual(ramp.accumulated, 25.0)
        self.assertAlmostEqual(ramp.bonus(), 25.0)

    def test_only_configured_events_count(self) -> None:
        ramp = self.make()
        ramp.on_event(LowHealth(ratio=0.1), now=100.0)
        self.assertAlmostEqual(ramp.accumulated, 0.0)
        ramp.on_event(LimbInjury(slot=1), now=101.0)
        self.assertAlmostEqual(ramp.accumulated, 2.0)

    def test_hp_component(self) -> None:
        ramp = self.make(hp_missing_pct=20.0)
        ramp.note_hp(0.25)
        self.assertAlmostEqual(ramp.hp_component, 15.0)
        self.assertAlmostEqual(ramp.bonus(), 15.0)
        ramp.note_hp(None)
        self.assertAlmostEqual(ramp.hp_component, 0.0)
        ramp.note_hp("不是数字")                      # type: ignore[arg-type]
        self.assertAlmostEqual(ramp.hp_component, 0.0)
        ramp.note_hp(1.5)                             # 夹到 1.0
        self.assertAlmostEqual(ramp.hp_component, 0.0)

    def test_bonus_is_capped_by_ceiling(self) -> None:
        ramp = self.make(per_event=10.0, ceiling_pct=25.0, hp_missing_pct=20.0)
        ramp.note_hp(0.0)                             # 血量项 = 20
        ramp.on_event(Damage(severity=1.0), now=100.0)  # +10 → 30
        self.assertAlmostEqual(ramp.bonus(), 25.0)

    def test_death_resets_accumulation(self) -> None:
        ramp = self.make()
        ramp.on_event(Damage(severity=1.0), now=100.0)
        ramp.on_event(Damage(severity=1.0), now=101.0)
        ramp.on_event(Death(), now=102.0)
        self.assertAlmostEqual(ramp.accumulated, 0.0)
        self.assertEqual(ramp.resets, 1)
        ramp.on_event(Revive(), now=103.0)
        self.assertEqual(ramp.resets, 1)              # 已经是 0，不重复计数

    def test_death_counts_when_reset_disabled(self) -> None:
        ramp = self.make(reset_on_death=False, apply_to=["damage", "death"])
        ramp.on_event(Death(), now=100.0)
        self.assertAlmostEqual(ramp.accumulated, 2.0)
        self.assertEqual(ramp.resets, 0)

    def test_decay_after_idle(self) -> None:
        ramp = self.make(per_event=2.0, decay_after_s=4.0, decay_per_s=1.5)
        for i in range(3):
            ramp.on_event(Damage(severity=1.0), now=100.0 + i)   # 最后事件 102
        ramp.tick(now=105.0)                          # 才 3s：还在 decay_after_s 之内
        self.assertAlmostEqual(ramp.accumulated, 6.0)
        ramp.tick(now=106.0)                          # dt=1s → -1.5
        self.assertAlmostEqual(ramp.accumulated, 4.5)
        ramp.tick(now=110.0)                          # dt=4s → -6，夹到 0
        self.assertAlmostEqual(ramp.accumulated, 0.0)

    def test_no_decay_before_any_event(self) -> None:
        ramp = self.make()
        ramp.tick(now=1000.0)
        self.assertAlmostEqual(ramp.accumulated, 0.0)

    def test_reset(self) -> None:
        ramp = self.make()
        ramp.on_event(Damage(severity=1.0), now=100.0)
        ramp.reset(now=101.0)
        self.assertAlmostEqual(ramp.accumulated, 0.0)

    def test_describe_shows_parts(self) -> None:
        ramp = self.make(per_event=3.0, hp_missing_pct=20.0)
        ramp.note_hp(0.5)                             # 血量项 = 10
        ramp.on_event(Damage(severity=1.0), now=100.0)
        text = ramp.describe()
        self.assertIn("累积 3.0%", text)
        self.assertIn("血量 10.0%", text)
        self.assertIn("+13.0%", text)


if __name__ == "__main__":
    unittest.main()

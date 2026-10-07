"""检测层测试：合成 HUD 画面 -> 事件（不需要游戏、不需要硬件）。"""

from __future__ import annotations

import unittest

import numpy as np

from hd2coyote.config import AppConfig, DetectConfig
from hd2coyote.detectors import (DeathTracker, Detector, HealthTracker, InjuryTracker,
                                 bar_fill_ratio, injury_scores)
from hd2coyote.events import Damage, Death, LimbInjury, LowHealth, Revive
from hd2coyote.hud import auto_locate_hp_bar, default_injury_zone, default_layout
from hd2coyote.simulate import synthetic_frame

SIZE = (1920, 1080)


def make_cfg() -> AppConfig:
    cfg = AppConfig()
    layout = default_layout(*SIZE)
    cfg.hud.hp_bar = layout.hp_bar
    cfg.hud.injury_zone = layout.injury_zone
    cfg.hud.injury_slots = 3
    return cfg


class TestPixelLayer(unittest.TestCase):
    def setUp(self) -> None:
        self.layout = default_layout(*SIZE)

    def test_fill_ratio_half(self) -> None:
        frame = synthetic_frame(self.layout, hp=0.5, size=SIZE)
        ratio = bar_fill_ratio(frame, self.layout.hp_bar)
        self.assertIsNotNone(ratio)
        self.assertAlmostEqual(ratio, 0.5, delta=0.06)

    def test_fill_ratio_empty_and_dead(self) -> None:
        self.assertLess(bar_fill_ratio(synthetic_frame(self.layout, hp=0.0, size=SIZE),
                                       self.layout.hp_bar), 0.05)
        self.assertLess(bar_fill_ratio(synthetic_frame(self.layout, hp=1.0, dead=True, size=SIZE),
                                       self.layout.hp_bar), 0.05)

    def test_injury_slots(self) -> None:
        frame = synthetic_frame(self.layout, hp=0.8, injured=(True, False, False), size=SIZE)
        scores, bleeding = injury_scores(frame, self.layout.injury_zone, 3)
        self.assertGreater(scores[0], 0.05)
        self.assertLess(scores[1], 0.01)
        self.assertGreater(bleeding, 0.0)

    def test_no_injury(self) -> None:
        frame = synthetic_frame(self.layout, hp=1.0, size=SIZE)
        scores, bleeding = injury_scores(frame, self.layout.injury_zone, 3)
        self.assertTrue(all(s < 0.005 for s in scores))
        self.assertLess(bleeding, 0.005)

    def test_auto_locate_finds_bar(self) -> None:
        frame = synthetic_frame(self.layout, hp=0.7, size=SIZE)
        found = auto_locate_hp_bar(frame)
        self.assertIsNotNone(found)
        assert found is not None and self.layout.hp_bar is not None
        # 找到的条应当与真实血条在横向上基本重合
        self.assertLess(abs(found.x - self.layout.hp_bar.x), 12)
        self.assertGreater(found.w, self.layout.hp_bar.w * 0.6)

    def test_default_injury_zone_is_left_of_bar(self) -> None:
        assert self.layout.hp_bar is not None
        zone = default_injury_zone(self.layout.hp_bar, *SIZE)
        self.assertLess(zone.x + zone.w, self.layout.hp_bar.x + 4)


class TestTrackers(unittest.TestCase):
    def test_damage_accumulates_small_hits(self) -> None:
        tracker = HealthTracker(DetectConfig(damage_min_pct=4.0))
        self.assertEqual(tracker.update(1.00, 0.0), [])
        self.assertEqual(tracker.update(0.98, 0.1), [])
        events = tracker.update(0.94, 0.2)  # 累计 6%
        self.assertEqual(len(events), 1)
        self.assertIsInstance(events[0], Damage)
        self.assertAlmostEqual(events[0].severity, 6.0, delta=0.01)

    def test_heal_resets_pending(self) -> None:
        tracker = HealthTracker(DetectConfig(damage_min_pct=4.0))
        tracker.update(1.00, 0.0)
        tracker.update(0.97, 0.1)
        tracker.update(1.00, 0.2)  # 回血 -> 丢弃累计
        self.assertEqual(tracker.update(0.99, 0.3), [])

    def test_low_health_hysteresis(self) -> None:
        from hd2coyote.events import Recovered

        tracker = HealthTracker(DetectConfig(low_health_pct=35.0, recover_pct=60.0))
        tracker.update(0.8, 0.0)
        events = tracker.update(0.3, 0.1)
        self.assertTrue(any(isinstance(e, LowHealth) for e in events))
        self.assertEqual(tracker.update(0.5, 0.2), [])  # 中间地带不重复触发
        self.assertTrue(any(isinstance(e, Recovered) for e in tracker.update(0.7, 0.3)))

    def test_injury_edge_detection(self) -> None:
        tracker = InjuryTracker(DetectConfig(injury_min_fraction=0.02, injury_clear_s=0.5), 3,
                                ["左肢", "躯干", "右肢"])
        events = tracker.update([0.5, 0.0, 0.0], 0.0, now=0.0)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].name, "左肢")
        # 图标还在，不重复触发
        self.assertEqual(tracker.update([0.5, 0.0, 0.0], 0.0, now=0.5), [])
        # 图标消失且超过防抖时间后，可以再次触发
        self.assertEqual(tracker.update([0.0, 0.0, 0.0], 0.0, now=1.0), [])
        self.assertEqual(len(tracker.update([0.5, 0.0, 0.0], 0.0, now=2.0)), 1)

    def test_death_and_revive(self) -> None:
        tracker = DeathTracker(DetectConfig(dead_hold_s=0.8))
        self.assertEqual(tracker.update(1.0, 0.0, 0.0, 0.72), [])
        self.assertEqual(tracker.update(0.0, 0.0, 0.2, 0.72), [])
        events = tracker.update(0.0, 0.0, 1.1, 0.72)
        self.assertTrue(any(isinstance(e, Death) for e in events))
        events = tracker.update(0.9, 0.0, 1.5, 0.72)
        self.assertTrue(any(isinstance(e, Revive) for e in events))

    def test_death_by_template_score(self) -> None:
        tracker = DeathTracker(DetectConfig(dead_hold_s=0.5))
        self.assertEqual(tracker.update(1.0, 0.9, 0.0, 0.72), [])
        self.assertTrue(any(isinstance(e, Death) for e in tracker.update(1.0, 0.9, 0.6, 0.72)))


class TestDetectorEndToEnd(unittest.TestCase):
    def test_full_timeline(self) -> None:
        cfg = make_cfg()
        detector = Detector(cfg)
        layout = default_layout(*SIZE)
        seen: list[str] = []

        def feed(t: float, hp: float, injured=(), dead=False) -> None:
            frame = synthetic_frame(layout, hp=hp, injured=injured, dead=dead, size=SIZE)
            for ev in detector.process(frame, t):
                seen.append(type(ev).__name__)

        feed(0.0, 1.00)
        feed(0.1, 0.90)                       # 受伤
        feed(0.3, 0.90, (True, False, False))  # 左肢损伤
        feed(2.0, 0.00, (True, False, False), dead=True)   # 阵亡
        feed(4.0, 0.00, (), dead=True)
        feed(6.0, 0.60)                       # 复活
        for _ in range(3):
            feed(6.4, 1.00)

        self.assertIn("Damage", seen)
        self.assertIn("LimbInjury", seen)
        self.assertIn("Death", seen)
        self.assertIn("Revive", seen)
        self.assertEqual(detector.sample.hp is not None, True)

    def test_uncalibrated_does_not_crash(self) -> None:
        cfg = AppConfig()  # 没有标定
        detector = Detector(cfg)
        frame = np.zeros((100, 100, 3), dtype=np.uint8)
        self.assertEqual(detector.process(frame, 0.0), [])


if __name__ == "__main__":
    unittest.main()

"""共享状态机测试：结构化状态 -> 事件，不需要图像或硬件。"""

import unittest

from hd2coyote.config import AppConfig, DetectConfig
from hd2coyote.detectors import DeathTracker, EventTrackers, HealthTracker, InjuryTracker
from hd2coyote.events import Damage, Death, LimbInjury, LowHealth, Revive


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

    def test_death_by_state_flag(self) -> None:
        tracker = DeathTracker(DetectConfig(dead_hold_s=0.5))
        self.assertEqual(tracker.update(1.0, 0.9, 0.0, 0.72), [])
        self.assertTrue(any(isinstance(e, Death) for e in tracker.update(1.0, 0.9, 0.6, 0.72)))


class TestStateEndToEnd(unittest.TestCase):
    def test_full_timeline(self) -> None:
        cfg = AppConfig()
        detector = EventTrackers(cfg)
        seen: list[str] = []

        def feed(t: float, hp: float, injured=(), dead=False) -> None:
            scores = [float(v) for v in injured] + [0.0] * (3 - len(injured))
            for ev in detector.process(hp, scores, 0.0, float(dead), t):
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

    def test_missing_state_does_not_create_events(self) -> None:
        trackers = EventTrackers(AppConfig())
        self.assertEqual(trackers.process(None, [], 0.0, 0.0, 0.0), [])


if __name__ == "__main__":
    unittest.main()

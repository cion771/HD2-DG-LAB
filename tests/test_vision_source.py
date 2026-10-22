"""External HUD vision source tests; no game process or hardware required.

无 numpy 的解释器（用于验证默认路径不加载视觉依赖）会自动跳过。
"""
import importlib.util
import time
import unittest

NUMPY_OK = importlib.util.find_spec("numpy") is not None
if NUMPY_OK:
    import numpy as np

from hd2coyote.config import AppConfig, Box, from_dict
from hd2coyote.events import Damage
from hd2coyote.sources.vision import (
    DEFAULT_TITLES,
    VisionSource,
    title_candidates,
    window_title_matches,
)
from hd2coyote.sources import available_sources, source_catalog
from hd2coyote.vision import auto_locate_hp_bar, bar_fill_ratio, is_black_frame


def frame_with_bar(ratio: float, width: int = 800, height: int = 450):
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    x, y, w, h = 300, 390, 200, 8
    frame[y:y + h, x:x + w] = 45
    fill = int(w * ratio)
    frame[y:y + h, x:x + fill] = 230
    return frame, Box(x, y, w, h)


@unittest.skipUnless(NUMPY_OK, "需要 numpy 才能跑像素层测试")
class TestVisionPixels(unittest.TestCase):
    def test_bar_ratio_and_black_frame(self):
        frame, box = frame_with_bar(0.5)
        self.assertAlmostEqual(bar_fill_ratio(frame, box), 0.5, delta=0.03)
        self.assertFalse(is_black_frame(frame))
        self.assertTrue(is_black_frame(np.zeros((20, 20, 3), dtype=np.uint8)))

    def test_auto_locate_finds_thin_centred_bar(self):
        frame, expected = frame_with_bar(0.75)
        found = auto_locate_hp_bar(frame, min_run=30)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertLess(abs(found.x - expected.x), 8)
        self.assertGreater(found.w, 140)
        self.assertLessEqual(found.h, 40)

    def test_auto_locate_rejects_thick_panel(self):
        frame = np.zeros((450, 800, 3), dtype=np.uint8)
        frame[380:440, 250:550] = 230  # 60px 高的面板，不是血条
        self.assertIsNone(auto_locate_hp_bar(frame, min_run=30))

    def test_auto_locate_returns_none_without_hud(self):
        self.assertIsNone(auto_locate_hp_bar(np.zeros((450, 800, 3), dtype=np.uint8), min_run=30))


class TestVisionSource(unittest.TestCase):
    def test_registered_as_critical_state_source(self):
        self.assertIn("vision", available_sources())
        row = {item["name"]: item for item in source_catalog()}["vision"]
        self.assertTrue(row["critical"])
        self.assertEqual(row["kind"], "state")

    @unittest.skipUnless(NUMPY_OK, "需要 numpy 才能跑事件链路测试")
    def test_published_hp_uses_shared_trackers(self):
        cfg = AppConfig()
        cfg.detect.damage_merge_window_ms = 0.0
        src = VisionSource(cfg)
        src.started = True
        t = time.monotonic()
        src._publish(1.0, Box(1, 2, 100, 8))
        self.assertEqual(src.poll(t), [])
        src._publish(0.7, Box(1, 2, 100, 8))
        events = src.poll(t + 0.1)
        self.assertTrue(any(isinstance(event, Damage) for event in events), events)
        self.assertTrue(src.alive)

    def test_stale_recovery_rebaselines_before_damage(self):
        cfg = AppConfig()
        cfg.detect.damage_merge_window_ms = 0.0
        src = VisionSource(cfg)
        src.started = True
        box = Box(1, 2, 100, 8)
        src._publish(1.0, box)
        self.assertEqual(src.poll(time.monotonic()), [])
        src._last_at = time.monotonic() - 3.0
        self.assertIsNone(src.hp)
        self.assertEqual(src.poll(time.monotonic()), [])
        src._publish(0.2, box)
        self.assertEqual(src.poll(time.monotonic()), [])
        src._publish(0.1, box)
        self.assertTrue(any(isinstance(e, Damage) for e in src.poll(time.monotonic())))

    def test_capture_loss_invalidates_immediately(self):
        cfg = AppConfig()
        cfg.detect.damage_merge_window_ms = 0.0
        src = VisionSource(cfg)
        src.started = True
        box = Box(1, 2, 100, 8)
        src._publish(1.0, box)
        src.poll(time.monotonic())
        src._invalidate("游戏不在前台")
        self.assertFalse(src.alive)
        self.assertIsNone(src.hp)
        self.assertEqual(src.poll(time.monotonic()), [])
        src._publish(0.2, box)
        self.assertEqual(src.poll(time.monotonic()), [])

    def test_stale_reading_is_not_alive(self):
        cfg = AppConfig()
        # alive 有 0.5s 下限，避免读数抖动导致静音反复
        cfg.vision.stale_timeout_s = 0.2
        src = VisionSource(cfg)
        src.started = True
        src._publish(0.5, Box(1, 2, 100, 8))
        self.assertTrue(src.alive)
        time.sleep(0.65)
        self.assertFalse(src.alive)

    def test_title_candidates_cover_localised_titles(self):
        cands = title_candidates("HELLDIVERS|绝地潜兵")
        self.assertIn("HELLDIVERS", cands)
        self.assertIn("绝地潜兵", cands)
        self.assertEqual(len(cands), len({c.upper() for c in cands}))
        for item in DEFAULT_TITLES:
            self.assertIn(item, cands)

    def test_game_window_title_rejects_browser_pages(self):
        self.assertTrue(window_title_matches("HELLDIVERS™ 2", "HELLDIVERS"))
        self.assertTrue(window_title_matches("绝地潜兵 2", "HELLDIVERS"))
        self.assertFalse(window_title_matches("HELLDIVERS 2 Wiki - Edge", "HELLDIVERS"))
        self.assertFalse(window_title_matches("如何打败 HELLDIVERS 2 - 浏览器", "HELLDIVERS"))

    @unittest.skipUnless(NUMPY_OK, "需要 numpy 才能实例化视觉源")
    def test_legacy_vision_config_is_migrated(self):
        cfg = from_dict(AppConfig, {
            "source": "vision",
            "capture": {"monitor": 2, "fps": 12, "backend": "mss"},
            "hud": {"hp_bar": {"x": 10, "y": 20, "w": 200, "h": 8}},
        })
        self.assertEqual(cfg.source, "hook")
        self.assertEqual(cfg.sources.enabled, ["vision"])
        self.assertEqual(cfg.vision.monitor, 2)
        self.assertEqual(cfg.vision.fps, 12)
        self.assertEqual(cfg.vision.hp_bar, Box(10, 20, 200, 8))


if __name__ == "__main__":
    unittest.main()

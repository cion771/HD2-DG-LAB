"""配置读写测试：保存 -> 读取 -> 嵌套 dataclass 正确还原。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hd2coyote.config import (AppConfig, DetectConfig, DeviceConfig,
                              HudConfig, RuleConfig, SafetyConfig, from_dict)


class TestConfig(unittest.TestCase):
    def test_default_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            cfg = AppConfig()
            cfg.hud.injury_slot_names = ["left", "body", "right"]
            cfg.rules["damage"].base_pct = 21.5
            cfg.device.port = 10086
            cfg.save(path)

            loaded = AppConfig.load(path)
            self.assertIsInstance(loaded.device, DeviceConfig)
            self.assertIsInstance(loaded.hud, HudConfig)
            self.assertIsInstance(loaded.detect, DetectConfig)
            self.assertIsInstance(loaded.safety, SafetyConfig)
            self.assertIsInstance(loaded.rules["damage"], RuleConfig)
            self.assertEqual(loaded.device.port, 10086)
            self.assertEqual(loaded.hud.injury_slot_names, ["left", "body", "right"])
            self.assertAlmostEqual(loaded.rules["damage"].base_pct, 21.5)
            self.assertEqual(loaded.rules["death"].wave, "death")

    def test_load_creates_file_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "config.json"
            cfg = AppConfig.load(path)
            self.assertTrue(path.exists())
            self.assertEqual(cfg.device.kind, "socket")

    def test_unknown_and_missing_keys(self) -> None:
        data = {
            "device": {"kind": "mock", "port": 1234, "未来字段": 1},
            "safety": {"max_absolute": 12},
        }
        cfg = from_dict(AppConfig, data)
        self.assertEqual(cfg.device.kind, "mock")
        self.assertEqual(cfg.device.port, 1234)
        self.assertEqual(cfg.safety.max_absolute, 12)
        # 缺失字段回落到默认值
        self.assertEqual(cfg.safety.max_pct, AppConfig().safety.max_pct)
        self.assertIn("damage", cfg.rules)

    def test_legacy_vision_config_migrates_without_capture(self) -> None:
        cfg = from_dict(AppConfig, {
            "source": "vision", "capture": {"backend": "mss"},
            "hud": {"hp_bar": {"x": 1, "y": 2, "w": 3, "h": 4}, "death_template": "private.png"},
            "detect": {"death_template_threshold": 0.9, "capture_error_limit": 30},
            "sources": {"enabled": ["http"]}, "device": {"kind": "mock"},
        })
        self.assertEqual(cfg.source, "hook")
        self.assertEqual(cfg.sources.enabled, ["http"])
        self.assertEqual(cfg.detect.death_state_threshold, 0.9)
        self.assertNotIn("capture", cfg.to_dict())
        self.assertNotIn("hp_bar", cfg.to_dict()["hud"])
        self.assertNotIn("death_template", cfg.to_dict()["hud"])
        self.assertNotIn("death_template_threshold", cfg.to_dict()["detect"])
        self.assertNotIn("capture_error_limit", cfg.to_dict()["detect"])

    def test_new_threshold_wins_over_legacy_alias(self) -> None:
        cfg = from_dict(AppConfig, {"detect": {"death_template_threshold": 0.9, "death_state_threshold": 0.8}})
        self.assertEqual(cfg.detect.death_state_threshold, 0.8)


if __name__ == "__main__":
    unittest.main()

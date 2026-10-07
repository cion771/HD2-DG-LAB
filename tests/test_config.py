"""配置读写测试：保存 -> 读取 -> 嵌套 dataclass 正确还原。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hd2coyote.config import (AppConfig, Box, CaptureConfig, DetectConfig, DeviceConfig,
                              HudConfig, RuleConfig, SafetyConfig, from_dict)


class TestConfig(unittest.TestCase):
    def test_default_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            cfg = AppConfig()
            cfg.hud.hp_bar = Box(100, 200, 300, 12)
            cfg.rules["damage"].base_pct = 21.5
            cfg.device.port = 10086
            cfg.save(path)

            loaded = AppConfig.load(path)
            self.assertIsInstance(loaded.device, DeviceConfig)
            self.assertIsInstance(loaded.capture, CaptureConfig)
            self.assertIsInstance(loaded.hud, HudConfig)
            self.assertIsInstance(loaded.detect, DetectConfig)
            self.assertIsInstance(loaded.safety, SafetyConfig)
            self.assertIsInstance(loaded.rules["damage"], RuleConfig)
            self.assertEqual(loaded.device.port, 10086)
            self.assertEqual(loaded.hud.hp_bar, Box(100, 200, 300, 12))
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

    def test_null_boxes(self) -> None:
        cfg = from_dict(AppConfig, json.loads(json.dumps(AppConfig().to_dict())))
        self.assertIsNone(cfg.hud.hp_bar)
        self.assertIsNone(cfg.capture.region)


if __name__ == "__main__":
    unittest.main()

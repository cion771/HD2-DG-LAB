"""事件源：注册表 + 游戏桥包装 + 手动注入的 JSON 解析与状态机。

手动注入和网页那一层共用同一套解析与阈值，所以「注入一个事件」和真实数据
走的是同一条判定路径。
"""

from __future__ import annotations

import logging
import time
import unittest

from hd2coyote.config import AppConfig
from hd2coyote.events import Damage, Death, LimbInjury, LowHealth, Revive
from hd2coyote.sources import (GameBridgeSource, available_sources, build_sources,
                               create_source, source_catalog)
from hd2coyote.sources.manual import ManualEventParser, hp_ratio, parse_event

LOG = logging.getLogger("hd2coyote.tests.sources")


class TestRegistry(unittest.TestCase):
    def test_catalog_lists_builtin_sources(self) -> None:
        names = available_sources()
        self.assertIn("game_bridge", names)
        self.assertIn("vision", names)
        self.assertNotIn("http", names)                        # 0.7.0 移除了 HTTP 事件源
        rows = {row["name"]: row for row in source_catalog()}
        self.assertTrue(rows["game_bridge"]["critical"])       # 游戏桥断了 = 状态不可信
        self.assertTrue(rows["game_bridge"]["label"])
        self.assertNotIn("<", rows["game_bridge"]["hint"])     # 尖括号会被页面当 HTML 吃掉

    def test_build_sources_keeps_configured_order_and_dedupes(self) -> None:
        cfg = AppConfig()
        cfg.sources.enabled = ["vision", "game_bridge", "vision"]
        sources = build_sources(cfg, LOG)
        self.assertEqual([src.name for src in sources], ["vision", "game_bridge"])

    def test_build_sources_ignores_unknown_names(self) -> None:
        cfg = AppConfig()
        cfg.sources.enabled = ["game_bridge", "脑电波"]
        self.assertEqual([src.name for src in build_sources(cfg, LOG)], ["game_bridge"])

    def test_create_source_unknown_raises(self) -> None:
        with self.assertRaises(KeyError):
            create_source("nope", AppConfig())

    def test_game_bridge_wraps_the_lua_hook(self) -> None:
        src = GameBridgeSource(AppConfig(), LOG)
        self.assertTrue(src.critical)
        self.assertIsNotNone(src.hook)
        self.assertFalse(src.alive)                            # 还没启动
        self.assertIn("桥", src.label)
        self.assertIn("game_bridge", src.status().as_dict()["name"])

    def test_source_status_is_jsonable(self) -> None:
        raw = create_source("vision", AppConfig(), LOG).status().as_dict()
        for key in ("name", "label", "kind", "started", "alive", "critical",
                    "detail", "events", "error"):
            self.assertIn(key, raw)


class TestParseEvent(unittest.TestCase):
    def test_event_kinds(self) -> None:
        self.assertIsInstance(parse_event({"ev": "damage", "severity": 12.5}), Damage)
        self.assertIsInstance(parse_event({"ev": "hit", "damage": 3}), Damage)
        self.assertIsInstance(parse_event({"ev": "受伤", "pct": 4}), Damage)
        self.assertIsInstance(parse_event({"ev": "limb", "slot": 2, "bleeding": True}), LimbInjury)
        self.assertIsInstance(parse_event({"ev": "limb_injury"}), LimbInjury)
        self.assertIsInstance(parse_event({"ev": "death"}), Death)
        self.assertIsInstance(parse_event({"ev": "阵亡"}), Death)
        self.assertIsInstance(parse_event({"ev": "revive"}), Revive)
        self.assertIsInstance(parse_event({"ev": "low_health", "ratio": 0.2}), LowHealth)

    def test_unknown_returns_none(self) -> None:
        self.assertIsNone(parse_event({"ev": "喝茶"}))
        self.assertIsNone(parse_event({}))
        self.assertIsNone(parse_event(None))                   # type: ignore[arg-type]

    def test_values_are_clamped(self) -> None:
        damage = parse_event({"ev": "damage", "severity": -5})
        assert isinstance(damage, Damage)
        self.assertEqual(damage.severity, 0.0)
        low = parse_event({"ev": "low_health", "ratio": 9})
        assert isinstance(low, LowHealth)
        self.assertEqual(low.ratio, 1.0)

    def test_limb_slot_and_bleeding(self) -> None:
        limb = parse_event({"ev": "limb", "slot": 1, "bleeding": "yes", "name": "左腿"})
        assert isinstance(limb, LimbInjury)
        self.assertEqual(limb.slot, 1)
        self.assertTrue(limb.bleeding)
        self.assertEqual(limb.name, "左腿")


class TestHpRatio(unittest.TestCase):
    def test_forms(self) -> None:
        self.assertAlmostEqual(hp_ratio({"hp": 0.4}), 0.4)           # 比例
        self.assertAlmostEqual(hp_ratio({"hp": 40}), 0.4)            # 百分制
        self.assertAlmostEqual(hp_ratio({"hp": 40, "hp_max": 100}), 0.4)
        self.assertAlmostEqual(hp_ratio({"hp": 40, "hp_max": 80}), 0.5)
        self.assertAlmostEqual(hp_ratio({"hp": 200}), 1.0)           # 夹住
        self.assertIsNone(hp_ratio({}))
        self.assertIsNone(hp_ratio({"hp": "坏"}))



class TestManualStateMachine(unittest.TestCase):
    """手动注入的状态包走的是和游戏内桥同一套 EventTrackers，判定逻辑只有一份。"""

    def make(self, **detect) -> tuple[AppConfig, ManualEventParser]:
        cfg = AppConfig()
        for key, value in detect.items():
            setattr(cfg.detect, key, value)
        return cfg, ManualEventParser(cfg, LOG)

    def test_damage_from_hp_drop(self) -> None:
        _, src = self.make(damage_merge_window_ms=0.0)
        t = time.monotonic()
        src.ingest({"hp": 100, "hp_max": 100}, now=t)
        events = src.ingest({"hp": 90, "hp_max": 100}, now=t + 0.1)
        self.assertIn("damage", [e.kind for e in events])

    def test_limb_injury_from_limb_flags(self) -> None:
        _, src = self.make()
        t = time.monotonic()
        src.ingest({"hp": 90, "hp_max": 100, "limbs": [0, 0, 0]}, now=t)
        events = src.ingest({"hp": 90, "hp_max": 100, "limbs": [1, 0, 0], "bleeding": 1},
                            now=t + 0.1)
        kinds = [e.kind for e in events]
        self.assertIn("limb_injury", kinds)

    def test_death_and_revive_from_dead_flag(self) -> None:
        _, src = self.make(dead_hold_s=0.15)
        t = time.monotonic()
        src.ingest({"hp": 100, "hp_max": 100}, now=t)
        src.ingest({"hp": 0, "hp_max": 100, "dead": 1}, now=t + 0.1)     # 还没到 hold 时间
        events = src.ingest({"hp": 0, "hp_max": 100, "dead": 1}, now=t + 0.4)
        self.assertIn("death", [e.kind for e in events])
        events = src.ingest({"hp": 60, "hp_max": 100, "dead": 0}, now=t + 0.6)
        self.assertIn("revive", [e.kind for e in events])

    def test_limbs_as_dict_is_accepted(self) -> None:
        _, src = self.make()
        t = time.monotonic()
        src.ingest({"hp": 90, "hp_max": 100, "limbs": {"0": 0, "1": 0}}, now=t)
        events = src.ingest({"hp": 90, "hp_max": 100, "limbs": {"0": 1, "1": 0}}, now=t + 0.1)
        self.assertIn("limb_injury", [e.kind for e in events])


if __name__ == "__main__":
    unittest.main()

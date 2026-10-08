"""事件源：注册表 + 游戏桥包装 + HTTP 源（解析 / 状态机 / 限速 / 令牌）。

HTTP 源把一个端口当入口，任何外部程序都能上报，所以这一层必须自己扛住
「不认识的东西」「太多太快」「令牌不对」——这些都在这里钉死。
"""

from __future__ import annotations

import logging
import time
import unittest

from hd2coyote.config import AppConfig
from hd2coyote.events import Damage, Death, LimbInjury, LowHealth, Revive
from hd2coyote.sources import (GameBridgeSource, HttpEventSource, available_sources,
                               build_sources, create_source, source_catalog)
from hd2coyote.sources.http import MAX_QUEUE, parse_event, _hp_ratio

LOG = logging.getLogger("hd2coyote.tests.sources")


class TestRegistry(unittest.TestCase):
    def test_catalog_lists_builtin_sources(self) -> None:
        names = available_sources()
        self.assertIn("game_bridge", names)
        self.assertIn("http", names)
        rows = {row["name"]: row for row in source_catalog()}
        self.assertTrue(rows["game_bridge"]["critical"])       # 游戏桥断了 = 状态不可信
        self.assertFalse(rows["http"]["critical"])             # 没人上报时不算断流
        self.assertTrue(rows["game_bridge"]["label"])
        self.assertNotIn("<", rows["http"]["hint"])            # 尖括号会被页面当 HTML 吃掉

    def test_build_sources_keeps_configured_order_and_dedupes(self) -> None:
        cfg = AppConfig()
        cfg.sources.enabled = ["http", "game_bridge", "http"]
        sources = build_sources(cfg, LOG)
        self.assertEqual([src.name for src in sources], ["http", "game_bridge"])

    def test_build_sources_ignores_unknown_names(self) -> None:
        cfg = AppConfig()
        cfg.sources.enabled = ["http", "脑电波"]
        self.assertEqual([src.name for src in build_sources(cfg, LOG)], ["http"])

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
        raw = create_source("http", AppConfig(), LOG).status().as_dict()
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
        self.assertAlmostEqual(_hp_ratio({"hp": 0.4}), 0.4)           # 比例
        self.assertAlmostEqual(_hp_ratio({"hp": 40}), 0.4)            # 百分制
        self.assertAlmostEqual(_hp_ratio({"hp": 40, "hp_max": 100}), 0.4)
        self.assertAlmostEqual(_hp_ratio({"hp": 40, "hp_max": 80}), 0.5)
        self.assertAlmostEqual(_hp_ratio({"hp": 200}), 1.0)           # 夹住
        self.assertIsNone(_hp_ratio({}))
        self.assertIsNone(_hp_ratio({"hp": "坏"}))


class TestHttpSource(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = AppConfig()
        self.src = HttpEventSource(self.cfg, LOG)

    def test_ingest_unknown_event_counts_bad(self) -> None:
        self.assertEqual(self.src.ingest({"ev": "喝茶"}), [])
        self.assertEqual(self.src.packets, 1)
        self.assertEqual(self.src.bad, 1)

    def test_ingest_non_dict(self) -> None:
        self.assertEqual(self.src.ingest("不是字典"), [])          # type: ignore[arg-type]
        self.assertEqual(self.src.bad, 1)

    def test_ingest_known_event(self) -> None:
        events = self.src.ingest({"ev": "limb", "slot": 0, "bleeding": True})
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "limb_injury")
        self.assertEqual(self.src.bad, 0)

    def test_push_poll_and_events_counter(self) -> None:
        self.src.push([Damage(severity=5.0), Damage(severity=6.0)])
        self.assertEqual(self.src.events, 2)
        self.assertEqual(len(self.src.poll(0.0)), 2)
        self.assertEqual(self.src.poll(0.0), [])
        self.src.push([])                                       # 空列表什么也不做
        self.assertEqual(self.src.events, 2)

    def test_queue_cap_drops_oldest(self) -> None:
        self.src.push([Damage(severity=1.0) for _ in range(MAX_QUEUE + 5)])
        self.assertEqual(self.src.dropped, 5)
        self.assertEqual(len(self.src.poll(0.0)), MAX_QUEUE)

    def test_token(self) -> None:
        self.assertTrue(self.src.token_ok(None))                # 没设令牌就都放行
        self.cfg.sources.http_token = "s3cret"
        self.assertFalse(self.src.token_ok("bad"))
        self.assertFalse(self.src.token_ok(None))
        self.assertTrue(self.src.token_ok("s3cret"))
        self.assertTrue(self.src.token_ok(None, "s3cret"))      # ?token= 写法

    def test_rate_limit_sliding_window(self) -> None:
        self.cfg.sources.http_max_per_s = 3
        self.assertTrue(self.src.rate_ok(now=100.0))
        self.assertTrue(self.src.rate_ok(now=100.1))
        self.assertTrue(self.src.rate_ok(now=100.2))
        self.assertFalse(self.src.rate_ok(now=100.3))
        self.assertEqual(self.src.dropped, 1)
        self.assertTrue(self.src.rate_ok(now=101.5))            # 窗口滑过去了

    def test_stats_shape(self) -> None:
        stats = self.src.stats()
        for key in ("packets", "events", "bad", "dropped", "queued", "state_seen",
                    "state_age_s", "listening", "token_required", "max_per_s"):
            self.assertIn(key, stats)
        self.assertEqual(stats["listening"],
                         f"{self.cfg.sources.http_host}:{self.cfg.sources.http_port}")

    def test_describe_before_start(self) -> None:
        self.assertEqual(self.src.describe(), "未启动")

    def test_apply_config_rebuilds_trackers(self) -> None:
        old = self.src.trackers
        cfg = AppConfig()
        self.src.apply_config(cfg)
        self.assertIs(self.src.cfg, cfg)
        self.assertIsNot(self.src.trackers, old)


class TestHttpStateMachine(unittest.TestCase):
    """状态包走的是和游戏内桥同一套 EventTrackers，所以判定逻辑只有一份。"""

    def make(self, **detect) -> tuple[AppConfig, HttpEventSource]:
        cfg = AppConfig()
        for key, value in detect.items():
            setattr(cfg.detect, key, value)
        src = HttpEventSource(cfg, LOG)
        src.started = True          # 这里只测状态判定，不真的占端口
        return cfg, src

    def test_state_packet_marks_seen_and_critical(self) -> None:
        _, src = self.make()
        self.assertFalse(src.critical_now())
        src.ingest({"ev": "state", "hp": 100, "hp_max": 100}, now=time.monotonic())
        self.assertTrue(src.state_seen)
        self.assertTrue(src.critical_now())                     # 配了超时才算状态源
        self.assertTrue(src.alive)
        self.assertEqual(src.last_state["hp"], 100)

    def test_state_times_out(self) -> None:
        cfg, src = self.make()
        now = time.monotonic()
        src.ingest({"hp": 100, "hp_max": 100}, now=now)
        self.assertTrue(src.alive)
        src.last_state_at = now - (cfg.sources.http_timeout_s + 1.0)
        self.assertFalse(src.alive)

    def test_no_timeout_configured_never_goes_stale(self) -> None:
        cfg, src = self.make()
        cfg.sources.http_timeout_s = 0.0
        src.ingest({"hp": 100, "hp_max": 100}, now=time.monotonic())
        self.assertFalse(src.critical_now())
        self.assertTrue(src.alive)

    def test_catalog_reports_static_criticality(self) -> None:
        rows = {row["name"]: row for row in source_catalog()}
        self.assertFalse(rows["http"]["critical"])              # 静态：事件型
        self.assertTrue(rows["game_bridge"]["critical"])

    def test_damage_from_hp_drop(self) -> None:
        _, src = self.make()
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

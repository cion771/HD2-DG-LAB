"""波形库：命名波形（自定义单元 / 内置预设）+ 与 DG-Lab-Punishment 的 pulse_data 互通。

参考项目（Apache-2.0）的 conf/csgo_dglab/default.json 用「名字 → 16 进制单元数组」
存波形，这里必须能直接把那种 JSON 粘进来，也能原样导出。
"""

from __future__ import annotations

import unittest

from hd2coyote import wave_lib, waves
from hd2coyote.config import AppConfig

DEATH_UNIT = "1414141464646464"
HURT_UNIT = "0A0A0A0A64646464"


class TestUnits(unittest.TestCase):
    def test_normalize_unit(self) -> None:
        self.assertEqual(wave_lib.normalize_unit(DEATH_UNIT), DEATH_UNIT)
        self.assertEqual(wave_lib.normalize_unit("14 14 14 14 64 64 64 64"), DEATH_UNIT)
        self.assertEqual(wave_lib.normalize_unit("0x0a0a0a0a64646464"), HURT_UNIT)
        self.assertEqual(wave_lib.normalize_unit("1414-1414:6464,6464"), DEATH_UNIT)
        self.assertEqual(wave_lib.normalize_unit(None), "")
        self.assertEqual(wave_lib.normalize_unit("141414146464646"), "")      # 少一位
        self.assertEqual(wave_lib.normalize_unit("ZZZZZZZZZZZZZZZZ"), "")     # 不是十六进制

    def test_normalize_units_and_invalid(self) -> None:
        self.assertEqual(wave_lib.normalize_units([DEATH_UNIT, "坏"]), [DEATH_UNIT])
        self.assertEqual(wave_lib.invalid_units([DEATH_UNIT, "坏"]), ["坏"])

    def test_unit_roundtrip(self) -> None:
        unit = wave_lib.unit_from_pair(30, 60)
        self.assertEqual(unit, waves.unit(30, 60))
        pairs = wave_lib.parse_unit(unit)
        self.assertEqual(len(pairs), 4)
        self.assertEqual(pairs[0], (30, 60))
        self.assertEqual(wave_lib.parse_unit("nope"), [])

    def test_illegal_intensity_unit_is_kept_but_flagged(self) -> None:
        # 强度 >100（64）是合法的 16 进制单元，编码层不拦，规范层才作废
        entry = wave_lib.set_entry(AppConfig(), "超范围", units=["6464646464646464"])
        self.assertEqual(entry.units, ["6464646464646464"])


class TestLibrary(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = AppConfig()

    def test_set_and_resolve_units(self) -> None:
        wave_lib.set_entry(self.cfg, "死亡长按", units=[DEATH_UNIT], default_ms=800)
        self.assertTrue(wave_lib.in_library(self.cfg, "死亡长按"))
        units = wave_lib.resolve(self.cfg, "死亡长按", 300.0)
        self.assertEqual(len(units), waves.units_for(300.0))   # 一个单元 = 一个 100ms 格子
        self.assertEqual(set(units), {DEATH_UNIT})

    def test_units_cycle_when_too_short(self) -> None:
        wave_lib.set_entry(self.cfg, "两拍", units=[DEATH_UNIT, HURT_UNIT])
        units = wave_lib.resolve(self.cfg, "两拍", 500.0)
        self.assertGreaterEqual(len(units), 4)
        self.assertEqual(units[:4], [DEATH_UNIT, HURT_UNIT, DEATH_UNIT, HURT_UNIT])

    def test_resolve_unknown_returns_none(self) -> None:
        self.assertIsNone(wave_lib.resolve(self.cfg, "没这个", 100.0))

    def test_preset_entry(self) -> None:
        wave_lib.set_entry(self.cfg, "我的死亡", preset="death", freq=40.0)
        units = wave_lib.resolve(self.cfg, "我的死亡", 200.0)
        self.assertTrue(units)
        self.assertTrue(all(len(u) == wave_lib.UNIT_CHARS for u in units))

    def test_empty_entry_falls_back_to_pinch(self) -> None:
        entry = wave_lib.set_entry(self.cfg, "空")
        self.assertEqual(entry.preset, "pinch")

    def test_preset_overrides_units_when_both_given(self) -> None:
        entry = wave_lib.set_entry(self.cfg, "混合", units=[DEATH_UNIT], preset="death")
        self.assertEqual(entry.units, [])
        self.assertEqual(entry.preset, "death")

    def test_set_entry_validation(self) -> None:
        with self.assertRaises(ValueError):
            wave_lib.set_entry(self.cfg, "", units=[DEATH_UNIT])
        with self.assertRaises(ValueError):
            wave_lib.set_entry(self.cfg, "x" * 65, units=[DEATH_UNIT])
        with self.assertRaises(ValueError):
            wave_lib.set_entry(self.cfg, "坏的", units=["12"])
        with self.assertRaises(ValueError):
            wave_lib.set_entry(self.cfg, "坏的预设", preset="不存在的预设")

    def test_clamps_peak_and_duration(self) -> None:
        entry = wave_lib.set_entry(self.cfg, "夹一下", units=[DEATH_UNIT],
                                   peak=999, default_ms=-5, freq=0)
        self.assertEqual(entry.peak, 100.0)
        self.assertEqual(entry.default_ms, 0.0)
        self.assertIsNone(entry.freq)

    def test_remove_entry(self) -> None:
        wave_lib.set_entry(self.cfg, "临时", units=[DEATH_UNIT])
        self.assertTrue(wave_lib.remove_entry(self.cfg, "临时"))
        self.assertFalse(wave_lib.remove_entry(self.cfg, "临时"))
        self.assertNotIn("临时", wave_lib.names(self.cfg))

    def test_catalog(self) -> None:
        wave_lib.set_entry(self.cfg, "自定义", units=[DEATH_UNIT], note="测试")
        wave_lib.set_entry(self.cfg, "预设", preset="heartbeat")
        rows = {row["name"]: row for row in wave_lib.catalog(self.cfg)}
        self.assertEqual(rows["自定义"]["kind"], "units")
        self.assertEqual(rows["自定义"]["unit_count"], 1)
        self.assertEqual(rows["自定义"]["note"], "测试")
        self.assertEqual(rows["预设"]["kind"], "preset")
        self.assertEqual(rows["预设"]["preset"], "heartbeat")
        self.assertEqual(rows["自定义"]["invalid"], [])


class TestImportExport(unittest.TestCase):
    REFERENCE = {
        "pulse_data": {
            "死亡": [DEATH_UNIT],
            "受伤": [HURT_UNIT, DEATH_UNIT],
            "坏东西": "这不是单元",
        },
        "punish_time": {"死亡": 8, "受伤": 2},
        "SetZero": True,
    }

    def test_import_reference_format(self) -> None:
        cfg = AppConfig()
        result = wave_lib.import_pulse_data(cfg, self.REFERENCE)
        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["skipped"], ["坏东西"])
        self.assertEqual(cfg.waves.entries["死亡"].default_ms, 8000.0)
        self.assertEqual(len(cfg.waves.entries["受伤"].units), 2)
        self.assertEqual(cfg.waves.entries["受伤"].default_ms, 2000.0)

    def test_import_bare_name_to_units(self) -> None:
        cfg = AppConfig()
        result = wave_lib.import_pulse_data(cfg, {"我的死亡": [DEATH_UNIT]})
        self.assertEqual(result["count"], 1)
        self.assertTrue(wave_lib.in_library(cfg, "我的死亡"))

    def test_import_rejects_non_object(self) -> None:
        with self.assertRaises(ValueError):
            wave_lib.import_pulse_data(AppConfig(), [1, 2, 3])

    def test_export_roundtrip(self) -> None:
        cfg = AppConfig()
        wave_lib.import_pulse_data(cfg, self.REFERENCE)
        wave_lib.set_entry(cfg, "预设波形", preset="death", freq=35.0, default_ms=500)
        export = wave_lib.export_pulse_data(cfg)
        self.assertEqual(export["format"], wave_lib.EXPORT_FORMAT)
        self.assertEqual(sorted(export["pulse_data"]), ["受伤", "死亡"])
        self.assertIn("预设波形", export["presets"])
        self.assertEqual(export["punish_time"]["死亡"], 8.0)

        again = AppConfig()
        wave_lib.import_pulse_data(again, export)
        self.assertEqual(sorted(again.waves.entries), ["受伤", "死亡", "预设波形"])
        self.assertEqual(again.waves.entries["死亡"].default_ms, 8000.0)
        self.assertEqual(again.waves.entries["预设波形"].preset, "death")

    def test_replace_clears_library(self) -> None:
        cfg = AppConfig()
        wave_lib.set_entry(cfg, "旧的", units=[DEATH_UNIT])
        wave_lib.import_pulse_data(cfg, {"pulse_data": {"新的": [HURT_UNIT]}}, replace=True)
        self.assertEqual(wave_lib.names(cfg), ["新的"])

    def test_from_text_roundtrip_and_bad_json(self) -> None:
        cfg = AppConfig()
        wave_lib.set_entry(cfg, "死亡长按", units=[DEATH_UNIT], default_ms=900)
        text = wave_lib.to_text(cfg)
        again = AppConfig()
        result = wave_lib.from_text(again, text)
        self.assertEqual(result["count"], 1)
        self.assertEqual(again.waves.entries["死亡长按"].default_ms, 900.0)
        with self.assertRaises(ValueError):
            wave_lib.from_text(AppConfig(), "{不是 JSON")

    def test_apply_preset_entry(self) -> None:
        cfg = AppConfig()
        wave_lib.apply_preset_entry(cfg, "心跳", "heartbeat", freq=25.0, default_ms=400)
        entry = cfg.waves.entries["心跳"]
        self.assertEqual(entry.preset, "heartbeat")
        self.assertEqual(entry.freq, 25.0)
        self.assertEqual(entry.default_ms, 400.0)


if __name__ == "__main__":
    unittest.main()

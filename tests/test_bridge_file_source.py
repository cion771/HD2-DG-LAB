"""文件广播源：桥在无 ffi 的 MDL3 宿主上写 bridge_state.json，这个源要认它。"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from hd2coyote.config import AppConfig
from hd2coyote.sources.bridge_file import BridgeFileSource, state_path


class TestBridgeFileSource(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self._old = os.environ.get("HD2COYOTE_DIR")
        os.environ["HD2COYOTE_DIR"] = str(self.dir)
        self.cfg = AppConfig()
        self.src = BridgeFileSource(self.cfg)
        self.src.start()

    def tearDown(self) -> None:
        self.src.stop()
        if self._old is None:
            os.environ.pop("HD2COYOTE_DIR", None)
        else:
            os.environ["HD2COYOTE_DIR"] = self._old
        self.tmp.cleanup()

    def write(self, packet: dict) -> None:
        (self.dir / "bridge_state.json").write_text(json.dumps(packet), encoding="utf-8")
        # 让 mtime 一定变化（Windows 的时间戳精度有时只有 1ms）
        stat = (self.dir / "bridge_state.json").stat()
        os.utime(self.dir / "bridge_state.json", ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000))

    def test_path_follows_env(self) -> None:
        self.assertEqual(state_path(), self.dir / "bridge_state.json")

    def test_state_packet_drives_tracker(self) -> None:
        self.write({"v": 1, "ev": "state", "t": 1.0, "hp": 62, "hp_max": 100,
                    "limbs": [1, 1, 0, 0, 0, 0], "dead": 0})
        events = self.src.poll(time.monotonic())
        self.assertEqual(self.src.state.hp, 0.62)
        self.assertEqual(self.src.state.hp_max, 100.0)
        self.assertEqual(self.src.state.limbs, [1, 1, 0, 0, 0, 0])
        self.assertTrue(self.src.alive)
        self.assertTrue(events or self.src.state.packets == 1)

    def test_unchanged_file_is_not_refed(self) -> None:
        self.write({"v": 1, "ev": "state", "hp": 50, "hp_max": 100, "limbs": [0] * 6, "dead": 0})
        self.src.poll(time.monotonic())
        packets = self.src.state.packets
        for _ in range(5):
            self.src.poll(time.monotonic() + 1.0)
        self.assertEqual(self.src.state.packets, packets, "文件没变时不应重复计包")

    def test_bad_json_counts_as_bad_packet(self) -> None:
        (self.dir / "bridge_state.json").write_text('{"v":1,"ev":"state","hp":', encoding="utf-8")
        self.src.poll(time.monotonic())
        self.assertEqual(self.src.state.bad_packets, 1)
        self.assertFalse(self.src.alive)

    def test_heartbeat_does_not_renew_state(self) -> None:
        self.write({"v": 1, "ev": "heartbeat", "mode": "live", "detail": "ok"})
        self.src.poll(time.monotonic())
        self.assertEqual(self.src.state.mode, "live")
        self.assertEqual(self.src.state.received, 0.0, "心跳绝不能延长旧状态的有效期")
        self.assertFalse(self.src.alive)

    def test_source_is_registered(self) -> None:
        from hd2coyote.sources import available_sources, source_catalog

        self.assertIn("bridge_file", available_sources())
        row = {r["name"]: r for r in source_catalog()}["bridge_file"]
        self.assertTrue(row["critical"])
        self.assertTrue(row["label"])


if __name__ == "__main__":
    unittest.main()

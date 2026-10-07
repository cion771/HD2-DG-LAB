"""Web 控制台的测试：起真服务器，打真 HTTP（不需要游戏、不需要硬件）。

覆盖：
  * 页面能拿出来，且不依赖任何外部 CDN；
  * /api/status 的 JSON 结构（控制器 + 桥 + 诊断）；
  * /api/config 局部合并 → 落盘 → 立即生效（含越界拒绝）；
  * /api/bridge 写 bridge_config.lua（含校验、备份、回显）；
  * /api/actions 的急停 / 重新武装 / 测试脉冲真的作用到引擎与设备。
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from hd2coyote import __version__
from hd2coyote.config import AppConfig
from hd2coyote.webui import (BRIDGE_MODES, WebApp, bridge_config_path, build_server,
                             parse_bridge_config, render_bridge_config, write_bridge_config)


def free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def http(method: str, url: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            if "json" in ctype:
                return resp.status, json.loads(body)
            return resp.status, {"raw": body.decode("utf-8", "replace")}
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            return exc.code, json.loads(body)
        except Exception:
            return exc.code, {"raw": body.decode("utf-8", "replace")}


class TestBridgeConfigFile(unittest.TestCase):
    def test_render_and_parse_roundtrip(self) -> None:
        values = {"mode": "menu", "port": 47778, "interval": 0.2, "profile": "steam_25480438",
                  "offsets": {"hp": 0x2C, "hp_max": -1, "limb_mask": 0x38, "limb_shift": 2, "dead": -1}}
        text = render_bridge_config(values)
        parsed = parse_bridge_config(text)
        self.assertEqual(parsed["mode"], "menu")
        self.assertEqual(parsed["port"], 47778)
        self.assertAlmostEqual(parsed["interval"], 0.2)
        self.assertEqual(parsed["offsets"]["hp"], 0x2C)
        self.assertEqual(parsed["offsets"]["limb_shift"], 2)
        self.assertEqual(parsed["offsets"]["dead"], -1)
        self.assertIn("return {", text)

    def test_rejects_bad_values(self) -> None:
        with self.assertRaises(ValueError):
            render_bridge_config({"mode": "menuu"})
        with self.assertRaises(ValueError):
            render_bridge_config({"mode": "menu", "port": 80})
        with self.assertRaises(ValueError):
            render_bridge_config({"mode": "menu", "interval": 99})
        with self.assertRaises(ValueError):
            render_bridge_config({"mode": "menu", "profile": "bad name"})
        with self.assertRaises(ValueError):
            render_bridge_config({"mode": "menu", "offsets": {"hp": 999999}})
        with self.assertRaises(ValueError):
            render_bridge_config({"mode": "menu", "offsets": {"limb_shift": 9}})

    def test_write_makes_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_bridge_config({"mode": "safe"}, tmp)
            self.assertEqual(path.name, "bridge_config.lua")
            path.write_text("-- 旧内容\n", encoding="utf-8")
            write_bridge_config({"mode": "menu"}, tmp)
            self.assertIn("menu", bridge_config_path(tmp).read_text(encoding="utf-8"))
            backup = path.with_suffix(".lua.bak")
            self.assertTrue(backup.exists())
            self.assertIn("旧内容", backup.read_text(encoding="utf-8"))

    def test_all_modes_are_renderable(self) -> None:
        for mode in BRIDGE_MODES:
            self.assertIn(f"mode = '{mode}'", render_bridge_config({"mode": mode}))


class TestWebServer(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.cfg = AppConfig()
        self.cfg.device.kind = "mock"          # 不碰硬件
        self.config_path = self.dir / "config.json"
        self.cfg.save(self.config_path)
        self.port = free_port()
        self.httpd, self.app = build_server(self.cfg, self.config_path, "127.0.0.1", self.port,
                                            bridge_cfg_dir=self.dir / "bridge")
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self) -> None:
        self.app.engine.stop()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)
        self.tmp.cleanup()

    # ------------------------------------------------------------ 页面
    def test_page_is_self_contained(self) -> None:
        status, body = http("GET", self.base + "/")
        self.assertEqual(status, 200)
        html = body["raw"]
        self.assertIn("hd2-coyote 控制台", html)
        self.assertIn("/api/status", html)
        self.assertNotIn("http://cdn", html)
        self.assertNotIn("https://", html)          # 不引任何外部资源

    def test_status_shape(self) -> None:
        status, data = http("GET", self.base + "/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(data["controller"]["version"], __version__)
        for key in ("running", "detecting", "hp", "limbs", "output", "armed", "device", "hook"):
            self.assertIn(key, data["controller"])
        self.assertIn("events", data)
        self.assertIn("bridge", data)
        self.assertIn("config_path", data["bridge"])

    def test_unknown_route_404(self) -> None:
        status, _ = http("GET", self.base + "/nope")
        self.assertEqual(status, 404)

    # ------------------------------------------------------------ 控制器配置
    def test_post_config_applies_and_persists(self) -> None:
        status, data = http("POST", self.base + "/api/config", {
            "rules": {"death": {"base_pct": 55, "enabled": False}},
            "safety": {"max_absolute": 25, "master_multiplier": 0.5},
        })
        self.assertEqual(status, 200, data)
        self.assertTrue(data["ok"])
        self.assertAlmostEqual(data["config"]["rules"]["death"]["base_pct"], 55)
        self.assertFalse(data["config"]["rules"]["death"]["enabled"])
        self.assertEqual(data["config"]["safety"]["max_absolute"], 25)
        # 已落盘
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["safety"]["max_absolute"], 25)
        # 引擎侧立即生效（安全上限是同一份对象）
        self.assertEqual(self.app.engine.safety.cfg.max_absolute, 25)
        self.assertEqual(self.app.engine.safety.strength_for(100.0, 200), 25)

    def test_post_config_rejects_out_of_range(self) -> None:
        status, data = http("POST", self.base + "/api/config", {"device": {"port": 80}})
        self.assertEqual(status, 400)
        self.assertIn("port", data["error"])

    def test_post_config_bad_source(self) -> None:
        status, data = http("POST", self.base + "/api/config", {"source": "telepathy"})
        self.assertEqual(status, 400)
        self.assertIn("source", data["error"])

    # ------------------------------------------------------------ 桥配置
    def test_post_bridge_writes_file(self) -> None:
        status, data = http("POST", self.base + "/api/bridge", {
            "mode": "live", "port": 47779, "interval": 0.15,
            "offsets": {"hp": 0x2C, "hp_max": 0x30, "limb_mask": 0x38, "limb_shift": 0, "dead": -1},
        })
        self.assertEqual(status, 200, data)
        self.assertTrue(data["ok"])
        path = Path(data["path"])
        self.assertTrue(path.exists())
        parsed = parse_bridge_config(path.read_text(encoding="utf-8"))
        self.assertEqual(parsed["mode"], "live")
        self.assertEqual(parsed["port"], 47779)
        self.assertEqual(parsed["offsets"]["hp"], 0x2C)
        self.assertIn("重启游戏", data["note"])

    def test_post_bridge_rejects_bad_mode(self) -> None:
        status, data = http("POST", self.base + "/api/bridge", {"mode": "yolo"})
        self.assertEqual(status, 400)
        self.assertIn("mode", data["error"])

    def test_status_reflects_bridge_file(self) -> None:
        http("POST", self.base + "/api/bridge", {"mode": "menu", "port": 47780})
        _, data = http("GET", self.base + "/api/status")
        self.assertTrue(data["bridge"]["exists"])
        self.assertEqual(data["bridge"]["parsed"]["mode"], "menu")

    # ------------------------------------------------------------ 动作
    def test_actions_trip_and_arm(self) -> None:
        status, data = http("POST", self.base + "/api/actions", {"action": "trip"})
        self.assertEqual(status, 200, data)
        self.assertFalse(self.app.engine.safety.armed)
        _, data = http("POST", self.base + "/api/actions", {"action": "arm"})
        self.assertTrue(data["ok"])
        self.assertTrue(self.app.engine.safety.armed)

    def test_action_test_pulse_reaches_device(self) -> None:
        status, data = http("POST", self.base + "/api/actions",
                            {"action": "test_pulse", "pct": 3, "ms": 300})
        self.assertEqual(status, 200, data)
        self.app.engine.tick()
        self.assertGreater(self.app.engine.device.peak_strength, 0)
        self.app.engine.trip("测试结束")

    def test_action_rejects_bad_pulse(self) -> None:
        status, data = http("POST", self.base + "/api/actions",
                            {"action": "test_pulse", "pct": 500, "ms": 300})
        self.assertEqual(status, 400)

    def test_action_unknown(self) -> None:
        status, data = http("POST", self.base + "/api/actions", {"action": "dance"})
        self.assertEqual(status, 400)
        self.assertIn("未知动作", data["error"])

    def test_control_actions_appear_in_event_log(self) -> None:
        http("POST", self.base + "/api/actions", {"action": "trip"})
        http("POST", self.base + "/api/actions", {"action": "arm"})
        _, data = http("GET", self.base + "/api/status")
        joined = "\n".join(data["events"])
        self.assertIn("急停", joined)
        self.assertIn("重新武装", joined)

    def test_detection_events_appear_in_event_log(self) -> None:
        from hd2coyote.events import Damage

        self.app._on_event(Damage(severity=17.0))
        _, data = http("GET", self.base + "/api/status")
        self.assertTrue(any("17" in line for line in data["events"]), data["events"])


class TestWebAppDirect(unittest.TestCase):
    """不经 HTTP 直接测 WebApp（更快，也覆盖 JSON 之外的返回结构）。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = AppConfig()
        self.cfg.device.kind = "mock"
        self.app = WebApp(self.cfg, Path(self.tmp.name) / "config.json",
                          bridge_cfg_dir=Path(self.tmp.name) / "bridge")

    def tearDown(self) -> None:
        self.app.engine.stop()
        self.tmp.cleanup()

    def test_status_without_any_bridge_files(self) -> None:
        data = self.app.status()
        self.assertFalse(data["bridge"]["exists"])
        self.assertEqual(data["bridge"]["status"], "")
        self.assertEqual(data["bridge"]["log_tail"], [])

    def test_qr_svg_is_available_for_socket_device(self) -> None:
        cfg = AppConfig()          # 默认 socket 设备
        app = WebApp(cfg, Path(self.tmp.name) / "config2.json",
                     bridge_cfg_dir=Path(self.tmp.name) / "bridge")
        try:
            svg = app.qr_svg()
            self.assertTrue(svg is None or svg.startswith(b"<?xml") or b"<svg" in svg)
        finally:
            app.engine.stop()


if __name__ == "__main__":
    unittest.main()

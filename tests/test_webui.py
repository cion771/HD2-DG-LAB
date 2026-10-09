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
import time
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

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
        home = mock.patch("pathlib.Path.home", return_value=self.dir)
        home.start()
        self.addCleanup(home.stop)
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
        self.app.close("测试结束")
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)
        self.tmp.cleanup()

    # ------------------------------------------------------------ 页面
    def test_page_is_self_contained(self) -> None:
        status, body = http("GET", self.base + "/")
        self.assertEqual(status, 200)
        html = body["raw"]
        self.assertIn("HD2", html)
        self.assertNotIn("/*__STYLE__*/", html)
        self.assertNotIn("/*__SCRIPT__*/", html)
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
        self.assertEqual(self.app.engine.safety.strength_for(100.0, 200), 0)
        self.app.engine.arm()
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
        self.app.action({"action": "start"})
        self.app.action({"action": "arm"})
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

    # ------------------------------------------------------------ 关闭程序
    def test_shutdown_stops_engine_then_server(self) -> None:
        """「关闭程序」必须先把输出归零/断开设备，再让服务器退出。"""
        self.app.engine.start()
        self.app.engine.tick()
        status, data = http("POST", self.base + "/api/actions", {"action": "shutdown"})
        self.assertEqual(status, 200, data)
        self.assertTrue(data["ok"])
        self.assertIn("请求停止输出", data["detail"])
        self.assertFalse(self.app.engine.status.running)
        self.assertFalse(self.app.engine.device.connected)
        deadline = time.time() + 5          # 服务器应在几秒内真的停掉
        stopped = False
        while time.time() < deadline:
            try:
                http("GET", self.base + "/api/status")
            except Exception:
                stopped = True
                break
            time.sleep(0.2)
        self.assertTrue(stopped, "服务器没有停下来")
        self.assertTrue(self.app.shutdown_event.is_set())

    def test_shutdown_is_recorded_in_events(self) -> None:
        http("POST", self.base + "/api/actions", {"action": "shutdown"})
        _, data = http("GET", self.base + "/api/status")
        self.assertTrue(any("关闭程序" in line for line in data["events"]), data["events"])

    def test_loopback_check(self) -> None:
        from hd2coyote.webui import is_loopback

        for good in ("127.0.0.1", "127.0.0.5", "::1", "::1%lo0", "localhost"):
            self.assertTrue(is_loopback(good), good)
        for bad in ("", "192.168.1.10", "10.0.0.2", "203.0.113.7", "::ffff:8.8.8.8"):
            self.assertFalse(is_loopback(bad), bad)

    def test_page_shutdown_button_cannot_double_shutdown(self) -> None:
        """踩过的坑：关成功后按钮没禁用，再点一次弹 `关闭失败: Failed to fetch`。"""
        from hd2coyote.webui import PAGE

        self.assertIn('id="shutdownBtn"', PAGE)
        self.assertIn("function markClosed", PAGE)
        self.assertIn("if (closed || closing) return;", PAGE)
        self.assertIn("$('shutdownBtn').disabled = closing || closed", PAGE)
        # 断线不是关闭成功，必须显示未知状态并继续重连。
        self.assertIn("function markUnavailable", PAGE)
        self.assertIn("输出状态未知", PAGE)
        self.assertIn("markClosed(res.detail)", PAGE)
        self.assertNotIn("innerHTML", PAGE)

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

    # ------------------------------------------------- 0.5.0：事件源 / 波形 / 更新
    def test_get_sources_endpoint(self) -> None:
        status, data = http("GET", self.base + "/api/sources")
        self.assertEqual(status, 200, data)
        self.assertTrue(data["ok"])
        self.assertIn("game_bridge", data["enabled"])
        names = [row["name"] for row in data["catalog"]]
        self.assertIn("game_bridge", names)
        self.assertIn("http", names)
        self.assertIn("/event", data["http"]["url"])
        self.assertIn("damage", data["http"]["examples"])

    def test_get_waves_endpoint(self) -> None:
        status, data = http("GET", self.base + "/api/waves")
        self.assertEqual(status, 200, data)
        self.assertIn("pinch", data["presets"])
        self.assertEqual(data["unit_chars"], 16)
        self.assertEqual(data["unit_ms"], 100)          # 一个单元 = 100ms（4×25ms）
        self.assertIn("pulse_data", data["text"])

    def test_get_update_endpoint_before_any_check(self) -> None:
        status, data = http("GET", self.base + "/api/update")
        self.assertEqual(status, 200, data)
        self.assertIsNone(data["ok"])
        self.assertEqual(data["current"], __version__)

    def test_post_event_injects_and_logs(self) -> None:
        status, data = http("POST", self.base + "/api/event", {"ev": "damage", "severity": 25})
        self.assertEqual(status, 200, data)
        self.assertTrue(data["ok"])
        self.assertIn("受伤", data["detail"])
        _, snap = http("GET", self.base + "/api/status")
        self.assertTrue(any("受伤" in line for line in snap["events"]), snap["events"])

    def test_post_event_accepts_a_list(self) -> None:
        status, data = http("POST", self.base + "/api/event",
                            [{"ev": "damage", "severity": 25}, {"ev": "death"}])
        self.assertEqual(status, 200, data)
        self.assertEqual(sorted(data["events"]), ["damage", "death"])

    def test_post_event_state_packet_is_not_an_error(self) -> None:
        status, data = http("POST", self.base + "/api/event",
                            {"ev": "state", "hp": 100, "hp_max": 100})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["events"], [])

    def test_post_event_rejects_nonsense(self) -> None:
        status, data = http("POST", self.base + "/api/event", {"ev": "喝茶"})
        self.assertEqual(status, 400)
        self.assertIn("没听懂", data["error"])

    def test_post_waves_saves_and_persists(self) -> None:
        status, data = http("POST", self.base + "/api/waves",
                            {"action": "set", "name": "死亡测试", "units": ["1414141464646464"]})
        self.assertEqual(status, 200, data)
        self.assertIn("死亡测试", [row["name"] for row in data["waves"]])
        reloaded = AppConfig.load(self.config_path)
        self.assertIn("死亡测试", reloaded.waves.entries)

    def test_post_waves_rejects_bad_units(self) -> None:
        status, data = http("POST", self.base + "/api/waves",
                            {"action": "set", "name": "坏的", "units": ["不是十六进制"]})
        self.assertEqual(status, 400)
        self.assertIn("十六进制", data["error"])

    def test_post_waves_remove_missing_is_400(self) -> None:
        status, data = http("POST", self.base + "/api/waves",
                            {"action": "remove", "name": "没这个"})
        self.assertEqual(status, 400)

    def test_post_waves_unknown_action_is_400(self) -> None:
        status, data = http("POST", self.base + "/api/waves", {"action": "跳舞"})
        self.assertEqual(status, 400)

    def test_status_carries_sources_ramp_and_update(self) -> None:
        _, data = http("GET", self.base + "/api/status")
        self.assertIn("sources", data["controller"])
        self.assertIn("ramp", data["controller"])
        self.assertIn("pct", data["controller"]["ramp"])
        self.assertIn("update", data)
        self.assertIn("hook", data["controller"])
        self.assertIn("device", data["controller"])
        self.assertIn("bridge", data)

    def test_post_config_rejects_empty_or_unknown_sources(self) -> None:
        status, data = http("POST", self.base + "/api/config",
                            {"sources": {"enabled": ["脑电波"]}})
        self.assertEqual(status, 400)
        self.assertIn("未知事件源", data["error"])
        status, data = http("POST", self.base + "/api/config", {"sources": {"enabled": []}})
        self.assertEqual(status, 400)

    def test_post_config_applies_ramp_settings(self) -> None:
        status, data = http("POST", self.base + "/api/config",
                            {"ramp": {"enabled": True, "per_event": 3, "ceiling_pct": 18,
                                      "apply_to": ["damage"]}})
        self.assertEqual(status, 200, data)
        self.assertTrue(self.app.cfg.ramp.enabled)
        self.assertEqual(self.app.cfg.ramp.per_event, 3.0)
        self.assertEqual(self.app.cfg.ramp.ceiling_pct, 18.0)
        self.assertEqual(self.app.cfg.ramp.apply_to, ["damage"])
        self.assertTrue(any("ramp.enabled" in line for line in data["applied"]), data["applied"])

    def test_post_config_applies_update_settings(self) -> None:
        status, data = http("POST", self.base + "/api/config",
                            {"update": {"repo": "someone/else", "timeout_s": 99}})
        self.assertEqual(status, 200, data)
        self.assertEqual(self.app.cfg.update.repo, "someone/else")
        self.assertEqual(self.app.cfg.update.timeout_s, 30.0)      # 夹到 1..30


class TestWebAppDirect(unittest.TestCase):
    """不经 HTTP 直接测 WebApp（更快，也覆盖 JSON 之外的返回结构）。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        home = mock.patch("pathlib.Path.home", return_value=Path(self.tmp.name))
        home.start()
        self.addCleanup(home.stop)
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


class TestWaveLibraryApi(unittest.TestCase):
    """波形库面板背后的那套动作（不走 HTTP，直接打方法）。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        home = mock.patch("pathlib.Path.home", return_value=Path(self.tmp.name))
        home.start()
        self.addCleanup(home.stop)
        self.cfg = AppConfig()
        self.cfg.device.kind = "mock"
        self.path = Path(self.tmp.name) / "config.json"
        self.app = WebApp(self.cfg, self.path, bridge_cfg_dir=Path(self.tmp.name) / "bridge")
        self.app.engine.device.start()
        self.app.engine.safety.arm()

    def tearDown(self) -> None:
        self.app.engine.stop()
        self.tmp.cleanup()

    def entry(self, name: str) -> dict:
        return next(row for row in self.app.waves_dict()["waves"] if row["name"] == name)

    def test_set_then_persist_then_export(self) -> None:
        result = self.app.waves_api({"action": "set", "name": "死亡", "units": ["1414141464646464"],
                                     "default_ms": 800, "note": "参考项目抄的"})
        self.assertTrue(result["ok"])
        row = self.entry("死亡")
        self.assertEqual(row["unit_count"], 1)
        self.assertEqual(row["kind"], "units")
        self.assertEqual(row["default_ms"], 800.0)
        reloaded = AppConfig.load(self.path)
        self.assertEqual(reloaded.waves.entries["死亡"].units, ["1414141464646464"])
        exported = self.app.waves_api({"action": "export"})
        self.assertIn('"死亡"', exported["text"])
        self.assertIn("pulse_data", exported["text"])

    def test_preset_action_and_rules_view(self) -> None:
        result = self.app.waves_api({"action": "preset", "name": "长按死亡", "preset": "death",
                                     "freq": 45})
        self.assertTrue(result["ok"])
        self.assertEqual(self.entry("长按死亡")["preset"], "death")
        self.assertIn("damage", result["rules"])

    def test_import_pulse_data_from_reference_project(self) -> None:
        text = json.dumps({"pulse_data": {"受伤": ["0A0A0A0A64646464"]},
                           "punish_time": {"受伤": 2}})
        result = self.app.waves_api({"action": "import", "text": text})
        self.assertEqual(result["import"]["count"], 1)
        self.assertEqual(self.entry("受伤")["default_ms"], 2000.0)

    def test_import_replace_clears_old_entries(self) -> None:
        self.app.waves_api({"action": "set", "name": "旧的", "units": ["1414141464646464"]})
        self.app.waves_api({"action": "replace", "text": json.dumps({"pulse_data": {"新的": "1414141464646464"}})})
        names = [row["name"] for row in self.app.waves_dict()["waves"]]
        self.assertNotIn("旧的", names)
        self.assertIn("新的", names)

    def test_import_bad_json_is_value_error(self) -> None:
        with self.assertRaises(ValueError):
            self.app.waves_api({"action": "import", "text": "{不是 json"})

    def test_remove_and_unknown_action(self) -> None:
        self.app.waves_api({"action": "set", "name": "临时", "units": ["1414141464646464"]})
        result = self.app.waves_api({"action": "remove", "name": "临时"})
        self.assertTrue(result["ok"])
        with self.assertRaises(ValueError):
            self.app.waves_api({"action": "remove", "name": "临时"})
        with self.assertRaises(ValueError):
            self.app.waves_api({"action": "跳舞"})
        with self.assertRaises(ValueError):
            self.app.waves_api({"action": "set", "name": "", "units": ["1414141464646464"]})

    def test_test_action_plays_the_library_wave(self) -> None:
        self.app.waves_api({"action": "set", "name": "试打", "units": ["0A0A0A0A64646464"]})
        self.app.action({"action": "start"})
        self.app.waves_api({"action": "test", "name": "试打", "pct": 4, "ms": 300})
        self.app.engine.tick()
        self.assertGreater(self.app.engine.device.peak_strength, 0)
        self.app.engine.trip("测试结束")

    def test_test_action_rejects_bad_parameters(self) -> None:
        for bad in ({"name": "试打", "channel": "C"}, {"name": "试打", "pct": 500},
                    {"name": "试打", "ms": 5}, {}):
            with self.assertRaises(ValueError):
                self.app.waves_api({"action": "test", **bad})


class TestUpdateApi(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        cfg = AppConfig()
        cfg.device.kind = "mock"
        self.app = WebApp(cfg, Path(self.tmp.name) / "config.json",
                          bridge_cfg_dir=Path(self.tmp.name) / "bridge")

    def tearDown(self) -> None:
        self.app.engine.stop()
        self.tmp.cleanup()

    def test_info_before_check_does_not_touch_the_network(self) -> None:
        info = self.app.update_info()
        self.assertIsNone(info["ok"])
        self.assertEqual(info["current"], __version__)
        self.assertIn("还没检查过", info["error"])

    def test_check_is_cached_until_cleared(self) -> None:
        fake = {"ok": True, "current": __version__, "update_available": True,
                "latest": {"tag": "v9.9.9", "url": "https://example.invalid/r"}, "zip": None}
        with mock.patch("hd2coyote.webui.update_check.check", return_value=fake) as checker:
            self.assertEqual(self.app.update_api({})["latest"]["tag"], "v9.9.9")
            checker.assert_called_once()
        # 再读就是缓存，不再打网络
        self.assertEqual(self.app.update_info()["latest"]["tag"], "v9.9.9")
        with mock.patch("hd2coyote.webui.update_check.check") as checker:
            self.app.update_api({"clear": True})
            checker.assert_not_called()
        self.assertIsNone(self.app.update_info()["ok"])

    def test_check_records_an_event(self) -> None:
        fake = {"ok": True, "current": __version__, "update_available": False}
        with mock.patch("hd2coyote.webui.update_check.check", return_value=fake):
            self.app.update_api({})
        self.assertTrue(any("已是最新" in line for line in self.app.events), self.app.events)

    def test_disabled_update_is_refused(self) -> None:
        self.app.cfg.update.enabled = False
        with self.assertRaises(ValueError):
            self.app.update_api({})


if __name__ == "__main__":
    unittest.main()


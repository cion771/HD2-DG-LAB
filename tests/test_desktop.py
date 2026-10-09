"""Desktop regressions: mock hardware, temporary data, real loopback HTTP."""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from hd2coyote.config import AppConfig
from hd2coyote.desktop import DesktopSession, SingleInstance, run_desktop


class TestDesktopSession(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        home = patch("pathlib.Path.home", return_value=self.root)
        home.start()
        self.addCleanup(home.stop)
        self.addCleanup(self.tmp.cleanup)
        self.cfg = AppConfig()
        self.cfg.device.kind = "mock"
        self.cfg.hook.port = 0
        self.session = DesktopSession(self.cfg, self.root / "config.json", self.root / "bridge")
        self.addCleanup(self.session.close)
        self.app = self.session.app

    def request(self, route="api/status", body=None, headers=None):
        data = None if body is None else json.dumps(body).encode()
        h = {"Content-Type": "application/json", **(headers or {})}
        req = urllib.request.Request(self.session.url + route, data=data, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=3) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.loads(error.read())

    def test_launch_is_idle_and_disarmed(self):
        code, data = self.request()
        self.assertEqual(code, 200)
        self.assertFalse(data["controller"]["running"])
        self.assertFalse(data["controller"]["armed"])
        self.assertFalse(data["controller"]["device"]["connected"])
        self.assertEqual(self.session.server.server_address[0], "127.0.0.1")
        self.assertGreater(self.session.server.server_port, 0)
        for action in ({"action": "test_pulse"},):
            self.assertEqual(self.request("api/actions", action)[0], 400)
        self.assertEqual(self.request("api/waves", {"action": "test", "name": "pinch"})[0], 400)

    def test_close_is_idempotent_and_releases_server(self):
        self.app.action({"action": "start"})
        self.app.action({"action": "arm"})
        self.app.action({"action": "test_pulse", "pct": 3, "ms": 300})
        self.app.engine.tick()
        self.session.close()
        self.session.close()
        self.assertFalse(self.app.engine.status.running)
        self.assertFalse(self.app.engine.device.connected)
        self.assertFalse(self.app.engine.safety.armed)
        self.assertFalse(self.session.thread.is_alive())
        self.assertTrue(self.app.shutdown_event.is_set())
        self.assertEqual(self.app.engine.status.output_a, 0)
        with self.assertRaises((OSError, urllib.error.URLError)):
            self.request()
        for action in ("start", "arm", "device_start", "test_pulse"):
            with self.assertRaises(ValueError):
                self.app.action({"action": action})
        with self.assertRaises(ValueError):
            self.app.update_config({"safety": {"max_absolute": 30}})

    def test_stop_and_reconfigure_do_not_rearm(self):
        self.app.action({"action": "start"})
        self.app.action({"action": "arm"})
        self.app.action({"action": "stop"})
        self.assertFalse(self.app.engine.safety.armed)
        self.app.action({"action": "start"})
        self.app.action({"action": "arm"})
        previous = self.app.engine
        self.app.update_config({"device": {"kind": "mock"}})
        self.assertIsNot(previous, self.app.engine)
        self.assertFalse(previous.status.running)
        self.assertFalse(previous.device.connected)
        self.assertFalse(self.app.engine.status.running)
        self.assertFalse(self.app.engine.safety.armed)

    def test_browser_cross_origin_and_rebinding_are_rejected(self):
        port = self.session.server.server_port
        for headers in ({"Origin": "https://evil.invalid"},
                        {"Sec-Fetch-Site": "cross-site"},
                        {"Host": f"evil.invalid:{port}"},
                        {"Content-Type": "text/plain"}):
            self.assertEqual(self.request("api/actions", {"action": "arm"}, headers)[0], 403)
        self.assertFalse(self.app.engine.safety.armed)
        self.assertEqual(self.request("api/actions", {"action": "arm"},
                                     {"Origin": self.session.url.rstrip("/")})[0], 200)

    def test_close_attempts_stop_even_if_trip_fails(self):
        with patch.object(self.app.engine, "trip", side_effect=RuntimeError("mock failure")), \
                patch.object(self.app.engine, "stop") as stop, \
                self.assertLogs("hd2coyote.webui", level="ERROR"):
            result = self.app.close("test")
        stop.assert_called_once()
        self.assertFalse(result["ok"])
        self.assertIn("状态未知", result["detail"])
        self.assertNotIn("已归零", result["detail"])
        self.assertTrue(self.app.shutdown_event.is_set())

    def test_close_serializes_against_pending_start(self):
        with self.app.lifecycle_lock:
            worker = threading.Thread(target=self.session.close)
            worker.start()
            self.app.close("first")
            with self.assertRaises(ValueError):
                self.app.action({"action": "start"})
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertFalse(self.app.engine.status.running)


class ClosingEvent:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def fire(self):
        for handler in self.handlers:
            handler()


class TestNativeHost(unittest.TestCase):
    def test_window_close_and_gui_failure_both_cleanup(self):
        for failure in (False, True):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                cfg = AppConfig()
                cfg.device.kind = "mock"
                cfg.save(root / "config.json")
                event = ClosingEvent()
                window = SimpleNamespace(events=SimpleNamespace(closing=event))
                view = Mock()
                view.create_window.return_value = window
                session = Mock()
                session.url = "http://127.0.0.1:12345/"
                def start(*args, **kwargs):
                    if failure:
                        raise RuntimeError("mock WebView2 unavailable")
                    event.fire()
                view.start.side_effect = start
                with patch.dict(sys.modules, {"webview": view}), \
                        patch("hd2coyote.desktop.DesktopSession", return_value=session) as factory:
                    if failure:
                        with self.assertRaises(RuntimeError):
                            run_desktop(data_dir=root)
                    else:
                        run_desktop(data_dir=root)
                self.assertTrue(session.close.called)
                self.assertEqual(factory.call_args.args[1], root / "config.json")
                self.assertEqual(view.start.call_args.kwargs["gui"], "edgechromium")
                self.assertTrue(view.start.call_args.kwargs["private_mode"])
                self.assertNotIn("js_api", view.create_window.call_args.kwargs)

    @unittest.skipUnless(sys.platform == "win32", "Windows mutex")
    def test_single_instance_released(self):
        first, second = SingleInstance(), SingleInstance()
        try:
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            first.close()
            self.assertTrue(second.acquire())
        finally:
            first.close()
            second.close()


if __name__ == "__main__":
    unittest.main()

"""Regression coverage for the removal of the screenshot backend."""
import contextlib
import importlib.util
import io
import sys
import tempfile
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest import mock

from hd2coyote.__main__ import build_parser
from hd2coyote.config import AppConfig, from_dict
from hd2coyote.detectors import EventTrackers
from hd2coyote.engine import Engine
from hd2coyote.simulate import DemoStep, run_demo
from hd2coyote.webui import WebApp


class TestNoVision(unittest.TestCase):
    def test_removed_modules_and_cli_are_unavailable(self):
        self.assertIsNone(importlib.util.find_spec("hd2coyote.capture"))
        self.assertIsNone(importlib.util.find_spec("hd2coyote.hud"))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            build_parser().parse_args(["calibrate"])
        self.assertFalse(hasattr(Engine, "_run_vision"))

    def test_api_rejects_vision_without_reconfiguring_engine(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig()
            cfg.device.kind = "mock"
            app = WebApp(cfg, Path(tmp) / "config.json", bridge_cfg_dir=Path(tmp))
            try:
                original = app.engine
                with self.assertRaises(ValueError):
                    app.update_config({"source": "vision"})
                self.assertIs(app.engine, original)
                self.assertEqual(app.cfg.source, "hook")
                self.assertFalse(app.engine.status.running)
                self.assertFalse(app.engine.safety.armed)
            finally:
                app.close()

    def test_state_demo_does_not_load_image_packages(self):
        cfg = AppConfig()
        cfg.device.kind = "mock"
        engine = Engine(cfg)
        script = [DemoStep(0.0, 1.0, label="full"), DemoStep(0.1, 0.8, label="damage")]
        clock = SimpleNamespace(monotonic=mock.Mock(side_effect=[0.0, 0.0, 0.0, 0.1, 0.1, 0.3]), sleep=mock.Mock())
        with mock.patch("hd2coyote.simulate.time", clock):
            run_demo(cfg, engine, duration=0.2, script=script)
        self.assertTrue(any(type(ev).__name__ == "Damage" for ev in engine.events_log))
        for package in ("numpy", "PIL", "mss", "dxcam", "hd2coyote.capture", "hd2coyote.hud"):
            self.assertNotIn(package, sys.modules)


if __name__ == "__main__":
    unittest.main()

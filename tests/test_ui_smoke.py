"""界面冒烟测试：能建出窗口、能改配置，但不进入 mainloop。

没有图形环境（纯命令行/CI）时自动跳过。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

try:
    import tkinter as tk

    _root = tk.Tk()
    _root.destroy()
    TK_OK = True
except Exception:  # pragma: no cover
    TK_OK = False


@unittest.skipUnless(TK_OK, "没有可用的 Tk 图形环境")
class TestUiSmoke(unittest.TestCase):
    def test_build_window(self) -> None:
        from hd2coyote.config import AppConfig
        from hd2coyote.ui import MainWindow

        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig()
            cfg.device.kind = "mock"
            win = MainWindow(cfg, Path(tmp) / "config.json")
            try:
                win.root.update()
                # 滑块 -> 配置（模拟拖动）
                win._set_rule_pct("death", _FakeVar(55.0))
                self.assertAlmostEqual(cfg.rules["death"].base_pct, 55.0)
                win.var_maxabs.set(12)
                win._set_maxabs()
                self.assertEqual(cfg.safety.max_absolute, 12)
                win._save(silent=True)
                self.assertTrue((Path(tmp) / "config.json").exists())
            finally:
                win.engine.stop()
                win.root.destroy()


class _FakeVar:
    def __init__(self, value: float) -> None:
        self._value = value

    def get(self) -> float:
        return self._value


if __name__ == "__main__":
    unittest.main()

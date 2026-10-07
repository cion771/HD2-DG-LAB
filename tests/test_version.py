"""版本号一致性测试。

版本号唯一的来源是仓库根目录的 `VERSION` 文件，三处必须一致：
  1. `VERSION` 文件
  2. `hd2coyote.__version__`（控制器界面 / `doctor` / 日志都显示它）
  3. `lua/hd2_coyote_bridge.lua` 里的 `local VERSION`（游戏内 STATUS/日志/hello 上报）

任何一处漂移，这里和 `tools/build_addon.py` 都会直接失败 ——
之前正是因为没有这道闸门，改了三轮内容版本号还停在 0.1.0。
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_addon  # noqa: E402
import hd2coyote  # noqa: E402
from hd2coyote.config import AppConfig  # noqa: E402
from hd2coyote.hook import PROTOCOL_VERSION, HookSource  # noqa: E402

LUA = ROOT / "lua" / "hd2_coyote_bridge.lua"
VERSION_RE = re.compile(r"^local\s+VERSION\s*=\s*'([^']+)'", re.MULTILINE)


def free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class TestVersionConsistency(unittest.TestCase):
    def test_version_file_exists_and_is_semver(self) -> None:
        text = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        self.assertRegex(text, r"^\d+\.\d+\.\d+$", f"VERSION 文件内容不合规：{text!r}")

    def test_all_three_agree(self) -> None:
        file_version = build_addon.read_version()
        self.assertEqual(hd2coyote.__version__, file_version,
                         "hd2coyote/__init__.py 的 __version__ 与 VERSION 文件不一致")
        m = VERSION_RE.search(LUA.read_text(encoding="utf-8"))
        self.assertIsNotNone(m, "Lua 里找不到 local VERSION")
        self.assertEqual(m.group(1), file_version,
                         "lua/hd2_coyote_bridge.lua 的 VERSION 与 VERSION 文件不一致")

    def test_packer_defaults_to_version_file(self) -> None:
        # 不开 --version 时，默认必须来自 VERSION 文件
        self.assertEqual(build_addon.read_version(), hd2coyote.__version__)
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                lua=str(LUA), resource=build_addon.DEFAULT_RESOURCE,
                display_name="HD2 Coyote Bridge", guid=build_addon.DEFAULT_GUID,
                version=build_addon.read_version(), patch_number="auto",
                output=tmp, lua_mods_dir="", force=False)
            zip_path = build_addon.build(args)
            self.assertIn(build_addon.read_version(), zip_path.name,
                          "ZIP 文件名里必须带版本号")
            self.assertTrue(zip_path.name.endswith(f"-{hd2coyote.__version__}.zip"))

    def test_packer_rejects_lua_version_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            drifted = Path(tmp) / "drifted.lua"
            text = LUA.read_text(encoding="utf-8")
            drifted.write_text(text.replace("local VERSION = '", "local VERSION = '9.9.9x", 1),
                               encoding="utf-8")
            with self.assertRaises(ValueError):
                build_addon.check_lua_version(drifted, hd2coyote.__version__)

    def test_addon_reports_version_to_controller(self) -> None:
        """游戏内桥必须在 hello 里上报版本，控制器才能发现版本不一致。"""
        source = LUA.read_text(encoding="utf-8")
        self.assertIn("{ 'ver', VERSION }", source)

        cfg = AppConfig()
        cfg.hook.port = free_udp_port()
        src = HookSource(cfg)
        src.start()
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.sendto(json.dumps({
                "v": PROTOCOL_VERSION, "ev": "hello", "ver": hd2coyote.__version__,
                "build": "1.8.46015.0", "profile": "steam_25480438", "mode": "live",
            }).encode(), ("127.0.0.1", cfg.hook.port))
            time.sleep(0.1)
            src.poll()
            self.assertEqual(src.state.version, hd2coyote.__version__)
            sock.close()
        finally:
            src.stop()


if __name__ == "__main__":
    unittest.main()

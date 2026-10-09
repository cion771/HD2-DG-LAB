"""关闭控制器的测试：起真服务器，用真 HTTP 请它退出，并验证端口真的释放。

覆盖：
  * `post_shutdown()` 能让运行中的控制器优雅退出（先归零/断开，再关服务器）；
  * `wait_port_closed()` 能等到端口释放；
  * 控制器没在跑时，`stop_console()` 明确报告"本来就没在运行"而不是报错；
  * `port_pids()` 能通过 netstat 找到占用端口的进程（用本进程自测）；
  * 非本机来源不能关闭控制器（`is_loopback` 判定）。
"""

from __future__ import annotations

import socket
import tempfile
import threading
import unittest
from pathlib import Path

from hd2coyote.config import AppConfig
from hd2coyote.console_ctl import (port_is_open, port_pids, post_shutdown, stop_console,
                                   wait_port_closed)
from hd2coyote.webui import build_server


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class TestStopConsole(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.cfg = AppConfig()
        self.cfg.device.kind = "mock"
        self.config_path = self.dir / "config.json"
        self.cfg.save(self.config_path)
        self.port = free_port()
        self.httpd, self.app = build_server(self.cfg, self.config_path, "127.0.0.1",
                                            self.port, bridge_cfg_dir=self.dir / "bridge")
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        try:
            self.app.close("测试结束")
        except Exception:
            pass
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:
            pass
        self.thread.join(timeout=3)
        self.tmp.cleanup()

    def test_post_shutdown_stops_the_server(self) -> None:
        self.assertTrue(port_is_open("127.0.0.1", self.port))
        ok, detail = post_shutdown("127.0.0.1", self.port)
        self.assertTrue(ok, detail)
        self.assertIn("请求停止输出", detail)
        self.assertTrue(wait_port_closed("127.0.0.1", self.port, timeout=6.0),
                        "端口应当被释放")
        self.assertFalse(self.app.engine.status.running)
        self.assertFalse(self.app.engine.device.connected)

    def test_stop_console_reports_when_not_running(self) -> None:
        dead = free_port()          # 没有进程在监听
        closed, lines = stop_console("127.0.0.1", dead, force=False, log=lambda *_: None)
        self.assertTrue(closed)
        self.assertTrue(any("本来就没在运行" in line for line in lines), lines)

    def test_stop_console_graceful_path(self) -> None:
        closed, lines = stop_console("127.0.0.1", self.port, force=False,
                                     log=lambda *_: None)
        self.assertTrue(closed, lines)
        self.assertTrue(any("已请求优雅退出" in line for line in lines), lines)
        self.assertFalse(self.app.engine.status.running)

    def test_port_pids_finds_our_own_listener(self) -> None:
        """占用端口的进程就是本测试进程。"""
        import os

        with socket.socket() as srv:
            srv.bind(("127.0.0.1", 0))
            srv.listen(1)
            port = int(srv.getsockname()[1])
            pids = port_pids(port)
        self.assertIn(os.getpid(), pids, f"netstat 应能找到本进程（拿到 {pids}）")

    def test_kill_pids_skips_self(self) -> None:
        from hd2coyote.console_ctl import kill_pids

        import os

        self.assertEqual(kill_pids([os.getpid()]), [])


class TestClientAddressGuard(unittest.TestCase):
    def test_remote_client_cannot_shutdown(self) -> None:
        """通过 HTTP 层验证：非本机来源请求 shutdown 会被 403 拒绝。"""
        from hd2coyote.webui import is_loopback as loop

        self.assertFalse(loop("203.0.113.9"))
        # 真实 HTTP 场景由 test_webui 覆盖；这里只锁住判定函数本身
        self.assertTrue(loop("127.0.0.1"))


if __name__ == "__main__":
    unittest.main()

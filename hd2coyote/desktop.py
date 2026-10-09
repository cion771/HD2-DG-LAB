"""Native Windows host for the offline Fluent UI (WebView2 via pywebview)."""
from __future__ import annotations

import argparse
import ctypes
import logging
import os
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import AppConfig
from .webui import build_server

LOGGER = logging.getLogger(__name__)


def default_data_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return base / "hd2-DG-LAB"


class DesktopSession:
    """Own exactly one server/engine; do not attach to someone else's service."""
    def __init__(self, cfg: AppConfig, config_path: Path,
                 bridge_cfg_dir: str | Path | None = None) -> None:
        self.server, self.app = build_server(cfg, config_path, "127.0.0.1", 0, bridge_cfg_dir)
        self.url = f"http://127.0.0.1:{self.server.server_port}/"
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       name="desktop-http", daemon=True)
        # Start before exposing close(): HTTPServer.shutdown needs serve_forever running.
        self.thread.start()
        self._closed = False
        self._close_lock = threading.Lock()

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            try:
                self.app.close("桌面窗口关闭")
            finally:
                self.server.shutdown()
                self.server.server_close()
                self.thread.join(timeout=3)
                self._closed = True


def run_desktop(config_path: str | Path | None = None,
                bridge_cfg_dir: str | Path | None = None,
                data_dir: str | Path | None = None) -> None:
    """Opening the window never starts detection or arms the output."""
    import webview

    root = Path(data_dir) if data_dir else default_data_dir()
    root.mkdir(parents=True, exist_ok=True)
    path = Path(config_path).resolve() if config_path else root / "config.json"
    cfg = AppConfig.load(path)
    session = DesktopSession(cfg, path, bridge_cfg_dir)
    window_closed = threading.Event()
    try:
        window = webview.create_window("HD2 · DG-LAB", session.url,
                                       width=1180, height=850, min_size=(760, 600),
                                       background_color="#f5f6f8", text_select=True)
        if window is None:
            raise RuntimeError("无法创建桌面窗口")

        def on_closing() -> None:
            # Do not delay urgent stop behind a confirmation dialog.
            window_closed.set()
            session.close()

        def wait_for_shutdown() -> None:
            session.app.shutdown_event.wait()
            if not window_closed.is_set():
                window.destroy()

        window.events.closing += on_closing
        webview.start(wait_for_shutdown, gui="edgechromium", debug=False,
                      private_mode=True, storage_path=str(root / "webview"))
    finally:
        window_closed.set()
        session.close()


class SingleInstance:
    """Per-Windows-session mutex. Never start a second hardware controller."""
    def __init__(self) -> None:
        self.handle = None

    def acquire(self) -> bool:
        if sys.platform != "win32":
            return True
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        self._kernel = kernel
        self.handle = kernel.CreateMutexW(None, False, "Local\\hd2-DG-LAB.desktop")
        error = ctypes.get_last_error()
        if not self.handle:
            raise ctypes.WinError(error)
        if error == 183:  # ERROR_ALREADY_EXISTS
            self.close()
            return False
        return True

    def close(self) -> None:
        if self.handle:
            self._kernel.CloseHandle(self.handle)
            self.handle = None


def notify_error(message: str) -> None:
    if sys.platform == "win32":
        ctypes.windll.user32.MessageBoxW(None, message, "HD2 · DG-LAB", 0x10)
    else:
        print(message, file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="HD2 · DG-LAB Windows 桌面控制台")
    parser.add_argument("--config", help="显式指定配置；默认使用独立的本机应用数据目录")
    parser.add_argument("--bridge-dir", help="游戏内桥配置目录（默认与 Lua 桥共用）")
    parser.add_argument("--data-dir", help="桌面日志与 WebView2 数据目录")
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        notify_error("桌面 EXE 需要 Windows 10/11；其他系统请使用 Web 模式。")
        return 1
    instance = SingleInstance()
    try:
        if not instance.acquire():
            notify_error("HD2 · DG-LAB 桌面程序已在运行，请切换到已有窗口。")
            return 0
        root = Path(args.data_dir).resolve() if args.data_dir else default_data_dir()
        root.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(root / "desktop.log", maxBytes=2_000_000,
                                      backupCount=2, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
        logging.getLogger().addHandler(handler)
        logging.getLogger().setLevel(logging.INFO)
        try:
            run_desktop(args.config, args.bridge_dir, root)
        except Exception:
            LOGGER.exception("桌面启动或运行失败")
            raise
        finally:
            logging.getLogger().removeHandler(handler)
            handler.close()
        return 0
    except Exception:
        LOGGER.exception("桌面启动或运行失败")
        notify_error("桌面程序未能正常运行。请确认已安装 Microsoft Edge WebView2 Runtime，"
                     "并查看本机应用数据目录中的 desktop.log。\n"
                     "若设备曾在输出，请立即在手机 App 停止输出。")
        return 1
    finally:
        instance.close()


if __name__ == "__main__":
    raise SystemExit(main())

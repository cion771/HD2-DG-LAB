"""安全层：硬上限、急停、会话预算、热键。

设计原则（很重要）：
  * 任何一次输出都要经过 SafetyGuard.strength_for() 换算，规则里的强度只是「意愿」。
  * 急停 = 立即静音 + 清空队列 + 解除武装（armed=False），
    必须由用户显式「重新武装」才恢复输出。
  * 会话预算（max_session_seconds）用于限制单次累计输出时长。
"""

from __future__ import annotations

import logging
import platform
import threading
import time
from typing import Callable

from .config import SafetyConfig
from .device.base import Device

IS_WINDOWS = platform.system() == "Windows"

#: 常用虚拟键码（Windows）
VK_CODES: dict[str, int] = {
    "ESC": 0x1B,
    "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73, "F5": 0x74, "F6": 0x75,
    "F7": 0x76, "F8": 0x77, "F9": 0x78, "F10": 0x79, "F11": 0x7A, "F12": 0x7B,
    "SPACE": 0x20,
    "PAUSE": 0x13,
    "HOME": 0x24, "END": 0x23,
}


def foreground_window_title() -> str:
    """当前前台窗口标题（仅 Windows）。"""
    if not IS_WINDOWS:
        return ""
    try:
        import ctypes

        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value
    except Exception:
        return ""


class HotkeyWatcher(threading.Thread):
    """轮询式全局热键（Windows 用 GetAsyncKeyState，无需管理员权限）。

    非 Windows 平台不注册热键，只记一条警告（UI 上的急停按钮依然可用）。
    """

    def __init__(self, bindings: dict[str, Callable[[], None]], poll_s: float = 0.05) -> None:
        super().__init__(name="hotkey-watcher", daemon=True)
        self.bindings = bindings
        self.poll_s = poll_s
        self.log = logging.getLogger("hd2coyote.hotkeys")
        self._stop = threading.Event()
        self._down: dict[int, bool] = {}

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        if not IS_WINDOWS:
            self.log.warning("非 Windows 平台：全局热键不可用，请使用界面上的急停按钮")
            return
        import ctypes

        user32 = ctypes.windll.user32
        vks = {name.upper(): VK_CODES[name.upper()] for name in self.bindings if name.upper() in VK_CODES}
        for name in self.bindings:
            if name.upper() not in VK_CODES:
                self.log.warning("不认识的急停键名：%s", name)
        while not self._stop.is_set():
            for name, vk in vks.items():
                pressed = bool(user32.GetAsyncKeyState(vk) & 0x8000)
                if pressed and not self._down.get(vk):
                    self._down[vk] = True
                    try:
                        self.bindings[name]()
                    except Exception as exc:  # 热键回调不允许崩线程
                        self.log.error("热键 %s 回调异常：%s", name, exc)
                elif not pressed:
                    self._down[vk] = False
            time.sleep(self.poll_s)


class SafetyGuard:
    """所有输出的唯一出口。"""

    def __init__(self, cfg: SafetyConfig, device: Device | None = None) -> None:
        self.cfg = cfg
        self.device = device
        self.log = logging.getLogger("hd2coyote.safety")
        self._armed = True
        self._mute_reason = ""
        self._session_seconds = 0.0
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ 状态
    @property
    def armed(self) -> bool:
        with self._lock:
            return self._armed

    @property
    def mute_reason(self) -> str:
        with self._lock:
            return self._mute_reason

    @property
    def session_seconds(self) -> float:
        with self._lock:
            return self._session_seconds

    def attach(self, device: Device) -> None:
        self.device = device

    # ------------------------------------------------------------------ 武装
    def arm(self) -> None:
        with self._lock:
            self._armed = True
            self._mute_reason = ""
        self.log.info("已武装：允许输出")

    def disarm(self, reason: str = "手动静音") -> None:
        with self._lock:
            self._armed = False
            self._mute_reason = reason
        self.log.warning("已解除武装：%s", reason)

    def trip(self, reason: str = "急停") -> None:
        """急停：静音 + 清空 + 解除武装。"""
        self.disarm(reason)
        self.silence()

    def silence(self) -> None:
        if self.device is not None:
            try:
                self.device.mute()
            except Exception as exc:
                self.log.error("静音失败（请手动断开设备）：%s", exc)

    # ------------------------------------------------------------------ 换算
    def effective_pct(self, pct: float) -> float:
        """把规则强度换算成最终百分比：乘总倍率，并夹到 max_pct 以内。"""
        with self._lock:
            if not self._armed:
                return 0.0
            pct = max(0.0, float(pct)) * max(0.0, float(self.cfg.master_multiplier))
            return min(pct, max(0.0, float(self.cfg.max_pct)))

    def strength_for(self, pct: float, limit: int) -> int:
        """百分比 + 通道上限 -> App 强度值。"""
        eff = self.effective_pct(pct)
        value = int(round(limit * eff / 100.0))
        return max(0, min(int(value), int(limit), int(self.cfg.max_absolute)))

    # ------------------------------------------------------------------ 预算
    def note_output(self, seconds: float, pct: float) -> None:
        if seconds <= 0 or pct <= 0:
            return
        with self._lock:
            self._session_seconds += seconds
            over = self.cfg.max_session_seconds > 0 and self._session_seconds >= self.cfg.max_session_seconds
        if over:
            self.trip(f"会话预算用尽（{self.cfg.max_session_seconds:.0f}s）")

    def reset_session(self) -> None:
        with self._lock:
            self._session_seconds = 0.0

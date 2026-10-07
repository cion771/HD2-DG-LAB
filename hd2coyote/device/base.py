"""设备抽象层。

一个 Device 只需要会做三件事：
    1. set_strength(a=None, b=None)  设置通道强度（App 单位 0~200）
    2. send_wave(channel, units)     追加波形单元
    3. clear(channel)                清空波形队列
"""

from __future__ import annotations

import abc
import threading
from dataclasses import dataclass
from typing import Callable, Iterable

from ..events import Event

CHANNELS = ("A", "B")


@dataclass
class Limits:
    """App 上报的通道强度上限（用户可在手机上调），默认 200。"""

    a: int = 200
    b: int = 200


class Device(abc.ABC):
    """所有输出设备的基类。"""

    name = "device"

    def __init__(self) -> None:
        self._limits = Limits()
        self._connected = False
        self._lock = threading.RLock()
        self.on_event: Callable[[Event], None] | None = None

    # ---------------------------------------------------------------- 状态
    @property
    def limits(self) -> Limits:
        with self._lock:
            return Limits(self._limits.a, self._limits.b)

    @property
    def connected(self) -> bool:
        return self._connected

    def limit_of(self, channel: str) -> int:
        lim = self.limits
        if channel == "A":
            return lim.a
        if channel == "B":
            return lim.b
        return max(lim.a, lim.b)

    def _emit(self, event: Event) -> None:
        cb = self.on_event
        if cb is not None:
            try:
                cb(event)
            except Exception:  # 回调异常不允许影响设备线程
                pass

    # ---------------------------------------------------------------- 生命周期
    @abc.abstractmethod
    def start(self) -> None:
        """启动（例如开始监听 WebSocket）。"""

    @abc.abstractmethod
    def stop(self) -> None:
        """停止并确保输出归零。"""

    # ---------------------------------------------------------------- 控制
    @abc.abstractmethod
    def set_strength(self, a: int | None = None, b: int | None = None) -> None:
        """设置通道强度；None 表示该通道不变。"""

    @abc.abstractmethod
    def send_wave(self, channel: str, units: Iterable[str]) -> None:
        """向指定通道追加波形单元（8 字节 HEX 字符串）。"""

    @abc.abstractmethod
    def clear(self, channel: str) -> None:
        """清空指定通道的波形队列。"""

    # ---------------------------------------------------------------- 便捷方法
    def channels_of(self, channel: str) -> tuple[str, ...]:
        return CHANNELS if channel == "both" else (channel,)

    def mute(self) -> None:
        """立即归零并清空队列。任何异常都不允许向上抛。"""
        try:
            for ch in CHANNELS:
                self.clear(ch)
        except Exception:
            pass
        try:
            self.set_strength(0, 0)
        except Exception:
            pass

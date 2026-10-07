"""Mock 设备：不接硬件，只记录调用。用于离线自测与干跑（dry-run）。"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from ..events import DeviceState, Event
from .base import Device


@dataclass
class MockRecord:
    op: str
    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = field(default_factory=dict)


class MockDevice(Device):
    """把所有输出记到 history 里，可选打印到控制台。"""

    name = "mock"

    def __init__(self, verbose: bool = True, on_event: Callable[[Event], None] | None = None) -> None:
        super().__init__()
        self.verbose = verbose
        self.on_event = on_event
        self.log = logging.getLogger("hd2coyote.device.mock")
        self.history: list[MockRecord] = []
        self.strength: dict[str, int] = {"A": 0, "B": 0}
        self.wave_counts: dict[str, int] = {"A": 0, "B": 0}
        self._lock = threading.RLock()

    # ---------------------------------------------------------------- 生命周期
    def start(self) -> None:
        self._connected = True
        self._emit(DeviceState(connected=True, detail="mock"))

    def stop(self) -> None:
        self.mute()
        self._connected = False
        self._emit(DeviceState(connected=False, detail="mock"))

    # ---------------------------------------------------------------- 控制
    def _record(self, op: str, *args: Any) -> None:
        with self._lock:
            self.history.append(MockRecord(op, args))
            if len(self.history) > 2000:
                del self.history[:1000]
        if self.verbose:
            self.log.info("%s%s", op, args if args else "")

    def set_strength(self, a: int | None = None, b: int | None = None) -> None:
        with self._lock:
            if a is not None:
                self.strength["A"] = max(0, min(200, int(a)))
            if b is not None:
                self.strength["B"] = max(0, min(200, int(b)))
        self._record("set_strength", a, b)

    def send_wave(self, channel: str, units: Iterable[str]) -> None:
        units = list(units)
        with self._lock:
            self.wave_counts[channel] = self.wave_counts.get(channel, 0) + len(units)
        self._record("send_wave", channel, len(units))

    def clear(self, channel: str) -> None:
        self._record("clear", channel)

    # ---------------------------------------------------------------- 测试辅助
    @property
    def peak_strength(self) -> int:
        with self._lock:
            return max(self.strength.values())

    def ops(self) -> list[str]:
        with self._lock:
            return [r.op for r in self.history]

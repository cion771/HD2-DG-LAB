"""设备工厂。"""

from __future__ import annotations

import logging
from typing import Callable

from ..config import DeviceConfig
from ..events import Event
from .base import Device, Limits
from .dglab_socket import DGLabSocketDevice, cmd_clear, cmd_pulse, cmd_strength, lan_ip
from .mock import MockDevice

__all__ = [
    "Device",
    "Limits",
    "MockDevice",
    "DGLabSocketDevice",
    "create_device",
    "cmd_strength",
    "cmd_pulse",
    "cmd_clear",
    "lan_ip",
]


def create_device(
    cfg: DeviceConfig,
    on_event: Callable[[Event], None] | None = None,
    logger: logging.Logger | None = None,
) -> Device:
    if cfg.kind == "mock":
        return MockDevice(on_event=on_event)
    return DGLabSocketDevice(
        host=cfg.host,
        port=cfg.port,
        advertise_ip=cfg.advertise_ip,
        on_event=on_event,
        logger=logger,
    )

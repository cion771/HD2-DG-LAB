"""游戏事件定义。

检测层（detectors）只负责把画面变成这些事件，
规则层（rules）只负责把事件变成电击动作，
这样两边都可以脱离游戏、脱离硬件单独测试。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Channel = Literal["A", "B", "both", "alternate"]


@dataclass(slots=True)
class Event:
    """所有事件的基类。"""

    t: float = 0.0  # 事件时间戳（monotonic 秒）
    kind: str = field(default="event", init=False)


@dataclass(slots=True)
class Damage(Event):
    """血量下降。severity 为本次掉血占最大血量的百分比（0~100）。"""

    severity: float = 0.0
    hp_before: float = 1.0
    hp_after: float = 1.0
    kind: str = field(default="damage", init=False)


@dataclass(slots=True)
class LimbInjury(Event):
    """肢体损伤：slot 为损伤部位索引，bleeding 表示是否伴随流血。"""

    slot: int = 0
    bleeding: bool = False
    name: str = ""
    kind: str = field(default="limb_injury", init=False)


@dataclass(slots=True)
class Death(Event):
    """阵亡（等待增援）。"""

    kind: str = field(default="death", init=False)


@dataclass(slots=True)
class Revive(Event):
    """被增援 / 复活。"""

    kind: str = field(default="revive", init=False)


@dataclass(slots=True)
class LowHealth(Event):
    """进入低血量状态。"""

    ratio: float = 0.0
    kind: str = field(default="low_health", init=False)


@dataclass(slots=True)
class Recovered(Event):
    """脱离低血量状态。"""

    ratio: float = 0.0
    kind: str = field(default="recovered", init=False)


@dataclass(slots=True)
class FeedbackButton(Event):
    """手机 App 上的反馈按钮被按下（index 0~9）。"""

    index: int = 0
    kind: str = field(default="feedback", init=False)


@dataclass(slots=True)
class DeviceState(Event):
    """设备连接状态变化。"""

    connected: bool = False
    detail: str = ""
    kind: str = field(default="device_state", init=False)


# --------------------------------------------------------------------- 解析工具
def positive_float(value: object, default: float) -> float:
    """把来路不明的值转成有限浮点数（NaN / inf / 非数字都退回默认值）。"""
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if out != out or out in (float("inf"), float("-inf")):  # NaN / inf
        return default
    return out


def truthy(value: object) -> bool:
    """0/1、true/false、yes/no、on/off 都算数（外部工具爱用什么都有）。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return False

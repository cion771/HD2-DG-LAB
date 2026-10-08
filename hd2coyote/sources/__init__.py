"""事件源插件包。

新增一个源只需要三步：
    1. 在本包（或任何被 import 的模块）里定义 Source 的子类；
    2. 用 @register_source 装饰（类属性 name 就是配置里写的名字）；
    3. 在 config.sources.enabled 里加上这个名字。
源只允许「产生 Event」，输出的强度依旧由规则层 + SafetyGuard 说了算。
"""

from __future__ import annotations

from .base import (
    REGISTRY,
    Source,
    SourceStatus,
    available_sources,
    build_sources,
    create_source,
    register_source,
    source_catalog,
)
from .game_bridge import GameBridgeSource
from .http import HttpEventSource

__all__ = [
    "REGISTRY",
    "Source",
    "SourceStatus",
    "available_sources",
    "build_sources",
    "create_source",
    "register_source",
    "source_catalog",
    "GameBridgeSource",
    "HttpEventSource",
]

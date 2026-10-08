"""游戏内 Lua 桥事件源：把现有的 HookSource（UDP 状态流）包成插件。

行为与 0.4.x 完全一致，只是换了个位置 —— 这样「游戏桥」和其他源（HTTP 等）
在引擎里是同一种东西，网页面板也能统一列出。
"""

from __future__ import annotations

import logging
from typing import Any

from ..config import AppConfig
from ..events import Event
from ..hook import HookSource
from .base import Source, SourceStatus, register_source


@register_source
class GameBridgeSource(Source):
    """游戏内 addon 通过 UDP 上报玩家状态（推荐的主源）。"""

    name = "game_bridge"
    label = "游戏内 Lua 桥（UDP）"
    kind = "state"
    critical = True
    hint = "在游戏里装 hd2coyote 的 Lua 桥 addon：它用 FFI 只读内存，把血量/肢体/阵亡通过 UDP 发过来"

    def __init__(self, cfg: AppConfig, logger: logging.Logger | None = None) -> None:
        super().__init__(cfg, logger)
        #: 真正干活的还是老 HookSource（协议、状态机都没动）
        self.hook = HookSource(cfg, logger)

    # ------------------------------------------------------------- 委托
    @property
    def state(self) -> Any:
        return self.hook.state

    @property
    def hello(self) -> dict[str, object] | None:
        return self.hook.hello

    @property
    def alive(self) -> bool:
        return self.hook.alive

    @property
    def packets(self) -> int:
        return self.hook.state.packets

    @property
    def bad_packets(self) -> int:
        return self.hook.state.bad_packets

    def start(self) -> None:
        self.hook.start()  # OSError（端口占用）交给引擎处理
        self.started = True
        self.error = ""

    def stop(self) -> None:
        self.hook.stop()
        self.started = False

    def poll(self, now: float) -> list[Event]:
        events = self.hook.poll(now)
        self.events += len(events)
        return events

    def describe(self) -> str:
        return self.hook.describe()

    def apply_config(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self.hook.cfg = cfg
        self.hook.hook_cfg = cfg.hook
        self.hook.trackers = type(self.hook.trackers)(cfg)

    def status(self) -> SourceStatus:
        st = super().status()
        st.detail = self.describe()
        return st

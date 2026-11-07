"""文件广播事件源：读游戏内桥写出的 bridge_state.json。

为什么需要它：MDL3（3.0.0-dev.x）的 Lua 宿主调的是游戏自己的 Lua 5.1，
不保证有 ffi —— 没有 ffi 就拿不到 ws2_32，也发不了 UDP。桥在这种情况下把
**同一条 JSON 报文**写到 %LOCALAPPDATA%\\hd2coyote\\bridge_state.json，
这个源按行读、复用 HookSource 的解析与状态机，所以事件判定只有一份代码。

与 game_bridge（UDP）二选一即可；两者同时启用时哪条先到算哪条。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from ..config import AppConfig
from ..events import Event
from ..hook import HookSource
from .base import Source, SourceStatus, register_source

STATE_FILE = "bridge_state.json"
POLL_S = 0.05


def state_path() -> Path:
    """桥写状态的地方：优先 HD2COYOTE_DIR（诊断/测试），否则 %LOCALAPPDATA%\\hd2coyote。"""
    env = os.environ.get("HD2COYOTE_DIR")
    if env:
        return Path(env) / STATE_FILE
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(base) / "hd2coyote" / STATE_FILE


@register_source
class BridgeFileSource(Source):
    """MDL3 宿主没有 ffi 时，桥把状态写到文件，这个源轮询它。"""

    name = "bridge_file"
    label = "游戏内 Lua 桥（文件广播）"
    kind = "state"
    critical = True
    hint = ("MDL3 宿主没有 ffi 时桥会自动改用文件广播："
            "把 %LOCALAPPDATA%\\hd2coyote\\bridge_state.json 交给这个源读")

    def __init__(self, cfg: AppConfig, logger: logging.Logger | None = None) -> None:
        super().__init__(cfg, logger)
        #: 解析与事件判定复用 UDP 那条路的实现，绝不复制第二份状态机
        self.hook = HookSource(cfg, logger)
        self.path = state_path()
        self._last_text: str | None = None
        self._last_read = 0.0

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
        self.started = True
        self.error = ""
        self.log.info("文件广播源已启用：%s", self.path)

    def stop(self) -> None:
        self.started = False

    # ------------------------------------------------------------- 轮询
    def _read_text(self) -> str | None:
        """读不到就返回 None，不抛错：桥正在写文件时 Windows 会短暂拒绝共享读。"""
        try:
            return self.path.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeDecodeError):
            return None

    def poll(self, now: float) -> list[Event]:
        if now - self._last_read >= POLL_S:
            self._last_read = now
            text = self._read_text()
            # 内容没变就不重复计包；用内容而不是 mtime，
            # 因为 NTFS 时间戳精度、写锁、以及"长度相同的两条报文"都会让 mtime 判据失真。
            if text is not None and text.strip() and text != self._last_text:
                self._last_text = text
                line = text.strip().splitlines()[-1]
                packet = self.hook._decode(line.encode("utf-8"))
                if packet is not None:
                    self.hook.feed(packet, now)
                else:
                    # 半个 JSON 就当掉包：和 UDP 坏包走同一条路，不推进状态
                    self.hook.state.bad_packets += 1
        events = self.hook.poll(now)
        self.events += len(events)
        return events

    def describe(self) -> str:
        age = self.hook.state.age
        where = "已收到状态" if self.hook.alive else "等待状态"
        return f"{self.path}（{where}，延迟 {age * 1000:.0f} ms）" if self.hook.state.received else \
               f"{self.path}（尚未收到状态）"

    def apply_config(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self.hook.cfg = cfg
        self.hook.hook_cfg = cfg.hook
        self.hook.trackers = type(self.hook.trackers)(cfg)

    def status(self) -> SourceStatus:
        st = super().status()
        st.detail = self.describe()
        return st

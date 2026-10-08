"""事件源（插件）层的公共设施：Source 基类 + 注册表。

设计要点：
  * 一个源只做一件事：把外部世界翻译成 Event 列表。谁产生事件不重要。
  * 所有事件都必须经过 RuleEngine → SafetyGuard —— 多接一个源**不会**绕过
    急停、强度上限、会话预算。这也是我们敢开放 HTTP 源的原因。
  * 源可以声明 critical = True：它「断流」就说明玩家状态已经不可信，引擎会
    清掉正在输出的效果（游戏内桥就是这种）。事件型的源（HTTP 收到才动）默认
    不是 critical，否则没人发事件时会被误判成断流。
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any

from ..config import AppConfig
from ..events import Event


@dataclass
class SourceStatus:
    """一个源的运行时快照（网页诊断面板直接用）。"""

    name: str = ""
    label: str = ""
    kind: str = "event"
    started: bool = False
    alive: bool = False
    critical: bool = False
    detail: str = ""
    events: int = 0
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class Source:
    """事件源基类。子类至少要实现 start / stop / poll。"""

    #: 注册名（config.sources.enabled 里写的名字）
    name: str = "source"
    #: 界面上显示的中文名
    label: str = "事件源"
    #: state / event / state+event，纯展示用途
    kind: str = "event"
    #: 断流是否代表状态不可信（True 会被引擎当作 dead-man）
    critical: bool = False
    #: 面板上的提示（怎么用、需要配什么）
    hint: str = ""

    def __init__(self, cfg: AppConfig, logger: logging.Logger | None = None) -> None:
        self.cfg = cfg
        self.log = logger or logging.getLogger(f"hd2coyote.sources.{self.name}")
        self.started = False
        self.events = 0
        self.error = ""

    # ------------------------------------------------------------- 生命周期
    def start(self) -> None:
        """启动（可能抛 OSError：端口占用等）。"""
        self.started = True

    def stop(self) -> None:
        self.started = False

    # ------------------------------------------------------------- 取事件
    def poll(self, now: float) -> list[Event]:
        """返回自上次调用以来产生的事件（引擎每帧调用一次）。"""
        return []

    @property
    def alive(self) -> bool:
        return self.started

    def critical_now(self) -> bool:
        """运行时的「断流算不算状态不可信」。默认就是类属性 critical。

        动态的源（HTTP 源只有在收到过状态包之后才算状态源）重写这个，
        而类属性 critical 保留静态语义，供网页的源清单使用。
        """
        return bool(type(self).critical)

    def describe(self) -> str:
        return self.label

    def apply_config(self, cfg: AppConfig) -> None:
        """热更新配置（不能热更的源应当在引擎里触发重建）。"""
        self.cfg = cfg

    # ------------------------------------------------------------- 展示
    def status(self) -> SourceStatus:
        return SourceStatus(
            name=self.name, label=self.label, kind=self.kind,
            started=self.started, alive=bool(self.alive), critical=bool(self.critical_now()),
            detail=self.describe(), events=self.events, error=self.error,
        )


#: 注册表：名字 -> 源类型
REGISTRY: dict[str, type[Source]] = {}


def register_source(cls: type[Source]) -> type[Source]:
    """把一个源类登记进注册表（用类属性 name 当键）。"""
    if not getattr(cls, "name", "") or cls.name == "source":
        raise ValueError(f"{cls.__name__} 没有设置 name")
    REGISTRY[cls.name] = cls
    return cls


def available_sources() -> list[str]:
    return sorted(REGISTRY)


def source_catalog() -> list[dict[str, Any]]:
    """给网页/CLI 看的源清单（不需要先实例化）。"""
    return [
        {"name": cls.name, "label": cls.label, "kind": cls.kind,
         "critical": bool(cls.critical), "hint": cls.hint}
        for cls in sorted(REGISTRY.values(), key=lambda c: c.name)
    ]


def create_source(name: str, cfg: AppConfig, logger: logging.Logger | None = None) -> Source:
    cls = REGISTRY.get(name)
    if cls is None:
        raise KeyError(f"未知事件源：{name}（可用：{', '.join(available_sources()) or '无'}）")
    return cls(cfg, logger)


def build_sources(cfg: AppConfig, logger: logging.Logger | None = None) -> list[Source]:
    """按 config.sources.enabled 的顺序建源；未知名字只告警、不抛异常。"""
    log = logger or logging.getLogger("hd2coyote.sources")
    out: list[Source] = []
    seen: set[str] = set()
    for name in cfg.sources.enabled:
        if name in seen:
            continue
        seen.add(name)
        try:
            out.append(create_source(name, cfg, log))
        except KeyError:
            log.warning("配置里有未知事件源「%s」，已忽略（可用：%s）",
                        name, ", ".join(available_sources()) or "无")
    return out

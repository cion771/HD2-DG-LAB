"""事件 → 电击动作 的映射（规则层）。

这一层不碰画面、不碰硬件，只做算术，因此可以完全离线单测。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from . import wave_lib, waves
from .config import AppConfig, RuleConfig
from .events import Death, Damage, Event, LimbInjury, LowHealth, Revive


@dataclass(slots=True)
class Action:
    """一次要输出的电击动作。pct 是「相对通道上限的百分比」。"""

    name: str
    pct: float
    units: list[str]
    duration_ms: float
    attack_ms: float
    release_ms: float
    channel: str = "A"  # "A" | "B" | "both"
    priority: int = 1
    repeat_ms: float = 0.0
    period_ms: float = 0.0
    created: float = field(default_factory=time.monotonic)

    @property
    def key(self) -> str:
        return f"{self.name}#{id(self)}"


class RuleEngine:
    """把事件翻译成 Action，并负责冷却与通道选择。"""

    def __init__(self, cfg: AppConfig, logger: logging.Logger | None = None) -> None:
        self.cfg = cfg
        self.log = logger or logging.getLogger("hd2coyote.rules")
        self._last: dict[str, float] = {}
        self._alternate = 0

    # ------------------------------------------------------------------ API
    def actions(self, event: Event, now: float | None = None) -> list[Action]:
        now = time.monotonic() if now is None else now
        if isinstance(event, Damage):
            return self._damage(event, now)
        if isinstance(event, LimbInjury):
            return self._limb(event, now)
        if isinstance(event, Death):
            return self._simple("death", event, now)
        if isinstance(event, LowHealth):
            return self._simple("low_health", event, now)
        if isinstance(event, Revive):
            # 复活不做惩罚（需要「复活奖励」可自行加规则）
            return []
        return []

    def rule(self, name: str) -> RuleConfig:
        return self.cfg.rules.get(name, RuleConfig(enabled=False))

    def reset_cooldowns(self) -> None:
        """清空冷却记录（重新武装后应当可以立刻再次触发）。"""
        self._last.clear()

    # ------------------------------------------------------------- internal
    def _cooldown_ok(self, key: str, ms: float, now: float) -> bool:
        last = self._last.get(key)
        if last is not None and (now - last) * 1000.0 < ms:
            return False
        self._last[key] = now
        return True

    def _channel(self, rule: RuleConfig) -> str:
        ch = rule.channel
        if ch == "default":
            return self.cfg.device.channel
        if ch == "alternate":
            self._alternate ^= 1
            return "A" if self._alternate else "B"
        return ch

    def _make(self, name: str, rule: RuleConfig, pct: float, duration_ms: float,
              channel: str | None = None) -> Action:
        duration_ms = max(100.0, min(duration_ms, self.cfg.safety.max_event_ms))
        units = self._units(name, rule, duration_ms)
        return Action(
            name=name,
            pct=max(0.0, pct),
            units=units,
            duration_ms=duration_ms,
            attack_ms=self.cfg.safety.attack_ms,
            release_ms=self.cfg.safety.release_ms,
            channel=channel or self._channel(rule),
            priority=rule.priority,
            repeat_ms=rule.repeat_ms,
            period_ms=rule.period_ms,
        )

    def _units(self, name: str, rule: RuleConfig, duration_ms: float) -> list[str]:
        """波形来源：命名波形库优先（用户可在网页里编辑），其次内置预设。"""
        units = wave_lib.resolve(self.cfg, rule.wave, duration_ms, peak=100.0, freq=rule.freq)
        if units:
            return units
        try:
            return waves.build(rule.wave, duration_ms, peak=100.0, freq=rule.freq)
        except KeyError:
            self.log.warning("规则 %s 的波形 %r 不存在（波形库里没有、内置预设也没有），退回 pinch",
                             name, rule.wave)
            return waves.build("pinch", duration_ms, peak=100.0, freq=rule.freq)

    def _damage(self, ev: Damage, now: float) -> list[Action]:
        rule = self.rule("damage")
        if not rule.enabled or ev.severity < self.cfg.detect.damage_min_pct:
            return []
        if not self._cooldown_ok("damage", rule.cooldown_ms, now):
            self.log.debug("受伤规则冷却中（%.0fms），跳过", rule.cooldown_ms)
            return []
        pct = rule.base_pct + rule.per_10hp * (ev.severity / 10.0)
        return [self._make("damage", rule, pct, rule.duration_ms)]

    def _limb(self, ev: LimbInjury, now: float) -> list[Action]:
        rule = self.rule("limb_injury")
        if not rule.enabled:
            return []
        if not self._cooldown_ok(f"limb_injury:{ev.slot}", rule.cooldown_ms, now):
            self.log.debug("肢体损伤规则冷却中（%.0fms），跳过", rule.cooldown_ms)
            return []
        pct = rule.base_pct * (1.2 if ev.bleeding else 1.0)
        return [self._make("limb_injury", rule, pct, rule.duration_ms)]

    def _simple(self, name: str, ev: Event, now: float) -> list[Action]:
        rule = self.rule(name)
        if not rule.enabled:
            return []
        if not self._cooldown_ok(name, rule.cooldown_ms, now):
            return []
        return [self._make(name, rule, rule.base_pct, rule.duration_ms)]

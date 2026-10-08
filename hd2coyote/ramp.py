"""惩罚累积模型：伤害会攒起来，血越少越强，长时间没挨打慢慢回落。

灵感来自 DG-Lab-Punishment（它的 CS2 插件用 `ceil((100-hp)/100 * 上限)` 把血量
映射成强度），但这里**只算「意愿」**：结果会加到动作的 pct 上，之后照样要过
SafetyGuard.strength_for()，所以 safety.max_pct / max_absolute / 急停一个都不会被绕过。

三个来源合成一个 bonus：
    累积项  —— 每个伤害/损伤事件 +per_event，封顶 ceiling_pct
    血量项  —— hp_missing_pct ×（1 - 血量比例）
    回落    —— 超过 decay_after_s 没有新事件后，按 decay_per_s 每秒衰减累积项
阵亡 / 被增援（reset_on_death）直接把累积项清零。
"""

from __future__ import annotations

import time

from .config import RampConfig
from .events import Damage, Death, Event, LimbInjury, LowHealth, Revive


def rule_name(event: Event) -> str:
    """事件对应的规则名（和 config.rules 的键一致）。"""
    if isinstance(event, Damage):
        return "damage"
    if isinstance(event, LimbInjury):
        return "limb_injury"
    if isinstance(event, Death):
        return "death"
    if isinstance(event, Revive):
        return "revive"
    if isinstance(event, LowHealth):
        return "low_health"
    return str(getattr(event, "kind", ""))


class PunishmentRamp:
    """累积状态机（纯计算，可离线单测）。"""

    def __init__(self, cfg: RampConfig) -> None:
        self.cfg = cfg
        self._acc = 0.0
        self._hp: float | None = None
        self._last_event = 0.0
        self._last_tick = time.monotonic()
        self.events = 0  # 统计：累计加过的次数
        self.resets = 0

    # ------------------------------------------------------------------ 输入
    def reset(self, now: float | None = None) -> None:
        """清零（重新武装 / 换局时调用）。"""
        self._acc = 0.0
        self._last_event = time.monotonic() if now is None else now

    def note_hp(self, hp: float | None) -> None:
        """记住当前血量比例（None = 不知道，血量项按 0 计）。"""
        if hp is None:
            self._hp = None
            return
        try:
            self._hp = max(0.0, min(1.0, float(hp)))
        except (TypeError, ValueError):
            self._hp = None

    def on_event(self, event: Event, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self._last_tick = now
        if not self.cfg.enabled:
            return
        if isinstance(event, (Death, Revive)) and self.cfg.reset_on_death:
            if self._acc > 0:
                self.resets += 1
            self._acc = 0.0
            return
        if rule_name(event) not in set(self.cfg.apply_to or []):
            return
        cap = max(0.0, float(self.cfg.ceiling_pct))
        self._acc = min(cap, self._acc + max(0.0, float(self.cfg.per_event)))
        self._last_event = now
        self.events += 1

    def tick(self, now: float | None = None) -> None:
        """推进回落（引擎每帧调用）。"""
        now = time.monotonic() if now is None else now
        dt = max(0.0, now - self._last_tick)
        self._last_tick = now
        if not self.cfg.enabled or dt <= 0 or not self._last_event:
            return
        if now - self._last_event < max(0.0, float(self.cfg.decay_after_s)):
            return
        self._acc = max(0.0, self._acc - max(0.0, float(self.cfg.decay_per_s)) * dt)

    # ------------------------------------------------------------------ 输出
    @property
    def accumulated(self) -> float:
        return self._acc

    @property
    def hp_component(self) -> float:
        if self._hp is None:
            return 0.0
        return max(0.0, float(self.cfg.hp_missing_pct)) * (1.0 - self._hp)

    def bonus(self) -> float:
        """当前应当加在动作强度上的百分点（未启用时恒为 0）。"""
        if not self.cfg.enabled:
            return 0.0
        cap = max(0.0, float(self.cfg.ceiling_pct))
        return min(cap, self._acc + self.hp_component)

    def describe(self) -> str:
        if not self.cfg.enabled:
            return "关闭"
        return (f"累积 {self._acc:.1f}% + 血量 {self.hp_component:.1f}% "
                f"= +{self.bonus():.1f}%（上限 {float(self.cfg.ceiling_pct):.0f}%）")

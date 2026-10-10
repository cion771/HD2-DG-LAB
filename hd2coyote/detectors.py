"""桥接/HTTP 状态转事件：阈值、边沿、防抖与复活保护，不读取图像。"""

from __future__ import annotations

from .config import AppConfig, DetectConfig
from .events import Damage, Death, Event, LimbInjury, LowHealth, Recovered, Revive


class HealthTracker:
    """把血量状态变成伤害 / 低血量事件。"""

    def __init__(self, cfg: DetectConfig) -> None:
        self.cfg = cfg
        self._prev: float | None = None
        self._pending = 0.0
        self._low = False
        self._ignore_until = 0.0

    def reset(self) -> None:
        self._prev = None
        self._pending = 0.0
        self._low = False

    def ignore_damage_for(self, seconds: float, now: float) -> None:
        self._ignore_until = now + max(0.0, seconds)

    def update(self, hp: float | None, now: float) -> list[Event]:
        if hp is None:
            return []
        events: list[Event] = []
        if self._prev is None:
            self._prev = hp
            return events
        delta = self._prev - hp
        if delta > 0:
            self._pending += delta
        else:
            self._pending = 0.0
        if self._pending * 100.0 >= self.cfg.damage_min_pct and now >= self._ignore_until:
            events.append(Damage(t=now, severity=self._pending * 100.0,
                                 hp_before=self._prev, hp_after=hp))
            self._pending = 0.0
        pct = hp * 100.0
        if not self._low and hp > 0.02 and pct <= self.cfg.low_health_pct:
            self._low = True
            events.append(LowHealth(t=now, ratio=hp))
        elif self._low and pct >= self.cfg.recover_pct:
            self._low = False
            events.append(Recovered(t=now, ratio=hp))
        self._prev = hp
        return events


class InjuryTracker:
    """把肢体状态的「上升沿」变成肢体损伤事件。"""

    def __init__(self, cfg: DetectConfig, slots: int, names: list[str] | None = None) -> None:
        self.cfg = cfg
        self.slots = max(1, slots)
        self.names = list(names or [])
        self._active = [False] * self.slots
        self._last_hit = [0.0] * self.slots

    def update(self, scores: list[float], bleeding: float, now: float) -> list[Event]:
        events: list[Event] = []
        is_bleeding = bleeding >= self.cfg.bleeding_min_fraction
        for i in range(min(self.slots, len(scores))):
            hit = scores[i] >= self.cfg.injury_min_fraction
            if hit:
                self._last_hit[i] = now
                if not self._active[i]:
                    self._active[i] = True
                    name = self.names[i] if i < len(self.names) else f"槽位{i}"
                    events.append(LimbInjury(t=now, slot=i, bleeding=is_bleeding, name=name))
            elif self._active[i] and (now - self._last_hit[i]) >= self.cfg.injury_clear_s:
                self._active[i] = False
        return events

    def clear(self) -> None:
        self._active = [False] * self.slots


class DeathTracker:
    """血量为零 + 阵亡状态 -> 阵亡 / 复活。"""

    def __init__(self, cfg: DetectConfig) -> None:
        self.cfg = cfg
        self.dead = False
        self._since: float | None = None

    def update(self, hp: float | None, death_score: float, now: float, threshold: float) -> list[Event]:
        events: list[Event] = []
        score_hit = death_score >= threshold
        hp_hit = hp is not None and hp <= 0.02
        if not self.dead:
            if score_hit or hp_hit:
                if self._since is None:
                    self._since = now
                if now - self._since >= self.cfg.dead_hold_s:
                    self.dead = True
                    self._since = None
                    events.append(Death(t=now))
            else:
                self._since = None
        else:
            alive = (hp >= 0.15) if hp is not None else (death_score < threshold * 0.8)
            if alive:
                self.dead = False
                events.append(Revive(t=now))
        return events


# --------------------------------------------------------------------- 组合
class EventTrackers:
    """把「血量 + 损伤 + 阵亡」读数变成事件。

    游戏桥（HookSource）和 HTTP 状态源共用这一份逻辑。
    """

    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self.slots = max(1, int(cfg.hud.injury_slots))
        self._build()

    def _build(self) -> None:
        self.health = HealthTracker(self.cfg.detect)
        self.injury = InjuryTracker(self.cfg.detect, self.slots)
        self.death = DeathTracker(self.cfg.detect)

    def reset(self) -> None:
        self._build()

    @property
    def dead(self) -> bool:
        return self.death.dead

    def process(
        self,
        hp: float | None,
        injury_scores: list[float],
        bleeding: float,
        death_score: float,
        now: float,
        threshold: float | None = None,
        names: list[str] | None = None,
    ) -> list[Event]:
        threshold = (self.cfg.detect.death_state_threshold if threshold is None
                     else threshold)
        if names:
            self.injury.names = list(names)

        events: list[Event] = []
        death_events = self.death.update(hp, death_score, now, threshold)
        events.extend(death_events)

        health_events = self.health.update(hp, now)
        if self.death.dead:
            # 阵亡瞬间血量会掉到 0，不该再当成一次「伤害」
            health_events = [e for e in health_events if not isinstance(e, Damage)]
        events.extend(health_events)

        for ev in death_events:
            if isinstance(ev, Revive):
                self.health.ignore_damage_for(self.cfg.detect.revive_ignore_damage_s, now)
                self.injury.clear()

        events.extend(self.injury.update(injury_scores, bleeding, now))
        return events

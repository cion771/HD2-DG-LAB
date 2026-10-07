"""HUD 检测：血量 / 肢体损伤 / 阵亡。

分工：
  * 纯函数（bar_fill_ratio / injury_scores / template_score）负责「读画面」；
  * Tracker 类负责「读时间」——边沿检测、防抖、阈值迟滞，全是纯 Python，方便单测。

只要不带游戏，也能用 simulate.py 生成合成画面来验证整条链路。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .config import AppConfig, Box, DetectConfig
from .events import Damage, Death, Event, LimbInjury, LowHealth, Recovered, Revive
from .hud import HudLayout, crop, gray


@dataclass
class HudSample:
    """一次画面分析的结果（UI 与日志都看它）。"""

    t: float = 0.0
    hp: float | None = None
    injury: list[float] = field(default_factory=list)
    bleeding: float = 0.0
    death_score: float = 0.0
    note: str = ""


# --------------------------------------------------------------------- 像素层
def bar_fill_ratio(frame: np.ndarray, box: Box | None, margin: int = 1) -> float | None:
    """血条填充比例 0~1。

    做法：取血条中间几行，逐列取最亮值；明显亮于峰值的列算「已填充」。
    空条 => 接近 0；满血 => 接近 1。
    """
    sub = crop(frame, box)
    if sub is None or sub.size == 0 or box is None:
        return None
    if box.w > margin * 2 + 2:
        sub = sub[:, margin:sub.shape[1] - margin]
    g = gray(sub)
    if g.shape[0] >= 4:
        y0 = g.shape[0] // 4
        y1 = max(y0 + 1, g.shape[0] * 3 // 4)
        g = g[y0:y1]
    col = g.max(axis=0)
    peak = float(col.max())
    # 没有明显「亮填充」时视为空条：空条内部是半透明深色（约 40~80）
    if peak < 110.0:
        return 0.0
    thr = max(60.0, peak * 0.55)
    return float(np.count_nonzero(col > thr) / max(1, col.size))


def injury_scores(frame: np.ndarray, zone: Box | None, slots: int) -> tuple[list[float], float]:
    """把损伤图标条横向切成 slots 格，返回每格「橙红色像素占比」与整体「血迹红」占比。"""
    if slots < 1:
        slots = 1
    scores = [0.0] * slots
    sub = crop(frame, zone)
    if sub is None or sub.size == 0:
        return scores, 0.0
    arr = sub[..., :3].astype(np.int16)
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    warm = (r > 115) & ((r - g) > 45) & ((r - b) > 45)  # 橙 / 红（受伤部位高亮）
    blood = (r > 100) & (g < 80) & (b < 80) & ((r - g) > 55)  # 深红（流血）
    w = arr.shape[1]
    for i in range(slots):
        x0 = int(w * i / slots)
        x1 = max(x0 + 1, int(w * (i + 1) / slots))
        seg = warm[:, x0:x1]
        scores[i] = float(seg.mean()) if seg.size else 0.0
    return scores, float(blood.mean())


def load_template(path: str | Path) -> np.ndarray | None:
    """读取阵亡模板（PNG/JPG），转成灰度 float32。"""
    p = Path(path)
    if not p.exists():
        return None
    from PIL import Image

    img = Image.open(p).convert("L")
    return np.asarray(img, dtype=np.float32)


def _resize_nearest(img: np.ndarray, h: int, w: int) -> np.ndarray:
    ys = np.clip((np.arange(h) * img.shape[0] / max(1, h)).astype(int), 0, img.shape[0] - 1)
    xs = np.clip((np.arange(w) * img.shape[1] / max(1, w)).astype(int), 0, img.shape[1] - 1)
    return img[ys][:, xs]


def _ncc(a: np.ndarray, b: np.ndarray) -> float:
    a = a - a.mean()
    b = b - b.mean()
    denom = float(np.sqrt((a * a).sum()) * np.sqrt((b * b).sum()))
    if denom < 1e-6:
        return 0.0
    return float((a * b).sum() / denom)


def template_score(frame: np.ndarray, box: Box | None, template: np.ndarray | None) -> float:
    """模板匹配相似度（-1~1）。box 与模板同一位置、同一块区域。"""
    if template is None:
        return 0.0
    sub = crop(frame, box)
    if sub is None or sub.size == 0:
        return 0.0
    g = gray(sub)
    if g.shape != template.shape:
        g = _resize_nearest(g, template.shape[0], template.shape[1])
    return _ncc(g, template)


# --------------------------------------------------------------------- 时间层
class HealthTracker:
    """把血条读数变成伤害 / 低血量事件。"""

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
    """把损伤图标读数的「上升沿」变成肢体损伤事件。"""

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
    """血条清空 + 模板匹配 -> 阵亡 / 复活。"""

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

    视觉检测（Detector）和游戏内 hook（HookSource）共用这一份逻辑，
    保证两条数据源的判定语义完全一致，也只需要维护一套状态机。
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
        threshold = (self.cfg.detect.death_template_threshold if threshold is None
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


class Detector:
    """一次 process() = 一帧画面 -> 一组事件。"""

    def __init__(self, cfg: AppConfig, logger: logging.Logger | None = None) -> None:
        self.cfg = cfg
        self.log = logger or logging.getLogger("hd2coyote.detector")
        self.layout = HudLayout.from_config(cfg.hud)
        self.detect = cfg.detect
        self.trackers = EventTrackers(cfg)
        self.template: np.ndarray | None = None
        self.sample = HudSample()
        self._warned = False
        self._load_template()

    # ---------------------------------------------------------------- 配置
    def apply_config(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self.detect = cfg.detect
        self.layout = HudLayout.from_config(cfg.hud)
        self.trackers = EventTrackers(cfg)
        self._load_template()

    def _load_template(self) -> None:
        self.template = None
        path = self.layout.death_template
        if path:
            self.template = load_template(path)
            if self.template is None:
                self.log.warning("阵亡模板读取失败：%s", path)

    # ---------------------------------------------------------------- 主流程
    def process(self, frame: np.ndarray, now: float | None = None) -> list[Event]:
        now = time.monotonic() if now is None else now
        hp = self._hp_ratio(frame)
        scores, bleeding = injury_scores(frame, self.layout.injury_zone, self.layout.injury_slots)
        death_score = template_score(frame, self.layout.death_probe, self.template)

        events = self.trackers.process(
            hp, scores, bleeding, death_score, now,
            threshold=self.detect.death_template_threshold,
            names=self.layout.injury_slot_names,
        )

        self.sample = HudSample(t=now, hp=hp, injury=scores, bleeding=bleeding,
                                death_score=death_score)
        if not self.layout.calibrated and not self._warned:
            self.log.warning("血条区域尚未标定：请在界面上点「标定血条」，否则无法检测伤害/阵亡")
            self._warned = True
        return events

    # ---------------------------------------------------------------- 细节
    @property
    def health(self) -> HealthTracker:
        return self.trackers.health

    @property
    def injury(self) -> InjuryTracker:
        return self.trackers.injury

    @property
    def death(self) -> DeathTracker:
        return self.trackers.death

    def _hp_ratio(self, frame: np.ndarray) -> float | None:
        return bar_fill_ratio(frame, self.layout.hp_bar)

    def describe(self) -> str:
        s = self.sample
        hp = "--" if s.hp is None else f"{s.hp * 100:5.1f}%"
        inj = ",".join(f"{v:.3f}" for v in s.injury) or "-"
        return (f"HP={hp} 损伤=[{inj}] 血迹={s.bleeding:.3f} "
                f"阵亡分={s.death_score:.2f} 状态={'阵亡' if self.death.dead else '存活'}")

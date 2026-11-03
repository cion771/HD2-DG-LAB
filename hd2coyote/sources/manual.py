"""手动注入用的 JSON → 事件解析。

这里只留「把一段 JSON 翻译成事件」的纯逻辑，不监听端口、不限速、不校验令牌 ——
它曾经长在 HTTP 事件源里，那个源已经移除，而网页的「注入一个事件」还需要同一套
映射，所以把解析单独留一份，避免网页和状态机各写一套跑偏。

    {"ev":"state","hp":62,"hp_max":100,"limbs":[0,1,0],"bleeding":0,"dead":0}
    {"ev":"damage","severity":12.5}
    {"ev":"limb","slot":1,"bleeding":true,"name":"左腿"}
    {"ev":"death"}  {"ev":"revive"}  {"ev":"low_health","ratio":0.3}

解析出来的事件照样要过规则层与安全层，上限、急停、会话预算一个都不少。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ..config import AppConfig
from ..detectors import EventTrackers
from ..events import (
    Damage,
    Death,
    Event,
    LimbInjury,
    LowHealth,
    Revive,
    positive_float,
    truthy,
)

#: parse_event 认识的事件名（网页「手动注入」用它判断是不是在瞎按）
EVENT_KINDS = frozenset({
    "damage", "hit", "hurt", "受伤", "掉血",
    "limb", "limb_injury", "injury", "肢体",
    "death", "dead", "阵亡",
    "revive", "respawn", "复活", "增援",
    "low_health", "lowhp", "低血量",
})
#: 状态包的特征字段（只要有这些，就是「状态」而不是「事件」）
STATE_KEYS = ("hp", "limbs", "dead", "bleeding")
#: 显式声明成状态包的 kind
STATE_KINDS = frozenset({"", "state", "status", "player"})


def looks_known(payload: Any) -> bool:
    """这个 JSON 是不是「我们能理解的东西」（事件名认识，或者带状态字段）。"""
    if not isinstance(payload, dict):
        return False
    if any(key in payload for key in STATE_KEYS):
        return True
    kind = str(payload.get("ev") or payload.get("event") or payload.get("kind") or "").strip().lower()
    if not kind:
        return False
    return kind in EVENT_KINDS or kind in STATE_KINDS


def parse_event(payload: dict[str, Any], now: float | None = None) -> Event | None:
    """把「事件包」JSON 变成事件（不认识就返回 None）。"""
    if not isinstance(payload, dict):
        return None
    now = time.monotonic() if now is None else now
    kind = str(payload.get("ev") or payload.get("event") or payload.get("kind") or "").strip().lower()
    if kind in ("damage", "hit", "hurt", "受伤", "掉血"):
        return Damage(
            t=now,
            severity=max(0.0, positive_float(payload.get("severity",
                                                      payload.get("damage",
                                                                  payload.get("pct"))), 0.0)),
            hp_before=positive_float(payload.get("hp_before"), 1.0),
            hp_after=positive_float(payload.get("hp_after"), 1.0),
        )
    if kind in ("limb", "limb_injury", "injury", "肢体"):
        return LimbInjury(
            t=now,
            slot=int(positive_float(payload.get("slot"), 0.0)),
            bleeding=truthy(payload.get("bleeding")),
            name=str(payload.get("name") or ""),
        )
    if kind in ("death", "dead", "阵亡"):
        return Death(t=now)
    if kind in ("revive", "respawn", "复活", "增援"):
        return Revive(t=now)
    if kind in ("low_health", "lowhp", "低血量"):
        return LowHealth(t=now,
                         ratio=max(0.0, min(1.0, positive_float(payload.get("ratio"), 0.0))))
    return None


def hp_ratio(payload: dict[str, Any]) -> float | None:
    """把各种写法的血量统一成 0~1 比例。

    给了 hp_max 就按「绝对值」算；只给 hp 时，<=1 视为比例、>1 视为百分制。
    """
    raw = payload.get("hp")
    if raw is None:
        return None
    value = positive_float(raw, -1.0)
    if value < 0:
        return None
    hp_max = positive_float(payload.get("hp_max"), 0.0)
    if hp_max > 0:
        return max(0.0, min(1.0, value / hp_max))
    return max(0.0, min(1.0, value if value <= 1.0 else value / 100.0))


class ManualEventParser:
    """网页「注入一个事件」用的解析器：只解析，不监听。

    状态包交给和游戏桥同一套 `EventTrackers`，所以手动注入的伤害/阵亡判定
    与真实数据走同一条判定路径，不会出现两套阈值。
    """

    def __init__(self, cfg: AppConfig, logger: logging.Logger | None = None) -> None:
        self.cfg = cfg
        self.log = logger or logging.getLogger("hd2coyote.manual")
        self.trackers = EventTrackers(cfg)

    def ingest(self, payload: dict[str, Any], now: float | None = None) -> list[Event]:
        now = time.monotonic() if now is None else now
        if not isinstance(payload, dict):
            return []
        kind = str(payload.get("ev") or payload.get("event") or payload.get("kind") or "").strip().lower()
        has_state = any(k in payload for k in STATE_KEYS)
        if kind in STATE_KINDS and has_state:
            return self._state_events(payload, now)
        parsed = parse_event(payload, now)
        if parsed is None:
            self.log.debug("手动注入收到不认识的内容：%s", kind or payload)
            return []
        return [parsed]

    def _state_events(self, payload: dict[str, Any], now: float) -> list[Event]:
        hp = hp_ratio(payload)
        limbs = payload.get("limbs") or []
        if isinstance(limbs, dict):  # 允许 {"0":0,"1":1} 这种写法
            limbs = [limbs[k] for k in sorted(limbs, key=lambda x: str(x))]
        scores = [1.0 if truthy(v) else 0.0 for v in limbs][:8] if isinstance(limbs, (list, tuple)) else []
        dead = truthy(payload.get("dead"))
        return self.trackers.process(
            hp, scores, 1.0 if truthy(payload.get("bleeding")) else 0.0,
            1.0 if dead else 0.0, now,
            threshold=self.cfg.detect.death_state_threshold,
            names=self.cfg.hud.injury_slot_names,
        )

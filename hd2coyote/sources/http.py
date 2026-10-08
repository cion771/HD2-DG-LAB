"""HTTP 事件源：任何能发 HTTP 的程序都能给它当传感器。

为什么要有它：游戏内 Lua 桥只覆盖「绝地潜兵 2 + 我们自己的 addon」这一条路。
想让别的游戏、直播工具、主播面板、甚至手机上的快捷指令也能触发同一套电击规则，
最省事的接口就是 HTTP —— 一个 POST 一行 JSON 就行。

    GET  /health                     存活探测（返回统计）
    POST /event  （也接受 / 和 /state）
        状态包（推荐）：和游戏内桥同一种状态，交给**同一套**状态机判定事件
            {"ev":"state","hp":62,"hp_max":100,"limbs":[0,1,0],"bleeding":0,"dead":0}
        事件包（直接指定事件，跳过判定）
            {"ev":"damage","severity":12.5,"hp_before":100,"hp_after":87.5}
            {"ev":"limb","slot":1,"bleeding":true,"name":"左腿"}
            {"ev":"death"}  {"ev":"revive"}  {"ev":"low_health","ratio":0.3}
    一次可以发数组（批量）。

安全：
  * 默认只监听 127.0.0.1；要跨机用请自己改 sources.http_host（并务必设 http_token）；
  * sources.http_token 非空时，要求 `X-HD2Coyote-Token` 头（或 `?token=`）；
  * 有限速（sources.http_max_per_s，默认 20/s），超了直接 429，别让外部脚本刷爆电极；
  * 这个源产生的事件照样要过规则层与 SafetyGuard，强度上限/急停/会话预算一个都不少。
"""

from __future__ import annotations

import hmac
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .. import __version__
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
from .base import Source, register_source

MAX_BODY = 64 * 1024  # 一个上报包最大 64KB（批量事件也够）
MAX_QUEUE = 256  # 引擎还没取走时最多攒这么多事件

USAGE = {
    "service": "hd2coyote-http",
    "protocol": 1,
    "endpoints": {
        "GET /health": "存活探测 + 统计",
        "POST /event": "上报状态包或事件包（也接受 / 与 /state）",
    },
    "state_example": {"ev": "state", "hp": 62, "hp_max": 100, "limbs": [0, 1, 0],
                      "bleeding": 0, "dead": 0},
    "event_examples": [
        {"ev": "damage", "severity": 12.5, "hp_before": 100, "hp_after": 87.5},
        {"ev": "limb", "slot": 1, "bleeding": True, "name": "左腿"},
        {"ev": "death"}, {"ev": "revive"}, {"ev": "low_health", "ratio": 0.3},
    ],
    "note": "所有上报只产生事件；强度仍由规则与安全上限决定。",
}


class _EventHttpHandler(BaseHTTPRequestHandler):
    server_version = f"hd2coyote-http/{__version__}"
    source: "HttpEventSource" = None  # type: ignore[assignment]

    # ------------------------------------------------------------ 工具
    def log_message(self, fmt: str, *args: Any) -> None:  # 别把访问日志刷进控制台
        if self.source is not None:
            self.source.log.debug("%s - %s", self.address_string(), fmt % args)

    def _json(self, payload: Any, code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:  # 对方提前断开
            pass

    def _read_body(self) -> Any:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length <= 0 or length > MAX_BODY:
            return None
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8", errors="replace"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

    # ------------------------------------------------------------ 路由
    def do_GET(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        if route in ("/", "/health", "/status"):
            if route == "/" and not self._token_ok():
                return self._json({"error": "token 不对（X-HD2Coyote-Token 或 ?token=）"}, 403)
            self._json({"ok": True, "v": 1, "stats": self.source.stats(), **USAGE})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        if route not in ("/", "/event", "/state"):
            return self._json({"error": "not found"}, 404)
        if not self._token_ok():
            return self._json({"error": "token 不对（X-HD2Coyote-Token 或 ?token=）"}, 403)
        if not self.source.rate_ok():
            return self._json({"error": f"上报太频繁（上限 {self.source.max_per_s:.0f}/秒）",
                               "stats": self.source.stats()}, 429)
        body = self._read_body()
        if body is None:
            return self._json({"error": "请求体不是合法 JSON（或太大/为空）"}, 400)
        items = body if isinstance(body, list) else [body]
        events: list[Event] = []
        for item in items:
            if isinstance(item, dict):
                events.extend(self.source.ingest(item))
        self.source.push(events)
        return self._json({"ok": True, "accepted": len(events),
                           "events": [e.kind for e in events],
                           "stats": self.source.stats()})

    def _token_ok(self) -> bool:
        return self.source.token_ok(self.headers.get("X-HD2Coyote-Token"),
                                    parse_qs(urlparse(self.path).query).get("token", [None])[0])


@register_source
class HttpEventSource(Source):
    """HTTP 上报（状态包走状态机，事件包直接触发）。"""

    name = "http"
    label = "HTTP 事件源"
    kind = "state+event"
    hint = ("其它游戏/工具可以 POST /event 上报状态或事件；默认只听本机（地址见下面的说明），"
            "跨机请设 sources.http_token")

    def __init__(self, cfg: AppConfig, logger: logging.Logger | None = None) -> None:
        super().__init__(cfg, logger)
        self.trackers = EventTrackers(cfg)
        self.packets = 0
        self.bad = 0
        self.dropped = 0
        self.state_seen = False
        self.last_state: dict[str, Any] = {}
        self.last_state_at = 0.0
        self._queue: list[Event] = []
        self._stamps: list[float] = []
        self._lock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------- 配置
    @property
    def http_cfg(self) -> Any:
        return self.cfg.sources

    @property
    def max_per_s(self) -> float:
        return max(1.0, positive_float(self.http_cfg.http_max_per_s, 20.0))

    @property
    def state_timeout(self) -> float:
        return max(0.0, positive_float(self.http_cfg.http_timeout_s, 0.0))

    def critical_now(self) -> bool:
        """只有「有人上报过状态」且配了超时，才算会断流的状态源。

        类属性 critical 保持 False：清单/网页上它是事件型源，没人上报不算断流。
        """
        return bool(self.state_seen and self.state_timeout > 0)

    @property
    def alive(self) -> bool:
        if not self.started:
            return False
        if not self.critical_now():
            return True
        return (time.monotonic() - self.last_state_at) <= self.state_timeout

    # ------------------------------------------------------------- 生命周期
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        host = str(self.http_cfg.http_host or "127.0.0.1")
        port = int(self.http_cfg.http_port)
        handler = type("BoundEventHandler", (_EventHttpHandler,), {"source": self})
        self._httpd = ThreadingHTTPServer((host, port), handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="http-event-source", daemon=True)
        self._thread.start()
        self.started = True
        self.error = ""
        self.log.info("HTTP 事件源已监听 http://%s:%d/event%s",
                      host, port, "（要求 token）" if self.http_cfg.http_token else "")

    def stop(self) -> None:
        self.started = False
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            try:
                httpd.shutdown()
                httpd.server_close()
            except Exception as exc:  # 关服务器不允许抛到调用方
                self.log.debug("关闭 HTTP 事件源时出错：%s", exc)
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    # ------------------------------------------------------------- 收事件
    def push(self, events: list[Event]) -> None:
        if not events:
            return
        with self._lock:
            self._queue.extend(events)
            self.events += len(events)
            if len(self._queue) > MAX_QUEUE:
                over = len(self._queue) - MAX_QUEUE
                del self._queue[:over]
                self.dropped += over

    def poll(self, now: float) -> list[Event]:
        with self._lock:
            out, self._queue = self._queue, []
        return out

    # ------------------------------------------------------------- 校验
    def token_ok(self, header: str | None, query: str | None = None) -> bool:
        token = str(self.http_cfg.http_token or "")
        if not token:
            return True
        given = header or query or ""
        return hmac.compare_digest(str(given), token)

    def rate_ok(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        with self._lock:
            self._stamps = [t for t in self._stamps if now - t < 1.0]
            if len(self._stamps) >= self.max_per_s:
                self.dropped += 1
                return False
            self._stamps.append(now)
            return True

    # ------------------------------------------------------------- 解析
    def ingest(self, payload: dict[str, Any], now: float | None = None) -> list[Event]:
        """把一行 JSON 变成事件（纯逻辑，可直接单测）。"""
        now = time.monotonic() if now is None else now
        if not isinstance(payload, dict):
            self.bad += 1
            return []
        kind = str(payload.get("ev") or payload.get("event") or payload.get("kind") or "").strip().lower()
        has_state = any(k in payload for k in ("hp", "limbs", "dead", "bleeding"))
        if kind in ("", "state", "status", "player") and has_state:
            return self._state_event(payload, now)
        self.packets += 1
        parsed = parse_event(payload, now)
        if parsed is None:
            self.bad += 1
            self.log.debug("HTTP 事件源收到不认识的上报：%s", kind or payload)
            return []
        return [parsed]

    def _state_event(self, payload: dict[str, Any], now: float) -> list[Event]:
        """状态包：和游戏内桥走同一个状态机，判定逻辑只有一份。"""
        self.packets += 1
        self.state_seen = True
        self.last_state_at = now
        self.last_state = {"hp": payload.get("hp"), "limbs": payload.get("limbs"),
                           "dead": payload.get("dead"), "bleeding": payload.get("bleeding")}
        hp = _hp_ratio(payload)
        limbs = payload.get("limbs") or []
        if isinstance(limbs, dict):  # 允许 {"0":0,"1":1} 这种写法
            limbs = [limbs[k] for k in sorted(limbs, key=lambda x: str(x))]
        scores = [1.0 if truthy(v) else 0.0 for v in limbs][:8] if isinstance(limbs, (list, tuple)) else []
        dead = truthy(payload.get("dead"))
        return self.trackers.process(
            hp, scores, 1.0 if truthy(payload.get("bleeding")) else 0.0,
            1.0 if dead else 0.0, now,
            threshold=self.cfg.detect.death_template_threshold,
            names=self.cfg.hud.injury_slot_names,
        )

    # ------------------------------------------------------------- 展示
    def stats(self) -> dict[str, Any]:
        age = time.monotonic() - self.last_state_at if self.state_seen else None
        return {
            "packets": self.packets,
            "events": self.events,
            "bad": self.bad,
            "dropped": self.dropped,
            "queued": len(self._queue),
            "state_seen": self.state_seen,
            "state_age_s": None if age is None else round(age, 2),
            "listening": f"{self.http_cfg.http_host}:{self.http_cfg.http_port}",
            "token_required": bool(self.http_cfg.http_token),
            "max_per_s": self.max_per_s,
        }

    def describe(self) -> str:
        if not self.started:
            return "未启动"
        base = (f"http://{self.http_cfg.http_host}:{self.http_cfg.http_port}/event "
                f"上报 {self.packets} 个包 / 产生 {self.events} 个事件")
        if self.state_seen:
            base += f"（最近一次状态 {max(0.0, time.monotonic() - self.last_state_at):.1f}s 前）"
        if self.dropped:
            base += f"，丢弃 {self.dropped}"
        return base

    def apply_config(self, cfg: AppConfig) -> None:
        # 监听地址/端口/令牌变了必须重建（引擎会重建整个源列表），这里只热更状态机参数
        self.cfg = cfg
        self.trackers = EventTrackers(cfg)


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
    """把「事件包」JSON 变成事件（不认识就返回 None）。

    和 ingest() 共用同一份映射：网页的「手动注入」、HTTP 上报、
    以后别的源想复用都走这里，避免两套解析逻辑跑偏。
    """
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


def _hp_ratio(payload: dict[str, Any]) -> float | None:
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

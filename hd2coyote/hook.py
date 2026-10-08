"""Hook 数据源：接收游戏内 Lua addon 通过 UDP 发来的玩家状态。

为什么用 UDP + JSON 行协议：
  * addon 跑在游戏的 LuaJIT 里，用 FFI 调 ws2_32.sendto 是最省事的出口；
  * 无连接、无握手负担，掉包无所谓（10Hz 状态流，丢一帧不影响判定）；
  * 单向：控制器不需要往回发任何东西，减少在游戏进程里的代码路径。

协议 v1（一行一个 JSON，UTF-8，≤ 512 字节）：
    {"v":1,"ev":"hello","build":"1.8.46015.0","profile":"recon-1"}
    {"v":1,"ev":"state","t":12.5,"hp":0.62,"limbs":[0,1,0],"bleeding":0,"dead":0}
    {"v":1,"ev":"bye"}

收到的是「状态」而不是「事件」，事件判定复用 detectors 里那套已经测过的
健康/损伤/阵亡状态机 —— 这样 Lua 侧只需要忠实读值，逻辑只有一份。
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from dataclasses import dataclass, field

from .config import AppConfig
from .detectors import EventTrackers
from .events import Event
from .events import positive_float as _positive_float
from .events import truthy as _truthy

PROTOCOL_VERSION = 1
MAX_DATAGRAM = 1024


@dataclass
class HookState:
    """最近一次收到的状态（UI 与日志都看它）。"""

    t: float = 0.0
    received: float = 0.0
    hp: float | None = None
    hp_max: float = 100.0
    limbs: list[int] = field(default_factory=list)
    bleeding: bool = False
    dead: bool = False
    build: str = ""
    version: str = ""
    profile: str = ""
    packets: int = 0
    bad_packets: int = 0

    @property
    def age(self) -> float:
        return time.monotonic() - self.received if self.received else float("inf")


class HookSource:
    """监听本机 UDP 端口，把状态流转换成事件。"""

    def __init__(self, cfg: AppConfig, logger: logging.Logger | None = None) -> None:
        self.cfg = cfg
        self.log = logger or logging.getLogger("hd2coyote.hook")
        self.hook_cfg = cfg.hook
        self.trackers = EventTrackers(cfg)
        self.state = HookState()
        self.hello: dict[str, object] | None = None

        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._queue: list[dict] = []

    # ------------------------------------------------------------- 生命周期
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.hook_cfg.host, self.hook_cfg.port))
        self._sock.settimeout(0.2)
        self._thread = threading.Thread(target=self._recv_loop, name="hook-udp", daemon=True)
        self._thread.start()
        self.log.info("Hook 数据源已监听 udp://%s:%d（等待游戏内 addon 发状态）",
                      self.hook_cfg.host, self.hook_cfg.port)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._thread = None
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    @property
    def alive(self) -> bool:
        """状态流是否还在（超时 = addon 挂了 / 退出了游戏）。"""
        return self.state.age <= max(0.5, self.hook_cfg.timeout_s)

    # ------------------------------------------------------------- 接收
    def _recv_loop(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                data, _addr = self._sock.recvfrom(MAX_DATAGRAM)
            except socket.timeout:
                continue
            except OSError:
                break
            except Exception as exc:  # 不允许因为单个坏包退出
                self.log.debug("UDP 接收异常：%s", exc)
                continue
            with self._lock:
                # 到达时间按「收到」算，而不是等 poll() 处理 —— 否则断流判定会滞后
                self.state.received = time.monotonic()
                self._queue.append({"_raw": data})

    # ------------------------------------------------------------- 处理
    def poll(self, now: float | None = None) -> list[Event]:
        """把收到的数据包转成事件（引擎每帧调用）。"""
        now = time.monotonic() if now is None else now
        with self._lock:
            pending, self._queue = self._queue, []
        events: list[Event] = []
        for item in pending:
            packet = self._decode(item["_raw"])
            if packet is None:
                self.state.bad_packets += 1
                continue
            events.extend(self._handle(packet, now))
        return events

    def _decode(self, raw: bytes) -> dict | None:
        try:
            text = raw.decode("utf-8", errors="strict").strip()
            packet = json.loads(text)
        except Exception:
            self.log.debug("收到无法解析的数据包（%d 字节）", len(raw))
            return None
        if not isinstance(packet, dict) or int(packet.get("v", 0)) != PROTOCOL_VERSION:
            return None
        return packet

    def _handle(self, packet: dict, now: float) -> list[Event]:
        kind = str(packet.get("ev", ""))
        self.state.packets += 1

        if kind == "hello":
            self.hello = packet
            self.state.build = str(packet.get("build", ""))
            self.state.version = str(packet.get("ver", ""))
            self.state.profile = str(packet.get("profile", ""))
            from . import __version__ as controller_version

            self.log.info("游戏内 addon 已连接：版本=%s build=%s profile=%s（控制器 %s）",
                          self.state.version or "?", self.state.build or "?",
                          self.state.profile or "?", controller_version)
            if self.state.version and self.state.version != controller_version:
                self.log.warning("版本不一致：游戏内桥 %s / 控制器 %s —— "
                                 "建议用同一个 VERSION 重新打包并 Deploy",
                                 self.state.version, controller_version)
            return []

        if kind == "bye":
            self.log.warning("游戏内 addon 已退出")
            self.state.received = 0.0
            return []

        if kind != "state":
            return []

        hp_max = _positive_float(packet.get("hp_max"), 100.0)
        hp_raw = packet.get("hp")
        hp = None
        if hp_raw is not None:
            hp = max(0.0, min(1.0, _positive_float(hp_raw, 0.0) / hp_max))
        limbs = packet.get("limbs") or []
        limbs = [1 if _truthy(v) else 0 for v in limbs][:8]
        bleeding = _truthy(packet.get("bleeding"))
        dead = _truthy(packet.get("dead"))

        self.state.t = _positive_float(packet.get("t"), 0.0)
        self.state.hp = hp
        self.state.hp_max = hp_max
        self.state.limbs = limbs
        self.state.bleeding = bleeding
        self.state.dead = dead

        scores = [float(v) for v in limbs]
        death_score = 1.0 if dead else 0.0
        return self.trackers.process(hp, scores, 1.0 if bleeding else 0.0,
                                     death_score, now,
                                     threshold=self.cfg.detect.death_template_threshold,
                                     names=self.cfg.hud.injury_slot_names)

    # ------------------------------------------------------------- 展示
    def describe(self) -> str:
        s = self.state
        if s.received == 0.0:
            return (f"等待游戏内 addon（UDP {self.hook_cfg.host}:{self.hook_cfg.port} "
                    f"未收到数据）")
        hp = "--" if s.hp is None else f"{s.hp * 100:5.1f}%"
        limbs = "".join(str(v) for v in s.limbs) or "-"
        return (f"HP={hp} 肢体={limbs} 流血={'是' if s.bleeding else '否'} "
                f"{'阵亡' if s.dead else '存活'} 包={s.packets} 延迟={s.age * 1000:.0f}ms")


def _positive_float(value: object, default: float) -> float:
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if out != out or out in (float("inf"), float("-inf")):  # NaN / inf
        return default
    return out


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return False

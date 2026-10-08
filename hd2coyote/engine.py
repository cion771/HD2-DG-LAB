"""引擎：抓屏 -> 检测 -> 规则 -> 安全换算 -> 设备输出。

线程模型（简单优先）：
   一个后台线程按 fps 跑循环：抓帧、检测、触发动作、推进强度渐变。
   设备（WebSocket）在自己的线程里跑 asyncio，引擎只调用它的同步接口。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field, replace
from typing import Callable

from . import wave_lib, waves
from .capture import CaptureError, ScreenGrabber, is_black_frame
from .config import AppConfig
from .detectors import Detector, HudSample
from .device import Device, create_device
from .events import Damage, Death, DeviceState, Event, FeedbackButton, LimbInjury, LowHealth, Revive
from .hook import HookSource
from .ramp import PunishmentRamp
from .rules import Action, RuleEngine
from .safety import SafetyGuard, foreground_window_title
from .sources import Source, build_sources


@dataclass
class ActiveEffect:
    action: Action
    started: float
    ends: float
    next_repeat: float = 0.0

    def done(self, now: float) -> bool:
        return now >= self.ends


@dataclass
class EngineStatus:
    running: bool = False
    detecting: bool = False
    source: str = "hook"
    hp: float | None = None
    injury: list[float] = field(default_factory=list)
    output_a: int = 0
    output_b: int = 0
    pct: float = 0.0
    device_connected: bool = False
    armed: bool = True
    hook_alive: bool = False
    hook_detail: str = ""
    last_error: str = ""
    detail: str = ""
    # 0.5.0：事件源清单（每个源的 name/label/alive/events/error…）与惩罚累积
    sources: list[dict] = field(default_factory=list)
    ramp_pct: float = 0.0
    ramp_detail: str = ""


class Engine:
    """把一切串起来的中枢。"""

    def __init__(self, cfg: AppConfig, logger: logging.Logger | None = None) -> None:
        self.cfg = cfg
        self.log = logger or logging.getLogger("hd2coyote.engine")
        self.grabber: ScreenGrabber | None = None
        self.hook: HookSource | None = None
        self.sources: list[Source] = []
        self._source_map: dict[str, Source] = {}
        self.detector = Detector(cfg, self.log)
        self.rules = RuleEngine(cfg, self.log)
        self.ramp = PunishmentRamp(cfg.ramp)
        self.device: Device = create_device(cfg.device, on_event=self._on_device_event, logger=self.log)
        self.safety = SafetyGuard(cfg.safety, self.device)

        self.status = EngineStatus(source=cfg.source)
        self.events_log: deque[Event] = deque(maxlen=200)
        self.on_event: Callable[[Event], None] | None = None
        self.on_status: Callable[[EngineStatus], None] | None = None

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._effects: list[ActiveEffect] = []
        self._cur_a = 0.0
        self._cur_b = 0.0
        self._last_tick = 0.0
        self._errors = 0
        self._focus_ok = True
        self._focus_checked = 0.0

    # ------------------------------------------------------------- 生命周期
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self.device.start()
        if self.cfg.device.kind != "mock":
            self.log.info("手机 App 扫码地址：%s", getattr(self.device, "qr_payload", "(mock)"))
        self._thread = threading.Thread(target=self._run, name="engine", daemon=True)
        self._thread.start()
        self.status.running = True

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=4.0)
        self._thread = None
        self._effects.clear()
        self._push_output(force_zero=True)
        try:
            self.device.stop()
        except Exception as exc:
            self.log.error("设备停止异常：%s", exc)
        if self.grabber is not None:
            self.grabber.close()
            self.grabber = None
        self._close_sources()
        self.status.running = False
        self.status.detecting = False
        self._notify_status()

    # ------------------------------------------------------------- 外部控制
    def arm(self) -> None:
        self.rules.reset_cooldowns()
        self.safety.arm()

    def trip(self, reason: str = "急停") -> None:
        self._effects.clear()
        self.safety.trip(reason)
        self._cur_a = self._cur_b = 0.0
        self._push_output(force_zero=True)

    def test_pulse(self, pct: float | None = None, ms: float = 800.0,
                   channel: str = "both", wave: str = "ramp_up") -> None:
        """手动测试一发（依然受安全上限约束）。"""
        rule = self.cfg.rules.get("limb_injury")
        base = pct if pct is not None else (rule.base_pct if rule else 20.0)
        action = Action(
            name="test",
            pct=float(base),
            units=self._wave_units(wave, ms),
            duration_ms=float(ms),
            attack_ms=self.cfg.safety.attack_ms,
            release_ms=self.cfg.safety.release_ms,
            channel=channel,
            priority=9,
        )
        self._activate(action, time.monotonic(), apply_ramp=False)

    def _wave_units(self, wave: str, duration_ms: float, peak: float = 100.0) -> list[str]:
        """取波形单元：命名波形库优先，其次内置预设，名字写错就退回 ramp_up。"""
        units = wave_lib.resolve(self.cfg, wave, duration_ms, peak=peak)
        if units:
            return units
        try:
            return waves.build(wave, duration_ms, peak=peak)
        except KeyError:
            self.log.warning("未知波形 %r：库里没有、内置预设也没有，退回 ramp_up", wave)
            return waves.build("ramp_up", duration_ms, peak=peak)

    def set_config(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self.rules = RuleEngine(cfg, self.log)
        self.safety.cfg = cfg.safety
        self.ramp.cfg = cfg.ramp
        self.detector.apply_config(cfg)
        for src in list(self.sources):
            try:
                src.apply_config(cfg)
            except Exception as exc:
                self.log.error("事件源 %s 应用配置失败：%s", src.label, exc)
        self._bind_hook()

    def inject(self, event: Event, now: float | None = None) -> None:
        """从外部注入一个事件（模拟模式、或对接其它检测插件/框架）。"""
        self._handle_event(event, time.monotonic() if now is None else now)

    def tick(self, now: float | None = None) -> None:
        """推进一次效果调度（模拟模式手动调用）。"""
        self._advance_effects(time.monotonic() if now is None else now)

    # ------------------------------------------------------------- 主循环
    def _run(self) -> None:
        try:
            if self.cfg.source == "vision":
                self._run_vision()
            else:
                self._run_sources()
        finally:
            self.status.detecting = False
            self._push_output(force_zero=True)
            self._notify_status()

    # ------------------------------------------------------------- 事件源
    def _run_sources(self) -> None:
        """外部事件源循环（游戏内 Lua 桥 / HTTP 事件源 / 之后可以再加别的）。"""
        try:
            self.sources = build_sources(self.cfg, self.log)
        except Exception as exc:
            self.status.last_error = f"事件源初始化失败：{exc}"
            self.log.exception("事件源初始化失败：%s", exc)
            return
        self._source_map = {src.name: src for src in self.sources}
        self._bind_hook()
        if not self.sources:
            self.status.last_error = "没有启用任何事件源（看配置里的 sources.enabled）"
            self.log.error("%s", self.status.last_error)
            return

        started: list[Source] = []
        for src in self.sources:
            try:
                src.start()
            except Exception as exc:
                self.status.last_error = f"{src.label} 启动失败：{exc}"
                self.log.error("事件源 %s 启动失败：%s", src.label, exc)
                if src.critical and not started:
                    # 唯一的现场状态源起不来（例如 UDP 端口被占）就别空转了
                    self._close_sources()
                    return
                continue
            started.append(src)
        if not started:
            self.status.last_error = "所有事件源都启动失败"
            self.log.error("所有事件源都启动失败，引擎空转")
            self._close_sources()
            return

        if self.cfg.source == "hook" and self.hook is None:
            self.log.warning("配置里 source=hook，但事件源里没有 game_bridge —— "
                             "改走 HTTP 事件源，请确认外部程序会往 %s:%d 上报",
                             self.cfg.sources.http_host, self.cfg.sources.http_port)
        self.log.info("事件源就绪：%s", self._source_hint())
        self.status.detecting = True
        self._last_tick = time.monotonic()
        lost = False
        while not self._stop.is_set():
            now = time.monotonic()
            try:
                for src in self.sources:
                    for event in src.poll(now):
                        self._handle_event(event, now)
                critical = [s for s in self.sources if s.critical_now()]
                gone = [s for s in critical if not s.alive]
                if gone and not lost:
                    lost = True
                    self.log.warning("关键状态流中断（%s），已静音等它回来",
                                     "、".join(s.label for s in gone))
                elif not gone and lost:
                    lost = False
                    self.log.info("关键状态流已恢复：%s", self._source_hint())
                if lost and self._effects:
                    self._effects.clear()
                self._advance_effects(now)
                self._update_status(now)
            except Exception as exc:  # 任何异常都不允许留下输出
                self.log.exception("引擎异常：%s", exc)
                self.trip(f"引擎异常：{exc}")
                break
            time.sleep(0.02)
        self._close_sources()

    def _close_sources(self) -> None:
        for src in list(self.sources):
            try:
                src.stop()
            except Exception as exc:
                self.log.error("事件源 %s 停止异常：%s", src.label, exc)
        self.sources = []
        self._source_map = {}
        self.hook = None

    def _bind_hook(self) -> None:
        """把 game_bridge 源内部的 HookSource 暴露成 self.hook（历史调用点依赖它）。"""
        bridge = self._source_map.get("game_bridge")
        self.hook = getattr(bridge, "hook", None) if bridge is not None else None

    def source(self, name: str) -> Source | None:
        """按名字取当前运行的事件源（例如 "http"、"game_bridge"）；没有则 None。"""
        return self._source_map.get(str(name))

    def _source_hint(self) -> str:
        if not self.sources:
            return "无"
        return "；".join(src.describe() for src in self.sources)

    def _run_vision(self) -> None:
        """屏幕识别路径（不需要装 mod，但需要标定 HUD）。"""
        try:
            self.grabber = ScreenGrabber(self.cfg.capture, self.log)
        except CaptureError as exc:
            self.status.last_error = str(exc)
            self.log.error("抓屏初始化失败：%s", exc)
            return
        self.status.detecting = True
        self._last_tick = time.monotonic()
        interval = 1.0 / max(1.0, self.cfg.capture.fps)
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                self._tick(t0)
            except CaptureError as exc:
                self._errors += 1
                self.status.last_error = str(exc)
                if self._errors >= self.cfg.detect.capture_error_limit:
                    self.log.error("连续抓屏失败 %d 次，已停止检测：%s", self._errors, exc)
                    self.trip("抓屏失败")
                    break
            except Exception as exc:  # 任何未预期异常都不允许留下输出
                self.log.exception("引擎异常：%s", exc)
                self.trip(f"引擎异常：{exc}")
                break
            spent = time.monotonic() - t0
            time.sleep(max(0.0, interval - spent))

    def _tick(self, now: float) -> None:
        assert self.grabber is not None
        region = self.cfg.hud.hp_bar
        frame = self.grabber.grab(self._capture_region(region))
        if is_black_frame(frame) and self._errors == 0:
            self.log.warning("抓到全黑画面：若游戏是独占全屏，请改为「无边框窗口」")
        self._errors = 0

        for event in self.detector.process(frame, now):
            self._handle_event(event, now)

        self._advance_effects(now)
        self._check_focus(now)
        self._update_status(now)

    def _capture_region(self, hp_box):
        """抓屏区域 = 血条 + 图标区（+ 模板区）的并集，面积小、帧率高。"""
        boxes = [b for b in (hp_box, self.detector.layout.injury_zone,
                             self.detector.layout.death_probe) if b is not None and b.is_valid()]
        if not boxes:
            return None
        x0 = min(b.x for b in boxes)
        y0 = min(b.y for b in boxes)
        x1 = max(b.x + b.w for b in boxes)
        y1 = max(b.y + b.h for b in boxes)
        pad = 8
        from .config import Box

        return Box(max(0, x0 - pad), max(0, y0 - pad), x1 - x0 + pad * 2, y1 - y0 + pad * 2)

    # ------------------------------------------------------------- 事件处理
    def _handle_event(self, event: Event, now: float) -> None:
        if isinstance(event, (Damage, LimbInjury, Death, LowHealth, Revive)):
            self.events_log.append(event)
            self.log.info("事件：%s", _fmt_event(event))
            if self.on_event is not None:
                try:
                    self.on_event(event)
                except Exception:
                    pass
        self.ramp.on_event(event, now)
        for action in self.rules.actions(event, now):
            self._activate(action, now)

    def _activate(self, action: Action, now: float, apply_ramp: bool = True) -> None:
        # 惩罚累积：伤害攒起来的强度加在这一发上（加完照样要过 SafetyGuard 的上限）
        bonus = self.ramp.bonus() if apply_ramp else 0.0
        if bonus > 0:
            action = replace(action, pct=min(100.0, float(action.pct) + bonus))
        # 同一动作重复触发时替换旧的，避免叠加
        self._effects = [e for e in self._effects if not _same_action(e.action, action)]
        effect = ActiveEffect(
            action=action,
            started=now,
            ends=now + action.duration_ms / 1000.0,
            next_repeat=now + action.repeat_ms / 1000.0 if action.repeat_ms > 0 else 0.0,
        )
        self._effects.append(effect)
        channels = self.device.channels_of(action.channel)
        for ch in channels:
            self.device.clear(ch)
            self.device.send_wave(ch, action.units)
        self.log.info("输出：%s pct=%.1f%s 通道=%s 时长=%.0fms",
                      action.name, action.pct,
                      f"（含累积 +{bonus:.1f}）" if bonus > 0 else "",
                      "+".join(channels), action.duration_ms)

    def _advance_effects(self, now: float) -> None:
        self.ramp.tick(now)
        for eff in list(self._effects):
            if eff.next_repeat and now >= eff.next_repeat and now < eff.ends:
                for ch in self.device.channels_of(eff.action.channel):
                    self.device.clear(ch)
                    self.device.send_wave(ch, eff.action.units)
                eff.next_repeat = now + eff.action.repeat_ms / 1000.0
            if eff.done(now):
                self._effects.remove(eff)
        self._push_output(now)

    def _push_output(self, now: float | None = None, force_zero: bool = False) -> None:
        now = time.monotonic() if now is None else now
        dt = max(0.0, now - self._last_tick)
        self._last_tick = now

        target_a = target_b = 0.0
        if not force_zero and self.safety.armed and self._focus_ok:
            for eff in self._effects:
                ch = eff.action.channel
                if ch in ("A", "both"):
                    target_a = max(target_a, eff.action.pct)
                if ch in ("B", "both"):
                    target_b = max(target_b, eff.action.pct)

        self._cur_a = _approach(self._cur_a, target_a, dt, self.cfg.safety.attack_ms,
                                self.cfg.safety.release_ms)
        self._cur_b = _approach(self._cur_b, target_b, dt, self.cfg.safety.attack_ms,
                                self.cfg.safety.release_ms)

        lim = self.device.limits
        value_a = self.safety.strength_for(self._cur_a, lim.a)
        value_b = self.safety.strength_for(self._cur_b, lim.b)
        if force_zero:
            value_a = value_b = 0
        if value_a != self.status.output_a or value_b != self.status.output_b:
            self.device.set_strength(value_a, value_b)
            self.status.output_a, self.status.output_b = value_a, value_b
        self.status.pct = max(self._cur_a, self._cur_b)
        self.safety.note_output(dt, self.status.pct)

    # ------------------------------------------------------------- 杂项
    def _check_focus(self, now: float) -> None:
        if not self.cfg.safety.mute_on_focus_loss:
            self._focus_ok = True
            return
        if now - self._focus_checked < 0.5:
            return
        self._focus_checked = now
        title = foreground_window_title()
        ok = self.cfg.safety.game_window_title.lower() in title.lower() if title else False
        if ok != self._focus_ok:
            self.log.info("焦点变化：%s（%s）", title, "输出启用" if ok else "输出静音")
        self._focus_ok = ok

    def _on_device_event(self, event: Event) -> None:
        if isinstance(event, DeviceState):
            self.log.info("设备状态：%s %s", "已连接" if event.connected else "未连接", event.detail)
            if not event.connected:
                self._effects.clear()
                self._cur_a = self._cur_b = 0.0
            self._notify_status()
        elif isinstance(event, FeedbackButton):
            # 手机上的反馈按钮：0 号 = 急停 / 恢复
            if event.index == 0:
                if self.safety.armed:
                    self.trip("App 反馈按钮急停")
                else:
                    self.arm()
            else:
                self.events_log.append(event)

    def _update_status(self, now: float) -> None:
        if self.cfg.source == "vision":
            s = self.detector.sample
            self.status.hp = s.hp
            self.status.injury = list(s.injury)
        else:
            self.status.sources = [src.status().as_dict() for src in self.sources]
            bridge = self._source_map.get("game_bridge")
            state = getattr(bridge, "state", None)
            if state is not None:
                self.status.hp = state.hp
                self.status.injury = [float(v) for v in state.limbs]
            self.status.hook_alive = bool(getattr(bridge, "alive", False))
            self.status.hook_detail = bridge.describe() if bridge is not None else "未启用游戏桥"
        self.ramp.note_hp(self.status.hp)
        self.status.ramp_pct = self.ramp.bonus()
        self.status.ramp_detail = self.ramp.describe()
        self.status.source = self.cfg.source
        self.status.device_connected = self.device.connected
        self.status.armed = self.safety.armed
        self._notify_status()

    def _notify_status(self) -> None:
        cb = self.on_status
        if cb is None:
            return
        try:
            cb(self.status)
        except Exception:
            pass


def _approach(current: float, target: float, dt: float, attack_ms: float, release_ms: float) -> float:
    if abs(target - current) < 0.01:
        return target
    if target > current:
        rate = 100.0 / max(0.05, attack_ms / 1000.0)
        return min(target, current + rate * dt)
    rate = 100.0 / max(0.05, release_ms / 1000.0)
    return max(target, current - rate * dt)


def _same_action(a: Action, b: Action) -> bool:
    return a.name == b.name and a.channel == b.channel


def _fmt_event(event: Event) -> str:
    if isinstance(event, Damage):
        return f"受伤 -{event.severity:.1f}%（{event.hp_before * 100:.0f}%→{event.hp_after * 100:.0f}%）"
    if isinstance(event, LimbInjury):
        # 名字为空（例如 HTTP 上报只给了 slot）时别显示成「肢体损伤 []」
        text = f"肢体损伤 {event.name}".rstrip()
        return text + ("（流血）" if event.bleeding else "")
    if isinstance(event, Death):
        return "阵亡"
    if isinstance(event, Revive):
        return "被增援 / 复活"
    if isinstance(event, LowHealth):
        return f"低血量 {event.ratio * 100:.0f}%"
    if isinstance(event, FeedbackButton):
        return f"App 反馈按钮 #{event.index}"
    return event.kind

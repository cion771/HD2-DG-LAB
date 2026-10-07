"""合成 HUD 画面 + 无游戏自测。

没有《绝地潜兵 2》也能验证整条链路：
    python -m hd2coyote simulate --device mock
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np

from .config import AppConfig
from .detectors import Detector
from .hud import HudLayout, default_layout

BG = (26, 28, 30)
BAR_EMPTY = (60, 62, 66)
BAR_FILL = (238, 240, 242)
INJURY_ON = (255, 122, 0)
BLOOD = (190, 40, 40)


def synthetic_frame(
    layout: HudLayout,
    hp: float = 1.0,
    injured: tuple[bool, ...] = (),
    dead: bool = False,
    size: tuple[int, int] = (1920, 1080),
) -> np.ndarray:
    """按给定布局画一帧「假 HUD」。"""
    w, h = size
    frame = np.zeros((h, w, 3), dtype=np.uint8)
    frame[:, :] = BG

    bar = layout.hp_bar
    if bar is not None and bar.is_valid():
        x0, y0 = max(0, bar.x), max(0, bar.y)
        x1, y1 = min(w, bar.x + bar.w), min(h, bar.y + bar.h)
        if x1 > x0 and y1 > y0:
            frame[y0:y1, x0:x1] = BAR_EMPTY
            fill_w = 0 if dead else int((x1 - x0) * max(0.0, min(1.0, hp)))
            if fill_w > 0:
                frame[y0:y1, x0:x0 + fill_w] = BAR_FILL

    zone = layout.injury_zone
    if zone is not None and zone.is_valid() and injured:
        slots = max(1, layout.injury_slots)
        for i in range(min(slots, len(injured))):
            if not injured[i]:
                continue
            seg_w = zone.w // slots
            x0 = max(0, zone.x + i * seg_w + 2)
            x1 = min(w, zone.x + (i + 1) * seg_w - 2)
            y0 = max(0, zone.y + 2)
            y1 = min(h, zone.y + zone.h - 2)
            frame[y0:y1, x0:x1] = INJURY_ON
            frame[y0:y1, x0:x0 + max(1, (x1 - x0) // 4)] = BLOOD

    probe = layout.death_probe
    if dead and probe is not None and probe.is_valid():
        x0, y0 = max(0, probe.x), max(0, probe.y)
        x1, y1 = min(w, probe.x + probe.w), min(h, probe.y + probe.h)
        frame[y0:y1, x0:x1] = (200, 200, 200)
    return frame


@dataclass
class DemoStep:
    at: float  # 秒
    hp: float = 1.0
    injured: tuple[bool, ...] = ()
    dead: bool = False
    label: str = ""


DEFAULT_SCRIPT: list[DemoStep] = [
    DemoStep(0.0, 1.00, label="满血"),
    DemoStep(1.0, 0.88, label="挨了一发（轻伤）"),
    DemoStep(2.0, 0.66, (), label="持续掉血"),
    DemoStep(3.0, 0.66, (True, False, False), label="左臂受伤"),
    DemoStep(4.5, 0.66, (True, True, False), label="左臂 + 躯干"),
    DemoStep(6.0, 0.40, (True, True, False), label="低血量"),
    DemoStep(7.5, 0.00, (True, True, True), True, label="阵亡"),
    DemoStep(10.0, 0.05, (), False, label="被增援（复活）"),
    DemoStep(11.0, 1.00, label="恢复满血"),
]


def run_demo(cfg: AppConfig, engine, duration: float = 0.0, script=DEFAULT_SCRIPT) -> None:
    """按脚本喂合成画面，走完整条链路（检测->规则->设备）。"""
    width, height = 1920, 1080
    if cfg.hud.hp_bar is None:
        fallback = default_layout(width, height)
        cfg.hud.hp_bar = fallback.hp_bar
        cfg.hud.injury_zone = fallback.injury_zone
    layout = HudLayout.from_config(cfg.hud)
    detector = Detector(cfg)
    started = time.monotonic()
    deadline = started + (duration if duration > 0 else script[-1].at + 1.5)
    idx = 0
    interval = 1.0 / max(1.0, cfg.capture.fps)
    while time.monotonic() < deadline:
        elapsed = time.monotonic() - started
        while idx + 1 < len(script) and elapsed >= script[idx + 1].at:
            idx += 1
            print(f"[demo {elapsed:5.1f}s] {script[idx].label}")
        step = script[idx]
        frame = synthetic_frame(layout, hp=step.hp, injured=step.injured, dead=step.dead,
                                size=(width, height))
        now = time.monotonic()
        for event in detector.process(frame, now):
            engine.inject(event, now)
        engine.tick(now)
        time.sleep(interval)

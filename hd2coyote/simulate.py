"""合成结构化状态的无游戏自测：python -m hd2coyote simulate --mock。"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .config import AppConfig
from .detectors import EventTrackers


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
    """按脚本输入血量/肢体/阵亡状态，验证状态机、规则与输出调度。"""
    if not script:
        return
    trackers = EventTrackers(cfg)
    started = time.monotonic()
    deadline = started + (duration if duration > 0 else script[-1].at + 1.5)
    idx = 0
    while time.monotonic() < deadline:
        now = time.monotonic()
        elapsed = now - started
        while idx + 1 < len(script) and elapsed >= script[idx + 1].at:
            idx += 1
            print(f"[demo {elapsed:5.1f}s] {script[idx].label}")
        step = script[idx]
        scores = [float(v) for v in step.injured]
        scores += [0.0] * max(0, cfg.hud.injury_slots - len(scores))
        for event in trackers.process(step.hp, scores, float(any(step.injured)),
                                      float(step.dead), now, names=cfg.hud.injury_slot_names):
            engine.inject(event, now)
        engine.tick(now)
        time.sleep(1.0 / 15.0)

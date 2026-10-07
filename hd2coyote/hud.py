"""HUD 布局：血条 / 损伤图标区 / 阵亡模板区。

游戏分辨率、UI 缩放、宽高比都会影响坐标，所以：
  * 提供 `auto_locate_hp_bar()` 自动找血条；
  * 提供标定向导（ui.py）让用户框选；
  * 默认值只是 16:9 下的粗略猜测，务必先标定。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .config import Box, HudConfig


@dataclass
class HudLayout:
    hp_bar: Box | None = None
    injury_zone: Box | None = None
    injury_slots: int = 3
    injury_slot_names: list[str] = field(default_factory=lambda: ["左肢", "躯干", "右肢"])
    death_probe: Box | None = None
    death_template: str = ""

    # ---------------------------------------------------------------- 转换
    @classmethod
    def from_config(cls, cfg: HudConfig) -> "HudLayout":
        return cls(
            hp_bar=cfg.hp_bar,
            injury_zone=cfg.injury_zone,
            injury_slots=max(1, int(cfg.injury_slots)),
            injury_slot_names=list(cfg.injury_slot_names),
            death_probe=cfg.death_probe,
            death_template=cfg.death_template,
        )

    def to_config(self, cfg: HudConfig) -> None:
        cfg.hp_bar = self.hp_bar
        cfg.injury_zone = self.injury_zone
        cfg.injury_slots = self.injury_slots
        cfg.injury_slot_names = list(self.injury_slot_names)
        cfg.death_probe = self.death_probe
        cfg.death_template = self.death_template

    @property
    def calibrated(self) -> bool:
        return bool(self.hp_bar and self.hp_bar.is_valid())

    def slot_name(self, index: int) -> str:
        names = self.injury_slot_names
        return names[index] if 0 <= index < len(names) else f"槽位{index}"


def default_layout(width: int, height: int) -> HudLayout:
    """按屏幕尺寸给出一份 16:9 的粗略默认布局。"""
    bar_w = int(width * 0.155)
    bar_h = max(6, int(height * 0.010))
    bar_x = int(width * 0.5 - bar_w / 2)
    bar_y = int(height * 0.885)
    hp = Box(bar_x, bar_y, bar_w, bar_h)
    zone_w = int(bar_w * 0.55)
    zone = Box(max(0, bar_x - zone_w - int(width * 0.006)), bar_y - int(bar_h * 0.6),
               zone_w, int(bar_h * 2.2))
    return HudLayout(hp_bar=hp, injury_zone=zone)


def clamp_box(box: Box, width: int, height: int) -> Box:
    x = max(0, min(int(box.x), width - 1))
    y = max(0, min(int(box.y), height - 1))
    w = max(1, min(int(box.w), width - x))
    h = max(1, min(int(box.h), height - y))
    return Box(x, y, w, h)


def gray(frame: np.ndarray) -> np.ndarray:
    """灰度（用最亮通道，避免纯色通道丢失信息，也比加权平均更抗压缩噪声）。"""
    if frame.ndim == 2:
        return frame.astype(np.float32)
    return frame[..., :3].max(axis=2).astype(np.float32)


def crop(frame: np.ndarray, box: Box | None) -> np.ndarray | None:
    if box is None or not box.is_valid():
        return None
    h, w = frame.shape[:2]
    x0 = max(0, min(box.x, w - 1))
    y0 = max(0, min(box.y, h - 1))
    x1 = max(x0 + 1, min(box.x + box.w, w))
    y1 = max(y0 + 1, min(box.y + box.h, h))
    return frame[y0:y1, x0:x1]


def auto_locate_hp_bar(
    frame: np.ndarray,
    search_top_ratio: float = 0.60,
    brightness: float = 165.0,
    min_run: int = 40,
) -> Box | None:
    """在画面下半部分寻找「一条横向的亮带」= 血条。

    思路：血条的填充是接近白色的亮色，背景是半透明深色面板，
    因此在每一行里找最长的连续亮像素段，取最长的一段作为血条。
    """
    h, w = frame.shape[:2]
    y0 = int(h * search_top_ratio)
    sub = gray(frame[y0:, :, :] if frame.ndim == 3 else frame[y0:, :])
    mask = sub > brightness
    best_len = 0
    best: tuple[int, int, int] | None = None  # row, start, end
    for row in range(mask.shape[0]):
        line = mask[row]
        if not line.any():
            continue
        padded = np.concatenate(([False], line, [False]))
        edges = np.diff(padded.astype(np.int8))
        starts = np.flatnonzero(edges == 1)
        ends = np.flatnonzero(edges == -1)
        if starts.size == 0:
            continue
        lengths = ends - starts
        idx = int(np.argmax(lengths))
        if lengths[idx] >= max(min_run, best_len):
            best_len = int(lengths[idx])
            best = (row, int(starts[idx]), int(ends[idx]))
    if best is None or best_len < min_run:
        return None
    row, x_start, x_end = best
    # 以最长行为中心，上下扩展到亮带边缘
    top = bottom = row
    while top > 0 and (mask[top - 1, x_start:x_end].mean() > 0.5):
        top -= 1
    while bottom + 1 < mask.shape[0] and (mask[bottom + 1, x_start:x_end].mean() > 0.5):
        bottom += 1
    bar_h = max(3, bottom - top + 1)
    return Box(x_start, y0 + top, x_end - x_start, bar_h)


def default_injury_zone(hp_bar: Box, width: int, height: int) -> Box:
    """血条左侧的损伤图标条（默认位置，建议标定）。"""
    zone_w = max(24, int(hp_bar.h * 4.0))
    x = max(0, hp_bar.x - zone_w - 8)
    y = max(0, hp_bar.y - int(hp_bar.h * 0.6))
    h = max(12, int(hp_bar.h * 2.4))
    return clamp_box(Box(x, y, zone_w, h), width, height)

"""外部 HUD 读取纯函数；只处理屏幕像素，不接触游戏进程。

安全边界：本模块只接收 numpy 数组，不做抓屏、不打开游戏句柄、不读进程内存。
"""
from __future__ import annotations

from .config import Box


def _np():
    import numpy as np

    return np


def gray(frame):
    """取最亮通道的灰度：HUD 血条是亮色，最亮通道比加权平均更抗压缩噪声。"""
    np = _np()
    arr = np.asarray(frame)
    if arr.ndim == 2:
        return arr.astype(np.float32)
    return arr[..., :3].max(axis=2).astype(np.float32)


def crop(frame, box: Box | None):
    if box is None or not box.is_valid():
        return None
    h, w = frame.shape[:2]
    x0, y0 = max(0, min(box.x, w - 1)), max(0, min(box.y, h - 1))
    x1 = max(x0 + 1, min(box.x + box.w, w))
    y1 = max(y0 + 1, min(box.y + box.h, h))
    return frame[y0:y1, x0:x1]


def is_black_frame(frame, threshold: float = 0.6) -> bool:
    """受保护内容黑屏：整帧接近全黑。"""
    return getattr(frame, "size", 0) == 0 or float(frame.mean()) < threshold


def bar_fill_ratio(frame, box: Box | None = None, margin: int = 1) -> float | None:
    """估计亮色血条填充比例（0..1）。"""
    np = _np()
    sub = crop(frame, box) if box is not None else frame
    if sub is None or getattr(sub, "size", 0) == 0:
        return None
    if sub.shape[1] > margin * 2 + 2:
        sub = sub[:, margin:sub.shape[1] - margin]
    g = gray(sub)
    if g.shape[0] >= 4:
        mid0 = g.shape[0] // 4
        mid1 = max(mid0 + 1, g.shape[0] * 3 // 4)
        g = g[mid0:mid1]
    col = g.max(axis=0)
    peak = float(col.max())
    if peak < 110.0:
        return 0.0
    threshold = max(60.0, peak * 0.55)
    return float(np.count_nonzero(col > threshold) / max(1, col.size))


def _runs(row_mask):
    np = _np()
    padded = np.concatenate(([False], row_mask, [False]))
    edges = np.diff(padded.astype(np.int8))
    return list(zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)))


def auto_locate_hp_bar(
    frame,
    brightness: float = 165.0,
    min_run: int = 40,
    min_width_ratio: float = 0.05,
    max_width_ratio: float = 0.35,
) -> Box | None:
    """在画面下部寻找细长 HUD 血条。

    真实血条特征：屏幕下部、接近水平居中、宽度约屏宽 5%~35%、很薄。
    同时用长度、居中程度和竖向厚度打分，避免把菜单分隔线当成血条。
    """
    np = _np()
    h, w = frame.shape[:2]
    y0 = int(h * 0.55)
    band = gray(frame[y0:]) > brightness
    min_len = max(int(min_run), int(w * min_width_ratio))
    max_len = int(w * max_width_ratio)

    best = None
    best_score = float("-inf")
    for row in range(band.shape[0]):
        for start, end in _runs(band[row]):
            length = int(end - start)
            if length < min_len or length > max_len:
                continue
            center = (start + end) / 2.0
            score = length - abs(center - w / 2.0) * 0.6
            if score > best_score:
                best_score, best = score, (row, int(start), int(end))
    if best is None:
        return None

    row, start, end = best
    top = bottom = row
    while top > 0 and band[top - 1, start:end].mean() > 0.45:
        top -= 1
    while bottom + 1 < band.shape[0] and band[bottom + 1, start:end].mean() > 0.45:
        bottom += 1
    thickness = max(3, bottom - top + 1)
    if thickness > 40:
        return None
    return Box(x=start, y=y0 + top, w=end - start, h=thickness)

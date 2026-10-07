"""郊狼 3.0（DG-LAB Socket）波形生成。

波形单元格式（官方 / 社区实测一致）：
    8 字节 HEX = 100 ms 输出 = 4 个 25 ms 子脉冲
    前 4 字节：频率，有效范围 10~240 Hz
    后 4 字节：强度百分比，有效范围 0~100
    任意一个强度值 > 100，整个 100 ms 单元会被 App 丢弃（静音）

注意：这里的「强度百分比」是波形内部的相对强度，
真正的体感强弱由通道强度（strength-…）决定，
两者相乘才是最终输出。所以本项目把「强度旋钮」做在通道强度上，
波形只负责形状（节奏、频率、攻击感）。
"""

from __future__ import annotations

from typing import Callable, Iterable, Sequence

FREQ_MIN = 10
FREQ_MAX = 240
UNIT_MS = 100
SUB_PULSES = 4


def clamp_freq(freq: float) -> int:
    return int(max(FREQ_MIN, min(FREQ_MAX, round(freq))))


def clamp_intensity(intensity: float) -> int:
    # 严禁 >100：官方实现会把整个 100ms 单元作废
    return int(max(0, min(100, round(intensity))))


def unit(freq: float, intensity: float) -> str:
    """生成一个波形单元：4 个相同频率 + 4 个相同强度。"""
    f = clamp_freq(freq)
    i = clamp_intensity(intensity)
    return f"{f:02X}" * SUB_PULSES + f"{i:02X}" * SUB_PULSES


def unit4(freqs: Sequence[float], intensities: Sequence[float]) -> str:
    """生成一个波形单元，4 个子脉冲可独立指定。"""
    if len(freqs) != SUB_PULSES or len(intensities) != SUB_PULSES:
        raise ValueError("unit4 需要 4 个频率与 4 个强度")
    return "".join(f"{clamp_freq(f):02X}" for f in freqs) + "".join(
        f"{clamp_intensity(i):02X}" for i in intensities
    )


def units_for(ms: float) -> int:
    """毫秒换算成波形单元个数（不足一个单元按一个算）。"""
    return max(1, int(round(ms / UNIT_MS)))


def constant(freq: float, intensity: float, ms: float) -> list[str]:
    return [unit(freq, intensity) for _ in range(units_for(ms))]


def ramp(
    freq: float,
    i_from: float,
    i_to: float,
    ms: float,
    freq_to: float | None = None,
) -> list[str]:
    """强度（可同时频率）线性渐变。"""
    n = units_for(ms)
    f_to = freq if freq_to is None else freq_to
    out: list[str] = []
    for k in range(n):
        r = k / max(1, n - 1)
        out.append(unit(freq + (f_to - freq) * r, i_from + (i_to - i_from) * r))
    return out


def pinch(freq: float, intensity: float, ms: float, on_ms: int = 25, off_ms: int = 75) -> list[str]:
    """按捏感：每 100ms 内 1 个 25ms 强脉冲 + 3 个静默。"""
    pattern = ([intensity] * max(1, on_ms // 25)) + [0] * max(1, off_ms // 25)
    pattern = (pattern * 4)[:SUB_PULSES]
    out: list[str] = []
    for _ in range(units_for(ms)):
        out.append(unit4([freq] * SUB_PULSES, pattern))
    return out


def breath(freq: float, peak: float, ms: float) -> list[str]:
    """呼吸波：渐强 → 持续 → 渐弱。"""
    n = units_for(ms)
    out: list[str] = []
    for k in range(n):
        r = k / max(1, n - 1)
        if r < 0.4:
            i = peak * (r / 0.4)
        elif r < 0.7:
            i = peak
        else:
            i = peak * (1 - (r - 0.7) / 0.3)
        out.append(unit(freq, i))
    return out


def heartbeat(freq: float, peak: float, ms: float, period_ms: int = 900) -> list[str]:
    """心跳：咚—咚——，适合低血量提示。"""
    n = units_for(ms)
    period = max(3, int(period_ms / UNIT_MS))
    out: list[str] = []
    for k in range(n):
        pos = k % period
        if pos == 0:
            i = peak
        elif pos == 1:
            i = peak * 0.45
        elif pos == 2 and period > 6:
            i = peak * 0.75
        else:
            i = 0.0
        out.append(unit(freq, i))
    return out


def sting(freq: float, peak: float, ms: float) -> list[str]:
    """短促刺痛：单发快速上升 + 立即归零。"""
    n = units_for(ms)
    out: list[str] = []
    for k in range(n):
        if k == 0:
            i = peak * 0.7
        elif k == 1:
            i = peak
        else:
            i = 0.0
        out.append(unit(freq, i))
    return out


def death_pattern(freq: float, peak: float, ms: float) -> list[str]:
    """阵亡：三次由弱到强的「推力」，段间静默。"""
    n = units_for(ms)
    out: list[str] = []
    stroke = max(3, n // 4)
    gap = max(1, (n - stroke * 3) // 3) if n > stroke * 3 else 0
    peak_scale = (0.55, 0.8, 1.0)
    for s in range(3):
        head = stroke // 3 or 1
        body = max(1, stroke - head)
        for _ in range(head):
            out.append(unit(freq, peak * peak_scale[s] * 0.35))
        for k in range(body):
            out.append(unit(freq * (1.0 - 0.25 * k / max(1, body - 1)), peak * peak_scale[s]))
        for _ in range(gap):
            out.append(unit(freq, 0.0))
    while len(out) < n:
        out.append(unit(freq, 0.0))
    return out[:n]


Builder = Callable[[float, float], list[str]]

#: 波形预设：名字 -> (频率, 峰值强度) -> 单元数组
PRESETS: dict[str, Builder] = {
    "pinch": lambda f, i, ms=400: pinch(f, i, ms),
    "sting": lambda f, i, ms=400: sting(f, i, ms),
    "buzz": lambda f, i, ms=1200: constant(f, peak_to_buzz(i), ms),
    "ramp_up": lambda f, i, ms=1200: ramp(f, i * 0.15, i, ms),
    "breath": lambda f, i, ms=1500: breath(f, i, ms),
    "heartbeat": lambda f, i, ms=1200: heartbeat(f, i, ms),
    "death": lambda f, i, ms=4000: death_pattern(f, i, ms),
}

#: 预设默认频率
PRESET_FREQ: dict[str, float] = {
    "pinch": 60.0,
    "sting": 120.0,
    "buzz": 30.0,
    "ramp_up": 20.0,
    "breath": 10.0,
    "heartbeat": 10.0,
    "death": 25.0,
}


def peak_to_buzz(peak: float) -> float:
    """低频连续波形用满强度即可，无需衰减。"""
    return peak


def build(name: str, duration_ms: float, peak: float = 100.0, freq: float | None = None) -> list[str]:
    """按预设名生成一段波形（总长 = duration_ms）。"""
    key = (name or "pinch").strip().lower()
    f = PRESET_FREQ.get(key, 30.0) if freq is None else float(freq)
    if key not in PRESETS:
        raise KeyError(f"未知波形预设：{name}（可用：{', '.join(sorted(PRESETS))}）")
    return PRESETS[key](f, clamp_intensity(peak), float(duration_ms))


def chunk(units: Sequence[str], max_units: int = 100) -> Iterable[list[str]]:
    """按 App 限制切分（单条消息最多 100 个单元 / 10 秒）。"""
    buf: list[str] = []
    for u in units:
        buf.append(u)
        if len(buf) >= max_units:
            yield buf
            buf = []
    if buf:
        yield buf

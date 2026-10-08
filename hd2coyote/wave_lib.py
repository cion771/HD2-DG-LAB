"""波形库：命名波形（可编辑、可导入导出、可被规则引用）。

三件事：
  1. 解析：rules[].wave / 试打按钮的 wave 都可以引用库里的名字（**库优先于内置预设**）；
  2. 编辑：网页上按 25ms 格子填频率与强度（或直接填 16 进制单元）；
  3. 互通：直接导入/导出 DG-Lab-Punishment 那种 `pulse_data` JSON：
         {"pulse_data": {"受伤": ["0A0A0A0A64646464", ...]},
          "punish_time": {"受伤": 2}}
     这样两边社区攒的波形可以互相搬。

编码规则（官方 V3，见 coyote/v3/README.md）：
  一个单元 = 8 字节 = 4 组 (频率, 强度)，每组 25ms；
  频率 10~240，强度 0~100（**强度 >100 主机会把整个 100ms 单元作废**，所以这里死夹）。
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable

from . import waves
from .config import AppConfig, WaveEntry
from .events import positive_float

UNIT_CHARS = 16
VALID_UNIT = re.compile(r"^[0-9A-Fa-f]{16}$")
NAME_MAX = 64

EXPORT_FORMAT = "hd2-coyote-wave-lib/1"


# --------------------------------------------------------------------- 单元
def normalize_unit(text: Any) -> str:
    """把用户输入的一个单元规范化（去掉 0x/空格/逗号/连字符）；非法返回空串。"""
    t = str(text or "").strip().replace(" ", "").replace(",", "").replace("-", "").replace(":", "")
    if t[:2].lower() == "0x":
        t = t[2:]
    return t.upper() if VALID_UNIT.match(t) else ""


def normalize_units(units: Iterable[Any] | None) -> list[str]:
    out: list[str] = []
    for item in units or []:
        norm = normalize_unit(item)
        if norm:
            out.append(norm)
    return out


def invalid_units(units: Iterable[Any] | None) -> list[str]:
    return [str(u) for u in (units or []) if not normalize_unit(u)]


def unit_from_pair(freq: float, intensity: float) -> str:
    """按官方编码拼一个单元：4 组相同的 (频率,强度)。"""
    return waves.unit(freq, intensity)


def parse_unit(unit: str) -> list[tuple[int, int]]:
    """把一个单元拆成 4 组 (频率, 强度)（给波形编辑器显示用）。"""
    norm = normalize_unit(unit)
    if not norm:
        return []
    return [(int(norm[i * 2:i * 2 + 2], 16), int(norm[8 + i * 2:8 + i * 2 + 2], 16)) for i in range(4)]


# --------------------------------------------------------------------- 解析
def units_of(entry: WaveEntry, duration_ms: float, peak: float | None = None,
             freq: float | None = None) -> list[str]:
    """取出一段时长内的单元列表（自定义单元不够长就循环）。"""
    base = normalize_units(entry.units)
    if base:
        count = max(1, waves.units_for(duration_ms))
        return [base[i % len(base)] for i in range(count)]
    preset = entry.preset or "pinch"
    return waves.build(preset, duration_ms,
                       peak=entry.peak if peak is None else peak,
                       freq=entry.freq if freq is None else freq)


def in_library(cfg: AppConfig, name: str) -> bool:
    return str(name) in cfg.waves.entries


def names(cfg: AppConfig) -> list[str]:
    return sorted(cfg.waves.entries)


def resolve(cfg: AppConfig, name: str, duration_ms: float, peak: float = 100.0,
            freq: float | None = None) -> list[str] | None:
    """库里有这个名字就返回单元，否则 None（调用方回退到内置预设）。"""
    entry = cfg.waves.entries.get(str(name))
    if entry is None:
        return None
    try:
        return units_of(entry, duration_ms, peak=peak, freq=freq)
    except KeyError:
        return None


def catalog(cfg: AppConfig) -> list[dict[str, Any]]:
    """给网页/CLI 看的波形清单。"""
    rows: list[dict[str, Any]] = []
    for name in sorted(cfg.waves.entries):
        entry = cfg.waves.entries[name]
        units = normalize_units(entry.units)
        rows.append({
            "name": name,
            "kind": "units" if units else "preset",
            "preset": entry.preset or ("" if units else "pinch"),
            "units": units,
            "unit_count": len(units),
            "default_ms": entry.default_ms,
            "freq": entry.freq,
            "peak": entry.peak,
            "note": entry.note,
            "invalid": invalid_units(entry.units),
        })
    return rows


# --------------------------------------------------------------------- 增删改
def set_entry(cfg: AppConfig, name: str, *, units: Iterable[Any] | None = None,
              preset: str | None = None, freq: float | None = None, peak: float | None = None,
              default_ms: float | None = None, note: str | None = None) -> WaveEntry:
    """新增/更新一个命名波形（units 与 preset 互斥，后写的赢）。"""
    key = str(name or "").strip()
    if not key:
        raise ValueError("波形名字不能为空")
    if len(key) > NAME_MAX:
        raise ValueError(f"波形名字太长（≤{NAME_MAX} 字符）")

    entry = cfg.waves.entries.get(key) or WaveEntry()
    if units is not None:
        raw = [str(u) for u in units if str(u).strip()]
        bad = invalid_units(raw)
        if bad:
            raise ValueError(f"波形单元必须是 {UNIT_CHARS} 个十六进制字符（8 字节），收到：{bad[0][:24]}")
        entry.units = normalize_units(raw)
        if entry.units:
            entry.preset = ""
    if preset is not None:
        key_preset = str(preset).strip()
        if key_preset and key_preset not in waves.PRESETS:
            raise ValueError(f"未知预设：{key_preset}（可用：{', '.join(sorted(waves.PRESETS))}）")
        entry.preset = key_preset
        if key_preset:
            entry.units = []
    if freq is not None:
        value = positive_float(freq, 0.0)
        entry.freq = value if value > 0 else None
    if peak is not None:
        entry.peak = max(0.0, min(100.0, positive_float(peak, 100.0)))
    if default_ms is not None:
        entry.default_ms = max(0.0, min(60000.0, positive_float(default_ms, 0.0)))
    if note is not None:
        entry.note = str(note)[:200]
    if not entry.units and not entry.preset:
        entry.preset = "pinch"
    cfg.waves.entries[key] = entry
    return entry


def remove_entry(cfg: AppConfig, name: str) -> bool:
    return cfg.waves.entries.pop(str(name), None) is not None


# --------------------------------------------------------------------- 互通
def import_pulse_data(cfg: AppConfig, data: Any, replace: bool = False) -> dict[str, Any]:
    """导入波形（兼容参考项目的 default.json 与我们的导出格式）。"""
    if not isinstance(data, dict):
        raise ValueError("导入的 JSON 必须是对象")
    payload = data.get("pulse_data") or data.get("waves") or data.get("entries")
    if not isinstance(payload, dict):
        # 也可能是「直接就是 {名字: [单元...]}」
        payload = {k: v for k, v in data.items()
                   if isinstance(v, (list, tuple, str)) or (isinstance(v, dict) and
                                                           ("units" in v or "preset" in v))}
    if not isinstance(payload, dict):
        raise ValueError("找不到波形数据（期望 pulse_data / entries / {名字: [单元...]}）")
    times = data.get("punish_time") if isinstance(data.get("punish_time"), dict) else {}
    presets = data.get("presets") if isinstance(data.get("presets"), dict) else {}
    # 只有预设的条目在导出时被单独放进 presets（不污染 pulse_data），这里读回来，
    # 保证 to_text() → from_text() 是无损的往返。
    merged: dict[Any, Any] = dict(presets)
    merged.update(payload)

    if replace:
        cfg.waves.entries.clear()
    imported: list[str] = []
    skipped: list[str] = []
    for raw_name, value in merged.items():
        name = str(raw_name).strip()
        if not name or name in ("format", "note", "version"):
            continue
        if isinstance(value, str) and not normalize_units([value]):
            # presets 里也可能写成 {"死亡": "death"} 这种简写
            try:
                entry = WaveEntry(preset=str(value))
                if entry.preset not in waves.PRESETS:
                    raise KeyError(entry.preset)
            except KeyError:
                skipped.append(name)
                continue
        else:
            entry = _entry_from_value(value)
        if entry is None:
            skipped.append(name)
            continue
        if entry.default_ms <= 0 and raw_name in times:
            entry.default_ms = max(0.0, positive_float(times.get(raw_name), 0.0) * 1000.0)
        cfg.waves.entries[name[:NAME_MAX]] = entry
        imported.append(name[:NAME_MAX])
    return {"ok": True, "imported": imported, "skipped": skipped,
            "count": len(imported), "replaced": bool(replace)}


def _entry_from_value(value: Any) -> WaveEntry | None:
    if isinstance(value, (list, tuple)):
        units = normalize_units(value)
        return WaveEntry(units=units) if units else None
    if isinstance(value, str):
        units = normalize_units([value])
        return WaveEntry(units=units) if units else None
    if not isinstance(value, dict):
        return None
    units = normalize_units(value.get("units") or value.get("pulse_data") or [])
    preset = str(value.get("preset") or "").strip()
    if not units and not preset:
        return None
    freq = value.get("freq")
    return WaveEntry(
        units=units,
        preset="" if units else preset,
        freq=None if freq is None else positive_float(freq, 0.0) or None,
        peak=positive_float(value.get("peak"), 100.0),
        default_ms=positive_float(value.get("default_ms") or value.get("ms"), 0.0),
        note=str(value.get("note") or "")[:200],
    )


def export_pulse_data(cfg: AppConfig) -> dict[str, Any]:
    """导出（pulse_data 与参考项目互通；预设单独放 presets，别污染 pulse_data）。"""
    pulse: dict[str, list[str]] = {}
    times: dict[str, float] = {}
    presets: dict[str, dict[str, Any]] = {}
    for name in sorted(cfg.waves.entries):
        entry = cfg.waves.entries[name]
        units = normalize_units(entry.units)
        if units:
            pulse[name] = units
            if entry.default_ms > 0:
                times[name] = round(entry.default_ms / 1000.0, 3)
        else:
            presets[name] = {"preset": entry.preset or "pinch", "freq": entry.freq,
                             "peak": entry.peak, "default_ms": entry.default_ms,
                             "note": entry.note}
    return {"format": EXPORT_FORMAT, "note": "pulse_data 与 DG-Lab-Punishment 的格式互通",
            "pulse_data": pulse, "punish_time": times, "presets": presets}


def to_text(cfg: AppConfig) -> str:
    return json.dumps(export_pulse_data(cfg), ensure_ascii=False, indent=2)


def from_text(cfg: AppConfig, text: str, replace: bool = False) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON 解析失败：{exc}") from exc
    return import_pulse_data(cfg, data, replace=replace)


def apply_preset_entry(cfg: AppConfig, name: str, preset: str, freq: float | None = None,
                       peak: float | None = None, default_ms: float | None = None) -> WaveEntry:
    """便捷写法：把一个内置预设存成命名波形。"""
    return set_entry(cfg, name, preset=preset, freq=freq, peak=peak, default_ms=default_ms)

"""配置模型与读写。

全部配置集中在一个 JSON 文件里（默认 config.json），
UI、CLI、标定向导都读写同一个 AppConfig。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Literal


@dataclass
class Box:
    """屏幕上的矩形区域（像素，屏幕绝对坐标）。"""

    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0

    def is_valid(self) -> bool:
        return self.w > 1 and self.h > 1

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, self.w, self.h)


@dataclass
class DeviceConfig:
    """设备连接配置。"""

    kind: Literal["socket", "mock"] = "socket"
    host: str = "0.0.0.0"  # 监听地址；App 通过局域网连过来
    port: int = 9999
    channel: Literal["both", "A", "B"] = "both"  # 规则未指定时使用的默认通道
    advertise_ip: str = ""  # 留空 = 自动探测本机局域网 IP


@dataclass
class HookConfig:
    """游戏内 addon（Lua 桥）的 UDP 上报端口。"""

    host: str = "127.0.0.1"
    port: int = 47777
    timeout_s: float = 2.0  # 超过这么久没有状态 -> 视为断流并静音（dead-man）


@dataclass
class CaptureConfig:
    """抓屏配置（仅在 source = vision 时使用）。"""

    monitor: int = 1  # mss 显示器编号，1 = 主屏
    region: Box | None = None  # None = 整屏（标定时用整屏）
    fps: float = 15.0
    backend: Literal["auto", "dxcam", "mss", "pil"] = "auto"


@dataclass
class HudConfig:
    """HUD 区域。坐标由标定向导写入，默认值仅作 16:9 的粗略参考。"""

    hp_bar: Box | None = None
    injury_zone: Box | None = None  # 损伤图标条（血条左侧）
    injury_slots: int = 3  # 把图标条横向分成几格
    injury_slot_names: list[str] = field(default_factory=lambda: ["左肢", "躯干", "右肢"])
    death_probe: Box | None = None  # 阵亡判定用的模板区域（可选）
    death_template: str = ""  # 模板 PNG 路径（可选，优先级高于 death_probe）


@dataclass
class DetectConfig:
    """检测阈值。"""

    damage_min_pct: float = 4.0  # 单次掉血超过最大血量的百分之几才算一次伤害
    low_health_pct: float = 35.0  # 低于该血量进入低血量状态
    recover_pct: float = 60.0  # 回升到该血量解除低血量状态
    dead_hold_s: float = 0.8  # 血条空持续多久判定为阵亡
    revive_ignore_damage_s: float = 2.0  # 复活后忽略伤害的时间（重生动画期间）
    injury_min_fraction: float = 0.015  # 图标格内橙红色像素占比阈值
    injury_clear_s: float = 0.5  # 图标消失多久后才允许再次触发（防抖）
    bleeding_min_fraction: float = 0.01
    death_template_threshold: float = 0.72  # 模板匹配相似度阈值
    capture_error_limit: int = 30  # 连续抓屏失败多少次自动停止


@dataclass
class RuleConfig:
    """单条规则（事件 -> 电击）。"""

    enabled: bool = True
    base_pct: float = 20.0  # 基础强度，百分比（相对通道上限）
    wave: str = "pinch"
    duration_ms: float = 400.0
    channel: Literal["default", "A", "B", "both", "alternate"] = "default"
    cooldown_ms: float = 500.0
    priority: int = 1
    freq: float | None = None
    # 仅伤害规则使用：
    per_10hp: float = 6.0  # 每掉 10% 血额外增加的强度百分点
    # 仅死亡规则使用：
    repeat_ms: float = 0.0  # >0 时在持续时间内重复触发
    # 仅低血量规则使用：
    period_ms: float = 1500.0


@dataclass
class SafetyConfig:
    """安全限制。这些值在任何情况下都优先于规则强度。"""

    max_pct: float = 30.0  # 任何单次输出的强度上限（百分比，相对通道上限）
    max_absolute: int = 40  # 强度绝对值上限（App 单位 0~200）
    attack_ms: float = 150.0  # 起效渐变时间
    release_ms: float = 800.0  # 释放（回落到 0）时间
    max_event_ms: float = 8000.0  # 单个事件最长输出时长
    emergency_key: str = "F12"  # 急停热键（Windows 虚拟键名）
    pause_key: str = "F11"  # 暂停/恢复检测热键
    mute_on_focus_loss: bool = False  # 游戏窗口失去焦点时静音
    game_window_title: str = "HELLDIVERS"  # 焦点检测用
    max_session_seconds: float = 0.0  # >0 时累计输出超过该秒数自动静音
    master_multiplier: float = 1.0  # 总强度倍率（0~2）


@dataclass
class AppConfig:
    """根配置。"""

    #: 状态来源：hook = 游戏内 Lua addon（推荐）；vision = 屏幕识别（备用/无需装 mod）
    source: Literal["hook", "vision"] = "hook"
    device: DeviceConfig = field(default_factory=DeviceConfig)
    hook: HookConfig = field(default_factory=HookConfig)
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    hud: HudConfig = field(default_factory=HudConfig)
    detect: DetectConfig = field(default_factory=DetectConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    rules: dict[str, RuleConfig] = field(
        default_factory=lambda: {
            "damage": RuleConfig(
                base_pct=16, per_10hp=7, wave="pinch", duration_ms=400,
                channel="A", cooldown_ms=400, priority=1,
            ),
            "limb_injury": RuleConfig(
                base_pct=30, wave="ramp_up", duration_ms=1200,
                channel="both", cooldown_ms=1500, priority=2,
            ),
            "death": RuleConfig(
                base_pct=45, wave="death", duration_ms=4000,
                channel="both", cooldown_ms=5000, priority=5, repeat_ms=1500,
            ),
            "low_health": RuleConfig(
                enabled=False, base_pct=12, wave="heartbeat", duration_ms=1200,
                channel="both", cooldown_ms=0, priority=0, period_ms=1500,
            ),
        }
    )
    log_level: str = "INFO"

    # ------------------------------------------------------------------ IO
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "AppConfig":
        p = Path(path)
        if not p.exists():
            cfg = cls()
            cfg.save(p)
            return cfg
        raw = json.loads(p.read_text(encoding="utf-8"))
        return from_dict(cls, raw)


#: AppConfig 里嵌套的 dataclass 字段
NESTED_TYPES: dict[str, type] = {
    "device": DeviceConfig,
    "hook": HookConfig,
    "capture": CaptureConfig,
    "hud": HudConfig,
    "detect": DetectConfig,
    "safety": SafetyConfig,
}

BOX_FIELDS = {"hp_bar", "injury_zone", "death_probe", "region"}


def from_dict(cls: type, data: dict[str, Any]) -> Any:
    """把 dict 还原为 dataclass（含嵌套 dataclass / Box / rules），忽略未知键。"""
    if data is None:
        return None
    if cls is Box:
        return Box(**{k: int(v) for k, v in data.items() if k in {"x", "y", "w", "h"}})
    if cls is RuleConfig:
        known = {f.name for f in fields(RuleConfig)}
        return RuleConfig(**{k: v for k, v in data.items() if k in known})
    if not is_dataclass(cls):
        return data
    out: dict[str, Any] = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        v = data[f.name]
        if f.name in NESTED_TYPES:
            out[f.name] = from_dict(NESTED_TYPES[f.name], v)
        elif f.name == "rules":
            out[f.name] = {k: from_dict(RuleConfig, vv) for k, vv in (v or {}).items()}
        elif f.name in BOX_FIELDS:
            out[f.name] = None if v is None else from_dict(Box, v)
        else:
            out[f.name] = v
    return cls(**out)


def default_config() -> AppConfig:
    return AppConfig()

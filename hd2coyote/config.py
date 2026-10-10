"""配置模型与读写。

全部配置集中在一个 JSON 文件里（默认 config.json），
UI、CLI都读写同一个 AppConfig。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Literal


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
class SourcesConfig:
    """事件源（插件）开关。

    事件源只负责把外部世界翻译成 Event，所有事件依旧要过规则层与安全层，
    所以「多接一个源」不会绕过任何上限。名字即 hd2coyote/sources 里注册的源。
    """

    enabled: list[str] = field(default_factory=lambda: ["game_bridge"])
    #: HTTP 事件源：任何外部程序 POST 一行 JSON 就能触发（详见 docs/SOURCES.md）
    http_host: str = "127.0.0.1"
    http_port: int = 47778
    http_token: str = ""  # 非空时要求 X-HD2Coyote-Token 头（或 ?token=）
    http_max_per_s: float = 20.0  # 限速：超过就丢弃并告警，避免外部脚本刷爆电极
    http_timeout_s: float = 3.0  # 收到过 state 后超过这么久没数据 = 断流（0 = 不判）


@dataclass
class HudConfig:
    """桥接肢体状态配置；沿用 hud 键兼容旧配置。"""

    injury_slots: int = 3  # 上报的肢体槽位数
    injury_slot_names: list[str] = field(default_factory=lambda: ["左肢", "躯干", "右肢"])


@dataclass
class DetectConfig:
    """检测阈值。"""

    damage_min_pct: float = 4.0  # 单次掉血超过最大血量的百分之几才算一次伤害
    low_health_pct: float = 35.0  # 低于该血量进入低血量状态
    recover_pct: float = 60.0  # 回升到该血量解除低血量状态
    dead_hold_s: float = 0.8  # 血量为零持续多久判定为阵亡
    revive_ignore_damage_s: float = 2.0  # 复活后忽略伤害的时间（重生动画期间）
    injury_min_fraction: float = 0.015  # 肢体状态阈值（桥上报 0/1）
    injury_clear_s: float = 0.5  # 损伤解除多久后才允许再次触发（防抖）
    bleeding_min_fraction: float = 0.01  # 流血状态阈值（桥上报 0/1）
    death_state_threshold: float = 0.72  # 阵亡状态阈值（桥上报 0/1）


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
class WaveEntry:
    """波形库里的一个命名波形。

    两种写法二选一：
      * units：直接给 16 进制单元（每个 8 字节 = 4 个 25ms，如 "0A0A0A0A64646464"），
        不够长就循环 —— 这就是「波形」的本意，也方便和别的工具互通；
      * preset + freq/peak：引用内置生成器（pinch/sting/buzz/ramp_up/breath/heartbeat/death）。
    """

    units: list[str] = field(default_factory=list)
    preset: str = ""
    freq: float | None = None
    peak: float = 100.0
    default_ms: float = 0.0  # 建议时长（从参考项目的 punish_time 导入）
    note: str = ""


@dataclass
class WaveLibConfig:
    """命名波形库：rules 里的 wave 可以引用这里的名字（**库优先于内置预设**）。"""

    entries: dict[str, WaveEntry] = field(default_factory=dict)


@dataclass
class RampConfig:
    """惩罚累积模型：伤害会攒起来，血越少越强，长时间没挨打慢慢回落。

    参考 DG-Lab-Punishment 的「血量越少强度越高」，但保留我们的安全模型：
    这里算出来的只是「意愿」，最终仍然被 safety.max_pct / max_absolute 硬夹。
    """

    enabled: bool = False
    per_event: float = 2.0  # 每个伤害/损伤事件累加的百分点
    hp_missing_pct: float = 20.0  # 血量掉光时按缺失比例额外抬升的百分点
    ceiling_pct: float = 25.0  # 累积上限
    decay_after_s: float = 4.0  # 超过这么久没有新事件就开始回落
    decay_per_s: float = 1.5  # 回落速度（百分点/秒）
    reset_on_death: bool = True  # 阵亡 / 被增援后清零
    apply_to: list[str] = field(default_factory=lambda: ["damage", "limb_injury"])


@dataclass
class UpdateConfig:
    """检查更新（只提示 + 给链接，**绝不自动下载安装**）。"""

    enabled: bool = True
    repo: str = "cion771/hd2-coyote"
    check_on_start: bool = False
    timeout_s: float = 6.0


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

    #: 兼容旧配置；启用的事件源由 sources.enabled 决定。
    source: Literal["hook"] = "hook"
    device: DeviceConfig = field(default_factory=DeviceConfig)
    hook: HookConfig = field(default_factory=HookConfig)
    sources: SourcesConfig = field(default_factory=SourcesConfig)
    hud: HudConfig = field(default_factory=HudConfig)
    detect: DetectConfig = field(default_factory=DetectConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    waves: WaveLibConfig = field(default_factory=WaveLibConfig)
    ramp: RampConfig = field(default_factory=RampConfig)
    update: UpdateConfig = field(default_factory=UpdateConfig)
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

    def __post_init__(self) -> None:
        # 旧 vision 配置不再抓屏，安全迁移为事件源模式。
        self.source = "hook"

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
    "sources": SourcesConfig,
    "hud": HudConfig,
    "detect": DetectConfig,
    "safety": SafetyConfig,
    "waves": WaveLibConfig,
    "ramp": RampConfig,
    "update": UpdateConfig,
}



def from_dict(cls: type, data: dict[str, Any]) -> Any:
    """把 dict 还原为 dataclass（含嵌套 dataclass / rules / 波形库），忽略未知键。"""
    if data is None:
        return None
    if cls is RuleConfig:
        known = {f.name for f in fields(RuleConfig)}
        return RuleConfig(**{k: v for k, v in data.items() if k in known})
    if cls is WaveEntry:
        known = {f.name for f in fields(WaveEntry)}
        out: dict[str, Any] = {}
        for k, v in data.items():
            if k not in known:
                continue
            if k == "units":
                out[k] = [str(u) for u in v] if isinstance(v, (list, tuple)) else []
            elif k == "freq" and v is not None:
                out[k] = float(v)
            else:
                out[k] = v
        return WaveEntry(**out)
    if cls is WaveLibConfig:
        entries = data.get("entries") or {}
        return WaveLibConfig(entries={
            str(k): from_dict(WaveEntry, v) for k, v in entries.items() if isinstance(v, dict)
        })
    if cls is DetectConfig and "death_template_threshold" in data:
        data = dict(data)
        data.setdefault("death_state_threshold", data["death_template_threshold"])
    if not is_dataclass(cls):
        return data
    out = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        v = data[f.name]
        if f.name in NESTED_TYPES:
            out[f.name] = from_dict(NESTED_TYPES[f.name], v)
        elif f.name == "rules":
            out[f.name] = {k: from_dict(RuleConfig, vv) for k, vv in (v or {}).items()}
        elif f.name == "enabled" and cls is SourcesConfig:
            # 事件源列表：只收字符串，空列表回退到默认（别把控制器变成聋子）
            items = [str(x) for x in v] if isinstance(v, (list, tuple)) else []
            out[f.name] = items or ["game_bridge"]
        else:
            out[f.name] = v
    return cls(**out)


def default_config() -> AppConfig:
    return AppConfig()

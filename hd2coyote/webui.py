"""Web 控制台：用浏览器管理 hd2-coyote（状态 / 强度 / 桥配置 / 诊断）。

设计取舍：
  * 只用标准库（`http.server` + 内联 HTML/JS），**不引任何前端依赖**，离线可用；
  * 默认只监听 127.0.0.1 —— 这个页面能触发实际输出，别裸奔到局域网；
  * 桥（游戏内 Lua）的配置是**文件**，改完要重启游戏才生效；页面里会明确提示；
  * 诊断区直接给 addon 的 STATUS / 日志尾部 / 加载器日志行 —— 这几轮排查都靠它们。

路由：
    GET  /                 单页控制台
    GET  /qr.svg           手机 App 扫码用的二维码（装了 qrcode 时）
    GET  /api/status       控制器 + 桥 + 诊断的实时快照
    GET  /api/config       控制器配置（config.json）
    GET  /api/sources      可用事件源清单 + 每个源此刻的运行状态
    GET  /api/waves        命名波形库 + 内置预设清单
    GET  /api/update       上次检查更新的结果（缓存的）
    POST /api/config       改控制器配置（局部合并、落盘、立即生效）
    POST /api/bridge       改游戏内桥的配置（写 bridge_config.lua，带备份）
    POST /api/actions      控制：start / stop / arm / trip / test_pulse / shutdown
    POST /api/event        手动注入一个事件/状态（调试用；照样过规则与安全上限）
    POST /api/waves        波形库增删改 / 导入导出 / 试打
    POST /api/update       真的去查一次 GitHub Releases（只提示，不自动安装）
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import webbrowser
from dataclasses import asdict
from functools import wraps
from urllib.parse import urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import __version__, update_check, wave_lib, waves
from .config import AppConfig
from .engine import Engine, _fmt_event
from .events import truthy
from .sources import source_catalog
from .sources.http import HttpEventSource, looks_known

LOGGER = logging.getLogger("hd2coyote.webui")

BRIDGE_MODES = ("safe", "net", "menu", "recon", "live")
BRIDGE_DEFAULT_DIR = Path.home() / "AppData" / "Local" / "hd2coyote"
OFFSET_KEYS = {"hp": 0x20, "hp_max": 0x24, "limb_mask": 0x28, "limb_shift": 0, "dead": 0x2C}
OFFSET_RANGE = {"hp": (-1, 0xFFFF), "hp_max": (-1, 0xFFFF), "limb_mask": (-1, 0xFFFF),
                "limb_shift": (0, 7), "dead": (-1, 0xFFFF)}


def is_loopback(addr: str) -> bool:
    """只允许本机来源执行「关闭程序」——万一有人把它开在 0.0.0.0 上，也不能被远端关掉。"""
    if not addr:
        return False
    host = addr.split("%", 1)[0]          # 去掉 IPv6 的 scope id
    return host in ("127.0.0.1", "::1", "localhost") or host.startswith("127.")


# --------------------------------------------------------------------- 桥配置
def bridge_dir(override: str | Path | None = None) -> Path:
    return Path(override) if override else BRIDGE_DEFAULT_DIR


def bridge_config_path(override: str | Path | None = None) -> Path:
    return bridge_dir(override) / "bridge_config.lua"


def render_bridge_config(values: dict[str, Any]) -> str:
    """把桥配置渲染成 bridge_config.lua 的内容（Lua 形式，人也能看懂）。"""
    mode = str(values.get("mode", "safe"))
    if mode not in BRIDGE_MODES:
        raise ValueError(f"非法 mode：{mode}（可选 {'/'.join(BRIDGE_MODES)}）")
    port = int(values.get("port", 47777))
    if not 1024 <= port <= 65535:
        raise ValueError("port 必须在 1024..65535")
    interval = float(values.get("interval", 0.1))
    if not 0.02 <= interval <= 5.0:
        raise ValueError("interval 必须在 0.02..5.0 秒")
    profile = str(values.get("profile", "steam_25480438"))
    if not re.fullmatch(r"[A-Za-z0-9_]+", profile):   # 注意：Python 正则，不是 Lua 的 %w
        raise ValueError("profile 只能包含字母/数字/下划线")

    offsets = values.get("offsets") or {}
    lines = []
    for key in ("hp", "hp_max", "limb_mask", "limb_shift", "dead"):
        raw = offsets.get(key, OFFSET_KEYS[key])
        value = int(raw)
        lo, hi = OFFSET_RANGE[key]
        if not lo <= value <= hi:
            raise ValueError(f"{key} 必须在 {lo}..{hi}")
        if key == "limb_shift":
            lines.append(f"      limb_shift = {value},")
        else:
            lines.append(f"      {key} = {'nil' if value < 0 else hex(value)},")

    return (
        "-- 由 hd2-coyote Web 控制台生成（改完重启游戏生效）\n"
        "-- 档位：safe(只写日志) / net(+UDP) / menu(+游戏内菜单) / recon(内存侦察) / live\n"
        "return {\n"
        "  config = {\n"
        f"    mode = '{mode}',\n"
        f"    port = {port},\n"
        f"    interval = {interval},\n"
        f"    profile = '{profile}',\n"
        "  },\n"
        "  profiles = {\n"
        f"    {profile} = {{\n"
        + "\n".join(lines) + "\n"
        "    },\n"
        "  },\n"
        "}\n"
    )


def parse_bridge_config(text: str) -> dict[str, Any]:
    """从 bridge_config.lua 里粗略读出关键值（只用于页面回显，不做求值）。"""
    out: dict[str, Any] = {"mode": "safe", "port": 47777, "interval": 0.1,
                           "profile": "steam_25480438", "offsets": dict(OFFSET_KEYS)}
    m = re.search(r"mode\s*=\s*'([A-Za-z0-9_]+)'", text)
    if m:
        out["mode"] = m.group(1)
    m = re.search(r"port\s*=\s*(\d+)", text)
    if m:
        out["port"] = int(m.group(1))
    m = re.search(r"interval\s*=\s*([\d.]+)", text)
    if m:
        out["interval"] = float(m.group(1))
    m = re.search(r"profile\s*=\s*'([A-Za-z0-9_]+)'", text)
    if m:
        out["profile"] = m.group(1)
    for key in ("hp", "hp_max", "limb_mask", "limb_shift", "dead"):
        m = re.search(rf"^\s*{key}\s*=\s*(nil|0x[0-9A-Fa-f]+|\d+)", text, re.MULTILINE)
        if not m:
            continue
        raw = m.group(1)
        out["offsets"][key] = -1 if raw == "nil" else int(raw, 0)
    return out


def write_bridge_config(values: dict[str, Any], override: str | Path | None = None) -> Path:
    path = bridge_config_path(override)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = render_bridge_config(values)
    if path.exists():
        backup = path.with_suffix(".lua.bak")
        try:
            backup.write_bytes(path.read_bytes())
        except OSError as exc:
            LOGGER.warning("备份 %s 失败：%s", path, exc)
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------- 诊断
def _tail(path: Path, limit: int = 40) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return lines[-limit:]


def bridge_diagnostics(bridge_cfg_dir: str | Path | None = None) -> dict[str, Any]:
    """桥的现状：配置文件、addon 自己的 STATUS/日志、加载器日志行。"""
    directory = bridge_dir(bridge_cfg_dir)
    path = bridge_config_path(bridge_cfg_dir)
    info: dict[str, Any] = {
        "dir": str(directory),
        "config_path": str(path),
        "exists": path.exists(),
        "text": path.read_text(encoding="utf-8") if path.exists() else "",
        "parsed": {},
        "status": "",
        "log_tail": [],
        "loader_line": "",
        "loader_log": "",
    }
    if info["exists"]:
        info["parsed"] = parse_bridge_config(info["text"])

    status_file = directory / "hd2_coyote_status.txt"
    if status_file.exists():
        info["status"] = status_file.read_text(encoding="utf-8", errors="replace").strip()
    info["log_tail"] = _tail(directory / "hd2_coyote_bridge.log")
    report = directory / "recon_report.txt"
    if report.exists():
        info["recon_report"] = _tail(report, 80)

    loader_log_dir = Path.home() / "AppData" / "Local" / "CowboyBingus" / "Helldivers2" / "Logs"
    loader_log = loader_log_dir / "BingusSharedLoader.log"
    info["loader_log"] = str(loader_log)
    if loader_log.exists():
        for line in loader_log.read_text(encoding="utf-8", errors="replace").splitlines():
            if "hd2coyote" in line:
                info["loader_line"] = line.strip()
    shared = loader_log_dir / "Hd2CoyoteBridge.log"
    if shared.exists():
        info["shared_log_tail"] = _tail(shared, 20)
    return info


# --------------------------------------------------------------------- 服务器
class _Handler(BaseHTTPRequestHandler):
    server_version = f"hd2coyote/{__version__}"
    app: "WebApp" = None  # type: ignore[assignment]

    # ------------------------------------------------------------ 工具
    def log_message(self, fmt: str, *args: Any) -> None:  # 别把访问日志刷进控制台
        LOGGER.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: Any, code: int = 200) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _read_body(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0 or length > 1024 * 1024:
            raise ValueError("请求体不能超过 1 MiB")
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("请求体必须是有效 JSON") from exc

    def _trusted_request(self, write: bool = False) -> bool:
        # 限制 loopback 服务的 Host，阻止 DNS rebinding；CLI 无 Origin 可正常调用。
        host = self.headers.get("Host", "")
        try:
            parsed = urlsplit("http://" + host)
            bound_host, bound_port = self.server.server_address[:2]
            if parsed.port != bound_port:
                return False
            if is_loopback(bound_host) and parsed.hostname not in (bound_host, "localhost", "127.0.0.1", "::1"):
                return False
            origin = self.headers.get("Origin")
            if origin and origin != "http://" + host:
                return False
        except ValueError:
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            return False
        return not write or self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() == "application/json"

    # ------------------------------------------------------------ 路由
    def do_GET(self) -> None:  # noqa: N802
        if not self._trusted_request():
            self._json({"error": "只允许受信任的本机 / 同源请求"}, 403)
            return
        route = self.path.split("?", 1)[0]
        if route in ("/", "/index.html"):
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif route == "/qr.svg":
            svg = self.app.qr_svg()
            if svg:
                self._send(200, svg, "image/svg+xml")
            else:
                self._json({"error": "二维码不可用（设备未启动或未安装 qrcode）"}, 404)
        elif route == "/api/status":
            self._json(self.app.status())
        elif route == "/api/config":
            self._json(self.app.config_dict())
        elif route == "/api/sources":
            self._json(self.app.sources_dict())
        elif route == "/api/waves":
            self._json(self.app.waves_dict())
        elif route == "/api/update":
            self._json(self.app.update_info())
        else:
            self._json({"error": "not found"}, 404)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        route = self.path.split("?", 1)[0]
        if not self._trusted_request(write=True):
            self._json({"error": "需要同源 application/json 请求"}, 403)
            return
        try:
            body = self._read_body()
            if not isinstance(body, dict) and route != "/api/event":
                # /api/event 允许一次注入一串（数组），其它端点只要对象
                self._json({"error": "请求体必须是 JSON 对象"}, 400)
                return
            if route == "/api/actions" and isinstance(body, dict) \
                    and str(body.get("action")) == "shutdown" \
                    and not is_loopback(self.client_address[0]):
                self._json({"error": "只允许本机（127.0.0.1）关闭程序"}, 403)
                return
            if route == "/api/config":
                self._json(self.app.update_config(body))
            elif route == "/api/bridge":
                self._json(self.app.update_bridge(body))
            elif route == "/api/actions":
                self._json(self.app.action(body))
            elif route == "/api/event":
                self._json(self.app.event_api(body))
            elif route == "/api/waves":
                self._json(self.app.waves_api(body))
            elif route == "/api/update":
                self._json(self.app.update_api(body))
            else:
                self._json({"error": "not found"}, 404)
        except ValueError as exc:
            self._json({"error": str(exc)}, 400)
        except Exception as exc:  # 任何异常都返回 JSON，别把页面搞崩
            LOGGER.exception("处理 %s 失败", route)
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)


def mutating(method):
    """窗口关闭与 HTTP 写操作共用锁，关闭开始后不允许重新启动硬件。"""
    @wraps(method)
    def guarded(self, *args, **kwargs):
        with self.lifecycle_lock:
            if self.closing:
                raise ValueError("控制器正在关闭，拒绝新的操作")
            return method(self, *args, **kwargs)
    return guarded


class WebApp:
    """把 Engine / 配置 / 桥诊断包成 JSON API。"""

    def __init__(self, cfg: AppConfig, config_path: Path,
                 bridge_cfg_dir: str | Path | None = None) -> None:
        self.cfg = cfg
        self.config_path = Path(config_path)
        self.bridge_cfg_dir = bridge_cfg_dir
        self.engine = Engine(cfg)
        self.engine.trip("启动后请手动重新武装")
        self.lifecycle_lock = threading.RLock()
        self.closing = False
        self.events: list[str] = []
        self.engine.on_event = self._on_event
        self._lock = threading.Lock()
        self.started_at = time.time()
        # 检查更新的缓存（页面只读它，避免每次刷新都去戳 GitHub）
        self.update_result: dict[str, Any] | None = None
        # 手动注入用：HTTP 源没启用时，自己留一个只做解析的实例（不监听端口）
        self._manual_source: HttpEventSource | None = None
        # 「关闭程序」：置位后由服务器主循环退出（见 build_server / run_web）
        self.shutdown_event = threading.Event()
        self.shutdown_reason = ""

    # ------------------------------------------------------------ 内部
    def _note(self, text: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        with self._lock:
            self.events.append(f"{stamp}  {text}")
            if len(self.events) > 200:
                del self.events[:100]

    def _on_event(self, event: Any) -> None:
        self._note(_fmt_event(event))

    # ------------------------------------------------------------ API
    def status(self) -> dict[str, Any]:
        engine = self.engine
        st = engine.status
        hook = engine.hook
        with self._lock:
            events = list(self.events[-60:])
        device_name = getattr(engine.device, "name", self.cfg.device.kind)
        qr_url = getattr(engine.device, "ws_url", "")
        return {
            "controller": {
                "version": __version__,
                "uptime_s": round(time.time() - self.started_at, 1),
                "running": st.running,
                "detecting": st.detecting,
                "source": self.cfg.source,
                "hp": st.hp,
                "limbs": list(st.injury),
                "output": {"a": st.output_a, "b": st.output_b, "pct": round(st.pct, 1)},
                "armed": engine.safety.armed,
                "mute_reason": engine.safety.mute_reason,
                "session_seconds": round(engine.safety.session_seconds, 1),
                "device": {
                    "kind": device_name,
                    "connected": engine.device.connected,
                    "limit_a": engine.device.limits.a,
                    "limit_b": engine.device.limits.b,
                    "qr_url": qr_url,
                },
                "hook": {
                    "alive": st.hook_alive,
                    "detail": st.hook_detail,
                    "version": getattr(hook, "state", None) and hook.state.version or "",
                    "build": getattr(hook, "state", None) and hook.state.build or "",
                    "profile": getattr(hook, "state", None) and hook.state.profile or "",
                    "packets": getattr(hook, "state", None) and hook.state.packets or 0,
                },
                "last_error": st.last_error,
                "sources": list(st.sources),
                "ramp": {
                    "enabled": bool(self.cfg.ramp.enabled),
                    "pct": round(st.ramp_pct, 1),
                    "detail": st.ramp_detail or "-",
                    "ceiling_pct": float(self.cfg.ramp.ceiling_pct),
                },
            },
            "events": events,
            "bridge": bridge_diagnostics(self.bridge_cfg_dir),
            "update": self.update_info(),
        }

    def config_dict(self) -> dict[str, Any]:
        return self.cfg.to_dict()

    @mutating
    def update_config(self, patch: dict[str, Any]) -> dict[str, Any]:
        """局部合并控制器配置：rules / safety / device / hook / source。"""
        applied: list[str] = []
        if "source" in patch:
            source = str(patch["source"])
            if source != "hook":
                raise ValueError("屏幕识别已移除；source 只能是 hook，事件源请在 sources.enabled 中配置")
            self.cfg.source = source
            applied.append(f"source={source}")

        rules = patch.get("rules") or {}
        if isinstance(rules, dict):
            for name, values in rules.items():
                rule = self.cfg.rules.get(name)
                if rule is None or not isinstance(values, dict):
                    continue
                for key, value in values.items():
                    if hasattr(rule, key):
                        setattr(rule, key, value)
                applied.append(f"rules.{name}")

        safety = patch.get("safety") or {}
        if isinstance(safety, dict):
            for key, value in safety.items():
                if hasattr(self.cfg.safety, key):
                    if key == "max_absolute":
                        value = int(value)
                    elif key in ("max_pct", "master_multiplier", "release_ms", "attack_ms"):
                        value = float(value)
                    setattr(self.cfg.safety, key, value)
                    applied.append(f"safety.{key}={value}")

        device = patch.get("device") or {}
        if isinstance(device, dict):
            for key, value in device.items():
                if hasattr(self.cfg.device, key):
                    if key == "port":
                        value = int(value)
                        if not 1024 <= value <= 65535:
                            raise ValueError("device.port 必须在 1024..65535")
                    setattr(self.cfg.device, key, value)
                    applied.append(f"device.{key}={value}")

        hook = patch.get("hook") or {}
        if isinstance(hook, dict):
            for key, value in hook.items():
                if hasattr(self.cfg.hook, key):
                    setattr(self.cfg.hook, key, value)
                    applied.append(f"hook.{key}={value}")

        # ---- 0.5.0：事件源（改 enabled 需要重建引擎，因为源是在引擎线程里起的）
        sources = patch.get("sources") or {}
        if isinstance(sources, dict) and sources:
            known = {item["name"] for item in source_catalog()}
            for key, value in sources.items():
                if not hasattr(self.cfg.sources, key):
                    continue
                if key == "enabled":
                    names = [str(x).strip() for x in (value or []) if str(x).strip()]
                    unknown = [n for n in names if n not in known]
                    if unknown:
                        raise ValueError(f"未知事件源：{'、'.join(unknown)}"
                                         f"（可用：{'、'.join(sorted(known))}）")
                    if not names:
                        raise ValueError("至少要启用一个事件源")
                    self.cfg.sources.enabled = names
                elif key == "http_port":
                    value = int(value)
                    if not 1024 <= value <= 65535:
                        raise ValueError("sources.http_port 必须在 1024..65535")
                    self.cfg.sources.http_port = value
                elif key == "http_max_per_s":
                    value = max(1.0, min(200.0, float(value)))
                    self.cfg.sources.http_max_per_s = value
                else:
                    setattr(self.cfg.sources, key, value)
                applied.append(f"sources.{key}={value}")

        # ---- 0.5.0：惩罚累积
        ramp = patch.get("ramp") or {}
        if isinstance(ramp, dict):
            for key, value in ramp.items():
                if not hasattr(self.cfg.ramp, key):
                    continue
                if key == "apply_to":
                    value = [str(x).strip() for x in (value or []) if str(x).strip()]
                elif key in ("enabled", "reset_on_death"):
                    value = truthy(value)
                elif key in ("per_event", "hp_missing_pct", "ceiling_pct",
                             "decay_after_s", "decay_per_s"):
                    value = max(0.0, float(value))
                setattr(self.cfg.ramp, key, value)
                applied.append(f"ramp.{key}={value}")

        # ---- 0.5.0：检查更新（只影响提示，不影响输出）
        update = patch.get("update") or {}
        if isinstance(update, dict):
            for key, value in update.items():
                if key not in ("enabled", "repo", "check_on_start", "timeout_s"):
                    continue
                if key in ("enabled", "check_on_start"):
                    value = truthy(value)
                elif key == "timeout_s":
                    value = max(1.0, min(30.0, float(value)))
                else:
                    value = str(value)
                setattr(self.cfg.update, key, value)
                applied.append(f"update.{key}={value}")

        self.cfg.save(self.config_path)
        self._manual_source = None    # 配置变了，手动注入的解析实例重建
        # 立即生效（规则/安全是引用，设备与数据源要重建）
        if "device" in patch or "source" in patch or "sources" in patch:
            self.engine.trip("配置变更：停止检测并重新连接")
            self.engine.stop()
            self.engine = Engine(self.cfg)
            self.engine.trip("配置已变更，请手动重新武装")
            self.engine.on_event = self._on_event
        else:
            self.engine.set_config(self.cfg)
        return {"ok": True, "applied": applied, "config": self.cfg.to_dict()}

    @mutating
    def update_bridge(self, payload: dict[str, Any]) -> dict[str, Any]:
        values = {
            "mode": payload.get("mode", "safe"),
            "port": payload.get("port", 47777),
            "interval": payload.get("interval", 0.1),
            "profile": payload.get("profile", "steam_25480438"),
            "offsets": payload.get("offsets") or {},
        }
        path = write_bridge_config(values, self.bridge_cfg_dir)
        return {"ok": True, "path": str(path), "content": path.read_text(encoding="utf-8"),
                "note": "桥的配置在游戏启动时读取 —— 请重启游戏生效"}

    @mutating
    def action(self, payload: dict[str, Any]) -> dict[str, Any]:
        name = str(payload.get("action", "")).strip()
        engine = self.engine
        if name == "start":
            engine.start()
            self._note("控制台：开始检测")
            return {"ok": True, "detail": "已开始检测"}
        if name == "stop":
            engine.trip("已停止检测，请手动重新武装")
            engine.stop()
            self._note("控制台：停止检测")
            return {"ok": True, "detail": "已停止"}
        if name == "arm":
            engine.arm()
            self._note("控制台：重新武装")
            return {"ok": True, "detail": "已重新武装"}
        if name == "trip":
            engine.trip("Web 控制台急停")
            self._note("控制台：急停（需重新武装才恢复输出）")
            return {"ok": True, "detail": "已急停（需重新武装才恢复输出）"}
        if name == "test_pulse":
            pct = float(payload.get("pct", 5.0))
            ms = float(payload.get("ms", 700.0))
            if not 0 < pct <= 100 or not 100 <= ms <= 5000:
                raise ValueError("test_pulse 参数越界（pct 0-100，ms 100-5000）")
            if not engine.status.running or not engine.safety.armed:
                raise ValueError("测试前需要开始检测并重新武装")
            engine.test_pulse(pct=pct, ms=ms)
            self._note(f"控制台：测试脉冲 {pct}% / {ms:.0f}ms")
            return {"ok": True, "detail": f"已发送测试脉冲 {pct}% / {ms:.0f}ms"}
        if name == "device_start":
            engine.device.start()
            self._note("控制台：启动设备")
            return {"ok": True, "detail": "设备已启动（可用二维码连接手机 App）"}
        if name == "shutdown":
            return self.close(str(payload.get("reason", "Web 控制台")), delay=0.6)
        raise ValueError(f"未知动作：{name}")

    def close(self, reason: str = "桌面窗口", delay: float = 0.0) -> dict[str, Any]:
        """幂等关闭。先禁止后续写入、急停与停止，再通知服务器退出。

        软件只能请求归零，不能把网络传输成功当成硬件已归零的证明。
        """
        with self.lifecycle_lock:
            if self.closing:
                return {"ok": True, "detail": "控制器正在关闭，请在手机 App 核对输出已停止"}
            self.closing = True
            self.shutdown_reason = reason
            errors = []
            try:
                self.engine.trip("关闭程序")
            except Exception as exc:
                errors.append(str(exc))
                LOGGER.exception("关闭时急停失败")
            try:
                self.engine.stop()
            except Exception as exc:
                errors.append(str(exc))
                LOGGER.exception("关闭时停止引擎失败")
            detail = "正在关闭：已请求停止输出并断开设备，请在手机 App 核对"
            if errors:
                detail = "关闭时发生错误，输出状态未知，请立即在手机 App 停止输出"
            self._note("控制台：关闭程序；" + detail)
            if delay > 0:
                timer = threading.Timer(delay, self.shutdown_event.set)
                timer.daemon = True
                timer.start()
            else:
                self.shutdown_event.set()
            return {"ok": not errors, "detail": detail, **({"error": detail} if errors else {})}

    def qr_svg(self) -> bytes | None:
        url = getattr(self.engine.device, "qr_payload", "")
        if not url:
            return None
        try:
            import qrcode
            import qrcode.image.svg as svg

            img = qrcode.make(url, image_factory=svg.SvgPathImage, border=2)
            return img.to_string()  # type: ignore[no-any-return]
        except Exception as exc:
            LOGGER.warning("生成二维码失败：%s", exc)
            return None

    # ------------------------------------------------- 0.5.0：事件源 / 波形库
    def sources_dict(self) -> dict[str, Any]:
        """可用事件源 + 此刻的运行状态（页面「事件源」面板用）。"""
        return {
            "ok": True,
            "engine_source": self.cfg.source,
            "enabled": list(self.cfg.sources.enabled),
            "catalog": source_catalog(),
            "runtime": list(self.engine.status.sources),
            "http": {
                "host": self.cfg.sources.http_host,
                "port": int(self.cfg.sources.http_port),
                "url": f"http://{self.cfg.sources.http_host}:{int(self.cfg.sources.http_port)}/event",
                "token_required": bool(self.cfg.sources.http_token),
                "max_per_s": float(self.cfg.sources.http_max_per_s),
                "examples": HTTP_USAGE_EXAMPLE,
            },
        }

    @mutating
    def event_api(self, payload: Any) -> dict[str, Any]:
        """手动注入事件/状态（调试、联调用）—— 照样过规则层与安全上限。"""
        items = payload if isinstance(payload, list) else [payload]
        events: list[Any] = []
        recognized = False
        for item in items:
            if not isinstance(item, dict):
                continue
            recognized = recognized or looks_known(item)
            events.extend(self._ingest_manual(item))
        if not events:
            if recognized:
                # 状态包本身不产生事件（比如第一次上报只是建立基线），这不是错误
                return {"ok": True, "detail": "状态已更新（这次没有触发规则）", "events": []}
            raise ValueError(
                "没听懂这个输入：事件用 ev=damage|limb_injury|death|revive|low_health，"
                "状态用 {ev:'state', hp, hp_max, limbs, dead, bleeding}"
            )
        for event in events:
            self.engine.inject(event)
        detail = "、".join(_fmt_event(ev) for ev in events)
        self._note(f"手动注入：{detail}")
        return {"ok": True, "detail": detail, "events": [ev.kind for ev in events]}

    def _ingest_manual(self, item: dict[str, Any]) -> list[Any]:
        """借 HTTP 源的状态机解析（它没启用时，自己留一个只解析、不监听端口的实例）。"""
        source = self.engine.source("http")
        if source is not None:
            return list(source.ingest(item))
        if self._manual_source is None:
            self._manual_source = HttpEventSource(self.cfg, LOGGER)
        return list(self._manual_source.ingest(item))

    def waves_dict(self) -> dict[str, Any]:
        """命名波形库 + 内置预设（页面「波形库」面板用）。"""
        return {
            "ok": True,
            "waves": wave_lib.catalog(self.cfg),
            "presets": sorted(waves.PRESETS),
            "freq_min": waves.FREQ_MIN,
            "freq_max": waves.FREQ_MAX,
            "unit_ms": waves.UNIT_MS,
            "unit_chars": wave_lib.UNIT_CHARS,
            "text": wave_lib.to_text(self.cfg),
            "rules": {name: rule.wave for name, rule in self.cfg.rules.items()},
        }

    @mutating
    def waves_api(self, payload: dict[str, Any]) -> dict[str, Any]:
        """波形库增删改 / 导入导出 / 试打（改完立即生效）。"""
        action = str(payload.get("action") or "set").strip().lower()
        name = str(payload.get("name") or "").strip()
        if action in ("set", "edit"):
            if not name:
                raise ValueError("波形名字不能为空")
            wave_lib.set_entry(
                self.cfg, name,
                units=payload.get("units"),
                preset=payload.get("preset") or None,
                freq=payload.get("freq"),
                peak=payload.get("peak"),
                default_ms=payload.get("default_ms"),
                note=payload.get("note"),
            )
            self._save_and_apply()
            self._note(f"波形库：保存「{name}」")
            return {"ok": True, "applied": f"已保存「{name}」", **self.waves_dict()}
        if action == "preset":
            if not name:
                raise ValueError("波形名字不能为空")
            wave_lib.apply_preset_entry(self.cfg, name, str(payload.get("preset") or "pinch"),
                                        freq=payload.get("freq"), peak=payload.get("peak"),
                                        default_ms=payload.get("default_ms"))
            self._save_and_apply()
            self._note(f"波形库：用预设 {payload.get('preset')} 新建「{name}」")
            return {"ok": True, "applied": f"已新建「{name}」", **self.waves_dict()}
        if action == "remove":
            if not wave_lib.remove_entry(self.cfg, name):
                raise ValueError(f"波形库里没有「{name}」")
            self._save_and_apply()
            self._note(f"波形库：删除「{name}」")
            return {"ok": True, "applied": f"已删除「{name}」", **self.waves_dict()}
        if action in ("import", "replace"):
            data = payload.get("data")
            if data is not None:
                result = wave_lib.import_pulse_data(self.cfg, data, replace=(action == "replace"))
            else:
                result = wave_lib.from_text(self.cfg, str(payload.get("text") or ""),
                                            replace=(action == "replace"))
            self._save_and_apply()
            detail = f"导入 {result['count']} 个波形"
            if result["skipped"]:
                detail += f"，跳过 {len(result['skipped'])} 个"
            self._note("波形库：" + detail)
            return {"ok": True, "applied": detail, "import": result, **self.waves_dict()}
        if action == "export":
            return {"ok": True, "text": wave_lib.to_text(self.cfg), **self.waves_dict()}
        if action == "test":
            if not name:
                raise ValueError("要试打哪个波形？")
            pct = float(payload.get("pct", 5.0))
            ms = float(payload.get("ms", 700.0))
            channel = str(payload.get("channel") or "both")
            if channel not in ("A", "B", "both"):
                raise ValueError("channel 只能是 A / B / both")
            if not 0 < pct <= 100 or not 100 <= ms <= 5000:
                raise ValueError("试打参数越界（pct 0-100，ms 100-5000）")
            if not self.engine.status.running or not self.engine.safety.armed:
                raise ValueError("测试前需要开始检测并重新武装")
            self.engine.test_pulse(pct=pct, ms=ms, channel=channel, wave=name)
            self._note(f"波形库：试打「{name}」{pct:.0f}% / {ms:.0f}ms")
            return {"ok": True, "applied": f"已试打「{name}」", **self.waves_dict()}
        raise ValueError(f"未知波形库动作：{action}")

    def _save_and_apply(self) -> None:
        """波形库改完：落盘 + 让引擎用上新的 AppConfig（规则/波形都在里面）。"""
        self.cfg.save(self.config_path)
        self.engine.set_config(self.cfg)

    # ----------------------------------------------------- 0.5.0：检查更新
    def update_info(self) -> dict[str, Any]:
        """上次检查更新的结果（页面只读缓存，不主动联网）。"""
        if self.update_result is not None:
            return dict(self.update_result)
        return {"ok": None, "current": __version__, "repo": self.cfg.update.repo,
                "enabled": bool(self.cfg.update.enabled),
                "error": "还没检查过（点「检查更新」）"}

    def update_api(self, payload: dict[str, Any]) -> dict[str, Any]:
        """真的去查一次 GitHub Releases —— 只提示 + 给链接，绝不自动下载安装。"""
        if truthy(payload.get("clear")):
            self.update_result = None
            return self.update_info()
        if not self.cfg.update.enabled:
            raise ValueError("配置里 update.enabled = false（想检查请先在配置里打开）")
        result = update_check.check(repo=self.cfg.update.repo, current=__version__,
                                    timeout=float(self.cfg.update.timeout_s))
        self.update_result = result
        if result.get("update_available"):
            tag = str((result.get("latest") or {}).get("tag") or "")
            self._note(f"检查更新：有新版本 {tag}")
        elif result.get("ok"):
            self._note("检查更新：已是最新版本")
        else:
            self._note("检查更新失败：" + str(result.get("error")))
        return dict(result)


HTTP_USAGE_EXAMPLE: dict[str, Any] = {
    "state": {"ev": "state", "hp": 62, "hp_max": 100, "limbs": [0, 1, 0], "bleeding": 0, "dead": 0},
    "damage": {"ev": "damage", "severity": 12.5},
    "limb_injury": {"ev": "limb_injury", "slot": 1, "bleeding": True, "name": "左腿"},
    "death": {"ev": "death"},
    "revive": {"ev": "revive"},
    "low_health": {"ev": "low_health", "ratio": 0.18},
}


def build_server(cfg: AppConfig, config_path: Path, host: str, port: int,
                 bridge_cfg_dir: str | Path | None = None) -> tuple[ThreadingHTTPServer, WebApp]:
    app = WebApp(cfg, config_path, bridge_cfg_dir)
    handler = type("BoundHandler", (_Handler,), {"app": app})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True

    def watchdog() -> None:
        """等「关闭程序」的请求，然后让 serve_forever 返回（主线程收尾）。"""
        app.shutdown_event.wait()
        LOGGER.info("收到关闭请求（%s），正在退出", app.shutdown_reason or "Web 控制台")
        try:
            httpd.shutdown()
        except Exception:  # 服务器可能已经关了
            pass

    threading.Thread(target=watchdog, name="shutdown-watchdog", daemon=True).start()
    return httpd, app


def run_web(cfg: AppConfig, config_path: Path, host: str = "127.0.0.1", port: int = 8787,
            bridge_cfg_dir: str | Path | None = None, open_browser: bool = True) -> None:
    httpd, app = build_server(cfg, config_path, host, port, bridge_cfg_dir)
    url = f"http://{host if host != '0.0.0.0' else '127.0.0.1'}:{port}/"
    print(f"hd2-coyote Web 控制台：{url}")
    print("  · 页面里可以：看状态、调强度、改桥的档位/偏移、看日志与部署自检")
    print("  · 桥的配置改完需要重启游戏生效；控制器动作（急停/测试脉冲）立即生效")
    print("  · 退出：网页右上角「关闭程序」，或在本窗口按 Ctrl+C")
    if host == "0.0.0.0":
        print("  ⚠️ 你把它开在了所有网卡上 —— 这个页面能触发实际输出，公网/宿舍网请勿如此")
    if open_browser:
        threading.Thread(target=lambda: (time.sleep(0.6), webbrowser.open(url)),
                         daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，正在停止……")
    finally:
        app.close("Web 服务退出")
        httpd.server_close()
        print("控制器已关闭；已请求停止输出，请在手机 App 核对。")


# --------------------------------------------------------------------- 页面
# 源码与 PyInstaller 包都从模块旁的 frontend 目录加载；不依赖工作目录或 CDN。
def load_page() -> str:
    root = Path(__file__).with_name("frontend")
    return (root.joinpath("index.html").read_text(encoding="utf-8")
            .replace("/*__STYLE__*/", root.joinpath("fluent.css").read_text(encoding="utf-8"))
            .replace("/*__SCRIPT__*/", root.joinpath("app.js").read_text(encoding="utf-8")))


PAGE = load_page()

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
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: Any, code: int = 200) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _read_body(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}

    # ------------------------------------------------------------ 路由
    def do_GET(self) -> None:  # noqa: N802
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
        body = self._read_body()
        try:
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


class WebApp:
    """把 Engine / 配置 / 桥诊断包成 JSON API。"""

    def __init__(self, cfg: AppConfig, config_path: Path,
                 bridge_cfg_dir: str | Path | None = None) -> None:
        self.cfg = cfg
        self.config_path = Path(config_path)
        self.bridge_cfg_dir = bridge_cfg_dir
        self.engine = Engine(cfg)
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

    def update_config(self, patch: dict[str, Any]) -> dict[str, Any]:
        """局部合并控制器配置：rules / safety / device / hook / source。"""
        applied: list[str] = []
        if "source" in patch:
            source = str(patch["source"])
            if source not in ("hook", "vision"):
                raise ValueError("source 只能是 hook 或 vision")
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
            was_running = self.engine.status.running
            self.engine.stop()
            self.engine = Engine(self.cfg)
            self.engine.on_event = self._on_event
            if was_running:
                self.engine.start()
        else:
            self.engine.set_config(self.cfg)
        return {"ok": True, "applied": applied, "config": self.cfg.to_dict()}

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

    def action(self, payload: dict[str, Any]) -> dict[str, Any]:
        name = str(payload.get("action", "")).strip()
        engine = self.engine
        if name == "start":
            engine.start()
            self._note("控制台：开始检测")
            return {"ok": True, "detail": "已开始检测"}
        if name == "stop":
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
            engine.test_pulse(pct=pct, ms=ms)
            self._note(f"控制台：测试脉冲 {pct}% / {ms:.0f}ms")
            return {"ok": True, "detail": f"已发送测试脉冲 {pct}% / {ms:.0f}ms"}
        if name == "device_start":
            engine.device.start()
            self._note("控制台：启动设备")
            return {"ok": True, "detail": "设备已启动（可用二维码连接手机 App）"}
        if name == "shutdown":
            # ★ 顺序很重要：先把输出归零并停掉设备（戴着电极的人不能等），
            #   再让 HTTP 服务器退出。engine.stop() 内部会 device.stop()，
            #   socket 设备会先 mute()（清波形 + 强度置 0）再关连接。
            try:
                engine.stop()
            except Exception as exc:
                LOGGER.warning("停止引擎时出错：%s", exc)
            self._note("控制台：关闭程序（输出已归零，设备已断开）")
            self.shutdown_reason = str(payload.get("reason", "Web 控制台"))
            # 延迟一点再退出，保证这个响应能发回浏览器
            threading.Thread(target=self._delayed_shutdown, daemon=True).start()
            return {"ok": True, "detail": "正在关闭：输出已归零、设备已断开，页面可以关了"}
        raise ValueError(f"未知动作：{name}")

    def _delayed_shutdown(self, delay: float = 0.6) -> None:
        time.sleep(delay)
        self.shutdown_event.set()

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
        app.engine.stop()        # 内部会 device.stop()：清波形 + 强度归零 + 断开
        httpd.server_close()
        print("控制器已关闭（输出已归零，设备已断开）。")


# --------------------------------------------------------------------- 页面
PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>hd2-coyote 控制台</title>
<style>
:root{--bg:#14161a;--card:#1d2026;--line:#2c313a;--fg:#e6e8ec;--dim:#8b93a1;
      --ok:#3fb950;--warn:#d29922;--bad:#f85149;--accent:#388bfd}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 "Segoe UI",system-ui,sans-serif}
header{padding:14px 18px;border-bottom:1px solid var(--line);display:flex;
       align-items:center;gap:14px;flex-wrap:wrap}
h1{font-size:16px;margin:0;font-weight:600}
.tag{font-size:12px;color:var(--dim)}
main{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:14px;padding:14px}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
section h2{font-size:13px;margin:0 0 10px;color:var(--dim);font-weight:600;letter-spacing:.4px}
.row{display:flex;justify-content:space-between;gap:10px;padding:3px 0}
.row span:last-child{font-variant-numeric:tabular-nums}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
.ok{background:var(--ok)}.bad{background:var(--bad)}.warn{background:var(--warn)}
.bar{height:10px;background:#2a2f38;border-radius:5px;overflow:hidden;margin:6px 0 10px}
.bar>i{display:block;height:100%;background:linear-gradient(90deg,#3fb950,#d29922,#f85149);
       width:0;transition:width .2s}
button{background:#262b33;color:var(--fg);border:1px solid var(--line);border-radius:7px;
       padding:7px 11px;cursor:pointer;font-size:13px}
button:hover{border-color:var(--accent)}
button.danger{border-color:#6b2b2b;background:#2a1b1b}
button.primary{border-color:#245a9c;background:#16273d}
.btns{display:flex;gap:8px;flex-wrap:wrap}
label.slider{display:grid;grid-template-columns:1fr 74px;gap:8px;align-items:center;margin:6px 0}
input[type=range]{width:100%}
input[type=number],input[type=text],input[type=checkbox],select,textarea{background:#141821;color:var(--fg);
       border:1px solid var(--line);border-radius:6px;padding:5px 7px;width:100%}
input[type=checkbox]{width:auto}
textarea{font-family:ui-monospace,Consolas,monospace;font-size:12px;resize:vertical}
pre{background:#11141a;border:1px solid var(--line);border-radius:8px;padding:9px;
    overflow:auto;max-height:220px;font-size:12px;white-space:pre-wrap;margin:6px 0 0}
.muted{color:var(--dim);font-size:12px}
.pill{display:inline-block;padding:1px 7px;border-radius:99px;border:1px solid var(--line);
      font-size:12px;color:var(--dim)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:8px}
</style>
</head>
<body>
<header>
  <h1>hd2-coyote 控制台</h1>
  <span class="tag" id="ver"></span>
  <span class="pill" id="dev"></span>
  <span class="pill" id="hk"></span>
  <span class="btns" style="margin-left:auto">
    <button class="primary" onclick="act('start')">开始检测</button>
    <button onclick="act('stop')">停止</button>
    <button class="danger" onclick="act('trip')">急停</button>
    <button onclick="act('arm')">重新武装</button>
    <button class="danger" id="shutdownBtn" onclick="shutdownApp()"
            title="输出归零并退出控制器">关闭程序</button>
  </span>
</header>
<main>
  <section>
    <h2>实时状态</h2>
    <div class="bar"><i id="hpbar"></i></div>
    <div class="row"><span>血量</span><span id="hp">--</span></div>
    <div class="row"><span>肢体损伤</span><span id="limbs">--</span></div>
    <div class="row"><span>输出强度</span><span id="out">--</span></div>
    <div class="row"><span>状态</span><span id="state">--</span></div>
    <div class="row"><span>累计输出</span><span id="sess">--</span></div>
    <div class="row"><span>数据源 / 桥</span><span id="src">--</span></div>
    <div id="qrwrap" style="margin-top:8px"></div>
    <h2 style="margin-top:14px">测试脉冲</h2>
    <div class="grid2">
      <label class="slider"><span>强度 %</span><input type="number" id="tpct" value="5" min="1" max="100"></label>
      <label class="slider"><span>时长 ms</span><input type="number" id="tms" value="700" min="100" max="5000"></label>
    </div>
    <div class="btns" style="margin-top:8px">
      <button onclick="testPulse()">发送测试脉冲</button>
      <button onclick="act('device_start')">启动设备（二维码）</button>
    </div>
  </section>

  <section>
    <h2>强度（立即生效）</h2>
    <div id="rules"></div>
    <label class="slider"><span>总倍率</span><input type="number" id="master" step="0.05" min="0" max="2"></label>
    <label class="slider"><span>单次上限 %</span><input type="number" id="maxpct" min="1" max="60"></label>
    <label class="slider"><span>绝对上限</span><input type="number" id="maxabs" min="1" max="200"></label>
    <div class="btns" style="margin-top:8px"><button class="primary" onclick="saveConfig()">保存强度设置</button></div>
    <div class="muted" id="cfginfo" style="margin-top:6px"></div>
  </section>

  <section>
    <h2>游戏内桥（改完要重启游戏）</h2>
    <label class="slider"><span>档位 mode</span>
      <select id="bmode">
        <option value="safe">safe（只写日志）</option>
        <option value="net">net（+UDP）</option>
        <option value="menu">menu（+游戏内菜单）</option>
        <option value="recon">recon（+内存侦察）</option>
        <option value="live">live（正式运行）</option>
      </select></label>
    <div class="grid2">
      <label class="slider"><span>UDP 端口</span><input type="number" id="bport" min="1024" max="65535"></label>
      <label class="slider"><span>上报间隔 s</span><input type="number" id="bint" step="0.05" min="0.05" max="1"></label>
      <label class="slider"><span>血量偏移</span><input type="number" id="bhp" min="-1" max="65535"></label>
      <label class="slider"><span>上限偏移</span><input type="number" id="bhpma" min="-1" max="65535"></label>
      <label class="slider"><span>肢体掩码偏移</span><input type="number" id="blimb" min="-1" max="65535"></label>
      <label class="slider"><span>肢体起始位</span><input type="number" id="bshift" min="0" max="7"></label>
      <label class="slider"><span>阵亡偏移</span><input type="number" id="bdead" min="-1" max="65535"></label>
      <label class="slider"><span>profile</span><input type="text" id="bprof"></label>
    </div>
    <div class="btns" style="margin-top:8px">
      <button class="primary" onclick="saveBridge()">写入 bridge_config.lua</button>
      <button onclick="loadBridge()">重新读取</button>
    </div>
    <div class="muted" id="binfo" style="margin-top:6px"></div>
  </section>

  <section>
    <h2>诊断</h2>
    <div id="dwarn" class="muted"></div>
    <div class="row"><span>桥上次运行</span><span id="dlast" style="text-align:right"></span></div>
    <div class="row"><span>addon STATUS</span><span id="dstatus" style="text-align:right"></span></div>
    <div class="row"><span>加载器日志行</span><span id="dloader" style="text-align:right"></span></div>
    <div class="muted" id="dpaths"></div>
    <pre id="dlog">（暂无日志）</pre>
    <h2 style="margin-top:12px">recon 报告</h2>
    <pre id="drecon">（还没跑过 recon —— 把档位设成 recon 后重启游戏）</pre>
  </section>

  <section>
    <h2>事件</h2>
    <pre id="events">（暂无）</pre>
  </section>

  <section>
    <h2>事件源（改完立即重建引擎）</h2>
    <div id="srcList" class="muted">加载中…</div>
    <div class="btns" style="margin-top:8px">
      <button class="primary" onclick="saveSources()">保存事件源</button>
      <button onclick="loadSources()">重新读取</button>
    </div>
    <div class="muted" id="srcInfo" style="margin-top:6px"></div>
    <h2 style="margin-top:12px">手动注入（调试）</h2>
    <div class="grid2">
      <label class="slider"><span>事件</span>
        <select id="evKind">
          <option value="damage">受伤 damage</option>
          <option value="limb_injury">肢体损伤 limb_injury</option>
          <option value="death">阵亡 death</option>
          <option value="revive">复活 revive</option>
          <option value="low_health">低血量 low_health</option>
          <option value="state">状态包 state</option>
        </select></label>
      <label class="slider"><span>数值（severity / 血量%）</span>
        <input type="number" id="evVal" value="20" step="1"></label>
    </div>
    <div class="btns" style="margin-top:8px">
      <button class="primary" onclick="injectEvent()">注入这一个</button>
    </div>
    <div class="muted" id="evInfo" style="margin-top:6px"></div>
    <pre id="srcUsage">（上报示例）</pre>
  </section>

  <section>
    <h2>波形库（规则里按名字选用）</h2>
    <div id="waveList" class="muted">加载中…</div>
    <div class="btns" style="margin-top:8px">
      <button onclick="loadWaves()">重新读取</button>
      <button onclick="exportWaves()">导出到下面</button>
      <button onclick="importWaves()">从下面导入</button>
    </div>
    <h2 style="margin-top:12px">新建 / 覆盖</h2>
    <div class="grid2">
      <label class="slider"><span>名字</span><input type="text" id="wName" placeholder="例如 死亡长按"></label>
      <label class="slider"><span>内置预设</span><select id="wPreset"></select></label>
      <label class="slider"><span>频率 Hz（留空=预设）</span>
        <input type="number" id="wFreq" min="10" max="240" step="1" placeholder="10-240"></label>
      <label class="slider"><span>峰值 %</span><input type="number" id="wPeak" min="1" max="100" value="100"></label>
    </div>
    <label class="slider"><span>单元（16 位十六进制，逗号/空格分隔）</span>
      <textarea id="wUnits" rows="2" placeholder="0A0A0A0A64646464"></textarea></label>
    <div class="btns" style="margin-top:8px">
      <button class="primary" onclick="saveWave()">保存到波形库</button>
      <button onclick="testWave()">按左边测试脉冲的强度/时长试打</button>
      <button class="danger" onclick="removeWave()">删除这个波形</button>
    </div>
    <div class="muted" id="wInfo" style="margin-top:6px"></div>
    <textarea id="wJson" rows="7" placeholder="导出/导入的 JSON（DG-Lab-Punishment 的 default.json 也能直接粘进来）"></textarea>
  </section>

  <section>
    <h2>惩罚累积（受伤越多、强度越高）</h2>
    <label class="slider"><span>启用累积</span><input type="checkbox" id="rpOn"></label>
    <div class="grid2">
      <label class="slider"><span>每个事件 +%</span><input type="number" id="rpPerEvent" step="0.5" min="0"></label>
      <label class="slider"><span>缺血量权重 %</span><input type="number" id="rpHp" step="1" min="0"></label>
      <label class="slider"><span>累积封顶 %</span><input type="number" id="rpCeil" step="1" min="0"></label>
      <label class="slider"><span>几秒后开始回落</span><input type="number" id="rpDecayAfter" step="0.5" min="0"></label>
      <label class="slider"><span>回落速度 %/s</span><input type="number" id="rpDecayPer" step="0.5" min="0"></label>
      <label class="slider"><span>阵亡/复活清零</span><input type="checkbox" id="rpResetOnDeath"></label>
    </div>
    <div class="btns" style="margin-top:8px">
      <button class="primary" onclick="saveRamp()">保存累积设置</button>
    </div>
    <div class="muted" id="rpInfo" style="margin-top:6px"></div>
  </section>

  <section>
    <h2>检查更新</h2>
    <div class="row"><span>仓库</span><span id="upRepo" style="text-align:right"></span></div>
    <div class="row"><span>当前版本</span><span id="upCur" style="text-align:right"></span></div>
    <div class="row"><span>最新版本</span><span id="upLatest" style="text-align:right"></span></div>
    <div class="btns" style="margin-top:8px">
      <button class="primary" onclick="checkUpdate()">检查更新</button>
      <a id="upLink" href="#" target="_blank" rel="noreferrer" style="display:none">打开发布页</a>
    </div>
    <div class="muted" id="upInfo" style="margin-top:6px">只提示 + 给链接，绝不自动下载安装。</div>
  </section>
</main>
<script>
const RULES = {damage:'受伤', limb_injury:'肢体损伤', death:'阵亡', low_health:'低血量'};
let CFG = null;
let FAILS = 0;
let LASTVER = '';

async function api(path, opts) {
  const r = await fetch(path, opts);
  const t = await r.text();
  let j = null; try { j = JSON.parse(t); } catch (e) { j = {error: t}; }
  if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
  return j;
}
function fmtPct(v){ return (v===null||v===undefined) ? '--' : (v*100).toFixed(1)+'%'; }

async function refresh() {
  let s; try { s = await api('/api/status'); FAILS = 0; }
  catch (e) {
    // 服务器没了（比如刚点了「关闭程序」）—— 别让页面看起来还在监控
    if (++FAILS === 5) {
      markClosed('ℹ️ 连不上控制器 —— 它已经退出了。要再用一次，重新运行 run.bat 或 '
                 + 'python -m hd2coyote web。');
    }
    return;
  }
  const c = s.controller, b = s.bridge || {};
  LASTVER = c.version;
  document.getElementById('ver').textContent = 'v' + c.version + ' · 运行 ' + c.uptime_s + 's';
  document.getElementById('dev').textContent = '设备 ' + (c.device.connected ? '已连接' : '未连接')
        + (c.device.kind ? ' ('+c.device.kind+')' : '');
  document.getElementById('hk').textContent = c.source === 'hook'
        ? ('桥 ' + (c.hook.alive ? '在线' : '离线') + (c.hook.version ? ' v'+c.hook.version : ''))
        : ('来源 ' + c.source);
  document.getElementById('hp').textContent = fmtPct(c.hp);
  document.getElementById('hpbar').style.width = ((c.hp||0)*100).toFixed(1) + '%';
  document.getElementById('limbs').textContent = (c.limbs && c.limbs.length) ? c.limbs.join('') : '--';
  document.getElementById('out').textContent = 'A=' + c.output.a + ' B=' + c.output.b
        + ' (' + c.output.pct + '%)'
        + (c.ramp && c.ramp.enabled ? ' 累积+' + c.ramp.pct + '%' : '');
  document.getElementById('state').innerHTML = (c.armed
        ? '<span class="dot ok"></span>已武装' : '<span class="dot bad"></span>已静音 ' + (c.mute_reason||''))
        + ' · ' + (c.detecting ? '检测中' : '未检测');
  document.getElementById('sess').textContent = c.session_seconds + ' s';
  document.getElementById('src').textContent = c.source + ' · ' + (c.hook.detail || '')
        + ((c.sources && c.sources.length)
            ? ' ｜ ' + c.sources.map(x => x.name + (x.alive ? '✓' : '✗')).join(' ')
            : '');
  if (c.device.qr_url) {
    document.getElementById('qrwrap').innerHTML =
      '<div class="muted">手机 App 扫码（或手输）：<br>' + c.device.qr_url + '</div>'
      + '<img src="/qr.svg" style="width:150px;height:150px;background:#fff;border-radius:8px;margin-top:6px">';
  }
  document.getElementById('events').textContent = (s.events && s.events.length)
        ? s.events.slice().reverse().join('\n') : '（暂无）';
  document.getElementById('dstatus').textContent = b.status || '（没有 STATUS 文件 —— addon 还没跑过）';
  document.getElementById('dloader').textContent = b.loader_line || '（加载器日志里没有 hd2coyote）';
  // 桥上次实际跑的档位 vs 现在配置里的档位：不一致 = 忘了重启游戏
  const mLast = /version=([\w.]+)\s+mode=(\w+)/.exec(b.status || '');
  const wantMode = (b.parsed && b.parsed.mode) || '';
  document.getElementById('dlast').textContent = mLast
        ? ('v' + mLast[1] + ' mode=' + mLast[2]) : '（还没运行过）';
  let warn = '';
  if (!b.exists) {
    warn = '⚠️ 没有 bridge_config.lua：addon 会跑内置默认（safe，什么都不做）。'
         + '在右边选好档位后点"写入"。';
  } else if (mLast && wantMode && mLast[2] !== wantMode) {
    warn = '⚠️ 配置里是 ' + wantMode + '，但游戏上次实际跑的是 ' + mLast[2]
         + ' —— 需要重启游戏才会生效。';
  } else if (!b.loader_line) {
    warn = '⚠️ 加载器日志里没有 hd2coyote：确认 Arsenal 里已启用并 Deploy，且这局游戏启动过。';
  }
  document.getElementById('dwarn').textContent = warn;
  document.getElementById('dpaths').textContent = (b.config_path||'') + '  ·  ' + (b.loader_log||'');
  const tail = (b.log_tail||[]).concat(b.shared_log_tail||[]);
  document.getElementById('dlog').textContent = tail.length ? tail.join('\n') : '（暂无日志）';
  document.getElementById('drecon').textContent = (b.recon_report||[]).length
        ? b.recon_report.join('\n')
        : '（还没跑过 recon —— 把档位设成 recon 后重启游戏，报告会自动出现在这里）';
  renderUpdate(s.update || {});
}

// ---------------------------------------------------------------- 检查更新
function renderUpdate(u) {
  document.getElementById('upRepo').textContent = u.repo || '-';
  document.getElementById('upCur').textContent = u.current || LASTVER || '-';
  const latest = u.latest || {};
  document.getElementById('upLatest').textContent = latest.tag
        ? (latest.tag + (latest.published_at ? '（' + String(latest.published_at).slice(0,10) + '）' : ''))
        : '-';
  const link = document.getElementById('upLink');
  const url = u.url || latest.url || '';
  if (url) { link.href = url; link.style.display = ''; } else { link.style.display = 'none'; }
  let text = '只提示 + 给链接，绝不自动下载安装。';
  if (u.ok === false) text = '⚠️ 检查失败：' + (u.error || '未知错误');
  else if (u.ok === true && u.update_available) {
    text = '🎉 有新版本 ' + (u.latest && u.latest.tag) + '（当前 ' + u.current + '）';
    if (u.zip && u.zip.name) text += ' · 附件 ' + u.zip.name;
  } else if (u.ok === true) text = '✓ 已是最新版本（' + u.current + '）';
  document.getElementById('upInfo').textContent = text;
}

async function checkUpdate() {
  const box = document.getElementById('upInfo');
  box.textContent = '正在查 GitHub Releases…';
  try {
    const res = await api('/api/update', {method:'POST', headers:{'Content-Type':'application/json'},
                                          body: JSON.stringify({})});
    renderUpdate(res);
  } catch (e) { box.textContent = '检查失败：' + e.message; }
}

// ---------------------------------------------------------------- 事件源
async function loadSources() {
  const s = await api('/api/sources');
  window.__SRC = s;
  document.getElementById('srcList').innerHTML = s.catalog.map(item =>
      `<div class="row"><span><input type="checkbox" id="src_${item.name}"
        ${s.enabled.includes(item.name) ? 'checked' : ''}> <b>${item.label}</b>
        <span class="muted">${item.name}${item.critical ? ' · 关键源' : ''}</span></span>
        <span class="muted" style="text-align:right">${item.hint || ''}</span></div>`).join('');
  const rt = (s.runtime || []).map(x => x.label + '：' + (x.detail || (x.alive ? '在线' : '离线')));
  document.getElementById('srcInfo').textContent =
      '引擎数据来源：' + s.engine_source + ' ｜ HTTP 上报 ' + s.http.url
      + (s.http.token_required ? '（需要令牌）' : '（无令牌）')
      + ' ｜ 限速 ' + s.http.max_per_s + '/s'
      + (rt.length ? ' ｜ 此刻：' + rt.join('；') : '');
  document.getElementById('srcUsage').textContent = Object.keys(s.http.examples)
      .map(k => k + '  ' + JSON.stringify(s.http.examples[k])).join('\n');
}

async function saveSources() {
  const s = window.__SRC || await api('/api/sources');
  const enabled = s.catalog.filter(i => {
    const box = document.getElementById('src_' + i.name);
    return box && box.checked;
  }).map(i => i.name);
  try {
    const res = await api('/api/config', {method:'POST', headers:{'Content-Type':'application/json'},
                                          body: JSON.stringify({sources:{enabled}})});
    document.getElementById('srcInfo').textContent =
        '✓ 已保存：' + res.applied.join(', ') + '（数据源会重建）';
  } catch (e) {
    document.getElementById('srcInfo').textContent = '保存失败：' + e.message;
  }
  await loadSources();
}

async function injectEvent() {
  const kind = document.getElementById('evKind').value;
  const val = Number(document.getElementById('evVal').value);
  let body;
  if (kind === 'state') body = {ev:'state', hp:val, hp_max:100, limbs:[0,0,0]};
  else if (kind === 'damage') body = {ev:'damage', severity:val};
  else if (kind === 'limb_injury') body = {ev:'limb_injury', slot:1, name:'左肢'};
  else if (kind === 'low_health') body = {ev:'low_health', ratio:val/100};
  else body = {ev:kind};
  try {
    const res = await api('/api/event', {method:'POST', headers:{'Content-Type':'application/json'},
                                         body: JSON.stringify(body)});
    document.getElementById('evInfo').textContent = '✓ ' + res.detail;
  } catch (e) {
    document.getElementById('evInfo').textContent = '注入失败：' + e.message;
  }
  refresh();
}

// ---------------------------------------------------------------- 波形库
async function loadWaves() {
  const w = await api('/api/waves');
  window.__WAVES = w;
  document.getElementById('wPreset').innerHTML = w.presets
      .map(p => `<option value="${p}">${p}</option>`).join('');
  document.getElementById('waveList').innerHTML = w.waves.length
    ? w.waves.map(row =>
        `<div class="row"><span><a href="#" onclick="pickWave('${row.name}');return false">${row.name}</a>
          <span class="muted">${row.kind === 'units' ? row.unit_count + ' 个单元' : '预设 ' + row.preset}</span></span>
          <span class="muted" style="text-align:right">${row.note || ''}</span></div>`).join('')
    : '<div class="muted">（波形库是空的 —— 规则会退回内置预设 pinch / sting / …）</div>';
  document.getElementById('wInfo').textContent = '规则现在选用的波形：'
      + Object.keys(w.rules).map(k => k + '=' + (w.rules[k] || '-')).join('、')
      + `（一个单元 ${w.unit_chars} 个十六进制字符 = ${w.unit_ms}ms，其中每 ${w.unit_ms/4}ms 一组频率/强度，频率 ${w.freq_min}-${w.freq_max}）`;
  document.getElementById('wJson').value = w.text;
}

function pickWave(name) {
  const w = window.__WAVES; if (!w) return;
  const row = w.waves.find(x => x.name === name); if (!row) return;
  document.getElementById('wName').value = row.name;
  document.getElementById('wUnits').value = (row.units || []).join(' ');
  document.getElementById('wPeak').value = row.peak;
  if (row.freq) document.getElementById('wFreq').value = row.freq;
  if (row.kind === 'preset' && row.preset) document.getElementById('wPreset').value = row.preset;
  document.getElementById('wInfo').textContent = '已把「' + name + '」填进上面的表单，改完点保存。';
}

async function saveWave() {
  const name = document.getElementById('wName').value.trim();
  const units = document.getElementById('wUnits').value.split(/[\s,;]+/).filter(x => x);
  const body = {action:'set', name,
      peak: Number(document.getElementById('wPeak').value),
      freq: document.getElementById('wFreq').value || null};
  if (units.length) body.units = units;
  else body.preset = document.getElementById('wPreset').value;
  try {
    const res = await api('/api/waves', {method:'POST', headers:{'Content-Type':'application/json'},
                                         body: JSON.stringify(body)});
    document.getElementById('wInfo').textContent = '✓ ' + res.applied;
  } catch (e) {
    document.getElementById('wInfo').textContent = '保存失败：' + e.message;
  }
  await loadWaves();
}

async function removeWave() {
  const name = document.getElementById('wName').value.trim();
  if (!name) { document.getElementById('wInfo').textContent = '先在「名字」里填要删的波形'; return; }
  if (!confirm('把波形「' + name + '」从库里删掉？规则里还在用它的会退回内置预设。')) return;
  try {
    const res = await api('/api/waves', {method:'POST', headers:{'Content-Type':'application/json'},
                                         body: JSON.stringify({action:'remove', name})});
    document.getElementById('wInfo').textContent = '✓ ' + res.applied;
  } catch (e) {
    document.getElementById('wInfo').textContent = '删除失败：' + e.message;
  }
  await loadWaves();
}

async function testWave() {
  const name = document.getElementById('wName').value.trim();
  const pct = Number(document.getElementById('tpct').value);
  const ms = Number(document.getElementById('tms').value);
  try {
    const res = await api('/api/waves', {method:'POST', headers:{'Content-Type':'application/json'},
                                         body: JSON.stringify({action:'test', name, pct, ms})});
    document.getElementById('wInfo').textContent = '✓ ' + res.applied + '（' + pct + '% / ' + ms + 'ms）';
  } catch (e) {
    document.getElementById('wInfo').textContent = '试打失败：' + e.message;
  }
}

async function exportWaves() {
  try {
    const res = await api('/api/waves', {method:'POST', headers:{'Content-Type':'application/json'},
                                         body: JSON.stringify({action:'export'})});
    document.getElementById('wJson').value = res.text;
    document.getElementById('wInfo').textContent = '✓ 已导出到下面的框里（可复制保存）';
  } catch (e) { document.getElementById('wInfo').textContent = '导出失败：' + e.message; }
}

async function importWaves() {
  const text = document.getElementById('wJson').value.trim();
  if (!text) { document.getElementById('wInfo').textContent = '先把 JSON 粘到下面的框里'; return; }
  try {
    const res = await api('/api/waves', {method:'POST', headers:{'Content-Type':'application/json'},
                                         body: JSON.stringify({action:'import', text})});
    document.getElementById('wInfo').textContent = '✓ ' + res.applied;
  } catch (e) {
    document.getElementById('wInfo').textContent = '导入失败：' + e.message;
  }
  await loadWaves();
}

// ---------------------------------------------------------------- 惩罚累积
async function loadRamp() {
  const c = await api('/api/config');
  const r = c.ramp || {};
  document.getElementById('rpOn').checked = !!r.enabled;
  document.getElementById('rpPerEvent').value = r.per_event;
  document.getElementById('rpHp').value = r.hp_missing_pct;
  document.getElementById('rpCeil').value = r.ceiling_pct;
  document.getElementById('rpDecayAfter').value = r.decay_after_s;
  document.getElementById('rpDecayPer').value = r.decay_per_s;
  document.getElementById('rpResetOnDeath').checked = !!r.reset_on_death;
  document.getElementById('rpInfo').textContent = '作用于：'
      + ((r.apply_to || []).join('、') || '（无）') + ' —— 累积会加到每次输出的百分比上，'
      + '但仍然受「单次上限 / 绝对上限」约束。';
}

async function saveRamp() {
  const body = {ramp: {
      enabled: document.getElementById('rpOn').checked,
      per_event: Number(document.getElementById('rpPerEvent').value),
      hp_missing_pct: Number(document.getElementById('rpHp').value),
      ceiling_pct: Number(document.getElementById('rpCeil').value),
      decay_after_s: Number(document.getElementById('rpDecayAfter').value),
      decay_per_s: Number(document.getElementById('rpDecayPer').value),
      reset_on_death: document.getElementById('rpResetOnDeath').checked }};
  try {
    const res = await api('/api/config', {method:'POST', headers:{'Content-Type':'application/json'},
                                          body: JSON.stringify(body)});
    document.getElementById('rpInfo').textContent = '✓ 已保存：' + res.applied.join(', ');
  } catch (e) {
    document.getElementById('rpInfo').textContent = '保存失败：' + e.message;
  }
  await loadRamp();
}

function sliderRow(key, rule, label) {
  const id = 'r_' + key;
  return `<div class="row"><span><input type="checkbox" id="${id}_on" ${rule.enabled?'checked':''}>
    ${label}</span><span><input type="range" id="${id}" min="0" max="60" step="1" value="${rule.base_pct}"
    oninput="document.getElementById('${id}_v').textContent=this.value+'%'">
    <b id="${id}_v">${Math.round(rule.base_pct)}%</b></span></div>`;
}

async function loadConfig() {
  CFG = await api('/api/config');
  document.getElementById('rules').innerHTML = Object.keys(CFG.rules)
      .map(k => sliderRow(k, CFG.rules[k], RULES[k] || k)).join('');
  document.getElementById('master').value = CFG.safety.master_multiplier;
  document.getElementById('maxpct').value = CFG.safety.max_pct;
  document.getElementById('maxabs').value = CFG.safety.max_absolute;
  document.getElementById('cfginfo').textContent = '数据源：' + CFG.source
      + '（bridge 里改档位；这里只改控制器的强度与设备）';
}

async function saveConfig() {
  const rules = {};
  Object.keys(CFG.rules).forEach(k => {
    rules[k] = { enabled: document.getElementById('r_'+k+'_on').checked,
                 base_pct: Number(document.getElementById('r_'+k).value) };
  });
  const body = { rules, safety: {
      master_multiplier: Number(document.getElementById('master').value),
      max_pct: Number(document.getElementById('maxpct').value),
      max_absolute: Number(document.getElementById('maxabs').value) } };
  const res = await api('/api/config', {method:'POST', headers:{'Content-Type':'application/json'},
                                        body: JSON.stringify(body)});
  document.getElementById('cfginfo').textContent = '已保存：' + res.applied.join(', ');
  await loadConfig();
}

async function loadBridge() {
  const s = await api('/api/status');
  const p = (s.bridge && s.bridge.parsed) || {};
  if (!s.bridge.exists) {
    document.getElementById('binfo').textContent = '还没有配置文件：当前 addon 跑内置默认（safe）。'
      + '选好档位点"写入"即可创建。';
  } else {
    document.getElementById('binfo').textContent = '已读取：' + s.bridge.config_path;
  }
  if (p.mode) document.getElementById('bmode').value = p.mode;
  if (p.port) document.getElementById('bport').value = p.port;
  if (p.interval) document.getElementById('bint').value = p.interval;
  if (p.profile) document.getElementById('bprof').value = p.profile;
  const o = p.offsets || {};
  if (o.hp !== undefined) document.getElementById('bhp').value = o.hp;
  if (o.hp_max !== undefined) document.getElementById('bhpma').value = o.hp_max;
  if (o.limb_mask !== undefined) document.getElementById('blimb').value = o.limb_mask;
  if (o.limb_shift !== undefined) document.getElementById('bshift').value = o.limb_shift;
  if (o.dead !== undefined) document.getElementById('bdead').value = o.dead;
}

async function saveBridge() {
  const body = {
    mode: document.getElementById('bmode').value,
    port: Number(document.getElementById('bport').value),
    interval: Number(document.getElementById('bint').value),
    profile: document.getElementById('bprof').value,
    offsets: {
      hp: Number(document.getElementById('bhp').value),
      hp_max: Number(document.getElementById('bhpma').value),
      limb_mask: Number(document.getElementById('blimb').value),
      limb_shift: Number(document.getElementById('bshift').value),
      dead: Number(document.getElementById('bdead').value),
    },
  };
  try {
    const res = await api('/api/bridge', {method:'POST', headers:{'Content-Type':'application/json'},
                                          body: JSON.stringify(body)});
    document.getElementById('binfo').textContent = res.note + '  → ' + res.path;
  } catch (e) {
    document.getElementById('binfo').textContent = '写入失败：' + e.message;
  }
  await loadBridge();
}

async function act(name) {
  try { await api('/api/actions', {method:'POST', headers:{'Content-Type':'application/json'},
                                   body: JSON.stringify({action:name})}); }
  catch (e) { alert('操作失败：' + e.message); }
  refresh();
}

async function testPulse() {
  const pct = Number(document.getElementById('tpct').value);
  const ms = Number(document.getElementById('tms').value);
  try { await api('/api/actions', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({action:'test_pulse', pct, ms})}); }
  catch (e) { alert('测试脉冲失败：' + e.message); }
}

// 控制器已经关了：按钮禁用 + 页面明说"别再监控了"（成功和"本来就没在跑"都走这里）
function markClosed(text) {
  window.__closed = true;
  if (window.__timer) { clearInterval(window.__timer); window.__timer = null; }
  document.getElementById('state').innerHTML = '<span class="dot bad"></span>控制器已关闭';
  document.getElementById('dwarn').textContent = text;
  const btn = document.getElementById('shutdownBtn');
  if (btn) { btn.disabled = true; btn.textContent = '已关闭'; btn.title = '控制器已经退出'; }
  document.getElementById('ver').textContent = (LASTVER ? 'v' + LASTVER : '') + ' · 已退出';
  document.getElementById('dev').textContent = '设备 已断开';
  document.getElementById('hk').textContent = '桥 离线';
}

async function shutdownApp() {
  if (window.__closed) return;
  if (!confirm('关闭 hd2-coyote 控制器？\n\n会先把输出归零、断开设备，然后退出程序。')) return;
  const btn = document.getElementById('shutdownBtn');
  if (btn) { btn.disabled = true; btn.textContent = '正在关闭…'; }
  try {
    const res = await api('/api/actions', {method:'POST', headers:{'Content-Type':'application/json'},
                                           body: JSON.stringify({action:'shutdown'})});
    markClosed('✓ ' + res.detail);
  } catch (e) {
    // 连不上 = 它已经退出了（最常见就是又点了一次）。这不是"失败"，别吓人。
    markClosed('✓ 控制器已经退出（连不上服务器）。要再用一次，重新运行 run.bat 或 '
               + 'python -m hd2coyote web。');
  }
}

loadConfig().then(loadBridge).then(loadSources).then(loadWaves).then(loadRamp)
    .catch(e => console.error(e));
refresh();
window.__timer = setInterval(refresh, 700);
</script>
</body>
</html>
"""

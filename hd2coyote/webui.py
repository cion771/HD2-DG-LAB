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
    POST /api/config       改控制器配置（局部合并、落盘、立即生效）
    POST /api/bridge       改游戏内桥的配置（写 bridge_config.lua，带备份）
    POST /api/actions      控制：start / stop / arm / trip / test_pulse
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

from . import __version__
from .config import AppConfig
from .engine import Engine, _fmt_event

LOGGER = logging.getLogger("hd2coyote.webui")

BRIDGE_MODES = ("safe", "net", "menu", "recon", "live")
BRIDGE_DEFAULT_DIR = Path.home() / "AppData" / "Local" / "hd2coyote"
OFFSET_KEYS = {"hp": 0x20, "hp_max": 0x24, "limb_mask": 0x28, "limb_shift": 0, "dead": 0x2C}
OFFSET_RANGE = {"hp": (-1, 0xFFFF), "hp_max": (-1, 0xFFFF), "limb_mask": (-1, 0xFFFF),
                "limb_shift": (0, 7), "dead": (-1, 0xFFFF)}


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

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

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
        else:
            self._json({"error": "not found"}, 404)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        route = self.path.split("?", 1)[0]
        body = self._read_body()
        try:
            if route == "/api/config":
                self._json(self.app.update_config(body))
            elif route == "/api/bridge":
                self._json(self.app.update_bridge(body))
            elif route == "/api/actions":
                self._json(self.app.action(body))
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
            },
            "events": events,
            "bridge": bridge_diagnostics(self.bridge_cfg_dir),
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

        self.cfg.save(self.config_path)
        # 立即生效（规则/安全是引用，设备与数据源要重建）
        if "device" in patch or "source" in patch:
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
        raise ValueError(f"未知动作：{name}")

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


def build_server(cfg: AppConfig, config_path: Path, host: str, port: int,
                 bridge_cfg_dir: str | Path | None = None) -> tuple[ThreadingHTTPServer, WebApp]:
    app = WebApp(cfg, config_path, bridge_cfg_dir)
    handler = type("BoundHandler", (_Handler,), {"app": app})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd, app


def run_web(cfg: AppConfig, config_path: Path, host: str = "127.0.0.1", port: int = 8787,
            bridge_cfg_dir: str | Path | None = None, open_browser: bool = True) -> None:
    httpd, app = build_server(cfg, config_path, host, port, bridge_cfg_dir)
    url = f"http://{host if host != '0.0.0.0' else '127.0.0.1'}:{port}/"
    print(f"hd2-coyote Web 控制台：{url}")
    print("  · 页面里可以：看状态、调强度、改桥的档位/偏移、看日志与部署自检")
    print("  · 桥的配置改完需要重启游戏生效；控制器动作（急停/测试脉冲）立即生效")
    if host == "0.0.0.0":
        print("  ⚠️ 你把它开在了所有网卡上 —— 这个页面能触发实际输出，公网/宿舍网请勿如此")
    if open_browser:
        threading.Thread(target=lambda: (time.sleep(0.6), webbrowser.open(url)),
                         daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止……")
    finally:
        app.engine.stop()
        httpd.server_close()


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
input[type=number],input[type=text],select{background:#141821;color:var(--fg);
       border:1px solid var(--line);border-radius:6px;padding:5px 7px;width:100%}
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
</main>
<script>
const RULES = {damage:'受伤', limb_injury:'肢体损伤', death:'阵亡', low_health:'低血量'};
let CFG = null;

async function api(path, opts) {
  const r = await fetch(path, opts);
  const t = await r.text();
  let j = null; try { j = JSON.parse(t); } catch (e) { j = {error: t}; }
  if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
  return j;
}
function fmtPct(v){ return (v===null||v===undefined) ? '--' : (v*100).toFixed(1)+'%'; }

async function refresh() {
  let s; try { s = await api('/api/status'); } catch (e) { return; }
  const c = s.controller, b = s.bridge || {};
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
        + ' (' + c.output.pct + '%)';
  document.getElementById('state').innerHTML = (c.armed
        ? '<span class="dot ok"></span>已武装' : '<span class="dot bad"></span>已静音 ' + (c.mute_reason||''))
        + ' · ' + (c.detecting ? '检测中' : '未检测');
  document.getElementById('sess').textContent = c.session_seconds + ' s';
  document.getElementById('src').textContent = c.source + ' · ' + (c.hook.detail || '');
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

loadConfig().then(loadBridge).catch(e => console.error(e));
refresh();
setInterval(refresh, 700);
</script>
</body>
</html>
"""

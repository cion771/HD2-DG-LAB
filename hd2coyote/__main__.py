"""命令行入口：

    python -m hd2coyote ui            图形界面（推荐）
    python -m hd2coyote run           纯命令行运行（Ctrl+C 退出）
    python -m hd2coyote simulate      无游戏自测（合成 HUD 画面）
    python -m hd2coyote calibrate     标定血条 / 损伤区
    python -m hd2coyote doctor        环境自检
    python -m hd2coyote test-pulse    手动测试一发（受安全上限约束）
    python -m hd2coyote waves [名称]   查看内置波形
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import time
from pathlib import Path

from . import __version__
from .config import AppConfig, CaptureConfig
from .waves import PRESETS, build

# engine / safety 是重依赖（engine 要 numpy，safety 经 device 要 websockets），
# 而 `stop` 这种命令完全用不到它们 —— 所以在函数内部按需导入，
# 这样"关掉控制器"永远不依赖游戏识别/设备栈是否装好。

# ⚠️ `from .device import ...` 会连带导入 websockets —— 而 `stop` 之类的命令根本不需要它。
#    所以改成用到时再导入（惰性），这样没装 websockets 的 Python 也能执行 stop/doctor 的一部分，
#    而且 `python -m hd2coyote stop` 的启动更快。
DEFAULT_CONFIG = "config.json"


def _device_api():
    """惰性取设备层的 API（首次调用才导入 websockets 等重依赖）。"""
    from .device import cmd_pulse, create_device, lan_ip

    return cmd_pulse, create_device, lan_ip


def setup_logging(level: str = "INFO", logfile: str = "hd2coyote.log") -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        handlers.append(logging.FileHandler(logfile, encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )


def load_config(path: str, overrides: dict | None = None) -> AppConfig:
    cfg = AppConfig.load(path)
    for key, value in (overrides or {}).items():
        if value is None:
            continue
        section, _, field = key.partition(".")
        target = getattr(cfg, section, None)
        if target is not None and hasattr(target, field):
            setattr(target, field, value)
    return cfg


# --------------------------------------------------------------------- 子命令
def cmd_web(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    from .webui import run_web

    run_web(cfg, Path(args.config), host=args.host, port=args.port,
            bridge_cfg_dir=args.bridge_dir or None, open_browser=not args.no_browser)
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    """关闭正在运行的 Web 控制台（先优雅退出，必要时强杀）。"""
    from .console_ctl import stop_console

    closed, _ = stop_console(host=args.host, port=args.port, force=not args.no_force)
    return 0 if closed else 1


def cmd_ui(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    from .ui import run_ui

    run_ui(cfg, Path(args.config))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from .engine import Engine

    overrides = {"device.kind": "mock" if args.mock else None}
    cfg = load_config(args.config, overrides)
    if args.mock:
        cfg.device.kind = "mock"
    engine = Engine(cfg)
    engine.start()
    if cfg.device.kind == "socket":
        dev = engine.device
        print(f"\n请用 DG-LAB App 扫描二维码（或手输）：\n  {getattr(dev, 'qr_payload', '')}\n")
    if cfg.source == "hook":
        print(f"状态来源：游戏内 Lua 桥（UDP {cfg.hook.host}:{cfg.hook.port}）")
        print("  安装：见 docs/HOOK.md —— 需要 Bingus Shared Loader v15+ 与 lua/hd2_coyote_bridge.lua")
        print("  自检：python -m hd2coyote doctor")
    else:
        print("状态来源：屏幕识别（需要先在界面里标定 HUD）")
    print(f"事件源  ：{'、'.join(cfg.sources.enabled)}")
    if "http" in cfg.sources.enabled:
        token = "（需要 X-HD2Coyote-Token）" if cfg.sources.http_token else ""
        print(f"  HTTP 上报：POST http://{cfg.sources.http_host}:{cfg.sources.http_port}/event{token}")
        print("  例子：见 docs/SOURCES.md，或 Web 控制台的「事件源」面板")
    print("检测中…… Ctrl+C 退出")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n正在停止……")
    finally:
        engine.stop()
    return 0


def cmd_simulate(args: argparse.Namespace) -> int:
    from .engine import Engine

    from .simulate import run_demo

    cfg = load_config(args.config)
    cfg.source = "vision"  # 合成画面走视觉链路
    cfg.device.kind = "mock" if args.mock else cfg.device.kind
    if args.socket:
        cfg.device.kind = "socket"
    engine = Engine(cfg)
    engine.on_event = lambda ev: None
    engine.device.start()
    try:
        run_demo(cfg, engine, duration=args.seconds)
    finally:
        engine.device.stop()
    return 0


def cmd_calibrate(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    from .ui import run_calibration

    run_calibration(cfg, Path(args.config))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    print(f"hd2-coyote {__version__}")
    print(f"Python      : {sys.version.split()[0]}")
    print(f"配置文件    : {Path(args.config).resolve()} 存在={Path(args.config).exists()}")
    for mod in ("numpy", "mss", "PIL", "websockets", "qrcode", "dxcam", "tkinter"):
        try:
            __import__(mod)
            print(f"依赖 {mod:<12}: OK")
        except Exception as exc:
            print(f"依赖 {mod:<12}: 缺失（{exc}）")
    cfg = AppConfig.load(args.config)
    print(f"状态来源    : {cfg.source}（hook = 游戏内 Lua 桥；vision = 屏幕识别）")
    from .sources import source_catalog

    names = [item["name"] for item in source_catalog()]
    print(f"事件源      : {'、'.join(cfg.sources.enabled) or '（未启用任何源）'}"
          f"（可用：{'、'.join(names)}）")
    if "http" in cfg.sources.enabled:
        print(f"HTTP 事件源 : http://{cfg.sources.http_host}:{cfg.sources.http_port}/event"
              f"{'（需要令牌）' if cfg.sources.http_token else '（无令牌）'}")
    print(f"设备        : {cfg.device.kind} {cfg.device.host}:{cfg.device.port}")
    _, _, lan_ip = _device_api()
    print(f"本机局域网IP: {lan_ip()}")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        busy = s.connect_ex(("127.0.0.1", cfg.device.port)) == 0
    print(f"端口 {cfg.device.port}     : {'已被占用（先关掉占用程序）' if busy else '可用'}")

    # 游戏内 addon（hook 路线）环境自检
    try:
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.bind((cfg.hook.host, cfg.hook.port))
        udp.close()
        print(f"Hook 端口   : {cfg.hook.host}:{cfg.hook.port} 可用（UDP）")
    except OSError as exc:
        print(f"Hook 端口   : {cfg.hook.host}:{cfg.hook.port} 不可用（{exc}）")
    log = Path(os.environ.get("LOCALAPPDATA", "")) / "CowboyBingus" / "Helldivers2" / "Logs" / "BingusSharedLoader.log"
    if log.exists():
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
        print(f"加载器      : {lines[0] if lines else '?'}")
        declared = [ln for ln in lines if ln.startswith("mods/")]
        bridge = [ln for ln in declared if "hd2coyote" in ln]
        print(f"已发现 addon: {len(declared)} 个；hd2-coyote 桥："
              f"{'已安装（' + bridge[0] + '）' if bridge else '未安装 —— 见 docs/HOOK.md'}")
    else:
        print("加载器      : 未找到 BingusSharedLoader.log（没装社区加载器 → hook 路线不可用）")

    try:
        from .capture import ScreenGrabber

        grab = ScreenGrabber(CaptureConfig(monitor=cfg.capture.monitor,
                                           backend=cfg.capture.backend))
        frame = grab.full_frame()
        print(f"抓屏后端    : {grab.backend} 画面 {frame.shape[1]}x{frame.shape[0]} "
              f"平均亮度 {frame.mean():.1f}")
        grab.close()
        print("提示        : 游戏请用「无边框窗口」；独占全屏可能导致黑屏")
    except Exception as exc:
        print(f"抓屏        : 失败（{exc}）")
    print(f"血条已标定  : {bool(cfg.hud.hp_bar and cfg.hud.hp_bar.is_valid())}")
    print(f"安全上限    : max_pct={cfg.safety.max_pct}% max_absolute={cfg.safety.max_absolute}")
    print("说明        : 本程序只读屏幕像素，不注入游戏、不读写游戏内存")
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    """查一眼 GitHub Releases 有没有新版本 —— 只提示 + 给链接，不自动下载安装。"""
    from . import update_check

    cfg = load_config(args.config)
    repo = str(args.repo or cfg.update.repo)
    result = update_check.check(repo=repo, current=__version__, timeout=float(args.timeout))
    print(update_check.format_result(result))
    if not cfg.update.enabled:
        print("（配置里 update.enabled = false：网页上不会主动提示，这次是你手动查的）")
    return 0 if result.get("ok") else 1


def cmd_test_pulse(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if args.mock:
        cfg.device.kind = "mock"
    _, create_device, _ = _device_api()
    device = create_device(cfg.device)
    from .safety import SafetyGuard

    guard = SafetyGuard(cfg.safety, device)
    device.start()
    if cfg.device.kind == "socket":
        print(f"等待 App 绑定：{getattr(device, 'qr_payload', '')}")
        deadline = time.monotonic() + args.wait
        while time.monotonic() < deadline and not device.connected:
            time.sleep(0.2)
        if not device.connected:
            print("超时：App 未连接，改为本地演示（不发强度）")
    value = guard.strength_for(args.pct, device.limit_of("A"))
    print(f"测试输出：pct={args.pct} -> 实际强度 {value}（上限 {cfg.safety.max_absolute}）")
    for ch in device.channels_of(args.channel):
        device.clear(ch)
        device.send_wave(ch, build(args.wave, args.ms))
    device.set_strength(value, value)
    time.sleep(args.ms / 1000.0)
    device.mute()
    device.stop()
    return 0


def cmd_waves(args: argparse.Namespace) -> int:
    if not args.name:
        print("内置波形：")
        for name in sorted(PRESETS):
            units = build(name, 1000)
            print(f"  {name:<10} 1 秒 {len(units)} 单元，示例 {units[0]}")
        return 0
    units = build(args.name, args.ms)
    print(f"{args.name}: {len(units)} 单元 / {len(units) * 100} ms")
    cmd_pulse, _, _ = _device_api()
    print(cmd_pulse("A", units[:10]))
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=DEFAULT_CONFIG, help="配置文件路径（可写在子命令后面）")

    ap = argparse.ArgumentParser(prog="hd2coyote", description="绝地潜兵 2 × 郊狼 DG-LAB",
                                 parents=[common])
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("ui", parents=[common], help="桌面界面（Tkinter）")
    p.set_defaults(func=cmd_ui)

    p = sub.add_parser("web", parents=[common], help="Web 控制台（浏览器，推荐）")
    p.add_argument("--host", default="127.0.0.1", help="监听地址（默认只监听本机）")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--bridge-dir", default="", help="桥的配置目录（默认 %%LOCALAPPDATA%%\\hd2coyote）")
    p.add_argument("--no-browser", action="store_true", help="不要自动打开浏览器")
    p.set_defaults(func=cmd_web)

    p = sub.add_parser("stop", help="关闭正在运行的 Web 控制台")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--no-force", action="store_true", help="优雅退出失败时不要强杀进程")
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("run", parents=[common], help="命令行运行")
    p.add_argument("--mock", action="store_true", help="不接硬件，打印输出")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("simulate", parents=[common], help="无游戏自测")
    p.add_argument("--mock", action="store_true", default=True)
    p.add_argument("--socket", action="store_true", help="真的连 App 输出")
    p.add_argument("--seconds", type=float, default=0.0)
    p.set_defaults(func=cmd_simulate)

    p = sub.add_parser("calibrate", parents=[common], help="标定 HUD 区域")
    p.set_defaults(func=cmd_calibrate)

    p = sub.add_parser("doctor", parents=[common], help="环境自检")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("test-pulse", parents=[common], help="手动测试一发")
    p.add_argument("--pct", type=float, default=5.0, help="强度百分比（相对通道上限）")
    p.add_argument("--ms", type=float, default=800.0)
    p.add_argument("--channel", default="both", choices=["A", "B", "both"])
    p.add_argument("--wave", default="ramp_up")
    p.add_argument("--wait", type=float, default=30.0, help="等待 App 连接的秒数")
    p.add_argument("--mock", action="store_true")
    p.set_defaults(func=cmd_test_pulse)

    p = sub.add_parser("waves", parents=[common], help="查看/试听波形数据")
    p.add_argument("name", nargs="?", default="")
    p.add_argument("--ms", type=float, default=1000.0)
    p.set_defaults(func=cmd_waves)

    p = sub.add_parser("update", parents=[common], help="检查有没有新版本（只提示，不自动安装）")
    p.add_argument("--check", action="store_true", help="查一次（默认行为，加上只为读起来清楚）")
    p.add_argument("--repo", default="", help="覆盖 GitHub 仓库（owner/name）")
    p.add_argument("--timeout", type=float, default=6.0, help="网络超时秒数")
    p.set_defaults(func=cmd_update)
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if not getattr(args, "cmd", None):
        args = ap.parse_args(["ui"])
    setup_logging()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

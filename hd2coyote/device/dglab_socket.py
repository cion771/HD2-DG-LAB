"""DG-LAB Socket 协议（郊狼 3.0 / 官方 App 中转）实现。

协议拓扑（官方 v2 Socket 协议，社区实现一致）：

    第三方控制器（本程序，WebSocket 服务端）
        ▲  ws://<本机局域网IP>:<port>/<clientId>
        │
    DG-LAB 手机 App（扫码连接，作为客户端）
        ▲  蓝牙
        │
    郊狼主机（Coyote 3.0）

握手：
    1. App 扫码后连到 ws://ip:port/<clientId>
    2. 服务端发 {"type":"bind","clientId":<控制器UUID>,"targetId":<新UUID>,"message":"targetId"}
    3. App 回 {"type":"bind",...,"message":"DGLAB"}
    4. 服务端回 {"type":"bind",...,"message":"200"} → 绑定完成

控制指令（message 字段，纯文本）：
    strength-{通道}+{模式}+{值}   通道 1=A 2=B；模式 0=降低 1=增加 2=设置为；值 0~200
    pulse-{A|B}:["<16位HEX>", …]  波形数据，单条最多 100 单元，App 队列 500 单元
    clear-{1|2}                   清空该通道波形队列
    heartbeat                     60 秒一次

App 上行：
    strength-{A强度}+{B强度}+{A上限}+{B上限}   通道状态反馈
    feedback-{0..9}                            手机上的反馈按钮

限制：单条 WebSocket 帧不得超过 1950 字符，超出会被 App 丢弃（错误码 405）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import threading
import uuid
from typing import Any, Callable, Iterable

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from ..events import DeviceState, Event, FeedbackButton
from .base import Device

MAX_FRAME_CHARS = 1950
SAFE_FRAME_CHARS = 1880  # 留一点余量
CHANNEL_INDEX = {"A": 1, "B": 2}
MODE_DECREASE, MODE_INCREASE, MODE_SET = 0, 1, 2

#: 手机 App 二维码内容前缀（官方格式）
QR_PREFIX = "https://www.dungeon-lab.com/app-download.php#DGLAB-SOCKET#"


def lan_ip() -> str:
    """探测本机在局域网中的 IP（不会真的发包）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def cmd_strength(channel: str, value: int, mode: int = MODE_SET) -> str:
    """strength-{1|2}+{0|1|2}+{0~200}"""
    idx = CHANNEL_INDEX[channel]
    value = max(0, min(200, int(value)))
    return f"strength-{idx}+{int(mode)}+{value}"


def cmd_clear(channel: str) -> str:
    """clear-{1|2}"""
    return f"clear-{CHANNEL_INDEX[channel]}"


def cmd_pulse(channel: str, units: list[str]) -> str:
    """pulse-{A|B}:["hex", …]"""
    body = ",".join(f'"{u}"' for u in units)
    return f"pulse-{channel}:[{body}]"


class DGLabSocketDevice(Device):
    """作为 WebSocket 服务端，等待 DG-LAB App 扫码接入。"""

    name = "dglab-socket"

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 9999,
        advertise_ip: str = "",
        on_event: Callable[[Event], None] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        super().__init__()
        self.host = host
        self.port = port
        self.advertise_ip = advertise_ip or None
        self.client_id = str(uuid.uuid4())
        self.on_event = on_event
        self.log = logger or logging.getLogger("hd2coyote.device.dglab")

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop_event: asyncio.Event | None = None
        self._ready = threading.Event()
        self._clients: dict[ServerConnection, str] = {}
        self._bound: set[ServerConnection] = set()
        self._hb_task: asyncio.Task | None = None
        #: 已发送的原始报文（测试与排错用）
        self.sent: list[str] = []

    # ---------------------------------------------------------------- 展示信息
    @property
    def ws_url(self) -> str:
        return f"ws://{self.advertise_ip or lan_ip()}:{self.port}/{self.client_id}"

    @property
    def qr_payload(self) -> str:
        """手机 App 扫码用的二维码内容。"""
        return QR_PREFIX + self.ws_url

    @property
    def app_count(self) -> int:
        return len(self._bound)

    # ---------------------------------------------------------------- 生命周期
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._ready.clear()
        self._stop_event = asyncio.Event()
        self._thread = threading.Thread(target=self._run, name="dglab-ws", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=5.0):
            raise RuntimeError(f"WebSocket 服务未能启动（{self.host}:{self.port} 可能被占用）")

    def stop(self) -> None:
        self.mute()
        loop, ev = self._loop, self._stop_event
        if loop is not None and ev is not None:
            try:
                loop.call_soon_threadsafe(ev.set)
            except RuntimeError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self._thread = None
        self._set_connected(False, "已停止")

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._main())
        except Exception as exc:  # 端口占用等
            self.log.error("WebSocket 服务异常退出：%s", exc)
            self._ready.set()
        finally:
            try:
                loop.close()
            except Exception:
                pass
            self._loop = None

    async def _main(self) -> None:
        async with serve(self._handle, self.host, self.port, ping_interval=20, ping_timeout=20):
            self.log.info("WebSocket 服务已启动：%s", self.ws_url)
            self._ready.set()
            self._hb_task = asyncio.create_task(self._heartbeat_loop())
            assert self._stop_event is not None
            try:
                await self._stop_event.wait()
            finally:
                if self._hb_task is not None:
                    self._hb_task.cancel()

    async def _heartbeat_loop(self) -> None:
        """协议心跳：官方建议 60 秒，这里用 25 秒更保险。"""
        while True:
            await asyncio.sleep(25.0)
            await self._broadcast_message("heartbeat", extra="200", raw_type="heartbeat")

    # ---------------------------------------------------------------- 连接处理
    async def _handle(self, ws: ServerConnection) -> None:
        path = ""
        request = getattr(ws, "request", None)
        if request is not None:
            path = getattr(request, "path", "") or ""
        token = path.rsplit("/", 1)[-1] if path else ""
        if token and token != self.client_id:
            self.log.warning("收到未知 clientId 的连接：%s（仍接受）", token)
        target_id = str(uuid.uuid4())
        self._clients[ws] = target_id
        self.log.info("App 已接入：%s", path or "(无路径)")
        try:
            await self._send_raw(ws, {
                "type": "bind",
                "clientId": self.client_id,
                "targetId": target_id,
                "message": "targetId",
            })
            async for raw in ws:
                await self._on_message(ws, raw)
        except ConnectionClosed:
            pass
        except Exception as exc:
            self.log.debug("连接异常结束：%s", exc)
        finally:
            self._clients.pop(ws, None)
            self._bound.discard(ws)
            if not self._bound:
                self._set_connected(False, "App 已断开")
            self.log.info("App 已断开（剩余 %d）", len(self._bound))

    async def _on_message(self, ws: ServerConnection, raw: str | bytes) -> None:
        try:
            data = json.loads(raw)
        except Exception:
            self.log.warning("收到非 JSON 报文：%r", raw[:120])
            return
        mtype = str(data.get("type", ""))
        message = str(data.get("message", ""))

        if mtype == "bind":
            if message == "DGLAB":
                app_target = str(data.get("targetId") or self._clients.get(ws, ""))
                self._clients[ws] = app_target
                await self._send_raw(ws, {
                    "type": "bind",
                    "clientId": self.client_id,
                    "targetId": app_target,
                    "message": "200",
                })
                self._bound.add(ws)
                self._set_connected(True, "App 已绑定")
                # 归零强度，促使 App 回报通道上限
                for ch in ("A", "B"):
                    await self._send_msg_to(ws, cmd_strength(ch, 0, MODE_SET))
            else:
                self.log.debug("bind 消息：%s", message)
            return

        if mtype == "msg":
            if message.startswith("strength-"):
                self._parse_strength_feedback(message)
                self._bound.add(ws)
                self._set_connected(True, "App 已绑定")
            elif message.startswith("feedback-"):
                try:
                    idx = int(message.split("-", 1)[1])
                except (IndexError, ValueError):
                    idx = -1
                self._emit(FeedbackButton(index=idx))
            else:
                self.log.debug("App 上行消息：%s", message)
            return

        if mtype == "heartbeat":
            if message != "200":
                await self._send_raw(ws, {
                    "type": "heartbeat",
                    "clientId": self.client_id,
                    "targetId": self._clients.get(ws, ""),
                    "message": "200",
                })
            return

        if mtype in ("break", "error"):
            self.log.info("App 通知 %s：%s", mtype, message)
            self._bound.discard(ws)
            if not self._bound:
                self._set_connected(False, f"{mtype} {message}")

    def _parse_strength_feedback(self, message: str) -> None:
        """strength-{A强度}+{B强度}+{A上限}+{B上限}"""
        parts = message.split("-", 1)[1].split("+")
        if len(parts) != 4:
            return
        try:
            a, b, a_lim, b_lim = (int(p) for p in parts)
        except ValueError:
            return
        with self._lock:
            self._limits.a = max(0, min(200, a_lim))
            self._limits.b = max(0, min(200, b_lim))
        self.log.debug("通道反馈：A=%s/%s B=%s/%s", a, a_lim, b, b_lim)

    # ---------------------------------------------------------------- 发送
    def _submit(self, coro: Any) -> None:
        loop = self._loop
        if loop is None or not loop.is_running():
            coro.close()
            return
        try:
            asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=2.0)
        except Exception as exc:
            self.log.debug("发送失败：%s", exc)

    async def _send_raw(self, ws: ServerConnection, payload: dict[str, Any]) -> None:
        text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        with self._lock:
            self.sent.append(text)
            if len(self.sent) > 500:
                del self.sent[:250]
        await ws.send(text)

    async def _send_msg_to(self, ws: ServerConnection, message: str) -> None:
        await self._send_raw(ws, {
            "type": "msg",
            "clientId": self.client_id,
            "targetId": self._clients.get(ws, ""),
            "message": message,
        })

    async def _broadcast_message(self, message: str, extra: str = "", raw_type: str = "msg") -> None:
        payload_message = extra or message
        for ws in list(self._bound):
            payload = {
                "type": raw_type,
                "clientId": self.client_id,
                "targetId": self._clients.get(ws, ""),
                "message": payload_message,
            }
            try:
                await self._send_raw(ws, payload)
            except Exception:
                self._bound.discard(ws)

    async def _broadcast_msg(self, message: str) -> None:
        await self._broadcast_message(message, raw_type="msg")

    def _max_units(self) -> int:
        """按 1950 字符上限反推单条 pulse 消息最多能装多少单元。

        注意 message 里的引号在 JSON 里会被转义（每个 +1 字符），
        所以这里直接按真实报文长度试算，而不是靠估算。
        """
        payload = {
            "type": "msg",
            "clientId": self.client_id,
            "targetId": "x" * 36,
            "message": "",
        }
        overhead = len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
        units = min(100, max(1, (SAFE_FRAME_CHARS - overhead) // 21))
        while units > 1:
            payload["message"] = cmd_pulse("A", ["0A0A0A0A64646464"] * units)
            size = len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
            if size <= MAX_FRAME_CHARS - 20:
                break
            units -= 1
        return units

    # ---------------------------------------------------------------- Device 接口
    def set_strength(self, a: int | None = None, b: int | None = None) -> None:
        if a is not None:
            self._submit(self._broadcast_msg(cmd_strength("A", a, MODE_SET)))
        if b is not None:
            self._submit(self._broadcast_msg(cmd_strength("B", b, MODE_SET)))

    def send_wave(self, channel: str, units: Iterable[str]) -> None:
        units = list(units)
        if not units:
            return
        step = self._max_units()
        for i in range(0, len(units), step):
            self._submit(self._broadcast_msg(cmd_pulse(channel, units[i:i + step])))

    def clear(self, channel: str) -> None:
        self._submit(self._broadcast_msg(cmd_clear(channel)))

    # ---------------------------------------------------------------- 内部工具
    def _set_connected(self, value: bool, detail: str = "") -> None:
        if self._connected == value:
            return
        self._connected = value
        self._emit(DeviceState(connected=value, detail=detail))

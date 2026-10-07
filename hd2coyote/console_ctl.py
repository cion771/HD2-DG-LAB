"""关闭控制器的客户端逻辑（`python -m hd2coyote stop` 与 stop.bat 都用它）。

为什么要单独一个模块：`stop.bat` 里用 `powershell -Command "..."` 拼多行命令
在 cmd 下极其脆弱（`(`、`{`、引号都会把 cmd 的解析搞崩 —— 实测就是这么坏的）。
所以批处理只做一件事：调用本模块，逻辑全在 Python 里，可测试。

关闭顺序（安全相关）：
  1. 先请控制器**自己**优雅退出 —— 它会先把输出归零、清波形、断开设备，再关服务器；
  2. 等端口真的释放；
  3. 只有在第 1 步没成功时才强杀进程（这样最坏情况是"电极侧靠 App 自己断开"）。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787


def post_shutdown(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                  timeout: float = 5.0) -> tuple[bool, str]:
    """请控制器优雅退出。返回 (是否成功发出, 说明)。"""
    url = f"http://{host}:{port}/api/actions"
    body = json.dumps({"action": "shutdown"}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return True, str(payload.get("detail", "已请求退出"))
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}（{exc.reason}）"
    except Exception as exc:  # 连不上 = 可能本来就没在跑
        return False, f"连不上 {host}:{port}（{type(exc).__name__}）"


def port_is_open(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 timeout: float = 0.6) -> bool:
    with socket.socket() as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def wait_port_closed(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                     timeout: float = 8.0, interval: float = 0.25) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not port_is_open(host, port):
            return True
        time.sleep(interval)
    return not port_is_open(host, port)


def port_pids(port: int, state: str = "LISTENING") -> list[int]:
    """找出正在监听该端口的进程号（用系统 netstat，不引入 psutil 依赖）。"""
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True,
                             text=True, errors="replace", timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    pids: list[int] = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP":
            continue
        if parts[3].upper() != state.upper():
            continue
        if parts[1].rsplit(":", 1)[-1] != str(port):
            continue
        try:
            pid = int(parts[4])
        except ValueError:
            continue
        if pid not in pids:
            pids.append(pid)
    return pids


def kill_pids(pids: list[int]) -> list[int]:
    killed: list[int] = []
    for pid in pids:
        if pid == os.getpid():
            continue
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True,
                           timeout=10)
            killed.append(pid)
        except (OSError, subprocess.SubprocessError):
            continue
    return killed


def stop_console(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, force: bool = True,
                 timeout: float = 8.0, log=print) -> tuple[bool, list[str]]:
    """关掉控制器。返回 (是否已关闭, 过程说明)。"""
    lines: list[str] = []

    def say(text: str) -> None:
        lines.append(text)
        log(text)

    if not port_is_open(host, port):
        say(f"端口 {port} 没有在监听 —— 控制器本来就没在运行。")
        return True, lines

    ok, detail = post_shutdown(host, port)
    say(("已请求优雅退出：" if ok else "优雅退出失败：") + detail)

    if wait_port_closed(host, port, timeout=timeout):
        say(f"端口 {port} 已释放，控制器已关闭（输出在退出时已归零）。")
        return True, lines

    pids = port_pids(port)
    if not pids:
        say(f"端口 {port} 仍被占用，但找不到占用进程；请手动处理。")
        return False, lines
    if not force:
        say(f"端口 {port} 仍被占用（PID {', '.join(map(str, pids))}）；按 --no-force 的约定不强制结束。")
        return False, lines

    say(f"仍在监听，强制结束 PID {', '.join(map(str, pids))} ……")
    killed = kill_pids(pids)
    closed = wait_port_closed(host, port, timeout=5.0)
    say(f"已结束 {', '.join(map(str, killed)) or '（无）'}；端口{'已释放' if closed else '仍占用'}。")
    if closed:
        say("注意：这次是强杀，电极侧应由手机 App 在连接断开后自行停止。")
    return closed, lines

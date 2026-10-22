"""外部 HUD 视觉状态源：独立抓屏线程，不注入、不读取游戏内存。

安全边界（重要）：
  * 只调用 Win32 的窗口枚举/几何 API 与桌面复制（mss），不 OpenProcess、
    不 ReadProcessMemory、不写游戏内存、不加载任何模块进游戏进程；
  * 只截取游戏窗口的客户区，不截整屏，避免把任务栏/其它窗口当 HUD；
  * 游戏不在前台时不发布读数，旧读数自然过期，由引擎按 critical 源断流静音。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading
import time

from ..config import AppConfig, Box
from ..detectors import EventTrackers
from ..events import Event
from ..vision import auto_locate_hp_bar, bar_fill_ratio, is_black_frame
from .base import Source, register_source

_user32 = ctypes.windll.user32
# ctypes 默认以 32 位 int 传 HWND；显式签名防止 64 位窗口句柄被截断。
_user32.GetForegroundWindow.restype = wt.HWND
_user32.IsWindowVisible.argtypes = [wt.HWND]
_user32.IsWindowVisible.restype = wt.BOOL
_user32.IsIconic.argtypes = [wt.HWND]
_user32.IsIconic.restype = wt.BOOL
_user32.GetWindowTextLengthW.argtypes = [wt.HWND]
_user32.GetWindowTextLengthW.restype = ctypes.c_int
_user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
_user32.GetWindowTextW.restype = ctypes.c_int
_user32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
_user32.GetClientRect.restype = wt.BOOL
_user32.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
_user32.ClientToScreen.restype = wt.BOOL
_user32.SetProcessDPIAware()


def _client_rect(hwnd: int) -> tuple[int, int, int, int]:
    """窗口客户区在屏幕上的 (x, y, w, h)；最小化时返回 0 尺寸。"""
    if _user32.IsIconic(hwnd):
        return (0, 0, 0, 0)
    rect = wt.RECT()
    if not _user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return (0, 0, 0, 0)
    point = wt.POINT(0, 0)
    if not _user32.ClientToScreen(hwnd, ctypes.byref(point)):
        return (0, 0, 0, 0)
    return (point.x, point.y, rect.right - rect.left, rect.bottom - rect.top)


DEFAULT_TITLES = ("HELLDIVERS", "绝地潜兵", "HELLDIVER")


def title_candidates(title_contains: str) -> tuple[str, ...]:
    """把配置里的标题（可用 | 分隔）和内置候选合并去重。"""
    parts = [p.strip() for p in str(title_contains).split("|") if p.strip()]
    out: list[str] = []
    for item in (*parts, *DEFAULT_TITLES):
        if item and item.upper() not in {x.upper() for x in out}:
            out.append(item)
    return tuple(out)


def window_title_matches(title: str, title_contains: str) -> bool:
    """保守匹配完整标题，不让含游戏名的网页/聊天窗口冒充游戏 HUD。"""
    candidate = title.strip().upper().replace("™", "").replace(" ", "")
    allowed = set()
    for name in title_candidates(title_contains):
        token = name.upper().replace("™", "").replace(" ", "")
        allowed.add(token)
        if token in {"HELLDIVERS", "绝地潜兵", "HELLDIVER"}:
            allowed.add(token + "2")
    return candidate in allowed


def find_window(title_contains: str) -> int:
    """只用 EnumWindows + 窗口标题识别游戏，不打开任何进程句柄。"""
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def callback(hwnd, _lparam):
        if not _user32.IsWindowVisible(hwnd):
            return True
        length = _user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buffer, length + 1)
        if window_title_matches(buffer.value, title_contains):
            found.append(hwnd)
        return True

    _user32.EnumWindows(callback, 0)
    return found[0] if found else 0


def capture_game_client(title: str, backend: str = "mss"):
    """抓取前台游戏窗口客户区 RGB 帧及屏幕几何；校准与实时读取共用。"""
    hwnd = find_window(title)
    if not hwnd:
        raise RuntimeError("未找到游戏窗口（请先启动游戏并进入任务）")
    if _user32.GetForegroundWindow() != hwnd:
        raise RuntimeError("游戏不在前台；校准时请切回任务画面")
    cx, cy, cw, ch = _client_rect(hwnd)
    if cw < 320 or ch < 240:
        raise RuntimeError("游戏窗口已最小化或尺寸过小")
    import numpy as np
    if backend == "pil":
        from PIL import ImageGrab
        frame = np.asarray(ImageGrab.grab(
            bbox=(cx, cy, cx + cw, cy + ch), all_screens=True).convert("RGB"))
    elif backend == "mss":
        import mss
        with mss.mss() as sct:
            shot = sct.grab({"left": cx, "top": cy, "width": cw, "height": ch})
        frame = np.frombuffer(shot.rgb, dtype=np.uint8).reshape(shot.height, shot.width, 3)
    else:
        raise ValueError(f"不支持抓屏后端：{backend}")
    if is_black_frame(frame):
        raise RuntimeError("窗口客户区接近黑屏；请使用窗口化/无边框模式")
    return frame, (cx, cy, cw, ch)


@register_source
class VisionSource(Source):
    name = "vision"
    label = "外部 HUD 视觉（EAC 兼容）"
    kind = "state"
    critical = True
    hint = ("独立进程只截取游戏窗口客户区。需先在任务中校准血条；"
            "选择 vision 时取消勾选 game_bridge，避免旧桥断流使输出持续静音。")

    def __init__(self, cfg: AppConfig, logger=None) -> None:
        super().__init__(cfg, logger)
        self.trackers = EventTrackers(cfg)
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._seq = self._polled_seq = 0
        self._had_live_sample = False
        self._needs_baseline = True
        self._capture_valid = False
        self._hp: float | None = None
        self._last_at = 0.0
        self._box: Box | None = cfg.vision.hp_bar
        self._startup_error = ""
        self._capture_errors = 0
        self._note = ""

    # ---------------------------------------------------------------- 状态
    @property
    def alive(self) -> bool:
        timeout = max(0.5, float(self.cfg.vision.stale_timeout_s))
        return bool(self.started and self._capture_valid and self._last_at
                    and time.monotonic() - self._last_at <= timeout)

    @property
    def hp(self) -> float | None:
        """只有新鲜读数能提供给桌面状态卡与惩罚累积。"""
        with self._lock:
            return self._hp if self.alive else None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._ready.clear()
        self._startup_error = ""
        self._thread = threading.Thread(target=self._run, name="vision-hud", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=4.0):
            raise RuntimeError("视觉源启动超时")
        if self._startup_error:
            raise RuntimeError(self._startup_error)
        self.started = True
        self.error = ""

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        self._thread = None
        self.started = False

    def poll(self, now: float) -> list[Event]:
        with self._lock:
            # 断流后的首帧只能建立血量基线，不能把菜单/复活造成的跳变当作伤害。
            if not self.alive:
                if self._had_live_sample:
                    self.trackers.reset()
                    self._had_live_sample = False
                self._needs_baseline = True
                self._polled_seq = self._seq
                return []
            if self._seq == self._polled_seq:
                return []
            if self._needs_baseline:
                self.trackers.reset()
                self._needs_baseline = False
            self._polled_seq = self._seq
            self._had_live_sample = True
            hp = self._hp
        events = self.trackers.process(
            hp, [], 0.0, 0.0, now,
            threshold=self.cfg.detect.death_state_threshold,
            names=self.cfg.hud.injury_slot_names,
        )
        self.events += len(events)
        return events

    def describe(self) -> str:
        with self._lock:
            hp, box, note = self._hp, self._box, self._note
        if self._startup_error:
            return "启动失败：" + self._startup_error
        if not self.started:
            return "未启动"
        area = "未定位血条" if box is None else f"血条 {box.x},{box.y} {box.w}x{box.h}"
        value = "--" if hp is None else f"{hp * 100:.1f}%"
        extra = f"；{note}" if note else ""
        return f"{area}；HP={value}；抓屏错误={self._capture_errors}{extra}"

    def apply_config(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self.trackers = EventTrackers(cfg)

    # ---------------------------------------------------------------- 内部
    def _publish(self, hp: float, box: Box, note: str = "") -> None:
        with self._lock:
            now = time.monotonic()
            timeout = max(0.5, float(self.cfg.vision.stale_timeout_s))
            if not self._capture_valid or (self._last_at and now - self._last_at > timeout):
                self._needs_baseline = True
            self._capture_valid = True
            self._hp = max(0.0, min(1.0, float(hp)))
            self._box = box
            self._note = note
            self._last_at = now
            self._seq += 1

    def _invalidate(self, reason: str) -> None:
        """失焦、黑帧或校准失败时立刻撤销上一帧读数。"""
        with self._lock:
            self._capture_valid = False
            self._hp = None
            self._needs_baseline = True
            self._note = reason[:120]

    def _run(self) -> None:
        try:
            if self.cfg.vision.backend == "pil":
                self._run_pil()
            else:
                self._run_mss()
        except Exception as exc:
            self._startup_error = f"{type(exc).__name__}: {exc}"
            self.error = self._startup_error
            self.log.error("视觉源失败：%s", self._startup_error)
        finally:
            self._ready.set()

    def _run_mss(self) -> None:
        import mss
        import numpy as np

        with mss.mss() as sct:
            self._ready.set()
            self._capture_loop(lambda region: np.asarray(sct.grab(region), dtype=np.uint8)[..., :3])

    def _run_pil(self) -> None:
        import numpy as np
        from PIL import ImageGrab

        self._ready.set()

        def grab(region: dict[str, int]):
            bbox = (region["left"], region["top"],
                    region["left"] + region["width"], region["top"] + region["height"])
            return np.asarray(ImageGrab.grab(bbox=bbox, all_screens=True).convert("RGB"))

        self._capture_loop(grab)

    def _capture_loop(self, grab) -> None:
        fps = max(1.0, min(30.0, float(self.cfg.vision.fps)))
        delay = 1.0 / fps
        box = self._box if self._box is not None and self._box.is_valid() else None
        title = self.cfg.safety.game_window_title
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                hwnd = find_window(title)
                if not hwnd:
                    raise RuntimeError(
                        "没有找到游戏窗口（标题候选：" + "/".join(title_candidates(title))
                        + "）；请先启动游戏")
                cx, cy, cw, ch = _client_rect(hwnd)
                if cw < 320 or ch < 240:
                    raise RuntimeError("游戏窗口已最小化或尺寸过小")
                foreground = _user32.GetForegroundWindow() == hwnd
                if not foreground:
                    raise RuntimeError("游戏不在前台，暂不读取（避免误读其它窗口）")
                if box is None:
                    frame = grab({"left": cx, "top": cy, "width": cw, "height": ch})
                    if is_black_frame(frame):
                        raise RuntimeError("窗口客户区全黑；请改用无边框窗口模式")
                    if self.cfg.vision.auto_locate:
                        box = auto_locate_hp_bar(frame, brightness=float(self.cfg.vision.brightness))
                    if box is None:
                        raise RuntimeError("尚未定位到血条；需在任务中、HUD 可见")
                    hp = bar_fill_ratio(frame, box)
                else:
                    if box.x + box.w > cw or box.y + box.h > ch:
                        raise RuntimeError("已保存的血条区域超出窗口；请重新校准")
                    frame = grab({"left": cx + box.x, "top": cy + box.y,
                                  "width": box.w, "height": box.h})
                    if is_black_frame(frame):
                        raise RuntimeError("血条区域全黑")
                    hp = bar_fill_ratio(frame)
                if hp is None:
                    raise RuntimeError("血条读数无效")
                self._capture_errors = 0
                self._publish(hp, box, "前台")
            except Exception as exc:
                self._capture_errors += 1
                if self._capture_errors == 1 or self._capture_errors % 30 == 0:
                    self.log.warning("视觉源等待 HUD：%s", exc)
                self._invalidate(str(exc))
            self._stop.wait(max(0.0, delay - (time.monotonic() - started)))

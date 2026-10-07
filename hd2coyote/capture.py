"""屏幕抓取。

优先级：dxcam（DXGI，最快）> mss（GDI，够用）> Pillow ImageGrab（兜底）。
本程序只读像素，不注入游戏进程、不读写游戏内存 —— 对反作弊是「零接触」。
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np

from .config import Box, CaptureConfig


class CaptureError(RuntimeError):
    pass


class ScreenGrabber:
    def __init__(self, cfg: CaptureConfig, logger: logging.Logger | None = None) -> None:
        self.cfg = cfg
        self.log = logger or logging.getLogger("hd2coyote.capture")
        self._impl = None
        self._backend = ""
        self._sct: Any = None
        self._camera: Any = None
        self._origin = (0, 0)
        self._size = (1920, 1080)
        self._open()

    # ---------------------------------------------------------------- 初始化
    def _open(self) -> None:
        order = ["dxcam", "mss", "pil"] if self.cfg.backend == "auto" else [self.cfg.backend]
        errors: list[str] = []
        for name in order:
            try:
                if name == "dxcam":
                    self._open_dxcam()
                elif name == "mss":
                    self._open_mss()
                elif name == "pil":
                    self._open_pil()
                else:
                    continue
                self._backend = name
                self.log.info("抓屏后端：%s，显示器尺寸 %sx%s", name, *self._size)
                return
            except Exception as exc:
                errors.append(f"{name}: {exc}")
        raise CaptureError("没有可用的抓屏后端：" + "; ".join(errors))

    def _open_mss(self) -> None:
        import mss

        self._sct = mss.mss()
        mon = self._sct.monitors[self.cfg.monitor]
        self._origin = (mon["left"], mon["top"])
        self._size = (mon["width"], mon["height"])
        self._impl = "mss"

    def _open_dxcam(self) -> None:
        import dxcam  # type: ignore

        self._camera = dxcam.create(output_idx=max(0, self.cfg.monitor - 1), output_color="RGB")
        if self._camera is None:
            raise CaptureError("dxcam.create 返回 None")
        self._origin = (0, 0)
        self._size = tuple(self._camera.width_height)  # type: ignore[assignment]
        self._impl = "dxcam"

    def _open_pil(self) -> None:
        from PIL import ImageGrab  # noqa: F401

        self._impl = "pil"
        frame = self.full_frame()
        self._size = (frame.shape[1], frame.shape[0])
        self._origin = (0, 0)

    # ---------------------------------------------------------------- 属性
    @property
    def backend(self) -> str:
        return self._backend

    @property
    def size(self) -> tuple[int, int]:
        return self._size

    @property
    def origin(self) -> tuple[int, int]:
        return self._origin

    # ---------------------------------------------------------------- 抓取
    def grab(self, region: Box | None = None) -> np.ndarray:
        """抓取一块区域，返回 RGB uint8 数组 (H, W, 3)。"""
        box = region or Box(self._origin[0], self._origin[1], self._size[0], self._size[1])
        if box.w <= 0 or box.h <= 0:
            raise CaptureError(f"非法区域：{box}")
        if self._impl == "mss":
            shot = self._sct.grab({"left": box.x, "top": box.y, "width": box.w, "height": box.h})
            return np.frombuffer(shot.rgb, dtype=np.uint8).reshape(shot.height, shot.width, 3)
        if self._impl == "dxcam":
            left = box.x - self._origin[0]
            top = box.y - self._origin[1]
            frame = self._camera.grab(region=(left, top, left + box.w, top + box.h))
            if frame is None:  # 画面未变化时 dxcam 会返回 None
                frame = self._camera.get_latest_frame()
            if frame is None:
                raise CaptureError("dxcam 未返回画面")
            return np.asarray(frame)[:, :, :3]
        if self._impl == "pil":
            from PIL import ImageGrab

            img = ImageGrab.grab(bbox=box.as_tuple(), all_screens=True)
            return np.asarray(img.convert("RGB"))
        raise CaptureError("抓屏后端未初始化")

    def full_frame(self) -> np.ndarray:
        return self.grab(None)

    def close(self) -> None:
        try:
            if self._sct is not None:
                self._sct.close()
        except Exception:
            pass
        try:
            if self._camera is not None:
                self._camera.release()
        except Exception:
            pass
        self._sct = None
        self._camera = None


def is_black_frame(frame: np.ndarray, threshold: float = 0.6) -> bool:
    """判断是否为「受保护内容」黑屏（某些游戏会屏蔽抓屏）。"""
    return frame.size == 0 or float(frame.mean()) < threshold

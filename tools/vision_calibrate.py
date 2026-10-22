"""外部 HUD 视觉源的校准辅助：抓游戏窗口客户区、找血条、保存标记图。

只读屏幕像素；不注入、不打开游戏句柄、不读写游戏内存。

用法（游戏需在任务中且在前台）：
    python tools/vision_calibrate.py
    python tools/vision_calibrate.py --brightness 150 --out build/vision-calibrated.png
    python tools/vision_calibrate.py --box 900,1300,300,10
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hd2coyote.config import AppConfig, Box  # noqa: E402
from hd2coyote.sources.vision import (  # noqa: E402
    _client_rect,
    find_window,
    title_candidates,
)
from hd2coyote.vision import auto_locate_hp_bar, bar_fill_ratio, is_black_frame  # noqa: E402


def grab_client(title: str, backend: str):
    import numpy as np

    hwnd = find_window(title)
    if not hwnd:
        raise RuntimeError("没有找到游戏窗口（标题候选：" + "/".join(title_candidates(title))
                           + "）；请先启动游戏")
    cx, cy, cw, ch = _client_rect(hwnd)
    if cw < 320 or ch < 240:
        raise RuntimeError("游戏窗口已最小化或尺寸过小")
    region = {"left": cx, "top": cy, "width": cw, "height": ch}
    if backend == "pil":
        from PIL import ImageGrab

        return np.asarray(ImageGrab.grab(
            bbox=(cx, cy, cx + cw, cy + ch), all_screens=True).convert("RGB")), region
    import mss

    with mss.mss() as sct:
        shot = sct.grab(region)
    return np.frombuffer(shot.rgb, dtype=np.uint8).reshape(shot.height, shot.width, 3), region


def main() -> int:
    ap = argparse.ArgumentParser(description="校准外部 HUD 视觉源")
    ap.add_argument("--title", default=AppConfig().safety.game_window_title)
    ap.add_argument("--backend", choices=("mss", "pil"), default="mss")
    ap.add_argument("--brightness", type=float, default=165.0)
    ap.add_argument("--out", default=str(ROOT / "build" / "vision-calibrated.png"))
    ap.add_argument("--box", default="", help="已知血条 x,y,w,h（相对游戏窗口客户区）")
    args = ap.parse_args()

    try:
        frame, region = grab_client(args.title, args.backend)
    except Exception as exc:
        print(f"抓屏失败：{type(exc).__name__}: {exc}")
        return 2

    print(f"游戏窗口客户区：{region['left']},{region['top']} {region['width']}x{region['height']}")
    print(f"客户区平均亮度 {frame.mean():.1f}")
    if is_black_frame(frame):
        print("客户区接近全黑：请改用无边框窗口模式，或把游戏切到前台后重试。")
        return 3

    box = None
    if args.box:
        parts = [int(v) for v in args.box.split(",")]
        if len(parts) != 4:
            print("--box 需要 x,y,w,h")
            return 4
        box = Box(*parts)
    else:
        box = auto_locate_hp_bar(frame, brightness=args.brightness)

    if box is None:
        print("没有找到血条。必须在任务中（不是飞船/军械库），且 HUD 未被隐藏。")
        return 1

    ratio = bar_fill_ratio(frame, box)
    print(f"血条：x={box.x} y={box.y} w={box.w} h={box.h}")
    print(f"血量读数：{ratio * 100:.1f}%" if ratio is not None else "血量读数：无效")
    print("把下面这行写进 config.json 的 vision 段即可固定区域：")
    print(f'  "hp_bar": {{"x": {box.x}, "y": {box.y}, "w": {box.w}, "h": {box.h}}}')

    try:
        from PIL import Image, ImageDraw

        img = Image.fromarray(frame.copy())
        ImageDraw.Draw(img).rectangle(
            [box.x, box.y, box.x + box.w, box.y + box.h], outline=(0, 255, 64), width=3)
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        img.save(args.out)
        print(f"标记图：{args.out}")
    except Exception as exc:
        print(f"（标记图保存失败：{exc}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

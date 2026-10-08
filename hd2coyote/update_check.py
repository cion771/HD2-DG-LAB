"""检查更新：只查 GitHub Releases，**只提示 + 给链接**，绝不自动下载安装。

为什么不学 DG-Lab-Punishment 的自动更新：它的 update.py 会下载 zip、杀掉主程序、
解压覆盖安装目录 —— 版本/网络/杀软任何一环出岔子，用户就得到半坏的程序，
而这个程序手上握着电极。所以这里明确只做两件事：查版本、告诉你去哪下载。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any, Callable

from . import __version__

DEFAULT_REPO = "cion771/hd2-coyote"
API_URL = "https://api.github.com/repos/{repo}/releases/latest"
USER_AGENT = f"hd2-coyote/{__version__}"


def parse_version(text: Any) -> tuple[int, ...]:
    """从 'v0.5.0' / 'hd2-coyote 0.5.0-beta' 里抠出版本号元组。"""
    match = re.search(r"(\d+(?:\.\d+)*)", str(text or ""))
    if not match:
        return ()
    return tuple(int(x) for x in match.group(1).split("."))[:4]


def compare_versions(a: Any, b: Any) -> int:
    """a>b 返回 1，a<b 返回 -1，相等返回 0（缺失的段按 0 补齐）。"""
    va, vb = parse_version(a), parse_version(b)
    size = max(len(va), len(vb))
    va = va + (0,) * (size - len(va))
    vb = vb + (0,) * (size - len(vb))
    return (va > vb) - (va < vb)


def fetch_latest(repo: str = DEFAULT_REPO, timeout: float = 6.0,
                 opener: Callable[..., Any] | None = None) -> dict[str, Any]:
    """取最新 Release 的原始 JSON（可能抛 HTTPError / URLError / ValueError）。"""
    request = urllib.request.Request(
        API_URL.format(repo=repo),
        headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"},
    )
    open_fn = opener or urllib.request.urlopen
    with open_fn(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8", errors="replace"))
    if not isinstance(data, dict):
        raise ValueError("GitHub 返回的不是一个 Release 对象")
    return data


def summarize(data: dict[str, Any]) -> dict[str, Any]:
    assets: list[dict[str, Any]] = []
    for item in data.get("assets") or []:
        if isinstance(item, dict):
            assets.append({
                "name": str(item.get("name") or ""),
                "size": int(item.get("size") or 0),
                "url": str(item.get("browser_download_url") or ""),
                "downloads": int(item.get("download_count") or 0),
            })
    tag = str(data.get("tag_name") or data.get("name") or "")
    return {
        "tag": tag,
        "name": str(data.get("name") or tag),
        "url": str(data.get("html_url") or ""),
        "published_at": str(data.get("published_at") or ""),
        "prerelease": bool(data.get("prerelease")),
        "assets": assets,
        "body": str(data.get("body") or "")[:4000],
    }


def pick_zip(assets: list[dict[str, Any]]) -> dict[str, Any] | None:
    """优先挑 addon 的 zip 包（HD2-Coyote-Bridge-*.zip），否则任意 zip。"""
    zips = [a for a in assets if str(a.get("name", "")).lower().endswith(".zip")]
    for asset in zips:
        if "bridge" in str(asset["name"]).lower():
            return asset
    return zips[0] if zips else None


def check(repo: str = DEFAULT_REPO, current: str | None = None, timeout: float = 6.0,
          opener: Callable[..., Any] | None = None) -> dict[str, Any]:
    """检查更新。**永远不抛异常**：失败也返回带 error 的字典（界面照样能显示）。"""
    current = current or __version__
    try:
        data = fetch_latest(repo, timeout=timeout, opener=opener)
    except urllib.error.HTTPError as exc:
        message = "仓库还没有发布任何 Release" if exc.code == 404 else f"GitHub 返回 HTTP {exc.code}"
        return {"ok": False, "current": current, "repo": repo, "error": message}
    except urllib.error.URLError as exc:
        return {"ok": False, "current": current, "repo": repo,
                "error": f"连不上 GitHub：{getattr(exc, 'reason', exc)}"}
    except Exception as exc:  # 超时、DNS、坏 JSON……都只是「查不到」
        return {"ok": False, "current": current, "repo": repo,
                "error": f"{type(exc).__name__}: {exc}"}

    latest = summarize(data)
    tag = latest["tag"] or latest["name"]
    diff = compare_versions(tag, current)
    return {
        "ok": True,
        "current": current,
        "repo": repo,
        "latest": latest,
        "update_available": diff > 0,
        "same": diff == 0,
        "older": diff < 0,
        "zip": pick_zip(latest["assets"]),
        "release_url": latest["url"] or f"https://github.com/{repo}/releases",
    }


def format_result(result: dict[str, Any]) -> str:
    """给人看的多行文本（CLI 与日志共用）。"""
    current = result.get("current") or __version__
    lines = [f"当前版本：{current}"]
    if not result.get("ok"):
        lines.append(f"检查更新失败：{result.get('error') or '未知原因'}")
        lines.append("（不影响使用：离线时忽略这条即可，或到 "
                     f"https://github.com/{result.get('repo') or DEFAULT_REPO}/releases 手动看）")
        return "\n".join(lines)

    latest = result.get("latest") or {}
    published = str(latest.get("published_at") or "")[:10]
    lines.append(f"最新版本：{latest.get('tag') or latest.get('name')}"
                 + (f"（{published}）" if published else ""))
    if result.get("update_available"):
        lines.append(f"有新版本可用：{latest.get('tag')}")
        zip_asset = result.get("zip") or {}
        if zip_asset:
            size_kb = (zip_asset.get("size") or 0) / 1024.0
            lines.append(f"  下载：{zip_asset.get('url')}（{zip_asset.get('name')}，{size_kb:.1f} KB）")
        lines.append(f"  Release 说明：{result.get('release_url')}")
        lines.append("  提示：更新只替换程序文件，不影响你的 config.json；升级前先看 SAFETY.md")
    elif result.get("older"):
        lines.append("你本地版本比线上还新（自己改过的版本号？）")
    else:
        lines.append("已是最新版本。")
    return "\n".join(lines)

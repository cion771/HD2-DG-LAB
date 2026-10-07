"""部署自检：看游戏 data 目录里到底有没有你的桥，它在哪个 patch 里。

用法：
    python tools/inspect_game_patches.py
    python tools/inspect_game_patches.py --data "E:\\SteamLibrary\\steamapps\\common\\Helldivers 2\\data"
    python tools/inspect_game_patches.py --data <目录> --expect mods/hd2coyote/hd2_coyote_bridge

它做三件事：
  1. 解析每个 9ba626afa44a3aa3.patch_N（顺便验证我们的解析器能吃真实归档）；
  2. 按资源名哈希找目标资源，报出它在哪个 patch、大小、是否明文（首行 -- HD2-Addon）；
  3. 和本地最新源码比字节，告诉你部署的是不是旧版本。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from hd2_patch import LUA_RESOURCE_TYPE, PatchArchive, resource_hash  # noqa: E402

CARRIER = "9ba626afa44a3aa3"
DEFAULT_DATA = r"E:\SteamLibrary\steamapps\common\Helldivers 2\data"
DEFAULT_RESOURCE = "mods/hd2coyote/hd2_coyote_bridge"
DEFAULT_SOURCE = Path(__file__).resolve().parents[1] / "lua" / "hd2_coyote_bridge.lua"


def find_patches(data: Path) -> list[Path]:
    out = [p for p in data.glob(f"{CARRIER}.patch_*") if p.suffix != ".stream"
           and not p.name.endswith(".gpu_resources")]
    return sorted(out, key=lambda p: int(p.name.rsplit("_", 1)[1]) if p.name.rsplit("_", 1)[1].isdigit() else -1)


def main() -> int:
    ap = argparse.ArgumentParser(description="检查游戏内的 patch 部署情况")
    ap.add_argument("--data", default=DEFAULT_DATA)
    ap.add_argument("--expect", default=DEFAULT_RESOURCE)
    ap.add_argument("--source", default=str(DEFAULT_SOURCE))
    args = ap.parse_args()

    data = Path(args.data)
    if not data.is_dir():
        print(f"找不到 data 目录：{data}（用 --data 指定游戏 data 目录）")
        return 2

    patches = find_patches(data)
    print(f"data 目录：{data}")
    print(f"patch 归档：{len(patches)} 个（{patches[0].name if patches else '-'} … "
          f"{patches[-1].name if patches else '-'}）")

    want = resource_hash(args.expect)
    print(f"目标资源：{args.expect}")
    print(f"  名字哈希：0x{want:016X}")

    total_resources = 0
    parse_failed: list[str] = []
    found: list[tuple[int, PatchArchive, object]] = []
    other_lua: list[tuple[int, str]] = []

    for path in patches:
        index = int(path.name.rsplit("_", 1)[1]) if path.name.rsplit("_", 1)[1].isdigit() else -1
        try:
            archive = PatchArchive.from_bytes(path.read_bytes())
        except Exception as exc:
            parse_failed.append(f"{path.name}: {exc}")
            continue
        total_resources += len(archive.resources)
        for res in archive.resources:
            if res.name_hash == want:
                found.append((index, archive, res))
            elif res.type_hash == LUA_RESOURCE_TYPE:
                head = res.data.split(b"\n", 1)[0][:120].decode("utf-8", "replace")
                if head.startswith("-- HD2-Addon:"):
                    other_lua.append((index, head))

    print(f"解析成功：{len(patches) - len(parse_failed)}/{len(patches)}，"
          f"共 {total_resources} 个资源")
    for line in parse_failed:
        print(f"  解析失败 {line}")

    if other_lua:
        print(f"\n已部署的 Lua addon（{len(other_lua)} 个，按 patch 号）：")
        for index, head in sorted(other_lua):
            print(f"  patch_{index:<3} {head}")

    if not found:
        print(f"\n❌ 没有部署：{args.expect}")
        print("   → 把 build/HD2-Coyote-Bridge-<版本>.zip 用 Arsenal / HD2 Mod Manager 导入、")
        print("     启用、Deploy，然后重启游戏。菜单分类要重启后才出现。")
        print("     （版本号见仓库根的 VERSION 文件；也可用 tools/build_addon.py 手工部署）")
        return 1

    source = Path(args.source)
    current = source.read_bytes() if source.exists() else b""
    print(f"\n✅ 已部署 {args.expect}")
    for index, _archive, res in sorted(found):
        same = "与本地源码一致" if current and res.data == current else "**与本地源码不同（旧版本？）**"
        head = res.data.split(b"\n", 1)[0].decode("utf-8", "replace")
        print(f"   patch_{index}  {len(res.data)} 字节  {same}")
        print(f"     首行：{head}")
    if len(found) > 1:
        print("   注意：有多个副本；游戏只加载编号最高的那个（加载器日志的 copies: 行会说明）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

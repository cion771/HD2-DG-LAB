"""把 lua/hd2_coyote_bridge.lua 打包成 mod 管理器能导入的 ZIP。

产物结构（以 HD2 Arsenal 为准，它按目录决定部署什么）：

    <DisplayName>-<version>.zip
      ├─ manifest.json                       # 见 wiki 的 manifest v1
      └─ data/9ba626afa44a3aa3.patch_N       # 补丁放 data/ 下 —— 与 Bingus Shared Loader
         9ba626afa44a3aa3.patch_N.stream       等官方分发包完全一致（已实测可部署）
         9ba626afa44a3aa3.patch_N.gpu_resources

**为什么不是 `Addon/`**：加载器文档提到 addon 包里有 `Addon/`，但实测（用户机器上的
HD2 Arsenal 0.36.2）把补丁放进 `Addon/` 会被当成"可选分组"，导入成功却**一个文件都不部署**
（deployment_snapshot 里 patchFiles 为空）。Arsenal 认的是 `data/`、根目录或 `options/<名>/`。
用 `--layout` 可以切到其它结构：

    --layout data   （默认）data/<patch> + 两个 0 字节 sidecar —— Arsenal 实测可用
    --layout root   直接放 ZIP 根目录（部分管理器认这个）
    --layout addon  Addon/<patch>（HD2 Mod Manager 风格；Arsenal 下不会被部署）

用法：
    python tools/build_addon.py                                  # 默认参数直接出包
    python tools/build_addon.py --display-name "HD2 Coyote Bridge" --patch-number 3
    python tools/build_addon.py --layout addon                    # 换成 Addon/ 结构
    python tools/build_addon.py --lua-mods-dir "…/Helldivers 2/data"   # 直接部署（可选）

注意两条社区踩过的坑：
  * manifest 的 Name 会被管理器当文件夹名 —— 必须纯 ASCII 且不含 \\ / : * ? " < > |
  * patch 编号由管理器重排，手工装才需要自己挑一个没占用的号
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).parent))

from hd2_patch import PatchArchive, Resource, build_lua_patch  # noqa: E402

CARRIER = "9ba626afa44a3aa3"          # 游戏读取 patch 归档时用的载体名
DEFAULT_RESOURCE = "mods/hd2coyote/hd2_coyote_bridge"
DEFAULT_GUID = "8f3d1b2a-6c47-4e0a-9b6d-2f5f0c9a7d31"  # 固定 GUID：同一个 mod 的升级要复用
VERSION_FILE = ROOT / "VERSION"
ILLEGAL = set('\\/:*?"<>|')
HEADER_RE = re.compile(r"^--\s*HD2-Addon:\s*(\S+)\s*$")
VERSION_RE = re.compile(r"^local\s+VERSION\s*=\s*'([^']+)'", re.MULTILINE)


def read_version() -> str:
    """版本号的唯一来源：仓库根目录的 VERSION 文件。"""
    if VERSION_FILE.exists():
        text = VERSION_FILE.read_text(encoding="utf-8").strip()
        if text:
            return text
    raise SystemExit(f"读不到版本号：{VERSION_FILE} 不存在或为空")


def check_lua_version(path: Path, version: str) -> str:
    """Lua 里的 VERSION 必须和 VERSION 文件一致 —— 否则 ZIPP 里的桥和包名会对不上。"""
    text = path.read_text(encoding="utf-8", errors="replace")
    m = VERSION_RE.search(text)
    if not m:
        raise ValueError(f"{path.name} 里找不到 local VERSION = '...'")
    if m.group(1) != version:
        raise ValueError(f"版本号不一致：VERSION 文件是 {version}，"
                         f"{path.name} 里写的是 {m.group(1)}（改一处，两处一起改）")
    return m.group(1)


def check_display_name(name: str) -> str:
    bad = ILLEGAL & set(name)
    if bad:
        raise ValueError(f"manifest Name 含非法文件名字符 {sorted(bad)}（管理器会导入失败）")
    if not name.isascii():
        raise ValueError("manifest Name 必须是纯 ASCII（实测非 ASCII 会导致导入失败）")
    if not name.strip():
        raise ValueError("manifest Name 不能为空")
    return name


def check_lua_source(path: Path, expect_resource: str) -> tuple[str, str]:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ValueError("Lua 文件有 BOM —— 加载器只认明文且首行必须是 -- HD2-Addon:")
    first = raw.split(b"\n", 1)[0].decode("utf-8", errors="replace").strip()
    m = HEADER_RE.match(first)
    if not m:
        raise ValueError(f"第一行不是 -- HD2-Addon: 头：{first[:80]!r}")
    if len(first.encode("utf-8")) > 256:
        raise ValueError("-- HD2-Addon 头超过 256 字节")
    declared = m.group(1)
    if declared != expect_resource:
        raise ValueError(f"声明的资源名({declared})与要打包的({expect_resource})不一致")
    return declared, raw.decode("utf-8")


def next_free_patch_number(data_dir: Path) -> int:
    """挑一个没被占用的 patch 编号（绝不动别人的补丁）。"""
    used = []
    for p in data_dir.glob(f"{CARRIER}.patch_*"):
        suffix = p.name.rsplit("_", 1)[1]
        if suffix.isdigit():
            used.append(int(suffix))
    return (max(used) + 1) if used else 0


def build(args: argparse.Namespace) -> Path:
    lua_path = Path(args.lua)
    if not lua_path.is_absolute():
        lua_path = ROOT / lua_path
    resource, _source = check_lua_source(lua_path, args.resource)
    check_lua_version(lua_path, args.version)
    display = check_display_name(args.display_name)

    patch_bytes = PatchArchive([
        Resource(name=resource, data=lua_path.read_bytes())
    ]).to_bytes()

    # 编号：默认 auto —— 有 --lua-mods-dir 就取下一个空闲号，否则用 0
    number = args.patch_number
    if str(number).lower() == "auto":
        number = next_free_patch_number(Path(args.lua_mods_dir)) if args.lua_mods_dir else 0
    number = int(number)
    args.patch_number = number

    out_dir = Path(args.output)
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    patch_name = f"{CARRIER}.patch_{args.patch_number}"
    layout = getattr(args, "layout", "data")
    if layout == "data":
        include_dir = "data"
        members = [
            (f"data/{patch_name}", patch_bytes),
            (f"data/{patch_name}.stream", b""),          # 0 字节 sidecar，与管理器产出的一致
            (f"data/{patch_name}.gpu_resources", b""),
        ]
    elif layout == "addon":
        include_dir = "Addon"
        members = [(f"Addon/{patch_name}", patch_bytes)]
    else:
        include_dir = "."
        members = [(patch_name, patch_bytes)]

    manifest = {
        "Version": 1,
        "Guid": args.guid,
        "Name": display,
        "Description": "Reads your Helldiver's health / limb damage / death in-game (read-only) "
                       "and forwards it to hd2-coyote over UDP. Requires Bingus Shared Loader "
                       "v15+ (API 1). Only this one mod is needed.",
        # ★ 关键：Arsenal/HD2MM 靠 Options[].Include 决定"这个 mod 要部署哪些目录"。
        #   没有 Options 时，Arsenal 日志会写 "Deployed ... successfully - 0 files copied"，
        #   游戏里一个文件都不会出现（实测踩过：mod 列表里有、data 里没有）。
        "Options": [
            {
                "Name": display,
                "Description": "Install the in-game read-only bridge.",
                "Include": [include_dir],
            }
        ],
    }
    icon = ROOT / "assets" / "icon.png"
    if icon.exists():
        manifest["IconPath"] = "icon.png"
        manifest["Options"][0]["Image"] = "icon.png"

    zip_path = out_dir / f"{display.replace(' ', '-')}-{args.version}.zip"

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        for arcname, content in members:
            zf.writestr(arcname, content)
        icon = ROOT / "assets" / "icon.png"
        if icon.exists():
            zf.write(icon, "icon.png")

    patch_file = out_dir / f"{CARRIER}.patch_{args.patch_number}"
    patch_file.write_bytes(patch_bytes)
    patch_file.with_suffix(patch_file.suffix + ".stream").write_bytes(b"")
    patch_file.with_suffix(patch_file.suffix + ".gpu_resources").write_bytes(b"")

    print(f"已生成：{zip_path}")
    print(f"  manifest.json   Name={display!r} Guid={args.guid}")
    print(f"  布局            {layout}：{', '.join(a for a, _ in members)}")
    print(f"  资源            {resource}（{len(patch_bytes)} 字节）")
    print(f"  裸归档（手工安装用）：{patch_file}（+ 两个 0 字节 sidecar）")
    print(f"\n安装：在 Arsenal / HD2 Mod Manager 里导入上面的 ZIP，启用后 Deploy。")
    print(f"验证：`python tools\\inspect_game_patches.py` 应显示 ✅ 已部署；")
    print(f"      游戏启动后加载器日志里应出现 {resource}: loaded")
    return zip_path


def deploy(args: argparse.Namespace, zip_path: Path) -> None:
    """可选：直接把 patch 归档拷进游戏 data 目录（手工安装路线）。

    安全第一：只写一个编号没被占用的新文件；宁可用下一个空号，也不覆盖别人的补丁。
    """
    data_dir = Path(args.lua_mods_dir)
    if not data_dir.is_dir():
        raise SystemExit(f"data 目录不存在：{data_dir}")
    src = Path(args.output) / f"{CARRIER}.patch_{args.patch_number}"
    if not src.is_absolute():
        src = ROOT / src
    target = data_dir / f"{CARRIER}.patch_{args.patch_number}"
    if target.exists() and not args.force:
        raise SystemExit(f"{target.name} 已存在（可能是别的 mod 的补丁）。"
                         f"用 --patch-number auto 自动挑空号；确认要覆盖再加 --force")
    target.write_bytes(src.read_bytes())
    print(f"已部署到 {target}")
    print("重启游戏后生效；卸载就是删掉这个文件。")
    print("注意：用 mod 管理器的 Purge 可能会一起清掉它 —— 也可以改用管理器导入 ZIP。")


def main() -> None:
    ap = argparse.ArgumentParser(description="打包 hd2-coyote 游戏内 Lua 桥")
    ap.add_argument("--lua", default="lua/hd2_coyote_bridge.lua")
    ap.add_argument("--resource", default=DEFAULT_RESOURCE, help="Lua 资源名，必须与首行头一致")
    ap.add_argument("--display-name", default="HD2 Coyote Bridge")
    ap.add_argument("--guid", default=DEFAULT_GUID)
    ap.add_argument("--version", default=None,
                    help="默认读仓库根的 VERSION 文件；只在特殊情况下覆盖")
    ap.add_argument("--patch-number", default="auto",
                    help="'auto'（默认，部署时挑空闲编号）或具体数字")
    ap.add_argument("--output", default="build")
    ap.add_argument("--layout", default="data", choices=["data", "root", "addon"],
                    help="ZIP 内补丁的摆放：data（Arsenal 实测可用）/ root / addon")
    ap.add_argument("--lua-mods-dir", default="", help="可选：直接部署到游戏 data 目录")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    if not args.version:
        args.version = read_version()
    print(f"hd2-coyote bridge v{args.version}（来自 VERSION 文件）")
    try:
        zip_path = build(args)
    except ValueError as exc:
        raise SystemExit(f"打包失败：{exc}")
    if args.lua_mods_dir:
        deploy(args, zip_path)


if __name__ == "__main__":
    main()

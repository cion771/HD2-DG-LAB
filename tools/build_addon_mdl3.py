r"""把 addon/ 打包成 MDL3 版 hd2-coyote 的两种发布形态。

    Arsenal zip（默认）
      <Display>-<version>.zip
        |-- manifest.json                      # manifest v1；Options[].Include = ["data"]
        |-- data/9ba626afa44a3aa3.patch_3      # 所有 Lua 源码装进一个 .patch_N
        |-- data/9ba626afa44a3aa3.patch_3.stream
        +-- data/9ba626afa44a3aa3.patch_3.gpu_resources     # 两个 0 字节 sidecar

    本地 mod zip（--local）
      <Display>-<version>-local.zip
        +-- hd2coyote/...                      # 解压后整个文件夹丢进
                                               # %LOCALAPPDATA%\MDL\Helldivers2\Mods\

资源名（entry 与模块都按这个约定，改目录就得改这里）：

    entry.lua                    -> mods/hd2coyote/hd2_coyote     （首行 -- HD2-Addon: 声明）
    core/log.lua                 -> mods/hd2coyote/hd2_coyote/core_log
    sources/direct.lua           -> mods/hd2coyote/hd2_coyote/sources_direct
    state/sampler.lua            -> mods/hd2coyote/hd2_coyote/state_sampler
    profiles/steam_25480438.lua  -> mods/hd2coyote/hd2_coyote/profiles_steam_25480438
    bridge.lua                   -> mods/hd2coyote/hd2_coyote/bridge
    mod.lua                      -> 只进本地 zip（MDL3 本地入口，不是补丁资源）

三道闸门（任何一条不过都直接退出，不会留下半成品 zip）：

  * entry.lua 首行必须是 -- HD2-Addon: mods/hd2coyote/hd2_coyote，且与资源名一致；
  * 模块不许自带 -- HD2-Addon: 首行（那是 entry 专用），也不许有 BOM；
  * 产出的 .patch_N 从 zip 里读回来用 hd2_patch 反解再序列化，必须逐字节一致。

缺失的文件不算失败：addon/ 只写了一半时照常出包，缺哪些会在输出里列出来。
补丁容器逻辑一律 import tools/hd2_patch.py，manifest / 首行 / 版本闸门复用
tools/build_addon.py —— 这里不复制第二份。

用法：
    python tools/build_addon_mdl3.py                 # Arsenal zip + 裸归档（patch_3）
    python tools/build_addon_mdl3.py --local         # 只出本地 mod zip
    python tools/build_addon_mdl3.py --both          # 两种都出
    python tools/build_addon_mdl3.py --addon-dir _w/mdl3/packtest/addon --output _w/mdl3/dist
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).parent))

import build_addon  # noqa: E402  （复用 manifest/版本/首行闸门）
from hd2_patch import PatchArchive, Resource, resource_hash  # noqa: E402

CARRIER = "9ba626afa44a3aa3"          # 游戏读取 patch 归档时用的载体名
ENTRY_RESOURCE = "mods/hd2coyote/hd2_coyote"
DEFAULT_DISPLAY = "HD2 Coyote"
# 固定 GUID：同一个 mod 的升级要复用，别每次 build 都换
DEFAULT_GUID = "c1f4a7e2-9b3d-4a68-8e05-2d7c6b91f4a3"
DEFAULT_PATCH_NUMBER = 3
MODULE_PREFIX = ENTRY_RESOURCE + "/"
LOCAL_FOLDER = ENTRY_RESOURCE.split("/")[1]   # mods/hd2coyote/... -> hd2coyote
ICON = ROOT / "assets" / "icon.png"
DEFAULT_ADDON_DIR = ROOT / "addon"
DEFAULT_OUTPUT = ROOT / "build" / "mdl3"
SKIP_DIRS = {"__pycache__"}

# 定稿的目录约定：缺了就跳过，并在输出里点名（不报错、不崩）
EXPECTED = (
    "mod.lua",
    "entry.lua",
    "core/log.lua",
    "core/json.lua",
    "core/compat.lua",
    "core/mem.lua",
    "core/net.lua",
    "core/config.lua",
    "core/clock.lua",
    "sources/runtime.lua",
    "sources/direct.lua",
    "sources/mock.lua",
    "state/sampler.lua",
    "profiles/steam_25480438.lua",
    "bridge.lua",
)


# --------------------------------------------------------------------- 发现
@dataclass
class Discovery:
    addon_dir: Path
    entry: Path | None
    files: list[tuple[str, Path]] = field(default_factory=list)
    modules: list[tuple[str, str, Path]] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    extras: list[str] = field(default_factory=list)
    non_lua: list[str] = field(default_factory=list)


def module_resource(rel: str) -> str:
    """addon 内相对路径 -> 资源名：core/log.lua -> mods/hd2coyote/hd2_coyote/core_log"""
    stem = rel[:-4] if rel.endswith(".lua") else rel
    return MODULE_PREFIX + stem.replace("/", "_")


def collect(addon_dir: Path) -> Discovery:
    """只读 addon/ 下真实存在的文件；缺文件不是错误。"""
    if not addon_dir.is_dir():
        raise ValueError(f"addon 目录不存在：{addon_dir}（用 --addon-dir 指到桩目录）")

    files: list[tuple[str, Path]] = []
    for path in sorted(addon_dir.rglob("*")):
        if path.is_dir():
            continue
        rel = path.relative_to(addon_dir).as_posix()
        if rel.startswith(".") or SKIP_DIRS.intersection(path.parts):
            continue
        files.append((rel, path))

    present = {rel for rel, _ in files}
    found = Discovery(addon_dir=addon_dir, entry=dict(files).get("entry.lua"), files=files)
    found.missing = [rel for rel in EXPECTED if rel not in present]
    found.extras = sorted(present - set(EXPECTED))

    for rel, path in files:
        if rel in ("entry.lua", "mod.lua"):
            continue  # entry 走 ENTRY_RESOURCE；mod.lua 只属于本地 zip
        if not rel.endswith(".lua"):
            found.non_lua.append(rel)
            continue
        found.modules.append((rel, module_resource(rel), path))
    return found


# --------------------------------------------------------------------- 闸门
def check_module_source(path: Path, rel: str) -> None:
    """模块只能是纯 UTF-8 Lua，且不许抢 entry 的 -- HD2-Addon: 首行。"""
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ValueError(f"{rel} 带 UTF-8 BOM —— 加载器只吃明文 Lua")
    if raw.startswith(b"\x1bLua"):
        raise ValueError(f"{rel} 是编译后的字节码 —— 加载器只吃源码")
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{rel} 不是 UTF-8：{exc}") from exc
    first = raw.split(b"\n", 1)[0].decode("utf-8", errors="replace").strip()
    if build_addon.HEADER_RE.match(first):
        raise ValueError(f"{rel} 自带 -- HD2-Addon: 首行 —— 只有 entry.lua 能有这一行，"
                         f"模块由 entry 的 require 加载")


def check_gates(found: Discovery) -> None:
    if found.entry is not None:
        build_addon.check_lua_source(found.entry, ENTRY_RESOURCE)
    for rel, _name, path in found.modules:
        check_module_source(path, rel)


def resolve_version(version_file: Path | None) -> str:
    """版本唯一来源：仓库根 VERSION（--version-file 只给测试/预发布用）。"""
    if version_file is None:
        return build_addon.read_version()
    if not version_file.is_file():
        raise ValueError(f"VERSION 文件不存在：{version_file}")
    text = version_file.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"VERSION 文件为空：{version_file}")
    return text


def inspect_mod_lua_version(found: Discovery, version: str) -> tuple[bool, str]:
    """addon/mod.lua 的 local VERSION 必须和 VERSION 文件一致；还没写就只警告。

    返回 (是否通过, 说明)。先把清单和说明打完再退出，出错时也能看到现场。
    """
    mod = found.addon_dir / "mod.lua"
    if not mod.exists():
        return True, f"mod.lua 缺失，跳过版本一致性校验（VERSION={version}）"
    text = mod.read_text(encoding="utf-8", errors="replace")
    match = build_addon.VERSION_RE.search(text)
    if not match:
        return True, f"mod.lua 里还没写 local VERSION = '...'，跳过一致性校验（VERSION={version}）"
    if match.group(1) != version:
        return False, (f"版本号不一致：VERSION 文件是 {version}，addon/mod.lua 里写的是 "
                       f"{match.group(1)}（改一处，两处一起改）")
    return True, f"VERSION 文件与 addon/mod.lua 一致：{version}"


# --------------------------------------------------------------------- 构建
def make_manifest(display: str, guid: str) -> dict:
    manifest = {
        "Version": 1,
        "Guid": guid,
        "Name": display,
        "Description": "HD2 Coyote for MDL3: reads the local Helldiver's health, limb damage "
                       "and death in-game (read-only) and feeds hd2-coyote over UDP. Requires "
                       "MDL3 (Mod Dynamic Loader 3) and its HD2 Data Hub mod. No BSL needed.",
        # Arsenal/HD2MM 靠 Options[].Include 决定部署哪些目录；没有它就会
        # "Deployed successfully - 0 files copied"（实测过的坑）
        "Options": [
            {
                "Name": display,
                "Description": "Deploy the MDL3 script addon (data/).",
                "Include": ["data"],
            }
        ],
    }
    if ICON.is_file():
        manifest["IconPath"] = "icon.png"
        manifest["Options"][0]["Image"] = "icon.png"
    return manifest


def build_arsenal(found: Discovery, version: str, args: argparse.Namespace,
                  out_dir: Path) -> Path:
    if args.patch_number < 0:
        raise ValueError(f"patch 编号不能为负：{args.patch_number}")
    display = build_addon.check_display_name(args.display_name)
    resources = []
    if found.entry is not None:
        resources.append(Resource(name=ENTRY_RESOURCE, data=found.entry.read_bytes()))
    for _rel, name, path in found.modules:
        resources.append(Resource(name=name, data=path.read_bytes()))
    if not resources:
        raise ValueError("addon/ 里一个可打包的 .lua 都没有（至少要 entry.lua）")
    expected = {res.name: res.data for res in resources}

    patch_bytes = PatchArchive(resources).to_bytes()
    patch_name = f"{CARRIER}.patch_{args.patch_number}"
    manifest = make_manifest(display, args.guid)

    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"{display.replace(' ', '-')}-{version}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        zf.writestr(f"data/{patch_name}", patch_bytes)
        zf.writestr(f"data/{patch_name}.stream", b"")
        zf.writestr(f"data/{patch_name}.gpu_resources", b"")
        if ICON.is_file():
            zf.write(ICON, "icon.png")

    # 手工安装路线的那份裸归档（管理器导入时会自己挑号，手工装才用得上）
    bare = out_dir / patch_name
    bare.write_bytes(patch_bytes)
    bare.with_suffix(bare.suffix + ".stream").write_bytes(b"")
    bare.with_suffix(bare.suffix + ".gpu_resources").write_bytes(b"")

    try:
        report_arsenal_verification(zip_path, patch_name, expected)
    except ValueError:
        # 校验不过就把这一轮写出去的东西全撤掉，别留半成品
        zip_path.unlink(missing_ok=True)
        for path in (bare, bare.with_suffix(bare.suffix + ".stream"),
                     bare.with_suffix(bare.suffix + ".gpu_resources")):
            path.unlink(missing_ok=True)
        raise
    return zip_path


def verify_arsenal(zip_path: Path, patch_name: str, expected: dict[str, bytes]) -> tuple[int, int]:
    """把 zip 里的 .patch_N 用 hd2_patch 反解回来，重写后必须逐字节一致。"""
    member = f"data/{patch_name}"
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
        for needed in ("manifest.json", member, member + ".stream", member + ".gpu_resources"):
            if needed not in names:
                raise ValueError(f"zip 里缺 {needed}")
        if zf.read(member + ".stream") != b"" or zf.read(member + ".gpu_resources") != b"":
            raise ValueError("sidecar 必须是 0 字节（与官方分发包一致）")
        raw = zf.read(member)
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
    options = manifest.get("Options") or []
    if not options or "data" not in options[0].get("Include", []):
        raise ValueError("manifest 的 Options[].Include 不含 data —— Arsenal 会部署 0 个文件")

    archive = PatchArchive.from_bytes(raw)
    again = archive.to_bytes()
    if again != raw:
        raise ValueError(f"round-trip 不一致：读回来 {len(raw)} 字节，重写是 {len(again)} 字节")
    by_hash = {res.name_hash: res.data for res in archive.resources}
    if len(by_hash) != len(expected):
        raise ValueError(f"round-trip 资源数不对：{len(by_hash)} != {len(expected)}")
    for name, data in expected.items():
        if resource_hash(name) not in by_hash:
            raise ValueError(f"round-trip 找不到资源：{name}")
        if by_hash[resource_hash(name)] != data:
            raise ValueError(f"round-trip 资源内容不一致：{name}")
    return len(raw), len(expected)


def report_arsenal_verification(zip_path: Path, patch_name: str,
                                expected: dict[str, bytes]) -> None:
    size, count = verify_arsenal(zip_path, patch_name, expected)
    bare = zip_path.parent / patch_name
    with zipfile.ZipFile(zip_path) as zf:
        in_zip = zf.read(f"data/{patch_name}")
    if not bare.is_file():
        raise ValueError(f"裸归档没写出来：{bare}")
    if bare.read_bytes() != in_zip:
        raise ValueError("裸归档与 zip 内的 .patch_N 不一致")
    print(f"  round-trip      {patch_name}：反解 {count} 个资源，重写后逐字节一致（{size} 字节）")
    print(f"  裸归档           {bare.name}（+ 两个 0 字节 sidecar）")


def verify_local_zip(zip_path: Path, contents: dict[str, bytes], prefix: str) -> int:
    """本地 zip 里每个文件都必须在 hd2coyote/ 下，且与源码逐字节一致。"""
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
        for rel, data in contents.items():
            if prefix + rel not in names:
                raise ValueError(f"本地 zip 缺 {prefix + rel}")
            if zf.read(prefix + rel) != data:
                raise ValueError(f"本地 zip 内容被改动：{prefix + rel}")
        strays = [name for name in names if not name.startswith(prefix)]
        if strays:
            raise ValueError(f"本地 zip 里有不在 {prefix} 下的文件：{strays}")
    return len(contents)


def build_local(found: Discovery, version: str, args: argparse.Namespace,
                out_dir: Path) -> Path:
    display = build_addon.check_display_name(args.display_name)
    if not found.files:
        raise ValueError("addon/ 是空的，本地 zip 没东西可打")

    contents = {rel: path.read_bytes() for rel, path in found.files}
    prefix = LOCAL_FOLDER + "/"
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"{display.replace(' ', '-')}-{version}-local.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in sorted(contents):
            zf.writestr(prefix + rel, contents[rel])
        if ICON.is_file() and (prefix + "icon.png") not in contents:
            zf.write(ICON, prefix + "icon.png")

    try:
        verify_local_zip(zip_path, contents, prefix)
    except ValueError:
        zip_path.unlink(missing_ok=True)
        raise
    print(f"  round-trip      {len(contents)} 个文件在 {prefix} 下逐字节一致")
    if "mod.lua" not in contents:
        print(f"  警告           本地 zip 里没有 mod.lua —— MDL3 不会把它当成 mod")
    return zip_path


# --------------------------------------------------------------------- 输出
def report(found: Discovery, version_line: str) -> None:
    addon_dir = found.addon_dir
    print(f"addon 目录：{addon_dir}")
    print(f"版本      ：{version_line}")
    if found.entry is not None:
        print(f"entry     ：{ENTRY_RESOURCE}  <- entry.lua（{found.entry.stat().st_size} 字节）")
    else:
        print("entry     ：缺失 —— 包里没有可加载入口")
    print(f"模块      ：{len(found.modules)} 个")
    for rel, name, path in found.modules:
        print(f"    {name:<52} <- {rel}（{path.stat().st_size} 字节）")
    if found.missing:
        print(f"缺失      ：{len(found.missing)} 个（已跳过，不影响出包）")
        for rel in found.missing:
            print(f"    - {rel}")
    if found.extras:
        print(f"约定外文件：{len(found.extras)} 个（照样打包，确认一下是不是漏进了仓库）")
        for rel in found.extras:
            print(f"    ? {rel}")
    if found.non_lua:
        print(f"非 .lua   ：{len(found.non_lua)} 个（只进本地 zip，补丁里没有资源类型）")
        for rel in found.non_lua:
            print(f"    - {rel}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="把 addon/ 打包成 MDL3 版 hd2-coyote（Arsenal zip / 本地 mod zip）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "例子：\n"
            "  python tools/build_addon_mdl3.py\n"
            "  python tools/build_addon_mdl3.py --local --output dist/mdl3\n"
            "  python tools/build_addon_mdl3.py --both --patch-number 7\n"
            "  python tools/build_addon_mdl3.py --addon-dir _w/mdl3/packtest/addon --output _w/mdl3/dist\n"
        ),
    )
    parser.add_argument("--addon-dir", default=str(DEFAULT_ADDON_DIR),
                        help="addon 源码目录（默认仓库根的 addon/）")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT),
                        help="产物目录（默认 build/mdl3）")
    parser.add_argument("--patch-number", type=int, default=DEFAULT_PATCH_NUMBER,
                        help=f"patch 编号，默认 {DEFAULT_PATCH_NUMBER}；手工装才需要挑号")
    parser.add_argument("--display-name", default=DEFAULT_DISPLAY,
                        help="manifest Name（管理器当文件夹名，必须纯 ASCII）")
    parser.add_argument("--guid", default=DEFAULT_GUID,
                        help="manifest Guid；升级必须沿用同一个")
    parser.add_argument("--version-file", default=None,
                        help="默认仓库根 VERSION；只给测试/预发布换文件用")
    form = parser.add_mutually_exclusive_group()
    form.add_argument("--local", action="store_true", help="只出本地 mod zip（Mods 文件夹用）")
    form.add_argument("--both", action="store_true", help="Arsenal zip 与本地 mod zip 都出")
    args = parser.parse_args()

    addon_dir = Path(args.addon_dir)
    if not addon_dir.is_absolute():
        addon_dir = ROOT / addon_dir
    out_dir = Path(args.output)
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir

    try:
        version = resolve_version(Path(args.version_file) if args.version_file else None)
        found = collect(addon_dir)
        version_ok, version_line = inspect_mod_lua_version(found, version)
        print(f"hd2-coyote MDL3 打包 v{version}")
        report(found, version_line)
        check_gates(found)
        if not version_ok:
            raise ValueError(version_line)
        if found.entry is None:
            print("  !  缺 entry.lua：这个包没有可加载入口，装上去也不会跑（工具链仍然照常出包）")

        outputs = []
        if not args.local:
            print("\n[Arsenal]  manifest.json + data/" + CARRIER + f".patch_{args.patch_number}")
            outputs.append(build_arsenal(found, version, args, out_dir))
        if args.local or args.both:
            print(f"\n[本地 mod] {LOCAL_FOLDER}/ 文件夹（直接放进 MDL 的 Mods 目录）")
            outputs.append(build_local(found, version, args, out_dir))
    except ValueError as exc:
        raise SystemExit(f"打包失败：{exc}") from exc

    print("\n已生成：")
    for path in outputs:
        print(f"  {path}")
    if not args.local:
        print("\n安装：Arsenal 里导入上面的 zip -> 启用 -> Deploy -> 重启游戏 -> ESC 里看 MDL Settings。")
        print("      本地路线：把本地 zip 解压出的 hd2coyote/ 放进 "
              "%LOCALAPPDATA%\\MDL\\Helldivers2\\Mods\\，不用 Deploy。")
    print("注意：MDL3 与 BSL 不能同时部署；依赖 MDL3 自带的 HD2 Data Hub。")


if __name__ == "__main__":
    main()

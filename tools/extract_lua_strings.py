"""从 patch 归档里的 Lua 资源提取可读字符串（逆向社区 mod 用）。

社区 mod 的入口通常是明文（`-- HD2-Addon:` 头 + 一段 loadstring），
真正的实现是内嵌的 LuaJIT 字节码 —— 字节码反编译很重，但**常量表里的字符串**
就足以看清：全局名、api 版本、参数名、上限、错误信息。

用法：
    python tools/extract_lua_strings.py --patch "<路径.patch_0>"
    python tools/extract_lua_strings.py --patch ... --grep "register|api|version"
    python tools/extract_lua_strings.py --data "…\\Helldivers 2\\data" --resource mods/cowboybingus/mod_options_menu
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from hd2_patch import LUA_RESOURCE_TYPE, PatchArchive, resource_hash  # noqa: E402

MIN_LEN = 4


def printable_runs(data: bytes, min_len: int = MIN_LEN) -> list[str]:
    """扫描出所有可打印 ASCII 串（含 UTF-8 中文串的粗提取）。"""
    out: list[str] = []
    buf = bytearray()
    for b in data:
        if 32 <= b < 127 or b >= 0x80:      # 允许高位字节（UTF-8 中文）
            buf.append(b)
        else:
            if len(buf) >= min_len:
                out.append(buf.decode("utf-8", "replace"))
            buf.clear()
    if len(buf) >= min_len:
        out.append(buf.decode("utf-8", "replace"))
    return out


def show(blob: bytes, label: str, pattern: str | None) -> None:
    header = blob[:64]
    is_bytecode = header.startswith(b"\x1bLJ")
    print(f"\n===== {label} =====")
    print(f"大小 {len(blob)} 字节 | 明文头 | {'LuaJIT 字节码' if is_bytecode else '明文 Lua'}")
    if not is_bytecode:
        first = blob.split(b"\n", 1)[0].decode("utf-8", "replace")
        print("首行:", first)
        # 明文部分直接看源码
        text = blob.decode("utf-8", "replace")
        lines = [ln for ln in text.splitlines()
                 if not pattern or re.search(pattern, ln, re.IGNORECASE)]
        print(f"明文行数 {len(text.splitlines())}，匹配 {len(lines)} 行：")
        for ln in lines[:80]:
            print("   ", ln[:200])
        return
    strings = printable_runs(blob)
    # 去重但保留顺序
    seen: set[str] = set()
    uniq = [s for s in strings if not (s in seen or seen.add(s))]
    if pattern:
        rx = re.compile(pattern, re.IGNORECASE)
        uniq = [s for s in uniq if rx.search(s)]
    print(f"字符串 {len(strings)} 个，展示 {min(len(uniq), 120)} 个：")
    for s in uniq[:120]:
        print("   ", s[:160])


def main() -> None:
    ap = argparse.ArgumentParser(description="提取 patch 里 Lua 资源的字符串")
    ap.add_argument("--patch", default="", help="直接给 .patch_N 路径")
    ap.add_argument("--data", default=r"E:\SteamLibrary\steamapps\common\Helldivers 2\data")
    ap.add_argument("--resource", default="", help="要按资源名哈希查找的资源名")
    ap.add_argument("--grep", default="", help="只显示匹配该正则的字符串")
    ap.add_argument("--all", action="store_true", help="打印该归档所有 Lua 资源")
    args = ap.parse_args()

    if args.patch:
        archive = PatchArchive.read(args.patch)
        blobs = [(f"<hash=0x{r.name_hash:016X}>", r) for r in archive.resources]
    elif args.resource:
        want = resource_hash(args.resource)
        data = Path(args.data)
        found = False
        for path in sorted(data.glob("9ba626afa44a3aa3.patch_*")):
            if path.name.endswith((".stream", ".gpu_resources")):
                continue
            try:
                archive = PatchArchive.from_bytes(path.read_bytes())
            except Exception:
                continue
            for res in archive.resources:
                if res.name_hash == want:
                    found = True
                    show(res.data, f"{path.name} :: {args.resource}", args.grep or None)
        if not found:
            print(f"没找到资源 {args.resource}")
        return
    else:
        print("需要 --patch 或 --resource")
        return

    for label, res in blobs:
        if res.type_hash != LUA_RESOURCE_TYPE and not args.all:
            continue
        show(res.data, label, args.grep or None)


if __name__ == "__main__":
    main()

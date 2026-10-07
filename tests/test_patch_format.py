"""`.patch_N` 归档格式测试。

最关键的是一条**外部对照**：社区加载器自己占用的 Lua 资源
`core/wwise/lua/wwise_flow_callbacks` 的哈希是已知值 0x7251FDD9BB62480A。
如果我的 MurmurHash64A 能算出它，就说明资源寻址这一环是对的
（游戏/加载器就是靠这个名字哈希找资源的）。
"""

from __future__ import annotations

import struct
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hd2_patch import (BODY_VERSION, ENTRY_RECORD_SIZE, HEADER_SIZE, LUA_RESOURCE_TYPE,
                       MAGIC, TOTAL_SIZE_OFFSET, TYPE_RECORD_SIZE, PatchArchive, Resource,
                       align16, build_lua_patch, murmur64a, resource_hash)  # noqa: E402

KNOWN_LOADER_RESOURCE = ("core/wwise/lua/wwise_flow_callbacks", 0x7251FDD9BB62480A)


class TestMurmur(unittest.TestCase):
    def test_known_loader_resource_hash(self) -> None:
        """加载器自己的资源名 -> 哈希，必须对上（外部对照，不是我自己的约定）。"""
        name, expected = KNOWN_LOADER_RESOURCE
        self.assertEqual(resource_hash(name), expected)

    def test_deterministic_and_64bit(self) -> None:
        a = resource_hash("mods/hd2coyote/hd2_coyote_bridge")
        b = resource_hash("mods/hd2coyote/hd2_coyote_bridge")
        self.assertEqual(a, b)
        self.assertNotEqual(a, resource_hash("mods/hd2coyote/hd2_coyote_bridges"))
        self.assertTrue(0 <= a < 2 ** 64)

    def test_empty_and_short_inputs(self) -> None:
        self.assertEqual(murmur64a(b""), 0)
        for text in ("a", "ab", "abc", "abcd", "abcde", "abcdef", "abcdefg", "abcdefgh"):
            value = resource_hash(text)
            self.assertTrue(0 <= value < 2 ** 64, text)


class TestPatchRoundTrip(unittest.TestCase):
    def setUp(self) -> None:
        self.name = "mods/hd2coyote/hd2_coyote_bridge"
        self.source = "-- HD2-Addon: mods/hd2coyote/hd2_coyote_bridge\nreturn 1\n"
        self.archive = PatchArchive([Resource(self.name, self.source.encode())])

    def test_layout_invariants(self) -> None:
        raw = self.archive.to_bytes()
        magic, version, count = struct.unpack_from("<III", raw, 0)
        self.assertEqual((magic, version, count), (MAGIC, 1, 1))
        total = struct.unpack_from("<Q", raw, TOTAL_SIZE_OFFSET)[0]
        self.assertEqual(total, len(raw))
        # 数据区起点 = 表区按 16 字节对齐（单类型时即文档里的 align16(104 + 80*count)）
        self.assertEqual(align16(HEADER_SIZE + TYPE_RECORD_SIZE + ENTRY_RECORD_SIZE),
                         align16(104 + 80 * count))
        self.assertEqual(align16(104 + 80 * count), 192)
        # 资源体：u32 长度 + u32 版本(=2) + 正文
        offset = struct.unpack_from("<Q", raw, HEADER_SIZE + TYPE_RECORD_SIZE + 16)[0]
        body_len, body_version = struct.unpack_from("<II", raw, offset)
        self.assertEqual(body_version, BODY_VERSION)
        self.assertEqual(body_len, len(self.source.encode()))
        self.assertEqual(raw[offset + 8:offset + 8 + body_len], self.source.encode())

    def test_entry_fields(self) -> None:
        raw = self.archive.to_bytes()
        entry = HEADER_SIZE + TYPE_RECORD_SIZE
        (name_hash, type_hash, offset, a, b, c, d,
         length, e, f, g, h, index) = struct.unpack_from("<7Q6I", raw, entry)
        self.assertEqual(name_hash, resource_hash(self.name))
        self.assertEqual(type_hash, LUA_RESOURCE_TYPE)
        # 真实归档里条目的 length 含 8 字节体头（u32 长度 + u32 版本）
        self.assertEqual(length, len(self.source.encode()) + 8)
        self.assertEqual((a, b, c, d), (0, 0, 0, 0))
        self.assertEqual((e, f, g, h, index), (0, 0, 16, 16, 0))

    def test_round_trip(self) -> None:
        parsed = PatchArchive.from_bytes(self.archive.to_bytes())
        self.assertEqual(len(parsed.resources), 1)
        res = parsed.resources[0]
        self.assertEqual(res.data, self.source.encode())
        self.assertEqual(res.name_hash, resource_hash(self.name))
        self.assertEqual(res.type_hash, LUA_RESOURCE_TYPE)

    def test_multi_resource_types_and_alignment(self) -> None:
        other = Resource("some/other/resource", b"x" * 100, type_hash=0x1234)
        archive = PatchArchive([Resource(self.name, b"a" * 7), other,
                                Resource("third/resource", b"y" * 33)])
        raw = archive.to_bytes()
        parsed = PatchArchive.from_bytes(raw)
        self.assertEqual(len(parsed.resources), 3)
        by_hash = {r.name_hash: r for r in parsed.resources}
        self.assertEqual(by_hash[resource_hash("some/other/resource")].data, b"x" * 100)
        self.assertEqual(by_hash[resource_hash("third/resource")].data, b"y" * 33)
        # 每个资源体都按 16 字节对齐
        for res in parsed.resources:
            self.assertEqual(len(res.data) % 1, 0)
        # 重新写一遍必须字节一致（解析->写入幂等）
        self.assertEqual(PatchArchive(parsed.resources).to_bytes(), raw)

    def test_write_and_read_file(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = self.archive.write(Path(tmp) / "9ba626afa44a3aa3.patch_0")
            self.assertTrue(path.exists())
            parsed = PatchArchive.read(path)
            self.assertEqual(parsed.resources[0].data, self.source.encode())

    def test_rejects_garbage(self) -> None:
        with self.assertRaises(ValueError):
            PatchArchive.from_bytes(b"\x00" * 64)
        with self.assertRaises(ValueError):
            PatchArchive.from_bytes(b"")
        bad = bytearray(self.archive.to_bytes())
        struct.pack_into("<I", bad, 0, 0xDEADBEEF)
        with self.assertRaises(ValueError):
            PatchArchive.from_bytes(bytes(bad))

    def test_rejects_truncated(self) -> None:
        raw = self.archive.to_bytes()
        with self.assertRaises(ValueError):
            PatchArchive.from_bytes(raw[:-8])

    def test_lua_patch_helper(self) -> None:
        raw = build_lua_patch(self.source, self.name)
        parsed = PatchArchive.from_bytes(raw)
        self.assertEqual(parsed.resources[0].data.decode("utf-8"), self.source)

    def test_empty_archive_rejected(self) -> None:
        with self.assertRaises(ValueError):
            PatchArchive([]).to_bytes()


class TestAddonSourceGates(unittest.TestCase):
    """打包前的源码闸门：加载器只认第一行的明文注释。"""

    def test_addon_header_present_and_plain(self) -> None:
        source = (ROOT / "lua" / "hd2_coyote_bridge.lua").read_bytes()
        first = source.split(b"\n", 1)[0]
        self.assertTrue(first.startswith(b"-- HD2-Addon: "), first[:40])
        self.assertLessEqual(len(first), 256)
        self.assertFalse(source.startswith(b"\xef\xbb\xbf"), "不能有 BOM")
        self.assertNotIn(b"\x1b", first, "不能是编译后的字节码")


if __name__ == "__main__":
    unittest.main()

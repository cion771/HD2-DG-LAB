"""HD2 `.patch_N` 归档的读写（游戏内 Lua addon 的载体文件）。

布局是从**游戏目录里 20 个真实归档**反推并用它们逐一验证的
（社区文档的字段尺寸有两处不对：头部是 80 字节不是 72，类型记录是 24 字节不是 32；
条目的 length 字段**包含** 8 字节的体头）：

    +0    80 字节头部   magic=0xF0000011(I), version=1(I), count(I), 20x0,
                        totalSize(Q @32，= 文件总长), 0(Q @40), 32x0
    +80   24 x nTypes   <QIIII   typeHash, entryCount, 0, 16, 16
    之后   80 x count    <7Q6I   nameHash, typeHash, bodyOffset, 0,0,0,0,
                                length(= 8 + 数据长度), 0, 0, 16, 16, index
    数据区 = align16(80 + 24*nTypes + 80*count)，每个资源体 16 字节对齐
    资源体 = u32 dataLen + u32 version(=2) + data

资源名哈希 = MurmurHash64A(name, seed=0)；Lua 资源类型标记 = 0xA14E8DFA2CD117E2。
哈希实现用官方加载器自己的资源名做了对照验证（见 tests/test_patch_format.py）。
"""

from __future__ import annotations

import struct
import uuid
from dataclasses import dataclass
from pathlib import Path

MAGIC = 0xF0000011
VERSION = 1
BODY_VERSION = 2
BODY_HEADER_SIZE = 8
LUA_RESOURCE_TYPE = 0xA14E8DFA2CD117E2
HEADER_SIZE = 80
TOTAL_SIZE_OFFSET = 32  # <III20s> 之后就是 totalSize（Q）
TYPE_RECORD_SIZE = 24
ENTRY_RECORD_SIZE = 80
MASK64 = 0xFFFFFFFFFFFFFFFF


def align16(value: int) -> int:
    return (value + 15) & ~15


# --------------------------------------------------------------------- 哈希
def murmur64a(data: bytes, seed: int = 0) -> int:
    """MurmurHash64A（资源名哈希）。对照值见 tests。"""
    m = 0xC6A4A7935BD1E995
    r = 47
    length = len(data)
    h = (seed ^ (length * m)) & MASK64
    n_blocks = length // 8
    for i in range(n_blocks):
        k = int.from_bytes(data[i * 8:i * 8 + 8], "little")
        k = (k * m) & MASK64
        k ^= k >> r
        k = (k * m) & MASK64
        h ^= k
        h = (h * m) & MASK64
    tail = data[n_blocks * 8:]
    if tail:
        h ^= int.from_bytes(tail, "little")
        h = (h * m) & MASK64
    h ^= h >> r
    h = (h * m) & MASK64
    h ^= h >> r
    return h


def resource_hash(name: str) -> int:
    """资源名 -> 64 位哈希（名字按 UTF-8 字节算）。"""
    return murmur64a(name.encode("utf-8"), 0)


# --------------------------------------------------------------------- 结构
@dataclass
class Resource:
    name: str  # 资源名；从文件读回来时只能得到哈希，这里会是空串
    data: bytes
    type_hash: int = LUA_RESOURCE_TYPE
    name_hash: int = 0  # 读取时填；写入时按 name 计算

    def resolved_name_hash(self) -> int:
        return self.name_hash or resource_hash(self.name)


class PatchArchive:
    """一个 .patch_N 归档。"""

    def __init__(self, resources: list[Resource] | None = None) -> None:
        self.resources: list[Resource] = list(resources or [])

    # ---------------------------------------------------------------- 写
    def to_bytes(self) -> bytes:
        if not self.resources:
            raise ValueError("归档里至少要有一个资源")
        # 同名资源只保留最后一个（游戏也是按覆盖语义）
        # 真实归档里资源表按资源名哈希升序排列
        merged: dict[int, Resource] = {}
        for res in self.resources:
            merged[res.resolved_name_hash()] = res
        entries = [merged[k] for k in sorted(merged)]
        count = len(entries)

        types: dict[int, int] = {}
        for res in entries:
            types[res.type_hash] = types.get(res.type_hash, 0) + 1

        data_start = align16(HEADER_SIZE + TYPE_RECORD_SIZE * len(types)
                             + ENTRY_RECORD_SIZE * count)
        blobs: list[bytes] = []
        offsets: list[int] = []
        lengths: list[int] = []
        cursor = data_start
        for res in entries:
            if res.type_hash == LUA_RESOURCE_TYPE:
                # Lua 资源：u32 数据长度 + u32 版本(=2) + 数据；条目的 length 含这 8 字节
                body = struct.pack("<II", len(res.data), BODY_VERSION) + res.data
                length = len(body)
            else:
                # 其它类型（实测 patch_17）：没有 Lua 信封，length 就是资源体字节数
                body = res.data
                length = len(body)
            pad = (-len(body)) % 16
            blobs.append(body + b"\x00" * pad)
            offsets.append(cursor)
            lengths.append(length)
            cursor += len(body) + pad
        total = cursor

        out = bytearray()
        out += struct.pack("<III", MAGIC, VERSION, count)
        out += b"\x00" * 20
        out += struct.pack("<Q", total)
        out += struct.pack("<Q", 0)
        out += b"\x00" * 32
        assert len(out) == HEADER_SIZE

        for type_hash, type_count in types.items():
            out += struct.pack("<QIIII", type_hash, type_count, 0, 16, 16)

        index_in_type: dict[int, int] = {}
        for res, offset, length, blob in zip(entries, offsets, lengths, blobs):
            idx = index_in_type.get(res.type_hash, 0)
            index_in_type[res.type_hash] = idx + 1
            out += struct.pack(
                "<7Q6I",
                res.resolved_name_hash(), res.type_hash, offset,
                0, 0, 0, 0,
                length, 0, 0, 16, 16, idx,
            )
        # 表区对齐到 16 字节后才放数据区（单个类型时正好等于文档里的 align16(104 + 80*count)）
        out += b"\x00" * (data_start - len(out))
        assert len(out) == data_start, (len(out), data_start)
        for blob in blobs:
            out += blob
        assert len(out) == total
        return bytes(out)

    def write(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(self.to_bytes())
        return p

    # ---------------------------------------------------------------- 读
    @classmethod
    def from_bytes(cls, raw: bytes) -> "PatchArchive":
        if len(raw) < HEADER_SIZE:
            raise ValueError("文件太小，不是 patch 归档")
        magic, version, count = struct.unpack_from("<III", raw, 0)
        if magic != MAGIC:
            raise ValueError(f"magic 不对：0x{magic:08X}（期望 0x{MAGIC:08X}）")
        if version != VERSION:
            raise ValueError(f"版本不支持：{version}")
        total = struct.unpack_from("<Q", raw, TOTAL_SIZE_OFFSET)[0]
        if total != len(raw):
            raise ValueError(f"头部记录的总长({total})与文件实际长度({len(raw)})不符")
        if count <= 0:
            raise ValueError("归档里没有资源")

        # 类型表条数没有单独记在头部：逐个候选值试，直到整张表自洽
        last_error = "无法解析类型表"
        for n_types in range(1, min(count, 64) + 1):
            try:
                return cls._parse_with(raw, count, n_types)
            except ValueError as exc:
                last_error = str(exc)
        raise ValueError(f"解析失败：{last_error}")

    @classmethod
    def _parse_with(cls, raw: bytes, count: int, n_types: int) -> "PatchArchive":
        types: dict[int, int] = {}
        for i in range(n_types):
            type_hash, type_count, _a, _b, _c = struct.unpack_from(
                "<QIIII", raw, HEADER_SIZE + i * TYPE_RECORD_SIZE)
            if type_hash in types:
                raise ValueError(f"类型表出现重复的类型哈希 0x{type_hash:016X}")
            types[type_hash] = type_count
        if sum(types.values()) != count:
            raise ValueError(f"类型表计数({sum(types.values())})与资源总数({count})不符")

        entries_start = HEADER_SIZE + n_types * TYPE_RECORD_SIZE
        archive = cls()
        seen: dict[int, int] = {}
        for i in range(count):
            off = entries_start + i * ENTRY_RECORD_SIZE
            if off + ENTRY_RECORD_SIZE > len(raw):
                raise ValueError("表项越界：文件被截断")
            (name_hash, type_hash, offset, _a, _b, _c, _d,
             length, _e, _f, _g, _h, _index) = struct.unpack_from("<7Q6I", raw, off)
            if type_hash not in types:
                raise ValueError(f"表项的类型哈希 0x{type_hash:016X} 不在类型表里")
            seen[type_hash] = seen.get(type_hash, 0) + 1
            if offset >= len(raw):
                raise ValueError(f"资源体起点越界：offset={offset}")
            # length 可能为 0（未知长度）：那就一直读到文件尾
            end = len(raw) if length == 0 else offset + length
            if end > len(raw):
                raise ValueError(f"资源体越界：offset={offset} length={length}")
            body = raw[offset:end]
            if type_hash == LUA_RESOURCE_TYPE:
                if len(body) < BODY_HEADER_SIZE:
                    raise ValueError("Lua 资源体小于体头长度")
                data_len, body_version = struct.unpack_from("<II", body, 0)
                if body_version != BODY_VERSION:
                    raise ValueError(f"Lua 资源体版本不支持：{body_version}")
                if data_len + BODY_HEADER_SIZE > len(body):
                    raise ValueError(f"资源体数据长度({data_len})超出条目长度({length})")
                data = body[BODY_HEADER_SIZE:BODY_HEADER_SIZE + data_len]
            else:
                data = body  # 其它类型原样保留（不假设它有 Lua 信封）
            archive.resources.append(Resource(
                name="", name_hash=name_hash, type_hash=type_hash, data=data))
        if seen != types:
            raise ValueError(f"类型表计数与实际条目不符：{seen} != {types}")
        return archive

    @classmethod
    def read(cls, path: str | Path) -> "PatchArchive":
        return cls.from_bytes(Path(path).read_bytes())


# --------------------------------------------------------------------- 便捷
def build_lua_patch(lua_source: str, resource_name: str) -> bytes:
    """把一份 Lua 源码打包成 patch 归档字节。"""
    return PatchArchive([Resource(name=resource_name, data=lua_source.encode("utf-8"))]).to_bytes()


def random_guid() -> str:
    return str(uuid.uuid4())

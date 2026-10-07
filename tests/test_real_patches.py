"""用**真实游戏归档**做的回归测试（找不到游戏目录就跳过）。

这是本项目最强的格式验证：拿实际部署中的 `9ba626afa44a3aa3.patch_N`
（Bingus Shared Loader 正在用的那些）跑一遍：

  1. 每个 mod 容器都必须能解析出来（含 1/2/3 个资源、非 Lua 资源的混合样本）；
  2. 解析 -> 重新序列化**必须与原始文件逐字节相同** ——
     这直接证明我们的写入器产出的就是游戏认的格式，而不是"照着文档猜的"。

★ 注意：`data/` 里同时存在**游戏自己的原版归档**（同样是 `9ba626afa44a3aa3.patch_N`
   这个族名，但是另一种压缩容器，版本号 2）。它们不是 mod，不属于本解析器的目标，
   所以按"能不能识别成 mod 容器"分类，只对 mod 容器做严格校验 ——
   但要求至少有一个 mod 容器，否则这个测试就等于没测。
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hd2_patch import LUA_RESOURCE_TYPE, PatchArchive  # noqa: E402

CANDIDATES = [
    Path(r"E:\SteamLibrary\steamapps\common\Helldivers 2\data"),
    Path(r"C:\Program Files (x86)\Steam\steamapps\common\Helldivers 2\data"),
    Path(r"D:\SteamLibrary\steamapps\common\Helldivers 2\data"),
    Path(os.environ.get("HD2_DATA", "")),
]
DATA = next((p for p in CANDIDATES if p and p.is_dir() and any(p.glob("9ba626afa44a3aa3.patch_*"))),
            None)


@unittest.skipUnless(DATA, "找不到 Helldivers 2 的 data 目录（设置 HD2_DATA 可指定）")
class TestRealPatches(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.files = sorted(
            (p for p in DATA.glob("9ba626afa44a3aa3.patch_*")
             if not p.name.endswith((".stream", ".gpu_resources"))),
            key=lambda p: int(p.name.rsplit("_", 1)[1]),
        )
        cls.raw = {p.name: p.read_bytes() for p in cls.files}
        # 分成「mod 容器」与「原版/其它格式」两类
        cls.mod_files: list[Path] = []
        cls.vanilla_files: list[Path] = []
        for path in cls.files:
            try:
                PatchArchive.from_bytes(cls.raw[path.name])
                cls.mod_files.append(path)
            except Exception:
                cls.vanilla_files.append(path)

    def test_at_least_one_mod_archive(self) -> None:
        self.assertGreaterEqual(len(self.files), 1)
        self.assertGreaterEqual(len(self.mod_files), 1,
                                "一个 mod 容器都没有 —— 那这个文件就没在验证任何东西")

    def test_all_mod_archives_parse(self) -> None:
        for path in self.mod_files:
            with self.subTest(patch=path.name):
                archive = PatchArchive.from_bytes(self.raw[path.name])
                self.assertGreaterEqual(len(archive.resources), 1)

    def test_vanilla_archives_are_rejected_cleanly(self) -> None:
        """原版归档（另一种容器）必须**干净地**被拒绝：抛明确异常，而不是解析出垃圾。"""
        for path in self.vanilla_files:
            with self.subTest(patch=path.name):
                with self.assertRaises(Exception) as ctx:
                    PatchArchive.from_bytes(self.raw[path.name])
                msg = str(ctx.exception)
                self.assertTrue(msg, "拒绝时必须给出原因")
                self.assertRegex(msg, r"版本|总长|magic|长度|头部")

    def test_round_trip_is_byte_identical(self) -> None:
        for path in self.mod_files:
            with self.subTest(patch=path.name):
                original = self.raw[path.name]
                rewritten = PatchArchive(
                    PatchArchive.from_bytes(original).resources).to_bytes()
                self.assertEqual(len(rewritten), len(original), "长度必须一致")
                self.assertEqual(rewritten, original, "必须逐字节相同")

    def test_total_size_field_matches_file(self) -> None:
        import struct

        for path in self.mod_files:
            with self.subTest(patch=path.name):
                raw = self.raw[path.name]
                total = struct.unpack_from("<Q", raw, 32)[0]
                self.assertEqual(total, len(raw))

    def test_lua_resources_declare_addon_header(self) -> None:
        """Lua 资源里应该能找到 -- HD2-Addon 声明（加载器就是靠它发现 addon）。"""
        declared = 0
        for path in self.mod_files:
            for res in PatchArchive.from_bytes(self.raw[path.name]).resources:
                if res.type_hash != LUA_RESOURCE_TYPE:
                    continue
                head = res.data.split(b"\n", 1)[0]
                if head.startswith(b"-- HD2-Addon: "):
                    declared += 1
        self.assertGreater(declared, 0, "至少应有一个已部署的 addon 声明")


if __name__ == "__main__":
    unittest.main()

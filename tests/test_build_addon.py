"""打包工具测试：manifest 校验、addon 头校验、ZIP 结构。"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_addon  # noqa: E402
from hd2_patch import PatchArchive  # noqa: E402


def make_args(tmp: Path, **over) -> argparse.Namespace:
    base = dict(
        lua=str(ROOT / "lua" / "hd2_coyote_bridge.lua"),
        resource=build_addon.DEFAULT_RESOURCE,
        display_name="HD2 Coyote Bridge",
        guid=build_addon.DEFAULT_GUID,
        version=build_addon.read_version(),
        patch_number=0,
        output=str(tmp),
        layout="data",
        lua_mods_dir="",
        force=False,
    )
    base.update(over)
    return argparse.Namespace(**base)


class TestBuildAddon(unittest.TestCase):
    def test_display_name_validation(self) -> None:
        self.assertEqual(build_addon.check_display_name("HD2 Coyote Bridge"),
                         "HD2 Coyote Bridge")
        # 社区实测：Name 会被管理器当文件夹名，非 ASCII / 非法字符都会导致导入失败
        with self.assertRaises(ValueError):
            build_addon.check_display_name("轨道激光:取消次数限制")
        with self.assertRaises(ValueError):
            build_addon.check_display_name("bad\\name")
        with self.assertRaises(ValueError):
            build_addon.check_display_name("   ")

    def test_lua_header_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / "good.lua"
            good.write_text("-- HD2-Addon: mods/x/y\nreturn {}\n", encoding="utf-8")
            name, _src = build_addon.check_lua_source(good, "mods/x/y")
            self.assertEqual(name, "mods/x/y")

            bad = Path(tmp) / "bad.lua"
            bad.write_text("local M = {}\nreturn M\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                build_addon.check_lua_source(bad, "mods/x/y")

            mismatch = Path(tmp) / "mismatch.lua"
            mismatch.write_text("-- HD2-Addon: mods/other/name\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                build_addon.check_lua_source(mismatch, "mods/x/y")

            bom = Path(tmp) / "bom.lua"
            bom.write_bytes(b"\xef\xbb\xbf-- HD2-Addon: mods/x/y\n")
            with self.assertRaises(ValueError):
                build_addon.check_lua_source(bom, "mods/x/y")

    def test_zip_structure_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = build_addon.build(make_args(Path(tmp)))
            self.assertTrue(zip_path.exists())
            with zipfile.ZipFile(zip_path) as zf:
                names = zf.namelist()
                self.assertIn("manifest.json", names)
                # Arsenal 实测：补丁必须放在 data/ 下才会被部署（Addon/ 会被当成选项分组）
                self.assertIn("data/9ba626afa44a3aa3.patch_0", names)
                self.assertIn("data/9ba626afa44a3aa3.patch_0.stream", names)
                self.assertIn("data/9ba626afa44a3aa3.patch_0.gpu_resources", names)
                self.assertEqual(zf.read("data/9ba626afa44a3aa3.patch_0.stream"), b"")
                manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
                self.assertEqual(manifest["Version"], 1)
                self.assertEqual(manifest["Name"], "HD2 Coyote Bridge")
                self.assertEqual(manifest["Guid"], build_addon.DEFAULT_GUID)
                self.assertTrue(manifest["Description"])
                # ★ Arsenal 靠 Options[].Include 决定部署哪些目录；缺了它就会
                #   "Deployed successfully - 0 files copied"（实测踩过的坑）
                self.assertIn("Options", manifest, "manifest 必须有 Options，否则 Arsenal 一个文件都不部署")
                option = manifest["Options"][0]
                self.assertIn("data", option["Include"])
                self.assertTrue(option["Name"])
                archive = PatchArchive.from_bytes(zf.read("data/9ba626afa44a3aa3.patch_0"))
            self.assertEqual(len(archive.resources), 1)
            body = archive.resources[0].data.decode("utf-8")
            self.assertTrue(body.startswith("-- HD2-Addon: " + build_addon.DEFAULT_RESOURCE))
            self.assertIn("return", body)

    def test_layout_variants(self) -> None:
        """data（默认，Arsenal 可用）/ root / addon 三种摆放都要能出包，且 Include 对应得上。"""
        with tempfile.TemporaryDirectory() as tmp:
            for layout, expect, include in (
                    ("data", "data/9ba626afa44a3aa3.patch_0", "data"),
                    ("root", "9ba626afa44a3aa3.patch_0", "."),
                    ("addon", "Addon/9ba626afa44a3aa3.patch_0", "Addon")):
                with self.subTest(layout=layout):
                    zip_path = build_addon.build(
                        make_args(Path(tmp), layout=layout, output=str(Path(tmp) / layout)))
                    with zipfile.ZipFile(zip_path) as zf:
                        self.assertIn(expect, zf.namelist())
                        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
                        self.assertEqual(manifest["Options"][0]["Include"], [include])

    def test_bare_archive_has_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            build_addon.build(make_args(Path(tmp)))
            for suffix in ("", ".stream", ".gpu_resources"):
                self.assertTrue((Path(tmp) / f"9ba626afa44a3aa3.patch_0{suffix}").exists(),
                                f"缺少 sidecar：{suffix}")

    def test_patch_number_and_bare_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            build_addon.build(make_args(Path(tmp), patch_number=7))
            bare = Path(tmp) / "9ba626afa44a3aa3.patch_7"
            self.assertTrue(bare.exists())
            self.assertEqual(len(PatchArchive.read(bare).resources), 1)

    def test_real_addon_source_is_packable(self) -> None:
        """真源码必须能过打包闸门（含首行头与资源名一致）。"""
        resource, source = build_addon.check_lua_source(
            ROOT / "lua" / "hd2_coyote_bridge.lua", build_addon.DEFAULT_RESOURCE)
        self.assertEqual(resource, build_addon.DEFAULT_RESOURCE)
        self.assertIn("CowboyBingusModLoader", source)


if __name__ == "__main__":
    unittest.main()

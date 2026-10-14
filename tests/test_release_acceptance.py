from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import release_acceptance as acceptance  # noqa: E402


class TestReleaseAcceptance(unittest.TestCase):
    def test_repository_versions_pass(self) -> None:
        result = acceptance.check_versions(ROOT)
        self.assertEqual(result.status, "PASS", result.evidence)

    def test_report_keeps_manual_checks_pending(self) -> None:
        report = acceptance.render_report(
            "1.2.3", [acceptance.Result("AUT-01", "版本", "PASS", "ok")],
            commit="abc123", tree_state="clean")
        self.assertIn("源码提交：`abc123`；工作树：`clean`", report)
        self.assertIn("自动门禁结论：**PASS**", report)
        self.assertIn("MAN-01", report)
        self.assertIn("PENDING", report)
        self.assertIn("不代表实机、真机或人体安全验收", report)

    def test_desktop_exe_name_includes_exact_version(self) -> None:
        self.assertEqual(acceptance.desktop_exe_name("1.2.3"), "HD2-DG-LAB-1.2.3.exe")

    def test_artifact_matrix_accepts_complete_release_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dist = Path(tmp)
            exe = dist / "HD2-DG-LAB-1.2.3.exe"
            with exe.open("wb") as handle:
                handle.write(b"MZ")
                handle.seek(1024 * 1024)
                handle.write(b"\0")
            bridge = dist / "HD2-Coyote-Bridge-1.2.3.zip"
            with zipfile.ZipFile(bridge, "w") as archive:
                archive.writestr("manifest.json", json.dumps({
                    "Options": [{"Include": ["data"]}],
                }))
                archive.writestr("data/9ba626afa44a3aa3.patch_0", b"patch")
            for name in ("DESKTOP.md", "HOOK.md", "UPGRADE.md", "RELEASE_ACCEPTANCE.md", "RELEASE_NOTES.md"):
                (dist / name).write_text(name, encoding="utf-8")
            release_files = [p for p in dist.iterdir() if p.is_file()]
            sums = "\n".join(
                f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}"
                for path in sorted(release_files)
            ) + "\n"
            # PowerShell 5 commonly emits UTF-8 with BOM; the release gate must accept it.
            (dist / "SHA256SUMS.txt").write_text(sums, encoding="utf-8-sig")

            results = acceptance.check_artifacts(dist, "1.2.3")
            self.assertTrue(results)
            self.assertTrue(all(row.status == "PASS" for row in results), results)

    def test_unversioned_desktop_exe_fails_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dist = Path(tmp)
            exe = dist / "HD2-DG-LAB.exe"
            with exe.open("wb") as handle:
                handle.write(b"MZ")
                handle.seek(1024 * 1024)
                handle.write(b"\0")
            result = next(row for row in acceptance.check_artifacts(dist, "1.2.3") if row.check_id == "AUT-06")
            self.assertEqual(result.status, "FAIL")
            self.assertIn("HD2-DG-LAB-1.2.3.exe", result.evidence)

    def test_checksum_mismatch_fails_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dist = Path(tmp)
            (dist / "SHA256SUMS.txt").write_text("0" * 64 + "  missing.bin\n", encoding="utf-8")
            results = acceptance.check_artifacts(dist, "1.2.3")
            checksum = next(row for row in results if row.check_id == "AUT-09")
            self.assertEqual(checksum.status, "FAIL")
            self.assertIn("extra", checksum.evidence)


if __name__ == "__main__":
    unittest.main()

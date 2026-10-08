"""检查更新：只提示 + 给链接，绝不自动下载覆盖。

网络请求全部通过注入的 opener 假造，测试不碰真网络。
"""

from __future__ import annotations

import io
import json
import unittest
import urllib.error

from hd2coyote import update_check

RELEASE = {
    "tag_name": "v0.5.0",
    "name": "hd2-coyote v0.5.0",
    "html_url": "https://github.com/cion771/hd2-coyote/releases/tag/v0.5.0",
    "published_at": "2026-10-09T10:20:30Z",
    "prerelease": False,
    "body": "更新说明" * 3000,
    "assets": [
        {"name": "source.zip", "size": 1024, "browser_download_url": "u1", "download_count": 1},
        {"name": "HD2-Coyote-Bridge-0.5.0.zip", "size": 2048,
         "browser_download_url": "u2", "download_count": 7},
    ],
}


def opener_for(payload: object):
    def opener(request, timeout=None):        # noqa: ANN001 - 假装是 urlopen
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return io.BytesIO(raw)
    return opener


def opener_raising(exc: Exception):
    def opener(request, timeout=None):        # noqa: ANN001
        raise exc
    return opener


class TestVersions(unittest.TestCase):
    def test_parse_version(self) -> None:
        self.assertEqual(update_check.parse_version("v0.5.0"), (0, 5, 0))
        self.assertEqual(update_check.parse_version("0.5.0-beta.1"), (0, 5, 0))
        self.assertEqual(update_check.parse_version("1.2.3.4"), (1, 2, 3, 4))
        self.assertEqual(update_check.parse_version(""), ())
        self.assertEqual(update_check.parse_version(None), ())

    def test_compare_versions(self) -> None:
        self.assertEqual(update_check.compare_versions("v0.5.0", "0.4.3"), 1)
        self.assertEqual(update_check.compare_versions("0.4.3", "v0.5.0"), -1)
        self.assertEqual(update_check.compare_versions("0.5.0", "0.5"), 0)
        self.assertEqual(update_check.compare_versions("0.10.0", "0.9.9"), 1)   # 数字比较，不是字符串


class TestCheck(unittest.TestCase):
    def test_update_available(self) -> None:
        result = update_check.check(current="0.4.3", opener=opener_for(RELEASE))
        self.assertTrue(result["ok"])
        self.assertTrue(result["update_available"])
        self.assertFalse(result["same"])
        self.assertFalse(result["older"])
        self.assertEqual(result["latest"]["tag"], "v0.5.0")
        self.assertEqual(result["zip"]["name"], "HD2-Coyote-Bridge-0.5.0.zip")
        self.assertIn("v0.5.0", result["release_url"])
        self.assertEqual(len(result["latest"]["body"]), 4000)      # 说明截断

    def test_already_latest(self) -> None:
        result = update_check.check(current="0.5.0", opener=opener_for(RELEASE))
        self.assertTrue(result["ok"])
        self.assertTrue(result["same"])
        self.assertFalse(result["update_available"])

    def test_local_is_newer(self) -> None:
        result = update_check.check(current="0.6.0", opener=opener_for(RELEASE))
        self.assertTrue(result["older"])
        self.assertFalse(result["update_available"])

    def test_missing_release_is_only_a_message(self) -> None:
        result = update_check.check(opener=opener_raising(
            urllib.error.HTTPError("u", 404, "not found", {}, None)))
        self.assertFalse(result["ok"])
        self.assertIn("还没有发布", result["error"])

    def test_http_error_code_is_reported(self) -> None:
        result = update_check.check(opener=opener_raising(
            urllib.error.HTTPError("u", 403, "rate limited", {}, None)))
        self.assertFalse(result["ok"])
        self.assertIn("403", result["error"])

    def test_offline_is_only_a_message(self) -> None:
        result = update_check.check(opener=opener_raising(urllib.error.URLError("没网")))
        self.assertFalse(result["ok"])
        self.assertIn("连不上 GitHub", result["error"])

    def test_broken_json_is_only_a_message(self) -> None:
        result = update_check.check(opener=opener_for("不是 json".encode("utf-8")))
        self.assertFalse(result["ok"])
        self.assertIn("JSONDecodeError", result["error"])

    def test_non_object_payload_is_rejected(self) -> None:
        result = update_check.check(opener=opener_for(b"[1, 2, 3]"))
        self.assertFalse(result["ok"])
        self.assertIn("Release", result["error"])

    def test_summarize_tolerates_missing_fields(self) -> None:
        summary = update_check.summarize({})
        self.assertEqual(summary["tag"], "")
        self.assertEqual(summary["assets"], [])
        self.assertFalse(summary["prerelease"])

    def test_pick_zip(self) -> None:
        self.assertIsNone(update_check.pick_zip([]))
        self.assertIsNone(update_check.pick_zip([{"name": "readme.txt"}]))
        self.assertEqual(
            update_check.pick_zip([{"name": "a.zip"}, {"name": "b.zip"}])["name"], "a.zip")
        self.assertEqual(
            update_check.pick_zip([{"name": "a.zip"},
                                   {"name": "HD2-Coyote-Bridge-0.5.0.ZIP"}])["name"],
            "HD2-Coyote-Bridge-0.5.0.ZIP")

    def test_format_result(self) -> None:
        text = update_check.format_result(
            update_check.check(current="0.4.3", opener=opener_for(RELEASE)))
        self.assertIn("当前版本：0.4.3", text)
        self.assertIn("v0.5.0", text)
        self.assertIn("u2", text)
        failure = update_check.format_result({"ok": False, "current": "0.4.3", "error": "连不上"})
        self.assertIn("连不上", failure)
        self.assertIn("releases", failure)

    def test_default_repo_is_this_project(self) -> None:
        self.assertEqual(update_check.DEFAULT_REPO, "cion771/hd2-coyote")


if __name__ == "__main__":
    unittest.main()

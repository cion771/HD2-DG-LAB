"""游戏内连接信息/日志菜单的 LuaJIT 离线测试。

使用公开 api=1/version=3 的选择行和动态 description；不伪造纯文本 API。
信息行不应用任何菜单保存值或用户改动到桥配置。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

try:
    import lupa.luajit21 as lj

    LUPA_OK = True
except Exception:  # pragma: no cover
    LUPA_OK = False

ROOT = Path(__file__).resolve().parents[1]
ADDON = ROOT / "lua" / "hd2_coyote_bridge.lua"
FAKE = ROOT / "tests" / "lua" / "fake_game.lua"

BASE = 0x140000000
ACTORS = 0x141000000
PINGREC = 0x142000000
MOD_ID = "hd2coyote"


@unittest.skipUnless(LUPA_OK, "需要 lupa（LuaJIT）才能跑 Lua 侧测试")
class TestInGameMenu(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self._old = os.environ.get("HD2COYOTE_DIR")
        os.environ["HD2COYOTE_DIR"] = str(self.dir)

    def tearDown(self) -> None:
        if self._old is None:
            os.environ.pop("HD2COYOTE_DIR", None)
        else:
            os.environ["HD2COYOTE_DIR"] = self._old
        self.tmp.cleanup()

    # ------------------------------------------------------------ 工具
    def _rt(self, menu: bool = True, hp: float = 62.0):
        rt = lj.LuaRuntime()
        rt.execute(FAKE.read_text(encoding="utf-8"))
        for addr, expr in (
            (BASE + 0x3326d20, f"u64le({ACTORS})"),
            (ACTORS + 0x70, "u32le(1)"),
            (ACTORS + 0x110, f"u64le({PINGREC})"),
            (PINGREC + 8, "u32le(4242)"),
            (PINGREC + 16, "u32le(7)"),
            (PINGREC + 0x20, f"f32le({hp})"),
            (PINGREC + 0x24, "f32le(100.0)"),
            (PINGREC + 0x28, "string.char(3)"),
            (PINGREC + 0x2C, "string.char(0)"),
        ):
            rt.execute(f"poke({addr}, {expr})")
        rt.execute("__hd2_tick = function() end")
        if menu:
            rt.execute("ModOptionsMenu = make_fake_menu()")
        return rt

    def _write_cfg(self, mode: str = "live", extra: str = "") -> None:
        (self.dir / "bridge_config.lua").write_text(
            "return {\n"
            f"  config = {{ mode = '{mode}', port = 47777, frame_hooks = {{ '__hd2_tick' }} }},\n"
            "  profiles = { steam_25480438 = { hp = 0x20, hp_max = 0x24, limb_mask = 0x28,"
            " dead = 0x2C } },\n" + extra + "}\n", encoding="utf-8")

    def _load(self, rt) -> None:
        self.exports = rt.compile(ADDON.read_text(encoding="utf-8"), "bridge")()
        rt.globals()["EXPORTS"] = self.exports

    def _registered(self, rt) -> list[dict]:
        tbl = rt.globals()["ModOptionsMenu"].registered
        return [{"id": tbl[i].id, "spec": tbl[i].spec} for i in range(1, len(tbl) + 1)]

    def _spec(self, spec, key):
        value = spec[key]
        return None if value is None else value

    def _description(self, rt, suffix: str) -> str:
        regs = {r["id"]: r["spec"] for r in self._registered(rt)}
        text = regs[f"{MOD_ID}.info.{suffix}"]["description"]
        return text() if callable(text) else text

    def test_registers_information_only(self) -> None:
        rt = self._rt()
        self._write_cfg()
        self._load(rt)
        regs = self._registered(rt)
        self.assertEqual(len(regs), 10)
        self.assertEqual(len({r["id"] for r in regs}), 10)
        self.assertEqual(rt.eval("next(ModOptionsMenu.changes)"), None)
        for r in regs:
            self.assertTrue(r["id"].startswith(f"{MOD_ID}.info."))
            spec = r["spec"]
            self.assertEqual(spec["type"], "choice")
            self.assertEqual(spec["choices"][1](), spec["choices"][2]())
            self.assertLessEqual(len(spec["choices"][1]()), 48)
            self.assertEqual(spec["default"], 1)
            self.assertLessEqual(len(spec["label"]), 64)
            self.assertLessEqual(len(spec["mod"]), 40)
            self.assertLessEqual(len(spec["description"]()), 400)
        self.assertTrue(rt.eval("EXPORTS.menu.registered"))

    def test_menu_never_reads_or_sets_shared_saved_values(self) -> None:
        rt = self._rt()
        rt.execute("menu_value_calls = 0; "
                   "ModOptionsMenu.get = function() menu_value_calls=menu_value_calls+1 end; "
                   "ModOptionsMenu.set = ModOptionsMenu.get")
        self._write_cfg()
        self._load(rt)
        for _ in range(5):
            rt.execute("EXPORTS.frame_tick()")
        self.assertEqual(rt.eval("menu_value_calls"), 0)

    def test_saved_legacy_values_cannot_override_file_config(self) -> None:
        rt = self._rt()
        rt.execute("ModOptionsMenu.values['hd2coyote.mode'] = 1; "
                   "ModOptionsMenu.values['hd2coyote.port'] = 48888; "
                   "ModOptionsMenu.values['hd2coyote.enabled'] = false; "
                   "ModOptionsMenu.values['hd2coyote.info.endpoint'] = 2")
        self._write_cfg()
        self._load(rt)
        self.assertEqual(rt.eval("EXPORTS.config.mode"), "live")
        self.assertEqual(rt.eval("EXPORTS.config.port"), 47777)
        self.assertTrue(rt.eval("EXPORTS.config.enabled"))
        self.assertEqual(rt.eval("ModOptionsMenu.values['hd2coyote.info.endpoint']"), 2)

    def test_information_interaction_cannot_change_bridge(self) -> None:
        rt = self._rt()
        self._write_cfg()
        self._load(rt)
        for r in self._registered(rt):
            rt.execute(f"ModOptionsMenu.apply('{r['id']}', 2)")
        rt.execute("EXPORTS.menu_tick()")
        self.assertEqual(rt.eval("EXPORTS.config.mode"), "live")
        self.assertEqual(rt.eval("EXPORTS.config.port"), 47777)
        self.assertEqual(rt.eval("EXPORTS.config.interval"), 0.1)
        self.assertTrue(rt.eval("EXPORTS.config.enabled"))
        self.assertTrue(rt.eval("EXPORTS.live.active"))
        self.assertEqual(rt.eval("EXPORTS.profiles.steam_25480438.hp"), 0x20)
        self.assertEqual(rt.eval("EXPORTS.config.frame_hooks[1]"), "__hd2_tick")
        self.assertFalse((self.dir / "recon_report.txt").exists())
        for r in self._registered(rt):
            self.assertEqual(rt.eval(f"ModOptionsMenu.get('{r['id']}')"), 2)

    def test_connection_information_is_not_peer_acknowledgement(self) -> None:
        rt = self._rt()
        self._write_cfg()
        self._load(rt)
        self.assertIn("127.0.0.1:47777", self._description(rt, "endpoint"))
        self.assertIn("无法确认", self._description(rt, "endpoint"))
        self.assertIn("对端接收未确认", self._description(rt, "transport"))
        initial = self._description(rt, "packets")
        rt.execute("EXPORTS.frame_tick()")
        self.assertNotEqual(initial, self._description(rt, "packets"))
        rt.execute("FAKE_WS2.Hd2Coyote_sendto = function() return -1 end")
        rt.execute("EXPORTS.frame_tick()")
        self.assertIn("发送失败", self._description(rt, "transport"))
        rt.execute("FAKE_WS2.Hd2Coyote_sendto = function(_, _, n) return n end")
        rt.execute("EXPORTS.frame_tick()")
        self.assertIn("对端接收未确认", self._description(rt, "transport"))

    def test_recent_logs_refresh_and_remain_bounded_utf8(self) -> None:
        rt = self._rt()
        self._write_cfg()
        self._load(rt)
        self.assertIn("STATUS:", self._description(rt, "log1"))
        # Force protected frame errors, exercising the actual bounded log path.
        rt.execute("EXPORTS.live.P.hp_anchor = 'base'; "
                   "EXPORTS.live.R.f32 = function() error(string.rep('测试', 500)) end")
        for _ in range(12):
            rt.execute("EXPORTS.frame_tick()")
        for i in range(1, 7):
            text = self._description(rt, f"log{i}")
            self.assertIn("live_step 异常", text)
            self.assertLessEqual(len(text), 400)
            self.assertNotIn("\n", text)
        self.assertEqual(len(self._registered(rt)), 10)

    def test_old_menu_has_explicit_upgrade_message(self) -> None:
        rt = self._rt()
        rt.execute("ModOptionsMenu.version = 1")
        self._write_cfg()
        self._load(rt)
        for r in self._registered(rt):
            self.assertIsInstance(r["spec"]["description"], str)
            self.assertIn("升级", r["spec"]["description"])
        self.assertTrue(rt.eval("EXPORTS.live.active"))

    def test_partial_failure_is_logged_not_fatal(self) -> None:
        rt = self._rt(menu=False)
        rt.execute("ModOptionsMenu = make_fake_menu({ fail_after = 4 })")
        self._write_cfg()
        self._load(rt)
        self.assertEqual(len(self._registered(rt)), 4)
        log = (self.dir / "hd2_coyote_bridge.log").read_text(encoding="utf-8")
        self.assertIn("部分信息注册失败", log)
        self.assertTrue(rt.eval("EXPORTS.live.active"))

    def test_gives_up_when_menu_absent(self) -> None:
        rt = self._rt(menu=False)
        self._write_cfg()
        self._load(rt)
        rt.execute("EXPORTS.config.menu_retry_frames = 3")
        for _ in range(3):
            rt.execute("EXPORTS.menu_tick()")
        self.assertTrue(rt.eval("EXPORTS.menu.gave_up"))
        log = (self.dir / "hd2_coyote_bridge.log").read_text(encoding="utf-8")
        self.assertIn("放弃注册", log)

    # ------------------------------------------------------------ 帧钩子
    def test_frame_hook_wraps_update_and_forwards(self) -> None:
        """官方要求：参数与返回值原样透传，自己的错误不许影响下面。"""
        rt = self._rt(menu=False)
        rt.execute("__calls = 0; update = function(a, b) __calls = __calls + 1; return a + b end")
        self._write_cfg("live")
        # 让钩子选 update
        (self.dir / "bridge_config.lua").write_text(
            "return { config = { mode = 'live', frame_hooks = { 'update' } },"
            " profiles = { steam_25480438 = { hp = 0x20, hp_max = 0x24, limb_mask = 0x28 } } }\n",
            encoding="utf-8")
        self._load(rt)
        self.assertEqual(rt.eval("EXPORTS.hook.name"), "update")
        result = rt.eval("update(2, 3)")
        self.assertEqual(result, 5, "返回值必须透传")
        self.assertEqual(rt.eval("__calls"), 1, "原函数必须被调用")

    def test_menu_tick_runs_from_frame_hook(self) -> None:
        """菜单比我们晚加载时，要在帧钩子里补注册。"""
        rt = self._rt(menu=False)
        self._write_cfg("live")
        self._load(rt)
        self.assertEqual(rt.eval("EXPORTS.menu.registered"), False)
        rt.execute("ModOptionsMenu = make_fake_menu()")  # 菜单"现在才加载"
        rt.execute("EXPORTS.menu_tick()")
        self.assertEqual(rt.eval("EXPORTS.menu.registered"), True)
        self.assertEqual(rt.eval("#ModOptionsMenu.registered"), 10)

    def test_log_falls_back_to_loader_open_log(self) -> None:
        """自建目录写不了时，用加载器的 open_log（那个目录由加载器保证可用）。

        注意：**绝不 os.execute/mkdir** —— 主线程同步 spawn cmd.exe 会被安全软件拦住，
        表现就是"游戏停止响应 + 黑屏"（实测踩过）。
        """
        logfile = self.dir / "Hd2CoyoteBridge.log"
        blocker = self.dir / "not_a_dir"
        blocker.write_text("x", encoding="utf-8")  # 让 HD2COYOTE_DIR 指向一个文件
        os.environ["HD2COYOTE_DIR"] = str(blocker)

        rt = self._rt(menu=False)
        target = str(logfile).replace("\\", "/")
        # 用"每次写都开关文件"的代理句柄，避免测试读文件时被占用
        rt.execute("CowboyBingusModLoader = { api = 1, version = 17,"
                   " open_log = function(name) return {"
                   "   write = function(_, s) local f = io.open('" + target + "', 'a');"
                   "     f:write(s); f:close() end,"
                   "   flush = function() end } end }")
        self._write_cfg("live")
        self._load(rt)

        self.assertTrue(logfile.exists(), "应当写进加载器给的日志文件")
        text = logfile.read_text(encoding="utf-8")
        self.assertIn("启动 v", text)
        self.assertIn("STATUS:", text)  # STATUS 文件写不了时，状态行必须落到日志里

    def test_after_startup_registration_on_loader_v19(self) -> None:
        """v19+ 有 after_startup（官方推荐的注册时机）时必须用它。"""
        rt = self._rt(menu=False)
        rt.execute("""
            __after = {}
            CowboyBingusModLoader = { api = 1, version = 17, revision = 'loader-v19',
                after_startup = function(fn) __after[#__after + 1] = fn; return true end }
        """)
        self._write_cfg("live")
        self._load(rt)
        self.assertEqual(rt.eval("#__after"), 1, "应当注册一个 after_startup 回调")
        log = (self.dir / "hd2_coyote_bridge.log").read_text(encoding="utf-8")
        self.assertIn("after_startup", log)
        # 回调触发时菜单才出现 -> 仍能注册
        rt.execute("ModOptionsMenu = make_fake_menu()")
        rt.execute("__after[1]()")
        self.assertEqual(rt.eval("EXPORTS.menu.registered"), True)
        self.assertEqual(rt.eval("#ModOptionsMenu.registered"), 10)

    def test_loader_v18_without_after_startup_still_registers(self) -> None:
        """用户的加载器是 v18（没有 after_startup）：不能因此不注册。"""
        rt = self._rt(menu=False)
        rt.execute("CowboyBingusModLoader = { api = 1, version = 17 }")
        self._write_cfg("live")
        self._load(rt)
        self.assertEqual(rt.eval("EXPORTS.menu.registered"), False)
        rt.execute("ModOptionsMenu = make_fake_menu()")
        rt.execute("EXPORTS.frame_tick()")  # 每帧重试这条路
        self.assertEqual(rt.eval("EXPORTS.menu.registered"), True)
        self.assertEqual(rt.eval("#ModOptionsMenu.registered"), 10)


if __name__ == "__main__":
    unittest.main()

"""游戏内设置界面（Mod Options Menu 集成）的离线测试。

官方 API 参考：https://github.com/CowboyBingus/ModOptionsMenu 的 README
（api=1 / version=3，register_option / get / set / on_change / ready，
选项类型 toggle|choice|slider，每个 mod 最多 32 个选项，
label ≤64 字符、mod 名 ≤40、每个 choice ≤48、description ≤400，
choice 2~16 项、slider 需要有限且 min<max、0<step<=max-min）。

这里的假菜单按同样语义实现，所以能验证：注册参数合法、值能落到配置、
失败有原因、菜单不在时不崩也不刷屏。
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

    # ------------------------------------------------------------ 注册
    def test_registers_options_with_legal_specs(self) -> None:
        rt = self._rt()
        self._write_cfg("live")
        self._load(rt)
        regs = self._registered(rt)
        self.assertEqual(len(regs), 10, [r["id"] for r in regs])
        ids = [r["id"] for r in regs]
        self.assertEqual(ids[0], f"{MOD_ID}.enabled")
        self.assertEqual(len(set(ids)), len(ids), "选项 id 不能重复")
        for r in regs:
            self.assertTrue(r["id"].startswith(MOD_ID + "."))
            self.assertLessEqual(len(r["id"].encode()), 96)
            spec = r["spec"]
            self.assertIn(self._spec(spec, "type"), ("toggle", "choice", "slider"))
            # 官方文本上限
            self.assertLessEqual(len(self._spec(spec, "label")), 64)
            self.assertLessEqual(len(self._spec(spec, "mod")), 40)
            desc = self._spec(spec, "description") or ""
            self.assertLessEqual(len(desc), 400)
            self.assertEqual(self._spec(spec, "mod_id"), MOD_ID)
            if self._spec(spec, "type") == "choice":
                choices = [self._spec(spec, "choices")[i]
                           for i in range(1, len(self._spec(spec, "choices")) + 1)]
                self.assertGreaterEqual(len(choices), 2)
                self.assertLessEqual(len(choices), 16)
                for c in choices:
                    self.assertLessEqual(len(c), 48)
                default = self._spec(spec, "default")
                self.assertTrue(1 <= default <= len(choices), f"{r['id']} default 越界")
            if self._spec(spec, "type") == "slider":
                lo, hi, step = (self._spec(spec, k) for k in ("min", "max", "step"))
                self.assertLess(lo, hi)
                self.assertGreater(step, 0)
                self.assertLessEqual(step, hi - lo)
                default = self._spec(spec, "default")
                self.assertTrue(lo <= default <= hi, f"{r['id']} default 越界")
        self.assertEqual(rt.eval("EXPORTS.menu.registered"), True)

    def test_defaults_come_from_effective_config(self) -> None:
        """菜单默认值必须等于当前生效配置（文件里填过偏移时不能被默认值冲掉）。"""
        rt = self._rt()
        self._write_cfg("live")
        self._load(rt)
        regs = {r["id"]: r["spec"] for r in self._registered(rt)}
        self.assertEqual(regs[f"{MOD_ID}.port"]["default"], 47777)
        self.assertEqual(regs[f"{MOD_ID}.mode"]["default"], 2)  # live
        self.assertEqual(regs[f"{MOD_ID}.hp_offset"]["default"], 0x20)
        self.assertEqual(regs[f"{MOD_ID}.limb_offset"]["default"], 0x28)
        # 钩子默认值应指向配置文件里的那个函数
        choices = regs[f"{MOD_ID}.hook"]["choices"]
        names = [choices[i] for i in range(1, len(choices) + 1)]
        self.assertEqual(names[regs[f"{MOD_ID}.hook"]["default"] - 1], "__hd2_tick")

    def test_partial_failure_is_logged_not_fatal(self) -> None:
        rt = self._rt(menu=False)
        rt.execute("ModOptionsMenu = make_fake_menu({ fail_after = 4 })")
        self._write_cfg("live")
        self._load(rt)
        self.assertEqual(len(self._registered(rt)), 4)
        log = (self.dir / "hd2_coyote_bridge.log").read_text(encoding="utf-8")
        self.assertIn("部分选项注册失败", log)
        # 注册失败也不影响 live 工作
        self.assertEqual(rt.eval("EXPORTS.live.active"), True)

    def test_gives_up_when_menu_absent(self) -> None:
        rt = self._rt(menu=False)
        self._write_cfg("live")
        self._load(rt)
        rt.execute("EXPORTS.config.menu_retry_frames = 3")
        for _ in range(3):
            rt.execute("EXPORTS.menu_tick()")
        self.assertEqual(rt.eval("EXPORTS.menu.gave_up"), True)
        log = (self.dir / "hd2_coyote_bridge.log").read_text(encoding="utf-8")
        self.assertIn("放弃注册", log)

    def test_hook_choices_contain_update_and_none(self) -> None:
        rt = self._rt()
        self._write_cfg("live")
        self._load(rt)
        choices = rt.eval("EXPORTS.menu.hook_choices")
        values = [choices[i] for i in range(1, len(choices) + 1)]
        self.assertEqual(values[0], "update", "官方参考：包装全局 update")
        self.assertIn("none", values)
        self.assertLessEqual(len(values), 16)

    # ------------------------------------------------------------ APPLY 后生效
    def test_apply_updates_configuration(self) -> None:
        rt = self._rt()
        self._write_cfg("live")
        self._load(rt)

        rt.execute(f"ModOptionsMenu.apply('{MOD_ID}.port', 48888)")
        self.assertEqual(rt.eval("EXPORTS.config.port"), 48888)

        rt.execute(f"ModOptionsMenu.apply('{MOD_ID}.interval', 0.25)")
        self.assertAlmostEqual(rt.eval("EXPORTS.config.interval"), 0.25, places=3)

        rt.execute(f"ModOptionsMenu.apply('{MOD_ID}.enabled', false)")
        self.assertEqual(rt.eval("EXPORTS.config.enabled"), False)

        rt.execute(f"ModOptionsMenu.apply('{MOD_ID}.limb_shift', 3)")
        self.assertEqual(rt.eval("EXPORTS.profiles['steam_25480438'].limb_shift"), 3)

    def test_apply_offsets_and_switch_to_live(self) -> None:
        rt = self._rt()
        self._write_cfg("recon")  # 先侦察模式，偏移也清空
        self._load(rt)
        # 模拟玩家在菜单里填偏移（去掉文件里的 hp，模拟"还没填"）
        rt.execute("EXPORTS.profiles['steam_25480438'].hp = nil")
        rt.execute("EXPORTS.live.active = false")
        rt.execute("EXPORTS.config.mode = 'recon'")
        self.assertEqual(rt.eval("EXPORTS.live.active"), False)

        rt.execute(f"ModOptionsMenu.apply('{MOD_ID}.hp_offset', 0x20)")
        self.assertEqual(rt.eval("EXPORTS.profiles['steam_25480438'].hp"), 0x20)

        rt.execute(f"ModOptionsMenu.apply('{MOD_ID}.mode', 2)")  # 切到 Live
        self.assertEqual(rt.eval("EXPORTS.config.mode"), "live")
        self.assertEqual(rt.eval("EXPORTS.live.active"), True)
        status = (self.dir / "hd2_coyote_status.txt").read_text(encoding="utf-8")
        self.assertTrue(status.startswith("OK - "), status)

    def test_apply_mode_recon_runs_scan(self) -> None:
        rt = self._rt()
        self._write_cfg("live")
        self._load(rt)
        self.assertTrue((self.dir / "recon_report.txt").exists() is False)
        rt.execute(f"ModOptionsMenu.apply('{MOD_ID}.mode', 1)")  # 切回侦察
        self.assertEqual(rt.eval("EXPORTS.config.mode"), "recon")
        self.assertEqual(rt.eval("EXPORTS.live.active"), False)
        self.assertTrue((self.dir / "recon_report.txt").exists())

    def test_picking_none_hook_stops_live(self) -> None:
        rt = self._rt()
        self._write_cfg("live")
        self._load(rt)
        idx = rt.eval("(function() "
                      "for i, n in ipairs(EXPORTS.menu.hook_choices) do "
                      "if n == 'none' then return i end end return 1 end)()")
        rt.execute(f"ModOptionsMenu.apply('{MOD_ID}.hook', {idx})")
        self.assertEqual(rt.eval("EXPORTS.config.frame_hooks[1]"), "none")

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

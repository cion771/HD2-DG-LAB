"""游戏内 Lua 桥的离线验证（LuaJIT + 假 FFI + 假内存）。

游戏装不上自动化测试，但 addon 的这些东西可以离线验：
  * 语法闸门：必须能过 **LuaJIT**（游戏就是 LuaJIT 2.1，不是 Lua 5.4）
  * 读内存 / 定位本地玩家 / 读状态：用假内存喂进去，看解析对不对
  * 打包出去的 UDP 报文：格式对不对、Python 侧认不认
没有游戏也能把一个真 bug 挡在实机之前。
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import time
import unittest
from pathlib import Path

try:
    import lupa.luajit21 as lj

    LUPA_OK = True
except Exception:  # pragma: no cover
    LUPA_OK = False

from hd2coyote.config import AppConfig
from hd2coyote.events import Damage
from hd2coyote.hook import HookSource

ROOT = Path(__file__).resolve().parents[1]
ADDON = ROOT / "lua" / "hd2_coyote_bridge.lua"
FAKE = ROOT / "tests" / "lua" / "fake_game.lua"

BASE = 0x140000000
ACTORS = 0x141000000
PINGREC = 0x142000000
ENTITY_ID = 4242

#: LuaJIT(5.1) 里不存在的库函数 —— 源码扫描兜底（语法闸门抓不到运行时才报错的）
FORBIDDEN = ("string.pack", "string.unpack", "math.type", "table.move", "table.pack",
             "utf8.", "os.getenv('X')", "::")


def free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def lua_list(rt, name: str) -> list:
    tbl = rt.globals()[name]
    return [tbl[i] for i in range(1, len(tbl) + 1)]


@unittest.skipUnless(LUPA_OK, "需要 lupa（LuaJIT）才能跑 Lua 侧测试")
class TestLuaAddon(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self._old_env = os.environ.get("HD2COYOTE_DIR")
        os.environ["HD2COYOTE_DIR"] = str(self.dir)

    def tearDown(self) -> None:
        if self._old_env is None:
            os.environ.pop("HD2COYOTE_DIR", None)
        else:
            os.environ["HD2COYOTE_DIR"] = self._old_env
        self.tmp.cleanup()

    # ------------------------------------------------------------ 工具
    def _rt(self):
        rt = lj.LuaRuntime()
        rt.execute(FAKE.read_text(encoding="utf-8"))
        return rt

    def _memory(self, rt, hp=62.0, hp_max=100.0, limb_mask=3, dead=0, bleeding=0) -> None:
        """按协议语义喂假内存：hp / hp_max 都是**绝对值**（如 62/100）。"""
        rt.execute(f"poke({BASE + 0x3326d20}, u64le({ACTORS}))")
        rt.execute(f"poke({ACTORS + 0x70}, u32le(1))")
        rt.execute(f"poke({ACTORS + 0x110}, u64le({PINGREC}))")
        rt.execute(f"poke({PINGREC + 8}, u32le({ENTITY_ID}))")
        rt.execute(f"poke({PINGREC + 16}, u32le(7))")
        rt.execute(f"poke({PINGREC + 0x20}, f32le({hp}))")
        rt.execute(f"poke({PINGREC + 0x24}, f32le({hp_max}))")
        rt.execute(f"poke({PINGREC + 0x28}, string.char({limb_mask}))")
        rt.execute(f"poke({PINGREC + 0x2C}, string.char({dead}))")
        rt.execute(f"poke({PINGREC + 0x2D}, string.char({bleeding}))")

    def _write_user_config(self, mode: str, port: int | None = None,
                           hooks: str = "{ '__hd2_test_tick' }", with_offsets: bool = True) -> None:
        offsets = ("hp = 0x20, hp_max = 0x24, limb_mask = 0x28, dead = 0x2C, bleeding = 0x2D,"
                   if with_offsets else "")
        port_line = f"port = {port}," if port is not None else ""
        (self.dir / "bridge_config.lua").write_text(
            "return {\n"
            f"  config = {{ mode = '{mode}', {port_line} frame_hooks = {hooks} }},\n"
            "  profiles = { steam_25480438 = {\n"
            f"      {offsets}\n"
            "  } },\n"
            "}\n", encoding="utf-8")

    def _load(self, rt) -> None:
        chunk = rt.compile(ADDON.read_text(encoding="utf-8"), "hd2_coyote_bridge")
        self.exports = chunk()
        rt.globals()["EXPORTS"] = self.exports

    def _write_cfg(self, mode: str = "recon") -> None:
        """写 bridge_config.lua 指定模式（addon 默认是 safe，读内存的测试必须显式选）。"""
        (self.dir / "bridge_config.lua").write_text(
            "return {\n"
            f"  config = {{ mode = '{mode}', frame_hooks = {{ '__hd2_tick' }} }},\n"
            "  profiles = { steam_25480438 = { hp = 0x20, hp_max = 0x24, limb_mask = 0x28,"
            " dead = 0x2C } },\n"
            "}\n", encoding="utf-8")

    # ------------------------------------------------------------ 1. 语法闸门
    def test_compiles_under_luajit(self) -> None:
        rt = self._rt()
        source = ADDON.read_text(encoding="utf-8")
        # 必须是明文、第一行必须是加载器认的 addon 头
        first = source.splitlines()[0]
        self.assertTrue(first.startswith("-- HD2-Addon: "), first)
        self.assertLessEqual(len(first.encode("utf-8")), 256)
        self.assertTrue(source.isprintable() or "\n" in source)
        # 真·LuaJIT 编译闸门（// 、goto 这类 5.2+ 语法会在这里炸）
        self.assertIsNotNone(rt.compile(source, "hd2_coyote_bridge"))

    def test_no_luajit_missing_stdlib(self) -> None:
        code = "\n".join(line.split("--")[0] for line in
                         ADDON.read_text(encoding="utf-8").splitlines())
        for bad in FORBIDDEN:
            if bad == "::":
                continue  # 标签语法在 LuaJIT 里由编译闸门负责
            self.assertNotIn(bad, code, f"用到了 LuaJIT 没有的 {bad}")

    # ------------------------------------------------------------ 2. 定位与读状态
    def test_recon_finds_player_and_writes_evidence(self) -> None:
        rt = self._rt()
        self._memory(rt)
        self._write_cfg("recon")
        self._load(rt)

        sent = lua_list(rt, "FAKE_SENT")
        self.assertTrue(any('"ev":"hello"' in s for s in sent), sent)

        status = (self.dir / "hd2_coyote_status.txt").read_text(encoding="utf-8")
        self.assertTrue(status.startswith("OK - "), status)

        report = (self.dir / "recon_report.txt").read_text(encoding="utf-8")
        self.assertIn(f"local_player_entity={ENTITY_ID}", report)
        self.assertIn("globals_functions=", report)
        self.assertIn("anchor_hex_dump:", report)
        self.assertIn("module_base=0x140000000", report)

    def test_pointer_above_32bit_not_truncated(self) -> None:
        """回归：LuaJIT 的 tonumber(hex,16) 只按 32 位解析，指针不能用它。"""
        rt = self._rt()
        self._memory(rt)
        self._write_cfg("recon")
        self._load(rt)
        value = rt.eval(f"(function() local R = EXPORTS.reader({BASE}) "
                        f"return R.ptr(0x3326d20) end)()")
        self.assertEqual(value, ACTORS)

    def test_recon_survives_bad_offsets(self) -> None:
        rt = self._rt()
        self._write_cfg("recon")
        # 不建假内存：所有读都会失败 —— 必须优雅降级，不能崩
        self._load(rt)
        status = (self.dir / "hd2_coyote_status.txt").read_text(encoding="utf-8")
        self.assertTrue(status.startswith("FAILED - "), status)
        self.assertIn("recon", status)
        log = (self.dir / "hd2_coyote_bridge.log").read_text(encoding="utf-8")
        self.assertIn("本地玩家未定位", log)

    def test_read_state_values(self) -> None:
        rt = self._rt()
        self._memory(rt, hp=62.5, hp_max=100.0, limb_mask=3, dead=0, bleeding=1)
        self._write_user_config("recon")  # 把偏移通过用户配置注入 PROFILES
        self._load(rt)
        st = rt.eval("(function() "
                     f"local r = EXPORTS.reader({BASE}) "
                     f"local p = {{ address = {PINGREC} }} "
                     "return EXPORTS.read_state(r, EXPORTS.profiles['steam_25480438'], p) "
                     "end)()")
        self.assertIsNotNone(st, rt.globals().FAKE_SENT)
        self.assertAlmostEqual(st["hp"], 62.5, places=2)
        self.assertAlmostEqual(st["hp_max"], 100.0, places=1)
        self.assertEqual([st["limbs"][i] for i in range(1, 4)], [1, 1, 0])
        self.assertEqual(st["bleeding"], 1)
        self.assertEqual(st["dead"], 0)

    def test_profile_without_offsets_refuses_live(self) -> None:
        rt = self._rt()
        self._memory(rt)
        self._write_user_config("live", with_offsets=False)
        self._load(rt)
        status = (self.dir / "hd2_coyote_status.txt").read_text(encoding="utf-8")
        self.assertTrue(status.startswith("FAILED - "), status)
        self.assertIn("hp", status)

    # ------------------------------------------------------------ 3. live + 帧钩子
    def test_live_mode_hooks_frame_function(self) -> None:
        rt = self._rt()
        self._memory(rt, hp=62.0, limb_mask=3)
        rt.execute("__hd2_test_tick = function() end")
        self._write_user_config("live")
        self._load(rt)

        status = (self.dir / "hd2_coyote_status.txt").read_text(encoding="utf-8")
        self.assertTrue(status.startswith("OK - "), status)
        self.assertIn("hook=__hd2_test_tick", status)

        rt.globals()["__hd2_test_tick"]()
        sent = lua_list(rt, "FAKE_SENT")
        states = [s for s in sent if '"ev":"state"' in s]
        self.assertEqual(len(states), 1, sent)
        payload = json.loads(states[0])
        self.assertAlmostEqual(payload["hp"], 62.0, places=2)
        self.assertAlmostEqual(payload["hp_max"], 100.0, places=1)
        self.assertEqual(payload["limbs"], [1, 1, 0])
        self.assertEqual(payload["dead"], 0)
        self.assertEqual(payload["v"], 1)

    def test_missing_frame_hook_reports_failed(self) -> None:
        rt = self._rt()
        self._memory(rt)
        self._write_user_config("live", hooks="{ '不存在的函数' }")
        self._load(rt)
        status = (self.dir / "hd2_coyote_status.txt").read_text(encoding="utf-8")
        self.assertTrue(status.startswith("FAILED - "), status)
        self.assertIn("每帧钩子", status)


@unittest.skipUnless(LUPA_OK, "需要 lupa（LuaJIT）才能跑 Lua 侧测试")
class TestLuaToPythonWire(unittest.TestCase):
    """真实链路：LuaJIT addon -> UDP -> Python HookSource -> 事件。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self._old_env = os.environ.get("HD2COYOTE_DIR")
        os.environ["HD2COYOTE_DIR"] = str(self.dir)
        self.port = free_udp_port()
        cfg = AppConfig()
        cfg.hook.port = self.port
        cfg.hook.timeout_s = 3.0
        self.src = HookSource(cfg)
        self.src.start()

    def tearDown(self) -> None:
        self.src.stop()
        if self._old_env is None:
            os.environ.pop("HD2COYOTE_DIR", None)
        else:
            os.environ["HD2COYOTE_DIR"] = self._old_env
        self.tmp.cleanup()

    def test_lua_state_packets_drive_python_events(self) -> None:
        rt = lj.LuaRuntime()
        rt.execute(FAKE.read_text(encoding="utf-8"))

        def poke(addr: int, expr: str) -> None:
            rt.execute(f"poke({addr}, {expr})")

        base, actors, rec = 0x140000000, 0x141000000, 0x142000000
        poke(base + 0x3326d20, f"u64le({actors})")
        poke(actors + 0x70, "u32le(1)")
        poke(actors + 0x110, f"u64le({rec})")
        poke(rec + 8, "u32le(4242)")
        poke(rec + 16, "u32le(7)")
        poke(rec + 0x20, "f32le(100.0)")
        poke(rec + 0x24, "f32le(100.0)")
        poke(rec + 0x28, "string.char(0)")
        poke(rec + 0x2C, "string.char(0)")

        # 把 addon 发的报文真发到 Python 的 UDP 端口
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        def forward(text: str) -> None:
            sock.sendto(str(text).encode("utf-8"), ("127.0.0.1", self.port))

        rt.globals().FAKE_FORWARD = forward
        (self.dir / "bridge_config.lua").write_text(
            "return {\n"
            f"  config = {{ mode = 'live', port = {self.port}, frame_hooks = {{ '__hd2_tick' }} }},\n"
            "  profiles = { steam_25480438 = { hp = 0x20, hp_max = 0x24, limb_mask = 0x28,"
            " dead = 0x2C } },\n"
            "}\n", encoding="utf-8")
        rt.execute("__hd2_tick = function() end")
        rt.execute(ADDON.read_text(encoding="utf-8"))

        tick = rt.globals()["__hd2_tick"]
        for _ in range(5):
            tick()
            time.sleep(0.03)
        time.sleep(0.2)
        self.src.poll()  # 第一帧只用来建立基线
        self.assertTrue(self.src.alive)
        self.assertEqual(self.src.state.build, "1.8.46015.0")

        poke(rec + 0x20, "f32le(62.0)")  # 掉 38% 血
        for _ in range(3):
            tick()
            time.sleep(0.03)
        time.sleep(0.15)
        events = self.src.poll()
        self.assertTrue(any(isinstance(e, Damage) for e in events), events)
        self.assertAlmostEqual(self.src.state.hp or 0.0, 0.62, places=2)
        sock.close()


if __name__ == "__main__":
    unittest.main()

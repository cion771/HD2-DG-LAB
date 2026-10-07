"""崩溃安全测试：**绝不对无效地址解引用**。

背景（真实事故）：addon 直接读游戏内存，偏移是某个构建的快照。偏移过期时
`ffi.string` 撞到未映射地址 = 进程级访问违例，`pcall` 挡不住，**游戏直接崩**。
现在每一次读之前都先用 VirtualQuery 验址（`range_readable`），这个文件就是守这条线的：

  * 未映射地址 -> 读返回 nil（而不是抛错/崩溃）
  * 越界读（区域边界之外）-> 同样拒绝
  * 垃圾指针 / 内核态地址 -> 定位本地玩家直接失败，给出原因
  * 拿不到 VirtualQuery -> recon 一个字节都不读，STATUS.txt 说明原因
"""

from __future__ import annotations

import os
import sys
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
UNMAPPED = 0x150000000
KERNELISH = 0xFFFF800000000000


@unittest.skipUnless(LUPA_OK, "需要 lupa（LuaJIT）才能跑 Lua 侧测试")
class TestCrashSafety(unittest.TestCase):
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

    def _rt(self, with_memory: bool = True, with_virtual_query: bool = True):
        rt = lj.LuaRuntime()
        rt.execute(FAKE.read_text(encoding="utf-8"))
        if not with_virtual_query:
            rt.execute("require('ffi').load('kernel32').Hd2Coyote_VirtualQuery = nil")
        if with_memory:
            rt.execute(f"map({PINGREC}, 0x100)")
            rt.execute(f"poke({BASE + 0x3326d20}, u64le({ACTORS}))")
            rt.execute(f"poke({ACTORS + 0x70}, u32le(1))")
            rt.execute(f"poke({ACTORS + 0x110}, u64le({PINGREC}))")
            rt.execute(f"poke({PINGREC + 8}, u32le(4242))")
            rt.execute(f"poke({PINGREC + 16}, u32le(7))")
            rt.execute(f"poke({PINGREC + 0x20}, f32le(62.0))")
        rt.execute("__hd2_tick = function() end")
        return rt

    def _load(self, rt) -> None:
        rt.globals()["EXPORTS"] = rt.compile(ADDON.read_text(encoding="utf-8"), "bridge")()

    def _cfg(self, mode: str = "recon", extra: str = "") -> None:
        """写 bridge_config.lua 指定模式（默认 safe，需要读内存的测试必须显式选 recon）。"""
        (self.dir / "bridge_config.lua").write_text(
            "return { config = { mode = '" + mode + "', frame_hooks = { '__hd2_tick' } }"
            + (", " + extra if extra else "") + " }\n", encoding="utf-8")

    def _status(self) -> str:
        path = self.dir / "hd2_coyote_status.txt"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    # ------------------------------------------------------------ 验址
    def test_read_unmapped_returns_nil(self) -> None:
        rt = self._rt()
        self._cfg("recon")          # 需要 init_ffi 才谈得上"验址"
        self._load(rt)
        value = rt.eval(f"(function() local R = EXPORTS.reader({BASE}) "
                        f"return R.u32(0x9999999) end)()")
        self.assertIsNone(value, "未映射地址必须返回 nil，不能抛错也不能读")
        self.assertGreater(rt.eval("FAKE_VQ_CALLS"), 0, "应当先经过 VirtualQuery 验址")

    def test_read_past_region_end_is_refused(self) -> None:
        rt = self._rt()
        self._cfg("recon")
        self._load(rt)
        # ping 记录只 map 了 0x100 字节；读 0x200 处应当被拒（真机上是越界）
        value = rt.eval(f"(function() local R = EXPORTS.reader({PINGREC}) "
                        f"return R.u32(0x200) end)()")
        self.assertIsNone(value)

    def test_garbage_pointer_does_not_crash(self) -> None:
        """ping actor 表指向未映射内存：定位失败，但**进程必须活着**。"""
        rt = self._rt()
        rt.execute(f"poke({BASE + 0x3326d20}, u64le({UNMAPPED}))")
        self._cfg("recon")
        self._load(rt)
        status = self._status()
        self.assertTrue(status.startswith("FAILED - "), status)
        self.assertIn("actor", status)

    def test_kernel_address_is_rejected_before_deref(self) -> None:
        rt = self._rt()
        rt.execute(f"poke({BASE + 0x3326d20}, u64le({KERNELISH}))")
        self._cfg("recon")
        self._load(rt)
        value = rt.eval(f"(function() local R = EXPORTS.reader({BASE}) "
                        f"return R.ptr(0x3326d20) end)()")
        self.assertEqual(value, KERNELISH, "指针本身能读出来")
        # 通过这个内核态指针去读：必须在解引用之前被拒
        self.assertIsNone(
            rt.eval(f"(function() local R = EXPORTS.reader({BASE}) "
                    f"local p = R.ptr(0x3326d20) "
                    f"return EXPORTS.reader(p).u32(0) end)()"),
            "内核态地址绝不能被解引用")
        self.assertIn("FAILED", self._status())

    def test_count_sanity_rejects_garbage(self) -> None:
        """读到"合法但错误"的数据时，靠合理性校验拒绝（不是靠崩溃）。"""
        rt = self._rt()
        rt.execute(f"poke({ACTORS + 0x70}, u32le(9999))")  # actor 数量离谱
        self._cfg("recon")
        self._load(rt)
        self.assertIn("异常", self._status())

    # ------------------------------------------------------------ 安全闸门
    def test_no_virtual_query_means_no_memory_reads(self) -> None:
        """拿不到 VirtualQuery 时：一个字节都不读，STATUS 说明原因。"""
        rt = self._rt(with_memory=False, with_virtual_query=False)
        self._cfg("recon")
        self._load(rt)
        status = self._status()
        self.assertTrue(status.startswith("FAILED - "), status)
        self.assertIn("VirtualQuery", status)
        log = (self.dir / "hd2_coyote_bridge.log").read_text(encoding="utf-8")
        self.assertIn("不进行任何内存读取", log)

    # ------------------------------------------------------------ 模式阶梯
    def test_safe_mode_touches_nothing(self) -> None:
        """safe 模式：只写日志，不碰 FFI、不读内存、不挂钩子、不发 UDP。"""
        rt = self._rt()          # 默认 mode = safe
        self._load(rt)
        status = self._status()
        self.assertTrue(status.startswith("OK - "), status)
        self.assertIn("safe 模式", status)
        self.assertEqual(rt.eval("FAKE_VQ_CALLS"), 0, "safe 模式不该调用 VirtualQuery")
        self.assertEqual(rt.eval("FAKE_PEEK_CALLS"), 0, "safe 模式不该读任何内存")
        self.assertEqual(len(rt.globals()["FAKE_SENT"]), 0, "safe 模式不发 UDP")
        self.assertEqual(rt.eval("EXPORTS.hook.installed"), False, "safe 模式不挂钩子")
        log = (self.dir / "hd2_coyote_bridge.log").read_text(encoding="utf-8")
        self.assertIn("生效模式 = safe", log)

    def test_plain_text_config_is_accepted(self) -> None:
        """纯文本 key = value 形式也必须能开模式（不依赖 loadstring）。"""
        rt = self._rt()
        (self.dir / "bridge_config.txt").write_text("mode = menu\n", encoding="utf-8")
        self._load(rt)
        status = self._status()
        self.assertTrue(status.startswith("OK - "), status)
        self.assertIn("menu 模式", status)
        log = (self.dir / "hd2_coyote_bridge.log").read_text(encoding="utf-8")
        self.assertIn("按纯文本 key = value 解析", log)
        self.assertIn("生效模式 = menu", log)

    def test_broken_config_is_reported_not_swallowed(self) -> None:
        """配置读不了/写错必须写进日志 —— 否则就是"文件在、却不生效、还没线索"。"""
        rt = self._rt()
        (self.dir / "bridge_config.lua").write_text("return { config = { mode = 'menu'\n",
                                                    encoding="utf-8")  # 少一个 }
        self._load(rt)
        log = (self.dir / "hd2_coyote_bridge.log").read_text(encoding="utf-8")
        self.assertIn("语法错误", log)
        self.assertIn("生效模式 = safe", log)   # 回退到最安全档
        self.assertIn("safe 模式", self._status())

    def test_unknown_mode_falls_back_to_safe(self) -> None:
        rt = self._rt()
        (self.dir / "bridge_config.lua").write_text(
            "return { config = { mode = 'menuu' } }\n", encoding="utf-8")
        self._load(rt)
        log = (self.dir / "hd2_coyote_bridge.log").read_text(encoding="utf-8")
        self.assertIn("未知 mode=menuu", log)
        self.assertIn("生效模式 = safe", log)

    def test_missing_config_is_logged_with_reason(self) -> None:
        rt = self._rt()
        self._load(rt)
        log = (self.dir / "hd2_coyote_bridge.log").read_text(encoding="utf-8")
        self.assertIn("未找到用户配置", log)

    def test_webui_generated_config_is_parsed_as_lua(self) -> None:
        """回归：Web 控制台写出的文件以 `--` 注释开头 —— 必须仍按 Lua 形式解析。

        （之前只看"第一个非空 token 是否 return"，`--` 不是字母 → 误判成纯文本 →
          mode 变成带引号的 "'live'" → 未知档位 → 回退 safe。用户看到的现象就是
          "配置写了却一直不生效"。）
        """
        from hd2coyote.webui import render_bridge_config

        rt = self._rt()
        (self.dir / "bridge_config.lua").write_text(
            render_bridge_config({"mode": "live", "port": 47781, "interval": 0.2,
                                  "offsets": {"hp": 0x2C, "hp_max": 0x30, "limb_mask": 0x38,
                                              "limb_shift": 0, "dead": -1}}),
            encoding="utf-8")
        self._load(rt)
        log = (self.dir / "hd2_coyote_bridge.log").read_text(encoding="utf-8")
        self.assertIn("生效模式 = live", log)
        self.assertNotIn("未知 mode", log)
        self.assertNotIn("按纯文本", log)
        self.assertEqual(rt.eval("EXPORTS.config.port"), 47781)
        self.assertAlmostEqual(rt.eval("EXPORTS.config.interval"), 0.2)
        self.assertEqual(rt.eval("EXPORTS.profiles['steam_25480438'].hp"), 0x2C)
        self.assertIsNone(rt.eval("EXPORTS.profiles['steam_25480438'].dead"))

    def test_plain_text_with_comments_and_quotes(self) -> None:
        rt = self._rt()
        (self.dir / "bridge_config.txt").write_text(
            "-- 这是注释\nmode = 'menu'   -- 行内注释\nport = 47782\n", encoding="utf-8")
        self._load(rt)
        log = (self.dir / "hd2_coyote_bridge.log").read_text(encoding="utf-8")
        self.assertIn("生效模式 = menu", log)
        self.assertEqual(rt.eval("EXPORTS.config.port"), 47782)

    def test_net_mode_sends_hello_without_hook_or_memory(self) -> None:
        """net 模式：只有 FFI + UDP（hello），不挂钩子、不读游戏内存。"""
        rt = self._rt()
        self._cfg("net")
        self._load(rt)
        status = self._status()
        self.assertTrue(status.startswith("OK - "), status)
        self.assertIn("net 模式", status)
        sent = [rt.globals()["FAKE_SENT"][i]
                for i in range(1, len(rt.globals()["FAKE_SENT"]) + 1)]
        self.assertTrue(any('"ev":"hello"' in s for s in sent), sent)
        self.assertTrue(any('"mode":"net"' in s for s in sent), sent)
        self.assertEqual(rt.eval("FAKE_VQ_CALLS"), 0, "net 模式不读游戏内存")
        self.assertEqual(rt.eval("FAKE_PEEK_CALLS"), 0, "net 模式不读任何内存")
        self.assertEqual(rt.eval("EXPORTS.hook.installed"), False, "net 模式不挂钩子")

    def test_menu_mode_sends_hello_but_no_memory_reads(self) -> None:
        """menu 模式：UDP + 菜单就绪，但一个字节的游戏内存都不读。"""
        rt = self._rt()
        self._cfg("menu")
        self._load(rt)
        status = self._status()
        self.assertTrue(status.startswith("OK - "), status)
        self.assertIn("menu 模式", status)
        self.assertEqual(rt.eval("FAKE_VQ_CALLS"), 0, "menu 模式不该读游戏内存")
        self.assertEqual(rt.eval("FAKE_PEEK_CALLS"), 0, "menu 模式不该读任何内存")
        sent = [rt.globals()["FAKE_SENT"][i]
                for i in range(1, len(rt.globals()["FAKE_SENT"]) + 1)]
        self.assertTrue(any('"ev":"hello"' in s for s in sent), sent)
        self.assertTrue(rt.eval("EXPORTS.hook.installed"), "menu 模式要挂钩子以便补注册菜单")

    def test_uses_explicit_dll_namespaces(self) -> None:
        """socket/sendto 在 ws2_32 里：必须 ffi.load 拿命名空间，
        走 ffi.C 会解析不到符号 → UDP 静默失效（实测隐患）。"""
        source = ADDON.read_text(encoding="utf-8")
        code = "\n".join(line.split("--")[0] for line in source.splitlines())
        self.assertIn("ffi.load('ws2_32')", code)
        self.assertIn("ffi.load('kernel32')", code)
        self.assertNotIn("ffi.C.", code, "不应再依赖 ffi.C 解析这些符号")
        self.assertIn("WS2.Hd2Coyote_sendto", code)
        self.assertIn("K32.Hd2Coyote_VirtualQuery", code)

    def test_no_os_execute_in_addon(self) -> None:
        """启动路径里绝不能有 os.execute：它会同步 spawn cmd.exe，
        被杀软/GameGuard 拦住就是"游戏停止响应 + 黑屏"（实测踩过）。"""
        code = "\n".join(line.split("--")[0]
                         for line in ADDON.read_text(encoding="utf-8").splitlines())
        self.assertNotIn("os.execute", code)
        self.assertNotIn("mkdir", code)
        self.assertNotIn("io.popen", code)

    def test_recon_still_works_when_addresses_are_valid(self) -> None:
        """正常情况不能被安全闸门误伤。"""
        rt = self._rt()
        self._cfg("recon")
        self._load(rt)
        status = self._status()
        self.assertTrue(status.startswith("OK - "), status)
        report = (self.dir / "recon_report.txt").read_text(encoding="utf-8")
        self.assertIn("local_player_entity=4242", report)
        self.assertIn("anchor_hex_dump:", report)
        self.assertGreater(rt.eval("FAKE_PEEK_CALLS"), 0)

    def test_private_ffi_names_used(self) -> None:
        """加载器文档要求：Windows 函数用自己的私有名 + __asm__ 别名声明。"""
        source = ADDON.read_text(encoding="utf-8")
        for name in ("Hd2Coyote_GetModuleHandleA", "Hd2Coyote_VirtualQuery",
                     "Hd2Coyote_socket", "Hd2Coyote_sendto"):
            self.assertIn(name, source)
        self.assertIn('__asm__("VirtualQuery")', source)
        # 不能再出现裸的共享名声明
        self.assertNotIn("void* GetModuleHandleA(", source)
        self.assertNotIn("unsigned long long socket(", source)


if __name__ == "__main__":
    unittest.main()

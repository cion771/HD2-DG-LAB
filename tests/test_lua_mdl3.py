"""MDL3 重写后的游戏内桥离线验证（LuaJIT 2.1 + 假 ffi/假内存/假 ctx）。

验的是"重写后仍然对"的东西：
  * mod.lua / entry.lua 两条入口装载同一套模块（本地文件夹 / addon 资源两种形态）
  * safe 档真的什么都不做（0 次虚拟内存查询、0 个包）
  * mock 源把 state 报文写到文件广播，Python 侧解析路径认它
  * direct 源在假 game.dll 映像上定位本地玩家、读 HP/肢体/阵亡，发 UDP
  * 配置钳制、采样限频、JSON 编码
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
ADDON = ROOT / "addon"
FAKE = ROOT / "tests" / "lua" / "fake_game.lua"

BOOTSTRAP = r"""
NOW = 0
os.clock = function() return NOW end

function make_ctx(dir, settings)
    local logs = {}
    local ctx = { dir = dir, settings = settings }
    ctx.log = function(text) logs[#logs + 1] = tostring(text) end
    return ctx, logs
end

function load_mod(dir, settings)
    local f = assert(io.open(dir .. '/mod.lua', 'rb'))
    local src = f:read('*a'); f:close()
    local chunk = assert(loadstring(src, '@mod.lua'))
    local ctx, logs = make_ctx(dir, settings)
    CTX_LOGS = logs
    return chunk(ctx)
end

function tick(mod, n, dt)
    dt = dt or 0.05
    for _ = 1, (n or 1) do
        NOW = NOW + dt
        if mod.on_update then mod.on_update(nil, dt) end
    end
end

function sent_count() return #FAKE_SENT end
function sent(i) return FAKE_SENT[i] end
function last_sent() return FAKE_SENT[#FAKE_SENT] end

function load_mod_ctxread(dir)
    -- 模拟 MDL3 原生宿主：没有 ffi，但 ctx 提供 read/module
    package.loaded.ffi = nil
    local real_require = require
    function require(name)
        if name == 'ffi' then error('ffi unavailable (test host)') end
        return real_require(name)
    end
    local ctx = {
        dir = dir,
        log = function(t) end,
        read = function(addr, n) return peek(addr, n) end,
        module = function(name) if name == 'game.dll' then return FAKE_BASE end end,
    }
    local f = assert(io.open(dir .. '/mod.lua', 'rb'))
    local src = f:read('*a'); f:close()
    local chunk = assert(loadstring(src, '@mod.lua'))
    return chunk(ctx)
end

function require_shim(addon_dir)
    local real = require
    function require(name)
        local rel = name:match('^mods/hd2coyote/hd2_coyote/(.+)$')
        if not rel then return real(name) end
        local cached = package.loaded[name]
        if cached ~= nil then return cached end
        local dir, leaf = rel:match('^(.-)_(.+)$')
        local path
        if dir then
            path = addon_dir .. '/' .. dir .. '/' .. leaf .. '.lua'
        else
            path = addon_dir .. '/' .. rel .. '.lua'
        end
        local f = assert(io.open(path, 'rb'), 'no module ' .. path)
        local src = f:read('*a'); f:close()
        local chunk = assert(loadstring(src, '@' .. name))
        local value = chunk()
        package.loaded[name] = value
        return value
    end
end

function load_entry(dir)
    local f = assert(io.open(dir .. '/entry.lua', 'rb'))
    local src = f:read('*a'); f:close()
    local chunk = assert(loadstring(src, '@entry.lua'))
    chunk()
    return _G.update
end

-- 独立装载一个 core 模块（不经 bridge），用于单测
function load_module(dir, rel, env)
    local f = assert(io.open(dir .. '/' .. rel .. '.lua', 'rb'))
    local src = f:read('*a'); f:close()
    local chunk = assert(loadstring(src, '@' .. rel))
    return chunk(env)()
end
"""


@unittest.skipUnless(LUPA_OK, "需要 lupa（LuaJIT）")
class TestMdl3Bridge(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self._old = os.environ.get("HD2COYOTE_DIR")
        os.environ["HD2COYOTE_DIR"] = str(self.dir)
        self.rt = lj.LuaRuntime()
        self.rt.execute(FAKE.read_text(encoding="utf-8"))
        self.rt.execute(BOOTSTRAP)
        self.addon_dir = str(ADDON).replace("\\", "/")
        self.rt.globals().ADDON_DIR = self.addon_dir

    def tearDown(self) -> None:
        if self._old is None:
            os.environ.pop("HD2COYOTE_DIR", None)
        else:
            os.environ["HD2COYOTE_DIR"] = self._old
        self.tmp.cleanup()

    # ---------------------------------------------------------------- 工具
    @staticmethod
    def lua_value(value) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return repr(value)
        return json.dumps(str(value))

    def write_config(self, **values) -> None:
        body = "return { config = { " + ", ".join(
            f"{k} = {self.lua_value(v)}" for k, v in values.items()) + " } }\n"
        (self.dir / "bridge_config.lua").write_text(body, encoding="utf-8")

    def load_mod(self):
        self.rt.execute(f"mod = load_mod('{self.addon_dir}')")
        return self.rt.globals().mod

    def tick(self, n: int, dt: float = 0.05) -> None:
        self.rt.globals().mod = self.rt.globals().mod
        self.rt.execute(f"tick(mod, {n}, {dt})")

    def sent(self) -> list[dict]:
        out = []
        for i in range(1, int(self.rt.eval("sent_count()")) + 1):
            raw = self.rt.eval(f"sent({i})")
            if raw:
                out.append(json.loads(raw))
        return out

    # ---------------------------------------------------------------- 用例
    def test_safe_mode_touches_nothing(self) -> None:
        self.write_config(mode="safe")
        self.load_mod()
        self.tick(20)
        self.assertEqual(self.rt.eval("sent_count()"), 0)
        self.assertEqual(self.rt.eval("FAKE_VQ_CALLS"), 0)
        self.assertFalse((self.dir / "bridge_state.json").exists())

    def test_mock_file_transport_writes_state(self) -> None:
        self.write_config(mode="live", mock=True, transport="file", interval=0.05)
        self.load_mod()
        self.tick(30)
        path = self.dir / "bridge_state.json"
        self.assertTrue(path.exists(), "文件广播没有落盘")
        packet = json.loads(path.read_text(encoding="utf-8").strip())
        self.assertEqual(packet["v"], 1)
        self.assertEqual(packet["ev"], "state")
        self.assertEqual(packet["hp_max"], 100)
        first = packet["hp"]
        self.assertLessEqual(first, 100)
        self.tick(30)
        second = json.loads(path.read_text(encoding="utf-8").strip())["hp"]
        self.assertLess(second, first, "mock 源应当持续掉血")
        self.assertEqual(len(packet["limbs"]), 6)

    def test_direct_source_reads_fake_game(self) -> None:
        self.rt.execute("seed_current_game(62, 3, 0)")
        self.write_config(mode="live", transport="udp", interval=0.05)
        self.load_mod()
        packet = None
        for _ in range(40):
            self.tick(1)
            states = [p for p in self.sent() if p.get("ev") == "state"]
            if states:
                packet = states[-1]
                break
        self.assertIsNotNone(packet, "direct 源没有产出 state 报文")
        self.assertEqual(packet["hp"], 62)
        self.assertEqual(packet["hp_max"], 100)
        self.assertEqual(packet["limbs"], [1, 1, 0, 0, 0, 0])
        self.assertEqual(packet["dead"], 0)
        self.assertEqual(self.rt.eval("FAKE_MODULE_NAME"), "game.dll")

    def test_ctx_read_path_without_ffi(self) -> None:
        """MDL3 原生 ctx.read/ctx.module：没有 ffi 也要能读。"""
        self.rt.execute("seed_current_game(37, 0, 0)")
        # 没有 ffi 就发不了 UDP —— 这正是文件广播存在的理由
        self.write_config(mode="live", transport="file", interval=0.05)
        self.rt.execute(f"mod = load_mod_ctxread('{self.addon_dir}')")
        packet = None
        path = self.dir / "bridge_state.json"
        for _ in range(20):
            self.tick(1)
            if path.exists():
                try:
                    candidate = json.loads(path.read_text(encoding="utf-8").strip())
                except ValueError:
                    continue
                if candidate.get("ev") == "state":
                    packet = candidate
                    break
        self.assertIsNotNone(packet, "ctx.read 路径没有产出状态")
        self.assertEqual(packet["hp"], 37)
        self.assertEqual(packet["hp_max"], 100)
        self.assertGreater(self.rt.eval("FAKE_PEEK_CALLS"), 0)
        self.assertTrue(self.rt.eval("package.loaded.ffi == nil"), "测试前提：ffi 必须不可用")

    def test_hello_and_state_share_protocol_version(self) -> None:
        self.write_config(mode="live", mock=True, transport="udp", interval=0.05)
        self.load_mod()
        self.tick(6)
        packets = self.sent()
        self.assertTrue(packets)
        self.assertTrue(all(p["v"] == 1 for p in packets))
        self.assertEqual(packets[0]["ev"], "hello")
        self.assertTrue(any(p["ev"] == "state" for p in packets))

    def test_config_clamps_bad_values(self) -> None:
        self.write_config(mode="bogus", port=0, interval=999, transport="carrier-pigeon")
        self.rt.execute("MOD = load_module(ADDON_DIR, 'core/config', {})")
        self.rt.execute("MOD.init(nil)")
        self.assertEqual(self.rt.eval("MOD.get('mode')"), "safe")
        self.assertEqual(self.rt.eval("MOD.get('port')"), 1)
        self.assertEqual(self.rt.eval("MOD.get('interval')"), 5.0)
        self.assertEqual(self.rt.eval("MOD.get('transport')"), "auto")

    def test_rate_limit_respects_interval(self) -> None:
        self.write_config(mode="live", mock=True, transport="udp", interval=0.2)
        self.load_mod()
        self.tick(20)  # 20 × 50ms = 1s
        states = [p for p in self.sent() if p.get("ev") == "state"]
        self.assertLessEqual(len(states), 6, "采样频率没有被 interval 限制")
        self.assertGreaterEqual(len(states), 1)

    def test_entry_loads_via_addon_resources(self) -> None:
        self.write_config(mode="live", mock=True, transport="udp", interval=0.05)
        self.rt.execute(f"require_shim('{self.addon_dir}')")
        self.rt.execute(f"upd = load_entry('{self.addon_dir}')")
        self.assertTrue(self.rt.eval("type(upd) == 'function'"), "entry 没有接管 _G.update")
        for _ in range(10):
            self.rt.execute("NOW = NOW + 0.05; upd(0.05)")
        packets = self.sent()
        self.assertTrue(packets, "addon 形态没有发出任何报文")
        self.assertTrue(any(p.get("ev") == "state" for p in packets))

    def test_json_encoder_is_valid_json(self) -> None:
        self.rt.execute("JSON = load_module(ADDON_DIR, 'core/json', {})")
        raw = self.rt.eval("""JSON.encode({
            {'v', 1}, {'ev','state'}, {'t', 12.5}, {'hp', 62},
            {'limbs', {1,1,0,0,0,0}}, {'dead', false}, {'note', 'a"b'},
        })""")
        packet = json.loads(raw)
        self.assertEqual(packet["v"], 1)
        self.assertEqual(packet["limbs"], [1, 1, 0, 0, 0, 0])
        self.assertIs(packet["dead"], False)
        self.assertEqual(packet["note"], 'a"b')


if __name__ == "__main__":
    unittest.main()

-- 离线仿真用的「假 FFI + 假内存」桩。
--
-- 为什么需要它：addon 在游戏里用 FFI 直接读进程内存。本地测试如果放真 FFI 过去，
-- ffi.string 会从 Python 进程里乱读地址 —— 轻则报错重则崩溃。
-- 这里把 package.loaded.ffi 换成类型严格的假实现，并且**模拟 VirtualQuery**：
--   * 内存按「区域」组织（map / poke），区域外的一律不可读；
--   * 读越界或读未映射地址 → 假 ffi.string 抛错，正是要抓的场景；
--   * sendto 只把报文记进 FAKE_SENT，可选转发给 Python 做联调。
-- 于是 addon 的「验址 -> 读内存 -> 定位玩家 -> 发 UDP」全流程都能离线验证。

FAKE_BASE = 0x140000000
FAKE_REGIONS = {}      -- { addr, size, data }，按 addr 升序
FAKE_SENT = {}
FAKE_TICK = 0
FAKE_FORWARD = nil
FAKE_MBI_SIZE = 48
FAKE_VQ_CALLS = 0      -- VirtualQuery 被调用次数（安全测试用：safe 模式必须是 0）
FAKE_PEEK_CALLS = 0    -- 假内存实际被读次数

local function region_of(addr)
    for _, r in ipairs(FAKE_REGIONS) do
        if addr >= r.addr and addr < r.addr + r.size then return r end
    end
    return nil
end

local function insert_region(r)
    table.insert(FAKE_REGIONS, r)
    table.sort(FAKE_REGIONS, function(a, b) return a.addr < b.addr end)
    return r
end

--: 声明一段「已提交且可读」的内存（内容全 0）
function map(addr, size)
    return region_of(addr) or insert_region({ addr = addr, size = size,
                                              data = string.rep('\0', size) })
end

--: 写入字节：落在已有区域内就近改写，否则新建一个正好这么大的区域
function poke(addr, bytes)
    local r = region_of(addr)
    if not r then
        return insert_region({ addr = addr, size = #bytes, data = bytes })
    end
    local off = addr - r.addr
    if off + #bytes > r.size then error('poke 越过区域边界') end
    r.data = r.data:sub(1, off) .. bytes .. r.data:sub(off + #bytes + 1)
    return r
end

--: 读取字节：未映射或越界一律抛错（模拟访问违例）
function peek(addr, n)
    FAKE_PEEK_CALLS = FAKE_PEEK_CALLS + 1
    local r = region_of(addr)
    if not r then error(string.format('假内存未映射 0x%X（真机上是访问违例）', addr)) end
    local off = addr - r.addr
    if off + n > r.size then
        error(string.format('读取越过区域边界 0x%X +%d > %d', addr, n, r.size))
    end
    return r.data:sub(off + 1, off + n)
end

function u32le(v)
    v = math.floor(v)
    return string.char(v % 256, math.floor(v / 256) % 256,
                       math.floor(v / 65536) % 256, math.floor(v / 16777216) % 256)
end

function u64le(v)
    local out = {}
    for i = 1, 8 do
        out[i] = string.char(v % 256)
        v = math.floor(v / 256)
    end
    return table.concat(out)
end

-- IEEE754 单精度小端编码（LuaJIT 没有 string.pack）
function f32le(x)
    if x == 0 then return string.char(0, 0, 0, 0) end
    local sign = 0
    if x < 0 then sign = 128; x = -x end
    local exp = math.floor(math.log(x) / math.log(2))
    local mant = x / 2 ^ exp - 1
    if mant < 0 then
        exp = exp - 1
        mant = x / 2 ^ exp - 1
    end
    local e = exp + 127
    local m = math.floor(mant * 8388608 + 0.5)
    if m >= 8388608 then m = 0; e = e + 1 end
    local b1 = m % 256
    local b2 = math.floor(m / 256) % 256
    local b3 = math.floor(m / 65536) % 128 + (e % 2) * 128
    local b4 = sign + math.floor(e / 2)
    return string.char(b1, b2, b3, b4)
end

function ptr_at(addr)
    local s = peek(addr, 8)
    local v = 0
    for i = 8, 1, -1 do v = v * 256 + s:byte(i) end
    return v
end

local fake = {}
fake.cdef = function(_) end
fake.cast = function(ctype, value)
    local n = tonumber(value)
    if n then return n end
    if type(value) == 'table' then return value.__addr or 0 end
    return 0
end
fake.string = function(ptr, n)
    return peek(tonumber(ptr) or (type(ptr) == 'table' and ptr.__addr), n)
end
fake.new = function(ctype, _)
    if type(ctype) == 'string' and ctype:find('Hd2CoyoteMBI') then
        local slot = { BaseAddress = 0, RegionSize = 0, State = 0, Protect = 0 }
        -- 真 LuaJIT 里这是 Hd2CoyoteMBI[1]，取 [0] 或 [1] 都该拿到同一个元素
        return setmetatable({}, { __index = function(_, k)
            if k == 0 or k == 1 then return slot end
        end })
    end
    return {}
end
fake.sizeof = function(_) return FAKE_MBI_SIZE end

--: kernel32 命名空间（addon 用 ffi.load('kernel32') 拿它）
FAKE_K32 = {
    Hd2Coyote_GetModuleHandleA = function(_) return FAKE_BASE end,
    Hd2Coyote_GetTickCount64 = function()
        FAKE_TICK = FAKE_TICK + 1000  -- 每次调用前进 1 秒，便于驱动上报节流
        return FAKE_TICK
    end,
    -- 模拟 VirtualQuery：只认已 map 的区域；未映射返回 0（真机上就是不可读）
    Hd2Coyote_VirtualQuery = function(addr, buf, _)
        FAKE_VQ_CALLS = FAKE_VQ_CALLS + 1
        local r = region_of(tonumber(addr))
        if not r then return 0 end
        local slot = buf and buf[1]
        if slot then
            slot.BaseAddress = r.addr
            slot.RegionSize = r.size
            slot.State = 0x1000     -- MEM_COMMIT
            slot.Protect = 0x04     -- PAGE_READWRITE
        end
        return FAKE_MBI_SIZE
    end,
}

--: ws2_32 命名空间（socket/sendto 在这里，不在 exe/kernel32）
FAKE_WS2 = {
    Hd2Coyote_socket = function(_, _, _) return 0x1234 end,
    Hd2Coyote_sendto = function(_, buf, len, _, _, _)
        FAKE_SENT[#FAKE_SENT + 1] = buf
        if FAKE_FORWARD then FAKE_FORWARD(buf) end
        return len
    end,
}

fake.load = function(name)
    if name == 'kernel32' then return FAKE_K32 end
    if name == 'ws2_32' then return FAKE_WS2 end
    error('fake ffi.load: 未知 DLL ' .. tostring(name))
end
fake.C = FAKE_K32   -- 兼容：addon 不应再依赖 ffi.C（有测试守着）
package.loaded.ffi = fake

-- 假 Mod Options Menu：按官方 README 的 api 1 / version 3 语义实现，
-- 用来离线验证注册参数、上限、以及 APPLY 之后值有没有真的落到配置里。
function make_fake_menu(opts)
    opts = opts or {}
    local m = { api = 1, version = 3, max_mods = 112, max_options = 32 }
    m.registered = {}
    m.values = {}
    m.changes = {}
    m.fail_after = opts.fail_after
    m.ready_calls = 0

    function m.register_option(id, spec)
        if type(id) ~= 'string' or #id > 96 then return false, 'invalid id' end
        if m.fail_after and #m.registered >= m.fail_after then
            return false, 'mod already has 32 options'
        end
        m.registered[#m.registered + 1] = { id = id, spec = spec }
        if m.values[id] == nil then m.values[id] = spec.default end
        return true
    end

    function m.get(id) return m.values[id] end

    function m.set(id, value) m.values[id] = value end

    function m.on_change(id, fn) m.changes[id] = fn end

    function m.ready()
        m.ready_calls = m.ready_calls + 1
        return true
    end

    -- 测试辅助：模拟玩家在 MODS 页改完按 APPLY
    function m.apply(id, value)
        m.values[id] = value
        local fn = m.changes[id]
        if fn then
            fn(value, id)
            return true
        end
        return false, 'no callback'
    end

    return m
end

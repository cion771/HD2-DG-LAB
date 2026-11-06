-- HD2-Coyote 游戏内桥 · MDL3 实时 mod 入口
-- 前置：MDL3 3.0.0-dev.x（Lua mod 加载器）—— 不再需要 Bingus Shared Loader。
-- 装法 A（推荐）：整个文件夹放进 %LOCALAPPDATA%\MDL\Helldivers2\Mods\hd2coyote\
-- 装法 B：Arsenal / HD2MM 导入 addon 包（走 entry.lua 那条路）
-- 说明：本 mod 只读游戏状态；不写游戏内存、不改游戏数据、不向外部网络发包。
local ctx = ...

local VERSION = '0.8.0'
local NAME = 'mods/hd2coyote/hd2_coyote'

local function dir_of(ctx)
    if type(ctx) == 'table' and type(ctx.dir) == 'string' and ctx.dir ~= '' then return ctx.dir end
    return '.'
end

local H = {
    VERSION = VERSION,
    NAME = NAME,
    PROTOCOL = 1,
    TAG = 'hd2coyote',
    ctx = ctx,
}

local function say(text)
    if type(ctx) == 'table' and type(ctx.log) == 'function' then
        pcall(ctx.log, '[hd2coyote] ' .. tostring(text))
    end
    pcall(print, '[hd2coyote] ' .. tostring(text))
end

-- 引导装载器：它自己不能靠 core/load 加载，所以这两条路要就地写一次
local function bootstrap(dir)
    if dir and dir ~= '' and dir ~= '.' then
        local path = dir .. '/core/load.lua'
        local f = io.open(path, 'rb')
        if not f then f = io.open(dir .. '\\core\\load.lua', 'rb') end
        if f then
            local src = f:read('*a')
            f:close()
            local chunk, err = loadstring(src, '@mods/hd2coyote/hd2_coyote/core_load')
            if not chunk then return nil, err end
            local ok, factory = pcall(chunk)
            if not ok or type(factory) ~= 'function' then return nil, factory end
            return factory(H).make({ dir = dir, name = NAME })
        end
    end
    local ok, factory = pcall(require, NAME .. '/core_load')
    if not ok or type(factory) ~= 'function' then
        return nil, 'cannot load core_load: ' .. tostring(factory)
    end
    return factory(H).make({ name = NAME })
end

local function degraded(why)
    say('not started: ' .. tostring(why))
    return {
        name = 'HD2 Coyote Bridge (MDL3)',
        version = VERSION,
        author = 'hd2-coyote',
        on_enable = function() end,
        on_update = function() end,
        on_disable = function() end,
    }
end

local dir = dir_of(ctx)
local load, load_err = bootstrap(dir)
if not load then return degraded('loader: ' .. tostring(load_err)) end

local ok, bridge = pcall(function()
    local B = load('bridge')
    B.init(ctx, load)
    return B
end)
if not ok then return degraded('bridge init: ' .. tostring(bridge)) end

local active = true
local last_dt = nil

return {
    name = 'HD2 Coyote Bridge (MDL3)',
    version = VERSION,
    author = 'hd2-coyote',
    options = {
        { key = 'enabled', label = '启用状态上报', type = 'toggle', default = true },
        { key = 'mode', label = '运行模式', type = 'choice', default = 'safe',
          choices = { 'safe', 'net', 'recon', 'live' } },
        { key = 'interval', label = '上报间隔（秒）', type = 'number', default = 0.10,
          min = 0.05, max = 2.0, step = 0.05 },
        { key = 'port', label = 'UDP 端口（控制器）', type = 'number', default = 47777,
          min = 1024, max = 65535, step = 1 },
        { key = 'mock', label = '合成数据（不开游戏试链路）', type = 'toggle', default = false },
    },

    on_enable = function()
        active = true
        H.log.log('MDL3 启用（mode=' .. tostring(H.config.get('mode')) .. '）', true)
        if H.sampler and H.config.get('mode') ~= 'safe' then
            H.sampler.hello('enabled')
        end
    end,

    on_update = function(_, dt)
        if not active then return end
        last_dt = dt
        -- MDL3 的 on_update 就是每帧回调：不再需要包装游戏全局 update
        bridge.guard(bridge.update, bridge, dt)
    end,

    on_setting = function(_, key, value)
        H.log.log('选项变更：' .. tostring(key) .. '=' .. tostring(value))
    end,

    on_disable = function()
        active = false
        bridge.shutdown('mdl3 disable')
    end,
}

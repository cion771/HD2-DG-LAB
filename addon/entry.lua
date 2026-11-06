-- HD2-Addon: mods/hd2coyote/hd2_coyote
--
-- HD2-Coyote 游戏内桥 · addon 入口（Arsenal / HD2MM 导入的那一张）
-- 前置：MDL3 3.0.0-dev.x —— 它自带启动加载器，发现并启动 addon 的方式与 BSL 相同。
-- 这条路上没有 on_update，每帧只能包装游戏全局 update；配置只读
-- %LOCALAPPDATA%\hd2coyote\bridge_config.lua（拿不到 MDL3 的选项面板）。
--
-- ★ MDL3 的 _G 可能是沙箱表（__index / __newindex 代理到真正的全局表）：
--   只 rawget 会拿不到游戏已有的 update，只 rawset 又可能写不到游戏真正读的那张表。
--   所以读用 rawget + 索引兜底，写用 rawset + 普通赋值各来一次。
local VERSION = '0.8.0'
local NAME = 'mods/hd2coyote/hd2_coyote'

local H = {
    VERSION = VERSION,
    NAME = NAME,
    PROTOCOL = 1,
    TAG = 'hd2coyote',
    ctx = nil,
}

local function say(text) pcall(print, '[hd2coyote] ' .. tostring(text)) end

-- addon 的 ... 在 MDL3 下可能是 ctx 表（也可能什么都没有）：能用就用
local arg = ...
if type(arg) == 'table' and (arg.dir or arg.log or arg.settings) then
    H.ctx = arg
end

local function get_global(name)
    local ok, value = pcall(rawget, _G, name)
    if ok and value ~= nil then return value, 'rawget' end
    local ok2, value2 = pcall(function() return _G[name] end)
    if ok2 and value2 ~= nil then return value2, 'index' end
    return nil, nil
end

local function set_global(name, fn)
    local raw_ok = pcall(rawset, _G, name, fn)
    local idx_ok = pcall(function() _G[name] = fn end)
    return raw_ok or idx_ok
end

local ok_loader, factory = pcall(require, NAME .. '/core_load')
if not ok_loader or type(factory) ~= 'function' then
    say('not started: cannot load core_load: ' .. tostring(factory))
    return
end
local load = factory(H).make({ name = NAME })

local ok, bridge = pcall(function()
    local B = load('bridge')
    B.init(H.ctx, load)
    return B
end)
if not ok then
    say('not started: bridge init: ' .. tostring(bridge))
    return
end

local previous_update, how = get_global('update')
set_global('update', function(...)
    bridge.guard(bridge.update, bridge, ...)
    if type(previous_update) == 'function' then return previous_update(...) end
end)
H.log.log('每帧钩子：update（' .. tostring(how or '新建') .. '）', true)

local previous_shutdown = get_global('shutdown')
set_global('shutdown', function(...)
    pcall(bridge.shutdown, bridge, 'game shutdown')
    if type(previous_shutdown) == 'function' then return previous_shutdown(...) end
end)

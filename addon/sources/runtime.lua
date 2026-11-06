-- sources/runtime.lua —— HD2Runtime（SkyeShade）公开只读 API
-- 它自己负责适配游戏构建，所以这条路不随每次更新改偏移；装了就用，没装就降级到 direct。
-- 注意：MDL3 下是否还能 require 到它，取决于 HD2Runtime 是否以 addon 形式被加载 ——
-- 失败必须安静降级，不能把桥带崩。
return function(H)
    local M = { name = 'runtime' }
    local compat, log

    function M.setup()
        compat = H.compat
        log = H.log
    end

    function M.available()
        if M.hd2 then return true, 'HD2Runtime 已加载（' .. tostring(M.version or '?') .. '）' end
        local ok, hd2 = pcall(require, 'mods/skyeshade/hd2runtime')
        if not ok or type(hd2) ~= 'table' then
            return false, 'HD2Runtime 未安装或未加载'
        end
        if type(hd2.local_player) ~= 'function' or type(hd2.build) ~= 'function' then
            return false, 'HD2Runtime API 不完整（需要 0.31+）'
        end
        local ok_build, status, info = pcall(function() return hd2.build() end)
        if ok_build and status == 'mismatched' then
            return false, 'HD2Runtime 不支持当前构建：' .. tostring(info and info.reason or 'mismatched')
        end
        M.hd2 = hd2
        M.version = hd2.version
        return true, 'HD2Runtime ' .. tostring(hd2.version or '?')
    end

    function M.read_state()
        local hd2 = M.hd2
        if not hd2 then return nil, 'HD2Runtime 未初始化' end
        local ok, st, why = pcall(function()
            local player = hd2.local_player()
            if not player then return nil, '本地玩家尚未生成' end
            local hp = player:health()
            local hp_max = player:max_health()
            if type(hp) ~= 'number' or type(hp_max) ~= 'number' or hp_max <= 0 then
                return nil, '本地玩家生命状态尚不可读'
            end
            local alive = player:is_alive()
            return {
                hp = math.max(0, hp), hp_max = hp_max,
                limbs = { 0, 0, 0, 0, 0, 0 }, bleeding = 0,
                dead = alive == false and 1 or 0,
            }
        end)
        if not ok then return nil, 'HD2Runtime 读取异常：' .. tostring(st) end
        return st, why
    end

    function M.describe()
        return 'runtime hd2runtime=' .. tostring(M.version or '?') .. ' source=public_api'
    end

    return M
end

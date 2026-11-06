-- core/json.lua —— 只够线缆协议用的最小 JSON 编码器（无第三方依赖）
-- 输入是有序 kv 列表（保持字段顺序，方便日志对比），值支持 number / boolean / string / 数组。
return function(H)
    local M = {}

    local function num(v)
        if v ~= v or v == math.huge or v == -math.huge then return '0' end
        if v == math.floor(v) and math.abs(v) < 1e15 then
            return string.format('%.0f', v)
        end
        local s = string.format('%.4f', v)
        s = s:gsub('0+$', ''):gsub('%.$', '')
        if s == '' or s == '-' or s == '-0' then s = '0' end
        return s
    end

    local function esc(v)
        local s = tostring(v)
        s = s:gsub('[%c"\\]', function(c)
            if c == '"' then return '\\"' end
            if c == '\\' then return '\\\\' end
            if c == '\n' then return '\\n' end
            if c == '\r' then return '\\r' end
            if c == '\t' then return '\\t' end
            return string.format('\\u%04x', c:byte())
        end)
        return '"' .. s .. '"'
    end

    local function value(v)
        local t = type(v)
        if t == 'number' then return num(v) end
        if t == 'boolean' then return v and 'true' or 'false' end
        if t == 'table' then
            local items = {}
            for i = 1, #v do items[i] = value(v[i]) end
            return '[' .. table.concat(items, ',') .. ']'
        end
        if v == nil then return 'null' end
        return esc(v)
    end

    -- encode({ {'v', 1}, {'ev', 'state'}, {'limbs', {0,1,0}} })
    function M.encode(pairs_list)
        local parts = {}
        for i = 1, #pairs_list do
            local kv = pairs_list[i]
            local k = kv[1]
            if kv[2] ~= nil then
                parts[#parts + 1] = esc(k) .. ':' .. value(kv[2])
            end
        end
        return '{' .. table.concat(parts, ',') .. '}'
    end

    return M
end

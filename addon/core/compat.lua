-- core/compat.lua —— 宿主能力探测：MDL3 ctx / ffi / 加载器 / stingray
-- 这一层决定「能不能读内存」和「能不能发 UDP」，所有上层只问这里，不自己 pcall。
return function(H)
    local M = { notes = {} }

    local function note(text)
        M.notes[#M.notes + 1] = text
    end
    M.note = note

    --- ffi：有就返回模块，没有返回 nil（判定结果缓存）
    function M.ffi()
        if M._ffi ~= nil then return M._ffi or nil end
        local ok, ffi = pcall(require, 'ffi')
        if ok and type(ffi) == 'table' and type(ffi.cdef) == 'function' then
            M._ffi = ffi
            note('ffi: available')
            return ffi
        end
        M._ffi = false
        note('ffi: unavailable')
        return nil
    end

    local function to_int(v)
        local t = type(v)
        if t == 'number' then
            if v ~= v or v < 0 then return nil end
            return math.floor(v)
        end
        if t == 'string' then
            local hex = v:gsub('^0[xX]', '')
            if not hex:match('^%x+$') then return nil end
            local n = 0
            for i = 1, #hex do n = n * 16 + tonumber(hex:sub(i, i), 16) end
            return n
        end
        if t == 'table' then
            return to_int(v.base or v.address or v.addr or v.value or v[1])
        end
        if t == 'cdata' and M.ffi() then
            local ok, n = pcall(function() return tonumber(M.ffi().cast('uintptr_t', v)) end)
            if ok and type(n) == 'number' then return n end
        end
        return nil
    end
    M.to_int = to_int

    --- MDL3 ctx.read 归一化：字符串 / 字节数组 / nil
    local function to_bytes(v, want)
        local t = type(v)
        if t == 'string' then
            if want and #v ~= want then return nil end
            return v
        end
        if t == 'table' then
            local out = {}
            for i = 1, (want or #v) do
                local b = v[i]
                if type(b) ~= 'number' then return nil end
                out[i] = string.char(math.floor(b) % 256)
            end
            return table.concat(out)
        end
        if t == 'cdata' and M.ffi() then
            local ok, s = pcall(function() return M.ffi().string(v, want) end)
            if ok and type(s) == 'string' then return s end
        end
        return nil
    end

    --- 读内存：MDL3 原生 ctx.read 优先，ffi 兜底
    function M.read(addr, n, ctx)
        if not addr or addr == 0 or not n or n <= 0 then return nil end
        if ctx and type(ctx.read) == 'function' then
            local ok, v = pcall(ctx.read, addr, n)
            local bytes = ok and to_bytes(v, n) or nil
            if bytes then
                M.read_backend = 'ctx'
                return bytes
            end
        end
        local ffi = M.ffi()
        if ffi then
            local ok, s = pcall(function()
                return ffi.string(ffi.cast('const char*', addr), n)
            end)
            if ok and type(s) == 'string' and #s == n then
                M.read_backend = 'ffi'
                return s
            end
        end
        return nil
    end

    --- 模块基址：ctx.module 优先，GetModuleHandleA 兜底
    function M.module_base(name, ctx)
        name = name or 'game.dll'
        if ctx and type(ctx.module) == 'function' then
            local ok, v = pcall(ctx.module, name)
            local base = ok and to_int(v) or nil
            if base and base > 0 then
                M.base_backend = 'ctx.module'
                return base
            end
        end
        local ffi = M.ffi()
        if ffi then
            pcall(ffi.cdef, [[
                void* GetModuleHandleA(const char* lpModuleName);
            ]])
            local ok, h = pcall(function() return ffi.C.GetModuleHandleA(name) end)
            local base = ok and to_int(h) or nil
            if base and base > 0 then
                M.base_backend = 'ffi'
                return base
            end
        end
        return nil, M.base_backend
    end

    --- MDL3 选项快照：ctx.settings 可能是表，也可能是函数 / 方法
    function M.settings_snapshot(ctx)
        if type(ctx) ~= 'table' then return nil, 'no ctx' end
        local s = ctx.settings
        if type(s) == 'table' then return s, 'table' end
        if type(s) == 'function' then
            local ok, v = pcall(s, ctx)
            if ok and type(v) == 'table' then return v, 'function' end
            ok, v = pcall(s)
            if ok and type(v) == 'table' then return v, 'function' end
        end
        if type(ctx.settings_get) == 'function' then
            local ok, v = pcall(ctx.settings_get)
            if ok and type(v) == 'table' then return v, 'settings_get' end
        end
        return nil, 'unknown'
    end

    --- 环境报告（写进日志与状态文件，排查用）
    function M.describe(ctx)
        local parts = {
            'lua=' .. tostring(_VERSION),
            'jit=' .. tostring(rawget(_G, 'jit') and 'yes' or 'no'),
            'ffi=' .. tostring(M.ffi() and 'yes' or 'no'),
            'ctx=' .. tostring(type(ctx)),
            'stingray=' .. tostring(type(rawget(_G, 'stingray'))),
        }
        if type(ctx) == 'table' then
            local keys = {}
            for k in pairs(ctx) do
                if type(k) == 'string' then keys[#keys + 1] = k end
            end
            table.sort(keys)
            parts[#parts + 1] = 'ctx.members=' .. table.concat(keys, ',')
        end
        return table.concat(parts, ' ')
    end

    return M
end

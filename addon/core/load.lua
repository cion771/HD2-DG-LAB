-- core/load.lua —— 模块装载器：同一份源码同时支持两种部署形态
--   A) 本地 mod：%LOCALAPPDATA%\MDL\Helldivers2\Mods\hd2coyote\  —— 从磁盘读文件
--   B) Arsenal addon：每个模块是一个 require-able 资源 —— 走 require
-- 两种形态的模块文件内容完全相同，都是 return function(H) ... end。
return function(H)
    local M = {}

    local function file_source(dir, rel)
        local path = dir .. '/' .. rel .. '.lua'
        local f = io.open(path, 'rb')
        if not f then
            local alt = dir .. '\\' .. rel:gsub('/', '\\') .. '.lua'
            f = io.open(alt, 'rb')
            path = alt
        end
        if not f then return nil, nil, path end
        local src = f:read('*a')
        f:close()
        return src, path
    end

    function M.make(opts)
        opts = opts or {}
        local dir = opts.dir
        local name = opts.name or 'mods/hd2coyote/hd2_coyote'
        local cache = {}
        local missing = {}
        local api = {}

        local function load(rel)
            if cache[rel] ~= nil then return cache[rel] end
            local factory, err

            if dir and dir ~= '' then
                local src, path = file_source(dir, rel)
                if src then
                    local chunkname = '@mods/hd2coyote/hd2_coyote/' .. rel:gsub('/', '_')
                    local chunk
                    chunk, err = loadstring(src, chunkname)
                    if chunk then
                        local ok
                        ok, factory = pcall(chunk)
                        if not ok then err, factory = factory, nil end
                    end
                else
                    err = 'file not found: ' .. tostring(path)
                end
            end

            if type(factory) ~= 'function' then
                local resource = name .. '/' .. rel:gsub('/', '_')
                local ok, mod = pcall(require, resource)
                if ok and type(mod) == 'function' then
                    factory = mod
                elseif not err then
                    err = tostring(mod)
                end
            end

            if type(factory) ~= 'function' then
                missing[#missing + 1] = rel .. ': ' .. tostring(err)
                error('module load failed: ' .. rel .. ' (' .. tostring(err) .. ')')
            end

            local inst = factory(H)
            cache[rel] = inst
            return inst
        end

        function api.try(rel)
            local ok, value = pcall(load, rel)
            if ok then return value end
            return nil
        end

        function api.missing() return missing end

        return setmetatable(api, {
            __call = function(_, rel) return load(rel) end,
        })
    end

    return M
end

-- core/config.lua —— 分层配置：内置默认 ← bridge_config.lua/.txt ← MDL3 选项
-- 与旧版保持同一份用户文件（%LOCALAPPDATA%\hd2coyote\bridge_config.lua），老用户不用重新学。
return function(H)
    local M = { values = {}, source = {} }

    local DEFAULTS = {
        mode = 'safe',
        enabled = true,
        host = '127.0.0.1',
        port = 47777,
        interval = 0.10,
        profile = 'steam_25480438',
        transport = 'auto',
        state_path = nil,
        log_dir = nil,
        heartbeat = 2.0,
        max_error = 5,
        verbose = false,
        mock = false,
    }

    local MODES = { safe = true, net = true, recon = true, live = true }
    local TRANSPORTS = { auto = true, udp = true, file = true, off = true }

    local function clamp(value, lo, hi, fallback)
        local n = tonumber(value)
        if not n or n ~= n then return fallback end
        if n < lo then return lo end
        if n > hi then return hi end
        return n
    end

    local function set(key, value, source)
        M.values[key] = value
        M.source[key] = source or '?'
    end

    function M.get(key)
        local v = M.values[key]
        if v == nil then return DEFAULTS[key] end
        return v
    end

    function M.all() return M.values, M.source end
    function M.defaults() return DEFAULTS end

    local function sanitize()
        local mode = tostring(M.get('mode')):lower()
        if not MODES[mode] then mode = DEFAULTS.mode end
        M.values.mode = mode

        local transport = tostring(M.get('transport')):lower()
        if not TRANSPORTS[transport] then transport = DEFAULTS.transport end
        M.values.transport = transport

        M.values.port = math.floor(clamp(M.get('port'), 1, 65535, DEFAULTS.port))
        M.values.interval = clamp(M.get('interval'), 0.02, 5.0, DEFAULTS.interval)
        M.values.heartbeat = clamp(M.get('heartbeat'), 0.5, 60, DEFAULTS.heartbeat)
        M.values.max_error = math.floor(clamp(M.get('max_error'), 1, 100, DEFAULTS.max_error))
        M.values.enabled = M.get('enabled') and true or false
        M.values.mock = M.get('mock') and true or false
        local host = tostring(M.get('host'))
        if not host:match('^%d+%.%d+%.%d+%.%d+$') then M.values.host = DEFAULTS.host end
        if not M.values.profile or M.values.profile == '' then M.values.profile = DEFAULTS.profile end
    end

    local function base_dir()
        -- 优先级：显式 log_dir > HD2COYOTE_DIR（诊断/测试用）> %LOCALAPPDATA%\hd2coyote
        local env = os.getenv and os.getenv('HD2COYOTE_DIR') or nil
        if env and env ~= '' then return env end
        local base = os.getenv and os.getenv('LOCALAPPDATA') or nil
        if base then return base .. '\\hd2coyote' end
        return nil
    end

    local function read_file(path)
        local ok, f = pcall(io.open, path, 'rb')
        if not ok or not f then return nil end
        local text = f:read('*a')
        pcall(f.close, f)
        if type(text) ~= 'string' then return nil end
        return text
    end

    local function apply_table(tbl, source)
        if type(tbl) ~= 'table' then return end
        for k, v in pairs(tbl) do
            if type(k) == 'string' then set(k, v, source) end
        end
    end

    local function parse_plain(text)
        local out = {}
        for line in text:gmatch('[^\r\n]+') do
            local code = line:gsub('%-%-.*$', '')
            for key, value in code:gmatch('([%a_][%w_]*)%s*=%s*([^%s,;]+)') do
                value = value:gsub('^["\']', ''):gsub('["\']$', '')
                out[key] = tonumber(value) or value
            end
        end
        return out
    end

    function M.load_file()
        local dir = M.get('log_dir')
        if not dir or dir == '' then dir = base_dir() end
        if not dir then return false, 'no base dir' end
        local lua_path = dir .. '\\bridge_config.lua'
        local txt_path = dir .. '\\bridge_config.txt'
        local text = read_file(lua_path)
        if text then
            local chunk, err = loadstring(text, '@bridge_config.lua')
            if chunk then
                local ok, value = pcall(chunk)
                if ok and type(value) == 'table' then
                    if type(value.config) == 'table' then value = value.config end
                    apply_table(value, 'file:lua')
                    return true
                end
                M.file_error = tostring(value)
            else
                M.file_error = tostring(err)
            end
            return false, M.file_error
        end
        text = read_file(txt_path)
        if text then
            apply_table(parse_plain(text), 'file:txt')
            return true
        end
        return false, 'no config file'
    end

    --- MDL3 选项覆盖（只有前者不存在时才生效）
    function M.apply_options(snapshot)
        apply_table(snapshot, 'mdl3')
    end

    function M.init(ctx, log)
        for k, v in pairs(DEFAULTS) do M.values[k] = v; M.source[k] = 'default' end
        local ok, why = M.load_file()
        M.loaded_file = ok
        M.load_error = ok and nil or why
        if ctx and H.compat then
            local snapshot, how = H.compat.settings_snapshot(ctx)
            if snapshot then
                M.apply_options(snapshot)
                M.options_api = how
            end
        end
        sanitize()
        if not M.get('state_path') then
            local dir = M.get('log_dir')
            if not dir or dir == '' then dir = base_dir() end
            if dir then M.values.state_path = dir .. '\\bridge_state.json' end
        end
        return M
    end

    return M
end

-- core/net.lua —— 状态出口：UDP（有 ffi）或文件广播（无 ffi 的 MDL3 宿主）
-- 文件广播不是"降级玩具"：MDL3 的 Lua 宿主如果没开 ffi，游戏里唯一的本地出口就是文件。
-- 控制器端（Python）用同一条解析路径读 bridge_state.json，字段与 UDP 报文完全一致。
return function(H)
    local M = { backend = 'none', sent = 0, failed = 0, last_ok = nil, reason = nil }

    local compat, json, log

    local function addr_bytes(host)
        local a, b, c, d = host:match('^(%d+)%.(%d+)%.(%d+)%.(%d+)$')
        if not a then return nil end
        return string.char(tonumber(a), tonumber(b), tonumber(c), tonumber(d))
    end

    function M.init(cfg, logger, jsonmod)
        log = logger
        json = jsonmod
        compat = H.compat
        local want = cfg.get('transport')
        local udp_err
        if want == 'auto' or want == 'udp' then
            local ok, why = M.start_udp(cfg)
            if ok then return 'udp' end
            udp_err = why
            if want == 'udp' then
                M.reason = 'transport=udp 但 UDP 不可用：' .. tostring(why)
                M.backend = 'none'
                return 'none'
            end
        end
        if want == 'auto' or want == 'file' then
            local ok, why = M.start_file(cfg)
            if ok then
                if udp_err then M.reason = 'UDP 不可用（' .. tostring(udp_err) .. '），改用文件广播' end
                return 'file'
            end
            M.reason = '文件广播不可用：' .. tostring(why)
        end
        M.backend = 'none'
        return 'none'
    end

    function M.start_udp(cfg)
        local ffi = compat.ffi()
        if not ffi then return false, 'no ffi' end
        if not H.mem or not H.mem.init() then return false, 'ffi init failed' end
        local host = cfg.get('host')
        local port = cfg.get('port')
        local packed = addr_bytes(host)
        if not packed then return false, 'bad host ' .. tostring(host) end
        local ok, err = pcall(function()
            local sock = H.mem.WS2.Hd2Coyote_socket(2, 2, 17)
            if sock == nil or tonumber(ffi.cast('int', sock)) < 0 then error('socket() failed') end
            M.sock = sock
            local port_hi = math.floor(port / 256)
            local port_lo = port % 256
            M.sockaddr = string.char(2, 0, port_hi, port_lo) .. packed .. string.char(0, 0, 0, 0, 0, 0, 0, 0)
            M.sockaddr_len = #M.sockaddr
        end)
        if not ok then return false, tostring(err) end
        M.backend = 'udp'
        return true
    end

    function M.start_file(cfg)
        local path = cfg.get('state_path')
        if not path or path == '' then return false, 'state_path empty' end
        -- 用同目录的探针文件验证可写性：绝不靠"试着写正式文件"来探测，
        -- 否则读端会先看到一个空状态。
        local probe = path .. '.probe'
        if not (log and log.write_file(probe, 'ok', false)) then
            return false, 'cannot write ' .. tostring(path)
        end
        pcall(os.remove, probe)
        M.state_path = path
        M.backend = 'file'
        M.seq = 0
        return true
    end

    function M.send(pairs_list)
        if M.backend == 'none' then
            M.failed = M.failed + 1
            return false
        end
        local text = json.encode(pairs_list)
        if M.backend == 'udp' then
            local ok = pcall(function()
                local n = H.mem.WS2.Hd2Coyote_sendto(M.sock, text, #text, 0, M.sockaddr, M.sockaddr_len)
                if n == nil or tonumber(H.mem.ffi.cast('int', n)) < 0 then error('sendto failed') end
            end)
            M.last_ok = ok
            if ok then M.sent = M.sent + 1 else M.failed = M.failed + 1 end
            return ok
        end
        -- 文件广播：整文件覆盖写。读端解析失败就当掉包（和 UDP 坏包同一条路径），
        -- 不用 rename 是因为 Windows 的 os.rename 在目标已存在时会失败。
        M.seq = (M.seq or 0) + 1
        local body = text .. '\n'
        local ok = log.write_file(M.state_path, body, false)
        M.last_ok = ok and true or false
        if ok then M.sent = M.sent + 1 else M.failed = M.failed + 1 end
        return ok
    end

    function M.describe()
        return string.format('%s sent=%d failed=%d', tostring(M.backend), M.sent, M.failed)
    end

    return M
end

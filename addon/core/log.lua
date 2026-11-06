-- core/log.lua —— 有界日志：内存环形缓冲 + 文件追加 + MDL3 ctx.log + 状态文件
-- 铁律：日志路径不可写时**不许**抛错、不许每帧重开文件、不许无限增长。
return function(H)
    local M = { lines = 0, limit = 800, recent = {}, recent_limit = 8 }

    local function join(a, b)
        if not a or a == '' then return b end
        if not b or b == '' then return a end
        local last = a:sub(-1)
        if last == '\\' or last == '/' then return a .. b end
        return a .. '\\' .. b
    end
    M.join = join

    function M.write_file(path, text, append)
        if not path then return false end
        local ok, f = pcall(io.open, path, append and 'ab' or 'wb')
        if not ok or not f then return false end
        local wrote = pcall(f.write, f, text)
        pcall(f.close, f)
        return wrote and true or false
    end

    function M.short(value, limit)
        local text = tostring(value):gsub('[%c]', ' ')
        if #text <= limit then return text end
        return text:sub(1, limit) .. '…'
    end

    function M.init(ctx)
        M.ctx = ctx
        local base = os.getenv and os.getenv('LOCALAPPDATA') or nil
        local dir = H.config and H.config.get('log_dir') or nil
        if not dir then dir = (os.getenv and os.getenv('HD2COYOTE_DIR')) or nil end
        if not dir and base then dir = join(base, 'hd2coyote') end
        M.dir = dir
        if dir then
            M.path = join(dir, 'hd2_coyote_bridge.log')
            M.status_path = join(dir, 'hd2_coyote_status.txt')
            -- 目录不存在时 open 失败是正常的：此时只写内存与 ctx.log
            if M.write_file(M.path, '', true) then
                M.can_write = true
            else
                M.path = nil
                M.status_path = nil
                M.can_write = false
            end
        end
        return M
    end

    local function stamp()
        return os.date('%H:%M:%S')
    end

    function M.log(msg, force)
        if M.lines > M.limit and not force then return end
        M.lines = M.lines + 1
        local text = tostring(msg)
        local line = '[' .. stamp() .. '][' .. tostring(H.TAG or 'hd2coyote') .. '] ' .. text .. '\n'
        M.recent[#M.recent + 1] = M.short('[' .. stamp() .. '] ' .. text, 320)
        if #M.recent > M.recent_limit then table.remove(M.recent, 1) end
        local wrote = false
        if M.path and M.write_file(M.path, line, true) then wrote = true end
        if M.ctx and type(M.ctx.log) == 'function' then
            local ok = pcall(M.ctx.log, text)
            wrote = wrote or ok
        end
        if not wrote then pcall(print, '[hd2coyote] ' .. text) end
    end

    function M.status(ok, reason)
        M.detail = tostring(reason)
        local head = (ok and 'OK - ' or 'FAILED - ') .. tostring(reason)
        if M.status_path then
            local body = head .. '\n' .. string.format(
                'addon=%s version=%s mode=%s profile=%s transport=%s\n',
                tostring(H.NAME), tostring(H.VERSION),
                tostring(H.config and H.config.get('mode')),
                tostring(H.config and H.config.get('profile')),
                tostring(H.net and H.net.backend or '?'))
            M.write_file(M.status_path, body, false)
        end
        M.log('STATUS: ' .. head, true)
    end

    return M
end

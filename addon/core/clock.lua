-- core/clock.lua —— 单调时间（无 os.clock 可用时退到 os.time）
return function(H)
    local M = { origin = nil }

    local function raw_ms()
        if type(os.clock) == 'function' then
            local c = os.clock()
            if type(c) == 'number' and c >= 0 then return c * 1000 end
        end
        return os.time() * 1000
    end

    function M.now_ms()
        local v = raw_ms()
        if not M.origin or v < M.origin then M.origin = v end
        return v
    end

    function M.reset()
        M.origin = raw_ms()
    end

    -- 把 dt（秒，来自 MDL3 on_update）转成毫秒；dt 不可信时用挂钟差
    function M.step_ms(dt)
        local now = M.now_ms()
        local prev = M.last or now
        M.last = now
        if type(dt) == 'number' and dt > 0 and dt < 10 then
            M.dt_ms = dt * 1000
        else
            M.dt_ms = math.max(0, now - prev)
        end
        return M.dt_ms
    end

    return M
end

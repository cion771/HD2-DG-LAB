-- state/sampler.lua —— 唯一决定"什么时候发什么包"的地方
-- 三条流分开计时：hello（一次）、heartbeat（保活，不带游戏数据）、state（限频）。
-- 心跳绝不延长旧状态的有效期 —— 控制器靠 state 的到达时间判活。
return function(H)
    local M = { last_state = 0, last_beat = 0, seq = 0, ready = false }

    function M.init(cfg, log, net, clock)
        M.cfg, M.log, M.net, M.clock = cfg, log, net, clock
        M.interval_ms = cfg.get('interval') * 1000
        M.beat_ms = cfg.get('heartbeat') * 1000
        M.last_state, M.last_beat = 0, 0
    end

    function M.attach(source, detail)
        M.source = source
        M.source_detail = detail
        M.ready = false
        M.wait_reason = nil
    end

    local function meta()
        return H.meta or {}
    end

    function M.hello(extra)
        local m = meta()
        M.net.send({
            { 'v', H.PROTOCOL }, { 'ev', 'hello' },
            { 'ver', H.VERSION }, { 'build', m.build },
            { 'profile', m.profile }, { 'mode', m.mode },
            { 'transport', M.net.backend }, { 'detail', extra or m.detail },
        })
    end

    function M.heartbeat(detail)
        local m = meta()
        M.net.send({
            { 'v', H.PROTOCOL }, { 'ev', 'heartbeat' },
            { 'ver', H.VERSION }, { 'build', m.build },
            { 'profile', m.profile }, { 'mode', m.mode },
            { 'transport', M.net.backend },
            { 'detail', M.log.short(detail or m.detail or '', 280) },
        })
    end

    function M.bye()
        M.net.send({ { 'v', H.PROTOCOL }, { 'ev', 'bye' }, { 'ver', H.VERSION } })
    end

    --- 返回 true 表示这次真的发出了一条 state
    function M.update(dt)
        local now = M.clock.now_ms()
        local sent = false
        if now - M.last_beat >= M.beat_ms then
            M.last_beat = now
            M.heartbeat(M.wait_reason)
        end
        if not M.source then return false end
        if now - M.last_state < M.interval_ms then return false end
        M.last_state = now
        local st, why = M.source.read_state()
        if not st then
            M.ready = false
            M.wait_reason = why
            return false
        end
        M.wait_reason = nil
        M.seq = M.seq + 1
        local m = meta()
        sent = M.net.send({
            { 'v', H.PROTOCOL }, { 'ev', 'state' },
            { 't', now / 1000 }, { 'seq', M.seq },
            { 'hp', st.hp }, { 'hp_max', st.hp_max },
            { 'limbs', st.limbs }, { 'bleeding', st.bleeding },
            { 'dead', st.dead },
            { 'build', m.build }, { 'profile', m.profile }, { 'mode', m.mode },
        })
        if sent and not M.ready then
            M.ready = true
            H.log.status(true, string.format('%s 已就绪（%s，transport=%s）',
                m.mode, tostring(M.source_detail or M.source.name), M.net.backend))
        end
        return sent
    end

    return M
end

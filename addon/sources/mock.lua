-- sources/mock.lua —— 合成数据源：不开游戏也能把整条链路（采样→报文→控制器）跑通
-- 行为刻意做得像真的：掉血、肢体受伤、倒地、阵亡、复活，循环往复。
return function(H)
    local M = { name = 'mock', t = 0, hp = 100, hp_max = 100, phase = 'alive' }

    function M.setup()
        M.t, M.hp, M.phase = 0, M.hp_max, 'alive'
        M.limbs = { 0, 0, 0, 0, 0, 0 }
    end

    function M.available() return true, 'mock source（合成数据）' end

    function M.read_state()
        local dt = (H.clock and H.clock.dt_ms or 100) / 1000
        M.t = M.t + dt
        if M.phase == 'alive' then
            M.hp = M.hp - (8 * dt)          -- 每秒掉 8 点
            if M.t > 6 and M.limbs[1] == 0 then M.limbs[1] = 1 end
            if M.t > 9 and M.limbs[5] == 0 then M.limbs[5] = 1 end
            if M.hp <= 0 then
                M.hp, M.phase, M.t = 0, 'downed', 0
            end
        elseif M.phase == 'downed' then
            if M.t > 2 then M.phase, M.t = 'dead', 0 end
        else
            if M.t > 3 then
                M.phase, M.t, M.hp = 'alive', 0, M.hp_max
                M.limbs = { 0, 0, 0, 0, 0, 0 }
            end
        end
        return {
            hp = math.max(0, math.floor(M.hp + 0.5)), hp_max = M.hp_max,
            limbs = M.limbs, bleeding = 0,
            downed = M.phase == 'downed' and 1 or 0,
            dead = M.phase == 'dead' and 1 or 0,
        }
    end

    function M.describe() return 'mock source' end

    return M
end

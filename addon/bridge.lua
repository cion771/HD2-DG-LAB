-- bridge.lua —— 装配：配置 → 日志 → 内存/网络 → 数据源 → 采样
-- 模式阶梯（与旧版同名，但少了 menu 档：MDL3 的选项就是菜单）：
--   safe  只写日志，不碰内存、不开 socket、不发包
--   net   加出口：hello + 心跳，仍然不读游戏
--   recon 加一次性的数据源体检，把证据写进 recon_report.txt
--   live  加持续读取与状态上报
return function(H)
    local B = { mode = 'safe', dead = false, errors = 0 }

    local function write_report(text)
        if not H.log or not H.log.dir then return false end
        local path = H.log.join(H.log.dir, 'recon_report.txt')
        return H.log.write_file(path, text, false)
    end

    function B.recon(load)
        local lines = {}
        local function add(fmt, ...)
            lines[#lines + 1] = (select('#', ...) > 0) and string.format(fmt, ...) or fmt
        end
        add('hd2-coyote 游戏内桥 · recon 报告')
        add('时间: %s', os.date('%Y-%m-%d %H:%M:%S'))
        add('版本: %s  protocol=%d', H.VERSION, H.PROTOCOL)
        add('环境: %s', H.compat.describe(H.ctx))
        add('内存后端: %s  基址后端: %s', tostring(H.mem.backend), tostring(H.mem.base_backend))
        add('网络后端: %s  %s', tostring(H.net.backend), H.net.describe())
        add('配置来源: file=%s error=%s options=%s',
            tostring(H.config.loaded_file), tostring(H.config.load_error), tostring(H.config.options_api))
        local values, source = H.config.all()
        local keys = {}
        for k in pairs(values) do keys[#keys + 1] = k end
        table.sort(keys)
        add('')
        add('配置:')
        for _, k in ipairs(keys) do
            add('  %-14s = %-22s (%s)', k, tostring(values[k]), tostring(source[k]))
        end
        local source_obj = H.source
        add('')
        if source_obj then
            add('数据源: %s', source_obj.describe and source_obj:describe() or tostring(source_obj.name))
            local st, why = source_obj.read_state()
            if st then
                add('首个状态: hp=%.2f/%d dead=%d limbs=%s', st.hp, st.hp_max, st.dead,
                    table.concat(st.limbs, ','))
            else
                add('首个状态: 未就绪 —— %s', tostring(why))
            end
        else
            add('数据源: 无（%s）', tostring(B.source_reason))
        end
        add('')
        add('提示: 只有 live 档才会持续上报；recon 只做一次性体检。')
        local ok = write_report(table.concat(lines, '\n') .. '\n')
        H.log.log('recon 报告已写入 ' .. (ok and 'recon_report.txt' or '失败'), true)
        return ok
    end

    local function pick_source(load)
        if H.config.get('mock') then
            local mock = load('sources/mock')
            mock.setup()
            return mock, 'mock source'
        end
        local runtime = load.try('sources/runtime')
        if runtime then
            runtime.setup()
            local ok, detail = runtime.available()
            if ok then return runtime, detail end
            H.log.log('HD2Runtime 不可用：' .. tostring(detail))
        end
        local profile_name = H.config.get('profile')
        local profile_factory = load.try('profiles/' .. tostring(profile_name))
        if not profile_factory then
            return nil, '未知 profile：' .. tostring(profile_name)
        end
        local direct = load('sources/direct')
        direct.setup(profile_factory)
        local ok, detail = direct.available(H.ctx)
        if ok then return direct, detail end
        return nil, detail
    end

    function B.init(ctx, load)
        H.ctx = ctx
        H.compat = load('core/compat')
        H.json = load('core/json')
        H.config = load('core/config')
        H.config.init(ctx)
        H.log = load('core/log').init(ctx)
        H.clock = load('core/clock')
        H.mem = load('core/mem')
        H.net = load('core/net')

        local cfg = H.config
        B.mode = cfg.get('mode')
        H.meta = {
            mode = B.mode,
            profile = cfg.get('profile'),
            build = 'unknown',
            detail = 'starting',
        }
        H.log.log(string.format('启动 v%s mode=%s（%s）', H.VERSION, B.mode, H.compat.describe(ctx)), true)

        if B.mode == 'safe' then
            H.log.status(true, 'safe 模式：只写日志，不读游戏、不发包')
            return B
        end

        local backend = H.net.init(cfg, H.log, H.json)
        H.log.log(string.format('出口: %s %s', backend, tostring(H.net.reason or '')), true)
        if backend == 'none' then
            H.log.status(false, '出口不可用：' .. tostring(H.net.reason or '未知原因'))
            return B
        end

        H.sampler = load('state/sampler')
        H.sampler.init(cfg, H.log, H.net, H.clock)

        if B.mode == 'net' then
            H.meta.detail = 'net 模式：只发心跳，不读游戏'
            H.sampler.hello(H.meta.detail)
            H.log.status(true, 'net 模式：出口已就绪，尚未读取游戏数据')
            return B
        end

        local source, detail = pick_source(load)
        H.source = source
        B.source_reason = detail
        if source then
            H.meta.profile = (cfg.get('mock') and 'mock') or cfg.get('profile')
            H.meta.build = (source.name == 'runtime' and 'HD2Runtime') or
                           (source.name == 'direct' and 'game.dll') or 'mock'
            H.meta.detail = tostring(detail)
            H.sampler.attach(source, detail)
            H.log.log('数据源: ' .. tostring(detail), true)
        else
            H.meta.detail = '无数据源：' .. tostring(detail)
            H.log.log('数据源不可用: ' .. tostring(detail), true)
        end

        H.sampler.hello(H.meta.detail)
        if B.mode == 'recon' then
            B.recon(load)
            H.log.status(source ~= nil, source and ('recon 完成：' .. tostring(detail))
                                             or ('recon 失败：' .. tostring(detail)))
            return B
        end
        H.log.status(source ~= nil, source and ('live 已启动：' .. tostring(detail))
                                         or ('live 等待数据源：' .. tostring(detail)))
        return B
    end

    function B.update(dt)
        if B.dead or B.mode == 'safe' then return end
        H.clock.step_ms(dt)
        if H.sampler then H.sampler.update(dt) end
    end

    function B.shutdown(why)
        if B.dead then return end
        B.dead = true
        if H.sampler then
            H.sampler.bye()
            H.log.log('关闭：' .. tostring(why or 'shutdown'), true)
        end
    end

    --- 统一错误预算：连续出错到上限就主动停，绝不带崩游戏
    function B.guard(fn, ...)
        if B.dead then return false end
        local ok, err = pcall(fn, ...)
        if ok then
            B.errors = 0
            return true
        end
        B.errors = B.errors + 1
        H.log.log('运行异常(' .. B.errors .. ')：' .. tostring(err), true)
        if B.errors >= H.config.get('max_error') then
            B.shutdown('连续异常达到上限')
            H.log.status(false, '已停止：连续 ' .. B.errors .. ' 次异常')
        end
        return false
    end

    return B
end

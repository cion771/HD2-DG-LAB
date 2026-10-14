-- HD2-Addon: mods/hd2coyote/hd2_coyote_bridge
--
-- hd2-coyote 游戏内桥（只读）
--   在游戏进程内读取本地玩家的血量 / 肢体损伤 / 阵亡状态，
--   通过 UDP 发给本机的 hd2-coyote 控制器，由控制器驱动郊狼。
--
-- 先把话说清楚，免得被"hook 游戏"这个词误导：
--   * 《绝地潜兵 2》没有官方 mod API、没有 SDK、也没有 Workshop 支持。
--     本文件走的是社区加载器 Bingus Shared Loader 的 addon 契约
--     （部署成 data/9ba626afa44a3aa3.patch_N，第一行必须是 -- HD2-Addon:）。
--   * "hook 游戏"在 HD2 的真实含义 = addon 跑在游戏自带的 LuaJIT 里，
--     与游戏共享 Lua 状态和地址空间，于是可以用 FFI 只读地读游戏内存。
--   * 本 addon 全程只读：不写内存、不改游戏数据、不向外部网络发包
--     （只发 127.0.0.1 的 UDP）。
--   * 反作弊是 nProtect GameGuard。社区共识：进程内只读 addon 可接受；
--     从外部进程 OpenProcess / ReadProcessMemory 才是最容易出事的做法 —— 本 addon 不那样做。
--
-- 两种只读数据模式：
--   mode = 'recon'  —— 一次性验证 game.dll 构建并定位本地玩家，把诊断证据落盘。
--   mode = 'live'   —— 自动从本地用户 → 玩家 → Avatar → health manager 解析状态，
--                      以 10Hz 上报；切船/重生时找不到玩家会每 3 秒自动重试。
--
-- profile 固定到受支持的游戏构建。每次启动先验证 PE 映像和多处指令指纹；
-- 构建不匹配就拒绝读取，不接受用户手填偏移，也不扫描整个地址空间。

local ADDON = 'mods/hd2coyote/hd2_coyote_bridge'
local VERSION = '0.6.2'   -- 必须与仓库根的 VERSION 文件一致（打包时会校验）
local PROTOCOL = 1

--------------------------------------------------------------------- 配置
local CONFIG = {
    -- ★ 默认最保守。逐级往上开，每一步都可回退：
    --   'safe'  = 只写日志（不碰 FFI、不读内存、不挂钩子、不发 UDP、不注册菜单）
    --   'net'   = 再加 FFI + UDP 上报（hello），仍不挂钩子、不读游戏内存
    --   'menu'  = 再加每帧钩子 + 游戏内菜单（仍然**不读游戏内存**）
    --   'recon' = 再加一次性构建校验与本地玩家诊断
    --   'live'  = 自动解析本地玩家与生命状态并上报
    mode = 'safe',
    enabled = true,            -- 文件配置的上报开关（live/recon 时才有效）
    host = '127.0.0.1',
    port = 47777,
    interval = 0.10,           -- 状态上报间隔（秒）
    profile = 'steam_25480438',
    log_dir = nil,             -- nil = %HD2COYOTE_DIR% 或 %LOCALAPPDATA%\hd2coyote
    max_dump_bytes = 0x100,    -- recon 单块 dump 上限（有界；绝不整地址空间扫描）
    menu_retry_frames = 600,   -- 等 ModOptionsMenu 出现的最多帧数（有终止状态）
    -- 每帧钩子：官方第三方参考写明 CowboyBingus 的 mod 普遍包装全局 update
    frame_hooks = { 'update', 'Update', 'GameUpdate' },
}

-- 偏移 profile。全部固定到一个已验证构建；live 启动前会同时验证 PE 映像大小与关键指令。
-- 布局来自 SkyeShade/HD2Runtime 的公开 F5FEE03DCFDB 研究（MIT）。这里仅实现最小只读子集：
-- 本地玩家列表、network id -> avatar、health manager 与六个肢体 zone；不调用游戏函数、不写内存。
local PROFILES = {
    steam_25480438 = {
        build = '1.8.46015.0',
        exe_sha256_prefix = 'F5FEE03DCFDB',
        game_dll_sha256_prefix = '2E2C3B7C2500',
        image_size = 74727424,
        pins = {
            { rva = 4867098, hex = '4c8b156722e802', label = 'health manager global' },
            { rva = 4867242, hex = '4869c1b8010000', label = 'health record stride' },
            { rva = 6473498, hex = '488b0d479dcf02', label = 'player manager global' },
            { rva = 6473561, hex = '8b5108', label = 'player entity' },
            { rva = 11322988, hex = '428b9430a8030000', label = 'player avatar network id' },
            { rva = 16620452, hex = '4c8b1ded234902', label = 'network entity manager' },
            { rva = 9576278, hex = '3c02', label = 'injured limb state' },
        },
        health = {
            global = 53634696, capacity = 4112, live = 4128,
            buckets = 4144, hash_capacity = 4152, hash_empty = 4156,
            hash_multiplier = 4160, descriptors = 4168, records = 4184,
            ext_array = 4192, header_size = 88, stride = 440, ext_stride = 28,
            max_records = 4096, health = 20, life = 412, max_health = 20,
            descriptor_size = 24, descriptor_type = 0, descriptor_entity = 8,
            descriptor_flags = 20,
        },
        players = {
            global = 53634152, count = 132, peers = 712, peer_stride = 56,
            descriptors = 232, descriptor_entity = 8, max_players = 4,
            local_user = 55037680, local_peer = 45976,
            avatar_id = 936, avatar_id_stride = 32, no_network_id = 32767,
        },
        avatars = {
            entities = 54968216, map_slots = 15871688, map_capacity = 15871696,
            map_empty = 15871700, map_multiplier = 15871704,
            entity_base = 15937312, entity_stride = 24,
        },
        injury = {
            avatar_type = '4D1C334D294DFA97', zone_states = 32, zone_health = 248,
            -- 协议按 HUD 顺序发送：头、胸、左臂、右臂、左腿、右腿。
            zones = {
                { index = 0, max = 85 }, { index = 1, max = 60 },
                { index = 2, max = 35 }, { index = 3, max = 35 },
                { index = 4, max = 45 }, { index = 5, max = 45 },
            },
        },
    },
}

--------------------------------------------------------------------- 输出
local LOG_DIR, STATUS_PATH, LOG_PATH

local function join(a, b)
    if not a or a == '' then return b end
    local sep = (a:sub(-1) == '\\' or a:sub(-1) == '/') and '' or '\\'
    return a .. sep .. b
end

local function file_write(path, text, append)
    if not path then return false end
    local ok, f = pcall(io.open, path, append and 'a' or 'w')
    if not ok or not f then return false end
    pcall(f.write, f, text)
    pcall(f.close, f)
    return true
end

-- 固定长度内存日志，不从磁盘回读，也不在每帧读写日志文件。
local RECENT_LOG_LIMIT = 6
local recent_logs = {}
local function short_text(value, limit)
    local text = tostring(value):gsub('[%c]', ' ')
    local count = 0
    for pos in text:gmatch('()[%z\1-\127\194-\244][\128-\191]*') do
        count = count + 1
        if count > limit then return text:sub(1, pos - 1) .. '…' end
    end
    return text
end

local log_lines = 0
local LOG_HANDLE = nil      -- 优先用加载器给的日志句柄（它保证这个目录可用）

local function log(msg, force)
    -- 限流：addon 只有一次机会，日志刷爆等于没有日志
    if log_lines > 500 and not force then return end
    log_lines = log_lines + 1
    local line = string.format('[%s][%s] %s\n', os.date('%H:%M:%S'), ADDON, tostring(msg))
    recent_logs[#recent_logs + 1] = short_text('[' .. os.date('%H:%M:%S') .. '] ' .. tostring(msg), 350)
    if #recent_logs > RECENT_LOG_LIMIT then table.remove(recent_logs, 1) end
    local wrote = false
    if LOG_HANDLE then
        local ok = pcall(LOG_HANDLE.write, LOG_HANDLE, line)
        if ok then
            pcall(LOG_HANDLE.flush, LOG_HANDLE)
            wrote = true
        end
    end
    -- 同时也写自己的私有日志（两条路都留证据；私有日志是追加的，跨会话可查）
    if LOG_PATH and file_write(LOG_PATH, line, true) then wrote = true end
    if not wrote then pcall(print, '[hd2coyote] ' .. tostring(msg)) end
end

local bridge_detail = 'starting'
local function status(ok, reason)
    -- 只经本机心跳传简短状态，不传日志路径、报告或内存内容。
    bridge_detail = tostring(reason)
    local head = (ok and 'OK - ' or 'FAILED - ') .. tostring(reason)
    if STATUS_PATH then
        file_write(STATUS_PATH, head .. '\n' .. string.format(
            'addon=%s version=%s mode=%s profile=%s loader_api=%s\n',
            ADDON, VERSION, CONFIG.mode, CONFIG.profile, tostring(_G.__loader_api)), false)
    end
    log('STATUS: ' .. head, true)   -- 无论如何都写进日志（STATUS 文件写不了时这是唯一线索）
end

-- 只做「打开日志」这一件事：
--   * 绝不调用 os.execute / mkdir —— 那会在主线程同步 spawn cmd.exe，
--     一旦被安全软件/GameGuard 拦住就是"游戏停止响应 + 黑屏"（实测踩过）。
--   * 优先用加载器提供的 open_log（那个目录由加载器保证可用）。
local function resolve_log()
    local loader = rawget(_G, 'CowboyBingusModLoader')
    if type(loader) == 'table' and type(loader.open_log) == 'function' then
        local ok, handle = pcall(loader.open_log, 'Hd2CoyoteBridge.log')
        if ok and handle then LOG_HANDLE = handle end
    end
    local base = os.getenv and os.getenv('LOCALAPPDATA') or nil
    local dir = CONFIG.log_dir or (os.getenv and os.getenv('HD2COYOTE_DIR'))
        or (base and join(base, 'hd2coyote') or nil)
    if dir then
        LOG_DIR = dir
        LOG_PATH = join(dir, 'hd2_coyote_bridge.log')
        STATUS_PATH = join(dir, 'hd2_coyote_status.txt')
        if file_write(LOG_PATH, '', true) then return true end   -- 目录存在才能写；不存在就算了
        LOG_PATH, STATUS_PATH = nil, nil
    end
    return LOG_HANDLE ~= nil
end

--------------------------------------------------------------------- 用户配置
-- 可选：%LOCALAPPDATA%\hd2coyote\bridge_config.lua（或 bridge_config.txt）
-- 两种写法都支持：
--   return { config = { mode = 'menu', port = 47777 } }         -- Lua 形式
--   mode = menu                                                -- 纯文本形式（最保险）
-- 内存 profile 只随已测试版本发布，用户配置不能覆盖，避免旧配置破坏构建校验。
--
-- ★ 踩过的坑：以前用 loadfile() 读，而且**失败时静默返回** ——
--   配置文件明明在，addon 却一直跑默认值，日志里一个字都没有，白排查一轮。
--   现在只用「io.open 读 + loadstring 编译」（这两个在本环境已被证明可用：
--   加载器自己就用 loadstring），并且**每一步都写日志**。
local MODES = { safe = true, net = true, menu = true, recon = true, live = true }

local function read_file(path)
    local ok, f = pcall(io.open, path, 'r')
    if not ok or not f then return nil, 'io.open 失败' end
    local text = f:read('*a')
    pcall(f.close, f)
    if type(text) ~= 'string' then return nil, '读取失败' end
    return text
end

local function parse_plain_config(text)
    local out = {}
    for line in text:gmatch('[^\n]+') do
        local code = line:gsub('%-%-.*$', '')                     -- 去掉行内注释
        for key, value in code:gmatch('([%a_][%w_]*)%s*=%s*([^%s,;]+)') do
            value = value:gsub('^["\']', ''):gsub('["\']$', '')   -- 去掉引号
            out[key] = tonumber(value) or value
        end
    end
    return out
end

-- 跳过开头的空行与 `--` 注释行。
-- ★ 踩过的坑：Fluent 界面生成的文件以注释开头，而我原来只看"第一个非空 token 是不是
--   return"，`--` 不是字母 → 被判成纯文本 → mode 变成带引号的 "'menu'" → 未知档位 → 回退 safe。
local function strip_leading_comments(text)
    local rest = text:gsub('^%s+', '')
    while rest:sub(1, 2) == '--' do
        local nl = rest:find('\n', 1, true)
        if not nl then return '' end
        rest = rest:sub(nl + 1):gsub('^%s+', '')
    end
    return rest
end

local function apply_user_config()
    if not LOG_DIR then
        log('没有可用的配置目录，跳过用户配置')
        return false
    end
    local path = join(LOG_DIR, 'bridge_config.lua')
    local text, why = read_file(path)
    if not text then
        local alt = join(LOG_DIR, 'bridge_config.txt')
        local alt_text, alt_why = read_file(alt)
        if not alt_text then
            log('未找到用户配置（' .. path .. '：' .. tostring(why)
                .. '；' .. alt .. '：' .. tostring(alt_why) .. '），使用内置默认')
            return false
        end
        path, text = alt, alt_text
    end

    local user
    if strip_leading_comments(text):match('^return') then
        if type(loadstring) ~= 'function' then
            log('本环境没有 loadstring，无法解析 Lua 形式的配置（可改用 mode = menu 这种纯文本）')
            return false
        end
        local chunk, err = loadstring(text, '@' .. path)
        if not chunk then
            log('bridge_config 语法错误：' .. tostring(err) .. '（忽略配置）')
            return false
        end
        local ok, res = pcall(chunk)
        if not ok then
            log('bridge_config 执行出错：' .. tostring(res) .. '（忽略配置）')
            return false
        end
        if type(res) ~= 'table' then
            log('bridge_config 必须 return 一个表，实际是 ' .. type(res) .. '（忽略配置）')
            return false
        end
        user = res
    else
        user = { config = parse_plain_config(text) }
        log('按纯文本 key = value 解析：' .. path)
    end

    local applied = {}
    for k, v in pairs(user.config or {}) do
        CONFIG[k] = v
        applied[#applied + 1] = tostring(k) .. '=' .. tostring(v)
    end
    if next(user.profiles or {}) ~= nil then
        log('已忽略用户配置里的 profiles：自动读取布局只能随经验证版本发布，不能手工覆盖')
    end

    if type(CONFIG.mode) ~= 'string' or not MODES[CONFIG.mode] then
        log('未知 mode=' .. tostring(CONFIG.mode) .. '（可选 safe/net/menu/recon/live），回退 safe')
        CONFIG.mode = 'safe'
    end
    log('已应用配置 ' .. path .. '：'
        .. (#applied > 0 and table.concat(applied, ', ') or '(没有 config 项)'))
    return true
end

--------------------------------------------------------------------- 环境闸门
local function loader_api()
    local l = rawget(_G, 'CowboyBingusModLoader')
    if type(l) == 'table' then
        return tonumber(l.api), tonumber(l.version)
    end
    -- 读不到 global 不直接拒绝（将来可能有别的 loader），退化去翻 loader 日志首行
    local base = os.getenv and os.getenv('LOCALAPPDATA')
    if base then
        local p = join(join(join(base, 'CowboyBingus'), 'Helldivers2'), 'Logs\\BingusSharedLoader.log')
        local ok, f = pcall(io.open, p, 'r')
        if ok and f then
            local first = f:read('*l') or ''
            f:close()
            local v = first:match('loader%-v(%d+)')
            local a = first:match('API (%d+)')
            return a and tonumber(a) or nil, v and tonumber(v) or nil
        end
    end
    return nil, nil
end

--------------------------------------------------------------------- FFI
local ffi_ok, ffi = pcall(require, 'ffi')
local ffi_reason = ffi_ok and nil or 'ffi 不可用'
local ffi_ready = false

local function init_ffi()
    if ffi_ready then return true end
    if not ffi_ok then return false end
    local ok = pcall(function()
        -- ★ 全部用私有名 + __asm__ 别名：加载器文档要求（整个游戏只有一份 cdef，
        --   第一个声明生效；用共享名会和别的 mod 打架）。
        ffi.cdef[[
            void* Hd2Coyote_GetModuleHandleA(const char* name) __asm__("GetModuleHandleA");
            unsigned long long Hd2Coyote_GetTickCount64(void) __asm__("GetTickCount64");
            unsigned long long Hd2Coyote_socket(int af, int type, int protocol) __asm__("socket");
            int Hd2Coyote_sendto(unsigned long long s, const char* buf, int len, int flags,
                                 const void* to, int tolen) __asm__("sendto");
            typedef struct {
                void*    BaseAddress;
                void*    AllocationBase;
                uint32_t AllocationProtect;
                uint32_t __pad1;
                uint64_t RegionSize;
                uint32_t State;
                uint32_t Protect;
                uint32_t Type;
                uint32_t __pad2;
            } Hd2CoyoteMBI;
            size_t Hd2Coyote_VirtualQuery(const void* addr, Hd2CoyoteMBI* buf, size_t len)
                __asm__("VirtualQuery");
        ]]
        -- ★ 显式 load 两个 DLL：
        --   socket/sendto 在 ws2_32，不在 exe/kernel32 —— 走 ffi.C 会解析不到符号，
        --   表现就是"UDP 静默失效"（net.ready=false，控制器收不到任何东西）。
        K32 = ffi.load('kernel32')
        WS2 = ffi.load('ws2_32')
    end)
    if not ok then
        ffi_reason = 'ffi.cdef / ffi.load 失败'
        return false
    end
    ffi_ready = true
    return true
end

-- ★★ 崩溃防线 ★★
-- LuaJIT 的 ffi.string 撞到无效地址 = 进程级访问违例，pcall 完全挡不住 —— 游戏直接崩。
-- 所以**任何一次读内存之前，先用 VirtualQuery 确认这段地址真的已提交且可读**。
-- 偏移过期时最坏结果是"读到垃圾"（后面的合理性校验会拒绝），而不是把游戏带走。
-- 拿不到 VirtualQuery 时宁可什么都不读。
local MEM_COMMIT = 0x1000
local PAGE_READABLE = {
    [0x02] = true, -- PAGE_READONLY
    [0x04] = true, -- PAGE_READWRITE
    [0x08] = true, -- PAGE_WRITECOPY
    [0x20] = true, -- PAGE_EXECUTE_READ
    [0x40] = true, -- PAGE_EXECUTE_READWRITE
    [0x80] = true, -- PAGE_EXECUTE_WRITECOPY
}

local function range_readable(addr, n)
    if not addr or addr < 0x10000 or addr > 0x7FFFFFFFFFFF then return false end
    if not (ffi_ready and K32) then return false end
    local mbi = ffi.new('Hd2CoyoteMBI[1]')
    local ok, got = pcall(function()
        return tonumber(K32.Hd2Coyote_VirtualQuery(ffi.cast('const void*', addr), mbi,
                                                   ffi.sizeof(mbi)))
    end)
    if not ok or not got or got == 0 then return false end
    local m = mbi[0]
    if tonumber(m.State) ~= MEM_COMMIT then return false end
    local prot = tonumber(m.Protect) or 0
    if math.floor(prot / 0x100) % 2 == 1 then return false end -- PAGE_GUARD：绝不触碰
    if not PAGE_READABLE[prot % 0x100] then return false end    -- NOCACHE/WRITECOMBINE 不改变可读属性
    local start = tonumber(ffi.cast('uintptr_t', m.BaseAddress))
    local size = tonumber(m.RegionSize)
    if not start or not size then return false end
    return (addr + n) <= (start + size)
end

-- 数据布局偏移属于 game.dll，不属于 helldivers2.exe。
-- ASLR 每次运行都不同，所以必须取 game.dll 基址再加相对偏移。
-- 旧版错误地 GetModuleHandleA(nil) 取了主 EXE，导致 ping actor 指针落到无关地址。
local function module_base()
    if not init_ffi() then return nil end
    local ok, base = pcall(function()
        return tonumber(ffi.cast('uintptr_t', K32.Hd2Coyote_GetModuleHandleA('game.dll')))
    end)
    if not ok or not base or base == 0 then
        ffi_reason = '拿不到 game.dll 基址'
        return nil
    end
    return base
end

local function now_ms()
    if init_ffi() then
        local ok, v = pcall(function() return tonumber(K32.Hd2Coyote_GetTickCount64()) end)
        if ok and v then return v end
    end
    return (os.clock() or 0) * 1000
end

-- 只读内存访问器。
-- ★ 唯一的取值入口是 at()：**先 range_readable 验址，再读**。
--   pcall 只能挡 Lua 层错误，挡不住访问违例；验址才是防崩的关键。
local function reader(base)
    local R = { base = base }
    local function at(addr, n)
        if not addr or addr == 0 then return nil end
        if not range_readable(addr, n) then return nil end
        local ok, res = pcall(function()
            return ffi.string(ffi.cast('const char*', addr), n)
        end)
        if not ok or not res or #res ~= n then return nil end
        return res
    end
    function R.bytes(off, n)
        if not base then return nil end
        return at(base + off, n)
    end
    function R.u8(off)
        local s = R.bytes(off, 1); if not s then return nil end
        return s:byte(1)
    end
    function R.u32(off)
        local s = R.bytes(off, 4); if not s then return nil end
        local b1, b2, b3, b4 = s:byte(1, 4)
        return b1 + b2 * 256 + b3 * 65536 + b4 * 16777216
    end
    function R.i32(off)
        local value = R.u32(off)
        if value == nil then return nil end
        return value >= 2147483648 and value - 4294967296 or value
    end
    function R.u64hex(off)
        local s = R.bytes(off, 8); if not s then return nil end
        local out = {}
        for i = 8, 1, -1 do out[#out + 1] = string.format('%02X', s:byte(i)) end
        return table.concat(out)
    end
    function R.ptr(off)
        local hex = R.u64hex(off); if not hex then return nil end
        if hex == '0000000000000000' then return nil end
        -- 坑：LuaJIT 的 tonumber(hex, 16) 只按 32 位解析！
        -- 0x141000000 会被截成 0x41000000。必须逐字符自己算。
        local v = 0
        for i = 1, #hex do
            local d = tonumber(hex:sub(i, i), 16)
            if not d then return nil end
            v = v * 16 + d
        end
        return v
    end
    -- IEEE754 小端 float（不依赖 ffi.cast 的浮点，纯字节运算，方便离线测试）
    function R.f32(off)
        local s = R.bytes(off, 4); if not s then return nil end
        local b1, b2, b3, b4 = s:byte(1, 4)
        local sign = (b4 >= 128) and -1 or 1
        local exp = (b4 % 128) * 2 + math.floor(b3 / 128)
        local mant = ((b3 % 128) * 65536 + b2 * 256 + b1) / 8388608
        if exp == 0 then return sign * mant * 2 ^ -126 end
        if exp == 255 then return nil end
        return sign * (1 + mant) * 2 ^ (exp - 127)
    end
    function R.hex(off, n) return R.bytes(off, n) end
    return R
end

--------------------------------------------------------------------- UDP
local net = { ready = false, sent = 0, failed = 0 }

local function init_net()
    if net.ready then return true end
    if not init_ffi() then return false end
    local ok = pcall(function()
        local sock = WS2.Hd2Coyote_socket(2, 2, 17)  -- AF_INET, SOCK_DGRAM, IPPROTO_UDP
        if sock == nil then error('socket() 失败') end
        net.sock = sock
        local a, b, c, d = CONFIG.host:match('^(%d+)%.(%d+)%.(%d+)%.(%d+)$')
        if not a then error('非法 host') end
        local family = string.char(2, 0)
        local port = string.char(math.floor(CONFIG.port / 256), CONFIG.port % 256)
        local addr = string.char(tonumber(a), tonumber(b), tonumber(c), tonumber(d))
        net.sockaddr = family .. port .. addr .. string.char(0, 0, 0, 0, 0, 0, 0, 0)
        net.sockaddr_len = #net.sockaddr
    end)
    if not ok then return false end
    net.ready = true
    return true
end

local function send(text)
    if not net.ready then return false end
    local ok = pcall(function()
        local n = WS2.Hd2Coyote_sendto(net.sock, text, #text, 0, net.sockaddr, net.sockaddr_len)
        if n == nil or tonumber(n) < 0 then error('sendto 失败') end
    end)
    net.last_ok = ok
    if ok then net.sent = net.sent + 1 else net.failed = net.failed + 1 end
    return ok
end

-- 最小 JSON 编码：只发扁平对象 + 数字数组，避免引第三方库
local function json(pairs_list)
    local parts = {}
    for _, kv in ipairs(pairs_list) do
        local k, v = kv[1], kv[2]
        local enc
        if type(v) == 'number' then
            enc = string.format('%.4f', v)
            enc = enc:gsub('0+$', ''):gsub('%.$', '')
            if enc == '' or enc == '-' then enc = '0' end
        elseif type(v) == 'boolean' then
            enc = v and '1' or '0'
        elseif type(v) == 'table' then
            local items = {}
            for i = 1, #v do items[i] = tostring(tonumber(v[i]) or 0) end
            enc = '[' .. table.concat(items, ',') .. ']'
        else
            enc = '"' .. tostring(v):gsub('[^%w%._%-]', '') .. '"'
        end
        parts[#parts + 1] = '"' .. k .. '":' .. enc
    end
    return '{' .. table.concat(parts, ',') .. '}'
end

--------------------------------------------------------------------- 构建验证与本地玩家定位
local function hex_bytes(hex)
    local out = {}
    for pair in tostring(hex):gmatch('%x%x') do out[#out + 1] = string.char(tonumber(pair, 16)) end
    return table.concat(out)
end

-- 读取任何 profile 地址之前先证明当前 game.dll 就是研究所对应的映像。
-- 进程内用 PE SizeOfImage + 分散在关键读取路径上的多处指令 pin，防止更新后误读。
local function verify_profile(R, P)
    if P.__verified then return true end
    local mz = R.bytes(0, 2)
    local pe = R.u32(0x3C)
    if mz ~= 'MZ' or not pe or pe < 0x40 or pe > 0x1000 then return false, 'game.dll PE 头无效' end
    if R.bytes(pe, 4) ~= 'PE\0\0' then return false, 'game.dll PE 签名无效' end
    local image_size = R.u32(pe + 80)
    if image_size ~= P.image_size then
        return false, string.format('game.dll 映像大小不匹配（%s，期望 %s）', tostring(image_size), tostring(P.image_size))
    end
    for _, pin in ipairs(P.pins or {}) do
        local expected = hex_bytes(pin.hex)
        if #expected == 0 or R.bytes(pin.rva, #expected) ~= expected then
            return false, '游戏构建不匹配：' .. tostring(pin.label)
        end
    end
    P.__verified = true
    return true
end

-- (a*b) mod 2^32；拆成 16 位，避免 Lua number 乘法超过 2^53 后丢精度。
local function mul32(a, b)
    local low, high = b % 65536, math.floor(b / 65536)
    return (a * low + ((a * high) % 65536) * 65536) % 4294967296
end

local function power_of_two(value)
    if not value or value < 1 or value % 1 ~= 0 then return false end
    while value > 1 do
        if value % 2 ~= 0 then return false end
        value = value / 2
    end
    return true
end

local function network_entity(R, P, network_id)
    local A = P.avatars
    if not network_id or network_id == P.players.no_network_id then return nil end
    local manager = R.ptr(A.entities)
    if not manager then return nil end
    local M = reader(manager)
    local slots = M.ptr(A.map_slots)
    local capacity = M.u32(A.map_capacity)
    local empty = M.u32(A.map_empty)
    local multiplier = M.u32(A.map_multiplier)
    if not slots or not capacity or not empty or not multiplier
        or capacity > 1048576 or not power_of_two(capacity) then return nil end
    local S = reader(slots)
    local start = mul32(network_id, multiplier)
    for probe = 0, math.min(capacity, 4096) - 1 do
        local off = ((start + probe) % capacity) * 8
        local key = S.u32(off)
        if key == nil then return nil end
        if key == network_id then
            local index = S.u32(off + 4)
            return index and M.u32(A.entity_base + index * A.entity_stride) or nil
        end
        if key == empty then return nil end
    end
    return nil
end

-- 不再依赖“玩家必须先发过 ping”。直接从玩家管理器匹配本机 peer，再解析其 avatar network id。
local function locate_local_player(R, P)
    local function find()
        local valid, why = verify_profile(R, P)
        if not valid then return nil, why end
        local L = P.players
        local user = R.ptr(L.local_user)
        local manager = R.ptr(L.global)
        if not user then return nil, '本地用户上下文尚未就绪' end
        if not manager then return nil, '玩家管理器尚未就绪' end
        local local_peer = reader(user).u64hex(L.local_peer)
        if not local_peer then return nil, '读不到本地 peer id' end
        local M = reader(manager)
        local count = M.u32(L.count)
        if count == nil then return nil, '读不到玩家数量' end
        if count > L.max_players then return nil, '玩家数量异常（' .. tostring(count) .. '）' end
        for slot = 0, count - 1 do
            if M.u64hex(L.peers + slot * L.peer_stride) == local_peer then
                local descriptor = M.ptr(L.descriptors + slot * 8)
                local player_entity = descriptor and reader(descriptor).u32(L.descriptor_entity) or nil
                local network_id = M.u32(L.avatar_id + slot * L.avatar_id_stride)
                local avatar = network_entity(R, P, network_id)
                if avatar and avatar ~= 0 and avatar ~= 0xFFFFFFFF then
                    return { entity_id = avatar, id = network_id, address = descriptor,
                             player_entity = player_entity, slot = slot }
                end
                return nil, '本地玩家尚无可用 avatar（飞船切换/重生时会自动重试）'
            end
        end
        return nil, '玩家列表中未找到本地 peer'
    end
    local ok, player, why = pcall(find)
    if not ok then return nil, '定位异常：' .. tostring(player) end
    return player, why
end

local function health_header(R, H)
    local manager = R.ptr(H.global)
    if not manager then return nil, '生命管理器尚未就绪' end
    local M = reader(manager)
    local capacity, live, hash_capacity = M.u32(H.capacity), M.u32(H.live), M.u32(H.hash_capacity)
    if not capacity or capacity == 0 or capacity > H.max_records or not live or live > capacity
        or not hash_capacity or hash_capacity > 65536 or not power_of_two(hash_capacity) then
        return nil, '生命管理器头无效'
    end
    local header = {
        manager = manager, capacity = capacity, live = live, hash_capacity = hash_capacity,
        hash_empty = M.u32(H.hash_empty), hash_multiplier = M.u32(H.hash_multiplier),
        buckets = M.ptr(H.buckets), descriptors = M.ptr(H.descriptors),
        records = M.ptr(H.records), ext = M.ptr(H.ext_array),
    }
    if header.hash_empty == nil or header.hash_multiplier == nil or not header.buckets
        or not header.descriptors or not header.records then return nil, '生命管理器指针无效' end
    return header
end

local function health_index(header, entity)
    local B = reader(header.buckets)
    local start = mul32(entity, header.hash_multiplier)
    for probe = 0, header.hash_capacity - 1 do
        local off = ((start + probe) % header.hash_capacity) * 8
        local key = B.u32(off)
        if key == nil then return nil end
        if key == entity then
            local index = B.u32(off + 4)
            return index and index < header.live and index or nil
        end
        if key == header.hash_empty then return nil end
    end
    return nil
end

local function zone_state(record, offset, index)
    local word = record.u32(offset + math.floor(index / 16) * 4)
    return word and math.floor(word / (4 ^ (index % 16))) % 4 or nil
end

--------------------------------------------------------------------- 状态读取
-- 返回真实 health record：整数 HP、六肢伤势、倒地/阵亡。每次都重解 hash 与 descriptor，拒绝陈旧实体。
local function read_state(R, P, player)
    if not player or not player.entity_id then return nil, 'NO_PLAYER' end
    local valid, why = verify_profile(R, P)
    if not valid then return nil, why end
    local H, I = P.health, P.injury
    local header, header_why = health_header(R, H)
    if not header then return nil, header_why end
    local index = health_index(header, player.entity_id)
    if index == nil then return nil, '本地 avatar 暂无生命记录' end
    local descriptor = reader(header.descriptors).ptr(index * 8)
    if not descriptor then return nil, '生命 descriptor 不可读' end
    local D = reader(descriptor)
    if D.u32(H.descriptor_entity) ~= player.entity_id then return nil, '生命 descriptor 已失效' end
    if D.u64hex(H.descriptor_type) ~= I.avatar_type then return nil, '实体不是 Helldiver avatar' end
    local flags = D.u32(H.descriptor_flags)
    if flags == nil or flags % 2 ~= 1 then return nil, 'avatar 不属于本机' end

    local record = reader(header.records + index * H.stride)
    local hp, life = record.i32(H.health), record.u32(H.life)
    if hp == nil or life == nil or hp < -100000 or hp > 100000 or life > 16 then
        return nil, '生命记录数值异常'
    end
    local hp_max = header.ext and reader(header.ext + index * H.ext_stride).u32(H.max_health) or nil
    if not hp_max or hp_max == 0 or hp_max > 100000 then return nil, '最大生命值无效' end

    local limbs = {}
    for slot, zone in ipairs(I.zones) do
        local value = record.i32(I.zone_health + zone.index * 4)
        local state = zone_state(record, I.zone_states, zone.index)
        if value == nil or state == nil or value < -100000 or value > 100000 or state > 3 then
            return nil, '肢体生命记录异常（slot=' .. tostring(slot) .. '）'
        end
        -- 游戏自己的受伤判定是 zone state == 2；zone health 仅用于合理性验证，
        -- 不把 0 HP 猜成受伤，避免不同护甲/状态下产生假事件。
        limbs[slot] = state == 2 and 1 or 0
    end
    return { hp = math.max(0, hp), hp_max = hp_max, limbs = limbs,
             bleeding = 0, downed = life == 1 and 1 or 0, dead = life >= 2 and 1 or 0 }
end

-- 把原始字节串格式化成"16 进制 dump"文本。
-- （踩过：直接把字节串当 hex 文本 concat 进报告 → 报告里混进裸字节，无法阅读/不是 UTF-8）
local function hexdump_lines(data)
    local lines = {}
    for i = 1, #data, 16 do
        local chunk = data:sub(i, i + 15)
        local parts = {}
        for j = 1, #chunk do parts[#parts + 1] = string.format('%02X', chunk:byte(j)) end
        while #parts < 16 do parts[#parts + 1] = '  ' end
        lines[#lines + 1] = string.format('%04X  %s', i - 1, table.concat(parts, ' '))
    end
    return lines
end

--------------------------------------------------------------------- recon
-- 一次性、有界、带落盘证据。做完即终止，不做周期扫描（帧率优先）。
local function recon(R, P, base, player)
    local out, health_ok = {}, false
    local function say(s) out[#out + 1] = s; log(s) end
    say(string.format('module_base=0x%X profile=%s build=%s', base, CONFIG.profile,
                      tostring(P.build)))

    -- 1) Lua 侧全局表：哪些函数/表存在（用来确定每帧钩子的真实名字）
    local names, funcs, tables = 0, {}, {}
    local ok = pcall(function()
        for k, v in pairs(_G) do
            names = names + 1
            if names > 400 then break end
            if type(k) == 'string' then
                if type(v) == 'function' then funcs[#funcs + 1] = k
                elseif type(v) == 'table' then tables[#tables + 1] = k end
            end
        end
    end)
    if ok then
        table.sort(funcs); table.sort(tables)
        say('globals_functions=' .. table.concat(funcs, ',', 1, math.min(#funcs, 120)))
        say('globals_tables=' .. table.concat(tables, ',', 1, math.min(#tables, 120)))
    else
        say('globals= 枚举失败（_G 不可遍历）')
    end

    -- 2) 本地玩家 + 自动解析结果；只对已证明的 descriptor 做有界 dump。
    if player then
        say(string.format('local_avatar_entity=%d network_id=%d player_descriptor=0x%X',
                          player.entity_id, player.id, player.address))
        local state, why = read_state(R, P, player)
        if state then
            health_ok = true
            say(string.format('health=OK hp=%d/%d limbs=%s dead=%d', state.hp, state.hp_max,
                              table.concat(state.limbs, ','), state.dead))
        else
            say('health=FAILED（' .. tostring(why) .. '）')
        end
        local n = math.min(CONFIG.max_dump_bytes, 0x400)
        local raw = reader(player.address).hex(0, n)
        if raw then say('descriptor_hex_dump:\n' .. table.concat(hexdump_lines(raw), '\n')) end
    else
        say('local_player=UNKNOWN（' .. tostring(P.__why or '未定位') .. '）')
        say('提示：飞船切换或重生期间 avatar 可能暂时不存在；等待后再运行 recon')
    end

    file_write(join(LOG_DIR, 'recon_report.txt'), table.concat(out, '\n') .. '\n', false)
    return health_ok
end

--------------------------------------------------------------------- 游戏内连接信息与日志
-- 前向声明：连接信息展示使用帧钩子状态。
local install_frame_hook, HOOK

-- ModOptionsMenu api=1 没有纯文本/禁用行。用相同的两个选择文字承载信息，
-- 不读取持久化值，也不注册任何配置修改回调；不调用 set 干扰共享保存计时。
-- version >=2 的动态文本在重新打开 ESC 菜单时刷新，不冒充实时连接确认。
local MENU = {
    mod_id = 'hd2coyote', display = 'HD2 COYOTE 郊狼',
    registered = false, gave_up = false, attempts = 0, version = 0,
    specs = {}, ids = {},
}

local function menu()
    local m = rawget(_G, 'ModOptionsMenu')
    if type(m) ~= 'table' or m.api ~= 1 then return nil end
    return m
end

local function opt_id(suffix) return MENU.mod_id .. '.info.' .. suffix end

local function menu_text(fn)
    if MENU.version >= 2 then return fn end
    return '请升级 Mod Options Menu v1.1+；状态与日志请在桌面端查看。'
end

local function build_menu_specs()
    MENU.specs = {
        { suffix = 'endpoint', label = '连接目标 Endpoint', text = function()
            return 'UDP → ' .. tostring(CONFIG.host) .. ':' .. tostring(CONFIG.port)
                .. '；单向上报，无法确认桌面端接收或 DG-LAB 设备连接。'
        end },
        { suffix = 'transport', label = '本地发送状态 Transport', text = function()
            if not net.ready then return 'UDP 未就绪；请检查桌面端诊断与桥日志。' end
            if net.last_ok == false then return '最近一次发送失败；不代表设备状态。' end
            if net.last_ok == true then return '最近一次 UDP 已交给本机网络栈；对端接收未确认。' end
            return 'UDP 本地已就绪，尚未发送；对端接收未确认。'
        end },
        { suffix = 'packets', label = '发送统计 Packets', text = function()
            return string.format('本次会话：发送成功 %d / 失败 %d。成功仅指本地 sendto 返回成功。',
                                 net.sent, net.failed)
        end },
        { suffix = 'bridge', label = '桥接信息 Bridge', text = function()
            return 'v' .. VERSION .. ' / mode=' .. tostring(CONFIG.mode)
                .. ' / profile=' .. tostring(CONFIG.profile)
                .. ' / 上报开关=' .. (CONFIG.enabled and '开' or '关')
                .. ' / hook=' .. tostring(HOOK and HOOK.name or '未挂载')
                .. '。配置请在桌面端修改后重启游戏。'
        end },
    }
    for i = 1, RECENT_LOG_LIMIT do
        local slot = i
        MENU.specs[#MENU.specs + 1] = {
            suffix = 'log' .. slot, label = '最近日志 ' .. slot .. '（最新在前）', gap = slot == 1,
            text = function() return recent_logs[#recent_logs - slot + 1] or '暂无日志' end,
        }
    end
end

local function register_options(m)
    build_menu_specs()
    local failed = {}
    for _, opt in ipairs(MENU.specs) do
        local summary = MENU.version >= 2 and function() return short_text(opt.text(), 40) end
            or '请升级菜单 / 查看说明'
        local spec = {
            type = 'choice', label = opt.label, mod = MENU.display, mod_id = MENU.mod_id,
            default = 1, choices = { summary, summary }, gap = opt.gap,
            description = menu_text(function()
                return short_text(opt.text() .. '（重新打开 ESC 刷新）', 390)
            end),
        }
        local id = opt_id(opt.suffix)
        local ok, why = m.register_option(id, spec)
        if not ok then
            failed[#failed + 1] = opt.suffix .. '(' .. tostring(why) .. ')'
        else
            MENU.ids[#MENU.ids + 1] = id
        end
    end
    MENU.registered = true
    if #failed > 0 then log('菜单：部分信息注册失败 -> ' .. table.concat(failed, ',')) end
    log(string.format('菜单：已注册 %d 个只读信息行（menu version=%s）',
                      #MENU.ids, tostring(MENU.version)))
    return #failed == 0
end

-- 每帧重试注册：加载器只 require 一次，而 Mod Options Menu 可能比我们晚加载。
-- 有终止状态：重试 CONFIG.menu_retry_frames 帧后放弃并记日志。
local function menu_tick()
    if MENU.registered or MENU.gave_up then return end
    local m = menu()
    if not m then
        MENU.attempts = MENU.attempts + 1
        if MENU.attempts >= CONFIG.menu_retry_frames then
            MENU.gave_up = true
            log('菜单：等不到 ModOptionsMenu，放弃注册（未安装时属正常）')
        end
        return
    end
    MENU.version = tonumber(m.version) or 0
    local ok, err = pcall(register_options, m)
    if not ok then
        MENU.gave_up = true
        log('菜单注册异常：' .. tostring(err))
    end
end

--------------------------------------------------------------------- live
local live = { active = false, last = 0, player = nil, failures = 0, last_relocate = 0,
               R = nil, P = nil, base = nil, ready = false, wait_reason = nil }

-- 仅在状态变化时写日志；定位失败按固定间隔退避，不逐帧扫描。
local function wait_for_player(why)
    live.ready = false
    if live.wait_reason ~= why then
        live.wait_reason = why
        status(false, 'live 自动等待玩家数据（每 3 秒重试）：' .. tostring(why))
    end
end

local function live_step()
    local R, P = live.R, live.P
    if not R or not P then return end
    local now = now_ms()
    if now - live.last < CONFIG.interval * 1000 then return end
    live.last = now

    if not live.player then
        if now - live.last_relocate < 3000 then return end
        live.last_relocate = now
        local player, why = locate_local_player(R, P)
        if not player then
            wait_for_player(why)
            return
        end
        live.player = player
    end

    local st, why = read_state(R, P, live.player)
    if not st then
        live.failures = live.failures + 1
        -- 丢弃失效锚点，停止 state 上报；心跳不能延长桌面端旧数据的有效期。
        live.player = nil
        live.last_relocate = now
        wait_for_player(why)
        return
    end
    if not live.ready then
        live.ready, live.wait_reason = true, nil
        status(true, string.format('live 自动读取中（profile=%s hook=%s source=health_manager）',
                                  CONFIG.profile, tostring(HOOK.name or '?')))
    end
    live.failures = 0
    send(json({
        { 'v', PROTOCOL }, { 'ev', 'state' },
        { 't', now / 1000 }, { 'hp', st.hp }, { 'hp_max', st.hp_max },
        { 'limbs', st.limbs }, { 'bleeding', st.bleeding }, { 'dead', st.dead },
    }))
end

local function start_live()
    local P = PROFILES[CONFIG.profile]
    if not P then
        status(false, '未知 profile：' .. tostring(CONFIG.profile))
        return false
    end
    if not (P.health and P.players and P.avatars and P.injury) then
        status(false, 'live profile 缺少自动读取布局')
        return false
    end
    live.base = live.base or module_base()
    if not live.base then
        status(false, '拿不到模块基址：' .. tostring(ffi_reason))
        return false
    end
    if not HOOK.installed then
        status(false, '找不到每帧钩子：CONFIG.frame_hooks 里没有真实存在的全局函数；'
            .. '候选见 recon_report.txt 的 globals_functions，请在 bridge_config.lua 中设置 frame_hooks 并重启游戏')
        return false
    end
    live.R = live.R or reader(live.base)
    live.P = P
    local player, why = locate_local_player(live.R, P)
    local now = now_ms()
    live.player, live.active, live.last = player, true, now
    live.last_relocate, live.ready, live.wait_reason = now, false, nil
    if not player then
        wait_for_player(why)
    else
        status(true, string.format('live 自动读取已启动，等待首个有效状态（profile=%s hook=%s source=health_manager）',
                                   CONFIG.profile, tostring(HOOK.name or '?')))
    end
    return true
end

--------------------------------------------------------------------- 每帧钩子
-- 官方第三方参考：CowboyBingus 的 mod 普遍包装全局 update。
-- 包装规则（照抄参考里的要求）：参数与返回值原样透传，自己的错误不许影响下面。
HOOK = { name = nil, prev = nil, installed = false, active = true }

local last_heartbeat = nil
local function heartbeat_tick()
    if not net.ready or CONFIG.mode == 'safe' then return end
    local now = now_ms()
    if last_heartbeat and now - last_heartbeat < 1000 then return end
    last_heartbeat = now
    local P = PROFILES[CONFIG.profile] or {}
    send(json({ { 'v', PROTOCOL }, { 'ev', 'heartbeat' }, { 'ver', VERSION },
                { 'build', P.build or '' }, { 'profile', CONFIG.profile },
                { 'mode', CONFIG.mode }, { 'detail', bridge_detail:sub(1, 300) } }))
end

local function frame_tick()
    if not HOOK.active then return end
    local hb_ok, hb_err = pcall(heartbeat_tick)
    if not hb_ok then log('heartbeat tick 异常：' .. tostring(hb_err)) end
    local ok, err = pcall(menu_tick)
    if not ok then log('菜单 tick 异常：' .. tostring(err)) end
    if CONFIG.enabled and live.active then
        local ok2, err2 = pcall(live_step)
        if not ok2 then log('live_step 异常：' .. tostring(err2)) end
    end
end

function install_frame_hook()
    if HOOK.installed then return HOOK.name end
    for _, name in ipairs(CONFIG.frame_hooks) do
        if name == 'none' then return nil end
        local fn = rawget(_G, name)
        if type(fn) == 'function' then
            local wrapper = function(...)
                frame_tick()
                return fn(...)
            end
            rawset(_G, name, wrapper)
            HOOK.name, HOOK.prev, HOOK.installed = name, fn, true
            log('已挂钩子：' .. name)
            return name
        end
    end
    return nil
end

--------------------------------------------------------------------- 主流程
local function main()
    -- 第一步只开日志（不 mkdir、不 os.execute、不碰 FFI）
    if not resolve_log() then return end
    log(string.format('启动 v%s', VERSION))
    apply_user_config()
    log(string.format('生效模式 = %s（配置目录 %s）', tostring(CONFIG.mode), tostring(LOG_DIR)))

    local api, loader_version = loader_api()
    _G.__loader_api = api
    log(string.format('loader_api=%s loader_version=%s ffi=%s',
                      tostring(api), tostring(loader_version), tostring(ffi_ok)))

    if api and api < 1 then
        status(false, 'Bingus Shared Loader 太旧（需要 v15+ / API 1）')
        return
    end

    -- === safe 模式：到此为止。不碰 FFI、不读内存、不挂钩子、不发 UDP、不注册菜单。 ===
    -- 只用来回答一个问题：这个 addon 到底有没有被游戏加载、日志能不能写。
    if CONFIG.mode == 'safe' then
        status(true, 'safe 模式：只写日志，不做任何 FFI / 内存 / 菜单操作')
        return
    end

    -- === net 及以上：FFI + UDP 上报（还不挂钩子、不读游戏内存） ===
    if not init_ffi() then
        status(false, 'FFI 不可用：' .. tostring(ffi_reason))
        return
    end

    local base = module_base()
    if not base then
        status(false, '拿不到模块基址：' .. tostring(ffi_reason))
        return
    end
    live.base = base
    log(string.format('模块基址 = 0x%X', base))

    local P = PROFILES[CONFIG.profile]
    if not P then
        status(false, '未知 profile：' .. tostring(CONFIG.profile))
        return
    end

    local R = reader(base)
    live.R = R
    init_net()
    log('UDP 初始化：' .. (net.ready and '成功' or '失败（将只写本地日志）'))
    local hello_ok = send(json({ { 'v', PROTOCOL }, { 'ev', 'hello' }, { 'ver', VERSION },
                                 { 'build', P.build }, { 'profile', CONFIG.profile },
                                 { 'mode', CONFIG.mode } }))
    log('已发送 hello：' .. tostring(hello_ok))

    if CONFIG.mode == 'net' then
        status(true, 'net 模式：FFI 与 UDP 就绪（hello 已发），未挂钩子、未读游戏内存')
        return
    end

    -- === menu 及以上：每帧钩子 + 游戏内菜单（仍不读游戏内存） ===
    install_frame_hook()
    -- 菜单可能已经加载了：立刻注册一次（否则留给每帧重试）
    menu_tick()
    -- v19+ 的加载器提供 after_startup：官方推荐的注册时机
    local loader = rawget(_G, 'CowboyBingusModLoader')
    if type(loader) == 'table' and type(loader.after_startup) == 'function' then
        local ok_reg = pcall(loader.after_startup, function()
            install_frame_hook()  -- 启动时 update 还不存在的话，这里再试一次
            menu_tick()
        end)
        log(ok_reg and '已注册 after_startup 回调（loader v19+）'
                     or 'after_startup 注册被拒绝（不影响功能）')
    end

    if CONFIG.mode == 'menu' then
        status(true, string.format('menu 模式：UDP 与游戏内菜单就绪（hook=%s），未读取游戏内存',
                                   tostring(HOOK.name or '未装')))
        return
    end

    -- === recon / live：从这里开始要读游戏内存，必须能验址 ===
    if not (K32 and K32.Hd2Coyote_VirtualQuery) then
        status(false, '拿不到 VirtualQuery：为安全起见不进行任何内存读取（防止游戏卡死/崩溃）')
        return
    end

    log('开始校验 game.dll 构建并自动定位本地玩家…')
    local player, why = locate_local_player(R, P)
    if not player then
        P.__why = why
        log('本地玩家未定位：' .. tostring(why))
    else
        log(string.format('本地玩家锚点 = 0x%X', player.address))
    end

    if CONFIG.mode == 'recon' then
        local found = recon(R, P, base, player)
        local detail
        if found then
            detail = 'recon 完成：已自动定位本地玩家；报告包含生命数据验证结果'
        else
            -- 把具体原因写进 STATUS：用户第一眼看的就是这个文件
            detail = 'recon 未找到本地玩家：' .. tostring(P.__why or '未定位')
        end
        status(found, detail)
        return
    end

    live.P = P
    live.player = player
    -- 只有启动校验通过才进入自动读取；失败时不伪造一次性 state。
    start_live()
end

local ok, err = pcall(main)
if not ok then
    log('main 异常：' .. tostring(err), true)
    pcall(status, false, 'main 异常：' .. tostring(err))
end

-- 同时导出给离线测试/宿主使用（require 这个资源的代码可以拿到这些纯函数）
return {
    version = VERSION,
    config = CONFIG,
    profiles = PROFILES,
    menu = MENU,
    live = live,
    hook = HOOK,
    json = json,
    reader = reader,
    read_state = read_state,
    locate_local_player = locate_local_player,
    module_base = module_base,
    now_ms = now_ms,
    build_menu_specs = build_menu_specs,
    menu_tick = menu_tick,
    frame_tick = frame_tick,
    install_frame_hook = install_frame_hook,
    start_live = start_live,
    main = main,
}

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
-- 两种模式：
--   mode = 'recon'  —— 一次性侦察：模块基址、本地玩家锚点、_G 里的候选钩子名，
--                      并把证据落盘，然后进入终止状态（不周期扫描、不吃帧）。
--   mode = 'live'   —— 按 profile 的偏移以 10Hz 上报状态。
--                      偏移没填齐时拒绝启动并在 STATUS.txt 说明原因（宁可不干活也不猜着读）。
--
-- 偏移是"某个游戏构建"的快照，游戏更新会漂移：先跑 recon，更新后重跑 recon。

local ADDON = 'mods/hd2coyote/hd2_coyote_bridge'
local VERSION = '0.4.2'   -- 必须与仓库根的 VERSION 文件一致（打包时会校验）
local PROTOCOL = 1

--------------------------------------------------------------------- 配置
local CONFIG = {
    -- ★ 默认最保守。逐级往上开，每一步都可回退：
    --   'safe'  = 只写日志（不碰 FFI、不读内存、不挂钩子、不发 UDP、不注册菜单）
    --   'net'   = 再加 FFI + UDP 上报（hello），仍不挂钩子、不读游戏内存
    --   'menu'  = 再加每帧钩子 + 游戏内菜单（仍然**不读游戏内存**）
    --   'recon' = 再加一次性内存侦察（偏移未验证时用这个）
    --   'live'  = 按偏移实时读状态并上报
    mode = 'safe',
    enabled = true,            -- 游戏内菜单的总开关（live/recon 时才有效）
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

-- 偏移 profile。**全部是某个构建的快照，必须用 recon 验证后再用。**
-- ping_actors / ping_ring / ping_actors_* 取自开源项目
-- etxp/HD2-G60-Smart-Targeting（MIT）—— 目前唯一公开且验证过的
-- "如何在游戏内定位本地玩家实体"的实现，特此署名。
-- hp / limb_mask / dead 需要你跑一次 recon（或离线分析 dump）之后填。
local PROFILES = {
    steam_25480438 = {
        build = '1.8.46015.0',
        -- 本地玩家锚点：ping actor 表里"本地玩家自己发出的 ping"的 creator
        ping_actors = 0x3326d20,
        ping_actors_count = 0x70,
        ping_actors_first = 0x110,
        ping_actors_stride = 8,
        ping_ring = 0x347ce30,
        -- 玩家状态。anchor 决定偏移相对谁：
        --   'ping_record' = 相对上面定位到的 ping creator 记录地址
        --   'base'        = 相对模块基址
        hp_anchor = 'ping_record',
        hp = nil,              -- float：当前血量（绝对值）
        hp_max_anchor = 'ping_record',
        hp_max = nil,          -- float：最大血量（可选，缺省 100）
        limb_anchor = 'ping_record',
        limb_mask = nil,       -- u8：肢体损伤位掩码
        limb_mask_bits = { 1, 2, 4 },
        dead_anchor = 'ping_record',
        dead = nil,            -- u8：1 = 阵亡（可选）
        bleeding_anchor = 'ping_record',
        bleeding = nil,        -- u8：1 = 流血（可选）
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

local log_lines = 0
local LOG_HANDLE = nil      -- 优先用加载器给的日志句柄（它保证这个目录可用）

local function log(msg, force)
    -- 限流：addon 只有一次机会，日志刷爆等于没有日志
    if log_lines > 500 and not force then return end
    log_lines = log_lines + 1
    local line = string.format('[%s][%s] %s\n', os.date('%H:%M:%S'), ADDON, tostring(msg))
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

local function status(ok, reason)
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
--   return { config = { mode = 'menu' },
--            profiles = { steam_25480438 = { hp = 0x2C } } }     -- Lua 形式
--   mode = menu                                                -- 纯文本形式（最保险）
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
-- ★ 踩过的坑：Web 控制台生成的文件以注释开头，而我原来只看"第一个非空 token 是不是
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
    for pname, pdata in pairs(user.profiles or {}) do
        PROFILES[pname] = PROFILES[pname] or {}
        for k, v in pairs(pdata) do PROFILES[pname][k] = v end
        applied[#applied + 1] = 'profiles.' .. tostring(pname)
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
    if not PAGE_READABLE[prot % 0x100] then return false end  -- 去掉 PAGE_GUARD/NOCACHE 等修饰位
    local start = tonumber(ffi.cast('uintptr_t', m.BaseAddress))
    local size = tonumber(m.RegionSize)
    if not start or not size then return false end
    return (addr + n) <= (start + size)
end

-- 模块基址 = 主 exe 基址。ASLR 每次运行都不同，所以只能"基址 + 相对偏移"。
local function module_base()
    if not init_ffi() then return nil end
    local ok, base = pcall(function()
        return tonumber(ffi.cast('uintptr_t', K32.Hd2Coyote_GetModuleHandleA(nil)))
    end)
    if not ok or not base or base == 0 then return nil end
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

--------------------------------------------------------------------- 本地玩家定位
-- 思路（来自 etxp/HD2-G60-Smart-Targeting 的公开实现）：
--   游戏维护一张 ping actor 表；本地玩家自己发出的 ping，其 creator 就是本地玩家。
-- 所有边界都要校验；校验不过就判定 profile 失效，绝不"猜着读"。
local function locate_local_player(R, P)
    local function find()
        local actors = R.ptr(P.ping_actors)
        if not actors then return nil, 'ping actor 表指针为空（偏移可能已失效）' end
        -- 注意：count / 条目都在 actors 表内部，必须用 actors 自己的 reader
        local AR = reader(actors)
        local count = AR.u32(P.ping_actors_count)
        if not count then return nil, '读不到 actor 数量' end
        if count > 16 then return nil, 'actor 数量异常（' .. count .. '）' end
        for i = 0, count - 1 do
            local off = P.ping_actors_first + i * P.ping_actors_stride
            local ptr = AR.ptr(off)
            if ptr then
                local rec = reader(ptr)
                local id = rec.u32(16)
                local entity = rec.u32(8)
                if id and id ~= 0 and entity and entity ~= 0 and entity ~= 0xFFFFFFFF then
                    return { entity_id = entity, id = id, address = ptr, record = ptr }
                end
            end
        end
        return nil, '没有找到本地 ping creator'
    end
    local ok, player, why = pcall(find)
    if not ok then return nil, '定位异常：' .. tostring(player) end
    return player, why
end

local function anchor_reader(R, P, player, anchor_key)
    local anchor = P[anchor_key] or 'ping_record'
    if anchor == 'base' then return R end
    if not player or not player.address then return nil end
    return reader(player.address)
end

--------------------------------------------------------------------- 状态读取
-- 返回 { hp, hp_max, limbs, bleeding, dead }，读不到就返回 nil + 原因
local function read_state(R, P, player)
    if not player then return nil, 'NO_PLAYER' end
    if not P.hp then return nil, 'NO_HP_OFFSET' end

    local HR = anchor_reader(R, P, player, 'hp_anchor')
    if not HR then return nil, 'NO_ANCHOR' end
    local hp = HR.f32(P.hp)
    if hp == nil then return nil, 'HP_READ_FAILED' end
    if hp < 0 or hp > 1e9 then return nil, 'HP_OUT_OF_RANGE' end

    local hp_max = 100
    if P.hp_max then
        local MR = anchor_reader(R, P, player, 'hp_max_anchor')
        local v = MR and MR.f32(P.hp_max)
        if v and v > 0 and v <= 1e9 then hp_max = v end
    end

    local limbs = { 0, 0, 0 }
    if P.limb_mask then
        local LR = anchor_reader(R, P, player, 'limb_anchor')
        local mask = LR and LR.u8(P.limb_mask)
        if mask then
            if P.limb_mask_bits then
                for i, bit in ipairs(P.limb_mask_bits) do
                    limbs[i] = (math.floor(mask / bit) % 2 == 1) and 1 or 0
                end
            else
                -- 掩码 + 起始位（游戏内菜单里可调）：bit(shift+i-1) -> 第 i 个槽位
                local m = math.floor(mask / 2 ^ (P.limb_shift or 0))
                for i = 1, 3 do
                    limbs[i] = (math.floor(m / 2 ^ (i - 1)) % 2 == 1) and 1 or 0
                end
            end
        end
    end

    local bleeding = 0
    if P.bleeding then
        local BR = anchor_reader(R, P, player, 'bleeding_anchor')
        local v = BR and BR.u8(P.bleeding)
        if v then bleeding = (v ~= 0) and 1 or 0 end
    end

    local dead = 0
    if P.dead then
        local DR = anchor_reader(R, P, player, 'dead_anchor')
        local v = DR and DR.u8(P.dead)
        if v then dead = (v ~= 0) and 1 or 0 end
    end

    return { hp = hp, hp_max = hp_max, limbs = limbs, bleeding = bleeding, dead = dead }
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
    local out = {}
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

    -- 2) 本地玩家锚点 + 有界 dump（只 dump 锚点附近，不做全地址空间扫描）
    if player then
        say(string.format('local_player_entity=%d ping_id=%d anchor=0x%X',
                          player.entity_id, player.id, player.address))
        local n = math.min(CONFIG.max_dump_bytes, 0x400)
        local raw = reader(player.address).hex(0, n)
        if raw then
            say('anchor_hex_dump:\n' .. table.concat(hexdump_lines(raw), '\n'))
        else
            say('anchor_hex_dump: 读取失败（地址未映射或已失效）')
        end
        say('next: 把 hp / limb_mask / dead 的偏移填进 PROFILES.' .. CONFIG.profile)
    else
        say('local_player=UNKNOWN（' .. tostring(P.__why or '未定位') .. '）')
        say('提示：本地玩家要在飞船/局内存在 ping actor 时才可定位，请重跑一次 recon')
    end

    file_write(join(LOG_DIR, 'recon_report.txt'), table.concat(out, '\n') .. '\n', false)
    return player ~= nil
end

--------------------------------------------------------------------- 游戏内设置界面
-- 前向声明：菜单回调要用到后面才定义的东西
local set_mode, install_frame_hook, HOOK

-- 用社区的 Mod Options Menu（全局 ModOptionsMenu，api=1）在 ESC 菜单的 MODS 页里加一个分类。
-- API 见 https://github.com/CowboyBingus/ModOptionsMenu 的 README（0BSD 许可）：
--   register_option(id, spec) -> true | false, 原因
--   get(id) / set(id, value) / on_change(id, fn(value, id)) / ready()
--   spec: type(toggle|choice|slider) / label / mod / mod_id / default / description
--         choice: choices(2~16 项，每项 ≤48 字符)  slider: min/max/step
--   上限：112 个 mod 分类（每次显示 7 个）、每个 mod 32 个选项；选项注册后不能取消。
--   文本支持函数（api version ≥2，跟随游戏语言）；这里用「中文 English」双语字符串，
--   在所有版本、所有语言下都能用。
local MENU = {
    mod_id = 'hd2coyote',
    display = 'HD2 COYOTE 郊狼',
    registered = false,
    gave_up = false,
    attempts = 0,
    version = 0,
    hook_choices = { 'update' },
    specs = {},
}

local function menu()
    local m = rawget(_G, 'ModOptionsMenu')
    if type(m) ~= 'table' or m.api ~= 1 then return nil end
    return m
end

local function opt_id(suffix) return MENU.mod_id .. '.' .. suffix end

local function offset_or_nil(value)
    local v = tonumber(value)
    if not v or v < 0 then return nil end
    return math.floor(v + 0.5)
end

-- 每帧钩子候选：在 _G 里找 update/tick 之类的函数名。
-- 官方第三方参考写明"CowboyBingus 的 mod 普遍包装全局 update"，所以 update 排第一。
local function collect_hook_choices()
    local names, seen = { 'update' }, { update = true }
    local count = 0
    pcall(function()
        for k, v in pairs(_G) do
            count = count + 1
            if count > 400 then break end
            if type(k) == 'string' and type(v) == 'function' and not seen[k] then
                if k:lower():find('update') or k:lower():find('tick') or k:lower():find('frame') then
                    seen[k] = true
                    names[#names + 1] = k
                end
            end
        end
    end)
    table.sort(names, function(a, b)
        if a == 'update' then return true end
        if b == 'update' then return false end
        return a < b
    end)
    while #names > 15 do table.remove(names) end
    names[#names + 1] = 'none'  -- 允许显式不挂钩子（选项上限 16 项）
    return names
end

local function hook_choice_index(names)
    local want = CONFIG.frame_hooks[1]
    for i, name in ipairs(names) do
        if name == want then return i end
    end
    return 1
end

-- 玩家按 APPLY 后调用：把菜单值写进 CONFIG / PROFILES（立即生效，不用重启）
local function apply_option(suffix, value)
    local P = PROFILES[CONFIG.profile]
    if suffix == 'enabled' then
        CONFIG.enabled = value and true or false
        log('菜单：上报 ' .. (CONFIG.enabled and '开启' or '关闭'))
    elseif suffix == 'mode' then
        set_mode(tonumber(value) == 2 and 'live' or 'recon')
    elseif suffix == 'port' then
        local port = math.floor((tonumber(value) or CONFIG.port) + 0.5)
        if port ~= CONFIG.port then
            CONFIG.port = port
            net.ready = false          -- 端口变了要重开 socket
            init_net()
            log('菜单：UDP 端口 -> ' .. port .. '（控制器也要改成同一个端口）')
        end
    elseif suffix == 'interval' then
        CONFIG.interval = math.max(0.02, tonumber(value) or CONFIG.interval)
    elseif suffix == 'hp_offset' and P then
        P.hp = offset_or_nil(value)
    elseif suffix == 'hp_max_offset' and P then
        P.hp_max = offset_or_nil(value)
    elseif suffix == 'limb_offset' and P then
        P.limb_mask = offset_or_nil(value)
    elseif suffix == 'limb_shift' and P then
        P.limb_shift = math.max(0, math.floor((tonumber(value) or 0) + 0.5))
        P.limb_mask_bits = nil
    elseif suffix == 'dead_offset' and P then
        P.dead = offset_or_nil(value)
    elseif suffix == 'hook' then
        local name = MENU.hook_choices[math.floor((tonumber(value) or 1) + 0.5)] or 'update'
        CONFIG.frame_hooks = { name }
        if name == 'none' then
            live.active = false
            status(false, '每帧钩子已关闭（Frame Hook = none）：不再上报状态')
        else
            install_frame_hook()
        end
    else
        log('菜单：忽略未知选项 ' .. tostring(suffix))
    end
end

local function build_menu_specs()
    local P = PROFILES[CONFIG.profile] or {}
    MENU.hook_choices = collect_hook_choices()
    MENU.specs = {
        { suffix = 'enabled', type = 'toggle', label = '启用上报 Enabled', default = CONFIG.enabled,
          desc = '关闭后桥不再发状态；控制器会因为断流自动静音。' },
        { suffix = 'mode', type = 'choice', label = '模式 Mode',
          choices = { '侦察 Recon', '运行 Live' },
          default = (CONFIG.mode == 'live') and 2 or 1,
          desc = '侦察：只做一次定位并把证据写进 recon_report.txt；运行：按偏移实时上报。' },
        { suffix = 'port', type = 'slider', label = 'UDP 端口 Port', min = 1024, max = 65535, step = 1,
          default = CONFIG.port,
          desc = '必须与控制器 config.json 里的 hook.port 一致（默认 47777）。' },
        { suffix = 'interval', type = 'slider', label = '上报间隔(秒) Interval', min = 0.05, max = 1,
          step = 0.05, default = CONFIG.interval,
          desc = '越小越灵敏，越大越省。默认 0.1 秒。' },
        { suffix = 'hp_offset', type = 'slider', label = '血量偏移 HP Offset', min = -1, max = 65535,
          step = 1, default = P.hp or -1,
          desc = '-1 = 未设置。相对「本地玩家锚点」的偏移，recon 之后填。' },
        { suffix = 'hp_max_offset', type = 'slider', label = '血量上限偏移 HP Max', min = -1,
          max = 65535, step = 1, default = P.hp_max or -1,
          desc = '-1 = 未设置（缺省按 100 处理）。' },
        { suffix = 'limb_offset', type = 'slider', label = '肢体掩码偏移 Limb Mask', min = -1,
          max = 65535, step = 1, default = P.limb_mask or -1,
          desc = '-1 = 未设置。读 1 字节，按位表示各部位是否受伤。' },
        { suffix = 'limb_shift', type = 'slider', label = '肢体起始位 Limb Shift', min = 0, max = 7,
          step = 1, default = P.limb_shift or 0,
          desc = '掩码里从第几位开始对应「左肢/躯干/右肢」。默认 0。' },
        { suffix = 'dead_offset', type = 'slider', label = '阵亡标志偏移 Dead', min = -1, max = 65535,
          step = 1, default = P.dead or -1,
          desc = '-1 = 未设置（只用血量判阵亡）。' },
        { suffix = 'hook', type = 'choice', label = '每帧钩子 Frame Hook',
          choices = MENU.hook_choices, default = hook_choice_index(MENU.hook_choices),
          desc = '包装哪个全局函数来拿每帧回调（默认 update）。选 none 则不挂钩子。', gap = true },
    }
end

local function register_options(m)
    build_menu_specs()
    local failed = {}
    for _, opt in ipairs(MENU.specs) do
        local spec = {
            type = opt.type, label = opt.label, mod = MENU.display, mod_id = MENU.mod_id,
            default = opt.default,
        }
        if opt.desc then spec.description = opt.desc end
        if opt.gap then spec.gap = true end
        if opt.type == 'choice' then spec.choices = opt.choices end
        if opt.type == 'slider' then spec.min, spec.max, spec.step = opt.min, opt.max, opt.step end
        local ok, why = m.register_option(opt_id(opt.suffix), spec)
        if not ok then
            failed[#failed + 1] = opt.suffix .. '(' .. tostring(why) .. ')'
        else
            -- 注册成功后立刻拿到"已应用值"（没存过就是 default），保证和文件配置一致
            local ok_get, value = pcall(m.get, opt_id(opt.suffix))
            if ok_get and value ~= nil then
                pcall(apply_option, opt.suffix, value)
            end
            pcall(m.on_change, opt_id(opt.suffix), function(value)
                local ok_apply, err = pcall(apply_option, opt.suffix, value)
                if not ok_apply then log('菜单回调异常：' .. tostring(err)) end
            end)
        end
    end
    MENU.registered = true
    if #failed > 0 then
        log('菜单：部分选项注册失败 -> ' .. table.concat(failed, ','))
    end
    log(string.format('菜单：已注册 %d 个选项（menu version=%s）',
                      #MENU.specs - #failed, tostring(MENU.version)))
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
               R = nil, P = nil, base = nil }

local function live_step()
    local R, P = live.R, live.P
    if not R or not P then return end
    local now = now_ms()
    if now - live.last < CONFIG.interval * 1000 then return end
    live.last = now

    local st, why = read_state(R, P, live.player)
    if not st then
        live.failures = live.failures + 1
        if live.failures == 5 then
            log('读取失败 ' .. tostring(why) .. '，尝试重新定位本地玩家')
        end
        -- 退避重定位：每 3 秒一次，失败就继续跑（不刷屏、不写内存）
        if now - live.last_relocate > 3000 then
            live.last_relocate = now
            local p, why2 = locate_local_player(R, P)
            if p then
                live.player, live.failures = p, 0
                log(string.format('重新定位成功 anchor=0x%X', p.address))
            else
                live.player = nil
                log('重新定位失败：' .. tostring(why2))
            end
        end
        return
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
    if not P.hp then
        status(false, 'live 模式缺少 hp 偏移：先在菜单里填 HP Offset，或跑一次侦察')
        return false
    end
    live.base = live.base or module_base()
    if not live.base then
        status(false, '拿不到模块基址：' .. tostring(ffi_reason))
        return false
    end
    if not HOOK.installed then
        status(false, '找不到每帧钩子：CONFIG.frame_hooks 里没有真实存在的全局函数；'
            .. '候选见 recon_report.txt 的 globals_functions，或在游戏内菜单里选 Frame Hook')
        return false
    end
    live.R = live.R or reader(live.base)
    live.P = P
    local player, why = locate_local_player(live.R, P)
    if not player then
        status(false, 'live 模式定位不到本地玩家：' .. tostring(why))
        return false
    end
    live.player, live.active, live.last = player, true, now_ms()
    status(true, string.format('live 已启动（profile=%s hook=%s hp=0x%X）',
                               CONFIG.profile, tostring(HOOK.name or '?'), P.hp))
    return true
end

function set_mode(mode)
    local target = (mode == 'live') and 'live' or 'recon'
    if CONFIG.mode == target and (target == 'recon' or live.active) then return end
    CONFIG.mode = target
    log('菜单：模式 -> ' .. target)
    if target == 'live' then
        start_live()
    else
        live.active = false
        local P = PROFILES[CONFIG.profile]
        if live.R and P then
            pcall(recon, live.R, P, live.base or module_base(), live.player)
        end
    end
end

--------------------------------------------------------------------- 每帧钩子
-- 官方第三方参考：CowboyBingus 的 mod 普遍包装全局 update。
-- 包装规则（照抄参考里的要求）：参数与返回值原样透传，自己的错误不许影响下面。
HOOK = { name = nil, prev = nil, installed = false, active = true }

local function frame_tick()
    if not HOOK.active then return end
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

    log('开始定位本地玩家（偏移来自 profile，可能与本构建不符）…')
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
            detail = 'recon 完成：请在游戏内菜单里填血量偏移（或把 recon_report.txt 发回）'
        else
            -- 把具体原因写进 STATUS：用户第一眼看的就是这个文件
            detail = 'recon 未找到本地玩家：' .. tostring(P.__why or '未定位')
        end
        status(found, detail)
        return
    end

    live.P = P
    live.player = player
    if start_live() then return end
    -- live 起不来（多半是偏移没填）：退回一次性采样，并把原因写进 STATUS
    if player then
        local st = read_state(R, P, player)
        if st then
            send(json({ { 'v', PROTOCOL }, { 'ev', 'state' }, { 't', now_ms() / 1000 },
                        { 'hp', st.hp }, { 'hp_max', st.hp_max }, { 'limbs', st.limbs },
                        { 'bleeding', st.bleeding }, { 'dead', st.dead } }))
        end
    end
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
    apply_option = apply_option,
    build_menu_specs = build_menu_specs,
    collect_hook_choices = collect_hook_choices,
    menu_tick = menu_tick,
    frame_tick = frame_tick,
    install_frame_hook = install_frame_hook,
    start_live = start_live,
    set_mode = set_mode,
    main = main,
}

-- core/mem.lua —— 只读内存访问层
-- 三条后端，按优先级：MDL3 原生 ctx.read / LuaJIT ffi / 无。
-- 有 ffi 时**每次读之前先 VirtualQuery 验址**：LuaJIT 的 ffi.string 撞到坏地址是进程级
-- 访问违例，pcall 挡不住，游戏会直接崩（这个项目已经踩过一次）。
return function(H)
    local compat = H.compat
    local M = { backend = 'none', reason = nil }

    local MEM_COMMIT = 0x1000
    local PAGE_READABLE = {
        [0x02] = true, [0x04] = true, [0x08] = true,
        [0x20] = true, [0x40] = true, [0x80] = true,
    }

    local ffi_ready = false
    local K32, WS2

    function M.init()
        local ffi = compat.ffi()
        if not ffi then
            M.backend = 'ctx'
            M.reason = 'ffi unavailable; relying on MDL3 ctx.read'
            return false
        end
        if ffi_ready then return true end
        local ok, err = pcall(function()
            -- 私有名 + __asm__ 别名：整个游戏只有一份 cdef，共享名会和其他 mod 打架
            ffi.cdef[[
                void* Hd2Coyote_GetModuleHandleA(const char* name) __asm__("GetModuleHandleA");
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
            K32 = ffi.load('kernel32')
            WS2 = ffi.load('ws2_32')
        end)
        if not ok then
            M.reason = 'ffi.cdef/load failed: ' .. tostring(err)
            M.backend = 'none'
            return false
        end
        ffi_ready = true
        M.ffi = ffi
        M.K32 = K32
        M.WS2 = WS2
        M.backend = 'ffi'
        return true
    end
    M.init()

    function M.range_readable(addr, n)
        if not addr or addr < 0x10000 or addr > 0x7FFFFFFFFFFF then return false end
        if not (ffi_ready and K32) then return compat.ffi() == nil end
        local ffi = M.ffi
        local mbi = ffi.new('Hd2CoyoteMBI[1]')
        local ok, got = pcall(function()
            return tonumber(K32.Hd2Coyote_VirtualQuery(ffi.cast('const void*', addr), mbi, ffi.sizeof(mbi)))
        end)
        if not ok or not got or got == 0 then return false end
        local m = mbi[0]
        if tonumber(m.State) ~= MEM_COMMIT then return false end
        local prot = tonumber(m.Protect) or 0
        if math.floor(prot / 0x100) % 2 == 1 then return false end
        if not PAGE_READABLE[prot % 0x100] then return false end
        local start = tonumber(ffi.cast('uintptr_t', m.BaseAddress))
        local size = tonumber(m.RegionSize)
        if not start or not size then return false end
        return (addr + n) <= (start + size)
    end

    function M.base(ctx, name)
        name = name or 'game.dll'
        if ffi_ready then
            local ok, h = pcall(function() return K32.Hd2Coyote_GetModuleHandleA(name) end)
            local base = ok and compat.to_int(h) or nil
            if base and base > 0 then
                M.base_backend = 'ffi'
                return base
            end
        end
        return compat.module_base(name, ctx)
    end

    function M.read(addr, n)
        if not addr or addr == 0 or not n or n <= 0 then return nil end
        if ffi_ready and not M.range_readable(addr, n) then
            M.last_reject = 'range'
            return nil
        end
        return compat.read(addr, n, H.ctx)
    end

    function M.reader(base)
        local R = { base = base }
        local function at(addr, n)
            if not addr or addr == 0 then return nil end
            local ok, bytes = pcall(M.read, addr, n)
            if not ok or not bytes or #bytes ~= n then return nil end
            return bytes
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
            local v = R.u32(off); if v == nil then return nil end
            if v >= 2147483648 then return v - 4294967296 end
            return v
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
            -- tonumber(hex, 16) 在 LuaJIT 里只按 32 位解析，必须逐字符累乘
            local v = 0
            for i = 1, #hex do v = v * 16 + tonumber(hex:sub(i, i), 16) end
            return v
        end
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
        function R.at(addr, n) return at(addr, n) end
        return R
    end

    return M
end

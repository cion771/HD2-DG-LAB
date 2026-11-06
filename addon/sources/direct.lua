-- sources/direct.lua —— 走 game.dll 偏移的只读源（无公开 API 时的唯一选择）
-- 每次读之前都先用 PE 头 + 指令 pin 证明映像就是研究对应的那一版；不匹配就拒绝，
-- 不扫描地址空间、不接受用户手填偏移。
return function(H)
    local M = { name = 'direct' }
    local compat, log

    local function hex_bytes(hex)
        local out = {}
        for pair in tostring(hex):gmatch('%x%x') do out[#out + 1] = string.char(tonumber(pair, 16)) end
        return table.concat(out)
    end

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

    function M.setup(profile)
        M.P = profile
        compat = H.compat
        log = H.log
    end

    local function read(addr, n)
        return H.mem.read(addr, n)
    end

    local function reader(base) return H.mem.reader(base) end

    local function verify(R, P)
        if P.__verified then return true end
        local mz = R.bytes(0, 2)
        local pe = R.u32(0x3C)
        if mz ~= 'MZ' or not pe or pe < 0x40 or pe > 0x1000 then return false, 'game.dll PE 头无效' end
        if R.bytes(pe, 4) ~= 'PE\0\0' then return false, 'game.dll PE 签名无效' end
        local image_size = R.u32(pe + 80)
        if image_size ~= P.image_size then
            return false, string.format('game.dll 映像大小不匹配（%s，期望 %s）',
                                        tostring(image_size), tostring(P.image_size))
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

    local function network_entity(R, P, network_id)
        local A = P.avatars
        if not network_id or network_id == P.players.no_network_id then return nil end
        local manager = R.ptr(A.entities)
        if not manager then return nil end
        local M0 = reader(manager)
        local slots = M0.ptr(A.map_slots)
        local capacity = M0.u32(A.map_capacity)
        local empty = M0.u32(A.map_empty)
        local multiplier = M0.u32(A.map_multiplier)
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
                return index and M0.u32(A.entity_base + index * A.entity_stride) or nil
            end
            if key == empty then return nil end
        end
        return nil
    end

    local function locate(R, P)
        local function find()
            local valid, why = verify(R, P)
            if not valid then return nil, why end
            local L = P.players
            local user = R.ptr(L.local_user)
            local manager = R.ptr(L.global)
            if not user then return nil, '本地用户上下文尚未就绪' end
            if not manager then return nil, '玩家管理器尚未就绪' end
            local local_peer = reader(user).u64hex(L.local_peer)
            if not local_peer then return nil, '读不到本地 peer id' end
            local M0 = reader(manager)
            local count = M0.u32(L.count)
            if count == nil then return nil, '读不到玩家数量' end
            if count > L.max_players then return nil, '玩家数量异常（' .. tostring(count) .. '）' end
            for slot = 0, count - 1 do
                if M0.u64hex(L.peers + slot * L.peer_stride) == local_peer then
                    local descriptor = M0.ptr(L.descriptors + slot * 8)
                    local player_entity = descriptor and reader(descriptor).u32(L.descriptor_entity) or nil
                    local network_id = M0.u32(L.avatar_id + slot * L.avatar_id_stride)
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

    local function health_header(R, P)
        local Hc = P.health
        local manager = R.ptr(Hc.global)
        if not manager then return nil, '生命管理器尚未就绪' end
        local M0 = reader(manager)
        local capacity, live, hash_capacity = M0.u32(Hc.capacity), M0.u32(Hc.live), M0.u32(Hc.hash_capacity)
        if not capacity or capacity == 0 or capacity > Hc.max_records or not live or live > capacity
            or not hash_capacity or hash_capacity > 65536 or not power_of_two(hash_capacity) then
            return nil, '生命管理器头无效'
        end
        local header = {
            capacity = capacity, live = live, hash_capacity = hash_capacity,
            hash_empty = M0.u32(Hc.hash_empty), hash_multiplier = M0.u32(Hc.hash_multiplier),
            buckets = M0.ptr(Hc.buckets), descriptors = M0.ptr(Hc.descriptors),
            records = M0.ptr(Hc.records), ext = M0.ptr(Hc.ext_array),
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

    local function read_state(R, P, player)
        if not player or not player.entity_id then return nil, 'NO_PLAYER' end
        local valid, why = verify(R, P)
        if not valid then return nil, why end
        local Hc, I = P.health, P.injury
        local header, header_why = health_header(R, P)
        if not header then return nil, header_why end
        local index = health_index(header, player.entity_id)
        if index == nil then return nil, '本地 avatar 暂无生命记录' end
        local descriptor = reader(header.descriptors).ptr(index * 8)
        if not descriptor then return nil, '生命 descriptor 不可读' end
        local D = reader(descriptor)
        if D.u32(Hc.descriptor_entity) ~= player.entity_id then return nil, '生命 descriptor 已失效' end
        if D.u64hex(Hc.descriptor_type) ~= I.avatar_type then return nil, '实体不是 Helldiver avatar' end
        local flags = D.u32(Hc.descriptor_flags)
        if flags == nil or flags % 2 ~= 1 then return nil, 'avatar 不属于本机' end

        local record = reader(header.records + index * Hc.stride)
        local hp, life = record.i32(Hc.health), record.u32(Hc.life)
        if hp == nil or life == nil or hp < -100000 or hp > 100000 or life > 16 then
            return nil, '生命记录数值异常'
        end
        local hp_max = header.ext and reader(header.ext + index * Hc.ext_stride).u32(Hc.max_health) or nil
        if not hp_max or hp_max == 0 or hp_max > 100000 then return nil, '最大生命值无效' end

        local limbs = {}
        for slot, zone in ipairs(I.zones) do
            local value = record.i32(I.zone_health + zone.index * 4)
            local state = zone_state(record, I.zone_states, zone.index)
            if value == nil or state == nil or value < -100000 or value > 100000 or state > 3 then
                return nil, '肢体生命记录异常（slot=' .. tostring(slot) .. '）'
            end
            -- 受伤判定用游戏自己的 zone state == 2；zone health 只做合理性校验，
            -- 不把 0 HP 猜成受伤（会造出假事件）
            limbs[slot] = state == 2 and 1 or 0
        end
        return { hp = math.max(0, hp), hp_max = hp_max, limbs = limbs,
                 bleeding = 0, downed = life == 1 and 1 or 0, dead = life >= 2 and 1 or 0 }
    end

    function M.available(ctx)
        if not H.mem or H.mem.backend == 'none' then
            return false, '没有可用的内存后端（MDL3 ctx.read 与 ffi 都不可用）'
        end
        local base, why = H.mem.base(ctx, 'game.dll')
        if not base then return false, '拿不到 game.dll 基址：' .. tostring(why or H.mem.reason) end
        M.base = base
        M.R = M.R or reader(base)
        local valid, vwhy = verify(M.R, M.P)
        if not valid then return false, vwhy end
        return true, 'game.dll 构建匹配（profile=' .. tostring(M.P.build) .. '）'
    end

    function M.read_state()
        if not M.R then return nil, 'direct 源未初始化' end
        local now = H.clock.now_ms()
        if not M.player then
            if M.last_relocate and now - M.last_relocate < 3000 then
                return nil, M.wait_reason or '等待本地玩家'
            end
            M.last_relocate = now
            local player, why = locate(M.R, M.P)
            if not player then
                M.wait_reason = why
                return nil, why
            end
            M.player = player
        end
        local st, why = read_state(M.R, M.P, M.player)
        if not st then
            M.player = nil
            M.last_relocate = now
            M.wait_reason = why
            return nil, why
        end
        return st
    end

    function M.describe()
        return string.format('direct profile=%s base=%s backend=%s',
                             tostring(M.P and M.P.build), tostring(M.base), tostring(H.mem and H.mem.backend))
    end

    return M
end

-- profiles/steam_25480438.lua —— 已研究的 game.dll 布局（只读子集）
-- 数据来自 SkyeShade/HD2Runtime 的公开研究（MIT）。只实现读取本地玩家、生命管理器
-- 与六个肢体 zone；不调用游戏函数、不写内存。
-- ★ 布局随游戏构建变化。build 字段是唯一的准入条件：不认识就不读，绝不猜。
return function(H)
    return {
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
            -- 协议按 HUD 顺序：头、胸、左臂、右臂、左腿、右腿
            zones = {
                { index = 0, max = 85 }, { index = 1, max = 60 },
                { index = 2, max = 35 }, { index = 3, max = 35 },
                { index = 4, max = 45 }, { index = 5, max = 45 },
            },
        },
    }
end

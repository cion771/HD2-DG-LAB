# 游戏内桥（Lua）—— MDL3 版实现说明

本文说明「不用画面识别、直接在游戏里取状态」这条路在《绝地潜兵 2》里**到底是什么**、
重写后由哪些模块负责、怎么装、怎么逐级验证，以及出问题怎么回退。

> **前置变了**：0.8.0 起游戏内桥跑在 **MDL3**（麦当当3，Lua mod 加载器）上，
> 不再需要 Bingus Shared Loader。旧的 `lua/hd2_coyote_bridge.lua` 只保留一版做回滚，
> 日常请用 `addon/` 下的实现。

---

## 1. 先把事实说清楚

| 常见说法 | 实际情况 |
|---|---|
| "官方支持 mod / 有 mod API" | **没有。** Arrowhead 没有发布 HD2 的 mod API、SDK 或 Workshop 支持。 |
| "直接 hook 游戏" | 在 HD2 里指的是：用社区加载器把一个 Lua 资源塞进游戏自己的 Lua 虚拟机，addon 与游戏共享 Lua 状态和地址空间。 |
| "随便哪个加载器都行" | **不行。** 2026-10-20 的游戏更新把内置 Lua 换成 PUC Lua 5.1，BSL 赖以工作的 LuaJIT 虚拟机消失，BSL 自此失效。 |
| "换成 MDL3 就完事" | MDL3 自带启动加载器，**发现并启动 addon 的方式与 BSL 相同**（兼容 BSL addon 契约），但它不保证 `ffi` 一定可用 —— 所以本桥把"读内存"和"发包"都做成了**可探测、可降级**的两条路。 |
| "内存 hook 会被反作弊封" | 游戏带 nProtect GameGuard。本项目遵守的红线：**进程内、只读**；真正危险的是从外部进程 `OpenProcess + ReadProcessMemory`，本桥不那样做。 |

---

## 2. 两种安装形态（选一个）

同一份源码（`addon/`）支持两条装载路径，模块内容完全一样：

| 形态 | 装法 | 适用 | 能拿到的能力 |
|---|---|---|---|
| **A. 本地 mod（推荐）** | 解压 `HD2-Coyote-<版本>-local.zip`，把 `hd2coyote/` 放进 `%LOCALAPPDATA%\MDL\Helldivers2\Mods\`，在 MDL 设置页启用 | MDL3 原生 | `ctx`：选项面板、日志、每帧回调 `on_update` |
| **B. Arsenal / HD2MM** | 导入 `HD2-Coyote-<版本>.zip`，启用 → Deploy → 重启游戏 | 习惯用管理器、或与其它 addon 一起部署 | 只有 `-- HD2-Addon:` 契约：没有 `ctx`，配置只读文件 |

两种形态**不要同时装**：同一份 addon 被加载两次会发出两倍报文。装 B 之前先把 A 的文件夹移走，反之亦然。

打包：

```powershell
python tools\build_addon_mdl3.py --both          # 两个 zip 都出，落在 build/mdl3/
python tools\build_addon_mdl3.py --local         # 只要本地 mod
python tools\build_addon_mdl3.py --patch-number 7  # 手工安装时挑一个没占用的号
```

产物与版本号绑定：`VERSION` 文件与 `addon/mod.lua` 的 `local VERSION` 不一致时打包器直接拒绝出包。

---

## 3. 数据链路

```text
《绝地潜兵 2》进程
 ├─ MDL3 3.0.0-dev.x（Lua mod 加载器 / 启动加载器）
 └─ mods/hd2coyote/hd2_coyote        ← addon/mod.lua（本地 mod）或 addon/entry.lua（addon 包）
      ├─ 数据源（按能力自动挑选，见 §6）
      │    ├─ HD2Runtime 公开只读 API（装了才有）
      │    ├─ game.dll 只读偏移（PE + 指令指纹校验通过才读）
      │    └─ mock（合成数据，不开游戏也能验链路）
      └─ 出口（按能力自动挑选，见 §7）
           ├─ UDP 127.0.0.1:47777     （ffi 可用时）
           └─ bridge_state.json       （没有 ffi 时的文件广播）
                    │
                    ▼
本机 hd2-coyote 控制器（Python）
 hd2coyote/hook.py            ← UDP：收状态、复用 detectors 的状态机
 hd2coyote/sources/bridge_file.py ← 文件：同一条解析路径，事件判定只有一份
 → rules.py（强度/波形/通道）→ safety.py（硬上限/急停）→ DG-LAB Socket → 郊狼
```

Lua 侧只**忠实读值**，不判定；掉血累计、低血量迟滞、阵亡保持、复活忽略期全部在控制器里，
与视觉路线共用同一份被测过的代码。

---

## 4. 模块结构

```
addon/
  mod.lua                 MDL3 本地 mod 入口：ctx、选项、on_enable/on_update/on_disable
  entry.lua               HD2-Addon 入口：包装 _G.update / _G.shutdown
  bridge.lua              装配：配置 → 日志 → 内存/网络 → 数据源 → 采样
  core/
    load.lua              装载器：同一份源码同时支持"读文件"和"require 资源"两种形态
    compat.lua            宿主能力探测：ctx / ffi / 加载器 / stingray，全部缓存
    mem.lua               只读内存层：ctx.read 优先，ffi 兜底，ffi 路径先 VirtualQuery 验址
    net.lua               出口：UDP（ffi）或文件广播，报文编码只在这里
    config.lua            分层配置：默认 ← bridge_config.lua/.txt ← MDL3 选项
    log.lua               有界日志 + 状态文件 + ctx.log
    clock.lua             时间与 dt
    json.lua              线缆协议用的最小 JSON 编码器
  sources/
    runtime.lua           HD2Runtime 公开 API（可选）
    direct.lua            game.dll 偏移只读源（构建指纹不匹配就拒绝）
    mock.lua              合成数据源
  state/
    sampler.lua           hello / heartbeat / state 三条流的节流与打包
  profiles/
    steam_25480438.lua    已研究的 game.dll 布局（只读子集）
```

每个模块都是 `return function(H) ... end`：`H` 是共享上下文（配置、日志、时钟、出口……），
装载器负责实例化一次并缓存。这样"读文件"和"require 资源"两条路径拿到的模块行为完全一致。

---

## 5. 配置

**优先级**：内置默认 ← `%LOCALAPPDATA%\hd2coyote\bridge_config.lua` ← `bridge_config.txt` ← MDL3 选项面板。
文件名和位置与旧版一致，老用户不用迁移。

```lua
return { config = {
    mode = 'safe',            -- safe | net | recon | live
    enabled = true,
    host = '127.0.0.1',
    port = 47777,
    interval = 0.10,          -- 状态上报间隔（秒），钳制在 0.02–5
    heartbeat = 2.0,          -- 心跳间隔（秒），不延长状态有效期
    transport = 'auto',       -- auto | udp | file | off
    profile = 'steam_25480438',
    log_dir = nil,            -- nil = %LOCALAPPDATA%\hd2coyote
    state_path = nil,         -- nil = <log_dir>\bridge_state.json
    mock = false,             -- true = 合成数据，不开游戏也能验链路
    max_error = 5,            -- 连续异常多少次自动停
    verbose = false,
} }
```

也接受纯文本形式（`bridge_config.txt`）：`mode = live` 一行一个键。

**内存 profile 不能由用户覆盖**：布局只随已测试版本发布，配置里写 `profile` 只会切到别的已发布布局，
不存在的名字会被拒绝并降级为"无数据源"。

---

## 6. 模式阶梯（默认 safe，逐级开启）

| mode | 行为 | 用来验证什么 |
|---|---|---|
| `safe` | 只写日志，不碰内存、不开 socket、不发包 | addon 到底有没有被加载、日志能不能写 |
| `net` | + 出口（hello + 心跳），**仍然不读游戏** | 部署、通信、端口三件事 |
| `recon` | + 一次性数据源体检，写 `recon_report.txt` | 当前构建能不能读到本地玩家与生命记录 |
| `live` | + 持续读取并按 `interval` 上报 | 正式使用 |

`live` 里找不到本地玩家（切船/重生）时每 3 秒重试，期间不发 state，但心跳继续 ——
控制器靠 state 的到达时间判活，心跳**不会**让旧数据"续命"。

---

## 7. 出口：UDP 还是文件广播

| 出口 | 条件 | 特点 |
|---|---|---|
| `udp` | LuaJIT（`ffi` 可用） | 10 Hz 无连接报文，掉一帧无所谓 |
| `file` | 无 `ffi` 也能用（只要 `io`） | 整文件覆盖写 `bridge_state.json`，控制器按 mtime 轮询；半个 JSON 视为掉包 |

`transport = 'auto'`（默认）先试 UDP，失败自动落到文件广播，并把原因写进日志与状态文件。
控制器侧对应两个事件源，配置里二选一：

```json
"sources": { "enabled": ["game_bridge"] }   // UDP
"sources": { "enabled": ["bridge_file"] }   // 文件广播
```

两个都开也安全（哪条先到算哪条），但只开你实际在用的那条，诊断面板更干净。

---

## 8. 数据源

| 源 | 条件 | 说明 |
|---|---|---|
| `runtime` | `require('mods/skyeshade/hd2runtime')` 成功且构建匹配 | 由 HD2Runtime 负责适配游戏构建；肢体伤势目前拿不到，恒为 0 |
| `direct` | ffi 或 MDL3 原生读内存可用，且 PE 映像 + 7 处指令 pin 全部匹配 | 布局固定在 `profiles/`；不匹配就拒绝，绝不猜、不扫描地址空间 |
| `mock` | `mock = true` | 合成掉血/受伤/倒地/阵亡/复活循环 |

判定顺序：`mock` > `runtime` > `direct`。前一个不可用会安静降级到下一个，原因写进日志。

---

## 9. 诊断

| 文件 | 内容 |
|---|---|
| `%LOCALAPPDATA%\hd2coyote\hd2_coyote_bridge.log` | 本次会话日志（有界），含环境探测、出口选择、数据源选择、异常 |
| `%LOCALAPPDATA%\hd2coyote\hd2_coyote_status.txt` | 一行结论 + addon/版本/模式/profile/出口 |
| `%LOCALAPPDATA%\hd2coyote\recon_report.txt` | `recon` 档的体检报告：宿主能力、内存/网络后端、全部配置项与来源、首个状态或失败原因 |

`compat.describe()` 会把 `lua` / `jit` / `ffi` / `ctx` / `ctx.members` 打进日志 —— 排查"这个宿主到底给不给 ffi"时先看这一行。

---

## 10. 排错表

| 现象 | 处理 |
|---|---|
| MDL 设置页看不到这个 mod | 本地 mod 要看 `%LOCALAPPDATA%\MDL\Helldivers2\Mods\hd2coyote\mod.lua` 是否存在；Arsenal 路线要看 Deploy 后 `data\9ba626afa44a3aa3.patch_N` 有没有落地 |
| 日志里 `出口: none` | `transport=udp` 但宿主没 ffi；改 `auto` 或 `file` |
| 状态是 `FAILED - 拿不到 game.dll 基址` | ffi 与 `ctx.module` 都不可用；改用 `runtime` 或文件广播 + `mock` 先验链路 |
| `游戏构建不匹配：<pin 名>` | 游戏更新了，偏移失效。**不要手填地址**；等 `profiles/` 更新，或装 HD2Runtime 走公开 API |
| 控制器一直"等待游戏桥" | UDP 端口被占（`python -m hd2coyote doctor`），或用了 `file` 出口却没启用 `bridge_file` 源 |
| 有状态但事件不触发 | 血量是**绝对值**口径（`hp` + `hp_max`）；确认 `hp_max` 读对了 |
| 游戏卡顿/黑屏 | 先把 `mode` 降回 `safe` 重启游戏确认是不是本桥；再按 `%APPDATA%\Arrowhead\Helldivers2\crash_data` 与事件查看器排查 |

---

## 11. 安全边界

* 全程**只读**：不写游戏内存、不改游戏数据、不调用游戏函数。
* 网络只发 `127.0.0.1`（UDP）或写本机文件（`%LOCALAPPDATA%\hd2coyote\`），不向外部主机发送任何数据。
* addon 不 `os.execute`、不 spawn 进程 —— 旧版曾因在主线程 `mkdir` 触发安全软件拦截，导致黑屏卡死，这条红线保留。
* 所有输出强度仍在控制器侧的安全层里（硬上限 / 急停 / 会话预算），Lua 侧无法绕过。

---

## 12. 与旧版（BSL）实现的差异

| 项 | 旧版（BSL） | MDL3 版 |
|---|---|---|
| 每帧回调 | 包装 `_G.update` | 本地 mod 走 `on_update(ctx, dt)`；addon 包仍包装 `_G.update`，并兼容 MDL3 沙箱 `_G`（rawget + 索引兜底、rawset + 普通赋值都写） |
| 入口信息 | `CowboyBingusModLoader` 全局、`open_log` | `ctx`（dir/log/settings）或纯 `-- HD2-Addon:` 契约 |
| 游戏内菜单 | Mod Options Menu 里注册 10 个信息行 | 本地 mod 用 MDL3 原生选项；addon 包只读配置文件 |
| 读内存 | 只有 `ffi` | `ctx.read` 优先，`ffi` 兜底，都没有就换数据源 |
| 出口 | 只有 `ffi` + `ws2_32.sendto` | UDP 或文件广播，自动选择 |
| 配置 | `bridge_config.lua` / `.txt` | 同一份文件 **+** MDL3 选项面板 |
| 代码组织 | 单文件 1353 行 | 16 个模块，每个只做一件事，离线可测 |

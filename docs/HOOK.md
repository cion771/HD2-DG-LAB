# 游戏内 Hook（Lua 桥）—— 实现说明与上机步骤

本文说明「不用画面识别、直接在游戏里取状态」这条路在《绝地潜兵 2》里**到底是什么**、
我实现了什么、哪些已经离线验证过、以及剩下哪一步必须你在本机上跑一次。

---

## 1. 先把事实说清楚

> ⚠️ **上机前必读第 9 节**：本桥默认什么都不做（`mode = safe`），
> 要逐级开启；并且记录了两起真实事故（一次崩溃、一次黑屏卡死）的处理与回退办法。

| 常见说法 | 实际情况 |
|---|---|
| "官方支持 mod / 有 mod API" | **没有。** Arrowhead 没有发布 HD2 的 mod API、SDK 或 Steam Workshop 支持。现有 mod 生态全部走社区工具链。 |
| "直接 hook 游戏" | 在 HD2 里指的是：用社区加载器 **Bingus Shared Loader** 把一个 Lua 资源塞进游戏自带的 **LuaJIT** 虚拟机里，addon 与游戏共享同一个 Lua 状态和地址空间，于是可以用 **FFI 只读游戏内存**。 |
| "内存 hook 会被反作弊封" | 游戏带 **nProtect GameGuard**。社区共识（也是本项目遵守的红线）：**进程内、只读**的 addon 目前被广泛使用（Enemy HP、G-60 Smart Targeting 等都在公开分发）；真正危险的是**从外部进程 OpenProcess + ReadProcessMemory**。本项目只做前者。 |
| 你的环境 | 本机已装 Bingus Shared Loader **v18 / API 1**，加载器日志里有 **21 个 addon**（`%LOCALAPPDATA%\CowboyBingus\Helldivers2\Logs\BingusSharedLoader.log`）。所以这条路对你是可用的。 |

## 2. 两条路线的取舍

| | 游戏内 Hook（本项目默认） | 屏幕识别（保留为可选） |
|---|---|---|
| 延迟 | 10Hz 状态流，事件几乎实时 | 抓屏 15fps + 像素判读 |
| 精度 | 直接读血量/损伤位，无歧义 | 依赖血条像素、图标颜色，有误差 |
| 标定 | 不需要；需要一次 recon 定位偏移 | 必须标定 HUD，改分辨率/HUD 缩放要重标 |
| 抗更新 | 游戏更新会漂移偏移（需重跑 recon） | 游戏改 HUD 也要重标 |
| 进程接触 | 在游戏进程内读内存（社区惯例） | 完全不碰游戏进程 |
| 反作弊 | 社区在用，但非官方保证 | 零接触 |

配置里 `source` 一项切换：`"hook"`（默认）或 `"vision"`。

## 3. 数据链路

```
《绝地潜兵 2》进程
 ├─ Bingus Shared Loader v15+（把 addon 当 Lua 资源加载）
 └─ mods/hd2coyote/hd2_coyote_bridge   ← lua/hd2_coyote_bridge.lua
      ├─ 环境闸门（API 1 / loader 版本 / FFI 可用性）
      ├─ 只读内存读取器（u8/u32/f32/指针，逐读 pcall 包裹）
      ├─ 定位本地玩家（ping actor 表里"自己发的 ping"的 creator）
      ├─ 按 profile 偏移读出 hp / hp_max / 肢体掩码 / 阵亡
      └─ 通过 FFI 调 ws2_32.sendto → UDP 127.0.0.1:47777
                    │
                    ▼
本机 hd2-coyote 控制器（Python）
 hd2coyote/hook.py  ← 收状态、复用 detectors 里那套健康/损伤/阵亡状态机
 → rules.py（强度/波形/通道）→ safety.py（硬上限/急停）→ DG-LAB Socket → 郊狼
```

控制器收到的是**状态**而不是事件：Lua 侧只忠实读值，判定逻辑（掉血累计、低血量迟滞、
阵亡保持、复活忽略期）全部复用视觉路线同一份、已被单测覆盖的代码 —— 逻辑只有一份。

## 4. 已经实现并且**已离线验证**的部分

| 部分 | 验证方式 |
|---|---|
| addon 能在 **LuaJIT 2.1** 下编译（游戏就是 LuaJIT，不是 Lua 5.4） | `test_compiles_under_luajit` + 禁用 API 扫描 |
| 指针不被截断 | 回归测试：`tonumber(hex,16)` 在 LuaJIT 里**只按 32 位解析**，`0x141000000` 会被截成 `0x41000000` —— 已改成逐字符解析（`test_pointer_above_32bit_not_truncated`） |
| 定位本地玩家、读 hp/掩码/阵亡 | 假内存喂进去，断言读出的值（`test_read_state_values`、`test_recon_finds_player_and_writes_evidence`） |
| 偏移读不到时**优雅降级**，不崩、不猜 | `test_recon_survives_bad_offsets`（STATUS.txt 写 FAILED 并说明原因） |
| live 模式缺偏移就拒绝启动 | `test_profile_without_offsets_refuses_live` |
| 每帧钩子包装游戏全局函数 | `test_live_mode_hooks_frame_function` |
| 找不到每帧钩子时明确报错并给出候选名 | `test_missing_frame_hook_reports_failed` |
| 报文格式 Python 侧认得 | **跨语言联调**：真 LuaJIT 发 UDP → 真 HookSource → 真的产出 `Damage` 事件（`test_lua_state_packets_drive_python_events`） |
| `.patch_N` 归档容器 + 资源名哈希 | 与**加载器自己的资源名**对照：`core/wwise/lua/wwise_flow_callbacks` → `0x7251FDD9BB62480A`（外部参考值，不是我自己的约定） |

所以：**除"游戏真实内存里的字段偏移"之外，所有环节都已经跑通并测过。**

## 5. 还差的一步：一次 recon（必须在你本机跑）

我无法凭空知道你这个游戏构建里"本地玩家的血量字段在哪个偏移"：

* 偏移是**某个构建的快照**，游戏更新就会漂移；
* 社区公开的偏移只有 `ping actor` 表那套（已内建并署名），血量字段没人公开过；
* 硬编码一个猜的偏移 = 读垃圾值 → 触发莫名其妙的电击，**这绝对不能接受**。

所以 addon 默认跑 `mode = "recon"`：一次性、有界、只落盘证据，然后进入终止状态（不周期扫描、不吃帧）。

### 跑 recon 的步骤

```powershell
# 1) 打包（生成 build/HD2-Coyote-Bridge-<VERSION>.zip）
python tools\build_addon.py

# 2) 用 Arsenal / HD2 Mod Manager 导入这个 ZIP，启用，Deploy，重启游戏
#    进一局任务（飞船里 ping actor 可能还没建立，最好在局内），随便 ping 一下地面
# 3) 看产物：
notepad "%LOCALAPPDATA%\hd2coyote\hd2_coyote_status.txt"     # 第一行就是结论
notepad "%LOCALAPPDATA%\hd2coyote\recon_report.txt"          # 证据：基址、锚点地址、hex dump、_G 函数名
```

把 `recon_report.txt` 发我，我可以直接算出偏移；或者你自己对着 dump 找
"一个 float 在受伤时会下降"的字段。

### 填偏移（不用重新打包）

在 `%LOCALAPPDATA%\hd2coyote\bridge_config.lua` 写：

```lua
return {
  config = { mode = 'live', interval = 0.1, frame_hooks = { '<recon 里看到的真实函数名>' } },
  profiles = { steam_25480438 = {
      hp_anchor = 'ping_record',   -- 或 'base'
      hp        = 0x2C,            -- float，绝对值
      hp_max    = 0x30,            -- float，可选
      limb_mask = 0x38,            -- u8，位掩码
      limb_mask_bits = { 1, 2, 4 },-- 每一位对应哪个肢体
      dead      = 0x3C,            -- u8，可选
  } },
}
```

`hp_anchor` 决定偏移相对谁：`ping_record`（定位到的本地玩家锚点，默认）或 `base`（模块基址）。

### 每帧钩子

加载器只负责 `require` 一次，**游戏没有公开的每帧回调**，所以要包装游戏自己的全局更新函数。
名字因构建而异 —— `recon_report.txt` 里的 `globals_functions=` 一行就是 `_G` 里真实存在的函数名，
把候选填进 `config.frame_hooks` 即可。找不到时会明确写进 STATUS.txt，而不是"静默不干活"。

## 6. 游戏内设置界面（ESC → MODS 页）

用社区的 **Mod Options Menu** 做原生设置页 —— 你机器上已经装了它
（加载器日志里的 `mods/cowboybingus/mod_options_menu: loaded`），所以不用额外准备什么。

**位置**：游戏里按 `Esc` → 第四个标签页 **MODS** → 分类按钮 **HD2 COYOTE 郊狼**。
用法和游戏自己的 OPTIONS 页一样：改完按 **APPLY**（键盘 Tab），直接离开会弹
UNAPPLIED CHANGES 提示（确认 = 丢弃改动）。

| 选项 | 类型 | 范围/取值 | 说明 |
|---|---|---|---|
| 启用上报 Enabled | 开关 | 开/关 | 关掉后桥不再发状态，控制器会因断流自动静音 |
| 模式 Mode | 选择 | 侦察 Recon / 运行 Live | **不用重启游戏**：切 Live 立刻开始按偏移上报；切回 Recon 立刻重跑一次侦察 |
| UDP 端口 Port | 滑块 | 1024–65535 | **必须与控制器 `config.json` 里的 `hook.port` 一致**（默认 47777） |
| 上报间隔(秒) Interval | 滑块 | 0.05–1.00 | 越小越灵敏 |
| 血量偏移 HP Offset | 滑块 | -1–65535 | -1 = 未设置；recon 之后填这里最方便 |
| 血量上限偏移 HP Max | 滑块 | -1–65535 | 可选，缺省按 100 算 |
| 肢体掩码偏移 Limb Mask | 滑块 | -1–65535 | 可选，读 1 字节按位表示受伤部位 |
| 肢体起始位 Limb Shift | 滑块 | 0–7 | 掩码里第几位开始对应「左肢/躯干/右肢」 |
| 阵亡标志偏移 Dead | 滑块 | -1–65535 | 可选，不填就只用血量判断 |
| 每帧钩子 Frame Hook | 选择 | `update` / 其它 / `none` | 默认包装全局 `update`（社区通行做法）；`none` = 不挂钩子、不上报 |

**值存在哪**：`%LOCALAPPDATA%\CowboyBingus\Helldivers2\Logs\ModOptionsMenu.values`
（旁边有 `.bak` 备份；两个都删掉即恢复默认）。
没在菜单里动过的选项，默认值等于你 `bridge_config.lua` 里的值 —— 所以**先文件、后菜单**，
两边不会互相打架。

**两个来源的分工**：

* `bridge_config.lua`（文件）：锚点语义（`hp_anchor` 等）、profile、以及"随包发布的默认值"；
* 游戏内 MODS 页：日常调节（模式 / 端口 / 偏移 / 钩子），改完立刻生效、跨会话保存。

**端口提醒**：在菜单里改了端口，控制器那边也要改（`config.json` → `hook.port`），
否则状态发到了没人听的端口上。

**兼容与降级**（都有测试守着）：

* 没装 Mod Options Menu → 不崩、不刷屏：每帧重试 `menu_retry_frames`（默认 600 帧 ≈10 秒）后
  放弃注册并写日志，文件配置照常工作。
* 加载器 **v19+**（有 `capabilities.after_startup`）→ 在 `after_startup` 里注册（官方推荐时机）；
  **v18** 没有这个能力 → 走每帧重试。两条路都测过。
* 注册失败（比如"这个 mod 已经 32 个选项"）→ 记下原因继续跑，不影响桥的功能。

## 7. 排查表

| 现象 | 处理 |
|---|---|
| 加载器日志里没有 `mods/hd2coyote/...: loaded` | 用管理器重装；确认 `-- HD2-Addon:` 首行没被破坏（打包脚本会校验）；别和别的 patch 抢同一个 `.patch_N` 编号 |
| STATUS.txt 是 `FAILED - Bingus Shared Loader 太旧` | 升级加载器到 v15+（你已经是 v18） |
| STATUS.txt 是 `FAILED - 拿不到模块基址` | 大概率是 FFI 被禁；看 addon 日志 |
| recon 说"没找到本地玩家" | 进任务里再跑（飞船里 ping actor 可能不存在），并确认 `ping_actors` 偏移对这个构建仍有效 |
| 控制器界面一直"等待游戏内 Lua 桥上报" | 检查 UDP 端口（默认 47777）没被占用：`python -m hd2coyote doctor` |
| 有状态但事件不触发 | 血量是**绝对值**口径（`hp` + `hp_max`）；确认 `hp_max` 读对了 |

## 8. 我为什么没做"外部进程读内存"* 那是 GameGuard 最容易抓的动作；
* 社区踩坑清单里第一条就是"不要从外部进程读游戏内存"。

本项目的红线：**只读、进程内、不写游戏内存、不改游戏数据、不向外部网络发送任何数据**
（只发 127.0.0.1 的 UDP）。所有输出设备控制仍在 Python 侧的安全层里（硬上限 / 急停 / 会话预算）。

---

## 9. 模式阶梯与两起真实事故（必读）

### 事故记录

| 时间 | 现象 | 原因 | 现在的防护 |
|---|---|---|---|
| 17:57 | 游戏弹出"此游戏发生错误"（崩溃） | 0.2.2 按**过期的偏移**解引用内存；LuaJIT 的 FFI 访问违例 `pcall` 挡不住 | 每次读之前 `VirtualQuery` 验址（`range_readable`） |
| 18:05 / 18:10 | **黑屏 + 游戏停止响应**（Windows `Application Hang`） | 0.2.3 部署期间；加载器日志停在"准备加载第一个 discovered addon"处（按 patch 号倒序第一个就是本 addon），本 addon 一行日志都没写 → 卡在启动最前面。头号嫌疑：`os.execute('mkdir …')` 在主线程同步 spawn `cmd.exe`，被安全软件/GameGuard 拦住 | **彻底删除 `os.execute`/`mkdir`**；默认 `safe` 模式什么都不做 |

### 模式阶梯（默认 safe，逐级开启）

改 `%LOCALAPPDATA%\hd2coyote\bridge_config.lua`：

```lua
return { config = { mode = 'menu' } }   -- 或 'recon' / 'live'
```

| mode | 行为 | 用来验证什么 |
|---|---|---|
| `safe`（默认） | 只写日志 | addon 有没有被加载、日志能不能写 |
| `menu` | + UDP 上报 + 游戏内菜单（不读游戏内存） | 部署/通信/菜单三件事 |
| `recon` | + 一次性内存侦察 | 偏移在你的构建上对不对 |
| `live` | + 按偏移实时上报 | 正式使用 |

### 出问题怎么回退

1. Arsenal 里**禁用** HD2 Coyote Bridge → **Purge** → **Deploy**（它会把部署的文件收回去），或者直接删 `data\9ba626afa44a3aa3.patch_<N>` 及其两个 sidecar。
2. 游戏还卡/黑屏？那就与本 mod 无关，按顺序试：
   * Steam → 右键游戏 → 属性 → 已安装文件 → **验证游戏文件完整性**（修复 `data/` 与 `bin/GameGuard`）；
   * 重启电脑（清掉残留的 GameGuard 进程/驱动状态）；
   * 检查 `%APPDATA%\Arrowhead\Helldivers2\crash_data` 与 Windows 事件查看器里的 `Application Hang`。
3. 想快速确认是不是某个 mod 的问题：Arsenal 里只启用必需项（Bingus Shared Loader + Vanilla Plus Megapack），Deploy 后启动。

### 为什么 `safe` 模式不能省

它是唯一能回答"这个 addon 到底有没有跑起来"的档位：
日志里出现 `启动 v0.3.0 mode=safe` + `STATUS: OK - safe 模式…` 就说明
**部署链路、加载器发现、日志路径**全部正常，再往上开才有意义。

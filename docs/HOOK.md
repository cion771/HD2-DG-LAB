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

## 2. 状态来源

0.5.1 起已移除截图、像素识别与 HUD 标定。控制器仅接收结构化状态/事件：默认 `game_bridge`（本机 UDP），可选 `http`，由 `sources.enabled` 配置。旧 `source = "vision"` 配置读取时迁移为事件源模式。

游戏桥无需屏幕标定，也不再让用户手填字段偏移。0.5.6 内置当前受支持构建的只读 profile，并在运行时验证 `game.dll` 映像与关键指令；游戏更新后不匹配会安全拒绝，仍不能保证反作弊兼容或账号安全。

## 3. 数据链路

```
《绝地潜兵 2》进程
 ├─ Bingus Shared Loader v15+（把 addon 当 Lua 资源加载）
 └─ mods/hd2coyote/hd2_coyote_bridge   ← lua/hd2_coyote_bridge.lua
      ├─ 环境闸门（API 1 / loader 版本 / FFI 可用性）
      ├─ 只读内存读取器（u8/u32/i32/指针，每次读取前 VirtualQuery 验址）
      ├─ 校验 game.dll PE 映像大小与七处关键指令指纹
      ├─ 本地用户 → 玩家槽 → Avatar network id → 本机拥有的 Helldiver 实体
      ├─ health manager 读 hp / hp_max / 六肢原生伤势 / 倒地与阵亡
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
| 选择 `game.dll` 而不是主 EXE | 假 FFI 记录 `GetModuleHandleA("game.dll")` 参数 |
| 构建指纹、本地玩家、health manager 全链路 | 按当前构建布局造假内存，断言 HP、最大 HP、六肢伤势、倒地/阵亡（`test_read_state_values`、`test_recon_finds_player_and_writes_evidence`） |
| 地址、指纹或记录读不到时**优雅降级** | STATUS 写 FAILED，live 停止 state 上报并每 3 秒重试；不崩、不猜 |
| 无任何手填偏移也能启动 live | `test_profile_without_manual_offsets_reads_automatically` |
| 每帧钩子包装游戏全局函数 | `test_live_mode_hooks_frame_function` |
| 找不到每帧钩子时明确报错并给出候选名 | `test_missing_frame_hook_reports_failed` |
| 报文格式 Python 侧认得 | **跨语言联调**：真 LuaJIT 发 UDP → 真 HookSource → 真的产出 `Damage` 事件（`test_lua_state_packets_drive_python_events`） |
| `.patch_N` 归档容器 + 资源名哈希 | 与**加载器自己的资源名**对照：`core/wwise/lua/wwise_flow_callbacks` → `0x7251FDD9BB62480A`（外部参考值，不是我自己的约定） |

这些测试证明的是解析与安全闸门；**首次在真实游戏上仍必须用 Mock、保持未武装，核对读数后才能连接设备。**

## 5. 自动读取与 recon

0.5.6 的 `steam_25480438` profile 对应游戏构建 `1.8.46015.0`。它的地址全部是 `game.dll` RVA，不是 `helldivers2.exe` 地址。桥获取 `game.dll` 基址后，先验证 PE `SizeOfImage` 与七处关键指令，再沿游戏自己的管理器结构自动定位本地 Avatar 和生命记录。

用户配置只能选择 mode、port、interval 和 profile，不能覆盖内存布局；旧配置里的 `profiles` 会被忽略。这样旧版本留下的示例值不会绕过新构建校验。

### 建议验证步骤

```powershell
# 1) 打包（生成 build/HD2-Coyote-Bridge-<VERSION>.zip）
python tools\build_addon.py

# 2) 用 Arsenal / HD2 Mod Manager 导入 ZIP，启用、Deploy，并完整重启游戏
# 3) 桌面端选择 Mock、保持未武装；桥先设 recon 或直接 live
# 4) 看本机诊断：
notepad "%LOCALAPPDATA%\hd2coyote\hd2_coyote_status.txt"
notepad "%LOCALAPPDATA%\hd2coyote\recon_report.txt"
```

`recon` 只执行一次，报告构建、Avatar 实体及 health 解析结果；`live` 持续读取，飞船切换/重生时数据暂时消失会自动重试。构建不匹配时不要手填地址，应等待发布适配该构建的新 profile。

### 每帧钩子

加载器只负责 `require` 一次，**游戏没有公开的每帧回调**，所以要包装游戏自己的全局更新函数。
名字因构建而异 —— `recon_report.txt` 里的 `globals_functions=` 一行就是 `_G` 里真实存在的函数名，
把候选填进 `config.frame_hooks` 即可。找不到时会明确写进 STATUS.txt，而不是"静默不干活"。

## 6. 游戏内连接信息与日志（ESC → MODS 页）

安装可选的 [Mod Options Menu](https://github.com/CowboyBingus/ModOptionsMenu) 后，
按 **Esc → MODS → HD2 COYOTE 郊狼**。桥的 `menu`、`recon`、`live` 档会注册信息页；
`safe` / `net` 档不注册，安全分级不变。

菜单仅展示以下信息，选中行后在原生说明区域查看详情：

| 信息行 | 内容 |
|---|---|
| 连接目标 | UDP 目标地址与端口 |
| 本地发送状态 | UDP 未就绪、尚未发送、最近发送成功或失败 |
| 发送统计 | 本次会话成功/失败次数 |
| 桥接信息 | 版本、运行模式、profile、上报开关与帧钩子状态 |
| 最近日志 1–6 | 本次会话最近六条日志，最新在前；长行截断，完整日志仍保存在本地 |

**UDP 无应答握手**：发送成功只代表交给本机网络栈，不能据此确认桌面端收到数据，
更不能确认手机 App 或 DG-LAB 设备连接。请到桌面端确认设备状态。

**菜单接口限制**：公开 api=1 没有纯文本/禁用行，所以信息行仍借用原生选择行，
左右两个位置显示相同的信息摘要，选中行可查看完整说明。箭头可能可见，切换占位值可能触发原生 APPLY 提示，
但不会修改桥配置、触发侦察或控制输出；查看信息无需 APPLY。
不调用共享菜单的 set，避免干扰其他 mod 的保存计时。
Mod Options Menu v1.1+（version ≥2）在重新打开 ESC 时刷新动态说明，**不是实时滚动日志**；
v1.0 显示升级提示，请在桌面端查看诊断。

模式、端口、profile、上报开关和钩子均不再能从游戏菜单修改。
请在 Fluent 桌面版或桥配置文件中设置，然后重启游戏。
旧菜单保存值会被忽略，无需删除共享菜单的 values 文件（避免影响其他 mod）。
更新 addon 后也应重启游戏，已注册的旧选项不能在同一游戏会话中注销。

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
| recon 说“没找到本地玩家” | 飞船切换/重生时 Avatar 可暂时不存在，等数秒再试；持续失败则确认版本 `0.5.6` 与受支持构建 `1.8.46015.0` |
| STATUS 提示“游戏构建不匹配” | 游戏已更新；不要手填偏移，等待发布适配该构建的新 profile |
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
| `recon` | + 一次性构建、玩家与生命记录诊断 | 自动读取链路在当前构建上是否完整 |
| `live` | + 经指纹校验后自动读取并实时上报 | 正式使用 |

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

# 更新记录

版本号唯一来源：仓库根目录的 `VERSION` 文件。三处必须一致，否则打包与测试会直接失败：
`VERSION` 文件 / `hd2coyote.__version__` / `lua/hd2_coyote_bridge.lua` 的 `local VERSION`。

---

## 0.4.2

**「关闭程序」：网页按钮 + `stop.bat` + `python -m hd2coyote stop`。**

之前 Web 控制台只能在终端里 Ctrl+C —— 窗口一关就找不到怎么退了。现在三条路：

- **网页右上角「关闭程序」按钮**（带确认框）：先 `engine.stop()`（socket 设备会
  清波形 + 强度归零 + 断开连接），再让服务器退出；只允许**本机来源**调用（非 127.0.0.1 返回 403，
  万一有人开在 0.0.0.0 上也不会被远端关掉）。
- **`stop.bat`**：先请求优雅退出，等端口释放；没成功才按端口找出进程强杀（并提示这次是强杀）。
- **`python -m hd2coyote stop [--port 8787] [--no-force]`**：同一套逻辑的 CLI。

顺带修的（都是实测踩出来的）：

- `stop.bat` / `run.bat` 之前是 **LF 行尾 + 中文注释**，cmd 会按 OEM 码页解析而切坏行
  （实测报 `'再按端口找出进程强杀。' is not recognized`）→ 改成**纯 ASCII + CRLF**。
- `.bat` 里找不到虚拟环境时会退回系统 Python，而 `python -m hd2coyote` 顶层导入
  `engine`/`safety` 会连带要求 numpy/websockets → **改成惰性导入**，
  现在"关掉控制器"不再依赖设备栈是否装好（裸 Python 也能 `stop`）。
- `run.bat` 会优先复用上级目录的 `.venv`（开发布局），没有才创建本地 venv。

新增测试：优雅退出后端口真的释放、事件留痕、没有运行时明确报告、
`port_pids()` 能通过 netstat 找到占用端口的进程、`is_loopback()` 判定。

---

## 0.4.1

**Web 控制台 + 修掉"配置写了却不生效"的真凶（我自己引入的解析 bug）。**

### Web 控制台（`python -m hd2coyote web`）

浏览器里管全部事情，替换原来的 Tkinter 窗口。详见 `docs/WEBUI.md`。

- 实时状态（血量/肢体/输出强度/武装状态/桥在线）、测试脉冲、强度滑杆（立即生效并落盘）；
- 游戏内桥的档位/端口/间隔/偏移写入 `bridge_config.lua`（校验 + 备份 `*.lua.bak`）；
- **诊断区**：桥上次实际运行的版本与档位、addon STATUS、加载器日志行、桥日志尾部、**recon 报告**；
- 诊断会主动点出两种最容易踩的状态：没有配置文件（跑内置 `safe`）、
  **配置档位 ≠ 上次实际运行档位**（改了没重启游戏）。
- 只监听 `127.0.0.1`、只用标准库、页面不引任何外部资源（离线可用）。

### 修：Lua 形式的配置被误判成纯文本

22:26 那局的日志铁证（0.3.3）：

```
按纯文本 key = value 解析：…\bridge_config.lua     ← 走错分支
未知 mode='menu'（可选 safe/net/menu/recon/live），回退 safe
```

原因：我用 `^%s*(%a+)` 取"第一个 token"判断是不是 `return {`，而文件以 `--` 注释开头，
`-` 不是字母 → 判定成纯文本 → `mode = 'menu' })` 被解析成**带引号的** `"'menu'"`
→ 未知档位 → 回退 safe。于是"文件在、却不生效、菜单永远不出现"。

现在：`strip_leading_comments()` 先跳过开头空行与 `--` 注释再判形式；纯文本解析也会
去掉行内 `--` 注释并剥掉引号。回归测试直接拿 `webui.render_bridge_config()` 的输出
去喂真 addon，断言 `生效模式 = live`（已用真 addon 跑通：写 `recon` → 解析出 `mode=recon`）。

### 顺带

- `tests/test_real_patches.py` 能区分 **mod 容器** 与 **Steam 原版归档**
  （同一个 `9ba626afa44a3aa3.patch_N` 族名；原版是另一种压缩容器、版本号 2）：
  只对 mod 容器做逐字节往返校验，但要求至少有一个 mod 容器，并断言原版归档被**干净地**拒绝。

---

## 0.4.0

**Web 控制台的第一版**（浏览器管理：状态 / 强度 / 桥配置 / 诊断 / 动作）。
本版之后游戏内菜单不再是主路径 —— 桥的 `menu` 档保留可用，但配置与诊断都在网页里做。

---

## 0.3.3

**修复"配置文件明明在、addon 却一直跑默认值，而且日志里一个字都没有"。**

- 现象：`%LOCALAPPDATA%\hd2coyote\bridge_config.lua`（`mode = 'menu'`）写于 21:39，
  游戏 22:18 启动，日志里却还是 `启动 v0.3.2 mode=safe` —— 菜单当然不会出来。
- 原因：`apply_user_config()` 用 `loadfile()` 读配置，**失败时静默 return false**。
  本环境里 `loadfile` 是否可用无法确认（内置默认值一直在生效，且没有任何错误信息）。
- 现在改为只依赖已被证明可用的接口：
  * 读文件用 `io.open(path,'r')`（本 addon 一直用它写日志，已验证可用）；
  * 编译用 `loadstring`（加载器自己就用它）；
  * **额外支持纯文本形式** `mode = menu`（连 loadstring 都不需要）；
  * 找不到 / 读不了 / 语法错 / 执行错 / 返回值不是表 / mode 拼错 —— **每一种都写日志并说明**，
    未知 mode 回退 `safe`。
- 启动日志改成先 `启动 v0.3.3`，应用配置后再打 `生效模式 = menu（配置目录 …）`，
  不再出现"日志里的模式 ≠ 实际生效模式"这种误导。

### 顺带做的逆向（回答"为什么菜单没出来"）

把 `ref/` 下社区 mod 的**明文源码**导出后核对（`tools/extract_lua_strings.py` 可复现）：

- `ModOptionsMenu` 的真实 API：`_G.ModOptionsMenu = {api = 1, version = 2,
  max_mods = 按钮数, max_options = 每页行数}`，`register_option(id, spec)`、
  `get/set/on_change/ready`；`spec` 字段 `type/label/mod/mod_id?/default/choices/min/max/step/gap/description`，
  **分类名取 `spec.mod` 并做 upper()**；label ≤64、mod ≤40、choice ≤48、description ≤400（按字符数，v2）。
- **确认 `_G.update` 就是官方的每帧挂钩点**：Mod Options Menu 自己最后也是
  `local previous_update = rawget(_G,'update'); update = function(dt) … end`。
- 结论：我的注册用法与真实实现一致；菜单没出来**不是注册失败，而是根本没执行到注册那一步**。

---

## 0.3.2

**修一个会让 UDP 静默失效的隐患 + 把台阶再降一级。**

- `socket` / `sendto` 在 **ws2_32.dll** 里，走 `ffi.C` 不一定解析得到符号 ——
  失败时不会有任何报错，只是 `net.ready = false`、控制器永远收不到状态。
  现在改为显式 `ffi.load('kernel32')` / `ffi.load('ws2_32')`，测试里加了闸门
  （源码不得再出现 `ffi.C.`）。
- 实测确认：`bridge_config.lua` 不存在时 addon 停在默认 `safe` 档，
  **不会注册任何菜单**（用户看到"没有菜单"就是这个原因，不是注册失败）。
- 现在写一份 `%LOCALAPPDATA%\hd2coyote\bridge_config.lua`（`mode = 'menu'`）即可看到菜单。

---

## 0.3.1

**上机验证通过 + 阶梯再切细一档。**

- 实测（用户机器，Bingus Shared Loader v18 / API 1）：
  ```
  mods/hd2coyote/hd2_coyote_bridge: loaded
  STATUS: OK - safe 模式：只写日志，不做任何 FFI / 内存 / 菜单操作
  addon=… version=0.3.0 mode=safe loader_api=1
  ```
  游戏正常进入。**部署链路、加载器发现、日志（加载器共享日志 + 私有目录）全部打通**，
  也反证了之前那次黑屏出在旧的启动路径上。
- 新增 `mode = 'net'` 档：只做 FFI + UDP（发 hello），**不挂钩子、不读游戏内存**。
  阶梯变成 `safe → net → menu → recon → live`，每一步只多一件事，出事能立刻定位。
- 日志改为**双写**：加载器共享日志（`open_log`，每会话清空）+ 私有日志（追加、跨会话可查）。
- 小插曲：给 VERSION 文件打版本时 PowerShell 写进了 BOM，
  **被 0.2.1 加的版本一致性闸门当场拦下**（`打包失败：版本号不一致`）—— 闸门有效。

---

## 0.3.0

**安全重构：默认什么都不做，逐级开启；启动路径里去掉一切可能阻塞的调用。**

上机事故记录（都是真事，不藏）：

| 时间 | 现象 | 结论 |
|---|---|---|
| 17:57:16 | 游戏异常报告（CRS），弹出"此游戏发生错误" | **0.2.2 的错**：偏移过期 → 解引用无效地址 → 访问违例 |
| 18:05:50 / 18:10:20 | Windows 事件日志 `Application Hang`（AppHangB1），表现为**黑屏 + 停止响应** | 0.2.3 部署期间；加载器日志每次都停在"注册表模块加载完、准备加载第一个 discovered addon"处，而按 patch 号倒序**第一个就是本 addon**，且本 addon 一行日志都没写 → 卡在启动最前面 |

本版针对第二条做的改动：

- **删掉 `os.execute` / `mkdir`**。它在主线程同步 spawn `cmd.exe`（`system()`），
  一旦被杀软/GameGuard 拦住就是"停止响应 + 黑屏"。现在只做「打开日志」：
  优先用加载器提供的 `open_log`（那个目录由加载器保证可用），
  其次才试 `%LOCALAPPDATA%\hd2coyote`（**不创建目录**，写不了就只写进日志）。
- **模式阶梯**（默认 `safe`，改 `bridge_config.lua` 逐级开启，每一步都可回退）：
  | mode | 行为 |
  |---|---|
  | `safe`（默认） | 只写日志。不碰 FFI、不读内存、不挂钩子、不发 UDP、不注册菜单 |
  | `menu` | + UDP 上报 + 游戏内菜单（**仍不读游戏内存**） |
  | `recon` | + 一次性内存侦察（偏移未验证时用） |
  | `live` | + 按偏移实时上报 |
- 每一步都写日志/STATUS；`recon` 失败时把**具体原因**写进 STATUS（"actor 数量异常(9999)" 之类），
  而不是笼统的"未找到"。
- 测试新增：`safe` 模式**必须**零 FFI 调用（VirtualQuery 调用数 = 0、内存读取 = 0、不发 UDP、不挂钩子）；
  `menu` 模式**必须**零内存读取；源码里不得出现 `os.execute` / `mkdir` / `io.popen`。

---

## 0.2.3

**修复：游戏崩溃（0.2.2 上机实测崩了两次）。**

- 现象：补丁部署成功后，游戏启动即崩；addon 自己的日志停在
  `已挂钩子：update` 之后，再没有任何输出（`recon_report.txt` / `STATUS.txt` 都没写）。
- 原因：addon 直接按 profile 偏移去读内存，而**这些偏移来自另一个游戏构建**。
  偏移过期 → 读出垃圾指针 → `ffi.string` 撞到未映射地址 → **进程级访问违例**。
  LuaJIT 的 FFI 越界访问是段错误级别，`pcall` 完全挡不住 —— 我把"只读"当成了"安全"，
  这是设计缺陷。
- 现在**每一次读内存之前都先用 `VirtualQuery` 验址**（`range_readable`）：
  必须 `MEM_COMMIT`、页保护可读、且整段落在同一个区域内，才允许取字节。
  偏移过期时最坏结果是"读到垃圾"，由原有的合理性校验（actor 数量 ≤16 等）拒绝。
- **安全闸门**：拿不到 `VirtualQuery` 时，addon 一个字节都不读，STATUS.txt 写
  `FAILED - 拿不到 VirtualQuery：为安全起见不进行任何内存读取（防止游戏崩溃）`。
- 进程里每次读都走同一个入口 `reader.at()`，这是唯一的取值点。
- 顺带修：recon 的 hex dump 之前把**原始内存字节**当十六进制文本拼进报告，
  报告会是乱码且不是合法 UTF-8 —— 现在逐字节 `%02X` 格式化。
- FFI 声明全部改成私有名 + `__asm__` 别名（`Hd2Coyote_VirtualQuery` 等），
  遵守加载器文档的要求：整个游戏只有一份 cdef，用共享名会和别的 mod 打架。
- 启动日志增加进度标记（模块基址 / UDP 初始化 / hello / 开始定位），
  万一还有问题，日志能精确停在出事的步骤上。
- 新增 `tests/test_lua_safety.py`（8 项）：未映射地址读 → nil、越界读 → 拒绝、
  垃圾指针 / 内核态地址 → 定位失败但不崩、缺 VirtualQuery → 拒绝读内存、正常路径不被误伤。

---

## 0.2.2

**修复：Arsenal 导入成功、Deploy 也"成功"，但复制 0 个文件。**

- 决定性证据（Arsenal 自己的日志）：
  ```
  Deploying "HD2 Coyote Bridge" - source: …\HD2-Coyote-Bridge-0.2.1_AR731028
  Deployed "HD2 Coyote Bridge" successfully - 0 files copied      ← 0！
  Deployed "Bingus Shared Loader - v18" successfully - 3 files copied
  ```
- 原因：**manifest.json 缺少 `Options` 数组**。Arsenal 的 manifest v1 里，
  `Options[].Include` 是"这个 mod 要部署哪些目录"的唯一依据；没有 Options，
  Arsenal 就认为没有任何文件可部署（mod 出现在列表里，游戏 `data/` 里却是空的）。
  对比同一台机器上能正常部署的包：Bingus Shared Loader 的 `Options[0].Include = ["data"]`，
  Vanilla Plus Megapack 有 18 个 option，每个各带自己的 Include。
- 现在生成的 manifest 与它们同构：
  ```json
  { "Version": 1, "Guid": "…", "Name": "HD2 Coyote Bridge", "Description": "…",
    "IconPath": "",
    "Options": [ { "Name": "HD2 Coyote Bridge", "Description": "…", "Include": ["data"] } ] }
  ```
  `Include` 会跟着 `--layout` 一起变（`data` → `["data"]`、`addon` → `["Addon"]`、`root` → `["."]`）。
- 测试新增断言：manifest 必须有 `Options[].Include`，且与 ZIP 内实际目录一致。

---

## 0.2.1

**修复：HD2 Arsenal 导入成功但一个文件都不部署。**

- 原因：ZIP 里补丁放在 `Addon/` 下。Arsenal 把非 `data/` 的顶层目录当成"可选分组"，
  导入后 `deployment_snapshot.json` 里 `patchFiles` 为空 —— mod 列表里有它，
  游戏 `data/` 里却什么都没有（实测：导入时间对得上，patch 数量不变）。
- 现在默认布局改为 `data/9ba626afa44a3aa3.patch_N` + 两个 0 字节 sidecar
  （`.stream` / `.gpu_resources`），与用户库里能正常部署的 Bingus Shared Loader 包结构完全一致。
- 新增 `--layout data|root|addon` 切换摆放方式；`addon` 保留给认这个结构的管理器。
- 打包后同时产出裸归档与 sidecar，供"手工拷进 data/"这条路线使用。

---

## 0.2.0

**默认数据源从屏幕识别换成游戏内 Hook，并在游戏里加了设置界面。**

### 新增
- 游戏内只读 Lua 桥 `lua/hd2_coyote_bridge.lua`（Bingus Shared Loader addon）：
  只读 FFI 定位本地玩家、读血量 / 肢体掩码 / 阵亡，10Hz 通过 UDP 上报；
  复用控制器里同一套健康/损伤/阵亡状态机，判定逻辑只有一份。
- 控制器的 `hook` 数据源（默认），`source: "vision"` 仍可用。
- **游戏内设置界面**：接入 Mod Options Menu（ESC → MODS → HD2 COYOTE 郊狼），
  10 个选项：启用上报 / 模式（侦察·运行，切换不用重启）/ UDP 端口 / 上报间隔 /
  血量偏移 / 血量上限偏移 / 肢体掩码偏移 / 肢体起始位 / 阵亡标志偏移 / 每帧钩子。
- `tools/hd2_patch.py`：`.patch_N` 归档读写（按真实归档逆向）。
- `tools/build_addon.py`：打包成 mod 管理器可导入的 ZIP；`--patch-number auto` 自动挑空号，
  绝不覆盖别人的补丁。
- `tools/inspect_game_patches.py`：部署自检（游戏里到底装没装上、在哪个 patch）。
- `tools/fake_addon.py`：假装游戏内桥在发 UDP，无游戏联调。
- `docs/HOOK.md`：Hook 路线的原理、已验证项、recon 步骤、排查表。
- 每帧钩子默认包装全局 `update`（官方第三方参考确认的通行做法）；
  包装时参数/返回值原样透传、自身错误 pcall 隔离，不影响更新链。

### 修复
- **`.patch_N` 容器格式**：社区文档的字段尺寸有误（头部 72→实际 **80** 字节、
  类型记录 32→实际 **24** 字节、条目的 `length` 实际**含 8 字节体头**）。
  早先的打包器产出的归档游戏无法加载。现已用真实归档验证：
  **20/20 解析成功，且解析→重新序列化 20/20 逐字节一致**。
- **LuaJIT 的 `tonumber(hex,16)` 只按 32 位解析**：`0x141000000` 会被截成 `0x41000000`，
  指针一律改成逐字符解析（有回归测试）。
- 日志目录写不进去时，自动退回加载器的共享日志目录（`Hd2CoyoteBridge.log`），
  保证任何情况下都有诊断可查。
- 版本号收敛到 `VERSION` 文件：之前改内容不改版本号（三轮都是 0.1.0），
  现在打包与测试都会因不一致而失败。

### 兼容
- UDP 协议 v1 保持兼容（`hello` 新增 `ver` 字段，旧控制器会忽略）。
- 加载器 v18：菜单注册走每帧重试（有 600 帧上限）；v19+ 走 `after_startup`。

---

## 0.1.0

- 首版：屏幕识别（血条填充率 / 橙红损伤图标 / 阵亡模板）+ 规则引擎 + 安全层
  （硬上限 / 急停 / 会话预算）+ DG-LAB Socket 设备层 + Tkinter 界面。
- 注：该版本的 ZIP 使用错误的容器布局，**实际无法被游戏加载**，请使用 0.2.0。

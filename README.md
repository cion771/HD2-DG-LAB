# hd2-coyote —— 《绝地潜兵 2》× 郊狼 DG-LAB

把《绝地潜兵 2》里的**受伤、肢体损伤、阵亡**变成郊狼（DG-LAB Coyote）的电击反馈，
强度可以在界面上随时自由调节。

状态来源默认是**游戏内 Hook**：一个只读的 Lua 桥跑在游戏的 LuaJIT 里，直接读本地玩家的
血量 / 肢体损伤 / 阵亡，通过 UDP 发给本机控制器（不用画面识别、不用标定 HUD）。
原理、已验证项与上机步骤见 **[docs/HOOK.md](docs/HOOK.md)**。

界面是 **[Web 控制台](docs/WEBUI.md)**（浏览器打开，推荐）：

```powershell
cd "E:\md\n mod\hd2-coyote"
.\.venv\Scripts\python.exe -m hd2coyote web      # → http://127.0.0.1:8787/
```

在网页里就能：看实时状态与输出强度、发测试脉冲、调每类事件的强度、
写游戏内桥的档位与偏移、**看 addon 的 STATUS / 日志 / recon 报告**（诊断区还会提示
"配置改了但没重启游戏"这种最容易踩的情况）。

**关闭方式**（三条任选）：网页右上角 **「关闭程序」** / 双击 **`stop.bat`** /
`python -m hd2coyote stop` —— 都会先把输出归零、清空波形、断开设备，再退出程序。
（顶部「急停」只是静音，需要点「重新武装」才恢复输出，它不会退出程序。）

> ⚠️ **请先读 [SAFETY.md](SAFETY.md)。** 这是控制贴在人体上的电刺激设备的软件，
> 默认强度上限是保守值，第一次使用请务必用 `mock` 模式或最低强度试。
> 电极**严禁跨胸、颈部、头部**。

---

## 它是怎么工作的

默认走**游戏内 Hook**（不是画面识别）：

```
┌───────────────── 《绝地潜兵 2》进程 ─────────────────┐
│  Bingus Shared Loader（社区加载器）                   │
│    └─ mods/hd2coyote/hd2_coyote_bridge（本项目 addon）│
│         只读 FFI：本地玩家锚点 → hp / 肢体掩码 / 阵亡 │
└───────────────────────┬──────────────────────────────┘
                        │ UDP 127.0.0.1:47777（10Hz 状态流）
┌───────────────────────▼──────────────────────────────┐
│  hd2-coyote 控制器（本仓库，Python）                  │
│   hook.py 收状态 → 复用同一套健康/损伤/阵亡状态机      │
│   rules.py 事件→强度/波形/通道                        │
│   safety.py 硬上限 / 急停 / 会话预算（唯一出口）       │
│   device/dglab_socket.py  WebSocket 服务端            │
└───────────────────────┬──────────────────────────────┘
                        │ ws://<本机IP>:9999/<clientId>  ← 手机 App 扫码
┌───────────────────────▼──────┐      蓝牙      ┌──────────────────┐
│ DG-LAB 手机 App              │ ────────────► │ 郊狼 Coyote 3.0  │
└──────────────────────────────┘               └──────────────────┘
```

配置里 `source` 切换来源：`"hook"`（默认）或 `"vision"`（屏幕识别，备用方案，需要标定 HUD）。

### 事件源可以换（0.5.0）

「游戏内桥」只是**默认**的事件源。`config.json` 的 `sources.enabled` 里能同时启用多个源，
事件合流后走**同一套**规则层与安全层 —— 换源不会绕过任何上限：

| 源 | 用途 |
|---|---|
| `game_bridge` | 游戏内 Lua 桥的 UDP 状态流（默认，断流会静音） |
| `http` | 任何程序 / 别的游戏 / 直播工具 `POST` 一个 JSON 就能触发；带令牌与限速 |

网页控制台的「事件源」面板能勾选、看在线状态、还能手动注入一个事件来试规则。
协议、上报示例、以及"自己写一个源"（继承 `Source` + `@register_source`）见
[docs/SOURCES.md](docs/SOURCES.md)。

### 关于「官方支持 mod」这件事

**《绝地潜兵 2》没有官方 mod API、没有 SDK、也没有 Steam Workshop 支持。**
现在整个生态都走社区工具链（Bingus Shared Loader + Nexus/ayakamods 分发）。

所以「hook 游戏」在 HD2 里的真实含义是：用社区加载器把一个 Lua 资源塞进**游戏自带的 LuaJIT**，
addon 与游戏共享地址空间，于是能用 FFI **只读**游戏内存。本项目遵守社区红线：

* ✅ 进程内、只读、不写游戏内存、不改游戏数据、不向外部网络发包（只发 127.0.0.1 的 UDP）
* ❌ 不做外部进程 `OpenProcess` + `ReadProcessMemory`（GameGuard 最容易抓的动作）

反作弊是 **nProtect GameGuard**。社区同类只读 addon（Enemy HP、G-60 Smart Targeting 等）
在公开分发并被广泛使用，但这**不是官方保证**，风险请自行评估 —— 详见 [docs/HOOK.md](docs/HOOK.md)。

> 想完全不碰游戏进程？把 `source` 改成 `"vision"`，走屏幕识别（需要标定一次 HUD）。
> 两条路的取舍见 [docs/HOOK.md](docs/HOOK.md) 第 2 节。

---

## 快速开始

```powershell
# 1. 依赖（建议用虚拟环境）
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 2. 环境自检（会顺手检查：抓屏后端、UDP 端口、社区加载器与已发现的 addon）
.\.venv\Scripts\python.exe -m hd2coyote doctor

# 3. 打包游戏内桥（产出 build/HD2-Coyote-Bridge-<VERSION>.zip，版本号取自仓库根的 VERSION 文件）
.\.venv\Scripts\python.exe tools\build_addon.py

# 4. 启动图形界面
.\.venv\Scripts\python.exe -m hd2coyote ui
```

也可以双击 `run.bat`（自动建虚拟环境并装依赖）。

### 接着做四件事

1. **装桥**：把第 3 步的 ZIP 用 Arsenal / HD2 Mod Manager 导入、启用、Deploy，然后重启游戏。
   启动后确认加载器日志里出现 `mods/hd2coyote/hd2_coyote_bridge: loaded`。
2. **跑一次 recon**（只需要一次）：addon 默认 `mode = "recon"`，进一局任务随便 ping 一下，
   然后看 `%LOCALAPPDATA%\hd2coyote\recon_report.txt` 与 `hd2_coyote_status.txt`。
   **血量字段的偏移必须这样确定** —— 见 [docs/HOOK.md](docs/HOOK.md) 第 5 节。
3. **进游戏调设置**：`Esc` → **MODS** 页 → **HD2 COYOTE 郊狼** —— 先跑一次「侦察 Recon」拿到
   `recon_report.txt`，再在同一个页面里把血量偏移填进去、切到「运行 Live」。
   **不用改文件、不用重启游戏**（详见 [docs/HOOK.md](docs/HOOK.md) 第 6 节）。
4. **连手机**：界面点「启动连接」，DG-LAB App 扫二维码；先点「测试脉冲 5%」确认体感再调滑块。

> 只想先跑通链路？`python -m hd2coyote simulate` 用合成画面走完整流程，
> `python tools/fake_addon.py` 用假 addon 喂 UDP 状态流。都不需要游戏和硬件。

---

## 事件与默认强度

| 事件 | 触发条件 | 默认强度 | 波形 | 通道 | 默认冷却 |
|---|---|---|---|---|---|
| 受伤 | 血条一次掉 ≥4%（小掉血会累计） | 16% + 每掉 10% 血 +7% | `pinch` 短按捏 400ms | A | 0.4s |
| 肢体损伤 | 损伤图标条出现橙红色（按槽位分左肢/躯干/右肢，流血 ×1.2） | 30% | `ramp_up` 渐强 1.2s | A+B | 1.5s |
| 阵亡 | 血条清空持续 0.8s，或阵亡模板匹配 | 45% | `death` 三段递增 4s | A+B | 5s（期间每 1.5s 重复） |
| 低血量 | 血量 <35%（默认**关闭**） | 12% | `heartbeat` 心跳 | A+B | — |

另外：手机 App 上的 **1 号反馈按钮** = 急停/恢复，可以直接用手机喊停。

### 强度是怎么算的（很重要）

规则里的百分比不是直接下发给硬件的数值，而是「相对手机 App 通道上限的比例」：

```
最终百分比 = min(规则百分比 × 总倍率, max_pct)          # safety.py
实际强度   = min(通道上限 × 最终百分比, max_absolute)    # App 单位 0~200
```

* `总倍率`（默认 1.0）：一键把所有反馈整体加减档。
* `max_pct`（默认 30%）：单次输出的百分比天花板。
* `max_absolute`（默认 40）：**绝对天花板**，直接限制下发给设备的值。
  想更刺激再往上抬，但请一格一格加。

界面上这三项和每个事件的强度都是**实时生效**的滑块，不用重启。

---

## 波形

郊狼 3.0 的波形单元 = 16 位十六进制 = 8 字节 = 100ms 输出 = 4×25ms 子脉冲：

```
0A 0A 0A 0A | 64 64 64 64
└─ 频率 ────┘ └─ 强度% ──┘
  10~240Hz      0~100（任一 >100 会让这 100ms 整段静音）
```

内置预设（`waves.py`）：`pinch`（按捏）、`sting`（刺痛）、`buzz`（低频连续）、
`ramp_up`（渐强推力）、`breath`（呼吸）、`heartbeat`（心跳）、`death`（三段递增）。

### 波形库（0.5.0）

除了内置预设，还能在 `config.json` 的 `waves.entries` 里存**命名波形**，然后让规则的
`wave` 字段直接写这个名字（库里没有才退回内置预设）：

```json
"waves": { "entries": {
  "死亡长按": { "units": ["1414141464646464"], "default_ms": 2000, "note": "参考项目死亡波形" }
}}
```

* 一个字串 = 一个单元 = **100ms**（4×25ms 子脉冲），不够长就循环；
* 也可以只写 `"preset": "death"` + `freq`/`peak`，等于给内置预设起个别名；
* 网页控制台的**波形库**面板能编辑、试打、删除，并与参考项目
  [DG-Lab-Punishment](https://github.com/YingXIAmour/DG-Lab-Punishment) 那种
  `{"pulse_data": {"死亡": [...]}, "punish_time": {...}}` JSON **互导**（`pulse_data` 里的
  秒数会换算成 `default_ms`）。

### 惩罚累积（0.5.0）

`config.json` 的 `ramp` 段打开后，在规则基础强度之上再叠一层"越来越疼"的加成：

```json
"ramp": { "enabled": true, "per_event": 2.0, "hp_missing_pct": 20.0, "ceiling_pct": 25.0,
          "decay_after_s": 4.0, "decay_per_s": 1.5, "reset_on_death": true,
          "apply_to": ["damage", "limb_injury"] }
```

每次命中 `per_event`、血量缺失按 `hp_missing_pct` 换算、总加成封顶 `ceiling_pct`，
超过 `decay_after_s` 没新事件就按 `decay_per_s` 回落，`reset_on_death` 决定阵亡/复活是否清零。
**它只是加在规则百分比上的一层**，最终照样被 `max_pct` / `max_absolute` 夹住。

---

## 命令行

```powershell
python -m hd2coyote ui                  # 图形界面（推荐）
python -m hd2coyote run                 # 纯命令行运行（Ctrl+C 退出）
python -m hd2coyote run --mock          # 不接硬件，只打印输出
python -m hd2coyote simulate            # 合成画面自测整条链路（视觉路线）
python -m hd2coyote test-pulse --pct 5 --ms 700
python -m hd2coyote doctor              # 环境自检：抓屏/端口/加载器/已发现 addon
python -m hd2coyote waves
python -m hd2coyote update --check      # 查 GitHub 有没有新版本（只提示 + 给链接，不自动安装）

# 游戏内桥
python tools\build_addon.py             # 打包成管理器可导入的 ZIP
python tools\build_addon.py --lua-mods-dir "E:\SteamLibrary\steamapps\common\Helldivers 2\data"
                                        # （可选）直接按手工安装路线部署 patch
python tools\fake_addon.py              # 假装游戏内桥在发 UDP（联调）
python tools\fake_app.py --url ws://127.0.0.1:9999/<clientId>   # 假装手机 App
```

没有手机也想联调协议？用自带的假 App（见上）。没有游戏想验 hook 链路？用 `fake_addon.py`。

---

## 目录结构

```
hd2coyote/
  config.py       配置模型（config.json）
  hook.py         游戏内桥的 UDP 数据源（默认）—— game_bridge 源的内核
  sources/        事件源插件：base.py 注册表 + game_bridge.py + http.py
  capture.py      抓屏（dxcam > mss > Pillow）—— vision 路线用
  hud.py          血条自动定位、区域工具 —— vision 路线用
  detectors.py    健康/损伤/阵亡状态机（两条数据源共用）
  events.py       事件定义
  rules.py        事件 → 动作（强度、波形、冷却、通道）
  waves.py        郊狼波形生成与预设
  wave_lib.py     命名波形库（自定义单元 / 预设别名 / 与 pulse_data JSON 互导）
  ramp.py         惩罚累积（受伤越多、血量越少 → 加成越高，会回落）
  update_check.py 查 GitHub Releases（只提示 + 给链接，绝不自动安装）
  safety.py       硬上限、急停热键、会话预算
  engine.py       调度中枢（事件源 / vision 两种循环）
  device/
    base.py           设备接口
    dglab_socket.py   DG-LAB Socket 协议（WebSocket 服务端 + 二维码）
    mock.py           干跑设备
  simulate.py     合成 HUD 画面 / 演示脚本
  webui.py        Web 控制台（单页 + JSON API，零外部依赖）
  ui.py           Tkinter 界面 + 标定向导
  __main__.py     命令行入口
lua/hd2_coyote_bridge.lua   游戏内只读 Lua 桥（Bingus Shared Loader addon）
tools/hd2_patch.py          .patch_N 归档读写（资源名哈希对照过官方参考值）
tools/build_addon.py        打包成管理器可导入的 ZIP
tools/fake_addon.py         假装游戏内桥在发 UDP
tools/fake_app.py           假装 DG-LAB 手机 App
docs/HOOK.md                Hook 路线：原理、已验证项、recon 步骤、排查表
docs/WEBUI.md               Web 控制台：面板、接口、关闭与安全
docs/SOURCES.md             事件源：内置源、HTTP 协议、自己写一个源
tests/                      283 个单测（含 LuaJIT 侧 addon 仿真与跨语言联调）
```

---

## 测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -t . -s tests -v
```

覆盖五层：

1. **Python 逻辑**：报文格式、波形合法性、强度夹取、急停、会话预算、配置往返、界面冒烟。
2. **Hook 数据源**：UDP 状态流 → 事件 → 引擎输出；超时断流静音；坏包不影响正常包。
3. **事件源与扩展（0.5.0）**：源注册表与目录、HTTP 源的令牌/限速/队列上限、状态机断流、
   多源一起跑 + 关键源掉线静音、波形库优先于内置预设、惩罚累积的叠加/封顶/回落、
   更新检查在 404/403/网络失败下也**只返回错误、不抛异常**。
4. **Lua 桥（用真 LuaJIT + 假 FFI/假内存）**：LuaJIT 语法闸门、指针不被 32 位截断、
   本地玩家定位、状态读取、recon 落盘、live 帧钩子、缺偏移拒绝启动、
   **跨语言联调**（LuaJIT 发 UDP → Python 收 → 产出 Damage）。
5. **打包**：`.patch_N` 往返与布局不变量、manifest 校验（纯 ASCII / 非法字符）、ZIP 结构。

> 有一个特别值得说的回归：**LuaJIT 的 `tonumber(hex,16)` 只按 32 位解析**，
> `0x141000000` 会被截成 `0x41000000` —— 指针一律改成逐字符解析，
> 测试 `test_pointer_above_32bit_not_truncated` 守着这条。这类 bug 只有用游戏同款
> LuaJIT 做离线仿真才抓得到，用 CPython 或 Lua 5.4 跑测试会一路绿到实机才炸。

---

## 与 DG-Lab-Game-Controller 的关系

[LYQBING/DG-Lab-Game-Controller](https://github.com/LYQBING/DG-Lab-Game-Controller)
是一个「控制器框架 + 第三方检测插件」的架构：插件只负责产出惩罚事件，控制器负责连设备。

本项目把「检测 + 控制器」做在了一起（不依赖 .NET 环境，Windows 上装个 Python 就能跑），
但保留了同样的分层：如果你要接进那套框架，只需要把 `engine.inject(event)`
（`hd2coyote/engine.py`）当成事件出口即可 —— 事件定义在 `events.py`，
一一对应「受伤 / 肢体损伤 / 阵亡」。

后来参考 [YingXIAmour/DG-Lab-Punishment](https://github.com/YingXIAmour/DG-Lab-Punishment)
（Apache-2.0，郊狼惩罚姬）补了四件事：**事件源插件化**（它的 `plugins/` + `module_manager`）、
**命名波形库与网页波形编辑器**（它的 `pulse_data` + `pulse_wave_gui`，格式可互导）、
**惩罚累积**（它的"血越少越强"惩罚模型）、**检查更新**。
唯一明确没学的是它的 `update.py`：那种「杀主程序 + 解压覆盖安装目录」的自动更新一旦中断
就会把安装目录搞坏，本项目只做**提示 + 给链接**。

---

## 常见问题

| 现象 | 处理 |
|---|---|
| 加载器日志里没有 `mods/hd2coyote/…: loaded` | 用管理器重装 ZIP；`-- HD2-Addon:` 首行不能被改（打包脚本会校验）；别和别的 mod 抢同一个 `.patch_N` 编号 |
| 界面一直「等待游戏内 Lua 桥上报」 | 端口被占用或 addon 没加载：`python -m hd2coyote doctor` 看 Hook 端口与加载器日志 |
| `STATUS.txt` 写 `FAILED - …` | 打开 `%LOCALAPPDATA%\hd2coyote\hd2_coyote_bridge.log` 看原因；recon 未找到玩家就进局内重跑 |
| recon 找不到本地玩家 | 要在**局内**（飞船里 ping actor 可能没建立）；或该构建的 `ping_actors` 偏移已漂移 |
| 有状态但事件不触发 | 血量是**绝对值**口径（`hp` + `hp_max`）；确认 `hp_max` 读对、`limb_mask_bits` 与游戏位定义一致 |
| 手机扫码连不上 | 手机与电脑同一局域网；Windows 防火墙放行 9999 端口；换 `advertise_ip` 指定 IP |
| 检测到受伤但没感觉 | 手机 App 的通道强度上限太低；`max_absolute` 太小；波形强度是相对值，先把通道强度调上去 |
| 想更灵敏/更迟钝 | 改 `config.json` 里 `detect` 段的阈值：`damage_min_pct`、`dead_hold_s`、`injury_min_fraction` |
| 走 vision 时抓到全黑画面 | 改「无边框窗口」；确认抓屏的是游戏所在那块屏；HDR 建议关掉 |
| 走 vision 时血量一直是 0/100 | 血条框选不准，重新标定；改分辨率或 HUD 缩放后必须重标 |

---

## 可扩展方向

* 更多事件：战略配备就绪、增援用尽、任务失败、被追踪者锁定……（桥已经有一个位置明确的状态协议，
  加字段即可；Python 侧加一个 Tracker 就行）
* 更细的部位映射：把肢体掩码的每一位映射到不同通道 / 不同波形。
* 更多设备：`device/` 下新增实现即可（例如直接蓝牙连郊狼 V2/V3）。
* 桥的其他出口：现在只发 127.0.0.1 UDP；也可以改成命名管道或文件 tail（都不需要更多依赖）。

# hd2 × DG-LAB

**《绝地潜兵 2》× 郊狼 DG-LAB：把受伤、肢体损伤与阵亡事件转换为设备反馈。**

面向 Windows 的控制器，带可双击运行的 **Fluent 桌面 EXE**，同时保留本地 Web 控制台：查看游戏状态、调整规则与强度、管理波形，并通过 DG-LAB 手机 App 连接郊狼 Coyote 3.0。
仓库名为 **hd2-DG-LAB**，Python 模块与游戏内桥仍使用 `hd2coyote` / `HD2-Coyote-Bridge`，命令中无需改名。

[桌面版使用与打包](docs/DESKTOP.md) · [快速开始](#快速开始) · [连接游戏与设备](#连接游戏与设备) · [常见问题](#常见问题) · [隐私与发布检查](#隐私与发布检查) · [更新记录](CHANGELOG.md) · [反馈问题](https://github.com/cion771/hd2-DG-LAB/issues)

> [!WARNING]
> 本项目控制人体接触式电刺激设备。使用前请完整阅读 [安全须知](SAFETY.md) 和设备说明书，**先用 mock 自测，再考虑连接真机**。
> 禁止将电极放在头部、颈部或跨胸位置；有体内电子植入物等禁忌情况不得使用。软件限幅不能保证人体安全。
> 游戏内桥属于实验性社区 mod，**只读不等于无风险**，不保证反作弊兼容、账号安全或游戏更新后的稳定性。

## 功能一览

| 功能 | 说明 |
| --- | --- |
| 游戏事件反馈 | 受伤、肢体损伤、阵亡；低血量反馈默认关闭 |
| Fluent 桌面 / Web | 浅色与深色界面，状态卡片、规则调节、测试脉冲、桥配置与诊断 |
| 结构化状态来源 | 游戏内 Lua 桥与可选 HTTP 事件源；不抓屏、不做 OCR 或像素识别 |
| 可扩展事件源 | 游戏桥 UDP 与 HTTP 事件源可合流，统一经过规则与安全层 |
| 波形库 | 内置预设、命名波形、网页编辑，以及 `pulse_data` JSON 导入/导出 |
| 惩罚累积 | 按事件与缺失血量增加反馈，支持封顶和回落；默认关闭 |
| 输出保护 | 百分比/绝对强度双重上限、急停、关键状态源断流静音；会话预算需自行开启 |
| 检查更新 | 查询 GitHub Releases，只提示版本和链接，不自动下载安装 |

### 工作原理

```text
《绝地潜兵 2》+ 社区加载器 + Lua 桥
                  │ 本机 UDP 状态流（127.0.0.1:47777）
                  ▼
          Python 控制器 ← HTTP 事件源（可选）
                  │ 事件判定 → 规则/波形 → 安全限幅
                  ▼
          WebSocket 服务（端口 9999）
                  │ 手机 App 扫码，电脑与手机在同一局域网
                  ▼
          DG-LAB App ── 蓝牙 ── 郊狼 Coyote 3.0
```

已移除旧截图识别与标定管线；没有游戏时可用 Mock 和合成状态进行自测。
Web 控制台负责操作控制器，**不是**手机扫码连接的 WebSocket 服务。

## 使用前准备

- **桌面 EXE**：Windows 10/11 x64 + Microsoft Edge WebView2 Runtime；不需要 Python。构建与启动见 [桌面文档](docs/DESKTOP.md)。
- **源码 / Web**：Windows + Python 3.10 或更高版本，安装 Python 时勾选加入 PATH。
- 安装依赖时需要联网；使用 Git 克隆则还需要 Git，也可下载仓库 ZIP。
- 真机反馈需要 **郊狼 Coyote 3.0、DG-LAB 手机 App**，以及手机与电脑互通的可信局域网。
- Hook 路线需要《绝地潜兵 2》、[Bingus Shared Loader](https://github.com/CowboyBingus/Helldivers2ModLoader) 及兼容的 mod 管理器。
- [Mod Options Menu](https://github.com/CowboyBingus/ModOptionsMenu) 是可选的游戏内连接信息与日志菜单，不是 Web 控制台的前置依赖。

> 没有游戏或设备也可以完成 mock 自测。**安装成功、离线测试通过，不代表当前游戏构建的字段偏移已经适配。**

## 快速开始

从 [Releases](https://github.com/cion771/hd2-DG-LAB/releases) 下载 EXE 后可直接双击运行，先在「设备连接」选择 Mock。桌面版默认使用独立配置、保持解除武装，不会自动开始检测。详见 [桌面文档](docs/DESKTOP.md)。

以下是源码 / Web 路线，命令在 **PowerShell** 中运行。已有源码可从第 2 步开始。

### 1. 获取源码

```powershell
git clone https://github.com/cion771/hd2-DG-LAB.git
cd hd2-DG-LAB
```

不使用 Git：在仓库页面选择 **Code → Download ZIP**，完整解压，在含有 [requirements.txt](requirements.txt) 的目录打开终端。

### 2. 安装依赖

```powershell
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.txt
```

下面直接调用虚拟环境的 Python，**无需激活环境，也无需修改 PowerShell 执行策略**。
首次运行时，如果本地配置文件 `config.json` 不存在，程序会生成默认配置；已有配置不会被上述安装命令覆盖。
公开配置示例见 [config.example.json](config.example.json)。

### 3. 先做无硬件自测

```powershell
./.venv/Scripts/python.exe -m hd2coyote simulate --seconds 15
```

这个命令默认使用 **mock 设备 + 合成状态**，不需要游戏、手机或郊狼。
观察终端中的事件与模拟输出，约 15 秒后结束。**不要加 `--socket`**，该参数会切换为真实设备连接。
这一步验证的是模拟链路，不验证游戏内桥的真实偏移。

### 4. 打开控制台

```powershell
./.venv/Scripts/python.exe -m hd2coyote web
```

浏览器会打开 **<http://127.0.0.1:8787/>**。若没有自动打开，手动访问即可。
双击 [run.bat](run.bat) 则启动 **桌面版**（优先本地 EXE，否则安装桌面依赖并从源码启动）。Web 与桌面使用不同默认配置目录，不要同时运行它们连接设备。

首次先查看页面，不要急着启动设备、开始检测或发送测试脉冲。
需要继续无硬件联调时，在「设备连接」将设备类型选择为 **Mock** 并保存；保存会停止当前连接与检测。
真机使用前再改回 `"socket"`，并先检查安全上限。

## 连接游戏与设备

### 1. 选择状态来源

| 路线 | 适用场景 | 必须注意 |
| --- | --- | --- |
| `game_bridge`（默认） | 通过游戏内 Lua 桥读取血量、损伤与阵亡状态 | 依赖社区加载器和构建对应的偏移；存在崩溃、卡死或反作弊风险 |

也可启用 `http` 事件源接收外部结构化状态/事件，或与游戏桥合流。详见 [事件源文档](docs/SOURCES.md)。

0.5.1 起移除截图识别与标定。旧 `source: "vision"` 配置读取时迁移为事件源模式，忽略截图区域和模板路径，保留设备、规则、安全设置及 `sources.enabled`；不会自动开始检测或武装。

### 2. Hook 路线：安装并验证游戏内桥

**先保持 mock 模式或不连接真机**，不要用人体反馈判断偏移是否正确。

```powershell
./.venv/Scripts/python.exe -m hd2coyote doctor
./.venv/Scripts/python.exe tools/build_addon.py
```

1. 打包产物为 `build/HD2-Coyote-Bridge-<VERSION>.zip`，版本号来自 [VERSION](VERSION)。将 ZIP 用 Arsenal / HD2 Mod Manager 导入、启用、Deploy，然后重启游戏。
2. **桥默认是 `safe`，不是 `recon` 或 `live`**：只写日志，不读取游戏内存、不发 UDP、不注册菜单。先确认 addon 加载成功。
3. 按 [Hook 文档第 9 节](docs/HOOK.md#9-模式阶梯与两起真实事故必读) 逐级验证：`safe → net → menu → recon → live`。`net` 检查通信，`menu` 检查菜单，`recon` 收集字段定位证据。
4. 在任务内进行 recon，结合真实血量变化核对 `hp` / `hp_max` 等字段，再填写偏移；**不要照抄示例偏移**。游戏更新后需要重新确认，不能假定只做一次就永久有效。
5. 确认状态读数与实际一致后才切 `live`。在 Web 控制台点击「开始检测」，检查受伤、阵亡与复活是否正确；真机连接留到下一步。

**配置统一在桌面 / Web 控制台或桥配置文件中修改，修改后需重启游戏。** 游戏内 MODS 菜单只显示连接信息和最近日志，不再提供配置控制项；重新打开 ESC 刷新信息。
诊断区可以查看实际运行档位、状态与 recon 报告。详细步骤见 [Hook 文档](docs/HOOK.md)。

### 3. 连接手机与郊狼

1. 在 DG-LAB App 中通过蓝牙连接设备，按设备说明确认使用条件与禁忌。电脑和手机连接同一可信局域网。
2. 将控制器设备类型设为 `socket` 并重启；先把控制器上限与 App 通道上限降至低档，确认急停方式可用。
3. 在「设备连接」点击 **「启动设备服务」**，用 App 扫码，等待连接状态确认。
4. 完成无硬件验证后，先开始检测、确认重新武装，才考虑短时、低强度测试脉冲；从最低可感知水平谨慎调整。**5% 不是对所有人的安全保证。**
5. 点击「开始检测」，核对游戏事件与反馈。异常时立即急停，必要时直接关闭设备。

### 网络与端口

| 用途 | 默认地址/端口 | 访问范围 |
| --- | --- | --- |
| Web 控制台 | `http://127.0.0.1:8787/` | 仅电脑本机；可控制实际输出 |
| 游戏桥状态 | UDP `127.0.0.1:47777` | 游戏与控制器之间的本机通信 |
| 手机扫码连接 | WebSocket `9999`，默认监听 `0.0.0.0` | 手机访问电脑的局域网地址；不是 `127.0.0.1` |
| HTTP 事件源（可选） | `http://127.0.0.1:47778/` | 默认本机，启用该源后使用 |

只对可信专用网络放行必要的手机连接端口。不要关闭整个防火墙，也不要将这些接口做公网映射。
`0.0.0.0` 表示监听所有网卡，不是扫码地址；多网卡时可在本地配置的 `device.advertise_ip` 指定手机可达的电脑局域网 IP。

### 急停与退出

- **急停**：网页「急停」或默认热键 **F12**，静音并解除武装；确认问题排除后再点「重新武装」。App 的 **1 号反馈按钮**也可切换急停/恢复，避免误触恢复。
- **桌面退出**：窗口 × / Alt+F4 或页面「关闭程序」会尝试停止本实例与设备；在 App 核对输出已停止。
- **Web 正常退出**：优先用网页「关闭程序」，程序会尝试归零、清空波形并断开设备。**只关浏览器标签页不会停止后台控制器。**
- **Web 命令退出**（不针对随机端口的桌面实例）：双击 [stop.bat](stop.bat)，或执行以下命令：

```powershell
./.venv/Scripts/python.exe -m hd2coyote stop --no-force
```

`--no-force` 只请求正常退出；不带该参数或使用 [stop.bat](stop.bat) 时，失败后可能强制结束进程。
**强杀、断网或程序卡死不能保证归零指令已送达**，请在 App/设备端确认输出已停止；必要时直接关闭设备。

## 事件与强度

下表为默认规则的**请求强度**，不是设备实际输出；所有规则仍受安全上限约束。

| 事件 | 默认请求强度 | 波形 / 时长 | 通道 | 冷却 |
| --- | --- | --- | --- | --- |
| 受伤 | 16% + 每损失 10 个血量百分点增加 7 个强度百分点 | `pinch` / 400ms | A | 0.4s |
| 肢体损伤 | 30%；带流血标记时 ×1.2 | `ramp_up` / 1200ms | A+B | 每槽位 1.5s |
| 阵亡 | 45% | `death` / 4000ms | A+B | 5s |
| 低血量 | 12%，默认关闭 | `heartbeat` / 1200ms | A+B | 无；周期 1.5s |

默认掉血判定阈值为 4 个血量百分点，低血量阈值为 35%。共享状态机使用桥接/HTTP 上报的血量与损伤状态，不读取游戏画面。
阵亡规则另有 `repeat_ms = 1500` 的重复脉冲配置。具体参数见 [配置示例](config.example.json) 与 [配置模型](hd2coyote/config.py)。

### 双重限幅

```text
最终百分比 = min(请求百分比 × 总倍率, max_pct)
设备强度   = min(round(App 通道上限 × 最终百分比 / 100), App 通道上限, max_absolute)
```

默认总倍率为 `1.0`，`max_pct = 30`，`max_absolute = 40`（App 强度单位）；解除武装时输出为 0。
例如阵亡请求 45%，在默认百分比上限下最多按 30% 换算；若 App 通道上限为 100，换算结果为 30，而不是 45。这只是算法示例，不是推荐人体使用档位。

规则强度与安全上限可在页面调整；会话预算 `safety.max_session_seconds` 默认 `0`（未启用），焦点丢失静音默认关闭，需要时请自行开启。

## 扩展功能

- **事件源**：默认启用 `game_bridge`；在控制台可启用 `http`，接收其他程序的 JSON 事件或状态。HTTP 支持限速，**令牌仅在 `sources.http_token` 非空时启用**，默认不能当作已鉴权服务。协议与扩展接口见 [事件源文档](docs/SOURCES.md)。
- **波形库**：内置 `pinch`、`sting`、`buzz`、`ramp_up`、`breath`、`heartbeat`、`death`；支持自定义 16 个十六进制字符的单元（每单元 100ms）、预设别名，以及与参考项目 `pulse_data` JSON 的互导。规则优先查找命名波形库，再使用内置预设。
- **惩罚累积**：默认关闭；启用后按事件和缺失血量增加请求强度，再按时间回落，仍受双重限幅。配置项位于 `ramp`。
- **检查更新**：手动查询 GitHub Releases，不覆盖本地文件。若尚未发布 Release，查询不到版本不代表安装失败。

界面面板与 API 见 [Web 控制台文档](docs/WEBUI.md)。

## 常用命令

以下命令均在项目根目录执行，不会因为仓库改名而改变模块名。

```powershell
./.venv/Scripts/python.exe -m hd2coyote desktop              # Fluent 桌面（需桌面依赖）
./.venv/Scripts/python.exe -m hd2coyote web                  # Web 控制台
./.venv/Scripts/python.exe -m hd2coyote ui                   # Tkinter 桌面界面
./.venv/Scripts/python.exe -m hd2coyote doctor               # 环境与桥诊断
./.venv/Scripts/python.exe -m hd2coyote run --mock           # mock 接收事件，不连硬件
./.venv/Scripts/python.exe -m hd2coyote simulate --seconds 15 # 合成画面自测
./.venv/Scripts/python.exe -m hd2coyote waves                # 查看内置波形
./.venv/Scripts/python.exe -m hd2coyote stop --no-force      # 正常关闭 Web 控制台
./.venv/Scripts/python.exe -m hd2coyote update --check --repo cion771/hd2-DG-LAB
```

Hook 无游戏联调：在一个终端运行 `run --mock`，另一个终端运行以下命令；测试时不要同时运行真实游戏桥，以免状态混入。

```powershell
./.venv/Scripts/python.exe tools/fake_addon.py
```

更多参数可执行 `python -m hd2coyote --help`。真实设备测试请先完成安全检查，不要同时启动多个控制器占用同一端口。

## 常见问题

| 现象 | 排查方法 |
| --- | --- |
| 双击后提示缺少 Python 或模块 | 确认 Python 3.10+ 已加入 PATH；若复用了旧虚拟环境，按快速开始重新安装依赖 |
| 控制台打不开 | 确认程序仍在运行；检查 8787 端口占用，也可用 `web --port 8788`，退出命令需使用同一端口 |
| 等待 Lua 桥上报 | 先看运行档位：`safe` 不发 UDP 是正常行为；运行 `doctor`，确认 addon 加载且双方端口一致 |
| recon 找不到玩家或状态异常 | 在任务内验证；检查当前构建与 profile 是否匹配，不猜偏移，不连接真机继续试 |
| 网页改了桥配置却没生效 | 重启游戏，并在诊断区核对实际运行档位；游戏内菜单只读，不会覆盖文件配置 |
| 游戏崩溃或黑屏 | 先急停并断开设备，再通过 mod 管理器禁用本桥并重新部署；按 Hook 文档的回退流程排查 |
| 手机扫码失败 | 手机与电脑需互通；检查访客 Wi-Fi 隔离、防火墙、VPN/虚拟网卡及 `device.advertise_ip` |
| 有事件但没有输出 | 检查武装状态、设备连接、规则开关、冷却和 App 通道上限；不要直接提高强度排错 |
| 检查更新报错 | 核对仓库设置，可用上面的 `--repo` 命令；网络限制或尚无 Release 不影响本地控制功能 |

无法解决时可提交 [Issue](https://github.com/cion771/hd2-DG-LAB/issues)，附软件版本、Python/游戏版本、所用路线、复现步骤和**脱敏后的最小错误片段**。请勿直接上传完整诊断目录。

## 隐私与发布检查

游戏桥默认只向本机发 UDP；手机连接在局域网内。**这不代表所有功能都离线**：安装依赖会访问包源，检查更新会访问 GitHub。请保持 Web 控制台与 HTTP 事件源默认的本机监听，不要将能触发设备输出的接口暴露到公网。

## 开发与文档

| 入口 | 内容 |
| --- | --- |
| [Hook 文档](docs/HOOK.md) | 桥原理、模式阶梯、偏移验证、诊断与回退 |
| [Fluent 桌面](docs/DESKTOP.md) | EXE、源码启动、独立数据目录与打包 |
| [Web 控制台](docs/WEBUI.md) | 面板、API、配置与关闭行为 |
| [事件源](docs/SOURCES.md) | HTTP 协议、状态包与自定义源 |
| [核心代码](hd2coyote/) | 检测、规则、波形、安全层与设备协议 |
| [Lua 桥](lua/hd2_coyote_bridge.lua) / [工具](tools/) | 游戏内 addon、打包与模拟工具 |
| [测试](tests/) / [更新记录](CHANGELOG.md) | 回归测试与版本变更 |

```powershell
./.venv/Scripts/python.exe -m unittest discover -t . -s tests -v
```

测试涵盖规则、安全限幅、事件源、波形、Web API、UDP 链路与打包；部分 Lua 桥测试需要 LuaJIT 环境。离线测试不替代真实游戏版本适配与设备安全验证。

## 致谢与许可

- [YingXIAmour / DG-Lab-Punishment](https://github.com/YingXIAmour/DG-Lab-Punishment)：参考其易于上手的文档结构，以及事件源、波形管理、累积反馈与更新提示的设计思路。两者是独立项目，安装步骤与端口不同。
- [LYQBING / DG-Lab-Game-Controller](https://github.com/LYQBING/DG-Lab-Game-Controller)：参考事件检测与设备控制分层的思路。
- [DG-LAB 官方开源协议](https://github.com/DG-LAB-OPENSOURCE/DG-LAB-OPENSOURCE)：设备与 Socket 协议资料。
- 社区加载器、菜单和本地玩家定位参考的完整署名见 [第三方说明](THIRD_PARTY.md)。

本项目采用 [MIT License](LICENSE)。与 Arrowhead Game Studios、Sony、DG-LAB 均无关联或官方背书。
软件按现状提供，不承诺适用于所有游戏版本或设备环境；使用者应遵守游戏条款、当地法律与设备说明，并自行评估账号、设备及人身风险。

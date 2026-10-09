# Fluent 桌面版（Windows）

独立 Windows 窗口，内嵌离线 Fluent 界面与原有 Python 控制器。无需单独打开浏览器或终端。
首页信息层次参考 DG-Lab-Punishment 的系统状态、功能页面和日志区域；界面、图标与样式为本项目实现，未复制参考项目背景素材。

## 运行 EXE

1. 使用 Windows 10/11 x64，安装 Microsoft Edge WebView2 Evergreen Runtime（微软官方发行版；不随本程序捆绑）。
2. 构建产物为 `dist/HD2-DG-LAB.exe`，双击即可运行，不需要安装 Python。
3. 首次在「设备连接」把设备类型切为 **Mock** 并保存，先完成无硬件自测。
4. 程序启动时**不启动设备、不开始检测、保持解除武装**。需要测试时按顺序开始检测、确认重新武装，再发送测试脉冲。
5. 同一个 Windows 登录会话仅允许一个桌面实例。不要同时运行 Web/CLI/Tk 控制器连接同一台设备。

这是本地构建的未签名程序，不声称已通过 SmartScreen 或杀毒软件认证。来源不明、校验值不符时不要运行；不要为运行它关闭系统安全保护。
游戏内桥和社区加载器仍须按 [HOOK.md](HOOK.md) 单独安装、验证；EXE 不携带游戏或第三方 mod。

## 配置与隐私

- 默认数据目录：`%LOCALAPPDATA%/hd2-DG-LAB/`。
- `config.json`：桌面专用配置。**不会自动读取或迁移源码目录的旧配置**，首次生成默认值。
- `desktop.log`：本机轮转日志，单文件上限约 2 MB，保留两份备份。
- `webview/`：WebView2 存储路径；浏览器使用 private mode，外观选择不保证跨次保留。
- Lua 桥配置仍使用原有 `%LOCALAPPDATA%/hd2coyote/`；桌面与 Web 面板操作的是同一份桥配置，改完需重启游戏。
- 内嵌控制服务只监听 `127.0.0.1` 的随机可用端口，不占用 Web 模式的固定 8787 端口。
- 界面无 CDN、遥测或自动更新下载；安装依赖、主动点击检查更新会联网。
- 配置、连接二维码、令牌、桥诊断及日志均可能含隐私。不要直接分享完整目录或设备页截图。

可从终端显式选用配置（不要同时运行旧控制器）：

```powershell
./dist/HD2-DG-LAB.exe --config ./my-config.json
./dist/HD2-DG-LAB.exe --data-dir ./desktop-data --bridge-dir ./bridge-config
```

自定义数据目录建议放在仓库外；它不一定被 Git 忽略。打包脚本只按白名单收集前端和程序资源，不带本机配置、连接信息或运行日志。

## 急停、断线与关闭

- 顶栏 **急停**：请求清空当前输出并解除武装，不退出程序。
- **停止检测**：停止引擎与设备连接，并解除武装。
- 修改设备、来源或事件源：停止旧引擎，创建的新引擎保持待机、解除武装；不会自动恢复输出。
- **窗口右上角 × / Alt+F4**：先尝试急停与停止设备，再关闭本实例服务和窗口。
- 页面 **关闭程序**：确认后执行同一关闭流程，原生窗口随后退出。
- 意外断线显示 **输出状态未知** 并尝试重连，不把“连不上”误报为“已经安全关闭”。
- 软件只能请求归零，不能证明真实硬件已归零。强制结束、系统崩溃、断网或设备故障时，请立即在手机 App 停止输出或直接关闭设备。

`stop.bat` 默认管理固定 8787 端口的 Web 服务，**不是桌面实例的默认退出方式**。

## 从源码启动

需要 Python 3.10+、Windows 和 WebView2：

```powershell
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements-desktop.txt
./.venv/Scripts/python.exe -m hd2coyote desktop
```

也可双击根目录的 [run.bat](../run.bat)：若存在本地 EXE 则启动 EXE；否则使用虚拟环境，缺依赖时联网安装后运行桌面版。
修改源码后应重新打包，或直接用上面的 Python 命令，避免启动旧 EXE。

保留 Web 模式作为替代：

```powershell
./.venv/Scripts/python.exe -m hd2coyote web
```

Web 模式仍使用工作目录配置与默认端口 8787。**关闭浏览器标签页不等于关闭 Web 控制器**。

## 构建与验证

在项目根目录执行 [build-desktop.ps1](../build-desktop.ps1)，或手动执行：

```powershell
python -m venv build/desktop-venv
./build/desktop-venv/Scripts/python.exe -m pip install -r requirements-desktop.txt
./build/desktop-venv/Scripts/python.exe -m unittest discover -t . -s tests
./build/desktop-venv/Scripts/python.exe -m PyInstaller --noconfirm --clean desktop.spec
Get-FileHash ./dist/HD2-DG-LAB.exe -Algorithm SHA256
```

产物只有一个 EXE（首次启动会在系统临时目录解包，可能稍慢）。不自动发布 GitHub，不要求管理员权限。
回归覆盖启动解除武装、重配置不恢复输出、同源请求检查、关闭幂等与服务器端口释放、原生窗口关闭/启动失败清理。
这些是 Mock 软件测试，不代替真实硬件安全验证或当前游戏版本适配。

## 常见问题

| 现象 | 处理 |
| --- | --- |
| 无窗口或 WebView2 初始化失败 | 检查微软 WebView2 Runtime 安装，查看本机 desktop.log；可退回 Web 模式 |
| 提示程序已在运行 | 切换到已有桌面窗口，不要启动第二个控制器 |
| 界面与旧配置不一致 | 桌面配置独立；确认使用的配置路径，不要误把源码配置当成桌面配置 |
| 开始检测后无反馈 | 启动默认解除武装；先检查有效游戏数据、设备连接、规则与上限，再决定是否重新武装，不要盲目加大强度 |
| 只有等待数据 | 开始检测不代表已收到数据；桥为 safe 时不发送 UDP，请按模式阶梯验证 |
| 截图要分享 | 只使用 Mock 主页并遮挡诊断路径、二维码、连接地址、日志和令牌 |

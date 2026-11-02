# EAC 与外部 HUD 视觉源

## 结论

该模式通过独立进程抓取普通桌面窗口像素，不接触游戏进程。它降低了与注入式桥的耦合，但不承诺任何反作弊判定结果。

2026-10-21 的现场证据：`EasyAntiCheat_EOS` 运行期间，Bingus Shared Loader v18
正常加载了 20 多个 Lua 模组，`mods/skyeshade/hd2runtime` 也在其中。EAC 并没有拦截
模组加载。当时 `hd2coyote` 缺失的真实原因是游戏进程启动（23:54）早于新补丁部署
（23:59）——patch 资源只在启动时读取，不会热加载。

## 方案边界

现场日志仅证明旧进程中其它模组曾加载、我们的补丁在进程启动后才部署；它不能证明新版桥已在 EAC 下加载，更不能证明某一种运行方式会被反作弊接受。部署新版桥须在下一次完整启动后通过加载日志核实。

## 采用的方案：外部 HUD 视觉源

`vision` 事件源完全运行在游戏进程之外：

| 行为 | 是否执行 |
| --- | --- |
| `EnumWindows` / `GetClientRect` 只读窗口几何 | 是 |
| 抓取游戏窗口客户区像素（Python: `mss`/`ImageGrab`；Rust: GDI `BitBlt`） | 是 |
| `OpenProcess` / `ReadProcessMemory` | 否 |
| 写入游戏内存、加载 DLL 进游戏、注入 Lua | 否 |
| 修改游戏文件或绕过反作弊 | 否 |

它只把屏幕像素换算成血量比例，再交给与游戏桥**完全相同**的
`EventTrackers` → 规则 → 安全限幅链路。所以多接一个源不会绕过任何上限。

### 安全耦合

- `vision` 声明为 `critical = True`：读数超时（默认 1.5s）即视为状态不可信，
  引擎会立即清队列并向设备发送归零。信号恢复的首帧只建立血量基线。
- 游戏不在前台、窗口最小化、或是受保护内容黑屏时，源不发布读数，
  旧读数自然过期 → 自动静音。
- 只截取游戏窗口客户区，不会把任务栏或其它窗口误判成血条。

## 启用步骤

1. Python 桌面 EXE 已内置视觉依赖；从源码运行时安装 `requirements.txt`：

   ```powershell
   ./.venv/Scripts/python.exe -m pip install -r requirements-desktop.txt
   ```

   Rust 客户端不需要额外依赖：`hd2-source-vision` 直接链接 `user32`/`gdi32`。

2. 进入**任务中**并保持 HUD 可见。Python 桌面版“事件源”页点击“校准血条”，
   5 秒内切回游戏；程序会停止检测并保持静音。切回桌面后先查看候选血条
   预览与 HP 读数，确认无误再保存。游戏须位于前台，不能在飞船/菜单。

   从源码也可运行 `tools/vision_calibrate.py`；成功时打印相对游戏窗口客户区的
   `hp_bar` 坐标并保存标记图。

3. 在 `config.json` 中启用：

   ```json
   {
     "sources": { "enabled": ["vision"] },
     "vision": {
       "fps": 10,
       "auto_locate": false,
       "hp_bar": { "x": 0, "y": 0, "w": 320, "h": 10 }
     }
   }
   ```

   上述 `x/y/w/h` 是占位示例，必须用校准输出替换。默认关闭自动定位；手动启用 `auto_locate` 仍需核对候选位置。

4. 在“事件源”页**只勾选 vision**，取消 game_bridge，然后保存并手动开始检测。状态卡应显示实时 HP；如未校准或游戏不在前台，输出保持静音。

   Rust 客户端（`crates/hd2-source-vision`，GDI 实现，无额外依赖）的对应操作是：
   - 把 `sources.enabled` 写成 `["vision"]` 后保存，页面点“开始检测”，视觉源随检测一起启动；
   - 校准：游戏在任务画面且前台时 `POST /api/vision/calibrate {"delay_s":5}`（页面上的“校准血条”按钮同款），返回 `box`、`hp_pct` 和 `preview` 预览图；确认无误后前端写回 `vision.hp_bar`；
   - 观察：`GET /api/vision`，`alive=false` 时 `note` 会说明原因（窗口没找到 / 不在前台 / 黑帧 / 未定位到血条）。

5. 旧游戏桥补丁 `9ba626afa44a3aa3.patch_0` 已从当前游戏目录撤下并备份至本地 `build/rollback-bridge-0.6.3/`。若模组管理器重新 Deploy，会再次写入补丁；需要在管理器中禁用本项目桥。其他模组未改动。

## 局限

- 只读取**血量**与由此推导的伤害、低血量、阵亡；肢体伤势需要 HUD 图标区域，
  当前未实现，因此不会伪造断肢事件。
- 需要窗口化/无边框模式；独占全屏下桌面复制可能拿到黑帧，此时源会明确报错。窗口标题必须与内置游戏标题或自定义 `safety.game_window_title` 完整匹配。
- Rust 与 Python 的判定阈值一致，但抓屏实现不同（GDI 与 mss），同一帧的读数可能在最后一位小数上不同；两者都过同一个 250ms 合并窗口。
- 视觉读数天然有噪声，伤害合并窗口默认 250ms 用于抑制同一击被拆成多次。
- 仍需人工核对读数与游戏内血条一致，再考虑连接真机。

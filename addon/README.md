# addon/ —— 游戏内桥（MDL3 版）

Maps 1:1 to the in-game mod. 前置：**MDL3 3.0.0-dev.x**（Lua mod 加载器）。
旧版基于 Bingus Shared Loader 的单文件实现留在 `../lua/hd2_coyote_bridge.lua`，仅作回滚。

## 两种入口

| 文件 | 契约 | 什么时候用 |
|---|---|---|
| `mod.lua` | MDL3 原生：`local ctx = ...`，返回 `{name, version, options, on_enable, on_update, on_disable, on_setting}` | 本地 mod（`%LOCALAPPDATA%\MDL\Helldivers2\Mods\hd2coyote\`） |
| `entry.lua` | HD2-Addon：首行 `-- HD2-Addon: mods/hd2coyote/hd2_coyote`，包装 `_G.update` | Arsenal / HD2MM 导入的 addon 包 |

两个入口装载的是**同一套模块**，区别只在"每帧从哪来""有没有选项面板"。

## 模块

```
bridge.lua            装配：配置 → 日志 → 内存/网络 → 数据源 → 采样；模式阶梯在这里
core/load.lua         装载器：读文件（本地 mod）或 require 资源（addon 包），两条路同源
core/compat.lua       宿主探测：ctx / ffi / module / settings，结果缓存
core/mem.lua          只读内存：ctx.read 优先 → ffi 兜底 → 无；ffi 路径先 VirtualQuery 验址
core/net.lua          出口：UDP（ffi）或文件广播；报文编码只在 encode 一处
core/config.lua       分层配置：默认 ← bridge_config.lua/.txt ← MDL3 选项；钳制与校验
core/log.lua          有界日志 + status 文件 + ctx.log
core/clock.lua        now_ms / dt
core/json.lua         最小 JSON 编码器（线缆协议够用）
sources/runtime.lua   HD2Runtime 公开只读 API（可选）
sources/direct.lua    game.dll 偏移只读源（PE + 7 处 pin 校验通过才读）
sources/mock.lua      合成数据源（不开游戏验链路）
state/sampler.lua     hello / heartbeat / state 三条流的节流与打包
profiles/*.lua        已发布的内存布局，只读子集
```

每个模块都是 `return function(H) ... end`：`H` 是共享上下文（config/log/clock/net/...）。

## 不变量（改代码前先读）

1. **只读**：不写游戏内存、不调用游戏函数、不 `os.execute`。
2. **没有校验就不读**：direct 源必须先过 PE 映像大小 + 指令 pin；ffi 路径每次读前 `VirtualQuery`。
3. **心跳不续命**：heartbeat 只证明桥活着；控制器用 state 的到达时间判活。
4. **一条报文一种编码**：UDP 与文件广播发的是同一个字符串。
5. **出错不许带崩游戏**：`bridge.guard` 计数，超过 `max_error` 主动停。
6. 版本号必须与仓库根 `VERSION` 一致；`tools/build_addon_mdl3.py` 会拒绝不一致的包。

## 自测

```powershell
python -m unittest tests.test_lua_mdl3 tests.test_bridge_file_source -v
python tools\build_addon_mdl3.py --both
```

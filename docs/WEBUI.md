# Web 控制台

浏览器里的控制台，替代（并超出）原来那个 Tkinter 小窗口。启动：

```powershell
cd "E:\md\n mod\hd2-coyote"
.\.venv\Scripts\python.exe -m hd2coyote web
```

打开 `http://127.0.0.1:8787/`（会自动开浏览器）。参数：

| 参数 | 说明 |
|---|---|
| `--port 8787` | 端口 |
| `--host 127.0.0.1` | 监听地址。**默认只听本机** —— 这个页面能触发实际输出 |
| `--bridge-dir <目录>` | 桥的配置目录（默认 `%LOCALAPPDATA%\hd2coyote`） |
| `--no-browser` | 不自动开浏览器 |

## 它能做什么

| 区块 | 内容 |
|---|---|
| 实时状态 | 血量、肢体损伤、A/B 输出强度、是否武装/静音、累计输出、数据源与桥的在线状态 |
| 测试脉冲 | 指定 `强度% / 时长ms` 发一发，确认电极位置与体感 |
| 强度 | 每条规则（受伤/肢体损伤/阵亡/低血量）的开关与基础强度、总倍率、单次上限、绝对上限 —— **立即生效并落盘** |
| 游戏内桥 | 档位（`safe/net/menu/recon/live`）、UDP 端口、上报间隔、`profile`、血量/上限/肢体掩码/起始位/阵亡偏移 —— 写进 `bridge_config.lua`，**重启游戏生效** |
| 事件源 | 勾选启用哪些源（`game_bridge` / `http`）、看每个源此刻是否在线、HTTP 上报地址与令牌状态；下方「手动注入」可以直接造一个事件试试规则 |
| 波形库 | 命名波形（自定义 16 进制单元，或基于内置预设），可试打、删除，并与参考项目的 `pulse_data` JSON 互导 |
| 惩罚累积 | 受伤累积/血量越少越强的加成模型：每次事件加多少、缺失血量换算、封顶、多久不挨打开始回落、阵亡是否清零 |
| 检查更新 | 查 GitHub Releases 的最新版本；**只提示并给下载链接，绝不自动下载覆盖安装** |
| 诊断 | 桥上次实际运行的版本与档位、addon 的 `STATUS`、加载器日志行、桥日志尾部、**recon 报告** |
| 事件 | 检测事件 + 你在页面上的操作（急停/武装/测试脉冲/手动注入）都留痕 |

诊断区会主动提示最容易踩的两种状态：

* **没有 `bridge_config.lua`** → addon 跑内置默认（`safe`，什么都不做）；
* **配置档位 ≠ 上次实际运行档位** → 说明改了但没重启游戏。

## 关闭

三条路，任选：

1. **网页右上角「关闭程序」** —— 带确认框。它会先把输出归零、清空波形、断开设备，
   再让服务器退出；只接受**本机来源**（非 `127.0.0.1` 的请求返回 403，
   即使误把它开在 `0.0.0.0` 上也不会被远端关掉）。退出后页面会显示"控制器已关闭"。
2. **`stop.bat`** —— 先请求优雅退出，等端口释放；只有在没成功时才按端口找出进程强杀，
   并明确告诉你这次是强杀。用法：`stop.bat [端口]`（默认 8787）。
3. **`python -m hd2coyote stop [--port 8787] [--no-force]`** —— 同一套逻辑的 CLI。
   想先看它打算做什么就加 `--no-force`（只优雅退出，不强杀）。

> 顶部的「急停」只是**静音**（需要点「重新武装」才恢复输出），它不会退出程序；
> 要退出用「关闭程序」。

关闭成功后按钮会变成「已关闭」并禁用，页面顶部改成"已退出 / 设备 已断开 / 桥 离线"。
**再点一次也不会报错** —— 如果服务器已经没了，页面直接进入已关闭状态
（"✓ 控制器已经退出（连不上服务器）"），而不是弹一个 `Failed to fetch`。

任何一条路都不会让电极停在输出状态：`engine.stop()` 内部会 `device.stop()`，
socket 设备先 `mute()`（`clear-1/2` + 强度置 0）再断开连接。
若走的是强杀（第 2 条的兜底），电极侧要靠手机 App 在连接断开后自行停止 —— 所以优先用前两条。

## 接口

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/` | 单页控制台（无外部依赖，离线可用） |
| GET | `/api/status` | 控制器 + 桥 + 诊断的 JSON 快照 |
| GET | `/api/config` | 控制器配置（`config.json`） |
| GET | `/api/sources` | 可用事件源清单 + 每个源此刻的状态 + HTTP 上报示例 |
| GET | `/api/waves` | 命名波形库 + 内置预设清单 + 当前导出文本 |
| GET | `/api/update` | **上次**检查更新的结果（缓存的，不联网） |
| POST | `/api/config` | 局部合并控制器配置（rules/safety/device/hook/source/sources/ramp/update），落盘并立即生效 |
| POST | `/api/bridge` | 写 `bridge_config.lua`（校验 + 备份为 `.lua.bak`） |
| POST | `/api/actions` | `start` / `stop` / `arm` / `trip` / `test_pulse` / `device_start` / **`shutdown`** |
| POST | `/api/event` | 手动注入一个事件或状态包（也可以是数组）；照样过规则层与安全上限 |
| POST | `/api/waves` | 波形库：`set` / `edit` / `preset` / `remove` / `import` / `replace` / `export` / `test` |
| POST | `/api/update` | 真的去查一次 GitHub Releases（`{"clear": true}` 只清缓存） |
| GET | `/qr.svg` | 手机 App 扫码用的二维码（装了 `qrcode` 时） |

`POST /api/bridge` 会校验：`mode ∈ {safe,net,menu,recon,live}`、端口 `1024..65535`、
间隔 `0.02..5`、`profile` 只含字母数字下划线、偏移在 `-1..65535`（`limb_shift` 在 `0..7`，`-1` 写成 `nil`）。
越界一律返回 400 与中文原因，不会写出半截文件。

## 事件源 / 波形库 / 惩罚累积（0.5.0）

* **事件源**：勾选 `game_bridge`（游戏内桥，UDP）和/或 `http`（任何程序 POST JSON）。
  保存后**控制器会自动重建引擎**（源是在引擎线程里起的），不用重启控制台。
  细节、协议与"自己写一个源"见 [SOURCES.md](SOURCES.md)。
* **波形库**：`#wUnits` 里填 16 个十六进制字符的单元（**一个单元 = 100ms = 4×25ms**），
  或者只填预设名（`pinch/sting/buzz/ramp_up/breath/heartbeat/death`）。
  规则里的 `wave` 字段写波形库里的名字就优先用它，库里没有才退回内置预设。
  点「导出到下面」/「从下面导入」可与参考项目那种 `{"pulse_data": {...}}` JSON 互转。
* **惩罚累积**：勾上后，事件本身仍按规则给基础强度，另外叠一层"越来越疼"：
  每次命中 `per_event`、血量缺失换算 `hp_missing_pct`、封顶 `ceiling_pct`、
  超过 `decay_after_s` 没新事件就按 `decay_per_s` 回落，阵亡/复活是否清零看 `reset_on_death`。
  顶部状态里的 `累积+N%` 就是它当前的加成。
* **检查更新**：GET 只读缓存，点按钮才真去查 GitHub。查到新版只显示版本、说明和链接 ——
  本项目**不做自动下载覆盖安装**（那正是参考项目 `update.py` 里最容易把安装目录搞坏的一步）。

## 两种配置写法

`bridge_config.lua`（Lua 形式，Web 控制台生成的就是这种）：

```lua
-- 注释开头没问题
return {
  config = { mode = 'recon', port = 47777, interval = 0.1, profile = 'steam_25480438' },
  profiles = { steam_25480438 = { hp = 0x2C, hp_max = nil, limb_mask = 0x38, limb_shift = 0, dead = nil } },
}
```

或者 `bridge_config.txt`（纯文本，连 `loadstring` 都不需要）：

```
mode = recon
port = 47777
```

两种都会在日志里说明用了哪一种、解析出了什么、最终生效档位是什么。

## 安全

* 默认只绑定 `127.0.0.1`；`--host 0.0.0.0` 会把"能触发输出的接口"暴露给同网段，别这么干。
* `急停` 会把引擎置为静音（`armed=false`），必须点 `重新武装` 才恢复输出。
* 绝对上限（`safety.max_absolute`）是硬上限：任何规则、任何倍率都不能突破它。

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
| 诊断 | 桥上次实际运行的版本与档位、addon 的 `STATUS`、加载器日志行、桥日志尾部、**recon 报告** |
| 事件 | 检测事件 + 你在页面上的操作（急停/武装/测试脉冲）都留痕 |

诊断区会主动提示最容易踩的两种状态：

* **没有 `bridge_config.lua`** → addon 跑内置默认（`safe`，什么都不做）；
* **配置档位 ≠ 上次实际运行档位** → 说明改了但没重启游戏。

## 接口

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/` | 单页控制台（无外部依赖，离线可用） |
| GET | `/api/status` | 控制器 + 桥 + 诊断的 JSON 快照 |
| GET | `/api/config` | 控制器配置（`config.json`） |
| POST | `/api/config` | 局部合并控制器配置（rules/safety/device/hook/source），落盘并立即生效 |
| POST | `/api/bridge` | 写 `bridge_config.lua`（校验 + 备份为 `.lua.bak`） |
| POST | `/api/actions` | `start` / `stop` / `arm` / `trip` / `test_pulse` / `device_start` |
| GET | `/qr.svg` | 手机 App 扫码用的二维码（装了 `qrcode` 时） |

`POST /api/bridge` 会校验：`mode ∈ {safe,net,menu,recon,live}`、端口 `1024..65535`、
间隔 `0.02..5`、`profile` 只含字母数字下划线、偏移在 `-1..65535`（`limb_shift` 在 `0..7`，`-1` 写成 `nil`）。
越界一律返回 400 与中文原因，不会写出半截文件。

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

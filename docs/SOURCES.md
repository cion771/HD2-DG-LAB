# 事件源

「事件源」= 控制器的输入。每个源自己负责把外部世界变成 `Event`（受伤 / 肢体损伤 / 阵亡 /
复活 / 低血量），再交给**同一套**规则层与安全层 —— 所以**换源不会绕过任何上限**。

## 内置源

| 名字 | 类别 | 说明 | 关键源（断流会静音） |
|---|---|---|---|
| `game_bridge` | 状态流 | 游戏内 Lua 桥通过 UDP 上报血量/肢体/阵亡（默认） | 是 |
| `bridge_file` | 状态流 | 同一个桥改走文件广播：轮询 `%LOCALAPPDATA%\hd2coyote\bridge_state.json`（MDL3 宿主没有 `ffi` 时用） | 是 |
| `vision` | 状态流 | 桌面版进程外截取游戏客户区、按亮度算血条填充比，默认关闭 | 是 |
| `udp` | 状态流 | 兼容旧版 UDP 上报；Rust 客户端保留该名字 | 是 |

在 `config.json` 里选：

```json
"sources": {
  "enabled": ["game_bridge"]
}
```

* `enabled` 的顺序 = 启动顺序；写错名字会在保存时直接报错（不会静默忽略）。
* 想同时用两个源就写成 `["game_bridge", "vision"]` —— 两边的事件合流，规则层照常去重/冷却。
* Fluent 桌面版的**事件源**面板能勾选、看每个源此刻是否在线。

> 只勾 `vision` 的话**不会**去占 UDP 端口，也不会因为游戏没开而报错 —— 适合先拿校准工具试水。

## 0.7.0 移除了 HTTP 事件源

早先版本带一个 `http` 源：任何程序 POST 一行 JSON 到 `127.0.0.1:47778` 就能触发规则。
它在 0.7.0 被整体删除 —— 端口、令牌、限速、状态包超时一并移除。

* 旧配置里的 `sources.http_*` 字段会被忽略，不需要手动清理；
* `sources.enabled` 里若还留着 `"http"`，保存设置时会报「未知事件源」，删掉即可；
* 需要程序化触发规则时，用界面上的**手动注入**，或自己按下面「自己写一个源」加一个源。

## 手动注入

Fluent 桌面版的**事件源**面板下方有「手动注入」：选一个事件、填个数值，点一下就进规则层。
它走的是 `hd2coyote/sources/manual.py` 里的 `parse_event()`，状态包交给和游戏桥同一套
`EventTrackers` —— 所以手动注入和真实数据走同一条判定路径，不会出现两套阈值跑偏。

认识的事件名：`damage`（别名 `hit` / `hurt` / `受伤` / `掉血`）、`limb_injury`（`limb` /
`injury` / `肢体`）、`death`（`dead` / `阵亡`）、`revive`（`respawn` / `复活` / `增援`）、
`low_health`（`lowhp` / `低血量`）。

状态包写 `{"ev":"state","hp":62,"hp_max":100,"limbs":[0,1,0],"bleeding":0,"dead":0}`：
给了 `hp_max` 时 `hp` 是绝对值，不给时 `hp <= 1` 视为比例、`> 1` 视为百分制；
`limbs` 可以是数组，也可以是 `{"0": 1, "2": 1}` 这种字典。

## 自己写一个源

```python
from hd2coyote.events import Event
from hd2coyote.sources import Source, register_source


@register_source
class MySource(Source):
    name = "my_source"
    label = "我的事件源"
    kind = "event"          # 或 "state" / "state+event"
    critical = False        # True = 它断流时宁可静音
    hint = "一句话告诉用户怎么让它出数据"

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def poll(self, now: float) -> list[Event]:
        return []           # 每帧被引擎调用一次（20ms 一次）
```

放进 `hd2coyote/sources/` 并确保被 `sources/__init__.py` 导入，就会出现在
`source_catalog()` 与 Fluent 桌面版的事件源面板里。要点：

* 别在 `start()` 里做阻塞的重活（引擎线程在等它）；
* `critical_now()` 返回 True 时，引擎发现它掉线会立即**清空当前效果并静音**，
  等它回来再恢复；
* 需要被配置就覆盖 `apply_config(cfg)`（引擎热改配置时会调）；
* 事件的时间戳请用 `time.monotonic()`，与引擎同一条时间轴；
* 源只允许产生事件 —— 强度上限、急停、会话预算一律在规则层之后，源绕不过去。

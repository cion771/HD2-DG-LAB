# 事件源（0.5.0）

「事件源」= 控制器的输入。每个源自己负责把外部世界变成 `Event`（受伤 / 肢体损伤 / 阵亡 /
复活 / 低血量），再交给**同一套**规则层与安全层 —— 所以**换源不会绕过任何上限**。

## 内置源

| 名字 | 类别 | 说明 | 关键源（断流会静音） |
|---|---|---|---|
| `game_bridge` | 状态流 | 游戏内 Lua 桥通过 UDP 上报血量/肢体/阵亡（默认） | 是 |
| `http` | 状态 + 事件 | 任何程序 POST JSON 过来就能触发 | 只有上报过状态包之后才算 |

在 `config.json` 里选：

```json
"sources": {
  "enabled": ["game_bridge"],
  "http_host": "127.0.0.1",
  "http_port": 47778,
  "http_token": "",
  "http_max_per_s": 20.0,
  "http_timeout_s": 3.0
}
```

* `enabled` 的顺序 = 启动顺序；写错名字会在保存时直接报错（不会静默忽略）。
* 想同时用两个源就写成 `["game_bridge", "http"]` —— 两边的事件合流，规则层照常去重/冷却。
* 网页控制台的**事件源**面板能勾选、看每个源此刻是否在线、并带一份上报示例。

> 只勾 `http` 的话**不会**去占 UDP 端口，也不会因为游戏没开而报错 —— 适合先拿别的程序试水。

## HTTP 源协议

默认 `http://127.0.0.1:47778`。支持 `POST /`、`POST /event`、`POST /state`（等价），
`GET /health`、`GET /status`、`GET /` 返回统计。

**事件包**（`ev` 也可以用 `event` / `kind`）：

```json
{"ev": "damage",     "severity": 12.5}
{"ev": "limb_injury", "slot": 1, "bleeding": true, "name": "左腿"}
{"ev": "death"}
{"ev": "revive"}
{"ev": "low_health",  "ratio": 0.3}
```

中文名同样认识：`受伤` / `掉血` / `肢体` / `阵亡` / `复活` / `增援` / `低血量`。
一次 POST 也可以直接给数组：`[{...}, {...}]`。

**状态包**（和游戏桥的 UDP 报文同构，走的是同一套判定状态机）：

```json
{"ev": "state", "hp": 62, "hp_max": 100, "limbs": [0, 1, 0], "bleeding": 0, "dead": 0}
```

血量三种写法都认：给 `hp_max` 时 `hp` 是绝对值；不给时 `hp <= 1` 视为比例、`> 1` 视为百分制。
`limbs` 可以是数组，也可以是 `{"0": 1, "2": 1}` 这种字典。

> **断流保护**：一旦这个源上报过状态包，超过 `http_timeout_s` 没新状态就会被视为断流，
> 和游戏桥一样**静音等它回来**（不会拿着上一次的血量继续打）。只发事件包（不发状态包）的源
> 不会被这样判 —— 它本来就是"来一发算一发"。

命令行的例子（PowerShell）：

```powershell
# 状态：掉到 30% 血
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:47778/state -ContentType application/json `
  -Body '{"hp": 30, "hp_max": 100, "limbs": [0,0,0], "dead": 0}'

# 事件：受伤 12.5%
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:47778/event -ContentType application/json `
  -Body '{"ev":"damage","severity":12.5}'
```

* **令牌**：`http_token` 非空时，请求必须带 `X-Token: <令牌>` 头（也接受 `?token=`），
  用 `hmac.compare_digest` 比较；不匹配返回 403。
* **限速**：`http_max_per_s`（默认 20）是 1 秒滑窗，超了返回 429 并计入 `dropped` 统计。
  这是为了防止别的程序抽风时把强度顶到上限。
* **跨机**：默认只听 `127.0.0.1`。要让另一台机器上报，必须同时改 `http_host`、设 `http_token`，
  并且清楚这等于把"能触发输出的接口"暴露到网络上。

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
`source_catalog()` 与网页的事件源面板里。要点：

* 别在 `start()` 里做阻塞的重活（引擎线程在等它）；
* `critical_now()` 返回 True 时，引擎发现它掉线会立即**清空当前效果并静音**，
  等它回来再恢复；
* 需要被配置就覆盖 `apply_config(cfg)`（引擎热改配置时会调）；
* 事件的时间戳请用 `time.monotonic()`，与引擎同一条时间轴。

## 手动注入

网页控制台的**事件源**面板下方有「手动注入」：选一个事件、填个数值，点一下就进规则层。
它走的就是 `parse_event()` 这份映射（和 HTTP 上报、以后的新源是同一份），
所以不会出现"网页能触发、外部程序触发不了"这种两套逻辑跑偏的情况。

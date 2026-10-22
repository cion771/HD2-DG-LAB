# Rust 客户端

仓库中的 Rust workspace 是桌面控制器的新实现；Python 版本暂时保留为兼容性参考和差分测试基线，游戏内 Lua 桥继续保持原协议，不改写为 Rust。

## 结构

| crate | 职责 |
| --- | --- |
| `hd2-core` | 配置、事件、检测器、规则、惩罚累积、波形、安全限幅 |
| `hd2-source-udp` | Lua 桥 UDP v1、有限队列、传输/状态新鲜度 |
| `hd2-source-http` | HTTP 事件/状态源、令牌、限速和请求上限 |
| `hd2-dglab` | DG-LAB v2 WebSocket、绑定、反馈、心跳、分帧和二维码 URL |
| `hd2-controller` | 事件编排、效果仲裁、升降速、生命周期和故障归零 |
| `hd2-app` | 回环本地 API、内嵌 Fluent 前端、配置持久化和 CLI/桌面入口 |

## 构建与测试

Windows 需要 Rust stable（MSVC 工具链）和 Visual Studio Build Tools 的 C++ 工作负载。

```powershell
rustup toolchain install stable-x86_64-pc-windows-msvc --profile minimal
cargo test --workspace
cargo build --release -p hd2-app
.\target\release\hd2-app.exe doctor
.\target\release\hd2-app.exe desktop
```

桌面入口只监听 `127.0.0.1` 的随机端口，并复用现有离线前端。默认保持未武装；启动检测、设备连接与重新武装是分离的显式操作。

**当前 `vision` 外部 HUD 源仅接入 Python 桌面客户端，Rust crate 尚未绑定该适配器。** 如需完全不使用游戏内桥，按 [EAC 与视觉源说明](EAC.md) 使用 Python 桌面版及 `sources.enabled=["vision"]`。

## 兼容性重点

- 配置读取保留旧字段迁移，输出使用 UTF-8 JSON。
- 状态机、冷却、合并窗口和会话预算均使用单调时间。
- 波形编码使用与 Python `round` 一致的 ties-to-even 舍入。
- UDP、HTTP、WebSocket、配置和波形导入均有大小/队列限制。
- trip、stop、配置重载、关键状态源超时、设备断开和退出都必须收敛到清波形并将 A/B 强度归零。
- 更新功能只检查并提示，不自动安装。

## 安全边界

软件限幅不能保证人体安全。先使用 Mock，保持未武装完成验证；连接真机后也必须从最低可感知水平开始。强杀、断网或设备故障时无法证明归零指令已送达，必须在 App/设备端确认输出停止。

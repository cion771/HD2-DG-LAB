# 第三方组件与署名

本项目**不打包**任何第三方 mod 的代码。下面是它所依赖、参考或对接的东西。

## 运行时依赖（用户自己安装，本项目只对接）

| 组件 | 作者 | 许可 | 本项目如何使用 |
|---|---|---|---|
| [Bingus Shared Loader](https://github.com/CowboyBingus/Helldivers2ModLoader) | CowboyBingus | 见其仓库 | 社区加载器。本项目的 Lua 桥作为它的 addon 运行（`data/9ba626afa44a3aa3.patch_N` + `-- HD2-Addon:` 头）。 |
| [Mod Options Menu](https://github.com/CowboyBingus/ModOptionsMenu) | CowboyBingus | 0BSD | 桥的 `menu` 档会向它注册选项。**可选**：Web 控制台不依赖它。 |
| [Mod Bindings Menu](https://github.com/CowboyBingus/ModBindingsMenu) | CowboyBingus | 见其仓库 | 仅参考其按键绑定挂载方式，未对接。 |

## 参考实现

| 来源 | 许可 | 参考了什么 |
|---|---|---|
| [etxp / HD2-G60-Smart-Targeting](https://github.com/etxp/HD2-G60-Smart-Targeting) | MIT | "如何在游戏内定位本地玩家实体"（ping actor 表 + 环形缓冲）这一思路，以及对应的偏移锚点。`lua/hd2_coyote_bridge.lua` 的 `PROFILES` 里已署名。 |
| DG-LAB SOCKET 协议（官方 App 的局域网控制协议） | 官方文档/协议本身 | `hd2coyote/device/dglab_socket.py` 按该协议实现控制器侧服务端。 |

## 未随仓库分发的内容

- `ref/`（社区 mod 的源码导出）**不在版本库里**。它是为了核对 ModOptionsMenu 的真实 API、
  以及加载器的启动契约，用 `tools/extract_lua_strings.py` 从**本机已安装的** mod 里现场导出的，
  属于第三方代码，仅作本地阅读，不再分发。
- 游戏本体、加载器、各个社区 mod 的归档文件均不在本仓库中。

## 免责声明

本项目与 Arrowhead Game Studios、Sony、DG-LAB（郊狼）均无关联，未获其授权或背书。
《绝地潜兵 2》没有官方 mod API，使用社区加载器存在账号与稳定性风险，请自行判断。

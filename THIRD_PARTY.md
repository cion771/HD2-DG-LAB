# 第三方组件与署名

本项目**不打包**任何第三方 mod 的代码。下面是它所依赖、参考或对接的东西。

## 运行时依赖（用户自己安装，本项目只对接）

| 组件 | 作者 | 许可 | 本项目如何使用 |
|---|---|---|---|
| [MDL3（麦当当3）预览版](https://ayakamods.com/mods/mdl3-preview-%E9%BA%A6%E5%BD%93%E5%8A%A83-%E9%A2%84%E8%A7%88%E7%89%88%EF%BC%81.5710/) | 社区作者（闭源） | 闭源，禁再分发 | **0.8.0 起 Lua 桥的前置加载器。** 它自带启动加载器并兼容 BSL addon 契约；本项目只通过它的 Lua mod 契约（`ctx` / `on_update`）与 `-- HD2-Addon:` 契约对接，**不打包、不反编译、不再分发**。 |
| [MDL（Mod Dynamic Loader）](https://ayakamods.com/mods/mdl-mod-dynamic-loader-pro-%E6%A8%A1%E7%BB%84%E5%8A%A8%E6%80%81%E5%8A%A0%E8%BD%BD%E5%99%A8.4713/) | 社区作者 | 闭源 | MDL3 的前代；其公开文档是本项目了解 `ctx` 能力边界的参考。 |
| [Bingus Shared Loader](https://github.com/CowboyBingus/Helldivers2ModLoader) | CowboyBingus | 见其仓库 | **已失效**（2026-10-20 游戏更新后）。旧版 `lua/hd2_coyote_bridge.lua` 仍按它的 `-- HD2-Addon:` 契约运行，仅保留一版做回滚。 |
| [Mod Options Menu](https://github.com/CowboyBingus/ModOptionsMenu) | CowboyBingus | 0BSD | 仅旧版 addon 使用。0.8.0 的 MDL3 版本不再注册任何菜单项，改用 MDL3 原生选项。 |
| [Mod Bindings Menu](https://github.com/CowboyBingus/ModBindingsMenu) | CowboyBingus | 见其仓库 | 仅参考其按键绑定挂载方式，未对接。 |

## 参考实现

| 来源 | 许可 | 参考了什么 |
|---|---|---|
| [SkyeShade / HD2Runtime](https://github.com/SkyeShade/HD2Runtime) | MIT | 0.5.6 的当前构建只读数据布局与验证锚点：玩家管理器、本地 peer、Avatar network map、health manager、Helldiver 类型与六个肢体 zone。桥只移植完成读取所需的最小子集，不调用其写入/游戏函数。 |
| [etxp / HD2-G60-Smart-Targeting](https://github.com/etxp/HD2-G60-Smart-Targeting) | MIT | 旧版本曾参考 ping actor 表定位思路；0.5.6 已改为玩家管理器与本地 peer，不再依赖 ping。 |
| [YingXIAmour / DG-Lab-Punishment](https://github.com/YingXIAmour/DG-Lab-Punishment) | Apache-2.0 | 0.5.0 的四个特性参考了它的思路：事件源插件化（`plugins/` + `module_manager.py`）、命名波形库与波形编辑器（`pulse_data` + `pulse_wave_gui.py` 的频率换算）、"血越少越强"的惩罚模型、检查更新。**没有复制其代码**；波形库与它的 `pulse_data` JSON 格式保持互导。它的 `update.py`（杀进程 + 覆盖安装）是明确的反面教材，本项目只做"提示 + 给链接"。 |
| DG-LAB SOCKET 协议（官方 App 的局域网控制协议） | 官方文档/协议本身 | `hd2coyote/device/dglab_socket.py` 按该协议实现控制器侧服务端。 |

## Windows 桌面包

桌面首页参考 DG-Lab-Punishment 的“系统状态 / 功能页面 / 日志”信息层次，以原创 Fluent 风格侧栏、卡片、SVG 图标实现；未复制其图片、背景或界面代码。

EXE 包含 Python 及第三方运行库，各组件保留各自许可，不因本项目使用 MIT 而改变。打包脚本收集依赖的发行元数据 / LICENSE：

| 组件 | 许可 / 用途 |
|---|---|
| CPython | PSF 及随附第三方声明；嵌入式 Python 运行时 |
| pywebview / pythonnet / clr-loader | BSD-3-Clause / MIT / MIT；原生 Windows 窗口与 .NET 互操作 |
| websockets / qrcode | BSD-3-Clause 系列许可，详见随包元数据；设备协议与二维码 |
| bottle / proxy-tools / cffi / pycparser / typing-extensions | 各自 MIT / BSD / PSF 系列许可；间接运行依赖 |
| Microsoft WebView2 SDK | Microsoft 许可；pywebview 附带的桥接程序集。WebView2 Runtime 需单独安装，遵循 Microsoft 条款 |
| PyInstaller | GPL-2.0-or-later 附带 bootloader 分发例外；只用于构建，不改变本项目许可 |

0.5.1 起不再依赖或打包 NumPy、Pillow、MSS、DXcam；二维码使用 SVG / 网格绘制，不做图像识别。

## 未随仓库分发的内容

- `ref/`（社区 mod 的源码导出）**不在版本库里**。它是为了核对 ModOptionsMenu 的真实 API、
  以及加载器的启动契约，用 `tools/extract_lua_strings.py` 从**本机已安装的** mod 里现场导出的，
  属于第三方代码，仅作本地阅读，不再分发。
- 游戏本体、加载器、各个社区 mod 的归档文件均不在本仓库中。

## 免责声明

本项目与 Arrowhead Game Studios、Sony、DG-LAB（郊狼）均无关联，未获其授权或背书。
《绝地潜兵 2》没有官方 mod API，使用社区加载器存在账号与稳定性风险，请自行判断。

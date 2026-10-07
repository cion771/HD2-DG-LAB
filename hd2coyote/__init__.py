"""HD2 × 郊狼（DG-LAB）：把《绝地潜兵 2》的受伤/阵亡变成电击反馈。

版本号的唯一来源是仓库根目录的 `VERSION` 文件：
`hd2coyote.__version__`、`lua/hd2_coyote_bridge.lua` 的 `VERSION`、
以及打包时用的版本，三者必须一致 —— 由 `tools/build_addon.py` 和
`tests/test_version.py` 强制（不一致就构建/测试失败）。
"""

from __future__ import annotations

__version__ = "0.4.2"
__all__ = ["__version__"]

"""兼容桥接包 common (auto/common)

本项目的核心模块已从旧 ``common.*`` 命名空间收编进 ``core.*``
(见 core/exec.py:18 与 adapters/markets/a.yaml 注释)。但外部提供的新版
策略文件 (如 strategies/dragon_callback.py 由 tmp/dragon_callback.py 迁入)
仍按旧 ``common.indicators / common.market / common.exec_cn / common.filters``
命名空间书写。

为不改动这些策略源码、又让其在当前布局下可导入, 本包仅做**薄重导出**,
全部转发到 core.* 的真实实现。若后续策略源码改为直接 import core.*,
本包可整体删除。
"""

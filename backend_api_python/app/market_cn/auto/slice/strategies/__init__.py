"""slice/strategies — 已迁移到新契约的策略实现（切片/展示点归策略文件）。

导出与旧 `auto_slim.strategies` 一致；g56 / dragon_callback 处于迁移中，
按模块路径导入（其测试尚未并入默认导出）。
"""

from app.market_cn.auto.slice.strategies.knife_catch import KnifeCatchSlim
from app.market_cn.auto.slice.strategies.tail_oversold import TailOversoldSlim

__all__ = ["KnifeCatchSlim", "TailOversoldSlim"]

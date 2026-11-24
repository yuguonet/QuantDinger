# -*- coding: utf-8 -*-
"""stock-analyzer/_deps.py —— 依赖桥：把 market-screener 的取证/指标实现借给 `_deep` 层。

【为什么单独一层】`_deep.py` 是从 market-screener 完整复制的 deep 层，它调用的底层实现
（日线技术面、资金流、封板结构、同刻校准、形态识别、多周期、预测分、报告装配）属于
**取证/指标层**，不是 deep 层本身。把这些再复制一份会让同一口径出现两份实现、
必然各自漂移，故此处只做一次性转出，`_deep.py` 内所有 `from ._deps import …` 都落在这里。

【边界】本模块只做名字转出，不含任何逻辑：
  · `_H` = market-screener 的证据加工层（_helpers）
  · `_F` = market-screener 的客观取数层（_facts）
  · `_C` = market-screener 的公共口径（common）
若将来要彻底独立，替换本模块为本地实现即可，`_deep.py` 一行不用改。
"""

from ...modpath import import_module as _import_module

_H = _import_module("skills.market-screener._helpers")
_F = _import_module("skills.market-screener._facts")
_C = _import_module("skills.market-screener.common")

SkillResult = _C.SkillResult
fetch_kline = _C.fetch_kline

select_strategy = _H.select_strategy
resolve_names = _H.resolve_names
build_report = _H.build_report
tech_summary = _H.tech_summary
cross_verdict = _H.cross_verdict
flow_summary = _H.flow_summary
seal_summary = _H.seal_summary
pump_dump_risk = _H.pump_dump_risk
zt_index = _H.zt_index
attach_prediction = _H.attach_prediction
same_time_batch = _H.same_time_batch
intraday_shape_batch = _H.intraday_shape_batch
fetch_rt_map = _H.fetch_rt_map
apply_rt_overlay = _H.apply_rt_overlay
_now_hhmm = _H._now_hhmm
_FETCH_TIMEOUT_S = _H._FETCH_TIMEOUT_S

_detect_patterns = _F.detect_patterns
_market_state = _F.market_state
_multitimeframe = _F.multitimeframe
intraday_bars = _F.intraday_bars

__all__ = [
    "SkillResult", "fetch_kline", "select_strategy", "resolve_names", "build_report",
    "tech_summary", "cross_verdict", "flow_summary", "seal_summary", "pump_dump_risk",
    "zt_index", "attach_prediction", "same_time_batch", "intraday_shape_batch",
    "fetch_rt_map", "apply_rt_overlay", "_now_hhmm", "_FETCH_TIMEOUT_S",
    "_detect_patterns", "_market_state", "_multitimeframe", "intraday_bars",
]

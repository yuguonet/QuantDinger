# -*- coding: utf-8 -*-
"""finance 域分类学（DomainSpec）。

只放纯数据/正则，禁止 import 核心图（nodes/task_agent/tracing）——会成环。
工具注册仍在 tools/finance/*.py；本文件只回答「金融域如何被识别/归类」。
"""
from __future__ import annotations

import re

from ...domain_registry import DomainSpec

# verb → noun（追溯链名 domain+verb+noun 的缺省补齐）
_VERB_NOUNS = {
    "screen": "stock",
    "analysis": "stock",
    "compare": "stock",
    "query": "stock",
    "code": "strategy",
    "explain": "indicator",
}

# 行情/交易语境词（出现才考虑交易日口径与语义异常；纯闲聊不搅）
# 与 resolvers/time.py 历史口径保持一致，勿擅自增删（差一词就可能给错日期）
_MARKET_WORDS = re.compile(
    r"行情|开盘|收盘|涨停|跌停|大盘|指数|个股|股票|资金|龙虎榜|板块|题材|"
    r"买入|卖出|建仓|减仓|仓位|止损|止盈|K线|均线|MACD|RSI|KDJ|涨跌|做多|做空|看多|看空|"
    r"涨幅|跌幅|振幅|换手|成交|量比|市盈率|市值|涨速|跌速|封板|炸板|打板"
)

# ★ 2026-10-14：`_DATA_DOMAINS` / `_LIST_PRODUCERS` / `_LIST_CONSUMERS` /
#   `_INTENT_VERBS` 四个常量与 `primary=True` 已删除。
#   它们只喂给 `domain_registry` 那 6 个零消费方函数（旧编排层 plan_linter 遗产），
#   本次随函数一并删除；对应的 DomainSpec 字段也已移除（见其头注）。
#
# ⚠️ 关于那份被删的 `_DATA_DOMAINS`（**为什么不能再照它复原**）：
#   · 它是`数据域词典`的**第二份**，与活的那份
#     `app/agent/preselect/tool_preselect.py::DATA_DOMAINS` 早已分叉：
#     这里是 realtime_quote / fund_flow / financials / dragon_tiger / hot_rank /
#     limit_pool / intel_news / market_overview，活的那份是 quote / fundflow /
#     trend / chip / volume / market / dragon / strategy / intel / capital /
#     knowledge —— 同一件事两套拼法，正是"同一决策两个事实源"。
#   · 它引用的工具名里 `index_daily`、`day_series` **已下线**（2026-10-14 实测
#     注册表 53 个工具里没有这两个）；若照抄会把失效名带回活链路。
#
# ⚠️ 顺带订正一处**幻觉引用**（2026-10-14 核实）：本文件原先声称"已补上真实断言
#    W20a/W20b/W20c（词典 ⊆ 注册表 / 每域首选可用 / 摘除名不得回流）"，并指到一个
#    tests 目录下名为 test_wiring 的用例文件 —— 那个文件**根本不存在**，
#    即"声称有守卫、其实没有"（2026-10-13 那份外部审查报告里也出现过同类幻觉引用）。
#    真实守卫是 `tests/test_v9_preselect_pipeline.py` 的**基线逐键对拍**
#    （`tests/fixtures/p9_tool_preselect_baseline.json`）+ `app/agent/scripts/
#    qd_smoke.py` 里的 `check_domain_dict`。以后引断言请引这两个。
SPEC = DomainSpec(
    name="finance",
    entity_types=("stock",),
    trading_calendar=True,
    verb_nouns=_VERB_NOUNS,
    word_pattern=_MARKET_WORDS,
)

# -*- coding: utf-8 -*-
"""market-screener 实时行情回填契约对齐（C1 守卫，2026-10-21）。

【锁住的缺陷】fetch_rt_map 的 _f(None)=0.0 把缺失的
limit_up/limit_down/changePercent 伪造成 0 → apply_rt_overlay 判 no_limit
→ 封板票 buyable=True 进可买表、涨跌幅显示 +0.00%。锁：缺失保留 None、
missing≠no_limit、价未知绝不 buyable=True。

包目录名含连字符，走 modpath.import_module。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.modpath import import_module

_helpers = import_module("app.agent.skills.market-screener._helpers")



# ── C1（2026-10-21）：fetch_rt_map 把缺失归一成 0.0 → 封板票进可买表 ──
# 契约（data_tools.get_realtime_quote docstring）：limit_up/limit_down 腾讯增补
# 偶发失败时**缺省 None**；0/-1 是「无涨跌幅限制」哨兵，None 是「未知」——
# _f(None)=0.0 会把未知伪造成 0 价，_parse_limit_px(0.0)→no_limit→limit_up=False
# → 封板票 buyable=True 进可买表（limit-rules.md：missing≠no_limit）。

def test_fetch_rt_map_preserves_missing_limit_prices_and_change_pct():
    """quote 返回缺 limit_up/limit_down/changePercent → fetch_rt_map 保留 None。"""
    fake = {
        "count": 1, "error": None,
        "data": {"600408": {"stock_code": "600408", "last": 4.06,
                            "previousClose": 3.69, "name": "安泰集团",
                            "time": "2026-10-14 15:00:00"}},
    }
    import app.agent.tools.finance.data_tools as dt
    orig = dt.get_realtime_quote
    dt.get_realtime_quote = lambda codes: fake
    try:
        m = _helpers.fetch_rt_map(["600408"])
    finally:
        dt.get_realtime_quote = orig
    rt = m["600408"]
    assert rt["limit_up_price"] is None, f"缺失应保留None，实际{rt}"
    assert rt["limit_down_price"] is None, f"缺失应保留None，实际{rt}"
    assert rt["change_pct"] is None, f"缺失涨跌幅不得显示成+0.00%，实际{rt}"


def test_apply_rt_overlay_missing_limit_px_never_buyable_true():
    """涨跌停价未知且无昨收可近似 → limit_up/down 均 None，buyable=None（待定）。

    修复前：fetch_rt_map 给 0.0 → no_limit → limit_up=False → buyable=True，
    封板票进可买表。
    """
    row = {"code": "600408", "name": "安泰集团",
           "tech": {"ok": True, "close": 4.06, "limit_up": False}}
    rt = {"price": 4.06, "change_pct": None, "previous_close": None,
          "limit_up_price": None, "limit_down_price": None,
          "time": "2026-10-14 15:00:00"}
    _helpers.apply_rt_overlay(row, rt, code="600408", name="安泰集团")
    assert row["rt"]["limit_band"] == "missing", row["rt"]
    assert row["rt"]["limit_up"] is None, row["rt"]
    assert row["rt"]["buyable"] is None, row["rt"]


def test_apply_rt_overlay_partial_missing_not_buyable_true():
    """任一侧涨跌停状态未知 → 不可确认可买（buyable=None），另一侧已知 False 也不给 True。"""
    row = {"code": "600001", "name": "浦发银行",
           "tech": {"ok": True, "close": 10.0, "limit_up": False}}
    rt = {"price": 10.0, "change_pct": 1.0, "previous_close": 9.9,
          "limit_up_price": 10.89, "limit_down_price": None,
          "time": "2026-10-14 15:00:00"}
    _helpers.apply_rt_overlay(row, rt, code="600001", name="浦发银行")
    assert row["rt"]["limit_up"] is False, row["rt"]
    assert row["rt"]["limit_down"] is None, row["rt"]
    assert row["rt"]["buyable"] is None, row["rt"]


def test_apply_rt_overlay_missing_limit_px_formula_fallback_sealed():
    """价未知但有昨收 → 按 get_limit_pct 公式近似（limit-rules.md missing 口径）：
    实价触涨停 → limit_up=True → buyable=False（封板票不得进可买表），且留痕。"""
    row = {"code": "600408", "name": "安泰集团",
           "tech": {"ok": True, "close": 3.69, "limit_up": False}}
    rt = {"price": 4.06, "change_pct": 10.03, "previous_close": 3.69,
          "limit_up_price": None, "limit_down_price": None,
          "time": "2026-10-14 15:00:00"}
    _helpers.apply_rt_overlay(row, rt, code="600408", name="安泰集团")
    assert row["rt"]["limit_band"] == "missing", row["rt"]
    assert row["rt"]["limit_up"] is True, row["rt"]
    assert row["rt"]["buyable"] is False, row["rt"]
    assert any("涨停封板" in x for x in row.get("rt_risks") or []), row

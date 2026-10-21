# -*- coding: utf-8 -*-
"""market-screener 行情回填契约对齐 + 缺价不判跌停守卫（2026-10-21）。

【锁住的缺陷】线上「现在买什么股?」无结果的第二处根因，稳定复现 54/54：
1. _facts.candidate_pool 的 fill_quote 字段映射写的是 price/change_pct，
   但 get_realtime_quote 统一契约是 last/changePercent（data_tools docstring），
   候选 price 恒为 None；
2. objective_preexclude 的 last 取值经 _f(None)=0.0 吞掉缺失，
   0.0 <= 跌停价+0.01 恒真 → 全池 100% 判「跌停封死」，
   funnel 报 scored=0 / deep_n=0（agent 记忆里判定为假阴性）。

包目录名含连字符，走 modpath.import_module。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.modpath import import_module

_facts = import_module("app.agent.skills.market-screener._facts")
_helpers = import_module("app.agent.skills.market-screener._helpers")


def test_fill_quote_maps_contract_keys_to_candidate_price():
    """fill_quote 按真实契约回填：last→price、changePercent→change_pct。"""
    import app.agent.tools.finance.data_tools as dt

    codes = [f"{600000 + i}" for i in range(3)]

    def fake_call_tool(name, **kw):
        if name == "search_stocks":
            return {"stocks": [{"code": c, "name": f"票{c}", "reason": "测试"}
                               for c in codes]}
        return {}

    def fake_rt(codes_str):
        part = [c.strip() for c in codes_str.split(",") if c.strip()]
        # 真实契约：没有 price / change_pct 键（探针实测 2026-10-21）
        data = {c: {"stock_code": c, "last": 10.0 + i, "changePercent": 2.5,
                    "previousClose": 9.9, "limit_up": 10.89, "limit_down": 8.91,
                    "name": f"RT{c}", "time": "10:00"}
                for i, c in enumerate(part)}
        return {"count": len(part), "error": None, "data": data}

    orig_rt = dt.get_realtime_quote
    orig_ct = _facts.call_tool
    dt.get_realtime_quote = fake_rt
    _facts.call_tool = fake_call_tool
    try:
        pool = _facts.candidate_pool(strategy="post_market", sources="search",
                                     queries="测试", search_top_n=10)
    finally:
        dt.get_realtime_quote = orig_rt
        _facts.call_tool = orig_ct

    cands = pool["candidates"]
    assert len(cands) == 3, f"3只搜索结果应在池中，实际{len(cands)}"
    sample = next(c for c in cands if c["code"] == "600001")
    assert sample.get("price") == 10.0 + 1, \
        f"price 应由契约键 last 回填，实际{sample.get('price')}"
    assert sample.get("change_pct") == 2.5, \
        f"change_pct 应由 changePercent 回填，实际{sample.get('change_pct')}"


def test_preexclude_missing_price_not_limit_down():
    """price 缺失 + limit_down 有值：判不了跌停，不剔，计入 quotes_missing。

    修复前：_f(None)=0.0 → 0.0 <= limit_down+0.01 恒真 → 100% 假阴性。
    """
    cands = [
        {"code": "600001", "name": "缺价票", "turnover_pct": 5.0,
         "previous_close": 10.01, "limit_up": 11.01, "limit_down": 9.01},
    ]
    kept, stats = _helpers.objective_preexclude(cands, "intraday")
    assert [c["code"] for c in kept] == ["600001"], \
        f"缺价不应判跌停剔除，实际kept={kept} stats={stats}"
    assert stats["limit_down"] == 0, stats
    assert stats["quotes_missing"] == 1, stats


def test_preexclude_real_limit_down_still_excluded():
    """回归：真实跌停（price <= limit_down）仍被剔除，防修复矫枉过正。"""
    cands = [
        {"code": "600001", "name": "跌停票", "price": 9.01, "turnover_pct": 5.0,
         "previous_close": 10.01, "limit_up": 11.01, "limit_down": 9.01},
    ]
    kept, stats = _helpers.objective_preexclude(cands, "intraday")
    assert kept == [], f"真实跌停仍应剔除，实际kept={kept}"
    assert stats["limit_down"] == 1, stats

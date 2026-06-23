# -*- coding: utf-8 -*-
"""
market_screener/run.py

A股全市场短线选股 — 入口 + 策略调度。

根据交易时间自动选择策略:
  - 09:25-10:00 → 关键窗口（集合竞价+开盘定基调）
  - 09:30-14:29 → intraday (盘中短线)
  - 14:30-15:00 → eod (尾盘隔夜)
  - 15:00+ / 非交易日 → post_market (盘后复盘)

框架调用方式:
    from market_screener.run import run
    result = run()
"""

from __future__ import annotations

import logging
from datetime import datetime, date
from typing import Any, Dict

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
#  策略调度
# ═══════════════════════════════════════════════════════════════

def _select_strategy() -> str:
    """根据当前时间返回策略名称: intraday / eod / post_market。"""
    now = datetime.now()
    h, m = now.hour, now.minute
    if now.weekday() >= 5:
        return "post_market"
    if h < 9 or (h == 9 and m < 30):
        return "post_market"
    if h == 9 and m >= 30:
        return "intraday"
    if h < 14 or (h == 14 and m < 30):
        return "intraday"
    if h == 14 and m >= 30:
        return "eod"
    return "post_market"


# ═══════════════════════════════════════════════════════════════
#  Phase 1 — 预筛选
# ═══════════════════════════════════════════════════════════════

def pre_screen() -> Dict[str, Any]:
    """Phase 1: Python 预筛选（0 token，不消耗模型调用）。

    注意：本函数不接受任何参数。不要传入 filters、kwargs 等。
    策略根据当前交易时间自动选择。

    流程：
    1. 评估市场状态（资金流向、涨跌停、板块强弱）
    2. 根据市场状态用 search_stocks 搜索候选
    3. 补充连板 + 龙回头
    4. 过滤（换手率>=2%、非涨停封板）
    """
    strategy = _select_strategy()
    today = date.today().isoformat()
    logger.info("[market_screener] Phase 1 预筛选，策略: %s", strategy)

    try:
        if strategy == "intraday":
            from .intraday import prescreen
            result = prescreen(today)
        elif strategy == "eod":
            from .eod import prescreen
            result = prescreen()
        else:
            from .post_market import prescreen
            result = prescreen(today)
    except Exception as e:
        logger.warning("[market_screener] Phase 1 预筛选失败: %s", e)
        return {"strategy": strategy, "error": str(e), "candidates": [], "market": {}, "main_themes": []}

    result["strategy"] = strategy
    return result


# ═══════════════════════════════════════════════════════════════
#  Phase 2 — 深入分析
# ═══════════════════════════════════════════════════════════════

def deep_analyze(prescreen_result: Dict[str, Any]) -> Dict[str, Any]:
    """Phase 2: 对 Phase 1 候选股做深入分析。

    参数：
        prescreen_result: pre_screen() 的返回值

    流程：
    - 盘中策略：逐只调用工具（资金流向+多周期分析）→ 评分排序
    - 尾盘策略：条件搜索+尾盘特征验证
    - 盘后策略：全市场技术形态扫描+介入点计算
    """
    strategy = prescreen_result.get("strategy", _select_strategy())
    _tool_calls = []
    _tool_nodes = []
    _missing_data = []

    if strategy == "intraday":
        from .intraday import deep_analyze as _deep, tech_check
        candidates = prescreen_result.get("candidates", [])
        market = prescreen_result.get("market", {})
        main_themes = prescreen_result.get("main_themes", [])

        if not candidates:
            from app.agent.chain.schema import SkillReport
            report = SkillReport(
                skill_name="market_screener", score=45.0, direction="neutral",
                confidence=0.5, signal="今日无明确短线标的",
                analysis="", factors=[], status="ok",
            )
        else:
            analyzed = []
            for c in candidates[:8]:
                tech = tech_check(c["code"])
                result = _deep(c, tech, _tool_calls, _tool_nodes, _missing_data)
                if result:
                    analyzed.append(result)
            analyzed = [a for a in analyzed if a.get("score", 0) >= 60 and a.get("direction") == "bullish"]
            analyzed.sort(key=lambda x: -x.get("score", 0))
            if analyzed:
                avg_score = sum(a["score"] for a in analyzed) / len(analyzed)
            else:
                avg_score = 50.0
            from app.agent.chain.schema import SkillReport
            report = SkillReport(
                skill_name="market_screener", score=round(avg_score, 1),
                direction="bullish" if avg_score >= 55 else ("bearish" if avg_score < 45 else "neutral"),
                confidence=min(0.9, 0.4 + len(analyzed) * 0.06),
                signal="", analysis="", factors=[], status="ok",
                output_data={"analyzed": analyzed, "market": market},
            )
    elif strategy == "eod":
        from .eod import deep_analyze as _deep
        candidates = prescreen_result.get("candidates", [])
        analyzed = []
        for c in candidates[:6]:
            result = _deep(c, _tool_calls, _tool_nodes, _missing_data)
            if result:
                analyzed.append(result)
        analyzed = [a for a in analyzed if a.get("score", 0) >= 60 and a.get("direction") == "bullish"]
        analyzed.sort(key=lambda x: -x.get("score", 0))
        if analyzed:
            avg_score = sum(a["score"] for a in analyzed) / len(analyzed)
        else:
            avg_score = 50.0
        from app.agent.chain.schema import SkillReport
        report = SkillReport(
            skill_name="market_screener", score=round(avg_score, 1),
            direction="bullish" if avg_score >= 55 else ("bearish" if avg_score < 45 else "neutral"),
            confidence=min(0.9, 0.4 + len(analyzed) * 0.06),
            signal="", analysis="", factors=[], status="ok",
            output_data={"analyzed": analyzed},
        )
    else:
        from .post_market import deep_analyze as _deep
        candidates = prescreen_result.get("candidates", [])
        analyzed = []
        for c in candidates[:6]:
            result = _deep(c, _tool_calls, _tool_nodes, _missing_data)
            if result:
                analyzed.append(result)
        analyzed = [a for a in analyzed if a.get("score", 0) >= 60 and a.get("direction") == "bullish"]
        analyzed.sort(key=lambda x: -x.get("score", 0))
        if analyzed:
            avg_score = sum(a["score"] for a in analyzed) / len(analyzed)
        else:
            avg_score = 50.0
        from app.agent.chain.schema import SkillReport
        report = SkillReport(
            skill_name="market_screener", score=round(avg_score, 1),
            direction="bullish" if avg_score >= 55 else ("bearish" if avg_score < 45 else "neutral"),
            confidence=min(0.9, 0.4 + len(analyzed) * 0.06),
            signal="", analysis="", factors=[], status="ok",
            output_data={"analyzed": analyzed},
        )

    # 构建最终输出
    output = report.to_dict() if hasattr(report, "to_dict") else {
        "skill": "market-screener",
        "score": report.score,
        "direction": report.direction,
        "confidence": report.confidence,
        "signal": report.signal,
        "analysis": report.analysis,
        "factors": [{"name": f.name, "value": f.value, "score": f.score} for f in (report.factors or [])],
        "status": report.status,
    }
    output["strategy_used"] = strategy
    output["tools_called"] = _tool_calls
    output["missing_data"] = _missing_data
    if hasattr(report, "output_data") and report.output_data:
        output["output_data"] = report.output_data

    return output


# ═══════════════════════════════════════════════════════════════
#  主入口
# ═══════════════════════════════════════════════════════════════

def run() -> Dict[str, Any]:
    """完整选股流程：Phase 1 + Phase 2。"""
    prescreen_result = pre_screen()
    if prescreen_result.get("error"):
        return prescreen_result
    return deep_analyze(prescreen_result)

# -*- coding: utf-8 -*-
"""
market_screener/run.py

A股全市场短线选股 — 入口 + 策略调度。

根据交易时间自动选择策略:
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
from typing import Any, Dict, List

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
    if h < 9 or (h == 9 and m < 30) or h >= 15:
        return "post_market"
    if h >= 14 and m >= 30:
        return "eod"
    return "intraday"


# ═══════════════════════════════════════════════════════════════
#  框架标准入口
# ═══════════════════════════════════════════════════════════════

def run() -> Dict[str, Any]:
    """一步完成 Phase 1 + Phase 2。"""
    strategy = _select_strategy()
    today = date.today().isoformat()
    _tool_calls: List[str] = []
    _tool_nodes: List = []
    _missing_data: List = []

    logger.info("[market_screener] 执行策略: %s", strategy)

    report = None
    if strategy == "intraday":
        from .intraday import run_strategy
        report = run_strategy(today, _tool_calls, _tool_nodes, _missing_data)
    elif strategy == "eod":
        from .eod import run_strategy
        report = run_strategy(_tool_calls, _tool_nodes, _missing_data)
    else:
        from .post_market import run_strategy
        report = run_strategy(today, _tool_calls, _tool_nodes, _missing_data)

    if report is None:
        return {
            "skill": "market_screener",
            "status": "failed",
            "error": f"策略 {strategy} 执行失败",
            "score": 0,
            "direction": "neutral",
            "confidence": 0,
            "signal": "",
            "analysis": "",
            "factors": [],
            "strategy_used": strategy,
            "tools_called": _tool_calls,
        }

    # 统一转 dict
    if hasattr(report, "to_dict"):
        d = report.to_dict()
    elif isinstance(report, dict):
        d = report
    else:
        d = {
            "score": getattr(report, "score", 50),
            "direction": getattr(report, "direction", "neutral"),
            "signal": getattr(report, "signal", ""),
            "analysis": str(getattr(report, "analysis", ""))[:2000],
            "status": getattr(report, "status", "ok"),
            "confidence": 0.5,
            "factors": [],
        }

    d.setdefault("skill", "market_screener")
    d["strategy_used"] = strategy
    d["tools_called"] = _tool_calls
    return d


def pre_screen() -> Dict[str, Any]:
    """Phase 1: Python 预筛选（0 token，不消耗模型调用）。"""
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
    logger.info("[market_screener] Phase 1 完成，候选 %d 只", len(result.get("candidates", [])))
    return result


def deep_analyze(prescreen_result: Dict[str, Any]) -> Dict[str, Any]:
    """Phase 2: 逐只深入分析（消耗 token，调用工具）。"""
    strategy = prescreen_result.get("strategy", _select_strategy())
    _tool_calls: List[str] = []
    _tool_nodes: List = []
    _missing_data: List = []

    logger.info("[market_screener] Phase 2 深入分析，策略: %s", strategy)

    report = None
    if strategy == "intraday":
        from .intraday import deep_analyze as _deep, prescreen
        # 盘中策略需要重建 prescreen 结构
        candidates = prescreen_result.get("candidates", [])
        market = prescreen_result.get("market", {})
        main_themes = prescreen_result.get("main_themes", [])
        if market.get("mood_score", 50) < 30:
            from app.agent.chain.schema import FactorItem, SkillReport
            report = SkillReport(
                skill_name="market_screener", score=25.0, direction="bearish",
                confidence=0.7,
                signal=f"市场冰点（涨停{market.get('zt_count', 0)}跌停{market.get('dt_count', 0)}），不宜短线",
                analysis="", factors=[], status="ok",
            )
        elif not candidates:
            from app.agent.chain.schema import FactorItem, SkillReport
            report = SkillReport(
                skill_name="market_screener", score=45.0, direction="neutral",
                confidence=0.5, signal="今日无明确短线标的",
                analysis="", factors=[], status="ok",
            )
        else:
            from .intraday import tech_check
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
                output_data={"analyzed": analyzed},
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
            confidence=min(0.85, 0.4 + len(analyzed) * 0.07),
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
            confidence=min(0.85, 0.4 + len(analyzed) * 0.07),
            signal="", analysis="", factors=[], status="ok",
            output_data={"analyzed": analyzed},
        )

    if report is None:
        return {
            "skill": "market_screener", "status": "failed",
            "error": f"Phase 2 分析失败 (策略: {strategy})",
            "score": 0, "direction": "neutral", "confidence": 0,
            "signal": "", "analysis": "", "factors": [],
            "strategy_used": strategy, "tools_called": _tool_calls,
        }

    # 统一转 dict
    if hasattr(report, "to_dict"):
        d = report.to_dict()
    elif isinstance(report, dict):
        d = report
    else:
        d = {
            "score": getattr(report, "score", 50),
            "direction": getattr(report, "direction", "neutral"),
            "signal": getattr(report, "signal", ""),
            "analysis": str(getattr(report, "analysis", ""))[:2000],
            "status": getattr(report, "status", "ok"),
            "confidence": 0.5, "factors": [],
        }

    d.setdefault("skill", "market_screener")
    d["strategy_used"] = strategy
    d["tools_called"] = _tool_calls
    logger.info("[market_screener] Phase 2 完成，评分 %s", d.get("score"))
    return d

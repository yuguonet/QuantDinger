# -*- coding: utf-8 -*-
"""
market_screener/run.py — 工具函数入口

根据交易时间自动选择策略:
  - 09:25-10:00 → 关键窗口（集合竞价+开盘定基调）
  - 09:30-14:29 → intraday (盘中短线)
  - 14:30-15:00 → eod (尾盘隔夜)
  - 15:00+ / 非交易日 → post_market (盘后复盘)

仅包含对 LLM 暴露的工具函数，内部实现在 _helpers.py 及各策略模块中。
"""

from datetime import date
from typing import Any, Dict, List

from app.agent.log import logger
from skills.market_screener._helpers import (
    select_strategy, analyze_batch, build_report, resolve_names,
    filter_candidates as _filter_candidates,
)
from skills.market_screener.common import SkillReport, SkillResult


def pre_screen() -> Dict[str, Any]:
    """Phase 1: 获取候选股列表 + 市场状态。不做筛选，筛选由 agent 按 SKILL.md 规则执行。

    返回:
        strategy: 当前策略 (intraday/eod/post_market)
        market: 市场状态 (资金流向、涨跌停、板块强弱)
        candidates: 候选股列表 [{code, name, source, ...}]
        main_themes: 主线题材
    """
    strategy = select_strategy()
    today = date.today().isoformat()
    logger.info("[market_screener] Phase 1 获取候选，策略: %s", strategy)

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
        logger.warning("[market_screener] Phase 1 失败: %s", e)
        return SkillResult({"strategy": strategy, "error": str(e), "candidates": [], "main_themes": [], "market": {}})

    result["strategy"] = strategy
    result.setdefault("main_themes", [])
    result.setdefault("candidates", [])
    result.setdefault("market", {})
    return SkillResult(result)


def deep_analyze(codes: str) -> Dict[str, Any]:
    """Phase 2: 对指定股票做深入技术分析。

    参数:
        codes: 逗号分隔的股票代码，如 "000001,000002,600519"

    返回:
        score: 综合评分 0-100
        direction: bullish/bearish/neutral
        signal: 信号摘要
        analyzed: 逐只分析结果 [{code, name, score, direction, signals, ...}]
    """
    strategy = select_strategy()
    _tool_calls = []
    _tool_nodes = []
    _missing_data = []

    code_list = [c.strip() for c in codes.split(",") if c.strip()][:15]
    if not code_list:
        return SkillResult({
            "score": 45.0, "direction": "neutral", "confidence": 0.5,
            "signal": "无股票可分析",
            "analyzed": [], "strategy": strategy,
        })

    # 解析股票名称
    name_map = resolve_names(code_list)

    if strategy == "intraday":
        from .intraday import analyze_code as _deep
        raw = analyze_batch(
            [{"code": c, "name": name_map.get(c, "")} for c in code_list],
            lambda item: _deep(item["code"], item["name"], _tool_calls, _tool_nodes, _missing_data),
            max_candidates=15,
        )
    elif strategy == "eod":
        from .eod import analyze_code as _deep
        raw = analyze_batch(
            [{"code": c, "name": name_map.get(c, "")} for c in code_list],
            lambda item: _deep(item["code"], item["name"], _tool_calls, _tool_nodes, _missing_data),
            max_candidates=15,
        )
    else:
        from .post_market import analyze_code as _deep
        raw = analyze_batch(
            [{"code": c, "name": name_map.get(c, "")} for c in code_list],
            lambda item: _deep(item["code"], item["name"], _tool_calls, _tool_nodes, _missing_data),
            max_candidates=15,
        )

    report = build_report(raw)

    # 构建输出
    analyzed = []
    if hasattr(report, "output_data") and report.output_data:
        analyzed = report.output_data.get("analyzed", [])

    return SkillResult({
        "score": report.score,
        "direction": report.direction,
        "confidence": report.confidence,
        "signal": report.signal,
        "analyzed": analyzed,
        "strategy": strategy,
    })


def filter_candidates(prescreen_result: Dict) -> str:
    """根据 strategy + mood 筛选 candidates，返回逗号分隔的 codes 字符串。

    Args:
        prescreen_result: pre_screen() 的返回值

    Returns:
        逗号分隔的股票代码，如 "000001,600519,300750"
        如果没有符合条件的股票，返回空字符串 ""
    """
    return _filter_candidates(prescreen_result)





def run() -> Dict[str, Any]:
    """完整选股流程：Phase 1 + Phase 2。agent 通常不调此函数，按 SKILL.md 规则分步调用。"""
    prescreen_result = pre_screen()
    if prescreen_result.get("error"):
        return prescreen_result
    candidates = prescreen_result.get("candidates", [])
    codes = ",".join(c.get("code", "") for c in candidates if c.get("code"))
    return deep_analyze(codes)

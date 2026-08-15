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
        if strategy == "early":
            from .early import prescreen
            result = prescreen()
        elif strategy == "intraday":
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

    if strategy == "early":
        from .early import analyze_code as _deep
        raw = analyze_batch(
            [{"code": c, "name": name_map.get(c, "")} for c in code_list],
            lambda item: _deep(item["code"], item["name"], _tool_calls, _tool_nodes, _missing_data),
            max_candidates=15,
        )
    elif strategy == "intraday":
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

    # 按评分从高到低排序
    analyzed.sort(key=lambda x: -x.get("score", 0))

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





def run() -> str:
    """完整选股流程：pre_screen → filter → deep_analyze → 格式化 markdown。

    agent 只需调一次 run()，拿到 markdown 文本直接用 final_answer() 输出。
    不需要分步调用，不需要理解内部逻辑。

    Returns:
        格式化的 markdown 文本，可直接用于 final_answer()
    """
    # Phase 1: 获取候选
    prescreen_result = pre_screen()
    if prescreen_result.get("error"):
        return f"选股失败: {prescreen_result['error']}"

    strategy = prescreen_result.get("strategy", "")

    # Phase 2: 筛选
    codes = filter_candidates(prescreen_result)
    if not codes:
        return "当前无符合条件的股票，建议观望。"

    # Phase 3: 深入分析
    deep_result = deep_analyze(codes)

    # Phase 4: 格式化输出
    score = deep_result.get("score", 0)
    direction = deep_result.get("direction", "neutral")
    confidence = deep_result.get("confidence", 0)
    analyzed = deep_result.get("analyzed", [])

    direction_cn = {"bullish": "看多", "bearish": "看空", "neutral": "中性"}.get(direction, direction)

    lines = [
        f"**{strategy}**",
        f" 综合评分: {score}/100",
        f" 方    向: {direction_cn}",
        f" 置 信 度: {confidence}",
        "",
        "股票代码\t股票名称\t评分\t方向\t置信度\t压力位\t支撑位\t上空间\t下空间\t信号",
    ]
    for a in analyzed:
        a_dir = {"bullish": "看多", "bearish": "看空", "neutral": "中性"}.get(a.get("direction", ""), a.get("direction", ""))
        levels = a.get("levels", {})
        lines.append(
            f"{a.get('code','')}\t{a.get('name','')}\t{a.get('score',0)}\t"
            f"{a_dir}\t{a.get('confidence',0)}\t"
            f"{levels.get('resistance', '-')}\t{levels.get('support', '-')}\t"
            f"{levels.get('upside_pct', '-')}%\t{levels.get('downside_pct', '-')}%\t"
            f"{a.get('signal','')}"
        )
    return "\n".join(lines)

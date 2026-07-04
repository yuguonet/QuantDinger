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
from typing import Any, Dict

from app.agent.log import logger
from skills.market_screener._helpers import select_strategy, analyze_batch, build_report
from skills.market_screener.common import SkillReport


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
    strategy = select_strategy()
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
        return {"strategy": strategy, "error": str(e), "candidates": [], "main_themes": []}

    result["strategy"] = strategy
    result.setdefault("main_themes", [])
    result.setdefault("candidates", [])
    return result


def deep_analyze(prescreen_result: Dict[str, Any]) -> Dict[str, Any]:
    """Phase 2: 对 Phase 1 候选股做深入分析。

    参数：
        prescreen_result: pre_screen() 的返回值

    流程：
    - 盘中策略：逐只调用工具（资金流向+多周期分析）→ 评分排序
    - 尾盘策略：条件搜索+尾盘特征验证
    - 盘后策略：全市场技术形态扫描+介入点计算
    """
    strategy = prescreen_result.get("strategy", select_strategy())
    _tool_calls = []
    _tool_nodes = []
    _missing_data = []

    if strategy == "intraday":
        from .intraday import deep_analyze as _deep, tech_check
        candidates = prescreen_result.get("candidates", [])
        main_themes = prescreen_result.get("main_themes", [])

        if not candidates:
            report = SkillReport(
                skill_name="market_screener", score=45.0, direction="neutral",
                confidence=0.5, signal="今日无明确短线标的",
                analysis="", factors=[], status="ok",
                output_data={"analyzed": []},
            )
        else:
            raw = analyze_batch(
                candidates,
                lambda c: _deep(c, tech_check(c["code"]), _tool_calls, _tool_nodes, _missing_data),
                max_candidates=8,
            )
            report = build_report(raw)
    elif strategy == "eod":
        from .eod import deep_analyze as _deep
        candidates = prescreen_result.get("candidates", [])
        raw = analyze_batch(
            candidates,
            lambda c: _deep(c, _tool_calls, _tool_nodes, _missing_data),
        )
        report = build_report(raw)
    else:
        from .post_market import deep_analyze as _deep
        candidates = prescreen_result.get("candidates", [])
        raw = analyze_batch(
            candidates,
            lambda c: _deep(c, _tool_calls, _tool_nodes, _missing_data),
        )
        report = build_report(raw)

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
    output["strategy"] = strategy
    output["strategy_used"] = strategy
    output["tools_called"] = _tool_calls
    output["missing_data"] = _missing_data
    # 确保 output_data 始终在输出中（to_dict() 可能不含此字段）
    _od = getattr(report, "output_data", None)
    if _od:
        output["output_data"] = _od
    elif "output_data" not in output:
        output["output_data"] = {}

    return output


def run() -> Dict[str, Any]:
    """完整选股流程：Phase 1 + Phase 2。"""
    prescreen_result = pre_screen()
    if prescreen_result.get("error"):
        return prescreen_result
    return deep_analyze(prescreen_result)

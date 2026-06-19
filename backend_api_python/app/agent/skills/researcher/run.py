# -*- coding: utf-8 -*-
"""
researcher — 多空研究员。

同时构建多头和空头论据，输出标准化 SkillReport 格式。
评分规则定义在 SKILL.md，本文件是实现。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def run(stock_code: str, stock_name: str = "", context: dict = None) -> Dict[str, Any]:
    """执行多空研究分析。

    Returns:
        {
            "skill": "researcher",
            "score": float,          # 0-100 综合评分
            "direction": str,        # bullish / bearish / neutral
            "confidence": float,     # 0.0-1.0
            "signal": str,           # 信号摘要
            "factors": list,         # 因子明细
            "analysis": str,         # 分析文字
            "bull_case": dict,       # 多头论据
            "bear_case": dict,       # 空头论据
            "verdict": str,          # 综合判断
            "status": "ok",
        }
    """
    from app.agent.tools.analysis_tools import (
        get_realtime_quote, analyze_trend, get_volume_analysis,
        get_indicator_snapshot,
    )
    from app.agent.tools.news_search_tools import search_stock_intel

    # ── 获取数据 ──
    data = {}
    for name, fn in [
        ("realtime_quote", lambda: get_realtime_quote(stock_code)),
        ("trend", lambda: analyze_trend(stock_code)),
        ("volume", lambda: get_volume_analysis(stock_code)),
        ("indicator", lambda: get_indicator_snapshot(stock_code)),
        ("intel", lambda: search_stock_intel(stock_code, stock_name or "")),
    ]:
        try:
            data[name] = fn()
        except Exception as e:
            data[name] = {"error": str(e)}

    # ── 构建多头论据 ──
    bull_factors = []
    bull_score = 50
    bull_signals = []

    trend = data.get("trend", {})
    if isinstance(trend, dict) and "error" not in trend:
        trend_score = trend.get("trend_score", 50)
        if trend_score >= 60:
            bull_factors.append(f"趋势偏多({trend_score})")
            bull_signals.append(f"趋势:{trend_score}")
        bull_score = trend_score

    indicator = data.get("indicator", {})
    if isinstance(indicator, dict) and "error" not in indicator:
        rsi = indicator.get("rsi6", 50)
        macd_hist = indicator.get("macd_hist", 0)
        if rsi < 30:
            bull_factors.append(f"RSI{rsi:.0f}超卖")
            bull_signals.append(f"RSI超卖:{rsi:.0f}")
        if macd_hist > 0:
            bull_factors.append("MACD多头")

    volume = data.get("volume", {})
    if isinstance(volume, dict) and "error" not in volume:
        vol_relation = volume.get("vol_price_relation", "")
        if "量价齐升" in vol_relation:
            bull_factors.append("量价齐升")
            bull_signals.append("量价齐升")

    intel = data.get("intel", {})
    if isinstance(intel, dict) and "error" not in intel:
        composite = intel.get("composite_score", 0)
        if composite > 1:
            bull_factors.append(f"情报偏多({composite:.1f})")

    # ── 构建空头论据 ──
    bear_factors = []
    bear_score = 50
    bear_signals = []

    if isinstance(trend, dict) and "error" not in trend:
        trend_score = trend.get("trend_score", 50)
        if trend_score <= 40:
            bear_factors.append(f"趋势偏空({trend_score})")
            bear_signals.append(f"趋势:{trend_score}")
        bear_score = 100 - trend_score

    if isinstance(indicator, dict) and "error" not in indicator:
        rsi = indicator.get("rsi6", 50)
        macd_hist = indicator.get("macd_hist", 0)
        if rsi >= 70:
            bear_factors.append(f"RSI{rsi:.0f}超买")
            bear_signals.append(f"RSI超买:{rsi:.0f}")
        if macd_hist < 0:
            bear_factors.append("MACD空头")

    if isinstance(volume, dict) and "error" not in volume:
        vol_relation = volume.get("vol_price_relation", "")
        if "放量下跌" in vol_relation:
            bear_factors.append("放量下跌")
            bear_signals.append("放量下跌")

    if isinstance(intel, dict) and "error" not in intel:
        composite = intel.get("composite_score", 0)
        if composite < -1:
            bear_factors.append(f"情报偏空({composite:.1f})")

    # ── 综合判断 ──
    # 多头得分 vs 空头得分
    bull_total = bull_score + len(bull_factors) * 5
    bear_total = bear_score + len(bear_factors) * 5

    if bull_total > bear_total + 10:
        verdict = "bullish"
        final_score = min(100, 50 + (bull_total - bear_total))
        direction = "bullish"
    elif bear_total > bull_total + 10:
        verdict = "bearish"
        final_score = max(0, 50 - (bear_total - bull_total))
        direction = "bearish"
    else:
        verdict = "neutral"
        final_score = 50
        direction = "neutral"

    confidence = min(1.0, max(0.3, (len(bull_factors) + len(bear_factors)) / 8))

    signal_parts = []
    if bull_signals:
        signal_parts.append(f"多:{','.join(bull_signals[:2])}")
    if bear_signals:
        signal_parts.append(f"空:{','.join(bear_signals[:2])}")
    signal = " | ".join(signal_parts) if signal_parts else "多空均衡"

    factors = []
    for f in bull_factors[:3]:
        factors.append({"name": f"多头:{f}", "score": bull_score, "direction": "bullish"})
    for f in bear_factors[:3]:
        factors.append({"name": f"空头:{f}", "score": bear_score, "direction": "bearish"})

    analysis = (
        f"多头论据({len(bull_factors)}项): {', '.join(bull_factors[:3]) or '无'}。"
        f"空头论据({len(bear_factors)}项): {', '.join(bear_factors[:3]) or '无'}。"
        f"综合判断: {verdict}"
    )

    return {
        "skill": "researcher",
        "score": final_score,
        "direction": direction,
        "confidence": confidence,
        "signal": signal,
        "factors": factors,
        "analysis": analysis,
        "status": "ok",
        "output_data": {
            "bull_case": {
                "score": bull_score,
                "factors": bull_factors,
                "signals": bull_signals,
            },
            "bear_case": {
                "score": bear_score,
                "factors": bear_factors,
                "signals": bear_signals,
            },
            "verdict": verdict,
            "data": data,
        },
    }

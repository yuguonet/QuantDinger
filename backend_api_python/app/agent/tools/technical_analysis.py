# -*- coding: utf-8 -*-
"""技术面综合分析 — 五维加权评分（趋势+指标+量价+形态+筹码），含流通盘修正。

本文件只暴露 technical_analysis() 作为标准 tool。
内部辅助函数（_call_tools / _algo_analyze）以下划线开头，不注册。
具体的分析工具（analyze_trend 等）在 analysis_tools.py 中定义。
"""
from __future__ import annotations

from app.agent.log import logger
from typing import Any, Dict, List
def _call_tools(stock_code: str) -> Dict[str, Any]:
    """调用 analysis_tools.py 中的分析工具 + basicinfo，返回结果字典。"""
    from app.utils.basicinfo_db import get_stock_basic_db
    from app.agent.tools.analysis_tools import (
        analyze_trend, get_indicator_snapshot, get_volume_analysis,
        analyze_pattern, get_chip_distribution,
    )
    from app.agent.tools.data_tools import get_realtime_quote

    results = {}
    for name, fn in [
        ("analyze_trend", lambda: analyze_trend(stock_code)),
        ("get_indicator_snapshot", lambda: get_indicator_snapshot(stock_code)),
        ("get_volume_analysis", lambda: get_volume_analysis(stock_code)),
        ("analyze_pattern", lambda: analyze_pattern(stock_code)),
        ("get_chip_distribution", lambda: get_chip_distribution(stock_code)),
        ("realtime_quote", lambda: get_realtime_quote(stock_code)),
    ]:
        try:
            results[name] = fn()
        except Exception as e:
            results[name] = {"error": str(e)}

    try:
        stock_db = get_stock_basic_db()
        info = stock_db.get_stock(stock_code)
        if info:
            results["basicinfo"] = info
    except Exception as e:
        results["basicinfo"] = {"error": str(e)}

    return results
def _algo_analyze(
    stock_code: str,
    tool_results: Dict[str, Any],
) -> dict:
    """纯算法技术面 + 动量分析。"""
    factors: List[dict] = []
    signals: List[str] = []

    # ── 1. 趋势评分（主权重 40%）──
    trend = tool_results.get("analyze_trend", {})
    trend_score = 50
    if isinstance(trend, dict) and "error" not in trend:
        trend_score = trend.get("trend_score", 50)
        trend_desc = trend.get("trend", "震荡")
        ma_align = trend.get("ma_alignment", "")
        bias_ma20 = trend.get("bias_ma20", 0)

        if bias_ma20 > 10:
            signals.append(f"偏离MA20达{bias_ma20:.1f}%，回调风险")
            trend_score = max(trend_score - 10, 0)
        elif bias_ma20 < -10:
            signals.append(f"偏离MA20达{bias_ma20:.1f}%，超跌反弹")

        if ma_align:
            signals.append(ma_align)

        factors.append({"name": "趋势", "value": trend_desc, "score": trend_score})
    else:
        factors.append({"name": "趋势", "value": "数据缺失", "score": 50})

    # ── 2. 动量指标（权重 25%）──
    indicator = tool_results.get("get_indicator_snapshot", {})
    indicator_score = 50
    if isinstance(indicator, dict) and "error" not in indicator:
        macd = indicator.get("macd", {})
        rsi = indicator.get("rsi", {})
        kdj = indicator.get("kdj", {})
        boll = indicator.get("boll", {})

        macd_signals = macd.get("signals", []) if isinstance(macd.get("signals"), list) else []
        rsi_value = rsi.get("rsi6", 50)
        kdj_j = kdj.get("j", 50)
        boll_pos = boll.get("position_pct", 50)

        macd_has_golden = any("金叉" in s for s in macd_signals)
        macd_has_death = any("死叉" in s for s in macd_signals)
        if macd_has_golden:
            indicator_score += 15
            signals.append("MACD金叉")
        elif macd_has_death:
            indicator_score -= 15
            signals.append("MACD死叉")

        if rsi_value < 30:
            indicator_score += 10
            signals.append(f"RSI超卖({rsi_value:.0f})")
        elif rsi_value > 70:
            indicator_score -= 10
            signals.append(f"RSI超买({rsi_value:.0f})")

        if kdj_j < 20:
            indicator_score += 5
        elif kdj_j > 80:
            indicator_score -= 5

        if isinstance(boll_pos, (int, float)):
            if boll_pos >= 80:
                indicator_score -= 5
            elif boll_pos <= 20:
                indicator_score += 5

        indicator_score = max(0, min(100, indicator_score))
        macd_label = "金叉" if macd_has_golden else ("死叉" if macd_has_death else "中性")
        factors.append({"name": "指标", "value": f"MACD:{macd_label} RSI:{rsi_value:.0f}", "score": indicator_score})
    else:
        factors.append({"name": "指标", "value": "数据缺失", "score": 50})

    # ── 3. 量价分析（权重 20%）──
    volume = tool_results.get("get_volume_analysis", {})
    volume_score = 50
    if isinstance(volume, dict) and "error" not in volume:
        vol_ratio = volume.get("volume_ratio", 1.0)
        turnover = volume.get("turnover_rate", 0)
        vol_trend = volume.get("volume_trend", "")

        if vol_ratio > 2.0:
            volume_score += 15
            signals.append(f"放量({vol_ratio:.1f}倍)")
        elif vol_ratio < 0.5:
            volume_score -= 10
            signals.append(f"缩量({vol_ratio:.1f}倍)")

        if turnover > 10:
            volume_score += 5
            signals.append(f"换手率{turnover:.1f}%")

        if vol_trend == "递增":
            volume_score += 5
        elif vol_trend == "递减":
            volume_score -= 5

        volume_score = max(0, min(100, volume_score))
        factors.append({"name": "量价", "value": f"量比{vol_ratio:.1f} 换手{turnover:.1f}%", "score": volume_score})
    else:
        factors.append({"name": "量价", "value": "数据缺失", "score": 50})

    # ── 4. 形态识别（权重 10%）──
    pattern = tool_results.get("analyze_pattern", {})
    pattern_score = 50
    if isinstance(pattern, dict) and "error" not in pattern:
        raw_patterns = pattern.get("patterns", [])
        if not isinstance(raw_patterns, list):
            raw_patterns = []
        # analyze_pattern 返回字符串列表，按关键词分看多/看空
        bullish_kw = ["底部反转", "看涨", "早晨之星", "三连阳", "红三兵", "蜻蜓线", "刺透", "上升三法"]
        bearish_kw = ["顶部反转", "看跌", "黄昏之星", "三连阴", "黑三鸦", "墓碑线", "乌云盖顶", "下降三法"]
        bullish_count = sum(1 for p in raw_patterns if any(k in p for k in bullish_kw))
        bearish_count = sum(1 for p in raw_patterns if any(k in p for k in bearish_kw))
        pattern_score = 50 + bullish_count * 10 - bearish_count * 10
        for p in raw_patterns[:3]:
            signals.append(f"形态:{p}")
        pattern_score = max(0, min(100, pattern_score))
        factors.append({"name": "形态", "value": f"{len(raw_patterns)}个形态", "score": pattern_score})
    else:
        factors.append({"name": "形态", "value": "数据缺失", "score": 50})

    # ── 5. 筹码分布（权重 5%）──
    chip = tool_results.get("get_chip_distribution", {})
    chip_score = 50
    if isinstance(chip, dict) and "error" not in chip:
        concentration = chip.get("concentration", 0)
        profit_ratio = chip.get("profit_ratio", 50)
        if profit_ratio > 80:
            chip_score -= 10
            signals.append(f"获利盘{profit_ratio:.0f}%，抛压风险")
        elif profit_ratio < 20:
            chip_score += 10
            signals.append(f"获利盘{profit_ratio:.0f}%，超跌")
        chip_score = max(0, min(100, chip_score))
        factors.append({"name": "筹码", "value": f"获利{profit_ratio:.0f}%", "score": chip_score})
    else:
        factors.append({"name": "筹码", "value": "数据缺失", "score": 50})

    # ── 流通盘修正 ──
    basicinfo = tool_results.get("basicinfo", {})
    float_shares = 0
    if isinstance(basicinfo, dict):
        float_shares = basicinfo.get("float_shares", 0) or basicinfo.get("circulating_shares", 0)

    float_score = 50
    if float_shares:
        if float_shares < 50000000:
            float_score = 65
            signals.append("小盘股(流通<5000万)")
        elif float_shares > 1000000000:
            float_score = 40
            signals.append("大盘股(流通>10亿)")
    factors.append({"name": "流通盘", "value": f"{float_shares/10000:.0f}万股" if float_shares else "未知", "score": float_score})

    # ── 综合加权评分 ──
    weights = {"趋势": 0.40, "指标": 0.25, "量价": 0.20, "形态": 0.10, "筹码": 0.05, "流通盘": 0.00}
    total_weight = 0
    weighted_score = 0
    for f in factors:
        w = weights.get(f["name"], 0.05)
        if f["score"] is not None and f["value"] != "数据缺失":
            weighted_score += f["score"] * w
            total_weight += w

    final_score = round(weighted_score / total_weight) if total_weight > 0 else 50
    final_score = max(0, min(100, final_score))

    # ── 方向判定 ──
    if final_score >= 60:
        direction = "bullish"
    elif final_score <= 40:
        direction = "bearish"
    else:
        direction = "neutral"

    # ── 置信度 ──
    valid_count = sum(1 for f in factors if f["value"] != "数据缺失")
    if valid_count >= 5:
        confidence = "high"
    elif valid_count >= 3:
        confidence = "medium"
    else:
        confidence = "low"

    # ── 信号摘要 ──
    signal = " | ".join(signals[:5]) if signals else "无明显信号"

    # ── markdown 分析 ──
    dir_map = {"bullish": "看多", "bearish": "看空", "neutral": "中性"}
    md = f"{stock_code}({stock_code}) {final_score:.0f}分 {dir_map.get(direction, direction)}"
    if factors:
        md += "\n" + " ".join(f"{f['name']}:{f['score']}" for f in factors[:4])
    if signals:
        md += "\n" + " ".join(signals[:3])
    analysis = md

    _r = {
        "score": final_score,
        "direction": direction,
        "confidence": confidence,
        "signal": signal,
        "factors": factors,
        "analysis": analysis,
        "stock_code": stock_code,
    }
    return _r
def technical_analysis(codes: str) -> dict:
    """技术面综合评分：内部调用 analyze_trend+get_indicator_snapshot+get_volume_analysis+analyze_pattern+get_chip_distribution，加权输出 0-100 分。需要单股深度分析时用此工具，不要同时调 analyze_trend。

    Args:
        codes: 股票代码（6位数字），如 "600519"

    Returns:
        dict: 标准化分析报告，包含 score(0-100)、direction(bullish/bearish/neutral)、
              confidence(high/medium/low)、signal(信号摘要)、factors(因子明细)、analysis(分析文字)
    """
    tool_results = _call_tools(codes)
    return _algo_analyze(codes, tool_results)

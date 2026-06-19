#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
technical-agent 入口脚本。

用法: python run.py <stock_code> [--name <stock_name>]
输出: JSON 格式的 SkillReport 到 stdout
"""
from __future__ import annotations

import argparse
import json
import sys
import os

# 确保能 import app 包
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from typing import Any, Dict, List


def call_tools(stock_code: str) -> Dict[str, Any]:
    """调用 6 个技术分析工具 + basicinfo，返回结果字典。"""
    from app.agent.tools.analysis_tools import (
        analyze_trend,
        get_indicator_snapshot,
        get_volume_analysis,
        analyze_pattern,
        get_chip_distribution,
    )
    from app.agent.tools.data_tools import get_realtime_quote
    from app.utils.basicinfo_db import get_stock_basic_db

    results = {}
    for name, fn in [
        ("analyze_trend", analyze_trend),
        ("get_indicator_snapshot", get_indicator_snapshot),
        ("get_volume_analysis", get_volume_analysis),
        ("analyze_pattern", analyze_pattern),
        ("get_chip_distribution", get_chip_distribution),
        ("realtime_quote", lambda: get_realtime_quote(stock_code)),
    ]:
        try:
            results[name] = fn()
        except Exception as e:
            results[name] = {"error": str(e)}

    # basicinfo_db 拉流通股本（比 realtime_quote 更靠谱）
    try:
        stock_db = get_stock_basic_db()
        info = stock_db.get_stock(stock_code)
        if info:
            results["basicinfo"] = info
    except Exception as e:
        results["basicinfo"] = {"error": str(e)}

    return results


def algo_analyze(
    stock_code: str,
    stock_name: str,
    tool_results: Dict[str, Any],
) -> dict:
    """纯算法技术面 + 动量分析（从 TechnicalSkill.algo_analyze 提取）。"""
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
    ind_score = 50
    if isinstance(indicator, dict) and "error" not in indicator:
        rsi_val = indicator.get("rsi6", 50)
        macd_hist = indicator.get("macd_hist", 0)
        kdj_j = indicator.get("kdj_j", 50)

        rsi_score = 50
        if rsi_val >= 80:
            rsi_score = 20
            signals.append(f"RSI{rsi_val:.0f}超买")
        elif rsi_val >= 70:
            rsi_score = 30
            signals.append(f"RSI{rsi_val:.0f}偏高")
        elif rsi_val <= 20:
            rsi_score = 80
            signals.append(f"RSI{rsi_val:.0f}超卖")
        elif rsi_val <= 30:
            rsi_score = 70
            signals.append(f"RSI{rsi_val:.0f}偏低")
        else:
            rsi_score = int(rsi_val)

        macd_score = 50
        if macd_hist > 0:
            macd_score = 70 if macd_hist > 0.5 else 60
        elif macd_hist < 0:
            macd_score = 30 if macd_hist < -0.5 else 40

        kdj_score = 50
        if kdj_j >= 80:
            kdj_score = 25
        elif kdj_j <= 20:
            kdj_score = 75

        ind_score = int(rsi_score * 0.35 + macd_score * 0.40 + kdj_score * 0.25)

        if macd_hist > 0 and rsi_val < 60:
            signals.append("MACD+RSI共振偏多")
        elif macd_hist < 0 and rsi_val > 40:
            signals.append("MACD+RSI共振偏空")

        factors.append({"name": "指标", "value": f"RSI{rsi_val:.0f}", "score": ind_score})
    else:
        factors.append({"name": "指标", "value": "数据缺失", "score": 50})

    # ── 3. 量价分析（权重 20%）──
    volume = tool_results.get("get_volume_analysis", {})
    vol_score = 50
    if isinstance(volume, dict) and "error" not in volume:
        vol_relation = volume.get("vol_price_relation", "")
        volume_ratio = volume.get("volume_ratio", 1.0)

        if "量价齐升" in vol_relation:
            vol_score = 80
        elif "缩量上涨" in vol_relation:
            vol_score = 45
        elif "放量下跌" in vol_relation:
            vol_score = 20
        elif "缩量下跌" in vol_relation:
            vol_score = 55
        elif "放量滞涨" in vol_relation:
            vol_score = 25

        if volume_ratio > 3.0:
            signals.append(f"量比{volume_ratio}异动")
        elif volume_ratio > 2.0:
            signals.append(f"量比{volume_ratio}放量")

        factors.append({"name": "量价", "value": vol_relation or "平量", "score": vol_score})
    else:
        factors.append({"name": "量价", "value": "数据缺失", "score": 50})

    # ── 4. 形态识别（权重 10%）──
    pattern = tool_results.get("analyze_pattern", {})
    pat_score = 50
    if isinstance(pattern, dict) and "error" not in pattern:
        patterns = pattern.get("patterns", [])
        if patterns:
            bullish_patterns = ["锤子线", "吞没", "早晨之星", "三连阳", "长下影线", "蜻蜓线", "突破", "大阳"]
            bearish_patterns = ["倒锤子", "墓碑线", "长上影线", "大阴线", "晚星", "三连阴", "跌破", "大阴"]

            for p in patterns:
                p_str = str(p)
                if any(bp in p_str for bp in bullish_patterns):
                    pat_score = max(pat_score, 70)
                    signals.append(p_str.split("（")[0])
                elif any(bp in p_str for bp in bearish_patterns):
                    pat_score = min(pat_score, 30)
                    signals.append(p_str.split("（")[0])

            factors.append({"name": "形态", "value": patterns[0].split("（")[0], "score": pat_score})
        else:
            factors.append({"name": "形态", "value": "无明显形态", "score": 50})
    else:
        factors.append({"name": "形态", "value": "数据缺失", "score": 50})

    # ── 5. 筹码分布（附加参考）──
    chip = tool_results.get("get_chip_distribution", {})
    data_missing = False
    if isinstance(chip, dict) and "error" not in chip:
        concentration = chip.get("concentration", "")
        if concentration:
            signals.append(f"筹码{concentration}")
    else:
        data_missing = True

    # ── 6. 流通盘分析（评分修正核心）──
    # 优先从 basicinfo_db 拿流通股本（靠谱），乘以当前价算流通市值
    float_mcap = 0.0  # 流通市值（亿元）
    float_size_label = ""
    float_size_tier = ""  # "micro"/"small"/"mid"/"large"/"mega"

    basicinfo = tool_results.get("basicinfo", {})
    quote = tool_results.get("realtime_quote", {})

    circ_shares = 0.0
    if isinstance(basicinfo, dict) and "error" not in basicinfo:
        circ_shares = float(basicinfo.get("circ_shares", 0) or 0)

    current_price = 0.0
    if isinstance(quote, dict) and "error" not in quote:
        current_price = float(quote.get("price", 0) or 0)

    if circ_shares > 0 and current_price > 0:
        # circ_shares 是股数，current_price 是元 → 亿元
        float_mcap = circ_shares * current_price / 1e8
    elif isinstance(quote, dict) and "error" not in quote:
        # fallback: realtime_quote 的 float_mcap_yi
        float_mcap = float(quote.get("float_mcap_yi", 0) or 0)

    if float_mcap > 0:
        if float_mcap < 30:
            float_size_tier = "micro"
            float_size_label = f"超小盘{float_mcap:.0f}亿"
        elif float_mcap < 100:
            float_size_tier = "small"
            float_size_label = f"小盘{float_mcap:.0f}亿"
        elif float_mcap < 500:
            float_size_tier = "mid"
            float_size_label = f"中盘{float_mcap:.0f}亿"
        elif float_mcap < 2000:
            float_size_tier = "large"
            float_size_label = f"大盘{float_mcap:.0f}亿"
        else:
            float_size_tier = "mega"
            float_size_label = f"超大盘{float_mcap:.0f}亿"
        signals.append(float_size_label)
    else:
        float_size_tier = "mid"  # 默认中盘

    # ── 流通盘对量价信号的修正 ──
    # 小盘股：量价信号可靠性低（易操纵），分数向50收缩
    # 大盘股：量价信号可靠性高（真实资金），分数保持
    _FLOAT_VOL_RELIABILITY = {
        "micro": 0.5,   # 超小盘：量价信号半信半疑
        "small": 0.7,   # 小盘：打七折
        "mid": 1.0,     # 中盘：正常
        "large": 1.1,   # 大盘：量价信号更可靠
        "mega": 1.2,    # 超大盘：机构行为，信号最可靠
    }
    vol_reliability = _FLOAT_VOL_RELIABILITY.get(float_size_tier, 1.0)
    # 量价分数向50收缩（小盘股信号不可靠时拉回中性）
    vol_score = int(50 + (vol_score - 50) * vol_reliability)
    vol_score = max(0, min(100, vol_score))

    # ── 流通盘对形态信号的修正 ──
    # 小盘股形态不可靠（主力画线），大盘股形态更真实
    _FLOAT_PAT_RELIABILITY = {
        "micro": 0.4,
        "small": 0.6,
        "mid": 1.0,
        "large": 1.1,
        "mega": 1.15,
    }
    pat_reliability = _FLOAT_PAT_RELIABILITY.get(float_size_tier, 1.0)
    pat_score = int(50 + (pat_score - 50) * pat_reliability)
    pat_score = max(0, min(100, pat_score))

    # ── 流通盘对趋势信号的修正 ──
    # 大盘股趋势更稳定，小盘股趋势更容易反转
    _FLOAT_TREND_RELIABILITY = {
        "micro": 0.6,
        "small": 0.8,
        "mid": 1.0,
        "large": 1.05,
        "mega": 1.1,
    }
    trend_reliability = _FLOAT_TREND_RELIABILITY.get(float_size_tier, 1.0)
    trend_score = int(50 + (trend_score - 50) * trend_reliability)
    trend_score = max(0, min(100, trend_score))

    factors.append({"name": "流通盘", "value": float_size_label or "未知", "score": 50})

    # ── 综合评分 ──
    final_score = int(
        trend_score * 0.40 +
        ind_score * 0.25 +
        vol_score * 0.20 +
        pat_score * 0.10
    )
    if isinstance(chip, dict) and "error" not in chip:
        profit_ratio = chip.get("profit_ratio")
        if profit_ratio is not None:
            if profit_ratio < 20:
                final_score += 5
            elif profit_ratio > 80:
                final_score -= 5

    final_score = max(0, min(100, final_score))

    if final_score >= 60:
        direction = "bullish"
    elif final_score <= 40:
        direction = "bearish"
    else:
        direction = "neutral"

    valid_count = sum(1 for f in factors if "缺失" not in str(f.get("value", "")))
    confidence_val = round(min(valid_count / 5, 1.0), 2)
    confidence = "high" if confidence_val >= 0.7 else ("medium" if confidence_val >= 0.4 else "low")

    if final_score >= 80:
        momentum_rating = "极强"
    elif final_score >= 65:
        momentum_rating = "强"
    elif final_score >= 45:
        momentum_rating = "中性"
    elif final_score >= 30:
        momentum_rating = "弱"
    else:
        momentum_rating = "极弱"

    signal_text = ",".join(str(s) for s in signals[:5]) if signals else "无明显信号"

    return {
        "skill": "technical_agent",
        "action": "buy" if final_score >= 60 else ("sell" if final_score <= 40 else "hold"),
        "score": final_score,
        "direction": direction,
        "confidence": confidence,
        "signal": signal_text,
        "factors": factors,
        "analysis": f"动量评级:{momentum_rating} 综合评分:{final_score}/100。"
                    f"趋势:{trend_score} 动量:{ind_score} 量价:{vol_score} 形态:{pat_score}"
                    f" 流通盘:{float_size_label or '未知'}",
        "status": "ok",
        "data_missing": data_missing,
        "float_mcap_yi": float_mcap,
        "float_size_tier": float_size_tier,
    }


def run(stock_code: str, stock_name: str = "", context: dict = None) -> dict:
    """薄壳入口：调用工具 + 算法分析，返回 dict。"""
    tool_results = call_tools(stock_code)
    return algo_analyze(stock_code, stock_name, tool_results)


def main():
    parser = argparse.ArgumentParser(description="A股技术面综合分析")
    parser.add_argument("stock_code", help="股票代码，如 600519")
    parser.add_argument("--name", default="", help="股票名称")
    args = parser.parse_args()

    tool_results = call_tools(args.stock_code)
    result = algo_analyze(args.stock_code, args.name, tool_results)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

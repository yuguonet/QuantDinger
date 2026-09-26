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
    """调用 analysis_tools 中的分析算法（同包内规范实现，非工具包装层）+ basicinfo，返回结果字典。

    说明：analyze_trend 等是 tools/finance 包内的规范实现本身（其底层 _fetch_ohlcv/_calc_* 在
    _analysis_utils），这里同包复用属正常库调用、二者都会被扫描注册，不构成"外部层偷用工具包装层"。
    唯一被替换为底层调用的是实时行情：get_realtime_quote 工具即 _get_ds().get_tickers 的封装，
    已改为直接走数据源，不再 import 工具包装层。
    """
    from app.utils.basicinfo_db import get_stock_basic_db
    from app.agent.tools.finance.analysis_tools import (
        analyze_trend, get_indicator_snapshot, get_volume_analysis,
        analyze_pattern, get_chip_distribution,
    )
    from app.agent.tools.finance._analysis_utils import _get_ds

    results = {}
    for name, fn in [
        ("analyze_trend", lambda: analyze_trend(stock_code)),
        ("get_indicator_snapshot", lambda: get_indicator_snapshot(stock_code)),
        ("get_volume_analysis", lambda: get_volume_analysis(stock_code)),
        ("analyze_pattern", lambda: analyze_pattern(stock_code)),
        ("get_chip_distribution", lambda: get_chip_distribution(stock_code)),
    ]:
        try:
            results[name] = fn()
        except Exception as e:
            results[name] = {"error": str(e)}

    # 实时行情：直接走底层数据源（get_realtime_quote 工具即此封装），避免 import 工具包装层
    try:
        ds = _get_ds("CNStock")
        tickers = ds.get_tickers([stock_code]) or []
        ticker_map = {t.get("symbol"): t for t in tickers}
        t = ticker_map.get(stock_code) or (tickers[0] if tickers else None)
        results["realtime_quote"] = (
            {"stock_code": stock_code, "market": "CNStock", **t} if t else {}
        )
    except Exception as e:
        results["realtime_quote"] = {"error": str(e)}

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
    # A1a 因子权重接线（2026-09-26）：优先从 qd_agent_weights 取校准权重（按 (skill,factor)
    # 双键的时间衰减准确率），无数据则回退启发式常量并显式标 calibrated=false。
    # 禁止静默假装修准过——缺口显式 > 硬凑数字（项目红线）。
    _DEFAULT_WEIGHTS = {"趋势": 0.40, "指标": 0.25, "量价": 0.20,
                        "形态": 0.10, "筹码": 0.05, "流通盘": 0.00}
    try:
        from chain.store import get_factor_weights
        _calibrated = get_factor_weights("technical_analysis")
    except Exception as _e:
        logger.debug("[technical_analysis] 因子权重读取失败，回退常量: %s", _e)
        _calibrated = {}
    if _calibrated:
        weights = _calibrated
        calibrated = True
    else:
        weights = _DEFAULT_WEIGHTS
        calibrated = False
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

    # ── A1c 校准命中率（score→P(方向正确)）──
    # 有校准数据用 calibration.apply，无则 None（输出标 n/a）。
    # 置信度优先用 hit_rate 区间映射（>0.6 high / 0.4-0.6 medium / <0.4 low），
    # 无校准则回退原"有效因子数"逻辑。
    try:
        from utils.calibration import apply as _cal_apply
        hit_rate = _cal_apply("technical_analysis", final_score)
    except Exception:
        hit_rate = None

    # ── 置信度 ──
    valid_count = sum(1 for f in factors if f["value"] != "数据缺失")
    if hit_rate is not None:
        if hit_rate >= 0.6:
            confidence = "high"
        elif hit_rate >= 0.4:
            confidence = "medium"
        else:
            confidence = "low"
    elif valid_count >= 5:
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
    if hit_rate is not None:
        md += f" 历史命中率{hit_rate*100:.0f}%"
    if factors:
        md += "\n" + " ".join(f"{f['name']}:{f['score']}" for f in factors[:4])
    if signals:
        md += "\n" + " ".join(signals[:3])
    analysis = md

    # ── 透传原始数据（供 stock_report 计算支撑/压力位）──
    trend_raw = tool_results.get("analyze_trend", {})
    indicator_raw = tool_results.get("get_indicator_snapshot", {})

    # ── A3 缺数据声明（缺口显式 > 硬凑数字）──
    # 复用各因子块已有的 "数据缺失" 标记，统一收集到 missing_data 供下游识别。
    missing_data = [f["name"] for f in factors if f.get("value") == "数据缺失"]

    # ── A4 可证伪条件（若 X 发生则本判断失效）──
    # 基于实际 MA/RSI 数据生成，数据缺失时降级为 score 阈值条件。
    ma5 = trend_raw.get("ma5") if isinstance(trend_raw, dict) else None
    ma20 = trend_raw.get("ma20") if isinstance(trend_raw, dict) else None
    rsi_val = None
    if isinstance(indicator_raw, dict):
        rsi_dict = indicator_raw.get("rsi", {})
        if isinstance(rsi_dict, dict):
            rsi_val = rsi_dict.get("rsi") or rsi_dict.get("value")
    falsifiable = []
    if direction == "bullish":
        if ma5 and ma20:
            falsifiable.append(f"MA5({ma5:.2f})跌破MA20({ma20:.2f})")
        if isinstance(rsi_val, (int, float)):
            falsifiable.append(f"RSI({rsi_val:.0f})跌破30")
        falsifiable.append(f"综合分({final_score:.0f})跌破55")
    elif direction == "bearish":
        if ma5 and ma20:
            falsifiable.append(f"MA5({ma5:.2f})突破MA20({ma20:.2f})")
        if isinstance(rsi_val, (int, float)):
            falsifiable.append(f"RSI({rsi_val:.0f})突破70")
        falsifiable.append(f"综合分({final_score:.0f})突破45")
    else:
        falsifiable.append(f"综合分({final_score:.0f})突破60或跌破40")
    falsifiable_conditions = "；".join(falsifiable) if falsifiable else "数据不足，无法生成可证伪条件"

    _r = {
        "score": final_score,
        "direction": direction,
        "confidence": confidence,
        "signal": signal,
        "factors": factors,
        "analysis": analysis,
        "stock_code": stock_code,
        # A1a：权重是否来自 qd_agent_weights 校准
        "calibrated": calibrated,
        # A1c：score→P(方向正确)，无校准数据时为 None
        "hit_rate": hit_rate,
        # A3：缺失的因子名列表（空列表表示数据完整）
        "missing_data": missing_data,
        # A4：可证伪条件（若发生则本判断失效）
        "falsifiable_conditions": falsifiable_conditions,
        # 原始数据透传
        "latest_close": trend_raw.get("latest_close", 0) if isinstance(trend_raw, dict) else 0,
        "boll": trend_raw.get("boll", {}) if isinstance(trend_raw, dict) else {},
        "ma20": trend_raw.get("ma20") if isinstance(trend_raw, dict) else None,
        "ma60": trend_raw.get("ma60") if isinstance(trend_raw, dict) else None,
        "bias_ma20": trend_raw.get("bias_ma20") if isinstance(trend_raw, dict) else None,
        "rsi": indicator_raw.get("rsi", {}) if isinstance(indicator_raw, dict) else {},
    }
    return _r
def technical_analysis(codes: str) -> dict:
    """技术面综合评分：内部调用 analyze_trend+get_indicator_snapshot+get_volume_analysis+analyze_pattern+get_chip_distribution，加权输出 0-100 分。需要单股深度分析时用此工具，不要同时调 analyze_trend。

    Args:
        codes: 股票代码（6位数字），如 "600519"

    Returns:
        dict: 标准化分析报告，键包括:
              score(0-100)、direction(bullish/bearish/neutral)、confidence(high/medium/low)、
              signal(信号摘要)、analysis(分析文字)、stock_code，
              以及透传原始数据 latest_close/boll/ma20/ma60/bias_ma20/rsi。
              ⚠️ factors 是【列表】，不是字典！每个元素是 {"name","value","score"} 三键字典，
                 因子名在 name 字段里（取值如 "趋势"/"指标"/"量价"/"形态"/"筹码"/"流通盘"），
                 没有 "MA"/"MACD"/"RSI"/"KDJ" 这种顶层键。
              正确取用法:
                  factors = r["factors"]                        # list
                  by_name = {f["name"]: f["value"] for f in factors}
                  # by_name.get("指标") -> "MACD:中性 RSI:72"
                  # by_name.get("趋势") -> "强烈看空"
              切勿写成 r["factors"].get("MA", "N/A")（list 无 .get，会报 InterpreterError）。
    """
    tool_results = _call_tools(codes)
    return _algo_analyze(codes, tool_results)

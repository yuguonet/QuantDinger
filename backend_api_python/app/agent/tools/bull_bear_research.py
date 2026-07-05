# -*- coding: utf-8 -*-
"""多空研究员 — 同时构建多头和空头论据，综合判断方向。"""
from typing import Any, Dict, List
def bull_bear_research(stock_code: str, stock_name: str = "", _output: str = "markdown") -> str:
    """多空研究：对单只股票做技术面+筹码+情报综合分析，返回多空评分和方向判断。

    Args:
        stock_code: 股票代码，如 "600066"
        stock_name: 股票名称，可选

    Returns:
        {
            
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
        _output: "markdown" (默认) | "json"
    """
        
    # ── 获取数据 ──
    data = {}
    for name, fn in [
        ("realtime_quote", lambda: get_realtime_quote(stock_code)),
        ("trend", lambda: _analyze_trend(stock_code)),
        ("volume", lambda: _get_volume_analysis(stock_code)),
        ("indicator", lambda: _get_indicator_snapshot(stock_code)),
        ("intel", lambda: _search_stock_intel(stock_code, stock_name or "")),
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

    dir_map = {"bullish": "看多", "bearish": "看空", "neutral": "中性"}
    all_signals = (bull_signals or []) + (bear_signals or [])
    md = f"多空分析 {final_score:.0f}分 {dir_map.get(direction, direction)}"
    if factors:
        md += "\n" + " ".join(f"{f['name']}:{f['score']}" for f in factors[:4])
    if all_signals:
        md += "\n" + " ".join(all_signals[:3])
    md += f"\n{verdict}"
    analysis = md

    _r = {
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
    return analysis if _output == "markdown" else _r
# ── 内联自 analysis_tools.py ──

def _analyze_trend(codes: str) -> Dict[str, Any]:
    """技术趋势（内部复用）：同 analysis_tools.analyze_trend，仅供 bull_bear_research 内部调用，外部请用 analyze_trend。

    Args:
        codes: 多股用逗号分隔"
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()][:20]
    if not code_list:
        return {"error": "codes 不能为空", "retriable": False}

    def _one(stock_code: str) -> Dict[str, Any]:
        try:
            data = _fetch_ohlcv(stock_code, 120)
            closes = data["close"]
            if len(closes) < 5:
                return {"error": "K线数据不足（至少需要5根）", "retriable": True}

            latest = closes[-1]
            prev = closes[-2] if len(closes) >= 2 else latest

            # ── 均线 ──
            ma5 = _safe_round(sum(closes[-5:]) / 5)
            ma10 = _safe_round(sum(closes[-10:]) / 10) if len(closes) >= 10 else ma5
            ma20 = _safe_round(sum(closes[-20:]) / 20) if len(closes) >= 20 else ma10
            ma60 = _safe_round(sum(closes[-60:]) / 60) if len(closes) >= 60 else _safe_round(sum(closes) / len(closes))
            ma120 = _safe_round(sum(closes[-120:]) / 120) if len(closes) >= 120 else ma60

            # 均线排列评分（0-100）
            ma_score = 50
            if ma5 > ma10 > ma20 > ma60:
                ma_desc = "强多头排列"
                ma_score = 90
            elif ma5 > ma10 > ma20:
                ma_desc = "多头排列"
                ma_score = 75
            elif ma5 > ma20:
                ma_desc = "弱势多头"
                ma_score = 60
            elif ma5 < ma10 < ma20 < ma60:
                ma_desc = "强空头排列"
                ma_score = 10
            elif ma5 < ma10 < ma20:
                ma_desc = "空头排列"
                ma_score = 25
            elif ma5 < ma20:
                ma_desc = "弱势空头"
                ma_score = 40
            else:
                ma_desc = "均线缠绕/震荡"
                ma_score = 50

            # 乖离率
            bias_ma5 = _safe_round((latest - ma5) / ma5 * 100, 2) if ma5 else 0
            bias_ma20 = _safe_round((latest - ma20) / ma20 * 100, 2) if ma20 else 0

            # ── MACD ──
            macd_result = _calc_macd(closes)
            macd_score = 50
            if isinstance(macd_result, dict) and "error" not in macd_result:
                if macd_result.get("dif", 0) > macd_result.get("dea", 0):
                    macd_score = 70 if macd_result.get("macd", 0) > 0 else 60
                else:
                    macd_score = 30 if macd_result.get("macd", 0) < 0 else 40
                if "金叉" in str(macd_result.get("signals", [])):
                    macd_score += 15
                elif "死叉" in str(macd_result.get("signals", [])):
                    macd_score -= 15

            # ── RSI ──
            rsi_result = _calc_rsi(closes, [6, 12, 24])
            rsi_score = 50
            if isinstance(rsi_result, dict) and "error" not in rsi_result:
                rsi6 = rsi_result.get("rsi6", 50)
                if rsi6 >= 70:
                    rsi_score = 25
                elif rsi6 <= 30:
                    rsi_score = 75
                else:
                    rsi_score = int(rsi6)

            # ── 布林带 ──
            boll_result = _calc_boll(closes)
            boll_score = 50
            if isinstance(boll_result, dict) and "error" not in boll_result:
                pos = boll_result.get("position_pct", 50)
                if pos >= 80:
                    boll_score = 30
                elif pos <= 20:
                    boll_score = 70
                else:
                    boll_score = int(50 + (50 - pos) * 0.4)

            # ── KDJ ──
            kdj_result = _calc_kdj(data["high"], data["low"], closes)
            kdj_score = 50
            if isinstance(kdj_result, dict) and "error" not in kdj_result:
                j = kdj_result.get("j", 50)
                if j >= 100:
                    kdj_score = 25
                elif j <= 0:
                    kdj_score = 75
                elif "金叉" in str(kdj_result.get("signals", [])):
                    kdj_score = 70
                elif "死叉" in str(kdj_result.get("signals", [])):
                    kdj_score = 30

            # ── 综合评分 ──
            total_score = int(
                ma_score * 0.30 +
                macd_score * 0.25 +
                rsi_score * 0.20 +
                boll_score * 0.15 +
                kdj_score * 0.10
            )
            total_score = max(0, min(100, total_score))

            if total_score >= 75:
                overall = "看多"
                buy_signal = "多指标共振看多"
                signal_score = total_score
            elif total_score >= 60:
                overall = "偏多"
                buy_signal = "偏多但需确认"
                signal_score = total_score
            elif total_score <= 25:
                overall = "看空"
                buy_signal = "多指标共振看空，建议回避"
                signal_score = total_score
            elif total_score <= 40:
                overall = "偏空"
                buy_signal = "偏空，观望为主"
                signal_score = total_score
            else:
                overall = "震荡"
                buy_signal = "多空分歧，观望"
                signal_score = total_score

            all_signals = []
            if isinstance(macd_result, dict):
                all_signals.extend(macd_result.get("signals", []))
            if isinstance(rsi_result, dict):
                all_signals.extend(rsi_result.get("signals", []))
            if isinstance(boll_result, dict):
                all_signals.extend(boll_result.get("signals", []))
            if isinstance(kdj_result, dict):
                all_signals.extend(kdj_result.get("signals", []))

            change_pct = _safe_round((latest - prev) / prev * 100, 2) if prev else 0

            return {
                "stock_code": stock_code,
                "latest_close": _safe_round(latest),
                "change_pct": change_pct,
                "ma5": ma5, "ma10": ma10, "ma20": ma20, "ma60": ma60, "ma120": ma120,
                "ma_alignment": ma_desc,
                "bias_ma5": bias_ma5, "bias_ma20": bias_ma20,
                "macd": macd_result,
                "rsi": rsi_result,
                "boll": boll_result,
                "kdj": kdj_result,
                "trend_score": total_score,
                "trend": overall,
                "buy_signal": buy_signal,
                "signal_score": signal_score,
                "all_signals": all_signals,
                "data_points": len(closes),
            }
        except Exception as e:
            logger.error("_analyze_trend(%s) failed: %s", stock_code, e)
            return {"error": str(e)}

    if len(code_list) == 1:
        return _one(code_list[0])

    results = {}
    for code in code_list:
        try:
            results[code] = _one(code)
        except Exception as e:
            results[code] = {"error": str(e)}
    return {"count": len(results), "data": results}

def _get_volume_analysis(codes: str) -> Dict[str, Any]:
    """量能分析：量比、换手率、成交量趋势。

    Args:
        codes: 多股用逗号分隔"
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()][:20]
    if not code_list:
        return {"error": "codes 不能为空", "retriable": False}

    def _one(stock_code: str) -> Dict[str, Any]:
        try:
            data = _fetch_ohlcv(stock_code, 30)
            volumes = data["volume"]
            closes = data["close"]
            if len(volumes) < 5:
                return {"error": "K线数据不足"}

            latest_vol = volumes[-1]
            avg_vol_5 = sum(volumes[-5:]) / 5
            avg_vol_20 = sum(volumes) / len(volumes)

            volume_ratio = _safe_round(latest_vol / avg_vol_5, 2) if avg_vol_5 else 0

            if volume_ratio > 2.0:
                status = "显著放量"
                meaning = "放量，需结合价格方向判断"
            elif volume_ratio > 1.5:
                status = "温和放量"
                meaning = "成交活跃度提升"
            elif volume_ratio < 0.5:
                status = "明显缩量"
                meaning = "缩量，抛压减轻或交投清淡"
            elif volume_ratio < 0.8:
                status = "温和缩量"
                meaning = "成交活跃度下降"
            else:
                status = "平量"
                meaning = "成交量维持常态"

            vol_trend = "数据不足"
            if len(volumes) >= 6:
                recent_avg = sum(volumes[-3:]) / 3
                earlier_avg = sum(volumes[-6:-3]) / 3
                vol_trend = "上升" if recent_avg > earlier_avg * 1.1 else ("下降" if recent_avg < earlier_avg * 0.9 else "平稳")

            vol_price_relation = "数据不足"
            if len(closes) >= 2 and latest_vol > 0:
                price_change = closes[-1] - closes[-2]
                if price_change > 0 and volume_ratio > 1.3:
                    vol_price_relation = "量价齐升（健康上涨）"
                elif price_change > 0 and volume_ratio < 0.7:
                    vol_price_relation = "缩量上涨（上涨乏力，需警惕）"
                elif price_change < 0 and volume_ratio > 1.3:
                    vol_price_relation = "放量下跌（恐慌抛售）"
                elif price_change < 0 and volume_ratio < 0.7:
                    vol_price_relation = "缩量下跌（下跌动能减弱）"
                else:
                    vol_price_relation = "量价配合一般"

            return {
                "stock_code": stock_code,
                "latest_volume": _safe_round(latest_vol, 0),
                "avg_volume_5d": _safe_round(avg_vol_5, 0),
                "avg_volume_20d": _safe_round(avg_vol_20, 0),
                "volume_ratio": volume_ratio,
                "volume_status": status,
                "volume_meaning": meaning,
                "volume_trend": vol_trend,
                "vol_price_relation": vol_price_relation,
            }
        except Exception as e:
            logger.error("_get_volume_analysis(%s) failed: %s", stock_code, e)
            return {"error": str(e)}

    if len(code_list) == 1:
        return _one(code_list[0])

    results = {}
    for code in code_list:
        try:
            results[code] = _one(code)
        except Exception as e:
            results[code] = {"error": str(e)}
    return {"count": len(results), "data": results}

def _analyze_pattern(codes: str) -> Dict[str, Any]:
    """识别K线形态（增强版）：锤子线、十字星、吞没、早晨/晚星、三连阳/阴、长上影/下影、缺口等。

    Args:
        codes: 多股用逗号分隔"
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()][:20]
    if not code_list:
        return {"error": "codes 不能为空", "retriable": False}

    def _one(stock_code: str) -> Dict[str, Any]:
        try:
            data = _fetch_ohlcv(stock_code, 10)
            if len(data["close"]) < 3:
                return {"error": "K线数据不足"}

            patterns = []
            o, h, l, c = data["open"], data["high"], data["low"], data["close"]
            n = len(c)

            lo, lh, ll, lc = o[-1], h[-1], l[-1], c[-1]
            body = abs(lc - lo)
            candle_range = lh - ll if lh > ll else 0.001
            upper_shadow = lh - max(lo, lc)
            lower_shadow = min(lo, lc) - ll

            if body > 0 and candle_range > 0:
                if lower_shadow >= 2 * body and upper_shadow <= body * 0.3:
                    patterns.append("锤子线（底部反转信号）")
                elif upper_shadow >= 2 * body and lower_shadow <= body * 0.3:
                    patterns.append("倒锤子线（顶部反转信号）")

            if body <= candle_range * 0.1:
                if upper_shadow > candle_range * 0.3 and lower_shadow > candle_range * 0.3:
                    patterns.append("长腿十字星（强烈犹豫信号）")
                elif upper_shadow > candle_range * 0.4 and lower_shadow < candle_range * 0.1:
                    patterns.append("墓碑线（顶部反转）")
                elif lower_shadow > candle_range * 0.4 and upper_shadow < candle_range * 0.1:
                    patterns.append("蜻蜓线（底部反转）")
                else:
                    patterns.append("十字星（犹豫信号）")

            if upper_shadow > body * 2 and upper_shadow > lower_shadow * 2:
                patterns.append("长上影线（上方压力大）")

            if lower_shadow > body * 2 and lower_shadow > upper_shadow * 2:
                patterns.append("长下影线（下方支撑强）")

            if body > candle_range * 0.85:
                if lc > lo:
                    patterns.append("光头光脚大阳线（强势）")
                else:
                    patterns.append("光头光脚大阴线（弱势）")

            if n >= 2:
                po, pc = o[-2], c[-2]
                prev_body = abs(pc - po)
                if prev_body > 0 and body > prev_body:
                    if pc < po and lc > lo and lo <= pc and lc >= po:
                        patterns.append("看涨吞没（底部反转）")
                    elif pc > po and lc < lo and lo >= pc and lc <= po:
                        patterns.append("看跌吞没（顶部反转）")

            if n >= 3:
                o1, c1 = o[-3], c[-3]
                o2, c2 = o[-2], c[-2]
                o3, c3 = o[-1], c[-1]
                body1 = abs(c1 - o1)
                body2 = abs(c2 - o2)
                body3 = abs(c3 - o3)

                if (c1 < o1 and body1 > 0 and
                        body2 < body1 * 0.3 and
                        c3 > o3 and body3 > 0 and
                        c3 > (o1 + c1) / 2):
                    patterns.append("早晨之星（底部反转，看多）")

                if (c1 > o1 and body1 > 0 and
                        body2 < body1 * 0.3 and
                        c3 < o3 and body3 > 0 and
                        c3 < (o1 + c1) / 2):
                    patterns.append("黄昏之星（顶部反转，看空）")

                if c1 > o1 and c2 > o2 and c3 > o3:
                    if body3 > body2 > body1:
                        patterns.append("三连阳递增（强势看多）")
                    else:
                        patterns.append("三连阳（多头排列）")

                if c1 < o1 and c2 < o2 and c3 < o3:
                    if body3 > body2 > body1:
                        patterns.append("三连阴递增（强势看空）")
                    else:
                        patterns.append("三连阴（空头排列）")

                if (c1 > o1 and c2 > o2 and c3 > o3 and
                        c2 > c1 and c3 > c2 and
                        o2 > o1 and o3 > o2):
                    patterns.append("红三兵（强烈看多）")

                if (c1 < o1 and c2 < o2 and c3 < o3 and
                        c2 < c1 and c3 < c2 and
                        o2 < o1 and o3 < o2):
                    patterns.append("黑三鸦（强烈看空）")

            if n >= 2:
                prev_high = h[-2]
                prev_low = l[-2]
                if l[-1] > prev_high:
                    gap_size = _safe_round((l[-1] - prev_high) / prev_high * 100, 2)
                    patterns.append(f"向上跳空缺口（幅度{gap_size}%）")
                elif h[-1] < prev_low:
                    gap_size = _safe_round((prev_low - h[-1]) / prev_low * 100, 2)
                    patterns.append(f"向下跳空缺口（幅度{gap_size}%）")

            if not patterns:
                patterns.append("无明显特殊形态")

            return {
                "stock_code": stock_code,
                "latest_candle": {"open": _safe_round(lo), "high": _safe_round(lh),
                                  "low": _safe_round(ll), "close": _safe_round(lc)},
                "body_size": _safe_round(body),
                "upper_shadow": _safe_round(upper_shadow),
                "lower_shadow": _safe_round(lower_shadow),
                "patterns": patterns,
                "pattern_count": len([p for p in patterns if "无明显" not in p]),
            }
        except Exception as e:
            logger.error("_analyze_pattern(%s) failed: %s", stock_code, e)
            return {"error": str(e)}

    if len(code_list) == 1:
        return _one(code_list[0])

    results = {}
    for code in code_list:
        try:
            results[code] = _one(code)
        except Exception as e:
            results[code] = {"error": str(e)}
    return {"count": len(results), "data": results}

def _get_chip_distribution(codes: str, lookback_days: int = 120) -> Dict[str, Any]:
    """筹码分布分析 — 委托给 chip_distribution.get_chip_distribution。"""
    from app.agent.tools.chip_distribution import get_chip_distribution
    return get_chip_distribution(codes, lookback_days=lookback_days, _output="json")

def _get_indicator_snapshot(codes: str) -> Dict[str, Any]:
    """单次获取多个技术指标快照（MACD、RSI、BOLL、KDJ等）。

    Args:
        codes: 多股用逗号分隔"
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()][:20]
    if not code_list:
        return {"error": "codes 不能为空", "retriable": False}

    def _one(stock_code: str) -> Dict[str, Any]:
        try:
            data = _fetch_ohlcv(stock_code, 120)
            closes = data["close"]
            if len(closes) < 10:
                return {"error": "K线数据不足（至少需要10根）", "retriable": True}

            result = {"stock_code": stock_code, "latest_close": _safe_round(closes[-1])}

            for p in [5, 10, 20, 60, 120]:
                if len(closes) >= p:
                    result[f"ma{p}"] = _safe_round(sum(closes[-p:]) / p)

            macd = _calc_macd(closes)
            if "error" not in macd:
                result["macd"] = macd

            rsi = _calc_rsi(closes, [6, 12, 24])
            if "error" not in rsi:
                result["rsi"] = rsi

            boll = _calc_boll(closes)
            if "error" not in boll:
                result["boll"] = boll

            kdj = _calc_kdj(data["high"], data["low"], closes)
            if "error" not in kdj:
                result["kdj"] = kdj

            if len(data["volume"]) >= 5:
                vol = data["volume"][-1]
                avg5 = sum(data["volume"][-5:]) / 5
                result["volume_ratio"] = _safe_round(vol / avg5, 2) if avg5 else 0

            return result
        except Exception as e:
            logger.error("_get_indicator_snapshot(%s) failed: %s", stock_code, e)
            return {"error": str(e)}

    if len(code_list) == 1:
        return _one(code_list[0])

    results = {}
    for code in code_list:
        try:
            results[code] = _one(code)
        except Exception as e:
            results[code] = {"error": str(e)}
    return {"count": len(results), "data": results}
# ── 内联自 news_search_tools.py ──

def _get_policy_from_cache() -> List[Dict[str, Any]]:
    """政策新闻: 只读 DB 缓存 (scheduler 每日写入)"""
    try:
        from app.services.news_search import get_news_cache_manager
        cached = get_news_cache_manager().get_items("POLICY", "CNStock")
        if not cached:
            return []
        return [
            {"title": r["title"], "link": r.get("url", ""),
             "snippet": r.get("snippet", "") if (r.get("sentiment_score") or 0) == -999 or abs(r.get("sentiment_score") or 0) >= 3 else "",
             "source": r.get("source", ""),
             "published": r.get("published_date", ""),
             "sentiment": r.get("sentiment", "neutral"),
             "sentiment_score": r.get("sentiment_score")}
            for r in cached
        ]
    except Exception as e:
        logger.warning("读取 POLICY 缓存失败: %s", e)
        return []

def _get_news(symbol: str, market: str = "CNStock", name: str = "") -> List[Dict[str, Any]]:
    """个股/板块新闻: 走 fetch_financial_news (缓存→搜索→写入)"""
    try:
        from app.services.news_search import fetch_financial_news
        resp = fetch_financial_news(lang="all", market=market, symbol=symbol, name=name)
        items = []
        for lang_key in ("cn", "en"):
            for it in resp.get(lang_key) or []:
                score = it.get("sentiment_score") or 0
                # 一票否决/强信号(|score|>=3)保留 snippet，中性/弱信号丢弃
                snippet = it.get("snippet", "") if score == -999 or abs(score) >= 3 else ""
                items.append({
                    "title": it.get("title", ""),
                    "link": it.get("link", ""),
                    "snippet": snippet,
                    "source": it.get("source", ""),
                    "published": it.get("published", ""),
                    "sentiment": it.get("sentiment", "neutral"),
                    "sentiment_score": score,
                })
        return items
    except Exception as e:
        logger.warning("获取新闻失败 %s(%s): %s", symbol, market, e)
        return []

def _build_result(items: List[Dict[str, Any]], label: str) -> Dict[str, Any]:
    """评分 + 排序: 一票否决置顶, 合计≤20条"""
    from app.services.news_analysis import composite_score

    articles = [
        {"score": it.get("sentiment_score") or 0.0,
         "published_date": it.get("published", "")}
        for it in items
    ]
    score_info = composite_score(articles) if articles else {}

    veto = score_info.get("veto", False)
    veto_article = score_info.get("veto_article")

    # 分离一票否决 vs 正常
    veto_items, normal_items = [], []
    for it in items:
        sc = it.get("sentiment_score")
        if sc == -999:
            veto_items.append({**it, "_veto": True})
        else:
            normal_items.append(it)

    # 正常按时间倒序
    normal_items.sort(key=lambda x: x.get("published", ""), reverse=True)

    # 合并: 一票否决置顶, 合计≤20
    merged = veto_items + normal_items
    merged = merged[:20]

    return {
        "label": label,
        "composite_score": score_info.get("composite_score", 0),
        "direction": score_info.get("direction", "中性"),
        "veto": veto,
        "veto_article": veto_article,
        "count": len(merged),
        "news": merged,
    }

def _search_stock_intel(codes: str, name: str = "") -> Dict[str, Any]:
    """个股情报搜索：返回指定股票的新闻、公告、研报列表及摘要。

    Args:
        codes: 多股用逗号分隔"
        name: 股票名称，如 "贵州茅台"
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()][:20]
    if not code_list:
        return {"error": "codes 不能为空", "retriable": False}

    def _one(stock_code: str) -> Dict[str, Any]:
        items = _get_news(stock_code, "CNStock", name)
        return _build_result(items, f"个股:{stock_code}")

    if len(code_list) == 1:
        return _one(code_list[0])

    results = {}
    for code in code_list:
        try:
            results[code] = _one(code)
        except Exception as e:
            results[code] = {"error": str(e)}
    return {"count": len(results), "data": results}

def _search_policy_intel(market: str = "CNStock") -> Dict[str, Any]:
    """政策情报搜索：返回最新财经政策、监管动态。

    Args:
        market: 市场或政策关键词
    """
    items = _get_policy_from_cache()
    return _build_result(items, f"政策:{market}")

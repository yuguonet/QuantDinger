# -*- coding: utf-8 -*-
"""技术面综合分析 — 五维加权评分（趋势+指标+量价+形态+筹码），含流通盘修正。"""
from __future__ import annotations

from typing import Any, Dict, List


def call_tools(stock_code: str) -> Dict[str, Any]:
    """调用 6 个技术分析工具 + basicinfo，返回结果字典。"""
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


def technical_analysis(stock_code: str, stock_name: str = "") -> dict:
    """技术面综合分析。五维加权评分（趋势40%+动量25%+量价20%+形态10%+筹码5%），含流通盘修正。

    Args:
        stock_code: 股票代码（6位数字），如 "600519"
        stock_name: 股票名称（可选），如 "贵州茅台"

    Returns:
        dict: 标准化分析报告，包含 score(0-100)、direction(bullish/bearish/neutral)、
              confidence(high/medium/low)、signal(信号摘要)、factors(因子明细)、analysis(分析文字)
    """
    tool_results = call_tools(stock_code)
    return algo_analyze(stock_code, stock_name, tool_results)


# ── 内联自 analysis_tools.py ──

def analyze_trend(codes: str) -> Dict[str, Any]:
    """获取股票的综合技术趋势分析，包括均线排列、MACD、RSI、BOLL和KDJ等指标，支持多股批量获取。

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
            logger.error("analyze_trend(%s) failed: %s", stock_code, e)
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

def get_volume_analysis(codes: str) -> Dict[str, Any]:
    """量能分析：量比、换手率、成交量趋势，支持多股批量获取。

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
            logger.error("get_volume_analysis(%s) failed: %s", stock_code, e)
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

def analyze_pattern(codes: str) -> Dict[str, Any]:
    """识别K线形态（增强版），支持多股批量获取：锤子线、十字星、吞没、早晨/晚星、三连阳/阴、长上影/下影、缺口等。

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
            logger.error("analyze_pattern(%s) failed: %s", stock_code, e)
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

def get_chip_distribution(codes: str, lookback_days: int = 120) -> Dict[str, Any]:
    """筹码分布分析（衰减成本分布模型），支持多股批量获取。

    从日K线计算筹码分布，不依赖数据源原生接口。
    算法：按日K线的 high/low 区间分配成交量到价格档位，
    用指数衰减加权（近期筹码权重更高），汇总计算各维度指标。

    Args:
        codes: 多股用逗号分隔"（也兼容 search_stock 返回的 dict）
        lookback_days: 回看天数，默认120天
    """
    # 兼容 search_stock 返回的 dict: {'results': [{'code': '600593', ...}], ...}
    if isinstance(codes, dict):
        results = codes.get("results", [])
        if results:
            codes = results[0].get("code", "")
        else:
            return {"error": "codes dict 中无 results", "retriable": False}

    code_list = [c.strip() for c in codes.split(",") if c.strip()][:20]
    if not code_list:
        return {"error": "codes 不能为空", "retriable": False}

    def _one(stock_code: str) -> Dict[str, Any]:
        stock_code = str(stock_code).strip()
        if not stock_code:
            return {"error": "stock_code 为空", "retriable": False}
        market = _detect_market(stock_code)
        if market != "CNStock":
            return {"error": f"筹码分布分析仅支持A股，当前市场: {market}", "retriable": False}

        try:
            klines = _fetch_klines(stock_code, lookback_days)
            return calc_chip_distribution(klines, stock_code=stock_code, lookback_days=lookback_days)
        except Exception as e:
            logger.error("get_chip_distribution(%s) failed: %s", stock_code, e, exc_info=True)
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

def get_indicator_snapshot(codes: str) -> Dict[str, Any]:
    """单次获取多个技术指标快照（MACD、RSI、BOLL、KDJ等），支持多股批量获取。

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
            logger.error("get_indicator_snapshot(%s) failed: %s", stock_code, e)
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


# ── 内联自 data_tools.py ──

def get_realtime_quote(codes: str) -> Dict[str, Any]:
    """获取股票实时行情数据，支持多股批量获取。

    Args:
        codes: 多股用逗号分隔"
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()][:20]
    if not code_list:
        return {"error": "codes 不能为空", "retriable": False}

    def _one(stock_code: str) -> Dict[str, Any]:
        market = _detect_market(stock_code) or "CNStock"
        ds = _get_ds(market)
        try:
            result = ds.get_ticker(stock_code)
            if isinstance(result, dict) and "error" not in result:
                return {"stock_code": stock_code, "market": market, **result}
            return result if isinstance(result, dict) else {"error": "Unexpected result type"}
        except NotImplementedError:
            return {"error": f"数据源 {market} 不支持 get_ticker", "retriable": False}
        except Exception as e:
            logger.error("get_realtime_quote(%s) failed: %s", stock_code, e)
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

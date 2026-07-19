# -*- coding: utf-8 -*-
"""
market_screener/post_market.py

策略 3 — 盘后复盘 (15:00+ / 非交易日)
全市场技术形态扫描 + 介入点计算 + 次日计划
"""

from __future__ import annotations

from app.agent.log import logger
from typing import Any, Dict, List, Optional


from .common import (
    FactorItem,
    call_tool, fetch_kline,
    fetch_hot_stocks_with_reason,
    compute_ma, compute_macd, compute_rsi, compute_kdj,
)
from .intraday import assess_market_state
# ═══════════════════════════════════════════════════════════════
#  形态检测器
# ═══════════════════════════════════════════════════════════════

def _detect_platform_breakout(bars: List[Dict]) -> Optional[Dict]:
    if len(bars) < 10:
        return None
    recent = bars[-6:-1]
    today = bars[-1]
    highs = [b["high"] for b in recent]
    lows = [b["low"] for b in recent]
    platform_high = max(highs)
    platform_low = min(lows)
    if platform_high <= 0:
        return None
    range_pct = (platform_high - platform_low) / platform_high * 100
    if range_pct > 8:
        return None
    if today["close"] > platform_high and today["close"] > today["open"]:
        vol_ratio = today["volume"] / (sum(b["volume"] for b in recent) / len(recent)) if sum(b["volume"] for b in recent) > 0 else 1
        if vol_ratio > 1.3:
            return {
                "pattern": "平台突破", "platform_high": round(platform_high, 2),
                "range_pct": round(range_pct, 1), "vol_ratio": round(vol_ratio, 2),
                "score": 75 if vol_ratio > 2 else 65,
            }
    return None
def _detect_volume_reversal(bars: List[Dict]) -> Optional[Dict]:
    if len(bars) < 10:
        return None
    recent = bars[-6:-1]
    today = bars[-1]
    vol_trend = [b["volume"] for b in recent]
    close_trend = [b["close"] for b in recent]
    avg_vol = sum(vol_trend) / len(vol_trend) if vol_trend else 1
    is_shrinking = all(v <= avg_vol * 1.1 for v in vol_trend[-3:])
    is_surge = today["volume"] > avg_vol * 1.8 and today["close"] > today["open"]
    if close_trend and close_trend[0] > 0:
        period_change = (close_trend[-1] - close_trend[0]) / close_trend[0] * 100
    else:
        return None
    if is_shrinking and is_surge and abs(period_change) < 5:
        vol_ratio = today["volume"] / avg_vol if avg_vol > 0 else 1
        return {
            "pattern": "底部放量启动", "vol_ratio": round(vol_ratio, 2),
            "period_change": round(period_change, 1),
            "score": 70 if vol_ratio > 2.5 else 60,
        }
    return None
def _detect_ma_support_pullback(bars: List[Dict]) -> Optional[Dict]:
    if len(bars) < 20:
        return None
    closes = [b["close"] for b in bars]
    ma5 = compute_ma(closes, 5)
    ma10 = compute_ma(closes, 10)
    ma20 = compute_ma(closes, 20)
    if not (ma5[-1] and ma10[-1] and ma20[-1]):
        return None
    if not (ma5[-1] > ma10[-1] > ma20[-1]):
        return None
    today = bars[-1]
    ma10_dist = abs(today["low"] - ma10[-1]) / ma10[-1] * 100
    if ma10_dist < 1.5 and today["close"] > ma10[-1]:
        return {
            "pattern": "均线支撑回踩", "ma10": round(ma10[-1], 2),
            "low": round(today["low"], 2), "distance_pct": round(ma10_dist, 2),
            "score": 68 if today["close"] > today["open"] else 58,
        }
    return None
def _detect_macd_golden_cross(bars: List[Dict]) -> Optional[Dict]:
    if len(bars) < 30:
        return None
    closes = [b["close"] for b in bars]
    macd = compute_macd(closes)
    dif = macd["dif"]
    dea = macd["dea"]
    if dif[-1] >= dea[-1] and dif[-2] < dea[-2]:
        is_underwater = dif[-1] < 0
        score = 72 if is_underwater else 62
        return {
            "pattern": "MACD金叉", "dif": round(dif[-1], 3),
            "dea": round(dea[-1], 3), "underwater": is_underwater, "score": score,
        }
    return None
def _detect_shrink_pullback_breakout(bars: List[Dict]) -> Optional[Dict]:
    if len(bars) < 15:
        return None
    recent_10 = bars[-11:-1]
    today = bars[-1]
    highs_10 = [b["high"] for b in recent_10]
    peak_idx = highs_10.index(max(highs_10))
    peak_price = max(highs_10)
    if peak_idx >= len(recent_10) - 2:
        return None
    pullback_bars = recent_10[peak_idx + 1:]
    pullback_low = min(b["low"] for b in pullback_bars)
    pullback_pct = (peak_price - pullback_low) / peak_price * 100
    if pullback_pct < 3 or pullback_pct > 12:
        return None
    pullback_vols = [b["volume"] for b in pullback_bars]
    peak_vols = [b["volume"] for b in recent_10[max(0, peak_idx - 2):peak_idx + 1]]
    avg_peak_vol = sum(peak_vols) / len(peak_vols) if peak_vols else 1
    avg_pullback_vol = sum(pullback_vols) / len(pullback_vols) if pullback_vols else 1
    is_shrink = avg_pullback_vol < avg_peak_vol * 0.7
    pullback_high = max(b["high"] for b in pullback_bars)
    is_breakout = today["close"] > pullback_high and today["close"] > today["open"]
    is_volume_up = today["volume"] > avg_pullback_vol * 1.5
    if is_shrink and is_breakout and is_volume_up:
        return {
            "pattern": "缩量回调放量突破", "peak": round(peak_price, 2),
            "pullback_pct": round(pullback_pct, 1),
            "vol_ratio": round(today["volume"] / avg_pullback_vol, 2) if avg_pullback_vol > 0 else 0,
            "score": 73,
        }
    return None
def _detect_prev_high_breakout(bars: List[Dict]) -> Optional[Dict]:
    if len(bars) < 20:
        return None
    today = bars[-1]
    prev_20 = bars[-21:-1]
    prev_high = max(b["high"] for b in prev_20)
    if today["close"] > prev_high and today["close"] > today["open"]:
        vol_5 = sum(b["volume"] for b in bars[-6:-1]) / 5 if len(bars) > 5 else 1
        vol_ratio = today["volume"] / vol_5 if vol_5 > 0 else 1
        if vol_ratio > 1.2:
            return {
                "pattern": "突破前高", "prev_high": round(prev_high, 2),
                "close": round(today["close"], 2), "vol_ratio": round(vol_ratio, 2),
                "score": 70 if vol_ratio > 2 else 60,
            }
    return None
_DETECTORS = [
    _detect_platform_breakout, _detect_volume_reversal,
    _detect_ma_support_pullback, _detect_macd_golden_cross,
    _detect_shrink_pullback_breakout, _detect_prev_high_breakout,
]
# ═══════════════════════════════════════════════════════════════
#  Phase 1 + Phase 2
# ═══════════════════════════════════════════════════════════════

def prescreen(date: str) -> Dict[str, Any]:
    hot_data = fetch_hot_stocks_with_reason(date)
    hot_stocks = hot_data.get("stocks", [])
    hot_tags = hot_data.get("hot_tags", [])

    main_themes = [(tag, cnt) for tag, cnt in hot_tags[:5]]

    try:
        from app.agent.tools.finance.screener_tools import search_stocks
        # 常规盘后选股
        screener_result = search_stocks(
            query="涨幅1%到8% 换手率大于2% 非ST",
            source="eastmoney",
            top_n=100,
        )
        raw_stocks = screener_result.get("stocks", []) if isinstance(screener_result, dict) else []

        # 4IN1 候选：搜索近期涨停过的股票（连板/首板回调）
        dragon_result = search_stocks(
            query="近10日涨停 换手率大于2% 非ST",
            source="eastmoney",
            top_n=50,
        )
        dragon_stocks = dragon_result.get("stocks", []) if isinstance(dragon_result, dict) else []
    except Exception as e:
        logger.warning("[MktScreen] search_stocks 调用失败: %s", e)
        raw_stocks = []
        dragon_stocks = []

    scan_pool = {}
    for s in hot_stocks:
        code = s.get("code", "")
        if code and len(code) == 6:
            scan_pool[code] = {**s, "source": "热点题材"}
    for s in raw_stocks:
        code = str(s.get("code", "") or s.get("symbol", ""))
        if code and len(code) == 6:
            change_pct = float(s.get("change_pct", 0) or s.get("pct_change", 0) or 0)
            turnover_pct = float(s.get("turnover_rate", 0) or 0)
            if code in scan_pool:
                # hot_stocks 已有 → 回填 change_pct/turnover_pct
                scan_pool[code].setdefault("change_pct", change_pct)
                scan_pool[code].setdefault("turnover_pct", turnover_pct)
            else:
                scan_pool[code] = {
                    "code": code, "name": s.get("name", ""),
                    "change_pct": change_pct,
                    "turnover_pct": turnover_pct,
                    "reason": "",
                    "source": "盘后筛选",
                }
    # 4IN1 候选：近期涨停过的股票
    for s in dragon_stocks:
        code = str(s.get("code", "") or s.get("symbol", ""))
        if code and len(code) == 6 and code not in scan_pool:
            scan_pool[code] = {
                "code": code, "name": s.get("name", ""),
                "change_pct": float(s.get("change_pct", 0) or s.get("pct_change", 0) or 0),
                "turnover_pct": float(s.get("turnover_rate", 0) or 0),
                "reason": "",
                "source": "4IN1(近期涨停)",
            }

    # 龙回头扫描（当 search_stocks 结果不足时补充）
    if len(scan_pool) < 10:
        try:
            from .common import scan_dragon_pullback
            dragon_pullback = scan_dragon_pullback(date)
            for s in dragon_pullback[:15]:
                code = s.get("code", "")
                if code and len(code) == 6 and code not in scan_pool:
                    scan_pool[code] = {
                        "code": code, "name": s.get("name", ""),
                        "change_pct": 0,
                        "turnover_pct": 0,
                        "reason": s.get("reason", ""),
                        "source": "龙回头",
                    }
            logger.info("[MktScreen] 龙回头补充: %d只", min(len(dragon_pullback), 15))
        except Exception as e:
            logger.warning("[MktScreen] 龙回头扫描失败: %s", e)

    candidates = []

    for code, info in scan_pool.items():
        turnover_pct = info.get("turnover_pct", 0)
        if turnover_pct > 0 and turnover_pct < 2.0:
            continue
        candidates.append({
            "code": code, "name": info.get("name", ""),
            "change_pct": info.get("change_pct", 0),
            "turnover_pct": turnover_pct,
            "reason": info.get("reason", ""),
            "source": info.get("source", "盘后筛选"),
        })

    candidates.sort(key=lambda x: -abs(x["change_pct"]))

    return {
        "date": date, "market": assess_market_state(),
        "scanned": len(scan_pool), "pool_size": len(scan_pool),
        "main_themes": main_themes, "candidates": candidates[:40],
    }
def analyze_code(
    code: str, name: str,
    _tool_calls, _tool_nodes, _missing_data,
) -> Optional[Dict]:
    """盘后个股深入分析：形态检测 + 指标快照 + 资金流向 + 介入点。

    Args:
        code: 股票代码
        name: 股票名称
    """
    try:
        bars = fetch_kline(code, days=40)
        if len(bars) < 15:
            return None
        today = bars[-1]
        close = today["close"]
        closes = [b["close"] for b in bars]

        # ── 形态检测 ──
        patterns = []
        pattern_score = 0
        for detector in _DETECTORS:
            result = detector(bars)
            if result:
                patterns.append(result)
                pattern_score += result["score"]
        if not patterns:
            return None

        # ── 技术指标 ──
        rsi = compute_rsi(closes)
        macd = compute_macd(closes)
        kdj = compute_kdj(bars)
        ma5 = compute_ma(closes, 5)
        ma10 = compute_ma(closes, 10)
        ma20 = compute_ma(closes, 20)
        rsi_val = rsi[-1]

        score = float(pattern_score)
        signals = [p["pattern"] for p in patterns]
        factors = []

        if rsi_val > 80:
            score -= 10
        elif rsi_val > 70:
            score -= 3
        elif 40 <= rsi_val <= 60:
            score += 3

        if kdj["k"][-1] > kdj["d"][-1] and kdj["k"][-2] <= kdj["d"][-2]:
            score += 5
            signals.append("KDJ金叉")

        if ma5[-1] and ma10[-1] and ma20[-1]:
            if ma5[-1] > ma10[-1] > ma20[-1]:
                score += 5
                signals.append("MA多头排列")
            factors.append(FactorItem(
                name="均线",
                value=f"MA5={ma5[-1]:.2f} MA10={ma10[-1]:.2f} MA20={ma20[-1]:.2f}",
                score=65 if (ma5[-1] or 0) > (ma20[-1] or 0) else 45,
            ))

        vol_5 = sum(b["volume"] for b in bars[-6:-1]) / 5 if len(bars) > 5 else 1
        vol_ratio = today["volume"] / vol_5 if vol_5 > 0 else 1
        if vol_ratio > 1.5:
            signals.append(f"放量{vol_ratio:.1f}倍")

        # ── 指标快照 ──
        snapshot = call_tool("get_indicator_snapshot", codes=code)
        if _tool_calls is not None and "get_indicator_snapshot" not in _tool_calls:
            _tool_calls.append("get_indicator_snapshot")

        if isinstance(snapshot, dict) and "error" not in snapshot:
            macd_data = snapshot.get("macd", {})
            macd_sig = str(macd_data.get("signal", ""))
            if "金叉" in macd_sig:
                score += 5
                signals.append("MACD金叉确认")
            elif "死叉" in macd_sig:
                score -= 5
                signals.append("MACD死叉警告")
            factors.append(FactorItem(
                name="MACD",
                value=f"DIF={macd_data.get('dif', '?')} DEA={macd_data.get('dea', '?')}",
                score=65 if "金叉" in macd_sig else (35 if "死叉" in macd_sig else 50),
            ))
            boll = snapshot.get("boll", {})
            if boll:
                factors.append(FactorItem(
                    name="BOLL",
                    value=f"上轨={boll.get('upper', '?')} 中轨={boll.get('mid', '?')} 下轨={boll.get('lower', '?')}",
                    score=55,
                ))

        # ── 资金流向 ──
        fund_flow = call_tool("get_fund_flow_realtime", code=code)
        if _tool_calls is not None and "get_fund_flow_realtime" not in _tool_calls:
            _tool_calls.append("get_fund_flow_realtime")

        if isinstance(fund_flow, dict) and "error" not in fund_flow:
            main_net = fund_flow.get("main_net_inflow", 0) or 0
            if main_net > 0:
                score += 5
                signals.append(f"主力净流入{main_net / 10000:.0f}万")
            elif main_net < -5000000:
                score -= 3
                signals.append(f"主力净流出{abs(main_net) / 10000:.0f}万")
            factors.append(FactorItem(
                name="资金流",
                value=f"主力净流入={main_net / 10000:.0f}万",
                score=65 if main_net > 0 else (35 if main_net < -5000000 else 50),
            ))

        # ── 介入点计算 ──
        entry_low = round(close * 0.995, 2)
        entry_high = round(close * 1.005, 2)
        stop_loss = round((ma10[-1] or close * 0.97) if ma10[-1] else close * 0.97, 2)
        risk = close - stop_loss
        if risk <= 0:
            risk = close * 0.03
        target_1 = round(close + risk * 1.5, 2)
        target_2 = round(close + risk * 2.5, 2)

        # ── 风险提示 ──
        risk_notes = []
        change_pct = (close - bars[-2]["close"]) / bars[-2]["close"] * 100 if len(bars) >= 2 else 0
        if rsi_val > 75:
            risk_notes.append(f"RSI{rsi_val:.0f}偏高，短线回调风险")
            score -= 3
        if change_pct > 6:
            risk_notes.append("涨幅偏大，追涨需谨慎")
        if vol_ratio > 4:
            risk_notes.append("量能异常放大，注意出货嫌疑")

        score = max(0, min(100, score))
        direction = "bullish" if score >= 60 else ("bearish" if score <= 40 else "neutral")
        confidence = round(min(0.85, max(0.1, score / 100 * 0.7 + 0.15)), 2)

        return {
            "code": code, "name": name,
            "patterns": [p["pattern"] for p in patterns],
            "score": round(score, 1), "direction": direction, "confidence": confidence,
            "signal": ", ".join(signals[:5]),
            "factors": [f.to_dict() for f in factors],
            "risk_notes": risk_notes,
            "entry": {
                "price_low": entry_low, "price_high": entry_high,
                "stop_loss": stop_loss, "target_1": target_1, "target_2": target_2,
                "risk_reward": "1:1.5 / 1:2.5",
            },
            "tech": {
                "close": round(close, 2), "rsi": round(rsi_val, 1),
                "vol_ratio": round(vol_ratio, 2),
                "ma5": round(ma5[-1], 2) if ma5[-1] else None,
                "ma10": round(ma10[-1], 2) if ma10[-1] else None,
            },
        }
    except Exception as e:
        logger.warning("[MktScreen] 盘后深入分析 %s 失败: %s", code, e)
        return None


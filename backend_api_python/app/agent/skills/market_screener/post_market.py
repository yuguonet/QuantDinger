# -*- coding: utf-8 -*-
"""
market_screener/post_market.py

策略 3 — 盘后复盘 (15:00+ / 非交易日)
全市场技术形态扫描 + 介入点计算 + 次日计划
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any, Dict, List, Optional

from app.agent.chain.schema import FactorItem, SkillReport
from .common import (
    call_tool, fetch_kline, get_limit_pct,
    fetch_hot_stocks_with_reason,
    compute_ma, compute_macd, compute_rsi, compute_kdj,
)

logger = logging.getLogger(__name__)


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
        from app.agent.tools.screener_tools import search_stocks
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
    except Exception:
        raw_stocks = []
        dragon_stocks = []

    scan_pool = {}
    for s in hot_stocks:
        code = s.get("code", "")
        if code and len(code) == 6:
            scan_pool[code] = s
    for s in raw_stocks:
        code = str(s.get("code", "") or s.get("symbol", ""))
        if code and len(code) == 6 and code not in scan_pool:
            scan_pool[code] = {
                "code": code, "name": s.get("name", ""),
                "change_pct": float(s.get("change_pct", 0) or s.get("pct_change", 0) or 0),
                "turnover_pct": float(s.get("turnover_rate", 0) or 0),
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

    candidates = []
    scanned = 0

    for code, info in scan_pool.items():
        bars = fetch_kline(code, days=40)
        if len(bars) < 15:
            continue

        scanned += 1
        today = bars[-1]
        closes = [b["close"] for b in bars]

        if len(bars) >= 2:
            prev_close = bars[-2]["close"]
            if prev_close > 0:
                change = (today["close"] - prev_close) / prev_close * 100
                if change > 9.8 or change < -9.8:
                    continue

        patterns = []
        total_score = 0

        for detector in _DETECTORS:
            result = detector(bars)
            if result:
                patterns.append(result)
                total_score += result["score"]

        if not patterns:
            continue

        rsi = compute_rsi(closes)
        macd = compute_macd(closes)
        kdj = compute_kdj(bars)

        rsi_val = rsi[-1]
        if rsi_val > 80:
            total_score -= 10
        elif rsi_val > 70:
            total_score -= 3
        elif 40 < rsi_val < 60:
            total_score += 3

        if kdj["k"][-1] > kdj["d"][-1] and kdj["k"][-2] <= kdj["d"][-2]:
            total_score += 5

        ma5 = compute_ma(closes, 5)
        ma10 = compute_ma(closes, 10)
        ma20 = compute_ma(closes, 20)
        if ma5[-1] and ma10[-1] and ma20[-1]:
            if ma5[-1] > ma10[-1] > ma20[-1]:
                total_score += 5

        vol_5 = sum(b["volume"] for b in bars[-6:-1]) / 5 if len(bars) > 5 else 1
        vol_ratio = today["volume"] / vol_5 if vol_5 > 0 else 1

        total_score = max(0, min(100, total_score))

        signals = [p["pattern"] for p in patterns]
        if rsi_val > 70:
            signals.append(f"RSI{rsi_val:.0f}偏高")
        if vol_ratio > 1.5:
            signals.append(f"放量{vol_ratio:.1f}倍")

        # 换手率 < 2% 排除（活跃度不够）
        turnover_pct = info.get("turnover_pct", 0)
        if turnover_pct > 0 and turnover_pct < 2.0:
            continue

        candidates.append({
            "code": code, "name": info.get("name", ""),
            "change_pct": info.get("change_pct", 0),
            "close": round(today["close"], 2),
            "patterns": patterns, "pattern_names": [p["pattern"] for p in patterns],
            "score": total_score, "rsi": round(rsi_val, 1),
            "macd_dif": round(macd["dif"][-1], 3),
            "kdj_k": round(kdj["k"][-1], 1), "vol_ratio": round(vol_ratio, 2),
            "ma5": round(ma5[-1], 2) if ma5[-1] else None,
            "ma10": round(ma10[-1], 2) if ma10[-1] else None,
            "reason": info.get("reason", ""), "signals": signals,
            "source": info.get("source", "盘后筛选"),
        })

    candidates.sort(key=lambda x: -x["score"])

    return {
        "date": date, "scanned": scanned, "pool_size": len(scan_pool),
        "main_themes": main_themes, "candidates": candidates[:20],
    }


def deep_analyze(candidate, _tool_calls, _tool_nodes, _missing_data) -> Optional[Dict]:
    code = candidate["code"]
    try:
        snapshot = call_tool("get_indicator_snapshot", codes=code)
        fund_flow = call_tool("get_fund_flow_realtime", code=code)

        if _tool_calls is not None:
            for t in ["get_indicator_snapshot", "get_fund_flow_realtime"]:
                if t not in _tool_calls:
                    _tool_calls.append(t)

        score = candidate.get("score", 60)
        signals = list(candidate.get("signals", []))
        factors = []

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

        close = candidate.get("close", 0)
        ma10 = candidate.get("ma10")
        entry_low = round(close * 0.995, 2)
        entry_high = round(close * 1.005, 2)
        stop_loss = round(min(ma10 or close * 0.97, close * 0.97), 2)
        risk = close - stop_loss
        if risk <= 0:
            risk = close * 0.03
        target_1 = round(close + risk * 1.5, 2)
        target_2 = round(close + risk * 2.5, 2)

        risk_notes = []
        rsi = candidate.get("rsi", 50)
        if rsi > 75:
            risk_notes.append(f"RSI{rsi:.0f}偏高，短线回调风险")
            score -= 3
        if candidate.get("change_pct", 0) > 6:
            risk_notes.append("涨幅偏大，追涨需谨慎")
        if candidate.get("vol_ratio", 1) > 4:
            risk_notes.append("量能异常放大，注意出货嫌疑")

        score = max(0, min(100, score))
        direction = "bullish" if score >= 60 else ("bearish" if score <= 40 else "neutral")

        return {
            "code": code, "name": candidate.get("name", ""),
            "patterns": candidate.get("pattern_names", []),
            "score": round(score, 1), "direction": direction,
            "signal": ", ".join(signals[:5]),
            "factors": [f.to_dict() for f in factors],
            "risk_notes": risk_notes,
            "entry": {
                "price_low": entry_low, "price_high": entry_high,
                "stop_loss": stop_loss, "target_1": target_1, "target_2": target_2,
                "risk_reward": "1:1.5 / 1:2.5",
            },
            "tech": {
                "close": close, "rsi": candidate.get("rsi"),
                "vol_ratio": candidate.get("vol_ratio"),
                "ma5": candidate.get("ma5"), "ma10": candidate.get("ma10"),
            },
        }
    except Exception as e:
        logger.warning("[MktScreen] 盘后深入分析 %s 失败: %s", code, e)
        return None


def run_strategy(date: str, _tool_calls, _tool_nodes, _missing_data) -> Optional[SkillReport]:
    try:
        prescreen_result = prescreen(date)
    except Exception as e:
        logger.warning("[MktScreen] 盘后形态扫描失败: %s", e)
        return None

    candidates = prescreen_result["candidates"]
    main_themes = prescreen_result["main_themes"]

    logger.info("[MktScreen] 盘后扫描: 池%d只, 扫描%d只, 候选%d只",
                prescreen_result["pool_size"], prescreen_result["scanned"], len(candidates))

    if not candidates:
        return SkillReport(
            skill_name="market_screener", score=40.0, direction="neutral",
            confidence=0.5, signal="今日无符合形态的短线标的",
            analysis=(
                "## 盘后短线选股 — 无合适标的\n\n"
                f"扫描 {prescreen_result['scanned']} 只股票，"
                f"未发现符合技术形态条件的标的。\n\n"
                "**建议：观望等待更好的形态出现。**"
            ),
            factors=[
                FactorItem(name="扫描数", value=str(prescreen_result["scanned"]), score=50),
                FactorItem(name="主线题材", value=", ".join(t for t, _ in main_themes[:3]) or "无", score=50),
            ],
            status="ok",
        )

    analyzed = []
    for c in candidates[:6]:
        result = deep_analyze(c, _tool_calls, _tool_nodes, _missing_data)
        if result:
            analyzed.append(result)

    # Phase 2: 过滤低分
    analyzed = [a for a in analyzed if a.get("score", 0) >= 60 and a.get("direction") == "bullish"]
    analyzed.sort(key=lambda x: -x.get("score", 0))

    if analyzed:
        avg_score = sum(a["score"] for a in analyzed) / len(analyzed)
        bullish = sum(1 for a in analyzed if a["direction"] == "bullish")
    else:
        avg_score = 50.0
        bullish = 0

    direction = "bullish" if avg_score >= 55 else ("bearish" if avg_score < 45 else "neutral")
    confidence = min(0.85, 0.4 + len(analyzed) * 0.07)

    factors = [
        FactorItem(name="扫描池", value=str(prescreen_result["pool_size"]), score=50),
        FactorItem(name="形态命中", value=str(len(candidates)), score=min(100, len(candidates) * 12 + 20)),
        FactorItem(name="主线题材", value=", ".join(t for t, _ in main_themes[:3]) or "无",
                   score=70 if main_themes else 40),
        FactorItem(name="深入分析", value=str(len(analyzed)), score=min(100, len(analyzed) * 15 + 20)),
        FactorItem(name="看多比例", value=f"{bullish}/{len(analyzed)}", score=int(avg_score)),
    ]

    lines = [
        "## 盘后短线选股结果",
        f"扫描池: {prescreen_result['pool_size']}只 | 形态命中: {len(candidates)}只 | 深入分析: {len(analyzed)}只",
        f"主线题材: {', '.join(t for t, _ in main_themes[:5]) or '无明确主线'}",
        "",
    ]

    pattern_counter: Counter = Counter()
    for c in candidates:
        for p in c.get("pattern_names", []):
            pattern_counter[p] += 1
    if pattern_counter:
        lines.append("### 形态分布")
        for p, cnt in pattern_counter.most_common(6):
            lines.append(f"- {p}: {cnt}只")
        lines.append("")

    if analyzed:
        lines.append("### 次日候选标的")
        for a in analyzed:
            risk = " ⚠️" + "、".join(a.get("risk_notes", [])) if a.get("risk_notes") else ""
            entry = a.get("entry", {})
            lines.append(
                f"- **{a['code']}** {a.get('name', '')} | 评分{a['score']:.0f} | {a['direction']} | "
                f"形态:{','.join(a.get('patterns', []))} | {a['signal']}{risk}"
            )
            lines.append(
                f"  入场:{entry.get('price_low', '?')}-{entry.get('price_high', '?')} | "
                f"止损:{entry.get('stop_loss', '?')} | "
                f"目标:{entry.get('target_1', '?')}/{entry.get('target_2', '?')} | "
                f"盈亏比:{entry.get('risk_reward', '?')}"
            )

    return SkillReport(
        skill_name="market_screener", score=round(avg_score, 1),
        direction=direction, confidence=confidence,
        signal=f"盘后{len(analyzed)}只候选，主线:{', '.join(t for t, _ in main_themes[:2]) or '无'}",
        factors=factors, analysis="\n".join(lines),
        output_data={
            "main_themes": main_themes,
            "pattern_distribution": dict(pattern_counter),
            "candidates": [c for c in candidates[:15]],
            "analyzed": analyzed,
        },
        tools_called=_tool_calls or [],
        missing_data=_missing_data or [],
        status="ok",
    )

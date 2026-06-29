# -*- coding: utf-8 -*-
"""
market_screener/eod.py

策略 2 — 尾盘隔夜 (14:30-15:00)
条件初筛 + 尾盘特征验证 + 尾盘封板
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from app.agent.chain.schema import FactorItem, SkillReport
from .common import (
    call_tool, fetch_kline, get_limit_pct, _today_str,
    fetch_zt_pool, fetch_hot_stocks_with_reason,
    compute_ma, compute_rsi,
)

logger = logging.getLogger(__name__)


def prescreen() -> Dict[str, Any]:
    screener_result = call_tool(
        "search_stocks",
        query="涨幅3%到8% 换手率大于3% 非ST",
        source="eastmoney",
        top_n=80,
    )
    raw_stocks = screener_result.get("stocks", []) if isinstance(screener_result, dict) else []

    date = _today_str()
    zt_pool = fetch_zt_pool(date)

    eod_zt = []
    for s in zt_pool:
        zt_time = s.get("zt_time", "") or ""
        if zt_time and ":" in zt_time:
            try:
                parts = zt_time.split(":")
                hour, minute = int(parts[0]), int(parts[1])
                if hour == 14 and minute >= 30:
                    eod_zt.append({
                        "code": s.get("stock_code", ""),
                        "name": s.get("stock_name", ""),
                        "source": "尾盘封板",
                        "reason": s.get("reason", ""),
                        "zt_time": zt_time,
                        "continuous_days": int(s.get("continuous_zt_days", 1) or 1),
                    })
            except (ValueError, IndexError):
                pass

    hot_data = fetch_hot_stocks_with_reason(date)
    reason_map = {}
    hot_tags = hot_data.get("hot_tags", [])
    for s in hot_data.get("stocks", []):
        reason_map[s.get("code", "")] = s.get("reason", "")

    main_tags = {tag for tag, _ in hot_tags[:5]}

    candidates = []
    for s in raw_stocks:
        code = str(s.get("code", "") or s.get("symbol", ""))

        if not code or len(code) != 6:
            continue
        name = s.get("name", "")
        change_pct = float(s.get("change_pct", 0) or s.get("pct_change", 0) or 0)
        turnover = float(s.get("turnover_rate", 0) or 0)

        if change_pct < 3 or change_pct > 8:
            continue

        bars = fetch_kline(code, days=10)
        if len(bars) < 3:
            continue

        today = bars[-1]
        close = today["close"]
        high = today["high"]
        low = today["low"]
        volume = today["volume"]

        if high <= 0:
            continue

        close_to_high = (high - close) / high * 100
        day_range = high - low
        close_position = (close - low) / day_range if day_range > 0 else 0

        prev_volumes = [bars[j]["volume"] for j in range(max(0, len(bars) - 5), len(bars) - 1)]
        avg_vol = sum(prev_volumes) / len(prev_volumes) if prev_volumes else 1
        vol_ratio = volume / avg_vol if avg_vol > 0 else 1

        eod_score = 50
        signals = []

        if close_to_high < 0.3:
            eod_score += 18
            signals.append("收盘=最高价")
        elif close_to_high < 0.8:
            eod_score += 12
            signals.append("收盘接近最高价")
        elif close_to_high < 1.5:
            eod_score += 5
            signals.append("收盘偏高位")
        else:
            continue

        if vol_ratio > 2.5:
            eod_score += 12
            signals.append(f"大幅放量{vol_ratio:.1f}倍")
        elif vol_ratio > 1.5:
            eod_score += 8
            signals.append(f"放量{vol_ratio:.1f}倍")
        elif vol_ratio > 1.2:
            eod_score += 3
            signals.append(f"温和放量{vol_ratio:.1f}倍")

        if close_position > 0.85:
            eod_score += 10
            signals.append("收盘在日内高位")
        elif close_position > 0.7:
            eod_score += 5

        if 4 <= change_pct <= 6:
            eod_score += 8
            signals.append(f"涨幅{change_pct:.1f}%适中")
        elif 6 < change_pct <= 7:
            eod_score += 3

        reason = reason_map.get(code, "")
        if reason:
            matched = [t for t in reason.replace("，", "+").replace(",", "+").split("+") if t.strip() in main_tags]
            if matched:
                eod_score += 10
                signals.append(f"主线题材:{'+'.join(matched[:2])}")
            else:
                eod_score += 3
                signals.append(f"题材:{reason[:15]}")

        closes = [b["close"] for b in bars]
        rsi = compute_rsi(closes)
        if rsi[-1] > 80:
            eod_score -= 10
            signals.append(f"RSI{rsi[-1]:.0f}超买警告")

        ma5 = compute_ma(closes, 5)
        ma10 = compute_ma(closes, 10)
        if ma5[-1] and ma10[-1] and ma5[-1] > ma10[-1]:
            eod_score += 5
            signals.append("MA5>MA10")

        if len(signals) < 2:
            continue

        candidates.append({
            "code": code, "name": name,
            "change_pct": change_pct, "turnover": turnover,
            "close": round(close, 3), "high": round(high, 3),
            "close_to_high": round(close_to_high, 2),
            "vol_ratio": round(vol_ratio, 2), "rsi": round(rsi[-1], 2),
            "reason": reason, "eod_score": eod_score,
            "signals": signals, "source": "尾盘强势",
            "evaluation": {
                "score": eod_score,
                "highlights": signals,
                "warnings": ["RSI超买"] if eod_score > 0 and any("超买" in s for s in signals) else [],
            },
        })

    for s in eod_zt:
        code = s["code"]
        bars = fetch_kline(code, days=5)
        close = bars[-1]["close"] if bars else 0
        _zt_signals = [f"尾盘封板{s['zt_time']}", f"{s['continuous_days']}连板"]
        candidates.append({
            "code": code, "name": s["name"],
            "change_pct": 9.9, "turnover": 0,
            "close": round(close, 3), "high": round(close, 3),
            "close_to_high": 0, "vol_ratio": 0, "rsi": 0,
            "reason": s.get("reason", ""), "eod_score": 90,
            "signals": _zt_signals,
            "source": "尾盘封板",
            "evaluation": {"score": 90, "highlights": _zt_signals, "warnings": []},
        })

    seen = set()
    unique = []
    for c in sorted(candidates, key=lambda x: -x["eod_score"]):
        if c["code"] not in seen:
            seen.add(c["code"])
            unique.append(c)

    themes = [(tag, cnt) for tag, cnt in hot_tags[:5]]

    # 过滤涨停封板股（买不进去）
    filtered = []
    for c in unique:
        code = c.get("code", "")
        name = c.get("name", "")
        limit_pct = get_limit_pct(code, name)
        if c.get("change_pct", 0) >= limit_pct - 0.5:
            continue
        # 换手率 < 2% 排除（活跃度不够）
        if c.get("turnover", 0) > 0 and c["turnover"] < 2.0:
            continue
        filtered.append(c)

    return {
        "date": date,
        "screener_count": len(raw_stocks),
        "zt_eod_count": len(eod_zt),
        "main_themes": themes,
        "candidates": filtered[:15],
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

        score = candidate.get("eod_score", 60)
        signals = list(candidate.get("signals", []))
        factors = []

        if isinstance(snapshot, dict) and "error" not in snapshot:
            macd = snapshot.get("macd", {})
            macd_sig = str(macd.get("signal", ""))
            if "金叉" in macd_sig:
                score += 8
                signals.append("MACD金叉")
            elif "死叉" in macd_sig:
                score -= 5
                signals.append("MACD死叉")
            factors.append(FactorItem(
                name="MACD",
                value=f"DIF={macd.get('dif', '?')} DEA={macd.get('dea', '?')}",
                score=65 if "金叉" in macd_sig else (35 if "死叉" in macd_sig else 50),
            ))
            kdj = snapshot.get("kdj", {})
            kdj_sigs = kdj.get("signals") or []
            if any("金叉" in s for s in kdj_sigs):
                score += 5
                signals.append("KDJ金叉")
            factors.append(FactorItem(
                name="KDJ",
                value=f"K={kdj.get('k', '?')} D={kdj.get('d', '?')} J={kdj.get('j', '?')}",
                score=60 if any("金叉" in s for s in kdj_sigs) else 50,
            ))

        if isinstance(fund_flow, dict) and "error" not in fund_flow:
            main_net = fund_flow.get("main_net_inflow", 0) or 0
            if main_net > 0:
                score += 6
                signals.append(f"主力净流入{main_net / 10000:.0f}万")
            elif main_net < -5000000:
                score -= 4
                signals.append(f"主力净流出{abs(main_net) / 10000:.0f}万")
            factors.append(FactorItem(
                name="资金流",
                value=f"主力净流入={main_net / 10000:.0f}万",
                score=65 if main_net > 0 else (35 if main_net < -5000000 else 50),
            ))

        risk_notes = []
        rsi = candidate.get("rsi", 50)
        if rsi > 75:
            risk_notes.append(f"RSI{rsi:.0f}偏高，次日回调风险")
            score -= 5
        if candidate.get("change_pct", 0) > 7:
            risk_notes.append("涨幅>7%，追涨风险高")
            score -= 5

        score = max(0, min(100, score))
        direction = "bullish" if score >= 60 else ("bearish" if score <= 40 else "neutral")

        return {
            "code": code, "name": candidate.get("name", ""),
            "source": candidate.get("source", ""), "reason": candidate.get("reason", ""),
            "score": round(score, 1), "direction": direction,
            "signal": ", ".join(signals[:5]),
            "factors": [f.to_dict() for f in factors],
            "risk_notes": risk_notes,
            "eod_data": {
                "close": candidate.get("close"),
                "close_to_high": candidate.get("close_to_high"),
                "vol_ratio": candidate.get("vol_ratio"),
                "change_pct": candidate.get("change_pct"),
                "rsi": rsi,
            },
        }
    except Exception as e:
        logger.warning("[MktScreen] 尾盘深入分析 %s 失败: %s", code, e)
        return None


def run_strategy(_tool_calls, _tool_nodes, _missing_data) -> Optional[SkillReport]:
    try:
        prescreen_result = prescreen()
    except Exception as e:
        logger.warning("[MktScreen] 尾盘预筛选失败: %s", e)
        return None

    candidates = prescreen_result["candidates"]
    main_themes = prescreen_result["main_themes"]

    logger.info("[MktScreen] 尾盘预筛选: 条件选股%d只, 尾盘封板%d只, 候选%d只",
                prescreen_result["screener_count"], prescreen_result["zt_eod_count"], len(candidates))

    if not candidates:
        return SkillReport(
            skill_name="market_screener", score=40.0, direction="neutral",
            confidence=0.5, signal="今日无合适隔夜标的",
            analysis=(
                f"## 尾盘选股 — 无合适标的\n\n"
                f"条件选股扫描 {prescreen_result['screener_count']} 只，"
                f"尾盘封板 {prescreen_result['zt_eod_count']} 只，"
                f"经尾盘特征验证后无合格标的。\n\n"
                f"**建议：空仓过夜，等待明日机会。**"
            ),
            factors=[
                FactorItem(name="条件选股", value=str(prescreen_result["screener_count"]), score=40),
                FactorItem(name="尾盘封板", value=str(prescreen_result["zt_eod_count"]), score=50),
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
        bullish = len(analyzed)
    else:
        avg_score = 50.0
        bullish = 0

    direction = "bullish" if avg_score >= 55 else ("bearish" if avg_score < 45 else "neutral")
    confidence = min(0.85, 0.4 + len(analyzed) * 0.07)
    lines = ["## 尾盘选股结果", f"候选: {len(candidates)}只 | 高分通过: {len(analyzed)}只"]
    for a in analyzed:
        lines.append(f"- **{a['code']}** {a.get('name', '')} | 评分{a['score']:.0f} | {a['direction']} | {a['signal']}")

    return SkillReport(
        skill_name="market_screener", score=round(avg_score, 1),
        direction=direction, confidence=confidence,
        signal=f"隔夜{bullish}只高分候选，主线:{', '.join(t for t, _ in main_themes[:2]) or '无'}",
        factors=[], analysis="\n".join(lines),
        output_data={"main_themes": main_themes, "candidates": candidates[:15], "analyzed": analyzed},
        status="ok",
    )

# -*- coding: utf-8 -*-
"""
market_screener/eod.py

策略 2 — 尾盘隔夜 (14:30-15:00)
条件初筛 + 尾盘特征验证 + 尾盘封板
"""

from __future__ import annotations

from app.agent.log import logger
from typing import Any, Dict, List, Optional


from .common import (
    FactorItem,
    call_tool, fetch_kline, _today_str,
    fetch_zt_pool, fetch_hot_stocks_with_reason,
    compute_ma, compute_rsi,
)
from .intraday import (
    assess_market_state,
    _fetch_zt_pool_em,
    _fetch_strong_stocks_em,
    _fetch_hot_stocks_em,
)
def prescreen() -> Dict[str, Any]:
    screener_result = call_tool(
        "search_stocks",
        query="涨幅3%到8% 换手率大于3% 非ST",
        source="eastmoney",
        top_n=80,
    )
    raw_stocks = screener_result.get("stocks", []) if isinstance(screener_result, dict) else []

    date = _today_str()
    zt_pool = _fetch_zt_pool_em(date)

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
            "change_pct": change_pct, "turnover_pct": turnover,
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
            "change_pct": 9.9, "turnover_pct": 0,
            "close": round(close, 3), "high": round(close, 3),
            "close_to_high": 0, "vol_ratio": 0, "rsi": 0,
            "reason": s.get("reason", ""), "eod_score": 90,
            "signals": _zt_signals,
            "source": "尾盘封板",
            "evaluation": {"score": 90, "highlights": _zt_signals, "warnings": []},
        })

    # 强势股 + 热榜（东财搜索，与前端卡片一致）
    strong_stocks = _fetch_strong_stocks_em()
    hot_stocks = _fetch_hot_stocks_em()

    # 归一化处理：按排名线性插值，排名越前分数越高
    # 强势股: rank 0 → 100, rank N-1 → 50
    for rank, s in enumerate(strong_stocks[:100]):
        code = s.get("code", "")
        if code and not any(c["code"] == code for c in candidates):
            n = min(len(strong_stocks), 100)
            score = round(100 - 50 * rank / max(n - 1, 1), 1)
            candidates.append({
                "code": code, "name": s.get("name", ""),
                "change_pct": s.get("change_rate", 0),
                "turnover_pct": s.get("turnoverrate", 0),
                "close": s.get("new_price", 0), "high": 0,
                "close_to_high": 0, "vol_ratio": 0, "rsi": 0,
                "reason": "强势股", "eod_score": score,
                "signals": [f"强势股(rank#{rank+1})"],
                "source": "强势股",
                "evaluation": {"score": score, "highlights": ["强势股"], "warnings": []},
            })
    # 热榜: rank 0 → 95, rank N-1 → 45
    for rank, s in enumerate(hot_stocks[:100]):
        code = s.get("code", "")
        if code and not any(c["code"] == code for c in candidates):
            n = min(len(hot_stocks), 100)
            score = round(95 - 50 * rank / max(n - 1, 1), 1)
            candidates.append({
                "code": code, "name": s.get("name", ""),
                "change_pct": s.get("change_rate", 0),
                "turnover_pct": s.get("turnoverrate", 0),
                "close": s.get("new_price", 0), "high": 0,
                "close_to_high": 0, "vol_ratio": 0, "rsi": 0,
                "reason": "热榜", "eod_score": score,
                "signals": [f"热榜(rank#{rank+1})"],
                "source": "热榜",
                "evaluation": {"score": score, "highlights": ["热榜"], "warnings": []},
            })
    logger.info("[MktScreen] 强势股: %d只, 热榜: %d只", len(strong_stocks), len(hot_stocks))

    seen = set()
    unique = []
    for c in sorted(candidates, key=lambda x: -x["eod_score"]):
        if c["code"] not in seen:
            seen.add(c["code"])
            unique.append(c)

    themes = [(tag, cnt) for tag, cnt in hot_tags[:5]]

    # 换手率 < 2% 排除（活跃度不够，turnover_pct=0 时不过滤避免误杀）
    filtered = []
    for c in unique:
        if c.get("turnover_pct", 0) > 0 and c["turnover_pct"] < 2.0:
            continue
        filtered.append(c)

    return {
        "date": date,
        "market": assess_market_state(),
        "screener_count": len(raw_stocks),
        "zt_eod_count": len(eod_zt),
        "main_themes": themes,
        "candidates": filtered[:15],
    }
def analyze_code(
    code: str, name: str,
    _tool_calls, _tool_nodes, _missing_data,
) -> Optional[Dict]:
    """尾盘个股深入分析：指标快照 + 资金流向 + 收盘形态。

    Args:
        code: 股票代码
        name: 股票名称
    """
    try:
        # 自取日线数据，不依赖 prescreen
        bars = fetch_kline(code, days=10)
        if len(bars) < 3:
            return None
        today = bars[-1]
        close = today["close"]
        high = today["high"]
        low = today["low"]
        volume = today["volume"]

        # 计算收盘形态指标
        close_to_high = (high - close) / high * 100 if high > 0 else 0
        day_range = high - low
        close_position = (close - low) / day_range if day_range > 0 else 0

        prev_volumes = [bars[j]["volume"] for j in range(max(0, len(bars) - 5), len(bars) - 1)]
        avg_vol = sum(prev_volumes) / len(prev_volumes) if prev_volumes else 1
        vol_ratio = volume / avg_vol if avg_vol > 0 else 1

        # 计算 RSI
        closes = [b["close"] for b in bars]
        rsi_val = compute_rsi(closes)[-1]

        change_pct = (close - bars[-2]["close"]) / bars[-2]["close"] * 100 if len(bars) >= 2 else 0

        score = 55.0
        signals = []
        factors = []

        # ── 收盘位置 ──
        if close_to_high < 0.3:
            score += 10
            signals.append("收盘=最高价")
        elif close_to_high < 0.8:
            score += 7
            signals.append("收盘接近最高价")
        elif close_to_high < 1.5:
            score += 3
            signals.append("收盘偏高位")

        # ── 量能 ──
        if vol_ratio > 2.5:
            score += 8
            signals.append(f"大幅放量{vol_ratio:.1f}倍")
        elif vol_ratio > 1.5:
            score += 5
            signals.append(f"放量{vol_ratio:.1f}倍")
        elif vol_ratio > 1.2:
            score += 2
            signals.append(f"温和放量{vol_ratio:.1f}倍")

        # ── 涨幅位置 ──
        if close_position > 0.85:
            score += 5
            signals.append("收盘在日内高位")
        elif close_position > 0.7:
            score += 2

        # ── 指标快照 ──
        snapshot = call_tool("get_indicator_snapshot", codes=code)
        if _tool_calls is not None and "get_indicator_snapshot" not in _tool_calls:
            _tool_calls.append("get_indicator_snapshot")

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

        # ── 资金流向 ──
        fund_flow = call_tool("get_fund_flow_realtime", code=code)
        if _tool_calls is not None and "get_fund_flow_realtime" not in _tool_calls:
            _tool_calls.append("get_fund_flow_realtime")

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

        # ── 风险提示 ──
        risk_notes = []
        if rsi_val > 75:
            risk_notes.append(f"RSI{rsi_val:.0f}偏高，次日回调风险")
            score -= 5
        if change_pct > 7:
            risk_notes.append("涨幅>7%，追涨风险高")
            score -= 5

        score = max(0, min(100, score))
        direction = "bullish" if score >= 60 else ("bearish" if score <= 40 else "neutral")
        confidence = round(min(0.85, max(0.1, score / 100 * 0.7 + 0.15)), 2)

        return {
            "code": code, "name": name,
            "score": round(score, 1), "direction": direction, "confidence": confidence,
            "signal": ", ".join(signals[:5]),
            "factors": [f.to_dict() for f in factors],
            "risk_notes": risk_notes,
            "eod_data": {
                "close": round(close, 3),
                "close_to_high": round(close_to_high, 2),
                "vol_ratio": round(vol_ratio, 2),
                "change_pct": round(change_pct, 1),
                "rsi": round(rsi_val, 1),
            },
        }
    except Exception as e:
        logger.warning("[MktScreen] 尾盘深入分析 %s 失败: %s", code, e)
        return None


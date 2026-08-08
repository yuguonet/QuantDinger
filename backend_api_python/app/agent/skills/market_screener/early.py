# -*- coding: utf-8 -*-
"""
market_screener/early.py

策略 0 — 早盘 (09:30-10:00)

开盘半小时，市场尚未定型。与 intraday 的区别：
- 不依赖 main_themes（涨停太少，题材数据稀疏）
- 不依赖 zt_pool/dragon_pullback（连板还没形成）
- 用 search_stocks 做板块+量价条件搜索
- 用 5m K 线做早盘趋势判断
- BB 超卖策略在此窗口有独立买入逻辑（9:50 左右）

核心逻辑：
  1. 评估大盘开盘状态（资金流向 + 开盘涨跌比）
  2. 板块强弱扫描 → 锁定早盘强势板块
  3. search_stocks 条件搜索（板块龙头 + 量价形态）
  4. 5m K 线趋势分析（开盘后的方向确认）
  5. 返回候选池 + market 状态
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from app.agent.log import logger
from app.agent.tools.finance.data_tools import get_realtime_quote

from .common import (
    call_tool, fetch_kline,
    fetch_hot_sectors,
    compute_ma, compute_rsi, compute_volume_ratio, compute_macd,
    SkillResult, _get_writer,
)


# ═══════════════════════════════════════════════════════════════
#  早盘市场状态评估（轻量版，不依赖涨停池）
# ═══════════════════════════════════════════════════════════════

def _assess_early_market() -> Dict[str, Any]:
    """早盘市场评估：资金流向 + 板块强弱。

    不依赖涨停池（9:30 涨停数据稀疏），
    改用大盘资金流向 + 板块涨跌比判断。
    """
    # 1. 大盘资金流向
    fund_flow_result = call_tool("get_fund_flow_realtime", code="000001")
    net_inflow = 0
    if isinstance(fund_flow_result, dict) and not fund_flow_result.get("error"):
        net_inflow = fund_flow_result.get("net_inflow", 0) or 0

    # 2. 板块强弱
    sectors = fetch_hot_sectors()
    strong_sectors = []
    weak_sectors = []
    if isinstance(sectors, dict) and not sectors.get("error"):
        for s in sectors.get("industry", [])[:15]:
            if s.get("change_pct", 0) > 0.3:
                strong_sectors.append({"name": s["name"], "change_pct": s["change_pct"]})
            elif s.get("change_pct", 0) < -0.3:
                weak_sectors.append({"name": s["name"], "change_pct": s["change_pct"]})

    # 3. 早盘情绪评分（不依赖涨跌停数据）
    mood_score = 50
    if net_inflow > 0:
        mood_score += min(25, net_inflow // 8000000)
    else:
        mood_score += max(-25, net_inflow // 8000000)

    strong_count = len(strong_sectors)
    weak_count = len(weak_sectors)
    if strong_count > weak_count * 2:
        mood_score += 15
    elif weak_count > strong_count * 2:
        mood_score -= 15

    mood_score = max(0, min(100, mood_score))

    if mood_score >= 70:
        mood = "偏强"
    elif mood_score >= 50:
        mood = "中性"
    elif mood_score >= 30:
        mood = "偏弱"
    else:
        mood = "弱势"

    highlights = []
    warnings = []
    if mood_score >= 70:
        highlights.append(f"早盘情绪偏强({mood_score})")
    elif mood_score <= 30:
        warnings.append(f"早盘情绪偏弱({mood_score})")
    if strong_count >= 8:
        highlights.append(f"{strong_count}个板块上涨")
    if weak_count >= 8:
        warnings.append(f"{weak_count}个板块下跌")

    return {
        "fund_flow": net_inflow,
        "mood": mood,
        "mood_score": mood_score,
        "strong_sectors": strong_sectors,
        "weak_sectors": weak_sectors,
        "strong_count": strong_count,
        "weak_count": weak_count,
        "evaluation": {
            "score": mood_score,
            "scores": {"mood": mood_score},
            "highlights": highlights,
            "warnings": warnings,
        },
    }


# ═══════════════════════════════════════════════════════════════
#  早盘条件搜索
# ═══════════════════════════════════════════════════════════════

def _search_early_candidates(market: Dict) -> List[Dict]:
    """早盘条件搜索：根据板块强弱 + 量价形态生成搜索条件。

    与 intraday 的区别：
    - 不依赖 main_themes，直接用 strong_sectors 的板块名
    - 搜索条件更宽泛（早盘数据少，宁多勿漏）
    """
    queries = []
    strong_sectors = market.get("strong_sectors", [])
    mood_score = market.get("mood_score", 50)

    # 强势板块龙头
    if strong_sectors:
        top = strong_sectors[0]["name"]
        queries.append(f"{top} 涨幅靠前")
        if len(strong_sectors) >= 2:
            second = strong_sectors[1]["name"]
            queries.append(f"{second} 放量上涨")

    # 通用量价条件
    if mood_score >= 60:
        queries.append("放量突破 站上5日均线")
    elif mood_score >= 40:
        queries.append("低开高走 放量")
    else:
        queries.append("逆势上涨 板块龙头")

    seen_codes = set()
    candidates = []
    for q in queries[:3]:
        result = call_tool("search_stocks", query=q, top_n=15)
        if isinstance(result, dict) and result.get("stocks"):
            for r in result["stocks"]:
                code = r.get("code", "")
                if code and code not in seen_codes:
                    seen_codes.add(code)
                    candidates.append({
                        "code": code,
                        "name": r.get("name", ""),
                        "source": "条件搜索",
                        "search_query": q,
                        "change_pct": r.get("change_pct", 0),
                        "turnover_pct": r.get("turnover_pct", 0),
                    })

    return candidates


# ═══════════════════════════════════════════════════════════════
#  5 分钟 K 线早盘趋势判断
# ═══════════════════════════════════════════════════════════════

def _fetch_5m_kline(code: str, days: int = 3) -> List[Dict]:
    """获取 5 分钟 K 线。"""
    try:
        writer = _get_writer()
        end = datetime.now().strftime("%Y-%m-%d")
        from datetime import timedelta
        start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        data = writer.query("CNStock", code, "5m", start_time=start, end_time=end, limit=0)
        if not data:
            return []
        return [{
            "time": str(r["time"]), "open": float(r["open"]),
            "high": float(r["high"]), "low": float(r["low"]),
            "close": float(r["close"]), "volume": float(r["volume"]),
        } for r in data]
    except Exception as e:
        logger.debug("[Early] %s 5m K线获取失败: %s", code, e)
        return []


def _analyze_early_5m(code: str) -> Optional[Dict]:
    """早盘 5m 趋势分析。

    返回：
        trend: "up" / "down" / "flat"
        vol_ratio: 量比（最近1根 vs 前5根均量）
        above_ma5: 是否站上 5m MA5
        macd_bar: 5m MACD 柱值
        signal: 信号摘要
    """
    bars = _fetch_5m_kline(code, days=3)
    if len(bars) < 15:
        return None

    closes = [b["close"] for b in bars]
    volumes = [b["volume"] for b in bars]
    n = len(bars)
    i = n - 1

    ma5 = compute_ma(closes, 5)
    macd = compute_macd(closes)

    # 量比
    if i >= 5 and sum(volumes[max(0, i-5):i]) > 0:
        avg_vol = sum(volumes[max(0, i-5):i]) / min(5, i)
        vol_ratio = volumes[i] / avg_vol if avg_vol > 0 else 1
    else:
        vol_ratio = 1

    # 趋势：最近5根收盘价方向
    if i >= 5:
        recent = closes[max(0, i-4):i+1]
        if recent[-1] > recent[0] * 1.005:
            trend = "up"
        elif recent[-1] < recent[0] * 0.995:
            trend = "down"
        else:
            trend = "flat"
    else:
        trend = "flat"

    above_ma5 = closes[i] > (ma5[i] or 0)
    macd_bar = macd["macd"][i]

    signal_parts = []
    if trend == "up" and above_ma5:
        signal_parts.append("5m上升趋势")
    if vol_ratio > 1.5:
        signal_parts.append(f"放量{vol_ratio:.1f}倍")
    if macd_bar > 0:
        signal_parts.append("MACD红柱")

    return {
        "trend": trend,
        "vol_ratio": round(vol_ratio, 2),
        "above_ma5": above_ma5,
        "macd_bar": round(macd_bar, 4),
        "signal": ", ".join(signal_parts) if signal_parts else "中性",
    }


# ═══════════════════════════════════════════════════════════════
#  prescreen 入口
# ═══════════════════════════════════════════════════════════════

def prescreen() -> Dict[str, Any]:
    """早盘预筛选。

    流程：
    1. 评估早盘市场状态（资金流向 + 板块强弱）
    2. 条件搜索候选（板块龙头 + 量价形态）
    3. 批量回填实时行情
    4. 换手率过滤
    """
    today = datetime.now().strftime("%Y-%m-%d")

    # 1. 市场状态
    market = _assess_early_market()
    logger.info("[Early] 市场状态: %s(score=%d) 强板块%d 弱板块%d",
                market["mood"], market["mood_score"],
                market["strong_count"], market["weak_count"])

    # 2. 条件搜索
    search_candidates = _search_early_candidates(market)
    logger.info("[Early] 条件搜索: %d 只候选", len(search_candidates))

    # 3. 合并候选
    candidates = {}
    for s in search_candidates:
        code = s.get("code", "")
        if code and code not in candidates:
            sq = s.get("search_query", "")
            candidates[code] = {
                "code": code, "name": s.get("name", ""),
                "source": f"条件搜索({sq})",
                "reason": sq,
                "change_pct": s.get("change_pct", 0),
                "turnover_pct": s.get("turnover_pct", 0),
                "tags": [], "pullback_signals": [],
                "continuous_days": 0,
            }

    # 4. 批量回填实时行情
    all_codes = list(candidates.keys())
    if all_codes:
        try:
            quote_raw = get_realtime_quote(",".join(all_codes))
            quotes = {}
            if isinstance(quote_raw, dict):
                if "data" in quote_raw:
                    quotes = quote_raw["data"]
                elif quote_raw.get("stock_code"):
                    quotes = {quote_raw["stock_code"]: quote_raw}
            for code_str, q in quotes.items():
                code = q.get("stock_code", code_str)
                if code in candidates:
                    candidates[code]["change_pct"] = q.get("change_pct", candidates[code].get("change_pct", 0))
                    candidates[code]["turnover_pct"] = q.get("turnover_pct", candidates[code].get("turnover_pct", 0))
                    candidates[code]["price"] = q.get("price", 0)
                    candidates[code]["vol_ratio"] = q.get("vol_ratio", 0)
        except Exception as e:
            logger.warning("[Early] 批量回填行情失败: %s", e)

    # 5. 过滤
    filtered = []
    for c in candidates.values():
        trn = c.get("turnover_pct", 0) or 0
        if trn > 0 and trn < 2.0:
            continue
        filtered.append(c)

    # 排序：涨幅优先
    filtered.sort(key=lambda x: -x.get("change_pct", 0))

    # 题材热点（早盘可能为空，不影响）
    main_themes = []

    return {
        "market": market,
        "main_themes": main_themes,
        "candidates": filtered[:20],
    }


# ═══════════════════════════════════════════════════════════════
#  analyze_code — 早盘深入分析
# ═══════════════════════════════════════════════════════════════

def analyze_code(code: str, name: str, _tool_calls, _tool_nodes, _missing_data) -> Optional[Dict]:
    """早盘深入分析：日线趋势 + 5m 入场信号。

    与 intraday.analyze_code 的区别：
    - 增加 5m 早盘趋势判断权重
    - 不依赖 15m（早盘数据不足）
    - 评分更保守（早盘不确定性高）
    """
    try:
        # 日线数据
        bars_1d = fetch_kline(code, days=60)
        if len(bars_1d) < 20:
            return None

        closes = [b["close"] for b in bars_1d]
        n = len(bars_1d)
        i = n - 1

        ma5 = compute_ma(closes, 5)
        ma20 = compute_ma(closes, 20)
        macd = compute_macd(closes)

        daily = {
            "close": closes[i],
            "ma5": ma5[i], "ma20": ma20[i],
            "macd_bar": macd["macd"][i],
            "above_ma5": closes[i] > (ma5[i] or 0),
            "above_ma20": closes[i] > (ma20[i] or 0),
        }

        # 5m 早盘趋势
        early_5m = _analyze_early_5m(code)

        # ── 基础分：调用 technical_analysis（与 stock_evaluation 一致）──
        from app.agent.tools.finance.technical_analysis import technical_analysis as _ta
        ta_result = _ta(code)
        if isinstance(ta_result, dict) and "error" not in ta_result:
            score = float(ta_result.get("score", 50))
        else:
            score = 50.0
        signals = []

        # 5m 早盘趋势（早盘核心信号）
        if early_5m:
            if early_5m["trend"] == "up" and early_5m["above_ma5"]:
                score += 12
                signals.append("5m上升趋势+站上MA5")
            elif early_5m["trend"] == "up":
                score += 6
                signals.append("5m上升趋势")

            if early_5m["vol_ratio"] > 1.5:
                score += 8
                signals.append(f"5m放量{early_5m['vol_ratio']}倍")

            if early_5m["macd_bar"] > 0:
                score += 3
                signals.append("5m MACD红柱")

        # 资金流向
        fund_flow = call_tool("get_fund_flow_realtime", code=code)
        if isinstance(fund_flow, dict) and not fund_flow.get("error"):
            net = fund_flow.get("net_inflow", 0) or 0
            if net > 0:
                score += 5
                signals.append(f"资金净流入{net/1e4:.0f}万")

        score = max(0, min(100, score))
        direction = "bullish" if score >= 60 else ("bearish" if score < 45 else "neutral")
        confidence = min(0.85, 0.35 + len(signals) * 0.05)  # 早盘置信度上限更低

        # ── 支撑位 / 压力位 ──
        from app.agent.tools.finance.analysis_tools import get_indicator_snapshot as _snap
        try:
            snap = _snap(code)
        except Exception:
            snap = {}
        boll = snap.get("boll", {}) if isinstance(snap, dict) else {}
        boll_upper = boll.get("upper") if isinstance(boll, dict) else None
        boll_lower = boll.get("lower") if isinstance(boll, dict) else None
        boll_mid = boll.get("mid") if isinstance(boll, dict) else None
        try:
            boll_upper = float(boll_upper) if boll_upper else None
            boll_lower = float(boll_lower) if boll_lower else None
            boll_mid = float(boll_mid) if boll_mid else None
        except (ValueError, TypeError):
            boll_upper = boll_lower = boll_mid = None

        resistance = None
        if boll_upper and boll_upper > daily["close"]:
            resistance = boll_mid if boll_mid and daily["close"] < boll_mid else boll_upper
        if resistance is None and daily.get("ma20") and daily["ma20"] > daily["close"]:
            resistance = daily["ma20"]
        if resistance is None:
            resistance = daily["close"] * 1.05

        support = None
        if boll_mid and daily["close"] > boll_mid:
            support = boll_mid
        elif boll_lower and boll_lower < daily["close"]:
            support = boll_lower
        if support is None and daily.get("ma20") and daily["ma20"] < daily["close"]:
            support = daily["ma20"]
        if support is None:
            support = daily["close"] * 0.95

        levels = {
            "resistance": round(resistance, 2),
            "support": round(support, 2),
            "upside_pct": round((resistance - daily["close"]) / daily["close"] * 100, 1),
            "downside_pct": round((daily["close"] - support) / daily["close"] * 100, 1),
        }

        return {
            "code": code, "name": name,
            "score": round(score, 1),
            "direction": direction,
            "confidence": round(confidence, 2),
            "signal": " | ".join(signals[:5]),
            "signals": signals,
            "factors": [],
            "levels": levels,
            "daily": daily,
            "early_5m": early_5m,
        }

    except Exception as e:
        logger.warning("[Early] 深入分析失败 %s: %s", code, e)
        return None

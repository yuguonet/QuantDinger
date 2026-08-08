# -*- coding: utf-8 -*-
"""
market_screener/intraday.py

策略 1 — 盘中短线 (09:30-14:29)

核心逻辑：
  1. 市场状态评估（资金流向 → 大盘强弱）
  2. 板块/概念分析（资金流入方向 → 选股条件）
  3. 条件选股（search_stocks）+ 连板/龙回头补充
  4. 多周期深入分析（1D/15m/5m）
  5. 弱转强/强转弱判断 + 量价分析

已验证策略参考：
  - BB 超卖：价格触 BB 下轨 + MA60 斜率 >= 0 + 振幅 > 8%
  - 4IN1：首板 V1 / 龙回头 A / 龙回头 B / 断板
"""

from __future__ import annotations

from app.agent.log import logger
from app.agent.tools.finance.data_tools import get_realtime_quote
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional


from .common import (
    FactorItem, SkillReport,
    call_tool, fetch_kline,
    fetch_zt_pool, fetch_dt_pool, fetch_broken_board,
    fetch_hot_stocks_with_reason, fetch_hot_sectors,
    compute_ma, compute_ema, compute_macd, compute_rsi,
    compute_volume_ratio, compute_kdj, compute_atr,
    scan_dragon_pullback, _get_writer,
)
# ═══════════════════════════════════════════════════════════════
#  多周期数据
# ═══════════════════════════════════════════════════════════════

def fetch_intraday_kline(code: str, timeframe: str = "15m", days: int = 10) -> List[Dict]:
    """获取分钟级K线数据（15m/5m/1m）。"""
    from app.data_sources.provider.adjustment import unadj_to_qfq
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        writer = _get_writer()
        data = writer.query("CNStock", code, timeframe, start_time=start, end_time=end, limit=0)
        if not data:
            return []
        bars = [{
            "time": str(r["time"]), "open": float(r["open"]),
            "high": float(r["high"]), "low": float(r["low"]),
            "close": float(r["close"]), "volume": float(r["volume"]),
        } for r in data]
        return bars
    except Exception as e:
        logger.debug("[MktScreen] %s %s K线获取失败: %s", code, timeframe, e)
        return []
# ═══════════════════════════════════════════════════════════════
#  市场状态评估
# ═══════════════════════════════════════════════════════════════

def assess_market_state() -> Dict[str, Any]:
    """评估市场状态：资金流向 + 涨跌停 + 板块强弱。

    返回：
        fund_flow: 大盘资金流向（净流入/净流出）
        mood: 市场情绪描述
        mood_score: 情绪评分 0-100
        strong_sectors: 资金流入的板块
        weak_sectors: 资金流出的板块
        zt_count / dt_count / broken_rate: 涨停/跌停/炸板率
    """
    today = datetime.now().strftime("%Y-%m-%d")

    # 1. 大盘资金流向
    fund_flow_result = call_tool("get_fund_flow_realtime", code="000001")  # 上证指数
    net_inflow = 0
    if isinstance(fund_flow_result, dict) and not fund_flow_result.get("error"):
        net_inflow = fund_flow_result.get("net_inflow", 0) or 0

    # 2. 涨跌停池
    zt_pool = fetch_zt_pool(today)
    dt_pool = fetch_dt_pool(today)
    broken = fetch_broken_board(today)
    zt_count = len(zt_pool)
    dt_count = len(dt_pool)
    broken_count = len(broken)
    broken_rate = round(broken_count / max(1, zt_count + broken_count) * 100, 1)

    # 3. 板块资金流向
    sectors = fetch_hot_sectors()
    strong_sectors = []
    weak_sectors = []
    if isinstance(sectors, dict) and not sectors.get("error"):
        for s in sectors.get("industry", [])[:10]:
            if s.get("change_pct", 0) > 0:
                strong_sectors.append({"name": s["name"], "change_pct": s["change_pct"]})
            else:
                weak_sectors.append({"name": s["name"], "change_pct": s["change_pct"]})

    # 4. 综合评分
    mood_score = 50
    if net_inflow > 0:
        mood_score += min(20, net_inflow // 10000000)  # 每千万加1分
    else:
        mood_score += max(-20, net_inflow // 10000000)
    if zt_count > 30:
        mood_score += 10
    elif zt_count < 15:
        mood_score -= 10
    if dt_count > 20:
        mood_score -= 15
    if broken_rate > 40:
        mood_score -= 10
    elif broken_rate < 20:
        mood_score += 5
    mood_score = max(0, min(100, mood_score))

    if mood_score >= 70:
        mood = "偏强"
    elif mood_score >= 50:
        mood = "中性"
    elif mood_score >= 30:
        mood = "偏弱"
    else:
        mood = "弱势"

    # ── highlights / warnings ──
    highlights = []
    warnings = []
    if mood_score >= 70:
        highlights.append(f"情绪偏强({mood_score})")
    elif mood_score <= 30:
        warnings.append(f"情绪偏弱({mood_score})")
    if zt_count > 80:
        highlights.append(f"涨停{zt_count}家，赚钱效应好")
    elif dt_count > 30:
        warnings.append(f"跌停{dt_count}家，亏钱效应")
    if net_inflow > 50000000:
        highlights.append(f"主力净流入{net_inflow/10000:.0f}万")
    elif net_inflow < -50000000:
        warnings.append(f"主力净流出{abs(net_inflow)/10000:.0f}万")

    return {
        "fund_flow": net_inflow,
        "mood": mood,
        "mood_score": mood_score,
        "strong_sectors": strong_sectors,
        "weak_sectors": weak_sectors,
        "zt_count": zt_count,
        "dt_count": dt_count,
        "broken_count": broken_count,
        "broken_rate": broken_rate,
        "evaluation": {
            "score": mood_score,
            "scores": {"mood": mood_score},
            "highlights": highlights,
            "warnings": warnings,
        },
    }
# ═══════════════════════════════════════════════════════════════
#  条件选股 — 根据市场状态生成搜索条件
# ═══════════════════════════════════════════════════════════════

def _search_by_condition(query: str, top_n: int = 20) -> List[Dict]:
    """调用 search_stocks 按条件搜索，返回候选股列表。"""
    result = call_tool("search_stocks", query=query, top_n=top_n)
    if isinstance(result, dict) and result.get("stocks"):
        return result["stocks"]
    return []
def search_candidates_by_market(market: Dict) -> List[Dict]:
    """根据市场状态生成选股条件，用 search_stocks 搜索。

    策略逻辑：
    - 市场偏强 → 搜索板块龙头、放量突破
    - 市场中性 → 搜索弱转强、缩量企稳
    - 市场偏弱 → 搜索逆势强势、BB超卖反弹
    """
    candidates = []
    mood_score = market["mood_score"]
    strong_sectors = market["strong_sectors"]

    # 根据市场状态选择搜索条件
    queries = []
    if mood_score >= 60:
        # 偏强：找板块龙头 + 放量突破
        queries.append("放量突破 站上20日均线")
        if strong_sectors:
            top_sector = strong_sectors[0]["name"]
            queries.append(f"{top_sector} 涨幅靠前")
    elif mood_score >= 40:
        # 中性：找弱转强 + 缩量企稳
        queries.append("弱转强 站上5日均线")
        queries.append("缩量企稳 底部放量")
    else:
        # 偏弱：找逆势强势 + 超卖反弹
        queries.append("逆势上涨 放量")
        queries.append("BB下轨 超卖反弹")

    # 板块条件
    if strong_sectors:
        sector_name = strong_sectors[0]["name"]
        queries.append(f"{sector_name} 主力资金流入")

    seen_codes = set()
    for q in queries[:3]:  # 最多3次搜索
        results = _search_by_condition(q, top_n=15)
        for r in results:
            code = r.get("code", "")
            if code and code not in seen_codes:
                seen_codes.add(code)
                r["source"] = "条件搜索"
                r["search_query"] = q
                candidates.append(r)

    return candidates
# ═══════════════════════════════════════════════════════════════
#  多周期分析
# ═══════════════════════════════════════════════════════════════

def analyze_multitimeframe(code: str) -> Dict[str, Any]:
    """多周期分析：1D + 15m + 5m。

    返回：
        daily: 日线趋势、均线、MACD
        intraday_15m: 15分钟趋势
        intraday_5m: 5分钟入场信号
        weak_to_strong: 是否弱转强
        strong_to_weak: 是否强转弱
        volume_pattern: 量价形态
    """
    result = {
        "daily": {}, "intraday_15m": {}, "intraday_5m": {},
        "weak_to_strong": False, "strong_to_weak": False,
        "volume_pattern": "neutral",
    }

    # ── 日线分析 ──
    bars_1d = fetch_kline(code, days=60)
    if len(bars_1d) < 20:
        return result

    closes = [b["close"] for b in bars_1d]
    volumes = [b["volume"] for b in bars_1d]
    highs = [b["high"] for b in bars_1d]
    lows = [b["low"] for b in bars_1d]
    n = len(bars_1d)

    ma5 = compute_ma(closes, 5)
    ma10 = compute_ma(closes, 10)
    ma20 = compute_ma(closes, 20)
    ma60 = compute_ma(closes, 60) if n >= 60 else [None] * n
    macd = compute_macd(closes)
    rsi = compute_rsi(closes)
    vol_ratio = compute_volume_ratio(volumes, 5)

    i = n - 1  # 最新一根

    daily = {
        "close": closes[i],
        "ma5": ma5[i], "ma10": ma10[i], "ma20": ma20[i], "ma60": ma60[i],
        "macd_bar": macd["macd"][i],
        "rsi": rsi[i],
        "vol_ratio": vol_ratio[i],
        "above_ma5": closes[i] > (ma5[i] or 0),
        "above_ma20": closes[i] > (ma20[i] or 0),
        "ma5_slope": (ma5[i] - ma5[i-1]) / ma5[i-1] * 100 if ma5[i-1] and ma5[i-1] > 0 else 0,
        "ma60_slope": (ma60[i] - ma60[i-1]) / ma60[i-1] * 100 if ma60[i] and ma60[i-1] and ma60[i-1] > 0 else 0,
    }
    result["daily"] = daily

    # ── 弱转强判断 ──
    # 条件：之前在日均线附近震荡 → 现在站稳日均线上方
    # MACD 绿柱阶段价格仍能稳定在日均线上方
    if i >= 5:
        recent_above_ma5 = sum(1 for j in range(i-4, i+1) if closes[j] > (ma5[j] or 0))
        prev_below_or_near = sum(1 for j in range(i-9, i-4) if closes[j] <= (ma5[j] or 0) * 1.01)

        # 弱转强：最近5天多数在MA5上方，之前5天多数在MA5下方或附近
        if recent_above_ma5 >= 4 and prev_below_or_near >= 3:
            result["weak_to_strong"] = True

        # MACD 绿柱但价格在 MA5 上方 = 弱转强信号
        if macd["macd"][i] < 0 and closes[i] > (ma5[i] or 0):
            if recent_above_ma5 >= 3:
                result["weak_to_strong"] = True

    # ── 强转弱判断 ──
    if i >= 5:
        recent_below_ma5 = sum(1 for j in range(i-4, i+1) if closes[j] < (ma5[j] or 0))
        prev_above = sum(1 for j in range(i-9, i-4) if closes[j] > (ma5[j] or 0))
        if recent_below_ma5 >= 4 and prev_above >= 3:
            result["strong_to_weak"] = True

    # ── 量价形态 ──
    if vol_ratio[i] > 1.5 and closes[i] > closes[i-1]:
        result["volume_pattern"] = "放量上涨"
    elif vol_ratio[i] < 0.7 and closes[i] > closes[i-1]:
        result["volume_pattern"] = "缩量上涨(拉升)"
    elif vol_ratio[i] > 1.5 and closes[i] < closes[i-1]:
        result["volume_pattern"] = "放量下跌(抛盘)"
    elif vol_ratio[i] < 0.7 and closes[i] < closes[i-1]:
        result["volume_pattern"] = "缩量下跌"

    # ── 15分钟分析 ──
    bars_15m = fetch_intraday_kline(code, "15m", days=5)
    if len(bars_15m) >= 10:
        closes_15m = [b["close"] for b in bars_15m]
        ma5_15m = compute_ma(closes_15m, 5)
        rsi_15m = compute_rsi(closes_15m, 14)
        k15 = len(bars_15m) - 1
        result["intraday_15m"] = {
            "close": closes_15m[k15],
            "above_ma5": closes_15m[k15] > (ma5_15m[k15] or 0),
            "rsi": rsi_15m[k15],
            "trend": "up" if closes_15m[k15] > closes_15m[max(0, k15-5)] else "down",
        }

    # ── 5分钟分析（入场信号 + MACD 承压/拉升强度）──
    bars_5m = fetch_intraday_kline(code, "5m", days=3)
    if len(bars_5m) >= 20:
        closes_5m = [b["close"] for b in bars_5m]
        highs_5m = [b["high"] for b in bars_5m]
        lows_5m = [b["low"] for b in bars_5m]
        volumes_5m = [b["volume"] for b in bars_5m]
        ma5_5m = compute_ma(closes_5m, 5)
        macd_5m = compute_macd(closes_5m)
        k5 = len(bars_5m) - 1
        vol_ratio_5m = volumes_5m[k5] / (sum(volumes_5m[max(0,k5-5):k5]) / min(5, k5)) if k5 > 0 else 1

        # ── MACD 红绿柱 vs 价格波动分析 ──
        # 绿柱对比价格看承压，红柱对比价格看拉升强度
        # 红绿柱高度相近 + 绿柱价格波动 < 红柱价格波动 = 强势
        macd_bars_5m = macd_5m["macd"]
        green_ranges = []  # 绿柱期间的价格波动范围
        red_ranges = []    # 红柱期间的价格波动范围
        green_heights = []
        red_heights = []
        # 取最近 20 根 K 线分析
        start_idx = max(0, k5 - 19)
        for j in range(start_idx, k5 + 1):
            bar_range = highs_5m[j] - lows_5m[j]
            if macd_bars_5m[j] < 0:
                green_ranges.append(bar_range)
                green_heights.append(abs(macd_bars_5m[j]))
            elif macd_bars_5m[j] > 0:
                red_ranges.append(bar_range)
                red_heights.append(macd_bars_5m[j])

        macd_strength = "neutral"
        if green_ranges and red_ranges:
            avg_green_range = sum(green_ranges) / len(green_ranges)
            avg_red_range = sum(red_ranges) / len(red_ranges)
            avg_green_height = sum(green_heights) / len(green_heights) if green_heights else 0
            avg_red_height = sum(red_heights) / len(red_heights) if red_heights else 0

            # 红绿柱高度相近（比例在 0.6~1.5 之间）
            height_ratio = avg_green_height / avg_red_height if avg_red_height > 0 else 1
            if 0.6 <= height_ratio <= 1.5:
                # 绿柱价格波动 < 红柱价格波动 → 强势（绿柱时卖压打不下去）
                if avg_green_range < avg_red_range * 0.8:
                    macd_strength = "strong"
                # 绿柱价格波动 > 红柱价格波动 → 弱势
                elif avg_green_range > avg_red_range * 1.2:
                    macd_strength = "weak"

            # 强势股绿柱本身就小
            if avg_green_height < avg_red_height * 0.5:
                macd_strength = "strong"

        result["intraday_5m"] = {
            "close": closes_5m[k5],
            "above_ma5": closes_5m[k5] > (ma5_5m[k5] or 0),
            "vol_ratio": round(vol_ratio_5m, 2),
            "recent_highs": max(closes_5m[max(0, k5-10):k5+1]),
            "near_high": closes_5m[k5] >= max(closes_5m[max(0, k5-10):k5+1]) * 0.98,
            "macd_strength": macd_strength,
            "green_avg_range": round(sum(green_ranges) / len(green_ranges), 3) if green_ranges else 0,
            "red_avg_range": round(sum(red_ranges) / len(red_ranges), 3) if red_ranges else 0,
        }

    return result
# ═══════════════════════════════════════════════════════════════
#  技术快检（保留原有，用于 Phase 1 快速筛选）
# ═══════════════════════════════════════════════════════════════

def tech_check(code: str) -> Optional[Dict]:
    """快速技术检查（日线级别），用于 Phase 1 初筛。"""
    bars = fetch_kline(code, days=60)
    if len(bars) < 20:
        return None
    closes = [b["close"] for b in bars]
    volumes = [b["volume"] for b in bars]
    n = len(bars)
    i = n - 1

    ma5 = compute_ma(closes, 5)
    ma10 = compute_ma(closes, 10)
    ma20 = compute_ma(closes, 20)
    macd = compute_macd(closes)
    rsi = compute_rsi(closes)
    vol_ratio = compute_volume_ratio(volumes, 5)

    return {
        "close": closes[i],
        "ma5": ma5[i], "ma10": ma10[i], "ma20": ma20[i],
        "macd_bar": macd["macd"][i], "rsi": rsi[i],
        "vol_ratio": vol_ratio[i],
        "above_ma5": closes[i] > (ma5[i] or 0),
        "above_ma20": closes[i] > (ma20[i] or 0),
    }
# ═══════════════════════════════════════════════════════════════
#  Phase 1 — 预筛选
# ═══════════════════════════════════════════════════════════════

def prescreen(date: str) -> Dict[str, Any]:
    """盘中预筛选主入口。

    流程：
    1. 评估市场状态（资金流向、涨跌停、板块强弱）
    2. 根据市场状态用 search_stocks 搜索候选
    3. 补充连板 + 龙回头
    4. 合并候选池，批量回填实时行情（价格、涨幅、换手率、量比等）
    5. 换手率>=2% 过滤
    6. 按来源+强度排序，返回前 20 只（不含涨停封板排除，由 agent 按 SKILL.md 规则处理）
    """
    # ── 1. 市场状态评估 ──
    market = assess_market_state()
    logger.info("[MktScreen] 市场状态: %s (score=%d) 资金=%s 涨停%d 跌停%d",
                market["mood"], market["mood_score"],
                f"{market['fund_flow']/1e8:.1f}亿" if market["fund_flow"] else "N/A",
                market["zt_count"], market["dt_count"])

    # ── 2. 条件选股 ──
    search_candidates = search_candidates_by_market(market)
    logger.info("[MktScreen] 条件搜索: %d 只候选", len(search_candidates))

    # ── 3. 连板 + 龙回头 ──
    zt_pool = fetch_zt_pool(date)
    continuous_board = []
    for s in zt_pool:
        days = int(s.get("continuous_zt_days", 1) or 1)
        if days >= 2:
            continuous_board.append({
                "code": s.get("stock_code", ""),
                "name": s.get("stock_name", ""),
                "continuous_days": days,
                "zt_time": s.get("zt_time", ""),
                "seal_amount": s.get("seal_amount", 0),
                "turnover": s.get("turnover_rate", 0),
                "reason": s.get("reason", ""),
            })
    continuous_board.sort(key=lambda x: (-x["continuous_days"], x["zt_time"] or "99:99"))

    dragon_pullback = scan_dragon_pullback(date)
    logger.info("[MktScreen] 连板: %d只, 龙回头: %d只", len(continuous_board), len(dragon_pullback))

    # ── 题材热点 ──
    hot_data = fetch_hot_stocks_with_reason(date)
    hot_tags = hot_data.get("hot_tags", [])
    main_themes = [(tag, cnt) for tag, cnt in hot_tags[:5]]

    # ── 4. 合并候选池 ──
    candidates = {}

    # 连板
    for s in continuous_board:
        code = s["code"]
        if code not in candidates:
            candidates[code] = {
                "code": code, "name": s["name"], "source": "连板",
                "continuous_days": s["continuous_days"],
                "zt_time": s["zt_time"], "reason": s["reason"],
                "change_pct": 0, "tags": [], "pullback_signals": [],
                "turnover_pct": s.get("turnover", 0),
            }

    # 龙回头
    for s in dragon_pullback[:10]:
        code = s["code"]
        if code not in candidates:
            candidates[code] = {
                "code": code, "name": s["name"], "source": "龙回头",
                "continuous_days": s.get("max_continuous_days", 0),
                "zt_time": "", "reason": s["reason"], "change_pct": 0,
                "tags": [], "pullback_signals": s["signals"],
                "pullback_pct": s["pullback_pct"],
                "strength_score": s["strength_score"],
                "turnover_pct": 0,
            }

    # 条件搜索结果
    for s in search_candidates:
        code = s.get("code", "")
        if code and code not in candidates:
            sq = s.get("search_query", "")
            candidates[code] = {
                "code": code, "name": s.get("name", ""),
                "source": f"条件搜索({sq})",
                "continuous_days": 0, "zt_time": "",
                "reason": sq,  # 搜索关键词作为 reason，用于主题匹配
                "change_pct": s.get("change_pct", 0),
                "tags": [], "pullback_signals": [],
                "turnover_pct": s.get("turnover_pct", 0),
            }

    # ── 5. 批量回填实时行情 ──
    all_codes = [c["code"] for c in candidates.values() if c.get("code")]
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
                    candidates[code]["high"] = q.get("high", 0)
                    candidates[code]["low"] = q.get("low", 0)
        except Exception as e:
            logger.warning("[MktScreen] 批量回填行情失败: %s", e)

    # ── 6. 过滤 ──
    candidate_list = list(candidates.values())
    filtered = []
    for c in candidate_list:
        # 换手率 < 2% 排除（活跃度不够，turnover_pct=0 时不过滤避免误杀）
        if c.get("turnover_pct", 0) > 0 and c["turnover_pct"] < 2.0:
            continue
        filtered.append(c)

    # 排序：连板 > 龙回头 > 条件搜索，同类型按连续天数/强度排序
    source_priority = {"连板": 0, "龙回头": 1, "条件搜索": 2}
    filtered.sort(key=lambda x: (
        min(source_priority.get(s, 9) for s in x.get("source", "").split("+")),
        -(x.get("continuous_days", 0)),
        -x.get("strength_score", 0),
    ))

    return {
        "market": market,
        "main_themes": main_themes[:10],
        "continuous_board": continuous_board[:10],
        "dragon_pullback": dragon_pullback[:10],
        "candidates": filtered[:20],
    }
# ═══════════════════════════════════════════════════════════════
#  Phase 2 — 多周期深入分析
# ═══════════════════════════════════════════════════════════════

def analyze_code(
    code: str, name: str,
    _tool_calls, _tool_nodes, _missing_data,
) -> Optional[Dict]:
    """单个股票深入分析：多周期 + 量价 + 弱转强判断。

    Args:
        code: 股票代码（如 "000001"）
        name: 股票名称

    评分逻辑（纯技术面）：
    - 基础分 55
    - 弱转强 +15, 强转弱 -15
    - 量价配合 +10~+15
    - 多周期共振 +10
    """
    try:
        fund_flow = call_tool("get_fund_flow_realtime", code=code)
        mtf = analyze_multitimeframe(code)

        if _tool_calls is not None:
            for t in ["get_fund_flow_realtime"]:
                if t not in _tool_calls:
                    _tool_calls.append(t)

        daily = mtf.get("daily", {})
        if not daily:
            return None

        # ── 基础分：调用 technical_analysis（与 stock_evaluation 一致）──
        from app.agent.tools.finance.technical_analysis import technical_analysis as _ta
        ta_result = _ta(code)
        if isinstance(ta_result, dict) and "error" not in ta_result:
            score = float(ta_result.get("score", 50))
        else:
            score = 50.0
        signals = []
        factors = []

        # ── 盘中特有信号（technical_analysis 不覆盖的多周期信号）──
        # 注：均线/MACD/RSI/量价等日线指标已在 technical_analysis 五维加权中计算，不重复叠加

        # 弱转强/强转弱（日线+5m 多周期判断，technical_analysis 不含）
        if mtf.get("weak_to_strong"):
            score += 15
            signals.append("弱转强")
            factors.append(FactorItem(name="趋势转折", value="弱转强", score=85))
        elif mtf.get("strong_to_weak"):
            score -= 15
            signals.append("强转弱")
            factors.append(FactorItem(name="趋势转折", value="强转弱", score=20))

        # 多周期共振（15m+5m，technical_analysis 不含）
        mtf_score = 0
        if daily.get("above_ma5") and mtf.get("intraday_15m", {}).get("above_ma5"):
            mtf_score += 5
            signals.append("日线+15m共振")
        if mtf.get("intraday_5m", {}).get("above_ma5") and mtf.get("intraday_15m", {}).get("above_ma5"):
            mtf_score += 5

        macd_5m_strength = mtf.get("intraday_5m", {}).get("macd_strength", "neutral")
        if macd_5m_strength == "strong":
            mtf_score += 8
            signals.append("5m MACD强势(绿柱价格波动小)")
        elif macd_5m_strength == "weak":
            mtf_score -= 5
            signals.append("5m MACD弱势(绿柱价格波动大)")

        if mtf_score > 0:
            score += mtf_score
            factors.append(FactorItem(name="多周期共振", value=f"+{mtf_score}", score=70))

        # ── 最终评分 ──
        score = max(0, min(100, score))
        direction = "bullish" if score >= 60 else ("bearish" if score < 45 else "neutral")
        confidence = min(0.9, 0.4 + len(signals) * 0.05)

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
            "code": code,
            "name": name,
            "score": round(score, 1),
            "direction": direction,
            "confidence": round(confidence, 2),
            "signal": " | ".join(signals[:5]),
            "signals": signals,
            "factors": factors,
            "levels": levels,
            "daily": daily,
            "intraday_15m": mtf.get("intraday_15m", {}),
            "intraday_5m": mtf.get("intraday_5m", {}),
            "weak_to_strong": mtf.get("weak_to_strong", False),
            "volume_pattern": mtf.get("volume_pattern", "neutral"),
        }

    except Exception as e:
        logger.warning("[MktScreen] 深入分析失败 %s: %s", code, e)
        return None

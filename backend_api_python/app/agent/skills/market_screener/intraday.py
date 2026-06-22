# -*- coding: utf-8 -*-
"""
market_screener/intraday.py

策略 1 — 盘中短线 (09:30-14:29)
涨停池连板 + 主线题材龙头 + 龙回头弱转强
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any, Dict, List, Optional

from app.agent.chain.schema import FactorItem, SkillReport
from .common import (
    call_tool, fetch_kline, get_limit_pct,
    fetch_zt_pool, fetch_dt_pool, fetch_broken_board,
    fetch_hot_stocks_with_reason, fetch_hot_sectors,
    compute_ma, compute_rsi, compute_volume_ratio,
    scan_dragon_pullback,
)

logger = logging.getLogger(__name__)


def tech_check(code: str) -> Optional[Dict]:
    bars = fetch_kline(code, days=60)
    if len(bars) < 20:
        return None
    closes = [b["close"] for b in bars]
    volumes = [b["volume"] for b in bars]
    i = len(bars) - 1
    ma5 = compute_ma(closes, 5)
    ma10 = compute_ma(closes, 10)
    ma20 = compute_ma(closes, 20)
    rsi = compute_rsi(closes)
    vol_ratio = compute_volume_ratio(volumes)
    ma_bullish = (
        ma5[i] is not None and ma10[i] is not None and ma20[i] is not None
        and ma5[i] > ma10[i] > ma20[i]
    )
    recent_vol_ratio = vol_ratio[i] if i < len(vol_ratio) else 0
    return {
        "close": round(closes[i], 3),
        "ma5": round(ma5[i], 3) if ma5[i] else None,
        "ma10": round(ma10[i], 3) if ma10[i] else None,
        "ma20": round(ma20[i], 3) if ma20[i] else None,
        "rsi": round(rsi[i], 2),
        "vol_ratio": round(recent_vol_ratio, 3),
        "ma_bullish": ma_bullish,
    }


def prescreen(date: str) -> Dict[str, Any]:
    zt_pool = fetch_zt_pool(date)
    dt_pool = fetch_dt_pool(date)
    broken_pool = fetch_broken_board(date)
    hot_data = fetch_hot_stocks_with_reason(date)
    sector_data = fetch_hot_sectors()

    hot_stocks = hot_data.get("stocks", [])
    hot_tags = hot_data.get("hot_tags", [])
    industry_sectors = sector_data.get("industry", [])
    concept_sectors = sector_data.get("concept", [])

    zt_count = len(zt_pool)
    dt_count = len(dt_pool)
    broken_count = len(broken_pool)
    broken_rate = broken_count / max(1, zt_count + broken_count)

    if zt_count >= 50 and dt_count <= 10:
        mood = "亢奋"
        mood_score = 80
    elif zt_count >= 30 and dt_count <= 20:
        mood = "偏暖"
        mood_score = 65
    elif zt_count < 20 or dt_count > 30:
        mood = "冰点"
        mood_score = 25
    else:
        mood = "中性"
        mood_score = 50

    market_summary = {
        "zt_count": zt_count, "dt_count": dt_count,
        "broken_count": broken_count,
        "broken_rate": round(broken_rate * 100, 1),
        "mood": mood, "mood_score": mood_score,
    }

    zt_reason_counter: Counter = Counter()
    for s in zt_pool:
        reason = s.get("reason", "") or ""
        if reason:
            for tag in reason.replace("，", "+").replace(",", "+").split("+"):
                tag = tag.strip()
                if tag:
                    zt_reason_counter[tag] += 1

    combined_tags: Counter = Counter()
    for tag, cnt in hot_tags:
        combined_tags[tag] += cnt
    for tag, cnt in zt_reason_counter.items():
        combined_tags[tag] += cnt

    main_themes = combined_tags.most_common(10)

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

    main_theme_keywords = {tag for tag, _ in main_themes[:5]}

    theme_stocks = []
    for s in hot_stocks:
        reason = s.get("reason", "") or ""
        if not reason:
            continue
        matched_tags = [tag for tag in reason.replace("，", "+").replace(",", "+").split("+")
                        if tag.strip() in main_theme_keywords]
        if matched_tags:
            theme_stocks.append({
                "code": s.get("code", ""), "name": s.get("name", ""),
                "reason": reason, "matched_tags": matched_tags,
                "change_pct": s.get("change_pct", 0),
                "turnover_pct": s.get("turnover_pct", 0),
                "amount": s.get("amount", 0),
            })
    theme_stocks.sort(key=lambda x: -x["change_pct"])

    dragon_pullback = scan_dragon_pullback(date)

    candidates = {}
    for s in continuous_board:
        code = s["code"]
        candidates[code] = {
            "code": code, "name": s["name"], "source": "连板",
            "continuous_days": s["continuous_days"], "zt_time": s["zt_time"],
            "reason": s["reason"], "change_pct": 0, "tags": [],
            "pullback_signals": [],
        }
    for s in theme_stocks[:20]:
        code = s["code"]
        if code in candidates:
            candidates[code]["source"] += "+主线题材"
            candidates[code]["tags"] = s["matched_tags"]
        else:
            candidates[code] = {
                "code": code, "name": s["name"], "source": "主线题材",
                "continuous_days": 0, "zt_time": "", "reason": s["reason"],
                "change_pct": s["change_pct"], "tags": s["matched_tags"],
                "pullback_signals": [],
            }
    for s in dragon_pullback[:10]:
        code = s["code"]
        if code in candidates:
            candidates[code]["source"] += "+龙回头"
            candidates[code]["pullback_signals"] = s["signals"]
        else:
            candidates[code] = {
                "code": code, "name": s["name"], "source": "龙回头",
                "continuous_days": s.get("max_continuous_days", 0),
                "zt_time": "", "reason": s["reason"], "change_pct": 0,
                "tags": [], "pullback_signals": s["signals"],
                "pullback_pct": s["pullback_pct"],
                "strength_score": s["strength_score"],
            }

    candidate_list = list(candidates.values())

    filtered = []
    for c in candidate_list:
        code = c.get("code", "")
        name = c.get("name", "")
        change_pct = c.get("change_pct", 0)
        limit_pct = get_limit_pct(code, name)

        if "连板" in c.get("source", "") and "龙回头" not in c.get("source", ""):
            continue
        if change_pct >= limit_pct - 0.5:
            continue
        filtered.append(c)

    source_priority = {"连板": 0, "龙回头": 1, "主线题材": 2}
    filtered.sort(key=lambda x: (
        min(source_priority.get(s, 9) for s in x.get("source", "").split("+")),
        -(x.get("continuous_days", 0)),
        -x.get("strength_score", 0),
    ))

    return {
        "market": market_summary,
        "main_themes": main_themes[:10],
        "continuous_board": continuous_board[:10],
        "dragon_pullback": dragon_pullback[:10],
        "candidates": filtered[:20],
    }


def deep_analyze(
    candidate: Dict, tech: Optional[Dict],
    _tool_calls, _tool_nodes, _missing_data,
) -> Optional[Dict]:
    code = candidate["code"]
    try:
        fund_flow = call_tool("get_fund_flow_realtime", stock_code=code)
        snapshot = call_tool("get_indicator_snapshot", stock_code=code)

        if _tool_calls is not None:
            for t in ["get_fund_flow_realtime", "get_indicator_snapshot"]:
                if t not in _tool_calls:
                    _tool_calls.append(t)

        score = 55.0
        signals = []
        factors = []

        source = candidate.get("source", "")
        if "连板" in source:
            score += 12
            signals.append(f"{candidate.get('continuous_days', 1)}连板")
        if "主线题材" in source:
            score += 8
            tags_str = "+".join(candidate.get("tags", []))
            signals.append(f"主线题材:{tags_str}")
        if "龙回头" in source:
            score += 10
            pb_signals = candidate.get("pullback_signals", [])
            signals.append(f"龙回头弱转强:{','.join(pb_signals[:3])}")
            pullback_pct = candidate.get("pullback_pct", 0)
            if pullback_pct:
                signals.append(f"回调{pullback_pct}%")
        factors.append(FactorItem(
            name="来源", value=source,
            score=70 if "连板" in source else (68 if "龙回头" in source else (60 if "主线题材" in source else 50)),
        ))

        if tech:
            if tech["ma_bullish"]:
                score += 8
                signals.append("均线多头排列")
            if 40 <= tech["rsi"] <= 70:
                score += 3
                signals.append(f"RSI{tech['rsi']}适中")
            elif tech["rsi"] > 80:
                score -= 5
                signals.append(f"RSI{tech['rsi']}超买")
            if tech["vol_ratio"] > 1.5:
                score += 5
                signals.append(f"量比{tech['vol_ratio']}放量")
            factors.append(FactorItem(
                name="技术面",
                value=f"MA5={tech['ma5']} RSI={tech['rsi']} 量比={tech['vol_ratio']}",
                score=70 if tech["ma_bullish"] else 50,
            ))

        if isinstance(snapshot, dict) and "error" not in snapshot:
            macd = snapshot.get("macd", {})
            macd_sig = str(macd.get("signal", ""))
            if "金叉" in macd_sig:
                score += 6
                signals.append("MACD金叉")
            elif "死叉" in macd_sig:
                score -= 4
                signals.append("MACD死叉")
            factors.append(FactorItem(
                name="MACD",
                value=f"DIF={macd.get('dif', '?')} DEA={macd.get('dea', '?')}",
                score=65 if "金叉" in macd_sig else (35 if "死叉" in macd_sig else 50),
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

        reason = candidate.get("reason", "")
        if "ST" in reason or "退市" in reason:
            score -= 20
            signals.append("ST/退市风险")

        score = max(0, min(100, score))
        direction = "bullish" if score >= 60 else ("bearish" if score <= 40 else "neutral")

        return {
            "code": code, "name": candidate.get("name", ""),
            "source": source, "reason": reason,
            "score": round(score, 1), "direction": direction,
            "signal": ", ".join(signals[:5]),
            "factors": [f.to_dict() for f in factors],
            "tech": tech,
        }
    except Exception as e:
        logger.warning("[MktScreen] 盘中深入分析 %s 失败: %s", code, e)
        return None


def run_strategy(date: str, _tool_calls, _tool_nodes, _missing_data) -> Optional[SkillReport]:
    try:
        prescreen_result = prescreen(date)
    except Exception as e:
        logger.warning("[MktScreen] 盘中预筛选失败: %s", e)
        return None

    market = prescreen_result["market"]
    main_themes = prescreen_result["main_themes"]
    candidates = prescreen_result["candidates"]

    logger.info("[MktScreen] 盘中预筛选: 涨停%d 跌停%d 候选%d只",
                market["zt_count"], market["dt_count"], len(candidates))

    if market["mood_score"] < 30:
        return SkillReport(
            skill_name="market_screener", score=25.0, direction="bearish",
            confidence=0.7,
            signal=f"市场冰点（涨停{market['zt_count']}跌停{market['dt_count']}），不宜短线",
            analysis=(
                f"## 盘中短线选股 — 市场冰点\n\n"
                f"涨停 {market['zt_count']} 只，跌停 {market['dt_count']} 只，"
                f"炸板率 {market['broken_rate']}%。\n\n"
                f"**建议：空仓观望，等待情绪回暖。**"
            ),
            factors=[
                FactorItem(name="涨停数", value=str(market["zt_count"]), score=market["mood_score"]),
                FactorItem(name="跌停数", value=str(market["dt_count"]), score=max(0, 100 - market["dt_count"] * 3)),
                FactorItem(name="炸板率", value=f"{market['broken_rate']}%", score=max(0, 100 - int(market["broken_rate"]))),
            ],
            status="ok",
        )

    if not candidates:
        return SkillReport(
            skill_name="market_screener", score=45.0, direction="neutral",
            confidence=0.5, signal="今日无明确短线标的",
            analysis=(
                f"## 盘中短线选股 — 无明确标的\n\n"
                f"市场情绪：{market['mood']}（涨停{market['zt_count']}跌停{market['dt_count']}）\n"
                f"今日无连板股或主线题材强势股进入候选池。"
            ),
            factors=[FactorItem(name="市场情绪", value=market["mood"], score=market["mood_score"])],
            status="ok",
        )

    tech_results = {}
    for c in candidates[:15]:
        tech = tech_check(c["code"])
        if tech:
            tech_results[c["code"]] = tech

    analyzed = []
    for c in candidates[:8]:
        tech = tech_results.get(c["code"])
        result = deep_analyze(c, tech, _tool_calls, _tool_nodes, _missing_data)
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
    confidence = min(0.9, 0.4 + len(analyzed) * 0.06)

    factors = [
        FactorItem(name="市场情绪", value=market["mood"], score=market["mood_score"]),
        FactorItem(name="涨停/跌停", value=f"{market['zt_count']}/{market['dt_count']}", score=market["mood_score"]),
        FactorItem(name="候选股数", value=str(len(candidates)), score=min(100, len(candidates) * 8 + 30)),
        FactorItem(name="主线题材", value=", ".join(t for t, _ in main_themes[:3]) or "无", score=70 if main_themes else 40),
        FactorItem(name="深入分析数", value=str(len(analyzed)), score=min(100, len(analyzed) * 12 + 20)),
        FactorItem(name="看多比例", value=f"{bullish}/{len(analyzed) or len(candidates)}", score=int(avg_score)),
    ]

    lines = [
        "## 盘中短线选股结果",
        f"市场情绪: {market['mood']} | 涨停{market['zt_count']} 跌停{market['dt_count']} 炸板率{market['broken_rate']}%",
        f"主线题材: {', '.join(t for t, _ in main_themes[:5]) or '无明确主线'}",
        f"候选: {len(candidates)}只 | 深入分析: {len(analyzed)}只 | 综合评分: {avg_score:.0f}",
        "",
    ]

    cb = prescreen_result.get("continuous_board", [])
    if cb:
        lines.append("### 连板龙头")
        for s in cb[:5]:
            lines.append(f"- **{s['code']}** {s['name']} | {s['continuous_days']}连板 | 涨停时间{s['zt_time']} | {s['reason']}")
        lines.append("")

    dp = prescreen_result.get("dragon_pullback", [])
    if dp:
        lines.append("### 龙回头弱转强")
        for s in dp[:5]:
            sig_str = ", ".join(s["signals"][:4])
            lines.append(
                f"- **{s['code']}** {s['name']} | 前期{s['max_continuous_days']}连板 | "
                f"回调{s['pullback_pct']}% | 弱转强信号: {sig_str}"
            )
        lines.append("")

    lines.append("### 候选标的")
    for a in analyzed:
        src = a.get("source", "")
        lines.append(
            f"- **{a['code']}** {a.get('name', '')} | 评分{a['score']:.0f} | "
            f"{a['direction']} | 来源:{src} | {a['signal']}"
        )

    return SkillReport(
        skill_name="market_screener", score=round(avg_score, 1),
        direction=direction, confidence=confidence,
        signal=f"短线{bullish}只看多候选，主线:{', '.join(t for t, _ in main_themes[:2]) or '无'}",
        factors=factors, analysis="\n".join(lines),
        output_data={
            "market": market, "main_themes": main_themes,
            "dragon_pullback": dp,
            "candidates": [c for c in candidates[:15]],
            "analyzed": analyzed,
        },
        tools_called=_tool_calls or [],
        missing_data=_missing_data or [],
        status="ok",
    )

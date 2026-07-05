# -*- coding: utf-8 -*-
"""
market_screener/_helpers.py

内部辅助函数：策略选择、批量分析、报告构建、名称解析。
"""

from datetime import date, datetime, time
from typing import Any, Dict, List


def select_strategy() -> str:
    """根据当前时间选择交易日策略。"""
    now = datetime.now()
    # 非交易日 → 盘后
    if now.weekday() >= 5:
        return "post_market"
    t = now.time()
    if time(9, 30) <= t < time(14, 30):
        return "intraday"
    if time(14, 30) <= t < time(15, 0):
        return "eod"
    return "post_market"


def resolve_names(code_list: List[str]) -> Dict[str, str]:
    """批量解析股票名称。返回 {code: name}。"""
    if not code_list:
        return {}
    try:
        from app.agent.tools.data_tools import get_realtime_quote
        q = get_realtime_quote(",".join(code_list))
        name_map = {}
        if isinstance(q, dict):
            data = q.get("data", q)
            if isinstance(data, dict):
                for code, info in data.items():
                    if isinstance(info, dict) and info.get("name"):
                        name_map[code] = info["name"]
        return name_map
    except Exception:
        return {}


def analyze_batch(items: list, fn, max_candidates: int = 8) -> list:
    """批量分析，逐项调用分析函数。

    Args:
        items: 待分析项列表，每项会作为 fn 的参数
        fn: 分析函数，接收一项 item，返回分析结果 dict 或 None
        max_candidates: 最多分析数量，默认 8

    Returns:
        非 None 的分析结果列表
    """
    results = []
    for i, item in enumerate(items):
        if i >= max_candidates:
            break
        try:
            r = fn(item)
            if r is not None:
                results.append(r)
        except Exception:
            continue
    return results


def build_report(results: list):
    """从分析结果列表构建 SkillReport。"""
    from .common import SkillReport

    valid = [r for r in results if r is not None and isinstance(r, dict)]
    if not valid:
        return SkillReport(
            skill_name="market_screener",
            score=50.0,
            signal="无有效分析结果",
        )

    scores = [v.get("score", 50) for v in valid]
    directions = [v.get("direction", "neutral") for v in valid]
    avg_score = sum(scores) / len(scores)

    # 综合方向
    bullish = directions.count("bullish")
    bearish = directions.count("bearish")
    if bullish > bearish and bullish > len(valid) * 0.3:
        direction = "bullish"
    elif bearish > bullish and bearish > len(valid) * 0.3:
        direction = "bearish"
    else:
        direction = "neutral"

    # 综合信心
    confs = [v.get("confidence", 0.5) for v in valid]
    avg_conf = sum(confs) / len(confs) if confs else 0.5

    return SkillReport(
        skill_name="market_screener",
        score=round(avg_score, 1),
        direction=direction,
        confidence=round(avg_conf, 2),
        signal=f"分析 {len(valid)} 只",
        output_data={"analyzed": valid},
    )


def filter_candidates(prescreen_result: Dict) -> str:
    """根据 strategy + mood 筛选 candidates，返回逗号分隔的 codes 字符串。

    封装了 SKILL.md 中的筛选逻辑，agent 只需调用此函数，无需写过滤代码。

    Args:
        prescreen_result: pre_screen() 的返回值（SkillResult dict）

    Returns:
        逗号分隔的股票代码，如 "000001,600519,300750"
        如果没有符合条件的股票，返回空字符串 ""
    """
    strategy = prescreen_result.get("strategy", "")
    market = prescreen_result.get("market", {}) or {}
    candidates = prescreen_result.get("candidates", []) or []
    main_themes = prescreen_result.get("main_themes", []) or []
    themes = [t[0] for t in main_themes if isinstance(t, (list, tuple)) and len(t) > 0]

    mood = market.get("mood", "")
    mood_score = market.get("mood_score", 50)

    filtered = []
    for c in candidates:
        # 通用排除
        src = c.get("source", "")
        change = abs(c.get("change_pct", 0) or 0)
        trn = c.get("turnover_pct", 0) or 0
        reason = c.get("reason", "") or ""

        if src in ("ST股",) or trn < 2:
            continue

        if strategy == "post_market":
            # 热点题材且 reason 涉及主线
            if src == "热点题材" and any(t in reason for t in themes):
                filtered.append(c)
                continue
            # 热点题材换手率适中
            if src == "热点题材" and 3 <= trn <= 25:
                filtered.append(c)
                continue
            # 涨停活跃股 / 龙回头
            if src in ("4IN1(近期涨停)", "龙回头") and change >= 5:
                filtered.append(c)
                continue
            # 盘后筛选但换手率高且有涨幅
            if src == "盘后筛选" and trn >= 8 and change >= 5:
                filtered.append(c)
                continue

        elif strategy == "eod":
            if c.get("close_at_high", False) or (change >= 4 and trn > 2.5):
                filtered.append(c)

        else:  # intraday
            if src == "连板":
                filtered.append(c)
            elif src == "龙回头" and reason == "弱转强信号":
                filtered.append(c)
            elif any(t in reason for t in themes):
                if mood in ("偏强",) or mood_score >= 70:
                    filtered.append(c)
                elif mood in ("中性",) or mood_score >= 50:
                    if reason:
                        filtered.append(c)
                # 偏弱: 只保留连板/龙回头(已在上面处理)
                # post_market 的 mood 规则不受此限制

    filtered = filtered[:15]
    codes = ",".join([c["code"] for c in filtered if c.get("code")])
    return codes




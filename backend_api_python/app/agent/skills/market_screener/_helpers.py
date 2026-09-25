# -*- coding: utf-8 -*-
"""
market_screener/_helpers.py

内部辅助函数：策略选择、批量分析、报告构建、名称解析。
"""

from datetime import date, datetime, time

from app.agent.log import logger
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
        from app.agent.tools.finance.data_tools import get_realtime_quote
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


# ── 预测分（P(T+1涨)×100）──────────────────────────────────────
# 2026-09-25：原 analyze_code 是手拍分档（+5/-3 累加），对次日涨跌无标定，
# 选股排序质量低。复用 app/watchlist.predict 的已标定 T+1 预测（AUC≈0.61），
# score 语义统一为「次日上涨概率×100」，技术形态只作解释与轻量门闩。

_MKT_KLINES_CACHE = {"data": None}


def _market_klines():
    if _MKT_KLINES_CACHE["data"] is None:
        try:
            from app.watchlist.predict import load_market_klines
            _MKT_KLINES_CACHE["data"] = load_market_klines(120) or []
        except Exception:
            _MKT_KLINES_CACHE["data"] = []
    return _MKT_KLINES_CACHE["data"]


def _norm_bars(bars: list) -> list:
    """fetch_kline 的 time 是日期串；predict._by_day 要 int 时间戳。"""
    from datetime import datetime as _dt
    out = []
    for b in bars or []:
        if not isinstance(b, dict):
            continue
        x = dict(b)
        tm = x.get("time")
        if isinstance(tm, str):
            try:
                x["time"] = int(_dt.strptime(tm[:10], "%Y-%m-%d").timestamp())
            except Exception:
                continue
        out.append(x)
    return out


def _mood_regime(market: dict) -> str:
    """情绪分桶：strong / neutral / weak。"""
    market = market or {}
    mood = str(market.get("mood") or "")
    try:
        ms = float(market.get("mood_score", 50) or 50)
    except Exception:
        ms = 50.0
    if mood in ("弱势", "偏弱") or ms < 40:
        return "weak"
    if mood in ("偏强",) or ms >= 70:
        return "strong"
    return "neutral"


def enrich_predictive(results: list, market: dict = None) -> list:
    """给每只候选打 P(T+1涨) 分，按情绪分桶裁剪，再按预测分降序重排。

    - 有预测：score = p_up*100（情绪弱时对涨停活跃源做小幅折价）
    - 门槛：weak ≥0.55 / neutral ≥0.50 / strong ≥0.48；无预测的垫底且不进最终推荐
    - 条数：weak 5 / neutral 8 / strong 10
    """
    if not results:
        return []
    regime = _mood_regime(market)
    from .common import fetch_kline
    try:
        from app.watchlist.predict import predict_next_day
    except Exception as e:
        logger.warning("[MktScreen] 预测模块不可用，退化为技术分: %s", e)
        return list(results)

    mk = _market_klines()
    out = []
    for r in results:
        if not isinstance(r, dict) or not r.get("code"):
            continue
        item = dict(r)
        item.setdefault("tech_score", item.get("score", 50))
        try:
            bars = _norm_bars(fetch_kline(item["code"], days=120))
            pred = predict_next_day(bars, mk) if bars else None
        except Exception as e:
            logger.debug("[MktScreen] predict %s 失败: %s", item.get("code"), e)
            pred = None
        if pred and pred.get("score") is not None:
            p_up = float(pred.get("p_up") or 0.5)
            # 情绪弱时对涨停活跃源折价：连板/炸板题材在弱势市次日溢价差
            src = str(item.get("source") or "")
            if regime == "weak" and any(k in src for k in ("连板", "4IN1", "龙回头", "涨停")):
                p_up = max(0.05, p_up * 0.92)
                item["mood_haircut"] = 0.92
            item["score"] = round(p_up * 100, 1)
            item["p_up"] = round(p_up, 4)
            item["exp_ret_bp"] = pred.get("exp_ret_bp")
            item["direction"] = (
                "bullish" if p_up >= 0.55 else ("bearish" if p_up <= 0.45 else "neutral")
            )
            item["pred"] = {
                "beta": (pred.get("beta") or {}).get("beta"),
                "regime": (pred.get("beta") or {}).get("regime"),
                "zhuang": (pred.get("zhuang") or {}).get("score"),
                "market_p_up": (pred.get("market") or {}).get("p_up"),
                "mood_regime": regime,
            }
            item["has_pred"] = True
        else:
            item["has_pred"] = False
        out.append(item)

    thr = {"weak": 0.55, "neutral": 0.50, "strong": 0.48}[regime]
    cap = {"weak": 5, "neutral": 8, "strong": 10}[regime]
    kept = [x for x in out if x.get("has_pred") and (x.get("p_up") or 0) >= thr]
    kept.sort(key=lambda x: x.get("score") or 0, reverse=True)
    kept = kept[:cap]
    kept_ids = {id(x) for x in kept}
    for x in out:
        x["selected"] = id(x) in kept_ids
    dropped = [x for x in out if id(x) not in kept_ids]
    dropped.sort(key=lambda x: x.get("score") or x.get("tech_score") or 0, reverse=True)
    return kept + dropped


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

    # 组合评分取 Top5 均值：反映「最终推荐质量」，避免被垫底候选拉平
    _ranked = sorted(valid, key=lambda v: v.get("score") or 0, reverse=True)
    scores = [v.get("score", 50) for v in _ranked[:5]] or [50]
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
    regime = _mood_regime(market)

    filtered = []
    for c in candidates:
        # 通用排除
        src = c.get("source", "")
        change = abs(c.get("change_pct", 0) or 0)
        trn = c.get("turnover_pct", 0) or 0
        reason = c.get("reason", "") or ""
        name = str(c.get("name") or "")
        price = c.get("price") or 0

        if src in ("ST股",) or "ST" in name.upper() or trn < 2:
            continue
        # 价格带：仙股/过高价流动性与可交易性差
        try:
            if price and (float(price) < 2 or float(price) > 300):
                continue
        except Exception:
            pass

        # 情绪弱势：涨停活跃源需题材/理由支撑，否则丢弃（打板退潮）
        if regime == "weak" and any(k in src for k in ("连板", "4IN1", "龙回头")):
            if not reason:
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




# -*- coding: utf-8 -*-
"""个股情报+政策面分析 — 新闻/事件/舆情/解禁/减持/质押，RMS评分+一票否决。"""

import logging
from datetime import datetime
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def intelligence_analysis(stock_code: str, stock_name: str = "") -> Dict[str, Any]:
    """个股情报+政策面综合分析：搜索新闻公告研报 + 政策动态，返回情报评分和利空/利多信号。

    Args:
        stock_code: 股票代码，如 "600066"
        stock_name: 股票名称，可选

    Returns:
        {
            
            "score": float,          # 综合评分 (0-100)
            "direction": str,        # bullish / bearish / neutral
            "confidence": float,     # 0.0-1.0
            "signal": str,           # 信号摘要
            "factors": list,         # 因子明细
            "analysis": str,
            "veto": bool,            # 是否一票否决
            "stock_score": float,    # 个股情报分 (5分制)
            "policy_score": float,   # 政策面分 (5分制)
            "stock_signals": list,   # 个股信号列表
            "policy_signals": list,  # 政策信号列表
            "status": "ok",
        }
    """
    
    # ── 个股情报 ──
    stock_result, stock_score, stock_veto, stock_signals = _analyze_stock(stock_code, stock_name)

    # ── 政策面 ──
    policy_result, policy_score, policy_veto, policy_signals = _analyze_policy()

    # ── 综合判断 ──
    veto = stock_veto or policy_veto

    # 5分制 → 0-100 分制
    # stock_score 和 policy_score 都是 -5 ~ +5
    # 综合 = 个股权重 0.7 + 政策权重 0.3
    combined_5 = stock_score * 0.7 + policy_score * 0.3
    final_score = max(0, min(100, int(50 + combined_5 * 10)))

    if veto:
        final_score = max(0, min(100, int(50 + min(stock_score, policy_score) * 10)))
        direction = "bearish"
    elif combined_5 >= 2:
        direction = "bullish"
    elif combined_5 <= -2:
        direction = "bearish"
    else:
        direction = "neutral"

    # 信号: 一票否决置顶，其余按重要性
    signal_parts = []
    if stock_veto:
        # 找否决源头
        veto_src = _find_veto_source(stock_result)
        signal_parts.append(f"⚠否决:{veto_src}" if veto_src else "⚠个股一票否决")
    if policy_veto:
        veto_src = _find_veto_source(policy_result)
        signal_parts.append(f"⚠政策否决:{veto_src}" if veto_src else "⚠政策一票否决")

    # 只显示有实质影响的信号（已过滤中性）
    for s in stock_signals[:3]:
        if s not in signal_parts:
            signal_parts.append(s)
    for s in policy_signals[:2]:
        if s not in signal_parts:
            signal_parts.append(s)

    signal = " | ".join(signal_parts) if signal_parts else "无显著信号"

    # 因子
    factors = []
    if stock_signals:
        factors.append({"name": "个股情报", "value": f"{len(stock_signals)}条", "score": _5_to_100(stock_score)})
    if policy_signals:
        factors.append({"name": "政策面", "value": f"{len(policy_signals)}条", "score": _5_to_100(policy_score)})

    analysis = (
        f"个股情报:{stock_score}/5({len(stock_signals)}条信号) "
        f"政策面:{policy_score}/5({len(policy_signals)}条信号) "
        f"{'一票否决' if veto else ''}"
    )

    return {
        
        "score": final_score,
        "direction": direction,
        "confidence": 0.5,
        "signal": signal,
        "factors": factors,
        "analysis": analysis,
        "status": "ok",
        "output_data": {
            "stock": stock_result,
            "policy": policy_result,
            "stock_signals": stock_signals,
            "policy_signals": policy_signals,
        },
        "veto": veto,
        "stock_veto": stock_veto,
        "policy_veto": policy_veto,
        "stock_score": stock_score,
        "policy_score": policy_score,
        "stock_signals": stock_signals,
        "policy_signals": policy_signals,
    }


# ═══════════════════════════════════════════════════════════════
# 个股情报分析
# ═══════════════════════════════════════════════════════════════

def _analyze_stock(stock_code: str, stock_name: str):
    """个股情报分析。返回 (result, score, veto, signals)。

    使用 search_stock_intel() → composite_score() (RMS + 时间衰减 + 一票否决)
    只输出有实质影响的内容（|score| > 3），中性不显示。
    """
    
    result = {}
    score = 0.0
    veto = False
    signals = []

    try:
        result = search_stock_intel(stock_code, stock_name or "")
        score = _composite_to_5(result.get("composite_score", 0))
        veto = result.get("veto", False)

        if veto:
            score = -5.0
            # 一票否决源头
            veto_src = _find_veto_source(result)
            if veto_src:
                signals.append(f"⚠否决:{veto_src}")

        # 只保留有实质影响的（|score| > 3 或一票否决）
        for it in result.get("news", []):
            sc = it.get("sentiment_score", 0) or 0
            if sc == -999:
                continue  # 已在否决项处理
            if abs(sc) > 3:
                title = it.get("title", "")[:20]
                date = _extract_date(it.get("published", ""))
                signals.append(f"{title}({date})" if date else title)

    except Exception as e:
        logger.warning("[Intelligence] 个股情报失败: %s", e)

    return result, score, veto, signals


# ═══════════════════════════════════════════════════════════════
# 政策面分析
# ═══════════════════════════════════════════════════════════════

def _analyze_policy():
    """政策面分析。返回 (result, score, veto, signals)。

    输出格式和个股情报统一：总分 + 一票否决 + 1-20字说明(带日期)
    政策利好/利空哪个行业，1-20字说明。
    """
    
    result = {}
    score = 0.0
    veto = False
    signals = []

    try:
        result = search_policy_intel("CNStock")
        score = _composite_to_5(result.get("composite_score", 0))
        veto = result.get("veto", False)

        if veto:
            score = -5.0
            veto_src = _find_veto_source(result)
            if veto_src:
                signals.append(f"⚠否决:{veto_src}")

        # 只保留有实质影响的（|score| > 3 或一票否决）
        for it in result.get("news", []):
            sc = it.get("sentiment_score", 0) or 0
            if sc == -999:
                continue  # 已在否决项处理
            if abs(sc) > 3:
                title = it.get("title", "")[:20]
                date = _extract_date(it.get("published", ""))
                signals.append(f"{title}({date})" if date else title)

    except Exception as e:
        logger.warning("[Intelligence] 政策面失败: %s", e)

    return result, score, veto, signals


# ═══════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════

def _find_veto_source(result: dict) -> str:
    """从结果中找到一票否决源头，返回 1-20 字说明(带日期)。"""
    veto_article = result.get("veto_article")
    if veto_article:
        title = str(veto_article.get("title", ""))[:20]
        date = _extract_date(veto_article.get("published_date", "") or veto_article.get("published", ""))
        return f"{title}({date})" if date else title

    # fallback: 从 news 列表找 score=-999
    for it in result.get("news", []):
        if it.get("sentiment_score") == -999:
            title = str(it.get("title", ""))[:20]
            date = _extract_date(it.get("published", ""))
            return f"{title}({date})" if date else title

    return ""


def _composite_to_5(composite: float) -> float:
    """composite_score (-5~+5) → 5 分制。"""
    return round(max(-5.0, min(5.0, composite)), 1)


def _5_to_100(score_5: float) -> int:
    """5分制 (-5~+5) → 0-100 分制。"""
    return max(0, min(100, int(50 + score_5 * 10)))


def _extract_date(pub: str) -> str:
    """从发布时间提取 \"M月D日\" 格式。"""
    if not pub:
        return ""
    try:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(pub[:19], fmt)
                return f"{dt.month}月{dt.day}日"
            except ValueError:
                continue
        return pub[:10]
    except Exception:
        return ""


# ── 内联自 news_search_tools.py ──

def _get_policy_from_cache() -> List[Dict[str, Any]]:
    """政策新闻: 只读 DB 缓存 (scheduler 每日写入)"""
    try:
        from app.services.news_search import get_news_cache_manager
        cached = get_news_cache_manager().get_items("POLICY", "CNStock")
        if not cached:
            return []
        return [
            {"title": r["title"], "link": r.get("url", ""),
             "snippet": r.get("snippet", ""), "source": r.get("source", ""),
             "published": r.get("published_date", ""),
             "sentiment": r.get("sentiment", "neutral"),
             "sentiment_score": r.get("sentiment_score")}
            for r in cached
        ]
    except Exception as e:
        logger.warning("读取 POLICY 缓存失败: %s", e)
        return []

def _get_news(symbol: str, market: str = "CNStock", name: str = "") -> List[Dict[str, Any]]:
    """个股/板块新闻: 走 fetch_financial_news (缓存→搜索→写入)"""
    try:
        from app.services.news_search import fetch_financial_news
        resp = fetch_financial_news(lang="all", market=market, symbol=symbol, name=name)
        items = []
        for lang_key in ("cn", "en"):
            for it in resp.get(lang_key) or []:
                title = it.get("title", "")
                snippet = it.get("snippet", "")
                # 去重: snippet 和 title 一样或 snippet 是 title 的子串 → 只留 title
                if snippet and snippet != title and snippet not in title:
                    # snippet 有额外信息 → 拼到 title 末尾（截断）
                    keep = snippet[:80] if len(snippet) > 80 else snippet
                    title = f"{title} | {keep}" if title else keep
                items.append({
                    "title": title[:120] if title else "",
                    "source": it.get("source", ""),
                    "published": it.get("published", ""),
                    "sentiment": it.get("sentiment", "neutral"),
                    "sentiment_score": it.get("sentiment_score"),
                })
        return items
    except Exception as e:
        logger.warning("获取新闻失败 %s(%s): %s", symbol, market, e)
        return []

def _build_result(items: List[Dict[str, Any]], label: str) -> Dict[str, Any]:
    """评分 + 排序: 一票否决置顶, 合计≤20条"""
    from app.services.news_analysis import composite_score

    articles = [
        {"score": it.get("sentiment_score") or 0.0,
         "published_date": it.get("published", "")}
        for it in items
    ]
    score_info = composite_score(articles) if articles else {}

    veto = score_info.get("veto", False)
    veto_article = score_info.get("veto_article")

    # 分离一票否决 vs 正常
    veto_items, normal_items = [], []
    for it in items:
        sc = it.get("sentiment_score")
        if sc == -999:
            veto_items.append({**it, "_veto": True})
        else:
            normal_items.append(it)

    # 正常按时间倒序
    normal_items.sort(key=lambda x: x.get("published", ""), reverse=True)

    # 过滤: 只保留有明确情感倾向的新闻，去掉中性
    filtered = []
    for it in veto_items + normal_items:
        sc = it.get("sentiment_score")
        sentiment = it.get("sentiment", "neutral")
        # 一票否决始终保留
        if it.get("_veto"):
            filtered.append(it)
            continue
        # 有明确分数且非中性 → 保留
        if sc is not None and sc != 0 and sentiment != "neutral":
            filtered.append(it)
            continue
        # 有分数但中性 → 跳过
    merged = filtered[:20]

    # 精简: 只保留 title + sentiment + score + source
    slim = []
    for it in merged:
        slim.append({
            "title": it.get("title", ""),
            "sentiment": it.get("sentiment", ""),
            "sentiment_score": it.get("sentiment_score"),
            "source": it.get("source", ""),
            "published": it.get("published", ""),
            **({"_veto": True} if it.get("_veto") else {}),
        })

    return {
        "label": label,
        "composite_score": score_info.get("composite_score", 0),
        "direction": score_info.get("direction", "中性"),
        "veto": veto,
        "veto_article": veto_article,
        "count": len(slim),
        "news": slim,
    }

def search_stock_intel(codes: str, name: str = "") -> Dict[str, Any]:
    """个股情报搜索：返回指定股票的新闻、公告、研报列表及摘要。

    Args:
        codes: 多股用逗号分隔"
        name: 股票名称，如 "贵州茅台"
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()][:20]
    if not code_list:
        return {"error": "codes 不能为空", "retriable": False}

    def _one(stock_code: str) -> Dict[str, Any]:
        try:
            items = _get_news(stock_code, "CNStock", name)
            return _build_result(items, f"个股:{stock_code}")
        except Exception as e:
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

def search_policy_intel(market: str = "CNStock") -> Dict[str, Any]:
    """政策情报搜索：返回最新财经政策、监管动态。

    Args:
        market: 市场或政策关键词
    """
    items = _get_policy_from_cache()
    return _build_result(items, f"政策:{market}")

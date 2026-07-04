# -*- coding: utf-8 -*-
"""个股情报+政策面分析 — 新闻/事件/舆情/解禁/减持/质押，RMS评分+一票否决。"""

from app.agent.log import logger
import json
from datetime import datetime
from typing import Any, Dict, List
from app.agent.utils.md_format import _format_final_md, _lookup_stock_name

from app.agent.tools.news_search_tools import (
    search_stock_intel,
    search_policy_intel,
)
def intelligence_analysis(stock_code: str, _output: str = "markdown") -> str:
    """个股情报+政策面综合分析：搜索新闻公告研报 + 政策动态，返回情报评分和利空/利多信号。

    Args:
        stock_code: 股票代码，如 "600066"

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
        _output: "markdown"(默认) | "json"
    """
    
    stock_name = _lookup_stock_name(stock_code)

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

    all_signals = (stock_signals or []) + (policy_signals or [])
    extra = ["一票否决"] if veto else []
    analysis = _format_final_md(
        title=f"{stock_code or '综合'}情报", score=final_score, direction=direction,
        factors=factors, signals=all_signals, extra=extra,
    )

    # ── highlights / warnings ──
    highlights = []
    warnings = []

    # 一票否决是最高级别警告
    if stock_veto:
        veto_src = _find_veto_source(stock_result)
        warnings.append(f"个股否决: {veto_src}" if veto_src else "个股一票否决")
    if policy_veto:
        veto_src = _find_veto_source(policy_result)
        warnings.append(f"政策否决: {veto_src}" if veto_src else "政策一票否决")

    # 有实质影响的信号 → highlights
    for s in stock_signals[:3]:
        if not s.startswith("⚠"):
            highlights.append(s)
    for s in policy_signals[:2]:
        if not s.startswith("⚠"):
            highlights.append(s)

    if stock_score >= 3:
        highlights.append(f"个股情报正面({stock_score}/5)")
    elif stock_score <= -3:
        warnings.append(f"个股情报负面({stock_score}/5)")
    if policy_score >= 3:
        highlights.append(f"政策面利好({policy_score}/5)")
    elif policy_score <= -3:
        warnings.append(f"政策面利空({policy_score}/5)")

    evaluation = {
        "score": final_score,
        "scores": {"stock": _5_to_100(stock_score), "policy": _5_to_100(policy_score)},
        "highlights": highlights,
        "warnings": warnings,
    }

    _r = {
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
        "evaluation": evaluation,
    }
    return analysis if _output == "markdown" else json.dumps(_r, ensure_ascii=False)
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


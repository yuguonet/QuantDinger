# -*- coding: utf-8 -*-
"""
intelligence-agent — 个股情报 + 政策面分析。

评分规则定义在 SKILL.md，本文件是实现。
输出: dict（skill_runner 直接转发）
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def run(stock_code: str, stock_name: str = "", context: dict = None) -> Dict[str, Any]:
    """执行个股情报+政策面分析。

    Returns:
        {
            "skill": "intelligence_agent",
            "score": float,          # 5 分制，-5 ~ +5
            "direction": str,        # bullish / bearish / neutral
            "veto": bool,            # 是否一票否决
            "stock_veto": bool,      # 个股一票否决
            "policy_veto": bool,     # 政策一票否决
            "stock_score": float,    # 个股情报分
            "policy_score": float,   # 政策面分
            "signal": str,           # 信号摘要
            "stock_signals": list,   # 个股信号列表
            "policy_signals": list,  # 政策信号列表
            "analysis": str,
            "status": "ok",
        }
    """
    from app.agent.tools.news_search_tools import search_stock_intel, search_policy_intel

    # ── 个股情报 ──
    stock_result, stock_score, stock_veto, stock_items = _analyze_stock(stock_code, stock_name)

    # ── 政策面 ──
    policy_result, policy_score, policy_veto, policy_items = _analyze_policy()

    # ── 构建信号 ──
    stock_signals = _build_signals(stock_items, stock_result, stock_veto)
    policy_signals = _build_signals(policy_items, policy_result, policy_veto)

    veto = stock_veto or policy_veto
    signal = "一票否决" if veto else " ".join(stock_signals[:3]) or "无显著信号"

    stock_direction = _score_to_direction(stock_score)
    policy_direction = _score_to_direction(policy_score)

    return {
        "skill": "intelligence_agent",
        "score": stock_score,
        "direction": "bearish" if veto else stock_direction,
        "confidence": 0.5,
        "signal": signal,
        "factors": [],
        "analysis": f"个股情报:{stock_score}/5({stock_direction}) 政策面:{policy_score}/5({policy_direction})",
        "status": "ok",
        "output_data": {
            "stock": stock_result, "policy": policy_result,
            "stock_signals": stock_signals, "policy_signals": policy_signals,
        },
        "veto": veto,
        "stock_veto": stock_veto,
        "policy_veto": policy_veto,
        "stock_score": stock_score,
        "policy_score": policy_score,
        "policy_direction": policy_direction,
        "policy_signals": policy_signals,
    }


# ═══════════════════════════════════════════════════════════════
# 个股情报
# ═══════════════════════════════════════════════════════════════

def _analyze_stock(stock_code: str, stock_name: str):
    """个股情报分析。返回 (result, score, veto, items)。"""
    from app.agent.tools.news_search_tools import search_stock_intel

    result = {}
    score = 0.0
    veto = False
    items = []

    try:
        result = search_stock_intel(stock_code, stock_name or "")
        score = _composite_to_5(result.get("composite_score", 0))
        veto = result.get("veto", False)
        if veto:
            score = -5.0
        # 只保留有实质影响的（|score| > 3 或一票否决）
        for it in result.get("news", []):
            sc = it.get("sentiment_score", 0) or 0
            if sc == -999 or abs(sc) > 3:
                items.append(it)
    except Exception as e:
        logger.warning("[Intelligence] 个股情报失败: %s", e)

    return result, score, veto, items


# ═══════════════════════════════════════════════════════════════
# 政策面
# ═══════════════════════════════════════════════════════════════

def _analyze_policy():
    """政策面分析。返回 (result, score, veto, items)。"""
    from app.agent.tools.news_search_tools import search_policy_intel

    result = {}
    score = 0.0
    veto = False
    items = []

    try:
        result = search_policy_intel("CNStock")
        score = _composite_to_5(result.get("composite_score", 0))
        veto = result.get("veto", False)
        if veto:
            score = -5.0
        # 只保留有实质影响的（|score| > 3 或一票否决）
        for it in result.get("news", []):
            sc = it.get("sentiment_score", 0) or 0
            if sc == -999 or abs(sc) > 3:
                items.append(it)
    except Exception as e:
        logger.warning("[Intelligence] 政策面失败: %s", e)

    return result, score, veto, items


# ═══════════════════════════════════════════════════════════════
# 信号构建
# ═══════════════════════════════════════════════════════════════

def _build_signals(items: list, result: dict, veto: bool) -> List[str]:
    """构建信号列表。一票否决项置顶，每条 1-20 字 + 日期。"""
    signals = []

    # 一票否决源头
    if veto:
        for it in result.get("news", []):
            if it.get("sentiment_score") == -999:
                title = it.get("title", "")[:20]
                date = _extract_date(it.get("published", ""))
                signals.append(f"⚠否决:{title}({date})" if date else f"⚠否决:{title}")
                break

    # 有实质影响的信号
    for it in items:
        if it.get("sentiment_score") == -999:
            continue  # 已在否决项处理
        title = it.get("title", "")[:20]
        date = _extract_date(it.get("published", ""))
        if title:
            signals.append(f"{title}({date})" if date else title)

    return signals


# ═══════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════

def _composite_to_5(composite: float) -> float:
    """composite_score (-5~+5) → 5 分制。"""
    return round(max(-5.0, min(5.0, composite)), 1)


def _score_to_direction(score: float) -> str:
    """5 分制 → 方向。"""
    if score >= 2:
        return "bullish"
    if score <= -2:
        return "bearish"
    return "neutral"


def _extract_date(pub: str) -> str:
    """从发布时间提取 \"M月D日\" 格式。"""
    if not pub:
        return ""
    try:
        from datetime import datetime
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(pub[:19], fmt)
                return f"{dt.month}月{dt.day}日"
            except ValueError:
                continue
        return pub[:10]
    except Exception:
        return ""

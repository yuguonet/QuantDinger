# -*- coding: utf-8 -*-
"""
stock_report — 从工具结果生成个股分析报告（纯规则，0 幻觉）

完全兼容 OpenAI Function Calling 标准。
MCP 自动发现：函数名 = MCP 工具名 = stock_report。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def stock_report(
    info: Optional[Dict[str, Any]] = None,
    technical: Optional[Dict[str, Any]] = None,
    capital: Optional[Dict[str, Any]] = None,
    quote: Optional[Dict[str, Any]] = None,
    fund_flow: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """从工具返回的结构化数据直接生成个股分析报告。

    必须传入以下 5 个工具的返回结果（按参数名传入）：
    - info: 调用 get_stock_info 获取
    - technical: 调用 technical_analysis 获取（必须，评分/方向/信号来源）
    - capital: 调用 get_capital_summary 获取
    - quote: 调用 get_realtime_quote 获取
    - fund_flow: 调用 get_fund_flow 获取

    Args:
        info: get_stock_info 的返回结果
        technical: technical_analysis 的返回结果（必须）
        capital: get_capital_summary 的返回结果
        quote: get_realtime_quote 的返回结果
        fund_flow: get_fund_flow 的返回结果

    Returns:
        {"report": "markdown 报告", "summary": {结构化摘要}} 或 {"error": "..."}
    """
    if not any([info, technical, capital, quote, fund_flow]):
        return {"error": "未传入任何工具数据"}

    try:
        report, summary = _generate_report(info or {}, technical or {}, capital or {}, quote or {}, fund_flow or {})
        return {
            "report": report,
            "summary": summary,  # 结构化摘要，供 LLM 做定性分析
        }
    except Exception as e:
        logger.error("[stock_report] 生成失败: %s", e, exc_info=True)
        return {"error": f"报告生成失败: {e}"}


# ═══════════════════════════════════════════════════════════════
#  核心逻辑：从工具结果提取数据 → 评分 → 拼 markdown
# ═══════════════════════════════════════════════════════════════

def _generate_report(
    info: dict, technical: dict, capital: dict, quote: dict, fund_flow: dict
) -> tuple[str, dict]:
    """纯规则生成报告，不经过 LLM。

    Returns:
        (report_markdown, summary_dict)
    """

    # ── 基本信息 ──
    stock_code = info.get("stock_code", "") or quote.get("stock_code", "")
    stock_name = info.get("name", "") or quote.get("name", "")

    # ── 技术面 ──
    score = technical.get("score", 0)
    direction = technical.get("direction", "neutral")
    signal = technical.get("signal", "")

    # ── 估值 ──
    pe_ttm = info.get("pe_ttm") or info.get("pe_static") or info.get("pe_ratio")
    pe_str = f"{pe_ttm:.2f}" if pe_ttm and pe_ttm > 0 else ("亏损" if pe_ttm and pe_ttm < 0 else "未知")

    # ── 资金面 ──
    fund_signal = ""
    if fund_flow and "error" not in fund_flow:
        flow_data = fund_flow.get("data", {}).get(stock_code, {})
        if not flow_data:
            flow_data = fund_flow
        fund_signal = flow_data.get("signal", "")

    # ── 资本结构 ──
    capital_signal = ""
    capital_data = capital.get("summary", capital)
    if isinstance(capital_data, dict):
        margin = capital_data.get("margin", {})
        fund_signal = fund_signal or margin.get("signal", "")
        capital_signal = capital_data.get("overall_signal", "")

    # ── 综合判断 ──
    action = _determine_action(score, direction, pe_ttm, fund_signal)
    confidence = _determine_confidence(technical, fund_signal, capital_signal)

    # ── 信号描述 ──
    signal_parts = []
    if signal:
        short_signal = signal.split("|")[0].strip() if "|" in signal else signal
        signal_parts.append(short_signal)
    if pe_ttm and pe_ttm < 0:
        signal_parts.append("基本面亏损")
    full_signal = "+".join(signal_parts) if signal_parts else "数据不足"

    # ── 结构化摘要（供 LLM 做定性分析）──
    # 提取技术面因子明细
    factors = technical.get("factors", [])
    factors_str = ", ".join([f"{f['name']}:{f['score']}" for f in factors if f.get('score')]) if factors else ""

    summary = {
        "stock": f"{stock_name}({stock_code})",
        "score": score,
        "direction": _direction_cn(direction),
        "action": action,
        "pe": pe_str,
        "signal_short": signal.split("|")[0].strip() if "|" in signal else signal,
        "signal_full": signal,
        "fund": fund_signal or "无数据",
        "capital": capital_signal or "无数据",
        "factors": factors_str,
        "confidence": confidence,
    }

    # ── 输出（结构化数据，不含综合分析）──
    report = (
        f"**股票名称**: {stock_name} ({stock_code})\n"
        f"**综合评分**: {score}\n"
        f"**操作建议**: {action}\n"
        f"**置 信 度**: {confidence}\n"
        f"**时间窗口**: T+3\n"
        f"**技术面**: {score}分 ({factors_str})\n"
        f"**资金面**: {fund_signal or '无数据'}\n"
        f"**基本面**: PE {pe_str}\n"
        f"**信号**: {full_signal}"
    )
    return report, summary


def _determine_action(score: int, direction: str, pe: float | None, fund_signal: str) -> str:
    """根据多维度数据决定操作建议。"""
    # 评分→操作
    if score >= 70:
        base_action = "买入"
    elif score >= 55:
        base_action = "持有"
    else:
        base_action = "跳过"

    # 方向修正
    if direction == "bearish" and base_action == "买入":
        base_action = "持有"
    if direction == "bearish" and score < 55:
        base_action = "跳过"

    # 基本面修正
    if pe and pe < 0 and base_action == "买入":
        base_action = "持有"

    # 资金面修正
    if "流出" in (fund_signal or "") and base_action == "买入":
        base_action = "持有"

    return base_action


def _determine_confidence(technical: dict, fund_signal: str, capital_signal: str) -> str:
    """判断置信度。"""
    confidence_val = technical.get("confidence", "")
    if confidence_val in ("high", "高"):
        return "高"
    if confidence_val in ("low", "低"):
        return "低"

    # 根据数据完整性判断
    has_technical = bool(technical.get("score"))
    has_fund = bool(fund_signal)
    has_capital = bool(capital_signal)

    if has_technical and has_fund and has_capital:
        return "高"
    if has_technical and (has_fund or has_capital):
        return "中"
    return "低"


def _direction_cn(direction: str) -> str:
    """英文方向 → 中文。"""
    return {"bullish": "看涨", "bearish": "看跌", "neutral": "中性"}.get(direction, direction)

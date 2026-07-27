# -*- coding: utf-8 -*-
"""
stock_report — 从工具结果生成个股分析报告（纯规则，0 幻觉）

完全兼容 OpenAI Function Calling 标准。
函数名 = 工具名 = stock_report，由 list_tools 自动发现。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def _safe_round(val: Any, ndigits: int = 2) -> Any:
    """安全四舍五入，非数值原样返回。"""
    try:
        return round(float(val), ndigits)
    except (TypeError, ValueError):
        return val


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

def _calc_support_resistance(
    technical: dict, quote: dict, capital: dict, fund_flow: dict
) -> tuple[Optional[float], Optional[float], str]:
    """计算支撑位和压力位，返回 (support, resistance, signal_desc)。

    优先级：Bollinger 带 > MA 均线 > 近期高低点。
    """
    import math

    latest = technical.get("latest_close", 0) or quote.get("price", 0) or quote.get("latest_price", 0)
    support = None
    resistance = None

    # ── 方法1: Bollinger 带 ──
    boll = technical.get("boll", {})
    if isinstance(boll, dict) and "error" not in boll:
        upper = boll.get("upper")
        lower = boll.get("lower")
        mid = boll.get("mid")
        if upper and lower and latest:
            # 压力位: 上轨（如果当前价在中轨以下）或中轨（如果在上轨附近）
            if latest < mid:
                resistance = mid
            else:
                resistance = upper
            # 支撑位: 下轨（如果当前价在中轨以上）或中轨（如果在下轨附近）
            if latest > mid:
                support = mid
            else:
                support = lower

    # ── 方法2: MA 均线作为支撑/压力 ──
    ma20 = technical.get("ma20")
    ma60 = technical.get("ma60")
    if latest and ma20:
        if support is None:
            if latest > ma20:
                support = ma20
            elif ma60 and latest > ma60:
                support = ma60
        if resistance is None:
            if latest < ma20:
                resistance = ma20
            elif ma60 and latest < ma60:
                resistance = ma60

    # ── 信号描述 ──
    signal = technical.get("signal", "")
    signal_parts = []
    if signal:
        short_signal = signal.split("|")[0].strip() if "|" in signal else signal
        signal_parts.append(short_signal)

    # 布林带位置信号
    if isinstance(boll, dict) and "error" not in boll:
        pos = boll.get("position_pct", 50)
        if pos and pos <= 20:
            signal_parts.append(f"接近布林下轨，关注支撑")
        elif pos and pos >= 80:
            signal_parts.append(f"接近布林上轨，注意压力")

    # MA 偏离信号（避免与主信号重复）
    bias20 = technical.get("bias_ma20")
    if bias20 is not None and abs(bias20) > 10:
        bias_desc = f"偏离MA20达{bias20:.1f}%，{'超跌反弹' if bias20 < 0 else '超涨回调'}"
        if bias_desc not in signal_parts:
            signal_parts.append(bias_desc)

    # 去重
    seen = set()
    unique_parts = []
    for p in signal_parts:
        if p not in seen:
            seen.add(p)
            unique_parts.append(p)
    full_signal = "+".join(unique_parts) if unique_parts else "数据不足"

    return _safe_round(support), _safe_round(resistance), full_signal


def _build_comprehensive_analysis(
    technical: dict, fund_signal: str, capital_signal: str,
    action: str, direction: str,
) -> str:
    """生成综合分析摘要（150字以内）。"""
    parts = []

    # 形态信号
    factors = technical.get("factors", [])
    good_factors = [f for f in factors if f.get("score", 0) >= 70]
    if good_factors:
        # 用 factor value（具体描述）而非 name（可能和"形态"重复）
        descs = [f.get("value", f["name"]) for f in good_factors[:2]]
        parts.append(f"形态:{','.join(descs)}")

    # 资金面
    if fund_signal and "无数据" not in fund_signal:
        parts.append(f"资金{fund_signal}")

    # 指标状态
    rsi = technical.get("rsi", {})
    if isinstance(rsi, dict):
        rsi_val = rsi.get("value", 50)
        if rsi_val < 30:
            parts.append("RSI超卖")
        elif rsi_val > 70:
            parts.append("RSI超买")
        else:
            parts.append("指标健康")

    # 趋势
    trend = technical.get("trend", "") or technical.get("direction", "")
    if "bullish" in str(trend) or "上升" in str(trend):
        parts.append("趋势向上")
    elif "bearish" in str(trend) or "下降" in str(trend):
        parts.append("趋势向下")

    if not parts:
        return "数据不足，建议观望。"

    analysis = "，".join(parts) + f"。建议{action}。"
    return analysis[:150]


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

    # ── 当前价 ──
    latest_price = technical.get("latest_close", 0) or quote.get("price", 0) or quote.get("latest_price", 0)
    price_str = f"{latest_price:.2f}" if latest_price else "--"

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
    action = _determine_action(score, direction, None, fund_signal)
    confidence = _determine_confidence(technical, fund_signal, capital_signal)
    direction_cn = _direction_cn(direction)

    # ── 支撑位 / 压力位 ──
    support, resistance, signal_desc = _calc_support_resistance(
        technical, quote, capital, fund_flow
    )
    support_str = f"{support:.2f}" if support else "--"
    resistance_str = f"{resistance:.2f}" if resistance else "--"

    # ── 综合分析 ──
    analysis = _build_comprehensive_analysis(
        technical, fund_signal, capital_signal, action, direction
    )

    # ── 结构化摘要（供 LLM 做定性分析）──
    factors = technical.get("factors", [])
    factors_str = ", ".join([f"{f['name']}:{f['score']}" for f in factors if f.get('score')]) if factors else ""

    summary = {
        "stock": f"{stock_name}({stock_code})",
        "score": score,
        "direction": direction_cn,
        "action": action,
        "signal_short": signal_desc.split("+")[0] if signal_desc else "",
        "signal_full": signal_desc,
        "fund": fund_signal or "无数据",
        "capital": capital_signal or "无数据",
        "factors": factors_str,
        "confidence": confidence,
        "latest_price": latest_price,
        "support": support,
        "resistance": resistance,
    }

    # ── 输出（精简格式，含支撑/压力位）──
    report = (
        f"**股票名称**: {stock_name} ({stock_code})\n"
        f"**综合评分**: {score}\n"
        f"**操作建议**: {action}\n"
        f"**方    向**: {direction_cn}\n"
        f"**置 信 度**: {confidence}\n"
        f"**时间窗口**: T+3\n"
        f"**当 前 价**: {price_str}\n"
        f"**支 撑 位**: {support_str}\n"
        f"**压 力 位**: {resistance_str}\n"
        f"**信号**: {signal_desc}\n"
        f"**综合分析**: {analysis}"
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
    """英文方向 → 中文。未知/空值返回空字符串（不显示）。"""
    if not direction or direction in ("unknown", ""):
        return ""
    return {"bullish": "看涨", "bearish": "看跌", "neutral": "中性"}.get(direction, "")

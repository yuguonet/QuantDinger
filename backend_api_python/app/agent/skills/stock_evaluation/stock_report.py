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
    chip: Optional[Dict[str, Any]] = None,
    period: str = "T+3",
    intel: Optional[Dict[str, Any]] = None,
    web: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """从工具返回的结构化数据直接生成个股分析报告。

    必须传入以下 5 个工具的返回结果（按参数名传入）：
    - info: 调用 get_stock_info 获取
    - technical: 调用 technical_analysis 获取（必须，评分/方向/信号来源）
    - capital: 调用 get_capital_summary 获取
    - quote: 调用 get_realtime_quote 获取
    - fund_flow: 调用 get_fund_flow 获取
    - chip: 调用 get_chip_distribution 获取（可选，deep/complete 级别）

    Args:
        info: get_stock_info 的返回结果
        technical: technical_analysis 的返回结果（必须）
        capital: get_capital_summary 的返回结果
        quote: get_realtime_quote 的返回结果
        fund_flow: get_fund_flow 的返回结果
        chip: get_chip_distribution 的返回结果（可选）

    Returns:
        {"report": "markdown 报告", "summary": {结构化摘要}} 或 {"error": "..."}
    """
    if not any([info, technical, capital, quote, fund_flow]):
        return {"error": "未传入任何工具数据"}

    try:
        report, summary = _generate_report(info or {}, technical or {}, capital or {}, quote or {}, fund_flow or {}, chip or {}, period, intel or {}, web or {})
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
    technical: dict, quote: dict, capital: dict, fund_flow: dict, chip_data: dict = None
) -> tuple[Optional[float], Optional[float], str]:
    """计算支撑位和压力位（技术面 + 筹码面融合）。

    优先级：Bollinger 带 > MA 均线 > 近期高低点。
    筹码面作为修正：筹码支撑/压力与技术面接近时取均值，差异大时保留两者。
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
            if latest < mid:
                resistance = mid
            else:
                resistance = upper
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

    # ── 筹码面修正 ──
    if chip_data:
        chip_supports = chip_data.get("support_prices", [])
        chip_resists = chip_data.get("resistance_prices", [])
        if chip_supports and latest:
            # 取离当前价最近且低于当前价的筹码支撑
            valid_s = [p for p in chip_supports if p < latest]
            if valid_s:
                chip_sup = max(valid_s)
                if support is None:
                    support = chip_sup
                else:
                    # 技术支撑与筹码支撑接近（<5%），取均值更稳定
                    if abs(chip_sup - support) / latest < 0.05:
                        support = round((support + chip_sup) / 2, 2)
                    elif chip_sup > support:
                        support = chip_sup  # 筹码支撑更高，用更保守的
        if chip_resists and latest:
            valid_r = [p for p in chip_resists if p > latest]
            if valid_r:
                chip_res = min(valid_r)
                if resistance is None:
                    resistance = chip_res
                else:
                    if abs(chip_res - resistance) / latest < 0.05:
                        resistance = round((resistance + chip_res) / 2, 2)
                    elif chip_res < resistance:
                        resistance = chip_res  # 筹码压力更低，用更保守的

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
    chip: dict = None, intel: dict = None, web: dict = None,
) -> str:
    """生成综合分析摘要（200字以内），整合技术面+资金面+筹码+新闻+搜索。"""
    parts = []

    # 形态信号
    factors = technical.get("factors", [])
    good_factors = [f for f in factors if f.get("score", 0) >= 70]
    if good_factors:
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

    # 筹码
    if chip and "error" not in chip:
        chip_data = chip if "profit_ratio_pct" in chip else chip.get("data", {})
        if isinstance(chip_data, dict):
            profit = chip_data.get("profit_ratio_pct", "")
            avg = chip_data.get("avg_cost")
            if profit:
                chip_desc = f"筹码获利{profit}"
                if avg:
                    chip_desc += f"，均价{avg:.2f}"
                parts.append(chip_desc)

    # 新闻情报
    if intel and "error" not in intel:
        news_list = intel.get("news", [])
        if news_list:
            # 取第一条有情感倾向的新闻
            for n in news_list[:3]:
                title = n.get("title", "")[:40]
                sentiment = n.get("sentiment", "")
                if sentiment and sentiment != "neutral":
                    parts.append(f"新闻:{title}({sentiment})")
                    break

    # web_search 关键信息
    if web and web.get("success"):
        results = web.get("results", [])
        if results:
            snippet = results[0].get("snippet", "")[:60]
            if snippet:
                parts.append(f"资讯:{snippet}")

    if not parts:
        return "数据不足，建议观望。"

    analysis = "，".join(parts) + f"。建议{action}。"
    return analysis[:200]


def _generate_report(
    info: dict, technical: dict, capital: dict, quote: dict, fund_flow: dict,
    chip: dict = None, period: str = "T+3", intel: dict = None, web: dict = None,
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

    # ── 筹码数据提取（如有）──
    chip_data = {}
    chip_info = ""
    if chip and "error" not in chip:
        raw = chip.get("data", {}).get(stock_code, chip)
        if isinstance(raw, dict) and "error" not in raw:
            chip_data = raw
            profit_pct = chip_data.get("profit_pct")
            avg_cost = chip_data.get("avg_cost")
            concentration = chip_data.get("concentration_90pct")
            if profit_pct is not None:
                chip_info = f"获利{profit_pct:.1f}%"
                if avg_cost:
                    chip_info += f"，均价{avg_cost:.2f}"
                if concentration:
                    low, high = concentration.get("low", 0), concentration.get("high", 0)
                    chip_info += f"，90%筹码[{low:.2f}-{high:.2f}]"

    # ── 新闻情报提取（如有）──
    news_signal = ""
    news_titles = []
    if intel and "error" not in intel:
        for n in intel.get("news", [])[:3]:
            title = n.get("title", "")[:50]
            sentiment = n.get("sentiment", "neutral")
            news_titles.append(title)
            if sentiment == "positive" and not news_signal:
                news_signal = "偏多"
            elif sentiment == "negative" and not news_signal:
                news_signal = "偏空"

    # ── 评分修正（筹码 + 新闻）──
    score_adjust = 0
    if chip_data:
        profit_ratio = chip_data.get("profit_ratio", 0.5)
        if profit_ratio >= 0.8:
            score_adjust += 5   # 高度获利，多头强势
        elif profit_ratio <= 0.2:
            score_adjust -= 5   # 深度套牢，抛压大
    if news_signal == "偏多":
        score_adjust += 3
    elif news_signal == "偏空":
        score_adjust -= 3
    score = max(0, min(100, score + score_adjust))

    # ── 方向修正 ──
    if direction == "neutral":
        if fund_signal and "流入" in fund_signal and chip_data and chip_data.get("profit_ratio", 0) >= 0.6:
            direction = "bullish"
        elif fund_signal and "流出" in fund_signal and chip_data and chip_data.get("profit_ratio", 0) <= 0.3:
            direction = "bearish"

    # ── 综合判断 ──
    action = _determine_action(score, direction, None, fund_signal, news_signal=news_signal)
    multi_source = _count_sources(technical, fund_signal, capital_signal, chip_data, news_signal)
    confidence = _determine_confidence(technical, fund_signal, capital_signal, chip_data, news_signal)
    direction_cn = _direction_cn(direction)

    # ── 支撑位 / 压力位（技术面 + 筹码面融合）──
    support, resistance, signal_desc = _calc_support_resistance(
        technical, quote, capital, fund_flow, chip_data
    )
    support_str = f"{support:.2f}" if support else "--"
    resistance_str = f"{resistance:.2f}" if resistance else "--"

    # ── 信号补充（筹码 + 新闻）──
    extra_signals = []
    if chip_data:
        chip_supports = chip_data.get("support_prices", [])
        chip_resists = chip_data.get("resistance_prices", [])
        if chip_supports:
            extra_signals.append(f"筹码支撑{chip_supports[0]:.2f}")
        if chip_resists:
            extra_signals.append(f"筹码压力{chip_resists[0]:.2f}")
    if news_signal:
        extra_signals.append(f"新闻{news_signal}")
    if extra_signals:
        signal_desc = signal_desc + " | " + ",".join(extra_signals) if signal_desc else ",".join(extra_signals)

    # ── 综合分析 ──
    analysis = _build_comprehensive_analysis(
        technical, fund_signal, capital_signal, action, direction,
        chip=chip, intel=intel, web=web,
    )

    # ── 结构化摘要 ──
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
        "chip": chip_info or "无数据",
    }

    # ── 输出（精简格式，含支撑/压力位）──
    report = (
        f"**股票名称**: {stock_name} ({stock_code})\n"
        f"**综合评分**: {score}\n"
        f"**操作建议**: {action}\n"
        f"**方    向**: {direction_cn}\n"
        f"**置 信 度**: {confidence}\n"
        f"**时间窗口**: {period}\n"
        f"**当 前 价**: {price_str}\n"
        f"**支 撑 位**: {support_str}\n"
        f"**压 力 位**: {resistance_str}\n"
        f"**信号**: {signal_desc}\n"
        f"**综合分析**: {analysis}"
    )
    if chip_info:
        report += f"\n**筹 码**: {chip_info}"
    return report, summary


def _determine_action(score: int, direction: str, pe: float | None, fund_signal: str, news_signal: str = "") -> str:
    """根据多维度数据决定操作建议（含新闻修正）。"""
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

    # 新闻修正
    if news_signal == "偏空" and base_action == "买入":
        base_action = "持有"
    if news_signal == "偏多" and base_action == "跳过" and score >= 50:
        base_action = "持有"

    return base_action


def _count_sources(technical: dict, fund_signal: str, capital_signal: str, chip_data: dict, news_signal: str) -> int:
    """统计有效数据源数量。"""
    count = 0
    if technical.get("score"):
        count += 1
    if fund_signal:
        count += 1
    if capital_signal:
        count += 1
    if chip_data:
        count += 1
    if news_signal:
        count += 1
    return count


def _determine_confidence(technical: dict, fund_signal: str, capital_signal: str, chip_data: dict = None, news_signal: str = "") -> str:
    """判断置信度（多源交叉验证）。"""
    confidence_val = technical.get("confidence", "")
    if confidence_val in ("high", "高"):
        base_conf = "高"
    elif confidence_val in ("low", "低"):
        base_conf = "低"
    else:
        base_conf = "中"

    # 根据数据源数量升级
    sources = _count_sources(technical, fund_signal, capital_signal, chip_data or {}, news_signal)
    if sources >= 4:
        return "高"
    if sources >= 2 and base_conf != "低":
        return "高" if base_conf == "高" else "中"
    if sources <= 1:
        return "低"
    return base_conf


def _direction_cn(direction: str) -> str:
    """英文方向 → 中文。未知/空值返回空字符串（不显示）。"""
    if not direction or direction in ("unknown", ""):
        return ""
    return {"bullish": "看涨", "bearish": "看跌", "neutral": "中性"}.get(direction, "")

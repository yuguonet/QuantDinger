# -*- coding: utf-8 -*-
"""
market-prediction/_report.py — 报告生成层

生成标准化 SkillReport，支持 formal/brief 双模板。
供 run.py 入口调用。

2026-11-27 v1.0: 初始版本
"""

import logging
from datetime import datetime
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def build_skill_report(
    factors: Dict[str, Any],
    prediction: Dict[str, Any],
    health: Dict[str, Any],
    style: str = "formal"
) -> Dict[str, Any]:
    """
    组装完整的 SkillReport。
    
    Args:
        factors: compute_all_factors() 返回的四维因子
        prediction: predict_market_direction() 返回的预测结果
        health: assess_model_health() 返回的健康度
        style: "formal" 详细版 / "brief" 简报版
        
    Returns:
        dict: 标准化 SkillReport
    """
    # 基础字段
    score = round(prediction.get("p_up", 0.5) * 100)
    confidence = prediction.get("confidence", 0.5)
    direction = prediction.get("direction", "neutral")
    
    # 仓位建议
    position_sizing = _build_position_sizing(score)
    
    # 多周期方向
    horizons = _build_horizons(prediction, factors)
    
    # 关键位
    key_levels = _build_key_levels(factors)
    
    # 风险标记
    risk_flags = _build_risk_flags(factors, prediction, health)
    
    # 自然语言分析
    analysis = _build_analysis(factors, prediction, health, style)
    
    report = {
        "skill": "market-prediction",
        "version": "1.0.0",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "score": score,
        "confidence": round(confidence, 3),
        "direction": direction,
        "factors": {
            "technical": factors.get("technical", {}),
            "fund_flow": factors.get("fund_flow", {}),
            "sentiment": factors.get("sentiment", {}),
            "valuation": factors.get("valuation", {}),
        },
        "position_sizing": position_sizing,
        "horizons": horizons,
        "model_health": health,
        "analysis": analysis,
        "key_levels": key_levels,
        "risk_flags": risk_flags,
    }
    
    if style == "brief":
        return _to_brief(report)
    
    return report


def _build_position_sizing(score: int) -> Dict[str, Any]:
    """构建仓位建议分档表"""
    if score >= 80:
        current_tier = "aggressive"
        max_pos = 100
    elif score >= 60:
        current_tier = "half"
        max_pos = 60
    elif score >= 40:
        current_tier = "light"
        max_pos = 30
    else:
        current_tier = "defensive"
        max_pos = 10
    
    tiers = [
        {"score_range": "80-100", "position": "满仓 80-100%", "action": "积极进取", "tier": "aggressive"},
        {"score_range": "60-79", "position": "半仓 40-60%", "action": "择机买入", "tier": "half"},
        {"score_range": "40-59", "position": "轻仓 20-30%", "action": "观望为主", "tier": "light"},
        {"score_range": "0-39", "position": "空仓 0-10%", "action": "严防风险", "tier": "defensive"},
    ]
    
    return {
        "max_position_pct": max_pos,
        "current_tier": current_tier,
        "tiers": tiers,
    }


def _build_horizons(prediction: Dict[str, Any], factors: Dict[str, Any]) -> Dict[str, Any]:
    """构建多周期方向"""
    p_up = prediction.get("p_up", 0.5)
    confidence = prediction.get("confidence", 0.5)
    
    # 简化：T+1 用模型直接输出，T+3/T+5 衰减置信度
    # 实际可接入多周期模型
    return {
        "T+1": {
            "direction": prediction.get("direction", "neutral"),
            "confidence": round(confidence, 3),
            "p_up": round(p_up, 4),
        },
        "T+3": {
            "direction": _p_up_to_direction(p_up * 0.9 + 0.05),  # 向均值回归
            "confidence": round(confidence * 0.85, 3),
            "p_up": round(p_up * 0.9 + 0.05, 4),
        },
        "T+5": {
            "direction": _p_up_to_direction(p_up * 0.8 + 0.1),
            "confidence": round(confidence * 0.7, 3),
            "p_up": round(p_up * 0.8 + 0.1, 4),
        },
    }


def _p_up_to_direction(p_up: float) -> str:
    if p_up >= 0.55:
        return "bullish"
    elif p_up <= 0.45:
        return "bearish"
    return "neutral"


def _build_key_levels(factors: Dict[str, Any]) -> Dict[str, List[float]]:
    """构建关键支撑/压力位"""
    tech_details = factors.get("technical", {}).get("details", {})
    current = tech_details.get("current_price", 3300)
    ma20 = tech_details.get("ma20", current)
    ma60 = tech_details.get("ma60", current)
    
    # 简化：基于均线和整数关口
    resistance = []
    support = []
    
    # 压力位：MA20, MA60, 整数关口
    for level in [ma20, ma60]:
        if level > current * 1.005:
            resistance.append(round(level, 2))
    
    # 整数关口
    int_levels = [i * 100 for i in range(25, 45)]
    for level in int_levels:
        if level > current * 1.005 and level < current * 1.1:
            resistance.append(float(level))
    
    # 支撑位
    for level in [ma20, ma60]:
        if level < current * 0.995:
            support.append(round(level, 2))
    
    for level in int_levels:
        if level < current * 0.995 and level > current * 0.9:
            support.append(float(level))
    
    # 去重排序
    resistance = sorted(list(set(resistance)))[:5]
    support = sorted(list(set(support)), reverse=True)[:5]
    
    return {"resistance": resistance, "support": support}


def _build_risk_flags(
    factors: Dict[str, Any],
    prediction: Dict[str, Any],
    health: Dict[str, Any]
) -> List[str]:
    """构建风险标记"""
    flags = []
    
    # 模型健康度风险
    if health.get("drift_alert"):
        flags.append("模型特征漂移预警")
    if health.get("signal_decay"):
        flags.append("信号衰减预警")
    
    # 因子冲突风险
    tech = factors.get("technical", {}).get("score", 50)
    fund = factors.get("fund_flow", {}).get("score", 50)
    sent = factors.get("sentiment", {}).get("score", 50)
    val = factors.get("valuation", {}).get("score", 50)
    
    scores = {"技术": tech, "资金": fund, "情绪": sent, "估值": val}
    max_s = max(scores.values())
    min_s = min(scores.values())
    
    if max_s - min_s > 40:
        flags.append(f"多维度分歧大（{max_s}-{min_s}={max_s-min_s}分）")
    
    # 具体风险
    tech_det = factors.get("technical", {}).get("details", {})
    if tech_det.get("bollinger", 50) > 85:
        flags.append("布林带上轨压力大")
    if tech_det.get("bollinger", 50) < 15:
        flags.append("布林带下轨支撑强")
    
    fund_det = factors.get("fund_flow", {}).get("details", {})
    if fund_det.get("north_flow", 50) < 30:
        flags.append("北向资金持续流出")
    
    sent_det = factors.get("sentiment", {}).get("details", {})
    if sent_det.get("broken_rate", 50) < 30:
        flags.append("炸板率过高")
    
    val_det = factors.get("valuation", {}).get("details", {})
    if val_det.get("pe_percentile", 50) < 20:
        flags.append("PE处于历史极低分位")
    elif val_det.get("pe_percentile", 50) > 90:
        flags.append("PE处于历史极高分位")
    
    return flags


def _build_analysis(
    factors: Dict[str, Any],
    prediction: Dict[str, Any],
    health: Dict[str, Any],
    style: str
) -> str:
    """生成自然语言分析"""
    score = round(prediction.get("p_up", 0.5) * 100)
    direction = prediction.get("direction", "neutral")
    confidence = prediction.get("confidence", 0.5)
    
    tech_s = factors.get("technical", {}).get("score", 50)
    fund_s = factors.get("fund_flow", {}).get("score", 50)
    sent_s = factors.get("sentiment", {}).get("score", 50)
    val_s = factors.get("valuation", {}).get("score", 50)
    
    dir_map = {"bullish": "偏多", "neutral": "中性", "bearish": "偏空"}
    dir_cn = dir_map.get(direction, "中性")
    
    if style == "brief":
        return (
            f"大盘择时分 {score}，{dir_cn}（置信度 {confidence:.0%}）。"
            f"四维：技术{tech_s:.0f}/资金{fund_s:.0f}/情绪{sent_s:.0f}/估值{val_s:.0f}。"
            f"建议仓位：{_position_tier_cn(score)}。"
        )
    
    # formal 详细版
    lines = [
        f"【大盘择时分析】{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"综合得分：{score} 分 | 方向：{dir_cn} | 置信度：{confidence:.0%}",
        "",
        "【四维因子拆解】",
        f"  技术面（30%）：{tech_s:.1f} 分 — {_factor_desc('technical', tech_s, factors.get('technical', {}).get('details', {}))}",
        f"  资金面（25%）：{fund_s:.1f} 分 — {_factor_desc('fund_flow', fund_s, factors.get('fund_flow', {}).get('details', {}))}",
        f"  情绪面（20%）：{sent_s:.1f} 分 — {_factor_desc('sentiment', sent_s, factors.get('sentiment', {}).get('details', {}))}",
        f"  估值面（25%）：{val_s:.1f} 分 — {_factor_desc('valuation', val_s, factors.get('valuation', {}).get('details', {}))}",
        "",
        "【模型预测】",
        f"  P(次日上涨) = {prediction.get('p_up', 0.5):.1%}",
        f"  预期收益 = {prediction.get('exp_ret_bp', 0):.0f} bp",
        f"  Logit = {prediction.get('logit', 0):.3f}",
        "",
        "【仓位建议】",
        f"  当前档位：{_position_tier_cn(score)}（{_position_range_cn(score)}）",
        f"  操作建议：{_position_action_cn(score)}",
        "",
        "【关键位】",
        f"  压力位：{', '.join(str(x) for x in _build_key_levels(factors)['resistance'][:3]) or '—'}",
        f"  支撑位：{', '.join(str(x) for x in _build_key_levels(factors)['support'][:3]) or '—'}",
    ]
    
    risk_flags = _build_risk_flags(factors, prediction, health)
    if risk_flags:
        lines.append("")
        lines.append("【风险提示】")
        for flag in risk_flags:
            lines.append(f"  ⚠ {flag}")
    
    if health.get("drift_alert") or health.get("signal_decay"):
        lines.append("")
        lines.append("【模型健康度】")
        if health.get("drift_alert"):
            lines.append("  ⚠ 特征分布发生漂移，建议重训练")
        if health.get("signal_decay"):
            lines.append("  ⚠ IC滚动均值下降，预测力衰减")
        lines.append(f"  最近重训练：{health.get('last_retrain', 'unknown')}")
    
    return "\n".join(lines)


def _factor_desc(dim: str, score: float, details: Dict) -> str:
    """因子文字描述"""
    if dim == "technical":
        ma_trend = details.get("ma_trend", 50)
        macd = details.get("macd", 50)
        if ma_trend > 70 and macd > 70:
            return "均线多头排列完美，MACD金叉向上"
        elif ma_trend > 60:
            return "均线偏多，趋势向上"
        elif ma_trend < 40:
            return "均线空头排列，趋势向下"
        else:
            return "均线粘合，方向不明"
    elif dim == "fund_flow":
        north = details.get("north_flow", 50)
        if north > 70:
            return "北向资金大幅净流入，融资余额增加"
        elif north > 55:
            return "资金面温和流入"
        elif north < 30:
            return "北向资金持续流出，融资余额下降"
        else:
            return "资金面中性偏弱"
    elif dim == "sentiment":
        ltd = details.get("limit_up_down", 50)
        broken = details.get("broken_rate", 50)
        if ltd > 70 and broken > 70:
            return "涨停家数多、炸板率低，情绪高涨"
        elif ltd > 60:
            return "涨跌停比健康，情绪偏暖"
        elif ltd < 30:
            return "涨停稀少、跌停增多，情绪低迷"
        else:
            return "情绪中性"
    elif dim == "valuation":
        pe = details.get("pe_percentile", 50)
        yg = details.get("yield_gap", 50)
        if pe < 30 and yg > 70:
            return "PE历史低位，股债利差宽，安全边际高"
        elif pe < 40:
            return "估值处于历史低位区间"
        elif pe > 70:
            return "估值偏高，需警惕回调"
        else:
            return "估值中性"
    return ""


def _position_tier_cn(score: int) -> str:
    if score >= 80:
        return "激进档"
    elif score >= 60:
        return "半仓档"
    elif score >= 40:
        return "轻仓档"
    return "防守档"


def _position_range_cn(score: int) -> str:
    if score >= 80:
        return "80-100%"
    elif score >= 60:
        return "40-60%"
    elif score >= 40:
        return "20-30%"
    return "0-10%"


def _position_action_cn(score: int) -> str:
    if score >= 80:
        return "积极进取，追击强势"
    elif score >= 60:
        return "择机买入，回踩加仓"
    elif score >= 40:
        return "观望为主，仅做确认票"
    return "严防风险，空仓待机"


def _to_brief(report: Dict[str, Any]) -> Dict[str, Any]:
    """转为简报版（精简字段）"""
    return {
        "skill": report["skill"],
        "version": report["version"],
        "timestamp": report["timestamp"],
        "score": report["score"],
        "confidence": report["confidence"],
        "direction": report["direction"],
        "position": report["position_sizing"]["current_tier"],
        "horizons": {k: v["direction"] for k, v in report["horizons"].items()},
        "analysis": report["analysis"],
        "risk_flags": report["risk_flags"],
    }
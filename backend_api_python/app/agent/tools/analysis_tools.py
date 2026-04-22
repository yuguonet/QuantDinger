# -*- coding: utf-8 -*-
"""
Analysis tools — technical trend, MA system, volume analysis, chip distribution.
Pure-Python calculations on K-line data, no external API calls.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _get_ds(market: str = "CNStock"):
    from app.data_sources.factory import DataSourceFactory
    return DataSourceFactory.get_source(market)


# ── Re-exported from shared utils (kept for backward compat) ──
from app.agent.utils import detect_market as _detect_market


def _fetch_closes(stock_code: str, days: int = 60) -> List[float]:
    """Fetch close prices from data source."""
    market = _detect_market(stock_code)
    ds = _get_ds(market)
    klines = ds.get_kline(stock_code, "1D", days) or []
    return [float(k.get("close", 0)) for k in klines if k.get("close")]


def analyze_trend(stock_code: str) -> Dict[str, Any]:
    """分析股票技术趋势：均线排列、趋势方向、买卖信号。
    
    计算 MA5/MA10/MA20/MA60，判断多头/空头排列，给出趋势评分。
    """
    try:
        closes = _fetch_closes(stock_code, 60)
        if len(closes) < 5:
            return {"error": "K线数据不足（至少需要5根）", "retriable": True}

        latest = closes[-1]
        prev = closes[-2] if len(closes) >= 2 else latest

        ma5 = round(sum(closes[-5:]) / min(5, len(closes)), 4)
        ma10 = round(sum(closes[-10:]) / min(10, len(closes)), 4) if len(closes) >= 10 else ma5
        ma20 = round(sum(closes[-20:]) / min(20, len(closes)), 4) if len(closes) >= 20 else ma10
        ma60 = round(sum(closes) / len(closes), 4)

        # 趋势判断
        if ma5 > ma10 > ma20 > ma60:
            trend = "强多头排列"
            trend_score = 90
        elif ma5 > ma10 > ma20:
            trend = "多头排列"
            trend_score = 75
        elif ma5 > ma20:
            trend = "弱势多头"
            trend_score = 60
        elif ma5 < ma10 < ma20 < ma60:
            trend = "强空头排列"
            trend_score = 10
        elif ma5 < ma10 < ma20:
            trend = "空头排列"
            trend_score = 25
        elif ma5 < ma20:
            trend = "弱势空头"
            trend_score = 40
        else:
            trend = "均线缠绕/震荡"
            trend_score = 50

        # 乖离率
        bias_ma5 = round((latest - ma5) / ma5 * 100, 2) if ma5 else 0
        bias_ma20 = round((latest - ma20) / ma20 * 100, 2) if ma20 else 0

        # 买卖信号
        if bias_ma5 < 2 and trend_score >= 75:
            buy_signal = "回踩低吸"
            signal_score = 85
        elif trend_score >= 75:
            buy_signal = "趋势延续"
            signal_score = 70
        elif trend_score <= 25:
            buy_signal = "观望/减仓"
            signal_score = 20
        else:
            buy_signal = "观望"
            signal_score = 50

        change_pct = round((latest - prev) / prev * 100, 2) if prev else 0

        return {
            "stock_code": stock_code,
            "latest_close": round(latest, 4),
            "change_pct": change_pct,
            "ma5": ma5, "ma10": ma10, "ma20": ma20, "ma60": ma60,
            "trend": trend,
            "trend_score": trend_score,
            "bias_ma5": bias_ma5,
            "bias_ma20": bias_ma20,
            "buy_signal": buy_signal,
            "signal_score": signal_score,
            "data_points": len(closes),
        }
    except Exception as e:
        logger.error("analyze_trend(%s) failed: %s", stock_code, e)
        return {"error": str(e)}


def calculate_ma(stock_code: str, periods: str = "5,10,20,60") -> Dict[str, Any]:
    """计算指定周期的均线数值。
    
    Args:
        stock_code: 股票代码
        periods: 均线周期，逗号分隔（如 "5,10,20,60"）
    """
    try:
        period_list = sorted(set(int(p.strip()) for p in periods.split(",") if p.strip().isdigit()))
        if not period_list:
            return {"error": "无效的周期参数"}
        
        max_period = max(period_list)
        closes = _fetch_closes(stock_code, max_period + 5)
        if len(closes) < max_period:
            return {"error": f"数据不足，需要至少{max_period}根K线"}

        result = {"stock_code": stock_code, "latest_close": round(closes[-1], 4)}
        for p in period_list:
            avg = round(sum(closes[-p:]) / p, 4)
            result[f"ma{p}"] = avg
        return result
    except Exception as e:
        logger.error("calculate_ma(%s) failed: %s", stock_code, e)
        return {"error": str(e)}


def get_volume_analysis(stock_code: str) -> Dict[str, Any]:
    """分析量能变化：量比、成交量趋势、放量/缩量判断。"""
    try:
        market = _detect_market(stock_code)
        ds = _get_ds(market)
        klines = ds.get_kline(stock_code, "1D", 20) or []
        if len(klines) < 5:
            return {"error": "K线数据不足"}

        volumes = [float(k.get("volume", 0)) for k in klines if k.get("volume")]
        if not volumes:
            return {"error": "成交量数据缺失"}

        latest_vol = volumes[-1]
        avg_vol_5 = sum(volumes[-5:]) / min(5, len(volumes))
        avg_vol_20 = sum(volumes) / len(volumes)

        volume_ratio = round(latest_vol / avg_vol_5, 2) if avg_vol_5 else 0

        if volume_ratio > 2.0:
            status = "显著放量"
            meaning = "放量上涨/下跌，需结合价格方向判断"
        elif volume_ratio > 1.5:
            status = "温和放量"
            meaning = "成交活跃度提升"
        elif volume_ratio < 0.5:
            status = "明显缩量"
            meaning = "缩量回调，抛压减轻；或缩量上涨，需谨慎"
        elif volume_ratio < 0.8:
            status = "温和缩量"
            meaning = "成交活跃度下降"
        else:
            status = "平量"
            meaning = "成交量维持常态"

        # 成交量趋势
        if len(volumes) >= 5:
            recent_avg = sum(volumes[-3:]) / 3
            earlier_avg = sum(volumes[-6:-3]) / 3 if len(volumes) >= 6 else avg_vol_20
            vol_trend = "上升" if recent_avg > earlier_avg * 1.1 else ("下降" if recent_avg < earlier_avg * 0.9 else "平稳")
        else:
            vol_trend = "数据不足"

        return {
            "stock_code": stock_code,
            "latest_volume": round(latest_vol, 0),
            "avg_volume_5d": round(avg_vol_5, 0),
            "avg_volume_20d": round(avg_vol_20, 0),
            "volume_ratio": volume_ratio,
            "volume_status": status,
            "volume_meaning": meaning,
            "volume_trend": vol_trend,
        }
    except Exception as e:
        logger.error("get_volume_analysis(%s) failed: %s", stock_code, e)
        return {"error": str(e)}


def analyze_pattern(stock_code: str) -> Dict[str, Any]:
    """识别K线形态（简化版）：锤子线、十字星、吞没形态等。"""
    try:
        market = _detect_market(stock_code)
        ds = _get_ds(market)
        klines = ds.get_kline(stock_code, "1D", 5) or []
        if len(klines) < 2:
            return {"error": "K线数据不足"}

        patterns = []
        latest = klines[-1]
        o, h, l, c = (float(latest.get(k, 0)) for k in ("open", "high", "low", "close"))
        body = abs(c - o)
        candle_range = h - l if h > l else 1
        upper_shadow = h - max(o, c)
        lower_shadow = min(o, c) - l

        # 锤子线 / 倒锤子
        if body > 0 and candle_range > 0:
            if lower_shadow >= 2 * body and upper_shadow <= body * 0.3:
                patterns.append("锤子线（底部反转信号）")
            elif upper_shadow >= 2 * body and lower_shadow <= body * 0.3:
                patterns.append("倒锤子线（顶部反转信号）")

        # 十字星
        if body <= candle_range * 0.1:
            patterns.append("十字星（犹豫信号）")

        # 吞没形态
        if len(klines) >= 2:
            prev = klines[-2]
            po, pc = float(prev.get("open", 0)), float(prev.get("close", 0))
            prev_body = abs(pc - po)
            if prev_body > 0 and body > prev_body:
                if pc < po and c > o and o < pc and c > po:
                    patterns.append("看涨吞没（底部反转）")
                elif pc > po and c < o and o > pc and c < po:
                    patterns.append("看跌吞没（顶部反转）")

        if not patterns:
            patterns.append("无明显特殊形态")

        return {
            "stock_code": stock_code,
            "latest_candle": {"open": o, "high": h, "low": l, "close": c},
            "body_size": round(body, 4),
            "upper_shadow": round(upper_shadow, 4),
            "lower_shadow": round(lower_shadow, 4),
            "patterns": patterns,
        }
    except Exception as e:
        logger.error("analyze_pattern(%s) failed: %s", stock_code, e)
        return {"error": str(e)}


def get_chip_distribution(stock_code: str) -> Dict[str, Any]:
    """分析筹码分布：获利比例、平均成本、集中度。
    仅A股支持，其他市场返回提示。
    """
    market = _detect_market(stock_code)
    if market != "CNStock":
        return {"error": f"筹码分布分析仅支持A股，当前市场: {market}", "retriable": False}

    ds = _get_ds(market)
    try:
        if hasattr(ds, "get_chip_distribution"):
            return ds.get_chip_distribution(stock_code)
        return {"error": "当前数据源不支持筹码分布分析", "retriable": False}
    except NotImplementedError:
        return {"error": "当前数据源不支持筹码分布分析", "retriable": False}
    except Exception as e:
        logger.error("get_chip_distribution(%s) failed: %s", stock_code, e)
        return {"error": str(e)}


# ── OpenAI tool declarations ─────────────────────────────────

ANALYSIS_TOOLS = [
    {
        "fn": analyze_trend,
        "name": "analyze_trend",
        "description": "分析股票技术趋势：均线排列（MA5/10/20/60）、趋势方向、乖离率、买卖信号评分。",
        "parameters": {
            "type": "object",
            "properties": {
                "stock_code": {"type": "string", "description": "股票代码"},
            },
            "required": ["stock_code"],
        },
    },
    {
        "fn": calculate_ma,
        "name": "calculate_ma",
        "description": "计算指定周期的移动平均线数值。",
        "parameters": {
            "type": "object",
            "properties": {
                "stock_code": {"type": "string", "description": "股票代码"},
                "periods": {"type": "string", "description": "均线周期，逗号分隔，默认5,10,20,60", "default": "5,10,20,60"},
            },
            "required": ["stock_code"],
        },
    },
    {
        "fn": get_volume_analysis,
        "name": "get_volume_analysis",
        "description": "分析量能变化：量比、成交量趋势、放量/缩量判断。",
        "parameters": {
            "type": "object",
            "properties": {
                "stock_code": {"type": "string", "description": "股票代码"},
            },
            "required": ["stock_code"],
        },
    },
    {
        "fn": analyze_pattern,
        "name": "analyze_pattern",
        "description": "识别K线形态：锤子线、十字星、吞没形态等反转信号。",
        "parameters": {
            "type": "object",
            "properties": {
                "stock_code": {"type": "string", "description": "股票代码"},
            },
            "required": ["stock_code"],
        },
    },
    {
        "fn": get_chip_distribution,
        "name": "get_chip_distribution",
        "description": "分析筹码分布：获利比例、平均成本、集中度（仅A股支持）。",
        "parameters": {
            "type": "object",
            "properties": {
                "stock_code": {"type": "string", "description": "股票代码"},
            },
            "required": ["stock_code"],
        },
    },
]

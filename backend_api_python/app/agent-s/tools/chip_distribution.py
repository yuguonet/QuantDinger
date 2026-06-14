# -*- coding: utf-8 -*-
"""
Chip Distribution — 筹码分布计算（衰减成本分布模型）。

从日K线计算筹码分布，不依赖数据源原生接口。
算法：按日K线的 high/low 区间分配成交量到价格档位，
用指数衰减加权（近期筹码权重更高），汇总计算各维度指标。

设计文档：AGENT_ACCOUNTABLE.md §12.2
"""
from __future__ import annotations

import logging
import math
from datetime import date as _date
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def calc_chip_distribution(
    klines: List[Dict[str, Any]],
    stock_code: str = "",
    lookback_days: int = 120,
    half_life: int = 30,
    num_bins: int = 200,
) -> Dict[str, Any]:
    """筹码分布计算。

    Args:
        klines: K线数据列表，每条需含 date/time, low, high, close, volume
        stock_code: 股票代码（仅用于返回值）
        lookback_days: 回看天数
        half_life: 衰减半衰期（天），默认30天
        num_bins: 价格档位数，默认200

    Returns:
        筹码分布结果 dict，或 {"error": "..."} 
    """
    if not klines or len(klines) < 20:
        return {"error": f"K线数据不足（需要至少20根，获取到{len(klines)}根）", "retriable": True}

    today = _date.today()

    # ── 解析 K 线 ──
    bars = []
    for k in klines:
        try:
            bar_date = k.get("date", k.get("time", k.get("timestamp", "")))
            if isinstance(bar_date, str) and len(bar_date) >= 10:
                y, m, d = int(bar_date[:4]), int(bar_date[5:7]), int(bar_date[8:10])
                bar_dt = _date(y, m, d)
            else:
                bar_dt = today
            bars.append({
                "date": bar_dt,
                "low": float(k.get("low", 0)),
                "high": float(k.get("high", 0)),
                "close": float(k.get("close", 0)),
                "volume": float(k.get("volume", 0)),
            })
        except (ValueError, TypeError, KeyError):
            continue

    if len(bars) < 20:
        return {"error": "有效K线数据不足", "retriable": True}

    # ── 确定价格区间 ──
    all_low = min(b["low"] for b in bars)
    all_high = max(b["high"] for b in bars)
    price_range = all_high - all_low
    if price_range <= 0:
        return {"error": "价格区间为零，无法计算筹码分布", "retriable": True}

    step = price_range / num_bins
    current_price = bars[-1]["close"]

    # ── 逐日分配筹码 ──
    chips = [0.0] * num_bins
    ln2 = math.log(2)

    for bar in bars:
        days_ago = (today - bar["date"]).days
        if days_ago < 0:
            days_ago = 0
        weight = math.exp(-ln2 / half_life * days_ago)

        low_idx = max(0, int((bar["low"] - all_low) / step))
        high_idx = min(num_bins - 1, int((bar["high"] - all_low) / step))
        if high_idx < low_idx:
            high_idx = low_idx

        spread = high_idx - low_idx + 1
        vol_per_bin = bar["volume"] * weight / spread

        for i in range(low_idx, high_idx + 1):
            chips[i] += vol_per_bin

    # ── 汇总计算 ──
    total_chips = sum(chips)
    if total_chips <= 0:
        return {"error": "筹码总量为零", "retriable": True}

    price_at = lambda idx: all_low + (idx + 0.5) * step

    # 获利比例
    profit_chips = sum(chips[i] for i in range(num_bins) if price_at(i) <= current_price)
    profit_ratio = round(profit_chips / total_chips * 100, 2)
    trapped_ratio = round(100 - profit_ratio, 2)

    # 平均成本
    avg_cost = round(sum(chips[i] * price_at(i) for i in range(num_bins)) / total_chips, 2)

    # 筹码密集峰值价
    peak_idx = chips.index(max(chips))
    peak_price = round(price_at(peak_idx), 2)

    # 90% 筹码价格区间（去掉上下各5%）
    cumsum = 0.0
    lower_bound_idx = 0
    upper_bound_idx = num_bins - 1
    lower_5pct = total_chips * 0.05
    upper_5pct = total_chips * 0.95

    for i in range(num_bins):
        cumsum += chips[i]
        if cumsum >= lower_5pct and lower_bound_idx == 0:
            lower_bound_idx = i
        if cumsum >= upper_5pct:
            upper_bound_idx = i
            break

    support_1 = round(price_at(lower_bound_idx), 2)
    resistance_1 = round(price_at(upper_bound_idx), 2)
    chip_range_90 = round(resistance_1 - support_1, 2)
    concentration = round(chip_range_90 / avg_cost * 100, 2) if avg_cost > 0 else 0

    return {
        "stock_code": stock_code,
        "current_price": round(current_price, 2),
        "profit_ratio": profit_ratio,
        "trapped_ratio": trapped_ratio,
        "avg_cost": avg_cost,
        "peak_price": peak_price,
        "chip_range_90": chip_range_90,
        "concentration": concentration,
        "support_1": support_1,
        "resistance_1": resistance_1,
        "lookback_days": len(bars),
        "data_source": "kline_calc",
    }

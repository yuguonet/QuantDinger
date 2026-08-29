# -*- coding: utf-8 -*-
"""
筹码分布计算服务（前端图表专用）。

独立于 agent/tools，不走 LLM function calling 链路。
算法与 chip_distribution.py 相同：三角分布 + 时间衰减。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.utils.logger import get_logger

logger = get_logger(__name__)


def calc_chip_for_chart(
    klines: List[Dict[str, Any]],
    lookback_days: int = 120,
    num_buckets: int = 80,
) -> Optional[Dict[str, Any]]:
    """计算筹码分布，返回前端绘图所需的 prices/density 数组。

    Args:
        klines: [{ time, open, high, low, close, volume }, ...]
        lookback_days: 回看天数
        num_buckets: 价格桶数量

    Returns:
        { prices: [...], density: [...], avg_cost, current_price,
          profit_ratio, support_prices, resistance_prices }
        失败返回 None
    """
    if not klines:
        return None

    if lookback_days > 0 and len(klines) > lookback_days:
        klines = klines[-lookback_days:]

    closes = [float(k.get("close", 0)) for k in klines]
    highs = [float(k.get("high", 0)) for k in klines]
    lows = [float(k.get("low", 0)) for k in klines]
    volumes = [float(k.get("volume", 0)) for k in klines]

    if not closes:
        return None

    current_price = closes[-1]
    price_min = min(lows)
    price_max = max(highs)
    if price_max <= price_min:
        return None

    bucket_width = max((price_max - price_min) / num_buckets, 0.01)
    buckets = int((price_max - price_min) / bucket_width) + 1
    chip_density = [0.0] * buckets
    n = len(klines)

    for i in range(n):
        lo, hi, cl, vol = lows[i], highs[i], closes[i], volumes[i]
        if hi <= lo or vol <= 0:
            continue

        age = n - 1 - i
        decay = 0.98 ** age

        left_half = max(cl - lo, 0.001)
        right_half = max(hi - cl, 0.001)

        steps = max(int((hi - lo) / bucket_width) + 1, 10)
        total_weight = 0.0
        weights = []
        for j in range(steps + 1):
            p = lo + (hi - lo) * j / steps
            dist = (cl - p) / left_half if p <= cl else (p - cl) / right_half
            w = max(1.0 - dist, 0.0)
            weights.append((p, w))
            total_weight += w

        if total_weight <= 0:
            continue

        for p, w in weights:
            idx = int((p - price_min) / bucket_width)
            idx = max(0, min(idx, buckets - 1))
            chip_density[idx] += (vol * decay * w / total_weight)

    max_density = max(chip_density) if chip_density else 0
    if max_density <= 0:
        return None

    total_chips = sum(chip_density)
    prices = [round(price_min + i * bucket_width, 4) for i in range(buckets)]
    density_normalized = [round(d / max_density, 6) for d in chip_density]

    # 加权平均成本
    avg_cost = round(sum(prices[i] * chip_density[i] for i in range(buckets)) / total_chips, 2)

    # 获利比例
    profit_ratio = round(sum(chip_density[i] for i in range(buckets) if prices[i] <= current_price) / total_chips, 4)

    # 筹码峰 → 支撑/阻力
    peak_threshold = max_density * 0.15
    peaks = []
    for i in range(1, buckets - 1):
        if (chip_density[i] > chip_density[i - 1] and
                chip_density[i] >= chip_density[i + 1] and
                chip_density[i] >= peak_threshold):
            strength = chip_density[i] / total_chips
            peaks.append({"price": prices[i], "strength": round(strength, 4)})
    peaks.sort(key=lambda x: x["strength"], reverse=True)

    support = sorted([p["price"] for p in peaks if p["price"] < current_price], reverse=True)[:3]
    resistance = sorted([p["price"] for p in peaks if p["price"] > current_price])[:3]

    return {
        "prices": prices,
        "density": density_normalized,
        "avg_cost": avg_cost,
        "current_price": round(current_price, 2),
        "profit_ratio": profit_ratio,
        "support_prices": support,
        "resistance_prices": resistance,
    }

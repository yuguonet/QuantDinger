# -*- coding: utf-8 -*-
"""
芯片分布（筹码分布）计算模块。

用 K 线数据模拟筹码分布，不依赖外部 API。
算法：
  1. 每条 K 线的成交量按三角分布（峰值在 close）分配至 low~high 区间
  2. 时间指数衰减加权（近期权重更高）
  3. 离散化价格桶，累积筹码量
  4. 计算加权平均成本、90% 集中区间、获利/亏损比例
"""
from __future__ import annotations

from app.agent.log import logger
from typing import Any, Dict, List
def calc_chip_distribution(
    klines: List[Dict[str, Any]],
    stock_code: str = "",
    lookback_days: int = 120,
    num_buckets: int = 80,
    output: str = "markdown",
) -> str:
    """计算筹码分布。

    Args:
        klines: K 线列表，每项含 time/open/high/low/close/volume
        stock_code: 股票代码
        lookback_days: 回看天数
        num_buckets: 价格桶数量

    Returns:
        {
            "stock_code": ...,
            "avg_cost": 加权平均成本,
            "current_price": 最新收盘价,
            "profit_ratio": 获利比例(0~1),
            "loss_ratio":  亏损比例(0~1),
            "concentration_90": "低/中/高",
            "concentration_90_upper": 90%区间上沿,
            "concentration_90_lower": 90%区间下沿,
            "concentration_90_width": 区间宽度百分比,
            "support_prices": [支撑位列表],
            "resistance_prices": [阻力位列表],
            "chip_peaks": [筹码峰(price, ratio)],
            "total_volume_analyzed": 分析总成交量,
        }
    """
    if not klines:
        return {"error": "K线数据为空"}

    # 提取 OHLCV
    closes = [float(k.get("close", 0)) for k in klines]
    highs = [float(k.get("high", 0)) for k in klines]
    lows = [float(k.get("low", 0)) for k in klines]
    volumes = [float(k.get("volume", 0)) for k in klines]

    if not closes:
        return {"error": "K线缺少价格数据"}

    current_price = closes[-1]

    # 价格区间 + 桶宽度
    price_min = min(lows)
    price_max = max(highs)
    if price_max <= price_min:
        return {"error": "价格区间异常"}

    # 自适应桶间距（最小 0.01）
    bucket_width = max((price_max - price_min) / num_buckets, 0.01)
    buckets = int((price_max - price_min) / bucket_width) + 1

    # 筹码累积数组 [price_index] = total_weighted_volume
    chip_density = [0.0] * buckets

    n = len(klines)
    for i in range(n):
        lo, hi, cl, vol = lows[i], highs[i], closes[i], volumes[i]
        if hi <= lo or vol <= 0:
            continue

        # 时间衰减权重：近期权重高，指数衰减
        age = n - 1 - i  # 0=最新
        decay = 0.98 ** age

        # 三角分布：峰值在 close，两端在 low/high
        # 计算每个离散价格步长的权重，然后分配到桶
        steps = max(int((hi - lo) / bucket_width) + 1, 10)
        total_weight = 0.0
        weights = []
        for j in range(steps + 1):
            p = lo + (hi - lo) * j / steps
            # 三角分布：到 close 越近权重越高
            half_range = max(hi - cl, cl - lo, 0.001)
            dist = abs(p - cl) / half_range
            w = max(1.0 - dist, 0.0)
            weights.append((p, w))
            total_weight += w

        if total_weight <= 0:
            continue

        # 按桶累积
        for p, w in weights:
            idx = int((p - price_min) / bucket_width)
            if 0 <= idx < buckets:
                chip_density[idx] += (vol * decay * w / total_weight)

    if max(chip_density) <= 0:
        return {"error": "筹码计算无有效数据"}

    # ── 计算总量和累积比例 ──
    total_chips = sum(chip_density)

    # 累积比例
    cum_ratio = []
    acc = 0.0
    for d in chip_density:
        acc += d
        cum_ratio.append(acc / total_chips)

    # 价格序列
    prices = [price_min + i * bucket_width for i in range(buckets)]

    # ── 加权平均成本 ──
    avg_cost = sum(prices[i] * chip_density[i] for i in range(buckets)) / total_chips

    # ── 90% 集中区间（剔除两端各 5%） ──
    lower_idx = 0
    upper_idx = buckets - 1
    for i in range(buckets):
        if cum_ratio[i] >= 0.05:
            lower_idx = i
            break
    for i in range(buckets - 1, -1, -1):
        if cum_ratio[i] <= 0.95:
            upper_idx = i
            break

    c90_lower = prices[lower_idx]
    c90_upper = prices[upper_idx]
    c90_width = (c90_upper - c90_lower) / avg_cost if avg_cost > 0 else 0

    if c90_width < 0.15:
        concentration = "高"
    elif c90_width < 0.35:
        concentration = "中"
    else:
        concentration = "低"

    # ── 获利/亏损比例 ──
    profit_ratio = 0.0
    for i in range(buckets):
        if prices[i] <= current_price:
            profit_ratio += chip_density[i]
    profit_ratio /= total_chips if total_chips > 0 else 1
    loss_ratio = 1.0 - profit_ratio

    # ── 筹码峰检测（局部最大值） ──
    peaks = []
    density_sum = sum(chip_density)
    peak_threshold = max(chip_density) * 0.4  # 至少主峰 40%

    for i in range(1, buckets - 1):
        if (chip_density[i] > chip_density[i - 1] and
                chip_density[i] >= chip_density[i + 1] and
                chip_density[i] >= peak_threshold):
            strength = chip_density[i] / density_sum if density_sum > 0 else 0
            peaks.append({
                "price": round(prices[i], 2),
                "strength": round(strength, 4),
            })

    peaks.sort(key=lambda x: x["strength"], reverse=True)

    # ── 支撑/阻力位：筹码峰最密集的价格区间 ──
    sorted_peaks = sorted(peaks, key=lambda x: x["strength"], reverse=True)
    support_prices = []
    resistance_prices = []
    for p in sorted_peaks:
        if p["price"] < current_price:
            support_prices.append(p["price"])
        elif p["price"] > current_price:
            resistance_prices.append(p["price"])

    # 只保留前 3
    support_prices = sorted(support_prices, reverse=True)[:3]
    resistance_prices = sorted(resistance_prices)[:3]

    _r = {
        "stock_code": stock_code,
        "avg_cost": round(avg_cost, 2),
        "current_price": round(current_price, 2),
        "profit_ratio": round(profit_ratio, 4),
        "loss_ratio": round(loss_ratio, 4),
        "profit_ratio_pct": f"{round(profit_ratio * 100, 1)}%",
        "loss_ratio_pct": f"{round(loss_ratio * 100, 1)}%",
        "concentration_90": concentration,
        "concentration_90_lower": round(c90_lower, 2),
        "concentration_90_upper": round(c90_upper, 2),
        "concentration_90_width_pct": f"{round(c90_width * 100, 1)}%",
        "support_prices": support_prices,
        "resistance_prices": resistance_prices,
        "chip_peaks": peaks[:5],
        "total_volume_analyzed": round(total_chips, 0),
        "analyzed_days": n,
    }
    import json
    from app.agent.utils.md_format import _to_md
    return json.dumps(_r, ensure_ascii=False) if output == "json" else _to_md(_r)

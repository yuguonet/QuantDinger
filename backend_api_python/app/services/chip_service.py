# -*- coding: utf-8 -*-
"""筹码分布计算服务（基座层，全仓唯一实现）。

2026-09-23 合并: 本文件原与 app/agent/tools/finance/chip_distribution.py 各有一份
**同算法重复实现**（三角分布 + 时间衰减）。现收敛为:
  - 本文件 `compute_chip_core()` = **唯一数学内核**(纯函数, 返回未取整内部状态)
  - `calc_chip_for_chart()` = 前端口径的展示器(原键逐位不变, 加法式增返 *_levels)
  - agent/tools/finance/chip_distribution.py = agent 口径的展示器(改为调用同一内核)
⇒ 两处口径漂移的温床已消除; 日后改算法只改 `compute_chip_core` 一处。

依赖方向: services(基座) ← agent(上级)。本文件**不 import** app.agent / app.market_cn.auto。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.utils.logger import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════
# 唯一数学内核（纯函数，不做取整，不依赖调用方口径）
# ═══════════════════════════════════════════════════════════════

def compute_chip_core(
    klines: List[Dict[str, Any]],
    lookback_days: int = 120,
    num_buckets: int = 80,
) -> Dict[str, Any]:
    """筹码分布**唯一实现**：三角分布 + 时间衰减，返回未取整的内部状态。

    算法（与合并前逐字一致，不得在展示层复制）:
      1. 每条 K 线的成交量按三角分布（峰值在 close）分配至 low~high 区间
      2. 时间指数衰减加权（decay = 0.98 ** age，近期权重更高）
      3. 离散化价格桶（桶宽自适应，最小 0.01），累积筹码量
      4. 派生: 加权平均成本 / 获利比例 / 累计分布 / 筹码峰

    Args:
        klines: [{ time, open, high, low, close, volume }, ...]（升序，末根最新）
        lookback_days: 回看天数，>0 且超长时截取末 N 根
        num_buckets: 价格桶数量

    Returns:
        内部状态 dict（**未取整**，价格为 4 位小数，见下）:
          kline_count / current_price / price_min / bucket_width / buckets
          prices        — 桶价格序列（round 4；与合并前 chip_service 口径一致）
          density_raw   — 未归一化筹码密度（长度 = buckets）
          total_chips / max_density
          avg_cost_raw  — 加权平均成本（未取整）
          profit_ratio_raw — 获利比例 0~1（未取整）
          cum_ratio     — 累计占比序列（长度 = buckets）
          peaks         — 筹码峰 [{price(4位), strength(未取整)}]，**桶序（价格升序）**
        失败返回 {"error": "<原因>"}（原因文案与合并前逐字一致）。
    """
    if not klines:
        return {"error": "K线数据为空"}

    if lookback_days > 0 and len(klines) > lookback_days:
        klines = klines[-lookback_days:]

    closes = [float(k.get("close", 0)) for k in klines]
    highs = [float(k.get("high", 0)) for k in klines]
    lows = [float(k.get("low", 0)) for k in klines]
    volumes = [float(k.get("volume", 0)) for k in klines]

    if not closes:
        return {"error": "K线缺少价格数据"}

    current_price = closes[-1]

    price_min = min(lows)
    price_max = max(highs)
    if price_max <= price_min:
        return {"error": "价格区间异常"}

    # 自适应桶间距（最小 0.01）
    bucket_width = max((price_max - price_min) / num_buckets, 0.01)
    buckets = int((price_max - price_min) / bucket_width) + 1

    chip_density = [0.0] * buckets

    n = len(klines)
    for i in range(n):
        lo, hi, cl, vol = lows[i], highs[i], closes[i], volumes[i]
        if hi <= lo or vol <= 0:
            continue

        age = n - 1 - i          # 0 = 最新
        decay = 0.98 ** age

        # 三角分布：峰值在 close，两端在 low/high（左右半宽分别算，避免 close 偏侧失真）
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

    max_density = max(chip_density) if chip_density else 0.0
    if max_density <= 0:
        return {"error": "筹码计算无有效数据"}

    total_chips = sum(chip_density)
    prices = [round(price_min + i * bucket_width, 4) for i in range(buckets)]

    # 加权平均成本 / 获利比例（口径: 用 4 位小数的桶价格, 与合并前 chip_service 一致）
    avg_cost = sum(prices[i] * chip_density[i] for i in range(buckets)) / total_chips
    profit_ratio = sum(
        chip_density[i] for i in range(buckets) if prices[i] <= current_price
    ) / total_chips

    # 累计占比（供 70%/90% 集中区间使用）
    cum_ratio = []
    acc = 0.0
    for d in chip_density:
        acc += d
        cum_ratio.append(acc / total_chips)

    # 筹码峰（局部最大值，阈值 = 峰高 15%，捕获次级峰）
    # ⚠️ 保持**桶序（价格升序）原序返回**，不在此排序 —— 排序口径属展示层
    #    (chip_service 按价格取近端位; agent 口径按取整后强度降序), 内核只负责判定
    peak_threshold = max_density * 0.15
    peaks: List[Dict[str, float]] = []
    for i in range(1, buckets - 1):
        if (chip_density[i] > chip_density[i - 1] and
                chip_density[i] >= chip_density[i + 1] and
                chip_density[i] >= peak_threshold):
            peaks.append({"price": prices[i], "strength": chip_density[i] / total_chips})

    return {
        "kline_count": n,
        "current_price": current_price,
        "price_min": price_min,
        "bucket_width": bucket_width,
        "buckets": buckets,
        "prices": prices,
        "density_raw": chip_density,
        "total_chips": total_chips,
        "max_density": max_density,
        "avg_cost_raw": avg_cost,
        "profit_ratio_raw": profit_ratio,
        "cum_ratio": cum_ratio,
        "peaks": peaks,
    }


def split_levels(core: Dict[str, Any]) -> Dict[str, List[Dict[str, float]]]:
    """由筹码峰切出「最近支撑 / 最近压力」各至多 3 项（价近者在前），并附强度。

    口径: 与合并前 `support_prices / resistance_prices` **同集合同顺序**
    （支撑=现价下方按价格降序取 3；压力=现价上方按价格升序取 3），仅额外带 strength。
    不变式: [l["price"] for l in support_levels] == support_prices
    """
    c = core["current_price"]
    peaks = core["peaks"]
    support = sorted((p for p in peaks if p["price"] < c), key=lambda x: x["price"], reverse=True)[:3]
    resistance = sorted((p for p in peaks if p["price"] > c), key=lambda x: x["price"])[:3]
    return {
        "support_levels": [{"price": p["price"], "strength": round(p["strength"], 4)} for p in support],
        "resistance_levels": [{"price": p["price"], "strength": round(p["strength"], 4)} for p in resistance],
    }


# ═══════════════════════════════════════════════════════════════
# 展示器 1：前端图表口径（/chip_distribution 路由）
# ═══════════════════════════════════════════════════════════════

def calc_chip_for_chart(
    klines: List[Dict[str, Any]],
    lookback_days: int = 120,
    num_buckets: int = 80,
) -> Optional[Dict[str, Any]]:
    """计算筹码分布，返回前端绘图所需的 prices/density 数组。

    2026-09-23: 改为 `compute_chip_core` 的展示器。**原有键全部逐位不变**，
    另**加法式**增返 `support_levels` / `resistance_levels`（带 strength），
    供展示层(自选股标签)取关键位强度，避免再写第三份筹码实现。

    Returns:
        { prices, density, avg_cost, current_price,
          profit_ratio, support_prices, resistance_prices,
          support_levels, resistance_levels }
        失败返回 None
    """
    core = compute_chip_core(klines, lookback_days=lookback_days, num_buckets=num_buckets)
    if "error" in core:
        return None

    max_density = core["max_density"]
    prices = core["prices"]
    levels = split_levels(core)

    return {
        "prices": prices,
        "density": [round(d / max_density, 6) for d in core["density_raw"]],
        "avg_cost": round(core["avg_cost_raw"], 2),
        "current_price": round(core["current_price"], 2),
        "profit_ratio": round(core["profit_ratio_raw"], 4),
        "support_prices": [l["price"] for l in levels["support_levels"]],
        "resistance_prices": [l["price"] for l in levels["resistance_levels"]],
        "support_levels": levels["support_levels"],
        "resistance_levels": levels["resistance_levels"],
    }

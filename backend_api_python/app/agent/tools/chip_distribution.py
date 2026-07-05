# -*- coding: utf-8 -*-
"""
筹码分布模块。

计算 + tool 包装自包含，不依赖 analysis_tools。
算法：
  1. 每条 K 线的成交量按三角分布（峰值在 close）分配至 low~high 区间
  2. 时间指数衰减加权（近期权重更高）
  3. 离散化价格桶，累积筹码量
  4. 计算加权平均成本、90% 集中区间、获利/亏损比例
"""
from __future__ import annotations

from typing import Any, Dict, List

from app.agent.log import logger
from app.agent.tools._analysis_utils import _fetch_klines


# ═══════════════════════════════════════════════════════════════
# 筹码计算（纯函数，只做数学）
# ═══════════════════════════════════════════════════════════════

def _calc_chip_distribution(
    klines: List[Dict[str, Any]],
    stock_code: str = "",
    lookback_days: int = 120,
    num_buckets: int = 80,
) -> Dict[str, Any]:
    """计算筹码分布。

    Args:
        klines: K 线列表，每项含 time/open/high/low/close/volume
        stock_code: 股票代码
        lookback_days: 回看天数，截取最近 N 条 K 线
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
            "chip_peaks": [筹码峰(price, strength)],
            "total_volume_analyzed": 分析总成交量,
        }
    """
    if not klines:
        return {"error": "K线数据为空"}

    # lookback_days 生效：截取最近 N 条
    if lookback_days > 0 and len(klines) > lookback_days:
        klines = klines[-lookback_days:]

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
        # 左右半宽分别计算，避免 close 偏向一侧时权重失真
        left_half = cl - lo
        right_half = hi - cl
        if left_half < 0.001:
            left_half = 0.001
        if right_half < 0.001:
            right_half = 0.001

        steps = max(int((hi - lo) / bucket_width) + 1, 10)
        total_weight = 0.0
        weights = []
        for j in range(steps + 1):
            p = lo + (hi - lo) * j / steps
            if p <= cl:
                dist = (cl - p) / left_half
            else:
                dist = (p - cl) / right_half
            w = max(1.0 - dist, 0.0)
            weights.append((p, w))
            total_weight += w

        if total_weight <= 0:
            continue

        # 按桶累积（clamp 索引防越界）
        for p, w in weights:
            idx = int((p - price_min) / bucket_width)
            idx = max(0, min(idx, buckets - 1))
            chip_density[idx] += (vol * decay * w / total_weight)

    if max(chip_density) <= 0:
        return {"error": "筹码计算无有效数据"}

    # ── 计算总量和累积比例 ──
    total_chips = sum(chip_density)

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
        concentration_90 = "高"
    elif c90_width < 0.35:
        concentration_90 = "中"
    else:
        concentration_90 = "低"

    # ── 70% 集中区间（剔除两端各 15%） ──
    c70_lower_idx = 0
    c70_upper_idx = buckets - 1
    for i in range(buckets):
        if cum_ratio[i] >= 0.15:
            c70_lower_idx = i
            break
    for i in range(buckets - 1, -1, -1):
        if cum_ratio[i] <= 0.85:
            c70_upper_idx = i
            break

    c70_lower = prices[c70_lower_idx]
    c70_upper = prices[c70_upper_idx]
    c70_width = (c70_upper - c70_lower) / avg_cost if avg_cost > 0 else 0

    if c70_width < 0.08:
        concentration_70 = "高"
    elif c70_width < 0.20:
        concentration_70 = "中"
    else:
        concentration_70 = "低"

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
    peak_threshold = max(chip_density) * 0.15  # 降至 15%，捕获次级峰

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

    # ── 支撑/阻力位 ──
    sorted_peaks = sorted(peaks, key=lambda x: x["strength"], reverse=True)
    support_prices = []
    resistance_prices = []
    for p in sorted_peaks:
        if p["price"] < current_price:
            support_prices.append(p["price"])
        elif p["price"] > current_price:
            resistance_prices.append(p["price"])

    support_prices = sorted(support_prices, reverse=True)[:3]
    resistance_prices = sorted(resistance_prices)[:3]

    return {
        "stock_code": stock_code,
        "avg_cost": round(avg_cost, 2),
        "current_price": round(current_price, 2),
        "profit_ratio": round(profit_ratio, 4),
        "loss_ratio": round(loss_ratio, 4),
        "profit_ratio_pct": f"{round(profit_ratio * 100, 1)}%",
        "loss_ratio_pct": f"{round(loss_ratio * 100, 1)}%",
        "concentration_90": concentration_90,
        "concentration_90_lower": round(c90_lower, 2),
        "concentration_90_upper": round(c90_upper, 2),
        "concentration_90_width_pct": f"{round(c90_width * 100, 1)}%",
        "concentration_70": concentration_70,
        "concentration_70_lower": round(c70_lower, 2),
        "concentration_70_upper": round(c70_upper, 2),
        "concentration_70_width_pct": f"{round(c70_width * 100, 1)}%",
        "support_prices": support_prices,
        "resistance_prices": resistance_prices,
        "chip_peaks": peaks[:5],
        "total_volume_analyzed": round(total_chips, 0),
        "analyzed_days": n,
    }


# ═══════════════════════════════════════════════════════════════
# Tool 包装（Agent 调用入口）
# ═══════════════════════════════════════════════════════════════

def _format_chip_markdown(r: Dict[str, Any]) -> str:
    """将筹码分布结果格式化为中文 markdown。"""
    code = r.get("stock_code", "")
    avg_cost = r.get("avg_cost", 0)
    price = r.get("current_price", 0)
    profit_pct = r.get("profit_ratio_pct", "")
    loss_pct = r.get("loss_ratio_pct", "")
    conc90 = r.get("concentration_90", "")
    conc90_width = r.get("concentration_90_width_pct", "")
    conc90_lower = r.get("concentration_90_lower", 0)
    conc90_upper = r.get("concentration_90_upper", 0)
    conc70 = r.get("concentration_70", "")
    conc70_width = r.get("concentration_70_width_pct", "")
    conc70_lower = r.get("concentration_70_lower", 0)
    conc70_upper = r.get("concentration_70_upper", 0)
    supports = r.get("support_prices", [])
    resistances = r.get("resistance_prices", [])
    peaks = r.get("chip_peaks", [])
    days = r.get("analyzed_days", 0)

    lines = [f"### {code} 筹码分布"]
    lines.append(f"- **平均成本**: {avg_cost}  **现价**: {price}")
    lines.append(f"- **获利盘**: {profit_pct}  **套牢盘**: {loss_pct}")
    lines.append(f"- **70%筹码集中度**: {conc70}（{conc70_width}，区间 {conc70_lower}~{conc70_upper}）")
    lines.append(f"- **90%筹码集中度**: {conc90}（{conc90_width}，区间 {conc90_lower}~{conc90_upper}）")

    if supports:
        lines.append(f"- **支撑位**: {', '.join(str(s) for s in supports)}")
    if resistances:
        lines.append(f"- **压力位**: {', '.join(str(s) for s in resistances)}")
    if peaks:
        peak_str = ', '.join(f"{p['price']}({p['strength']:.1%})" for p in peaks)
        lines.append(f"- **筹码峰**: {peak_str}")

    lines.append(f"\n> 分析 {days} 根K线")
    return '\n'.join(lines)


def get_chip_distribution(codes: str, lookback_days: int = 120) -> Dict[str, Any]:
    """筹码分布：返回获利比例、平均成本、90%筹码集中度、套牢/获利盘比例。

    从日K线计算筹码分布，不依赖数据源原生接口。
    算法：按日K线的 high/low 区间分配成交量到价格档位，
    用指数衰减加权（近期筹码权重更高），汇总计算各维度指标。

    Args:
        codes: 多股用逗号分隔（也兼容 search_stock 返回的 dict）
        lookback_days: 回看天数，默认120天
    """
    # 兼容 search_stock 返回的 dict: {'results': [{'code': '600593', ...}], ...}
    if isinstance(codes, dict):
        results = codes.get("results", [])
        if results:
            codes = results[0].get("code", "")
        else:
            return {"error": "codes dict 中无 results", "retriable": False}

    code_list = [c.strip() for c in codes.split(",") if c.strip()][:20]
    if not code_list:
        return {"error": "codes 不能为空", "retriable": False}

    def _one(stock_code: str) -> Dict[str, Any]:
        stock_code = str(stock_code).strip()
        if not stock_code:
            return {"error": "stock_code 为空", "retriable": False}

        try:
            klines = _fetch_klines(stock_code, lookback_days)
            return _calc_chip_distribution(klines, stock_code=stock_code, lookback_days=lookback_days)
        except Exception as e:
            logger.error("get_chip_distribution(%s) failed: %s", stock_code, e, exc_info=True)
            return {"error": str(e)}

    if len(code_list) == 1:
        return _one(code_list[0])

    results = {}
    for code in code_list:
        try:
            results[code] = _one(code)
        except Exception as e:
            results[code] = {"error": str(e)}
    return {"count": len(results), "data": results}

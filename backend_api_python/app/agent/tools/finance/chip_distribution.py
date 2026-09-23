# -*- coding: utf-8 -*-
"""
筹码分布模块（agent 口径展示器）。

2026-09-23 合并: 本模块原与 app/services/chip_service.py 各有一份**同算法重复实现**。
现**唯一数学内核** = `app.services.chip_service.compute_chip_core`（三角分布 + 时间衰减），
本模块只做「取数 + agent 口径组装」（多出集中度/套牢盘/筹码峰等高层面），
改算法一律改内核，**禁止在本文件复制判定**。

算法（内核内实现）：
  1. 每条 K 线的成交量按三角分布（峰值在 close）分配至 low~high 区间
  2. 时间指数衰减加权（近期权重更高）
  3. 离散化价格桶，累积筹码量
  4. 计算加权平均成本、90%/70% 集中区间、获利/亏损比例、筹码峰

依赖方向: services(基座) ← agent(上级)。本文件可 import services，反向禁止。
"""
from __future__ import annotations

from typing import Any, Dict, List

from app.agent.log import logger
from app.agent.tools.finance._analysis_utils import _fetch_klines
from app.services.chip_service import compute_chip_core


# ═══════════════════════════════════════════════════════════════
# 筹码展示组装（agent 口径；数学全在 compute_chip_core）
# ═══════════════════════════════════════════════════════════════

def _calc_chip_distribution(
    klines: List[Dict[str, Any]],
    stock_code: str = "",
    lookback_days: int = 120,
    num_buckets: int = 80,
) -> Dict[str, Any]:
    """计算筹码分布（agent 口径）。

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
            "analyzed_days": 分析 K 线数,
        }
        失败返回 {"error": "<原因>"}
    """
    core = compute_chip_core(klines, lookback_days=lookback_days, num_buckets=num_buckets)
    if "error" in core:
        return {"error": core["error"]}

    buckets = core["buckets"]
    prices = core["prices"]
    cum_ratio = core["cum_ratio"]
    current_price = core["current_price"]
    avg_cost = core["avg_cost_raw"]
    profit_ratio = core["profit_ratio_raw"]

    # ── 90% 集中区间（剔除两端各 5%）──
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

    # ── 70% 集中区间（剔除两端各 15%）──
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

    loss_ratio = 1.0 - profit_ratio

    # ── 筹码峰 & 支撑/阻力位 ──
    # 口径与合并前一致: 峰价取整到 2 位展示; 峰按**取整后强度**降序 (稳定排序 ⇒ 同强度保留价格升序)
    peaks = [{"price": round(p["price"], 2), "strength": round(p["strength"], 4)}
             for p in core["peaks"]]
    peaks.sort(key=lambda x: x["strength"], reverse=True)

    support_prices = [p["price"] for p in peaks if p["price"] < current_price]
    resistance_prices = [p["price"] for p in peaks if p["price"] > current_price]
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
        "total_volume_analyzed": round(core["total_chips"], 0),
        "analyzed_days": core["kline_count"],
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

    Returns:
        dict: {avg_cost, current_price, profit_ratio, concentration_90, support_prices/resistance_prices/chip_peaks 均为 list}；多代码→{count, data:{代码:上述}}；error=失败。

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

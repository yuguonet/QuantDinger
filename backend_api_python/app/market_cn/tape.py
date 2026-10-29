"""
五档盘口 / 当日分笔 / 历史分笔 / 个股资金流向

功能:
  1. 五档实时行情 — 买一~买五 / 卖一~卖五 + 实时快照（最新价/涨跌/成交量等）
  2. 当日分笔成交 — 逐笔成交明细（仅交易时段可用，非交易时段返回空）
  3. 历史分笔成交 — 指定日期的逐笔成交明细（可用于复盘分析）
  4. 个股资金流向 — 当日分钟级 + 近120日日级

数据源（2026-10-28 Phase 2 收敛后）:
  五档 / 分笔: 一律经 app/data_sources/coord_tape
               （orderbook / ticks_today / ticks_history；mootdx·sina·tencent·eastmoney_dc
                四源由 dispatch 排序降级，本文件不再自建"源1→源2"链）
  资金流向:    分钟级 本地 realtime_snapshot 派生
               日级   本地 kline_1m 派生（+ qd_fund_flow_stock 缓存）

遗留直连（未收敛，勿增用）:
  `_get_liutongguben` 仍经 utils/mootdx_client 直连 TDX get_finance_info（万股口径），
  改走 coord_basic 需先解决"万股 vs 股"单位对拍，单列。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.utils.logger import get_logger

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════
#  mootdx 客户端管理 — 使用全局共享客户端
# ══════════════════════════════════════════════════════════════

from app.utils.mootdx_client import get_client as _get_client


# ══════════════════════════════════════════════════════════════
#  源表（插槽）— 已于 2026-10-28 全部清空
#  orderbook/ticks 于 Phase C（2026-10-26）改走 coord_tape；个股资金流于
#  2026-10-28 改走本地派生（snapshot / kline_1m），远端源已整族下线。
#  插槽语义现由 dispatch.py 统一保证（见 tests/test_tape_source_slot.py）
# ══════════════════════════════════════════════════════════════


def get_order_book(code: str) -> Dict[str, Any]:
    """获取五档盘口 + 实时行情快照（2026-10-26 Phase C: 改走 coord_tape 多源调度）

    Returns:
        成功: {code, name, price, open, high, low, last_close, change, change_percent,
              volume, amount, bid: {bid1~bid5}, ask: {ask1~ask5}}
        失败: {code, error: "获取失败"}
    """
    from app.data_sources.coord_tape import coordinate
    out = coordinate("orderbook", code=code)
    if out.get("ok") and out.get("data", {}).get("object"):
        return out["data"]["object"]
    logger.error("[coord_tape] orderbook 失败: %s (%s)", code, out.get("error", ""))
    return {"code": code, "error": "获取失败"}


def get_ticks_today(code: str, limit: int = 2000) -> List[Dict[str, Any]]:
    """获取当日分笔成交（Phase C: 改走 coord_tape）

    Returns:
        成功: [{time, price, vol, amount, buy_sell}, ...]
        失败: []
    """
    from app.data_sources.coord_tape import coordinate
    out = coordinate("ticks_today", code=code, limit=limit)
    if out.get("ok") and out.get("data", {}).get("rows"):
        return out["data"]["rows"]
    logger.error("[coord_tape] ticks_today 失败: %s (%s)", code, out.get("error", ""))
    return []


def get_ticks_history(code: str, date: str, limit: int = 2000) -> List[Dict[str, Any]]:
    """获取历史分笔成交（Phase C: 改走 coord_tape）

    Returns:
        成功: [{time, price, vol, amount, buy_sell}, ...]
        失败: []
    """
    from app.data_sources.coord_tape import coordinate
    out = coordinate("ticks_history", code=code, date=date, limit=limit)
    if out.get("ok") and out.get("data", {}).get("rows"):
        return out["data"]["rows"]
    logger.error("[coord_tape] ticks_history 失败: %s %s (%s)", code, date, out.get("error", ""))
    return []


# ══════════════════════════════════════════════════════════════
#  4. 个股资金流向
# ══════════════════════════════════════════════════════════════

# ── 4a. 基于分笔数据的资金流向计算（mootdx 主源）──

# 单笔成交金额阈值（元），用于划分订单类型
# 划分标准参考东方财富的分类方式
_SUPER_THRESHOLD = 2_000_000   # 超大单: 单笔 >= 200万元（通常是机构/游资大单）
_LARGE_THRESHOLD = 200_000     # 大单:   单笔 >= 20万元（中等规模资金）
_MID_THRESHOLD = 40_000        # 中单:   单笔 >= 4万元（散户大户级别）
# 小单: 单笔 < 4万元（普通散户）


def _classify_order(amount: float) -> str:
    """单笔成交金额 → 订单类型分类。

    分类标准:
      超大单(super) >= 200万:  通常是机构投资者、大型游资
      大单(large)   >= 20万:   中等规模资金、大户
      中单(mid)     >= 4万:    散户中的大户、小机构
      小单(small)   <  4万:    普通散户

    Args:
        amount: 单笔成交金额（元）

    Returns:
        "super" | "large" | "mid" | "small"
    """
    if amount >= _SUPER_THRESHOLD:
        return "super"
    if amount >= _LARGE_THRESHOLD:
        return "large"
    if amount >= _MID_THRESHOLD:
        return "mid"
    return "small"


def _calc_flow_from_ticks(ticks: List[Dict[str, Any]]) -> Dict[str, Any]:
    """从分笔数据计算资金流向统计。

    计算逻辑:
      1. 按 buy_sell 字段区分主动买入/卖出
      2. 按单笔金额分类为超大/大/中/小单
      3. 统计各类型的买入额、卖出额、净流入
      4. 主力净流入 = 超大单净流入 + 大单净流入

    净流入含义:
      正值 = 该类型资金净流入（买入 > 卖出）
      负值 = 该类型资金净流出（卖出 > 买入）

    Args:
        ticks: 分笔数据列表，每项需含 amount、buy_sell 字段

    Returns:
        {
            ticks_count: 分笔总条数,
            total_buy: 总主动买入额,
            total_sell: 总主动卖出额,
            total_net: 总净流入（= 买入 - 卖出）,
            main_net: 主力净流入（= 超大单 + 大单）,
            super_net: 超大单净流入,
            large_net: 大单净流入,
            mid_net: 中单净流入,
            small_net: 小单净流入,
            detail: {super/large/mid/small: {buy, sell, net}}
        }
    """
    # 初始化各类型的买入/卖出/净额累加器
    flow = {
        "super_net": 0.0, "super_buy": 0.0, "super_sell": 0.0,
        "large_net": 0.0,  "large_buy": 0.0,  "large_sell": 0.0,
        "mid_net": 0.0,    "mid_buy": 0.0,    "mid_sell": 0.0,
        "small_net": 0.0,  "small_buy": 0.0,  "small_sell": 0.0,
    }
    total_buy = 0.0
    total_sell = 0.0

    for t in ticks:
        amount = float(t.get("amount", 0))
        buy_sell = str(t.get("buy_sell", "2"))
        cat = _classify_order(amount)

        if buy_sell == "0":  # 主动买入（外盘）
            flow[f"{cat}_buy"] += amount
            flow[f"{cat}_net"] += amount
            total_buy += amount
        elif buy_sell == "1":  # 主动卖出（内盘）
            flow[f"{cat}_sell"] += amount
            flow[f"{cat}_net"] -= amount
            total_sell += amount
        # "2" 中性成交不计入买卖方向（如集合竞价撮合）

    # 主力净流入 = 超大单净流入 + 大单净流入
    main_net = flow["super_net"] + flow["large_net"]

    return {
        "ticks_count": len(ticks),
        "total_buy": round(total_buy, 2),
        "total_sell": round(total_sell, 2),
        "total_net": round(total_buy - total_sell, 2),
        "main_net": round(main_net, 2),
        "super_net": round(flow["super_net"], 2),
        "large_net": round(flow["large_net"], 2),
        "mid_net": round(flow["mid_net"], 2),
        "small_net": round(flow["small_net"], 2),
        "detail": {
            "super": {"buy": round(flow["super_buy"], 2), "sell": round(flow["super_sell"], 2), "net": round(flow["super_net"], 2)},
            "large": {"buy": round(flow["large_buy"], 2), "sell": round(flow["large_sell"], 2), "net": round(flow["large_net"], 2)},
            "mid":   {"buy": round(flow["mid_buy"], 2),   "sell": round(flow["mid_sell"], 2),   "net": round(flow["mid_net"], 2)},
            "small": {"buy": round(flow["small_buy"], 2), "sell": round(flow["small_sell"], 2), "net": round(flow["small_net"], 2)},
        },
    }


def get_fund_flow_from_ticks(code: str, limit: int = 2000) -> Dict[str, Any]:
    """通过 mootdx 分笔数据计算当日资金流向（主源）

    原理: 拉取当日所有分笔成交，按单笔金额分类统计买卖方向，
    得出各类型资金的净流入/流出情况。

    适用场景:
      - 实时监控盘中资金动向
      - 判断主力（超大单+大单）是在吸筹还是出货
      - 仅交易时段有数据

    Args:
        code: 股票代码，如 "600519"
        limit: 最大分笔条数，默认 2000

    Returns:
        成功: {code, source, ticks_count, total_buy, total_sell, total_net,
               main_net, super_net, large_net, mid_net, small_net, detail}
        失败: {code, source, error: "非交易时段或无数据"}

    Example:
        >>> flow = get_fund_flow_from_ticks("600519")
        >>> if flow.get("main_net", 0) > 0:
        ...     print(f"主力净流入 {flow['main_net']/10000:.0f} 万")
    """
    ticks = get_ticks_today(code, limit)
    if not ticks:
        return {"code": code, "source": "ticks", "error": "非交易时段或无数据"}

    result = _calc_flow_from_ticks(ticks)
    result["code"] = code
    result["source"] = "ticks"
    return result


def get_fund_flow_history_from_ticks(code: str, date: str, limit: int = 2000) -> Dict[str, Any]:
    """通过 mootdx 历史分笔计算指定日期的资金流向

    用于复盘分析：查看某天的主力资金动向。

    Args:
        code: 股票代码
        date: 日期 "YYYY-MM-DD" 或 "YYYYMMDD"
        limit: 最大分笔条数

    Returns:
        同 get_fund_flow_from_ticks，额外包含 date 字段

    Example:
        >>> flow = get_fund_flow_history_from_ticks("600519", "2025-01-15")
    """
    ticks = get_ticks_history(code, date, limit)
    if not ticks:
        return {"code": code, "date": date, "source": "ticks", "error": "无数据"}

    result = _calc_flow_from_ticks(ticks)
    result["code"] = code
    result["date"] = date
    result["source"] = "ticks"
    return result


# ── 4b. 东财资金流向 API（分钟级实时 + 日级历史）──

# 通用 User-Agent，模拟浏览器请求
# 2026-09-14：截断 UA（缺 "(KHTML, like Gecko)…" 段）会被东财服务端直接掐连接
# （hot_sectors 实测：截断 UA 必 RemoteDisconnected，完整 UA 稳定返回），统一换完整版。
def _safe_float(v) -> float:
    """安全的浮点数转换，异常时返回 0.0。

    东财 API 返回的数值可能是字符串、None 或空值，
    用此函数统一处理，避免 try/except 散落在各处。

    Args:
        v: 任意值

    Returns:
        四舍五入到4位小数的浮点数，或 0.0
    """
    try:
        return round(float(v), 4)
    except (TypeError, ValueError):
        return 0.0


# ══════════════════════════════════════════════════════════════
#  5. 实时换手率
# ══════════════════════════════════════════════════════════════

# 缓存流通股本，避免每次换手率计算都调 finance 接口
# 格式: {code: (liutongguben_万股, cache_timestamp)}
_finance_cache: Dict[str, tuple] = {}
_FINANCE_CACHE_TTL = 86400  # 流通股本数据缓存 24 小时（股本变动频率很低）


def _get_liutongguben(code: str) -> Optional[float]:
    """获取流通股本（万股），带 24h 缓存。

    流通股本来源于通达信 get_finance_info 接口。
    股本变动频率很低（增发/回购/解禁才会变），所以缓存 24 小时足够。

    Args:
        code: 股票代码

    Returns:
        流通股本（万股），失败返回 None
    """
    now = time.time()

    # 检查缓存
    if code in _finance_cache:
        val, ts = _finance_cache[code]
        if (now - ts) < _FINANCE_CACHE_TTL:
            return val

    # 从 mootdx finance 接口拉取
    try:
        cli = _get_client()
        if cli is None:
            return None

        mkt = 1 if code.startswith("6") else 0
        result = cli.client.get_finance_info(mkt, code)
        if not result:
            return None

        r = result[0] if isinstance(result, list) and result else result
        if isinstance(r, dict):
            val = float(r.get("liutongguben", 0))
            if val > 0:
                _finance_cache[code] = (val, now)
                return val
    except Exception as e:
        logger.warning("[tape] 获取流通股本失败(%s): %s", code, e)

    return None


def get_realtime_turnover(code: str) -> Dict[str, Any]:
    """获取实时换手率。

    换手率 = 当日成交量 / 流通股本 × 100%

    含义:
      - 反映股票当日的交易活跃度
      - 换手率 > 5%: 较为活跃
      - 换手率 > 10%: 高度活跃（可能是游资接力、新股、题材炒作）
      - 换手率 < 1%: 低迷（可能是大盘蓝筹或冷门股）

    计算方式:
      1. 从 mootdx quotes 获取当日成交量（单位: 股）
      2. 从 mootdx finance 获取流通股本（单位: 万股）
      3. 换手率(%) = 成交量(股) / (流通股本(万股) × 10000) × 100

    Args:
        code: 股票代码，如 "600519"

    Returns:
        成功: {
            code: "600519",
            name: "贵州茅台",
            volume: 12345678,          # 当日成交量（股）
            liutongguben: 125619.78,   # 流通股本（万股）
            turnover_rate: 0.98,       # 换手率（%）
            price: 1800.00,            # 最新价
            change_percent: 1.23,      # 涨跌幅(%)
        }
        失败: {code, error: "..."}

    Example:
        >>> t = get_realtime_turnover("600519")
        >>> print(f"换手率: {t['turnover_rate']:.2f}%")
    """
    # Step 1: 获取实时行情（含成交量）—— 经 coord_tape，多源降级由 data_sources 层调度
    quote = get_order_book(code)
    if quote.get("error"):
        return {"code": code, "error": quote["error"]}

    volume = quote.get("volume", 0)  # 成交量（股）
    if volume <= 0:
        return {
            "code": code,
            "name": quote.get("name", ""),
            "volume": 0,
            "turnover_rate": 0.0,
            "price": quote.get("price", 0),
            "change_percent": quote.get("change_percent", 0),
            "note": "非交易时段或无成交",
        }

    # Step 2: 获取流通股本
    liutong = _get_liutongguben(code)
    if not liutong or liutong <= 0:
        return {
            "code": code,
            "name": quote.get("name", ""),
            "volume": volume,
            "liutongguben": None,
            "turnover_rate": None,
            "price": quote.get("price", 0),
            "change_percent": quote.get("change_percent", 0),
            "error": "无法获取流通股本",
        }

    # Step 3: 计算换手率
    # volume 单位是股，liutong 单位是万股，统一为股
    liutong_shares = liutong * 10000  # 万股 → 股
    turnover_rate = round(volume / liutong_shares * 100, 4)

    return {
        "code": code,
        "name": quote.get("name", ""),
        "volume": volume,                  # 当日成交量（股）
        "liutongguben": liutong,           # 流通股本（万股）
        "turnover_rate": turnover_rate,    # 换手率（%）
        "price": quote.get("price", 0),
        "change_percent": quote.get("change_percent", 0),
    }


# ══════════════════════════════════════════════════════════════
#  6. 实时主力净流入（整合版）
# ══════════════════════════════════════════════════════════════

def get_realtime_main_flow(code: str) -> Dict[str, Any]:
    """获取实时主力净流入（整合东财 + mootdx 双源）。

    主力 = 超大单(>=200万) + 大单(>=20万)
    净流入 = 主动买入金额 - 主动卖出金额

    数据源优先级:
      1. 东财 push2 API — 分钟级精度，覆盖面广
      2. mootdx 分笔计算 — TCP 直连，精度高但仅交易时段可用

    Args:
        code: 股票代码，如 "600519"

    Returns:
        成功: {
            code: "600519",
            name: "贵州茅台",
            source: "eastmoney" | "mootdx",
            main_net: 123456789.00,       # 主力净流入（元）
            main_net_wan: 12345.68,       # 主力净流入（万元）
            main_net_yi: 1.23,            # 主力净流入（亿元）
            super_net: 80000000.00,       # 超大单净流入（元）
            large_net: 43456789.00,       # 大单净流入（元）
            mid_net: -20000000.00,        # 中单净流入（元）
            small_net: -50000000.00,      # 小单净流入（元）
            total_buy: 300000000.00,      # 总主动买入（元）
            total_sell: 250000000.00,     # 总主动卖出（元）
            price: 1800.00,               # 最新价
            change_percent: 1.23,         # 涨跌幅(%)
            points: 240,                  # 数据点数（东财模式）
        }
        失败: {code, error: "..."}

    Example:
        >>> flow = get_realtime_main_flow("600519")
        >>> yi = flow.get("main_net_yi", 0)
        >>> print(f"主力净流入: {yi:.2f} 亿")
    """
    result = None
    source = None

    # 数据源1: 东财分钟级资金流（覆盖面广，非交易时段也有历史数据）
    ef = get_fund_flow_realtime(code)
    if not ef.get("error") and ef.get("data"):
        # 汇总主力净流入
        # ⚠ 四档净额可能是 **None 而非 0**：本地快照派生源（fund_flow_local，
        #   量价方向法）只能算主力方向，四档不可知就显式给 None（宁缺勿假）。
        #   直接 sum 会 TypeError（None + int）—— 2026-10-28 接线时实测踩到，
        #   此处按"缺失即 0 计入合计、但不改写单点数值"处理。
        total_main = sum((r.get("main_net") or 0) for r in ef["data"])
        result = {
            "main_net": round(total_main, 2),
            "super_net": round(sum((r.get("super_net") or 0) for r in ef["data"]), 2),
            "large_net": round(sum((r.get("large_net") or 0) for r in ef["data"]), 2),
            "mid_net": round(sum((r.get("mid_net") or 0) for r in ef["data"]), 2),
            "small_net": round(sum((r.get("small_net") or 0) for r in ef["data"]), 2),
            "points": ef.get("points", 0),
        }
        source = ef.get("source") or "eastmoney"

    # 数据源2: 分笔派生（精度高，仅交易时段；分笔源由 coord_tape 多源调度）
    if result is None:
        mf = get_fund_flow_from_ticks(code)
        if not mf.get("error"):
            result = {
                "main_net": mf.get("main_net", 0),
                "super_net": mf.get("super_net", 0),
                "large_net": mf.get("large_net", 0),
                "mid_net": mf.get("mid_net", 0),
                "small_net": mf.get("small_net", 0),
                "total_buy": mf.get("total_buy", 0),
                "total_sell": mf.get("total_sell", 0),
                "ticks_count": mf.get("ticks_count", 0),
            }
            source = "ticks"

    if result is None:
        return {"code": code, "error": "所有数据源获取主力资金流均失败"}

    # 获取行情快照（最新价、涨跌幅）—— 经 coord_tape，多源由 data_sources 层调度
    quote = get_order_book(code)
    if quote.get("error"):
        quote = None

    main_net = result["main_net"]
    return {
        "code": code,
        "name": quote.get("name", "") if quote else "",
        "source": source,
        "main_net": main_net,                          # 元
        "main_net_wan": round(main_net / 10000, 2),    # 万元
        "main_net_yi": round(main_net / 1e8, 2),       # 亿元
        "super_net": result.get("super_net", 0),
        "large_net": result.get("large_net", 0),
        "mid_net": result.get("mid_net", 0),
        "small_net": result.get("small_net", 0),
        "total_buy": result.get("total_buy", 0),
        "total_sell": result.get("total_sell", 0),
        "points": result.get("points", result.get("ticks_count", 0)),
        "price": quote.get("price", 0) if quote else None,
        "change_percent": quote.get("change_percent", 0) if quote else None,
    }


# ══════════════════════════════════════════════════════════════
#  7. 实时综合快照（一次调用拿全部指标）
# ══════════════════════════════════════════════════════════════

def get_realtime_snapshot(code: str) -> Dict[str, Any]:
    """获取个股实时综合快照 — 一次调用返回行情+五档+换手率+主力资金。

    整合本模块所有实时数据，适合做盯盘面板或实时监控。

    Args:
        code: 股票代码，如 "600519"

    Returns:
        {
            code: "600519",
            name: "贵州茅台",
            price: 1800.00,
            open: 1790.00,
            high: 1810.00,
            low: 1785.00,
            last_close: 1790.00,
            change: 10.00,
            change_percent: 0.56,
            volume: 12345678,
            amount: 22222222222,
            turnover_rate: 0.98,         # 换手率(%)
            liutongguben: 125619.78,     # 流通股本(万股)
            main_net: 123456789.00,      # 主力净流入(元)
            main_net_wan: 12345.68,      # 主力净流入(万元)
            main_net_yi: 1.23,           # 主力净流入(亿元)
            super_net: 80000000.00,      # 超大单净(元)
            large_net: 43456789.00,      # 大单净(元)
            mid_net: -20000000.00,       # 中单净(元)
            small_net: -50000000.00,     # 小单净(元)
            flow_source: "eastmoney",    # 资金数据源
            bid: {bid1~bid5: {price, vol}},
            ask: {ask1~ask5: {price, vol}},
        }

    Example:
        >>> s = get_realtime_snapshot("600519")
        >>> print(f"{s['name']} {s['price']} 换手{s['turnover_rate']:.2f}% 主力{s['main_net_yi']:.2f}亿")
    """
    # 行情 + 五档 —— 经 coord_tape，多源由 data_sources 层调度
    quote = get_order_book(code)
    if quote.get("error"):
        return {"code": code, "error": "获取行情失败"}

    # 换手率
    volume = quote.get("volume", 0)
    liutong = _get_liutongguben(code)
    turnover_rate = None
    if liutong and liutong > 0 and volume > 0:
        turnover_rate = round(volume / (liutong * 10000) * 100, 4)

    # 主力资金
    flow = get_realtime_main_flow(code)

    return {
        "code": code,
        "name": quote.get("name", ""),
        # 行情
        "price": quote.get("price", 0),
        "open": quote.get("open", 0),
        "high": quote.get("high", 0),
        "low": quote.get("low", 0),
        "last_close": quote.get("last_close", 0),
        "change": quote.get("change", 0),
        "change_percent": quote.get("change_percent", 0),
        "volume": volume,
        "amount": quote.get("amount", 0),
        # 换手率
        "turnover_rate": turnover_rate,
        "liutongguben": liutong,
        # 主力资金
        "main_net": flow.get("main_net"),
        "main_net_wan": flow.get("main_net_wan"),
        "main_net_yi": flow.get("main_net_yi"),
        "super_net": flow.get("super_net"),
        "large_net": flow.get("large_net"),
        "mid_net": flow.get("mid_net"),
        "small_net": flow.get("small_net"),
        "flow_source": flow.get("source"),
        # 五档盘口
        "bid": quote.get("bid"),
        "ask": quote.get("ask"),
    }


def get_fund_flow_realtime(code: str) -> Dict[str, Any]:
    """获取当日分钟级资金流向 —— 本地 realtime_snapshot 派生（零外网）。

    返回结构：
        {code, points, total_main_net, total_amount, main_pct,
         data: [{time, main_net, small_net, mid_net, large_net, super_net}]}
    ⚠ 为**量价近似**（返回带 approx=True），非真实主力/散户分类：
       main_net 是「累计成交量增量 × 价格方向」，度量日内价格动能，不含"谁在买"；
       main_pct 是当日净额占成交额百分比（%，-100~+100），跨票可比，优先用它。
       口径见 market_cn/fund_flow_local.py。
    """
    try:
        from app.market_cn.fund_flow_local import get_fund_flow_from_snapshot
        local = get_fund_flow_from_snapshot(code)
        if local.get("data"):
            return local
        return {"code": code, "error": "empty snapshot", "source": "realtime_snapshot",
                "points": 0, "total_main_net": 0.0, "total_amount": 0.0,
                "main_pct": None, "data": [], "approx": True}
    except Exception as e:
        logger.warning("[fund_flow] 本地快照派生失败(%s): %s", code, e)
        return {"code": code, "error": str(e)[:160], "source": ""}


# ── 个股日级资金流（本地 kline_1m 派生 + 库，本段只做摘要计算）──

def get_fund_flow_daily(code: str, days: int = 120) -> Dict[str, Any]:
    """获取近 N 日资金方向净额序列与摘要。

    取数: `fund_flow_api.stock_fund_flow_history` —— 本地 kline_1m 量价方向派生
    （优先读 `qd_fund_flow_stock`，缺日现算并回填）。本函数只做摘要计算。

    口径（勿按"主力资金"读取）:
        net = Σ(上涨分钟成交额) - Σ(下跌分钟成交额) = 成交额 × 价格方向偏度。
        它度量当日**价格动能的方向与强度**，不含"谁在买"的信息，与东财主力净额
        （超大+大单净额，四档零和分解）不可换算：同日同票比值跨日 CV 2~25、
        近 5 日累计方向相反率 7/12，任何固定系数缩放都不成立。
        横向比较强度用 net_ratio（无量纲占比），不要用 main_net 的绝对值。

    Args:
        code: 股票代码，如 "600519"
        days: 获取天数，默认 120（约半年交易日）

    Returns:
        成功: {
            code: "600519",
            total_days: 120,
            recent_5d_main_net / recent_20d_main_net（元）,
            recent_5d_net_ratio / recent_20d_net_ratio（%，区间净额合计÷成交额合计）,
            main_net_streak / main_net_streak_dir,
            max_main_net_day / max_main_net / recent_20d_main_net_ex_max,
            recent_5d_sign,
            data: [{date, main_net, net_ratio, turnover, small_net, mid_net,
                    large_net, super_net}] (升序),
            source: "kline_1m",
        }
        失败: {code, error, source:""}

    net_ratio 单位 %，范围 -100~+100；turnover 缺失的日子为 None（不回落 0）。
    四档恒为 None（本地不具备拆单能力）。
    """
    from app.market_cn.fund_flow_api import stock_fund_flow_history

    try:
        flow = stock_fund_flow_history(code, days=days) or []
    except Exception as e:
        logger.warning("[fund_flow] 日级资金流失败(%s): %s", code, e)
        return {"code": code, "error": str(e)[:160], "source": ""}
    if not flow:
        logger.warning("[fund_flow] 日级资金流无数据(%s)", code)
        return {"code": code, "error": "本地 kline_1m 无数据", "source": ""}

    rows: List[Dict[str, Any]] = [{
        "date": str(r.get("trade_date") or r.get("date") or "")[:10],
        "main_net": r.get("main_net"),
        "net_ratio": r.get("main_pct"),
        "turnover": r.get("turnover"),
        "small_net": r.get("small_net"),
        "mid_net": r.get("mid_net"),
        "large_net": r.get("large_net"),
        "super_net": r.get("super_net"),
    } for r in flow if isinstance(r, dict)]
    rows = [r for r in rows if r["date"] and r["main_net"] is not None]
    if not rows:
        return {"code": code, "error": "本地 kline_1m 无数据", "source": ""}
    rows.sort(key=lambda x: x["date"])  # 升序 (旧->新)

    recent_5 = rows[-5:] if len(rows) >= 5 else rows
    recent_20 = rows[-20:] if len(rows) >= 20 else rows

    def _ratio(rs: List[Dict[str, Any]]) -> Optional[float]:
        """净额合计 / 成交额合计（%）。只在两者齐全的日子上算，缺数日不参与。"""
        pairs = [(r["main_net"], r["turnover"]) for r in rs
                 if r.get("main_net") is not None and r.get("turnover")]
        amt = sum(t for _, t in pairs)
        return round(sum(n for n, _ in pairs) / amt * 100.0, 2) if amt else None

    # ★ 2026-10-21 聚合摘要扩展（原来只有 5/20 日累计，模型仍要自己翻明细）：
    #   实测模型写「近5日 +4.18亿，主要来自 10-14 单日 +6.58亿；剔除该日后
    #   10-15→10-20 合计约 -2.40亿（算式：-1.01-0.33-1.06+0.003）」——就是把
    #   明细当账本逐行加。这三个量**正是它每次都要自己算的**，直接给：
    #     main_net_streak  —— 主力连续净流入/流出的天数与方向（"近3日净流出"直接读）
    #     max_main_net_day / 剔除该单日后的 20 日累计（"剔除某日后合计"直接读）
    #     recent_5d_sign    —— 近 5 日逐日符号序列（"是否连续流出"一眼可判）
    streak = 0
    last_sign = (1 if rows[-1]["main_net"] >= 0 else -1)
    for r in reversed(rows):
        sgn = (1 if r["main_net"] >= 0 else -1)
        if sgn != last_sign:
            break
        streak += 1
    main_vals = [r["main_net"] for r in rows]
    max_i = max(range(len(main_vals)), key=lambda idx: main_vals[idx]) if main_vals else -1
    # 剔除最大单日：按**索引**排除（不能用 `is not` / `!=` 比 dict —— 相同内容
    # 的不同对象会被误判为"没剔除"，相同 dict 也会被误剔除）
    excl20_idx = {idx for idx in range(len(rows)) if idx not in (max_i,)}
    recent_20_idx = range(max(0, len(rows) - 20), len(rows))
    excl20 = [rows[idx]["main_net"] for idx in recent_20_idx if idx in excl20_idx]

    return {
        "code": code,
        "total_days": len(rows),
        "recent_5d_main_net": round(sum(r["main_net"] for r in recent_5), 2),
        "recent_20d_main_net": round(sum(r["main_net"] for r in recent_20), 2),
        "recent_5d_net_ratio": _ratio(recent_5),
        "recent_20d_net_ratio": _ratio(recent_20),
        # ── 以下为 2026-10-21 新增摘要 ──
        "main_net_streak": streak,                      # 连续同向天数
        "main_net_streak_dir": ("inflow" if rows[-1]["main_net"] >= 0 else "outflow"),
        "max_main_net_day": (rows[max_i]["date"] if max_i >= 0 else None),
        "max_main_net": (round(main_vals[max_i], 2) if max_i >= 0 else None),
        "recent_20d_main_net_ex_max": round(sum(excl20), 2),  # 剔除最大单日后
        "recent_5d_sign": "".join("+" if r["main_net"] >= 0 else "-" for r in recent_5),
        "data": rows,
        "source": "kline_1m",
    }

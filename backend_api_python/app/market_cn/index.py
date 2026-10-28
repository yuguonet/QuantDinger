"""
市场指数接口

功能:
  1. 指数实时行情  — 11 大核心指数（上证/深证/创业板/沪深300/中证500 等）
  2. 指数日K线     — 任意指数代码，返回 OHLCV 标准格式
  3. 指数多周期K线 — 1m/5m/15m/30m/1H/1D/1W/1M
  4. 北向资金      — 实时分钟级 / 日级历史 / 持股明细
  5. 大盘资金流向  — 实时(分钟级) / 日线(历史)  主力/超大单/大单/中单/小单
  6. 行业资金流向  — 行业板块资金流入/流出排名

数据源（2026-10-28 Phase 3 收敛后）:
  实时行情: coord_index("index_quote") —— tencent 优先(多股单请求) / mootdx / akshare 兜底
  日K线:    本地 kline_index_5m 聚合 → coord_index("index_kline") 多源
            原 `_rt_mootdx/_rt_tencent/_rt_akshare` 自建三级降级链已删除
  北向/资金流: 仍是本文件自建链（coord 无对应 kind，见 §3 不动清单标注）

注意:
  - 选源 / 排序 / 超时 / 熔断 一律由 app/data_sources 层负责，本文件不写降级链
  - 腾讯接口无需安装额外依赖，但数据精度略低
  - AKShare / BaoStock 需要 pip install akshare / baostock
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

import pandas as pd

from app.utils.logger import get_logger
from app.utils.trading_calendar import is_trading_day_today

logger = logging.getLogger(__name__)

# 北向日级数据文件缓存（JSON，保留最近 500 个交易日）
_NB_DAILY_CACHE_FILE = os.path.join(os.getcwd(), "data", "market_cn_cache", "northbound_daily.json")
_nb_daily_lock = threading.Lock()

# ══════════════════════════════════════════════════════════════
#  常量 — 核心指数代码映射
# ══════════════════════════════════════════════════════════════

# A股市场核心指数: 代码 → 名称
# 涵盖宽基指数（上证/深证/创业板）+ 规模指数（300/500/1000）+ 风格指数（上证50/科创50/北证50）
INDEX_CODES: Dict[str, str] = {
    "000001": "上证指数",    # 上海证券交易所综合指数，A股最核心指标
    "399001": "深证成指",    # 深圳证券交易所成份指数
    "399006": "创业板指",    # 创业板成份指数，成长股风向标
    "000300": "沪深300",     # 沪深两市最大市值300只，机构基准
    "000905": "中证500",     # 排除沪深300后中等市值500只
    "000852": "中证1000",    # 小市值1000只，小盘股指标
    "000016": "上证50",      # 上海市值最大50只，大盘蓝筹
    "000688": "科创50",      # 科创板50只核心标的
    "899050": "北证50",      # 北交所50只核心标的
    "399303": "国证2000",    # 小盘股2000只
    "399005": "中小100",    # 中小板100只（原中小板指）
}

# ══════════════════════════════════════════════════════════════
#  日K线数据源（三级降级）
# ══════════════════════════════════════════════════════════════

# ── 本地指数日K聚合（2026-10-25 L2；2026-10-27 归位 cn_stock_ext）─────
# kline_index_5m（scheduler 盘后同步）5m→1D 聚合实现收口在
# app/data_sources/cn_stock_ext.aggregate_daily_df（含白名单/深度/新鲜度三重
# 门控，新鲜度锚点走 trading_calendar）。此处保留薄包装，维持既有 import 点。
# 原多源 fetcher 链（_kline_mootdx/_kline_tencent/_kline_sina/_kline_baostock，
# 含 _idx_prefix/_LOCAL_AGG_INDEX_SYMBOLS）已于 2026-10-27 删除：Phase C 起
# 远端统一走 coord_index 多源调度，这些函数自此无调用点（死代码）。


def _kline_local_agg(code: str, days: int) -> Optional[pd.DataFrame]:
    """数据源0: 本地 kline_index_5m 聚合日K（实现见 cn_stock_ext.aggregate_daily_df）。

    门控不过返回 None，由 get_index_daily_kline 回落 coord_index。

    Args:
        code: 6 位指数代码，如 "000001"
        days: 需要的日K条数

    Returns:
        成功: DataFrame [date, open, high, low, close, volume]（升序, volume=手）
        门控不过/异常: None
    """
    from app.data_sources.cn_stock_ext import aggregate_daily_df
    return aggregate_daily_df(code, days)


# ══════════════════════════════════════════════════════════════
#  对外接口（Public API）
# ══════════════════════════════════════════════════════════════

def get_index_realtime(codes: Optional[List[str]] = None, force: bool = False) -> List[Dict[str, Any]]:
    """获取指数实时行情（2026-10-28 Phase 3：改走 coord_index 多源调度）

    一次调用 `coord_index("index_quote", codes=...)`，选源/排序/批量/降级全在
    data_sources 层（tencent 真·多股单请求批量优先，mootdx/akshare 兜底）。
    原 `_rt_mootdx → _rt_tencent → _rt_akshare` 三级自建降级链已删除。

    Returns:
        list[dict]: [{code, name, price, open, high, low, last_close, change,
                     change_percent, volume(手), amount(元)}]；失败 []。
    """
    if not force and _rt_idx_realtime is not None:  # 内存缓存
        return _rt_idx_realtime
    if codes is None:                        # ② 原有逻辑
        codes = list(INDEX_CODES.keys())

    from app.data_sources.coord_index import coordinate
    out = coordinate("index_quote", codes=list(codes))
    entries = ((out.get("data") or {}).get("rows") or []) if out.get("ok") else []
    # 批量契约与 index_kline 一致：每码一条信封 {code, rows:[record], source, error}，
    # 此处展开成扁平行情列表（本函数对外契约不变）。
    rows: List[Dict[str, Any]] = []
    for e in entries:
        payload = e.get("rows")
        if isinstance(payload, list):
            rows.extend(payload)
        elif isinstance(payload, dict):
            rows.append(payload)
    if rows:
        logger.info("[coord_index] index_quote: %d 条 (source=%s)", len(rows), out.get("source", ""))
        return rows

    logger.error("[coord_index] 指数实时行情全部源失败: %s", out.get("error", ""))
    return []


def get_index_daily_kline(code: str = "000001", days: int = 200, force: bool = False) -> List[Dict[str, Any]]:
    """获取指数日K线（自动降级）

    依次尝试 本地聚合(kline_index_5m, cn_stock_ext) → coord_index 多源调度
    （mootdx → 腾讯 → 新浪 → BaoStock）。本地聚合门控（白名单∧深度≥days∧
    新鲜度≥trading_calendar 锚点）不过自动回落远端。

    Args:
        code: 指数代码，默认 "000001"（上证指数）
        days: 数据条数，默认 200（约一个交易年的日线数）
        force: 强制从数据源拉取，跳过缓存

    Returns:
        成功: [{date, open, high, low, close, volume, amount}, ...]
              date 格式 "YYYY-MM-DD"，按日期升序排列
        失败: []（空列表）

    Example:
        >>> get_index_daily_kline("000300", 60)  # 沪深300最近60个交易日
    """
    global _rt_max_idx_daily_days
    _rt_max_idx_daily_days = min(max(_rt_max_idx_daily_days, days), _RT_MAX_IDX_DAILY_DAYS)  # ① 峰值（有上限）
    cached = _rt_idx_daily.get(code)                 # ② 内存缓存
    if not force and cached and len(cached) >= days:
        return cached[:days]
    # ③ 本地聚合（kline_index_5m → 1D，零外网；实现收口 cn_stock_ext）
    df = _kline_local_agg(code, days)
    if df is not None:
        return df.to_dict("records")
    # ④ 远端 fallback（2026-10-26 Phase C: 改走 coord_index 多源调度；
    #    index_kline 统 kind + timeframe 参数，与 coordinator 周期语义对齐）
    from app.data_sources.coord_index import coordinate
    out = coordinate("index_kline", code=code, timeframe="1D", limit=days)
    if out.get("ok") and out.get("data", {}).get("rows"):
        rows = out["data"]["rows"]
        logger.info("[coord_index] 日K线 %s: %d 条 (source=%s)", code, len(rows), out.get("source", ""))
        return rows

    logger.error("所有数据源获取指数日K线均失败: %s", code)
    return []


def get_index_kline(code: str = "000001", frequency: str = "1D", days: int = 200) -> List[Dict[str, Any]]:
    """获取指数K线（支持多周期）

    2026-10-27 起非日线周期聚合收口 `app/data_sources/cn_stock_ext.get_index_kline`
    （5m 底仓聚合 5m/15m/30m/1H/2H/4H/1W/1M + trading_calendar 新鲜度闸门
    + coord_index 兜底；1m 无 5m 底仓，远端 mootdx 原生直拉）。
    1D 仍走 get_index_daily_kline 降级链（含内存缓存）。
    本函数保留原签名/返回口径作门面。

    Args:
        code: 指数代码，如 "000001"
        frequency: K线周期，可选值:
                   "1m"  — 1分钟
                   "5m"  — 5分钟
                   "15m" — 15分钟
                   "30m" — 30分钟
                   "1H"  — 1小时
                   "1D"  — 日线（默认，支持降级）
                   "1W"  — 周线
                   "1M"  — 月线
        days: 数据条数

    Returns:
        成功: [{date, open, high, low, close, volume}, ...]
              非日线的 date 格式为 "YYYY-MM-DD HH:MM:SS"
        失败: []（空列表）

    Example:
        >>> get_index_kline("000300", "30m", 100)  # 沪深300的30分钟线，100条
    """
    # 1D 保持原降级链（内存缓存 + aggregate_daily_df 口径 volume=手）
    if frequency == "1D":
        return get_index_daily_kline(code, days)
    from app.data_sources.cn_stock_ext import get_index_kline as _ext
    return _ext(code, timeframe=frequency, limit=days)


# ══════════════════════════════════════════════════════════════
#  北向资金（沪深港通）
# ══════════════════════════════════════════════════════════════

# 北向资金 = 境外投资者通过沪港通/深港通买入 A 股的资金
# 沪股通(hgt): 香港 → 上海
# 深股通(sgt): 香港 → 深圳
# 北向合计 = 沪股通净买入 + 深股通净买入（单位: 亿元）


def _northbound_summary_fallback() -> Dict[str, Any]:
    """hexin 实时不可用/缺腿时的兜底：东财 summary 当日北向净买入（单点，无分钟序列）。

    取数收口 `coord_index("northbound_summary")`（provider_index/akshare.py，
    同一 `stock_hsgt_fund_flow_summary_em` 接口）；本函数只做沪深两腿拆分与
    出口组装，不再自拉远端。

    返回与 get_northbound_realtime 同构（points=1，data=[1 点]，source=em_summary）；
    兜底也失败 ⇒ error dict（维持"宁缺勿假"）。单位口径与
    get_northbound_daily 的 akshare summary 分支一致。
    """
    from app.data_sources.coord_index import coordinate as _coord_index
    try:
        out = _coord_index("northbound_summary")
        rows = (out.get("data") or {}).get("rows") or []
        if not out.get("ok") or not rows:
            return {"error": "北向数据不完整，且东财 summary 无数据"}
        hgt_val = 0.0
        sgt_val = 0.0
        date_val = ""
        for row in rows:
            board = str(row.get("板块", ""))
            direction = str(row.get("资金方向", ""))
            if direction != "北向":
                continue
            date_val = str(row.get("交易日", ""))[:10]
            net = row.get("成交净买额", 0)
            try:
                if net is not None and str(net) not in ("", "nan", "NaN"):
                    if "沪" in board:
                        hgt_val = float(net)
                    elif "深" in board:
                        sgt_val = float(net)
            except (TypeError, ValueError):
                continue
        if not date_val:
            return {"error": "北向数据不完整，且东财 summary 缺交易日"}
        logger.info("[northbound_summary] 北向实时兜底(summary): hgt=%.2f sgt=%.2f (%s)",
                    hgt_val, sgt_val, date_val)
        pt = {"time": "15:00", "hgt_yi": round(hgt_val, 2), "sgt_yi": round(sgt_val, 2)}
        return {
            "points": 1,
            "hgt_latest_yi": round(hgt_val, 2),
            "sgt_latest_yi": round(sgt_val, 2),
            "total_latest_yi": round(hgt_val + sgt_val, 2),
            "data": [pt],
            "source": "em_summary",
        }
    except Exception as e:
        logger.warning("[northbound_summary] 北向实时兜底失败: %s", e)
        return {"error": f"北向数据不完整，无法展示（兜底失败: {e}）"}


def get_northbound_realtime(force: bool = False) -> Dict[str, Any]:
    """获取当日北向资金实时分钟级流向（同花顺 hexin API）。

    数据来源: 同花顺 data.hexin.cn
    接口: /market/hsgtApi/method/dayChart/
    更新频率: 盘中每分钟更新，约 262 个时间点（9:15-15:00）

    返回的每一行包含:
      - time: 时间点（如 "09:30", "14:58"）
      - hgt_yi: 沪股通累计净买入（亿元）
      - sgt_yi: 深股通累计净买入（亿元）

    注意: 数值为当日累计值（从开盘到该时间点的总净买入），
    不是单分钟净买入。最后一个非零值即为当日收盘时的总净买入。

    Returns:
        成功: {
            points: 262,              # 数据点数
            hgt_latest_yi: 52.3,      # 沪股通最新净买入（亿元）
            sgt_latest_yi: 38.7,      # 深股通最新净买入（亿元）
            total_latest_yi: 91.0,    # 北向合计净买入（亿元）
            data: [{time, hgt_yi, sgt_yi}, ...]  # 最近10条
        }
        失败: {error: "..."}

    Example:
        >>> nb = get_northbound_realtime()
        >>> print(f"北向合计: {nb['total_latest_yi']:.2f} 亿")
    """
    if not force and _rt_nb_realtime is not None:  # 内存缓存
        return _rt_nb_realtime

    from app.data_sources.coord_index import coordinate as coord_index
    out = coord_index("northbound_flow")
    if not out.get("ok"):
        logger.warning("[hexin] 北向实时失败: %s", out.get("error"))
        return _northbound_summary_fallback()

    raw_points = (out.get("data") or {}).get("rows") or []
    times = [p.get("time") for p in raw_points]
    hgt = [p.get("hgt_yi") for p in raw_points]
    sgt = [p.get("sgt_yi") for p in raw_points]

    # 组装数据点（三者长度可能不一致，按 time 长度填充）
    n = len(times)
    points = []
    for i in range(n):
        points.append({
            "time": times[i],
            "hgt_yi": hgt[i] if i < len(hgt) else None,
            "sgt_yi": sgt[i] if i < len(sgt) else None,
        })

    # 日期校验：今天不是交易日 → 数据非当天，不输出
    if not is_trading_day_today():
        logger.info("[hexin] 今天非交易日，北向实时数据不适用")
        return {"error": "今天非交易日，无实时北向数据"}

    # 数据完整性校验：hgt/sgt 任一严重缺失 → 数据不可信，不输出
    if len(times) > 0:
        hgt_ratio = len(hgt) / len(times)
        sgt_ratio = len(sgt) / len(times)
        if hgt_ratio < 0.5 or sgt_ratio < 0.5:
            logger.warning(
                "[hexin] 北向数据不完整: time=%d, hgt=%d(%.0f%%), sgt=%d(%.0f%%)，走东财兜底",
                len(times), len(hgt), hgt_ratio * 100, len(sgt), sgt_ratio * 100
            )
            return _northbound_summary_fallback()

    # hgt/sgt 长度可能不同，分别取各自最后一个非零值
    hgt_latest = next((h for h in reversed(hgt) if h is not None and h != 0), 0)
    sgt_latest = next((s for s in reversed(sgt) if s is not None and s != 0), 0)

    return {
        "points": len(points),
        "hgt_latest_yi": hgt_latest,
        "sgt_latest_yi": sgt_latest,
        "total_latest_yi": round((hgt_latest or 0) + (sgt_latest or 0), 2),
        "data": points[-10:],  # 返回最近 10 条
    }


def get_northbound_daily(days: int = 120, force: bool = False) -> List[Dict[str, Any]]:
    """获取北向资金日级净流入历史数据。

    数据源优先级:
      1. AKShare — 调用东方财富接口，数据最全（需 pip install akshare）
      2. 同花顺 hexin dayKline — 备用，约 30 天数据

    Args:
        days: 获取天数，默认 120
        force: 强制从数据源拉取（跳过缓存），默认 False

    Returns:
        成功: [{
            date: "2025-01-15",       # 交易日期
            hgt_yi: 52.3,             # 沪股通净买入（亿元）
            sgt_yi: 38.7,             # 深股通净买入（亿元）
            total_yi: 91.0,           # 北向合计（亿元）
        }, ...]
        失败: []

    Example:
        >>> data = get_northbound_daily(30)
        >>> for d in data[-5:]:
        ...     print(f"{d['date']}: 合计 {d['total_yi']:.2f} 亿")
    """
    global _rt_max_nb_daily_days, _rt_nb_daily
    _rt_max_nb_daily_days = min(max(_rt_max_nb_daily_days, days), _RT_MAX_NB_DAILY_DAYS)

    # 首次调用时从文件缓存加载
    if _rt_nb_daily is None:
        cached = _load_nb_daily_cache()
        if cached:
            _rt_nb_daily = cached
            logger.info("[cache] 加载北向日级文件缓存: %d 条", len(cached))

    # 缓存足够且非强制刷新 → 直接返回
    if not force and _rt_nb_daily and len(_rt_nb_daily) >= days:
        return _rt_nb_daily[-days:]

    # 远端取数：coord_index 多源（akshare hist_em 两腿合并 → hexin dayKline 备用），
    # 选源/降级/超时全在 data_sources 层，本函数只管缓存合并。
    from app.data_sources.coord_index import coordinate as coord_index
    out = coord_index("northbound_daily", days=days)
    if not out.get("ok"):
        logger.error("北向日级取数失败: %s", out.get("error"))
        return []
    remote = [r for r in ((out.get("data") or {}).get("rows") or []) if isinstance(r, dict)]
    if not remote:
        logger.error("北向日级取数为空（source=%s）", out.get("source"))
        return []
    logger.info("[%s] 北向日级: %d 条", out.get("source"), len(remote))
    return _merge_and_cache_nb_daily(remote, days)


def get_northbound_holdings(top: int = 50, force: bool = False) -> List[Dict[str, Any]]:
    """获取北向持股明细（同花顺 hexin API）。

    返回当前北向资金（沪股通+深股通）持有的 A 股个股明细，
    按持仓市值降序排列。

    Args:
        top: 返回前 N 只，默认 50

    Returns:
        [{
            code: "300750",           # 股票代码
            name: "宁德时代",         # 股票名称
            price: 200.00,            # 最新价
            hold_cost: 180.00,        # 持仓成本
            profit_pct: 11.11,        # 持仓盈亏(%)
            hold_shares: 451852170,   # 持股数(股)
            hold_market: 182096424510,# 持仓市值(元)
            hold_ratio: 10.61,        # 占流通股比(%)
        }, ...]

    Example:
        >>> top10 = get_northbound_holdings(10)
        >>> for h in top10:
        ...     print(f"{h['name']}: {h['hold_market']/1e8:.0f}亿 占比{h['hold_ratio']}%")
    """
    global _rt_max_nb_holdings_top
    _rt_max_nb_holdings_top = min(max(_rt_max_nb_holdings_top, top), _RT_MAX_NB_HOLDINGS_TOP)
    if not force and _rt_nb_holdings and len(_rt_nb_holdings) >= top:
        return _rt_nb_holdings[:top]

    from app.data_sources.coord_index import coordinate as coord_index
    out = coord_index("northbound_holdings", top=top)
    if not out.get("ok"):
        logger.warning("[hexin] 北向持股失败: %s", out.get("error"))
        return []
    rows = [r for r in ((out.get("data") or {}).get("rows") or []) if isinstance(r, dict)]
    logger.info("[hexin] 北向持股: %d 只", len(rows))
    return rows[:top]


# ══════════════════════════════════════════════════════════════
#  大盘资金流向 + 板块资金流向
# ══════════════════════════════════════════════════════════════
#
# 远端取数全部下沉 app/data_sources（coord_index），本文件只留内存缓存与出口结构：
#   market_flow_rt    : 新浪板块汇总(15) → 同花顺板块汇总(30) → 东财指数分钟(40)
#   market_flow_daily : 东财 push2his 日线（唯一源）
#   sector_flow       : 新浪(仅今日/行业,15) → 同花顺(仅今日/行业,30) → 东财(全周期/概念,40)
# 统一金额单位「元」；统一字段 main_net / super_net / large_net / mid_net / small_net。
# 本文件不再写降级链：选源/排序/降级/超时由 data_sources 层按 provider priority 决定。


def get_market_fund_flow_realtime(force: bool = False) -> Dict[str, Any]:
    """获取实时大盘资金流向（沪深两市主力资金净流入/流出）。

    Returns:
        dict: {source, timestamp, main_net, main_pct, in_net, out_net, ...}；
              全源失败 {source:"none", error}（维持"宁缺勿假"，不返回 0 值快照）。
    内存缓存: _rt_mf_realtime，由 scheduler 定期刷新。
    """
    if not force and _rt_mf_realtime is not None:  # 内存缓存（由 refresh 写入）
        return _rt_mf_realtime

    from app.data_sources.coord_index import coordinate as coord_index
    out = coord_index("market_flow_rt")
    rows = (out.get("data") or {}).get("rows") or []
    if out.get("ok") and rows:
        return rows[0]

    logger.error("所有数据源获取大盘实时资金流均失败: %s", out.get("error"))
    return {"source": "none", "error": "所有数据源均失败"}


def get_market_fund_flow_daily(days: int = 120, force: bool = False) -> List[Dict[str, Any]]:
    """获取大盘资金流向日线数据（沪深两市每日主力资金净流入/流出）。

    数据源: 东财 push2his 日线（新浪/腾讯/同花顺/百度均无免费大盘资金流日线）。

    Args:
        days: 获取天数，默认 120

    Returns:
        成功: [{date, main_net, main_pct, super_net, large_net, mid_net, small_net}, ...]
        失败: []

    Example:
        >>> data = get_market_fund_flow_daily(30)
        >>> for d in data[-5:]:
        ...     print(f"{d['date']}: 主力 {d['main_net']/1e8:.2f} 亿")
    """
    global _rt_max_mf_daily_days
    _rt_max_mf_daily_days = min(max(_rt_max_mf_daily_days, days), _RT_MAX_MF_DAILY_DAYS)  # ① 峰值（有上限）
    if not force and _rt_mf_daily and len(_rt_mf_daily) >= days:  # 内存缓存
        return _rt_mf_daily[:days]

    from app.data_sources.coord_index import coordinate as coord_index
    out = coord_index("market_flow_daily", days=days)
    rows = (out.get("data") or {}).get("rows") or []
    if out.get("ok") and rows:
        return rows

    logger.error("所有数据源获取大盘资金流日线均失败: %s", out.get("error"))
    return []


def get_sector_fund_flow(indicator: str = "今日", force: bool = False,
                         board_type: str = "industry") -> List[Dict[str, Any]]:
    """获取板块资金流向排名（行业 / 概念）。

    数据源（由 coord_index 按 provider priority 选）:
      新浪（仅当日行业）→ 同花顺（仅当日行业）→ 东财（行业/概念 × 今日/3日/5日/10日）

    Args:
        indicator: 统计周期，可选 "今日"(默认) / "3日" / "5日" / "10日"
        board_type: "industry"(默认) / "concept"
            （2026-09-14 补上——此前 fund_flow_tools.get_concept_fund_flow 传
            board_type="concept" 触发 TypeError，概念资金流工具自上线起即坏）

    Returns:
        成功: [{
            name: "电子信息",             # 板块名称
            code: "new_dzxx",            # 板块代码
            change_pct: -0.60,           # 涨跌幅(%)
            main_net: 16384862877.0,     # 主力净流入(元)
            main_pct: 5.49,              # 主力净占比(%)
            in_net: 147724801548.0,      # 总流入(元)
            out_net: 131339938671.0,     # 总流出(元)
            turnover: 594.43,            # 换手率
            lead_stock: "东土科技",       # 领涨股
            lead_pct: 20.02,             # 领涨股涨幅(%)
        }, ...]  按主力净流入降序
        失败: []

    Example:
        >>> sectors = get_sector_fund_flow("今日")
        >>> for s in sectors[:5]:
        ...     print(f"{s['name']}: 主力 {s['main_net']/1e8:.2f} 亿")
    """
    if not force and board_type == "industry" and _rt_sector_flow is not None:  # 内存缓存（仅行业）
        return _rt_sector_flow
    if indicator not in ("今日", "3日", "5日", "10日"):
        logger.warning("不支持的统计周期: %s，使用'今日'", indicator)
        indicator = "今日"

    from app.data_sources.coord_index import coordinate as coord_index
    out = coord_index("sector_flow", indicator=indicator, board_type=board_type)
    rows = (out.get("data") or {}).get("rows") or []
    if out.get("ok") and rows:
        return rows

    logger.error("板块资金流获取失败(%s/%s): %s", board_type, indicator, out.get("error"))
    return []


# ═══ 内存缓存 + refresh（scheduler 调用）═══

_rt_idx_realtime = None
_rt_idx_daily = {}            # {code: [kline_data]}
_rt_nb_realtime = None
_rt_nb_daily = None
_rt_nb_holdings = None
_rt_mf_realtime = None
_rt_mf_daily = None
_rt_sector_flow = None

# 峰值硬上限（防止刷新时无限增长）
_RT_MAX_IDX_DAILY_DAYS = 750       # 指数日K最多 3 年
_RT_MAX_NB_DAILY_DAYS = 250        # 北向日K最多 1 年
_RT_MAX_NB_HOLDINGS_TOP = 200      # 北向持仓最多 Top 200
_RT_MAX_MF_DAILY_DAYS = 750        # 主力资金日K最多 3 年

_rt_max_idx_daily_days = 0
_rt_max_nb_daily_days = 0
_rt_max_nb_holdings_top = 0
_rt_max_mf_daily_days = 0


def _load_nb_daily_cache() -> List[Dict[str, Any]]:
    """从 JSON 文件加载北向日级缓存数据 (最多 250 天，按日期升序)。"""
    with _nb_daily_lock:
        if not os.path.exists(_NB_DAILY_CACHE_FILE):
            return []
        try:
            with open(_NB_DAILY_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
        except Exception as e:
            logger.warning("读取北向日级缓存文件失败: %s", e)
    return []


def _save_nb_daily_cache(data: List[Dict[str, Any]]) -> None:
    """合并数据并写入北向日级缓存文件（最多 250 条，按日期去重排序）。"""
    with _nb_daily_lock:
        seen: Dict[str, Dict[str, Any]] = {}
        for item in data:
            date_key = item.get("date", "")
            if date_key:
                seen[date_key] = item
        merged = sorted(seen.values(), key=lambda x: x.get("date", ""), reverse=True)
        merged = merged[:250]
        merged.sort(key=lambda x: x.get("date", ""))  # 升序
        try:
            os.makedirs(os.path.dirname(_NB_DAILY_CACHE_FILE), exist_ok=True)
            with open(_NB_DAILY_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(merged, f, ensure_ascii=False)
            logger.info("[cache] 北向日级缓存已保存: %d 条", len(merged))
        except Exception as e:
            logger.warning("保存北向日级缓存文件失败: %s", e)


def _merge_and_cache_nb_daily(new_data: List[Dict[str, Any]], days: int) -> List[Dict[str, Any]]:
    """合并新获取的数据到缓存，写入文件，更新 _rt_nb_daily，返回截取后的结果。"""
    global _rt_nb_daily
    combined = (_rt_nb_daily or []) + new_data
    seen: Dict[str, Dict[str, Any]] = {}
    for item in combined:
        date_key = item.get("date", "")
        if date_key:
            seen[date_key] = item
    merged = sorted(seen.values(), key=lambda x: x.get("date", ""))
    merged = merged[-250:]  # 保留最近 250 天
    _rt_nb_daily = merged
    _save_nb_daily_cache(merged)
    return merged[-days:]


def refresh_index_realtime():
    global _rt_idx_realtime
    try:
        _rt_idx_realtime = get_index_realtime(force=True)
    except Exception as e:
        logger.warning("[refresh] refresh_index_realtime 失败: %s", e)

def refresh_index_daily_kline():
    global _rt_idx_daily, _rt_max_idx_daily_days
    try:
        fetch_days = max(500, _rt_max_idx_daily_days)
        for code in INDEX_CODES:
            data = get_index_daily_kline(code, fetch_days, force=True)
            if data:
                _rt_idx_daily[code] = data
    except Exception as e:
        logger.warning("[refresh] index_daily_kline 失败: %s", e)

def refresh_northbound_realtime():
    global _rt_nb_realtime
    try:
        _rt_nb_realtime = get_northbound_realtime(force=True)
    except Exception as e:
        logger.warning("[refresh] refresh_northbound_realtime 失败: %s", e)

def refresh_northbound_daily():
    global _rt_nb_daily, _rt_max_nb_daily_days
    try:
        fetch_days = max(250, _rt_max_nb_daily_days)
        _rt_nb_daily = get_northbound_daily(fetch_days, force=True)
    except Exception as e:
        logger.warning("[refresh] northbound_daily 失败: %s", e)

def refresh_northbound_holdings():
    global _rt_nb_holdings, _rt_max_nb_holdings_top
    try:
        fetch_top = max(100, _rt_max_nb_holdings_top)
        _rt_nb_holdings = get_northbound_holdings(fetch_top, force=True)
    except Exception as e:
        logger.warning("[refresh] northbound_holdings 失败: %s", e)

def refresh_market_fund_flow_realtime():
    global _rt_mf_realtime
    try:
        _rt_mf_realtime = get_market_fund_flow_realtime(force=True)
    except Exception as e:
        logger.warning("[refresh] refresh_market_fund_flow_realtime 失败: %s", e)

def refresh_market_fund_flow_daily():
    global _rt_mf_daily, _rt_max_mf_daily_days
    try:
        fetch_days = max(500, _rt_max_mf_daily_days)
        _rt_mf_daily = get_market_fund_flow_daily(fetch_days, force=True)
    except Exception as e:
        logger.warning("[refresh] market_fund_flow_daily 失败: %s", e)

def refresh_sector_fund_flow():
    global _rt_sector_flow
    try:
        _rt_sector_flow = get_sector_fund_flow(force=True)
    except Exception as e:
        logger.warning("[refresh] refresh_sector_fund_flow 失败: %s", e)


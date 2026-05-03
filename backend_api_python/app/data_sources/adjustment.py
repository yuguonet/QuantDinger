# -*- coding: utf-8 -*-
"""
复权因子计算模块 — 上层统一复权

模块职责:
    统一处理股票K线数据的前复权/后复权计算。所有 Provider 返回原始(未复权)数据，
    复权在此模块统一处理，避免各 Provider 重复实现复权逻辑。

设计原理:
    - 关注点分离: Provider 只负责获取原始数据，复权逻辑集中在此模块
    - 缓存优化: 除权除息数据缓存 24h（一年才变几次），避免重复请求东财 API
    - 算法正确性: 严格按照交易所复权公式计算，确保价格连续性

在架构中的位置:
    数据源层 — 位于 Provider 之上、调用方之下，作为数据后处理的统一入口

关键依赖:
    - app.data_sources.normalizer: 股票代码标准化
    - app.data_sources.rate_limiter: 东财 API 限流
    - app.utils.logger: 日志记录

复权公式推导:
    除权除息后，股票价格需要调整以保持价格连续性。
    设除权前一日收盘价为 prev_close，除权方案为：
    - 每股派现 bonus_cash 元
    - 每10股送 bonus_shares 股（即每股送 bonus_shares/10 股，这里已转为每股）
    - 每10股配 rights_shares 股，配股价 rights_price 元

    除权参考价 = (prev_close - bonus_cash + rights_price × rights_shares) / (1 + bonus_shares + rights_shares)
    复权因子  = ref_price / prev_close

    说明:
    - 分子: 原始市值 - 派现金额 + 配股缴款金额 = 调整后总市值
    - 分母: 原有1股 + 送股 + 配股 = 调整后总股数
    - 复权因子 < 1 表示除权后价格下降（送股/派现导致）
    - 复权因子 > 1 表示除权后价格上升（罕见，如低价配股）

用法:
    from app.data_sources.adjustment import adjust_kline
    raw_bars = provider.fetch_kline(code, tf, limit, adj="")  # 原始
    adjusted = adjust_kline(code, raw_bars, adj="qfq")         # 前复权
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests

from app.data_sources.normalizer import to_raw_digits
from app.data_sources.rate_limiter import (
    get_request_headers, get_eastmoney_limiter,
)
from app.utils.logger import get_logger

logger = get_logger(__name__)


# ================================================================
# 除权除息数据 — 从东财获取，缓存 24h
# ================================================================

# 全局缓存: {raw_code: (events_list, cache_timestamp)}
# 线程安全由 _exdiv_cache_lock 保护
_exdiv_cache: Dict[str, Tuple[List[Dict], float]] = {}
_exdiv_cache_lock = threading.Lock()

# 缓存TTL: 86400秒 = 24小时
# 除权除息数据一年才发布几次，24h缓存完全够用，大幅减少API调用
_EXDIV_CACHE_TTL = 86400


def fetch_exdividend_events(code: str) -> List[Dict[str, Any]]:
    """
    获取股票的除权除息记录（东财 API）。

    东财 datacenter-web API 返回 RPT_SHAREBONUS_DET 报表，包含历年的
    送股、转增、配股、派息方案。结果按除权日升序排列后缓存。

    Args:
        code: 股票代码（任意格式，内部转换为纯数字）

    Returns:
        除权除息事件列表，每个事件包含:
        - date: 除权日 (YYYY-MM-DD)
        - bonus_cash: 每股派现（元）
        - bonus_shares: 每股送股数（已从"每10股送X股"转换）
        - rights_shares: 每股配股数
        - rights_price: 配股价（元）

    缓存策略:
        结果缓存 24 小时。除权除息方案一年才变化几次（年报/半年报后），
        24h 缓存既能保证数据及时性，又能大幅减少 API 调用。
    """
    now = time.time()
    raw_code = to_raw_digits(code)
    if not raw_code or not raw_code.isdigit() or len(raw_code) != 6:
        return []

    # 先查缓存（加锁读取）
    with _exdiv_cache_lock:
        if raw_code in _exdiv_cache:
            events, cached_ts = _exdiv_cache[raw_code]
            if now - cached_ts < _EXDIV_CACHE_TTL:
                return events

    # 缓存未命中，从东财 API 获取
    # 东财 datacenter-web API 文档: RPT_SHAREBONUS_DET 报表
    # filter 参数使用 SECURITY_CODE 精确匹配
    try:
        get_eastmoney_limiter().wait()  # 限流：避免触发东财反爬
        resp = requests.get(
            "https://datacenter-web.eastmoney.com/api/data/v1/get",
            headers=get_request_headers(referer="https://data.eastmoney.com/"),
            params={
                "sortColumns": "EX_DIVIDEND_DATE",
                "sortTypes": "-1",           # 降序排列（最新在前）
                "pageSize": 200,             # 单页最多200条，足够覆盖所有历史
                "pageNumber": 1,
                "reportName": "RPT_SHAREBONUS_DET",  # 除权除息明细报表
                "columns": "ALL",
                "source": "WEB",
                "client": "WEB",
                "filter": f'(SECURITY_CODE="{raw_code}")',
            },
            timeout=10,
        )
        data = resp.json()
        items = ((data.get("result") or {}).get("data")) or []
    except Exception as e:
        logger.warning("[复权] 获取除权除息数据失败 %s: %s", code, e)
        return []

    # 解析 API 返回的原始数据，提取复权所需字段
    events = []
    for item in items:
        raw_date = item.get("EX_DIVIDEND_DATE")
        if not raw_date:
            continue
        ex_date = str(raw_date).strip()[:10]
        if not ex_date or ex_date == "None":
            continue

        # 每股派现 — 尝试多个字段名（东财API字段名可能随版本变化）
        bonus_cash = 0.0
        for key in ("PRETAX_BONUS_RMB", "BONUS_CASH", "DIVIDEND_AMOUNT"):
            v = item.get(key)
            if v is not None and v != "-" and v != "":
                try:
                    bonus_cash = float(v)
                    break
                except (TypeError, ValueError):
                    continue

        # 每股送股数 — 东财原始数据为"每10股送X股"，已转为每股
        bonus_shares = 0.0
        for key in ("BONUS_SHARES_RATIO", "BONUS_RATIO"):
            v = item.get(key)
            if v is not None and v != "-" and v != "":
                try:
                    bonus_shares = float(v)
                    break
                except (TypeError, ValueError):
                    continue

        # 每股配股数
        rights_shares = 0.0
        v = item.get("RIGHTS_SHARES_RATIO")
        if v is not None and v != "-" and v != "":
            try:
                rights_shares = float(v)
            except (TypeError, ValueError):
                pass

        # 配股价
        rights_price = 0.0
        v = item.get("RIGHTS_SHARES_PRICE")
        if v is not None and v != "-" and v != "":
            try:
                rights_price = float(v)
            except (TypeError, ValueError):
                pass

        # 只记录有实际复权动作的事件
        if bonus_cash > 0 or bonus_shares > 0 or rights_shares > 0:
            events.append({
                "date": ex_date,
                "bonus_cash": bonus_cash,
                "bonus_shares": bonus_shares,
                "rights_shares": rights_shares,
                "rights_price": rights_price,
            })

    # 按除权日升序排列，便于后续顺序遍历
    events.sort(key=lambda x: x["date"])

    # 写入缓存
    with _exdiv_cache_lock:
        _exdiv_cache[raw_code] = (events, time.time())

    if events:
        logger.debug("[复权] %s 获取 %d 条除权除息记录", code, len(events))
    return events


# ================================================================
# 复权因子计算
# ================================================================

def calc_adjustment_factors(
    kline: List[Dict[str, Any]],
    events: List[Dict[str, Any]],
    adj: str = "qfq",
) -> Dict[int, float]:
    """
    计算复权因子，返回 {timestamp: factor} 映射表。

    复权因子的含义:
        调整后价格 = 原始价格 × 复权因子

    前复权 (qfq - 前复权):
        以最新价格为基准，将历史价格向下调整。
        效果：最新K线价格 = 真实价格，历史价格 = 复权后价格
        适用：看盘软件的历史K线展示

    后复权 (hfq - 后复权):
        以最早价格为基准，将新价格向上调整。
        效果：最早K线价格 = 真实价格，新价格 = 复权后价格
        适用：计算长期收益率

    Args:
        kline: K线数据列表，每条包含 time(时间戳) 和 close(收盘价)
        events: 除权除息事件列表（需按日期升序排列）
        adj: 复权类型，"qfq" 前复权 / "hfq" 后复权

    Returns:
        {timestamp: factor} 映射表，每个时间戳对应一个复权因子
    """
    if not events or not kline:
        return {}

    # 步骤1: 构建日期→收盘价映射，用于查找除权前一日收盘价
    date_to_close: Dict[str, float] = {}
    for bar in kline:
        ts = bar.get("time", 0)
        if ts and bar.get("close", 0) > 0:
            dt = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            date_to_close[dt] = bar["close"]

    def _get_prev_close(ex_date: str) -> Optional[float]:
        """
        获取除权日前一个交易日的收盘价。

        由于K线数据可能不包含周末/节假日，需要向前搜索最多15天。
        为什么是15天？最长的连续假期（如春节）通常不超过10天，15天留有余量。
        """
        if not ex_date or ex_date == "None":
            return None
        try:
            dt = datetime.strptime(ex_date, "%Y-%m-%d")
        except (ValueError, TypeError):
            return None
        for i in range(1, 15):
            prev_str = (dt - timedelta(days=i)).strftime("%Y-%m-%d")
            c = date_to_close.get(prev_str)
            if c and c > 0:
                return c
        return None

    # 步骤2: 计算每个除权事件的复权因子
    # 复权公式:
    #   ref_price = (prev_close - bonus_cash + rights_price × rights_shares) / (1 + bonus_shares + rights_shares)
    #   factor = ref_price / prev_close
    #
    # 公式推导:
    #   除权前总市值 = prev_close × 1股
    #   除权后总市值 = (prev_close - bonus_cash) × 1股 + rights_price × rights_shares 股
    #                = prev_close - bonus_cash + rights_price × rights_shares
    #   除权后总股数 = 1 + bonus_shares + rights_shares
    #   除权参考价   = 除权后总市值 / 除权后总股数
    event_pairs: List[Tuple[str, float]] = []
    for ev in events:
        if not ev.get("date"):
            continue
        prev_close = _get_prev_close(ev["date"])
        if prev_close is None or prev_close <= 0:
            continue

        bonus_cash = ev["bonus_cash"]
        bonus_shares = ev["bonus_shares"]
        rights_shares = ev["rights_shares"]
        rights_price = ev["rights_price"]

        # 分母: 原有1股 + 送股 + 配股 = 调整后总股数
        denom = 1.0 + bonus_shares + rights_shares
        if denom <= 0:
            continue
        # 除权参考价 = (原始市值 - 派现 + 配股缴款) / 调整后总股数
        ref_price = (prev_close - bonus_cash + rights_price * rights_shares) / denom
        # 复权因子 = 除权参考价 / 原始收盘价
        factor = ref_price / prev_close

        # 过滤异常因子（正常范围 0.01 ~ 100）
        if 0.01 < factor < 100:
            event_pairs.append((ev["date"], factor))

    if not event_pairs:
        return {}

    event_pairs.sort(key=lambda x: x[0])
    result: Dict[int, float] = {}

    if adj == "qfq":
        # ================================================================
        # 前复权算法 (qfq):
        # 从最新到最旧遍历，累积乘法计算。
        # 目标：最新价格不变，历史价格逐级下调。
        #
        # 逻辑：
        # 1. 从最后一个除权事件开始，向前累积乘法
        # 2. 每经过一个除权日，累积因子乘以该事件的 factor
        # 3. 遍历K线时，根据K线所在日期选择对应的累积因子
        # ================================================================
        cum = 1.0
        factor_map: Dict[str, float] = {}
        for ex_date, factor in reversed(event_pairs):
            cum *= factor
            factor_map[ex_date] = cum

        # 从最新K线向前遍历，按除权日切换累积因子
        active = 1.0
        rev_idx = len(event_pairs) - 1
        for bar in reversed(kline):
            ts = bar.get("time", 0)
            if not ts:
                continue
            dt = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            # 如果当前K线日期早于当前除权日，切换到更早的累积因子
            while rev_idx >= 0:
                ex_date, _ = event_pairs[rev_idx]
                if dt < ex_date:
                    active = factor_map[ex_date]
                    rev_idx -= 1
                else:
                    break
            result[ts] = active
    else:
        # ================================================================
        # 后复权算法 (hfq):
        # 从最旧到最新遍历，累积倒数乘法计算。
        # 目标：最早价格不变，新价格逐级上调。
        #
        # 逻辑：
        # 1. 从第一个除权事件开始，向后累积 1/factor 乘法
        # 2. 每经过一个除权日，累积因子乘以 1/factor
        # 3. 遍历K线时，根据K线所在日期选择对应的累积因子
        # ================================================================
        cum = 1.0
        factor_map: Dict[str, float] = {}
        for ex_date, factor in event_pairs:
            cum *= (1.0 / factor)
            factor_map[ex_date] = cum

        # 从最早K线向后遍历，按除权日切换累积因子
        active_factor = 1.0
        event_idx = 0
        for bar in kline:
            ts = bar.get("time", 0)
            if not ts:
                continue
            dt = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            # 如果当前K线日期达到或超过下一个除权日，切换到新的累积因子
            while event_idx < len(event_pairs):
                ex_date, _ = event_pairs[event_idx]
                if dt >= ex_date:
                    active_factor = factor_map[ex_date]
                    event_idx += 1
                else:
                    break
            result[ts] = active_factor

    return result


# ================================================================
# 上层统一入口
# ================================================================

def adjust_kline(
    code: str,
    raw_bars: List[Dict[str, Any]],
    adj: str = "qfq",
) -> List[Dict[str, Any]]:
    """
    对原始K线应用复权 — 统一入口函数。

    流程:
        1. 获取除权除息事件（带24h缓存）
        2. 计算复权因子映射表
        3. 对每条K线应用复权因子

    Args:
        code:      股票代码（任意格式）
        raw_bars:  Provider 返回的原始(未复权)K线
        adj:       复权类型 — "qfq" 前复权 / "hfq" 后复权 / "" 不复权

    Returns:
        复权后的K线列表。OHLC 价格按因子调整，成交量不变。
        如果无除权除息记录或复权失败，返回原始数据。

    Examples:
        >>> raw = [{"time": 1700000000, "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 1000}]
        >>> adjust_kline("600519", raw, adj="qfq")
    """
    if not adj or raw_bars is None:
        return raw_bars

    # 缓存层可能返回 DataFrame，统一转为 list[dict]
    if hasattr(raw_bars, 'to_dict'):
        raw_bars = raw_bars.to_dict('records')

    if not raw_bars:
        return raw_bars

    events = fetch_exdividend_events(code)
    if not events:
        return raw_bars

    factors = calc_adjustment_factors(raw_bars, events, adj)
    if not factors:
        return raw_bars

    adjusted = []
    for bar in raw_bars:
        ts = bar.get("time", 0)
        factor = factors.get(ts, 1.0)
        if factor == 1.0:
            # 无需调整，直接复用原始对象（节省内存）
            adjusted.append(bar)
        else:
            # OHLC 价格按因子调整，成交量不变
            adjusted.append({
                "time": ts,
                "open": round(bar["open"] * factor, 4),
                "high": round(bar["high"] * factor, 4),
                "low": round(bar["low"] * factor, 4),
                "close": round(bar["close"] * factor, 4),
                "volume": bar["volume"],
            })

    return adjusted

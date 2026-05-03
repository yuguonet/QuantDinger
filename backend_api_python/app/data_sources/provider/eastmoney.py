# -*- coding: utf-8 -*-
"""
东方财富数据源 Provider

模块职责:
  通过东方财富 API 获取 A股的 K线、实时行情以及市场数据（龙虎榜/热度/涨停池/跌停池/炸板池）。
  东财是国内最稳定的免费数据源之一，作为A股第三选择（priority=30）。

能力:
  - K线: 全周期（1m/5m/15m/30m/1H/1D/1W），通过 kline/get API
  - 单只行情: 实时行情快照（stock/get API）
  - 批量行情: 单次HTTP获取全市场行情（clist/get API，最多6000只）
  - 市场数据: 龙虎榜、热度排名、涨停池、跌停池、炸板池（datacenter API）

特点:
  - 国内最稳定的免费数据源
  - 批量行情支持全市场（一次HTTP获取所有A股行情）
  - 市场数据丰富（龙虎榜/涨停池等独有数据）
  - K线API是 per-symbol 的，不支持原生批量

在架构中的位置:
  KlineService → DataSourceFactory → Coordinator → EastMoneyDataSource（本模块）

关键依赖:
  - requests: HTTP 请求
  - app.data_sources.normalizer: 股票代码标准化（to_eastmoney_secid, to_raw_digits, safe_float, safe_int）
  - app.data_sources.rate_limiter: 限流器
"""

from __future__ import annotations

import itertools
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests

from app.data_sources.normalizer import to_eastmoney_secid, to_raw_digits, safe_float, safe_int
from app.data_sources.rate_limiter import (
    get_request_headers, retry_with_backoff, get_eastmoney_limiter,
)
from app.data_sources.provider import register
from app.utils.logger import get_logger

logger = get_logger(__name__)


# ================================================================
# Referer 轮换池 — 提高访问成功率
# ================================================================

class _RefererPool:
    """线程安全的 Referer 轮换池"""

    def __init__(self, referers: List[str]):
        self._referers = referers
        self._cycle = itertools.cycle(referers)
        self._lock = threading.Lock()

    def next(self) -> str:
        with self._lock:
            return next(self._cycle)


# 东财行情/K线接口 Referer 池
_em_quote_referers = _RefererPool([
    "https://quote.eastmoney.com/",
    "https://www.eastmoney.com/",
    "https://stock.eastmoney.com/",
    "https://data.eastmoney.com/",
    "https://push2.eastmoney.com/",
])

# 东财数据中心 Referer 池
_em_data_referers = _RefererPool([
    "https://data.eastmoney.com/",
    "https://datacenter-web.eastmoney.com/",
    "https://www.eastmoney.com/",
    "https://quote.eastmoney.com/",
])

# 东财K线周期映射: 内部周期 → 东财 klt 参数
# klt (K Line Type): 1=1分钟, 5=5分钟, ..., 101=日线, 102=周线
_EM_KLT = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1H": 60, "1D": 101, "1W": 102}

# 东财复权类型映射: 内部复权方式 → 东财 fqt 参数
# fqt (Forward/Backward Adjust): 0=不复权, 1=前复权, 2=后复权
_EM_FQT = {"": 0, "qfq": 1, "hfq": 2}


@register(priority=30)
class EastMoneyDataSource:
    """
    东方财富数据源 — 国内最稳定的免费数据源之一（priority=30）。

    能力:
      - K线: 全周期（分钟/日/周），通过 kline/get API
      - 行情: 单只实时行情（stock/get API）
      - 批量行情: 全市场行情（clist/get API，一次HTTP最多6000只）
      - 市场数据: 龙虎榜/热度/涨停池/跌停池/炸板池（独立函数）

    线程安全性:
      - 实例方法无状态，线程安全
      - 通过 get_eastmoney_limiter() 进行全局限流

    API参数说明:
      - secid: 证券ID，格式为 "市场代码.股票代码"（如 "1.600519"）
      - ut: 用户令牌（固定值，东财API要求）
      - fields1: 基础字段（f1=代码, f2=名称, f3=最新价）
      - fields2: K线字段（f51=日期, f52=开盘, f53=收盘, f54=最高, f55=最低, f56=成交量...）
      - klt: K线周期类型
      - fqt: 复权类型
    """

    name = "eastmoney"
    priority = 25

    capabilities = {
        "kline": True,
        "kline_priority": 25,
        "kline_tf": {"1m", "5m", "15m", "30m", "1H", "1D", "1W"},
        "kline_batch": False,   # 东财K线API是per-symbol，无原生批量
        "quote": True,
        "quote_priority": 20,
        "batch_quote": True,
        "batch_quote_priority": 5,
        "hk": False,
        "markets": {"CNStock"},
    }

    @retry_with_backoff(max_attempts=3, base_delay=2.0, max_delay=12.0, exceptions=(
        requests.exceptions.RequestException, ConnectionError, TimeoutError,
    ))
    def fetch_kline(
        self, code: str, timeframe: str = "1D", count: int = 300,
        adj: str = "qfq", timeout: int = 10,
    ) -> List[Dict[str, Any]]:
        """
        获取单只股票K线数据。

        通过东财 kline/get API 获取，支持全周期。
        响应格式: data.klines = ["2024-01-01 10:30,100.00,103.00,105.00,98.00,1000,100000,..."]

        Args:
            code:      股票代码
            timeframe: K线周期
            count:     请求数据条数
            adj:       复权方式（通过 fqt 参数控制）
            timeout:   请求超时秒数

        Returns:
            K线数据列表
        """
        secid = to_eastmoney_secid(code)
        if not secid:
            return []
        klt = _EM_KLT.get(timeframe)
        if klt is None:
            return []
        get_eastmoney_limiter().wait()
        resp = requests.get(
            "https://49.push2his.eastmoney.com/api/qt/stock/kline/get",
            headers=get_request_headers(referer=_em_quote_referers.next()),
            params={
                "secid": secid,                    # 证券ID（如 "1.600519"）
                "ut": "fa5fd1943c7b386f172d6893dbbd1835",  # 用户令牌（固定值）
                "fields1": "f1,f2,f3",             # 基础字段: 代码/名称/最新价
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",  # K线字段: 日期/开/收/高/低/量/额/振幅/涨跌幅/涨跌额/换手率
                "klt": klt,                        # K线周期类型
                "fqt": 0,                          # 复权类型（0=不复权，复权在上层处理）
                "end": "20500101",                 # 结束日期（设为未来日期取最新数据）
                "lmt": min(int(count), 5000),      # 数据条数限制
            },
            timeout=timeout,
        )
        try:
            data = resp.json()
        except Exception:
            return []
        if not isinstance(data, dict):
            return []
        klines_data = (data.get("data") or {}).get("klines")
        if not isinstance(klines_data, list):
            return []

        # 解析K线数据: 每行格式 "日期,开盘,收盘,最高,最低,成交量,成交额,..."
        out = []
        for line in klines_data:
            parts = line.split(",")
            if len(parts) < 7:
                continue
            try:
                dt_str = parts[0].strip()
                ts = None
                for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
                    try:
                        ts = int(datetime.strptime(dt_str, fmt).timestamp())
                        break
                    except ValueError:
                        continue
                if ts is None:
                    continue
                o, c, h, low, v = float(parts[1]), float(parts[2]), float(parts[3]), float(parts[4]), float(parts[5])
                if o == 0 and c == 0:
                    continue
                # 防御性修正: 如果最高价低于最低价，交换两者
                if h > 0 and low > 0 and h < low:
                    h, low = low, h
                out.append({
                    "time": ts, "open": round(o, 4), "high": round(h, 4),
                    "low": round(low, 4), "close": round(c, 4), "volume": round(v, 2),
                })
            except (ValueError, TypeError, IndexError):
                continue
        out.sort(key=lambda x: x["time"])
        return out[-count:] if len(out) > count else out

    def fetch_quote(self, code: str, timeout: int = 8) -> Optional[Dict[str, Any]]:
        """
        获取单只股票实时行情。

        通过东财 stock/get API 获取行情快照。
        字段映射:
          f43=最新价, f44=最高, f45=最低, f46=开盘, f47=成交量, f48=成交额
          f57=代码, f58=名称, f60=昨收, f170=涨跌幅, f171=涨跌额

        Args:
            code:    股票代码
            timeout: 请求超时秒数

        Returns:
            行情字典，失败返回 None
        """
        secid = to_eastmoney_secid(code)
        if not secid:
            return None
        get_eastmoney_limiter().wait()
        resp = requests.get(
            "https://push2.eastmoney.com/api/qt/stock/get",
            headers=get_request_headers(referer=_em_quote_referers.next()),
            params={
                "secid": secid,
                "ut": "fa5fd1943c7b386f172d6893dbfba10b",  # 用户令牌（固定值）
                "fields": "f43,f44,f45,f46,f47,f48,f57,f58,f60,f170,f171",
            },
            timeout=timeout,
        )
        try:
            data = resp.json()
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        d = data.get("data")
        if not isinstance(d, dict):
            return None

        def _f(key: str, default: float = 0.0) -> float:
            """安全提取浮点字段，处理 None/"-" 等异常值"""
            v = d.get(key)
            if v is None or v == "-" or v == "":
                return default
            try:
                return float(v)
            except (TypeError, ValueError):
                return default

        last = _f("f43")
        prev = _f("f60")
        if last == 0 and prev == 0:
            return None
        change = round(last - prev, 4) if prev else 0.0
        change_pct = round(change / prev * 100, 2) if prev else 0.0
        return {
            "symbol": secid, "name": str(d.get("f58", "")).strip(),
            "last": last, "change": change, "changePercent": change_pct,
            "high": _f("f44"), "low": _f("f45"), "open": _f("f46"),
            "previousClose": prev, "volume": _f("f47"), "amount": _f("f48"),
        }

    def fetch_quotes_batch(self, codes: List[str], timeout: int = 15) -> Dict[str, Dict[str, Any]]:
        """
        批量获取全市场实时行情 — 单次HTTP请求。

        通过东财 clist/get API 获取全市场行情（最多6000只）。
        从响应中筛选出请求的代码。

        东财 clist API 参数说明:
          - fs: 市场筛选条件（m:0+t:6=沪A, m:0+t:80=深A, m:1+t:2=沪B, m:1+t:23=深B）
          - fields: 请求字段（f2=最新价, f5=成交量, f6=成交额, f12=代码, f15=最高, f16=最低, f17=开盘, f18=昨收）

        Args:
            codes:   股票代码列表
            timeout: 请求超时秒数

        Returns:
            {code: quote_dict} — 仅包含请求代码中成功获取的部分
        """
        if not codes:
            return {}
        # 提取6位纯数字代码用于匹配
        code_set: Dict[str, str] = {}
        for sym in codes:
            raw = to_raw_digits(sym)
            if raw and raw.isdigit() and len(raw) == 6:
                code_set[raw] = sym
        if not code_set:
            return {}
        try:
            get_eastmoney_limiter().wait()
            resp = requests.get(
                "https://push2.eastmoney.com/api/qt/clist/get",
                headers=get_request_headers(referer=_em_quote_referers.next()),
                params={
                    "pn": 1, "pz": 6000, "po": 1, "np": 1,  # 分页: 第1页, 每页6000条
                    "ut": "bd1d9ddb04089700cf9c27f6f7426281",  # 用户令牌
                    "fltt": 2, "invt": 2, "fid": "f3",       # 排序: 按涨跌幅降序
                    "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",  # 市场: 沪A+深A+沪B+深B
                    "fields": "f2,f5,f6,f12,f15,f16,f17,f18",  # 字段: 最新/量/额/代码/高/低/开/昨收
                },
                timeout=timeout,
            )
            data = resp.json()
            diff = ((data.get("data") or {}).get("diff")) or []
        except Exception as e:
            logger.warning("[东财批量行情] clist 请求失败: %s", e)
            return {}

        # 计算今日零点时间戳（东财行情不返回时间，手动补上）
        now = datetime.now(timezone(timedelta(hours=8)))
        today_ts = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        result: Dict[str, Dict[str, Any]] = {}
        for item in diff:
            code = str(item.get("f12", "")).strip()
            sym = code_set.get(code)
            if not sym:
                continue
            try:
                last = float(item.get("f2", 0))
                if last <= 0:
                    continue
                result[sym] = {
                    "last": last,
                    "open": round(float(item.get("f17", 0)), 4),
                    "high": round(float(item.get("f15", 0)), 4),
                    "low": round(float(item.get("f16", 0)), 4),
                    "previousClose": float(item.get("f18", 0)),
                    "volume": round(float(item.get("f5", 0)), 2),
                    "name": "", "symbol": sym, "time": today_ts,
                }
            except (ValueError, TypeError):
                continue
        return result


# ================================================================
# 市场数据 — 独立函数
# ================================================================

def _em_request(report_name: str, params: dict = None, timeout: int = 10) -> list:
    """
    东财 datacenter API 通用请求函数。

    所有市场数据（龙虎榜/涨停池等）共用同一个 datacenter API，
    通过 report_name 区分不同的报表。

    Args:
        report_name: 报表名称（如 "RPT_DAILYBILLBOARD_DETAILSNEW"）
        params:      额外请求参数
        timeout:     请求超时秒数

    Returns:
        数据列表，失败返回空列表
    """
    get_eastmoney_limiter().wait()
    default_params = {
        "sortColumns": "TRADE_DATE", "sortTypes": "-1",
        "pageSize": 500, "pageNumber": 1,
        "reportName": report_name, "columns": "ALL",
        "source": "WEB", "client": "WEB",
    }
    if params:
        default_params.update(params)
    resp = requests.get(
        "https://datacenter-web.eastmoney.com/api/data/v1/get",
        headers=get_request_headers(referer=_em_data_referers.next()),
        params=default_params, timeout=timeout,
    )
    try:
        data = resp.json()
    except Exception:
        return []
    return ((data.get("result") or {}).get("data")) or []


def fetch_dragon_tiger(start_date: str, end_date: str) -> List[Dict[str, Any]]:
    """
    获取龙虎榜数据（每日龙虎榜明细）。

    龙虎榜是交易所公布的异常波动股票的买卖席位明细，
    包括买入/卖出金额、净买入、涨跌幅、换手率等。

    Args:
        start_date: 开始日期（格式: "2024-01-01"）
        end_date:   结束日期（格式: "2024-01-31"）

    Returns:
        龙虎榜明细列表，每个元素包含:
          stock_code/stock_name/trade_date/reason/buy_amount/sell_amount/net_amount
          change_percent/close_price/turnover_rate/amount/buy_seat_count/sell_seat_count
    """
    items = _em_request(
        "RPT_DAILYBILLBOARD_DETAILSNEW",
        params={"filter": f"(TRADE_DATE>='{start_date}')(TRADE_DATE<='{end_date}')"},
    )
    if not items:
        return []
    result = []
    for item in items:
        try:
            result.append({
                "stock_code": str(item.get("SECURITY_CODE", "")).strip(),
                "stock_name": str(item.get("SECURITY_NAME_ABBR", "")).strip(),
                "trade_date": (str(item.get("TRADE_DATE", ""))[:10]).strip(),
                "reason": str(item.get("EXPLANATION", "") or "").strip()[:100],
                "buy_amount": safe_float(item.get("BUY")),
                "sell_amount": safe_float(item.get("SELL")),
                "net_amount": safe_float(item.get("NET_BUY")),
                "change_percent": safe_float(item.get("CHANGE_RATE")),
                "close_price": safe_float(item.get("CLOSE_PRICE")),
                "turnover_rate": safe_float(item.get("TURNOVERRATE")),
                "amount": safe_float(item.get("ACCUM_AMOUNT")),
                "buy_seat_count": safe_int(item.get("BUYER_NUM") or 0),
                "sell_seat_count": safe_int(item.get("SELLER_NUM") or 0),
            })
        except Exception:
            continue
    return result


def fetch_hot_rank() -> List[Dict[str, Any]]:
    """
    获取市场热度排名（热门股票TOP50）。

    热度排名基于东财的热度数据，按涨跌幅降序排列，
    包含热度分数和排名变化。

    Returns:
        热门股票列表（最多50只），每个元素包含:
          rank/stock_code/stock_name/price/change_percent/popularity_score/current_rank_change
    """
    items = _em_request(
        "RPT_HOT_STOCK_NEW",
        params={
            "sortColumns": "CHANGE_RATE", "sortTypes": "-1", "pageSize": 50,
            "filter": "(MARKET_TYPE in (\"沪深A股\"))",
        },
    )
    if not items:
        return []
    result = []
    for i, item in enumerate(items):
        try:
            code = str(item.get("SECURITY_CODE", "")).strip()
            if not code:
                continue
            result.append({
                "rank": i + 1, "stock_code": code,
                "stock_name": str(item.get("SECURITY_NAME_ABBR", "")).strip(),
                "price": safe_float(item.get("NEWEST_PRICE", item.get("CLOSE_PRICE"))),
                "change_percent": safe_float(item.get("CHANGE_RATE")),
                "popularity_score": safe_float(item.get("HOT_NUM", item.get("SCORE"))),
                "current_rank_change": str(item.get("RANK_CHANGE", "")),
            })
        except Exception:
            continue
    return result


def fetch_zt_pool(trade_date: str) -> List[Dict[str, Any]]:
    """
    获取涨停板股票池（当日涨停股票列表）。

    涨停板是当日涨幅达到涨停限制（主板10%，创业板/科创板20%）的股票，
    包含涨停时间、封单金额、连板天数等信息。

    Args:
        trade_date: 交易日期（格式: "2024-01-01"）

    Returns:
        涨停股票列表，每个元素包含:
          stock_code/stock_name/trade_date/price/change_percent/continuous_zt_days
          zt_time/seal_amount/turnover_rate/volume/amount/sector/reason/open_count
    """
    items = _em_request(
        "RPT_LIMITED_BOARD_POOL",
        params={"sortColumns": "TOTAL_MARKET_CAP", "sortTypes": "-1",
                "filter": f"(TRADE_DATE='{trade_date}')"},
    )
    if not items:
        return []
    result = []
    for item in items:
        try:
            result.append({
                "stock_code": str(item.get("SECURITY_CODE", "")).strip(),
                "stock_name": str(item.get("SECURITY_NAME_ABBR", "")).strip(),
                "trade_date": trade_date,
                "price": safe_float(item.get("CLOSE_PRICE")),
                "change_percent": safe_float(item.get("CHANGE_RATE")),
                "continuous_zt_days": safe_int(item.get("CONTINUOUS_LIMIT_DAYS", item.get("ZT_DAYS", 1)) or 1),
                "zt_time": str(item.get("FIRST_ZDT_TIME", "")),
                "seal_amount": safe_float(item.get("LIMIT_ORDER_AMT")),
                "turnover_rate": safe_float(item.get("TURNOVERRATE")),
                "volume": safe_float(item.get("VOLUME")),
                "amount": safe_float(item.get("TURNOVER")),
                "sector": str(item.get("BOARD_NAME", "")),
                "reason": str(item.get("ZT_REASON", ""))[:80],
                "open_count": safe_int(item.get("OPEN_NUM", 0) or 0),
            })
        except Exception:
            continue
    return result


def fetch_dt_pool(trade_date: str) -> List[Dict[str, Any]]:
    """
    获取跌停板股票池（当日跌停股票列表）。

    跌停板是当日跌幅达到跌停限制的股票。

    Args:
        trade_date: 交易日期（格式: "2024-01-01"）

    Returns:
        跌停股票列表，每个元素包含:
          stock_code/stock_name/trade_date/price/change_percent/seal_amount
          turnover_rate/amount
    """
    items = _em_request(
        "RPT_DOWNTREND_LIMIT_POOL",
        params={"sortColumns": "TOTAL_MARKET_CAP", "sortTypes": "-1",
                "filter": f"(TRADE_DATE='{trade_date}')"},
    )
    if not items:
        return []
    result = []
    for item in items:
        try:
            result.append({
                "stock_code": str(item.get("SECURITY_CODE", "")).strip(),
                "stock_name": str(item.get("SECURITY_NAME_ABBR", "")).strip(),
                "trade_date": trade_date,
                "price": safe_float(item.get("CLOSE_PRICE")),
                "change_percent": safe_float(item.get("CHANGE_RATE")),
                "seal_amount": safe_float(item.get("LIMIT_ORDER_AMT")),
                "turnover_rate": safe_float(item.get("TURNOVERRATE")),
                "amount": safe_float(item.get("TURNOVER")),
            })
        except Exception:
            continue
    return result


def fetch_broken_board(trade_date: str) -> List[Dict[str, Any]]:
    """
    获取炸板股票池（当日涨停后打开的股票列表）。

    炸板是指股票涨停后，封单被打开（未能封住涨停至收盘），
    包含涨停时间和炸板时间。

    Args:
        trade_date: 交易日期（格式: "2024-01-01"）

    Returns:
        炸板股票列表，每个元素包含:
          stock_code/stock_name/trade_date/price/change_percent
          zt_time/break_time/turnover_rate/amount
    """
    items = _em_request(
        "RPT_LIMITED_BOARD_UNSEALED",
        params={"sortColumns": "TOTAL_MARKET_CAP", "sortTypes": "-1",
                "filter": f"(TRADE_DATE='{trade_date}')"},
    )
    if not items:
        return []
    result = []
    for item in items:
        try:
            result.append({
                "stock_code": str(item.get("SECURITY_CODE", "")).strip(),
                "stock_name": str(item.get("SECURITY_NAME_ABBR", "")).strip(),
                "trade_date": trade_date,
                "price": safe_float(item.get("CLOSE_PRICE")),
                "change_percent": safe_float(item.get("CHANGE_RATE")),
                "zt_time": str(item.get("FIRST_ZDT_TIME", "")),
                "break_time": str(item.get("LAST_ZDT_TIME", "")),
                "turnover_rate": safe_float(item.get("TURNOVERRATE")),
                "amount": safe_float(item.get("TURNOVER")),
            })
        except Exception:
            continue
    return result


def aggregate_daily_to_monthly(daily_bars: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    """
    将日线K线聚合为月线K线。

    聚合算法:
      1. 按时间排序所有日线数据
      2. 按月份分组（以每月1日零点为分组键）
      3. 每组内:
         - open = 该月第一天的开盘价
         - high = 该月所有最高价的最大值
         - low = 该月所有最低价的最小值
         - close = 该月最后一天的收盘价
         - volume = 该月所有成交量之和
      4. 截取最后 limit 条

    Args:
        daily_bars: 日线K线数据列表
        limit:      返回的月线条数

    Returns:
        月线K线数据列表，按时间升序排列
    """
    if not daily_bars:
        return []
    # 按时间排序
    bars = sorted(daily_bars, key=lambda x: x.get("time", 0))

    # 按月份分组: 使用每月1日零点时间戳作为分组键
    groups: Dict[int, list] = {}
    order: List[int] = []
    for bar in bars:
        t = bar.get("time", 0)
        if not t:
            continue
        dt = datetime.fromtimestamp(t, tz=timezone(timedelta(hours=8)))
        # 取该月1日零点时间戳
        ms = int(dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())
        if ms not in groups:
            groups[ms] = []
            order.append(ms)
        groups[ms].append(bar)

    # 按月聚合
    result = []
    for ms in order:
        chunk = groups[ms]
        if not chunk:
            continue
        result.append({
            "time": ms,
            "open": float(chunk[0].get("open", 0)),                          # 月初开盘
            "high": max(float(b.get("high", 0)) for b in chunk),             # 月内最高
            "low": min(float(b.get("low", 0)) for b in chunk),               # 月内最低
            "close": float(chunk[-1].get("close", 0)),                        # 月末收盘
            "volume": round(sum(float(b.get("volume", 0)) for b in chunk), 2),  # 月内总成交量
        })
    return result[-limit:] if len(result) > limit else result

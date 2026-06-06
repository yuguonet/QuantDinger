"""
市场指数接口 — mootdx 主源 + 腾讯/AKShare/BaoStock 降级

功能:
  1. 指数实时行情  — 9 大核心指数
  2. 指数日K线     — 任意指数代码
  3. 指数多周期K线 — 1m/5m/15m/30m/1H/1D/1W/1M

数据源优先级:
  实时行情: mootdx(TCP) → 腾讯财经(HTTP) → AKShare
  日K线:    mootdx(TCP) → AKShare → BaoStock

依赖: pip install mootdx
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import pandas as pd

from app.utils.logger import get_logger

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════
#  常量
# ══════════════════════════════════════════════════════════════

INDEX_CODES: Dict[str, str] = {
    "000001": "上证指数",
    "399001": "深证成指",
    "399006": "创业板指",
    "000300": "沪深300",
    "000905": "中证500",
    "000852": "中证1000",
    "000016": "上证50",
    "000688": "科创50",
    "899050": "北证50",
    "399303": "国证2000",
    "399005": "中小100",
}

# 通达信 K线周期: 0=5m 1=15m 2=30m 3=1h 4=daily 5=weekly 6=monthly 7=1m
TDX_FREQ: Dict[str, int] = {
    "1m": 7, "5m": 0, "15m": 1, "30m": 2, "1H": 3,
    "1D": 4, "1W": 5, "1M": 6,
}

# ══════════════════════════════════════════════════════════════
#  mootdx 客户端（单例，1h 自动重建）
# ══════════════════════════════════════════════════════════════

_client = None
_client_ts = 0
_CLIENT_TTL = 3600


def _get_client():
    global _client, _client_ts
    if _client is not None and (time.time() - _client_ts) < _CLIENT_TTL:
        try:
            if not _client.closed:
                return _client
        except Exception:
            pass
        _client = None

    try:
        from mootdx.quotes import Quotes
        _client = Quotes.factory(market='std', timeout=10, heartbeat=True)
        _client_ts = time.time()
        logger.info("[mootdx] 连接成功")
        return _client
    except Exception as e:
        logger.warning("[mootdx] 连接失败: %s", e)
        _client = None
        return None


def _idx_market(code: str) -> int:
    return 1 if code[:3] in ("000", "88", "99") else 0


# ══════════════════════════════════════════════════════════════
#  实时行情数据源
# ══════════════════════════════════════════════════════════════

def _rt_mootdx(codes: List[str]) -> Optional[List[Dict[str, Any]]]:
    cli = _get_client()
    if cli is None:
        return None
    try:
        df = cli.quotes(symbol=codes)
        if df is None or (isinstance(df, pd.DataFrame) and df.empty):
            return None
        out = []
        for _, r in df.iterrows():
            code = str(r.get("code", ""))
            price = float(r.get("price", 0))
            last_close = float(r.get("last_close", 0))
            out.append({
                "code": code,
                "name": INDEX_CODES.get(code, str(r.get("name", code))),
                "price": price, "open": float(r.get("open", 0)),
                "high": float(r.get("high", 0)), "low": float(r.get("low", 0)),
                "last_close": last_close,
                "change": round(price - last_close, 4),
                "change_percent": float(r.get("percent", 0)),
                "volume": float(r.get("vol", 0)),
                "amount": float(r.get("amount", 0)),
            })
        logger.info("[mootdx] 实时行情 %d 条", len(out))
        return out
    except Exception as e:
        logger.warning("[mootdx] 实时行情失败: %s", e)
        return None


def _rt_tencent(codes: List[str]) -> Optional[List[Dict[str, Any]]]:
    import urllib.request
    prefixed = []
    for c in codes:
        pfx = "sh" if c[:3] in ("000", "88", "99") else "sz"
        prefixed.append(f"{pfx}{c}")
    try:
        req = urllib.request.Request("https://qt.gtimg.cn/q=" + ",".join(prefixed))
        req.add_header("User-Agent", "Mozilla/5.0")
        raw = urllib.request.urlopen(req, timeout=10).read().decode("gbk")
    except Exception as e:
        logger.warning("[tencent] 请求失败: %s", e)
        return None

    out = []
    for line in raw.strip().split(";"):
        if "=" not in line or '"' not in line:
            continue
        v = line.split('"')[1].split("~")
        if len(v) < 50:
            continue
        code = line.split("_")[-1][2:]
        price = float(v[3]) if v[3] else 0
        last_close = float(v[4]) if v[4] else 0
        out.append({
            "code": code, "name": v[1],
            "price": price, "open": float(v[5]) if v[5] else 0,
            "high": float(v[33]) if v[33] else 0, "low": float(v[34]) if v[34] else 0,
            "last_close": last_close,
            "change": float(v[31]) if v[31] else 0,
            "change_percent": float(v[32]) if v[32] else 0,
            "volume": float(v[36]) if v[36] else 0,
            "amount": float(v[37]) if v[37] else 0,
        })
    return out or None


def _rt_akshare(codes: List[str]) -> Optional[List[Dict[str, Any]]]:
    try:
        import akshare as ak
    except ImportError:
        return None
    try:
        df = ak.stock_zh_index_spot_em()
        out = []
        for code in codes:
            row = df[df["代码"] == code]
            if row.empty:
                continue
            r = row.iloc[0]
            out.append({
                "code": code,
                "name": str(r.get("名称", INDEX_CODES.get(code, code))),
                "price": float(r.get("最新价", 0)),
                "open": float(r.get("今开", 0)),
                "high": float(r.get("最高", 0)),
                "low": float(r.get("最低", 0)),
                "last_close": float(r.get("昨收", 0)),
                "change": float(r.get("涨跌额", 0)),
                "change_percent": float(r.get("涨跌幅", 0)),
                "volume": float(r.get("成交量", 0)),
                "amount": float(r.get("成交额", 0)),
            })
        return out or None
    except Exception as e:
        logger.warning("[akshare] 实时行情失败: %s", e)
        return None


# ══════════════════════════════════════════════════════════════
#  日K线数据源
# ══════════════════════════════════════════════════════════════

def _kline_mootdx(code: str, days: int) -> Optional[pd.DataFrame]:
    cli = _get_client()
    if cli is None:
        return None
    try:
        df = cli.index_bars(symbol=code, frequency=4, start=0, offset=min(days, 800))
        if df is None or (isinstance(df, pd.DataFrame) and df.empty):
            return None
        df = df.rename(columns={"datetime": "date", "vol": "volume"})
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
        cols = [c for c in ["date", "open", "high", "low", "close", "volume", "amount"] if c in df.columns]
        logger.info("[mootdx] 日K线 %s: %d 条", code, len(df))
        return df[cols].tail(days)
    except Exception as e:
        logger.warning("[mootdx] 日K线失败(%s): %s", code, e)
        return None


def _kline_akshare(code: str, days: int) -> Optional[pd.DataFrame]:
    try:
        import akshare as ak
    except ImportError:
        return None
    try:
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=days + 60)).strftime("%Y%m%d")
        df = ak.index_zh_a_hist(symbol=code, period="daily", start_date=start, end_date=end)
        if df is None or df.empty:
            return None
        df = df.rename(columns={
            "日期": "date", "开盘": "open", "最高": "high",
            "最低": "low", "收盘": "close", "成交量": "volume", "成交额": "amount",
        })
        df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
        cols = [c for c in ["date", "open", "high", "low", "close", "volume", "amount"] if c in df.columns]
        logger.info("[akshare] 日K线 %s: %d 条", code, len(df))
        return df[cols].tail(days)
    except Exception as e:
        logger.warning("[akshare] 日K线失败(%s): %s", code, e)
        return None


def _kline_baostock(code: str, days: int) -> Optional[pd.DataFrame]:
    try:
        import baostock as bs
    except ImportError:
        return None
    try:
        pfx = "sh" if code[:3] in ("000", "88", "99") else "sz"
        bs.login()
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=days + 60)).strftime("%Y-%m-%d")
        rs = bs.query_history_k_data_plus(
            f"{pfx}.{code}", "date,open,high,low,close,volume,amount",
            start_date=start, end_date=end, frequency="d", adjustflag="3",
        )
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
        bs.logout()
        if not rows:
            return None
        df = pd.DataFrame(rows, columns=rs.fields)
        for c in ["open", "high", "low", "close", "volume", "amount"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        logger.info("[baostock] 日K线 %s: %d 条", code, len(df))
        return df.tail(days)
    except Exception as e:
        logger.warning("[baostock] 日K线失败(%s): %s", code, e)
        try:
            bs.logout()
        except Exception:
            pass
        return None


# ══════════════════════════════════════════════════════════════
#  对外接口
# ══════════════════════════════════════════════════════════════

def get_index_realtime(codes: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """获取指数实时行情

    Args:
        codes: 指数代码列表，默认全部 9 大指数

    Returns:
        [{code, name, price, open, high, low, last_close, change, change_percent, volume, amount}, ...]
    """
    if codes is None:
        codes = list(INDEX_CODES.keys())

    for fetcher in (_rt_mootdx, _rt_tencent, _rt_akshare):
        data = fetcher(codes)
        if data:
            return data

    logger.error("所有数据源获取指数实时行情均失败")
    return []


def get_index_daily_kline(code: str = "000001", days: int = 200) -> List[Dict[str, Any]]:
    """获取指数日K线

    Args:
        code: 指数代码，如 "000001"（上证）、"000300"（沪深300）
        days: 数据条数，默认 200

    Returns:
        [{date, open, high, low, close, volume, amount}, ...]
    """
    for fetcher in (_kline_mootdx, _kline_akshare, _kline_baostock):
        df = fetcher(code, days)
        if df is not None and not df.empty:
            return df.to_dict(orient="records")

    logger.error("所有数据源获取指数日K线均失败: %s", code)
    return []


def get_index_kline(code: str = "000001", frequency: str = "1D", days: int = 200) -> List[Dict[str, Any]]:
    """获取指数K线（支持多周期）

    Args:
        code: 指数代码
        frequency: "1m"/"5m"/"15m"/"30m"/"1H"/"1D"/"1W"/"1M"
        days: 数据条数

    Returns:
        [{date, open, high, low, close, volume, amount}, ...]
    """
    if frequency == "1D":
        return get_index_daily_kline(code, days)

    freq = TDX_FREQ.get(frequency)
    if freq is None:
        logger.error("不支持的周期: %s", frequency)
        return []

    cli = _get_client()
    if cli is None:
        logger.error("[mootdx] 不可用，无法获取 %s 周期K线", frequency)
        return []

    try:
        df = cli.index_bars(symbol=code, frequency=freq, start=0, offset=min(days, 800))
        if df is None or (isinstance(df, pd.DataFrame) and df.empty):
            return []
        df = df.rename(columns={"datetime": "date", "vol": "volume"})
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d %H:%M:%S")
        cols = [c for c in ["date", "open", "high", "low", "close", "volume", "amount"] if c in df.columns]
        return df[cols].tail(days).to_dict(orient="records")
    except Exception as e:
        logger.error("[mootdx] K线失败(%s/%s): %s", code, frequency, e)
        return []

"""
市场指数接口 — mootdx 主源 + 腾讯/AKShare/BaoStock 降级

功能:
  1. 指数实时行情  — 11 大核心指数（上证/深证/创业板/沪深300/中证500 等）
  2. 指数日K线     — 任意指数代码，返回 OHLCV 标准格式
  3. 指数多周期K线 — 1m/5m/15m/30m/1H/1D/1W/1M

数据源优先级:
  实时行情: mootdx(TCP直连通达信服务器) → 腾讯财经(HTTP API) → AKShare(东方财富)
  日K线:    mootdx(TCP) → AKShare → BaoStock(证券宝)

注意:
  - mootdx 为 TCP 长连接，首次连接约 1-3 秒，后续请求毫秒级
  - 腾讯接口无需安装额外依赖，但数据精度略低
  - AKShare / BaoStock 需要 pip install akshare / baostock

依赖: pip install mootdx
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import pandas as pd

from app.utils.logger import get_logger

logger = logging.getLogger(__name__)

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

# 通达信 K线周期编码映射
# mootdx 的 frequency 参数使用通达信内部编码:
#   0=5分钟  1=15分钟  2=30分钟  3=1小时
#   4=日线   5=周线    6=月线    7=1分钟
TDX_FREQ: Dict[str, int] = {
    "1m": 7,   # 1分钟K线
    "5m": 0,   # 5分钟K线
    "15m": 1,  # 15分钟K线
    "30m": 2,  # 30分钟K线
    "1H": 3,   # 1小时K线
    "1D": 4,   # 日K线（最常用）
    "1W": 5,   # 周K线
    "1M": 6,   # 月K线
}

# ══════════════════════════════════════════════════════════════
#  mootdx 客户端管理（单例 + TTL 自动重建）
# ══════════════════════════════════════════════════════════════

# 全局单例: 三个模块(index/tape/finance)各自维护独立的 _client
# 这样做的好处是互不干扰——某个模块连接断开不会影响其他模块
_client = None       # mootdx Quotes 实例
_client_ts = 0       # 上次连接成功的时间戳（Unix epoch）
_CLIENT_TTL = 3600   # 连接有效期: 3600秒 = 1小时，超时后自动重建


def _get_client():
    """获取 mootdx 客户端单例。

    策略:
      1. 如果已有连接且未过期（< 1小时）且未关闭 → 直接复用
      2. 否则重新创建连接（TCP 连接通达信行情服务器）

    Returns:
        mootdx.quotes.Quotes 实例，或 None（连接失败时）
    """
    global _client, _client_ts

    # 检查现有连接是否可用: 未过期 + 未关闭
    if _client is not None and (time.time() - _client_ts) < _CLIENT_TTL:
        try:
            if not _client.closed:
                return _client
        except Exception:
            pass
        _client = None  # 连接已关闭，标记为无效

    # 创建新连接
    try:
        from mootdx.quotes import Quotes
        # market='std' 使用标准行情服务器
        # timeout=10 连接/读取超时10秒
        # heartbeat=True 启用心跳包，防止长连接被服务端断开
        _client = Quotes.factory(market='std', timeout=10, heartbeat=True)
        _client_ts = time.time()
        logger.info("[mootdx] 连接成功")
        return _client
    except Exception as e:
        logger.warning("[mootdx] 连接失败: %s", e)
        _client = None
        return None


def _idx_market(code: str) -> int:
    """指数代码 → 通达信市场号。

    通达信规则:
      - 市场号 1 = 上海（代码以 000/88/99 开头的指数）
      - 市场号 0 = 深圳（代码以 399 开头的指数）

    注意: 这里的 000 开头指的是上证指数等，不是深市股票！
    深市股票代码是 000xxx，但深市指数代码是 399xxx。

    Args:
        code: 6位指数代码，如 "000001"、"399001"

    Returns:
        0 或 1（通达信市场号）
    """
    return 1 if code[:3] in ("000", "88", "99") else 0


# ══════════════════════════════════════════════════════════════
#  实时行情数据源（三级降级）
# ══════════════════════════════════════════════════════════════

def _rt_mootdx(codes: List[str]) -> Optional[List[Dict[str, Any]]]:
    """数据源1: mootdx TCP 直连获取指数实时行情。

    最快最准的数据源，直接从通达信服务器拉取。
    非交易时段返回的是上一交易日收盘数据。

    Args:
        codes: 指数代码列表，如 ["000001", "399001"]

    Returns:
        成功: [{code, name, price, open, high, low, last_close, change,
                change_percent, volume, amount}, ...]
        失败: None（触发降级到下一个数据源）
    """
    cli = _get_client()
    if cli is None:
        return None
    try:
        # quotes() 支持批量查询，传入代码列表
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
                "price": price,                          # 最新价
                "open": float(r.get("open", 0)),         # 今日开盘价
                "high": float(r.get("high", 0)),         # 今日最高价
                "low": float(r.get("low", 0)),           # 今日最低价
                "last_close": last_close,                # 昨日收盘价
                "change": round(price - last_close, 4),  # 涨跌额 = 最新价 - 昨收
                "change_percent": float(r.get("percent", 0)),  # 涨跌幅(%)
                "volume": float(r.get("vol", 0)),        # 成交量（手）
                "amount": float(r.get("amount", 0)),     # 成交额（元）
            })
        logger.info("[mootdx] 实时行情 %d 条", len(out))
        return out
    except Exception as e:
        logger.warning("[mootdx] 实时行情失败: %s", e)
        return None


def _rt_tencent(codes: List[str]) -> Optional[List[Dict[str, Any]]]:
    """数据源2: 腾讯财经 HTTP API 获取指数实时行情。

    备用数据源，无需安装额外依赖，通过 HTTP GET 请求获取。
    接口地址: https://qt.gtimg.cn/q=sh000001,sz399001,...

    腾讯返回格式（GBK 编码）:
      v_sh000001="1~上证指数~000001~3261.56~3250.60~3256.75~...~";
      字段以 ~ 分隔，共 50+ 个字段。

    Args:
        codes: 指数代码列表

    Returns:
        成功: 同 _rt_mootdx 格式
        失败: None
    """
    import urllib.request

    # 为代码添加沪深前缀: 000/88/99 开头 → sh（上海），其他 → sz（深圳）
    prefixed = []
    for c in codes:
        pfx = "sh" if c[:3] in ("000", "88", "99") else "sz"
        prefixed.append(f"{pfx}{c}")

    try:
        url = "https://qt.gtimg.cn/q=" + ",".join(prefixed)
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "Mozilla/5.0")
        raw = urllib.request.urlopen(req, timeout=10).read().decode("gbk")
    except Exception as e:
        logger.warning("[tencent] 请求失败: %s", e)
        return None

    # 解析返回数据: 每条记录以 ";" 分隔
    out = []
    for line in raw.strip().split(";"):
        if "=" not in line or '"' not in line:
            continue

        # 提取 ~ 分隔的字段列表
        v = line.split('"')[1].split("~")
        if len(v) < 50:
            continue  # 字段不足，跳过异常数据

        # 从变量名中提取纯代码: v_sh000001 → 000001
        code = line.split("_")[-1][2:]

        price = float(v[3]) if v[3] else 0       # 最新价
        last_close = float(v[4]) if v[4] else 0   # 昨收

        out.append({
            "code": code,
            "name": v[1],                         # 指数名称
            "price": price,
            "open": float(v[5]) if v[5] else 0,   # 今开
            "high": float(v[33]) if v[33] else 0,  # 最高
            "low": float(v[34]) if v[34] else 0,   # 最低
            "last_close": last_close,
            "change": float(v[31]) if v[31] else 0,       # 涨跌额
            "change_percent": float(v[32]) if v[32] else 0, # 涨跌幅(%)
            "volume": float(v[36]) if v[36] else 0,       # 成交量
            "amount": float(v[37]) if v[37] else 0,       # 成交额
        })
    return out or None


def _rt_akshare(codes: List[str]) -> Optional[List[Dict[str, Any]]]:
    """数据源3: AKShare 获取指数实时行情（兜底）。

    通过 AKShare 库调用东方财富接口，速度较慢但覆盖面广。
    需要 pip install akshare。

    Args:
        codes: 指数代码列表

    Returns:
        成功: 同 _rt_mootdx 格式
        失败: None
    """
    try:
        import akshare as ak
    except ImportError:
        return None  # akshare 未安装，静默跳过
    try:
        # stock_zh_index_spot_em() 返回所有 A 股指数的实时行情 DataFrame
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
#  日K线数据源（三级降级）
# ══════════════════════════════════════════════════════════════

def _kline_mootdx(code: str, days: int) -> Optional[pd.DataFrame]:
    """数据源1: mootdx 获取指数日K线。

    通过 index_bars() 拉取指数日线数据，frequency=4 表示日线。
    最多单次拉取 800 条（通达信协议限制）。

    Args:
        code: 指数代码，如 "000001"
        days: 需要的K线条数

    Returns:
        成功: DataFrame，列 = [date, open, high, low, close, volume, amount]
        失败: None
    """
    cli = _get_client()
    if cli is None:
        return None
    try:
        # frequency=4 是日线编码; offset 最大 800（通达信协议限制）
        df = cli.index_bars(symbol=code, frequency=4, start=0, offset=min(days, 800))
        if df is None or (isinstance(df, pd.DataFrame) and df.empty):
            return None

        # 统一列名: mootdx 用 datetime/vol，我们用 date/volume
        df = df.rename(columns={"datetime": "date", "vol": "volume"})
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")

        # 只保留标准 OHLCV 列
        cols = [c for c in ["date", "open", "high", "low", "close", "volume", "amount"] if c in df.columns]
        logger.info("[mootdx] 日K线 %s: %d 条", code, len(df))
        return df[cols].tail(days)
    except Exception as e:
        logger.warning("[mootdx] 日K线失败(%s): %s", code, e)
        return None


def _kline_akshare(code: str, days: int) -> Optional[pd.DataFrame]:
    """数据源2: AKShare 获取指数日K线。

    通过 AKShare 调用东方财富的历史行情接口。
    会多请求 60 天数据以确保节假日等情况下能凑够 days 条。

    Args:
        code: 指数代码
        days: 需要的K线条数

    Returns:
        成功: DataFrame [date, open, high, low, close, volume, amount]
        失败: None
    """
    try:
        import akshare as ak
    except ImportError:
        return None
    try:
        # 多请求 60 天缓冲，防止节假日导致数据不足
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=days + 60)).strftime("%Y%m%d")

        # index_zh_a_hist: 东方财富指数历史行情接口
        df = ak.index_zh_a_hist(symbol=code, period="daily", start_date=start, end_date=end)
        if df is None or df.empty:
            return None

        # AKShare 返回中文列名，需要映射为英文
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
    """数据源3: BaoStock（证券宝）获取指数日K线（兜底）。

    BaoStock 是免费开源的证券数据接口，需要 pip install baostock。
    需要先 login() 才能查询，用完后 logout()。

    Args:
        code: 指数代码
        days: 需要的K线条数

    Returns:
        成功: DataFrame [date, open, high, low, close, volume, amount]
        失败: None
    """
    try:
        import baostock as bs
    except ImportError:
        return None
    try:
        # BaoStock 需要 sh./sz. 前缀格式
        pfx = "sh" if code[:3] in ("000", "88", "99") else "sz"
        bs.login()

        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=days + 60)).strftime("%Y-%m-%d")

        # adjustflag="3": 不复权（指数一般不需要复权）
        rs = bs.query_history_k_data_plus(
            f"{pfx}.{code}", "date,open,high,low,close,volume,amount",
            start_date=start, end_date=end, frequency="d", adjustflag="3",
        )

        # BaoStock 返回的是迭代器，逐行读取
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
        bs.logout()

        if not rows:
            return None

        df = pd.DataFrame(rows, columns=rs.fields)
        # BaoStock 返回的数值都是字符串，需要转换
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
#  对外接口（Public API）
# ══════════════════════════════════════════════════════════════

def get_index_realtime(codes: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """获取指数实时行情（自动降级）

    依次尝试 mootdx → 腾讯 → AKShare，第一个成功即返回。

    Args:
        codes: 指数代码列表。默认为 None 时查询 INDEX_CODES 中全部 11 个指数。

    Returns:
        成功: [{code, name, price, open, high, low, last_close,
                change, change_percent, volume, amount}, ...]
        失败: []（空列表，所有数据源都失败）

    Example:
        >>> get_index_realtime()  # 查询全部指数
        >>> get_index_realtime(["000001", "000300"])  # 只查上证和沪深300
    """
    if codes is None:
        codes = list(INDEX_CODES.keys())

    # 三级降级: mootdx → 腾讯 → AKShare
    for fetcher in (_rt_mootdx, _rt_tencent, _rt_akshare):
        data = fetcher(codes)
        if data:
            return data

    logger.error("所有数据源获取指数实时行情均失败")
    return []


def get_index_daily_kline(code: str = "000001", days: int = 200) -> List[Dict[str, Any]]:
    """获取指数日K线（自动降级）

    依次尝试 mootdx → AKShare → BaoStock。

    Args:
        code: 指数代码，默认 "000001"（上证指数）
        days: 数据条数，默认 200（约一个交易年的日线数）

    Returns:
        成功: [{date, open, high, low, close, volume, amount}, ...]
              date 格式 "YYYY-MM-DD"，按日期升序排列
        失败: []（空列表）

    Example:
        >>> get_index_daily_kline("000300", 60)  # 沪深300最近60个交易日
    """
    for fetcher in (_kline_mootdx, _kline_akshare, _kline_baostock):
        df = fetcher(code, days)
        if df is not None and not df.empty:
            return df.to_dict(orient="records")

    logger.error("所有数据源获取指数日K线均失败: %s", code)
    return []


def get_index_kline(code: str = "000001", frequency: str = "1D", days: int = 200) -> List[Dict[str, Any]]:
    """获取指数K线（支持多周期，仅 mootdx）

    对于日线(1D)会自动降级到 AKShare/BaoStock；
    对于分钟/周/月线，仅支持 mootdx（其他数据源不支持这些周期）。

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
        days: 数据条数，最大 800（通达信协议限制）

    Returns:
        成功: [{date, open, high, low, close, volume, amount}, ...]
              非日线的 date 格式为 "YYYY-MM-DD HH:MM:SS"
        失败: []（空列表）

    Example:
        >>> get_index_kline("000300", "30m", 100)  # 沪深300的30分钟线，100条
    """
    # 日线走完整的降级链路
    if frequency == "1D":
        return get_index_daily_kline(code, days)

    # 非日线周期必须通过 mootdx
    freq = TDX_FREQ.get(frequency)
    if freq is None:
        logger.error("不支持的周期: %s，可选: %s", frequency, list(TDX_FREQ.keys()))
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
            # 分钟级别保留时分秒
            df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d %H:%M:%S")

        cols = [c for c in ["date", "open", "high", "low", "close", "volume", "amount"] if c in df.columns]
        return df[cols].tail(days).to_dict(orient="records")
    except Exception as e:
        logger.error("[mootdx] K线失败(%s/%s): %s", code, frequency, e)
        return []


# ══════════════════════════════════════════════════════════════
#  北向资金（沪深港通）
# ══════════════════════════════════════════════════════════════

# 北向资金 = 境外投资者通过沪港通/深港通买入 A 股的资金
# 沪股通(hgt): 香港 → 上海
# 深股通(sgt): 香港 → 深圳
# 北向合计 = 沪股通净买入 + 深股通净买入（单位: 亿元）


def get_northbound_realtime() -> Dict[str, Any]:
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
    import urllib.request

    url = "https://data.hexin.cn/market/hsgtApi/method/dayChart/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/117.0.0.0 Safari/537.36",
        "Host": "data.hexin.cn",
        "Referer": "https://data.hexin.cn/",
    }
    try:
        req = urllib.request.Request(url)
        for k, v in headers.items():
            req.add_header(k, v)
        raw = urllib.request.urlopen(req, timeout=10).read()
        d = json.loads(raw)
    except Exception as e:
        logger.warning("[hexin] 北向实时失败: %s", e)
        return {"error": str(e)}

    times = d.get("time", [])
    hgt = d.get("hgt", [])
    sgt = d.get("sgt", [])

    # 组装数据点
    n = min(len(times), len(hgt), len(sgt))
    points = []
    for i in range(n):
        points.append({
            "time": times[i],
            "hgt_yi": hgt[i] if i < len(hgt) else None,
            "sgt_yi": sgt[i] if i < len(sgt) else None,
        })

    # 取最后一个非零值作为最新净买入
    hgt_latest = next((p["hgt_yi"] for p in reversed(points) if p["hgt_yi"] is not None and p["hgt_yi"] != 0), 0)
    sgt_latest = next((p["sgt_yi"] for p in reversed(points) if p["sgt_yi"] is not None and p["sgt_yi"] != 0), 0)

    return {
        "points": len(points),
        "hgt_latest_yi": hgt_latest,
        "sgt_latest_yi": sgt_latest,
        "total_latest_yi": round((hgt_latest or 0) + (sgt_latest or 0), 2),
        "data": points[-10:],  # 返回最近 10 条
    }


def get_northbound_daily(days: int = 120) -> List[Dict[str, Any]]:
    """获取北向资金日级净流入历史数据。

    数据源优先级:
      1. AKShare — 调用东方财富接口，数据最全（需 pip install akshare）
      2. 同花顺 hexin dayKline — 备用，约 30 天数据

    Args:
        days: 获取天数，默认 120

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
    # 数据源1: AKShare
    try:
        import akshare as ak
        df = ak.stock_hsgt_north_net_flow_in_em(symbol="北上")
        if df is not None and not df.empty:
            # AKShare 返回列名: 日期, 沪股通净流入, 深股通净流入, 北向资金净流入
            # 或英文列名，需要兼容处理
            cols = df.columns.tolist()
            out = []
            for _, row in df.tail(days).iterrows():
                date_val = str(row.iloc[0])[:10] if len(cols) > 0 else ""
                # 尝试中文列名，再试英文
                hgt = 0.0
                sgt = 0.0
                total = 0.0
                if len(cols) >= 4:
                    hgt = float(row.iloc[1]) if row.iloc[1] else 0
                    sgt = float(row.iloc[2]) if row.iloc[2] else 0
                    total = float(row.iloc[3]) if row.iloc[3] else 0
                elif len(cols) >= 2:
                    total = float(row.iloc[1]) if row.iloc[1] else 0

                out.append({
                    "date": date_val,
                    "hgt_yi": round(hgt, 2),
                    "sgt_yi": round(sgt, 2),
                    "total_yi": round(total, 2),
                })
            logger.info("[akshare] 北向日级: %d 条", len(out))
            return out
    except ImportError:
        pass  # akshare 未安装，降级
    except Exception as e:
        logger.warning("[akshare] 北向日级失败: %s", e)

    # 数据源2: 同花顺 hexin dayKline（约30天）
    try:
        import urllib.request
        url = "https://data.hexin.cn/market/hsgtApi/dayKline/"
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/117.0.0.0")
        req.add_header("Referer", "https://data.hexin.cn/")
        raw = urllib.request.urlopen(req, timeout=10).read()
        d = json.loads(raw)
        chart = d.get("chart", {})
        times = chart.get("time", [])
        hgt = chart.get("hgt", [])
        sgt = chart.get("sgt", [])

        out = []
        for i in range(len(times)):
            h = hgt[i] if i < len(hgt) else 0
            s = sgt[i] if i < len(sgt) else 0
            out.append({
                "date": times[i],
                "hgt_yi": round(float(h), 2) if h else 0,
                "sgt_yi": round(float(s), 2) if s else 0,
                "total_yi": round((float(h) if h else 0) + (float(s) if s else 0), 2),
            })
        logger.info("[hexin] 北向日级: %d 条", len(out))
        return out[-days:]
    except Exception as e:
        logger.warning("[hexin] 北向日级失败: %s", e)

    logger.error("所有数据源获取北向日级数据均失败")
    return []


def get_northbound_holdings(top: int = 50) -> List[Dict[str, Any]]:
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
    import urllib.request

    url = "https://data.hexin.cn/market/hsgtApi/dayKline/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/117.0.0.0",
        "Referer": "https://data.hexin.cn/",
    }
    try:
        req = urllib.request.Request(url)
        for k, v in headers.items():
            req.add_header(k, v)
        raw = urllib.request.urlopen(req, timeout=10).read()
        d = json.loads(raw)
        lst = d.get("list", [])

        out = []
        for item in lst:
            try:
                out.append({
                    "code": item.get("code", ""),
                    "name": item.get("name", ""),
                    "price": float(item.get("zxj", 0) or 0),           # 最新价
                    "hold_cost": float(item.get("cgcb", 0) or 0),      # 持仓成本
                    "profit_pct": float(item.get("cgyk", 0) or 0),     # 盈亏(%)
                    "hold_shares": int(float(item.get("cgl", 0) or 0)), # 持股数
                    "hold_market": float(item.get("cgsz", 0) or 0),    # 持仓市值(元)
                    "hold_ratio": float(item.get("ltb", 0) or 0),      # 占流通比(%)
                })
            except (ValueError, TypeError):
                continue

        # 按持仓市值降序
        out.sort(key=lambda x: x["hold_market"], reverse=True)
        logger.info("[hexin] 北向持股: %d 只", len(out))
        return out[:top]
    except Exception as e:
        logger.warning("[hexin] 北向持股失败: %s", e)
        return []

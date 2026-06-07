"""
市场指数接口 — mootdx 主源 + 腾讯/AKShare/BaoStock 降级

功能:
  1. 指数实时行情  — 11 大核心指数（上证/深证/创业板/沪深300/中证500 等）
  2. 指数日K线     — 任意指数代码，返回 OHLCV 标准格式
  3. 指数多周期K线 — 1m/5m/15m/30m/1H/1D/1W/1M
  4. 北向资金      — 实时分钟级 / 日级历史 / 持股明细
  5. 大盘资金流向  — 实时(分钟级) / 日线(历史)  主力/超大单/大单/中单/小单
  6. 行业资金流向  — 行业板块资金流入/流出排名

数据源优先级:
  实时行情: 腾讯财经(HTTP API) → AKShare(东方财富)
  日K线:    腾讯财经 → 新浪财经 → BaoStock(证券宝)
  北向资金: AKShare(东财) → 同花顺 hexin
  大盘资金流: 新浪财经(行业汇总) → 同花顺(行业汇总) → 东财 push2(兜底)
  行业资金流: 新浪财经 → 同花顺 → 东财 push2(兜底)

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
    """获取 mootdx 客户端单例（复用 provider 层 TDX 服务器探测结果）。

    策略:
      1. 已有连接且未过期且未关闭 → 直接复用
      2. 从 provider 层获取已探测的可用 HQ 服务器，逐个尝试连接
      3. 无可用服务器 → 返回 None

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
        _client = None

    # 从 provider 层获取已探测的可用服务器
    try:
        from app.data_sources.provider.tdx_ex import TdxExProvider
        provider = TdxExProvider()
        servers = [(h, p) for h, p, proto in provider._live_servers if proto == "hq"]
        if not servers:
            logger.warning("[mootdx] 无可用 HQ 服务器")
            return None
    except Exception as e:
        logger.warning("[mootdx] 获取服务器列表失败: %s", e)
        return None

    from mootdx.quotes import Quotes
    for host, port in servers:
        try:
            _client = Quotes.factory(market='std', timeout=10,
                                     heartbeat=True, server=(host, port))
            _client_ts = time.time()
            logger.info("[mootdx] 连接成功 %s:%d", host, port)
            return _client
        except Exception:
            continue

    logger.warning("[mootdx] 所有服务器连接失败")
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
            "volume": float(v[36]) / 100 if v[36] else 0,  # 成交量: 腾讯返回股→转手
            "amount": float(v[37]) * 10000 if v[37] else 0,  # 成交额: 腾讯返回万元→转元
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


def _idx_prefix(code: str) -> str:
    """指数代码加 sh/sz 前缀。"""
    return "sh" if code[:3] in ("000", "88", "99") else "sz"


def _kline_tencent(code: str, days: int) -> Optional[pd.DataFrame]:
    """数据源2: 腾讯财经 获取指数日K线。

    接口: web.ifzq.gtimg.cn/appstock/app/fqkline/get
    返回 JSON: data.{code}.day = [[date, open, close, high, low, volume], ...]
    volume 单位为手(lots)。

    Args:
        code: 指数代码，如 "000001"
        days: 需要的K线条数

    Returns:
        成功: DataFrame [date, open, high, low, close, volume]
        失败: None
    """
    import urllib.request
    try:
        sc = _idx_prefix(code) + code
        url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
        params = f"param={sc},day,,,{days},"
        full_url = f"{url}?{params}"
        req = urllib.request.Request(full_url)
        req.add_header("User-Agent", "Mozilla/5.0")
        req.add_header("Referer", "https://web.ifzq.gtimg.cn/")
        raw = urllib.request.urlopen(req, timeout=10).read()
        data = json.loads(raw)

        root = (data.get("data") or {}).get(sc)
        if not isinstance(root, dict):
            return None

        rows = root.get("day") or root.get("qfqday") or []
        if not rows:
            return None

        out = []
        for r in rows:
            if len(r) < 6:
                continue
            out.append({
                "date": str(r[0]),
                "open": float(r[1]),
                "high": float(r[3]),
                "low": float(r[4]),
                "close": float(r[2]),
                "volume": float(r[5]),  # 已是手(lots)
            })

        if out:
            logger.info("[tencent] 日K线 %s: %d 条", code, len(out))
        return pd.DataFrame(out) if out else None
    except Exception as e:
        logger.warning("[tencent] 日K线失败(%s): %s", code, e)
        return None


def _kline_sina(code: str, days: int) -> Optional[pd.DataFrame]:
    """数据源3: 新浪财经 获取指数日K线。

    接口: quotes.sina.cn/cn/api/jsonp_v2.php/CN_MarketDataService.getKLineData
    scale=240 表示日线，返回 JSONP 格式。

    Args:
        code: 指数代码，如 "000001"
        days: 需要的K线条数

    Returns:
        成功: DataFrame [date, open, high, low, close, volume]
        失败: None
    """
    import urllib.request, re
    try:
        sc = _idx_prefix(code) + code
        url = (
            f"https://quotes.sina.cn/cn/api/jsonp_v2.php"
            f"/var%20_{sc}=/CN_MarketDataService.getKLineData"
            f"?symbol={sc}&scale=240&ma=no&datalen={days}"
        )
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "Mozilla/5.0")
        req.add_header("Referer", "https://finance.sina.com.cn/")
        raw = urllib.request.urlopen(req, timeout=10).read().decode("utf-8", errors="replace")

        # 去掉可能的 <script> 标签和注释
        clean = re.sub(r"/\*.*?\*/", "", raw, flags=re.DOTALL)
        clean = re.sub(r"<[^>]+>", "", clean)
        # 匹配 JSON 数组: var _xxx=([...]) 或 =([...])
        m = re.search(r"=\s*\(?\s*(\[.*\])\s*\)?\s*;?\s*$", clean, re.DOTALL)
        if not m:
            return None

        arr = json.loads(m.group(1))
        out = []
        for r in arr:
            out.append({
                "date": str(r.get("day", "")),
                "open": float(r.get("open", 0)),
                "high": float(r.get("high", 0)),
                "low": float(r.get("low", 0)),
                "close": float(r.get("close", 0)),
                "volume": float(r.get("volume", 0)) / 100,  # 股→手
            })

        if out:
            logger.info("[sina] 日K线 %s: %d 条", code, len(out))
        return pd.DataFrame(out) if out else None
    except Exception as e:
        logger.warning("[sina] 日K线失败(%s): %s", code, e)
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
        # BaoStock volume 单位是股, 转为手 (÷100) 对齐 mootdx/akshare
        if "volume" in df.columns:
            df["volume"] = df["volume"] / 100

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
    for fetcher in (_kline_mootdx, _kline_tencent, _kline_sina, _kline_baostock):
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
        resp = urllib.request.urlopen(req, timeout=10)
        raw = resp.read()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("gbk", errors="replace")
        d = json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning("[hexin] 北向实时JSON解析失败: %s", e)
        return {"error": f"JSON解析失败: {e}"}
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
    # 注意: stock_hsgt_north_net_flow_in_em 已在 akshare >=1.14 中移除
    # 改用 stock_hsgt_hist_em 分别获取沪股通/深股通历史数据后合并
    try:
        import akshare as ak
        out = []
        # 获取沪股通历史
        hgt_df = ak.stock_hsgt_hist_em(symbol="沪股通")
        # 获取深股通历史
        sgt_df = ak.stock_hsgt_hist_em(symbol="深股通")

        if hgt_df is not None and not hgt_df.empty:
            hgt_map = {}
            for _, row in hgt_df.iterrows():
                date_val = str(row.get("日期", ""))[:10]
                net = row.get("当日成交净买额")
                if pd.notna(net):
                    hgt_map[date_val] = float(net)

            sgt_map = {}
            if sgt_df is not None and not sgt_df.empty:
                for _, row in sgt_df.iterrows():
                    date_val = str(row.get("日期", ""))[:10]
                    net = row.get("当日成交净买额")
                    if pd.notna(net):
                        sgt_map[date_val] = float(net)

            # 合并所有日期
            all_dates = sorted(set(list(hgt_map.keys()) + list(sgt_map.keys())))
            for date_val in all_dates[-days:]:
                hgt = hgt_map.get(date_val, 0.0)
                sgt = sgt_map.get(date_val, 0.0)
                out.append({
                    "date": date_val,
                    "hgt_yi": round(hgt, 2),
                    "sgt_yi": round(sgt, 2),
                    "total_yi": round(hgt + sgt, 2),
                })

            if out:
                logger.info("[akshare] 北向日级: %d 条", len(out))
                return out

        # fallback: 用 stock_hsgt_fund_flow_summary_em 获取最新一天
        summary = ak.stock_hsgt_fund_flow_summary_em()
        if summary is not None and not summary.empty:
            hgt_val = 0.0
            sgt_val = 0.0
            date_val = ""
            for _, row in summary.iterrows():
                board = str(row.get("板块", ""))
                direction = str(row.get("资金方向", ""))
                if direction == "北向":
                    date_val = str(row.get("交易日", ""))[:10]
                    net = row.get("成交净买额", 0)
                    if pd.notna(net):
                        if "沪" in board:
                            hgt_val = float(net)
                        elif "深" in board:
                            sgt_val = float(net)
            if date_val:
                out.append({
                    "date": date_val,
                    "hgt_yi": round(hgt_val, 2),
                    "sgt_yi": round(sgt_val, 2),
                    "total_yi": round(hgt_val + sgt_val, 2),
                })
                logger.info("[akshare] 北向日级(summary): %d 条", len(out))
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
        resp = urllib.request.urlopen(req, timeout=10)
        raw = resp.read()
        # hexin 返回的数据可能是 GBK 或含非法 UTF-8 字节，需容错解码
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("gbk", errors="replace")
        d = json.loads(text)
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
    except json.JSONDecodeError as e:
        logger.warning("[hexin] 北向日级JSON解析失败: %s", e)
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
        resp = urllib.request.urlopen(req, timeout=10)
        raw = resp.read()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("gbk", errors="replace")
        d = json.loads(text)
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


# ══════════════════════════════════════════════════════════════
#  大盘资金流向 + 行业资金流向
# ══════════════════════════════════════════════════════════════

# 统一金额单位: 所有金额字段以"元"为输出（各源自动换算）
# 统一输出字段: main_net / super_net / large_net / mid_net / small_net
# 数据源优先级: 新浪财经 → 腾讯财经（东财已排除）
# 大盘资金流: 通过新浪行业板块汇总计算（无单独大盘API）


def _ff_safe_float(v) -> float:
    """安全浮点转换，异常返回 0.0。"""
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return 0.0


# ── 新浪财经: 板块资金流 API ──
# 接口: vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow.ssl_bkzj_bk
# fenlei: 0=行业  1=概念  2=地域
# 返回字段: name, category, inamount(流入), outamount(流出), netamount(净额),
#           ratioamount(净占比), avg_changeratio(涨跌幅), turnover(换手率),
#           ts_symbol/ts_name/ts_trade(领涨股信息)

_SINA_FLOW_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php"
    "/MoneyFlow.ssl_bkzj_bk"
)


def _sina_fetch_sector_flow(fenlei: int = 0, num: int = 100) -> Optional[List[Dict[str, Any]]]:
    """从新浪获取板块资金流数据。

    Args:
        fenlei: 0=行业  1=概念  2=地域
        num: 获取板块数量

    Returns:
        成功: 原始列表 [{name, inamount, outamount, netamount, ratioamount, ...}]
        失败: None
    """
    import urllib.request

    url = f"{_SINA_FLOW_URL}?page=1&num={num}&sort=netamount&asc=0&fenlei={fenlei}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/117.0.0.0",
        "Referer": "https://finance.sina.com.cn/",
    }
    try:
        req = urllib.request.Request(url)
        for k, v in headers.items():
            req.add_header(k, v)
        raw = urllib.request.urlopen(req, timeout=15).read()
        data = json.loads(raw)
        if isinstance(data, list) and len(data) > 0:
            return data
        return None
    except Exception as e:
        logger.warning("[sina] 板块资金流失败(fenlei=%d): %s", fenlei, e)
        return None


def _sina_parse_sectors(raw_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """将新浪板块资金流原始数据解析为统一格式。

    Args:
        raw_list: 新浪 API 返回的原始列表

    Returns:
        统一格式列表
    """
    rows = []
    for item in raw_list:
        inamount = _ff_safe_float(item.get("inamount"))
        outamount = _ff_safe_float(item.get("outamount"))
        netamount = _ff_safe_float(item.get("netamount"))
        total = inamount + outamount
        rows.append({
            "name": item.get("name", ""),
            "code": item.get("category", ""),
            "change_pct": round(_ff_safe_float(item.get("avg_changeratio")) * 100, 2),
            "turnover": _ff_safe_float(item.get("turnover")),
            "in_net": inamount,
            "out_net": outamount,
            "main_net": netamount,
            "main_pct": round(netamount / total * 100, 2) if total > 0 else 0.0,
            "lead_stock": item.get("ts_name", ""),
            "lead_pct": round(_ff_safe_float(item.get("ts_changeratio")) * 100, 2),
        })
    return rows


# ── 大盘资金流向: 数据源实现 ──

def _mkt_flow_sina_realtime() -> Optional[Dict[str, Any]]:
    """数据源1: 新浪财经 — 行业板块汇总计算大盘资金流（实时）。

    原理: 新浪无单独大盘API，通过拉取全部行业板块资金流汇总得到沪深两市整体数据。
    48个行业板块的流入/流出/净额求和 = 大盘资金流。

    Returns:
        成功: {source, timestamp, main_net, main_pct, in_net, out_net,
               sectors_count, data:[...]}
        失败: None
    """
    raw = _sina_fetch_sector_flow(fenlei=0, num=100)
    if not raw:
        return None

    total_in = sum(_ff_safe_float(x.get("inamount")) for x in raw)
    total_out = sum(_ff_safe_float(x.get("outamount")) for x in raw)
    total_net = total_in - total_out
    total = total_in + total_out

    logger.info("[sina] 大盘实时资金流: %d 板块, 净流入 %.0f", len(raw), total_net)
    return {
        "source": "sina",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "main_net": total_net,
        "main_pct": round(total_net / total * 100, 2) if total > 0 else 0.0,
        "in_net": total_in,
        "out_net": total_out,
        "sectors_count": len(raw),
        "data": _sina_parse_sectors(raw),
    }


def _mkt_flow_tencent_realtime() -> Optional[Dict[str, Any]]:
    """数据源2: 腾讯财经 — 大盘资金流（实时）。

    腾讯无大盘聚合API，跳过。

    Returns:
        None
    """
    return None


def _ths_get_hexin_v() -> Optional[str]:
    """生成同花顺 hexin-v 加密 token。

    依赖 pywencai 库 (pip install pywencai)，
    内部通过执行 ths.js 的 v() 函数生成动态 token。

    Returns:
        成功: hexin-v token 字符串
        失败: None（pywencai 未安装或生成失败）
    """
    try:
        from pywencai.headers import get_token
        return get_token()
    except ImportError:
        logger.debug("[ths] pywencai 未安装, 跳过同花顺数据源")
        return None
    except Exception as e:
        logger.warning("[ths] hexin-v 生成失败: %s", e)
        return None


def _ths_fetch_sector_flow() -> Optional[List[Dict[str, Any]]]:
    """从同花顺获取行业板块资金流数据（全部页面）。

    接口: data.10jqka.com.cn/funds/hyzjl/field/tradezdf/order/desc/page/{n}/ajax/1/free/1/
    需要 hexin-v token + GBK 编码解析。

    Returns:
        成功: 原始 DataFrame 列表 [{行业, 涨跌幅, 流入资金(亿), 流出资金(亿), 净额(亿), 领涨股, ...}]
        失败: None
    """
    import urllib.request

    v_code = _ths_get_hexin_v()
    if not v_code:
        return None

    all_rows = []
    for page in range(1, 10):  # 最多 10 页，防止无限循环
        url = (
            f"http://data.10jqka.com.cn/funds/hyzjl/field/tradezdf"
            f"/order/desc/page/{page}/ajax/1/free/1/"
        )
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/117.0.0.0")
        req.add_header("Referer", "http://data.10jqka.com.cn/funds/hyzjl/")
        req.add_header("hexin-v", v_code)

        try:
            resp = urllib.request.urlopen(req, timeout=10)
            html = resp.read().decode("gbk", errors="replace")
        except Exception as e:
            logger.warning("[ths] 行业资金流第%d页失败: %s", page, e)
            break

        try:
            from bs4 import BeautifulSoup
            from io import StringIO
            import pandas as pd
            soup = BeautifulSoup(html, features="html.parser")
            tables = soup.find_all("table")
            if not tables:
                break
            df = pd.read_html(StringIO(html))[0]
            if df.empty:
                break
            all_rows.extend(df.to_dict(orient="records"))

            # 检查是否还有下一页
            page_info = soup.find(name="span", attrs={"class": "page_info"})
            if page_info:
                total_pages = int(page_info.text.split("/")[1])
                if page >= total_pages:
                    break
        except Exception as e:
            logger.warning("[ths] 解析第%d页失败: %s", page, e)
            break

    if all_rows:
        logger.info("[ths] 行业资金流: %d 个板块", len(all_rows))
    return all_rows if all_rows else None


def _mkt_flow_ths_realtime() -> Optional[Dict[str, Any]]:
    """数据源3: 同花顺 — 行业板块汇总计算大盘资金流（实时）。

    原理: 同花顺无单独大盘API，通过拉取全部行业板块资金流汇总得到。
    约 90 个行业板块。

    Returns:
        成功: {source, timestamp, main_net, main_pct, in_net, out_net,
               sectors_count, data:[...]}
        失败: None
    """
    import pandas as pd

    raw_list = _ths_fetch_sector_flow()
    if not raw_list:
        return None

    try:
        df = pd.DataFrame(raw_list)
        # 列名: 流入资金(亿), 流出资金(亿), 净额(亿)
        in_col = [c for c in df.columns if "流入" in str(c) and "资金" in str(c)]
        out_col = [c for c in df.columns if "流出" in str(c) and "资金" in str(c)]
        net_col = [c for c in df.columns if "净额" in str(c)]

        if not in_col or not out_col or not net_col:
            logger.warning("[ths] 列名不匹配: %s", list(df.columns))
            return None

        total_in = pd.to_numeric(df[in_col[0]], errors="coerce").sum()
        total_out = pd.to_numeric(df[out_col[0]], errors="coerce").sum()
        total_net = pd.to_numeric(df[net_col[0]], errors="coerce").sum()
        total = total_in + total_out

        logger.info("[ths] 大盘实时资金流: %d 板块, 净流入 %.0f", len(df), total_net * 1e8)
        return {
            "source": "ths",
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "main_net": round(total_net * 1e8, 2),  # 亿→元
            "main_pct": round(total_net / total * 100, 2) if total > 0 else 0.0,
            "in_net": round(total_in * 1e8, 2),
            "out_net": round(total_out * 1e8, 2),
            "sectors_count": len(df),
            "data": [],  # 大盘级别不返回板块明细
        }
    except Exception as e:
        logger.warning("[ths] 大盘资金流汇总失败: %s", e)
        return None


def _mkt_flow_eastmoney_realtime() -> Optional[Dict[str, Any]]:
    """数据源5(兜底): 东方财富 push2 分钟级大盘资金流向。

    接口: push2.eastmoney.com/api/qt/stock/fflow/kline/get
    secid=1.000001 代表上证指数（可近似代表大盘整体资金流）

    Returns:
        成功: {source, timestamp, main_net, in_net, out_net, points, data:[...]}
        失败: None
    """
    import urllib.request

    url = (
        "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get"
        "?secid=1.000001&klt=1&fields1=f1,f2,f3,f7"
        "&fields2=f51,f52,f53,f54,f55,f56,f57"
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/117.0.0.0",
        "Referer": "https://quote.eastmoney.com/",
    }
    try:
        req = urllib.request.Request(url)
        for k, v in headers.items():
            req.add_header(k, v)
        raw = urllib.request.urlopen(req, timeout=10).read()
        d = json.loads(raw)

        rows = []
        for line in d.get("data", {}).get("klines", []):
            p = line.split(",")
            if len(p) >= 6:
                rows.append({
                    "time": p[0],
                    "main_net": _ff_safe_float(p[1]),
                    "small_net": _ff_safe_float(p[2]),
                    "mid_net": _ff_safe_float(p[3]),
                    "large_net": _ff_safe_float(p[4]),
                    "super_net": _ff_safe_float(p[5]),
                })

        if not rows:
            return None

        main_net = sum(r["main_net"] for r in rows)
        logger.info("[eastmoney] 大盘实时资金流(兜底): %d 点, 主力 %.0f", len(rows), main_net)
        return {
            "source": "eastmoney",
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "main_net": main_net,
            "main_pct": 0.0,
            "in_net": 0.0,
            "out_net": 0.0,
            "points": len(rows),
            "data": rows[-10:],
        }
    except Exception as e:
        logger.warning("[eastmoney] 大盘实时资金流失败: %s", e)
        return None


def get_market_fund_flow_realtime() -> Dict[str, Any]:
    """获取实时大盘资金流向（沪深两市主力资金净流入/流出）。

    多源降级: 新浪(行业汇总) → 东财 push2(兜底)

    数据含义:
      - main_net = 主力净流入（新浪: 行业板块净额汇总; 东财: 超大单+大单）
      - main_pct = 主力净占比(%)
      - in_net / out_net = 总流入 / 总流出（仅新浪源有）
      - sectors_count = 行业板块数（仅新浪源有）

    Returns:
        成功: {
            source: "sina",
            timestamp: "2025-01-15 14:30:00",
            main_net: 21835000000.0,    # 主力净流入(元)
            main_pct: 1.34,             # 主力净占比(%)
            in_net: 825332000000.0,     # 总流入(元)
            out_net: 803496000000.0,    # 总流出(元)
            sectors_count: 48,          # 行业板块数
            data: [{name, code, change_pct, main_net, main_pct, ...}],
        }
        失败: {source: "none", error: "..."}

    Example:
        >>> flow = get_market_fund_flow_realtime()
        >>> if flow.get("main_net", 0) > 0:
        ...     print(f"主力净流入 {flow['main_net']/1e8:.2f} 亿")
    """
    for fetcher in (_mkt_flow_sina_realtime, _mkt_flow_tencent_realtime,
                    _mkt_flow_ths_realtime, _mkt_flow_eastmoney_realtime):
        data = fetcher()
        if data:
            return data

    logger.error("所有数据源获取大盘实时资金流均失败")
    return {"source": "none", "error": "所有数据源均失败"}


# ── 大盘资金流向日线: 数据源实现 ──

def _mkt_flow_daily_eastmoney(days: int = 120) -> Optional[List[Dict[str, Any]]]:
    """数据源5(兜底): 东方财富 push2his 大盘资金流日线。

    接口: push2his.eastmoney.com/api/qt/stock/fflow/kline/get
    secid=1.000001, klt=101 (日线级别)

    Returns:
        成功: [{date, main_net, main_pct, super_net, large_net, mid_net, small_net}]
        失败: None
    """
    import urllib.request

    url = (
        "https://push2his.eastmoney.com/api/qt/stock/fflow/kline/get"
        f"?secid=1.000001&klt=101&lmt={days}"
        "&fields1=f1,f2,f3,f7"
        "&fields2=f51,f52,f53,f54,f55,f56,f57"
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/117.0.0.0",
        "Referer": "https://data.eastmoney.com/",
    }
    try:
        req = urllib.request.Request(url)
        for k, v in headers.items():
            req.add_header(k, v)
        raw = urllib.request.urlopen(req, timeout=15).read()
        d = json.loads(raw)

        rows = []
        for line in d.get("data", {}).get("klines", []):
            p = line.split(",")
            if len(p) >= 6:
                main_net = _ff_safe_float(p[1])
                small_net = _ff_safe_float(p[2])
                mid_net = _ff_safe_float(p[3])
                large_net = _ff_safe_float(p[4])
                super_net = _ff_safe_float(p[5])
                total = abs(main_net) + abs(small_net) + abs(mid_net) + abs(large_net) + abs(super_net)
                rows.append({
                    "date": p[0],
                    "main_net": main_net,
                    "main_pct": round(main_net / total * 100, 2) if total > 0 else 0.0,
                    "super_net": super_net,
                    "large_net": large_net,
                    "mid_net": mid_net,
                    "small_net": small_net,
                })

        if rows:
            logger.info("[eastmoney] 大盘资金流日线(兜底): %d 条", len(rows))
        return rows or None
    except Exception as e:
        logger.warning("[eastmoney] 大盘资金流日线失败: %s", e)
        return None


def get_market_fund_flow_daily(days: int = 120) -> List[Dict[str, Any]]:
    """获取大盘资金流向日线数据（沪深两市每日主力资金净流入/流出）。

    多源降级: 东财 push2his(兜底)

    注意: 新浪/腾讯/同花顺/百度均无免费的大盘资金流日线API，
    目前仅东财 push2his 可用。如有其他数据源可补充。

    Args:
        days: 获取天数，默认 120

    Returns:
        成功: [{
            date: "2025-01-15",
            main_net: 1234567890.0,     # 主力净流入(元)
            main_pct: 3.45,             # 主力净占比(%)
            super_net: 800000000.0,     # 超大单净流入(元)
            large_net: 434567890.0,     # 大单净流入(元)
            mid_net: -500000000.0,      # 中单净流入(元)
            small_net: -734567890.0,    # 小单净流入(元)
        }, ...]
        失败: []

    Example:
        >>> data = get_market_fund_flow_daily(30)
        >>> for d in data[-5:]:
        ...     print(f"{d['date']}: 主力 {d['main_net']/1e8:.2f} 亿")
    """
    data = _mkt_flow_daily_eastmoney(days)
    if data:
        return data

    logger.error("所有数据源获取大盘资金流日线均失败")
    return []


# ── 行业资金流向: 数据源实现 ──

def _sector_flow_sina(indicator: str = "今日") -> Optional[List[Dict[str, Any]]]:
    """数据源1: 新浪财经 — 行业板块资金流向排名。

    接口: MoneyFlow.ssl_bkzj_bk, fenlei=0(行业)
    按净流入降序排列。

    注意: 新浪API仅返回当日数据，不支持"3日"/"5日"/"10日"统计周期。

    Args:
        indicator: "今日"（其他值会被忽略，仍返回当日数据）

    Returns:
        成功: [{name, code, change_pct, main_net, main_pct,
                in_net, out_net, turnover, lead_stock, lead_pct}, ...]
        失败: None
    """
    raw = _sina_fetch_sector_flow(fenlei=0, num=100)
    if not raw:
        return None

    rows = _sina_parse_sectors(raw)
    # 按净流入降序
    rows.sort(key=lambda x: x["main_net"], reverse=True)
    logger.info("[sina] 行业资金流: %d 个板块", len(rows))
    return rows


def _sector_flow_tencent(indicator: str = "今日") -> Optional[List[Dict[str, Any]]]:
    """数据源2: 腾讯财经 — 行业板块资金流向。

    腾讯无板块资金流聚合API，跳过。

    Returns:
        None
    """
    return None


def _sector_flow_ths(indicator: str = "今日") -> Optional[List[Dict[str, Any]]]:
    """数据源3: 同花顺 — 行业板块资金流向排名。

    接口: data.10jqka.com.cn/funds/hyzjl/
    需要 hexin-v token (pywencai 生成)。
    约 90 个行业板块，数据精确到亿。

    注意: 同花顺仅支持当日数据，不支持"3日"/"5日"/"10日"统计周期。

    Args:
        indicator: "今日"（其他值仍返回当日数据）

    Returns:
        成功: [{name, code, change_pct, main_net, main_pct,
                in_net, out_net, turnover, lead_stock, lead_pct}, ...]
        失败: None
    """
    import pandas as pd

    raw_list = _ths_fetch_sector_flow()
    if not raw_list:
        return None

    try:
        df = pd.DataFrame(raw_list)

        # 列名映射
        name_col = [c for c in df.columns if "行业" in str(c) and "指数" not in str(c)]
        in_col = [c for c in df.columns if "流入" in str(c) and "资金" in str(c)]
        out_col = [c for c in df.columns if "流出" in str(c) and "资金" in str(c)]
        net_col = [c for c in df.columns if "净额" in str(c)]
        chg_col = [c for c in df.columns if c == "涨跌幅"]
        lead_col = [c for c in df.columns if "领涨" in str(c)]

        if not name_col or not net_col:
            logger.warning("[ths] 行业资金流列名不匹配: %s", list(df.columns))
            return None

        rows = []
        for _, row in df.iterrows():
            in_val = float(pd.to_numeric(row[in_col[0]], errors="coerce")) if in_col else 0
            out_val = float(pd.to_numeric(row[out_col[0]], errors="coerce")) if out_col else 0
            net_val = float(pd.to_numeric(row[net_col[0]], errors="coerce")) if net_col else 0
            total = in_val + out_val

            chg_str = str(row[chg_col[0]]) if chg_col else "0"
            chg_str = chg_str.replace("%", "")
            try:
                chg_val = float(chg_str)
            except ValueError:
                chg_val = 0.0

            rows.append({
                "name": str(row[name_col[0]]),
                "code": "",
                "change_pct": chg_val,
                "main_net": round(net_val * 1e8, 2),  # 亿→元
                "main_pct": round(net_val / total * 100, 2) if total > 0 else 0.0,
                "in_net": round(in_val * 1e8, 2),
                "out_net": round(out_val * 1e8, 2),
                "turnover": 0.0,
                "lead_stock": str(row[lead_col[0]]) if lead_col else "",
                "lead_pct": 0.0,
            })

        # 按净流入降序
        rows.sort(key=lambda x: x["main_net"], reverse=True)
        logger.info("[ths] 行业资金流: %d 个板块", len(rows))
        return rows
    except Exception as e:
        logger.warning("[ths] 行业资金流解析失败: %s", e)
        return None


def _sector_flow_eastmoney(indicator: str = "今日") -> Optional[List[Dict[str, Any]]]:
    """数据源5(兜底): 东方财富 push2 行业板块资金流向排名。

    接口: push2.eastmoney.com/api/qt/clist/get
    行业板块 fid=f62(主力净流入) 降序

    Args:
        indicator: "今日"/"3日"/"5日"/"10日"（东财支持多周期）

    Returns:
        成功: [{name, code, change_pct, main_net, main_pct,
                super_net, large_net, mid_net, small_net,
                lead_stock, lead_pct}, ...]
        失败: None
    """
    import urllib.request

    fields = "f12,f14,f3,f62,f184,f66,f69,f72,f75,f204,f205"
    url = (
        "https://push2.eastmoney.com/api/qt/clist/get"
        f"?fid=f62&po=1&pz=100&pn=1&np=1&fs=m:90+t:2"
        f"&fields={fields}"
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/117.0.0.0",
        "Referer": "https://data.eastmoney.com/",
    }
    try:
        req = urllib.request.Request(url)
        for k, v in headers.items():
            req.add_header(k, v)
        raw = urllib.request.urlopen(req, timeout=15).read()
        d = json.loads(raw)

        rows = []
        for item in d.get("data", {}).get("diff", []):
            if not isinstance(item, dict):
                continue
            main_net = _ff_safe_float(item.get("f62"))
            super_net = _ff_safe_float(item.get("f66"))
            large_net = _ff_safe_float(item.get("f69"))
            mid_net = _ff_safe_float(item.get("f72"))
            small_net = _ff_safe_float(item.get("f75"))
            total = abs(main_net) + abs(super_net) + abs(large_net) + abs(mid_net) + abs(small_net)
            rows.append({
                "name": item.get("f14", ""),
                "code": item.get("f12", ""),
                "change_pct": _ff_safe_float(item.get("f3")),
                "main_net": main_net,
                "main_pct": round(main_net / total * 100, 2) if total > 0 else 0.0,
                "super_net": super_net,
                "large_net": large_net,
                "mid_net": mid_net,
                "small_net": small_net,
                "in_net": 0.0,
                "out_net": 0.0,
                "turnover": 0.0,
                "lead_stock": item.get("f204", ""),
                "lead_pct": _ff_safe_float(item.get("f205")),
            })

        if rows:
            logger.info("[eastmoney] 行业资金流(兜底): %d 个板块", len(rows))
        return rows or None
    except Exception as e:
        logger.warning("[eastmoney] 行业资金流失败: %s", e)
        return None


def get_sector_fund_flow(indicator: str = "今日") -> List[Dict[str, Any]]:
    """获取行业板块资金流向排名。

    多源降级: 新浪 → 东财(兜底)

    注意: 新浪仅支持当日数据；"3日"/"5日"/"10日"需走东财兜底。

    Args:
        indicator: 统计周期，可选 "今日"(默认) / "3日" / "5日" / "10日"

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
    if indicator not in ("今日", "3日", "5日", "10日"):
        logger.warning("不支持的统计周期: %s，使用'今日'", indicator)
        indicator = "今日"

    # 新浪仅支持当日，非"今日"时跳过新浪直接走同花顺/东财
    if indicator == "今日":
        for fetcher in (_sector_flow_sina, _sector_flow_tencent, _sector_flow_ths):
            data = fetcher(indicator)
            if data:
                return data

    # 兜底: 东财（支持多周期）
    data = _sector_flow_eastmoney(indicator)
    if data:
        return data

    logger.error("所有数据源获取行业资金流均失败")
    return []

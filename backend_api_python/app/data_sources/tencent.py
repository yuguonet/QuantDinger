"""
Tencent market data helpers (no API key).

Provides:
- Quote: https://qt.gtimg.cn/q=sh600519 / sz000001 / hk00700
- Kline: https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=CODE,PERIOD,,,COUNT,ADJ

This is used as a stable alternative when Yahoo/yfinance gets rate-limited.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests

from app.data_sources.rate_limiter import get_request_headers, retry_with_backoff, get_tencent_limiter
from app.utils.logger import get_logger

logger = get_logger(__name__)


def normalize_cn_code(symbol: str) -> str:
    """
    Normalize A-share symbol to Tencent code: sh600519 / sz000001 / bj830799.

    前缀规则：
      沪市 (sh): 600/601/603/605/688/900
      深市 (sz): 000/001/002/003/300/200
      北证 (bj): 43/82/83/87/88

    Accepts: 600519 / 600519.SH / 600519.SS / 000001.SZ / 830799.BJ
    """
    s = (symbol or "").strip().upper()
    if not s:
        return s
    # 已带后缀 → 剥离并直接加前缀
    if s.endswith(".SH") or s.endswith(".SS"):
        return "SH" + s[:-3]
    if s.endswith(".SZ"):
        return "SZ" + s[:-3]
    if s.endswith(".BJ"):
        return "BJ" + s[:-3]

    if s.isdigit() and len(s) == 6:
        # 沪市：60x / 688 / 689 / 900
        if s.startswith(("600", "601", "603", "605", "688", "689", "900")):
            return "SH" + s
        # 深市：00x / 300 / 301 / 200
        if s.startswith(("000", "001", "002", "003", "300", "301", "200")):
            return "SZ" + s
        # 北证：43 / 82 / 83 / 87 / 88
        if s.startswith(("43", "82", "83", "87", "88")):
            return "BJ" + s

    return s


def normalize_hk_code(symbol: str) -> str:
    """
    Normalize HK stock symbol to Tencent code: hk00700 (5 digits).
    Accepts:
    - 700 / 0700 / 00700.HK / 0700.HK
    """
    s = (symbol or "").strip().upper()
    if not s:
        return s
    if s.endswith(".HK"):
        s = s[:-3]
    if s.isdigit():
        return "HK" + s.zfill(5)
    # If user already passed HKxxxxx
    if s.startswith("HK") and s[2:].isdigit():
        return "HK" + s[2:].zfill(5)
    return s


def _lower_code(code: str) -> str:
    return (code or "").strip().lower()


@retry_with_backoff(max_attempts=3, base_delay=1.2, max_delay=8.0, exceptions=(Exception,))
def fetch_quote(code: str, timeout: int = 8) -> Optional[List[str]]:
    """
    Returns the raw '~' split array from qt.gtimg.cn, or None.
    """
    c = _lower_code(code)
    if not c:
        return None

    limiter = get_tencent_limiter()
    limiter.wait()
    url = f"https://qt.gtimg.cn/q={c}"
    resp = requests.get(url, headers=get_request_headers(referer="https://qt.gtimg.cn/"), timeout=timeout)
    # Tencent quote is often GBK encoded
    try:
        resp.encoding = "gbk"
    except Exception:
        pass

    text = (resp.text or "").strip()
    if not text or "~" not in text:
        return None

    # Format: v_sh600519="1~NAME~CODE~LAST~PREV~OPEN~..."
    try:
        start = text.index('="') + 2
        end = text.rindex('"')
        payload = text[start:end]
    except Exception:
        return None

    parts = payload.split("~")
    return parts if len(parts) > 5 else None


def fetch_quotes_batch(codes: List[str], timeout: int = 10) -> Dict[str, List[str]]:
    """
    批量获取多只股票的实时行情（一个 HTTP 请求）。

    腾讯行情 API 支持逗号拼接多只代码：
      http://qt.gtimg.cn/q=sh600000,sh600001,sh600002,...

    返回: {code: [~分割的字段列表]} — 仅包含有效数据的 code
    """
    if not codes:
        return {}
    lowered = [_lower_code(c) for c in codes if c]
    if not lowered:
        return {}

    # 分批，每批最多 500 只（腾讯支持）
    batch_size = 500
    result: Dict[str, List[str]] = {}

    for i in range(0, len(lowered), batch_size):
        batch = lowered[i:i + batch_size]
        query = ",".join(batch)

        limiter = get_tencent_limiter()
        limiter.wait()
        url = f"https://qt.gtimg.cn/q={query}"
        try:
            resp = requests.get(url, headers=get_request_headers(referer="https://qt.gtimg.cn/"), timeout=timeout)
            resp.encoding = "gbk"
        except Exception as e:
            logger.warning(f"[腾讯批量行情] 请求失败: {e}")
            continue

        # 每行一只股票: v_sh600519="1~贵州茅台~600519~1750.00~..."
        for line in (resp.text or "").strip().split("\n"):
            line = line.strip().rstrip(";")
            if "=" not in line or '""' in line:
                continue
            try:
                var_name, data = line.split("=", 1)
                data = data.strip('"')
                parts = data.split("~")
                if len(parts) > 5 and parts[1] and parts[2]:
                    # 从变量名提取原始 code（v_sh600519 → sh600519）
                    for c in batch:
                        if c in var_name:
                            result[c] = parts
                            break
            except Exception:
                continue

    return result


def parse_quote_to_ticker(parts: List[str]) -> Dict[str, Any]:
    """
    Best-effort conversion to a unified ticker dict.
    """
    def _f(i: int, default: float = 0.0) -> float:
        try:
            v = parts[i]
            if v is None or v == "":
                return default
            return float(v)
        except Exception:
            return default

    name = (parts[1] or "").strip() if len(parts) > 1 else ""
    symbol = (parts[2] or "").strip() if len(parts) > 2 else ""
    last_ = _f(3, 0.0)
    prev = _f(4, 0.0)
    open_ = _f(5, 0.0)

    change = round(last_ - prev, 4) if prev else 0.0
    change_pct = round(change / prev * 100, 2) if prev else 0.0

    # Indices are not fully consistent across markets; keep conservative.
    high = _f(33, last_) if len(parts) > 33 else last_
    low = _f(34, last_) if len(parts) > 34 else last_

    return {
        "symbol": symbol,
        "name": name,
        "last": last_,
        "change": change,
        "changePercent": change_pct,
        "high": high,
        "low": low,
        "open": open_ or last_,
        "previousClose": prev,
        "raw": parts,
    }


def parse_tencent_kline_time(ds: str) -> Optional[int]:
    """Parse Tencent fqkline first column to Unix seconds (local parse, matches prior chart behavior)."""
    raw = str(ds or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return int(datetime.strptime(raw, fmt).timestamp())
        except ValueError:
            continue
    try:
        ts = int(float(raw))
        if ts > 10**12:
            ts = int(ts / 1000)
        return ts
    except Exception:
        return None


def tencent_kline_rows_to_dicts(rows: List[Any]) -> List[Dict[str, Any]]:
    """Convert raw fqkline rows to chart dicts; ignores corporate-action tail objects on HK rows."""
    out: List[Dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, (list, tuple)) or len(r) < 6:
            continue
        ts = parse_tencent_kline_time(r[0])
        if ts is None:
            continue
        try:
            o, c, h, low, vol = float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])
        except (TypeError, ValueError):
            continue
        out.append(
            {
                "time": ts,
                "open": round(o, 4),
                "high": round(h, 4),
                "low": round(low, 4),
                "close": round(c, 4),
                "volume": round(vol, 2),
            }
        )
    return out


@retry_with_backoff(max_attempts=3, base_delay=1.2, max_delay=8.0, exceptions=(Exception,))
def fetch_kline(code: str, period: str, count: int = 300, adj: str = "qfq", timeout: int = 10) -> List[List[str]]:
    """
    Fetch kline arrays from Tencent.

    period examples:
    - day, week, month (supported by Tencent fqkline)

    Note: Minute periods (m1/m5/…) return **bad params** on this endpoint; use fetch_minute_kline instead.
    """
    c = _lower_code(code)
    if not c:
        return []

    limiter = get_tencent_limiter()
    limiter.wait()

    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    params = {"param": f"{c},{period},,,{int(count)},{adj}"}
    resp = requests.get(url, headers=get_request_headers(referer="https://gu.qq.com/"), params=params, timeout=timeout)
    data = resp.json() if resp.text else {}
    if not isinstance(data, dict):
        return []
    try:
        if int(data.get("code", 0)) != 0:
            return []
    except (ValueError, TypeError):
        return []
    root = (data.get("data") or {}).get(c)
    if not isinstance(root, dict):
        return []

    # Data key variants:
    # - A-share: qfqday / qfqweek / qfqm1 ...
    # - HK: day / week / m1 ...
    candidates = []
    if adj:
        candidates.append(f"{adj}{period}")
    candidates.append(period)

    for key in candidates:
        arr = root.get(key)
        if isinstance(arr, list) and arr:
            return arr

    # Fallback: search any key that endswith period and is a list
    for k, v in root.items():
        if isinstance(v, list) and v and str(k).lower().endswith(str(period).lower()):
            return v
    return []


# ================================================================
# 腾讯分钟K线 (mkline 接口)
# ================================================================

# 内部周期 → 腾讯 mkline 参数
_TF_TO_MKLINE = {
    "1m": "m1",
    "5m": "m5",
    "15m": "m15",
    "30m": "m30",
    "1H": "m60",
}


@retry_with_backoff(max_attempts=3, base_delay=1.2, max_delay=8.0, exceptions=(Exception,))
def fetch_minute_kline(
    code: str,
    timeframe: str,
    count: int = 300,
    timeout: int = 10,
) -> List[List[Any]]:
    """
    腾讯分钟K线 — proxy.finance.qq.com mkline 接口。

    支持周期: m1 / m5 / m15 / m30 / m60
    数据格式: [时间字符串, 开盘, 收盘, 最高, 最低, 成交量, {}, 振幅]

    Args:
        code: 腾讯格式代码，如 sh600519 / sz000001
        timeframe: 内部周期 1m/5m/15m/30m/1H
        count: 请求条数
    """
    c = _lower_code(code)
    if not c:
        return []

    mk_period = _TF_TO_MKLINE.get(timeframe)
    if not mk_period:
        logger.warning(f"[腾讯分钟K线] 不支持的周期: {timeframe}")
        return []

    limiter = get_tencent_limiter()
    limiter.wait()

    url = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/kline/mkline"
    params = {"param": f"{c},{mk_period},{int(count)}"}
    resp = requests.get(
        url,
        headers=get_request_headers(referer="https://gu.qq.com/"),
        params=params,
        timeout=timeout,
    )

    try:
        data = resp.json()
    except Exception:
        return []

    if not isinstance(data, dict):
        return []
    try:
        if int(data.get("code", 0)) != 0:
            return []
    except (ValueError, TypeError):
        return []

    root = (data.get("data") or {}).get(c)
    if not isinstance(root, dict):
        return []

    rows = root.get(mk_period)
    if not isinstance(rows, list):
        return []

    return rows


def tencent_minute_kline_to_dicts(rows: List[List[Any]]) -> List[Dict[str, Any]]:
    """
    腾讯分钟K线原始行 → 标准 dict 列表。

    原始格式: [时间字符串, 开盘, 收盘, 最高, 最低, 成交量, {}, 振幅]
    """
    out: List[Dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, (list, tuple)) or len(r) < 6:
            continue
        ts = parse_tencent_kline_time(r[0])
        if ts is None:
            continue
        try:
            o = float(r[1])
            c = float(r[2])
            h = float(r[3])
            low = float(r[4])
            vol = float(r[5])
        except (TypeError, ValueError):
            continue
        out.append({
            "time": ts,
            "open": round(o, 4),
            "high": round(h, 4),
            "low": round(low, 4),
            "close": round(c, 4),
            "volume": round(vol, 2),
        })
    return out


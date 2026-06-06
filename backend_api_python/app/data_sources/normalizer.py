"""
A股数据标准化层 — 工具函数 + safe 类型转换

提供:
  - safe_float / safe_int: 安全类型转换（处理 None/"-"/""/"nan"/"--"）
  - normalize_cn_code / normalize_hk_code: 股票代码格式归一
  - to_raw_digits / detect_market / add_market_prefix / strip_market_prefix: 市场前缀工具
  - Tencent 行情 helpers (fetch_quote / parse_quote_to_ticker)

注: 龙虎榜/涨跌停池/热榜的 normalize 函数已迁移至 app.market_cn.dragon_limit
"""

from __future__ import annotations


def _sf(v, default=0.0) -> float:
    """safe float"""
    if v is None or v == "-" or v == "" or str(v).strip() == "nan":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _si(v, default=0) -> int:
    """safe int"""
    if v is None or v == "-" or v == "":
        return default
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _ss(v, default="") -> str:
    """safe str"""
    if v is None:
        return default
    return str(v).strip()

"""
Tencent market data helpers (no API key).

Provides:
- Quote: https://qt.gtimg.cn/q=sh600519 / sz000001 / hk00700
- Kline: https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=CODE,PERIOD,,,COUNT,ADJ

This is used as a stable alternative when Yahoo/yfinance gets rate-limited.
"""

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


# NOTE: 龙虎榜/热榜/涨跌停池的 normalize 函数已迁移至 app.market_cn.dragon_limit
# NOTE: safe_float / safe_int 别名保留在文件末尾


# ================================================================
# 股票代码工具函数 — 供 Provider 层使用
# ================================================================

def to_raw_digits(symbol: str) -> str:
    """
    从各种格式的股票代码中提取纯 6 位数字。

    支持格式:
      600519 / SH600519 / 600519.SH / 600519.SS / sh600519 / SZ000001 / 000001.SZ

    Returns:
        6 位数字字符串，无法识别返回 ""
    """
    s = (symbol or "").strip().upper()
    if not s:
        return ""
    # 去掉常见前缀/后缀
    for prefix in ("SH", "SZ", "BJ"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    for suffix in (".SH", ".SS", ".SZ", ".BJ"):
        if s.endswith(suffix):
            s = s[:-len(suffix)]
            break
    s = s.strip()
    if s.isdigit() and len(s) == 6:
        return s
    return ""


def detect_market(symbol: str) -> tuple:
    """
    识别股票代码所属市场。

    Returns:
        (market, digits) — 如 ("SH", "600519") / ("SZ", "000001") / ("BJ", "830799")
        无法识别返回 ("", "")
    """
    s = (symbol or "").strip().upper()
    if not s:
        return ("", "")

    # 已带后缀
    if s.endswith(".SH") or s.endswith(".SS"):
        return ("SH", s[:-3].strip())
    if s.endswith(".SZ"):
        return ("SZ", s[:-3].strip())
    if s.endswith(".BJ"):
        return ("BJ", s[:-3].strip())

    # 已带前缀
    for prefix in ("SH", "SZ", "BJ"):
        if s.startswith(prefix):
            digits = s[len(prefix):]
            if digits.isdigit() and len(digits) == 6:
                return (prefix, digits)

    # 纯 6 位数字，按规则推断
    if s.isdigit() and len(s) == 6:
        if s.startswith(("600", "601", "603", "605", "688", "689", "900")):
            return ("SH", s)
        if s.startswith(("000", "001", "002", "003", "300", "301", "200")):
            return ("SZ", s)
        if s.startswith(("43", "82", "83", "87", "88")):
            return ("BJ", s)

    return ("", "")


def add_market_prefix(symbol: str, market: str = "") -> str:
    """
    给股票代码添加市场前缀，防止重复添加。

    已有合法前缀 → 原样返回（不重复加）
    纯数字 → 按规则推断市场并加前缀
    带后缀 (.SH/.SZ/.HK) → 剥离后缀再加前缀

    Args:
        symbol: 股票代码（任意格式）
        market:  "CNStock" / "HKStock"，为空时自动推断

    Returns:
        大写带前缀代码: SH600519 / SZ000001 / HK00700
        无法识别返回原值
    """
    s = (symbol or "").strip()
    if not s:
        return s
    upper = s.upper()

    # ── 已有合法前缀，直接返回（防重复） ──
    for prefix in ("SH", "SZ", "BJ"):
        if upper.startswith(prefix):
            digits = upper[len(prefix):]
            if digits.isdigit() and len(digits) == 6:
                return upper  # 已有前缀，原样返回
    if upper.startswith("HK"):
        digits = upper[2:]
        if digits.isdigit() and len(digits) == 5:
            return "HK" + digits.zfill(5)

    # ── 带后缀 → 剥离后加前缀 ──
    for suffix in (".SH", ".SS", ".SZ", ".BJ", ".HK"):
        if upper.endswith(suffix):
            core = upper[:-len(suffix)]
            if suffix in (".SH", ".SS"):
                return "SH" + core
            if suffix == ".SZ":
                return "SZ" + core
            if suffix == ".BJ":
                return "BJ" + core
            if suffix == ".HK":
                return "HK" + core.zfill(5)

    # ── 纯数字 → 按市场推断 ──
    digits = upper
    if digits.isdigit():
        is_hk = market == "HKStock" or (not market and len(digits) <= 5)
        if is_hk:
            return "HK" + digits.zfill(5)
        if len(digits) == 6:
            if digits.startswith(("600", "601", "603", "605", "688", "689", "900")):
                return "SH" + digits
            if digits.startswith(("000", "001", "002", "003", "300", "301", "200")):
                return "SZ" + digits
            if digits.startswith(("43", "82", "83", "87", "88")):
                return "BJ" + digits

    return s  # 无法识别，原样返回


def strip_market_prefix(symbol: str) -> str:
    """
    去掉股票代码的市场前缀，返回纯数字代码。防止错误除去。

    已知前缀 (SH/SZ/BJ/HK) → 验证数字长度后去掉
    已知后缀 (.SH/.SZ/.HK) → 验证数字长度后去掉
    纯数字 → 直接返回
    无法识别 → 返回原值（不乱剥）

    Returns:
        纯数字代码: 600519 / 000001 / 00700
        无法识别返回原值
    """
    s = (symbol or "").strip()
    if not s:
        return s
    upper = s.upper()

    # ── 前缀 ──
    for prefix in ("SH", "SZ", "BJ"):
        if upper.startswith(prefix):
            digits = s[len(prefix):]
            if digits.isdigit() and len(digits) == 6:
                return digits
            return s  # 前缀后不是 6 位数字，不剥
    if upper.startswith("HK"):
        digits = s[2:]
        if digits.isdigit() and len(digits) == 5:
            return digits
        return s

    # ── 后缀 ──
    for suffix in (".SH", ".SS", ".SZ", ".BJ", ".HK"):
        if upper.endswith(suffix):
            digits = s[:-len(suffix)]
            if digits.isdigit() and len(digits) in (5, 6):
                return digits
            return s

    # ── 纯数字 ──
    if s.isdigit() and len(s) in (5, 6):
        return s

    return s  # 无法识别，原样返回


# ================================================================
# 公开别名 — 供外部模块统一引用
# ================================================================
safe_float = _sf
safe_int = _si

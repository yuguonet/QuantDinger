# -*- coding: utf-8 -*-
"""
股票代码标准化模块 — 统一市场判断 + 各数据源格式转换

模块职责:
    提供股票代码的统一解析和格式转换能力。核心函数 detect_market() 将任意格式的
    股票代码解析为 (market, digits) 标准二元组，各数据源 Provider 只需调用对应转换
    函数即可获得目标格式，消除重复的市场判断逻辑。

设计原理:
    - 唯一真相源 (Single Source of Truth): 所有市场判断逻辑集中在 detect_market()
    - 无状态纯函数: 所有函数均为无副作用的纯函数，线程安全
    - 防御性输入: safe_float/safe_int 处理市场数据中常见的 "-" / "" / None 异常值

在架构中的位置:
    数据源层 — 被所有 Provider 和缓存层依赖，是最底层的工具模块

关键依赖:
    无外部依赖，仅使用 Python 标准库

支持输入格式:
    600519 / 600519.SH / sh600519 / SZ000001 / 830799.BJ
"""

from typing import Any, Tuple


# ================================================================
# 安全类型转换 — 从旧架构迁移，市场数据解析常用
# ================================================================

def safe_float(v: Any, default: float = 0.0) -> float:
    """
    安全转 float，处理市场数据中常见的异常值。

    市场数据源（东财、新浪、腾讯等）返回的数值字段经常包含 "-"、""、None
    等非数值标记，直接 float() 转换会抛异常。此函数提供安全降级。

    Args:
        v: 待转换的值，可以是任意类型
        default: 转换失败时的默认值

    Returns:
        转换后的 float 值，失败时返回 default

    Examples:
        >>> safe_float("12.34")    # 正常转换
        12.34
        >>> safe_float("-")        # 数据源的空值标记
        0.0
        >>> safe_float(None)       # API 返回的空值
        0.0
        >>> safe_float("abc", -1)  # 非数值字符串
        -1
    """
    if v is None or v == "-" or v == "":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def safe_int(v: Any, default: int = 0) -> int:
    """
    安全转 int，处理市场数据中常见的异常值。

    先转 float 再转 int，兼容 "12.0" 这类字符串。
    市场数据中的成交量、笔数等字段常用此函数。

    Args:
        v: 待转换的值，可以是任意类型
        default: 转换失败时的默认值

    Returns:
        转换后的 int 值，失败时返回 default

    Examples:
        >>> safe_int("123")       # 正常转换
        123
        >>> safe_int("12.0")      # 浮点字符串 → int
        12
        >>> safe_int("-")         # 数据源空值标记
        0
        >>> safe_int(None)
        0
    """
    if v is None or v == "-" or v == "":
        return default
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def detect_market(code: str) -> Tuple[str, str]:
    """
    统一市场判断 — 将任意格式的股票代码解析为标准 (market, digits) 二元组。

    判断优先级:
        1. 带后缀格式 (600519.SH) → 直接提取后缀作为市场
        2. 带前缀格式 (SH600519) → 直接提取前缀作为市场
        3. 纯数字格式 (600519) → 根据代码段规则推断市场：
           - 沪市: 60x主板、688/689科创板、900 B股、51x ETF、11x可转债
           - 深市: 00x主板、30x创业板、200 B股、15x/16x/18x基金、12x可转债
           - 北证: 43/82/83/87/88 开头

    Args:
        code: 任意格式的股票代码（支持带前缀/后缀/纯数字）

    Returns:
        (market, digits) 二元组：
        - market: 'SH'/'SZ'/'BJ'/'' （空字符串表示无法识别）
        - digits: 纯6位数字代码

    Examples:
        detect_market('600519')      → ('SH', '600519')
        detect_market('sh600519')    → ('SH', '600519')
        detect_market('600519.SH')   → ('SH', '600519')
        detect_market('SZ000001')    → ('SZ', '000001')
        detect_market('830799.BJ')   → ('BJ', '830799')
        detect_market('UNKNOWN')     → ('', 'UNKNOWN')
    """
    s = (code or "").strip().upper()
    if not s:
        return "", ""

    # 1) 带后缀: 600519.SH / 600519.SS / 600519.SZ / 830799.BJ
    for suffix in (".SH", ".SS", ".SZ", ".BJ"):
        if s.endswith(suffix):
            return s[-2:], s[:-3]

    # 2) 带前缀: SH600519 / SZ000001 / BJ830799
    if s.startswith(("SH", "SZ", "BJ")) and len(s) >= 3:
        rest = s[2:]
        if rest.isdigit() and len(rest) == 6:
            return s[:2], rest

    # 3) 纯6位数字: 600519 / 000001 / 830799 / 510050
    if s.isdigit() and len(s) == 6:
        # 沪市: 主板60x, 科创板688/689, B股900, ETF51x, 可转债110/113/118
        if s.startswith(("600", "601", "603", "605", "688", "689", "900",
                         "510", "511", "512", "513", "515", "516", "518", "519",
                         "110", "113", "118")):
            return "SH", s
        # 深市: 主板00x, 创业板300/301, B股200, 基金15/16/18, 可转债127/128
        if s.startswith(("000", "001", "002", "003", "300", "301", "200",
                         "150", "159", "160", "161", "162", "163", "164", "165",
                         "166", "167", "168", "169",
                         "180", "184", "185", "186", "187", "188", "189",
                         "127", "128")):
            return "SZ", s
        # 北证: 43/82/83/87/88
        if s.startswith(("43", "82", "83", "87", "88")):
            return "BJ", s

    return "", s


# ================================================================
# 各数据源格式转换
# ================================================================

def to_tencent_code(code: str) -> str:
    """
    转换为腾讯实时行情 API 格式: sh600519 / sz000001 / bj830799。

    腾讯/新浪的实时行情接口使用小写市场前缀 + 6位数字的格式。

    Args:
        code: 任意格式的股票代码

    Returns:
        腾讯格式代码，无法识别时返回小写原始输入

    Examples:
        to_tencent_code('600519')    → 'sh600519'
        to_tencent_code('SH600519')  → 'sh600519'
        to_tencent_code('600519.SH') → 'sh600519'
    """
    market, digits = detect_market(code)
    if market and digits:
        return f"{market.lower()}{digits}"
    return (code or "").strip().lower()


def to_sina_code(code: str) -> str:
    """
    转换为新浪行情 API 格式: sh600519 / sz000001 / bj830799。

    新浪格式与腾讯完全相同（小写前缀 + 数字），直接复用 to_tencent_code()。

    Args:
        code: 任意格式的股票代码

    Returns:
        新浪格式代码
    """
    return to_tencent_code(code)


def to_eastmoney_secid(code: str) -> str:
    """
    转换为东财 secid 格式: 1.600519 / 0.000001 / 0.830799。

    东财 API 使用 "市场ID.数字代码" 格式：
    - 沪市(含科创板) → market_id=1
    - 深市(含创业板) → market_id=0
    - 北证 → market_id=0（与深市共用）

    Args:
        code: 任意格式的股票代码

    Returns:
        东财 secid 格式字符串，无法识别时返回空字符串

    Examples:
        to_eastmoney_secid('600519')    → '1.600519'
        to_eastmoney_secid('000001')    → '0.000001'
        to_eastmoney_secid('830799')    → '0.830799'
        to_eastmoney_secid('UNKNOWN')   → ''
    """
    market, digits = detect_market(code)
    if not market or not digits:
        return ""
    if market == "SH":
        return f"1.{digits}"
    # SZ / BJ
    return f"0.{digits}"


def to_raw_digits(code: str) -> str:
    """
    提取纯6位数字代码 — 剥离所有前缀/后缀。

    适用于需要纯数字代码的场景，如东财 API 的 filter 参数、缓存键等。

    Args:
        code: 任意格式的股票代码

    Returns:
        6位纯数字代码字符串

    Examples:
        to_raw_digits('SH600519')  → '600519'
        to_raw_digits('000001.SZ') → '000001'
        to_raw_digits('600519')    → '600519'
    """
    _, digits = detect_market(code)
    return digits


def to_canonical(code: str) -> str:
    """
    转换为标准格式: SH600519 / SZ000001 / BJ830799。

    标准格式用于内部缓存键、日志输出等需要统一格式的场景。

    Args:
        code: 任意格式的股票代码

    Returns:
        标准格式代码，无法识别时返回大写原始输入

    Examples:
        to_canonical('600519')    → 'SH600519'
        to_canonical('sh600519')  → 'SH600519'
        to_canonical('600519.SH') → 'SH600519'
    """
    market, digits = detect_market(code)
    if market and digits:
        return f"{market}{digits}"
    return (code or "").strip().upper()


# ================================================================
# 港股代码标准化
# ================================================================

def normalize_hk_code(symbol: str) -> str:
    """
    港股代码标准化: 补齐为 HK + 5位数字格式。

    港股代码长度不固定（1-5位），统一补齐为5位便于后续处理。

    Args:
        symbol: 港股代码，支持任意格式（700 / 0700 / 00700.HK / HK700）

    Returns:
        标准化的港股代码 (HK00700 格式)

    Examples:
        normalize_hk_code('700')      → 'HK00700'
        normalize_hk_code('00700.HK') → 'HK00700'
        normalize_hk_code('HK700')    → 'HK00700'
    """
    s = (symbol or "").strip().upper()
    if not s:
        return s
    if s.endswith(".HK"):
        s = s[:-3]
    if s.isdigit():
        return "HK" + s.zfill(5)
    if s.startswith("HK") and s[2:].isdigit():
        return "HK" + s[2:].zfill(5)
    return s

"""
A股数据标准化层 — 工具函数 + safe 类型转换

提供:
  - safe_float / safe_int: 安全类型转换（处理 None/"-"/""/"nan"/"--"）
  - to_number: 数值化（剥千分位逗号 + 缺失保持 None，全库唯一实现）
  - normalize_cn_code / normalize_hk_code: 股票代码格式归一
  - to_raw_digits / detect_market / add_market_prefix / strip_market_prefix: 市场前缀工具
  - normalize_chart_timeframe: 图表周期归一化（全库唯一实现，2026-10-16 迁入）
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


def to_number(v):
    """数值化：剥千分位逗号 → float；缺失返回 None（**不回落 0**）。

    与 `safe_float` 不可互换，差别有两点：
      - 缺失语义：`safe_float` 回落 `default`（多为 0.0）；本函数保持 None
        —— null≠0，把缺失当 0 会虚构"零余额 / 零营收"。
      - 千分位：`safe_float("9,227,807.21")` → 0.0（转不动）；本函数先剥逗号再转。

    千分位来源：新浪三表页面（vFD_BalanceSheet / vFD_ProfitStatement）的金额
    单元格是带逗号的字符串，`pandas.read_html` 按 object 原样保留。

    Examples:
        to_number("9,227,807.21") → 9227807.21
        to_number(None) → None ; to_number("--") → None ; to_number("abc") → None
    """
    import math

    if v is None:
        return None
    if isinstance(v, str):
        v = v.replace(",", "").strip()
        if not v or v.lower() in ("nan", "none", "-"):
            return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f

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
        if s.startswith(("43", "82", "83", "87", "88", "92")):
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


def _strip_name_suffix(s: str) -> str:
    for sfx in ("-UW", "-U", "-W"):
        if s.endswith(sfx) and len(s) > len(sfx) + 1:
            return s[:-len(sfx)]
    return s


def normalize_stock_name(name: str) -> str:
    """A 股简称归一化：全角→半角、去空白、剥除权除息/新股标记与同股不同权后缀。

    三个 stock_basic 源原样给名：新浪/akshare 是「万  科Ａ」「金 螳 螂」，
    akshare 还带 `XD`/`C` 前缀与 `-U`/`-UW` 后缀（当日行情状态，非简称本体），
    巨潮 zwjc 则是干净简称。统一到巨潮口径后三源才能互相替换。
    """
    s = "".join(chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E else c
                for c in str(name or ""))
    s = "".join(s.split())
    for pfx in ("XD", "XR", "DR"):
        if s.startswith(pfx) and len(s) > len(pfx) + 1:
            return _strip_name_suffix(s[len(pfx):])
    if len(s) > 2 and s[:1] in ("N", "C"):
        s = s[1:]
    return _strip_name_suffix(s)


def build_stock_basic_row(code: str, name: str) -> dict:
    """stock_basic 单行 —— 对齐库表 stock_basic_info 的业务列（9 列）。

    远端清单接口只给代码与简称：`market_cn` 由代码推导，其余详情列（industry /
    concepts / list_date / total_shares / circ_shares）该接口不提供，取 DDL 默认值
    —— 落库侧对这几列用 COALESCE/NULLIF，空值不会冲掉库内已补充的详情。
    """
    pure = strip_market_prefix(str(code or ""))
    return {
        "code": pure,
        "name": normalize_stock_name(name),
        "market_cn": detect_market(pure)[0],
        "industry": "",
        "concepts": "",
        "list_date": "",
        "total_shares": 0.0,
        "circ_shares": 0.0,
        "status": "active",
    }


# ================================================================
# 图表周期归一化 — 全库唯一实现
# (2026-10-16 从 asia_stock_kline 迁入并合并 kline_clean 别名表;
#  asia_stock_kline 已删本地副本, cn_stock 改从这里 import)
# ================================================================

# 别名表: key = 小写输入, value = 规范输出
# 规范口径: 分钟族小写 (1m/5m/15m/30m) / 小时族大写 (1H/2H/4H) / 日周 (1D/1W)
# 易错点: "2h"→"2H" 是历史漏注册项 — 旧 asia_stock_kline 表无此键,
# "2h" 输入原样返回小写, 导致 cn_stock 聚合分支 tuple 匹配失败走远端
_CHART_TF_ALIASES = {
    # 分钟族 (小写规范)
    "1m": "1m", "m": "1m", "min": "1m", "1min": "1m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    # 小时族 (大写规范)
    "1h": "1H", "h": "1H", "60m": "1H", "60min": "1H",
    "2h": "2H", "120m": "2H", "120min": "2H",
    "4h": "4H", "240m": "4H", "240min": "4H",
    # 日线
    "1d": "1D", "d": "1D", "day": "1D", "daily": "1D", "1day": "1D",
    # 周线
    "1w": "1W", "w": "1W", "week": "1W", "weekly": "1W", "wk": "1W", "1week": "1W",
}


def normalize_chart_timeframe(timeframe: str) -> str:
    """
    归一化图表周期字符串为规范口径。

    规范输出:
      分钟族  1m / 5m / 15m / 30m   (小写)
      小时族  1H / 2H / 4H          (大写)
      日/周   1D / 1W               (大写)

    未登记输入 → 原样返回 (保留原始大小写); 空值 → "1D"。

    Examples:
        normalize_chart_timeframe("60m")   → "1H"
        normalize_chart_timeframe("2h")    → "2H"   # 旧表漏注册, 曾原样返回 "2h"
        normalize_chart_timeframe("240m")  → "4H"
        normalize_chart_timeframe("1DAY")  → "1D"
        normalize_chart_timeframe("3m")    → "3m"   # 未登记, 原样返回
    """
    t = (timeframe or "1D").strip()
    if not t:
        return "1D"
    key = t.lower()
    if key in _CHART_TF_ALIASES:
        return _CHART_TF_ALIASES[key]
    return t


# ================================================================
# 公开别名 — 供外部模块统一引用
# ================================================================
safe_float = _sf
safe_int = _si


# ================================================================
# 指数代码解析 — provider_index 全族唯一入口
# ================================================================
# 背景（2026-10-28，依据 1.log 九个指数日线全灭）：
#   同一个 index_kline kind 的入参历史上有三种形式 ——
#     index_daily 传裸码 "000001"；cn_stock_ext 传 "000001.SH"；
#   而 tencent / sina / akshare / baostock 四个 provider 各自 `code[:3]` 判前缀后
#   **直接拼接**，拿到 "000001.SH" 就拼成 "sh000001.SH" / "sh.000001.SH"（非法）：
#     akshare  → KeyError: 'date'
#     tencent  → HTTP 501 / 无 data 节点
#     baostock → 库内报「股票代码应为9位」
#   收口成本函数：provider 只从这里取 (纯代码, 交易所)，不再各自推断。

# 沪市指数前缀（000xxx 系列：000001 上证 / 000300 沪深300 / 000905 中证500 …）
_INDEX_SH_PREFIXES = ("000", "88", "99")


def split_index_code(code: str) -> tuple:
    """指数代码 → (纯 6 位代码, 交易所 "SH"/"SZ")。

    接受三种写法，与 provider 既有前缀规则保持一致：
        "000001"    → ("000001", "SH")   裸码按指数规则推断
        "000001.SH" → ("000001", "SH")   带后缀直接采信（不覆盖）
        "sh000001"  → ("000001", "SH")   带前缀直接采信（不覆盖）
        "399001"    → ("399001", "SZ")

    指数前缀规则：000/88/99 → SH，其余（399/899…）→ SZ。
    后缀/前缀无法识别时交易所回空串，纯代码原样返回，由 provider 决定是否支持。

    Examples:
        split_index_code("000300")    → ("000300", "SH")
        split_index_code("399006.SZ") → ("399006", "SZ")
        split_index_code("sh000688")  → ("000688", "SH")
    """
    s = (code or "").strip()
    if not s:
        return "", ""

    # 带后缀：000001.SH / 000001.SS / 399001.SZ / 899050.BJ
    if "." in s:
        base, _, sfx = s.partition(".")
        base, sfx = base.strip(), sfx.strip().upper()
        if sfx in ("SH", "SS"):
            return base, "SH"
        return base, sfx

    # 带前缀：sh000001 / SZ399001
    if s[:2].upper() in ("SH", "SZ") and s[2:].isdigit():
        return s[2:], s[:2].upper()

    # 裸码
    if s.isdigit():
        return s, ("SH" if s[:3] in _INDEX_SH_PREFIXES else "SZ")
    return s, ""


# ================================================================
# 指数/ETF 代码识别 — 供行情接口区分市场前缀
# ================================================================

# 上交所指数代码 (000xxx 中的指数)
_SH_INDEX_CODES = {
    "000001", "000016", "000300", "000510", "000688", "000852", "000905", "000906",
}

# 上交所 ETF 前缀
_SH_ETF_PREFIXES = ("510", "511", "512", "513", "515", "516", "517", "518", "519")

# 深交所 ETF 前缀
_SZ_ETF_PREFIXES = ("159",)


def is_index_or_etf(code: str) -> bool:
    """判断代码是否为指数或 ETF（而非普通个股）。"""
    c = strip_market_prefix(code)
    if not c.isdigit() or len(c) != 6:
        return False
    return (
        c in _SH_INDEX_CODES
        or c.startswith(_SH_ETF_PREFIXES)
        or c.startswith(_SZ_ETF_PREFIXES)
        or c.startswith("399")  # 深证指数
    )


def market_prefix_for_quote(code: str) -> str:
    """
    为行情接口推断市场前缀，正确处理指数/ETF。

    与 add_market_prefix 的区别:
      - add_market_prefix("000300") → "SZ000300" (按个股规则，错误)
      - market_prefix_for_quote("000300") → "SH000300" (识别为上证指数，正确)

    Returns:
        带前缀代码: SH000300 / SZ399001 / SZ000002 / SH510050
    """
    c = strip_market_prefix(code)
    if not c.isdigit() or len(c) != 6:
        return add_market_prefix(code)  # 无法识别，走默认

    # 上证指数
    if c in _SH_INDEX_CODES:
        return "SH" + c
    # 上交所 ETF
    if c.startswith(_SH_ETF_PREFIXES):
        return "SH" + c
    # 深交所 ETF
    if c.startswith(_SZ_ETF_PREFIXES):
        return "SZ" + c
    # 深证指数
    if c.startswith("399"):
        return "SZ" + c

    # 普通个股，走默认规则
    return add_market_prefix(code)

"""
市场类型自动推断
根据交易对/股票代码的格式推断所属市场类型

用途：
    - 防止 A 股代码误入加密货币数据源（如 000690 → CCXT）
    - 前端未传 market 或传错时自动纠正
    - 符号搜索时自动归类

用法：
    from app.data_sources.market_detector import detect_market, validate_market
    market = detect_market("000690")       # → "CNStock"
    validate_market("CNStock", "000690")   # → True
    validate_market("Crypto", "000690")    # → False
"""
from typing import Optional
import re


# ================================================================
# 推断规则（按优先级从高到低）
# ================================================================

# A 股：6 位纯数字，常见前缀 0/3/6/8
_CN_STOCK_RE = re.compile(r'^[03689]\d{5}$')

# 港股：4-5 位数字，或带 .HK 后缀
_HK_STOCK_RE_NUM = re.compile(r'^\d{4,5}$')
_HK_STOCK_RE_SUFFIX = re.compile(r'^\d{4,5}\.HK$', re.IGNORECASE)

# 外汇：6 字母（如 EURUSD）或带 / 分隔（如 EUR/USD），或贵金属代号
_FOREX_6LETTER = re.compile(r'^[A-Z]{6}$')
_FOREX_SLASH = re.compile(r'^[A-Z]{3}/[A-Z]{3}$')
_PRECIOUS_METALS = {'XAUUSD', 'XAGUSD', 'XAU', 'XAG'}

# 期货：常见期货代码
_FUTURES_CODES = {
    'GC', 'SI', 'CL', 'NG', 'HG', 'PA', 'PL',  # 贵金属/能源
    'ZC', 'ZW', 'ZS', 'ZL', 'ZM',               # 农产品
    'ES', 'NQ', 'YM', 'RTY',                     # 股指
    '6E', '6J', '6B', '6A', '6C', '6S',          # 外汇期货
    'FDAX', 'FESX', 'FTSE',                       # 欧洲期货
}

# 加密货币特征：包含 / 分隔符（BTC/USDT）或常见后缀
_CRYPTO_SUFFIXES = (
    'USDT', 'BUSD', 'USDC', 'DAI',   # 稳定币
    'BTC', 'ETH', 'BNB',              # 主流币对
)

# 美股：1-5 个字母（AAPL, MSFT, BRK.A）
_US_STOCK_RE = re.compile(r'^[A-Z]{1,5}(\.[A-Z])?$')


def detect_market(symbol: str) -> Optional[str]:
    """
    根据符号格式推断市场类型

    Args:
        symbol: 交易对或股票代码

    Returns:
        推断的市场类型，无法推断时返回 None
    """
    if not symbol or not symbol.strip():
        return None

    s = symbol.strip().upper()

    # 去掉空格和常见前缀
    s = s.replace(' ', '')

    # ---- 港股 ----
    if _HK_STOCK_RE_SUFFIX.match(s):
        return 'HKStock'
    # 纯数字 4-5 位 → 可能是港股也可能是 A 股，通过位数区分
    # A 股一定是 6 位，港股 4-5 位

    # ---- 外汇（优先级高于加密货币，因为 EUR/USD 也有 / ）----
    if _FOREX_SLASH.match(s):
        base, quote = s.split('/')
        # 3字母/3字母 → 外汇
        if len(base) == 3 and len(quote) == 3:
            return 'Forex'
    if _FOREX_6LETTER.match(s) and s in _PRECIOUS_METALS or _FOREX_6LETTER.match(s):
        # 排除加密货币后缀
        if not any(s.endswith(suffix) for suffix in _CRYPTO_SUFFIXES):
            if s in _PRECIOUS_METALS:
                return 'Forex'
            # 6 字母但不是常见外汇对 → 可能是外汇
            # 常见外汇对列表
            _COMMON_FOREX = {
                'EURUSD', 'GBPUSD', 'USDJPY', 'AUDUSD', 'USDCAD',
                'USDCHF', 'NZDUSD', 'EURJPY', 'GBPJPY', 'EURGBP',
            }
            if s in _COMMON_FOREX:
                return 'Forex'

    # ---- A 股（6 位数字）----
    if _CN_STOCK_RE.match(s):
        return 'CNStock'

    # ---- 港股（纯数字 4-5 位）----
    if _HK_STOCK_RE_NUM.match(s):
        return 'HKStock'

    # ---- 加密货币 ----
    # 格式：BTC/USDT, BTCUSDT, ETHUSDT
    if '/' in s:
        parts = s.split('/')
        if len(parts) == 2 and parts[0] and parts[1]:
            base, quote = parts
            # 防止 A 股代码被误判（如 000690/USDT）
            if _CN_STOCK_RE.match(base):
                return 'CNStock'
            # 排除外汇（已在上面处理）
            if not (len(base) == 3 and len(quote) == 3):
                return 'Crypto'
            # 3/3 但 quote 是稳定币 → 加密货币
            if quote in ('USDT', 'BUSD', 'USDC'):
                return 'Crypto'
    if any(s.endswith(suffix) for suffix in _CRYPTO_SUFFIXES):
        # 提取 base 部分
        base = s
        for suffix in _CRYPTO_SUFFIXES:
            if s.endswith(suffix):
                base = s[:-len(suffix)]
                break
        # 防止 A 股代码被误判（如 000690USDT）
        if base and _CN_STOCK_RE.match(base):
            return 'CNStock'
        if base and not base.isdigit():
            return 'Crypto'

    # ---- 期货 ----
    if s in _FUTURES_CODES:
        return 'Futures'

    # ---- 美股（兜底：1-5 字母）----
    if _US_STOCK_RE.match(s):
        return 'USStock'

    return None


def validate_market(declared_market: str, symbol: str) -> bool:
    """
    验证声明的市场类型是否与符号匹配

    Args:
        declared_market: 前端传入的市场类型
        symbol: 交易对或股票代码

    Returns:
        True = 匹配，False = 不匹配（可能需要纠正）
    """
    if not declared_market or not symbol:
        return True  # 缺失时不做校验

    inferred = detect_market(symbol)
    if inferred is None:
        return True  # 无法推断时放行

    return inferred == declared_market


def safe_market(declared_market: str, symbol: str) -> str:
    """
    返回安全的市场类型：
    - 如果推断结果与声明一致 → 使用声明值
    - 如果推断结果不同 → 以推断结果为准（纠正）
    - 如果无法推断 → 使用声明值

    Args:
        declared_market: 前端传入的市场类型
        symbol: 交易对或股票代码

    Returns:
        校正后的市场类型
    """
    if not declared_market:
        return detect_market(symbol) or ''

    inferred = detect_market(symbol)
    if inferred and inferred != declared_market:
        return inferred

    return declared_market

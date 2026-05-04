"""
Symbol/company name resolver for local-only mode.

Goal:
- When a symbol is not present in our seed list, try to resolve a human-readable name
  from public data sources, then persist it into watchlist records.

Notes:
- For US stocks we use Finnhub (if configured) or yfinance.
- For Crypto/Forex/Futures we provide best-effort fallbacks.
"""

from __future__ import annotations

from typing import Optional

import re
import os

import requests

from app.utils.logger import get_logger
from app.data.market_symbols_seed import get_symbol_name as seed_get_symbol_name
from app.data_sources.normalizer import normalize_cn_code, normalize_hk_code

logger = get_logger(__name__)


def fetch_quote(code: str, timeout: int = 8) -> Optional[list]:
    """
    通过腾讯 qt.gtimg.cn 接口获取单只股票/指数实时行情。

    返回 ``~`` 分隔的原始字段列表（list[str]），调用方按索引取值。
    失败时返回 ``None``。

    Args:
        code:    腾讯格式代码，如 ``sh600519`` / ``sz000001`` / ``hk00700``
        timeout: 请求超时秒数
    """
    c = (code or "").strip().lower()
    if not c:
        return None

    try:
        resp = requests.get(
            f"https://qt.gtimg.cn/q={c}",
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/120.0.0.0 Safari/537.36",
                "Referer": "https://finance.qq.com",
            },
            timeout=timeout,
        )
        try:
            resp.encoding = "gbk"
        except Exception:
            pass

        text = (resp.text or "").strip()
        if not text or "~" not in text:
            return None

        start = text.index('="') + 2
        end = text.rindex('"')
        parts = text[start:end].split("~")

        if len(parts) < 5:
            return None

        return parts
    except Exception as e:
        logger.debug(f"fetch_quote({code}) failed: {e}")
        return None


def _normalize_symbol_for_market(market: str, symbol: str) -> str:
    m = (market or '').strip()
    s = (symbol or '').strip().upper()
    if m == 'CNStock':
        return normalize_cn_code(s)
    if m == 'HKStock':
        return normalize_hk_code(s)
    return s


def _resolve_name_from_yfinance(symbol: str) -> Optional[str]:
    """
    Best-effort company name via yfinance.
    """
    def _try_one(sym: str) -> Optional[str]:
        import yfinance as yf
        t = yf.Ticker(sym)
        info = getattr(t, "info", None)
        if not isinstance(info, dict) or not info:
            return None
        name = (info.get('longName') or info.get('shortName') or '').strip()
        return name if name else None
    try:
        # yfinance uses '-' for some tickers (e.g. BRK-B) while users may input 'BRK.B'
        out = _try_one(symbol)
        if out:
            return out
        if '.' in symbol:
            out = _try_one(symbol.replace('.', '-'))
            if out:
                return out
        return None
    except Exception as e:
        logger.debug(f"yfinance name resolve failed: {symbol}: {e}")
        return None


def _resolve_name_from_finnhub(symbol: str) -> Optional[str]:
    """
    Finnhub company profile (requires FINNHUB_API_KEY).
    https://finnhub.io/docs/api/company-profile2
    """
    try:
        api_key = (os.getenv('FINNHUB_API_KEY') or '').strip()
        if not api_key:
            return None
        url = "https://finnhub.io/api/v1/stock/profile2"
        resp = requests.get(url, params={"symbol": symbol, "token": api_key}, timeout=8)
        if resp.status_code != 200:
            return None
        data = resp.json() if resp.text else {}
        if not isinstance(data, dict) or not data:
            return None
        name = (data.get("name") or data.get("ticker") or '').strip()
        return name if name else None
    except Exception as e:
        logger.debug(f"Finnhub name resolve failed: {symbol}: {e}")
        return None


def resolve_symbol_name(market: str, symbol: str) -> Optional[str]:
    """
    Resolve a display name for a symbol.
    Priority:
    1) Seed mapping (fast, offline)
    2) Market-specific public sources
    3) Reasonable fallback (None)
    """
    m = (market or '').strip()
    s = _normalize_symbol_for_market(m, symbol)
    if not m or not s:
        return None

    # 1) Seed
    seed = seed_get_symbol_name(m, s)
    if seed:
        return seed

    # 2) Market-specific
    if m == 'USStock':
        # Prefer Finnhub if configured (more stable for company name),
        # otherwise fall back to yfinance.
        return _resolve_name_from_finnhub(s) or _resolve_name_from_yfinance(s)

    # CN/HK stocks: try Tencent quote name first (no key), then yfinance best-effort.
    if m in ('CNStock', 'HKStock'):
        try:
            parts = fetch_quote(s)
            if parts and len(parts) > 1 and parts[1]:
                return str(parts[1]).strip()
        except Exception:
            pass
        return _resolve_name_from_yfinance(s)

    # Crypto: at least return base ticker-like display (not a "company", but better than empty)
    if m == 'Crypto':
        if '/' in s:
            base = s.split('/')[0].strip()
            return base if base else None
        return s

    # Forex: keep as-is (e.g. EURUSD) – you can later replace with a nicer mapping if needed.
    if m == 'Forex':
        return s

    # Futures: keep as-is
    if m == 'Futures':
        return s

    return None

# -*- coding: utf-8 -*-
"""
Shared agent utilities — market detection, code parsing, etc.
"""
from __future__ import annotations


def detect_market(stock_code: str) -> str:
    """Detect market type from stock code.

    Returns one of: CNStock, HKStock, Forex, Crypto
    """
    code = (stock_code or "").strip().upper()
    if not code:
        return "CNStock"

    # Explicit exchange prefixes
    if code.startswith(("SH", "SZ", "BJ")):
        return "CNStock"
    if code.startswith("HK"):
        return "HKStock"

    # Chinese A-share: 6-digit numeric (SH 6xxxxx/9xxxxx, SZ 0xxxxx/3xxxxx)
    if len(code) == 6 and code.isdigit():
        return "CNStock"

    # Known crypto patterns (check before Forex to catch things like OPUSDT)
    _CRYPTO_PREFIXES = (
        "BTC", "ETH", "BNB", "SOL", "XRP", "DOGE", "ADA", "DOT",
        "AVAX", "MATIC", "LINK", "UNI", "LTC", "ATOM", "FIL",
        "ARB", "OP", "APT", "SUI", "PEPE", "SHIB", "TRX",
    )
    _CRYPTO_SUFFIXES = ("USDT", "USDC", "BUSD", "BTC", "ETH")
    if any(code.startswith(p) for p in _CRYPTO_PREFIXES):
        return "Crypto"
    if any(code.endswith(s) for s in _CRYPTO_SUFFIXES) and not code.isalpha():
        return "Crypto"

    # Forex: exactly 6 alphabetic characters (e.g. EURUSD, GBPJPY)
    if len(code) == 6 and code.isalpha():
        return "Forex"

    return "CNStock"

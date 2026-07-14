"""工具函数模块"""

from .logger import get_logger
from .json_parser import safe_parse_json
from .prompt_loader import load_prompt
from .tracing import AgentTraceRecorder


def detect_market(stock_code: str) -> str:
    """Detect market type from stock code.
    Returns one of: CNStock, HKStock, Forex, Crypto
    """
    code = (stock_code or "").strip().upper()
    if not code:
        return "CNStock"
    if code.startswith(("SH", "SZ", "BJ")):
        return "CNStock"
    if code.startswith("HK"):
        return "HKStock"
    if len(code) == 6 and code.isdigit():
        return "CNStock"
    _CRYPTO = ("BTC", "ETH", "BNB", "SOL", "XRP", "DOGE", "ADA", "DOT",
               "AVAX", "MATIC", "LINK", "UNI", "LTC", "ATOM", "FIL")
    _CRYPTO_SFX = ("USDT", "USDC", "BUSD", "BTC", "ETH")
    if any(code.startswith(p) for p in _CRYPTO):
        return "Crypto"
    if any(code.endswith(s) for s in _CRYPTO_SFX) and not code.isalpha():
        return "Crypto"
    if len(code) == 6 and code.isalpha():
        return "Forex"
    return "CNStock"


__all__ = ["get_logger", "safe_parse_json", "extract_json", "extract_decision", "load_prompt", "AgentTraceRecorder", "detect_market"]

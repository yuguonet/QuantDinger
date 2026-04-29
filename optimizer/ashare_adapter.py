"""
A 股交易规则适配器
只负责交易规则约束（T+1、涨跌停、最小交易单位），
数据获取统一走 DataSourceFactory（market="CNStock"）。
"""
from datetime import datetime
from typing import Optional


# ============================================================
# A 股交易规则
# ============================================================

class AShareRules:
    """A 股交易规则约束"""

    # 涨跌停幅度
    PRICE_LIMITS = {
        "main": 0.10,       # 主板 10%
        "gem": 0.20,        # 创业板 20%
        "star": 0.20,       # 科创板 20%
        "bj": 0.30,         # 北交所 30%
        "st": 0.05,         # ST 股 5%
    }

    # 最小交易单位
    MIN_LOT = 100  # 1 手 = 100 股

    @staticmethod
    def get_board(symbol: str) -> str:
        """根据股票代码判断板块"""
        code = symbol.split(".")[0] if "." in symbol else symbol
        if code.startswith("3"):
            return "gem"       # 创业板
        elif code.startswith("68"):
            return "star"      # 科创板
        elif code.startswith(("00", "60")):
            return "main"      # 主板
        elif code.startswith(("8", "4")):
            return "bj"        # 北交所
        return "main"

    @classmethod
    def get_price_limit(cls, symbol: str) -> float:
        """获取涨跌停幅度"""
        board = cls.get_board(symbol)
        return cls.PRICE_LIMITS.get(board, 0.10)

    @staticmethod
    def round_lot(quantity: int) -> int:
        """取整到最小交易单位（100 股）"""
        return max(100, (quantity // 100) * 100)

    @staticmethod
    def is_t1_sellable(buy_date: datetime, current_date: datetime) -> bool:
        """T+1 检查：买入当天不能卖出"""
        return current_date.date() > buy_date.date()


# ============================================================
# A 股回测约束适配器
# ============================================================

class AShareBacktestAdapter:
    """
    A 股回测适配器
    对交易信号施加 A 股规则约束（T+1、涨跌停、最小交易单位）
    """

    def __init__(self):
        self.rules = AShareRules()

    def apply_constraints(
        self,
        signal: dict,
        current_price: float,
        symbol: str,
        current_date: datetime,
        buy_date: Optional[datetime] = None,
        position: int = 0,
    ) -> dict:
        """
        对交易信号施加 A 股约束

        Args:
            signal: 原始交易信号 {"action": "buy"/"sell", "quantity": 1000, ...}
            current_price: 当前价格
            symbol: 股票代码
            current_date: 当前日期
            buy_date: 买入日期（T+1 检查用）
            position: 当前持仓

        Returns:
            修正后的交易信号
        """
        action = signal.get("action", "hold")
        quantity = signal.get("quantity", 0)

        # 涨跌停检查
        price_limit = self.rules.get_price_limit(symbol)

        # T+1 检查（卖出时）
        if action == "sell" and buy_date:
            if not self.rules.is_t1_sellable(buy_date, current_date):
                return {"action": "hold", "reason": "T+1 限制，买入当天不能卖出"}

        # 最小交易单位
        if action == "buy":
            quantity = self.rules.round_lot(quantity)

        # 仓位检查
        if action == "sell":
            quantity = min(quantity, position)

        return {
            "action": action,
            "quantity": quantity,
            "price_limit": price_limit,
        }


# ============================================================
# 标的格式转换
# ============================================================

def normalize_symbol(symbol: str) -> str:
    """
    统一 A 股标的格式

    输入格式：
      - "000001"       → "000001.SZ"
      - "000001.SZ"    → "000001.SZ"
      - "600000.SH"    → "600000.SH"
      - "sh600000"     → "600000.SH"

    输出格式: "XXXXXX.SZ" 或 "XXXXXX.SH"
    """
    symbol = symbol.strip().upper()

    # 已经是标准格式
    if "." in symbol and symbol.endswith((".SZ", ".SH")):
        return symbol

    # sh600000 / sz000001
    if symbol.startswith(("SH", "SZ")):
        code = symbol[2:]
        suffix = ".SH" if symbol.startswith("SH") else ".SZ"
        return f"{code}{suffix}"

    # 纯数字
    code = symbol.split(".")[0]
    if code.startswith("6"):
        return f"{code}.SH"
    else:
        return f"{code}.SZ"


def parse_market_symbol(market_symbol: str) -> tuple:
    """
    解析 "A_SHARE:000001.SZ" 或 "CNStock:000001.SZ" 格式

    Returns:
        (market, symbol) — market 统一为 "CNStock"（DataSourceFactory 识别的格式）
    """
    if ":" in market_symbol:
        parts = market_symbol.split(":", 1)
        raw_market = parts[0].strip()
        symbol = parts[1].strip()

        # 统一市场名为 DataSourceFactory 能识别的格式
        market_map = {
            "A_SHARE": "CNStock",
            "ASHARE": "CNStock",
            "A": "CNStock",
            "CN": "CNStock",
            "CHINA": "CNStock",
            "CNSTOCK": "CNStock",
        }
        market = market_map.get(raw_market.upper(), raw_market)
        return market, symbol

    # 没有市场前缀，根据代码推断
    code = market_symbol.strip()
    if code.startswith(("0", "3", "6", "8")) and len(code.split(".")[0]) == 6:
        return "CNStock", normalize_symbol(code)

    return "Crypto", code


def get_ashare_commission() -> float:
    """A 股佣金费率（含印花税，双边）"""
    return 0.0015  # 万 15


def get_ashare_initial_capital() -> float:
    """A 股默认初始资金"""
    return 100000.0  # 10 万

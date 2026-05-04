"""
A股数据标准化层 — 统一不同数据源的字段命名和数据格式

不同数据源返回的字段名、格式、精度都不同:
- 东财: stock_code, stock_name, trade_date, buy_amount ...
- AkShare: 代码, 名称, 上榜日, 龙虎榜买入额 ...
- 新浪: code, name, changepercent ...

标准化层确保:
  1. 所有数据源返回统一的字段名
  2. 类型安全 (float/int/str)
  3. 缺失字段有合理默认值
  4. 数据校验 (合理性检查)
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


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


# ================================================================
# 龙虎榜
# ================================================================

DRAGON_TIGER_SCHEMA = {
    "stock_code": str,        # 股票代码 6位
    "stock_name": str,        # 股票名称
    "trade_date": str,        # 上榜日期 YYYY-MM-DD
    "reason": str,            # 上榜原因
    "buy_amount": float,      # 买入金额 (元)
    "sell_amount": float,     # 卖出金额 (元)
    "net_amount": float,      # 净买入额 (元)
    "change_percent": float,  # 涨跌幅 %
    "close_price": float,     # 收盘价
    "turnover_rate": float,   # 换手率 %
    "amount": float,          # 成交额 (元)
    "buy_seat_count": int,    # 买入席位数
    "sell_seat_count": int,   # 卖出席位数
}


def normalize_dragon_tiger(raw: Dict[str, Any], source: str = "") -> Dict[str, Any]:
    """
    标准化一条龙虎榜记录。

    支持的源字段映射:
    - eastmoney: 直接使用标准字段
    - akshare: 代码/名称/上榜日/解读 → stock_code/stock_name/trade_date/reason
    """
    if source == "akshare" or "代码" in raw:
        return {
            "stock_code": _ss(raw.get("代码", raw.get("code", raw.get("stock_code")))),
            "stock_name": _ss(raw.get("名称", raw.get("name", raw.get("stock_name")))),
            "trade_date": _ss(raw.get("上榜日", raw.get("trade_date", raw.get("stock_code", ""))))[:10],
            "reason": _ss(raw.get("解读", raw.get("reason", raw.get("EXPLANATION"))))[:100],
            "buy_amount": _sf(raw.get("龙虎榜买入额", raw.get("buy_amount", raw.get("BUY")))),
            "sell_amount": _sf(raw.get("龙虎榜卖出额", raw.get("sell_amount", raw.get("SELL")))),
            "net_amount": _sf(raw.get("龙虎榜净额", raw.get("net_amount", raw.get("NET_BUY")))),
            "change_percent": _sf(raw.get("涨跌幅", raw.get("change_percent", raw.get("CHANGE_RATE")))),
            "close_price": _sf(raw.get("收盘价", raw.get("close_price", raw.get("CLOSE_PRICE")))),
            "turnover_rate": _sf(raw.get("换手率", raw.get("turnover_rate", raw.get("TURNOVERRATE")))),
            "amount": _sf(raw.get("成交额", raw.get("amount", raw.get("ACCUM_AMOUNT")))),
            "buy_seat_count": _si(raw.get("买入席位数", raw.get("buy_seat_count", raw.get("BUYER_NUM", 0)))),
            "sell_seat_count": _si(raw.get("卖出席位数", raw.get("sell_seat_count", raw.get("SELLER_NUM", 0)))),
        }

    # eastmoney / default: 直接取标准字段
    return {
        "stock_code": _ss(raw.get("stock_code", raw.get("SECURITY_CODE"))),
        "stock_name": _ss(raw.get("stock_name", raw.get("SECURITY_NAME_ABBR"))),
        "trade_date": _ss(raw.get("trade_date", raw.get("TRADE_DATE", "")))[:10],
        "reason": _ss(raw.get("reason", raw.get("EXPLANATION"))),
        "buy_amount": _sf(raw.get("buy_amount", raw.get("BUY"))),
        "sell_amount": _sf(raw.get("sell_amount", raw.get("SELL"))),
        "net_amount": _sf(raw.get("net_amount", raw.get("NET_BUY"))),
        "change_percent": _sf(raw.get("change_percent", raw.get("CHANGE_RATE"))),
        "close_price": _sf(raw.get("close_price", raw.get("CLOSE_PRICE"))),
        "turnover_rate": _sf(raw.get("turnover_rate", raw.get("TURNOVERRATE"))),
        "amount": _sf(raw.get("amount", raw.get("ACCUM_AMOUNT"))),
        "buy_seat_count": _si(raw.get("buy_seat_count", raw.get("BUYER_NUM", 0))),
        "sell_seat_count": _si(raw.get("sell_seat_count", raw.get("SELLER_NUM", 0))),
    }


def normalize_dragon_tiger_list(raw_list: List[Dict], source: str = "") -> List[Dict[str, Any]]:
    """批量标准化龙虎榜数据"""
    return [normalize_dragon_tiger(r, source) for r in raw_list if isinstance(r, dict)]


# ================================================================
# 热榜
# ================================================================

HOT_RANK_SCHEMA = {
    "rank": int,              # 排名
    "stock_code": str,        # 股票代码
    "stock_name": str,        # 股票名称
    "price": float,           # 最新价
    "change_percent": float,  # 涨跌幅 %
    "popularity_score": float,  # 人气分数
    "current_rank_change": str, # 排名变化
}


def normalize_hot_rank(raw: Dict[str, Any], source: str = "") -> Dict[str, Any]:
    """标准化一条热榜记录"""
    if source == "akshare" or "股票代码" in raw:
        return {
            "rank": _si(raw.get("当前排名", raw.get("rank", raw.get("RANK")))),
            "stock_code": _ss(raw.get("股票代码", raw.get("code", raw.get("SECURITY_CODE")))),
            "stock_name": _ss(raw.get("股票名称", raw.get("name", raw.get("SECURITY_NAME_ABBR")))),
            "price": _sf(raw.get("最新价", raw.get("price", raw.get("NEWEST_PRICE")))),
            "change_percent": _sf(raw.get("涨跌幅", raw.get("change_percent", raw.get("CHANGE_RATE")))),
            "popularity_score": _sf(raw.get("人气值", raw.get("popularity", raw.get("HOT_NUM", raw.get("SCORE"))))),
            "current_rank_change": _ss(raw.get("排名变化", raw.get("rank_change", raw.get("RANK_CHANGE")))),
        }

    return {
        "rank": _si(raw.get("rank", raw.get("RANK"))),
        "stock_code": _ss(raw.get("stock_code", raw.get("SECURITY_CODE"))),
        "stock_name": _ss(raw.get("stock_name", raw.get("SECURITY_NAME_ABBR"))),
        "price": _sf(raw.get("price", raw.get("NEWEST_PRICE"))),
        "change_percent": _sf(raw.get("change_percent", raw.get("CHANGE_RATE"))),
        "popularity_score": _sf(raw.get("popularity_score", raw.get("HOT_NUM", raw.get("SCORE")))),
        "current_rank_change": _ss(raw.get("current_rank_change", raw.get("RANK_CHANGE"))),
    }


# ================================================================
# 涨停池 / 跌停池 / 炸板池
# ================================================================

ZT_POOL_SCHEMA = {
    "stock_code": str,
    "stock_name": str,
    "trade_date": str,
    "price": float,
    "change_percent": float,
    "continuous_zt_days": int,   # 连板天数
    "zt_time": str,              # 涨停时间
    "seal_amount": float,        # 封板资金
    "turnover_rate": float,
    "volume": float,
    "amount": float,
    "sector": str,               # 所属板块
    "reason": str,               # 涨停原因
    "open_count": int,           # 炸板次数
}


def normalize_zt_pool(raw: Dict[str, Any], source: str = "", trade_date: str = "") -> Dict[str, Any]:
    """标准化涨停池记录"""
    if source == "akshare" or "代码" in raw:
        return {
            "stock_code": _ss(raw.get("代码", raw.get("code", raw.get("stock_code")))),
            "stock_name": _ss(raw.get("名称", raw.get("name", raw.get("stock_name")))),
            "trade_date": trade_date,
            "price": _sf(raw.get("最新价", raw.get("close", raw.get("price")))),
            "change_percent": _sf(raw.get("涨跌幅", raw.get("pct_chg", raw.get("change_percent")))),
            "continuous_zt_days": _si(raw.get("连板数", raw.get("zt_days", raw.get("continuous_zt_days", 1)))),
            "zt_time": _ss(raw.get("涨停时间", raw.get("zt_time"))),
            "seal_amount": _sf(raw.get("封板资金", raw.get("seal_amount"))),
            "turnover_rate": _sf(raw.get("换手率", raw.get("turnover_rate"))),
            "volume": _sf(raw.get("成交额", raw.get("amount", raw.get("volume")))),
            "amount": _sf(raw.get("成交额", raw.get("amount"))),
            "sector": _ss(raw.get("所属行业", raw.get("sector"))),
            "reason": _ss(raw.get("涨停原因", raw.get("reason"))),
            "open_count": _si(raw.get("炸板次数", raw.get("open_count", 0))),
        }

    return {
        "stock_code": _ss(raw.get("stock_code", raw.get("SECURITY_CODE"))),
        "stock_name": _ss(raw.get("stock_name", raw.get("SECURITY_NAME_ABBR"))),
        "trade_date": _ss(raw.get("trade_date", trade_date)),
        "price": _sf(raw.get("price", raw.get("CLOSE_PRICE"))),
        "change_percent": _sf(raw.get("change_percent", raw.get("CHANGE_RATE"))),
        "continuous_zt_days": _si(raw.get("continuous_zt_days", raw.get("CONTINUOUS_LIMIT_DAYS", raw.get("ZT_DAYS", 1)))),
        "zt_time": _ss(raw.get("zt_time", raw.get("FIRST_ZDT_TIME"))),
        "seal_amount": _sf(raw.get("seal_amount", raw.get("LIMIT_ORDER_AMT"))),
        "turnover_rate": _sf(raw.get("turnover_rate", raw.get("TURNOVERRATE"))),
        "volume": _sf(raw.get("volume", raw.get("VOLUME"))),
        "amount": _sf(raw.get("amount", raw.get("TURNOVER"))),
        "sector": _ss(raw.get("sector", raw.get("BOARD_NAME"))),
        "reason": _ss(raw.get("reason", raw.get("ZT_REASON"))),
        "open_count": _si(raw.get("open_count", raw.get("OPEN_NUM", 0))),
    }


def normalize_dt_pool(raw: Dict[str, Any], source: str = "", trade_date: str = "") -> Dict[str, Any]:
    """标准化跌停池记录"""
    if source == "akshare" or "代码" in raw:
        return {
            "stock_code": _ss(raw.get("代码", raw.get("code", raw.get("stock_code")))),
            "stock_name": _ss(raw.get("名称", raw.get("name", raw.get("stock_name")))),
            "trade_date": trade_date,
            "price": _sf(raw.get("最新价", raw.get("price"))),
            "change_percent": _sf(raw.get("涨跌幅", raw.get("change_percent"))),
            "seal_amount": _sf(raw.get("封单资金", raw.get("seal_amount"))),
            "turnover_rate": _sf(raw.get("换手率", raw.get("turnover_rate"))),
            "amount": _sf(raw.get("成交额", raw.get("amount"))),
        }

    return {
        "stock_code": _ss(raw.get("stock_code", raw.get("SECURITY_CODE"))),
        "stock_name": _ss(raw.get("stock_name", raw.get("SECURITY_NAME_ABBR"))),
        "trade_date": _ss(raw.get("trade_date", trade_date)),
        "price": _sf(raw.get("price", raw.get("CLOSE_PRICE"))),
        "change_percent": _sf(raw.get("change_percent", raw.get("CHANGE_RATE"))),
        "seal_amount": _sf(raw.get("seal_amount", raw.get("LIMIT_ORDER_AMT"))),
        "turnover_rate": _sf(raw.get("turnover_rate", raw.get("TURNOVERRATE"))),
        "amount": _sf(raw.get("amount", raw.get("TURNOVER"))),
    }


def normalize_broken_board(raw: Dict[str, Any], source: str = "", trade_date: str = "") -> Dict[str, Any]:
    """标准化炸板池记录"""
    if source == "akshare" or "代码" in raw:
        return {
            "stock_code": _ss(raw.get("代码", raw.get("code", raw.get("stock_code")))),
            "stock_name": _ss(raw.get("名称", raw.get("name", raw.get("stock_name")))),
            "trade_date": trade_date,
            "price": _sf(raw.get("最新价", raw.get("price"))),
            "change_percent": _sf(raw.get("涨跌幅", raw.get("change_percent"))),
            "zt_time": _ss(raw.get("涨停时间", raw.get("zt_time"))),
            "break_time": _ss(raw.get("炸板时间", raw.get("break_time"))),
            "turnover_rate": _sf(raw.get("换手率", raw.get("turnover_rate"))),
            "amount": _sf(raw.get("成交额", raw.get("amount"))),
        }

    return {
        "stock_code": _ss(raw.get("stock_code", raw.get("SECURITY_CODE"))),
        "stock_name": _ss(raw.get("stock_name", raw.get("SECURITY_NAME_ABBR"))),
        "trade_date": _ss(raw.get("trade_date", trade_date)),
        "price": _sf(raw.get("price", raw.get("CLOSE_PRICE"))),
        "change_percent": _sf(raw.get("change_percent", raw.get("CHANGE_RATE"))),
        "zt_time": _ss(raw.get("zt_time", raw.get("FIRST_ZDT_TIME"))),
        "break_time": _ss(raw.get("break_time", raw.get("LAST_ZDT_TIME"))),
        "turnover_rate": _sf(raw.get("turnover_rate", raw.get("TURNOVERRATE"))),
        "amount": _sf(raw.get("amount", raw.get("TURNOVER"))),
    }


# ================================================================
# 市场快照
# ================================================================

MARKET_SNAPSHOT_SCHEMA = {
    "up_count": int,          # 上涨家数
    "down_count": int,        # 下跌家数
    "flat_count": int,        # 平盘家数
    "limit_up": int,          # 涨停家数
    "limit_down": int,        # 跌停家数
    "total_amount": float,    # 总成交额 (亿元)
    "emotion": int,           # 情绪指标 0-100
    "north_net_flow": float,  # 北向净流入 (亿元)
}


def normalize_market_snapshot(raw: Dict[str, Any]) -> Dict[str, Any]:
    """标准化市场快照"""
    up = _si(raw.get("up_count", 0))
    down = _si(raw.get("down_count", 0))
    total = up + down
    emotion = _si(raw.get("emotion", -1))
    if emotion < 0:
        emotion = int(up / total * 100) if total > 0 else 50

    return {
        "up_count": up,
        "down_count": down,
        "flat_count": _si(raw.get("flat_count", 0)),
        "limit_up": _si(raw.get("limit_up", 0)),
        "limit_down": _si(raw.get("limit_down", 0)),
        "total_amount": _sf(raw.get("total_amount", 0)),
        "emotion": emotion,
        "north_net_flow": _sf(raw.get("north_net_flow", 0)),
    }


# ================================================================
# 数据校验
# ================================================================

def validate_dragon_tiger(item: Dict) -> bool:
    """校验龙虎榜记录是否有效"""
    return bool(item.get("stock_code")) and bool(item.get("trade_date"))


def validate_hot_rank(item: Dict) -> bool:
    """校验热榜记录是否有效"""
    return bool(item.get("stock_code"))


def validate_zt_pool(item: Dict) -> bool:
    """校验涨停池记录是否有效"""
    return bool(item.get("stock_code")) and item.get("change_percent", 0) > 0


def validate_dt_pool(item: Dict) -> bool:
    """校验跌停池记录是否有效"""
    return bool(item.get("stock_code")) and item.get("change_percent", 0) < 0


def validate_broken_board(item: Dict) -> bool:
    """校验炸板池记录是否有效"""
    return bool(item.get("stock_code"))


# ================================================================
# 公开别名 — 供外部模块统一引用
# ================================================================
safe_float = _sf
safe_int = _si
safe_str = _ss

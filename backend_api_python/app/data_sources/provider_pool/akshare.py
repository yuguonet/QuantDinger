# -*- coding: utf-8 -*-
"""provider_pool / akshare —— 榜单族（龙虎榜 / 涨跌停池 / 炸板池 / 人气榜）。

【定位】
  数据源适配层的 provider：**只做单次 I/O + 解析 + 字段归一**。
  不重试、不熔断、不缓存、不落库（这些在 dispatch / coord_pool）。

【代码来源（2026-10-26）】
  下方 `_ss` / `_normalize_*` 系从 `app/market_cn/dragon_limit.py` **原样抽取**
  （AST 按函数边界切出，未改一字），保证与生产口径逐字段一致。
  `_ak_*` 取数函数按同一 akshare 调用重写，**唯一差异**：不再
  `except -> return []`（那会把"取数失败"伪装成"当天没有榜单"），改为向上抛，
  由 dispatch 计入该源失败。见方案 §7「空信封 vs 失败」。

【为什么复制而不 import】
  目标是把数据源层从 market_cn 里独立出来；若 import 生产模块，替换期删
  `dragon_limit.py` 就会把新层一起打断。复制换取可替换性。
"""

from __future__ import annotations

from typing import Any, Dict, List

from app.data_sources.normalizer import safe_float as _sf, safe_int as _si
from app.data_sources.rate_limiter import get_akshare_limiter
from app.utils.logger import get_logger

logger = get_logger(__name__)


def _ss(v, default="") -> str:
    """safe str"""
    if v is None:
        return default
    return str(v).strip() if v else default

def _normalize_dragon_tiger(raw: Dict[str, Any], source: str = "") -> Dict[str, Any]:
    """标准化一条龙虎榜记录（内部用）
    
    兼容 AkShare stock_lhb_detail_em 返回的列名:
      代码, 名称, 上榜日, 解读, 收盘价, 涨跌幅, 龙虎榜净买额, 龙虎榜买入额,
      龙虎榜卖出额, 龙虎榜成交额, 换手率, 上榜原因
    """
    if source == "akshare" or "代码" in raw:
        return {
            "stock_code": _ss(raw.get("代码", raw.get("code", raw.get("stock_code")))),
            "stock_name": _ss(raw.get("名称", raw.get("name", raw.get("stock_name")))),
            "trade_date": _ss(raw.get("上榜日", raw.get("trade_date", "")))[:10],
            "reason": _ss(raw.get("解读", raw.get("reason", raw.get("EXPLANATION"))))[:100],
            "buy_amount": _sf(raw.get("龙虎榜买入额", raw.get("buy_amount", raw.get("BUY")))),
            "sell_amount": _sf(raw.get("龙虎榜卖出额", raw.get("sell_amount", raw.get("SELL")))),
            "net_amount": _sf(raw.get("龙虎榜净买额", raw.get("龙虎榜净额", raw.get("net_amount", raw.get("NET_BUY"))))),
            "change_percent": _sf(raw.get("涨跌幅", raw.get("change_percent", raw.get("CHANGE_RATE")))),
            "close_price": _sf(raw.get("收盘价", raw.get("close_price", raw.get("CLOSE_PRICE")))),
            "turnover_rate": _sf(raw.get("换手率", raw.get("turnover_rate", raw.get("TURNOVERRATE")))),
            "amount": _sf(raw.get("龙虎榜成交额", raw.get("成交额", raw.get("amount", raw.get("ACCUM_AMOUNT"))))),
            "buy_seat_count": _si(raw.get("买入席位数", raw.get("buy_seat_count", raw.get("BUYER_NUM", 0)))),
            "sell_seat_count": _si(raw.get("卖出席位数", raw.get("sell_seat_count", raw.get("SELLER_NUM", 0)))),
        }
    return {
        "stock_code": _ss(raw.get("stock_code", raw.get("code", raw.get("SECURITY_CODE")))),
        "stock_name": _ss(raw.get("stock_name", raw.get("name", raw.get("SECURITY_NAME_ABBR")))),
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

def _normalize_dragon_tiger_list(raw_list: List[Dict], source: str = "") -> List[Dict[str, Any]]:
    """批量标准化龙虎榜数据（内部用）"""
    return [_normalize_dragon_tiger(r, source) for r in raw_list if isinstance(r, dict)]

def _normalize_hot_rank(raw: Dict[str, Any], source: str = "") -> Dict[str, Any]:
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

def _normalize_zt_pool(raw: Dict[str, Any], source: str = "", trade_date: str = "") -> Dict[str, Any]:
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

def _normalize_dt_pool(raw: Dict[str, Any], source: str = "", trade_date: str = "") -> Dict[str, Any]:
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

def _normalize_broken_board(raw: Dict[str, Any], source: str = "", trade_date: str = "") -> Dict[str, Any]:
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
# ══════════════════════════════════════════════════════════════
#  取数（akshare）—— 失败即抛，不吞
# ══════════════════════════════════════════════════════════════


def _ak():
    import akshare as ak
    return ak


def _rows(df) -> Dict[str, Any]:
    """DataFrame → 协议信封。空表是**成功**（当天确实没有榜单），不是失败。"""
    if df is None or getattr(df, "empty", True):
        return {"rows": []}
    return {"rows": df.to_dict("records")}


def fetch_dragon_tiger(start_date: str = "", end_date: str = "") -> Dict[str, Any]:
    """龙虎榜明细。日期 YYYY-MM-DD 或 YYYYMMDD 均可（内部转 YYYYMMDD）。"""
    get_akshare_limiter().wait()
    df = _ak().stock_lhb_detail_em(
        start_date=(start_date or "").replace("-", ""),
        end_date=(end_date or "").replace("-", ""),
    )
    env = _rows(df)
    env["rows"] = [r for r in _normalize_dragon_tiger_list(env["rows"], source="akshare")
                   if r.get("stock_code") and r.get("trade_date")]
    return env


def _pool(fn_name: str, trade_date: str, normalizer) -> Dict[str, Any]:
    get_akshare_limiter().wait()
    day = (trade_date or "").replace("-", "")
    if not day:
        # 空日期默认**今天**（榜单语义就是"当日池"）：调用方（dragon_limit 等）
        # 不传 trade_date 时 akshare 会拿到 date="" —— stock_zt_pool_em 返空表、
        # dtgc/zbgc 直接在库内 int('') 崩（2026-10-27 日志：三池全空/全炸）。
        from datetime import datetime
        day = datetime.now().strftime("%Y%m%d")
    df = getattr(_ak(), fn_name)(date=day)
    env = _rows(df)
    iso = ("%s-%s-%s" % (day[:4], day[4:6], day[6:8])) if len(day) == 8 else ""
    env["rows"] = [normalizer(r, source="akshare", trade_date=iso) for r in env["rows"]]
    return env


def fetch_limit_up_pool(trade_date: str = "") -> Dict[str, Any]:
    return _pool("stock_zt_pool_em", trade_date, _normalize_zt_pool)


def fetch_limit_down_pool(trade_date: str = "") -> Dict[str, Any]:
    return _pool("stock_zt_pool_dtgc_em", trade_date, _normalize_dt_pool)


def fetch_broken_board_pool(trade_date: str = "") -> Dict[str, Any]:
    return _pool("stock_zt_pool_zbgc_em", trade_date, _normalize_broken_board)


def fetch_hot_rank() -> Dict[str, Any]:
    get_akshare_limiter().wait()
    env = _rows(_ak().stock_hot_rank_em())
    env["rows"] = [_normalize_hot_rank(r, source="akshare") for r in env["rows"]]
    return env


# ══════════════════════════════════════════════════════════════
#  Provider 声明（族内注册）
# ══════════════════════════════════════════════════════════════

from app.data_sources.provider_pool import register   # noqa: E402


@register
class AksharePoolProvider:
    """榜单族唯一上游（akshare）。"""

    name = "akshare"
    priority = 50
    markets = {"CNStock"}

    #: kind -> 取数函数（**函数对象**，不是方法名 —— 取数函数在模块级，
    #: 不在类上；存字符串会得到 AttributeError）
    capabilities = {
        "dragon_tiger": fetch_dragon_tiger,
        "limit_up_pool": fetch_limit_up_pool,
        "limit_down_pool": fetch_limit_down_pool,
        "broken_board_pool": fetch_broken_board_pool,
        "hot_rank": fetch_hot_rank,
    }

    def fetch(self, kind: str, **params) -> Dict[str, Any]:
        fn = self.capabilities.get(kind)
        if fn is None:
            from app.data_sources.dispatch import NotSupported
            return NotSupported(kind)
        return fn(**params)

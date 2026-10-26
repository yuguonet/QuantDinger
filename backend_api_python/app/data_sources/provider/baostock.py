# -*- coding: utf-8 -*-
"""provider / baostock —— 个股域 BaoStock 源（kline 1D）。

BaoStock 免费开源，支持日线（含不复权/前复权/后复权）。
volume 单位: baostock 返回"股"，与 kline_1D 口径一致。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from app.utils.logger import get_logger

logger = get_logger(__name__)

# 复权编码: 1=后复权 2=前复权 3=不复权
_ADJUST = {"": 3, "qfq": 2, "hfq": 1}


def fetch_kline(code: str, timeframe: str = "1D", count: int = 250,
                timeout: int = 10, start_date: str = "", end_date: str = "",
                adj: str = "", **kwargs) -> Dict[str, Any]:
    """个股 K 线（BaoStock query_history_k_data_plus）。仅支持 1D。"""
    if timeframe != "1D":
        return {}

    try:
        import baostock as bs
    except ImportError:
        return {}

    # BaoStock 股票代码格式: sh.600519 / sz.000001
    c = (code or "").strip()
    if c.startswith(("sh", "sz")) and "." in c:
        bs_code = c
    elif c[:1] in ("6", "9"):
        bs_code = f"sh.{c}"
    else:
        bs_code = f"sz.{c}"

    lg = bs.login()
    if lg.error_code != "0":
        return {}

    try:
        import datetime
        end = datetime.date.today()
        if end_date:
            try:
                end = datetime.datetime.strptime(end_date, "%Y-%m-%d").date()
            except ValueError:
                pass
        start = end - datetime.timedelta(days=int(count * 1.5))
        if start_date:
            try:
                start = datetime.datetime.strptime(start_date, "%Y-%m-%d").date()
            except ValueError:
                pass

        rs = bs.query_history_k_data_plus(
            bs_code,
            "date,open,high,low,close,volume",
            start_date=start.strftime("%Y-%m-%d"),
            end_date=end.strftime("%Y-%m-%d"),
            frequency="d",
            adjustflag=str(_ADJUST.get(adj, 3)),  # 默认不复权
        )
        if rs.error_code != "0":
            return {}

        rows = []
        while rs.next():
            r = rs.get_row_data()
            try:
                rows.append({
                    "time": r[0],
                    "open": float(r[1] or 0),
                    "high": float(r[2] or 0),
                    "low": float(r[3] or 0),
                    "close": float(r[4] or 0),
                    "volume": float(r[5] or 0),  # 股
                })
            except (ValueError, IndexError):
                continue

        return {"bars": rows[-count:], "count": len(rows[-count:])}
    except Exception as e:
        logger.warning("[baostock] kline %s 失败: %s", code, e)
        return {}
    finally:
        bs.logout()


from app.data_sources.provider import register  # noqa: E402


@register
class BaostockDataSource:
    name = "baostock"
    priority = 35
    max_concurrency = 2
    min_interval = 0.5
    jitter_min = 0.2
    jitter_max = 1.0

    capabilities = {
        "kline": True,
        "kline_priority": 35,
        "kline_tf": {"1D"},
        "quote": True,
        "quote_priority": 35,
        "batch_quote": False,
        "hk": False,
        "markets": {"CNStock"},
    }

    def prepare(self) -> bool:
        try:
            import baostock
            return True
        except ImportError:
            return False

    def fetch_kline(self, code: str, timeframe: str = "1D", count: int = 250,
                    timeout: int = 10, start_date: str = "", end_date: str = "",
                    adj: str = "", **kwargs) -> Dict[str, Any]:
        return fetch_kline(code, timeframe, count, timeout, start_date, end_date, adj)

    def fetch_ticker(self, code: str, timeout: int = 8, **kwargs) -> Optional[Dict[str, Any]]:
        """个股实时行情（BaoStock 当日 K 线近似）。

        BaoStock 无专用实时行情 API，用当日 K 线最后一条近似。
        字段归一化到标准 quote 格式。
        """
        try:
            import baostock as bs
        except ImportError:
            return None

        c = (code or "").strip()
        if c.startswith(("sh", "sz")) and "." in c:
            bs_code = c
        elif c[:1] in ("6", "9"):
            bs_code = f"sh.{c}"
        else:
            bs_code = f"sz.{c}"

        lg = bs.login()
        if lg.error_code != "0":
            return None

        try:
            import datetime
            today = datetime.date.today().strftime("%Y-%m-%d")
            rs = bs.query_history_k_data_plus(
                bs_code,
                "date,open,high,low,close,volume",
                start_date=today,
                end_date=today,
                frequency="d",
                adjustflag="3",  # 不复权
            )
            if rs.error_code != "0":
                return None

            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
            if not rows:
                return None

            r = rows[-1]
            close = float(r[4] or 0)
            open_p = float(r[1] or 0)
            high = float(r[2] or 0)
            low = float(r[3] or 0)
            vol = float(r[5] or 0)

            # 昨收（查前一交易日）
            rs2 = bs.query_history_k_data_plus(
                bs_code, "date,close",
                start_date=str(datetime.date.today() - datetime.timedelta(days=7)),
                end_date=str(datetime.date.today() - datetime.timedelta(days=1)),
                frequency="d", adjustflag="3",
            )
            prev = 0
            if rs2.error_code == "0":
                prev_rows = []
                while rs2.next():
                    prev_rows.append(rs2.get_row_data())
                if prev_rows:
                    prev = float(prev_rows[-1][1] or 0)

            from app.data_sources.provider import normalize_quote
            result = {
                "last": close,
                "change": round(close - prev, 4) if prev else 0,
                "changePercent": round((close - prev) / prev * 100, 2) if prev else 0,
                "high": high,
                "low": low,
                "open": open_p,
                "previousClose": prev,
                "volume": vol,
                "time": r[0] if r else "",
                "name": "",
                "symbol": c,
            }
            normalize_quote(result)
            return result
        except Exception as e:
            logger.warning("[baostock] ticker %s 失败: %s", code, e)
            return None
        finally:
            bs.logout()

    def fetch_batch_quotes(self, codes, timeout: int = 10, **kwargs):
        return {}

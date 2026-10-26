# -*- coding: utf-8 -*-
"""provider_index / baostock —— 指数族 BaoStock 源（index_kline）。

BaoStock 免费开源证券数据接口，需 pip install baostock。
volume 单位: baostock 为"股"→÷100 转"手"对齐。
"""
from __future__ import annotations

from typing import Any, Dict

from app.utils.logger import get_logger

logger = get_logger(__name__)


def fetch_index_kline(code: str, days: int = 250) -> Dict[str, Any]:
    """指数日K线（BaoStock query_history_k_data_plus）。"""
    try:
        import baostock as bs
    except ImportError:
        return {"rows": [], "error": "baostock 未安装"}

    # BaoStock 指数代码格式: sh.000001 / sz.399001
    pfx = "sh" if code[:3] in ("000", "88", "99") else "sz"
    bs_code = f"{pfx}.{code}"

    lg = bs.login()
    if lg.error_code != "0":
        return {"rows": [], "error": f"login failed: {lg.error_msg}"}

    try:
        import datetime
        end = datetime.date.today()
        start = end - datetime.timedelta(days=int(days * 1.5))
        rs = bs.query_history_k_data_plus(
            bs_code,
            "date,open,high,low,close,volume",
            start_date=start.strftime("%Y-%m-%d"),
            end_date=end.strftime("%Y-%m-%d"),
            frequency="d",
            adjustflag="3",  # 不复权
        )
        if rs.error_code != "0":
            return {"rows": [], "error": rs.error_msg}

        rows = []
        while rs.next():
            r = rs.get_row_data()
            try:
                rows.append({
                    "date": r[0],
                    "open": float(r[1] or 0),
                    "high": float(r[2] or 0),
                    "low": float(r[3] or 0),
                    "close": float(r[4] or 0),
                    "volume": float(r[5] or 0),  # 股（对齐 kline_index_5m）
                })
            except (ValueError, IndexError):
                continue

        return {"rows": rows[-days:]}
    except Exception as e:
        logger.warning("[baostock] index_kline %s 失败: %s", code, e)
        return {"rows": [], "error": str(e)}
    finally:
        bs.logout()


from app.data_sources.provider_index import register  # noqa: E402


@register
class BaostockIndexProvider:
    name = "baostock"
    priority = 30
    markets = {"CNStock"}
    capabilities = {
        "index_kline": fetch_index_kline,
    }

    def fetch(self, kind: str, **params) -> Dict[str, Any]:
        fn = self.capabilities.get(kind)
        if fn is None:
            from app.data_sources.dispatch import NotSupported
            return NotSupported(kind)
        return fn(**params)

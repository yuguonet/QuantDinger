# -*- coding: utf-8 -*-
"""provider_index / baostock —— 指数族 BaoStock 源（index_kline）。

BaoStock 免费开源证券数据接口，需 pip install baostock。
volume 单位: baostock 为"股"→÷100 转"手"对齐。
"""
from __future__ import annotations

from typing import Any, Dict

from app.utils.logger import get_logger

logger = get_logger(__name__)


def fetch_index_kline(code: str, timeframe: str = "1D", limit: int = 250) -> Dict[str, Any]:
    """指数日K线（BaoStock query_history_k_data_plus, timeframe=1D；其余周期 NotSupported）。

    ⚠ 本函数所有失败分支一律 raise，不返回空信封：is_rows 只校验 rows 是 list，
    空 rows 会被判「成功」并终止 sequential 换源（2026-10-28 1.log 事故）。
    """
    from app.data_sources.normalizer import normalize_chart_timeframe, split_index_code
    from app.data_sources.dispatch import NotSupported

    if normalize_chart_timeframe(timeframe) != "1D":
        return NotSupported("index_kline/%s" % timeframe)
    try:
        import baostock as bs
    except ImportError:
        raise RuntimeError("baostock 未安装")

    # BaoStock 指数代码格式: sh.000001 / sz.399001
    # 旧实现 `code[:3]` 判前缀后直拼，入参带后缀时会得到 "sh.000001.SH"，
    # baostock 库内直接报「股票代码应为9位」。
    base, exch = split_index_code(code)
    if not base or not exch:
        raise ValueError("baostock index_kline 无法解析指数代码: %r" % (code,))
    bs_code = "%s.%s" % (exch.lower(), base)

    lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError("baostock login failed: %s" % lg.error_msg)

    try:
        import datetime
        end = datetime.date.today()
        start = end - datetime.timedelta(days=int(limit * 1.5))
        rs = bs.query_history_k_data_plus(
            bs_code,
            "date,open,high,low,close,volume",
            start_date=start.strftime("%Y-%m-%d"),
            end_date=end.strftime("%Y-%m-%d"),
            frequency="d",
            adjustflag="3",  # 不复权
        )
        if rs.error_code != "0":
            raise RuntimeError("baostock query failed: %s" % rs.error_msg)

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

        return {"rows": rows[-limit:]}
    except Exception as e:
        logger.warning("[baostock] index_kline %s 失败: %s", code, e)
        raise
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

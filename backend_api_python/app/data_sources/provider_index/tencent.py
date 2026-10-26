# -*- coding: utf-8 -*-
"""provider_index / tencent —— 指数族腾讯 HTTP 源（index_quote）。

替代 akshare（东财 push2 被出口拦截）。直连 qt.gtimg.cn，纯 HTTP，零依赖。

腾讯接口（与 market_cn/index.py:_rt_tencent 同口径）:
  https://qt.gtimg.cn/q=sh000001,sz399001,...
  返回 GBK 编码，字段以 ~ 分隔（共 50+ 字段）。

volume 单位: 指数成交量为"手"，本文件转"股"（×100）对齐 akshare 口径。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.utils.logger import get_logger

logger = get_logger(__name__)

_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}


def _index_tencent_code(code: str) -> str:
    """指数纯代码 → 腾讯带前缀代码（sh000001 / sz399001）。

    指数前缀规则: 000/88/99 → sh（沪），其余 → sz（深）。
    """
    c = (code or "").strip()
    if c[:3] in ("000", "88", "99"):
        return f"sh{c}"
    return f"sz{c}"


def _parse_tencent_line(line: str) -> Optional[Dict[str, Any]]:
    """解析一行腾讯行情 → 标准 dict。"""
    if "=" not in line or '"' not in line:
        return None
    v = line.split('"')[1].split("~")
    if len(v) < 50:
        return None

    # 从变量名提取纯代码: v_sh000001="..." → 000001
    var_part = line.split("=")[0]  # v_sh000001
    code = var_part.replace("v_sh", "").replace("v_sz", "")

    try:
        price = float(v[3]) if v[3] else 0
        last_close = float(v[4]) if v[4] else 0
        open_ = float(v[5]) if v[5] else 0
        high = float(v[33]) if v[33] else 0
        low = float(v[34]) if v[34] else 0
        volume = float(v[36]) if v[36] else 0  # 手
        amount = float(v[37]) if v[37] else 0
    except (ValueError, TypeError):
        return None

    return {
        "code": code,
        "name": v[1],
        "price": price,
        "last_close": last_close,
        "open": open_,
        "high": high,
        "low": low,
        "volume": round(volume * 100, 2),  # 手 → 股
        "amount": amount,
        "change": round(price - last_close, 4) if last_close else 0,
        "change_percent": round((price - last_close) / last_close * 100, 4) if last_close else 0,
    }


def fetch_index_quote(codes: Optional[List[str]] = None) -> Dict[str, Any]:
    """全市场指数实时快照（腾讯 qt.gtimg.cn 批量接口）。

    不传 codes 时拉取主要指数（上证/深成/创业板/科创50/沪深300 等）。
    返回格式与 akshare 版一致: {"rows": [...]}
    """
    import urllib.request

    if not codes:
        # 主要指数默认列表
        codes = [
            "000001",  # 上证指数
            "399001",  # 深证成指
            "399006",  # 创业板指
            "000300",  # 沪深300
            "000905",  # 中证500
            "000688",  # 科创50
            "399005",  # 中小板指
            "000016",  # 上证50
        ]

    prefixed = [_index_tencent_code(c) for c in codes]
    url = "https://qt.gtimg.cn/q=" + ",".join(prefixed)

    try:
        req = urllib.request.Request(url, headers=dict(_HTTP_HEADERS))
        raw = urllib.request.urlopen(req, timeout=10).read().decode("gbk")
    except Exception as e:
        logger.warning("[tencent] index_quote 请求失败: %s", e)
        return {"rows": [], "error": str(e)}

    rows = []
    for line in raw.strip().split(";"):
        rec = _parse_tencent_line(line.strip())
        if rec:
            rows.append(rec)

    return {"rows": rows}


def fetch_index_kline(code: str, days: int = 250) -> Dict[str, Any]:
    """指数日K线（腾讯 fqkline）。

    API: web.ifzq.gtimg.cn/appstock/app/fqkline/get
    返回 data.{code}.day = [[date, open, close, high, low, volume], ...]
    volume 原始"手"→×100 转"股"对齐 mootdx 口径。
    """
    import urllib.request

    tc_code = _index_tencent_code(code)
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    params = f"param={tc_code},day,,,{days},"

    try:
        req = urllib.request.Request(
            f"{url}?{params}",
            headers=dict(_HTTP_HEADERS, Referer="https://gu.qq.com/"),
        )
        raw = urllib.request.urlopen(req, timeout=10).read().decode("utf-8", errors="replace")
    except Exception as e:
        logger.warning("[tencent] index_kline %s 请求失败: %s", code, e)
        return {"rows": [], "error": str(e)}

    try:
        import json
        data = json.loads(raw)
    except Exception as e:
        return {"rows": [], "error": f"JSON parse: {e}"}

    root = (data.get("data") or {}).get(tc_code)
    if not isinstance(root, dict):
        return {"rows": [], "error": "no data"}
    day_arr = root.get("day")
    if not isinstance(day_arr, list):
        return {"rows": [], "error": "no day array"}

    rows = []
    for r in day_arr:
        if not isinstance(r, (list, tuple)) or len(r) < 5:
            continue
        try:
            rows.append({
                "date": str(r[0]),
                "open": float(r[1]),
                "close": float(r[2]),
                "high": float(r[3]),
                "low": float(r[4]),
                "volume": round(float(r[5]) * 100, 2) if len(r) > 5 and r[5] else 0,  # 手→股（对齐 kline_index_5m）
            })
        except (ValueError, TypeError):
            continue

    return {"rows": rows[-days:]}


from app.data_sources.provider_index import register  # noqa: E402


@register
class TencentIndexProvider:
    name = "tencent"
    priority = 10  # 优先于 akshare (50)
    markets = {"CNStock"}
    capabilities = {
        "index_quote": fetch_index_quote,
        "index_kline": fetch_index_kline,
    }

    def fetch(self, kind: str, **params) -> Dict[str, Any]:
        fn = self.capabilities.get(kind)
        if fn is None:
            from app.data_sources.dispatch import NotSupported
            return NotSupported(kind)
        return fn(**params)

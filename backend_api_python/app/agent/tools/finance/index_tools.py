# -*- coding: utf-8 -*-
"""指数行情工具（修复 2026-09-25 实测红字）。

问题：
  1. 能力层 `get_index_realtime(codes="000300")` 收到 **str**，底层按 list 迭代 → 结果错票/0 点
  2. 内存缓存 `_rt_idx_realtime` **不按 codes 过滤**，`data[0]` 可能是别的指数
  3. `get_realtime_quote` 只走个股源，指数代码拉不到

本包装复用 `app.market_cn.index` 既有接口：参数归一 + 缓存过滤 + 统一信封。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

from .._base import as_code_envelope


def _norm_codes(codes: Union[str, List[str], None]) -> List[str]:
    if codes is None:
        return []
    if isinstance(codes, (list, tuple)):
        out = [str(c).strip() for c in codes if str(c).strip()]
    else:
        out = [c.strip() for c in str(codes).split(",") if c.strip()]
    # 指数代码常见 6 位；去掉 sh/sz 前缀
    cleaned = []
    for c in out:
        c = c.replace("SH", "").replace("SZ", "").replace("sh", "").replace("sz", "").replace(".", "")
        if c.isdigit():
            cleaned.append(c.zfill(6) if len(c) < 6 else c)
        else:
            cleaned.append(c)
    return cleaned[:12]


def get_index_quote(codes: Union[str, List[str]] = "", scope: str = "single",
                    days: int = 60) -> Dict[str, Any]:
    """指数/ETF 行情统一入口：五大指数快照、指定指数实时、ETF 行情与指数日线。
    指数日 K 另有 get_index_kline（与 scope=kline 同源同实现，同名让位项保留注册）。

    Args:
        codes: 指数/ETF 代码，逗号分隔或列表。常用: 000001 上证, 000300 沪深300,
               399001 深成指, 399006 创业板指, 000905 中证500；ETF: 510050, 510300…
        scope: indices=五大指数快照 | single=指定指数实时（默认） | etf=指数对应 ETF
               | kline=指数日线序列
        days: K 线天数（scope=kline 用），默认60

    Returns:
        scope=indices → {"count", "indices": [{code,name,price,change_percent,...}]}。
        scope=single/etf → {"count", "data": {code: {code,name,price,change_percent,...}}}，
            单标的时顶层再镜像（双通道）；获取失败的代码其值为 {"code", "error"}。
        scope=kline → {"count", "data": {code: [{t,o,h,l,c,v}...]}}（可直接 bar['c']；
            无数据的代码其值为 {"error"}）。失败 → {"error", "retriable"}。
    """
    s = (scope or "single").strip().lower()
    if s == "indices":
        # 惰性 import：data_tools 与本模块同域，避免模块级环形 import 风险
        from .data_tools import _get_market_indices
        return _get_market_indices()
    if s == "etf":
        from .data_tools import _get_index_etf_quote
        raw = _get_index_etf_quote(
            codes if isinstance(codes, str) else ",".join(codes))
        if not isinstance(raw, dict) or "error" in raw:
            return raw
        by_code = {str(r.get("code", "")): r for r in (raw.get("quotes") or [])
                   if isinstance(r, dict)}
        return {"count": len(by_code), "data": by_code}
    if s == "kline":
        code_list = _norm_codes(codes) or ["000001"]
        data: Dict[str, Any] = {}
        for c in code_list[:20]:
            r = get_index_kline(c, days)
            bars = r.get("bars") if isinstance(r, dict) else None
            data[c] = bars if bars else {"error": (r or {}).get("error", "无指数K线数据")}
        return {"count": len(data), "data": data}
    if s != "single":
        return {"error": f"scope 无效: {scope!r}，可选 indices|single|etf|kline",
                "retriable": False}
    # ── scope=single：指定指数实时（原主体，零改动）──
    code_list = _norm_codes(codes)
    if not code_list:
        # 默认五大指数
        code_list = ["000001", "399001", "399006", "000300", "000688"]
    try:
        from app.market_cn.index import INDEX_CODES
        # 2026-10-28 Phase 3：原"优先 _rt_tencent → get_index_realtime 兜底"的双链已收口。
        # get_index_realtime 内部即 coord_index("index_quote")：tencent 优先（真·多股单请求），
        # mootdx 已按 kind_priority 降为兜底并在 provider 内过滤 price<=0
        # （原"脏数据短路后续源"在源头解决，包装层不再需要 _has_good 二次挑源）。
        from app.market_cn.index import get_index_realtime
        rows: List[Dict[str, Any]] = get_index_realtime(codes=code_list, force=True) or []
    except Exception as e:
        return {"error": f"指数行情获取失败: {e}", "retriable": True}

    by_code: Dict[str, Any] = {}
    want_names = {INDEX_CODES.get(c, ""): c for c in code_list if INDEX_CODES.get(c)}
    for r in rows:
        if not isinstance(r, dict):
            continue
        raw_code = str(r.get("code") or "")
        name = str(r.get("name") or "")
        c = None
        for w in code_list:
            if w == raw_code or w == raw_code[:6] or raw_code.startswith(w) or f"~{w}~" in raw_code:
                c = w
                break
        if c is None and name in want_names:
            c = want_names[name]
        if c is None:
            continue
        try:
            price = float(r.get("price") or 0)
        except Exception:
            price = 0.0
        if price <= 0:
            continue
        by_code[c] = {
            "code": c,
            "name": name or INDEX_CODES.get(c, c),
            "price": price,
            "open": r.get("open"),
            "high": r.get("high"),
            "low": r.get("low"),
            "last_close": r.get("last_close"),
            "change": r.get("change"),
            "change_percent": r.get("change_percent"),
            "volume": r.get("volume"),
            "amount": r.get("amount"),
        }
    # 请求了但源里没有的：显式 error，避免模型拿空 dict 当 0 点
    for c in code_list:
        if c not in by_code:
            by_code[c] = {"code": c, "name": INDEX_CODES.get(c, c), "error": "未获取到行情"}

    if len(code_list) == 1:
        return as_code_envelope(code_list[0], by_code.get(code_list[0], {}))
    return {"count": len(by_code), "data": by_code}


def get_index_kline(code: str = "000001", days: int = 60) -> Dict[str, Any]:
    """指数日K线（qfq 等价口径，可直接下标 bar['c']）。

    Returns: {count, data:{code:[{t,o,h,l,c,v}...]}}；失败 → {error}。
    """
    code = _norm_codes(code)[0] if _norm_codes(code) else "000001"
    try:
        from app.market_cn.index import get_index_daily_kline
        raw = get_index_daily_kline(code, int(days) or 60, force=True) or []
    except Exception as e:
        return {"error": f"指数K线获取失败: {e}", "retriable": True}
    bars = []
    for b in raw:
        if not isinstance(b, dict):
            continue
        t = b.get("date") or b.get("day") or b.get("time") or b.get("t")
        cl = b.get("close", b.get("c"))
        if cl is None:
            continue
        o, h, l = b.get("open", b.get("o")), b.get("high", b.get("h")), b.get("low", b.get("l"))
        v = b.get("volume", b.get("v"))
        bars.append({
            "t": t, "o": float(o or cl), "h": float(h or cl), "l": float(l or cl),
            "c": float(cl), "v": float(v or 0),
            "time": t, "open": float(o or cl), "high": float(h or cl),
            "low": float(l or cl), "close": float(cl), "volume": float(v or 0),
        })
    if not bars:
        return {"error": "无指数K线数据"}
    out = as_code_envelope(code, {"bars": bars, "count": len(bars)})
    out.setdefault("bars", bars)
    return out

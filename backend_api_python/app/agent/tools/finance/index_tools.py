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

from tools.base import as_code_envelope


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


def get_index_quote(codes: Union[str, List[str]] = "") -> Dict[str, Any]:
    """指数实时行情（沪深300/上证/创业板等）。

    Args:
        codes: 指数代码，逗号分隔或列表。常用: 000001 上证, 000300 沪深300,
               399001 深成指, 399006 创业板指, 000905 中证500。

    Returns:
        {count, data:{code:{code,name,price,change,change_percent,...}}}；
        单指数时顶层镜像。失败 → {error}。
    """
    code_list = _norm_codes(codes)
    if not code_list:
        # 默认五大指数
        code_list = ["000001", "399001", "399006", "000300", "000688"]
    try:
        from app.market_cn.index import INDEX_CODES
        rows: List[Dict[str, Any]] = []
        # mootdx 实测脏数据（code 错票 / price=0 / 假价 11.3）且会短路后续源
        # → 包装层**优先腾讯**，再用 get_index_realtime 兜底，且只收 price>0 且 code 匹配
        try:
            from app.market_cn.index import _rt_tencent
            rows = _rt_tencent(code_list) or []
        except Exception:
            rows = []
        # 腾讯 code 字段是原始串（000300="1~...），不能用 zfill 精确比
        def _has_good(rs):
            for r in rs or []:
                if not isinstance(r, dict):
                    continue
                try:
                    if float(r.get("price") or 0) > 10:
                        return True
                except Exception:
                    continue
            return False
        if not _has_good(rows):
            try:
                from app.market_cn.index import get_index_realtime
                rows = get_index_realtime(codes=code_list, force=True) or []
            except Exception:
                pass
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

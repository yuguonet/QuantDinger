# -*- coding: utf-8 -*-
"""盘中结构三件套 — VWAP / 开盘区间 / 分时均线（日内 1m，单日取数）。

## 解决什么

`technical_analysis` / `_analyze_trend` 等主分析链全部锁死在**日线**，
盘中「方向」没有结构量可依。本模块补的是**日内几何结构**，不是又一套评分：

  1. **vwap**        — 量加权均价（黄线/机构参考价），价在 VWAP 上/下
  2. **opening_range** — 开盘区间（默认前 15 分钟高低），突破状态
  3. **intraday_ma** — 分时均线（1m 收盘 MA5/20/60）与排列

## 数据口径（用户裁定 2026-10-23）

· **只做日内**：一次调用只吃**一个交易日**的 1m bar。跨日/多日历史会把
  取数放大一个量级，分析变慢，且方向语义也从「盘中」漂成「波段」。
· **盘后/历史**：`kline_1m_YYYY` 直读（准确，mootdx 回填，不复权原始价）。
· **盘中当日**：`kline_1m` 尚未回填（ETL ~15:30）时回落 `realtime_snapshot`
  合成 1m（与 `CNStockDataSource._snapshot_ticks_to_1m` 同口径）。
  判据同 minute_composite：**1m 有就用 1m，没有才用 snapshot**。
· **VWAP 无成交额字段**：`kline_1m` / `realtime_snapshot` 均只有 OHLCV，
  标准公式 Σ(amount)/Σ(volume) 不可用 ⇒ 用典型价近似
  `Σ((H+L+C)/3 × V) / Σ(V)`，输出 `method="hlc3"` + `approx=true`。
  与真值差通常 <0.1%，够判「价在均价上/下」；**不要**把数值当精确成交均价引用。

## 易错处

· bar 时间标签是**区间结束点**（首根 09:31 = 09:30–09:31 那一分钟）。
  开盘区间取「标签 ≤ 09:30+or_minutes」的 bar，不是「前 N 根」——
  午后停牌/缺拍时前 N 根会把下午混进开盘区间。
· `is_forming_bar=True` 时当日 bar **未走完**，VWAP/OR/MA 都是动态值，
  不得当已确认形态（同 auto-stock-analyze「今日合成 bar」纪律）。
· 单日区间查询 start==end 会被 DB 层按 00:00 截空（hub.minute_1m 已踩过），
  本模块起止窗都带时分秒，且不走 start==end 等值路径。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from ...log import logger

# 开盘区间默认长度（分钟）；允许 5/10/15/30
_DEFAULT_OR_MINUTES = 15
_VALID_OR_MINUTES = {5, 10, 15, 30}
# 分时均线默认窗口（1m 收盘 MA）
_DEFAULT_MA_WINDOWS = (5, 20, 60)


# ═══════════════════════════════════════════════════════════════
#  取数：单日 1m（kline_1m 优先，snapshot 兜底当日）
# ═══════════════════════════════════════════════════════════════

def _strip_prefix(code: str) -> str:
    """剥掉 sh./sz./.SH 等前后缀，只留 6 位裸码。"""
    s = str(code or "").strip()
    for sep in (".",):
        if sep in s:
            s = s.split(sep)[0]
    for p in ("sh", "sz", "bj", "SH", "SZ", "BJ"):
        if s.startswith(p) and len(s) > len(p):
            s = s[len(p):]
    return s.strip()


def _normalize_codes(codes) -> Tuple[List[str], List[str]]:
    """入参 codes → (raw_codes, bare_codes)。与 agent_get_kline 同容错。"""
    if isinstance(codes, (list, tuple)):
        parts = []
        for c in codes:
            if isinstance(c, (list, tuple)) and c:
                c = c[0]
            parts.append(str(c))
        codes = ",".join(parts)
    else:
        codes = str(codes)
    raw = [c.strip() for c in codes.split(",") if c.strip()][:20]
    bare = [_strip_prefix(c) for c in raw]
    return raw, bare


def _bar_time_hm(bar: Dict[str, Any]) -> str:
    """bar 时间 → 'HH:MM'（兼容 int 时间戳 / datetime / 'YYYY-MM-DD HH:MM'）。"""
    t = bar.get("time")
    if isinstance(t, datetime):
        return t.strftime("%H:%M")
    if isinstance(t, (int, float)):
        try:
            return datetime.fromtimestamp(int(t)).strftime("%H:%M")
        except (ValueError, OSError, OverflowError):
            return ""
    s = str(t or "")
    if " " in s:
        return s.split(" ")[1][:5]
    return s[-5:] if len(s) >= 5 else ""


def _bar_date_str(bar: Dict[str, Any]) -> str:
    t = bar.get("time")
    if isinstance(t, datetime):
        return t.strftime("%Y-%m-%d")
    if isinstance(t, (int, float)):
        try:
            return datetime.fromtimestamp(int(t)).strftime("%Y-%m-%d")
        except (ValueError, OSError, OverflowError):
            return ""
    return str(t or "")[:10]


def _to_float(v, default: float = 0.0) -> float:
    try:
        x = float(v)
        return x if x == x else default
    except (TypeError, ValueError):
        return default


def _fetch_kline_1m_day(code: str, date_str: str) -> List[Dict[str, Any]]:
    """从 kline_1m_YYYY 取指定交易日 1m bar（升序，OHLCV）。

    失败/无数据返回 []（不抛）。单日窗起止都带时分秒，避免 start==end 截空。
    """
    try:
        from app.utils.db_market import get_market_kline_writer
        writer = get_market_kline_writer()
        rows = writer.query(
            "CNStock", code, "1m",
            start_time=f"{date_str} 00:00:00",
            end_time=f"{date_str} 23:59:59",
            limit=0,
        )
    except Exception as e:
        logger.debug("[intraday] %s %s kline_1m 读失败: %s", code, date_str, e)
        return []
    out: List[Dict[str, Any]] = []
    for r in rows or []:
        t = r.get("time")
        if isinstance(t, datetime):
            ts = int(t.timestamp())
            d = t.strftime("%Y-%m-%d")
        else:
            try:
                ts = int(t)
            except (TypeError, ValueError):
                continue
            d = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        if d != date_str:
            continue
        out.append({
            "time": ts,
            "open": _to_float(r.get("open")),
            "high": _to_float(r.get("high")),
            "low": _to_float(r.get("low")),
            "close": _to_float(r.get("close")),
            "volume": _to_float(r.get("volume")),
        })
    out.sort(key=lambda b: b["time"])
    return out


def _fetch_snapshot_1m_day(code: str, date_str: str) -> List[Dict[str, Any]]:
    """realtime_snapshot 合成当日 1m（仅当日有效；其它日期返回 []）。"""
    try:
        from app.data_sources.cn_stock import CNStockDataSource
        ds = CNStockDataSource()
        rows = ds._read_snapshot_series(code, date_str)
        return ds._snapshot_ticks_to_1m(rows) if rows else []
    except Exception as e:
        logger.debug("[intraday] %s %s snapshot 合成失败: %s", code, date_str, e)
        return []


def fetch_day_1m(code: str, date_str: str) -> Dict[str, Any]:
    """单日 1m 序列（**日内分析唯一入口**）。

    口径：kline_1m 有数据 → 用 kline_1m（准确）；否则当日回落 snapshot。
    Returns:
        {"bars": [...], "source": "kline_1m"|"realtime_snapshot"|"", "date": date_str}
    """
    bars = _fetch_kline_1m_day(code, date_str)
    if bars:
        return {"bars": bars, "source": "kline_1m", "date": date_str}
    # 仅当日才回落 snapshot（历史日 snapshot 已清理，且语义上就是「当日实时」）
    today = datetime.now().strftime("%Y-%m-%d")
    if date_str == today:
        bars = _fetch_snapshot_1m_day(code, date_str)
        if bars:
            return {"bars": bars, "source": "realtime_snapshot", "date": date_str}
    return {"bars": [], "source": "", "date": date_str}


# ═══════════════════════════════════════════════════════════════
#  纯计算（无 IO，可单测）
# ═══════════════════════════════════════════════════════════════

def compute_vwap(bars: List[Dict[str, Any]]) -> Dict[str, Any]:
    """量加权均价（典型价近似）+ 现价相对位置。

    method="hlc3"：Σ((H+L+C)/3 × V) / Σ(V)。无成交额字段，故非精确 amount/volume。
    """
    if not bars:
        return {"error": "无 1m 数据"}
    cum_pv = 0.0
    cum_v = 0.0
    for b in bars:
        h, l, c, v = (_to_float(b.get("high")), _to_float(b.get("low")),
                      _to_float(b.get("close")), _to_float(b.get("volume")))
        if v <= 0:
            continue
        typ = (h + l + c) / 3.0 if (h or l or c) else c
        cum_pv += typ * v
        cum_v += v
    if cum_v <= 0:
        return {"error": "成交量全 0，无法计算 VWAP"}
    vwap = cum_pv / cum_v
    last = _to_float(bars[-1].get("close"))
    pct = round((last - vwap) / vwap * 100, 3) if vwap else None
    return {
        "value": round(vwap, 4),
        "method": "hlc3",
        "approx": True,
        "price": round(last, 4),
        "price_vs_vwap_pct": pct,
        "above": (last > vwap) if last else None,
    }


def _or_cutoff_hm(or_minutes: int) -> str:
    """开盘区间截止标签 'HH:MM'：09:30 + or_minutes（bar 标签=区间结束点）。"""
    base = 9 * 60 + 30 + int(or_minutes)
    return "%02d:%02d" % (base // 60, base % 60)


def compute_opening_range(bars: List[Dict[str, Any]],
                          or_minutes: int = _DEFAULT_OR_MINUTES) -> Dict[str, Any]:
    """开盘区间：09:30 起 or_minutes 内的高低 + 现价相对位置/突破状态。

    只取上午（标签 ≤ 09:30+or_minutes），不把午后混进来。
    """
    if not bars:
        return {"error": "无 1m 数据"}
    try:
        or_minutes = int(or_minutes)
    except (TypeError, ValueError):
        or_minutes = _DEFAULT_OR_MINUTES
    if or_minutes not in _VALID_OR_MINUTES:
        or_minutes = _DEFAULT_OR_MINUTES
    cutoff = _or_cutoff_hm(or_minutes)

    seg = [b for b in bars if _bar_time_hm(b) <= cutoff and _bar_time_hm(b) >= "09:30"]
    if not seg:
        # 兼容首根标签就是 09:31 的口径：上面已含；仍空则用当日最早 or_minutes 根上午 bar
        morning = [b for b in bars if _bar_time_hm(b) <= "11:30"]
        seg = morning[:or_minutes]
    if not seg:
        return {"error": "开盘区间时段无 bar", "minutes": or_minutes}

    hi = max(_to_float(b.get("high")) for b in seg)
    lo = min(_to_float(b.get("low")) for b in seg)
    hi_bar = next((b for b in seg if _to_float(b.get("high")) == hi), seg[-1])
    lo_bar = next((b for b in seg if _to_float(b.get("low")) == lo), seg[-1])
    last = _to_float(bars[-1].get("close"))
    rng = hi - lo
    range_pct = round(rng / lo * 100, 3) if lo > 0 else None

    if last > hi:
        pos = "above"
    elif last < lo:
        pos = "below"
    else:
        pos = "inside"

    return {
        "minutes": or_minutes,
        "cutoff": cutoff,
        "high": round(hi, 4),
        "low": round(lo, 4),
        "range_pct": range_pct,
        "high_at": _bar_time_hm(hi_bar),
        "low_at": _bar_time_hm(lo_bar),
        "price": round(last, 4),
        "pos": pos,
        "breakout_up": bool(last > hi),
        "breakout_down": bool(last < lo),
    }


def compute_intraday_ma(bars: List[Dict[str, Any]],
                        windows: Tuple[int, ...] = _DEFAULT_MA_WINDOWS) -> Dict[str, Any]:
    """分时均线：1m 收盘 MA + 价相对位置 + 排列。

    这里的「分时均线」= 分钟图上的 MA（不是均价线；均价线即 vwap 字段）。
    """
    if not bars:
        return {"error": "无 1m 数据"}
    closes = [_to_float(b.get("close")) for b in bars]
    n = len(closes)
    last = closes[-1]
    out: Dict[str, Any] = {}
    mas: List[Tuple[int, float]] = []
    for w in windows:
        try:
            w = int(w)
        except (TypeError, ValueError):
            continue
        if w <= 0 or n < w:
            continue
        m = sum(closes[-w:]) / w
        out["ma%d" % w] = round(m, 4)
        mas.append((w, m))
        out["price_vs_ma%d_pct" % w] = round((last - m) / m * 100, 3) if m else None

    if len(mas) >= 2:
        vals = [m for _, m in mas]
        if all(vals[i] >= vals[i + 1] for i in range(len(vals) - 1)):
            # windows 默认升序 (5,20,60) ⇒ 短期在前；短≥长 = 多头
            out["align"] = "多头" if vals[0] > vals[-1] else "缠绕"
        elif all(vals[i] <= vals[i + 1] for i in range(len(vals) - 1)):
            out["align"] = "空头" if vals[0] < vals[-1] else "缠绕"
        else:
            out["align"] = "缠绕"
    # MA5 斜率（近 5 根 MA5 的变化 %）
    if n >= 10 and 5 in dict(mas):
        closes_prev = closes[:-5]
        if len(closes_prev) >= 5:
            ma5_prev = sum(closes_prev[-5:]) / 5
            ma5_now = dict(mas)[5]
            if ma5_prev:
                out["ma5_slope_pct"] = round((ma5_now - ma5_prev) / ma5_prev * 100, 3)

    out["price"] = round(last, 4)
    out["bars"] = n
    return out


def _day_context(bars: List[Dict[str, Any]], date_str: str) -> Dict[str, Any]:
    """当日开高低收 + 现价在日内区间的位置（close_pos_in_day）。"""
    if not bars:
        return {}
    o = _to_float(bars[0].get("open"))
    h = max(_to_float(b.get("high")) for b in bars)
    lo = min(_to_float(b.get("low")) for b in bars)
    last = _to_float(bars[-1].get("close"))
    # 涨跌幅需昨收；本工具钉死「只看当日」，不拉昨日，故不给 change_pct（不猜）
    rng = h - lo
    pos = round((last - lo) / rng, 3) if rng > 0 else None
    return {
        "open": round(o, 4),
        "high": round(h, 4),
        "low": round(lo, 4),
        "last": round(last, 4),
        "close_pos_in_day": pos,
    }


# ═══════════════════════════════════════════════════════════════
#  工具入口
# ═══════════════════════════════════════════════════════════════

def _is_forming(date_str: str, source: str) -> bool:
    """当日 bar 是否未走完（盘中 / 仍靠 snapshot 合成）。"""
    today = datetime.now().strftime("%Y-%m-%d")
    if date_str != today:
        return False
    now = datetime.now()
    if now.hour * 60 + now.minute < 15 * 60:
        return True
    return source == "realtime_snapshot"


def _parse_ma_windows(s: str) -> Tuple[int, ...]:
    if not s:
        return _DEFAULT_MA_WINDOWS
    out = []
    for p in str(s).split(","):
        p = p.strip()
        if not p:
            continue
        try:
            w = int(p)
        except ValueError:
            continue
        if 1 <= w <= 240:
            out.append(w)
    return tuple(out) if out else _DEFAULT_MA_WINDOWS


def get_intraday_structure(codes: str, date: str = "",
                           or_minutes: int = 15,
                           ma_windows: str = "5,20,60") -> Dict[str, Any]:
    """盘中结构三件套：VWAP + 开盘区间 + 分时均线（**单日 1m**，日内分析用）。

    一次调用只分析**一个交易日**——跨日请自行多次调用；多日历史请走日线工具。
    盘后/历史取 kline_1m_YYYY（准确）；盘中当日 kline_1m 未回填时自动用
    realtime_snapshot 合成，source 字段标明来源。

    Args:
        codes: 股票代码，多股逗号分隔
        date: 交易日 YYYY-MM-DD，默认今天
        or_minutes: 开盘区间长度（分钟），可选 5/10/15/30，默认 15
        ma_windows: 分时均线窗口，逗号分隔，默认 "5,20,60"

    Returns:
        {"count": N, "data": {代码: {
            "date", "bars", "source", "is_forming_bar",
            "vwap": {value, method, approx, price, price_vs_vwap_pct, above},
            "opening_range": {minutes, high, low, range_pct, pos,
                              breakout_up, breakout_down, high_at, low_at},
            "intraday_ma": {ma5/ma20/ma60, price_vs_ma*, align, ...},
            "day": {open, high, low, last, close_pos_in_day}
        }}, "error": None}
        单码/多码同构；失败码在 data[码] 内以 {"error": ...} 给出。
        ⚠ vwap.approx=true：无成交额字段，HLC3 典型价近似，勿当精确成交均价。
    """
    raw_codes, bare_codes = _normalize_codes(codes)
    if not bare_codes:
        return {"error": "codes 不能为空", "retriable": False}

    date_str = (date or "").strip()[:10] or datetime.now().strftime("%Y-%m-%d")
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return {"error": f"date 无效: {date}，应为 YYYY-MM-DD", "retriable": False}

    try:
        or_minutes = int(or_minutes)
    except (TypeError, ValueError):
        or_minutes = _DEFAULT_OR_MINUTES
    if or_minutes not in _VALID_OR_MINUTES:
        or_minutes = _DEFAULT_OR_MINUTES
    windows = _parse_ma_windows(ma_windows)

    results: Dict[str, Any] = {}
    for raw, code in zip(raw_codes, bare_codes):
        fetched = fetch_day_1m(code, date_str)
        bars = fetched.get("bars") or []
        if not bars:
            results[raw] = {"error": f"{date_str} 无 1m 数据（source 空）", "date": date_str}
            continue
        source = fetched.get("source") or ""
        results[raw] = {
            "date": date_str,
            "bars": len(bars),
            "source": source,
            "is_forming_bar": _is_forming(date_str, source),
            "vwap": compute_vwap(bars),
            "opening_range": compute_opening_range(bars, or_minutes),
            "intraday_ma": compute_intraday_ma(bars, windows),
            "day": _day_context(bars, date_str),
        }

    return {"count": len(results), "data": results, "error": None}

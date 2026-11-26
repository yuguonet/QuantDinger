# -*- coding: utf-8 -*-
"""stock-analyzer/_levels.py —— 支撑/压力位的多方法聚合与交叉验证（反复推敲演算层）。

【定位】给 analyze() 的四要素（方向/触发/证伪/价位）提供**可验证**的关键价位：
每侧输出 1-2 个支撑位与压力位，且每个价位都带「哪几类方法算出来的、近 30 日
碰到几次、最近一次守住没有」的演算痕迹。单方法孤证的价位明确标注
「未经交叉验证（证据不足）」，不上升为结论价位。

【8 类独立方法】（同一根日线，互不复用同一算式）：
  均线   ma5/ma10/ma20/ma60
  枢轴   经典枢轴 P/S1/S2/R1/R2（最近一根 OHLC 推演）
  波段   分形摆动高低点（±2 根确认的波段峰/谷，近 60 根取最近 3 个）
  区间   hi20/lo10/lo20/lo60/hi60
  缺口   近 20 根未回补跳空缺口的上下沿
  密集区 近 60 根量价分布的成交密集峰（POC）
  涨停   最近一次涨停的起点（板前收盘价）
  引擎   deep 层 tech_summary._key_levels 的关键位候选（ma/lo/hi/gap/poc/zt 合集）

【推敲与演算（每轮做什么）】
  第 1 轮 聚类：8 类方法的候选价按 ±1%（相对现价）聚为同一位置，取中位数为代表价；
  第 2 轮 测试统计：近 30 根中触及该位置（影线触及或穿越）的次数——**触碰且当日收回
          才算有效测试**（touches），穿越不收回只计 broken（趋势段穿越会虚增触碰数，
          不能拿来验证）；另记最近一次测试是否收回（held_last）；
  第 3 轮 资格判定：≥2 类方法聚合 或 ≥2 次有效测试 ⇒ verified=True，否则为孤证；
  第 4 轮 排序取用：verified 优先，再按 聚合方法数↓ 有效测试数↓ 距离近↑ 取每侧前 2 个。
  支撑/压力按现价分侧（现价下方=支撑、上方=压力），与 deep 层口径一致。

【易错点】
  · bars 形状 = fetch_kline 出口：{time:"YYYY-MM-DD", open, high, low, close, volume}，
    数值可能是 str/None，一律经 _f() 归一，坏行丢弃不填占位；
  · 现价 rt 与日线收盘可能不同步（盘中/日线滞后）：分侧用**日线收盘**（全层同源），
    dist_pct 同口径，调用方要换现价口径需自行重算，勿两套价并存；
  · 无候选的一侧返回空列表（不是伪造价位）——「该侧未知」是合法答案。
"""
from __future__ import annotations

import statistics
from typing import Any, Dict, List, Optional, Tuple

from . import _inner

#: 聚类容差（相对现价 %）：同一位置的不同方法在此范围内互相印证
_CLUSTER_TOL_PCT = 1.0
#: 触碰判定容差（相对目标价 %）
_TOUCH_TOL_PCT = 1.2
#: 触碰统计回看根数
_TOUCH_WIN = 30
#: 只保留每侧前 N 个价位（用户要求 1-2 个）
_SIDE_KEEP = 2


def _f(v: Any) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if x != x else x


def _norm_bars(bars: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for b in bars or []:
        if not isinstance(b, dict):
            continue
        c = _f(b.get("close"))
        if c <= 0:
            continue
        out.append({
            "date": str(b.get("time") or b.get("date") or "")[:10],
            "open": _f(b.get("open")) or c,
            "high": _f(b.get("high")) or c,
            "low": _f(b.get("low")) or c,
            "close": c,
            "volume": _f(b.get("volume")),
        })
    return out


def _ma_at(closes: List[float], period: int) -> Optional[float]:
    if len(closes) < period or period <= 0:
        return None
    return sum(closes[-period:]) / period


def _pivot_set(b: Dict[str, Any]) -> Dict[str, float]:
    """经典枢轴（最近一根 OHLC）：P 及两侧 S1/S2、R1/R2。"""
    p = (b["high"] + b["low"] + b["close"]) / 3.0
    span = b["high"] - b["low"]
    return {
        "枢轴P": p,
        "枢轴S1": 2 * p - b["high"],
        "枢轴S2": p - span,
        "枢轴R1": 2 * p - b["low"],
        "枢轴R2": p + span,
    }


def _swing_points(bars: List[Dict[str, Any]], keep: int = 3) -> Tuple[List[float], List[float]]:
    """分形摆动高低点：±2 根确认的波段峰/谷，各取最近 keep 个。"""
    highs: List[float] = []
    lows: List[float] = []
    for i in range(2, len(bars) - 2):
        win_h = [bars[j]["high"] for j in range(i - 2, i + 3)]
        win_l = [bars[j]["low"] for j in range(i - 2, i + 3)]
        if bars[i]["high"] >= max(win_h):
            highs.append(bars[i]["high"])
        if bars[i]["low"] <= min(win_l):
            lows.append(bars[i]["low"])
    return highs[-keep:], lows[-keep:]


def _volume_poc(bars: List[Dict[str, Any]], lookback: int = 60, bins: int = 40) -> Optional[float]:
    """近 lookback 根量价分布的成交密集峰（POC）：典型价加权成交量直方图的最重 bin 中心。"""
    win = bars[-lookback:]
    if len(win) < 5:
        return None
    lo = min(b["low"] for b in win)
    hi = max(b["high"] for b in win)
    if hi <= lo:
        return None
    step = (hi - lo) / bins
    acc = [0.0] * bins
    for b in win:
        tp = (b["high"] + b["low"] + b["close"]) / 3.0
        idx = min(bins - 1, max(0, int((tp - lo) / step)))
        acc[idx] += b["volume"] or 0.0
    if max(acc) <= 0:
        return None
    k = acc.index(max(acc))
    return round(lo + step * (k + 0.5), 2)


def _gap_edges(bars: List[Dict[str, Any]], lookback: int = 20) -> List[Tuple[float, str]]:
    """近 lookback 根内**未回补**跳空缺口的上下沿。回补判据：缺口方向的收盘穿越。"""
    out: List[Tuple[float, str]] = []
    win = bars[-lookback:]
    for i in range(1, len(win)):
        prev, cur = win[i - 1], win[i]
        later = win[i + 1:]
        if cur["low"] > prev["high"]:          # 向上跳空：prev.high 为支撑沿
            filled = any(x["close"] <= prev["high"] for x in later)
            if not filled:
                out.append((prev["high"], "gap上沿"))
        if cur["high"] < prev["low"]:          # 向下跳空：prev.low 为压力沿
            filled = any(x["close"] >= prev["low"] for x in later)
            if not filled:
                out.append((prev["low"], "gap下沿"))
    return out[-3:]


def _zt_origin(bars: List[Dict[str, Any]], code: str, name: str, lookback: int = 10) -> Optional[float]:
    """最近一次涨停的起点（板前收盘）：连板行情的回撤参考位。"""
    lp = _inner.limit_pct(code, name)
    if lp <= 0:
        return None
    for i in range(len(bars) - 1, max(1, len(bars) - lookback) - 1, -1):
        pc = bars[i - 1]["close"]
        if pc and (bars[i]["close"] - pc) / pc * 100 >= lp - 0.8:
            return round(pc, 2)
    return None


def _candidates(bars: List[Dict[str, Any]], code: str, name: str,
                tech: Optional[Dict[str, Any]]) -> List[Tuple[float, str, str]]:
    """8 类方法的候选价位：(px, family, kind)。"""
    closes = [b["close"] for b in bars]
    last = bars[-1]
    cands: List[Tuple[float, str, str]] = []

    for period in (5, 10, 20, 60):
        v = _ma_at(closes, period)
        if v:
            cands.append((v, "均线", f"ma{period}"))

    for kind, px in _pivot_set(last).items():
        cands.append((px, "枢轴", kind))

    sw_h, sw_l = _swing_points(bars)
    for px in sw_l:
        cands.append((px, "波段", "波段低"))
    for px in sw_h:
        cands.append((px, "波段", "波段高"))

    win20 = bars[-20:]
    win60 = bars[-60:]
    cands.append((max(b["high"] for b in win20), "区间", "hi20"))
    cands.append((min(b["low"] for b in win20), "区间", "lo20"))
    if len(bars) >= 10:
        cands.append((min(b["low"] for b in bars[-10:]), "区间", "lo10"))
    if len(bars) >= 60:
        cands.append((min(b["low"] for b in win60), "区间", "lo60"))
        cands.append((max(b["high"] for b in win60), "区间", "hi60"))

    for px, kind in _gap_edges(bars):
        cands.append((px, "缺口", kind))

    poc = _volume_poc(bars)
    if poc:
        cands.append((poc, "密集区", "POC"))

    zt = _zt_origin(bars, code, name)
    if zt:
        cands.append((zt, "涨停起点", "板前收盘"))

    sup = (tech or {}).get("support") or {}
    for item in (sup.get("supports") or []) + (sup.get("resists") or []):
        if isinstance(item, dict) and item.get("px"):
            cands.append((_f(item["px"]), "引擎", str(item.get("k") or "key")))

    return [(round(px, 2), fam, kind) for px, fam, kind in cands if px and px > 0]


def _cluster(cands: List[Tuple[float, str, str]], close: float) -> List[Dict[str, Any]]:
    """第 1 轮：±tol% 聚类。代表价=成员中位数；family 取并集（同 family 多 kind 只算一类）。"""
    tol = close * _CLUSTER_TOL_PCT / 100.0
    groups: List[List[Tuple[float, str, str]]] = []
    for px, fam, kind in sorted(cands, key=lambda x: x[0]):
        if groups and abs(px - statistics.median(g[0] for g in groups[-1])) <= tol:
            groups[-1].append((px, fam, kind))
        else:
            groups.append([(px, fam, kind)])
    out: List[Dict[str, Any]] = []
    for g in groups:
        px = round(statistics.median(x[0] for x in g), 2)
        fams = sorted({x[1] for x in g})
        kinds = sorted({f"{x[1]}·{x[2]}" for x in g})
        short = list(dict.fromkeys(x.split("·")[-1] for x in kinds))
        name = "、".join(short[:3]) + (f" 等{len(short)}项" if len(short) > 3 else "")
        out.append({"px": px, "name": name, "methods": kinds, "families": fams})
    return out


def _touch_stat(bars: List[Dict[str, Any]], px: float, side: str) -> Dict[str, Any]:
    """第 2 轮：近 _TOUCH_WIN 根内该位置的测试统计（推敲演算）。

    口径（2026-11-03 修正）：影线触及或穿越都算「测试」，但**穿越不收回不算
    有效测试**（broken）——趋势段穿越会虚增触碰数，拿它验证支撑会误判。
    touches = 测试且当日收回（支撑=收在上方，压力=收在下方）的次数，计入验证强度；
    broken = 测试但当日失守的次数，只作风险事实；held_last = 最近一次测试是否收回。
    """
    win = bars[-_TOUCH_WIN:]
    touches = 0
    broken = 0
    held_last: Optional[bool] = None
    for b in win:
        if side == "sup":
            near = abs(b["low"] - px) / px * 100 <= _TOUCH_TOL_PCT
        else:
            near = abs(b["high"] - px) / px * 100 <= _TOUCH_TOL_PCT
        spanned = b["low"] <= px <= b["high"]
        if not (near or spanned):
            continue
        held = (b["close"] >= px) if side == "sup" else (b["close"] <= px)
        if held:
            touches += 1
        else:
            broken += 1
        held_last = held
    return {"touches": touches, "broken": broken, "held_last": held_last}


def build_levels(bars: List[Dict[str, Any]], code: str = "", name: str = "",
                 tech: Optional[Dict[str, Any]] = None,
                 close: Optional[float] = None) -> Dict[str, Any]:
    """聚合 8 类方法给出每侧 1-2 个支撑/压力位，附推敲演算痕迹。

    Returns:
        {ok, close, as_of, supports:[level], resists:[level], checks:[str], note}
        level = {px, dist_pct, name, methods[], families[], touches, broken, held_last,
                 verified, score, verdict}
        verified=False 的 level 只可当参考（孤证，证据不足），结论价位优先用 verified。
        touches=有效测试（触碰且收回）次数，broken=测试但失守次数；两者分开计，
        验证强度只看 touches。
        nearest_support: 距现价最近且 verified 的支撑（≤STOP_NEAREST_MAX_PCT，默认 6%），
        作短档止损参考位；无 verified 支撑时为 None（不拿孤证当止损参考）。
        一侧无候选 ⇒ 该侧 []，由调用方写「未知」。
    """
    norm = _norm_bars(bars or [])
    if len(norm) < 5:
        return {"ok": False, "reason": "日线不足 5 根，价位无法推演", "supports": [], "resists": [], "checks": []}

    px_now = _f(close) or norm[-1]["close"]
    cands = _candidates(norm, code, name, tech)
    if not cands:
        return {"ok": False, "reason": "无候选价位", "supports": [], "resists": [], "checks": []}

    clusters = _cluster(cands, px_now)
    checks: List[str] = []
    sides: Dict[str, List[Dict[str, Any]]] = {"sup": [], "res": []}
    for cl in clusters:
        side = "sup" if cl["px"] < px_now else "res"
        ts = _touch_stat(norm, cl["px"], side)
        dist = round((cl["px"] - px_now) / px_now * 100, 2)
        fam_n = len(cl["families"])
        verified = fam_n >= 2 or ts["touches"] >= 2
        score = (2 * fam_n + min(ts["touches"], 3)
                 + (1 if abs(dist) <= 3.0 else 0) + (1 if ts["held_last"] else 0))
        if verified:
            verdict = "交叉验证通过（%d 类方法聚合，30 日有效测试 %d 次%s%s）" % (
                fam_n, ts["touches"],
                "，最近守住" if ts["held_last"] else ("，最近未守住" if ts["held_last"] is False else ""),
                f"，另失守 {ts['broken']} 次" if ts["broken"] else "")
        else:
            verdict = "孤证（仅 %s，证据不足）" % "、".join(cl["methods"])
        item = {
            "px": cl["px"],
            "dist_pct": dist,
            "name": cl["name"],
            "methods": cl["methods"],
            "families": cl["families"],
            "touches": ts["touches"],
            "broken": ts["broken"],
            "held_last": ts["held_last"],
            "verified": verified,
            "score": score,
            "verdict": verdict,
        }
        sides[side].append(item)
        checks.append(f"{cl['px']}（{'支撑' if side == 'sup' else '压力'}，{dist:+.2f}%）← "
                      f"{' + '.join(cl['methods'])}｜有效测试 {ts['touches']} 次"
                      f"（失守 {ts['broken']} 次）｜{item['verdict']}")

    def _pick(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        rows.sort(key=lambda r: (not r["verified"], -r["score"], abs(r["dist_pct"])))
        return rows[:_SIDE_KEEP]

    picked_sup = _pick(sides["sup"])
    picked_res = _pick(sides["res"])
    near_max_pct = _inner.env_float("QD_STOCK_ANALYZER_STOP_NEAREST_MAX_PCT", 6.0)
    nearest = [r for r in picked_sup if r["verified"] and abs(r["dist_pct"]) <= near_max_pct]
    nearest.sort(key=lambda r: abs(r["dist_pct"]))
    out = {
        "ok": True,
        "close": round(px_now, 2),
        "as_of": norm[-1]["date"],
        "supports": picked_sup,
        "resists": picked_res,
        "nearest_support": nearest[0] if nearest else None,
        "checks": checks,
        "note": "8 类方法（均线/枢轴/波段/区间/缺口/密集区/涨停起点/关键位引擎）±1% 聚类交叉验证；"
                "verified=至少 2 类方法聚合或 30 日内有效测试（触碰且收回）≥2 次，孤证价位仅可参考",
    }
    if not out["supports"]:
        checks.append("支撑侧无候选：现价下方无可推演价位（答：未知）")
    if not out["resists"]:
        checks.append("压力侧无候选：现价上方无可推演价位（答：未知）")
    return out

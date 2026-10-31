# -*- coding: utf-8 -*-
"""stock-analyzer 内部指标体系 —— 本技能独占，不与任何其他技能共享。

设计原则：skill 是自包含黑盒。指标算法、A 股涨跌停规则、形态几何全部在此
独立实现，**不 import 任何其他 skill，也不被其他 skill import**。与
market-screener 存在算法同形是刻意保留的：两者口径需各自演化，共享会让
改一边悄悄改另一边。

对外契约（run.py 只用下面这几个）：
    load_bars(code, days)     日线 [{date, time, open, high, low, close, volume}]
                              time 为 int 时间戳（predict_next_day 依赖），失败返回 []
    tech(bars, code, name)    技术面事实；日线不足返回 {"ok": False, "reason": ...}
    detect(bars, params)      形态列表；无形态返回 []
    zt_days(bars, code, name, n)  近 n 个交易日的涨停记录

字段命名与 run.py 的判定逻辑绑定（support.near_support / ma_bull_stack /
pos_in_20d_pct / dist_ma20_pct / rsi14 / vol_ratio），改名前先看 run.py 的 _conds_for。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from ...log import logger

_TZ = ZoneInfo("Asia/Shanghai")

_BARS_DEFAULT = 120

_PATTERN_PARAMS: Dict[str, float] = {
    "platform_range_pct": 8.0,
    "breakout_vol_ratio": 1.3,
    "surge_vol_ratio": 1.8,
    "flat_change_pct": 5.0,
    "ma_touch_pct": 1.5,
    "min_pullback_pct": 3.0,
    "max_pullback_pct": 12.0,
    "shrink_ratio": 0.7,
    "rebreak_vol_ratio": 1.5,
}


def _f(v: Any) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if x != x else x


def _date_str(v: Any) -> Optional[str]:
    """把 'YYYY-MM-DD' / 'YYYY/MM/DD' / int 时间戳 统一成日期串；认不出返回 None。"""
    if isinstance(v, str):
        s = v.strip()[:10].replace("/", "-")
        try:
            datetime.strptime(s, "%Y-%m-%d")
        except ValueError:
            return None
        return s
    if isinstance(v, (int, float)):
        try:
            return datetime.fromtimestamp(float(v), _TZ).strftime("%Y-%m-%d")
        except (OSError, OverflowError, ValueError):
            return None
    return None


def load_bars(code: str, days: Optional[int] = None) -> List[Dict[str, Any]]:
    """个股日线，升序，含 `time`(int 时间戳)。取不到返回 []（调用方按缺口处理）。

    日期解析不出或价格非正的行整行丢弃，不填占位值 —— 占位会污染后续均线与预测。
    """
    n = int(days or _BARS_DEFAULT)
    try:
        from app.data_sources.cn_stock import CNStockDataSource
        raw = CNStockDataSource().get_kline(code, "1D", n)
    except Exception as e:
        logger.debug("[stock-analyzer] 取 %s 日线失败: %s: %s", code, type(e).__name__, e)
        return []

    out: List[Dict[str, Any]] = []
    for k in (raw or []):
        if not isinstance(k, dict):
            continue
        d = _date_str(k.get("date") or k.get("time"))
        if not d:
            continue
        close = _f(k.get("close"))
        if close <= 0:
            continue
        out.append({
            "date": d,
            "time": int(datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=_TZ).timestamp()),
            "open": _f(k.get("open")),
            "high": _f(k.get("high")),
            "low": _f(k.get("low")),
            "close": close,
            "volume": _f(k.get("volume")),
        })
    return out


def ma(closes: List[float], period: int) -> List[Optional[float]]:
    n = len(closes)
    out: List[Optional[float]] = [None] * n
    if period <= 0:
        return out
    for i in range(period - 1, n):
        out[i] = sum(closes[i - period + 1:i + 1]) / period
    return out


def ema(values: List[float], period: int) -> List[float]:
    if not values:
        return []
    k = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def macd(closes: List[float]) -> Dict[str, List[float]]:
    e12, e26 = ema(closes, 12), ema(closes, 26)
    dif = [a - b for a, b in zip(e12, e26)]
    dea = ema(dif, 9)
    return {"dif": dif, "dea": dea, "macd": [2 * (d - s) for d, s in zip(dif, dea)]}


def rsi(closes: List[float], period: int = 14) -> List[float]:
    n = len(closes)
    if n < period + 1:
        return [50.0] * n
    gains, losses = [], []
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag, al = sum(gains) / period, sum(losses) / period
    out = [50.0] * (period + 1)
    out[period] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
    alpha = 1.0 / period
    for i in range(period + 1, n):
        d = closes[i] - closes[i - 1]
        ag = alpha * max(d, 0.0) + (1 - alpha) * ag
        al = alpha * max(-d, 0.0) + (1 - alpha) * al
        out.append(100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al))
    return out


def limit_pct(code: str, name: str = "") -> float:
    """涨跌停幅度（%），0.0 表示无涨跌幅限制。

    ST 跟随所在板（主板 ±10%、创业/科创 ±20%），不单独 ±5%；简称 N/C 开头为新股无限制。
    北证段 43/82/83/87/88/92 一律 ±30%。
    """
    code = (code or "").strip()
    name = (name or "").strip()
    if len(name) >= 2 and name[0] in ("N", "C"):
        return 0.0
    if code.startswith(("300", "301", "688", "689")):
        return 20.0
    if code.startswith(("8", "4", "92")):
        return 30.0
    return 10.0


def limit_locked(code: str, name: str, close: float, prev_close: float) -> bool:
    """当日是否涨停封板（买不进）。无涨跌幅限制的标的不存在封板。"""
    if prev_close <= 0 or close <= 0:
        return False
    lp = limit_pct(code, name)
    if lp <= 0:
        return False
    return (close - prev_close) / prev_close * 100 >= lp - 0.5


def _prev_streak(bars: List[Dict], lp: float, streak: int) -> int:
    """截至上一交易日收盘的连板高度。今日仍涨停 ⇒ streak-1；今日断板 ⇒ 从昨日往前数。"""
    if lp <= 0:
        return 0
    if streak >= 1:
        return streak - 1
    idx, n = len(bars) - 2, 0
    while idx > 0:
        pc = bars[idx - 1]["close"]
        if pc and (bars[idx]["close"] - pc) / pc * 100 >= lp - 0.8:
            n += 1
            idx -= 1
        else:
            break
    return n


def tech(bars: List[Dict], code: str = "", name: str = "") -> Dict[str, Any]:
    """从日线算位置 / 量能 / 强度 / 涨停史。不含评分，也不含买卖建议。

    `support.near_support` 的定义是「最近均线距离 ≤ 3%」，与 run.py 的
    「贴支撑」判定同源，改 3.0 就要同步改 docs 里的口径说明。
    """
    if not bars or len(bars) < 2:
        return {"ok": False, "reason": "日线不足"}

    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    vols = [b["volume"] for b in bars]

    today, prev = bars[-1], bars[-2]
    close = today["close"]
    chg = (close - prev["close"]) / prev["close"] * 100 if prev["close"] else 0.0

    prev_vols = vols[-6:-1]
    vr = None
    if prev_vols and sum(prev_vols) > 0:
        vr = today["volume"] / (sum(prev_vols) / len(prev_vols))

    ma5, ma10, ma20 = ma(closes, 5), ma(closes, 10), ma(closes, 20)
    m5, m10, m20 = ma5[-1], ma10[-1], ma20[-1]
    dist_ma20 = ((close - m20) / m20 * 100) if m20 else None

    win20 = min(20, len(bars))
    hi20, lo20 = max(highs[-win20:]), min(lows[-win20:])
    pos20 = ((close - lo20) / (hi20 - lo20) * 100) if hi20 > lo20 else None

    ret5 = ((close / closes[-6]) - 1) * 100 if len(closes) > 6 else None
    ret20 = ((close / closes[-win20]) - 1) * 100 if len(closes) > win20 else None

    lp = limit_pct(code, name)
    streak = 0
    if lp > 0:
        for i in range(len(bars) - 1, 0, -1):
            pc = bars[i - 1]["close"]
            if pc and (bars[i]["close"] - pc) / pc * 100 >= lp - 0.8:
                streak += 1
            else:
                break

    def _d(v):
        return round((close - v) / v * 100, 2) if v and close else None

    d5, d10 = _d(m5), _d(m10)
    cands = [("ma5", d5), ("ma10", d10), ("ma20", dist_ma20)]
    valid = [c for c in cands if c[1] is not None and abs(c[1]) <= 3.0]
    nearest = min(valid, key=lambda c: abs(c[1])) if valid else None

    rng = today["high"] - today["low"]
    intraday = (close - today["low"]) / rng * 100 if rng > 0 else None

    return {
        "ok": True,
        "bars": len(bars),
        "close": round(close, 2),
        "change_pct": round(chg, 2),
        "amplitude_pct": round(rng / prev["close"] * 100, 2) if prev["close"] and rng else None,
        "close_pos_in_day": round(intraday, 1) if intraday is not None else None,
        "vol_ratio": round(vr, 2) if vr else None,
        "rsi14": round(rsi(closes)[-1], 1),
        "ma5": round(m5, 2) if m5 else None,
        "ma10": round(m10, 2) if m10 else None,
        "ma20": round(m20, 2) if m20 else None,
        "dist_ma5_pct": d5,
        "dist_ma10_pct": d10,
        "dist_ma20_pct": round(dist_ma20, 2) if dist_ma20 is not None else None,
        "support": {
            "nearest": nearest[0] if nearest else None,
            "nearest_dist_pct": nearest[1] if nearest else None,
            "near_support": bool(nearest),
            "ma5": round(m5, 2) if m5 else None,
            "ma10": round(m10, 2) if m10 else None,
            "ma20": round(m20, 2) if m20 else None,
        },
        "ma_bull_stack": bool(m5 and m10 and m20 and m5 > m10 > m20),
        "pos_in_20d_pct": round(pos20, 1) if pos20 is not None else None,
        "is_20d_high": bool(pos20 is not None and pos20 >= 99),
        "ret_5d_pct": round(ret5, 2) if ret5 is not None else None,
        "ret_20d_pct": round(ret20, 2) if ret20 is not None else None,
        "limit_up": bool(limit_locked(code, name, close, prev["close"])),
        "zt_streak": streak,
        "prev_zt_streak": _prev_streak(bars, lp, streak),
    }


def zt_days(bars: List[Dict], code: str, name: str, n: int = 8) -> List[Dict[str, Any]]:
    """近 n 个交易日的涨停记录（自算，不依赖涨停池缓存）。无涨停返回 []。"""
    lp = limit_pct(code, name)
    if lp <= 0 or len(bars) < 2:
        return []
    out: List[Dict[str, Any]] = []
    for i in range(max(1, len(bars) - n), len(bars)):
        pc = bars[i - 1]["close"]
        if not pc:
            continue
        chg = (bars[i]["close"] - pc) / pc * 100
        if chg < lp - 0.8:
            continue
        streak, j = 1, i - 1
        while j > 0:
            p2 = bars[j - 1]["close"]
            if p2 and (bars[j]["close"] - p2) / p2 * 100 >= lp - 0.8:
                streak += 1
                j -= 1
            else:
                break
        out.append({"date": bars[i]["date"], "change_pct": round(chg, 2), "streak": streak})
    return out


def _p_platform_breakout(bars: List[Dict], p: Dict) -> Optional[Dict]:
    if len(bars) < 10:
        return None
    recent, today = bars[-6:-1], bars[-1]
    p_high = max(b["high"] for b in recent)
    p_low = min(b["low"] for b in recent)
    if p_high <= 0:
        return None
    range_pct = (p_high - p_low) / p_high * 100
    if range_pct > p["platform_range_pct"]:
        return None
    if today["close"] <= p_high or today["close"] <= today["open"]:
        return None
    avg_vol = sum(b["volume"] for b in recent) / len(recent)
    vr = today["volume"] / avg_vol if avg_vol > 0 else 0
    if vr <= p["breakout_vol_ratio"]:
        return None
    return {"pattern": "平台突破", "platform_high": round(p_high, 2),
            "range_pct": round(range_pct, 1), "vol_ratio": round(vr, 2)}


def _p_volume_reversal(bars: List[Dict], p: Dict) -> Optional[Dict]:
    if len(bars) < 10:
        return None
    recent, today = bars[-6:-1], bars[-1]
    vols = [b["volume"] for b in recent]
    ct = [b["close"] for b in recent]
    avg_vol = sum(vols) / len(vols) if vols else 0
    if not ct or ct[0] <= 0 or avg_vol <= 0:
        return None
    period_change = (ct[-1] - ct[0]) / ct[0] * 100
    shrinking = all(v <= avg_vol * 1.1 for v in vols[-3:])
    surge = today["volume"] > avg_vol * p["surge_vol_ratio"] and today["close"] > today["open"]
    if shrinking and surge and abs(period_change) < p["flat_change_pct"]:
        return {"pattern": "底部放量启动", "vol_ratio": round(today["volume"] / avg_vol, 2),
                "period_change_pct": round(period_change, 1)}
    return None


def _p_ma_support_pullback(bars: List[Dict], p: Dict) -> Optional[Dict]:
    if len(bars) < 20:
        return None
    closes = [b["close"] for b in bars]
    m5, m10, m20 = ma(closes, 5), ma(closes, 10), ma(closes, 20)
    if not (m5[-1] and m10[-1] and m20[-1]):
        return None
    if not (m5[-1] > m10[-1] > m20[-1]):
        return None
    today = bars[-1]
    dist = abs(today["low"] - m10[-1]) / m10[-1] * 100
    if dist < p["ma_touch_pct"] and today["close"] > m10[-1]:
        return {"pattern": "均线支撑回踩", "ma10": round(m10[-1], 2),
                "low": round(today["low"], 2), "distance_pct": round(dist, 2),
                "close_above_open": today["close"] > today["open"]}
    return None


def _p_macd_golden_cross(bars: List[Dict], p: Dict) -> Optional[Dict]:
    if len(bars) < 30:
        return None
    m = macd([b["close"] for b in bars])
    dif, dea = m["dif"], m["dea"]
    if dif[-1] >= dea[-1] and dif[-2] < dea[-2]:
        return {"pattern": "MACD金叉", "dif": round(dif[-1], 4),
                "dea": round(dea[-1], 4), "underwater": bool(dif[-1] < 0)}
    return None


def _p_shrink_pullback_breakout(bars: List[Dict], p: Dict) -> Optional[Dict]:
    if len(bars) < 15:
        return None
    today = bars[-1]
    recent = bars[-11:-1]
    highs = [b["high"] for b in recent]
    peak = max(highs)
    peak_idx = highs.index(peak)
    if peak_idx >= len(recent) - 2:
        return None
    pullback = recent[peak_idx + 1:]
    pb_low = min(b["low"] for b in pullback)
    pb_pct = (peak - pb_low) / peak * 100
    if pb_pct < p["min_pullback_pct"] or pb_pct > p["max_pullback_pct"]:
        return None
    avg_pb = sum(b["volume"] for b in pullback) / len(pullback)
    pk = recent[max(0, peak_idx - 2):peak_idx + 1]
    avg_pk = sum(b["volume"] for b in pk) / len(pk) if pk else 1
    pb_high = max(b["high"] for b in pullback)
    if not (avg_pb < avg_pk * p["shrink_ratio"]
            and today["close"] > pb_high and today["close"] > today["open"]
            and today["volume"] > avg_pb * p["rebreak_vol_ratio"]):
        return None
    return {"pattern": "缩量回调放量突破", "peak": round(peak, 2),
            "pullback_pct": round(pb_pct, 1),
            "vol_ratio": round(today["volume"] / avg_pb, 2) if avg_pb else None}


def _p_prev_high_breakout(bars: List[Dict], p: Dict) -> Optional[Dict]:
    if len(bars) < 21:
        return None
    today = bars[-1]
    prev_high = max(b["high"] for b in bars[-21:-1])
    if today["close"] <= prev_high or today["close"] <= today["open"]:
        return None
    vol_5 = sum(b["volume"] for b in bars[-6:-1]) / 5
    vr = today["volume"] / vol_5 if vol_5 > 0 else 0
    if vr <= p["breakout_vol_ratio"]:
        return None
    return {"pattern": "突破前高", "prev_high": round(prev_high, 2),
            "close": round(today["close"], 2), "vol_ratio": round(vr, 2)}


_DETECTORS: Tuple = (
    _p_platform_breakout, _p_volume_reversal, _p_ma_support_pullback,
    _p_macd_golden_cross, _p_shrink_pullback_breakout, _p_prev_high_breakout,
)


def detect(bars: List[Dict], params: Optional[Dict[str, float]] = None) -> List[Dict]:
    """识别 K 线形态。只回答「是不是」，不回答「好不好」。单个检测器异常不影响其余。"""
    p = dict(_PATTERN_PARAMS)
    p.update({k: float(v) for k, v in (params or {}).items() if k in p})
    found: List[Dict] = []
    for fn in _DETECTORS:
        try:
            hit = fn(bars or [], p)
        except Exception as e:
            logger.debug("[stock-analyzer] 形态 %s 异常: %s", fn.__name__, e)
            continue
        if hit:
            found.append(hit)
    return found

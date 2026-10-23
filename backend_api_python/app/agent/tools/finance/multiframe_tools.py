# -*- coding: utf-8 -*-
"""多周期方向 — 日线定势 + 15m/5m 定节奏（几何方向 + 一致/冲突，非评分）。

## 解决什么

`technical_analysis` / `bull_bear_research` 是**两套 0-100 评分**，同股同数据
会打架；且全部锁死日线，没有「日线定势 + 分钟定节奏」的对照。本模块只回答
一个问题：**各周期方向是什么、它们是否一致？**

    日线（定势）→ 15m（节奏）→ 5m（入场）

输出是 `up / down / range` + `aligned_* / conflict / partial_*`，
**不是**又一个分数。如何解读写在 SKILL/提示层，本层只给客观几何量。

## 数据口径（与 intraday_tools 同源，2026-10-23）

· **日线**：`CNStockDataSource.get_kline(1D)`，默认 60 根（定势需要 MA20/60）。
· **分钟**：**只吃一个交易日**的 1m（跨日取数会把分析拖慢、方向语义也漂）。
  盘后/历史 → `kline_1m_YYYY`；盘中当日 → snapshot 合成。见
  `intraday_tools.fetch_day_1m`。
· **15m/5m 由 1m 按交易分钟桶聚合**（午休归桶，不按「前 N 根」硬切）——
  停牌缺拍时按根数切会把下午混进上午桶。
· 分钟窗内 MA 长窗可能不够（15m 全日仅 16 根 ⇒ 无 MA20）⇒ **窗口不足不给键**，
  方向只用可得信号，不回落 0。

## 易错处

· `direction` 是几何推导（价/短 MA/长 MA/动量投票），**不是**预测涨跌概率，
  也**不是**买卖建议；置信度语义交给上层（周期观），本层不编胜率。
· `is_forming_bar=True` 时分钟方向是动态值（今日未走完），日线定势仍以
  已收盘 K 线为准（末根可能是今日合成 bar，见 day_last_forming）。
· 三周期任一 `insufficient` 时 consensus 会降级 —— 不得把「缺周期」写成「中性」。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from ...log import logger

from .intraday_tools import (
    _is_forming,
    _normalize_codes,
    _to_float,
    fetch_day_1m,
)

# 分钟周期 → 聚合步长（A 股 1m 源本地聚合；15m/5m 查表恒空，勿直查）
_MF_STEPS = (("m15", 15), ("m5", 5))
# 日线定势默认回溯根数（够 MA60）
_DEFAULT_DAILY_BARS = 60
# 方向投票用的短/长窗（分钟帧长窗可因根数不足而缺）
_SHORT_WIN = 5
_LONG_WIN = 20


# ═══════════════════════════════════════════════════════════════
#  1m → 5m/15m 按交易分钟桶聚合（纯函数）
# ═══════════════════════════════════════════════════════════════

def _trading_minute_index(ts: int) -> int:
    """unix 时间戳 → 0..239（09:30 起，午休归 11:30 槽）。非法 -1。

    与 `market_cn.auto.core.data.hub._trading_minute_index` 同口径，此处不
    跨层 import（tools 不依赖 auto 策略层）；口径改动两处要对齐。
    """
    try:
        hm = datetime.fromtimestamp(int(ts)).strftime("%H:%M")
        h, m = int(hm[:2]), int(hm[3:5])
        minutes = h * 60 + m
        if minutes <= 570:          # ≤09:30
            return 0
        if 570 < minutes <= 690:    # 09:31-11:30
            return minutes - 571
        if 690 < minutes <= 780:    # 11:31-13:00 午休
            return 119
        if 780 < minutes <= 900:    # 13:01-15:00
            return 120 + (minutes - 781)
        return 239
    except Exception:
        return -1


def resample_trading(bars: List[Dict[str, Any]], step: int) -> List[Dict[str, Any]]:
    """1m → step 分钟 K 线（按交易分钟 // step 分桶，午休不裂开）。"""
    step = max(1, int(step))
    buckets: Dict[int, List[Dict[str, Any]]] = {}
    for b in bars or []:
        ts = b.get("time")
        try:
            ts = int(ts)
        except (TypeError, ValueError):
            continue
        idx = _trading_minute_index(ts)
        if idx < 0:
            continue
        buckets.setdefault(idx // step, []).append(b)
    out: List[Dict[str, Any]] = []
    for k in sorted(buckets):
        chunk = sorted(buckets[k], key=lambda x: int(x.get("time") or 0))
        out.append({
            "time": chunk[-1].get("time"),
            "open": _to_float(chunk[0].get("open")),
            "high": max(_to_float(x.get("high")) for x in chunk),
            "low": min(_to_float(x.get("low")) for x in chunk),
            "close": _to_float(chunk[-1].get("close")),
            "volume": sum(_to_float(x.get("volume")) for x in chunk),
        })
    return out


# ═══════════════════════════════════════════════════════════════
#  单帧方向（纯几何投票，无 0-100 分）
# ═══════════════════════════════════════════════════════════════

def _ma(closes: List[float], w: int) -> Optional[float]:
    if w <= 0 or len(closes) < w:
        return None
    return sum(closes[-w:]) / w


def frame_direction(bars: List[Dict[str, Any]],
                    short: int = _SHORT_WIN,
                    long: int = _LONG_WIN) -> Dict[str, Any]:
    """单周期几何方向：价 vs 短 MA、短 vs 长 MA、短窗动量 —— 全同意才定向。

    Returns:
        {direction: "up"|"down"|"range"|"insufficient",
         signals: {close, ma_short, ma_long, close_vs_ma_short, ma_align, mom_pct,
                   up_streak, down_streak}, bars}
    """
    n = len(bars or [])
    if n < short:
        return {"direction": "insufficient", "bars": n,
                "signals": {"reason": f"bars={n} < short={short}"}}
    closes = [_to_float(b.get("close")) for b in bars]
    last = closes[-1]
    ma_s = _ma(closes, short)
    ma_l = _ma(closes, long)

    close_vs = "at"
    if ma_s is not None and last > ma_s:
        close_vs = "above"
    elif ma_s is not None and last < ma_s:
        close_vs = "below"

    if ma_s is None or ma_l is None:
        align = "n/a"
    elif ma_s > ma_l:
        align = "bullish"
    elif ma_s < ma_l:
        align = "bearish"
    else:
        align = "flat"

    mom_pct = None
    if n > short and closes[-1 - short]:
        mom_pct = round((last - closes[-1 - short]) / closes[-1 - short] * 100, 3)

    up_streak = 0
    for i in range(n - 1, 0, -1):
        if closes[i] > closes[i - 1]:
            up_streak += 1
        else:
            break
    down_streak = 0
    for i in range(n - 1, 0, -1):
        if closes[i] < closes[i - 1]:
            down_streak += 1
        else:
            break

    # ── 投票：三个可得信号，全同号才定向（部分同意 → range，诚实降级）──
    votes: List[int] = []
    if close_vs == "above":
        votes.append(1)
    elif close_vs == "below":
        votes.append(-1)
    if align == "bullish":
        votes.append(1)
    elif align == "bearish":
        votes.append(-1)
    if mom_pct is not None and abs(mom_pct) >= 0.05:
        votes.append(1 if mom_pct > 0 else -1)

    if votes and all(v > 0 for v in votes):
        direction = "up"
    elif votes and all(v < 0 for v in votes):
        direction = "down"
    else:
        direction = "range"

    return {
        "direction": direction,
        "bars": n,
        "signals": {
            "close": round(last, 4),
            "ma_short": round(ma_s, 4) if ma_s is not None else None,
            "ma_long": round(ma_l, 4) if ma_l is not None else None,
            "close_vs_ma_short": close_vs,
            "ma_align": align,
            "mom_pct": mom_pct,
            "up_streak": up_streak,
            "down_streak": down_streak,
        },
    }


# ═══════════════════════════════════════════════════════════════
#  周期共识（日线优先 / 分时轻权，非评分）
# ═══════════════════════════════════════════════════════════════
#
# 【为什么日线权重 > 分时】2026-10-23 用户裁定：预测硬约束是 **T+1**（次日），
# 不是今日盘中。日线定势决定 T+1 方向；15m/5m 只是**入场节奏**（何时接、
# 是否等回踩），不得单独把 T+1 翻多空。旧版三帧等权投票会把
# 「日线 up + m15 down」抬成 conflict，等于让分时否掉 T+1 主方向 —— 过度恐吓。

def frame_consensus(frames: Dict[str, Optional[str]]) -> Dict[str, Any]:
    """多帧 direction → **日线优先**共识（T+1 语义）。

    frames: {"daily": "up", "m15": "up", "m5": "down"}；缺失/insufficient 可为 None。

    Returns:
        {state, t1_bias, timing, directions, missing, agree, count}
        · t1_bias — T+1 方向，**优先 daily**，缺日线才降级 m15→m5
        · timing  — 分时节奏（m15/m5）：strong=同向 / weak=反向 / mixed=半同 / n/a
        · state   — 层级标签，见下；分时反向 **不**再叫 conflict
    """
    order = ("daily", "m15", "m5")
    known = {k: frames.get(k) for k in order
             if frames.get(k) in ("up", "down", "range")}
    missing = [k for k in order if k not in known]
    if not known:
        return {"state": "insufficient", "t1_bias": "insufficient", "timing": "n/a",
                "directions": dict(frames), "missing": missing,
                "agree": 0, "count": 0}

    # ── T+1 主方向：日线优先 ──
    t1_bias = known.get("daily") or known.get("m15") or known.get("m5") or "insufficient"

    # ── 分时节奏：m15/m5 相对 t1_bias ──
    minute_dirs = [known[k] for k in ("m15", "m5") if k in known]
    if not minute_dirs or t1_bias == "range":
        timing = "n/a"
    else:
        agree_n = sum(1 for d in minute_dirs if d == t1_bias)
        if agree_n == len(minute_dirs):
            timing = "strong"
        elif agree_n == 0:
            timing = "weak"
        else:
            timing = "mixed"

    # ── state：日线定调，分时只注节奏（不再用等权 conflict）──
    if t1_bias == "range":
        if timing == "strong":
            state = "range"
        elif timing in ("weak", "mixed"):
            state = "range_timing_split"
        else:
            state = "range"
    elif timing == "strong":
        state = "aligned_up" if t1_bias == "up" else "aligned_down"
    elif timing == "weak":
        state = "t1_up_timing_weak" if t1_bias == "up" else "t1_down_timing_weak"
    elif timing == "mixed":
        state = "t1_up_timing_mixed" if t1_bias == "up" else "t1_down_timing_mixed"
    else:
        # 无分钟帧（或分钟 insufficient）：只有日线
        state = "t1_up_solo" if t1_bias == "up" else (
            "t1_down_solo" if t1_bias == "down" else "range")

    # agree：与 t1_bias 同向的帧数（含日线自身）
    agree = sum(1 for v in known.values() if v == t1_bias)

    return {
        "state": state,
        "t1_bias": t1_bias,
        "timing": timing,
        "directions": dict(frames),
        "missing": missing,
        "agree": agree,
        "count": len(known),
    }


# ═══════════════════════════════════════════════════════════════
#  大盘相对强弱（**修正注记**，非主论据；2026-10-23 用户裁定）
# ═══════════════════════════════════════════════════════════════
#
# 「大盘是晴雨表」：大盘弱个股强 = **真强**；大盘强个股弱 = **真弱**。
# ★ 定位（2026-10-23）：**修正性分析，不是决定性的**。
#   · **不得**当强弱/方向的主论据 —— 主论据永远是个股自身（日线 t1_bias + 支撑/量价）；
#   · **分歧时更不得**拿大盘盖过个股（否则预判被大盘拖后腿）——个股信号可**提前**下；
#   · 只对「弱转强 / 强转分歧」做轻量修正注记（boost/discount/heavier/lighter）。

def market_relative_view(stock: Dict[str, Any],
                         market: Dict[str, Any]) -> Dict[str, Any]:
    """个股 vs 大盘相对强弱 → 真强/真弱/…（**修正注记**，不参与主判定）。

    Args:
        stock / market: {"direction": up|down|range|None, "change_pct": float|None}
    Returns:
        {label, excess_pct, weak_to_strong_adj, divergence_adj,
         stock_dir, market_dir, role}
        · label: true_strong | true_weak | market_carried | market_dragged | sync | insufficient
        · role: 恒为 "correction" —— 提醒消费方：这是注记层，不是主分析点
        · weak_to_strong_adj / divergence_adj: boost|discount|heavier|lighter|neutral
          （只许轻微修正措辞与置信，**不得**单独翻多空或压过个股证据）
    """
    sd = (stock or {}).get("direction") or None
    md = (market or {}).get("direction") or None
    sc = (stock or {}).get("change_pct")
    mc = (market or {}).get("change_pct")
    try:
        sc = float(sc) if sc is not None else None
    except (TypeError, ValueError):
        sc = None
    try:
        mc = float(mc) if mc is not None else None
    except (TypeError, ValueError):
        mc = None

    if sd not in ("up", "down", "range") and sc is None:
        return {"label": "insufficient", "excess_pct": None,
                "weak_to_strong_adj": "neutral", "divergence_adj": "neutral",
                "stock_dir": sd, "market_dir": md, "role": "correction"}

    # 相对强弱优先看「个股相对大盘超额」；缺 change 才退 direction
    excess = round(sc - mc, 3) if (sc is not None and mc is not None) else None

    def _strong(d, chg, ref=0.05):
        if chg is not None:
            return chg > ref
        return d == "up"

    def _weak(d, chg, ref=0.05):
        if chg is not None:
            return chg < -ref
        return d == "down"

    st_strong = _strong(sd, sc)
    st_weak = _weak(sd, sc)
    mk_strong = _strong(md, mc, ref=0.0)   # 大盘以 0 为界更贴「晴雨表」
    mk_weak = _weak(md, mc, ref=0.0)

    if st_strong and mk_weak:
        label = "true_strong"        # 大盘弱个股强 = 真强
        w2s, div = "boost", "neutral"
    elif st_weak and mk_strong:
        label = "true_weak"          # 大盘强个股弱 = 真弱
        w2s, div = "neutral", "heavier"
    elif st_strong and mk_strong:
        label = "market_carried"     # 水涨船高，强要打折
        w2s, div = "discount", "neutral"
    elif st_weak and mk_weak:
        label = "market_dragged"     # 随大流，弱可减轻
        w2s, div = "neutral", "lighter"
    else:
        label = "sync"
        w2s, div = "neutral", "neutral"
        # 有超额且明显偏离时仍可加注（方向模糊但幅度说话）
        if excess is not None and abs(excess) >= 1.0:
            if excess > 0 and mk_weak:
                label = "true_strong"
                w2s = "boost"
            elif excess < 0 and mk_strong:
                label = "true_weak"
                div = "heavier"

    return {
        "label": label,
        "excess_pct": excess,
        "weak_to_strong_adj": w2s,
        "divergence_adj": div,
        "stock_dir": sd,
        "market_dir": md,
        "role": "correction",
    }


# ═══════════════════════════════════════════════════════════════
#  取数：日线
# ═══════════════════════════════════════════════════════════════

def _fetch_daily(code: str, days: int = _DEFAULT_DAILY_BARS) -> List[Dict[str, Any]]:
    """日线 OHLCV（升序）。失败返回 []。"""
    try:
        from ._analysis_utils import _fetch_klines
        return _fetch_klines(code, days) or []
    except Exception as e:
        logger.debug("[multiframe] %s 日线取数失败: %s", code, e)
        return []


def _fetch_market_daily(days: int = 60) -> List[Dict[str, Any]]:
    """上证指数日线（晴雨表默认 000001）。失败返回 []。"""
    try:
        from app.market_cn.index import get_index_daily_kline
        raw = get_index_daily_kline("000001", int(days) or 60) or []
        out = []
        for k in raw:
            if not isinstance(k, dict):
                continue
            out.append({
                "time": k.get("time") or k.get("date"),
                "open": _to_float(k.get("open")),
                "high": _to_float(k.get("high")),
                "low": _to_float(k.get("low")),
                "close": _to_float(k.get("close")),
                "volume": _to_float(k.get("volume")),
            })
        return out
    except Exception as e:
        logger.debug("[multiframe] 大盘日线取数失败: %s", e)
        return []


def _day_change_pct(bars: List[Dict[str, Any]]) -> Optional[float]:
    """末根日涨跌幅 %（相对前收）；数据不足 None。"""
    if not bars or len(bars) < 2:
        return None
    prev = _to_float(bars[-2].get("close"))
    last = _to_float(bars[-1].get("close"))
    if prev <= 0:
        return None
    return round((last - prev) / prev * 100, 3)


# ═══════════════════════════════════════════════════════════════
#  工具入口
# ═══════════════════════════════════════════════════════════════

def get_multiframe_direction(codes: str, date: str = "",
                             daily_bars: int = 60) -> Dict[str, Any]:
    """多周期方向：日线定势 + 15m/5m 定节奏；**日线权重 > 分时**（T+1 语义）。

    不是评分、不是涨跌概率、不是买卖建议。direction 只有 up/down/range。
    consensus：`t1_bias` = T+1 主方向（**优先 daily**）；`timing` = 分时节奏
    （strong/weak/mixed）；`state` 为层级标签——分时反向是 t1_*_timing_weak，
    **不是** conflict（分时不得单独否掉 T+1 日线方向）。

    `rel_strength`：大盘晴雨表 **修正注记**（role=correction，**非主论据**）。
    大盘弱个股强=true_strong（弱转强可 boost）；大盘强个股弱=true_weak（强转分歧
    可 heavier）；同涨 discount / 同跌 lighter。★ 强弱与方向的主论据永远是
    个股自身（t1_bias + 支撑/量价）；**分歧时不得**拿大盘盖过个股，预判可提前。

    分钟帧只分析**一个交易日**的 1m（盘后 kline_1m_YYYY，盘中 snapshot）。
    日线用 daily_bars 根定势。大盘取上证 000001 日线。

    Args:
        codes: 股票代码，多股逗号分隔
        date: 分钟帧交易日 YYYY-MM-DD，默认今天
        daily_bars: 日线回溯根数，默认 60（范围 30~120）

    Returns:
        {"count": N, "data": {代码: {
            "date", "is_forming_bar", "source",
            "daily": {direction, signals, bars},
            "m15": {direction, signals, bars},
            "m5": {direction, signals, bars},
            "consensus": {state, t1_bias, timing, directions, missing, agree, count},
            "rel_strength": {label, excess_pct, weak_to_strong_adj, divergence_adj}
        }}, "error": None}
        单码/多码同构；失败码在 data[码] 内 {"error": ...}。
        ⚠ 分钟帧 is_forming_bar=true 时方向为动态值；缺周期写在 consensus.missing，
        **不是**「中性」。state 词表：aligned_up/down、t1_up/down_timing_weak|mixed、
        t1_up/down_solo、range、range_timing_split、insufficient。
        rel_strength.label：true_strong/true_weak/market_carried/market_dragged/sync/insufficient。
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
        daily_bars = int(daily_bars)
    except (TypeError, ValueError):
        daily_bars = _DEFAULT_DAILY_BARS
    daily_bars = min(max(daily_bars, 30), 120)

    # ── 大盘晴雨表（一次取，全批次共用）──
    mkt_bars = _fetch_market_daily(daily_bars)
    mkt_frame = frame_direction(mkt_bars, _SHORT_WIN, _LONG_WIN) if mkt_bars else {}
    mkt_view = {
        "direction": mkt_frame.get("direction"),
        "change_pct": _day_change_pct(mkt_bars) if mkt_bars else None,
    }

    results: Dict[str, Any] = {}
    for raw, code in zip(raw_codes, bare_codes):
        # ── 日线定势 ──
        d_bars = _fetch_daily(code, daily_bars)
        if not d_bars:
            results[raw] = {"error": "日线数据不足", "date": date_str}
            continue
        daily = frame_direction(d_bars, _SHORT_WIN, _LONG_WIN)

        # ── 分钟节奏（单日 1m → 15m/5m）──
        fetched = fetch_day_1m(code, date_str)
        m_bars = fetched.get("bars") or []
        source = fetched.get("source") or ""
        forming = _is_forming(date_str, source)

        m15 = frame_direction(resample_trading(m_bars, 15), _SHORT_WIN, _LONG_WIN)
        m5 = frame_direction(resample_trading(m_bars, 5), _SHORT_WIN, _LONG_WIN)

        consensus = frame_consensus({
            "daily": daily.get("direction"),
            "m15": m15.get("direction"),
            "m5": m5.get("direction"),
        })

        # ── 大盘相对强弱（真强/真弱，修正弱转强·强转分歧）──
        rel = market_relative_view(
            {"direction": daily.get("direction"),
             "change_pct": _day_change_pct(d_bars)},
            mkt_view,
        )

        results[raw] = {
            "date": date_str,
            "source": source or None,
            "is_forming_bar": forming,
            "daily": daily,
            "m15": m15,
            "m5": m5,
            "consensus": consensus,
            "rel_strength": rel,
        }

    return {"count": len(results), "data": results, "error": None}

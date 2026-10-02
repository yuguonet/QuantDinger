# -*- coding: utf-8 -*-
"""
market_screener/_facts.py — 纯客观事实层（无评分、无取舍、无阈值）

2026-09-29 v7：原 intraday.py / eod.py / post_market.py 三个「策略模块」把四样东西
  混在一个文件里 —— ① 真正的客观计算 ② 选股规则 ③ 评分梯形 ④ 报告口径。
  其中 ②③④ 让 LLM 只能消费「别人替它做好的结论」，自由度≈0。本轮拆分：

    客观计算 → 本文件（LLM 难以自行稳定复算，且算错了没人知道）
    规则     → references/strategy-rules.md（由 LLM 读取、采纳或推翻）

本文件**禁止出现**：入选阈值、评分权重、截断条数、买卖建议、方向判断。
形态识别里的参数是该形态的**客观定义**（例如「平台」= 振幅 <8%），不是推荐标准。

已知数据源事实（2026-09-29 实测，勿凭记忆改）：
  - 分钟源只有 `1m` 有数据；`15m` / `5m` / `30m` / `60m` 查询均返回 0 行
    （历史裁定：kline_15m 已回填至 kline_1m 并标 void）
  - 因此多周期分析一律取 `1m` 后**本地聚合**成 15m / 5m，不得直接查其它周期

注意：本目录 .py 行尾为 CRLF（common.py 例外为 LF），改前用 read_bytes() 判定。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from log import logger

from .common import (
    call_tool, fetch_kline, _today_str,
    fetch_zt_pool, fetch_dt_pool, fetch_broken_board,
    fetch_hot_stocks_with_reason, fetch_hot_sectors,
    scan_dragon_pullback, _get_writer,
    compute_ma, compute_macd, compute_rsi, compute_volume_ratio,
)


# ═══════════════════════════════════════════════════════════════
#  分钟线：唯一可用源是 1m，其余周期本地聚合
# ═══════════════════════════════════════════════════════════════

# 实测结论：只有 1m 有数据。写死在这里防止后人再踩 15m 空表的坑。
_MINUTE_SOURCE = "1m"


def intraday_bars(code: str, days: int = 5) -> List[Dict]:
    """取该股最近若干个自然日的 1 分钟 K 线（升序）。

    Args:
        code: 6 位股票代码
        days: 回溯自然日，默认 5

    Returns:
        list: [{time, open, high, low, close, volume}]，取不到返回 []
    """
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=int(days))).strftime("%Y-%m-%d")
    try:
        writer = _get_writer()
        data = writer.query("CNStock", code, _MINUTE_SOURCE, start_time=start, end_time=end, limit=0)
        if not data:
            return []
        return [{
            "time": str(r["time"]), "open": float(r["open"]),
            "high": float(r["high"]), "low": float(r["low"]),
            "close": float(r["close"]), "volume": float(r["volume"]),
        } for r in data]
    except Exception as e:
        logger.debug("[MktScreen] %s 分钟线获取失败: %s", code, e)
        return []


def resample(bars: List[Dict], minutes: int) -> List[Dict]:
    """把 1 分钟 K 线按固定根数聚合为 N 分钟 K 线（客观换算，无加工）。

    Args:
        bars: 1m K 线（升序）
        minutes: 聚合根数，例如 15 / 5

    Returns:
        list: 聚合后的 K 线（升序），不足一根的尾部丢弃
    """
    minutes = max(1, int(minutes))
    out: List[Dict] = []
    chunk: List[Dict] = []
    for b in bars or []:
        chunk.append(b)
        if len(chunk) == minutes:
            out.append({
                "time": chunk[0]["time"],
                "open": chunk[0]["open"],
                "high": max(x["high"] for x in chunk),
                "low": min(x["low"] for x in chunk),
                "close": chunk[-1]["close"],
                "volume": sum(x["volume"] for x in chunk),
            })
            chunk = []
    return out


# ═══════════════════════════════════════════════════════════════
#  市场事实快照
# ═══════════════════════════════════════════════════════════════

def market_state() -> Dict[str, Any]:
    """市场层面客观事实：资金流向、涨跌停家数、炸板率、板块强弱、情绪描述标签。

    注意：mood / mood_score 是**描述性标签**（便于人和 LLM 快速理解当前环境），
    不是入场规则 —— 怎么用由调用方决定。

    Returns:
        dict: fund_flow / zt_count / dt_count / broken_count / broken_rate /
            strong_sectors / weak_sectors / mood / mood_score
    """
    today = _today_str()

    # 三对账修正（2026-09-30）：旧实现拿平安银行（000001）个券资金流冒充“上证净流入”，
    # 且读 net_inflow（真实键 total_main_net）恒 0 → 情绪分恒基线。改走大盘口径
    # （get_fund_flow scope=market，键 main_net，单位元），失败如实记 0。
    net_inflow = 0
    try:
        from tools.finance.fund_flow_tools import get_fund_flow
        ff = get_fund_flow(scope="market")
        if isinstance(ff, dict) and not ff.get("error"):
            net_inflow = float(ff.get("main_net") or ff.get("total_main_net") or 0)
    except Exception as e:
        try:
            from log import logger
            logger.warning("[MktScreen] 大盘资金流获取失败: %s", e)
        except Exception:
            pass

    zt_pool = fetch_zt_pool(today)
    dt_pool = fetch_dt_pool(today)
    broken = fetch_broken_board(today)
    zt_count = len(zt_pool)
    dt_count = len(dt_pool)
    broken_count = len(broken)
    broken_rate = round(broken_count / max(1, zt_count + broken_count) * 100, 1)

    strong_sectors: List[Dict] = []
    weak_sectors: List[Dict] = []
    sectors = fetch_hot_sectors()
    if isinstance(sectors, dict) and not sectors.get("error"):
        # 三对账修正（2026-09-30）：get_hot_sectors 返回 {code,msg,data}，板块行在
        # data.industry 二级键——旧读顶层 industry 恒空，强弱板块永远为空。
        _sd = sectors.get("data") if isinstance(sectors.get("data"), dict) else sectors
        for s in (_sd.get("industry") or [])[:10]:
            bucket = strong_sectors if (s.get("change_pct") or 0) > 0 else weak_sectors
            bucket.append({"name": s.get("name"), "change_pct": s.get("change_pct")})

    # 描述性情绪标签（历史口径，保持与 _helpers._mood_regime 兼容）
    mood_score = 50
    if net_inflow > 0:
        mood_score += min(20, net_inflow // 10000000)
    else:
        mood_score += max(-20, net_inflow // 10000000)
    if zt_count > 30:
        mood_score += 10
    elif zt_count < 15:
        mood_score -= 10
    if dt_count > 20:
        mood_score -= 15
    if broken_rate > 40:
        mood_score -= 10
    elif broken_rate < 20:
        mood_score += 5
    mood_score = max(0, min(100, mood_score))
    mood = "偏强" if mood_score >= 70 else ("中性" if mood_score >= 50 else ("偏弱" if mood_score >= 30 else "弱势"))

    return {
        "fund_flow": net_inflow,
        "mood": mood,
        "mood_score": mood_score,
        "strong_sectors": strong_sectors,
        "weak_sectors": weak_sectors,
        "zt_count": zt_count,
        "dt_count": dt_count,
        "broken_count": broken_count,
        "broken_rate": broken_rate,
    }


# ═══════════════════════════════════════════════════════════════
#  多周期几何量（客观描述，不含买入含义）
# ═══════════════════════════════════════════════════════════════

def multitimeframe(code: str, daily_days: int = 60, minute_days: int = 5) -> Dict[str, Any]:
    """日线 + 15m + 5m 的结构量。分钟线统一由 1m 聚合而来（其它周期查表恒空）。

    weak_to_strong / strong_to_weak / volume_pattern / macd_strength 是对几何关系的
    **速记标签**（例如「最近5根多数站上MA5 且之前5根多数未站上」），
    便于快速扫读；如何解读写在 references/strategy-rules.md，不构成推荐。

    Args:
        code: 6 位股票代码
        daily_days: 日线回溯根数
        minute_days: 分钟线回溯自然日

    Returns:
        dict: ok / daily / m15 / m5 / volume_pattern / weak_to_strong / strong_to_weak / missing
    """
    out: Dict[str, Any] = {
        "ok": False, "code": code, "daily": {}, "m15": {}, "m5": {},
        "weak_to_strong": False, "strong_to_weak": False,
        "volume_pattern": "unknown", "missing": [],
    }

    bars_1d = fetch_kline(code, days=daily_days)
    if len(bars_1d) < 20:
        out["missing"].append(f"日线不足20根（实得{len(bars_1d)}）")
        return out
    out["ok"] = True

    closes = [b["close"] for b in bars_1d]
    volumes = [b["volume"] for b in bars_1d]
    n = len(bars_1d)
    i = n - 1

    ma5 = compute_ma(closes, 5)
    ma10 = compute_ma(closes, 10)
    ma20 = compute_ma(closes, 20)
    ma60 = compute_ma(closes, 60) if n >= 60 else [None] * n
    macd = compute_macd(closes)
    rsi = compute_rsi(closes)
    vr = compute_volume_ratio(volumes, 5)

    def _slope(seq, k: int) -> float:
        a, b = seq[k], seq[k - 1]
        if not a or not b:
            return 0.0
        return (a - b) / b * 100

    out["daily"] = {
        "close": closes[i],
        "ma5": ma5[i], "ma10": ma10[i], "ma20": ma20[i], "ma60": ma60[i],
        "macd_bar": macd["macd"][i], "rsi": rsi[i], "vol_ratio": vr[i],
        "above_ma5": bool(closes[i] > (ma5[i] or 0)),
        "above_ma20": bool(closes[i] > (ma20[i] or 0)),
        "ma5_slope_pct": round(_slope(ma5, i), 3),
        "ma60_slope_pct": round(_slope(ma60, i), 3) if ma60[i] else None,
    }

    # 均线穿越结构（纯计数，无方向评价）
    if i >= 9:
        recent_above = sum(1 for j in range(i - 4, i + 1) if closes[j] > (ma5[j] or 0))
        recent_below = sum(1 for j in range(i - 4, i + 1) if closes[j] < (ma5[j] or 0))
        prev_near = sum(1 for j in range(i - 9, i - 4) if closes[j] <= (ma5[j] or 0) * 1.01)
        prev_above = sum(1 for j in range(i - 9, i - 4) if closes[j] > (ma5[j] or 0))
        out["weak_to_strong"] = bool(recent_above >= 4 and prev_near >= 3)
        out["strong_to_weak"] = bool(recent_below >= 4 and prev_above >= 3)
        if macd["macd"][i] < 0 and recent_above >= 3 and closes[i] > (ma5[i] or 0):
            out["weak_to_strong"] = True

    if vr[i] > 1.5 and closes[i] > closes[i - 1]:
        out["volume_pattern"] = "放量上涨"
    elif vr[i] < 0.7 and closes[i] > closes[i - 1]:
        out["volume_pattern"] = "缩量上涨"
    elif vr[i] > 1.5 and closes[i] < closes[i - 1]:
        out["volume_pattern"] = "放量下跌"
    elif vr[i] < 0.7 and closes[i] < closes[i - 1]:
        out["volume_pattern"] = "缩量下跌"
    else:
        out["volume_pattern"] = "平量整理"

    # ── 分钟线：1m 源 → 聚合 15m / 5m ──
    bars_1m = intraday_bars(code, days=minute_days)
    if not bars_1m:
        out["missing"].append("分钟线为空（1m 源无数据）")
        return out

    for label, step in (("m15", 15), ("m5", 5)):
        agg = resample(bars_1m, step)
        if len(agg) < 6:
            out["missing"].append(f"{label} 不足6根（实得{len(agg)}）")
            continue
        c = [x["close"] for x in agg]
        v = [x["volume"] for x in agg]
        h = [x["high"] for x in agg]
        lo = [x["low"] for x in agg]
        k = len(agg) - 1
        ma5_a = compute_ma(c, 5)
        prev_avg = sum(v[max(0, k - 5):k]) / min(5, k) if k > 0 else 0
        out[label] = {
            "bars": len(agg),
            "close": c[k],
            "above_ma5": bool(c[k] > (ma5_a[k] or 0)),
            "rsi": round(compute_rsi(c)[k], 1),
            "vol_ratio": round(v[k] / prev_avg, 2) if prev_avg else None,
            "trend": "up" if c[k] > c[max(0, k - 5)] else "down",
        }
        if label == "m5":
            macd_a = compute_macd(c)
            green_r, red_r = [], []
            for j in range(max(0, k - 19), k + 1):
                rng = h[j] - lo[j]
                if macd_a["macd"][j] < 0:
                    green_r.append(rng)
                elif macd_a["macd"][j] > 0:
                    red_r.append(rng)
            strength = "unknown"
            if green_r and red_r:
                gr, rr = sum(green_r) / len(green_r), sum(red_r) / len(red_r)
                strength = "even"
                if gr < rr * 0.8:
                    strength = "sell_pressure_low"
                elif gr > rr * 1.2:
                    strength = "sell_pressure_high"
            out["m5"]["macd_strength"] = strength
            out["m5"]["green_avg_range"] = round(sum(green_r) / len(green_r), 4) if green_r else None
            out["m5"]["red_avg_range"] = round(sum(red_r) / len(red_r), 4) if red_r else None

    return out


# ═══════════════════════════════════════════════════════════════
#  形态识别（几何定义，不含任何 score / 建议）
# ═══════════════════════════════════════════════════════════════

def _pattern_platform_breakout(bars: List[Dict], p: Dict) -> Optional[Dict]:
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
    vol_ratio = today["volume"] / avg_vol if avg_vol > 0 else 0
    if vol_ratio <= p["breakout_vol_ratio"]:
        return None
    return {"pattern": "平台突破", "platform_high": round(p_high, 2),
            "range_pct": round(range_pct, 1), "vol_ratio": round(vol_ratio, 2)}


def _pattern_volume_reversal(bars: List[Dict], p: Dict) -> Optional[Dict]:
    if len(bars) < 10:
        return None
    recent, today = bars[-6:-1], bars[-1]
    vols = [b["volume"] for b in recent]
    ct = [b["close"] for b in recent]
    avg_vol = sum(vols) / len(vols) if vols else 0
    if not ct or ct[0] <= 0 or avg_vol <= 0:
        return None
    period_change = (ct[-1] - ct[0]) / ct[0] * 100
    is_shrinking = all(v <= avg_vol * 1.1 for v in vols[-3:])
    is_surge = today["volume"] > avg_vol * p["surge_vol_ratio"] and today["close"] > today["open"]
    if is_shrinking and is_surge and abs(period_change) < p["flat_change_pct"]:
        return {"pattern": "底部放量启动", "vol_ratio": round(today["volume"] / avg_vol, 2),
                "period_change_pct": round(period_change, 1)}
    return None


def _pattern_ma_support_pullback(bars: List[Dict], p: Dict) -> Optional[Dict]:
    if len(bars) < 20:
        return None
    closes = [b["close"] for b in bars]
    ma5, ma10, ma20 = compute_ma(closes, 5), compute_ma(closes, 10), compute_ma(closes, 20)
    if not (ma5[-1] and ma10[-1] and ma20[-1]):
        return None
    if not (ma5[-1] > ma10[-1] > ma20[-1]):
        return None
    today = bars[-1]
    dist = abs(today["low"] - ma10[-1]) / ma10[-1] * 100
    if dist < p["ma_touch_pct"] and today["close"] > ma10[-1]:
        return {"pattern": "均线支撑回踩", "ma10": round(ma10[-1], 2),
                "low": round(today["low"], 2), "distance_pct": round(dist, 2),
                "close_above_open": today["close"] > today["open"]}
    return None


def _pattern_macd_golden_cross(bars: List[Dict], p: Dict) -> Optional[Dict]:
    if len(bars) < 30:
        return None
    macd = compute_macd([b["close"] for b in bars])
    dif, dea = macd["dif"], macd["dea"]
    if dif[-1] >= dea[-1] and dif[-2] < dea[-2]:
        return {"pattern": "MACD金叉", "dif": round(dif[-1], 4),
                "dea": round(dea[-1], 4), "underwater": bool(dif[-1] < 0)}
    return None


def _pattern_shrink_pullback_breakout(bars: List[Dict], p: Dict) -> Optional[Dict]:
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


def _pattern_prev_high_breakout(bars: List[Dict], p: Dict) -> Optional[Dict]:
    if len(bars) < 21:
        return None
    today = bars[-1]
    prev_high = max(b["high"] for b in bars[-21:-1])
    if today["close"] <= prev_high or today["close"] <= today["open"]:
        return None
    vol_5 = sum(b["volume"] for b in bars[-6:-1]) / 5
    vol_ratio = today["volume"] / vol_5 if vol_5 > 0 else 0
    if vol_ratio <= p["breakout_vol_ratio"]:
        return None
    return {"pattern": "突破前高", "prev_high": round(prev_high, 2),
            "close": round(today["close"], 2), "vol_ratio": round(vol_ratio, 2)}


_PATTERN_DETECTORS = [
    _pattern_platform_breakout, _pattern_volume_reversal,
    _pattern_ma_support_pullback, _pattern_macd_golden_cross,
    _pattern_shrink_pullback_breakout, _pattern_prev_high_breakout,
]

# 形态的**几何定义参数** —— 不是推荐门槛，改了就等于换了一种形态。
# 默认值与 md 里记录的一致，LLM 可以通过 params 覆盖做敏感性检查。
_PATTERN_PARAMS_DEFAULT: Dict[str, float] = {
    "platform_range_pct": 8.0,     # 平台：区间振幅上限
    "breakout_vol_ratio": 1.3,     # 突破：量能倍数下限
    "surge_vol_ratio": 1.8,        # 底部启动：当日倍量下限
    "flat_change_pct": 5.0,        # 底部启动：区间涨跌幅绝对值上限
    "ma_touch_pct": 1.5,           # 回踩：最低价距 MA10 的距离上限
    "min_pullback_pct": 3.0,       # 缩量回调：回调幅度下限
    "max_pullback_pct": 12.0,      # 缩量回调：回调幅度上限
    "shrink_ratio": 0.7,           # 缩量回调：回调段量能 / 前段量能上限
    "rebreak_vol_ratio": 1.5,      # 缩量回调：再突破当日倍量下限
}


def detect_patterns(bars: List[Dict], params: Dict[str, float] = None) -> List[Dict]:
    """识别常见 K 线形态。只回答「是不是」，不回答「好不好」。

    Args:
        bars: 日线升序列表
        params: 覆盖形态的几何定义参数（键见 _PATTERN_PARAMS_DEFAULT）

    Returns:
        list: [{pattern, ...几何量}]，无形态时返回 []
    """
    p = dict(_PATTERN_PARAMS_DEFAULT)
    p.update({k: float(v) for k, v in (params or {}).items() if k in p})
    found = []
    for detector in _PATTERN_DETECTORS:
        try:
            hit = detector(bars or [], p)
        except Exception as e:
            logger.debug("[MktScreen] 形态检测 %s 异常: %s", detector.__name__, e)
            continue
        if hit:
            found.append(hit)
    return found


# ═══════════════════════════════════════════════════════════════
#  候选池编排（只负责「从哪取」，不负责「留哪只」）
# ═══════════════════════════════════════════════════════════════

# 候选来源清单 —— 全部给，由调用方通过 sources= 自选，不再按策略写死。
POOL_SOURCES = ("zt", "eod_zt", "dragon", "hot", "search", "recent_zt")

_SOURCE_DEFAULT = {
    "intraday": ("zt", "dragon", "search"),
    "eod": ("eod_zt", "search"),
    "post_market": ("hot", "search", "recent_zt", "dragon"),
}

# search_stocks 的查询串只是**默认建议**，调用方可用 queries= 完全替换。
_QUERY_DEFAULT = {
    "intraday": ("放量突破 站上20日均线", "缩量企稳 底部放量"),
    "eod": ("涨幅3%到8% 换手率大于3% 非ST",),
    "post_market": ("涨幅1%到8% 换手率大于2% 非ST", "近10日涨停 换手率大于2% 非ST"),
}


def candidate_pool(
    strategy: str = "post_market",
    date: str = None,
    sources: Any = None,
    queries: Any = None,
        search_top_n: int = 50,
    min_turnover: float = None,
    limit: int = None,
    fill_quote: bool = True,
) -> Dict[str, Any]:
    """按来源取候选。**不做任何取舍**：不按涨跌幅/连板排序，不截断，不打分。

    Args:
        strategy: 时段（intraday / eod / post_market），仅用于挑 sources / queries 默认值
        date: 交易日，默认今天
        sources: 逗号串或列表，取值见 POOL_SOURCES；None = 用该时段的默认组合
        queries: 逗号串或列表，传给 search_stocks；None = 用默认建议词
        search_top_n: 每条查询返回上限
        min_turnover: 换手率下限过滤；None = 不过滤（推荐由 profile 的 hard 标记去处理）
        limit: 返回条数上限；None = 不截断
        fill_quote: 是否用实时行情批量回填 price / change_pct / turnover_pct / price extremes

    Returns:
        dict: strategy / date / sources_used / pool_size / main_themes /
            candidates[{code,name,source,reason,continuous_days,zt_time,change_pct,turnover_pct,...}] /
            continuous_board / dragon_pullback / eod_zt / missing
    """
    date = date or _today_str()
    missing: List[str] = []

    srcs = [s.strip() for s in (sources.split(",") if isinstance(sources, str) else (sources or [])) if s.strip()]
    if not srcs:
        srcs = list(_SOURCE_DEFAULT.get(strategy, ("search",)))
    unknown = [s for s in srcs if s not in POOL_SOURCES]
    missing.extend(f"未知候选源: {s}" for s in unknown)
    srcs = [s for s in srcs if s in POOL_SOURCES]

    qs = [q.strip() for q in (queries.split(",") if isinstance(queries, str) else (queries or [])) if q.strip()]
    if "search" in srcs and not qs:
        qs = list(_QUERY_DEFAULT.get(strategy, ()))

    pool: Dict[str, Dict] = {}

    def _put(code: str, payload: Dict) -> None:
        if not code:
            return
        if code in pool:
            old = pool[code]["source"]
            if payload["source"] not in old.split("+"):
                pool[code]["source"] = f"{old}+{payload['source']}"
            for k, v in payload.items():
                if k != "source" and pool[code].get(k) in (None, "", 0):
                    pool[code][k] = v
            return
        pool[code] = payload

    continuous_board: List[Dict] = []
    eod_zt: List[Dict] = []
    dragon_pullback: List[Dict] = []

    # ── 涨停池（连板 / 尾盘封板）──
    if "zt" in srcs or "eod_zt" in srcs:
        try:
            zt_pool = fetch_zt_pool(date)
        except Exception as e:
            zt_pool = []
            missing.append(f"涨停池: {type(e).__name__}: {e}")
        for s in zt_pool or []:
            code = str(s.get("stock_code", "") or "")
            if not code:
                continue
            days = int(s.get("continuous_zt_days", 1) or 1)
            zt_time = s.get("zt_time", "") or ""
            row = {
                "code": code, "name": s.get("stock_name", ""),
                "reason": s.get("reason", ""), "continuous_days": days,
                "zt_time": zt_time, "seal_amount": s.get("seal_amount", 0),
                "change_pct": None, "turnover_pct": s.get("turnover_rate", 0),
            }
            if "zt" in srcs and days >= 2:
                row["source"] = "连板"
                continuous_board.append(row)
                _put(code, row)
            if "eod_zt" in srcs and ":" in zt_time:
                try:
                    hh, mm = zt_time.split(":")[:2]
                    if int(hh) == 14 and int(mm) >= 30:
                        row2 = dict(row, source="尾盘封板")
                        eod_zt.append(row2)
                        _put(code, row2)
                except (ValueError, IndexError):
                    pass

    # ── 龙回头 ──
    if "dragon" in srcs:
        try:
            dragon_pullback = scan_dragon_pullback(date) or []
        except Exception as e:
            dragon_pullback = []
            missing.append(f"龙回头: {type(e).__name__}: {e}")
        for s in dragon_pullback:
            _put(str(s.get("code", "")), {
                "code": str(s.get("code", "")), "name": s.get("name", ""),
                "source": "龙回头", "reason": s.get("reason", ""),
                "pullback_signals": s.get("signals", []),
                "pullback_pct": s.get("pullback_pct"),
                "strength_score": s.get("strength_score"),
                "change_pct": None, "turnover_pct": s.get("turnover_pct", 0),
            })

    # ── 当日热点题材股 ──
    main_themes: List = []
    if "hot" in srcs:
        try:
            hot = fetch_hot_stocks_with_reason(date) or {}
        except Exception as e:
            hot = {}
            missing.append(f"热点题材: {type(e).__name__}: {e}")
        main_themes = [(t, c) for t, c in (hot.get("hot_tags") or [])[:10]]
        for s in hot.get("stocks", []) or []:
            code = str(s.get("code", "") or "")
            if len(code) == 6:
                _put(code, {
                    "code": code, "name": s.get("name", ""), "source": "热点题材",
                    "reason": s.get("reason", ""), "change_pct": s.get("change_pct"),
                    "turnover_pct": s.get("turnover_pct") or s.get("turnover_rate"),
                })

    # ── 条件搜索（含 recent_zt）──
    if "search" in srcs and qs:
        for q in qs:
            try:
                res = call_tool("search_stocks", query=q, source="eastmoney", top_n=int(search_top_n))
            except Exception as e:
                missing.append(f"条件搜索[{q}]: {type(e).__name__}: {e}")
                continue
            stocks = (res or {}).get("stocks", []) if isinstance(res, dict) else []
            if isinstance(res, dict) and res.get("error") and not stocks:
                missing.append(f"条件搜索[{q}]: {res.get('error')}")
            for s in stocks:
                code = str(s.get("code", "") or s.get("symbol", "") or "")
                if len(code) != 6:
                    continue
                _put(code, {
                    "code": code, "name": s.get("name", ""), "source": "条件搜索",
                    "search_query": q, "reason": s.get("reason", ""),
                    "change_pct": s.get("change_pct") or s.get("pct_change"),
                    "turnover_pct": s.get("turnover_pct") or s.get("turnover_rate"),
                })
    elif "recent_zt" in srcs:
        missing.append("recent_zt 需要 queries（近N日涨停靠 search_stocks 表达）")

    # ── 批量回填实时行情（事实补全，不是筛选）──
    if fill_quote and pool:
        try:
            from tools.finance.data_tools import get_realtime_quote
            raw = get_realtime_quote(",".join(list(pool.keys())[:200])) or {}
            quotes = {}
            if isinstance(raw, dict):
                if "data" in raw:
                    quotes = raw["data"] or {}
                elif raw.get("stock_code"):
                    quotes = {raw["stock_code"]: raw}
            for code_str, q in (quotes or {}).items():
                if not isinstance(q, dict):
                    continue
                target = pool.get(str(q.get("stock_code") or code_str))
                if target is None:
                    continue
                for dst, src in (("price", "price"), ("change_pct", "change_pct"),
                                 ("turnover_pct", "turnover_pct"), ("turnover_pct", "turnover_rate"),
                                 ("vol_ratio", "vol_ratio"), ("high", "high"),
                                 ("low", "low"), ("amount", "amount")):
                    v = q.get(src)
                    if v not in (None, "", 0):
                        target[dst] = v
        except Exception as e:
            missing.append(f"行情回填: {type(e).__name__}: {e}")

    candidates = list(pool.values())
    if min_turnover is not None:
        lo = float(min_turnover)
        # turnover_pct 缺失（0/None）时不据此剔除 —— 避免把没有字段的票误杀
        candidates = [c for c in candidates
                      if not (c.get("turnover_pct") and float(c["turnover_pct"]) < lo)]

    return {
        "strategy": strategy,
        "date": date,
        "sources_used": srcs,
        "queries_used": qs,
        "pool_size": len(candidates),
        "main_themes": main_themes,
        "candidates": candidates[:limit] if limit else candidates,
        "continuous_board": continuous_board[:20],
        "dragon_pullback": dragon_pullback[:20],
        "eod_zt": eod_zt[:20],
        "missing": missing,
    }

# -*- coding: utf-8 -*-
"""
market-screener/_facts.py — 纯客观事实层（无评分、无取舍、无阈值）

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

from ...log import logger

from .common import (
    call_tool, fetch_kline, _today_str,
    fetch_zt_pool, fetch_dt_pool, fetch_broken_board,
    fetch_hot_stocks_with_reason, fetch_hot_sectors,
    _get_writer,
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
        from ...tools.finance.fund_flow_tools import get_fund_flow
        ff = get_fund_flow(scope="market")
        if isinstance(ff, dict) and not ff.get("error"):
            net_inflow = float(ff.get("main_net") or ff.get("total_main_net") or 0)
    except Exception as e:
        try:
            from ...log import logger
            logger.warning("[MktScreen] 大盘资金流获取失败: %s", e)
        except Exception:
            pass

    # 源级失败显性化（2026-10-21）：三池 fetcher 已 fail-fast，此处降级为 0 计数
    # 但必须把失败写进 source_errors，由 pre_screen 合并进 missing —— 静默归零会把
    # 「涨跌停池源故障」伪装成「情绪冰点」（zt=0/dt=0/炸板率 0 的假情绪分）。
    src_errors: List[str] = []
    try:
        zt_pool = fetch_zt_pool(today)
    except Exception as e:
        zt_pool = []
        src_errors.append(f"涨停池源失败: {type(e).__name__}: {e}")
    try:
        dt_pool = fetch_dt_pool(today)
    except Exception as e:
        dt_pool = []
        src_errors.append(f"跌停池源失败: {type(e).__name__}: {e}")
    try:
        broken = fetch_broken_board(today)
    except Exception as e:
        broken = []
        src_errors.append(f"炸板池源失败: {type(e).__name__}: {e}")
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
        "source_errors": src_errors,
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

# 候选来源清单 —— 多源并进（2026-10-28：禁止东财搜索单点）。
# 【用户裁定】选股目标 = 短线最小风险、最大涨幅。
#   连板/尾盘封板/龙回头/近期涨停 = 追高或接刀，**整源删除**。
#   涨停池数据仍供 market_state / zt_streak 客观事实使用，但不进候选池。
POOL_SOURCES = ("hot", "search", "flow", "sector", "scan")

_SOURCE_DEFAULT = {
    "intraday": ("hot", "search", "flow", "sector", "scan"),
    "eod": ("hot", "search", "flow", "sector", "scan"),
    "post_market": ("hot", "search", "flow", "sector", "scan"),
}

# search_stocks 的查询串只是**默认建议**，调用方可用 queries= 完全替换。
# 口径（2026-10-28）：围绕「有支撑 + 资金/趋势确认 + 未极端追高」扩词，
# 不再用连板/龙虎榜/近端涨停等追高词。
# 【池子偏小缺陷 2026-10-28】hot 在涨停潮里几乎全是封板，若 search 再空转，
# 池子只剩十几只且全被 objective 剔光。默认词必须以**东财智能选股真能命中**
# 的过滤语句为主（涨幅/换手/均线结构），纯意象词只作补充。
_QUERY_DEFAULT = {
    "intraday": (
        "涨幅0%到7% 站上5日均线 非ST",
        "涨幅3%到8% 换手率大于3% 非ST",
        "主力资金净流入 换手率大于2%",
        "放量突破 站上20日均线",
        "缩量回调 站上20日均线 非ST",
        "站上5日均线 多头排列 非ST",
        "底部放量 非ST",
        "回踩5日均线 收阳",
    ),
    "eod": (
        "涨幅0%到7% 站上5日均线 非ST",
        "涨幅3%到8% 换手率大于3% 非ST",
        "主力资金净流入 换手率大于2%",
        "放量突破 站上20日均线",
        "缩量回调 站上20日均线 非ST",
        "站上5日均线 多头排列 非ST",
        "底部放量 非ST",
    ),
    "post_market": (
        "涨幅0%到7% 站上5日均线 非ST",
        "涨幅1%到8% 换手率大于2% 非ST",
        "主力资金净流入 换手率大于2%",
        "放量突破 站上20日均线",
        "缩量回调 站上20日均线 非ST",
        "站上5日均线 多头排列 非ST",
        "底部放量 非ST",
        "回踩5日均线 收阳",
    ),
}

# 池底补采（默认词仍不够时）：更宽的活跃/结构/资金词，目标是**买到的票**，
# 不是封板名单。与 Round1 通用词分工：这里在取数阶段就补，避免 score 层才放空。
_QUERY_EXPAND = (
    "涨幅大于1% 非ST 换手率大于1%",
    "现价高于5日均线 非ST",
    "现价高于10日均线 非ST",
    "主力资金净流入",
    "放量上涨 非ST",
    "上涨趋势 非ST",
    "中小盘 换手率大于3%",
    "行业板块涨幅",
)


def _annotate_empty_pool_calendar(missing: List[str], date: str) -> None:
    """空池兜底：校验交易日历，把「非真实空候选」显式写进 missing。

    纯真空池（全部源成功返回空）在**交易日**上才是真实筛选结果；非交易日或
    日历不可确认时，空池不可作为「今日无行情机会」的证据 —— 否则主 agent 会
    把休市/日历故障当「今日无候选」交付（2026-10-21 缺陷闭环的最后一块）。
    日历复用 app/utils/trading_calendar.py（akshare 沪深交易日历 + pickle 缓存，
    项目唯一既有交易日历实现），此处不自造日历；日历拉取失败视为「不可确认」
    如实留痕，不做任何推测性兜底。
    """
    try:
        from app.utils.trading_calendar import is_trading_day
        trading_day = bool(is_trading_day(date))
    except Exception as e:
        missing.append(
            f"交易日历不可确认（空池无法区分节假日与数据故障，非真实空候选）: "
            f"{type(e).__name__}: {e}")
        return
    if not trading_day:
        missing.append(f"{date} 非交易日（日历核实）：空池为休市所致，非真实候选筛选结果")


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
            candidates[{code,name,source,reason,change_pct,turnover_pct,...}] /
            missing
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

    # ── 当日热点题材股（题材命中线索 + 候选，不作追高特权）──
    main_themes: List = []
    if "hot" in srcs:
        try:
            hot = fetch_hot_stocks_with_reason(date) or {}
        except Exception as e:
            hot = {}
            logger.warning("[MktScreen] 热点题材源失败: %s", e)
            missing.append(f"热点题材源失败: {type(e).__name__}: {e}")
        main_themes = [(t, c) for t, c in (hot.get("hot_tags") or [])[:10]]
        for s in hot.get("stocks", []) or []:
            code = str(s.get("code", "") or "")
            if len(code) == 6:
                _put(code, {
                    "code": code, "name": s.get("name", ""), "source": "热点题材",
                    "reason": s.get("reason", ""), "change_pct": s.get("change_pct"),
                    "turnover_pct": s.get("turnover_pct") or s.get("turnover_rate"),
                })

    # ── 条件搜索（支撑/资金/趋势结构，非连板追高）──
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

    # ── 池底补采（2026-10-28）：hot 在涨停潮里几乎全是封板，search 默认词若
    #    命中偏少，池子会被「19 涨停 + 1 ST」吃光。pool < 门槛时自动追加更宽
    #    过滤词，目标是**可买**候选，不是封板名单。
    _POOL_FLOOR = 40
    if "search" in srcs and len(pool) < _POOL_FLOOR:
        tried = set(qs)
        for q in _QUERY_EXPAND:
            if q in tried:
                continue
            tried.add(q)
            try:
                res = call_tool("search_stocks", query=q, source="eastmoney",
                                top_n=int(search_top_n))
            except Exception as e:
                missing.append(f"条件搜索扩池[{q}]: {type(e).__name__}: {e}")
                continue
            stocks = (res or {}).get("stocks", []) if isinstance(res, dict) else []
            if isinstance(res, dict) and res.get("error") and not stocks:
                missing.append(f"条件搜索扩池[{q}]: {res.get('error')}")
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
            if len(pool) >= _POOL_FLOOR:
                break
        # 真空池（0 只）走下方交易日历标注，不写「偏少」——否则会污染
        # 「全源成功真空」契约（2026-10-21 missing==[] 才是真实空候选）。
        if 0 < len(pool) < _POOL_FLOOR:
            missing.append(
                f"池底补采后仍偏少: pool={len(pool)}（<{_POOL_FLOOR}）"
                f"——若大半为封板/ST，属盘面结构，不是数据故障")

    # ── 人气榜（flow）：关注度/资金聚集线索，**不是**涨停池 ──
    if "flow" in srcs:
        try:
            hr = call_tool("get_hot_rank", top_n=50) or {}
            hr_stocks = hr.get("stocks") if isinstance(hr, dict) else None
            if isinstance(hr, dict) and hr.get("error") and not hr_stocks:
                missing.append(f"人气榜源失败: {hr.get('error')}")
            for s in hr_stocks or []:
                code = str(s.get("code") or s.get("stock_code") or "")
                if len(code) != 6:
                    continue
                _put(code, {
                    "code": code, "name": s.get("name") or s.get("stock_name", ""),
                    "source": "人气榜", "reason": s.get("reason") or "",
                    "change_pct": s.get("change_pct"),
                    "turnover_pct": s.get("turnover_pct") or s.get("turnover_rate"),
                })
        except Exception as e:
            logger.warning("[MktScreen] 人气榜源失败: %s", e)
            missing.append(f"人气榜源失败: {type(e).__name__}: {e}")

    # ── 板块成分（sector）：主线板块内的非涨停候选 ──
    if "sector" in srcs:
        try:
            hs = fetch_hot_sectors() or {}
            sd = hs.get("data") if isinstance(hs, dict) and "data" in hs else hs
            if isinstance(hs, dict) and hs.get("error"):
                missing.append(f"热门板块源失败: {hs.get('error')}")
            board_names = []
            for s in ((sd or {}).get("industry") or [])[:6]:
                nm = s.get("name") or s.get("board_name") or ""
                if nm:
                    board_names.append(nm)
            for s in ((sd or {}).get("concept") or [])[:4]:
                nm = s.get("name") or s.get("board_name") or ""
                if nm:
                    board_names.append(nm)
            for bn in board_names[:8]:
                try:
                    br = call_tool("get_sector_stocks", board_name=bn, limit=15) or {}
                    rows = br.get("data") or br.get("stocks") or []
                    if isinstance(br, dict) and br.get("error") and not rows:
                        missing.append(f"板块成分[{bn}]: {br.get('error')}")
                        continue
                    for s in rows:
                        code = str(s.get("code") or s.get("stock_code") or s.get("symbol") or "")
                        if len(code) != 6:
                            continue
                        _put(code, {
                            "code": code, "name": s.get("name") or s.get("stock_name", ""),
                            "source": "板块", "reason": f"板块:{bn}",
                            "change_pct": s.get("change_pct") or s.get("pct_change"),
                            "turnover_pct": s.get("turnover_pct") or s.get("turnover_rate"),
                        })
                except Exception as e:
                    missing.append(f"板块成分[{bn}]: {type(e).__name__}: {e}")
        except Exception as e:
            logger.warning("[MktScreen] 板块成分源失败: %s", e)
            missing.append(f"板块成分源失败: {type(e).__name__}: {e}")

    # ── 本地结构扫描（scan）：不依赖东财搜索，用日线几何捞「有支撑/未追高」──
    if "scan" in srcs:
        try:
            universe = list(pool.keys())
            # 扩样：basicinfo 抽样，避免只扫到已有候选的邻居
            try:
                from app.utils.basicinfo_db import get_stock_basic_db
                allc = get_stock_basic_db().market_all_codes(status="active") or []
                step = max(1, len(allc) // 80) if allc else 1
                universe.extend(allc[::step][:120])
            except Exception as e:
                missing.append(f"结构扫描全市场抽样: {type(e).__name__}: {e}")
            seen_u = set()
            scanned = 0
            for code in universe:
                if code in seen_u or len(code) != 6 or code in pool:
                    continue
                seen_u.add(code)
                if scanned >= 150:
                    break
                scanned += 1
                try:
                    bars = fetch_kline(code, days=40)
                except Exception:
                    continue
                if not bars or len(bars) < 20:
                    continue
                closes = [b["close"] for b in bars]
                highs = [b["high"] for b in bars]
                lows = [b["low"] for b in bars]
                ma5 = compute_ma(closes, 5)
                ma10 = compute_ma(closes, 10)
                ma20 = compute_ma(closes, 20)
                c = closes[-1]
                m5, m10, m20 = ma5[-1], ma10[-1], ma20[-1]
                if not (m5 and m10 and m20):
                    continue
                # 结构门槛（客观，不打分）：站上/贴均线 且 非 20 日极端高位
                near = (abs(c - m5) / m5 <= 0.03 or abs(c - m10) / m10 <= 0.03
                        or abs(c - m20) / m20 <= 0.03 or c >= m5)
                win = min(20, len(bars))
                hi20, lo20 = max(highs[-win:]), min(lows[-win:])
                pos = ((c - lo20) / (hi20 - lo20) * 100) if hi20 > lo20 else 50.0
                if not near or pos >= 92:
                    continue
                _put(code, {
                    "code": code, "name": "", "source": "结构扫描",
                    "reason": "站上/贴均线且非20日高位",
                    "change_pct": None, "turnover_pct": None,
                })
        except Exception as e:
            logger.warning("[MktScreen] 结构扫描源失败: %s", e)
            missing.append(f"结构扫描源失败: {type(e).__name__}: {e}")

    # ── 批量回填实时行情（事实补全，不是筛选；2026-10-20 改分块绕过 20 只截断）──
    if fill_quote and pool:
        try:
            from .common import batch_realtime_quotes
            quotes, failed_chunks = batch_realtime_quotes(list(pool.keys()))
            for chunk in failed_chunks:
                missing.append(f"行情回填: 分块失败{len(chunk)}只({','.join(chunk[:3])}...)")
            for code_str, q in quotes.items():
                if not isinstance(q, dict):
                    continue
                target = pool.get(str(q.get("stock_code") or code_str))
                if target is None:
                    continue
                # 契约对齐（2026-10-21 线上假阴性根因）：get_realtime_quote 的
                # 统一键是 last/changePercent，写 price/change_pct 永远取不到
                for dst, src in (("price", "last"), ("change_pct", "changePercent"),
                                 ("turnover_pct", "turnover_pct"), ("turnover_pct", "turnover_rate"),
                                 ("vol_ratio", "vol_ratio"), ("high", "high"),
                                 ("low", "low"), ("amount", "amount")):
                    v = q.get(src)
                    if v not in (None, "", 0):
                        target[dst] = v
                # 扩展字段（漏斗 objective_preexclude 用）：name / 涨跌停价 / 昨收
                nm = q.get("name")
                if nm not in (None, ""):
                    target["name"] = nm
                for dst, src in (("limit_up", "limit_up"), ("limit_down", "limit_down")):
                    v = q.get(src)
                    # 0 是合法哨兵（无涨跌幅限制），仅排除 None / 空串
                    if v not in (None, ""):
                        try:
                            target[dst] = float(v)
                        except (TypeError, ValueError):
                            pass
                pc = q.get("previousClose")
                if pc not in (None, "", 0):
                    target["previous_close"] = pc
        except Exception as e:
            missing.append(f"行情回填: {type(e).__name__}: {e}")

    # ── 代码规则初筛（2026-10-28）：先剔不活跃/新股，禁止「命中 1470 取前 10」──
    # list_date 批量补全（新股判定）；缺字段不杀。
    if pool:
        try:
            from app.utils.basicinfo_db import get_stock_basic_db
            bdb = get_stock_basic_db()
            for code_str in list(pool.keys()):
                try:
                    info = bdb.get_stock(code_str) or {}
                except Exception:
                    info = {}
                ld = info.get("list_date") if isinstance(info, dict) else None
                if ld and not pool[code_str].get("list_date"):
                    pool[code_str]["list_date"] = str(ld)
        except Exception as e:
            missing.append(f"上市日期补全: {type(e).__name__}: {e}")

    try:
        from ._helpers import rule_prefilter, code_narrow
        cand_list, rule_stats = rule_prefilter(list(pool.values()))
    except Exception as e:
        cand_list = list(pool.values())
        rule_stats = {"error": str(e)}
        missing.append(f"规则初筛: {type(e).__name__}: {e}")

    # ── 代码收窄（2026-10-28）：400+ → ≤40，再进画像/评分；禁止取前 10 ──
    try:
        cand_list, narrow_stats = code_narrow(cand_list, main_themes=main_themes, max_n=40)
    except Exception as e:
        narrow_stats = {"error": str(e)}
        missing.append(f"代码收窄: {type(e).__name__}: {e}")

    candidates = cand_list
    if min_turnover is not None:
        lo = float(min_turnover)
        # turnover_pct 缺失（0/None）时不据此剔除 —— 避免把没有字段的票误杀
        candidates = [c for c in candidates
                      if not (c.get("turnover_pct") and float(c["turnover_pct"]) < lo)]

    # 空池兜底校验交易日历（2026-10-21）：非交易日/日历不可确认必须显式标注，
    # 保证「纯真空池 + missing 为空」只可能出现在可核实的交易日上。
    if not candidates:
        _annotate_empty_pool_calendar(missing, date)

    return {
        "strategy": strategy,
        "date": date,
        "sources_used": srcs,
        "queries_used": qs,
        "pool_size": len(candidates),
        "pool_raw": len(pool),
        "rule_prefilter": rule_stats,
        "code_narrow": narrow_stats,
        "main_themes": main_themes,
        "candidates": candidates[:limit] if limit else candidates,
        "missing": missing,
    }

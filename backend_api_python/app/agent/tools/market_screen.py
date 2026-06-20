# -*- coding: utf-8 -*-
"""全市场短线选股 — 自动选择盘中/盘后/收盘策略进行市场筛选。"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

# -*- coding: utf-8 -*-
"""
Market Screener Skill — A股选股（盘中/尾盘/盘后统一入口，按时间自动切换策略）。

时间自动调度：
  09:30-14:29 → 盘中短线（涨停池连板 + 热门板块龙头 + 题材归因）
  14:30-15:00 → 尾盘隔夜（条件初筛 + 尾盘特征验证 + 收盘抢筹）
  15:00+      → 盘后复盘（技术形态筛选 + 介入点计算 + 次日计划）

两阶段流程（各策略共享）：
  Phase 1: Python 预筛选（0 token，调底层数据源）
  Phase 2: 对候选股逐只调工具做深入分析

与 screening_agent 的区别：screening_agent 是通用条件选股（search_stocks），
本 Skill 是针对 A 股时间场景特化的三合一自动策略。
"""
from __future__ import annotations

import logging
import os
import sys
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from app.agent.chain.schema import FactorItem, SkillReport

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# 通用数据加载
# ═══════════════════════════════════════════════════════════════

_backend_root = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")
)
if _backend_root not in sys.path:
    sys.path.insert(0, _backend_root)

_writer_cache = None
_basic_db_cache = None


def _load_env():
    try:
        from dotenv import load_dotenv
        for p in [
            os.path.join(_backend_root, ".env"),
            os.path.join(os.path.dirname(_backend_root), ".env"),
        ]:
            if os.path.isfile(p):
                load_dotenv(p, override=False)
                break
    except Exception:
        pass


def _get_writer():
    global _writer_cache
    if _writer_cache is not None:
        return _writer_cache
    _load_env()
    from app.utils.db_market import get_market_kline_writer
    _writer_cache = get_market_kline_writer()
    return _writer_cache


def _get_basic_db():
    global _basic_db_cache
    if _basic_db_cache is not None:
        return _basic_db_cache
    _load_env()
    from app.utils.basicinfo_db import get_stock_basic_db
    _basic_db_cache = get_stock_basic_db()
    return _basic_db_cache


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


# ═══════════════════════════════════════════════════════════════
# 通用数据采集
# ═══════════════════════════════════════════════════════════════

def _fetch_kline(code: str, days: int = 60) -> List[Dict]:
    """从 db_market 获取日K线（前复权）。"""
    from app.data_sources.provider.adjustment import unadj_to_qfq
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=int(days * 1.5))).strftime("%Y-%m-%d")
    try:
        writer = _get_writer()
        data = writer.query("CNStock", code, "1D", start_time=start, end_time=end, limit=0)
        if not data:
            return []
        bars = []
        for r in data:
            bars.append({
                "time": str(r["time"])[:10],
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": float(r["volume"]),
            })
        return unadj_to_qfq(bars, code)
    except Exception:
        return []


def _fetch_zt_pool(date: str) -> List[Dict]:
    """涨停池。"""
    try:
        from app.market_cn.dragon_limit import get_zt_pool
        return get_zt_pool(date)
    except Exception as e:
        logger.warning("[MktScreen] 涨停池获取失败: %s", e)
        return []


def _fetch_dt_pool(date: str) -> List[Dict]:
    """跌停池。"""
    try:
        from app.market_cn.dragon_limit import get_dt_pool
        return get_dt_pool(date)
    except Exception as e:
        logger.warning("[MktScreen] 跌停池获取失败: %s", e)
        return []


def _fetch_broken_board(date: str) -> List[Dict]:
    """炸板池。"""
    try:
        from app.market_cn.dragon_limit import get_broken_board
        return get_broken_board(date)
    except Exception as e:
        logger.warning("[MktScreen] 炸板池获取失败: %s", e)
        return []


def _fetch_hot_stocks_with_reason(date: str) -> Dict:
    """同花顺强势股+题材归因。"""
    import requests as _req
    url = (
        f"http://zx.10jqka.com.cn/event/api/getharden/"
        f"date/{date}/orderby/date/orderway/desc/charset/GBK/"
    )
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/117.0.0.0"}
    try:
        r = _req.get(url, headers=headers, timeout=10)
        data = r.json()
        if data.get("errocode", 0) != 0:
            return {"error": f"同花顺错误: {data.get('errormsg', '')}"}
        rows = data.get("data") or []
        stocks = []
        for row in rows:
            stocks.append({
                "code": row.get("code", ""),
                "name": row.get("name", ""),
                "reason": row.get("reason", ""),
                "change_pct": float(row.get("zhangfu", 0) or 0),
                "turnover_pct": float(row.get("huanshou", 0) or 0),
                "amount": float(row.get("chengjiaoe", 0) or 0),
            })
        tag_counter: Counter = Counter()
        for s in stocks:
            if s["reason"]:
                tags = [t.strip() for t in s["reason"].replace("，", "+").replace(",", "+").split("+") if t.strip()]
                tag_counter.update(tags)
        return {"stocks": stocks, "hot_tags": tag_counter.most_common(20)}
    except Exception as e:
        logger.warning("[MktScreen] 强势股获取失败: %s", e)
        return {"error": str(e)}


def _fetch_hot_sectors() -> Dict:
    """热门板块。"""
    try:
        from app.market_cn.china_market import get_hot_sectors
        return get_hot_sectors(industry_limit=15, concept_limit=15)
    except Exception as e:
        logger.warning("[MktScreen] 热门板块获取失败: %s", e)
        return {"error": str(e)}


# ═══════════════════════════════════════════════════════════════
# 通用技术指标计算
# ═══════════════════════════════════════════════════════════════

def _compute_ma(closes: List[float], period: int) -> List[Optional[float]]:
    n = len(closes)
    ma = [None] * n
    for i in range(period - 1, n):
        ma[i] = sum(closes[i - period + 1:i + 1]) / period
    return ma


def _compute_ema(values: List[float], period: int) -> List[float]:
    n = len(values)
    if n == 0:
        return []
    ema = [values[0]]
    k = 2.0 / (period + 1)
    for i in range(1, n):
        ema.append(values[i] * k + ema[-1] * (1 - k))
    return ema


def _compute_macd(closes: List[float]) -> Dict[str, List[float]]:
    ema12 = _compute_ema(closes, 12)
    ema26 = _compute_ema(closes, 26)
    dif = [a - b for a, b in zip(ema12, ema26)]
    dea = _compute_ema(dif, 9)
    macd_bar = [2 * (d - e) for d, e in zip(dif, dea)]
    return {"dif": dif, "dea": dea, "macd": macd_bar}


def _compute_rsi(closes: List[float], period: int = 14) -> List[float]:
    n = len(closes)
    if n < period + 1:
        return [50.0] * n
    gains, losses = [], []
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = sum(gains) / period
    al = sum(losses) / period
    rsi = [50.0] * (period + 1)
    rsi[period] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
    alpha = 1.0 / period
    for i in range(period + 1, n):
        d = closes[i] - closes[i - 1]
        ag = alpha * max(d, 0.0) + (1 - alpha) * ag
        al = alpha * max(-d, 0.0) + (1 - alpha) * al
        rsi.append(100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al))
    return rsi


def _compute_volume_ratio(volumes: List[float], window: int = 5) -> List[float]:
    n = len(volumes)
    vr = [0.0] * n
    for i in range(window, n):
        avg = sum(volumes[i - window:i]) / window
        if avg > 0:
            vr[i] = volumes[i] / avg
    return vr


def _compute_kdj(bars: List[Dict], period: int = 9) -> Dict[str, List[float]]:
    n = len(bars)
    k_vals = [50.0] * n
    d_vals = [50.0] * n
    j_vals = [50.0] * n
    for i in range(period - 1, n):
        high_max = max(b["high"] for b in bars[i - period + 1:i + 1])
        low_min = min(b["low"] for b in bars[i - period + 1:i + 1])
        if high_max == low_min:
            rsv = 50.0
        else:
            rsv = (bars[i]["close"] - low_min) / (high_max - low_min) * 100
        k_vals[i] = 2 / 3 * k_vals[i - 1] + 1 / 3 * rsv
        d_vals[i] = 2 / 3 * d_vals[i - 1] + 1 / 3 * k_vals[i]
        j_vals[i] = 3 * k_vals[i] - 2 * d_vals[i]
    return {"k": k_vals, "d": d_vals, "j": j_vals}


def _compute_atr(bars: List[Dict], period: int = 14) -> List[float]:
    n = len(bars)
    trs = [0.0] * n
    for i in range(1, n):
        hl = bars[i]["high"] - bars[i]["low"]
        hc = abs(bars[i]["high"] - bars[i - 1]["close"])
        lc = abs(bars[i]["low"] - bars[i - 1]["close"])
        trs[i] = max(hl, hc, lc)
    atrs = [0.0] * n
    if n > period:
        atrs[period] = sum(trs[1:period + 1]) / period
        for i in range(period + 1, n):
            atrs[i] = (atrs[i - 1] * (period - 1) + trs[i]) / period
    return atrs


# ═══════════════════════════════════════════════════════════════
# 策略通用 — 龙回头弱转强检测（盘中+盘后共享）
# ═══════════════════════════════════════════════════════════════

def _fetch_recent_zt_pools(days: int = 8) -> Dict[str, List[Dict]]:
    pools = {}
    today = datetime.now()
    for i in range(days):
        d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        pool = _fetch_zt_pool(d)
        if pool:
            pools[d] = pool
    return pools


def _scan_dragon_pullback(date: str) -> List[Dict]:
    """扫描龙回头弱转强模式。"""
    recent_pools = _fetch_recent_zt_pools(8)
    code_history: Dict[str, List[Dict]] = {}

    for pool_date, pool in recent_pools.items():
        for s in pool:
            code = s.get("stock_code", "")
            if not code:
                continue
            if code not in code_history:
                code_history[code] = []
            code_history[code].append({
                "date": pool_date,
                "continuous_days": int(s.get("continuous_zt_days", 1) or 1),
                "reason": s.get("reason", ""),
                "name": s.get("stock_name", ""),
            })

    dragon_codes = {}
    for code, records in code_history.items():
        max_days = max(r["continuous_days"] for r in records)
        if max_days >= 2:
            dragon_codes[code] = {
                "name": records[0]["name"],
                "max_continuous_days": max_days,
                "zt_dates": [r["date"] for r in records],
                "last_zt_date": min(records[0]["date"], date),
                "reason": records[0]["reason"],
            }

    if not dragon_codes:
        return []

    candidates = []
    for code, info in dragon_codes.items():
        bars = _fetch_kline(code, days=30)
        if len(bars) < 10:
            continue

        closes = [b["close"] for b in bars]
        volumes = [b["volume"] for b in bars]
        highs = [b["high"] for b in bars]
        lows = [b["low"] for b in bars]
        n = len(bars)
        i = n - 1

        lookback_start = max(0, n - 15)
        peak_idx = lookback_start
        for j in range(lookback_start, n):
            if highs[j] > highs[peak_idx]:
                peak_idx = j

        if peak_idx >= i:
            continue

        peak_price = highs[peak_idx]
        current_close = closes[i]
        trough_price = min(lows[peak_idx + 1:i + 1]) if peak_idx + 1 <= i else current_close
        pullback_pct = (peak_price - trough_price) / peak_price * 100

        if pullback_pct < 8 or pullback_pct > 35:
            continue

        signals = []
        strength_score = 0

        pullback_volumes = volumes[peak_idx + 1:i]
        avg_pullback_vol = sum(pullback_volumes) / len(pullback_volumes) if pullback_volumes else 1
        vol_ratio_today = volumes[i] / avg_pullback_vol if avg_pullback_vol > 0 else 1
        if vol_ratio_today > 1.5:
            signals.append(f"放量{vol_ratio_today:.1f}倍")
            strength_score += 15
        elif vol_ratio_today > 1.2:
            signals.append(f"温和放量{vol_ratio_today:.1f}倍")
            strength_score += 8

        if closes[i] > bars[i]["open"]:
            signals.append("收阳")
            strength_score += 5

        ma5 = _compute_ma(closes, 5)
        if ma5[i] is not None and closes[i] > ma5[i]:
            signals.append("站上MA5")
            strength_score += 8

        if ma5[i] is not None and ma5[i - 1] is not None:
            ma5_slope_today = (ma5[i] - ma5[i - 1]) / ma5[i - 1] * 100 if ma5[i - 1] > 0 else 0
            if ma5_slope_today > 0:
                signals.append("MA5拐头")
                strength_score += 5

        rsi = _compute_rsi(closes)
        if rsi[i] > 40 and rsi[i - 1] < 40:
            signals.append(f"RSI低位回升{rsi[i]:.0f}")
            strength_score += 10
        elif 40 <= rsi[i] <= 60:
            signals.append(f"RSI{rsi[i]:.0f}中性")
            strength_score += 3

        if len(pullback_volumes) >= 2:
            vol_declining = all(
                pullback_volumes[j] <= pullback_volumes[j - 1] * 1.1
                for j in range(1, len(pullback_volumes))
            )
            if vol_declining:
                signals.append("回调缩量(卖盘衰竭)")
                strength_score += 10

        if ma5[i] is not None and lows[i] <= ma5[i] * 1.01 and closes[i] > ma5[i]:
            signals.append("均线支撑")
            strength_score += 8

        if len(signals) < 2:
            continue

        candidates.append({
            "code": code,
            "name": info["name"],
            "source": "龙回头",
            "max_continuous_days": info["max_continuous_days"],
            "zt_dates": info["zt_dates"],
            "reason": info["reason"],
            "pullback_pct": round(pullback_pct, 1),
            "peak_price": round(peak_price, 3),
            "trough_price": round(trough_price, 3),
            "close": round(closes[i], 3),
            "vol_ratio_today": round(vol_ratio_today, 2),
            "rsi": round(rsi[i], 2),
            "signals": signals,
            "strength_score": strength_score,
        })

    candidates.sort(key=lambda x: -x["strength_score"])
    return candidates


# ═══════════════════════════════════════════════════════════════
# 策略 1 — 盘中短线（9:30-14:29）
# ═══════════════════════════════════════════════════════════════

def _tech_check(code: str) -> Optional[Dict]:
    """对单只候选股做快速技术面检查（纯 Python）。"""
    bars = _fetch_kline(code, days=60)
    if len(bars) < 20:
        return None
    closes = [b["close"] for b in bars]
    volumes = [b["volume"] for b in bars]
    i = len(bars) - 1
    ma5 = _compute_ma(closes, 5)
    ma10 = _compute_ma(closes, 10)
    ma20 = _compute_ma(closes, 20)
    rsi = _compute_rsi(closes)
    vol_ratio = _compute_volume_ratio(volumes)
    ma_bullish = (
        ma5[i] is not None and ma10[i] is not None and ma20[i] is not None
        and ma5[i] > ma10[i] > ma20[i]
    )
    recent_vol_ratio = vol_ratio[i] if i < len(vol_ratio) else 0
    return {
        "close": round(closes[i], 3),
        "ma5": round(ma5[i], 3) if ma5[i] else None,
        "ma10": round(ma10[i], 3) if ma10[i] else None,
        "ma20": round(ma20[i], 3) if ma20[i] else None,
        "rsi": round(rsi[i], 2),
        "vol_ratio": round(recent_vol_ratio, 3),
        "ma_bullish": ma_bullish,
    }


def _prescreen_intraday(date: str) -> Dict[str, Any]:
    """盘中预筛选：涨停池 + 强势股题材 + 龙回头。"""
    zt_pool = _fetch_zt_pool(date)
    dt_pool = _fetch_dt_pool(date)
    broken_pool = _fetch_broken_board(date)
    hot_data = _fetch_hot_stocks_with_reason(date)
    sector_data = _fetch_hot_sectors()

    hot_stocks = hot_data.get("stocks", [])
    hot_tags = hot_data.get("hot_tags", [])
    industry_sectors = sector_data.get("industry", [])
    concept_sectors = sector_data.get("concept", [])

    zt_count = len(zt_pool)
    dt_count = len(dt_pool)
    broken_count = len(broken_pool)
    broken_rate = broken_count / max(1, zt_count + broken_count)

    if zt_count >= 50 and dt_count <= 10:
        mood = "亢奋"
        mood_score = 80
    elif zt_count >= 30 and dt_count <= 20:
        mood = "偏暖"
        mood_score = 65
    elif zt_count < 20 or dt_count > 30:
        mood = "冰点"
        mood_score = 25
    else:
        mood = "中性"
        mood_score = 50

    market_summary = {
        "zt_count": zt_count,
        "dt_count": dt_count,
        "broken_count": broken_count,
        "broken_rate": round(broken_rate * 100, 1),
        "mood": mood,
        "mood_score": mood_score,
    }

    zt_reason_counter: Counter = Counter()
    for s in zt_pool:
        reason = s.get("reason", "") or ""
        if reason:
            for tag in reason.replace("，", "+").replace(",", "+").split("+"):
                tag = tag.strip()
                if tag:
                    zt_reason_counter[tag] += 1

    combined_tags: Counter = Counter()
    for tag, cnt in hot_tags:
        combined_tags[tag] += cnt
    for tag, cnt in zt_reason_counter.items():
        combined_tags[tag] += cnt

    main_themes = combined_tags.most_common(10)

    continuous_board = []
    for s in zt_pool:
        days = int(s.get("continuous_zt_days", 1) or 1)
        if days >= 2:
            continuous_board.append({
                "code": s.get("stock_code", ""),
                "name": s.get("stock_name", ""),
                "continuous_days": days,
                "zt_time": s.get("zt_time", ""),
                "seal_amount": s.get("seal_amount", 0),
                "turnover": s.get("turnover_rate", 0),
                "reason": s.get("reason", ""),
            })
    continuous_board.sort(key=lambda x: (-x["continuous_days"], x["zt_time"] or "99:99"))

    main_theme_keywords = set()
    for tag, _ in main_themes[:5]:
        main_theme_keywords.add(tag)

    theme_stocks = []
    for s in hot_stocks:
        reason = s.get("reason", "") or ""
        if not reason:
            continue
        matched_tags = []
        for tag in reason.replace("，", "+").replace(",", "+").split("+"):
            tag = tag.strip()
            if tag in main_theme_keywords:
                matched_tags.append(tag)
        if matched_tags:
            theme_stocks.append({
                "code": s.get("code", ""),
                "name": s.get("name", ""),
                "reason": reason,
                "matched_tags": matched_tags,
                "change_pct": s.get("change_pct", 0),
                "turnover_pct": s.get("turnover_pct", 0),
                "amount": s.get("amount", 0),
            })
    theme_stocks.sort(key=lambda x: -x["change_pct"])

    dragon_pullback = _scan_dragon_pullback(date)

    candidates = {}
    for s in continuous_board:
        code = s["code"]
        candidates[code] = {
            "code": code, "name": s["name"], "source": "连板",
            "continuous_days": s["continuous_days"], "zt_time": s["zt_time"],
            "reason": s["reason"], "change_pct": 0, "tags": [], "pullback_signals": [],
        }
    for s in theme_stocks[:20]:
        code = s["code"]
        if code in candidates:
            candidates[code]["source"] += "+主线题材"
            candidates[code]["tags"] = s["matched_tags"]
        else:
            candidates[code] = {
                "code": code, "name": s["name"], "source": "主线题材",
                "continuous_days": 0, "zt_time": "", "reason": s["reason"],
                "change_pct": s["change_pct"], "tags": s["matched_tags"], "pullback_signals": [],
            }
    for s in dragon_pullback[:10]:
        code = s["code"]
        if code in candidates:
            candidates[code]["source"] += "+龙回头"
            candidates[code]["pullback_signals"] = s["signals"]
        else:
            candidates[code] = {
                "code": code, "name": s["name"], "source": "龙回头",
                "continuous_days": s.get("max_continuous_days", 0), "zt_time": "",
                "reason": s["reason"], "change_pct": 0, "tags": [],
                "pullback_signals": s["signals"],
                "pullback_pct": s["pullback_pct"], "strength_score": s["strength_score"],
            }

    candidate_list = list(candidates.values())
    source_priority = {"连板": 0, "龙回头": 1, "主线题材": 2}
    candidate_list.sort(key=lambda x: (
        min(source_priority.get(s, 9) for s in x.get("source", "").split("+")),
        -(x.get("continuous_days", 0)),
        -x.get("strength_score", 0),
    ))

    return {
        "market": market_summary,
        "main_themes": main_themes[:10],
        "continuous_board": continuous_board[:10],
        "dragon_pullback": dragon_pullback[:10],
        "candidates": candidate_list[:20],
    }


def _deep_analyze_intraday(
    candidate: Dict, tech: Optional[Dict],
    call_tool_fn, _tool_calls, _tool_nodes, _missing_data,
) -> Optional[Dict]:
    """盘中 — 对单只候选股做深入分析。"""
    code = candidate["code"]
    try:
        fund_flow = call_tool_fn("get_fund_flow_realtime", stock_code=code)
        snapshot = call_tool_fn("get_indicator_snapshot", stock_code=code)

        if _tool_calls is not None:
            for t in ["get_fund_flow_realtime", "get_indicator_snapshot"]:
                if t not in _tool_calls:
                    _tool_calls.append(t)

        score = 55.0
        signals = []
        factors = []

        source = candidate.get("source", "")
        if "连板" in source:
            score += 12
            signals.append(f"{candidate.get('continuous_days', 1)}连板")
        if "主线题材" in source:
            score += 8
            tags_str = "+".join(candidate.get("tags", []))
            signals.append(f"主线题材:{tags_str}")
        if "龙回头" in source:
            score += 10
            pb_signals = candidate.get("pullback_signals", [])
            signals.append(f"龙回头弱转强:{','.join(pb_signals[:3])}")
            pullback_pct = candidate.get("pullback_pct", 0)
            if pullback_pct:
                signals.append(f"回调{pullback_pct}%")
        factors.append(FactorItem(
            name="来源", value=source,
            score=70 if "连板" in source else (68 if "龙回头" in source else (60 if "主线题材" in source else 50)),
        ))

        if tech:
            if tech["ma_bullish"]:
                score += 8
                signals.append("均线多头排列")
            if 40 <= tech["rsi"] <= 70:
                score += 3
                signals.append(f"RSI{tech['rsi']}适中")
            elif tech["rsi"] > 80:
                score -= 5
                signals.append(f"RSI{tech['rsi']}超买")
            if tech["vol_ratio"] > 1.5:
                score += 5
                signals.append(f"量比{tech['vol_ratio']}放量")
            factors.append(FactorItem(
                name="技术面",
                value=f"MA5={tech['ma5']} RSI={tech['rsi']} 量比={tech['vol_ratio']}",
                score=70 if tech["ma_bullish"] else 50,
            ))

        if isinstance(snapshot, dict) and "error" not in snapshot:
            macd = snapshot.get("macd", {})
            macd_sig = str(macd.get("signal", ""))
            if "金叉" in macd_sig:
                score += 6
                signals.append("MACD金叉")
            elif "死叉" in macd_sig:
                score -= 4
                signals.append("MACD死叉")
            factors.append(FactorItem(
                name="MACD",
                value=f"DIF={macd.get('dif', '?')} DEA={macd.get('dea', '?')}",
                score=65 if "金叉" in macd_sig else (35 if "死叉" in macd_sig else 50),
            ))

        if isinstance(fund_flow, dict) and "error" not in fund_flow:
            main_net = fund_flow.get("main_net_inflow", 0) or 0
            if main_net > 0:
                score += 5
                signals.append(f"主力净流入{main_net/10000:.0f}万")
            elif main_net < -5000000:
                score -= 3
                signals.append(f"主力净流出{abs(main_net)/10000:.0f}万")
            factors.append(FactorItem(
                name="资金流",
                value=f"主力净流入={main_net/10000:.0f}万",
                score=65 if main_net > 0 else (35 if main_net < -5000000 else 50),
            ))

        reason = candidate.get("reason", "")
        if "ST" in reason or "退市" in reason:
            score -= 20
            signals.append("ST/退市风险")

        score = max(0, min(100, score))
        direction = "bullish" if score >= 60 else ("bearish" if score <= 40 else "neutral")

        return {
            "code": code, "name": candidate.get("name", ""),
            "source": source, "reason": reason,
            "score": round(score, 1), "direction": direction,
            "signal": ", ".join(signals[:5]),
            "factors": [f.to_dict() for f in factors],
            "tech": tech,
        }
    except Exception as e:
        logger.warning("[MktScreen] 盘中深入分析 %s 失败: %s", code, e)
        return None


# ═══════════════════════════════════════════════════════════════
# 策略 2 — 尾盘隔夜（14:30-15:00）
# ═══════════════════════════════════════════════════════════════

def _prescreen_eod(call_tool_fn) -> Dict[str, Any]:
    """尾盘预筛选：条件选股 + Python 尾盘特征验证。"""
    screener_result = call_tool_fn(
        "search_stocks",
        query="涨幅3%到8% 换手率大于3% 非ST",
        source="eastmoney",
        top_n=80,
    )
    raw_stocks = screener_result.get("stocks", []) if isinstance(screener_result, dict) else []

    date = _today_str()
    zt_pool = _fetch_zt_pool(date)

    eod_zt = []
    for s in zt_pool:
        zt_time = s.get("zt_time", "") or ""
        if zt_time and ":" in zt_time:
            try:
                parts = zt_time.split(":")
                hour, minute = int(parts[0]), int(parts[1])
                if hour == 14 and minute >= 30:
                    eod_zt.append({
                        "code": s.get("stock_code", ""),
                        "name": s.get("stock_name", ""),
                        "source": "尾盘封板",
                        "reason": s.get("reason", ""),
                        "zt_time": zt_time,
                        "continuous_days": int(s.get("continuous_zt_days", 1) or 1),
                    })
            except (ValueError, IndexError):
                pass

    hot_data = _fetch_hot_stocks_with_reason(date)
    reason_map = {}
    hot_tags = hot_data.get("hot_tags", [])
    for s in hot_data.get("stocks", []):
        reason_map[s.get("code", "")] = s.get("reason", "")

    main_tags = set()
    for tag, _ in hot_tags[:5]:
        main_tags.add(tag)

    candidates = []
    for s in raw_stocks:
        code = str(s.get("code", "") or s.get("symbol", ""))
        if not code or len(code) != 6:
            continue
        name = s.get("name", "")
        change_pct = float(s.get("change_pct", 0) or s.get("pct_change", 0) or 0)
        turnover = float(s.get("turnover_rate", 0) or 0)

        if change_pct < 3 or change_pct > 8:
            continue

        bars = _fetch_kline(code, days=10)
        if len(bars) < 3:
            continue

        today = bars[-1]
        close = today["close"]
        high = today["high"]
        low = today["low"]
        volume = today["volume"]

        if high <= 0:
            continue

        close_to_high = (high - close) / high * 100
        day_range = high - low
        close_position = (close - low) / day_range if day_range > 0 else 0

        prev_volumes = [bars[j]["volume"] for j in range(max(0, len(bars) - 5), len(bars) - 1)]
        avg_vol = sum(prev_volumes) / len(prev_volumes) if prev_volumes else 1
        vol_ratio = volume / avg_vol if avg_vol > 0 else 1

        eod_score = 50
        signals = []

        if close_to_high < 0.3:
            eod_score += 18
            signals.append("收盘=最高价")
        elif close_to_high < 0.8:
            eod_score += 12
            signals.append("收盘接近最高价")
        elif close_to_high < 1.5:
            eod_score += 5
            signals.append("收盘偏高位")
        else:
            continue

        if vol_ratio > 2.5:
            eod_score += 12
            signals.append(f"大幅放量{vol_ratio:.1f}倍")
        elif vol_ratio > 1.5:
            eod_score += 8
            signals.append(f"放量{vol_ratio:.1f}倍")
        elif vol_ratio > 1.2:
            eod_score += 3
            signals.append(f"温和放量{vol_ratio:.1f}倍")

        if close_position > 0.85:
            eod_score += 10
            signals.append("收盘在日内高位")
        elif close_position > 0.7:
            eod_score += 5

        if 4 <= change_pct <= 6:
            eod_score += 8
            signals.append(f"涨幅{change_pct:.1f}%适中")
        elif 6 < change_pct <= 7:
            eod_score += 3

        reason = reason_map.get(code, "")
        if reason:
            matched = [t for t in reason.replace("，", "+").replace(",", "+").split("+") if t.strip() in main_tags]
            if matched:
                eod_score += 10
                signals.append(f"主线题材:{'+'.join(matched[:2])}")
            else:
                eod_score += 3
                signals.append(f"题材:{reason[:15]}")

        closes = [b["close"] for b in bars]
        rsi = _compute_rsi(closes)
        if rsi[-1] > 80:
            eod_score -= 10
            signals.append(f"RSI{rsi[-1]:.0f}超买警告")

        ma5 = _compute_ma(closes, 5)
        ma10 = _compute_ma(closes, 10)
        if ma5[-1] and ma10[-1] and ma5[-1] > ma10[-1]:
            eod_score += 5
            signals.append("MA5>MA10")

        if len(signals) < 2:
            continue

        candidates.append({
            "code": code, "name": name,
            "change_pct": change_pct, "turnover": turnover,
            "close": round(close, 3), "high": round(high, 3),
            "close_to_high": round(close_to_high, 2),
            "vol_ratio": round(vol_ratio, 2), "rsi": round(rsi[-1], 2),
            "reason": reason, "eod_score": eod_score,
            "signals": signals, "source": "尾盘强势",
        })

    for s in eod_zt:
        code = s["code"]
        bars = _fetch_kline(code, days=5)
        close = bars[-1]["close"] if bars else 0
        candidates.append({
            "code": code, "name": s["name"],
            "change_pct": 9.9, "turnover": 0,
            "close": round(close, 3), "high": round(close, 3),
            "close_to_high": 0, "vol_ratio": 0, "rsi": 0,
            "reason": s.get("reason", ""), "eod_score": 90,
            "signals": [f"尾盘封板{s['zt_time']}", f"{s['continuous_days']}连板"],
            "source": "尾盘封板",
        })

    seen = set()
    unique = []
    for c in sorted(candidates, key=lambda x: -x["eod_score"]):
        if c["code"] not in seen:
            seen.add(c["code"])
            unique.append(c)

    themes = [(tag, cnt) for tag, cnt in hot_tags[:5]]

    return {
        "date": date,
        "screener_count": len(raw_stocks),
        "zt_eod_count": len(eod_zt),
        "main_themes": themes,
        "candidates": unique[:15],
    }


def _deep_analyze_eod(
    candidate: Dict, call_tool_fn, _tool_calls, _tool_nodes, _missing_data,
) -> Optional[Dict]:
    """尾盘 — 深入分析单只候选股，评估隔夜持仓价值。"""
    code = candidate["code"]
    try:
        snapshot = call_tool_fn("get_indicator_snapshot", stock_code=code)
        fund_flow = call_tool_fn("get_fund_flow_realtime", stock_code=code)

        if _tool_calls is not None:
            for t in ["get_indicator_snapshot", "get_fund_flow_realtime"]:
                if t not in _tool_calls:
                    _tool_calls.append(t)

        score = candidate.get("eod_score", 60)
        signals = list(candidate.get("signals", []))
        factors = []

        if isinstance(snapshot, dict) and "error" not in snapshot:
            macd = snapshot.get("macd", {})
            macd_sig = str(macd.get("signal", ""))
            if "金叉" in macd_sig:
                score += 8
                signals.append("MACD金叉")
            elif "死叉" in macd_sig:
                score -= 5
                signals.append("MACD死叉")
            factors.append(FactorItem(
                name="MACD",
                value=f"DIF={macd.get('dif', '?')} DEA={macd.get('dea', '?')}",
                score=65 if "金叉" in macd_sig else (35 if "死叉" in macd_sig else 50),
            ))

            kdj = snapshot.get("kdj", {})
            kdj_sigs = kdj.get("signals") or []
            if any("金叉" in s for s in kdj_sigs):
                score += 5
                signals.append("KDJ金叉")
            factors.append(FactorItem(
                name="KDJ",
                value=f"K={kdj.get('k', '?')} D={kdj.get('d', '?')} J={kdj.get('j', '?')}",
                score=60 if any("金叉" in s for s in kdj_sigs) else 50,
            ))

        if isinstance(fund_flow, dict) and "error" not in fund_flow:
            main_net = fund_flow.get("main_net_inflow", 0) or 0
            if main_net > 0:
                score += 6
                signals.append(f"主力净流入{main_net/10000:.0f}万")
            elif main_net < -5000000:
                score -= 4
                signals.append(f"主力净流出{abs(main_net)/10000:.0f}万")
            factors.append(FactorItem(
                name="资金流",
                value=f"主力净流入={main_net/10000:.0f}万",
                score=65 if main_net > 0 else (35 if main_net < -5000000 else 50),
            ))

        risk_notes = []
        rsi = candidate.get("rsi", 50)
        if rsi > 75:
            risk_notes.append(f"RSI{rsi:.0f}偏高，次日回调风险")
            score -= 5
        if candidate.get("change_pct", 0) > 7:
            risk_notes.append("涨幅>7%，追涨风险高")
            score -= 5

        score = max(0, min(100, score))
        direction = "bullish" if score >= 60 else ("bearish" if score <= 40 else "neutral")

        return {
            "code": code, "name": candidate.get("name", ""),
            "source": candidate.get("source", ""), "reason": candidate.get("reason", ""),
            "score": round(score, 1), "direction": direction,
            "signal": ", ".join(signals[:5]),
            "factors": [f.to_dict() for f in factors],
            "risk_notes": risk_notes,
            "eod_data": {
                "close": candidate.get("close"),
                "close_to_high": candidate.get("close_to_high"),
                "vol_ratio": candidate.get("vol_ratio"),
                "change_pct": candidate.get("change_pct"),
                "rsi": rsi,
            },
        }
    except Exception as e:
        logger.warning("[MktScreen] 尾盘深入分析 %s 失败: %s", code, e)
        return None


# ═══════════════════════════════════════════════════════════════
# 策略 3 — 盘后复盘（15:00+）
# ═══════════════════════════════════════════════════════════════

def _detect_platform_breakout(bars: List[Dict]) -> Optional[Dict]:
    """平台突破：近5日振幅<8%，今日放量突破平台高点。"""
    if len(bars) < 10:
        return None
    recent = bars[-6:-1]
    today = bars[-1]
    highs = [b["high"] for b in recent]
    lows = [b["low"] for b in recent]
    platform_high = max(highs)
    platform_low = min(lows)
    if platform_high <= 0:
        return None
    range_pct = (platform_high - platform_low) / platform_high * 100
    if range_pct > 8:
        return None
    if today["close"] > platform_high and today["close"] > today["open"]:
        vol_ratio = today["volume"] / (sum(b["volume"] for b in recent) / len(recent)) if sum(b["volume"] for b in recent) > 0 else 1
        if vol_ratio > 1.3:
            return {
                "pattern": "平台突破",
                "platform_high": round(platform_high, 2),
                "range_pct": round(range_pct, 1),
                "vol_ratio": round(vol_ratio, 2),
                "score": 75 if vol_ratio > 2 else 65,
            }
    return None


def _detect_volume_reversal(bars: List[Dict]) -> Optional[Dict]:
    """底部放量启动：连续缩量后今日放量阳线。"""
    if len(bars) < 10:
        return None
    recent = bars[-6:-1]
    today = bars[-1]
    vol_trend = [b["volume"] for b in recent]
    close_trend = [b["close"] for b in recent]
    avg_vol = sum(vol_trend) / len(vol_trend) if vol_trend else 1
    is_shrinking = all(v <= avg_vol * 1.1 for v in vol_trend[-3:])
    is_surge = today["volume"] > avg_vol * 1.8 and today["close"] > today["open"]
    if close_trend and close_trend[0] > 0:
        period_change = (close_trend[-1] - close_trend[0]) / close_trend[0] * 100
    else:
        return None
    if is_shrinking and is_surge and abs(period_change) < 5:
        vol_ratio = today["volume"] / avg_vol if avg_vol > 0 else 1
        return {
            "pattern": "底部放量启动",
            "vol_ratio": round(vol_ratio, 2),
            "period_change": round(period_change, 1),
            "score": 70 if vol_ratio > 2.5 else 60,
        }
    return None


def _detect_ma_support_pullback(bars: List[Dict]) -> Optional[Dict]:
    """均线支撑回踩：上升趋势中回踩MA10不破。"""
    if len(bars) < 20:
        return None
    closes = [b["close"] for b in bars]
    ma5 = _compute_ma(closes, 5)
    ma10 = _compute_ma(closes, 10)
    ma20 = _compute_ma(closes, 20)
    if not (ma5[-1] and ma10[-1] and ma20[-1]):
        return None
    if not (ma5[-1] > ma10[-1] > ma20[-1]):
        return None
    today = bars[-1]
    ma10_dist = abs(today["low"] - ma10[-1]) / ma10[-1] * 100
    if ma10_dist < 1.5 and today["close"] > ma10[-1]:
        return {
            "pattern": "均线支撑回踩",
            "ma10": round(ma10[-1], 2),
            "low": round(today["low"], 2),
            "distance_pct": round(ma10_dist, 2),
            "score": 68 if today["close"] > today["open"] else 58,
        }
    return None


def _detect_macd_golden_cross(bars: List[Dict]) -> Optional[Dict]:
    """MACD金叉：DIF上穿DEA。"""
    if len(bars) < 30:
        return None
    closes = [b["close"] for b in bars]
    macd = _compute_macd(closes)
    dif = macd["dif"]
    dea = macd["dea"]
    if dif[-1] >= dea[-1] and dif[-2] < dea[-2]:
        is_underwater = dif[-1] < 0
        score = 72 if is_underwater else 62
        return {
            "pattern": "MACD金叉",
            "dif": round(dif[-1], 3),
            "dea": round(dea[-1], 3),
            "underwater": is_underwater,
            "score": score,
        }
    return None


def _detect_shrink_pullback_breakout(bars: List[Dict]) -> Optional[Dict]:
    """缩量回调后放量突破。"""
    if len(bars) < 15:
        return None
    recent_10 = bars[-11:-1]
    today = bars[-1]
    highs_10 = [b["high"] for b in recent_10]
    peak_idx = highs_10.index(max(highs_10))
    peak_price = max(highs_10)
    if peak_idx >= len(recent_10) - 2:
        return None
    pullback_bars = recent_10[peak_idx + 1:]
    pullback_low = min(b["low"] for b in pullback_bars)
    pullback_pct = (peak_price - pullback_low) / peak_price * 100
    if pullback_pct < 3 or pullback_pct > 12:
        return None
    pullback_vols = [b["volume"] for b in pullback_bars]
    peak_vols = [b["volume"] for b in recent_10[max(0, peak_idx - 2):peak_idx + 1]]
    avg_peak_vol = sum(peak_vols) / len(peak_vols) if peak_vols else 1
    avg_pullback_vol = sum(pullback_vols) / len(pullback_vols) if pullback_vols else 1
    is_shrink = avg_pullback_vol < avg_peak_vol * 0.7
    pullback_high = max(b["high"] for b in pullback_bars)
    is_breakout = today["close"] > pullback_high and today["close"] > today["open"]
    is_volume_up = today["volume"] > avg_pullback_vol * 1.5
    if is_shrink and is_breakout and is_volume_up:
        return {
            "pattern": "缩量回调放量突破",
            "peak": round(peak_price, 2),
            "pullback_pct": round(pullback_pct, 1),
            "vol_ratio": round(today["volume"] / avg_pullback_vol, 2) if avg_pullback_vol > 0 else 0,
            "score": 73,
        }
    return None


def _detect_prev_high_breakout(bars: List[Dict]) -> Optional[Dict]:
    """突破前高：收盘突破近20日最高价。"""
    if len(bars) < 20:
        return None
    today = bars[-1]
    prev_20 = bars[-21:-1]
    prev_high = max(b["high"] for b in prev_20)
    if today["close"] > prev_high and today["close"] > today["open"]:
        vol_5 = sum(b["volume"] for b in bars[-6:-1]) / 5 if len(bars) > 5 else 1
        vol_ratio = today["volume"] / vol_5 if vol_5 > 0 else 1
        if vol_ratio > 1.2:
            return {
                "pattern": "突破前高",
                "prev_high": round(prev_high, 2),
                "close": round(today["close"], 2),
                "vol_ratio": round(vol_ratio, 2),
                "score": 70 if vol_ratio > 2 else 60,
            }
    return None


def _prescreen_post_market(date: str) -> Dict[str, Any]:
    """盘后全市场形态扫描。"""
    hot_data = _fetch_hot_stocks_with_reason(date)
    hot_stocks = hot_data.get("stocks", [])
    hot_tags = hot_data.get("hot_tags", [])

    main_themes = [(tag, cnt) for tag, cnt in hot_tags[:5]]

    try:
                screener_result = search_stocks(
            query="涨幅1%到8% 换手率大于2% 非ST",
            source="eastmoney",
            top_n=100,
        )
        raw_stocks = screener_result.get("stocks", []) if isinstance(screener_result, dict) else []
    except Exception:
        raw_stocks = []

    scan_pool = {}
    for s in hot_stocks:
        code = s.get("code", "")
        if code and len(code) == 6:
            scan_pool[code] = s
    for s in raw_stocks:
        code = str(s.get("code", "") or s.get("symbol", ""))
        if code and len(code) == 6 and code not in scan_pool:
            scan_pool[code] = {
                "code": code,
                "name": s.get("name", ""),
                "change_pct": float(s.get("change_pct", 0) or s.get("pct_change", 0) or 0),
                "turnover_pct": float(s.get("turnover_rate", 0) or 0),
                "reason": "",
            }

    candidates = []
    scanned = 0

    for code, info in scan_pool.items():
        bars = _fetch_kline(code, days=40)
        if len(bars) < 15:
            continue

        scanned += 1
        today = bars[-1]
        closes = [b["close"] for b in bars]

        if len(bars) >= 2:
            prev_close = bars[-2]["close"]
            if prev_close > 0:
                change = (today["close"] - prev_close) / prev_close * 100
                if change > 9.8 or change < -9.8:
                    continue

        patterns = []
        total_score = 0

        for detector in [
            _detect_platform_breakout, _detect_volume_reversal,
            _detect_ma_support_pullback, _detect_macd_golden_cross,
            _detect_shrink_pullback_breakout, _detect_prev_high_breakout,
        ]:
            result = detector(bars)
            if result:
                patterns.append(result)
                total_score += result["score"]

        if not patterns:
            continue

        rsi = _compute_rsi(closes)
        macd = _compute_macd(closes)
        kdj = _compute_kdj(bars)

        rsi_val = rsi[-1]
        if rsi_val > 80:
            total_score -= 10
        elif rsi_val > 70:
            total_score -= 3
        elif 40 < rsi_val < 60:
            total_score += 3

        if kdj["k"][-1] > kdj["d"][-1] and kdj["k"][-2] <= kdj["d"][-2]:
            total_score += 5

        ma5 = _compute_ma(closes, 5)
        ma10 = _compute_ma(closes, 10)
        ma20 = _compute_ma(closes, 20)
        if ma5[-1] and ma10[-1] and ma20[-1]:
            if ma5[-1] > ma10[-1] > ma20[-1]:
                total_score += 5

        vol_5 = sum(b["volume"] for b in bars[-6:-1]) / 5 if len(bars) > 5 else 1
        vol_ratio = today["volume"] / vol_5 if vol_5 > 0 else 1

        total_score = max(0, min(100, total_score))

        signals = [p["pattern"] for p in patterns]
        if rsi_val > 70:
            signals.append(f"RSI{rsi_val:.0f}偏高")
        if vol_ratio > 1.5:
            signals.append(f"放量{vol_ratio:.1f}倍")

        candidates.append({
            "code": code, "name": info.get("name", ""),
            "change_pct": info.get("change_pct", 0),
            "close": round(today["close"], 2),
            "patterns": patterns, "pattern_names": [p["pattern"] for p in patterns],
            "score": total_score, "rsi": round(rsi_val, 1),
            "macd_dif": round(macd["dif"][-1], 3),
            "kdj_k": round(kdj["k"][-1], 1), "vol_ratio": round(vol_ratio, 2),
            "ma5": round(ma5[-1], 2) if ma5[-1] else None,
            "ma10": round(ma10[-1], 2) if ma10[-1] else None,
            "reason": info.get("reason", ""), "signals": signals,
        })

    candidates.sort(key=lambda x: -x["score"])

    return {
        "date": date,
        "scanned": scanned,
        "pool_size": len(scan_pool),
        "main_themes": main_themes,
        "candidates": candidates[:20],
    }


def _deep_analyze_post_market(
    candidate: Dict, call_tool_fn, _tool_calls, _tool_nodes, _missing_data,
) -> Optional[Dict]:
    """盘后 — 深入分析，评估次日介入价值。"""
    code = candidate["code"]
    try:
        snapshot = call_tool_fn("get_indicator_snapshot", stock_code=code)
        fund_flow = call_tool_fn("get_fund_flow_realtime", stock_code=code)

        if _tool_calls is not None:
            for t in ["get_indicator_snapshot", "get_fund_flow_realtime"]:
                if t not in _tool_calls:
                    _tool_calls.append(t)

        score = candidate.get("score", 60)
        signals = list(candidate.get("signals", []))
        factors = []

        if isinstance(snapshot, dict) and "error" not in snapshot:
            macd_data = snapshot.get("macd", {})
            macd_sig = str(macd_data.get("signal", ""))
            if "金叉" in macd_sig:
                score += 5
                signals.append("MACD金叉确认")
            elif "死叉" in macd_sig:
                score -= 5
                signals.append("MACD死叉警告")
            factors.append(FactorItem(
                name="MACD",
                value=f"DIF={macd_data.get('dif', '?')} DEA={macd_data.get('dea', '?')}",
                score=65 if "金叉" in macd_sig else (35 if "死叉" in macd_sig else 50),
            ))

            boll = snapshot.get("boll", {})
            if boll:
                factors.append(FactorItem(
                    name="BOLL",
                    value=f"上轨={boll.get('upper', '?')} 中轨={boll.get('mid', '?')} 下轨={boll.get('lower', '?')}",
                    score=55,
                ))

        if isinstance(fund_flow, dict) and "error" not in fund_flow:
            main_net = fund_flow.get("main_net_inflow", 0) or 0
            if main_net > 0:
                score += 5
                signals.append(f"主力净流入{main_net / 10000:.0f}万")
            elif main_net < -5000000:
                score -= 3
                signals.append(f"主力净流出{abs(main_net) / 10000:.0f}万")
            factors.append(FactorItem(
                name="资金流",
                value=f"主力净流入={main_net / 10000:.0f}万",
                score=65 if main_net > 0 else (35 if main_net < -5000000 else 50),
            ))

        close = candidate.get("close", 0)
        ma10 = candidate.get("ma10")
        entry_low = round(close * 0.995, 2)
        entry_high = round(close * 1.005, 2)
        stop_loss = round(min(ma10 or close * 0.97, close * 0.97), 2)
        risk = close - stop_loss
        if risk <= 0:
            risk = close * 0.03
        target_1 = round(close + risk * 1.5, 2)
        target_2 = round(close + risk * 2.5, 2)

        risk_notes = []
        rsi = candidate.get("rsi", 50)
        if rsi > 75:
            risk_notes.append(f"RSI{rsi:.0f}偏高，短线回调风险")
            score -= 3
        if candidate.get("change_pct", 0) > 6:
            risk_notes.append("涨幅偏大，追涨需谨慎")
        if candidate.get("vol_ratio", 1) > 4:
            risk_notes.append("量能异常放大，注意出货嫌疑")

        score = max(0, min(100, score))
        direction = "bullish" if score >= 60 else ("bearish" if score <= 40 else "neutral")

        return {
            "code": code, "name": candidate.get("name", ""),
            "patterns": candidate.get("pattern_names", []),
            "score": round(score, 1), "direction": direction,
            "signal": ", ".join(signals[:5]),
            "factors": [f.to_dict() for f in factors],
            "risk_notes": risk_notes,
            "entry": {
                "price_low": entry_low, "price_high": entry_high,
                "stop_loss": stop_loss, "target_1": target_1, "target_2": target_2,
                "risk_reward": "1:1.5 / 1:2.5",
            },
            "tech": {
                "close": close, "rsi": candidate.get("rsi"),
                "vol_ratio": candidate.get("vol_ratio"),
                "ma5": candidate.get("ma5"), "ma10": candidate.get("ma10"),
            },
        }
    except Exception as e:
        logger.warning("[MktScreen] 盘后深入分析 %s 失败: %s", code, e)
        return None


# ═══════════════════════════════════════════════════════════════
# 策略选择 — 按时间自动调度
# ═══════════════════════════════════════════════════════════════

def _select_strategy() -> str:
    """根据当前时间返回策略名称: intraday / eod / post_market。"""
    now = datetime.now()
    h, m = now.hour, now.minute
    # 非交易时间(周末/节假日) → 盘后
    if now.weekday() >= 5:
        return "post_market"
    # 盘前 / 盘后 → 盘后
    if h < 9 or (h == 9 and m < 30) or h >= 15:
        return "post_market"
    # 尾盘
    if h >= 14 and m >= 30:
        return "eod"
    # 盘中
    return "intraday"


# ═══════════════════════════════════════════════════════════════
# Skill 定义
# ═══════════════════════════════════════════════════════════════

class MarketScreenerSkill:
    """A股选股（盘中/尾盘/盘后三合一，按时间自动切换）。"""

    def algo_analyze(
        self,
        stock_code: str,
        stock_name: str,
        tool_results: Dict[str, Any],
        **kwargs,
    ) -> Optional[SkillReport]:
        """Phase 1: Python 预筛选（0 token）+ Phase 2: 候选股深入分析。"""
        call_tool_fn = kwargs.get("call_tool_fn")
        _tool_calls = kwargs.get("_tool_calls", [])
        _tool_nodes = kwargs.get("_tool_nodes", [])
        _missing_data = kwargs.get("_missing_data", [])

        strategy = _select_strategy()
        date = _today_str()

        logger.info("[MktScreen] 当前策略: %s", strategy)

        if strategy == "intraday":
            return self._run_intraday(
                date, call_tool_fn, _tool_calls, _tool_nodes, _missing_data,
            )
        elif strategy == "eod":
            return self._run_eod(
                call_tool_fn, _tool_calls, _tool_nodes, _missing_data,
            )
        else:
            return self._run_post_market(
                date, call_tool_fn, _tool_calls, _tool_nodes, _missing_data,
            )

    # ── 盘中策略 ──

    def _run_intraday(self, date, call_tool_fn, _tool_calls, _tool_nodes, _missing_data):
        try:
            prescreen = _prescreen_intraday(date)
        except Exception as e:
            logger.warning("[MktScreen] 盘中预筛选失败: %s", e)
            return None

        market = prescreen["market"]
        main_themes = prescreen["main_themes"]
        candidates = prescreen["candidates"]

        logger.info(
            "[MktScreen] 盘中预筛选: 涨停%d 跌停%d 候选%d只",
            market["zt_count"], market["dt_count"], len(candidates),
        )

        if market["mood_score"] < 30:
            return SkillReport(
                skill_name=self.name, score=25.0, direction="bearish",
                confidence=0.7,
                signal=f"市场冰点（涨停{market['zt_count']}跌停{market['dt_count']}），不宜短线",
                analysis=(
                    f"## 盘中短线选股 — 市场冰点\n\n"
                    f"涨停 {market['zt_count']} 只，跌停 {market['dt_count']} 只，"
                    f"炸板率 {market['broken_rate']}%。\n\n"
                    f"**建议：空仓观望，等待情绪回暖。**"
                ),
                factors=[
                    FactorItem(name="涨停数", value=str(market["zt_count"]), score=market["mood_score"]),
                    FactorItem(name="跌停数", value=str(market["dt_count"]), score=max(0, 100 - market["dt_count"] * 3)),
                    FactorItem(name="炸板率", value=f"{market['broken_rate']}%", score=max(0, 100 - int(market["broken_rate"]))),
                ],
                status="ok",
            )

        if not candidates:
            return SkillReport(
                skill_name=self.name, score=45.0, direction="neutral",
                confidence=0.5, signal="今日无明确短线标的",
                analysis=(
                    f"## 盘中短线选股 — 无明确标的\n\n"
                    f"市场情绪：{market['mood']}（涨停{market['zt_count']}跌停{market['dt_count']}）\n"
                    f"今日无连板股或主线题材强势股进入候选池。"
                ),
                factors=[
                    FactorItem(name="市场情绪", value=market["mood"], score=market["mood_score"]),
                ],
                status="ok",
            )

        tech_results = {}
        for c in candidates[:15]:
            tech = _tech_check(c["code"])
            if tech:
                tech_results[c["code"]] = tech

        analyzed = []
        for c in candidates[:8]:
            tech = tech_results.get(c["code"])
            result = _deep_analyze_intraday(
                c, tech, call_tool_fn, _tool_calls, _tool_nodes, _missing_data,
            )
            if result:
                analyzed.append(result)

        if analyzed:
            avg_score = sum(a["score"] for a in analyzed) / len(analyzed)
            bullish = sum(1 for a in analyzed if a["direction"] == "bullish")
        else:
            avg_score = 50.0
            bullish = 0

        direction = "bullish" if avg_score >= 55 else ("bearish" if avg_score < 45 else "neutral")
        confidence = min(0.9, 0.4 + len(analyzed) * 0.06)

        factors = [
            FactorItem(name="市场情绪", value=market["mood"], score=market["mood_score"]),
            FactorItem(name="涨停/跌停", value=f"{market['zt_count']}/{market['dt_count']}", score=market["mood_score"]),
            FactorItem(name="候选股数", value=str(len(candidates)), score=min(100, len(candidates) * 8 + 30)),
            FactorItem(name="主线题材", value=", ".join(t for t, _ in main_themes[:3]) or "无", score=70 if main_themes else 40),
            FactorItem(name="深入分析数", value=str(len(analyzed)), score=min(100, len(analyzed) * 12 + 20)),
            FactorItem(name="看多比例", value=f"{bullish}/{len(analyzed) or len(candidates)}", score=int(avg_score)),
        ]

        lines = [
            f"## 盘中短线选股结果",
            f"市场情绪: {market['mood']} | 涨停{market['zt_count']} 跌停{market['dt_count']} 炸板率{market['broken_rate']}%",
            f"主线题材: {', '.join(t for t, _ in main_themes[:5]) or '无明确主线'}",
            f"候选: {len(candidates)}只 | 深入分析: {len(analyzed)}只 | 综合评分: {avg_score:.0f}",
            "",
        ]

        cb = prescreen.get("continuous_board", [])
        if cb:
            lines.append("### 连板龙头")
            for s in cb[:5]:
                lines.append(
                    f"- **{s['code']}** {s['name']} | "
                    f"{s['continuous_days']}连板 | 涨停时间{s['zt_time']} | {s['reason']}"
                )
            lines.append("")

        dp = prescreen.get("dragon_pullback", [])
        if dp:
            lines.append("### 龙回头弱转强")
            for s in dp[:5]:
                sig_str = ", ".join(s["signals"][:4])
                lines.append(
                    f"- **{s['code']}** {s['name']} | "
                    f"前期{s['max_continuous_days']}连板 | "
                    f"回调{s['pullback_pct']}% | "
                    f"弱转强信号: {sig_str}"
                )
            lines.append("")

        lines.append("### 候选标的")
        for a in analyzed:
            src = a.get("source", "")
            lines.append(
                f"- **{a['code']}** {a.get('name', '')} | "
                f"评分{a['score']:.0f} | {a['direction']} | "
                f"来源:{src} | {a['signal']}"
            )

        return SkillReport(
            skill_name=self.name, score=round(avg_score, 1),
            direction=direction, confidence=confidence,
            signal=f"短线{bullish}只看多候选，主线:{', '.join(t for t, _ in main_themes[:2]) or '无'}",
            factors=factors, analysis="\n".join(lines),
            output_data={
                "market": market, "main_themes": main_themes,
                "dragon_pullback": dp,
                "candidates": [c for c in candidates[:15]],
                "analyzed": analyzed,
            },
            tools_called=_tool_calls or [],
            missing_data=_missing_data or [],
            status="ok",
        )

    # ── 尾盘策略 ──

    def _run_eod(self, call_tool_fn, _tool_calls, _tool_nodes, _missing_data):
        try:
            prescreen = _prescreen_eod(call_tool_fn)
        except Exception as e:
            logger.warning("[MktScreen] 尾盘预筛选失败: %s", e)
            return None

        candidates = prescreen["candidates"]
        main_themes = prescreen["main_themes"]

        logger.info("[MktScreen] 尾盘预筛选: 条件选股%d只, 尾盘封板%d只, 候选%d只",
                     prescreen["screener_count"], prescreen["zt_eod_count"], len(candidates))

        if not candidates:
            return SkillReport(
                skill_name=self.name, score=40.0, direction="neutral",
                confidence=0.5, signal="今日无合适隔夜标的",
                analysis=(
                    f"## 尾盘选股 — 无合适标的\n\n"
                    f"条件选股扫描 {prescreen['screener_count']} 只，"
                    f"尾盘封板 {prescreen['zt_eod_count']} 只，"
                    f"经尾盘特征验证后无合格标的。\n\n"
                    f"**建议：空仓过夜，等待明日机会。**"
                ),
                factors=[
                    FactorItem(name="条件选股", value=str(prescreen["screener_count"]), score=40),
                    FactorItem(name="尾盘封板", value=str(prescreen["zt_eod_count"]), score=50),
                ],
                status="ok",
            )

        analyzed = []
        for c in candidates[:6]:
            result = _deep_analyze_eod(c, call_tool_fn, _tool_calls, _tool_nodes, _missing_data)
            if result:
                analyzed.append(result)

        if analyzed:
            avg_score = sum(a["score"] for a in analyzed) / len(analyzed)
            bullish = sum(1 for a in analyzed if a["direction"] == "bullish")
        else:
            avg_score = 50.0
            bullish = 0

        direction = "bullish" if avg_score >= 55 else ("bearish" if avg_score < 45 else "neutral")
        confidence = min(0.85, 0.4 + len(analyzed) * 0.07)

        factors = [
            FactorItem(name="条件选股数", value=str(prescreen["screener_count"]), score=50),
            FactorItem(name="尾盘封板", value=str(prescreen["zt_eod_count"]),
                       score=min(100, prescreen["zt_eod_count"] * 25 + 30)),
            FactorItem(name="主线题材", value=", ".join(t for t, _ in main_themes[:3]) or "无",
                       score=70 if main_themes else 40),
            FactorItem(name="候选标的", value=str(len(analyzed)), score=min(100, len(analyzed) * 15 + 20)),
            FactorItem(name="看多比例", value=f"{bullish}/{len(analyzed)}", score=int(avg_score)),
        ]

        lines = [
            f"## 尾盘选股结果（隔夜持仓）",
            f"条件选股: {prescreen['screener_count']}只 | 尾盘封板: {prescreen['zt_eod_count']}只 | 深入分析: {len(analyzed)}只",
            f"主线题材: {', '.join(t for t, _ in main_themes[:5]) or '无明确主线'}",
            "",
        ]

        eod_zt = [c for c in candidates if c.get("source") == "尾盘封板"]
        if eod_zt:
            lines.append("### 尾盘封板（最强信号）")
            for s in eod_zt[:3]:
                lines.append(
                    f"- **{s['code']}** {s['name']} | "
                    f"封板时间{s.get('zt_time', '')} | {s.get('reason', '')}"
                )
            lines.append("")

        if analyzed:
            lines.append("### 隔夜候选标的")
            for a in analyzed:
                risk = " ⚠️" + "、".join(a.get("risk_notes", [])) if a.get("risk_notes") else ""
                lines.append(
                    f"- **{a['code']}** {a.get('name', '')} | "
                    f"评分{a['score']:.0f} | {a['direction']} | "
                    f"涨幅{a.get('eod_data', {}).get('change_pct', 0):.1f}% | "
                    f"收盘距高{a.get('eod_data', {}).get('close_to_high', 0):.1f}% | "
                    f"{a['signal']}{risk}"
                )

        return SkillReport(
            skill_name=self.name, score=round(avg_score, 1),
            direction=direction, confidence=confidence,
            signal=f"隔夜{bullish}只看多，主线:{', '.join(t for t, _ in main_themes[:2]) or '无'}",
            factors=factors, analysis="\n".join(lines),
            output_data={
                "main_themes": main_themes,
                "candidates": [c for c in candidates[:15]],
                "analyzed": analyzed,
            },
            tools_called=_tool_calls or [],
            missing_data=_missing_data or [],
            status="ok",
        )

    # ── 盘后策略 ──

    def _run_post_market(self, date, call_tool_fn, _tool_calls, _tool_nodes, _missing_data):
        try:
            prescreen = _prescreen_post_market(date)
        except Exception as e:
            logger.warning("[MktScreen] 盘后形态扫描失败: %s", e)
            return None

        candidates = prescreen["candidates"]
        main_themes = prescreen["main_themes"]

        logger.info("[MktScreen] 盘后扫描: 池%d只, 扫描%d只, 候选%d只",
                     prescreen["pool_size"], prescreen["scanned"], len(candidates))

        if not candidates:
            return SkillReport(
                skill_name=self.name, score=40.0, direction="neutral",
                confidence=0.5, signal="今日无符合形态的短线标的",
                analysis=(
                    f"## 盘后短线选股 — 无合适标的\n\n"
                    f"扫描 {prescreen['scanned']} 只股票，"
                    f"未发现符合技术形态条件的标的。\n\n"
                    f"**建议：观望等待更好的形态出现。**"
                ),
                factors=[
                    FactorItem(name="扫描数", value=str(prescreen["scanned"]), score=50),
                    FactorItem(name="主线题材", value=", ".join(t for t, _ in main_themes[:3]) or "无", score=50),
                ],
                status="ok",
            )

        analyzed = []
        for c in candidates[:6]:
            result = _deep_analyze_post_market(c, call_tool_fn, _tool_calls, _tool_nodes, _missing_data)
            if result:
                analyzed.append(result)

        if analyzed:
            avg_score = sum(a["score"] for a in analyzed) / len(analyzed)
            bullish = sum(1 for a in analyzed if a["direction"] == "bullish")
        else:
            avg_score = 50.0
            bullish = 0

        direction = "bullish" if avg_score >= 55 else ("bearish" if avg_score < 45 else "neutral")
        confidence = min(0.85, 0.4 + len(analyzed) * 0.07)

        factors = [
            FactorItem(name="扫描池", value=str(prescreen["pool_size"]), score=50),
            FactorItem(name="形态命中", value=str(len(candidates)), score=min(100, len(candidates) * 12 + 20)),
            FactorItem(name="主线题材", value=", ".join(t for t, _ in main_themes[:3]) or "无",
                       score=70 if main_themes else 40),
            FactorItem(name="深入分析", value=str(len(analyzed)), score=min(100, len(analyzed) * 15 + 20)),
            FactorItem(name="看多比例", value=f"{bullish}/{len(analyzed)}", score=int(avg_score)),
        ]

        lines = [
            f"## 盘后短线选股结果",
            f"扫描池: {prescreen['pool_size']}只 | 形态命中: {len(candidates)}只 | 深入分析: {len(analyzed)}只",
            f"主线题材: {', '.join(t for t, _ in main_themes[:5]) or '无明确主线'}",
            "",
        ]

        pattern_counter: Counter = Counter()
        for c in candidates:
            for p in c.get("pattern_names", []):
                pattern_counter[p] += 1
        if pattern_counter:
            lines.append("### 形态分布")
            for p, cnt in pattern_counter.most_common(6):
                lines.append(f"- {p}: {cnt}只")
            lines.append("")

        if analyzed:
            lines.append("### 次日候选标的")
            for a in analyzed:
                risk = " ⚠️" + "、".join(a.get("risk_notes", [])) if a.get("risk_notes") else ""
                entry = a.get("entry", {})
                lines.append(
                    f"- **{a['code']}** {a.get('name', '')} | "
                    f"评分{a['score']:.0f} | {a['direction']} | "
                    f"形态:{','.join(a.get('patterns', []))} | "
                    f"{a['signal']}{risk}"
                )
                lines.append(
                    f"  入场:{entry.get('price_low', '?')}-{entry.get('price_high', '?')} | "
                    f"止损:{entry.get('stop_loss', '?')} | "
                    f"目标:{entry.get('target_1', '?')}/{entry.get('target_2', '?')} | "
                    f"盈亏比:{entry.get('risk_reward', '?')}"
                )

        return SkillReport(
            skill_name=self.name, score=round(avg_score, 1),
            direction=direction, confidence=confidence,
            signal=f"盘后{len(analyzed)}只候选，主线:{', '.join(t for t, _ in main_themes[:2]) or '无'}",
            factors=factors, analysis="\n".join(lines),
            output_data={
                "main_themes": main_themes,
                "pattern_distribution": dict(pattern_counter),
                "candidates": [c for c in candidates[:15]],
                "analyzed": analyzed,
            },
            tools_called=_tool_calls or [],
            missing_data=_missing_data or [],
            status="ok",
        )


# -*- coding: utf-8 -*-
"""全市场短线选股 — 自动选择盘中/盘后/收盘策略进行市场筛选。"""

def market_screen(stock_code: str = "", stock_name: str = "") -> dict:
    """薄壳入口，返回 dict。"""
    from app.agent.tools import registry as tool_registry
    tool_registry.discover()

    def call_tool_fn(tool_name, **kwargs):
        spec = tool_registry.get(tool_name)
        if not spec: raise ValueError(f"Unknown tool: {tool_name}")
        return spec.fn(**kwargs)

    from datetime import date

    strategy = _select_strategy()
    today = date.today().isoformat()

    if strategy == "intraday":
        report = _run_intraday("market_screener", today, call_tool_fn, [], [], [])
    elif strategy == "eod":
        report = _run_eod("market_screener", call_tool_fn, [], [], [])
    else:
        report = _run_post_market("market_screener", today, call_tool_fn, [], [], [])

    if report is None:
        return {"skill": "market_screener", "status": "failed", "error": "策略执行失败", "score": 0, "direction": "neutral", "confidence": 0, "factors": []}
    if hasattr(report, "to_dict"):
        d = report.to_dict()
        d.setdefault("skill", "market_screener")
        return d
    if isinstance(report, dict):
        report.setdefault("skill", "market_screener")
        return report
    return {"skill": "market_screener", "score": getattr(report, "score", 50),
            "direction": getattr(report, "direction", "neutral"),
            "signal": getattr(report, "signal", ""),
            "analysis": str(getattr(report, "analysis", ""))[:2000],
            "status": getattr(report, "status", "ok"), "confidence": 0.5, "factors": []}


# ── 内联自 screener_tools.py ──

def search_stocks(
    query: str = "",
    source: str = "auto",
    filters: Optional[Dict[str, Any]] = None,
    market: str = "全部",
    top_n: int = 50,
) -> Dict[str, Any]:
    """统一选股工具：根据条件从全市场筛选股票。

    支持自然语言条件（如 "PE<20 半导体"）和结构化 filters 字典。
    source 参数控制数据源：auto(东财优先,本地DB兜底) / eastmoney / local_db。

    Args:
        query: 自然语言选股条件（如 "半导体 净利增长>15%"、"PE在5到20之间"）
        source: 数据源 — auto(自动选择) / eastmoney(东财智能选股) / local_db(本地数据库)
        filters: 结构化筛选条件字典（可选，与 query 互补）
        market: 市场筛选（全部/A股/科创板/创业板/港股/美股/ETF基金）
        top_n: 返回数量上限，默认50，最大200

    Returns:
        dict: {"stocks": [{"code": "600519", "name": "贵州茅台", "industry": "白酒", ...}, ...], "count": N}
        取第一个结果: result["stocks"][0]["code"]
    """
    top_n = min(max(top_n, 1), 200)

    # 如果有 filters 但没 query，从 filters 生成 keyword
    if filters and not query:
        query = build_keyword_from_filters(filters)
        if market == "全部" and filters.get("_market"):
            market = filters["_market"]

    if not query or not query.strip():
        return {"error": "选股条件不能为空（传入 query 或 filters）", "retriable": False}

    search_keyword = query.strip()
    if market and market != "全部" and market in MARKET_FILTER_MAP:
        search_keyword = f"{market} {search_keyword}"

    # ── eastmoney / auto 模式 ──
    if source in ("eastmoney", "auto"):
        raw = _call_eastmoney_api(search_keyword, page_size=top_n)
        if str(raw.get("code")) == "100":
            data = raw.get("data", {})
            result = data.get("result", {})
            stocks_raw = result.get("dataList", [])
            total = result.get("total", len(stocks_raw))
            stocks = [_parse_stock_item(s) for s in stocks_raw]
            return {
                "source": "eastmoney",
                "keyword": query,
                "market": market,
                "total": total,
                "count": len(stocks),
                "stocks": stocks,
            }
        elif source == "eastmoney":
            return {"error": raw.get("msg", "东财选股搜索失败"), "retriable": True}
        # auto 模式下东财失败，继续 fallback

    # ── local_db 模式 / auto fallback ──
    if source in ("local_db", "auto"):
        return _search_local_db(query, market, top_n)

    return {"error": f"未知数据源: {source}", "retriable": False}

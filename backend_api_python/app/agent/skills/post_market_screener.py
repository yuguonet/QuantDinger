# -*- coding: utf-8 -*-
"""
Post-Market Screener Skill — 盘后短线选股专家（次日介入点筛选）。

盘后复盘，用全天收盘K线做技术形态筛选，找 1-3 日短线机会。

与现有 Skill 的区别：
  - short_term_screener: 盘中/盘后追板、龙回头、主线题材（动量导向）
  - eod_screener: 14:30 尾盘隔夜持仓（收盘位置+封板）
  - 本 Skill: 盘后复盘，技术形态筛选，找次日介入点（形态导向）

适用场景：收盘后"明天买什么"、"盘后复盘选股"、"短线技术选股"。
"""
from __future__ import annotations

import logging
import os
import sys
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from app.agent.chain.schema import FactorItem, SkillReport
from app.agent.skills.registry import skill

logger = logging.getLogger(__name__)

_backend_root = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")
)
if _backend_root not in sys.path:
    sys.path.insert(0, _backend_root)

_writer_cache = None


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


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


# ═══════════════════════════════════════════════════════════════
# 数据采集
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


def _fetch_hot_stocks(date: str) -> Dict:
    """获取强势股及题材归因。"""
    import requests as _req
    url = f"http://zx.10jqka.com.cn/event/api/getharden/date/{date}/orderby/date/orderway/desc/charset/GBK/"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/117.0.0.0"}
    try:
        r = _req.get(url, headers=headers, timeout=10)
        data = r.json()
        if data.get("errocode", 0) != 0:
            return {"stocks": [], "hot_tags": []}
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
                for tag in s["reason"].replace("，", "+").replace(",", "+").split("+"):
                    tag = tag.strip()
                    if tag:
                        tag_counter[tag] += 1
        return {"stocks": stocks, "hot_tags": tag_counter.most_common(15)}
    except Exception:
        return {"stocks": [], "hot_tags": []}


# ═══════════════════════════════════════════════════════════════
# 技术指标计算
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
    """计算 MACD (DIF, DEA, MACD柱)。"""
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


def _compute_kdj(bars: List[Dict], period: int = 9) -> Dict[str, List[float]]:
    """计算 KDJ 指标。"""
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
    """计算 ATR（平均真实波幅）。"""
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
# 形态识别
# ═══════════════════════════════════════════════════════════════

def _detect_platform_breakout(bars: List[Dict]) -> Optional[Dict]:
    """平台突破：近5日振幅<8%，今日收盘突破平台高点。"""
    if len(bars) < 10:
        return None
    recent = bars[-6:-1]  # 前5日
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

    # 今日收盘突破平台高点
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

    # 检查前5日是否缩量下跌或横盘
    recent = bars[-6:-1]
    today = bars[-1]

    vol_trend = [b["volume"] for b in recent]
    close_trend = [b["close"] for b in recent]

    # 缩量判断：最近成交量低于5日均量
    avg_vol = sum(vol_trend) / len(vol_trend) if vol_trend else 1
    is_shrinking = all(v <= avg_vol * 1.1 for v in vol_trend[-3:])

    # 今日放量阳线
    is_surge = today["volume"] > avg_vol * 1.8 and today["close"] > today["open"]

    # 前5日整体走平或微跌
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

    # 上升趋势：MA5 > MA10 > MA20
    if not (ma5[-1] > ma10[-1] > ma20[-1]):
        return None

    today = bars[-1]
    yesterday = bars[-2]

    # 今日最低触及MA10附近（±1%），收盘站稳
    ma10_dist = abs(today["low"] - ma10[-1]) / ma10[-1] * 100
    if ma10_dist < 1.5 and today["close"] > ma10[-1]:
        # 昨日或前日有下影线触MA10
        touch_ma10 = False
        for b in bars[-3:-1]:
            if abs(b["low"] - ma10[-2 if ma10[-2] else -1]) / max(ma10[-2] if ma10[-2] else 1, 0.01) < 1.5:
                touch_ma10 = True
                break

        return {
            "pattern": "均线支撑回踩",
            "ma10": round(ma10[-1], 2),
            "low": round(today["low"], 2),
            "distance_pct": round(ma10_dist, 2),
            "score": 68 if today["close"] > today["open"] else 58,
        }
    return None


def _detect_macd_golden_cross(bars: List[Dict]) -> Optional[Dict]:
    """MACD金叉：DIF上穿DEA，且价格在均线附近。"""
    if len(bars) < 30:
        return None

    closes = [b["close"] for b in bars]
    macd = _compute_macd(closes)
    dif = macd["dif"]
    dea = macd["dea"]

    # 今日金叉：昨日 DIF < DEA，今日 DIF >= DEA
    if dif[-1] >= dea[-1] and dif[-2] < dea[-2]:
        # 水下金叉更佳（底部信号更强）
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
    """缩量回调后放量突破：上涨→缩量回调→今日放量突破回调高点。"""
    if len(bars) < 15:
        return None

    # 找近10日的高点和回调低点
    recent_10 = bars[-11:-1]
    today = bars[-1]

    highs_10 = [b["high"] for b in recent_10]
    peak_idx = highs_10.index(max(highs_10))
    peak_price = max(highs_10)

    # 高点后必须有回调（至少跌3%）
    if peak_idx >= len(recent_10) - 2:
        return None  # 高点太近，还没回调

    pullback_bars = recent_10[peak_idx + 1:]
    pullback_low = min(b["low"] for b in pullback_bars)
    pullback_pct = (peak_price - pullback_low) / peak_price * 100
    if pullback_pct < 3 or pullback_pct > 12:
        return None

    # 回调期间缩量
    pullback_vols = [b["volume"] for b in pullback_bars]
    peak_vols = [b["volume"] for b in recent_10[max(0, peak_idx - 2):peak_idx + 1]]
    avg_peak_vol = sum(peak_vols) / len(peak_vols) if peak_vols else 1
    avg_pullback_vol = sum(pullback_vols) / len(pullback_vols) if pullback_vols else 1
    is_shrink = avg_pullback_vol < avg_peak_vol * 0.7

    # 今日放量突破回调高点
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


# ═══════════════════════════════════════════════════════════════
# Phase 1: 全市场形态扫描
# ═══════════════════════════════════════════════════════════════

def _prescreen_post_market(date: str) -> Dict[str, Any]:
    """盘后全市场形态扫描。"""

    # ── 1. 获取强势股作为扫描池 ──
    hot_data = _fetch_hot_stocks(date)
    hot_stocks = hot_data.get("stocks", [])
    hot_tags = hot_data.get("hot_tags", [])

    # 主线题材
    main_themes = [(tag, cnt) for tag, cnt in hot_tags[:5]]

    # ── 2. 用 search_stocks 做条件初筛 ──
    # 条件：涨幅 1-8% + 换手率 > 2% + 非 ST（比尾盘宽一些）
    try:
        from app.agent.tools.screening_tools import search_stocks
        screener_result = search_stocks(
            query="涨幅1%到8% 换手率大于2% 非ST",
            source="eastmoney",
            top_n=100,
        )
        raw_stocks = screener_result.get("stocks", []) if isinstance(screener_result, dict) else []
    except Exception:
        raw_stocks = []

    # 合并强势股 + 条件选股
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

    # ── 3. 逐只扫描形态 ──
    candidates = []
    scanned = 0

    for code, info in scan_pool.items():
        bars = _fetch_kline(code, days=40)
        if len(bars) < 15:
            continue

        scanned += 1
        today = bars[-1]
        closes = [b["close"] for b in bars]

        # 跳过涨停/跌停（无法买入或风险太大）
        if len(bars) >= 2:
            prev_close = bars[-2]["close"]
            if prev_close > 0:
                change = (today["close"] - prev_close) / prev_close * 100
                if change > 9.8 or change < -9.8:
                    continue

        # 形态检测（可叠加多个）
        patterns = []
        total_score = 0

        for detector in [
            _detect_platform_breakout,
            _detect_volume_reversal,
            _detect_ma_support_pullback,
            _detect_macd_golden_cross,
            _detect_shrink_pullback_breakout,
            _detect_prev_high_breakout,
        ]:
            result = detector(bars)
            if result:
                patterns.append(result)
                total_score += result["score"]

        if not patterns:
            continue

        # 技术指标辅助评分
        rsi = _compute_rsi(closes)
        macd = _compute_macd(closes)
        kdj = _compute_kdj(bars)

        # RSI 加减分
        rsi_val = rsi[-1]
        if rsi_val > 80:
            total_score -= 10
        elif rsi_val > 70:
            total_score -= 3
        elif 40 < rsi_val < 60:
            total_score += 3

        # KDJ 金叉加分
        if kdj["k"][-1] > kdj["d"][-1] and kdj["k"][-2] <= kdj["d"][-2]:
            total_score += 5

        # 均线排列
        ma5 = _compute_ma(closes, 5)
        ma10 = _compute_ma(closes, 10)
        ma20 = _compute_ma(closes, 20)
        if ma5[-1] and ma10[-1] and ma20[-1]:
            if ma5[-1] > ma10[-1] > ma20[-1]:
                total_score += 5  # 多头排列

        # 量能
        vol_5 = sum(b["volume"] for b in bars[-6:-1]) / 5 if len(bars) > 5 else 1
        vol_ratio = today["volume"] / vol_5 if vol_5 > 0 else 1

        total_score = max(0, min(100, total_score))

        # 构建信号摘要
        signals = [p["pattern"] for p in patterns]
        if rsi_val > 70:
            signals.append(f"RSI{rsi_val:.0f}偏高")
        if vol_ratio > 1.5:
            signals.append(f"放量{vol_ratio:.1f}倍")

        candidates.append({
            "code": code,
            "name": info.get("name", ""),
            "change_pct": info.get("change_pct", 0),
            "close": round(today["close"], 2),
            "patterns": patterns,
            "pattern_names": [p["pattern"] for p in patterns],
            "score": total_score,
            "rsi": round(rsi_val, 1),
            "macd_dif": round(macd["dif"][-1], 3),
            "kdj_k": round(kdj["k"][-1], 1),
            "vol_ratio": round(vol_ratio, 2),
            "ma5": round(ma5[-1], 2) if ma5[-1] else None,
            "ma10": round(ma10[-1], 2) if ma10[-1] else None,
            "reason": info.get("reason", ""),
            "signals": signals,
        })

    # 排序
    candidates.sort(key=lambda x: -x["score"])

    return {
        "date": date,
        "scanned": scanned,
        "pool_size": len(scan_pool),
        "main_themes": main_themes,
        "candidates": candidates[:20],
    }


# ═══════════════════════════════════════════════════════════════
# Phase 2: 深入分析
# ═══════════════════════════════════════════════════════════════

def _deep_analyze_post_market(
    candidate: Dict, call_tool_fn, _tool_calls, _tool_nodes, _missing_data,
) -> Optional[Dict]:
    """对候选股做深入分析，评估次日介入价值。"""
    code = candidate["code"]
    try:
        # 调用工具获取额外数据
        snapshot = call_tool_fn("get_indicator_snapshot", stock_code=code)
        fund_flow = call_tool_fn("get_fund_flow_realtime", stock_code=code)

        if _tool_calls is not None:
            for t in ["get_indicator_snapshot", "get_fund_flow_realtime"]:
                if t not in _tool_calls:
                    _tool_calls.append(t)

        score = candidate.get("score", 60)
        signals = list(candidate.get("signals", []))
        factors = []

        # ── 指标快照 ──
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

        # ── 资金流 ──
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

        # ── 介入点计算 ──
        close = candidate.get("close", 0)
        ma10 = candidate.get("ma10")
        atr = candidate.get("atr", close * 0.02)  # 默认 2%

        # 入场价：明日开盘价附近（±0.5%）
        entry_low = round(close * 0.995, 2)
        entry_high = round(close * 1.005, 2)

        # 止损位：MA10 或今日低点下方 1%
        stop_loss = round(min(ma10 or close * 0.97, close * 0.97), 2)

        # 目标位：根据形态强度
        risk = close - stop_loss
        if risk <= 0:
            risk = close * 0.03
        target_1 = round(close + risk * 1.5, 2)  # 1.5:1 盈亏比
        target_2 = round(close + risk * 2.5, 2)  # 2.5:1 盈亏比

        # ── 风险评估 ──
        risk_notes = []
        rsi = candidate.get("rsi", 50)
        if rsi > 75:
            risk_notes.append(f"RSI{rsi:.0f}偏高，短线回调风险")
            score -= 3
        if candidate.get("change_pct", 0) > 6:
            risk_notes.append("涨幅偏大，追涨需谨慎")
        if candidate.get("vol_ratio", 1) > 4:
            risk_notes.append("量能异常放大，注意是否有出货嫌疑")

        score = max(0, min(100, score))
        direction = "bullish" if score >= 60 else ("bearish" if score <= 40 else "neutral")

        return {
            "code": code,
            "name": candidate.get("name", ""),
            "patterns": candidate.get("pattern_names", []),
            "score": round(score, 1),
            "direction": direction,
            "signal": ", ".join(signals[:5]),
            "factors": [f.to_dict() for f in factors],
            "risk_notes": risk_notes,
            "entry": {
                "price_low": entry_low,
                "price_high": entry_high,
                "stop_loss": stop_loss,
                "target_1": target_1,
                "target_2": target_2,
                "risk_reward": "1:1.5 / 1:2.5",
            },
            "tech": {
                "close": close,
                "rsi": candidate.get("rsi"),
                "vol_ratio": candidate.get("vol_ratio"),
                "ma5": candidate.get("ma5"),
                "ma10": candidate.get("ma10"),
            },
        }

    except Exception as e:
        logger.warning("[PostMarket] 深入分析 %s 失败: %s", code, e)
        return None


# ═══════════════════════════════════════════════════════════════
# Skill 定义
# ═══════════════════════════════════════════════════════════════

@skill("post_market_screener", auto_load=True)
class PostMarketScreenerSkill:
    """盘后短线选股专家。"""

    def algo_analyze(
        self,
        stock_code: str,
        stock_name: str,
        tool_results: Dict[str, Any],
        **kwargs,
    ) -> Optional[SkillReport]:
        """Phase 1: 形态扫描 + Phase 2: 深入分析。"""
        call_tool_fn = kwargs.get("call_tool_fn")
        _tool_calls = kwargs.get("_tool_calls", [])
        _tool_nodes = kwargs.get("_tool_nodes", [])
        _missing_data = kwargs.get("_missing_data", [])

        # ── Phase 1: 全市场形态扫描 ──
        try:
            prescreen = _prescreen_post_market(_today_str())
        except Exception as e:
            logger.warning("[PostMarket] 形态扫描失败: %s", e)
            return None

        candidates = prescreen["candidates"]
        main_themes = prescreen["main_themes"]

        logger.info("[PostMarket] 扫描完成: 池%d只, 扫描%d只, 候选%d只",
                     prescreen["pool_size"], prescreen["scanned"], len(candidates))

        if not candidates:
            return SkillReport(
                skill_name=self.name,
                score=40.0,
                direction="neutral",
                confidence=0.5,
                signal="今日无符合形态的短线标的",
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

        # ── Phase 2: 深入分析（最多 6 只）──
        analyzed = []
        for c in candidates[:6]:
            result = _deep_analyze_post_market(
                c, call_tool_fn,
                _tool_calls, _tool_nodes, _missing_data,
            )
            if result:
                analyzed.append(result)

        # ── 综合评分 ──
        if analyzed:
            avg_score = sum(a["score"] for a in analyzed) / len(analyzed)
            bullish = sum(1 for a in analyzed if a["direction"] == "bullish")
        else:
            avg_score = 50.0
            bullish = 0

        direction = "bullish" if avg_score >= 55 else ("bearish" if avg_score < 45 else "neutral")
        confidence = min(0.85, 0.4 + len(analyzed) * 0.07)

        # ── 因子 ──
        factors = [
            FactorItem(name="扫描池", value=str(prescreen["pool_size"]), score=50),
            FactorItem(name="形态命中", value=str(len(candidates)), score=min(100, len(candidates) * 12 + 20)),
            FactorItem(name="主线题材", value=", ".join(t for t, _ in main_themes[:3]) or "无",
                       score=70 if main_themes else 40),
            FactorItem(name="深入分析", value=str(len(analyzed)), score=min(100, len(analyzed) * 15 + 20)),
            FactorItem(name="看多比例", value=f"{bullish}/{len(analyzed)}", score=int(avg_score)),
        ]

        # ── 分析文字 ──
        lines = [
            f"## 盘后短线选股结果",
            f"扫描池: {prescreen['pool_size']}只 | 形态命中: {len(candidates)}只 | 深入分析: {len(analyzed)}只",
            f"主线题材: {', '.join(t for t, _ in main_themes[:5]) or '无明确主线'}",
            "",
        ]

        # 形态分布
        pattern_counter: Counter = Counter()
        for c in candidates:
            for p in c.get("pattern_names", []):
                pattern_counter[p] += 1
        if pattern_counter:
            lines.append("### 形态分布")
            for p, cnt in pattern_counter.most_common(6):
                lines.append(f"- {p}: {cnt}只")
            lines.append("")

        # 深入分析结果
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
            skill_name=self.name,
            score=round(avg_score, 1),
            direction=direction,
            confidence=confidence,
            signal=f"盘后{len(analyzed)}只候选，主线:{', '.join(t for t, _ in main_themes[:2]) or '无'}",
            factors=factors,
            analysis="\n".join(lines),
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

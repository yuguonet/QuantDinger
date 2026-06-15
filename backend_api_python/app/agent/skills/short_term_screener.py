# -*- coding: utf-8 -*-
"""
Short Term Screener Skill — 盘中短线选股专家（A股1-3日交易特化）。

两阶段流程：
  Phase 1: Python 预筛选（0 token）
    → 涨停池连板股 + 热门板块龙头 + 强势股题材归因 + 资金流交叉验证
  Phase 2: 对候选股逐只调用工具做深入分析
    → 技术面验证 + 资金流确认 + 综合评分

与 screening_agent 的区别：screening_agent 是通用选股（含中线估值），本 Skill 只做纯短线。
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

# ═══════════════════════════════════════════════════════════════
# 数据加载
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
# Phase 1 数据采集（直接调底层数据源，0 token）
# ═══════════════════════════════════════════════════════════════

def _fetch_zt_pool(date: str) -> List[Dict]:
    """涨停池。"""
    try:
        from app.market_cn.dragon_limit import get_zt_pool
        return get_zt_pool(date)
    except Exception as e:
        logger.warning("[STScreen] 涨停池获取失败: %s", e)
        return []


def _fetch_dt_pool(date: str) -> List[Dict]:
    """跌停池。"""
    try:
        from app.market_cn.dragon_limit import get_dt_pool
        return get_dt_pool(date)
    except Exception as e:
        logger.warning("[STScreen] 跌停池获取失败: %s", e)
        return []


def _fetch_broken_board(date: str) -> List[Dict]:
    """炸板池。"""
    try:
        from app.market_cn.dragon_limit import get_broken_board
        return get_broken_board(date)
    except Exception as e:
        logger.warning("[STScreen] 炸板池获取失败: %s", e)
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
                tags = [t.strip() for t in s["reason"].split("+") if t.strip()]
                tag_counter.update(tags)
        return {"stocks": stocks, "hot_tags": tag_counter.most_common(20)}
    except Exception as e:
        logger.warning("[STScreen] 强势股获取失败: %s", e)
        return {"error": str(e)}


def _fetch_hot_sectors() -> Dict:
    """热门板块。"""
    try:
        from app.market_cn.china_market import get_hot_sectors
        return get_hot_sectors(industry_limit=15, concept_limit=15)
    except Exception as e:
        logger.warning("[STScreen] 热门板块获取失败: %s", e)
        return {"error": str(e)}


def _fetch_kline(code: str, days: int = 60) -> List[Dict]:
    """从 db_market 获取日K线。"""
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


# ═══════════════════════════════════════════════════════════════
# 纯 Python 技术指标计算
# ═══════════════════════════════════════════════════════════════

def _compute_ma(closes: List[float], period: int) -> List[Optional[float]]:
    n = len(closes)
    ma = [None] * n
    for i in range(period - 1, n):
        ma[i] = sum(closes[i - period + 1:i + 1]) / period
    return ma


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


# ═══════════════════════════════════════════════════════════════
# 龙回头弱转强检测（纯 Python）
# ═══════════════════════════════════════════════════════════════

def _fetch_recent_zt_pools(days: int = 8) -> Dict[str, List[Dict]]:
    """获取近N天涨停池，按日期返回。"""
    from datetime import timedelta
    pools = {}
    today = datetime.now()
    for i in range(days):
        d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        pool = _fetch_zt_pool(d)
        if pool:
            pools[d] = pool
    return pools


def _scan_dragon_pullback(date: str) -> List[Dict]:
    """扫描龙回头弱转强模式。

    逻辑：
    1. 从近8天涨停池中提取所有曾涨停的股票（前期龙头）
    2. 用K线检查是否经历了回调（缩量下跌/横盘整理）
    3. 今日是否出现弱转强信号（放量突破/均线金叉/止跌回升）

    返回候选列表，每只包含龙头身份、回调特征、今日信号。
    """
    # ── 1. 收集近8天涨停股，按 code 分组 ──
    recent_pools = _fetch_recent_zt_pools(8)
    code_history: Dict[str, List[Dict]] = {}  # code → [{date, days, reason}, ...]

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

    # 只保留有过连板（≥2）或单日涨停但封板资金大的个股
    dragon_codes = {}
    for code, records in code_history.items():
        max_days = max(r["continuous_days"] for r in records)
        if max_days >= 2:
            dragon_codes[code] = {
                "name": records[0]["name"],
                "max_continuous_days": max_days,
                "zt_dates": [r["date"] for r in records],
                "last_zt_date": min(records[0]["date"], date),  # 最近一次涨停
                "reason": records[0]["reason"],
            }

    if not dragon_codes:
        return []

    # ── 2. 逐只检查回调+弱转强 ──
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
        i = n - 1  # 最新一天

        # ── 找到最高点（连板结束后的最高价）──
        # 从最后一天往前找，找最近5-15天内的最高点
        lookback_start = max(0, n - 15)
        peak_idx = lookback_start
        for j in range(lookback_start, n):
            if highs[j] > highs[peak_idx]:
                peak_idx = j

        # 最高点必须在昨天之前（今天不是最高点 = 已经回调过）
        if peak_idx >= i:
            continue  # 今天创新高，不算龙回头

        peak_price = highs[peak_idx]
        current_close = closes[i]

        # 回调幅度：从最高点到最低点
        trough_price = min(lows[peak_idx + 1:i + 1]) if peak_idx + 1 <= i else current_close
        pullback_pct = (peak_price - trough_price) / peak_price * 100

        # 回调幅度合理性：10%-30%（太少没洗干净，太多说明逻辑破了）
        if pullback_pct < 8 or pullback_pct > 35:
            continue

        # ── 弱转强信号检测 ──
        signals = []
        strength_score = 0

        # 信号1: 今日放量（量比 vs 回调期间平均量）
        pullback_volumes = volumes[peak_idx + 1:i]  # 回调期间的量
        avg_pullback_vol = sum(pullback_volumes) / len(pullback_volumes) if pullback_volumes else 1
        vol_ratio_today = volumes[i] / avg_pullback_vol if avg_pullback_vol > 0 else 1
        if vol_ratio_today > 1.5:
            signals.append(f"放量{vol_ratio_today:.1f}倍")
            strength_score += 15
        elif vol_ratio_today > 1.2:
            signals.append(f"温和放量{vol_ratio_today:.1f}倍")
            strength_score += 8

        # 信号2: 今日收阳线
        if closes[i] > bars[i]["open"]:
            signals.append("收阳")
            strength_score += 5

        # 信号3: 今日收盘站上MA5
        ma5 = _compute_ma(closes, 5)
        if ma5[i] is not None and closes[i] > ma5[i]:
            signals.append("站上MA5")
            strength_score += 8

        # 信号4: MA5拐头向上（MA5斜率由负转正）
        if ma5[i] is not None and ma5[i - 1] is not None:
            ma5_slope_today = (ma5[i] - ma5[i - 1]) / ma5[i - 1] * 100 if ma5[i - 1] > 0 else 0
            if ma5_slope_today > 0:
                signals.append("MA5拐头")
                strength_score += 5

        # 信号5: RSI 从低位回升
        rsi = _compute_rsi(closes)
        if rsi[i] > 40 and rsi[i - 1] < 40:
            signals.append(f"RSI低位回升{rsi[i]:.0f}")
            strength_score += 10
        elif 40 <= rsi[i] <= 60:
            signals.append(f"RSI{rsi[i]:.0f}中性")
            strength_score += 3

        # 信号6: 回调缩量（回调期间量逐日递减 = 卖盘衰竭）
        if len(pullback_volumes) >= 2:
            vol_declining = all(
                pullback_volumes[j] <= pullback_volumes[j - 1] * 1.1
                for j in range(1, len(pullback_volumes))
            )
            if vol_declining:
                signals.append("回调缩量(卖盘衰竭)")
                strength_score += 10

        # 信号7: 下影线支撑（今日最低点触及均线后拉回）
        if ma5[i] is not None and lows[i] <= ma5[i] * 1.01 and closes[i] > ma5[i]:
            signals.append("均线支撑")
            strength_score += 8

        # 需要至少2个弱转强信号才入选
        if len(signals) < 2:
            continue

        # ── 构建候选 ──
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

    # 按弱转强强度排序
    candidates.sort(key=lambda x: -x["strength_score"])
    return candidates


# ═══════════════════════════════════════════════════════════════
# Phase 1 核心：Python 预筛选（0 token）
# ═══════════════════════════════════════════════════════════════

def _prescreen(date: str) -> Dict[str, Any]:
    """纯 Python 全市场预筛选，返回候选股列表和市场概况。"""

    # ── 1. 并行采集四维数据 ──
    zt_pool = _fetch_zt_pool(date)
    dt_pool = _fetch_dt_pool(date)
    broken_pool = _fetch_broken_board(date)
    hot_data = _fetch_hot_stocks_with_reason(date)
    sector_data = _fetch_hot_sectors()

    hot_stocks = hot_data.get("stocks", [])
    hot_tags = hot_data.get("hot_tags", [])
    industry_sectors = sector_data.get("industry", [])
    concept_sectors = sector_data.get("concept", [])

    # ── 2. 市场情绪评估 ──
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

    # ── 3. 识别主线题材（从涨停池 + 强势股 reason tags 交叉）──
    # 涨停原因统计
    zt_reason_counter: Counter = Counter()
    for s in zt_pool:
        reason = s.get("reason", "") or ""
        if reason:
            for tag in reason.replace("，", "+").replace(",", "+").split("+"):
                tag = tag.strip()
                if tag:
                    zt_reason_counter[tag] += 1

    # 合并同花顺 tags 和涨停原因
    combined_tags: Counter = Counter()
    for tag, cnt in hot_tags:
        combined_tags[tag] += cnt
    for tag, cnt in zt_reason_counter.items():
        combined_tags[tag] += cnt

    main_themes = combined_tags.most_common(10)

    # ── 4. 从涨停池中筛选连板股 ──
    continuous_board = []
    for s in zt_pool:
        days = int(s.get("continuous_zt_days", 1) or 1)
        if days >= 2:  # 至少2连板
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

    # ── 5. 从强势股中筛选主线题材标的 ──
    # 提取主线题材关键词
    main_theme_keywords = set()
    for tag, _ in main_themes[:5]:
        main_theme_keywords.add(tag)

    theme_stocks = []
    for s in hot_stocks:
        reason = s.get("reason", "") or ""
        if not reason:
            continue
        # 检查是否属于主线题材
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

    # ── 6. 龙回头弱转强扫描 ──
    dragon_pullback = _scan_dragon_pullback(date)

    # ── 7. 综合候选池（去重 + 合并来源标记）──
    candidates = {}

    # 连板股（最高优先级）
    for s in continuous_board:
        code = s["code"]
        candidates[code] = {
            "code": code,
            "name": s["name"],
            "source": "连板",
            "continuous_days": s["continuous_days"],
            "zt_time": s["zt_time"],
            "reason": s["reason"],
            "change_pct": 0,
            "tags": [],
            "pullback_signals": [],
        }

    # 主线题材强势股
    for s in theme_stocks[:20]:
        code = s["code"]
        if code in candidates:
            candidates[code]["source"] += "+主线题材"
            candidates[code]["tags"] = s["matched_tags"]
        else:
            candidates[code] = {
                "code": code,
                "name": s["name"],
                "source": "主线题材",
                "continuous_days": 0,
                "zt_time": "",
                "reason": s["reason"],
                "change_pct": s["change_pct"],
                "tags": s["matched_tags"],
                "pullback_signals": [],
            }

    # 龙回头弱转强（独立来源，不与上面合并）
    for s in dragon_pullback[:10]:
        code = s["code"]
        if code in candidates:
            # 已在候选池中，补充龙回头信号
            candidates[code]["source"] += "+龙回头"
            candidates[code]["pullback_signals"] = s["signals"]
        else:
            candidates[code] = {
                "code": code,
                "name": s["name"],
                "source": "龙回头",
                "continuous_days": s.get("max_continuous_days", 0),
                "zt_time": "",
                "reason": s["reason"],
                "change_pct": 0,
                "tags": [],
                "pullback_signals": s["signals"],
                "pullback_pct": s["pullback_pct"],
                "strength_score": s["strength_score"],
            }

    # 转为列表并排序（连板 > 龙回头 > 主线题材）
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


def _tech_check(code: str) -> Optional[Dict]:
    """对单只候选股做快速技术面检查（纯 Python，0 token）。"""
    bars = _fetch_kline(code, days=60)
    if len(bars) < 20:
        return None

    closes = [b["close"] for b in bars]
    volumes = [b["volume"] for b in bars]
    i = len(bars) - 1

    # 均线
    ma5 = _compute_ma(closes, 5)
    ma10 = _compute_ma(closes, 10)
    ma20 = _compute_ma(closes, 20)

    # RSI
    rsi = _compute_rsi(closes)

    # 量比
    vol_ratio = _compute_volume_ratio(volumes)

    # 均线多头排列？
    ma_bullish = (
        ma5[i] is not None and ma10[i] is not None and ma20[i] is not None
        and ma5[i] > ma10[i] > ma20[i]
    )

    # 近5日放量？
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


# ═══════════════════════════════════════════════════════════════
# Phase 2 深入分析（调工具）
# ═══════════════════════════════════════════════════════════════

def _deep_analyze_one(
    candidate: Dict, tech: Optional[Dict],
    call_tool_fn, _tool_calls, _tool_nodes, _missing_data,
) -> Optional[Dict]:
    """对单只候选股做深入分析。返回 dict 或 None。"""
    code = candidate["code"]
    try:
        # 调工具获取资金流 + 指标快照
        fund_flow = call_tool_fn("get_fund_flow_realtime", stock_code=code)
        snapshot = call_tool_fn("get_indicator_snapshot", stock_code=code)

        if _tool_calls is not None:
            for t in ["get_fund_flow_realtime", "get_indicator_snapshot"]:
                if t not in _tool_calls:
                    _tool_calls.append(t)

        score = 55.0  # 进入候选池就有基础分
        signals = []
        factors = []

        # ── 来源加分 ──
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

        # ── 技术面 ──
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

        # ── 指标快照（工具数据）──
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

        # ── 资金流 ──
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

        # ── 风险扣分 ──
        reason = candidate.get("reason", "")
        if "ST" in reason or "退市" in reason:
            score -= 20
            signals.append("ST/退市风险")

        score = max(0, min(100, score))
        direction = "bullish" if score >= 60 else ("bearish" if score <= 40 else "neutral")

        return {
            "code": code,
            "name": candidate.get("name", ""),
            "source": source,
            "reason": reason,
            "score": round(score, 1),
            "direction": direction,
            "signal": ", ".join(signals[:5]),
            "factors": [f.to_dict() for f in factors],
            "tech": tech,
        }

    except Exception as e:
        logger.warning("[STScreen] 深入分析 %s 失败: %s", code, e)
        return None


# ═══════════════════════════════════════════════════════════════
# Skill 定义
# ═══════════════════════════════════════════════════════════════

@skill("short_term_screener", auto_load=True)
class ShortTermScreenerSkill:
    """盘中短线选股专家子 Agent。"""

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

        date = _today_str()

        # ── Phase 1: 全市场预筛选 ──
        try:
            prescreen = _prescreen(date)
        except Exception as e:
            logger.warning("[STScreen] 预筛选失败: %s", e)
            return None  # fallback 到 LLM

        market = prescreen["market"]
        main_themes = prescreen["main_themes"]
        candidates = prescreen["candidates"]

        logger.info(
            "[STScreen] 预筛选完成: 涨停%d 跌停%d 候选%d只",
            market["zt_count"], market["dt_count"], len(candidates),
        )

        # 市场情绪太差，直接建议不参与
        if market["mood_score"] < 30:
            return SkillReport(
                skill_name=self.name,
                score=25.0,
                direction="bearish",
                confidence=0.7,
                signal=f"市场冰点（涨停{market['zt_count']}跌停{market['dt_count']}），不宜短线",
                analysis=(
                    f"## 短线选股 — 市场冰点\n\n"
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

        # 无候选股
        if not candidates:
            return SkillReport(
                skill_name=self.name,
                score=45.0,
                direction="neutral",
                confidence=0.5,
                signal="今日无明确短线标的",
                analysis=(
                    f"## 短线选股 — 无明确标的\n\n"
                    f"市场情绪：{market['mood']}（涨停{market['zt_count']}跌停{market['dt_count']}）\n"
                    f"今日无连板股或主线题材强势股进入候选池。"
                ),
                factors=[
                    FactorItem(name="市场情绪", value=market["mood"], score=market["mood_score"]),
                ],
                status="ok",
            )

        # ── 快速技术面检查（纯 Python）──
        tech_results = {}
        for c in candidates[:15]:
            tech = _tech_check(c["code"])
            if tech:
                tech_results[c["code"]] = tech

        # ── Phase 2: 深入分析（最多 8 只）──
        analyzed = []
        for c in candidates[:8]:
            tech = tech_results.get(c["code"])
            result = _deep_analyze_one(
                c, tech, call_tool_fn,
                _tool_calls, _tool_nodes, _missing_data,
            )
            if result:
                analyzed.append(result)

        # ── 综合评分 ──
        if analyzed:
            avg_score = sum(a["score"] for a in analyzed) / len(analyzed)
            bullish = sum(1 for a in analyzed if a["direction"] == "bullish")
            bearish = sum(1 for a in analyzed if a["direction"] == "bearish")
        else:
            avg_score = 50.0
            bullish, bearish = 0, 0

        direction = "bullish" if avg_score >= 55 else ("bearish" if avg_score < 45 else "neutral")
        confidence = min(0.9, 0.4 + len(analyzed) * 0.06)

        # ── 因子 ──
        factors = [
            FactorItem(name="市场情绪", value=market["mood"], score=market["mood_score"]),
            FactorItem(name="涨停/跌停", value=f"{market['zt_count']}/{market['dt_count']}", score=market["mood_score"]),
            FactorItem(name="候选股数", value=str(len(candidates)), score=min(100, len(candidates) * 8 + 30)),
            FactorItem(name="主线题材", value=", ".join(t for t, _ in main_themes[:3]) or "无", score=70 if main_themes else 40),
            FactorItem(name="深入分析数", value=str(len(analyzed)), score=min(100, len(analyzed) * 12 + 20)),
            FactorItem(name="看多比例", value=f"{bullish}/{len(analyzed) or len(candidates)}", score=int(avg_score)),
        ]

        # ── 构建分析文字 ──
        lines = [
            f"## 短线选股结果",
            f"市场情绪: {market['mood']} | 涨停{market['zt_count']} 跌停{market['dt_count']} 炸板率{market['broken_rate']}%",
            f"主线题材: {', '.join(t for t, _ in main_themes[:5]) or '无明确主线'}",
            f"候选: {len(candidates)}只 | 深入分析: {len(analyzed)}只 | 综合评分: {avg_score:.0f}",
            "",
        ]

        # 连板股
        cb = prescreen.get("continuous_board", [])
        if cb:
            lines.append("### 连板龙头")
            for s in cb[:5]:
                lines.append(
                    f"- **{s['code']}** {s['name']} | "
                    f"{s['continuous_days']}连板 | 涨停时间{s['zt_time']} | {s['reason']}"
                )
            lines.append("")

        # 龙回头弱转强
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

        # 候选股详情
        lines.append("### 候选标的")
        for a in analyzed:
            src = a.get("source", "")
            lines.append(
                f"- **{a['code']}** {a.get('name', '')} | "
                f"评分{a['score']:.0f} | {a['direction']} | "
                f"来源:{src} | {a['signal']}"
            )

        return SkillReport(
            skill_name=self.name,
            score=round(avg_score, 1),
            direction=direction,
            confidence=confidence,
            signal=f"短线{bullish}只看多候选，主线:{', '.join(t for t, _ in main_themes[:2]) or '无'}",
            factors=factors,
            analysis="\n".join(lines),
            output_data={
                "market": market,
                "main_themes": main_themes,
                "dragon_pullback": dp,
                "candidates": [c for c in candidates[:15]],
                "analyzed": analyzed,
            },
            tools_called=_tool_calls or [],
            missing_data=_missing_data or [],
            status="ok",
        )

# -*- coding: utf-8 -*-
"""
EOD Screener Skill — 尾盘选股专家（隔夜持仓特化）。

两阶段流程：
  Phase 1: search_stocks 条件选股 + Python 尾盘特征筛选（0 token）
    → 涨幅适中 + 放量 + 收盘在高位 + 主线题材
  Phase 2: 对候选股调用工具做深入分析
    → 技术面验证 + 资金流确认 + 隔夜风险评估

适用场景：14:30后"尾盘买什么"、"隔夜持仓"、"次日计划"。
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


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _fetch_kline(code: str, days: int = 20) -> List[Dict]:
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


def _fetch_zt_pool(date: str) -> List[Dict]:
    try:
        from app.market_cn.dragon_limit import get_zt_pool
        return get_zt_pool(date)
    except Exception:
        return []


def _fetch_hot_stocks_with_reason(date: str) -> Dict:
    import requests as _req
    url = f"http://zx.10jqka.com.cn/event/api/getharden/date/{date}/orderby/date/orderway/desc/charset/GBK/"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/117.0.0.0"}
    try:
        r = _req.get(url, headers=headers, timeout=10)
        data = r.json()
        if data.get("errocode", 0) != 0:
            return {"error": data.get("errormsg", "")}
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
    except Exception as e:
        return {"error": str(e)}


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


# ═══════════════════════════════════════════════════════════════
# Phase 1: 条件选股 + Python 尾盘特征筛选
# ═══════════════════════════════════════════════════════════════

def _prescreen_eod(call_tool_fn) -> Dict[str, Any]:
    """尾盘预筛选：search_stocks 条件选股 + Python 特征验证。"""

    # ── 1. 用 search_stocks 做条件初筛 ──
    # 条件：涨幅3%-8% + 换手率>3% + 非ST
    screener_result = call_tool_fn(
        "search_stocks",
        query="涨幅3%到8% 换手率大于3% 非ST",
        source="eastmoney",
        top_n=80,
    )
    raw_stocks = screener_result.get("stocks", []) if isinstance(screener_result, dict) else []

    # ── 2. 获取涨停池（识别尾盘封板）──
    date = _today_str()
    zt_pool = _fetch_zt_pool(date)

    # 尾盘封板股（14:30后）
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

    # ── 3. 获取强势股题材归因 ──
    hot_data = _fetch_hot_stocks_with_reason(date)
    reason_map = {}
    hot_tags = hot_data.get("hot_tags", [])
    for s in hot_data.get("stocks", []):
        reason_map[s.get("code", "")] = s.get("reason", "")

    # 主线题材关键词
    main_tags = set()
    for tag, _ in hot_tags[:5]:
        main_tags.add(tag)

    # ── 4. Python 特征验证（对 search_stocks 结果）──
    candidates = []

    for s in raw_stocks:
        code = str(s.get("code", "") or s.get("symbol", ""))
        if not code or len(code) != 6:
            continue

        name = s.get("name", "")
        change_pct = float(s.get("change_pct", 0) or s.get("pct_change", 0) or 0)
        turnover = float(s.get("turnover_rate", 0) or 0)

        # 涨幅范围二次校验
        if change_pct < 3 or change_pct > 8:
            continue

        # 拉K线验证尾盘特征
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

        # 核心指标
        close_to_high = (high - close) / high * 100  # 收盘离最高价的距离%
        day_range = high - low
        close_position = (close - low) / day_range if day_range > 0 else 0  # 收盘在日内位置

        # 量比
        prev_volumes = [bars[j]["volume"] for j in range(max(0, len(bars) - 5), len(bars) - 1)]
        avg_vol = sum(prev_volumes) / len(prev_volumes) if prev_volumes else 1
        vol_ratio = volume / avg_vol if avg_vol > 0 else 1

        # ── 尾盘特征评分 ──
        eod_score = 50
        signals = []

        # 收盘接近最高价（核心信号）
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
            continue  # 收盘离最高价太远，排除

        # 放量
        if vol_ratio > 2.5:
            eod_score += 12
            signals.append(f"大幅放量{vol_ratio:.1f}倍")
        elif vol_ratio > 1.5:
            eod_score += 8
            signals.append(f"放量{vol_ratio:.1f}倍")
        elif vol_ratio > 1.2:
            eod_score += 3
            signals.append(f"温和放量{vol_ratio:.1f}倍")

        # 收盘在日内高位区间
        if close_position > 0.85:
            eod_score += 10
            signals.append("收盘在日内高位")
        elif close_position > 0.7:
            eod_score += 5

        # 涨幅适中（4-6%最佳，隔夜性价比高）
        if 4 <= change_pct <= 6:
            eod_score += 8
            signals.append(f"涨幅{change_pct:.1f}%适中")
        elif 6 < change_pct <= 7:
            eod_score += 3

        # 主线题材加分
        reason = reason_map.get(code, "")
        if reason:
            matched = [t for t in reason.replace("，", "+").replace(",", "+").split("+") if t.strip() in main_tags]
            if matched:
                eod_score += 10
                signals.append(f"主线题材:{'+'.join(matched[:2])}")
            else:
                eod_score += 3
                signals.append(f"题材:{reason[:15]}")

        # RSI 不能太高
        closes = [b["close"] for b in bars]
        rsi = _compute_rsi(closes)
        if rsi[-1] > 80:
            eod_score -= 10
            signals.append(f"RSI{rsi[-1]:.0f}超买警告")

        # 均线多头加分
        ma5 = _compute_ma(closes, 5)
        ma10 = _compute_ma(closes, 10)
        if ma5[-1] and ma10[-1] and ma5[-1] > ma10[-1]:
            eod_score += 5
            signals.append("MA5>MA10")

        # 至少2个信号
        if len(signals) < 2:
            continue

        candidates.append({
            "code": code,
            "name": name,
            "change_pct": change_pct,
            "turnover": turnover,
            "close": round(close, 3),
            "high": round(high, 3),
            "close_to_high": round(close_to_high, 2),
            "vol_ratio": round(vol_ratio, 2),
            "rsi": round(rsi[-1], 2),
            "reason": reason,
            "eod_score": eod_score,
            "signals": signals,
            "source": "尾盘强势",
        })

    # 尾盘封板股加入候选池（最高优先级）
    zt_codes = set()
    for s in eod_zt:
        code = s["code"]
        zt_codes.add(code)
        # 拉K线获取基础数据
        bars = _fetch_kline(code, days=5)
        close = bars[-1]["close"] if bars else 0
        candidates.append({
            "code": code,
            "name": s["name"],
            "change_pct": 9.9,
            "turnover": 0,
            "close": round(close, 3),
            "high": round(close, 3),
            "close_to_high": 0,
            "vol_ratio": 0,
            "rsi": 0,
            "reason": s.get("reason", ""),
            "eod_score": 90,
            "signals": [f"尾盘封板{s['zt_time']}", f"{s['continuous_days']}连板"],
            "source": "尾盘封板",
        })

    # 去重 + 排序
    seen = set()
    unique = []
    for c in sorted(candidates, key=lambda x: -x["eod_score"]):
        if c["code"] not in seen:
            seen.add(c["code"])
            unique.append(c)

    # 主线题材
    themes = [(tag, cnt) for tag, cnt in hot_tags[:5]]

    return {
        "date": date,
        "screener_count": len(raw_stocks),
        "zt_eod_count": len(eod_zt),
        "main_themes": themes,
        "candidates": unique[:15],
    }


# ═══════════════════════════════════════════════════════════════
# Phase 2: 深入分析
# ═══════════════════════════════════════════════════════════════

def _deep_analyze_eod(
    candidate: Dict, call_tool_fn, _tool_calls, _tool_nodes, _missing_data,
) -> Optional[Dict]:
    """对候选股做深入分析，评估隔夜持仓价值。"""
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

        # ── 指标快照 ──
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

        # ── 资金流 ──
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

        # ── 隔夜风险评估 ──
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
            "code": code,
            "name": candidate.get("name", ""),
            "source": candidate.get("source", ""),
            "reason": candidate.get("reason", ""),
            "score": round(score, 1),
            "direction": direction,
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
        logger.warning("[EODScreener] 深入分析 %s 失败: %s", code, e)
        return None


# ═══════════════════════════════════════════════════════════════
# Skill 定义
# ═══════════════════════════════════════════════════════════════

@skill(
    name="eod_screener",
    description=(
        "尾盘选股专家。用条件选股(search_stocks)做初筛，再用Python验证尾盘特征（收盘接近最高价+放量+主线题材），"
        "最后对候选股做技术面+资金流深入分析。"
        "适用于：14:30后尾盘买什么、隔夜持仓标的、次日计划。"
    ),
    tools=[
        "search_stocks",
        "get_indicator_snapshot",
        "get_fund_flow_realtime",
        "get_limit_pool",
        "get_hot_sectors",
        "get_hot_stocks_with_reason",
        "agent_technical_analysis",
        "get_realtime_quote",
        "agent_get_kline",
        "search_stock_by_name",
    ],
    priority=8,
    default_weight=1.0,
    instructions=(
        "你是A股尾盘选股专家，专注隔夜持仓标的筛选。\n\n"
        "工作流程：\n"
        "1. 用 search_stocks 条件选股（涨幅3%-8% + 换手率>3%）做初筛\n"
        "2. 用 Python 验证尾盘特征：收盘接近最高价 + 放量 + 主线题材\n"
        "3. 涨停池识别尾盘封板股（14:30后封板 = 主力尾盘突击）\n"
        "4. 对候选股调用工具做技术面+资金流深入分析\n\n"
        "尾盘选股核心逻辑：\n"
        "- 收盘接近最高价 = 主力尾盘拉升，次日高开概率大\n"
        "- 放量 = 资金介入真实，非假突破\n"
        "- 涨幅4-6%最佳（太高追涨风险，太低力度不够）\n"
        "- 主线题材 = 次日有延续性\n"
        "- 尾盘封板 = 最强信号\n\n"
        "隔夜持仓纪律：\n"
        "- RSI>80超买不隔夜\n"
        "- 涨幅>7%谨慎隔夜\n"
        "- 主力净流出不隔夜\n"
        "- 非主线题材降低仓位\n\n"
        "## 输出格式（必须遵守）\n"
        "```json\n"
        "{\n"
        '  "direction": "bullish/bearish/neutral",\n'
        '  "confidence": 0.0-1.0,\n'
        '  "score": 0-100,\n'
        '  "signal": "一句话信号摘要",\n'
        '  "factors": [\n'
        '    {"name": "因子名", "value": "值", "score": 0-100, "status": "ok"}\n'
        '  ]\n'
        "}\n"
        "```\n"
    ),
)
class EODScreenerSkill:
    """尾盘选股专家子 Agent。"""

    def algo_analyze(
        self,
        stock_code: str,
        stock_name: str,
        tool_results: Dict[str, Any],
        **kwargs,
    ) -> Optional[SkillReport]:
        """Phase 1: 条件选股+Python尾盘筛选 + Phase 2: 深入分析。"""
        call_tool_fn = kwargs.get("call_tool_fn")
        _tool_calls = kwargs.get("_tool_calls", [])
        _tool_nodes = kwargs.get("_tool_nodes", [])
        _missing_data = kwargs.get("_missing_data", [])

        # ── Phase 1: 预筛选 ──
        try:
            prescreen = _prescreen_eod(call_tool_fn)
        except Exception as e:
            logger.warning("[EODScreener] 预筛选失败: %s", e)
            return None  # fallback 到 LLM

        candidates = prescreen["candidates"]
        main_themes = prescreen["main_themes"]

        logger.info("[EODScreener] 预筛选完成: 条件选股%d只, 尾盘封板%d只, 候选%d只",
                     prescreen["screener_count"], prescreen["zt_eod_count"], len(candidates))

        if not candidates:
            return SkillReport(
                skill_name=self.name,
                score=40.0,
                direction="neutral",
                confidence=0.5,
                signal="今日无合适隔夜标的",
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

        # ── Phase 2: 深入分析（最多 6 只）──
        analyzed = []
        for c in candidates[:6]:
            result = _deep_analyze_eod(
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
            FactorItem(name="条件选股数", value=str(prescreen["screener_count"]), score=50),
            FactorItem(name="尾盘封板", value=str(prescreen["zt_eod_count"]),
                       score=min(100, prescreen["zt_eod_count"] * 25 + 30)),
            FactorItem(name="主线题材", value=", ".join(t for t, _ in main_themes[:3]) or "无",
                       score=70 if main_themes else 40),
            FactorItem(name="候选标的", value=str(len(analyzed)), score=min(100, len(analyzed) * 15 + 20)),
            FactorItem(name="看多比例", value=f"{bullish}/{len(analyzed)}", score=int(avg_score)),
        ]

        # ── 分析文字 ──
        lines = [
            f"## 尾盘选股结果（隔夜持仓）",
            f"条件选股: {prescreen['screener_count']}只 | 尾盘封板: {prescreen['zt_eod_count']}只 | 深入分析: {len(analyzed)}只",
            f"主线题材: {', '.join(t for t, _ in main_themes[:5]) or '无明确主线'}",
            "",
        ]

        # 尾盘封板
        eod_zt = [c for c in candidates if c.get("source") == "尾盘封板"]
        if eod_zt:
            lines.append("### 尾盘封板（最强信号）")
            for s in eod_zt[:3]:
                lines.append(
                    f"- **{s['code']}** {s['name']} | "
                    f"封板时间{s.get('zt_time', '')} | {s.get('reason', '')}"
                )
            lines.append("")

        # 深入分析结果
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
            skill_name=self.name,
            score=round(avg_score, 1),
            direction=direction,
            confidence=confidence,
            signal=f"隔夜{bullish}只看多，主线:{', '.join(t for t, _ in main_themes[:2]) or '无'}",
            factors=factors,
            analysis="\n".join(lines),
            output_data={
                "main_themes": main_themes,
                "candidates": [c for c in candidates[:15]],
                "analyzed": analyzed,
            },
            tools_called=_tool_calls or [],
            missing_data=_missing_data or [],
            status="ok",
        )

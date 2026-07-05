# -*- coding: utf-8 -*-
"""
BB Screener Skill — BB超卖全市场扫描 + 逐只深入分析。

两阶段流程：
  Phase 1: 纯算法全市场扫描（0 token，复用 test_bb_indicator.py 核心逻辑）
    → 筛选出今日触发 BB 超卖信号的股票
  Phase 2: 对候选股票逐只调用工具做深入技术分析
    → 提高成功率，给出综合评分

触发方式：用户说"今天有没有BB超卖的股票"、"扫描BB信号"、"BB选股"等。
"""
from __future__ import annotations
import json

from app.agent.log import logger
import math
import os
import sys
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional
from app.agent.utils.md_format import _format_final_md

from dataclasses import dataclass, field

# ═══════════════════════════════════════════════════════════════
#  自包含数据结构
# ═══════════════════════════════════════════════════════════════

@dataclass
class FactorItem:
    """单个因子的评分结果。"""
    name: str
    value: str = ""
    score: Optional[float] = None
    weight: float = 1.0
    status: str = "ok"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name, "value": self.value,
            "score": self.score, "weight": self.weight, "status": self.status,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "FactorItem":
        return cls(
            name=d.get("name", ""),
            value=str(d.get("value", "")),
            score=d.get("score"),
            weight=d.get("weight", 1.0),
            status=d.get("status", "ok"),
        )


@dataclass
class SkillReport:
    """Skill 标准化输出。"""
    skill_name: str
    score: float = 50.0
    confidence: float = 0.0
    direction: str = "neutral"
    signal: str = ""
    factors: List[FactorItem] = field(default_factory=list)
    analysis: str = ""
    output_data: Dict[str, Any] = field(default_factory=dict)
    tools_called: List[str] = field(default_factory=list)
    missing_data: List[str] = field(default_factory=list)
    status: str = "ok"
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "skill_name": self.skill_name,
            "score": self.score, "confidence": self.confidence,
            "direction": self.direction, "signal": self.signal,
            "factors": [f.to_dict() for f in self.factors],
            "analysis": self.analysis, "output_data": self.output_data,
            "tools_called": self.tools_called, "missing_data": self.missing_data,
            "status": self.status, "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SkillReport":
        return cls(
            skill_name=d.get("skill_name", ""),
            score=d.get("score", 50.0),
            confidence=d.get("confidence", 0.0),
            direction=d.get("direction", "neutral"),
            signal=d.get("signal", ""),
            factors=[FactorItem.from_dict(f) for f in d.get("factors", [])],
            analysis=d.get("analysis", ""),
            output_data=d.get("output_data", {}),
            tools_called=d.get("tools_called", []),
            missing_data=d.get("missing_data", []),
            status=d.get("status", "ok"),
            error=d.get("error", ""),
        )

# ═══════════════════════════════════════════════════════════════
# 数据加载（复用 test_bb_indicator.py 的数据源）
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
def _get_all_codes(filter_st=True):
    db = _get_basic_db()
    stocks = db.get_all_stocks(status="active")
    if filter_st:
        stocks = [s for s in stocks if "ST" not in s.get("name", "").upper()]
    return [s["symbol"] for s in stocks]
def _get_name_map():
    db = _get_basic_db()
    stocks = db.get_all_stocks(status="active")
    return {s["symbol"]: s["name"] for s in stocks}
def _get_board_name(code):
    c = str(code)[:3]
    if c.startswith("68"):
        return "科创板"
    elif c.startswith("30"):
        return "创业板"
    elif c.startswith("6"):
        return "沪主板"
    elif c.startswith(("0", "2")):
        return "深主板"
    return "未知"
def _fetch_kline(code, days=300):
    """从 db_market 获取日 K 线（前复权）。"""
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
# 指标计算（从 test_bb_indicator.py 提取，纯 Python）
# ═══════════════════════════════════════════════════════════════

def _compute_rsi(closes, period=14):
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
def _compute_volume_ratio(volumes, window=5):
    n = len(volumes)
    vr = [0.0] * n
    for i in range(window, n):
        avg = sum(volumes[i - window:i]) / window
        if avg > 0:
            vr[i] = volumes[i] / avg
    return vr
def _compute_ma_slope(closes, ma_len=60):
    n = len(closes)
    slope = [-999.0] * n
    if n < ma_len + 1:
        return slope
    ma = [0.0] * n
    for i in range(ma_len - 1, n):
        ma[i] = sum(closes[i - ma_len + 1:i + 1]) / ma_len
    for i in range(ma_len, n):
        if ma[i - 1] > 0:
            slope[i] = (ma[i] - ma[i - 1]) / ma[i - 1] * 100
    return slope
def _compute_bb(closes, period=20, num_std=3.0):
    n = len(closes)
    mid, up, lo = [None] * n, [None] * n, [None] * n
    for i in range(period - 1, n):
        w = closes[i - period + 1:i + 1]
        sma = sum(w) / period
        std = math.sqrt(sum((x - sma) ** 2 for x in w) / period)
        mid[i], up[i], lo[i] = sma, sma + num_std * std, sma - num_std * std
    return mid, up, lo
# ═══════════════════════════════════════════════════════════════
# BB 超卖入场检查（今日信号，跳过 D+1 规则）
# ═══════════════════════════════════════════════════════════════

def _check_bb_entry(bars, code, bb_period=20, bb_std=3.0, ma_slope_threshold=0.0):
    """检查最新一根 K 线是否满足 BB 超卖入场条件。返回 dict 或 None。"""
    if len(bars) < max(bb_period + 2, 62):
        return None

    closes = [b["close"] for b in bars]
    volumes = [b["volume"] for b in bars]
    lows = [b["low"] for b in bars]
    i = len(bars) - 1

    # BB 下轨
    _, bb_upper, bb_lower = _compute_bb(closes, bb_period, bb_std)
    if bb_lower[i] is None:
        return None
    if lows[i] >= bb_lower[i]:
        return None
    if closes[i] > bb_lower[i] * 1.05:
        return None

    # 振幅 + 下影线
    prev_close = bars[i - 1]["close"] if i > 0 else bars[i]["open"]
    body_low = min(bars[i]["open"], bars[i]["close"])
    if prev_close <= 0:
        return None
    lower_shadow = (body_low - bars[i]["low"]) / prev_close
    amplitude = (bars[i]["high"] - bars[i]["low"]) / prev_close
    if amplitude < 0.08 or (amplitude > 0 and lower_shadow / amplitude >= 0.30):
        return None

    # MA60 斜率
    ma60_slopes = _compute_ma_slope(closes, 60)
    if ma60_slopes[i] != -999.0 and ma60_slopes[i] < ma_slope_threshold:
        return None

    # 补充指标
    rsi = _compute_rsi(closes)
    vol_ratio = _compute_volume_ratio(volumes, 5)

    return {
        "code": code,
        "board": _get_board_name(code),
        "signal_date": bars[i]["time"],
        "close": round(closes[i], 3),
        "low": round(lows[i], 3),
        "high": round(bars[i]["high"], 3),
        "bb_lower": round(bb_lower[i], 2),
        "bb_middle": round(bb_upper[i], 2) if bb_upper[i] else None,
        "amplitude": round(amplitude * 100, 2),
        "lower_shadow": round(lower_shadow * 100, 2),
        "rsi": round(rsi[i], 2),
        "vol_ratio": round(vol_ratio[i], 3),
        "ma60_slope": round(ma60_slopes[i], 4),
    }
# ═══════════════════════════════════════════════════════════════
# 深入分析（Phase 2）
# ═══════════════════════════════════════════════════════════════

def _deep_analyze_one(code, bb_hit, call_tool_fn, _tool_calls, _tool_nodes, _missing_data):
    """对单只候选股做深入技术分析。返回 dict 或 None。"""
    try:
        snapshot = call_tool_fn("get_indicator_snapshot", stock_code=code)
        chip = call_tool_fn("get_chip_distribution", stock_code=code)

        if _tool_calls is not None:
            for t in ["get_indicator_snapshot", "get_chip_distribution"]:
                if t not in _tool_calls:
                    _tool_calls.append(t)

        score = 55.0  # BB 超卖本身偏多，基础分
        signals = [f"BB超卖振幅{bb_hit['amplitude']}%"]
        factors = []

        # ── 指标快照 ──
        if isinstance(snapshot, dict) and "error" not in snapshot:
            # MACD
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

            # RSI
            rsi_val = snapshot.get("rsi", {}).get("value", 50)
            if rsi_val < 30:
                score += 10
                signals.append(f"RSI{rsi_val}超卖")
            elif rsi_val > 70:
                score -= 5
                signals.append(f"RSI{rsi_val}超买")
            factors.append(FactorItem(
                name="RSI", value=str(rsi_val),
                score=70 if rsi_val < 30 else (30 if rsi_val > 70 else 50),
            ))

            # KDJ
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

            # 均线
            ma = snapshot.get("ma", {})
            ma5, ma20 = ma.get("ma5", 0), ma.get("ma20", 0)
            if ma5 and ma20 and ma5 > ma20:
                score += 5
                signals.append("MA5>MA20")
            factors.append(FactorItem(
                name="均线", value=f"MA5={ma5} MA20={ma20}",
                score=60 if (ma5 and ma20 and ma5 > ma20) else 40,
            ))

        # ── 筹码 ──
        if isinstance(chip, dict) and "error" not in chip:
            profit = chip.get("profit_ratio", 50)
            if profit < 30:
                score += 5
                signals.append(f"获利盘{profit}%低位")
            factors.append(FactorItem(
                name="筹码", value=f"获利{profit}% 均价{chip.get('avg_cost', '?')}",
                score=65 if profit < 30 else 50,
            ))

        score = max(0, min(100, score))
        direction = "bullish" if score >= 60 else ("bearish" if score <= 40 else "neutral")

        return {
            "code": code,
            "name": bb_hit.get("name", ""),
            "board": bb_hit.get("board", ""),
            "score": round(score, 1),
            "direction": direction,
            "signal": ", ".join(signals[:5]),
            "factors": [f.to_dict() for f in factors],
            "bb_data": {
                "close": bb_hit["close"],
                "bb_lower": bb_hit["bb_lower"],
                "amplitude": bb_hit["amplitude"],
                "rsi": bb_hit["rsi"],
                "vol_ratio": bb_hit["vol_ratio"],
            },
        }

    except Exception as e:
        logger.warning("[BBScreener] 深入分析 %s 失败: %s", code, e)
        return None
# ═══════════════════════════════════════════════════════════════
# Skill 定义
# ═══════════════════════════════════════════════════════════════

class BBScreenerSkill:
    """BB超卖全市场扫描 + 深入分析。"""

    def algo_analyze(
        self,
        stock_code: str,
        stock_name: str,
        tool_results: Dict[str, Any],
        **kwargs,
    ) -> Optional[SkillReport]:
        """Phase 1: 全市场扫描（纯算法，0 token）+ Phase 2: 候选股深入分析。"""
        call_tool_fn = kwargs.get("call_tool_fn")
        _tool_calls = kwargs.get("_tool_calls", [])
        _tool_nodes = kwargs.get("_tool_nodes", [])
        _missing_data = kwargs.get("_missing_data", [])

        # ── 获取全市场股票列表 ──
        try:
            codes = _get_all_codes(filter_st=True)
            name_map = _get_name_map()
        except Exception as e:
            logger.warning("[BBScreener] 获取股票列表失败: %s", e)
            return None  # fallback 到 LLM

        logger.info("[BBScreener] 开始全市场扫描: %d 只", len(codes))

        # ── Phase 1: 全量 BB 超卖筛选 ──
        hits = []
        scanned = 0
        for code in codes:
            try:
                bars = _fetch_kline(code, days=300)
                if not bars:
                    continue
                sig = _check_bb_entry(bars, code)
                if sig:
                    sig["name"] = name_map.get(code, "")
                    hits.append(sig)
                scanned += 1
            except Exception:
                continue
            if scanned % 500 == 0:
                logger.info("[BBScreener] 已扫描 %d ...", scanned)

        logger.info("[BBScreener] 扫描完成: %d 只, 命中 %d 只", scanned, len(hits))

        # 无命中
        if not hits:
            return SkillReport(
                skill_name=self.name,
                score=50.0,
                direction="neutral",
                confidence=0.6,
                signal="今日无BB超卖信号",
                analysis=f"全市场 {scanned} 只股票扫描完成，今日无符合条件的BB超卖信号。",
                factors=[FactorItem(name="扫描数量", value=str(scanned), score=50)],
                status="ok",
            )

        # 按振幅降序
        hits.sort(key=lambda x: -x["amplitude"])

        # ── Phase 2: 候选股深入分析（最多 10 只）──
        analyzed = []
        for hit in hits[:10]:
            result = _deep_analyze_one(
                hit["code"], hit, call_tool_fn,
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
            avg_score = 60.0
            bullish, bearish = len(hits), 0

        direction = "bullish" if avg_score >= 55 else ("bearish" if avg_score < 45 else "neutral")
        confidence = min(0.9, 0.5 + len(analyzed) * 0.05)

        factors = [
            FactorItem(name="扫描总数", value=str(scanned), score=50),
            FactorItem(name="BB命中数", value=str(len(hits)),
                       score=min(100, len(hits) * 15 + 40)),
            FactorItem(name="深入分析数", value=str(len(analyzed)),
                       score=min(100, len(analyzed) * 10 + 30)),
            FactorItem(name="看多比例",
                       value=f"{bullish}/{len(analyzed) or len(hits)}",
                       score=int(avg_score)),
        ]

        # ── 构建分析文字 ──
        lines = [
            f"## BB超卖全市场扫描结果",
            f"扫描: {scanned}只 | 命中: {len(hits)}只 | 深入分析: {len(analyzed)}只",
            f"综合评分: {avg_score:.0f} | 方向: {direction}",
            "",
            "### 候选股票（按振幅排序）",
        ]
        for h in hits[:10]:
            lines.append(
                f"- **{h['code']}** {h.get('name', '')} ({h['board']}) | "
                f"收盘{h['close']} | 振幅{h['amplitude']}% | "
                f"RSI{h['rsi']} | BB下轨{h['bb_lower']}"
            )

        if analyzed:
            lines.append("\n### 深入分析摘要")
            for a in analyzed:
                f_names = [f["name"] for f in a.get("factors", [])]
                lines.append(
                    f"- **{a['code']}** {a.get('name', '')} | "
                    f"评分{a['score']:.0f} | {a['direction']} | {a['signal']}"
                )

        _factors = [{"name": f.name, "score": f.score} for f in factors[:3]] if factors else []
        _extra = [l.replace("- **", "").replace("**", "") for l in lines[:5]] if lines else []
        analysis = _format_final_md(
            title=f"BB超卖 命中{len(hits)}只", score=avg_score, direction=direction,
            factors=_factors, extra=_extra,
        )
        return SkillReport(
            skill_name=self.name,
            score=round(avg_score, 1),
            direction=direction,
            confidence=confidence,
            signal=f"BB超卖命中{len(hits)}只，{bullish}只看多",
            factors=factors,
            analysis=analysis,
            output_data={
                "candidates": hits[:20],
                "analyzed": analyzed,
            },
            status="ok",
        )
# -*- coding: utf-8 -*-
"""BB超卖全市场扫描 — 布林带下轨突破策略筛选全市场，再对候选股做技术面深入分析。"""

def bb_screener_scan(stock_code: str = "") -> dict:
    """布林带超卖全市场扫描：先筛选触及下轨的候选股，再对候选股做技术面深入分析返回推荐列表。

    Args:
        stock_code: 股票代码，可选，为空则全市场扫描
    """
    from app.agent.tools import registry as tool_registry
    tool_registry.discover()

    def call_tool_fn(tool_name, **kwargs):
        spec = tool_registry.get(tool_name)
        if not spec: raise ValueError(f"Unknown tool: {tool_name}")
        return spec.fn(**kwargs)

    skill = BBScreenerSkill()
    result = skill.run(call_tool_fn=call_tool_fn)
    if isinstance(result, dict):
        _r = result
    else:
        _r = {"score": 50, "direction": "neutral", "confidence": 0.4, "signal": "BB扫描完成", "factors": [], "analysis": str(result)[:500], "status": "ok"}
    return _r

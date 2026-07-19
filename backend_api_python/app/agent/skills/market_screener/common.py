# -*- coding: utf-8 -*-
"""
market_screener/common.py

通用基础设施：数据加载、技术指标计算、涨停/跌停/炸板池、龙回头检测。
三个策略（盘中/尾盘/盘后）共享此模块。
"""

from __future__ import annotations

from app.agent.log import logger
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, date
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field

# ═══════════════════════════════════════════════════════════════
#  Skill 自包含数据结构
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
#  Markdown 渲染
# ═══════════════════════════════════════════════════════════════

_SRC_SHORT = {
    "连板": "连板", "龙回头": "龙头", "尾盘封板": "封板", "尾盘强势": "尾强",
    "盘后筛选": "筛选", "条件搜索": "搜索", "4IN1(近期涨停)": "4IN1",
}


def _fmt(v, dec=2):
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.{dec}f}"
    return str(v)


def _fmt_pct(v):
    if v is None or v == 0:
        return "-"
    return f"{v:+.1f}"


class SkillResult(dict):
    """dict 子类，代码按 dict 用，转 str 出 markdown 给 LLM。"""

    def __str__(self):
        # 错误
        if self.get("error"):
            return f"ERR: {self['error']}"

        # pre_screen() 格式
        if "candidates" in self or "market" in self:
            return self._prescreen_md()

        # deep_analyze() 格式
        if "analyzed" in self:
            return self._deep_analyze_md()

        # raw dict fallback
        return _compact_json(self)

    def __repr__(self):
        return self.__str__()

    def _prescreen_md(self):
        parts = []

        # 策略 + market 一行
        strategy = self.get("strategy", "")
        market = self.get("market", {})
        if market:
            mood = market.get("mood", "")
            ms = market.get("mood_score", 0)
            zt = market.get("zt_count", 0)
            dt = market.get("dt_count", 0)
            fund = market.get("fund_flow", 0)
            brk = market.get("broken_rate", 0)
            m_line = f"{strategy} M:{mood}({ms}) ZT:{zt} DT:{dt}"
            if fund:
                m_line += f" F{fund/1e8:.1f}e"
            if brk:
                m_line += f" 炸{brk}%"
            parts.append(m_line)

        # main_themes
        themes = self.get("main_themes", [])
        if themes:
            parts.append("题材:" + ",".join(f"{t}({c})" for t, c in themes[:5]))

        # candidates
        candidates = self.get("candidates", [])
        if candidates:
            parts.append(f"候选{len(candidates)}只:")
            rows = ["C#   Name    Src  Chg%   Trn%   Price  Vol"]
            rows.append("---- ------ ---- ----- ------ ----- ---")
            for c in candidates:
                src = _SRC_SHORT.get(c.get("source", ""), c.get("source", "")[:3])
                price = c.get("price") or c.get("close") or ""
                price_s = f"{price:.2f}" if isinstance(price, (int, float)) else str(price)
                rows.append(
                    f"{c.get('code',''):6} {c.get('name',''):6} {src:4} "
                    f"{_fmt_pct(c.get('change_pct')):5} "
                    f"{_fmt_pct(c.get('turnover_pct')):4} "
                    f"{price_s:6} {_fmt(c.get('vol_ratio'),1):3}"
                )
            parts.append("\n".join(rows))

        return "\n".join(parts)

    def _deep_analyze_md(self):
        parts = []
        s = self.get("score", 0)
        d = self.get("direction", "")
        cnf = self.get("confidence", 0)
        sig = self.get("signal", "")
        strategy = self.get("strategy", "")
        parts.append(f"**{strategy}**")
        parts.append(f"  综合评分: {s:.1f}/100")
        parts.append(f"  方向: {d}")
        parts.append("")

        analyzed = self.get("analyzed", [])
        if analyzed:
            parts.append("股票代码\t股票名称\t评分\t方向\t置信度\t信号")
            for a in analyzed:
                code = a.get("code", "")
                name = a.get("name", "")
                score = a.get("score", 0)
                direction = a.get("direction", "")
                confidence = a.get("confidence", "")
                signal = (a.get("signal", "") or "")
                parts.append(f"{code}\t{name}\t{score}\t{direction}\t{confidence}\t{signal}")

        return "\n".join(parts)


def _compact_json(d, max_depth=2, _depth=0):
    if _depth >= max_depth or not isinstance(d, dict):
        return str(d)
    items = []
    for k, v in d.items():
        if isinstance(v, (list, dict)) and len(str(v)) > 80 and _depth > 0:
            items.append(f"{k}: [{type(v).__name__}({len(v)})]")
        else:
            items.append(f"{k}: {_compact_json(v, max_depth, _depth+1)}")
    return "{" + ", ".join(items) + "}"


# ═══════════════════════════════════════════════════════════════
#  路径与环境
# ═══════════════════════════════════════════════════════════════

_backend_root = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")
)
if _backend_root not in sys.path:
    sys.path.insert(0, _backend_root)
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
_writer_cache = None
_basic_db_cache = None
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
#  工具分发
# ═══════════════════════════════════════════════════════════════

from app.agent.tools.finance.screener_tools import search_stocks
from app.agent.tools.finance.analysis_tools import get_indicator_snapshot
from app.market_cn.tape import get_fund_flow_realtime

_TOOL_REGISTRY = {
    "get_fund_flow_realtime": get_fund_flow_realtime,
    "get_indicator_snapshot": get_indicator_snapshot,
    "search_stocks": search_stocks,
}
def call_tool(name: str, **kwargs) -> Any:
    """按名称分发工具调用。"""
    fn = _TOOL_REGISTRY.get(name)
    if fn is None:
        return {"error": f"未知工具: {name}"}
    try:
        return fn(**kwargs)
    except Exception as e:
        logger.warning("[market_screener] 工具 %s 调用失败: %s", name, e)
        return {"error": str(e)}
# ═══════════════════════════════════════════════════════════════
#  通用数据采集
# ═══════════════════════════════════════════════════════════════

def fetch_kline(code: str, days: int = 60) -> List[Dict]:
    from app.data_sources.provider.adjustment import unadj_to_qfq
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=int(days * 1.5))).strftime("%Y-%m-%d")
    try:
        writer = _get_writer()
        data = writer.query("CNStock", code, "1D", start_time=start, end_time=end, limit=0)
        if not data:
            return []
        bars = [{
            "time": str(r["time"])[:10], "open": float(r["open"]),
            "high": float(r["high"]), "low": float(r["low"]),
            "close": float(r["close"]), "volume": float(r["volume"]),
        } for r in data]
        return unadj_to_qfq(bars, code)
    except Exception:
        return []
def get_limit_pct(code: str, name: str = "") -> float:
    """根据股票代码和名称返回涨跌停幅度。"""
    if "ST" in name.upper():
        return 5.0
    if code.startswith(("300", "301")):
        return 20.0
    if code.startswith("688"):
        return 20.0
    if code.startswith(("8", "4")):
        return 30.0
    return 10.0
def is_limit_locked(code: str, name: str, close: float, prev_close: float) -> bool:
    """判断是否涨停封板（买不进去）。"""
    if prev_close <= 0 or close <= 0:
        return False
    limit_pct = get_limit_pct(code, name)
    change_pct = (close - prev_close) / prev_close * 100
    return change_pct >= limit_pct - 0.5
def fetch_zt_pool(date: str) -> List[Dict]:
    try:
        from app.market_cn.dragon_limit import get_zt_pool
        return get_zt_pool(date)
    except Exception as e:
        logger.warning("[MktScreen] 涨停池获取失败: %s", e)
        return []
def fetch_dt_pool(date: str) -> List[Dict]:
    try:
        from app.market_cn.dragon_limit import get_dt_pool
        return get_dt_pool(date)
    except Exception as e:
        logger.warning("[MktScreen] 跌停池获取失败: %s", e)
        return []
def fetch_broken_board(date: str) -> List[Dict]:
    try:
        from app.market_cn.dragon_limit import get_broken_board
        return get_broken_board(date)
    except Exception as e:
        logger.warning("[MktScreen] 炸板池获取失败: %s", e)
        return []
def fetch_hot_stocks_with_reason(date: str) -> Dict:
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
        stocks = [{
            "code": row.get("code", ""), "name": row.get("name", ""),
            "reason": row.get("reason", ""),
            "change_pct": float(row.get("zhangfu", 0) or 0),
            "turnover_pct": float(row.get("huanshou", 0) or 0),
            "amount": float(row.get("chengjiaoe", 0) or 0),
        } for row in rows]
        tag_counter: Counter = Counter()
        for s in stocks:
            if s["reason"]:
                tags = [t.strip() for t in s["reason"].replace("，", "+").replace(",", "+").split("+") if t.strip()]
                tag_counter.update(tags)
        return {"stocks": stocks, "hot_tags": tag_counter.most_common(20)}
    except Exception as e:
        logger.warning("[MktScreen] 强势股获取失败: %s", e)
        return {"error": str(e)}
def fetch_hot_sectors() -> Dict:
    try:
        from app.market_cn.china_market import get_hot_sectors
        return get_hot_sectors(industry_limit=15, concept_limit=15)
    except Exception as e:
        logger.warning("[MktScreen] 热门板块获取失败: %s", e)
        return {"error": str(e)}
# ═══════════════════════════════════════════════════════════════
#  技术指标计算
# ═══════════════════════════════════════════════════════════════

def compute_ma(closes: List[float], period: int) -> List[Optional[float]]:
    n = len(closes)
    ma = [None] * n
    for i in range(period - 1, n):
        ma[i] = sum(closes[i - period + 1:i + 1]) / period
    return ma
def compute_ema(values: List[float], period: int) -> List[float]:
    n = len(values)
    if n == 0:
        return []
    ema = [values[0]]
    k = 2.0 / (period + 1)
    for i in range(1, n):
        ema.append(values[i] * k + ema[-1] * (1 - k))
    return ema
def compute_macd(closes: List[float]) -> Dict[str, List[float]]:
    ema12 = compute_ema(closes, 12)
    ema26 = compute_ema(closes, 26)
    dif = [a - b for a, b in zip(ema12, ema26)]
    dea = compute_ema(dif, 9)
    macd_bar = [2 * (d - e) for d, e in zip(dif, dea)]
    return {"dif": dif, "dea": dea, "macd": macd_bar}
def compute_rsi(closes: List[float], period: int = 14) -> List[float]:
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
def compute_volume_ratio(volumes: List[float], window: int = 5) -> List[float]:
    n = len(volumes)
    vr = [0.0] * n
    for i in range(window, n):
        avg = sum(volumes[i - window:i]) / window
        if avg > 0:
            vr[i] = volumes[i] / avg
    return vr
def compute_kdj(bars: List[Dict], period: int = 9) -> Dict[str, List[float]]:
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
def compute_atr(bars: List[Dict], period: int = 14) -> List[float]:
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
#  龙回头弱转强检测（盘中 + 盘后共享）
# ═══════════════════════════════════════════════════════════════

def fetch_recent_zt_pools(days: int = 8) -> Dict[str, List[Dict]]:
    pools = {}
    today = datetime.now()
    for i in range(days):
        d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        pool = fetch_zt_pool(d)
        if pool:
            pools[d] = pool
    return pools
def scan_dragon_pullback(date: str) -> List[Dict]:
    recent_pools = fetch_recent_zt_pools(8)
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
                "last_zt_date": max(records[0]["date"], date),
                "reason": records[0]["reason"],
            }

    if not dragon_codes:
        return []

    candidates = []
    for code, info in dragon_codes.items():
        bars = fetch_kline(code, days=30)
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

        ma5 = compute_ma(closes, 5)
        if ma5[i] is not None and closes[i] > ma5[i]:
            signals.append("站上MA5")
            strength_score += 8

        if ma5[i] is not None and ma5[i - 1] is not None:
            ma5_slope_today = (ma5[i] - ma5[i - 1]) / ma5[i - 1] * 100 if ma5[i - 1] > 0 else 0
            if ma5_slope_today > 0:
                signals.append("MA5拐头")
                strength_score += 5

        rsi = compute_rsi(closes)
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
            "code": code, "name": info["name"], "source": "龙回头",
            "max_continuous_days": info["max_continuous_days"],
            "zt_dates": info["zt_dates"], "reason": info["reason"],
            "pullback_pct": round(pullback_pct, 1),
            "peak_price": round(peak_price, 3),
            "trough_price": round(trough_price, 3),
            "close": round(closes[i], 3),
            "vol_ratio_today": round(vol_ratio_today, 2),
            "rsi": round(rsi[i], 2),
            "signals": signals, "strength_score": strength_score,
            "evaluation": {
                "score": strength_score,
                "highlights": signals,
                "warnings": [] if strength_score >= 40 else ["强度偏低，谨慎"],
            },
        })

    candidates.sort(key=lambda x: -x["strength_score"])
    return candidates

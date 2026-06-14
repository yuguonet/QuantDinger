"""
A股市场贪婪恐惧指数 — 简化版

7个维度，等权平均，输出 0-100 分:
  0-25 极度恐惧 | 25-40 恐惧 | 40-60 中性 | 60-75 贪婪 | 75-100 极度贪婪
"""

import pandas as pd
import numpy as np
import time
from datetime import datetime
from .index import get_index_daily_kline, get_northbound_daily

# ── 简单 TTL 缓存 ──────────────────────────────────
_fg_cache = None
_fg_cache_ts = 0
_FG_TTL = 300  # 5分钟缓存


# ── 工具 ──────────────────────────────────────────

def _clamp(v, lo=0, hi=100):
    return float(max(lo, min(hi, v)))

def _map(v, lo, hi):
    """线性映射到 0-100"""
    return _clamp((v - lo) / (hi - lo) * 100) if hi != lo else 50.0

def _label(score):
    if score <= 25: return "极度恐惧"
    if score <= 40: return "恐惧"
    if score <= 60: return "中性"
    if score <= 75: return "贪婪"
    return "极度贪婪"

def _col(df, *names):
    """按优先级找列名"""
    for n in names:
        if n in df.columns:
            return n
    return None

def _index_df():
    """沪深300日线 (多源降级)"""
    data = get_index_daily_kline("000300", 200)
    if data:
        return pd.DataFrame(data)
    return None

def _sorted(df):
    """按日期排序"""
    c = _col(df, 'date', 'trade_date', '日期')
    if c:
        df = df.copy()
        df[c] = pd.to_datetime(df[c])
        return df.sort_values(c)
    return df

def _numeric_col(df, *names):
    """找到列并转数值"""
    c = _col(df, *names)
    return pd.to_numeric(df[c], errors='coerce').dropna() if c else pd.Series(dtype=float)


# ── 7 个指标 ──────────────────────────────────────

def _momentum():
    """1. 沪深300 vs 125日均线"""
    try:
        df = _sorted(_index_df())
        close = _numeric_col(df, 'close', '收盘')
        if len(close) < 130:
            return 50, f"数据不足({len(close)})"
        ma = close.rolling(125, min_periods=100).mean().iloc[-1]
        ratio = close.iloc[-1] / ma
        return _map(ratio, 0.85, 1.15), f"价格/MA125={ratio:.3f}"
    except Exception as e:
        return 50, str(e)

def _breadth():
    """2. 市场宽度 — 读 scheduler 缓存的行业板块涨跌家数，不发 HTTP"""
    try:
        from .china_market import _rt_hot_sectors
        if not _rt_hot_sectors:
            return 50, "板块数据未加载"
        industry = (_rt_hot_sectors.get("data") or {}).get("industry", [])
        if not industry:
            return 50, "行业板块数据为空"
        up_total = sum(int(b.get("up_count", 0) or 0) for b in industry)
        down_total = sum(int(b.get("down_count", 0) or 0) for b in industry)
        total = up_total + down_total
        if total == 0:
            return 50, "涨跌家数为0"
        ratio = up_total / total
        return _map(ratio, 0.2, 0.8), f"上涨{up_total}/下跌{down_total}"
    except Exception as e:
        return 50, str(e)

def _volatility():
    """3. 20日年化波动率 (高=恐惧)"""
    try:
        df = _sorted(_index_df())
        close = _numeric_col(df, 'close', '收盘').tail(30)
        if len(close) < 20:
            return 50, "数据不足"
        vol = close.pct_change().std() * np.sqrt(252) * 100
        return _map(vol, 40, 10), f"波动率{vol:.1f}%"
    except Exception as e:
        return 50, str(e)

def _volume():
    """4. 当日量 vs 20日均量"""
    try:
        df = _sorted(_index_df())
        vol = _numeric_col(df, 'volume', '成交量')
        if len(vol) < 22:
            return 50, "数据不足"
        ratio = vol.iloc[-1] / vol.iloc[-21:-1].mean()
        return _map(ratio, 0.5, 2.0), f"量比{ratio:.2f}x"
    except Exception as e:
        return 50, str(e)

def _northbound():
    """5. 近5日北向净流入 (亿)"""
    try:
        data = get_northbound_daily(5)
        if not data:
            return 50, "数据不可用"
        total = sum(d.get("total_yi", 0) for d in data)
        return _map(total, -200, 200), f"5日净流入{total:.0f}亿"
    except Exception as e:
        return 50, str(e)

def _limit_ratio():
    """6. 涨停/跌停比 — 读 scheduler 缓存，不发 HTTP"""
    try:
        from .dragon_limit import _rt_zt_pool, _rt_dt_pool
        up = len(_rt_zt_pool) if _rt_zt_pool else 0
        down = len(_rt_dt_pool) if _rt_dt_pool else 0
        if up + down == 0:
            return 50, "涨跌停数据未加载"
        ratio = up / max(down, 1)
        return _map(ratio, 0, 10), f"涨停{up}/跌停{down}"
    except Exception as e:
        return 50, str(e)

# ── 主函数 ────────────────────────────────────────

_CALC = [
    ("股价动量",     _momentum),
    ("市场宽度",     _breadth),
    ("市场波动率",   _volatility),
    ("成交量变化",   _volume),
    ("北向资金",     _northbound),
    ("涨跌停比",     _limit_ratio),
]


def fear_greed_index():
    """计算综合贪恐指数，返回结构化结果（5分钟缓存）"""
    global _fg_cache, _fg_cache_ts
    now = time.time()
    if _fg_cache is not None and (now - _fg_cache_ts) < _FG_TTL:
        return _fg_cache

    indicators = []
    scores = []
    for name, fn in _CALC:
        score, detail = fn()
        score = round(score, 1)
        scores.append(score)
        indicators.append({"name": name, "score": score, "detail": detail})

    avg = round(float(np.mean(scores)), 1) if scores else 50.0

    result = {
        "timestamp": datetime.now().isoformat(),
        "composite_score": avg,
        "label": _label(avg),
        "indicators": indicators,
    }
    _fg_cache = result
    _fg_cache_ts = now
    return result

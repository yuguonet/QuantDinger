# -*- coding: utf-8 -*-
"""盘中结构三件套守卫（2026-10-23）。

【钉什么】
  ① compute_vwap：HLC3 典型价量加权，与手算一致；approx=true 必须在；
  ② compute_opening_range：只取上午 OR 窗口（标签 ≤ 09:30+minutes），
     不把午后 bar 混进开盘区间；breakout_up/down 与 pos 一致；
  ③ compute_intraday_ma：窗口不足**不给键**（不回落 0）；align 三态；
  ④ get_intraday_structure 契约：count/data/error 同构、单日、source 标注。

【纪律】纯函数 + 注入 bars，不联网、不建库。
"""
from __future__ import annotations

from datetime import datetime, timedelta


def _bars_from(rows):
    """rows: [(hhmm, o, h, l, c, v), ...] → 工具内部 bar 形状。"""
    out = []
    for i, (hm, o, h, l, c, v) in enumerate(rows):
        y, m, d = 2026, 10, 23
        hh, mm = int(hm[:2]), int(hm[3:5])
        ts = int(datetime(y, m, d, hh, mm).timestamp())
        out.append({"time": ts, "open": float(o), "high": float(h),
                    "low": float(l), "close": float(c), "volume": float(v)})
    return out


def _day_bars():
    """构造一个完整上午到下午的 1m 序列（简化：每分钟一根，量固定）。"""
    rows = []
    # 开盘 15 分钟：09:31..09:45，价格从 10.00 爬到 10.20
    for i in range(15):
        p = 10.0 + i * 0.013
        hm = "09:%02d" % (31 + i)
        rows.append((hm, p - 0.01, p + 0.01, p - 0.02, p, 1000))
    # 随后到中午：缓涨到 10.50
    for i in range(45):
        p = 10.2 + i * 0.006
        hm = "09:%02d" % (46 + i) if i < 14 else "10:%02d" % (i - 14)
        rows.append((hm, p, p + 0.01, p - 0.01, p, 1100))
    # 午后继续涨到 10.80
    for i in range(60):
        p = 10.5 + i * 0.005
        hm = "13:%02d" % (1 + i) if i < 59 else "13:59"
        rows.append((hm, p, p + 0.01, p - 0.01, p, 1200))
    return _bars_from(rows)


# ── ① VWAP ───────────────────────────────────────────────────

def test_vwap_matches_hand_calc() -> None:
    from app.agent.tools.finance.intraday_tools import compute_vwap
    # 两根 bar，手算 HLC3×V
    # bar1: typ=(11+9+10)/3=10, v=100 → pv=1000
    # bar2: typ=(12+10+12)/3=34/3, v=300 → pv=3400
    # vwap = 4400/400 = 11.0
    bars = _bars_from([
        ("09:31", 10, 11, 9, 10, 100),
        ("09:32", 10, 12, 10, 12, 300),
    ])
    v = compute_vwap(bars)
    expect = (1000 + (12 + 10 + 12) / 3 * 300) / 400
    assert abs(v["value"] - expect) < 1e-6, (v["value"], expect)
    assert abs(v["value"] - 11.0) < 1e-6
    assert v["approx"] is True and v["method"] == "hlc3"
    assert v["above"] is True          # last=12 > vwap=11


def test_vwap_zero_volume_errors() -> None:
    from app.agent.tools.finance.intraday_tools import compute_vwap
    bars = _bars_from([("09:31", 10, 10, 10, 10, 0)])
    assert "error" in compute_vwap(bars)


# ── ② 开盘区间 ───────────────────────────────────────────────

def test_opening_range_excludes_afternoon() -> None:
    """午后创新高不得污染开盘区间。"""
    from app.agent.tools.finance.intraday_tools import compute_opening_range
    bars = _bars_from([
        ("09:31", 10, 10.2, 9.9, 10.1, 100),
        ("09:40", 10.1, 10.3, 10.0, 10.2, 100),    # OR 内（cutoff 09:45）
        ("09:50", 10.2, 10.25, 10.1, 10.15, 100),  # OR 外
        ("14:00", 10.15, 11.0, 10.1, 10.9, 100),   # 午后暴涨
    ])
    orr = compute_opening_range(bars, 15)
    assert orr["high"] == 10.3, orr          # 09:40，不是 09:50/14:00
    assert orr["low"] == 9.9, orr
    assert orr["breakout_up"] is True        # last=10.9 > 10.3
    assert orr["pos"] == "above"


def test_opening_range_inside_and_down() -> None:
    from app.agent.tools.finance.intraday_tools import compute_opening_range
    bars = _bars_from([
        ("09:31", 10, 10.5, 9.5, 10, 100),
        ("09:45", 10, 10.1, 9.6, 9.7, 100),
        ("10:30", 9.7, 9.8, 9.4, 9.5, 100),
    ])
    orr = compute_opening_range(bars, 15)
    assert orr["high"] == 10.5 and orr["low"] == 9.5
    assert orr["pos"] == "inside"
    assert orr["breakout_up"] is False and orr["breakout_down"] is False

    bars2 = bars[:-1] + _bars_from([("10:30", 9.7, 9.8, 9.0, 9.1, 100)])
    orr2 = compute_opening_range(bars2, 15)
    assert orr2["pos"] == "below" and orr2["breakout_down"] is True


def test_opening_range_invalid_minutes_falls_back() -> None:
    from app.agent.tools.finance.intraday_tools import compute_opening_range, _DEFAULT_OR_MINUTES
    bars = _day_bars()
    orr = compute_opening_range(bars, 7)     # 非法 → 默认 15
    assert orr["minutes"] == _DEFAULT_OR_MINUTES


# ── ③ 分时均线 ───────────────────────────────────────────────

def test_intraday_ma_keys_and_align() -> None:
    from app.agent.tools.finance.intraday_tools import compute_intraday_ma
    # 单边上涨 ⇒ 短 MA > 长 MA ⇒ 多头
    rows = []
    base = datetime(2026, 10, 23, 9, 31)
    for i in range(70):
        t = base + timedelta(minutes=i)
        p = 10 + i * 0.05
        rows.append((t.strftime("%H:%M"), p, p, p, p, 100))
    bars = _bars_from(rows)
    m = compute_intraday_ma(bars, (5, 20, 60))
    assert "ma5" in m and "ma20" in m and "ma60" in m
    assert m["align"] == "多头", m
    assert m["ma5"] > m["ma20"] > m["ma60"]


def test_intraday_ma_short_window_missing_key() -> None:
    from app.agent.tools.finance.intraday_tools import compute_intraday_ma
    bars = _bars_from([("09:31", 10, 10, 10, 10, 100)] * 8)
    m = compute_intraday_ma(bars, (5, 20, 60))
    assert "ma5" in m
    assert "ma20" not in m and "ma60" not in m, "窗口不足不得回落 0：%s" % m


# ── ④ 工具契约（静态）────────────────────────────────────────

def test_tool_schema_and_day_scope() -> None:
    import inspect
    from app.agent.tools.finance.intraday_tools import get_intraday_structure
    from app.agent.tools._schema_gen import func_to_openai_schema

    schema = func_to_openai_schema(get_intraday_structure)
    fn = schema.get("function") or {}
    assert fn.get("name") == "get_intraday_structure"
    desc = fn.get("description") or ""
    assert "VWAP" in desc and "开盘区间" in desc and "分时均线" in desc
    assert "单日" in desc or "一个交易日" in desc
    assert "approx" in desc  # 近似口径必须写进契约

    sig = inspect.signature(get_intraday_structure)
    assert "codes" in sig.parameters
    assert sig.parameters["or_minutes"].default == 15


def test_is_forming_logic() -> None:
    from app.agent.tools.finance import intraday_tools as it
    today = datetime.now().strftime("%Y-%m-%d")
    assert it._is_forming("2020-01-01", "kline_1m") is False
    # 历史日恒 False；今日 + snapshot 合成 = 未走完（15:00 后仍靠 snapshot 也算）
    assert it._is_forming(today, "realtime_snapshot") is True

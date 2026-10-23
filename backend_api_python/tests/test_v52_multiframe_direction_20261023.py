# -*- coding: utf-8 -*-
"""多周期方向工具守卫（2026-10-23）。

【钉什么】
  ① resample_trading：1m→15m/5m 按交易分钟桶聚合，午休不裂开；
  ② frame_direction：几何投票（价/短MA/长MA/动量），全同号才定向；
     窗口不足 → insufficient，不回落 0 分；
  ③ frame_consensus：aligned_*/partial_*/conflict/range/insufficient，
     缺周期写 missing，**不**当中性；
  ④ 工具契约：up/down/range 词汇表 + 无 0-100 分。

【纪律】纯函数 + 注入 bars，不联网、不建库。
"""
from __future__ import annotations

from datetime import datetime, timedelta


def _bars(rows):
    """rows: [(hhmm, o, h, l, c, v), ...] → 工具内部 bar 形状（time=unix ts）。"""
    out = []
    for hm, o, h, l, c, v in rows:
        hh, mm = int(hm[:2]), int(hm[3:5])
        ts = int(datetime(2026, 10, 22, hh, mm).timestamp())
        out.append({"time": ts, "open": float(o), "high": float(h),
                    "low": float(l), "close": float(c), "volume": float(v)})
    return out


def _seq_rows(closes, start_hm=(9, 31)):
    """连续 1m 序列：close=o=h=l，vol=100。"""
    rows = []
    base = datetime(2026, 10, 22, start_hm[0], start_hm[1])
    for i, p in enumerate(closes):
        t = base + timedelta(minutes=i)
        rows.append((t.strftime("%H:%M"), p, p, p, p, 100))
    return rows


# ── ① 聚合 ───────────────────────────────────────────────────

def test_resample_15m_buckets_and_lunch() -> None:
    """09:31 与 09:44 同桶；09:46 落下一桶；午休 11:31 归 11:30 槽。"""
    from app.agent.tools.finance.multiframe_tools import resample_trading, _trading_minute_index
    bars = _bars([
        ("09:31", 10, 10.2, 9.9, 10.1, 100),
        ("09:44", 10.1, 10.3, 10.0, 10.2, 100),
        ("09:46", 10.2, 10.25, 10.1, 10.15, 100),
        ("11:30", 10.15, 10.2, 10.1, 10.18, 50),
        ("11:31", 10.18, 10.18, 10.18, 10.18, 1),  # 午休拍，归 11:30 槽
    ])
    assert _trading_minute_index(bars[0]["time"]) == 0      # 09:31
    assert _trading_minute_index(bars[1]["time"]) == 13     # 09:44
    agg = resample_trading(bars, 15)
    # 桶0: 09:31+09:44；桶1: 09:46；桶7: 11:30+11:31
    assert len(agg) == 3, agg
    assert agg[0]["high"] == 10.3 and agg[0]["low"] == 9.9
    assert agg[0]["volume"] == 200
    assert agg[0]["close"] == 10.2
    assert agg[1]["close"] == 10.15
    assert agg[2]["volume"] == 51


def test_resample_5m_produces_bars() -> None:
    from app.agent.tools.finance.multiframe_tools import resample_trading
    rows = _seq_rows([10 + i * 0.01 for i in range(30)])
    agg = resample_trading(_bars(rows), 5)
    assert len(agg) == 6, len(agg)   # 30 根 1m → 6 根 5m
    assert agg[0]["close"] > agg[0]["open"]


# ── ② 单帧方向 ───────────────────────────────────────────────

def test_frame_direction_up_when_all_agree() -> None:
    from app.agent.tools.finance.multiframe_tools import frame_direction
    closes = [10 + i * 0.1 for i in range(40)]
    f = frame_direction(_bars(_seq_rows(closes)), 5, 20)
    assert f["direction"] == "up", f
    assert f["signals"]["close_vs_ma_short"] == "above"
    assert f["signals"]["ma_align"] == "bullish"
    assert (f["signals"]["mom_pct"] or 0) > 0


def test_frame_direction_insufficient_when_too_few_bars() -> None:
    from app.agent.tools.finance.multiframe_tools import frame_direction
    bars = _bars([("09:31", 10, 10, 10, 10, 100)] * 3)
    f = frame_direction(bars, 5, 20)
    assert f["direction"] == "insufficient"
    assert "reason" in f["signals"]


def test_frame_direction_range_when_signals_split() -> None:
    """价在 MA5 上方但动量为负 → 混合 → range（不硬定方向）。"""
    from app.agent.tools.finance.multiframe_tools import frame_direction
    # 前段平稳 20，随后回落到 ~10 再小幅回升：last>ma5 但 mom5<0
    closes = [20.0] * 10 + [10, 10, 10, 10, 11]
    f = frame_direction(_bars(_seq_rows(closes)), 5, 20)
    assert f["signals"]["close_vs_ma_short"] == "above", f
    assert f["signals"]["mom_pct"] is not None and f["signals"]["mom_pct"] < 0
    assert f["direction"] == "range", f


# ── ③ 共识（日线优先，2026-10-23 用户裁定）──────────────────

def test_consensus_daily_dominates_t1() -> None:
    """日线 up + 分时全 down → t1_bias 仍 up，state 为 timing_weak 而非 conflict。"""
    from app.agent.tools.finance.multiframe_tools import frame_consensus
    c = frame_consensus({"daily": "up", "m15": "down", "m5": "down"})
    assert c["t1_bias"] == "up", c
    assert c["timing"] == "weak", c
    assert c["state"] == "t1_up_timing_weak", c
    assert "conflict" not in c["state"]


def test_consensus_aligned_when_minutes_agree() -> None:
    from app.agent.tools.finance.multiframe_tools import frame_consensus
    a = frame_consensus({"daily": "up", "m15": "up", "m5": "up"})
    assert a["state"] == "aligned_up" and a["t1_bias"] == "up" and a["timing"] == "strong"

    d = frame_consensus({"daily": "down", "m15": "down", "m5": "down"})
    assert d["state"] == "aligned_down" and d["t1_bias"] == "down"


def test_consensus_mixed_and_solo() -> None:
    from app.agent.tools.finance.multiframe_tools import frame_consensus
    m = frame_consensus({"daily": "up", "m15": "down", "m5": "up"})
    assert m["t1_bias"] == "up" and m["timing"] == "mixed"
    assert m["state"] == "t1_up_timing_mixed", m

    s = frame_consensus({"daily": "up", "m15": "insufficient", "m5": None})
    assert s["t1_bias"] == "up" and s["timing"] == "n/a"
    assert s["state"] == "t1_up_solo", s
    assert set(s["missing"]) == {"m15", "m5"}

    # 无日线时降级 m15 为 t1_bias
    n = frame_consensus({"daily": "insufficient", "m15": "down", "m5": "up"})
    assert n["t1_bias"] == "down" and n["timing"] == "mixed"
    assert n["state"] == "t1_down_timing_mixed", n


def test_consensus_range_and_insufficient() -> None:
    from app.agent.tools.finance.multiframe_tools import frame_consensus
    r = frame_consensus({"daily": "range", "m15": "range", "m5": "range"})
    assert r["state"] == "range" and r["t1_bias"] == "range"

    f = frame_consensus({"daily": "insufficient", "m15": "insufficient", "m5": "insufficient"})
    assert f["state"] == "insufficient" and f["t1_bias"] == "insufficient"


# ── ④ 工具契约 ───────────────────────────────────────────────

def test_tool_schema_vocabulary() -> None:
    import inspect
    from app.agent.tools.finance.multiframe_tools import get_multiframe_direction
    from app.agent.tools._schema_gen import func_to_openai_schema

    schema = func_to_openai_schema(get_multiframe_direction)
    fn = schema.get("function") or {}
    assert fn.get("name") == "get_multiframe_direction"
    desc = fn.get("description") or ""
    assert "日线" in desc and "15m" in desc and "5m" in desc
    assert "t1_bias" in desc and "timing" in desc
    assert "up/down/range" in desc or "up/down" in desc
    # 明确禁止评分语义 + 日线优先
    assert "不是评分" in desc or "非评分" in desc or "不是涨跌概率" in desc
    assert "日线" in desc and ("优先" in desc or "权重" in desc)
    # 大盘晴雨表
    assert "true_strong" in desc or "真强" in desc

    sig = inspect.signature(get_multiframe_direction)
    assert sig.parameters["daily_bars"].default == 60


# ── ⑤ 大盘相对强弱（晴雨表修正）──────────────────────────────

def test_market_relative_true_strong_and_true_weak() -> None:
    from app.agent.tools.finance.multiframe_tools import market_relative_view
    # 大盘弱 + 个股强 = 真强 → 弱转强 boost
    r = market_relative_view({"direction": "up", "change_pct": 2.0},
                             {"direction": "down", "change_pct": -1.0})
    assert r["label"] == "true_strong", r
    assert r["weak_to_strong_adj"] == "boost"
    assert r["divergence_adj"] == "neutral"

    # 大盘强 + 个股弱 = 真弱 → 强转分歧 heavier
    w = market_relative_view({"direction": "down", "change_pct": -2.0},
                             {"direction": "up", "change_pct": 1.0})
    assert w["label"] == "true_weak", w
    assert w["divergence_adj"] == "heavier"
    assert w["weak_to_strong_adj"] == "neutral"


def test_market_relative_carried_and_dragged() -> None:
    from app.agent.tools.finance.multiframe_tools import market_relative_view
    c = market_relative_view({"direction": "up", "change_pct": 2.0},
                             {"direction": "up", "change_pct": 1.5})
    assert c["label"] == "market_carried" and c["weak_to_strong_adj"] == "discount"

    d = market_relative_view({"direction": "down", "change_pct": -1.5},
                             {"direction": "down", "change_pct": -1.2})
    assert d["label"] == "market_dragged" and d["divergence_adj"] == "lighter"


def test_market_relative_insufficient() -> None:
    from app.agent.tools.finance.multiframe_tools import market_relative_view
    r = market_relative_view({}, {})
    assert r["label"] == "insufficient"
    # ★ 修正注记定位：role 恒 correction，不得当主论据（2026-10-23）
    assert r.get("role") == "correction"


def test_market_relative_role_is_correction_not_decisive() -> None:
    """rel_strength 只许轻微修正措辞，不得单独翻多空（字段层钉 role）。"""
    from app.agent.tools.finance.multiframe_tools import market_relative_view
    for st, mk in (
        ({"direction": "up", "change_pct": 2.0}, {"direction": "down", "change_pct": -1.0}),
        ({"direction": "down", "change_pct": -2.0}, {"direction": "up", "change_pct": 1.0}),
    ):
        r = market_relative_view(st, mk)
        assert r["role"] == "correction", r
        # adj 只在 boost/discount/heavier/lighter/neutral 词表内
        assert r["weak_to_strong_adj"] in ("boost", "discount", "neutral")
        assert r["divergence_adj"] in ("heavier", "lighter", "neutral")

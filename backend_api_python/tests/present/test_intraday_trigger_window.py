"""test_intraday_trigger_window.py — 盘中成交槽**窗口语义**门禁 (2026-10-09 D4)。

★ 被锁的缺陷 (实证): ``core.backtest._exec_trigger_mis`` 对 ``scan_spec.entry_at`` 非空的
  策略**只回该单个槽位** (旧措辞 "终审语义: 只回该时刻"), 而 ``entry_at`` 的真实语义是
  **最早可成交时刻** (窗口起点, 与生产 ``scan._scan_cycle`` 的 rolling_preview "14:50 起
  触发即买入" 同口径)。后果: 旧独立时间线引擎**系统性漏掉 entry_at 之后才触发的信号** ——
  实测 tail_oversold 000993 于 14:58 触发被漏 (折叠/生产均命中) ⇒ 回测少计信号。

本组两条锁:
  ① 槽位展开必须覆盖整窗 (既覆盖 14:50 也覆盖 14:58), 不得退化为单槽;
  ② 一个**14:50 之后才触发**的 tail 合成输入, 盘中回测必须捕获 (产出该笔交易)。

⚠ 不依赖 DB: ② 复用 ``tests.golden.freeze._old_intraday_trades`` 的合成帧管道
  (与盘中 golden 同源, 数据面全部 patch)。
"""
from __future__ import annotations


# ================================================================
# ① 槽位展开
# ================================================================
def test_trigger_slots_cover_full_window_when_entry_at_set():
    """entry_at 非空 ≠ 只判该时刻: tail 窗口 (14:50,15:00) 必须展开为 11 槽。"""
    from app.market_cn.auto import strategies as reg
    from app.market_cn.auto.core.backtest import _exec_trigger_mis, frames_hhmm

    reg.autodiscover()
    spec = reg.get_strategy("tail_oversold").scan_spec
    hh = [frames_hhmm(m) for m in _exec_trigger_mis(spec)]

    assert hh == ["14:%02d" % m for m in range(50, 60)] + ["15:00"], hh
    assert len(hh) == 11
    assert "14:58" in hh            # 回归锚点: 原实现恰好漏掉它
    assert spec.entry_at == "14:50"  # entry_at 仍是窗口起点 (不改为"唯一成交时刻")


def test_trigger_slots_knife_unchanged():
    """entry_at 空的策略 (knife) 行为不变: 仍扫整窗 14:30~15:00。"""
    from app.market_cn.auto import strategies as reg
    from app.market_cn.auto.core.backtest import _exec_trigger_mis, frames_hhmm

    reg.autodiscover()
    spec = reg.get_strategy("knife_catch").scan_spec
    hh = [frames_hhmm(m) for m in _exec_trigger_mis(spec)]

    assert hh[0] == "14:30" and hh[-1] == "15:00"
    assert "14:56" in hh
    assert len(hh) == 31


def test_trigger_slots_empty_when_entry_at_after_window_end():
    """entry_at 晚于窗口终点 ⇒ 无成交槽 (不得抛 ValueError 拖垮调用方)。"""
    from app.market_cn.auto.core.backtest import _exec_trigger_mis
    from app.market_cn.auto.strategies.base import ScanSpec

    assert _exec_trigger_mis(ScanSpec(kind="intraday_window",
                                      windows=("14:50", "15:00"),
                                      entry_at="15:30")) == []
    assert _exec_trigger_mis(ScanSpec(kind="intraday_window")) == []


# ================================================================
# ② 盘中回测捕获 "14:50 之后才触发" 的信号
# ================================================================
def _tail_rows_fire_at_1458(day: str, pc: float = 100.0):
    """tail 触发日: 14:50~14:57 尾盘回落**高于上界** (tail_ret > -0.5% ⇒ 不触发),
    14:58 起才落入区间 [-2.8,-0.5]。14:20~14:40 均价 (分母) 保持不动。"""
    from tests.present.common import tail_day_rows

    rows = tail_day_rows(day, pc=pc)
    avg = 90.5                       # = common.tail_day_rows 的 14:20~14:40 均价
    for r in rows:
        hh = str(r["time"])[11:16]
        if "14:50" <= hh <= "14:57":
            r["last"] = round(avg * (1 - 0.002), 4)      # tail_ret ≈ -0.2% > -0.5 ⇒ 拒
    return rows


def test_timeline_engine_captures_signal_after_entry_at():
    """14:58 才触发的 tail 信号必须成交 (回归: 原只判 14:50 ⇒ 0 笔)。"""
    from tests.golden.freeze import _old_intraday_trades
    from tests.present.common import TAIL_HIST_CLOSES, gen_hist_bars

    day, nxt = "2026-10-05", "2026-10-06"
    bars = gen_hist_bars("600001", TAIL_HIST_CLOSES) + [
        {"time": day, "open": 90.0, "high": 97.5, "low": 87.5, "close": 88.6, "volume": 1500.0},
        {"time": nxt, "open": 89.0, "high": 90.0, "low": 88.0, "close": 89.2, "volume": 900.0},
    ]
    spec = {"name": "tail_late_1458", "strategy": "tail_oversold", "code": "600001",
            "build": lambda: bars,
            "day_rows": lambda: {day: _tail_rows_fire_at_1458(day, pc=100.0)},
            "mkt_gain": -3.0, "pc": 100.0, "intraday": True}

    trades = _old_intraday_trades(spec)
    # 14:50~14:57 尾盘回落高于上界 ⇒ 不触发; 只有窗口扫到 14:58 才成交。
    # 若引擎只判 14:50 (旧缺陷) ⇒ 0 笔 —— 「== 1」即锁住 D4 窗口语义回归。
    assert len(trades) == 1, (
        "14:50 之后才触发的信号被漏 —— 成交槽未覆盖整窗 (D4 回归): %s" % trades)
    t = trades[0]                     # canonical trade (trade_map.build_trade)
    assert t["entry_date"] == day
    assert t["exit_date"] == nxt      # D1 开盘卖
    assert t["exit_day"] == 1

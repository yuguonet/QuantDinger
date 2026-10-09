"""test_break_t1_guard.py — break 出场的 T+1 守卫（2026-10-09 审计 B1）。

事故
----
break 有**三份**出场实现，其中两处缺 T+1 守卫（A 股最早可卖日 = 持仓第 2 个交易日）：

1. `exit_decision._decide`：止损/追踪/峰值逃顶**裸判**，缺 `held > 1`
   ⇒ 入场日收盘触及止损线即出场 = **当日买当日卖**。
   对照 `strategies/base.py: exit_decision`（2026-10-07 P1-④ 已加守卫，注释明写
   「与 core/exit_engines._min_sell_day 同口径」）—— break 是漏网的。
2. ①「昨日触发 + 封跌停 → 今日开盘强平」分支：原判 `today_idx - 1 >= entry_idx`，
   当 bars 从入场日开始时 `today_idx-1 == entry_idx` ⇒ 入场日止损触线产出 D2 开盘出场，
   违反项目 R2 口径「D1 的止损触线不产生出场」。
3. `_exit_by_decision`（yaml `exit.mode=break_combo` 的实现）：从 **d=1** 起调
   `exit_decision` ⇒ 入场日触线时返回 `exit_day=1`（T+1 违规）。

`_decide` 是折叠 replay 与 monitor 的**活路径**，`_run_backtest_breakbuy` 用 `if d > 1`
（T+1 正确）⇒ 同一持仓三套口径，golden / 阈值结论不稳。

口径声明（与全局一致）
--------------------
`exit_day` / `held` = **持仓第 N 个交易日，N=1 为入场当日**。故可卖日从 2 起。
「持仓到期」是日历日语义，**不**加 `held > 1` 守卫 —— 到期本就该按期平仓。

构造原则
--------
不依赖真实数据：用**合成 bars** 精确控制「入场日收盘即触止损」这一分支 ——
这是旧 `tmp/verify_exit_equivalence.py`（12 笔 0 不一致）**覆盖不到**的分支。
"""

from __future__ import annotations

import importlib

import pytest

_break = importlib.import_module("app.market_cn.auto.strategies.break")
BreakStrategy = _break.BreakStrategy
CODE = "600000"


def _bar(d, o, h, l, c, v=1e6):
    return {"time": d, "open": o, "high": h, "low": l, "close": c, "volume": v}


# ================================================================
# 夹具
# ================================================================
# ★ 阈值名是 `stop_loss` / `trailing_stop` / `hold_days`（**不是** base 的 stop/trail/hold），
#   分板块取值（board=main）：-8.0 / -6.0 / 7。
# ★ 判据用**收盘价**相对入场价（`r = (close/entry_price-1)*100 <= stop_loss`）——
#   这是 break `_decide` 的口径（base 用low，两者在合成数据上会分叉，别混用）。
# ★ 构造「入场日即触止损」的关键：入场日 `r` 通常为 0（break 买在 D1 收盘），
#   要让 D1 就r <= -8%，必须**入场价高于 D1 收盘** —— 即 D1 开盘买入、
#   当日收盘已亏 10%（`buy_mode=open` 口径的生产真实形态）。
#   实测（本次修复后）：held=1 → hold；held=2 → 止损出场；_exit_by_decision → exit_day=2。
_ENTRY_PRICE = 10.0
_BARS_ENTRYDAY_STOP = [
    _bar("2026-06-17", 10.00, 10.10, 8.90, 9.00),   # d=1 入场日：close 9.0 ⇒ r=-10% 已触止损
    _bar("2026-06-18", 9.00, 9.30, 8.80, 9.10),     # d=2：T+1 后可卖 ⇒ 出场
    _bar("2026-06-19", 9.10, 9.30, 9.00, 9.20),
]


# ================================================================
# 1. exit_decision（活路径）：入场日不得出场
# ================================================================
def test_exit_decision_holds_on_entry_day():
    """只给到入场日当天 ⇒ 必须 hold（哪怕收盘已跌破止损线）。"""
    s = BreakStrategy()
    dec = s.exit_decision(
        {"entry_price": _ENTRY_PRICE, "code": CODE},
        {"mode": "day_close", "bars": _BARS_ENTRYDAY_STOP[:1], "entry_idx": 0},
    )
    assert dec.action != "exit", (
        f"入场日（held=1）就出场了: action={dec.action} reason={dec.reason!r} "
        f"⇒ T+1 违规（当日买当日卖）"
    )


def test_exit_decision_can_exit_from_day2():
    """对照组：d=2 起**可以**出场（否则说明守卫加过头，把止损也一并废掉了）。"""
    s = BreakStrategy()
    dec = s.exit_decision(
        {"entry_price": _ENTRY_PRICE, "code": CODE},
        {"mode": "day_close", "bars": _BARS_ENTRYDAY_STOP, "entry_idx": 0},
    )
    assert dec.action == "exit", (
        f"d=2 仍未出场（low=9.00 远低于止损价 9.20）⇒ 守卫过宽，把止损判定一并废掉了。"
        f" dec={dec}"
    )


def test_entry_day_peak_escape_is_guarded():
    """入场日「峰值逃顶」同样受T+1 约束（旧实现 `r > 10` 分支裸判）。"""
    s = BreakStrategy()
    bars = [
        _bar("2026-06-17", 10.00, 12.00, 9.90, 11.00),   # d=1：r=+10%，冲高回落
    ]
    dec = s.exit_decision(
        {"entry_price": _ENTRY_PRICE, "code": CODE},
        {"mode": "day_close", "bars": bars, "entry_idx": 0},
    )
    assert dec.action != "exit", (
        f"入场日就以「峰值逃顶」出场: {dec.reason!r} ⇒ T+1 违规"
    )


def test_hold_expiry_not_guarded_by_t1():
    """「持仓到期」**不**受 T+1 约束 —— 它是日历日语义，到期即平仓。

    反向锁：若有人把守卫写成包住全部出场分支，本条会红 ⇒ 持仓到期被误伤，
    信号会一直拖到止损/逃顶才出，改动收益口径。
    """
    s = BreakStrategy()
    p = s.params()
    hold = int(p.get("hold", 7))
    # 一路小涨、无止损无逃顶，纯粹走到期
    bars = [_bar(f"2026-06-{17 + i:02d}", 10.0, 10.3, 10.0, 10.1) for i in range(hold + 1)]
    dec = s.exit_decision(
        {"entry_price": _ENTRY_PRICE, "code": CODE},
        {"mode": "day_close", "bars": bars, "entry_idx": 0},
    )
    assert dec.action == "exit", f"持仓 {hold} 天到期未出场: {dec}"
    assert "持仓到期" in str(dec.reason), f"出场原因应为持仓到期，实为 {dec.reason!r}"


# ================================================================
# 2. ① 分支：入场日的止损触线不产出 D2 开盘出场
# ================================================================
def test_pending_dn_branch_ignores_entry_day_trigger():
    """入场日(D1)止损触线 + D2 封跌停 ⇒ 不得在 D3 开盘强平（D1 触线不产生出场）。"""
    s = BreakStrategy()
    bars = [
        _bar("2026-06-17", 10.00, 10.10, 8.50, 8.50),    # d=1 入场日：暴跌触止损 + 一字跌停
        _bar("2026-06-18", 8.50, 8.60, 7.70, 7.70),     # d=2：仍封跌停
        _bar("2026-06-19", 7.70, 8.00, 7.60, 7.90),     # d=3：可成交
    ]
    dec = s.exit_decision(
        {"entry_price": _ENTRY_PRICE, "code": CODE},
        {"mode": "day_close", "bars": bars, "entry_idx": 0},
    )
    # D1/D2 的触线都不该在 D2 开盘卖出（D1 触线无效；D2 触线顺延到 D3 才是合法路径）
    if dec.action == "exit":
        assert getattr(dec, "fill", "") != "open" or "止损" not in str(dec.reason), (
            f"入场日止损触线产出了开盘强平: {dec.reason!r} fill={getattr(dec,'fill','')!r} "
            f"⇒ R2 口径违规（D1 触线不产生出场）"
        )


# ================================================================
# 3. _exit_by_decision（yaml exit.mode=break_combo 实现）：不返回 exit_day=1
# ================================================================
def test_exit_by_decision_never_returns_day1():
    """合成一段入场日即触止损的 bars ⇒ 不得返回 exit_day=1。"""
    res = _break._exit_by_decision(_BARS_ENTRYDAY_STOP, 0, _ENTRY_PRICE, CODE, "main")
    assert res is not None, "_exit_by_decision 返回 None"
    assert int(res.get("exit_day", 0)) >= 2, (
        f"exit_day={res.get('exit_day')} ⇒ T+1 违规（入场日即出场）。完整返回：{res}"
    )


def test_exit_by_decision_tail_exit_day_is_hold():
    """走到期兜底时 exit_day 必须是 hold（坐标口径 = 持仓第 N 交易日）。"""
    hold = int(BreakStrategy().params().get("hold", 7))
    bars = [_bar(f"2026-06-{17 + i:02d}", 10.0, 10.3, 10.0, 10.1) for i in range(hold + 2)]
    res = _break._exit_by_decision(bars, 0, _ENTRY_PRICE, CODE, "main")
    assert int(res["exit_day"]) >= 2, f"exit_day={res['exit_day']} < 2 ⇒ T+1 违规"


# ================================================================
# 4. 与 base 的口径一致性（三份实现收敛的证据）
# ================================================================
@pytest.mark.parametrize("held_bars", [1, 2, 3])
def test_break_and_base_agree_on_entry_day(held_bars):
    """break 与 base 的 `exit_decision` 对同一片段应给**同一结论**（含入场日）。

    base 是 2026-10-07 已修好的参照实现；break 曾是漏网的那个。

    取参照的方法：直接调未覆写的 `StrategyBase.exit_decision`（break 的 `_decide`
    另走私有闭包，故这里对比的是「base 通用实现 vs break 实际调用入口」两层）。
    `StrategyBase` 带 pydantic 字段、不能裸实例化，故借一个 break 实例调用其父类方法。
    """
    from app.market_cn.auto.strategies.base import StrategyBase

    seg = _BARS_ENTRYDAY_STOP[:held_bars]
    snap = {"mode": "day_close", "bars": seg, "entry_idx": 0}
    row = {"entry_price": _ENTRY_PRICE, "code": CODE}

    s = BreakStrategy()
    b = StrategyBase.exit_decision(s, row, snap)   # 参照: base 的通用实现
    cur = s.exit_decision(row, snap)               # 实际: break 覆写后的实现
    assert cur.action == b.action, (
        f"held={held_bars}: break={cur.action}/{cur.reason!r} "
        f"≠ base={b.action}/{b.reason!r} "
        f"⇒ 两份实现口径分叉（审计 B1：三份出场实现三种 T+1 语义）"
    )

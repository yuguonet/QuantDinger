"""test_exit_rollover_real_behavior.py — 出场顺延路径的**现状特征锁**（审计 core B3-2 复核）。

本文件记录的是**实测现状**，不是「应该怎样」。目的是：
1. 任何人改动出场顺延路径时，立刻看到行为差异（而不是静默改了收益）；
2. 为后续专项修复 B3-2 提供**可对比的基线数据**。

审计结论的修正（重要）
----------------------
`audit_core.md` B3-2 断言「尾块 `nxt = entry_idx + exit_d + 1` 多一个 +1 ⇒ 跳过
一个交易日」，并称 `run_trail_stop` 靠「每日写到期不 break」与它**互相抵消**。

**实测复核结论：该诊断不成立，本次不修。** 证据（hold_days=3、entry_idx=0、
到期日 = idx2、构造一字跌停）：

| 场景 | run_hold_stop | run_trail_stop |
|------|---------------|----------------|
| 跌停在 idx1（非到期日） | `exit_day=3, price=8.0` | `exit_day=3, price=8.0` |
| 跌停在 idx2（**到期日**） | `exit_day=2, price=10.0` ⚠ | `exit_day=4, price=8.0` |
| 无跌停 | `exit_day=3, price=10.0` | `exit_day=3, price=10.0` |

- `run_trail_stop` 在**所有**场景下输出自洽（`nxt = entry_idx + exit_d + 1` 是正确的，
  因为 `d` 的坐标是「持仓第 d 交易日」⇒ 封死日索引 `= entry_idx + d - 1`，
  次一交易日 `= entry_idx + d` … 尾块多 +1 是为了跳到封死日**之后**的下一个可成交日，
  与 trail_stop 循环内 `pending_dn` 分支的 `d` 已经自增语义配套）。
- `run_hold_stop` 在「到期日封跌停」场景下确实异常（`exit_day=2 / price=10.0`，
  即在到期日之前就按开盘价平仓），但**成因不是 `+1` 公式**：
  到期判定位于循环**之后**，而「一字跌停 / 成交贴跌停」分支只置 `pending_dn`、**未记 `exit_d`**
  ⇒ 循环结束时 `exit_d` 仍为 0 ⇒ 尾块起点退化成 `entry_idx + 1`。
  已实测：仅补`exit_d = d` 会得到 `exit_day=5 / price=7.0`（**仍不对**，因为到期分支
  随后又把 `exit_d` 覆盖为 `hold_days`，与尾块公式再次错位）。
  ⇒ 该缺陷牵涉 `exit_d` 三处赋值点与两个引擎的**不同结构**（hold_stop 到期在循环后、
  trail_stop 到期在循环内），**不是一处最小修**，需专项复核后再动。

因此本文件只锁现状，不锁「期望值」。若将来专项修复 B3-2，本文件应被
「期望行为」测试替换，并把这里的差异作为验收依据。
"""

from __future__ import annotations

import pytest

from app.market_cn.auto.core import exit_engines as ee


def _bar(d, o, h, l, c, v=1e6):
    return {"time": d, "open": o, "high": h, "low": l, "close": c, "volume": v}


def _flat(n=15, start="2026-06-17", px=10.0):
    return [_bar(f"{start[:8]}{int(start[8:]) + i:02d}", px, px * 1.01, px * 0.99, px)
            for i in range(n)]


def _with_one_word_dn(dn_idx, n=15):
    """把 bars[dn_idx] 造成一字跌停（按前一日收盘 -10%），并让其后两日可成交。"""
    b = _flat(n)
    prev = float(b[dn_idx - 1]["close"])
    dn = round(prev * 0.9, 2)
    b[dn_idx] = _bar(b[dn_idx]["time"], dn, dn, dn, dn)
    for i, px in ((dn_idx + 1, 8.0), (dn_idx + 2, 7.0)):
        b[i]["open"] = px
        b[i]["high"] = px + 0.2
        b[i]["low"] = px - 0.1
        b[i]["close"] = px + 0.1
    return b


HOLD_DAYS = 3          # ⇒ 到期日 idx = 2（entry_idx=0）
HOLD_KW = dict(hold_days=HOLD_DAYS, stop_loss=-100.0, with_reason=True)
TRAIL_KW = dict(hold_days=HOLD_DAYS, stop_loss=-100.0, trailing_stop=-100.0,
                with_reason=True)


# ================================================================
# 现状快照（改动即红 —— 这正是本文件的目的）
# ================================================================
def test_hold_stop_no_dn_expiry():
    out = ee.run_hold_stop(_flat(), 0, 10.0, **HOLD_KW)
    assert (out["exit_day"], out["exit_price"], out["exit_reason"]) == \
           (HOLD_DAYS, 10.0, "持仓到期")


def test_trail_stop_no_dn_expiry():
    out = ee.run_trail_stop(_flat(), 0, 10.0, **TRAIL_KW)
    assert (out["exit_day"], out["exit_price"], out["exit_reason"]) == \
           (HOLD_DAYS, 10.0, "持仓到期")


def test_hold_stop_dn_before_expiry():
    """跌停在 idx1（非到期日）⇒ 顺延到 idx2 开盘，exit_day=3（现状正确）。"""
    out = ee.run_hold_stop(_with_one_word_dn(1), 0, 10.0, **HOLD_KW)
    assert (out["exit_day"], out["exit_price"]) == (3, 8.0)


def test_trail_stop_dn_before_expiry():
    out = ee.run_trail_stop(_with_one_word_dn(1), 0, 10.0, **TRAIL_KW)
    assert (out["exit_day"], out["exit_price"]) == (3, 8.0)


def test_trail_stop_dn_on_expiry_day():
    """跌停恰在到期日(idx2) ⇒ trail_stop 输出自洽（exit_day=4 / 顺延日 idx3 开盘）。"""
    out = ee.run_trail_stop(_with_one_word_dn(2), 0, 10.0, **TRAIL_KW)
    assert (out["exit_day"], out["exit_price"]) == (4, 8.0), (
        f"trail_stop 现状变了: {out} —— 若这是有意修复，请同步更新本文件与"
        f"docs/口径差异报告.md（审计 core B3-2）"
    )


def test_hold_stop_dn_on_expiry_day_is_known_anomaly():
    """⚠ **已知异常现状**（审计 core B3-2，本次不修，见模块头）。

    「到期日封跌停」时 `run_hold_stop` 在到期日**之前**就平仓：
    `exit_day=2 / price=10.0`（idx1 开盘），而合理值应是 `exit_day=4 / price=8.0`
    （idx3 开盘）。

    本条锁住**异常现状**而不是期望值 —— 这样修复一旦发生，本条会红，
    强制执行者更新文档与口径记录，不会静默改掉收益统计。
    若改成了 `exit_day=4 / price=8.0`，请把本条改为断言期望值并删掉本段警告。
    """
    out = ee.run_hold_stop(_with_one_word_dn(2), 0, 10.0, **HOLD_KW)
    assert (out["exit_day"], out["exit_price"]) == (2, 10.0), (
        f"hold_stop 的已知异常已变化: {out}。"
        f"若已修复，请改为断言 (4, 8.0) 并在 docs/口径差异报告.md 登记"
    )


# ================================================================
# 两引擎一致性现状（有差异，不是本文件要修的东西，只做记录）
# ================================================================
@pytest.mark.parametrize("dn_idx", [1, 2])
def test_two_engines_differ_only_on_expiry_day_dn(dn_idx):
    """记录两引擎在「跌停恰逢到期日」时的**分歧**（现状，非期望）。

    非到期日跌停时两者一致；到期日跌停时分歧 ⇒ B3-2 的真实病灶范围。
    """
    h = ee.run_hold_stop(_with_one_word_dn(dn_idx), 0, 10.0, **HOLD_KW)
    t = ee.run_trail_stop(_with_one_word_dn(dn_idx), 0, 10.0, **TRAIL_KW)
    if dn_idx == 1:
        assert (h["exit_day"], h["exit_price"]) == (t["exit_day"], t["exit_price"])
    else:
        assert (h["exit_day"], h["exit_price"]) != (t["exit_day"], t["exit_price"]), (
            "两引擎在到期日跌停场景已一致 ⇒ B3-2 可能已修复，请复核并更新本文件"
        )

"""test_exit_rollover_real_behavior.py — 出场顺延路径的行为锁（审计 core B3-2 / P1-3 专项）。

本文件记录实测行为，任何人改动出场顺延路径时会立刻看到行为差异（而不是静默改了收益）。

审计结论的修正（重要）
----------------------
`audit_core.md` B3-2 断言「尾块 `nxt = entry_idx + exit_d + 1` 多一个 +1 ⇒ 跳过
一个交易日」，并称 `run_trail_stop` 靠「每日写到期不 break」与它**互相抵消**。

**实测复核结论：该诊断对 `run_trail_stop` 不成立**（`+1` 与它的逐日占位坐标自洽）；
真病灶是 `run_hold_stop` 的**阻塞日换算**（`exit_d` 三处赋值点结构）。证据
（hold_days=3、entry_idx=0、到期日 = idx2、构造一字跌停）：

| 场景 | run_hold_stop（修复前） | run_hold_stop（2026-10-10 专项后） | run_trail_stop（不变） |
|------|---------------|----------------|----------------|
| 跌停在 idx1（非到期日） | `exit_day=3, price=8.0` | `exit_day=3, price=8.0` | `exit_day=3, price=8.0` |
| 跌停在 idx2（**到期日**） | `exit_day=2, price=10.0` ⚠ | **`exit_day=4, price=8.0`** ✓ | `exit_day=4, price=8.0` |
| 无跌停 | `exit_day=3, price=10.0` | `exit_day=3, price=10.0` | `exit_day=3, price=10.0` |

P1-3 专项修复（2026-10-10，`exit_engines.run_hold_stop` 尾块）
----------------------------------------------------------------
- 成因：本引擎**无逐日占位**（与 trail 不同），loop 尾阻塞时 `exit_d` 恒为 0 ⇒
  旧公式 `nxt = entry_idx + exit_d + 1` 退化成 `entry_idx + 1`，在阻塞日**之前**
  就平仓（已知异常 `exit_day=2/price=10.0`）；而到期块分支（`exit_d=hold_days`）
  同一公式又会多跳一天 —— 这就是「不是一处最小修」的三赋值点结构问题。
- 修法：尾块起点统一改为 `nxt = entry_idx + hold_days`（= 阻塞日次日；带 pending
  走到尾块的阻塞日只可能是到期日，论证见 `run_hold_stop` 尾块注释）。
- `run_trail_stop` **零改动**（其 `+1` 与逐日占位坐标自洽，修复未触碰它）。
- 影响面：仅「到期日封跌停」分支的 g56 回测笔会变（出场日 2→4、价格 10.0→8.0），
  已在 `docs/口径差异报告.md` 登记；其余场景与 trail 输出逐位不变。
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


def test_hold_stop_dn_on_expiry_day():
    """「到期日封跌停」⇒ 顺延到到期日次一交易日开盘卖出（P1-3 专项后期望行为）。

    修复前是已知异常 `exit_day=2 / price=10.0`（在阻塞日之前平仓），
    与 `run_trail_stop` 同场景输出 (4, 8.0) 分歧。2026-10-10 专项修正后
    两引擎一致；本条即专项验收断言（见模块头论证）。
    """
    out = ee.run_hold_stop(_with_one_word_dn(2), 0, 10.0, **HOLD_KW)
    assert (out["exit_day"], out["exit_price"]) == (4, 8.0), (
        f"hold_stop 到期日封跌停行为回退: {out}。"
        f"期望顺延到次一交易日 (4, 8.0) —— 若有意变更口径, "
        f"请同步 docs/口径差异报告.md 并复核 run_trail_stop 同场景"
    )
    assert out["exit_reason"] == "跌停顺延开盘"


# ================================================================
# 两引擎一致性（2026-10-10 P1-3 专项后: 全场景一致）
# ================================================================
@pytest.mark.parametrize("dn_idx", [1, 2])
def test_two_engines_agree_on_all_dn_scenarios(dn_idx):
    """两引擎在跌停顺延场景输出一致（专项修复后的**期望**，替代修复前的分歧记录）。

    修复前 dn_idx=2（到期日）时两者分歧（hold=2/10.0 vs trail=4/8.0）；
    现在必须逐位一致 —— 若再分歧，说明顺延坐标又改坏了。
    """
    h = ee.run_hold_stop(_with_one_word_dn(dn_idx), 0, 10.0, **HOLD_KW)
    t = ee.run_trail_stop(_with_one_word_dn(dn_idx), 0, 10.0, **TRAIL_KW)
    assert (h["exit_day"], h["exit_price"]) == (t["exit_day"], t["exit_price"]), (
        f"两引擎在 dn_idx={dn_idx} 场景分歧: hold={h} trail={t} —— "
        f"顺延坐标必须同口径（P1-3 专项验收）"
    )

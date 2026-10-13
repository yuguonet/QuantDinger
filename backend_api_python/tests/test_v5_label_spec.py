"""v5 P1-1 收口：`chain/label_spec.py` 是标签口径的**唯一真源**（TDD，2026-10-13）

背景（**已查证，勿重复查**）：口径曾在**三处**各写一份，且**数值不同**：
    · `chain/evaluator.py` `_TIMEFRAME_DAYS`  —— 1M=**22** / 3M=66 / 1Y=252
    · `chain/evaluator.py` `HOLD_LABELS`      —— 1M=**20**
    · `chain/claims.py`    `_HORIZON_DAYS`    —— 又一份 1M=20（含 `n/a: 0`）

## ★ 2026-10-13 收口：两组口径已**统一**（此前"故意并存"的判断是错的）

旧判断：`TRACE_HOLDS`（1M=22）是"自然月口径"、与 claim 的"交易日口径"语义不同，
        故不许合并。**已查证推翻**：
  1. 消费端按**交易日**解释 —— `evaluator._maturity_days` 注释原文「hold_days 按
     交易日计（_TIMEFRAME_DAYS）」，且 `_get_actual_return` 用 `base_idx + hold_days`
     偏移 **K 线根数** ⇒ 两者是**同一个量**。
  2. `22` 与表内锚点不自洽：22×12 = 264 > `1Y=252`（一年没有 264 个交易日）。
  3. **影响面实测为零**：`qd_agent_traces` 根节点 1256 条，`timeframe` **恒为空**
     （恒走默认 3）⇒ 22/66/252 从未被消费；`qd_agent_claims` 128 条里仅 3 条 1M。

⇒ 统一为**一张表 `HOLDS`**：1M=21 / 3M=63（= `1Y=252` 的 1/12 与 1/4）。
   自洽性由 `test_long_horizons_are_self_consistent` 钉死 —— 改一个必须改全部。

易错处：
  · `HOLD_LABELS` 反推必须**剔除 0**（`n/a: 0`），否则 `hold_label(0)` 返回 "n/a"。
  · `HOLD_LABELS` 必须**先到先得**：`T+5` 与 `1W` 同为 5 天，若用后覆盖写法
    `hold_label(5)` 会静默变成 "1W"（已由 `test_hold_label_roundtrip` 钉死）。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.agent.chain import label_spec as ls


# ═══════════════════════════════════════════════════════════════
#  口径内容：只有一张表
# ═══════════════════════════════════════════════════════════════

def test_single_source_of_truth():
    """只有 `HOLDS` 一张表 —— 旧的两组（`CLAIM_HOLDS`/`TRACE_HOLDS`）不得复活。

    ★ 这是本文件最重要的一条：分裂只要复活一次，静默漂移就回来了。
    """
    assert isinstance(ls.HOLDS, dict) and ls.HOLDS, "HOLDS 必须是非空映射"
    for dead in ("CLAIM_HOLDS", "TRACE_HOLDS", "CLAIM_LABELS", "TRACE_HOLD_DEFAULT"):
        assert not hasattr(ls, dead), (
            f"`label_spec.{dead}` 复活了 —— 口径只能有一张表，"
            f"两组并存正是当初静默漂移的根因")


def test_long_horizons_are_self_consistent():
    """长周期必须与 `1Y=252` 严格自洽 —— 22（旧值）就是在这里站不住：22×12=264>252。"""
    y = ls.HOLDS["1Y"]
    assert ls.HOLDS["1M"] * 12 == y, f"1M×12 应等于 1Y（{y}），现 1M={ls.HOLDS['1M']}"
    assert ls.HOLDS["3M"] * 4 == y, f"3M×4 应等于 1Y（{y}），现 3M={ls.HOLDS['3M']}"
    assert ls.HOLDS["2W"] == 2 * ls.HOLDS["1W"], "2W 应为 1W 的两倍"


def test_hold_days_is_strict():
    """严格版：未知返回 None（不是兜底成 3 —— 静默兜底会让口径漂移）。"""
    for lab, days in (("T+1", 1), ("T+3", 3), ("T+5", 5), ("1W", 5), ("2W", 10),
                      ("1M", 21), ("3M", 63), ("1Y", 252), ("n/a", 0)):
        assert ls.hold_days(lab) == days, f"{lab} 应为 {days}"
    assert ls.hold_days("T+99") is None
    assert ls.hold_days("") is None
    assert ls.hold_days(None) is None


def test_resolve_hold_days_has_default():
    """宽松版（resolver/evaluator 用）：未知兜底 default。**只有这一处**允许兜底。"""
    assert ls.resolve_hold_days("T+3") == 3
    assert ls.resolve_hold_days("乱写") == ls.DEFAULT_HOLD_DAYS
    assert ls.resolve_hold_days("乱写", default=5) == 5
    assert ls.resolve_hold_days("") == ls.DEFAULT_HOLD_DAYS, "空标签兜底（旧链路 timeframe 恒空）"


def test_hold_label_roundtrip():
    """days ⇄ label 必须往返，0 不得映射成 'n/a'，5 天规范名必须是 'T+5'。"""
    for lab, days in (("T+1", 1), ("T+3", 3), ("T+5", 5), ("2W", 10),
                      ("1M", 21), ("3M", 63), ("1Y", 252)):
        assert ls.hold_label(days) == lab, f"{days} 天的规范标签应为 {lab}"
        assert ls.hold_days(ls.hold_label(days)) == days
    assert ls.hold_label(0) == "T+0", "0 不该被反推成 n/a（HOLD_LABELS 必须剔除 0）"
    assert ls.hold_label(7) == "T+7"      # 未登记的天数 ⇒ 直给，不兜底成某个标签
    # ★ `T+5` 与 `1W` 同为 5 天：反查表**先到先得**，规范名必须是 T+5
    assert ls.HOLD_LABELS[5] == "T+5", (
        "反查表被 1W 覆盖了 —— 必须用先到先得，否则 hold_label(5) 静默改名")


def test_legacy_trace_path_uses_same_table():
    """旧链路 `evaluate_pending()` 现在**走同一张表**（不再分两套）。

    ★ 行为变更已实测无影响：traces 根节点 timeframe 恒为空 ⇒ 恒走 DEFAULT_HOLD_DAYS。
    """
    from app.agent.chain import evaluator

    assert evaluator._get_hold_days("1M") == ls.HOLDS["1M"], "旧链路必须走统一表，不是 22"
    assert evaluator._get_hold_days("不存在的") == ls.DEFAULT_HOLD_DAYS
    assert evaluator._get_hold_days("") == ls.DEFAULT_HOLD_DAYS


def test_bar_freq_default_and_env(monkeypatch):
    """`bar_freq`：拍板 2 只预留字段，默认 `1d`，env 可配。"""
    monkeypatch.delenv("QD_BAR_FREQ", raising=False)
    assert ls.bar_freq() == "1d"
    monkeypatch.setenv("QD_BAR_FREQ", "60m")
    assert ls.bar_freq() == "60m"
    monkeypatch.setenv("QD_BAR_FREQ", "瞎写")
    assert ls.bar_freq() == "1d", "未登记的 bar_freq 应回落默认，不是照抄（防脏值入库）"


def test_bar_freq_is_reserved_not_used():
    """预留字段语义：`DEFAULT_BAR_FREQ` 必须是 `1d`，且登记集合包含它。"""
    assert ls.DEFAULT_BAR_FREQ == "1d"
    assert ls.DEFAULT_BAR_FREQ in ls.BAR_FREQS


# ═══════════════════════════════════════════════════════════════
#  三处调用方必须**委派**到 label_spec（不得各写一份）
# ═══════════════════════════════════════════════════════════════

def test_evaluator_delegates_to_label_spec():
    from app.agent.chain import evaluator

    assert evaluator._TIMEFRAME_DAYS == ls.HOLDS, "evaluator 仍在自持一份口径"
    assert evaluator.HOLD_LABELS == ls.HOLD_LABELS
    # LABEL_HOLDS 是 **标签→天数**（resolver 用它把 "T+3" 翻成 3），由反查表推得
    assert evaluator.LABEL_HOLDS == {v: k for k, v in ls.HOLD_LABELS.items()}
    # ★ `T+5` 与 `1W` 同为 5 天：反查表先到先得 ⇒ 规范名是 T+5，1W 不该出现在
    #   反查结果里（否则同一个天数有两个入口，口径又会飘）
    assert "1W" not in evaluator.LABEL_HOLDS
    assert evaluator.LABEL_HOLDS["T+5"] == 5
    assert evaluator._DEFAULT_HOLD_DAYS == ls.DEFAULT_HOLD_DAYS


def test_claims_delegates_to_label_spec():
    from app.agent.chain import claims

    assert claims._HORIZON_DAYS == ls.HOLDS, "claims 仍在自持一份 horizon 映射"


def test_resolver_delegates_to_label_spec():
    from app.agent.chain import resolver

    for lab, days in (("T+1", 1), ("T+3", 3), ("1M", 21)):
        assert resolver._horizon_days(lab) == days
        assert resolver._days_label(days) == lab
    assert resolver._horizon_days("乱写") == 3


# ═══════════════════════════════════════════════════════════════
#  ★ 扫描源码：不许出现第二份
# ═══════════════════════════════════════════════════════════════

def test_no_second_copy_in_chain():
    """扫描 `app/agent/chain/*.py`：除 `label_spec.py` 外，不得再有 horizon→days 字面量。

    判据：同一行里同时出现 `"T+1"` 形式的标签**和**紧随其后的数字（如 `{"T+1": 1`）。
    """
    chain_dir = Path(__file__).resolve().parents[1] / "app" / "agent" / "chain"
    pattern = re.compile(r'"T\+1"\s*:\s*\d')
    offenders = []
    for py in sorted(chain_dir.glob("*.py")):
        if py.name == "label_spec.py":
            continue
        for i, line in enumerate(py.read_text(encoding="utf-8",
                                              errors="replace").splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{py.name}:{i}: {line.strip()[:80]}")
    assert not offenders, (
        "chain/ 下出现第二份 horizon→days 字面量（口径会分裂）：\n  "
        + "\n  ".join(offenders))


def test_no_legacy_alias_definition_in_chain():
    """`chain/` 下任何文件都**不得再定义**旧的两组常量（允许在注释里提及溯源）。

    ★ 只禁**定义**（`NAME =` / `NAME:`），不禁提及 —— 注释里说明"为什么合并过"
      是有价值的溯源信息，不该被误伤。
    """
    chain_dir = Path(__file__).resolve().parents[1] / "app" / "agent" / "chain"
    pattern = re.compile(
        r'^\s*(CLAIM_HOLDS|TRACE_HOLDS|CLAIM_LABELS|TRACE_HOLD_DEFAULT)\s*(?::[^=]+)?=',
        re.M)
    offenders = []
    for py in sorted(chain_dir.glob("*.py")):
        for i, line in enumerate(py.read_text(encoding="utf-8",
                                              errors="replace").splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{py.name}:{i}: {line.strip()[:80]}")
    assert not offenders, (
        "旧的两组口径常量被重新定义（口径分裂复活）：\n  " + "\n  ".join(offenders))

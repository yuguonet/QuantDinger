"""test_fold_mode.py — 折叠「两种推进模式」门禁（2026-10-06 要点③纠偏）。

背景（本文件要防的缺陷）:
    此前 `StrategyBase.scan_days` 自带一份折叠循环，且恒传 prev=None；
    runner/realtime 传 prev=rec.current ⇒ **两套编排 + 两种 prev 喂法**，
    一致性靠"break/dragon 已全市场对账"背书 —— 要点③否定的正是这种模式
    （对账是抽样证据，不是结构保证；改任一侧就可能静默分裂）。

现状（本文件断言的目标态）:
    * 编排单源 —— stateless 折叠只有 `runner.fold_range` 一处实现，
      `scan_days` 是它的薄封装（只做下标换算 + ready→Signal 组装）；
    * 模式契约化 —— prev=None = stateless（不结算、不抑制），非 None =
      stateful（按 prev 结算/抑制），语义写在 contract 顶部而非靠巧合。

两条都变成硬断言：任何一侧回退（自写循环 / 模式漂移）都会 FAIL，不静默。
"""

import inspect

import pytest

from app.market_cn.auto.core.present.runner import (
    DailyRunner, StateStore, fold_range,
)
from app.market_cn.auto.strategies.base import Signal, StrategyBase
from app.market_cn.auto.strategies.dragon_callback import DragonCallbackStrategy

from .common import gen_hist_bars
# ⚠ 必须用 crafted_bars：fuzz 数据在这两个入口上都产不出信号 ⇒ [] == [] 是假绿
from .test_dragon import CODE, SI, crafted_bars

START = 30          # 与既有对账同起点（dragon 折叠起点）


def _stateful(bars, tmp_path):
    """stateful 推进（runner 生命周期分支）：返回 (ready 日期集, 出现过的 stage 集)。"""
    s = DragonCallbackStrategy()
    runner = DailyRunner(StateStore(str(tmp_path / "st")))
    ready, stages = set(), set()
    for k in range(START, len(bars) + 1):
        trunc = bars[:k]
        for _code, evs in runner.run_day_all(
                s, str(trunc[-1]["time"])[:10], {CODE: trunc},
                {CODE: {"stock_info": SI}}).items():
            for e in evs:
                stages.add(e.stage)
                if e.stage == "ready":
                    ready.add(str(e.date)[:10])
    return ready, stages


def test_scan_days_delegates_fold_to_kernel():
    """编排单源：scan_days 不得自带折叠循环（否则 = 第二份编排，只能靠对账）。"""
    src = inspect.getsource(StrategyBase.scan_days)
    for banned in ("self.step(", "self.evaluate(", "while j", "for k in range"):
        assert banned not in src, (
            "scan_days 自己写了折叠循环(%s) —— 必须委托 core.present.runner"
            ".fold_range；两套编排只能靠对账维持一致，是要点③否定的模式" % banned)


def test_unmigrated_strategy_falls_back_to_scan_signals():
    """未迁移折叠契约的策略（init_state 未实现）退化到自己的 scan_signals。

    退化是**唯一门实现**的另一入口，不是第二份逻辑；此处防的是"退化路径被
    内核折叠顶掉"（那会让这些策略静默产出空集）。
    """
    class _Legacy(StrategyBase):
        key = "_t_legacy"

        def scan_signals(self, bars, code, **params):
            return [Signal(code=code, time=str(bars[-1]["time"])[:10])]

    bars = gen_hist_bars(CODE, [10.0] * 6)
    out = _Legacy().scan_days(bars, CODE)
    assert [s.time for s in out] == [str(b["time"])[:10] for b in bars]


@pytest.mark.parametrize("use_tech", (True, False))
def test_stateless_equals_scan_signals(use_tech):
    """stateless ≡ scan_signals：同一门在无 prev 下的两个入口，不是靠对账一致。"""
    s = DragonCallbackStrategy()
    s.use_tech_score = use_tech
    bars = crafted_bars()
    old = set()
    for k in range(START, len(bars)):
        for sig in s.scan_signals(bars[:k + 1], CODE, stock_info=SI):
            old.add(str(sig.time)[:10])
    new = {str(bars[i]["time"])[:10] for i, _ in fold_range(s, CODE, bars, START)}
    assert old, "构造场景应当触发信号 —— 空集对比等价于没测（假绿）"
    assert old == new, "stateless 与 scan_signals 口径分裂: %s" % sorted(old ^ new)


def test_stateful_ready_is_subset_of_stateless(tmp_path):
    """模式差异可断言：stateful 多一道 ±4 去重 ⇒ ready ⊆ stateless，且只有它结算。"""
    bars = crafted_bars()
    stateless = {str(bars[i]["time"])[:10]
                 for i, _ in fold_range(DragonCallbackStrategy(), CODE, bars, START)}
    assert stateless, "构造场景应当触发信号（空集会让子集断言恒真 = 假绿）"
    ready, stages = _stateful(bars, tmp_path)
    assert ready <= stateless, \
        "stateful 出现了 stateless 没有的信号（去重不得新增）: %s" % sorted(ready - stateless)
    assert "exec" in stages, \
        "stateful 必须结算 ready→exec；stateless 模式下该事件按契约不产生"

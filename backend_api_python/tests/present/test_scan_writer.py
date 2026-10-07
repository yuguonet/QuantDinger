"""test_scan_writer.py — P5-③ 门禁：**写路径切换**（scan 的行源改 `Record.ready`，2026-10-07）。

计划原文（改进方案.md P5-③）：`切 writer（scan 的写库段换投影写入器；scan 的扫描职责
并入 fold 调度）`；门禁 `切换后 signals 零差异`；回滚 `writer 切回 scan（单点）`。

本文件锁定三件事：
  1. **零差异**：同一份 bars + 同一 target，`_rows_by_scan`（旧）与 `_rows_by_record`
     （新）产出的行**逐字段相同** —— 且先断言**非空**（空集对空集恒相等 = 假绿，
     这正是本项目点名的坑）。
  2. **不写残缺 / 不停扫**：切片推进报错时该策略**退回直判**（与「无折叠契约」同一出口）。
     直接拿残缺行去 `upsert_scan_signals` 会 DELETE 掉没判出来的信号；直接抛错中止则
     一份信号都不落 —— 两条都是不可接受的结局，故退化为「同一份判定的另一条读法」。
  3. **无折叠契约不静默丢行**：`persist_days` 登记跳过（`skipped`）的策略必须仍然出行
     （退回逐票 `scan_days`），否则整个策略的信号会被静默清零。

易错点：`break` 是 Python 关键字 ⇒ 走 importlib 取类，不 `from ...strategies.break import`。
"""

from __future__ import annotations

import json
import tempfile

from app.market_cn.auto import present_daily, scan
from app.market_cn.auto.strategies.base import Signal

from .test_projection import _bars, _break_cls

CODE = "600000"
NAME = "南玻A"
#: 片段里真实出信号的一天（`test_projection` 的 ready 日集合含它）—— 用它避免空集假绿
READY_DAY = "2026-06-17"


class _NullSampler:
    """采样与行源无关（采样器自跑 `scan_signals`）⇒ 门禁里替身即可。"""

    def observe(self, *a, **k):
        pass


def _canon(rows):
    return sorted(json.dumps(r, sort_keys=True, default=str) for r in rows)


def _run_both(target=READY_DAY, active=None, bars_by_code=None):
    s = _break_cls()()
    bars = _bars()
    active = active or {"break": s}
    bars_by_code = bars_by_code or {CODE: bars}
    info = {CODE: {"name": NAME}}
    old, errs = scan._rows_by_scan(active, dict(bars_by_code), target, info, _NullSampler())
    root = tempfile.mkdtemp(prefix="writer_parity_")
    new = scan._rows_by_record(active, dict(bars_by_code), target, info, _NullSampler(),
                               root=root)
    return old, new, errs, bars


# ================================================================
# 1. 新旧行源逐字段零差异
# ================================================================
def test_rows_by_record_equals_rows_by_scan():
    old, new, errs, _bars_ = _run_both()
    assert not errs, f"旧路径判定异常: {errs}"
    assert old, "旧路径 0 行 ⇒ 该日不产信号, 断言会退化成空集对空集（换 READY_DAY）"
    assert _canon(old) == _canon(new), (
        "行源切换后行发生分叉（P5-③ 门禁「signals 零差异」被破坏）\n"
        f"只旧={list(set(_canon(old)) - set(_canon(new)))[:2]}\n"
        f"只新={list(set(_canon(new)) - set(_canon(old)))[:2]}")


def test_writer_switch_has_single_rollback_point(monkeypatch):
    """`present_persist.enabled` 是**唯一**切换点（回滚 = 翻回 false）。"""
    import app.market_cn.auto.strategies as strat_reg
    monkeypatch.setattr(strat_reg, "load_config",
                        lambda refresh=False: {"present_persist": {"enabled": False}})
    assert present_daily.enabled() is False, "缺省必须是旧 writer（零行为变化）"
    monkeypatch.setattr(strat_reg, "load_config",
                        lambda refresh=False: {"present_persist": {"enabled": True}})
    assert present_daily.enabled() is True


# ================================================================
# 2. 切片推进失败 ⇒ 该策略退回直判（既不写残缺、也不整日停扫）
# ================================================================
def test_rows_by_record_falls_back_when_persist_failed(monkeypatch):
    """判定期异常 = 整日整策略失败（`advance_all` 不在票级兜异常）。

    若直接拿残缺行去 `upsert_scan_signals`，会把没判出来的那部分信号 DELETE 掉；
    若直接抛错中止，则一份信号都不落。⇒ 该策略退回直判（与「无折叠契约」同一出口）。
    """
    fake = {"root": "/tmp/x", "dates": [READY_DAY], "ready": {}, "skipped": [],
            "strategies": {"break": {"days": 0, "advanced": 0, "warm_skipped": 0,
                                     "errors": [f"{READY_DAY}: boom"], "warmed": False}}}
    monkeypatch.setattr(present_daily, "persist_days", lambda *a, **k: fake)
    rows = scan._rows_by_record({"break": _break_cls()()}, {CODE: _bars()}, READY_DAY,
                                {CODE: {"name": NAME}}, _NullSampler(), root="/tmp/x")
    assert len(rows) == 1, ("切片推进失败时未退回直判 ⇒ 当日该策略信号整批丢失"
                            "（且它是静默的：调用方只看到 0 行）")
    assert rows[0]["strategy"] == "break" and rows[0]["code"] == CODE


# ================================================================
# 3. 无折叠契约 ⇒ 具名登记 + 退回直判（不静默丢行）
# ================================================================
def test_rows_by_record_falls_back_for_no_fold_contract(monkeypatch):
    class _NoFold:
        key = "nofold"
        prefilter_anchor = "signal"
        use_unified_prefilter = False

        def scan_days(self, bars, code, *, lo_date=None, hi_date=None, **params):
            assert lo_date == hi_date == READY_DAY, "兜底必须只判当日"
            return [Signal(code=code, time=READY_DAY, score=60, price=10.0,
                           extra={"note": "fallback"})]

    monkeypatch.setattr(present_daily, "enabled", lambda: True)
    rows = scan._rows_by_record({"nofold": _NoFold()}, {CODE: _bars()}, READY_DAY,
                                {CODE: {"name": NAME}}, _NullSampler(),
                                root=tempfile.mkdtemp(prefix="writer_nofold_"))
    assert len(rows) == 1, "无折叠契约的策略被静默丢行（该策略信号会整批消失）"
    assert rows[0]["strategy"] == "nofold" and rows[0]["score"] == 60

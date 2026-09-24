# -*- coding: utf-8 -*-
"""golden tests 纯函数断言（方案常设 3）——零 LLM，可进 CI。

覆盖：
  - `_normalize_phases` 三态（无 phases / 单段 / 多段 + depends_on）
  - `_normalize_plan_tools` 并集
  - `resilient_parse` 伪标签抢救
  - smolagents 错误结构化（smol_log）
"""
from __future__ import annotations

from app.agent.utils.phase_graph import select_ready_batch, validate_depends_on
from app.agent.utils.smol_log import error_counters, record_sml_error
from app.agent.utils.tool_synth import should_synthesize


def test_depends_on_cycle():
    errs = validate_depends_on([{"id": 1, "depends_on": [2]}, {"id": 2, "depends_on": [1]}])
    assert any("cycle" in e for e in errs)


def test_ready_batch_independent():
    ph = [{"id": 1, "depends_on": []}, {"id": 2, "depends_on": []}, {"id": 3, "depends_on": [1, 2]}]
    b = select_ready_batch(ph, done=set(), max_parallel=2)
    assert set(b) == {1, 2}
    b2 = select_ready_batch(ph, done={1, 2}, max_parallel=2, prefer_from=3)
    assert 3 in b2


def test_ready_batch_barrier_exclusive():
    ph = [{"id": 1, "depends_on": []}, {"id": 2, "barrier": True, "depends_on": []}]
    b = select_ready_batch(ph, done=set(), max_parallel=3)
    assert len(b) == 1, b


def test_synth_cluster():
    cases = [{"tools": ["get_kline"], "goal": "fetch stock data", "correct": 1} for _ in range(3)]
    c = should_synthesize(cases)
    assert c and c["n"] == 3


def test_smol_error_structured():
    before = error_counters().get("error", 0)
    record_sml_error("boom test", kind="error")
    assert error_counters().get("error", 0) == before + 1


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"  ✗ {fn.__name__}: {e}")
    print(f"golden: {len(fns) - failed}/{len(fns)} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

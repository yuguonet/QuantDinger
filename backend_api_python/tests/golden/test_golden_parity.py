"""test_golden_parity.py — **golden 逐笔对拍门禁**（改进方案 §4-P1 / §5-1 / §2.2.1-3）。

来源：golden 逐笔对拍任务 | 产出：2026-10-07

★ 这是「删除旧代码的**唯一开关**」（方案 §4 删除铁律）。
  `tests/golden/baselines/*.json` 是**不可变真相源** —— 由冻结时点的旧引擎产出。
  删除 `backtest_stock` / `core/backtest.py` / probe 面的前提，就是本文件全绿。

★ 为什么不能拿 live 的旧引擎当基准（§2.2.1 元问题）：
  旧回测本身是被删对象，拿它当基准等于「用将死之物当真相」；且它自带 T+1 缺陷。
  方案 §2.2.1-3 裁定：先统一「出场从 D2 起」为**双方共同基线**，再对拍其余口径。
  冻结基线即在 D2 口径下产出（`_min_sell_day=2`，见 core/exit_engines.py）。

⚠️ 假绿防线（本项目最常踩的坑）：
  - 每份输入**必须非空** —— 空集对空集恒相等是假绿，`test_zero_trades_is_not_a_pass`
    显式拒绝零 trade 基线。
  - **不用 `importorskip`** —— 静默 skip = 假绿（见 test_strategy_files_are_documents 头注）。
  - 输入指纹变了必须重冻 —— `test_inputs_unchanged` 防「改输入不改基线」的静默失效。

覆盖：break（真实 000032 片段）+ dragon_callback（合成配方 ×3，含 gap/未平）。
缺口见 docs/口径差异报告.md「缺口」节：g56 需横截面池（psycopg2）、knife/tail 需
IntradayFeed，两者的 golden 待有库环境补 —— 不在此假装覆盖。
"""
from __future__ import annotations

import json
import os

import pytest

from tests.golden.freeze import (
    BASELINE_DIR, BASELINE_VERSION, CORE_ALWAYS_COMPARABLE, GOLDEN_COMPARE_FIELDS,
    build, _bars_fingerprint,
)
from tests.golden.inputs import INPUT_SETS
from tests.golden.normalize import normalize_exit_reason

_NAMES = [s["name"] for s in INPUT_SETS]
_BY_NAME = {s["name"]: s for s in INPUT_SETS}


def _load(name: str) -> dict:
    path = os.path.join(BASELINE_DIR, name + ".json")
    assert os.path.exists(path), (
        "基线缺失: %s —— 先跑 `python -m tests.golden.freeze`（改输入/口径后必须重冻，"
        "并同步 docs/口径差异报告.md）" % path)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ================================================================
# 0. 假绿防线
# ================================================================
def test_zero_trades_is_not_a_pass():
    """每份基线必须非空 —— 零 trade 的基线对任何实现都「通过」，是假绿。"""
    for name in _NAMES:
        b = _load(name)
        assert b["trades"], "%s 基线零 trade —— 无证据，拒绝作为删除开关" % name


def test_baselines_are_current_version():
    """基线格式版本必须匹配（口径变更时 +1，旧文件即失效，不得静默沿用）。"""
    for name in _NAMES:
        b = _load(name)
        assert b.get("version") == BASELINE_VERSION, (
            "%s 基线版本 %s != 当前 %s —— 口径已变，须重冻" % (
                name, b.get("version"), BASELINE_VERSION))


def test_inputs_unchanged():
    """输入指纹必须与冻结时一致 —— 否则「改输入不改基线」会让对拍静默失效。"""
    from tests.golden.freeze import _universe_fingerprint
    for spec in INPUT_SETS:
        b = _load(spec["name"])
        if spec.get("pooled"):
            now = _universe_fingerprint(spec["universe"]())
        else:
            now = _bars_fingerprint(spec["build"]())
        assert now == b["bars_fingerprint"], (
            "%s 输入已变（%s != %s）—— 须重新冻结基线并更新口径差异报告" % (
                spec["name"], now, b["bars_fingerprint"]))


def test_exit_reason_normalizable():
    """出场原因必须能归一（归不出来的必须显式报，不得当成一致）。

    ⚠️ 只对**参与比对的** exit_reason 要求可归一；旧引擎本就不记出场原因的策略
    （g56，见 `not_comparable`）不在此要求 —— 按 trade_map「缺字段不猜」。
    """
    for spec in INPUT_SETS:
        b = _load(spec["name"])
        if "exit_reason" not in b.get("compare_fields", []):
            continue                        # 旧侧无此字段，已在基线登记
        for t in b["trades"]:
            code, raw = normalize_exit_reason(raw=t)
            if raw is not None:
                assert code is not None, (
                    "%s 出场原因归一失败: %r —— 补 tests/golden/normalize.py 的映射表" % (
                        spec["name"], raw))


def test_core_fields_always_compared():
    """行为核心 8 字段必须永远在 compare_fields 里（防「逐笔对拍」退化成假绿）。"""
    from tests.golden.freeze import CORE_ALWAYS_COMPARABLE as _CORE
    for spec in INPUT_SETS:
        b = _load(spec["name"])
        missing = [k for k in _CORE if k not in b["compare_fields"]]
        assert not missing, (
            "%s 的 compare_fields 缺核心字段 %s —— 不得把它们剔出比对" % (
                spec["name"], missing))


def test_baseline_records_not_comparable_fields():
    """被剔出比对的字段必须登记在案（不静默降级）。"""
    for spec in INPUT_SETS:
        b = _load(spec["name"])
        nc = b.get("not_comparable", [])
        for k in nc:
            assert k not in CORE_ALWAYS_COMPARABLE, (
                "%s 不得剔除核心字段 %s" % (spec["name"], k))
        if nc:
            # 有剔除时，必须能看出是哪一类（载荷缺字段），且每个 trade 都确实缺该字段
            for t in b["trades"]:
                for k in nc:
                    assert t.get(k) is None, (
                        "%s 声称 %s 不可比，但 trade 里有值 %r —— 登记失真" % (
                            spec["name"], k, t.get(k)))


# ================================================================
# 1. 逐笔对拍（删除旧代码的唯一开关）
# ================================================================
def _replay_trades(spec: dict) -> list[dict]:
    from app.market_cn.auto import strategies as reg
    from app.market_cn.auto.core.replay import (
        DailyFeed, TradesCollector, replay, replay_batch,
    )
    reg.autodiscover()
    strat = reg.get_strategy(spec["strategy"])
    if spec.get("pooled"):
        # 横截面策略：整个宇宙一起喂（分位需要同日其他票），按 code 收集后合并
        from tests.golden.freeze import _reset_g56_pool
        uni = spec["universe"]()
        _reset_g56_pool()          # 防跨种子串池（同 freeze._old_trades）
        # 用**新实例**，避免共享单例的池台账/状态跨宇宙污染
        cls = type(strat)
        import inspect
        strat = cls() if len(inspect.signature(cls).parameters) == 0 else strat
        res = replay_batch(strat, uni, collectors={
            c: [TradesCollector(code=c, strategy=spec["strategy"])] for c in uni})
        out = []
        for c in sorted(uni):
            out.extend((res.get(c).trades or []) if res.get(c) is not None else [])
        return out
    if spec.get("intraday"):
        # 盘中条目：快照序列经 ctx_provider 供给（同一 evaluate；旧侧同配方，见 freeze）
        rows_by_date = spec["day_rows"]()
        coll = TradesCollector(code=spec["code"], strategy=spec["strategy"])

        def _ctx(code_, date, bars_):
            if date in rows_by_date:
                return {"series": rows_by_date[date], "mkt_gain": spec["mkt_gain"]}
            return {}

        res = replay_batch(strat, {spec["code"]: spec["build"]()},
                           collectors={spec["code"]: [coll]},
                           ctx_provider=_ctx)
        return (res.get(spec["code"]).trades or []) if res.get(spec["code"]) else []
    bars = spec["build"]()
    coll = TradesCollector(code=spec["code"], strategy=spec["strategy"])
    res = replay(strat, spec["code"], DailyFeed(bars), collectors=[coll])
    return res.trades or []


@pytest.mark.parametrize("name", _NAMES)
def test_replay_matches_frozen_baseline(name):
    """replay 逐笔逐位 == 冻结基线（浮点 1e-12，门判定逐位）。"""
    spec = _BY_NAME[name]
    base = _load(name)
    old = base["trades"]
    new = _replay_trades(spec)

    assert old, "%s 基线为空（假绿）" % name
    assert len(old) == len(new), (
        "%s 笔数不符: 基线 %d != replay %d\n基线=%s\nreplay=%s" % (
            name, len(old), len(new), old, new))

    fields = base.get("compare_fields") or list(GOLDEN_COMPARE_FIELDS)
    for i, (o, nw) in enumerate(zip(old, new)):
        tag = "%s#%d" % (name, i)
        for k in fields:
            ov, nv = o.get(k), nw.get(k)
            if k in ("entry_price", "exit_price", "return_pct", "peak_return_pct"):
                if ov is None or nv is None:
                    assert ov == nv, "%s %s: 一侧为 None (%r vs %r)" % (tag, k, ov, nv)
                else:
                    assert abs(float(ov) - float(nv)) <= 1e-12, \
                        "%s %s: %r != %r" % (tag, k, ov, nv)
            elif k == "exit_reason":
                # §2.6b 载荷异构：两侧都归一到代码再比（标签↔代码，见 normalize.py）
                o_code, _ = normalize_exit_reason(raw=o)
                n_code, n_raw = normalize_exit_reason(label=nw.get(k))
                assert o_code is not None, "%s 基线 exit_reason 归一失败: %r" % (tag, o.get(k))
                assert n_code is not None, \
                    "%s replay exit_reason 归一失败: %r —— 补映射表" % (tag, n_raw)
                assert o_code == n_code, \
                    "%s exit_reason: %r(%s) != %r(%s)" % (tag, o.get(k), o_code, nw.get(k), n_code)
            else:
                assert ov == nv, "%s %s: %r != %r" % (tag, k, ov, nv)


@pytest.mark.parametrize("name", _NAMES)
def test_baseline_not_vacuous_on_extreme_cases(name):
    """极端案例也要有证据：gap/未平 输入不得退化成空对空。"""
    base = _load(name)
    assert base["trades"], "%s 极端案例零 trade —— 该分支未被 golden 覆盖" % name
    for t in base["trades"]:
        assert t.get("entry_price"), "%s 缺 entry_price" % name
        assert t.get("exit_price"), "%s 缺 exit_price" % name

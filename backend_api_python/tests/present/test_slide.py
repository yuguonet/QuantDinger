"""test_slide.py — 1 日滑动验收：每天只喂 2 根 bar（昨/今）+ 探针锚 ≡ 全量 fold。

回应设计要点："D+1 收盘后预处理只做 1 日的延续处理"——
kernel 只消费 state+当日 bar；全量历史**仅重建时**需要（RebuildNeedsHistory）。
"""

import copy

import pytest

from app.market_cn.auto.core.present.runner import DailyRunner, RebuildNeedsHistory, StateStore
from app.market_cn.auto.strategies.dragon_callback import DragonCallbackStrategy
from app.market_cn.auto.strategies.g56 import G56Strategy, PoolLedger
from tests.present.test_dragon import CODE, SI, _fuzz_bars, crafted_bars


def _mk_runner(tmp_path, tag):
    return DailyRunner(StateStore(str(tmp_path / tag)))


def _run_full(s, runner, bars, ctxs=None):
    for k in range(31, len(bars) + 1):
        runner.run_day(s, CODE, bars[:k], (ctxs or {}).get(k))
    return runner.store.load(s.key, CODE).to_json()


def _run_sliding(s, runner, bars, ctxs=None):
    """1 日延伸：每天只喂 (yesterday, today) + probe 锚（扮演数据层按需取锚）。"""
    src = {b["time"]: b for b in bars}
    for k in range(31, len(bars) + 1):
        today = bars[k - 1]
        st = runner.store.load(s.key, CODE)
        anchors = dict(runner.probe_anchors(s, CODE)) if st else {}
        probe_bars = {d: src[d] for d in anchors if d in src}
        runner.advance(s, CODE, today,
                       yesterday=bars[k - 2] if k >= 2 else None,
                       ctx=(ctxs or {}).get(k),
                       probe_bars=probe_bars or None,
                       history=bars[:k])
    return runner.store.load(s.key, CODE).to_json()


def test_dragon_one_bar_feed_equals_full_fold(tmp_path):
    bars = crafted_bars()[:34]
    a = _run_full(DragonCallbackStrategy(), _mk_runner(tmp_path, "a"), bars)
    b = _run_sliding(DragonCallbackStrategy(), _mk_runner(tmp_path, "b"), bars)
    assert a == b, "1 日滑动 == 全量 fold（state/progress/事件流水逐位一致）"


def test_g56_one_bar_feed_equals_full_fold(tmp_path):
    from tests.present.test_g56 import _gen
    universe = _gen(1)
    sa = G56Strategy(pool_ledger=PoolLedger())
    sb = G56Strategy(pool_ledger=PoolLedger())
    ra, rb = _mk_runner(tmp_path, "a"), _mk_runner(tmp_path, "b")
    src = {c: {b["time"]: b for b in bars} for c, bars in universe.items()}
    for k in range(36, 80):
        date = universe["600001"][k]["time"]
        trunc = {c: b[:k + 1] for c, b in universe.items()}
        ra.run_day_all(sa, date, trunc)
        di = {}
        for c, bars in universe.items():
            st = rb.store.load(sb.key, c)
            anchors = dict(rb.probe_anchors(sb, c)) if st else {}
            di[c] = {"today": bars[k], "yesterday": bars[k - 1],
                     "probe_bars": {d: src[c][d] for d in anchors if d in src[c]} or None,
                     "history": bars[:k + 1]}
        rb.advance_all(sb, date, di)
    for c in universe:
        assert ra.store.load(sa.key, c).to_json() == rb.store.load(sb.key, c).to_json(), c


def test_advance_daily_input_is_two_bars(tmp_path):
    """日常输入面核对：不给 history 也能推进（只有重建才需要）。"""
    s = DragonCallbackStrategy()
    runner = _mk_runner(tmp_path, "s")
    bars = crafted_bars()[:34]
    # 先 seed 一天（重建路径，需要 history；seed 至昨日需 ≥30 根）
    runner.advance(s, CODE, bars[30], yesterday=bars[29], history=bars[:31])
    # 之后每天只喂两根，无 history
    for k in range(31, 34):
        runner.advance(s, CODE, bars[k], yesterday=bars[k - 1])
    rec = runner.store.load(s.key, CODE)
    assert rec.date == bars[33]["time"]


def test_probe_rebuild_via_probe_bars(tmp_path):
    """除权改写：数据层取回的锚 bar close 变了 → 重建（且必须带 history）。"""
    s = DragonCallbackStrategy()
    runner = _mk_runner(tmp_path, "s")
    bars = crafted_bars()[:34]
    runner.advance(s, CODE, bars[30], yesterday=bars[29], history=bars[:31])
    rec = runner.store.load(s.key, CODE)
    d0, c0 = s.probe(rec.state)[0]
    forged = {d0: {**bars[0], "close": c0 * 0.8}}      # 前复权缩放
    with pytest.raises(RebuildNeedsHistory):
        runner.advance(s, CODE, bars[31], yesterday=bars[30], probe_bars=forged)
    rec2, _ = runner.advance(s, CODE, bars[31], yesterday=bars[30],
                             probe_bars=forged, history=bars[:32])
    assert rec2.state["abs_i"] == 31                   # 重建 seed 至昨日 + 推进今日


def test_rebuild_conditions_still_hold_1d(tmp_path):
    s = DragonCallbackStrategy()
    runner = _mk_runner(tmp_path, "s")
    bars = crafted_bars()[:34]
    runner.advance(s, CODE, bars[30], yesterday=bars[29], history=bars[:31])
    # ① 乱序
    with pytest.raises(RebuildNeedsHistory):
        runner.advance(s, CODE, bars[28], yesterday=bars[27])
    # ② 断档（切片不在昨根上）
    with pytest.raises(RebuildNeedsHistory):
        runner.advance(s, CODE, bars[33], yesterday=bars[31])
    # ④ 策略文件被修改
    rec = runner.store.load(s.key, CODE)
    rec.strategy_hash = "deadbeef"
    runner.store.save(s.key, CODE, rec)
    with pytest.raises(RebuildNeedsHistory):
        runner.advance(s, CODE, bars[31], yesterday=bars[30])

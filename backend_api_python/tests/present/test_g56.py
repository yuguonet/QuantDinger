"""test_g56.py — P4 等价性：g56 迁移版 vs 旧版（逐日信号 / 回测交易 / 横截面池）。

合成宇宙（4 票 × 140 日，种子固定）：种子 1/7/12 各含 ≥1 笔真实交易（正例），
其余全为负例对照。旧侧注入面：monkeypatch hub.all_codes + prewarm(bars_batch)；
台账开关 G1_*_FROM_LEDGER=0 关旁路。
"""

import os
import random

import numpy as np
import pytest

from app.market_cn.auto.core.present.runner import DailyRunner, StateStore
from app.market_cn.auto.strategies.g56 import G56Strategy, PoolLedger

g56_old = pytest.importorskip("app.market_cn.auto.strategies.g56",
                              reason="旧参照代码不可用")

SEEDS = (1, 7, 12, 2, 3)
N_DAYS = 140
FOLD_FROM = 36          # 折叠起点（seed 需 ≥win+1=36 根）；池成员自 age≥68 起出现，
                        # 两侧同样从无到有积累 ⇒ score_r 滚动历史一致


def _gen(seed, n=N_DAYS):
    rng = random.Random(seed)
    codes = {}
    for ci, code in enumerate(["600001", "600002", "600003", "300001"]):
        bars = []
        px = 100.0 * (1 + 0.1 * ci)
        for i in range(n):
            drift = 0.004 if (i // 20) % 2 == 0 else -0.006
            j = rng.gauss(drift, 0.035)
            if rng.random() < 0.06:
                j += 0.07
            o, c = px, px * (1 + j)
            h = max(o, c) * (1 + abs(rng.gauss(0, 0.02)))
            lo = min(o, c) * (1 - abs(rng.gauss(0, 0.02)))
            bars.append({"time": f"2025-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
                         "open": round(o, 3), "high": round(h, 3),
                         "low": round(lo, 3), "close": round(c, 3),
                         "volume": 1e6 * (1 + rng.random())})
            px = c
        codes[code] = bars
    return codes


@pytest.fixture(autouse=True)
def _no_ledger(monkeypatch):
    monkeypatch.setenv("G1_SIGNAL_FROM_LEDGER", "0")
    monkeypatch.setenv("G1_POOL_FROM_LEDGER", "0")


def _reset_old_pool():
    """旧 _POOL 是模块级单槽缓存（键=pool_target）；合成宇宙共享日期序列 ⇒
    不复位会跨种子/跨测试串池。"""
    try:
        from app.market_cn.auto.core.features import cross_section as cs
        cs._POOL.update({"target": None, "main": {}, "gem_star": {}})
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _pool_reset():
    _reset_old_pool()
    yield
    _reset_old_pool()


@pytest.fixture()
def hub_patch(monkeypatch):
    from app.market_cn.auto.core.data import hub
    return hub


def _old_daily_hits(universe, hub_patch):
    """旧口径逐日：prewarm(trunc) + scan_signals(判末根)。"""
    hub_patch.all_codes = lambda: list(universe)
    st = g56_old.G56Strategy()
    codes = list(universe)
    hits = {}
    for t in range(FOLD_FROM, N_DAYS):
        date = universe[codes[0]][t]["time"][:10]
        trunc = {c: b[:t + 1] for c, b in universe.items()}
        st.prewarm(trunc, date)
        for c in codes:
            for s in st.scan_signals(trunc[c], c):
                hits[(c, str(s.time)[:10])] = s
    return hits


def _new_daily_hits(universe, tmp_path):
    """新折叠逐日：run_day_all（begin_day 池 + evaluate）。"""
    s = G56Strategy(pool_ledger=PoolLedger())
    runner = DailyRunner(StateStore(str(tmp_path / "st")))
    hits = {}
    for t in range(FOLD_FROM, N_DAYS):
        date = universe["600001"][t]["time"][:10]
        trunc = {c: b[:t + 1] for c, b in universe.items()}
        for c, events in runner.run_day_all(s, date, trunc).items():
            for e in events:
                if e.stage == "ready":
                    hits[(c, e.date)] = e.payload
    return hits


@pytest.mark.parametrize("seed", SEEDS)
def test_g56_day_signals_match_old(seed, hub_patch, tmp_path):
    universe = _gen(seed)
    old_hits = _old_daily_hits(universe, hub_patch)
    new_hits = _new_daily_hits(universe, tmp_path)
    assert set(old_hits) == set(new_hits), f"seed={seed} 信号集合不一致"
    for key, sig in old_hits.items():
        p = new_hits[key]
        assert sig.score == p["score"], key
        assert sig.price == p["price"], key
        assert sig.label == p["label"], key
        assert sig.extra == p["extra"], key


def test_g56_signal_fields_match_macro(hub_patch, tmp_path):
    """D6/R1 门禁: 宏 g56.yaml `signal.fields` 是展示字段**唯一事实源**。

    折叠 (主干) 产出的 extra 键集必须与宏声明逐字一致 —— 防止「宏里加了字段、.py 却
    不产出」的静默脱节 (此前的 nd_score/nd_tag/nd_exp 就是这样: 宏声明了、产物没有,
    见 P5收口执行记录 R1)。结构性键 `buy_mode` 由 .py 追加, 不计入宏字段。
    """
    from app.market_cn.auto.core.runtime.evaluate import load_strategy
    declared = set(load_strategy("g56").signal.get("fields") or {})
    assert declared, "宏 signal.fields 不应为空"
    hits = _new_daily_hits(_gen(1), tmp_path)
    assert hits, "夹具须含正例 (防空转假绿)"
    for key, p in hits.items():
        assert set(p["extra"]) - {"buy_mode"} == declared, (key, sorted(p["extra"]))


def test_g56_has_positive_signals():
    """防空转：固定种子集必须含真实正例。"""
    hub = pytest.importorskip("app.market_cn.auto.core.data.hub")
    total = 0
    for seed in (1, 7, 12):
        _reset_old_pool()
        universe = _gen(seed)
        hub.all_codes = lambda u=universe: list(u)
        st = g56_old.G56Strategy()
        for code, bars in universe.items():
            st.prewarm(universe, str(bars[-1]["time"])[:10])
            total += len(st.backtest_stock(bars, code))
    assert total >= 3


@pytest.mark.parametrize("seed", (1, 7, 12))
def test_g56_backtest_trades_match_old(seed, hub_patch, tmp_path):
    universe = _gen(seed)
    hub_patch.all_codes = lambda: list(universe)
    st = g56_old.G56Strategy()
    last = str(universe["600001"][-1]["time"])[:10]
    st.prewarm(universe, last)
    old_trades = {}
    for code, bars in universe.items():
        for tr in st.backtest_stock(bars, code):
            # 2026-10-08 覆写已删: canonical 用 d0_date (旧钩子为 signal_date)
            old_trades[(code, tr.get("d0_date") or tr.get("signal_date"))] = tr

    # 新折叠：从事件流水组装交易（ready → exec → exit）
    s = G56Strategy(pool_ledger=PoolLedger())
    runner = DailyRunner(StateStore(str(tmp_path / "st")))
    for t in range(FOLD_FROM, N_DAYS):
        date = universe["600001"][t]["time"][:10]
        trunc = {c: b[:t + 1] for c, b in universe.items()}
        runner.run_day_all(s, date, trunc)
    new_trades = {}
    for code in universe:
        rec = runner.store.load(s.key, code)
        evs = list(rec.events)
        sig = None
        for e in evs:
            if e["stage"] == "ready":
                sig = e
            elif e["stage"] == "exec" and sig is not None:
                if e["payload"].get("buyable"):
                    ent = e
                else:
                    sig = None
            elif e["stage"] == "exit" and sig is not None:
                k_idx = [i for i, b in enumerate(universe[code])
                         if b["time"][:10] == sig["date"]][0]
                if not (67 <= k_idx <= N_DAYS - 11):    # 旧 backtest k∈[67, n-11]
                    sig = None
                    continue
                new_trades[(code, sig["date"])] = {
                    "signal_date": sig["date"],
                    "entry_date": ent["payload"]["entry_date"],
                    "entry_price": round(ent["payload"]["entry_price"], 3),
                    "entry_gap": ent["payload"]["gap"],
                    "exit_date": e["payload"]["exit_date"],
                    "exit_price": e["payload"]["exit_price"],
                    "exit_day": e["payload"]["exit_day"],
                    "return_pct": e["payload"]["return_pct"],
                    "peak_return_pct": e["payload"]["peak_return_pct"],
                    "rhist_chg": sig["payload"]["extra"]["rhist_chg"],
                    "boll_pctb": sig["payload"]["extra"]["boll_pctb"],
                    "rmed": sig["payload"]["extra"]["rmed"],
                    "score_r": sig["payload"]["extra"]["score_r"],
                    "buy_mode": "next_open",
                }
                sig = None
    assert set(old_trades) == set(new_trades), f"seed={seed} 交易集合不一致"
    # 2026-10-08: 旧侧=canonical(replay), 新侧=事件手拼(含 rhist/score_r 展示键);
    # 比对业务字段交集 —— canonical 无 rhist_*/entry_gap, 手拼无 exec_basis/score。
    _CMP = ("entry_date", "entry_price", "exit_date", "exit_price",
            "exit_day", "return_pct", "peak_return_pct")
    for key, ot in old_trades.items():
        nt = new_trades[key]
        for field in _CMP:
            assert nt[field] == ot.get(field), (key, field, nt[field], ot.get(field))
        assert nt["signal_date"] == (ot.get("d0_date") or ot.get("signal_date"))


def test_g56_pool_matches_old_aggregate(hub_patch, tmp_path):
    """跨票池：begin_day 台账聚合 == 旧全量桶 _aggregate（rmed/score_r 逐位）。"""
    seed = 1
    universe = _gen(seed)
    old = pytest.importorskip("app.market_cn.auto.core.features.cross_section")
    # 旧全量桶
    buckets = {"main": {}, "gem_star": {}}
    for code, bars in universe.items():
        board = "gem_star" if code.startswith(("30", "68")) else "main"
        f = old._g1_arrays(bars)
        for k in np.nonzero(old._g1_mask(f, board))[0]:
            b = buckets[board].setdefault(f["dates"][k], [[], [], []])
            b[0].append(f["rhist_chg"][k])
            b[1].append(f["dif0"][k])
            b[2].append(f["rsi"][k])
    old_pool = {brd: old._aggregate(bk) for brd, bk in buckets.items()}

    # 新折叠（全史跑完；池在 run_day_all 内部逐日积累）
    s = G56Strategy(pool_ledger=PoolLedger())
    runner = DailyRunner(StateStore(str(tmp_path / "st")))
    for t in range(36, N_DAYS):
        date = universe["600001"][t]["time"][:10]
        trunc = {c: b[:t + 1] for c, b in universe.items()}
        runner.run_day_all(s, date, trunc)
    # 从台账重建池
    from app.market_cn.auto.core.features import cross_section as G
    new_pool = {}
    new_pool = {}
    for board in ("main", "gem_star"):
        by_date = {q["date"]: [[q["rmed"]] * q["n"], [q["dmed"]] * q["n"],
                               [q["smed"]] * q["n"]]
                   for q in s.ledger.window(board)}
        new_pool[board] = G._aggregate(by_date)
    overlap = 0
    for board in ("main", "gem_star"):
        for date, st_new in new_pool[board].items():
            st_old = old_pool[board].get(date)
            if st_old is None:
                continue
            overlap += 1
            assert abs(st_new["rmed"] - st_old["rmed"]) < 1e-9, (board, date)
            if st_old["score_r"] is None or st_new["score_r"] is None:
                assert st_old["score_r"] is None and st_new["score_r"] is None, (board, date)
            else:
                assert abs(st_new["score_r"] - st_old["score_r"]) < 1e-9, (board, date)
    assert overlap >= 10

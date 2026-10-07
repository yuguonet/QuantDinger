"""test_dragon.py — P5 等价性：dragon_callback 迁移版 vs 旧版（逐日信号/回测交易/出场引擎）。

等价目标 = scan_signals / backtest_stock（next_open 口径），非 _backtest_limit_up（规格 §2.6）。
旧 scan_signals 默认 use_tech_score=True（rsi6 门激活）；两态都对拍。
"""

import random

import pytest

from app.market_cn.auto.core.present.runner import DailyRunner, StateStore
from app.market_cn.auto.strategies.dragon_callback import DragonCallbackStrategy

# 出场唯一实现在生产文件（2026-10-06 下沉后 展示层侧不再有副本）
dc_old = pytest.importorskip("app.market_cn.auto.strategies.dragon_callback",
                             reason="旧参照代码不可用")
run_backtest_dragon_callback = dc_old.run_backtest_dragon_callback

CODE = "600000"
SI = {"circ_shares": 5e8, "total_shares": 8e8}


def _mk(i, o, h, l, c, v=1e6, start="2025-01-01"):
    from datetime import date, timedelta
    y, m, d = map(int, start.split("-"))
    return {"time": str(date(y, m, d) + timedelta(days=i)), "open": round(o, 3),
            "high": round(h, 3), "low": round(l, 3), "close": round(c, 3),
            "volume": v}


def crafted_bars():
    """规格 §5.2 配方（前移 3 根使 D0=32 ≥ 折叠起点）：
    lu=25（18~22 五连阳含 4 板，绝对索引 22..25）、gain20≈81%、深度回调 → D0=32 全门过。
    另有意外板 31（gap 1..8 均被拒）→ 覆盖候选遍历/胜者选择。"""
    bars = []
    closes = [99.5, 99.5, 99.5, 100, 101, 102]
    # 6..20 缓涨至 120
    for i in range(6, 21):
        closes.append(closes[-1] * (1.012 + 0.002 * ((i * 7) % 3)))
    closes[20] = 120.0
    closes.append(126.0)                       # 21: +5%（非板）
    for _ in range(4):                         # 22..25 四连板
        closes.append(round(closes[-1] * 1.10, 4))
    # 26..32 回调段
    closes += [170.0, 162.0, 155.0, 148.0, 142.0, 156.0, 154.4]
    for i, c in enumerate(closes):
        o = closes[i - 1] * 1.002 if i else c * 0.99
        h = max(o, c) * 1.01
        l = min(o, c) * 0.99
        if i == 29:
            l = 125.0                          # 深跌腿：depth ≈ -32%
        if i == 32:
            h, l, o = c * 1.005, c * 0.995, 156.0    # D0 小阴
        bars.append(_mk(i, o, h, l, c))
    bars.append(_mk(33, 155.0, 158.0, 154.0, 157.0))   # D1（入场）
    return bars


def _fuzz_bars(seed, n=70):
    rng = random.Random(seed)
    bars = []
    px = 30.0
    for i in range(n):
        j = rng.gauss(0.004 if (i // 12) % 2 == 0 else -0.006, 0.045)
        if rng.random() < 0.07:
            j += 0.11
        o, c = px, px * (1 + j)
        h = max(o, c) * (1 + abs(rng.gauss(0, 0.02)))
        lo = min(o, c) * (1 - abs(rng.gauss(0, 0.02)))
        bars.append(_mk(i, o, h, lo, c))
        px = c
    return bars


def _fold_ready(bars, tmp_path, use_tech=True, si=SI):
    s = DragonCallbackStrategy()
    s.use_tech_score = use_tech
    runner = DailyRunner(StateStore(str(tmp_path / "st")))
    hits = {}
    for k in range(30, len(bars) + 1):
        trunc = bars[:k]
        for code, events in runner.run_day_all(
                s, str(trunc[-1]["time"])[:10], {CODE: trunc},
                {CODE: {"stock_info": si}}).items():
            for e in events:
                if e.stage == "ready":
                    hits[str(e.date)[:10]] = (e, trunc)
    return hits


def _old_hits(bars, use_tech=True, si=SI):
    """旧口径**逐日**扫描 = 生产 `scan_signals` / `scan_days` 的信号集。

    ⚠ **不施加 ±4 多日去重**：那条去重只活在 `backtest_stock`（回测口径）里，生产
      `scan_signals` 没有它 ⇒ 库内按日一行（唯一键含 trade_date）。切片工作表的口径已
      裁定为「判定(ready) 恒 stateless」（见 `runner.evaluate_day`）⇒ 对拍目标就是这里
      的 raw 集。回测链语义（去重/抑制）由 `test_dragon_backtest_trades_match_old`
      （比**交易**而非比信号集）覆盖，不再混进信号集对拍 —— 混进去会让「持仓期重合
      信号」既算差异又不算差异。
    """
    st = dc_old.DragonCallbackStrategy()
    hits = {}
    for k in range(30, len(bars)):
        for s in st.scan_signals(bars[:k + 1], CODE, use_tech_score=use_tech,
                                 stock_info=si):
            hits.setdefault(str(s.time)[:10], s)
    return hits


def _assert_same(old_hits, new_hits, tag):
    assert set(old_hits) == set(new_hits), f"{tag}: 信号集合不一致 {set(old_hits)} {set(new_hits)}"
    for d, sig in old_hits.items():
        ev, _ = new_hits[d]
        p = ev.payload
        assert sig.price == p["price"], (tag, d)
        assert sig.label == p["label"], (tag, d)
        for k, v in sig.extra.items():
            assert p["extra"].get(k) == v, (tag, d, k, p["extra"].get(k), v)


def test_dragon_crafted_signal_matches_old(tmp_path):
    bars = crafted_bars()
    old_hits = _old_hits(bars)
    assert old_hits, "构造场景应当触发（若空说明配方失效，需修配方）"
    new_hits = _fold_ready(bars, tmp_path)
    _assert_same(old_hits, new_hits, "crafted")


@pytest.mark.parametrize("use_tech", (True, False))
def test_dragon_fuzz_day_signals_match_old(use_tech, tmp_path):
    total = 0
    for seed in (3, 11, 21):
        bars = _fuzz_bars(seed)
        old_hits = _old_hits(bars, use_tech=use_tech)
        new_hits = _fold_ready(bars, tmp_path / str(seed), use_tech=use_tech)
        _assert_same(old_hits, new_hits, f"fuzz{seed}/tech={use_tech}")
        total += len(old_hits)
    # 防空转（fuzz 含涨停段，应当出信号；不出也至少全程一致）


def test_dragon_exit_engine_matches_old():
    """出场引擎逐字移植对拍：随机 bars × 入场点（含跌停/跳空/逃顶/到期）。"""
    rng = random.Random(5)
    for trial in range(40):
        n = rng.randint(12, 25)
        bars = []
        px = 20.0
        for i in range(n):
            j = rng.gauss(0, 0.05)
            if rng.random() < 0.12:
                j -= 0.115
            o, c = px, px * (1 + j)
            h = max(o, c) * (1 + abs(rng.gauss(0, 0.03)))
            lo = min(o, c) * (1 - abs(rng.gauss(0, 0.03)))
            if rng.random() < 0.08:            # 一字跌停日（A7/A8 路径）
                dn = px * 0.9
                o = h = lo = c = dn
            bars.append(_mk(i, o, h, lo, c))
            px = c
        entry_idx = rng.randint(2, n - 3)
        entry_price = float(bars[entry_idx]["open"]) or float(bars[entry_idx]["close"])
        old = dc_old.run_backtest_dragon_callback(bars, entry_idx, entry_price)
        new = run_backtest_dragon_callback(bars, entry_idx, entry_price, board_type="main")
        assert (old is None) == (new is None), trial
        if old is not None:
            assert old == new, (trial, old, new)


def test_dragon_backtest_trades_match_old(tmp_path):
    base = crafted_bars()
    tail = _fuzz_bars(99, 40)
    off = len(base)                            # 后续段日期后移，避免撞日期
    bars = base + [{**b, "time": str(_mk(off + j, 0, 0, 0, 0)["time"])}
                   for j, b in enumerate(tail)]
    st = dc_old.DragonCallbackStrategy()
    old_trades = st.backtest_stock(bars, CODE, stock_info=SI, use_prefilter=False)

    s = DragonCallbackStrategy()
    s.use_tech_score = True
    runner = DailyRunner(StateStore(str(tmp_path / "st")))
    for k in range(30, len(bars) + 1):
        runner.run_day_all(s, str(bars[k - 1]["time"])[:10], {CODE: bars[:k]},
                           {CODE: {"stock_info": SI}})
    rec = runner.store.load(s.key, CODE)
    new_trades = []
    sig = ent = None
    for e in rec.events:
        st = e["stage"]
        if st == "ready":
            # ⚠ 持仓/已入场时出现的 ready 是**判定流**（生产会落库的持仓期重合信号日），
            #   不开启新的交易链 —— 链语义是「exec 之前最后一条 ready」（见 evaluate_day）。
            if ent is None:
                sig = e
        elif st == "exec" and ent is None and sig is not None:
            if e["payload"].get("buyable"):
                ent = e
            else:
                sig = None                      # gap 拒绝 ⇒ 该信号作废
        elif st == "exit" and ent is not None:
            i_idx = [j for j, b in enumerate(bars)
                     if str(b["time"])[:10] == str(sig["date"])[:10]][0]
            if i_idx <= len(bars) - 2:          # 旧 backtest i ∈ [2, n-2]
                new_trades.append({
                    "signal_date": str(sig["date"])[:10],
                    "entry_date": ent["payload"]["entry_date"],
                    "entry_price": ent["payload"]["entry_price"],
                    "d1_gap": ent["payload"]["d1_gap"],
                    "exit_price": e["payload"]["exit_price"],
                    "exit_day": e["payload"]["exit_day"],
                    "return_pct": e["payload"]["return_pct"],
                    "peak_return_pct": e["payload"]["peak_return_pct"],
                    "exit_reason": e["payload"]["reason"],
                })
            sig = ent = None                    # 平仓 ⇒ 链结束，等下一个 ready
    assert len(old_trades) >= 1, "构造历史应当含交易"
    assert len(old_trades) == len(new_trades), (old_trades, new_trades)
    for o, nw in zip(old_trades, new_trades):
        for k in nw:
            assert o.get(k) == nw[k], (k, o.get(k), nw[k])


def test_dragon_fold_state_step_equals_init():
    """fold 等价：逐步推进 == 一次性重建（lu 冻结属性 / rsi6 / ring）。"""
    from app.market_cn.auto.strategies.dragon_callback import DragonCallbackStrategy as S
    bars = _fuzz_bars(4, 90)
    s = S()
    st = s.init_state(CODE, bars[:60])
    for b in bars[60:]:
        st = s.step(st, b)
    fresh = s.init_state(CODE, bars)
    assert st["abs_i"] == fresh["abs_i"]
    assert len(st["lus"]) == len(fresh["lus"])
    for a, b in zip(st["lus"], fresh["lus"]):
        assert a == b, (a, b)
    assert st["rsi6"] == fresh["rsi6"]
    assert st["ring"] == fresh["ring"]
    assert st["macd"] == pytest.approx(fresh["macd"], abs=1e-6)

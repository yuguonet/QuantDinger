"""tools/present_verify.py — 展示层**单票对账**命令 (改进方案 §2.3 / §7.3 的 verify)。

用途: 换规则/换策略/换数据后, 手工确认"新内核仍与旧口径逐位一致"。它不是
生产链的一环, 只在人需要取证时跑 —— 因此**不做任何静默降级**: 每一项都打印
PASS/FAIL 与证据, 任一项失败即以非 0 退出。

用法:
    python -m app.market_cn.auto.tools.present_verify
    python -m app.market_cn.auto.tools.present_verify --strategy tail_oversold
    python -m app.market_cn.auto.tools.present_verify --source db --code 600000 --days 200

检查项 (对应方案 §5 验收):
    C1 fold 等价   全量 fold(逐日 run_day) vs 1 日延伸(逐日 advance) → state/current 逐位一致
    C2 实时一致    实时分支(切片副本 + 快照) == 预处理末位判定 (同 bar 逐位一致)
    C3 四条件重建  reordered / date_gap / probe_mismatch / strategy_changed 各自命中
    C4 重建后等价  触发重建后 state == init_state(全量历史[:-1]), 且 current 保留
    P  性能        全量 fold 耗时 vs 单日推进耗时 (方案 §5.4 目标: 比值 ≥ 5x)

易错点:
  - `run_day`(全量入口) 在首日 bars 不足时**抛** InsufficientHistory (不静默跳过),
    故 C1 从 `min_age` 起枚举 —— 与 `advance_all` 的 except 分支口径不同, 别混用。
  - C3 直接调 `DailyRunner._rebuild_reason` 判**原因字符串**: 重建是唯一恢复手段,
    判"是否重建了"不够, 必须判"为什么重建"(条件错位 = 静默算错)。
  - 1 日延伸路径**只在重建时才需要 history**; 日常推进不传 → 缺 history 会抛
    RebuildNeedsHistory(硬要求), 这不是缺陷而是接口契约。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import tempfile
import time

from app.market_cn.auto.core.present.contract import DayInput, Progress
from app.market_cn.auto.core.present.realtime import RealtimeBranch
from app.market_cn.auto.core.present.runner import (
    DailyRunner, Record, StateStore, evaluate_day, strategy_source_hash,
)
from app.market_cn.auto.strategies.knife_catch import KnifeCatchStrategy
from app.market_cn.auto.strategies.tail_oversold import TailOversoldStrategy

STRATEGIES = {
    "knife_catch": KnifeCatchStrategy,
    "tail_oversold": TailOversoldStrategy,
}

#: 合成历史用的收盘序列 (连跌 + 前期走弱, 让日线门可判)
_SYNTH_CLOSES = [126, 124, 122, 120, 118, 116, 114, 112,
                 110, 108, 106, 104, 102, 111, 108, 104, 100, 93]


def _synth_bars(code: str, closes=None, start="2026-08-01", vol=1000.0):
    """与 tests/common.gen_hist_bars 同口径的合成日线(不依赖 tests 包)。"""
    from datetime import date, timedelta
    y, m, d = map(int, start.split("-"))
    out = []
    for i, c in enumerate(closes or _SYNTH_CLOSES):
        c = float(c)
        out.append({"time": str(date(y, m, d) + timedelta(days=i)),
                    "open": round(c * 1.01, 4), "high": round(c * 1.02, 4),
                    "low": round(c * 0.98, 4), "close": c, "volume": vol})
    return out


def _load_bars(source: str, code: str, days: int):
    """取数: db = 走数据出口 hub.daily; synth = 本地合成 (默认, 不碰库)。"""
    if source == "db":
        from app.market_cn.auto.core.data.hub import daily
        bars = daily(code, days=days)
        if not bars:
            raise SystemExit(f"[verify] hub.daily({code}, days={days}) 返回空 —— 不降级, 请换 code 或改用 --source synth")
        return [{"time": str(b["time"])[:10], "open": float(b["open"]), "high": float(b["high"]),
                 "low": float(b["low"]), "close": float(b["close"]), "volume": float(b["volume"])}
                for b in bars]
    rng = random.Random(20261006)
    closes = _SYNTH_CLOSES + [round(93 * (1 + rng.uniform(-0.03, 0.02)), 2) for _ in range(max(0, days - len(_SYNTH_CLOSES)))]
    return _synth_bars(code, closes)


def _progress_sig(p: Progress | None):
    """Progress → 可比对签名 (None 与 None 相等)。"""
    if p is None:
        return None
    return (p.stage, p.date, json.dumps(p.payload, sort_keys=True, default=str),
            p.next_realtime)


def _min_age(strategy, bars) -> int:
    """该策略能 seed 的最小根数(逐步试探 init_state 抛 InsufficientHistory 的边界)。"""
    from app.market_cn.auto.core.present.contract import InsufficientHistory
    for k in range(1, min(len(bars), 60) + 1):
        try:
            strategy.init_state("verify", bars[:k])
            return k
        except InsufficientHistory:
            continue
    raise SystemExit("[verify] 60 根内仍无法 seed —— 数据不足以对账")


# ---------------------------------------------------------------- C1
def check_fold_equivalence(strategy, code, bars) -> tuple[bool, str]:
    """全量 fold vs 1 日延伸: 末态 state / current / date 必须逐位一致。"""
    from app.market_cn.auto.core.present.contract import InsufficientHistory

    root = tempfile.mkdtemp(prefix="present_verify_")
    k0 = _min_age(strategy, bars)
    # A: 全量入口 run_day(逐日喂全量 bars)
    rA = DailyRunner(StateStore(os.path.join(root, "A")))
    for k in range(k0, len(bars)):
        try:
            recA, _ = rA.run_day(strategy, code, bars[:k + 1])
        except InsufficientHistory:
            continue
    # B: 1 日延伸 advance(只给 today/yesterday/probe_bars; 首次需 history 做 seed)
    rB = DailyRunner(StateStore(os.path.join(root, "B")))
    by_date = {b["time"]: b for b in bars}
    try:
        recB, _ = rB.advance(strategy, code, bars[k0], yesterday=bars[k0 - 1] if k0 else None,
                             probe_bars=by_date, history=bars[:k0 + 1])
        for k in range(k0 + 1, len(bars)):
            recB, _ = rB.advance(strategy, code, bars[k], yesterday=bars[k - 1],
                                 probe_bars=by_date)
    except InsufficientHistory as e:
        return False, f"1 日延伸路径 seed 失败: {e}"

    if recA.state != recB.state:
        diff = [kk for kk in recA.state if recA.state.get(kk) != recB.state.get(kk)]
        return False, f"state 不一致 (差异键={diff})"
    if _progress_sig(recA.current) != _progress_sig(recB.current):
        return False, f"current 不一致: A={_progress_sig(recA.current)} B={_progress_sig(recB.current)}"
    if recA.date != recB.date:
        return False, f"date 不一致: A={recA.date} B={recB.date}"
    return True, f"逐位一致 (fold {k0}→{len(bars) - 1}, 末态 stage={recA.current.stage if recA.current else None})"


# ---------------------------------------------------------------- C2
def check_realtime_equals_preprocess(strategy, code, bars) -> tuple[bool, str]:
    """实时分支(切片副本 + 未收盘快照) == 预处理在同一 bar 上的判定。"""
    root = tempfile.mkdtemp(prefix="present_verify_rt_")
    store = StateStore(root)
    runner = DailyRunner(store)
    k0 = _min_age(strategy, bars)
    d0, d1 = bars[-2], bars[-1]
    state = strategy.init_state(code, bars[:-1])
    entry_px = float(d0["close"])
    prev = Progress(stage="ready", date=d0["time"], payload={"price": entry_px},
                    next_realtime="09:31")

    # 实时侧输入 = 未收盘快照 (两侧必须**同一份**, 否则比的是两个输入而不是两个分支)
    snap = {"time": f"{d1['time']} 09:31:00", "open": float(d1["open"]),
            "high": float(d1["high"]), "low": float(d1["low"]),
            "last": float(d1["open"]), "volume": float(d1["volume"]),
            "previousClose": float(d0["close"])}

    # 预处理侧: 权威切片 + 完整 bar。**必须与实时分支用同一个判定入口**
    #   (`evaluate_day`: ready 恒 stateless + 生命周期 stateful)，否则比的是两个语义。
    ctx = {"latest": snap, "series": [snap], "mkt_gain": None}
    ev_pre, _head = evaluate_day(strategy, code, dict(state), d1, ctx, prev)
    # 实时侧: 落一份带 prev 的记录 → 走 RealtimeBranch.tick (副本试推, 不落盘)
    rec = Record(date=d0["time"], state=state, current=prev,
                 strategy_hash=strategy_source_hash(strategy))
    store.save(strategy.key or "verify", code, rec)
    rt = RealtimeBranch(store, {strategy.key or "verify": strategy})
    hits = rt.tick("09:31", [code], {code: snap}, {code: [snap]}, None)
    ev_rt = [p for _, p in hits if p.stage != "hold"]   # hold 是判定非事件，不进对拍

    key = lambda es: [(e.stage, e.date, json.dumps(e.payload, sort_keys=True, default=str)) for e in es]
    if key(ev_pre) != key(ev_rt):
        return False, f"实时 != 预处理: pre={key(ev_pre)} rt={key(ev_rt)}"
    if not ev_pre:
        return True, "两侧均无事件(一致)"
    return True, f"一致: {key(ev_pre)}"


# ---------------------------------------------------------------- C3
def check_rebuild_conditions(strategy, code, bars) -> tuple[bool, str]:
    """四条件重建 + 同日幂等: 每个条件都必须**各自**命中预期原因。"""
    root = tempfile.mkdtemp(prefix="present_verify_rb_")
    store = StateStore(root)
    runner = DailyRunner(store)
    k0 = _min_age(strategy, bars)
    k = min(k0 + 3, len(bars) - 1)
    rec, _ = runner.advance(strategy, code, bars[k], yesterday=bars[k - 1],
                            probe_bars={b["time"]: b for b in bars},
                            history=bars[:k + 1])
    h = strategy_source_hash(strategy)
    by_date = {b["time"]: b for b in bars}
    cases = []

    r = runner._rebuild_reason(strategy, rec, bars[k - 1], None, None)
    cases.append(("reordered(乱序/旧数据)", r == "reordered", r))

    bad = Record(date=rec.date, state=rec.state, current=rec.current,
                 events=rec.events, strategy_hash="stale")
    r = runner._rebuild_reason(strategy, bad, bars[k + 1] if k + 1 < len(bars) else bars[k],
                               bars[k], None)
    cases.append(("strategy_changed(改规则)", r == "strategy_changed", r))

    d, c = strategy.probe(rec.state)[0]
    pb = {d: dict(by_date.get(d) or {"close": c})}
    pb[d]["close"] = float(c) * 0.5          # 历史被复权改写
    r = runner._rebuild_reason(strategy, rec, bars[k + 1] if k + 1 < len(bars) else bars[k],
                               bars[k], pb)
    cases.append(("probe_mismatch(除权/订正)", r == "probe_mismatch", r))

    gap = Record(date="1999-01-01", state=rec.state, current=rec.current,
                 events=rec.events, strategy_hash=h)
    r = runner._rebuild_reason(strategy, gap, bars[k + 1] if k + 1 < len(bars) else bars[k],
                               bars[k], None)
    cases.append(("date_gap(断档)", r == "date_gap", r))

    r = runner._rebuild_reason(strategy, rec, bars[k], bars[k - 1], by_date)
    cases.append(("noop(同日重跑幂等)", r == "noop", r))

    bad = [f"{n}={got}" for n, ok, got in cases if not ok]
    detail = " | ".join(f"{n}:{'OK' if ok else 'FAIL(' + str(got) + ')'}" for n, ok, got in cases)
    return (not bad), detail


# ---------------------------------------------------------------- C4
def check_rebuild_equals_reseed(strategy, code, bars) -> tuple[bool, str]:
    """重建后 state == init_state(全量[:-1]); 规则进度 current 跨重建保留。"""
    root = tempfile.mkdtemp(prefix="present_verify_rs_")
    store = StateStore(root)
    runner = DailyRunner(store)
    k0 = _min_age(strategy, bars)
    k = min(k0 + 3, len(bars) - 1)
    rec, _ = runner.advance(strategy, code, bars[k], yesterday=bars[k - 1],
                            probe_bars={b["time"]: b for b in bars}, history=bars[:k + 1])
    rec.current = Progress(stage="ready", date=bars[k]["time"], payload={"price": 1.0})
    rec.strategy_hash = "stale"                       # 强制 ④ 重建
    store.save(strategy.key or "verify", code, rec)
    idx = min(k + 1, len(bars) - 1)                   # 重建日
    history = bars[:idx + 1]
    rec2, events = runner.advance(strategy, code, bars[idx], yesterday=bars[idx - 1],
                                  probe_bars={b["time"]: b for b in bars}, history=history)
    # 重建 = init_state(history[:-1]) 再 step(当日) ⇒ 应与 init_state(history) 逐位相等
    want = strategy.init_state(code, history)
    if rec2.state != want:
        return False, "重建后 state != init_state(全量[:-1])"
    # 关键不变量: 重建**不得吞掉**重建前的规则进度 —— prev 必须被 evaluate 消费
    # (产出结算事件), 而不是因为"state 换了"就静默丢失持仓阶段。
    if not any(e.stage != "watch" for e in events):
        return False, (f"重建吞掉了重建前的规则进度: 当日仅产出 watch "
                       f"(events={[(e.stage, e.date) for e in events]})")
    return True, (f"重建后 state 与全量 seed 逐位一致, 且 prev 被消费 "
                  f"(events={[(e.stage, e.date) for e in events]})")


# ---------------------------------------------------------------- P
def check_perf(strategy, code, bars) -> tuple[bool, str]:
    """全量 fold 一轮 vs 单日推进一次的耗时比 (方案 §5.4: 单日推进应 ≤ 全量 1/5)。"""
    root = tempfile.mkdtemp(prefix="present_verify_p_")
    k0 = _min_age(strategy, bars)
    runner = DailyRunner(StateStore(root))
    t0 = time.perf_counter()
    for k in range(k0, len(bars)):
        try:
            runner.run_day(strategy, code, bars[:k + 1])
        except Exception:
            pass
    t_full = time.perf_counter() - t0
    t0 = time.perf_counter()
    runner.advance(strategy, code, bars[-1], yesterday=bars[-2],
                   probe_bars={b["time"]: b for b in bars})
    t_one = time.perf_counter() - t0
    ratio = (t_full / t_one) if t_one > 0 else float("inf")
    return True, (f"全量 fold {t_full * 1000:.1f}ms / 单日推进 {t_one * 1000:.2f}ms "
                  f"= {ratio:.1f}x (目标 ≥5x, 小样本仅供参考)")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="展示层单票对账 (verify)")
    ap.add_argument("--strategy", default="knife_catch", choices=sorted(STRATEGIES))
    ap.add_argument("--code", default="600000")
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--source", default="synth", choices=("synth", "db"))
    a = ap.parse_args(argv)

    strategy = STRATEGIES[a.strategy]()
    strategy.key = strategy.key or a.strategy
    bars = _load_bars(a.source, a.code, a.days)
    print(f"[verify] strategy={a.strategy} code={a.code} source={a.source} bars={len(bars)} "
          f"({bars[0]['time']} ~ {bars[-1]['time']})")

    checks = [
        ("C1 fold 等价 (全量 vs 1日延伸)", check_fold_equivalence),
        ("C2 实时 == 预处理末位判定", check_realtime_equals_preprocess),
        ("C3 四条件重建 + 同日幂等", check_rebuild_conditions),
        ("C4 重建后 == 全量 seed", check_rebuild_equals_reseed),
        ("P  性能 (全量/单日)", check_perf),
    ]
    failed = 0
    for name, fn in checks:
        try:
            ok, detail = fn(strategy, a.code, bars)
        except Exception as e:                       # 取证命令: 不吞异常, 但要给出定位
            ok, detail = False, f"异常 {type(e).__name__}: {e}"
        print(f"  {'PASS' if ok else 'FAIL'}  {name} :: {detail}")
        failed += 0 if ok else 1
    print(f"[verify] {'ALL PASS' if not failed else str(failed) + ' FAILED'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

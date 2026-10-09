"""tests/golden/freeze.py — 冻结基线（改进方案 §2.2.1-3 裁定的「独立真相源」）。

来源：golden 逐笔对拍任务 | 产出：2026-10-07 | 冻结源切换：2026-10-08（草案①）

★ 本模块解决 §2.2.1 的元问题：
  golden 不能拿 **live 会变的引擎**当基准。本模块把当前口径的引擎输出一次性
  冻结成 JSON，此后对拍只对**冻结文件**，旧代码删掉也不影响门禁。

★ 冻结源（2026-10-08 批准切换）：**core.replay**（与对拍体合一）。
  原源 = `backtest_stock` 旧钩子（将删之物）。存量基线**不重冻**，继续作为
  冻结时点真相；golden 证 replay ≡ 存量基线 ⇒ 新源 `--check` 应一致。
  盘中条目（knife/tail）冻结源亦已改走折叠：`run_all_intraday` 现为**薄适配器**
  （委托 `IntradayFeed` + `core.replay`，见下方 `_old_intraday_trades`），与对拍体同源。

★ 为什么必须「先冻结、后删除」：
  冻结后 `tests/golden/baselines/*.json` 就是不可变的真相源；`test_golden_parity`
  比对 replay 与冻结文件，**逐笔逐位**。删除旧钩子的唯一开关就是它全绿。

用法（重新冻结，仅当输入集或口径有意变更时）：
    cd backend_api_python
    python -m tests.golden.freeze            # 写 tests/golden/baselines/*.json
    python -m tests.golden.freeze --check    # 只校验现有基线与当前冻结源一致

⚠️ 重新冻结是**改真相**，必须同时更新 docs/口径差异报告.md 并说明为什么。
"""
from __future__ import annotations

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

BASELINE_DIR = os.path.join(_HERE, "baselines")

#: 逐笔对拍的字段集（两侧都必须有；顺序稳定便于 diff）
#: ⚠️ 不含 score/label/exec_basis —— 旧 `backtest_stock` 的 trade dict 不带这三个，
#:    硬要比会得到 `None == None` 的假绿或无意义失败。它们的等价性由
#:    `test_dragon_backtest_trades_match_old` / `test_projection` 单独背书。
GOLDEN_COMPARE_FIELDS = (
    "d0_date", "entry_date", "entry_price",
    "exit_date", "exit_price", "exit_day", "exit_reason",
    "return_pct", "peak_return_pct",
)

#: 基线文件格式版本（字段/口径变更时 +1，旧文件即失效）
BASELINE_VERSION = 1

#: 行为核心字段：**永远参与逐位比**，任何基线不得把它们剔出 compare_fields。
#: （否则「逐笔对拍」会退化成比什么都行的假绿。）
CORE_ALWAYS_COMPARABLE = (
    "d0_date", "entry_date", "entry_price",
    "exit_date", "exit_price", "exit_day", "return_pct", "peak_return_pct",
)


def _project(raw: dict, bars: list[dict], code: str, strategy: str) -> dict:
    """trade dict → canonical 比对字段。

    兼容两套命名（旧 `signal_date`/`exit_rule` ↔ canonical `d0_date`/`exit_reason`）：
    现冻结源已统一到 `core.replay`（canonical），旧名分支仅为存量基线/外部参照保留。

    `exit_date` 旧 dict 不带（dragon/break 均如此），按 `entry_date + (exit_day-1)`
    从 bars 推 —— 只做**搬运**，不做判定（trade_map 同一纪律）。
    """
    ed = raw.get("exit_day")
    # 搬运纪律：旧 dict 自带 exit_date（盘中引擎/g56）就用原值；只对真缺的
    # （dragon/break）按 entry_date+(exit_day-1) 推导 —— 推导是搬运的兜底不是判定。
    exit_date = str(raw.get("exit_date"))[:10] if raw.get("exit_date") else None
    if exit_date is None and ed is not None:
        idx = None
        for j, b in enumerate(bars):
            if str(b.get("time"))[:10] == str(raw.get("entry_date"))[:10]:
                idx = j
                break
        if idx is not None:
            k = idx + int(ed) - 1
            if 0 <= k < len(bars):
                exit_date = str(bars[k].get("time"))[:10]
    out = {"code": code, "strategy": strategy}
    out["d0_date"] = str(raw.get("signal_date") or raw.get("d0_date"))[:10] \
        if (raw.get("signal_date") or raw.get("d0_date")) else None
    out["entry_date"] = str(raw.get("entry_date"))[:10] if raw.get("entry_date") else None
    out["exit_date"] = exit_date
    for k in ("entry_price", "exit_price", "exit_day",
              "return_pct", "peak_return_pct"):
        out[k] = raw.get(k)
    # 出场原因按 §2.6b 归一到**代码**（break 只有 exit_rule=代码，dragon 只有
    # exit_reason=标签；两侧归一后才可逐位比）。原值另存 exit_reason_raw 供追溯。
    from tests.golden.normalize import normalize_exit_reason
    code_v, raw_v = normalize_exit_reason(raw=raw)
    out["exit_reason"] = code_v
    out["exit_reason_raw"] = raw_v
    return {k: out.get(k) for k in GOLDEN_COMPARE_FIELDS + ("exit_reason_raw",)}


def _bars_fingerprint(bars: list[dict]) -> str:
    """输入指纹：输入变了基线就该重冻（防止「改输入不改基线」的静默失效）。"""
    import hashlib
    h = hashlib.sha256()
    for b in bars:
        h.update(("%s|%s|%s|%s|%s|%s|" % (
            b.get("time"), b.get("open"), b.get("high"),
            b.get("low"), b.get("close"), b.get("volume"))).encode())
    return h.hexdigest()[:16]


def _universe_fingerprint(uni: dict) -> str:
    """池化宇宙指纹（含代码与顺序，防「换池不重冻」）。"""
    import hashlib
    h = hashlib.sha256()
    for code in sorted(uni):
        h.update((code + "\n").encode())
        h.update(_bars_fingerprint(uni[code]).encode())
    return h.hexdigest()[:16]


def _reset_g56_pool() -> None:
    """清 g56 的模块级单槽池缓存（跨宇宙/跨种子必须清，否则串池）。"""
    from app.market_cn.auto.core.features import cross_section as cs
    pool = getattr(cs, "_POOL", None)
    if isinstance(pool, dict):
        pool.update({"target": None, "main": {}, "gem_star": {}})


def _old_trades(spec: dict):
    """跑冻结源一份输入 → (raw_trades 列表, bars 或 universe)。

    **冻结源 = core.replay（2026-10-08 批准，草案①）** —— 冻结源与对拍体合一：
    原从 `backtest_stock` 冻结（旧引擎将死之物），现从 replay 冻结；存量基线不重冻，
    继续作为冻结时点真相（golden 证 replay ≡ 存量基线 ⇒ 新源 --check 应一致）。
    池化条目（g56）需接两条取数线（**取数接线，不是规则**，故归本函数不进输入集）：
      1. `hub.all_codes` 返回池内代码（横截面分位需要同日其他票）；
      2. `prewarm(universe, last_date)` 预热池台账。
    """
    if spec.get("intraday"):
        return _old_intraday_trades(spec), spec["build"]()
    from app.market_cn.auto import strategies as reg
    from app.market_cn.auto.core.replay import TradesCollector, replay_batch
    reg.autodiscover()
    if spec.get("pooled"):
        uni = spec["universe"]()
        strat = reg.get_strategy(spec["strategy"])
        if strat is None:
            raise RuntimeError("策略未注册: %s" % spec["strategy"])
        last = str(uni[next(iter(uni))][-1]["time"])[:10]
        # 跨宇宙串池：单槽池缓存必须复位（同 test_g56 / replay_batch 纪律）
        _reset_g56_pool()
        import app.market_cn.auto.core.data.hub as hub
        orig = getattr(hub, "all_codes", None)
        hub.all_codes = lambda u=uni: list(u)
        try:
            strat.prewarm(uni, last)
            skey = strat.key or spec["strategy"]
            cols = {c: [TradesCollector(c, skey)] for c in uni}
            results = replay_batch(strat, uni, collectors=cols)
            out = []
            for c in sorted(uni):
                out.extend(results[c].trades if c in results else [])
        finally:
            if orig is not None:
                hub.all_codes = orig
        return out, uni
    bars = spec["build"]()
    strat = reg.get_strategy(spec["strategy"])
    if strat is None:
        raise RuntimeError("策略未注册: %s" % spec["strategy"])
    if not bars or len(bars) < 30:
        return [], bars
    coll = TradesCollector(spec["code"], strat.key or spec["strategy"])
    from app.market_cn.auto.core.replay import DailyFeed, replay
    res = replay(strat, spec["code"], DailyFeed(bars), collectors=[coll])
    return (res.trades or []), bars


def build(spec: dict) -> dict:
    """跑旧引擎一份输入 → 基线对象。"""
    raw, data = _old_trades(spec)
    pooled = bool(spec.get("pooled"))
    if pooled:
        bars_of = lambda c: data[c]                                    # noqa: E731
        trades = [_project(t, bars_of(t.get("code")), t.get("code"), spec["strategy"])
                  for t in raw]
        fp = _universe_fingerprint(data)
        n_bars = sum(len(v) for v in data.values())
    else:
        trades = [_project(t, data, spec["code"], spec["strategy"]) for t in raw]
        fp = _bars_fingerprint(data)
        n_bars = len(data)
    # 可比字段集：旧侧**全缺**的字段不可比（如 g56 的旧 trade 不带 exit_reason）。
    # 按 trade_map 纪律「缺字段一律置 None，不猜测、不补默认值」⇒ 不得假装可比。
    # ⚠️ 行为核心 8 字段**永远可比**（`CORE_ALWAYS_COMPARABLE`），不得被剔除 ——
    #   否则「逐笔对拍」会退化成比什么都行的假绿。
    comparable = [k for k in GOLDEN_COMPARE_FIELDS
                  if k in CORE_ALWAYS_COMPARABLE
                  or any(t.get(k) is not None for t in trades)]
    dropped = [k for k in GOLDEN_COMPARE_FIELDS if k not in comparable]
    return {
        "version": BASELINE_VERSION,
        "name": spec["name"],
        "strategy": spec["strategy"],
        "code": spec["code"],
        "pooled": pooled,
        "kwargs": spec["kwargs"],
        "n_bars": n_bars,
        "bars_fingerprint": fp,
        "compare_fields": comparable,
        "not_comparable": dropped,
        "note_exit_reason": (
            "exit_reason 已按 tests/golden/normalize.py 归一到代码"
            "（trail/stop/sweet/time/escape）；原值在 exit_reason_raw。"
            "若 not_comparable 含 exit_reason：该策略的旧引擎 trade 本就不记出场原因，"
            "按 trade_map「缺字段不猜」不参与逐位比（登记在案，不静默）。"
        ),
        "trades": trades,
    }


def freeze(check_only: bool = False) -> int:
    from tests.golden.inputs import INPUT_SETS
    os.makedirs(BASELINE_DIR, exist_ok=True)
    bad = 0
    for spec in INPUT_SETS:
        obj = build(spec)
        path = os.path.join(BASELINE_DIR, spec["name"] + ".json")
        n = len(obj["trades"])
        if n == 0:
            print("[空! ] %-22s 零 trade —— 基线无意义，拒绝冻结（假绿防线）" % spec["name"])
            bad += 1
            continue
        if check_only:
            if not os.path.exists(path):
                print("[缺失] %-22s 基线文件不存在" % spec["name"]); bad += 1; continue
            old = json.load(open(path, encoding="utf-8"))
            # 语义比较（2026-10-08 冻结源切换至 replay 后）：
            #   - 按基线**自己承诺的** compare_fields 比（旧覆写缺的字段如 g56 exit_reason
            #     本就不在承诺集；新源多产信息不算漂移——草案①: 存量基线不重冻）；
            #   - 外加输入指纹（bars_fingerprint/universe 语义）——输入变了基线就该重冻；
            #   - exit_reason_raw 等审计字段**不参与**（格式随源变，语义已在 exit_reason 归一码）。
            fields = old.get("compare_fields") or list(GOLDEN_COMPARE_FIELDS)
            def _sem(ts):
                return [{k: t.get(k) for k in fields} for t in ts]
            same = (old.get("bars_fingerprint") == obj.get("bars_fingerprint")
                    and _sem(old["trades"]) == _sem(obj["trades"]))
            print("[%s] %-22s trades=%d %s" % ("一致" if same else "漂移",
                                               spec["name"], n,
                                               "" if same else "← 基线承诺字段与当前冻结源不一致"))
            if not same:
                # 定位首个差异供排查
                a, b = _sem(obj["trades"]), _sem(old["trades"])
                if len(a) != len(b):
                    print("    笔数: 当前=%d 基线=%d" % (len(a), len(b)))
                else:
                    for i, (x, y) in enumerate(zip(a, b)):
                        for k in fields:
                            if x.get(k) != y.get(k):
                                print("    #%d %s: 当前=%r 基线=%r" % (i, k, x.get(k), y.get(k)))
                bad += 1
        else:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False, indent=1, sort_keys=True)
            print("[冻结] %-22s trades=%d -> %s" % (spec["name"], n,
                                                    os.path.relpath(path, _ROOT)))
    return 1 if bad else 0


# ================================================================
# P1.5-② 盘中条目旧侧：**折叠源**（2026-10-09 偏离2 后与对拍体同源）
# 旧独立时间线引擎已退役 ⇒ 盘中冻结源改走 `core.replay` + `IntradayFeed`（同对拍体），
# 数据面（分钟/日线/市场门）全部合成 patch —— **取数接线，不是规则**（同 g56 池接线原则）。
# ================================================================
def _old_intraday_trades(spec: dict) -> list[dict]:
    """盘中条目的旧侧：`run_all_intraday`（主干折叠）→ 合成数据面 → canonical trades。

    P6-1 后 `run_all_intraday` 直接产 canonical trade（不再是旧 trade 形状）；
    `build()` 的 `_project` 负责投影到比对字段（含 `exit_reason` 标签→代码归一），
    与日线条目同款 —— 冻结基线（`baselines/*.json`）不动，仍为不可变真相源。

    市场门数据面同样 patch 两层（2026-10-09 逐槽 as-of）:
      `load_market_gain`（日频回退）+ `load_market_slots`（逐槽）—— 两者都填**同一标量**
      `mkt`，故逐槽序列 == 日频标量 ⇒ 冻结输出逐位不变（顺带覆盖新逐槽路径）。
    """
    import unittest.mock as mock

    import app.market_cn.auto.core.backtest as bt
    import app.market_cn.auto.core.data.frames as fr_mod
    import app.market_cn.auto.core.data.hub as hub_mod
    import app.market_cn.auto.core.replay as replay_mod
    import app.market_cn.auto.core.replay.mkt_slots as mktslots_mod
    from app.market_cn.auto import strategies as reg

    reg.autodiscover()
    strat = reg.get_strategy(spec["strategy"])
    if strat is None:
        raise RuntimeError("策略未注册: %s" % spec["strategy"])
    code = spec["code"]
    bars = spec["build"]()
    rows_by_date = spec["day_rows"]()
    mkt = spec["mkt_gain"]
    dates = sorted(rows_by_date)
    d0 = dates[0]
    # 引擎首日 as-of 语义（core/backtest.py 2026-09-10 修复）：trading_dates 必须
    # **含覆盖起点前一交易日**，否则 prev_date=None → as-of 含当日 = 未来函数。
    prev_td = [str(b["time"])[:10] for b in bars if str(b["time"])[:10] < d0][-1:]

    def _daily(c, n=300, **k):
        return list(bars)

    def _load_minutes(c, start, end):
        lo, hi = str(start)[:10], str(end)[:10]
        return {d: rows for d, rows in rows_by_date.items() if lo <= d <= hi}

    def _snap_series(date, rows, pc, lo_hhmm="09:31", hi_hhmm="15:00"):
        # 合成行已是**快照形**（与生产 frame.snap 同形），直接透传（不再重建累计量）
        return [dict(r) for r in rows]

    with mock.patch.object(fr_mod, "trading_dates",
                           lambda *a, **k: prev_td + dates), \
            mock.patch.object(fr_mod, "first_1m_date", lambda: None), \
            mock.patch.object(fr_mod, "load_code_minutes", _load_minutes), \
            mock.patch.object(fr_mod, "snap_series", _snap_series), \
            mock.patch.object(replay_mod, "load_market_gain",
                              lambda *a, **k: {d0: mkt}), \
            mock.patch.object(mktslots_mod, "load_market_slots",
                              lambda *a, **k: {d0: {str(r["time"])[11:16]: mkt
                                                    for r in rows_by_date[d0]}}), \
            mock.patch.object(hub_mod, "daily", _daily):
        out = bt.run_all_intraday(strat, days=len(bars) + 5, codes=[code],
                                  start_date=str(bars[0]["time"])[:10],
                                  end_date=str(bars[-1]["time"])[:10])
    if isinstance(out, dict):
        return list(out.get("trades") or [])
    return list(out or [])


if __name__ == "__main__":
    sys.exit(freeze(check_only="--check" in sys.argv))

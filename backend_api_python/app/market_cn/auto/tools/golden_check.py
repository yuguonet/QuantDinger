"""golden_check — 回放引擎的 golden 门禁（改进方案 §4-P1，全新版本口径）。

⚠ 本工具**不与旧 backtest_stock 做逐笔对拍**（§2.2.1）：旧回测的出场重放从入场
当日（D1）起评，违反 T+1；拿它当基准只会「以错对错」，把缺陷焊进新引擎。
本门禁校验的是**不变量**——这些是任何正确回测都必须满足的硬性质：

  A. T+1      : exit_day >= 2（A 股当日买不可当日卖）
  B. 价格有效 : entry_price / exit_price 均 > 0
  C. 收益自洽 : return_pct == (exit_price/entry_price-1)*100（1e-6）
  D. 顺序单调 : 同一票内 entry_date <= exit_date，且下一笔 entry_date > 上一笔 exit_date
  E. 非重叠   : 持仓区间不重叠（去重口径）
  F. 字段完备 : canonical schema 必填字段非 None（除 peak_return_pct/exit_day 允许 None）

另外提供 ``--truth`` 模式（**等价性的硬证据**）：拿库内 ``qd_dragon_signals`` 的
生产信号当事实，回放同一只票，检查 ready 事件是否命中同一天。不变量只能证明
"没写错"，truth 能证明"和生产的判定一致"。

  ⚠ 零样本一律记 NO_SAMPLE，**不得计 PASS** —— 空集对空集是假绿（knife 当前库内
  0 行，就是这个情况，必须显式暴露而不是静默通过）。

用法:
    python -m app.market_cn.auto.tools.golden_check --strategy dragon_callback --n 60
    python -m app.market_cn.auto.tools.golden_check --all
    python -m app.market_cn.auto.tools.golden_check --truth
"""

from __future__ import annotations

import argparse
import sys

#: 收益容差：策略侧 return_pct 已 round 到 2 位小数，与精确值最大差 0.005
#: （不是逻辑误差；用 1e-6 会误报——见 §「C 收益自洽」）
#: batch 池样本票数（g56 的横截面分位需要同日其他票；太大拖慢，太小分位失真）
_POOL_N = 120

#: 收益容差（C 收益自洽用）—— 2026-10-07 补：原先此处只有一个孤零零的注释、变量从未
#: 定义 ⇒ `_check_trades` 一走到 C 检查就 NameError，本门禁只要有 trade 产出就崩
#: （零 trade 时 C 检查被 `r is None` 跳过 ⇒ 只有「空样本」能假绿）。
EPS = 0.005

REQUIRED = ("code", "strategy", "d0_date", "entry_date", "entry_price",
            "exit_date", "exit_price", "exit_reason", "return_pct")


def _check_trades(code: str, trades: list[dict]) -> list[str]:
    """返回违规描述列表（空 = 通过）。"""
    bad: list[str] = []
    prev_exit = ""
    for i, t in enumerate(trades):
        tag = f"{code}#{i}({t.get('entry_date')}→{t.get('exit_date')})"
        # F 字段完备
        for k in REQUIRED:
            if t.get(k) is None:
                bad.append(f"{tag} 缺字段 {k}")
        # B 价格有效
        ep, xp = t.get("entry_price") or 0, t.get("exit_price") or 0
        if ep <= 0:
            bad.append(f"{tag} entry_price<=0 ({ep})")
            continue
        if xp <= 0:
            bad.append(f"{tag} exit_price<=0 ({xp})")
            continue
        # A T+1
        d = t.get("exit_day")
        if d is not None and int(d) < 2:
            bad.append(f"{tag} 违反 T+1 (exit_day={d})")
        # C 收益自洽
        r = t.get("return_pct")
        if r is not None and abs(float(r) - (xp / ep - 1) * 100) > EPS:
            bad.append(f"{tag} return_pct 不自洽 ({r} vs {(xp/ep-1)*100:.4f})")
        # D/E 顺序与重叠
        ed, xd = str(t.get("entry_date") or ""), str(t.get("exit_date") or "")
        if ed and xd and ed > xd:
            bad.append(f"{tag} entry_date > exit_date")
        if prev_exit and ed and ed <= prev_exit:
            bad.append(f"{tag} 与上一笔持仓重叠 (prev_exit={prev_exit})")
        prev_exit = xd or prev_exit
    return bad


def run(strategy_key: str, n: int = 60, days: int = 300) -> dict:
    from app.market_cn.auto import strategies as reg
    from app.market_cn.auto.core.data.hub import all_codes, daily
    from app.market_cn.auto.core.replay import DailyFeed, TradesCollector, replay

    reg.autodiscover()
    strat = reg.get_strategy(strategy_key)
    if strat is None:
        return {"strategy": strategy_key, "error": "未注册"}
    if not hasattr(strat, "init_state"):
        return {"strategy": strategy_key, "skipped": "未迁移折叠契约"}

    codes = (all_codes() or [])[:n]
    total_trades = 0
    all_bad: list[str] = []
    scanned = 0
    for code in codes:
        try:
            bars = daily(code, days)
        except Exception:
            continue
        if not bars or len(bars) < 60:
            continue
        scanned += 1
        try:
            res = replay(strat, code, DailyFeed(bars),
                         collectors=[TradesCollector(code, strategy_key)])
        except Exception as e:
            all_bad.append(f"{code} replay 异常: {type(e).__name__}: {e}")
            continue
        total_trades += len(res.trades)
        all_bad.extend(_check_trades(code, res.trades))
    return {"strategy": strategy_key, "scanned": scanned,
            "trades": total_trades, "violations": all_bad}


def _load_truth() -> dict[str, list[tuple[str, str]]]:
    """库内生产信号 → {strategy: [(code, trade_date), ...]}。"""
    import os
    env = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "..", "..", "..", ".env")
    env = os.path.normpath(env)
    if os.path.exists(env) and not os.environ.get("DATABASE_URL"):
        for line in open(env, encoding="utf-8", errors="replace"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if k.strip() == "DATABASE_URL":
                    os.environ["DATABASE_URL"] = v.strip()
                    break
    from app.utils.db_postgres import get_pg_connection_sync
    cur = get_pg_connection_sync().cursor()
    cur.execute("SELECT strategy, code, trade_date FROM qd_dragon_signals")
    out: dict[str, list[tuple[str, str]]] = {}
    for r in cur.fetchall():
        out.setdefault(str(r["strategy"]), []).append(
            (str(r["code"]), str(r["trade_date"])[:10]))
    return out


def run_truth(days: int = 300) -> list[dict]:
    """生产信号对账：回放是否复现同一天的 ready。"""
    from app.market_cn.auto import strategies as reg
    from app.market_cn.auto.core.data.hub import daily
    from app.market_cn.auto.core.replay import DailyFeed, TradesCollector, replay
    from app.market_cn.auto.core.replay import (DailyFeed, TradesCollector, replay,
                                                replay_batch, load_market_gain)
    from app.market_cn.auto.core.replay.intraday import make_feed
    from app.market_cn.auto.strategies.base import StrategyBase

    reg.autodiscover()
    try:
        truth = _load_truth()
    except Exception as e:
        return [{"strategy": "*", "error": f"库读取失败: {type(e).__name__}: {e}"}]

    mk = load_market_gain("2000-01-01", "2099-12-31")
    report: list[dict] = []
    for skey, rows in sorted(truth.items()):
        strat = reg.get_strategy(skey)
        if strat is None or not hasattr(strat, "init_state"):
            report.append({"strategy": skey, "skipped": "未注册或未迁移折叠契约",
                           "total": len(rows)})
            continue
        intraday = getattr(strat.scan_spec, "kind", "") == "intraday_window"
        # 覆写了 begin_day = 需要横截面池（g56）⇒ 单票 replay 无池，必须走 batch
        pooled = getattr(type(strat), "begin_day", None) is not getattr(
            StrategyBase, "begin_day", None)
        hit, miss, err = 0, [], 0
        pool_cache: dict[str, list] = {}
        for code, td in rows:
            try:
                bars = daily(code, days)
            except Exception:
                err += 1
                continue
            # ⚠ 不设「最少 60 根」门槛：次新股只有 9 根（601091 上市 2026-09-17）也照样
            #   能出信号（ready 2026-09-30，与库内一致）。设门槛会把它误记成 err。
            if not bars or len(bars) < 2:
                err += 1
                continue
            try:
                if pooled:
                    # 池 = 该票 + 样本票（横截面分位数需要同日的其他票）
                    if not pool_cache:
                        from app.market_cn.auto.core.data.hub import all_codes, daily as _d
                        for c in (all_codes() or [])[:_POOL_N]:
                            try:
                                b2 = _d(c, days)
                            except Exception:
                                continue
                            if b2 and len(b2) >= 60:
                                pool_cache[c] = b2
                    pool = dict(pool_cache)
                    pool[code] = bars
                    res = replay_batch(strat, pool, collectors={
                        c: [TradesCollector(c, skey)] for c in pool}).get(code)
                    if res is None:
                        err += 1
                        continue
                else:
                    feed = (make_feed(strat, code, bars, mkt_map=mk) if intraday
                            else DailyFeed(bars, mkt_map=mk))
                    res = replay(strat, code, feed,
                                 collectors=[TradesCollector(code, skey)])
            except NotImplementedError:
                report.append({"strategy": skey, "skipped": "未迁移折叠契约",
                               "total": len(rows)})
                hit = -1
                break
            except Exception as e:
                err += 1
                miss.append(f"{code}@{td} 异常 {type(e).__name__}: {e}")
                continue
            dates = {str(e["date"])[:10] for e in res.events
                     if e.get("stage") == "ready"}
            if td in dates:
                hit += 1
            else:
                near = sorted(dates)[-3:] if dates else []
                miss.append(f"{code}@{td} 未复现 (最近 ready={near})")
        if hit >= 0:
            report.append({"strategy": skey, "total": len(rows), "hit": hit,
                           "miss": miss, "err": err, "pooled": pooled})
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="回放引擎 golden 门禁")
    ap.add_argument("--strategy", default="dragon_callback")
    ap.add_argument("--all", action="store_true", help="对全部已迁移策略跑一遍")
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--days", type=int, default=300)
    ap.add_argument("--truth", action="store_true",
                    help="用库内生产信号对账（等价性硬证据）")
    a = ap.parse_args(argv)

    if a.truth:
        rep = run_truth(days=a.days)
        failed = 0
        for r in rep:
            k = r["strategy"]
            if r.get("error"):
                print(f"[{k}] ERROR {r['error']}"); failed += 1; continue
            if r.get("skipped"):
                print(f"[{k}] SKIP {r['skipped']} (库内 {r['total']} 行)"); continue
            tot, hit, err = r["total"], r["hit"], r["err"]
            if tot == 0:
                print(f"[{k}] NO_SAMPLE  库内无信号 —— 不算通过（无证据）"); continue
            rate = hit / tot
            status = "PASS" if hit == tot else f"FAIL({tot-hit})"
            print(f"[{k}] {status}  命中 {hit}/{tot} ({rate:.0%})  异常 {err}"
                  f"{'  [横截面池]' if r.get('pooled') else ''}")
            for line in r["miss"][:8]:
                print(f"    - {line}")
            if hit != tot:
                failed += 1
        return 1 if failed else 0

    keys = (["dragon_callback", "g56", "knife_catch", "tail_oversold", "break"]
            if a.all else [a.strategy])
    failed = 0
    for k in keys:
        r = run(k, n=a.n, days=a.days)
        if r.get("error") or r.get("skipped"):
            print(f"[{k}] {r.get('error') or r.get('skipped')}")
            continue
        v = r["violations"]
        status = "PASS" if not v else f"FAIL({len(v)})"
        print(f"[{k}] {status}  扫描 {r['scanned']} 票 / {r['trades']} 笔")
        for line in v[:10]:
            print(f"    - {line}")
        if v:
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

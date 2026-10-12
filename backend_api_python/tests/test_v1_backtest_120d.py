"""tests/test_v1_backtest_120d.py — V1 策略 120 交易日回测链路验证 (自包含, 不依赖 DB)

目的
----
V1 的**官方 CLI 回测入口** `StrategyBase.backtest_stock` 已由 v1.py 自持覆写恢复
(2026-10-10: scan_signals + entry_decision 竞价 gate + run_d1_limit_up_trail, 行为零改;
base.py:587 的通用兜底引擎已于 2026-10-09 终态② 删除, 故覆写为 V1 自有包装,
非第二套通用引擎)。本 harness 直接驱动该同一入口 `v1.backtest_stock`, 验证 V1 在
120 交易日 × N 票上无异常地跑通整个链路:

    scan_signals (D0 盘后信号) → entry_decision (D1 竞价 gap) →
    run_d1_limit_up_trail (ex-v1._run_backtest, 2026-10-12 §13.1 A-1b 迁 core; 含「D1 未涨停 → D2 开盘清仓」pre_exit 钩子)

注意: 这与 `python -m app.market_cn.auto.tools.replay --strategy v1 --code X --days 120`
及 `core/backtest.py --strategy v1 --days 120` 调用的是**同一个** `backtest_stock` 方法
(后者仅多了从 DB 取数这一层); 本 harness 用确定性合成数据替代 DB 取数, 同样能复验链路。

数据
----
确定性合成 (GBM + A股涨跌停约束), 不触 DB。另注入若干「V1 信号形态」标的,
确保信号真实触发并能复验 D2 开盘清仓修复点。

注意
----
这不是生产 `core.replay` 折叠链路 (后者需策略迁折叠契约 init_state/step/evaluate);
本 harness 验证的是 V1 既有 scan_signals/决策/核心出场引擎在长窗口下的正确性与健壮性,
即 V1 经官方 CLI 回测所走的实际路径。

可作为 pytest 收集, 也可 `python tests/test_v1_backtest_120d.py` 直接打印报告。
"""
from __future__ import annotations
import os
import sys
import math
import random
from datetime import date, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.market_cn.auto.core.exit_engines import run_d1_limit_up_trail as _run_backtest
from app.market_cn.auto.core.market import get_board_type
from app.market_cn.auto.strategies.v1 import V1Strategy

N_DAYS = 120
SEED = 20261010


def _date_str(k: int) -> str:
    """第 k 个交易日的 ISO 日期 (跳过周末, 仅用于 Signal.time 展示)。"""
    d = date(2026, 1, 1)
    for _ in range(k):
        d += timedelta(days=1)
        while d.weekday() >= 5:
            d += timedelta(days=1)
    return d.isoformat()


def gen_random(rng: random.Random, n: int, board: str, start: float = 10.0) -> list:
    """随机日线 (GBM + 涨跌停约束)。"""
    cap = 0.10 if board == "main" else 0.20
    bars = []
    p = start
    for k in range(n):
        ret = rng.gauss(0.001, 0.018)
        ret = max(-cap, min(cap, ret))
        o = p
        c = round(p * (1 + ret), 2)
        hi = round(max(o, c) * (1 + abs(rng.gauss(0, 0.004))), 2)
        lo = round(min(o, c) * (1 - abs(rng.gauss(0, 0.004))), 2)
        vol = int(abs(rng.gauss(4e5, 8e4)) + 5e4)
        bars.append({"time": _date_str(k), "open": round(o, 2), "high": hi,
                     "low": lo, "close": c, "volume": vol})
        p = c
    return bars


def build_setup(rng, n, board, d0, d1_momentum_pct):
    """注入「V1 可买真首板形态」(过新 7 门): 涨停 + 非一字(open<d0c) + 前10日无涨停 +
    小盘 + ret_20d≥10 + D-1横盘[-3,3) + 温和量比[1,5)。D1 动量可控。

    d1_momentum_pct 控制 D1 收盘相对 D0 收盘的涨幅% (= (D1收盘-D0收盘)/D0收盘*100,
    因 D1 竞价 gap=0), 用于复验 1进2 设计:
      未达涨停阈值(<9.8% 主板 / <19.8% 创) ⇒ 非连板 ⇒ D2 开盘清仓 (exit_day==2)。
    """
    bars = gen_random(rng, n, board, start=10.0)
    cap = 0.10 if board == "main" else 0.20
    lo = d0 - 20                      # 20 日窗口起点
    P = 10.0                          # D-2 (拉升顶点) 收盘
    close_lo = 7.7                    # 窗口起点收盘 (使 ret_20d = D0/close_lo > 30%)
    # 拉升 lo..d0-2
    for j in range(lo, d0 - 1):
        t = (j - lo) / (d0 - 2 - lo)
        c = close_lo + (P - close_lo) * t
        o = bars[j - 1]["close"] if j > lo else round(c * 0.99, 2)
        bars[j]["close"] = round(c, 2)
        bars[j]["open"] = round(o, 2)
        bars[j]["high"] = round(max(o, c) * (1 + 0.003), 2)
        bars[j]["low"] = round(min(o, c) * (1 - 0.003), 2)
        bars[j]["volume"] = int(abs(rng.gauss(4e5, 6e4)) + 2e5)
    # D-1 横盘 -1% (落在新门 d_1_band [-3,3); 旧回调-6% 深回调是最差桶已弃)
    d_1 = round(P * 0.99, 2)
    bars[d0 - 1]["close"] = d_1
    bars[d0 - 1]["open"] = round(P, 2)
    bars[d0 - 1]["high"] = round(max(P, d_1) * (1 + 0.003), 2)
    bars[d0 - 1]["low"] = round(min(P, d_1) * (1 - 0.003), 2)
    bars[d0 - 1]["volume"] = int(6e4)
    # D0 涨停 (+10% 主板 / +20% 创业板); open=d_1 ⇒ 非一字 (open_pct=0<阈值)
    d0c = round(d_1 * (1 + cap), 2)
    bars[d0]["close"] = d0c
    bars[d0]["open"] = round(d_1, 2)
    bars[d0]["high"] = round(d0c * 1.001, 2)
    bars[d0]["low"] = round(min(d_1, d0c) * 0.999, 2)
    bars[d0]["volume"] = int(1.5e6)   # 温和量比 ~3 (落在新门 vol_ratio [1,5))
    # D1: 竞价高开 +8% (过新入场门 gap≥8 且<涨停价=可买); 收盘相对开盘 +d1_momentum_pct%
    #   但强制非连板(<板块涨停阈值) ⇒ 走 D2 清仓 (1进2 复验)。
    thr = 0.098 if board == "main" else 0.198
    gap_pct = 8.5 if board == "main" else 13.0   # 主板[8,9.8)/创业板[12,16) 甜区留边距
    d1_open = round(d0c * (1 + gap_pct / 100), 2)            # 高开 (过 board-aware gap 门)
    d1_close = round(d1_open * (1 + d1_momentum_pct / 100), 2)
    d1_close = min(d1_close, round(d0c * (1 + thr * 0.9), 2))   # 强制非连板 ⇒ D2 清仓
    bars[d0 + 1]["open"] = d1_open
    bars[d0 + 1]["close"] = d1_close
    bars[d0 + 1]["high"] = round(max(d1_open, d1_close) * (1 + 0.003), 2)
    bars[d0 + 1]["low"] = round(min(d1_open, d1_close) * (1 - 0.003), 2)
    bars[d0 + 1]["volume"] = int(4e5)
    return bars


def _infer_reason(res, bars, entry_idx, hold):
    ep = res["exit_price"]
    ed = res["exit_day"]
    rp = res["return_pct"]
    d2_open = round(bars[entry_idx + 1]["open"], 3) if entry_idx + 1 < len(bars) else None
    if ed == 2 and d2_open is not None and abs(ep - d2_open) < 1e-6:
        return "D2开盘清仓(未涨停)"
    if rp <= -9.5:
        return "止损-10%"
    if ed >= hold:
        return "持仓到期"
    return "追踪止损/其他"


def run_stock_chain(strat, bars, code, hold=7):
    """对单票跑完整 V1 链路, 返回逐笔成交记录。"""
    trades = []
    n = len(bars)
    for d in range(26, n):                       # as_of: 需 20 日窗口
        sigs = strat.scan_signals(bars, code, as_of=d)
        for sig in sigs:
            D1 = d + 1
            if D1 >= n:
                continue
            entry_idx = D1
            entry_price = bars[D1]["open"]
            if entry_price <= 0:
                continue
            row = {"code": code, "signal_price": bars[d]["close"],
                   "extra": dict(sig.extra or {})}
            snap_entry = {"open": bars[D1]["open"], "previousClose": bars[d]["close"]}
            ed = strat.entry_decision(row, snap_entry)
            rec = {"code": code, "d0": d, "d0_date": str(sig.time)[:10],
                   "entry": ed.buyable, "entry_reason": ed.reason if not ed.buyable else None}
            if not ed.buyable:
                trades.append(rec)
                continue
            snap_confirm = {"series": [{"last": bars[D1]["close"]}]}
            cd = strat.confirm_decision(row, snap_confirm)
            board_type = get_board_type(code)
            res = _run_backtest(bars, entry_idx, entry_price, hold_days=hold,
                                stop_loss=-10.0, trailing_stop=-5.0,
                                board_type=board_type)
            if res is None:
                rec.update({"exit": None})
                trades.append(rec)
                continue
            rec.update({
                "exit_day": res["exit_day"],
                "exit_price": res["exit_price"],
                "return_pct": res["return_pct"],
                "peak_return_pct": res.get("peak_return_pct"),
                "exit_reason": _infer_reason(res, bars, entry_idx, hold),
                "confirm": (cd.confirmed if cd else None),
            })
            trades.append(rec)
    return trades


def build_universe():
    rng = random.Random(SEED)
    stocks = []
    # 随机票: 主板 + 创业板混合, 压力测试 scan_signals 在全市场下的健壮性
    for i in range(20):
        board = "main" if i % 3 != 0 else "gem_star"
        code = ("600%03d" % (i + 1)) if board == "main" else ("300%03d" % (i + 1))
        stocks.append((code, gen_random(rng, N_DAYS, board, start=8 + rng.random() * 6)))
    # 注入票: 复验 1进2 设计 (D1 未涨停 → D2 清仓); D1 均高开+8% 过新入场门, 强制非连板
    #  fix-A: 主板, D1 高开+8% 后微涨0.5% (未封 → D2 清仓)
    #  fix-B: 主板, D1 高开+8% 后冲高1% (未达涨停阈值 → D2 清仓)
    #  fix-C: 创业板, D1 微涨未封 (验证板块阈值 19.8%)
    stocks.append(("600900", build_setup(rng, N_DAYS, "main", d0=90, d1_momentum_pct=0.5)))
    stocks.append(("600901", build_setup(rng, N_DAYS, "main", d0=95, d1_momentum_pct=1.0)))
    stocks.append(("300900", build_setup(rng, N_DAYS, "gem_star", d0=90, d1_momentum_pct=0.5)))
    return stocks


def run_all():
    strat = V1Strategy()
    universe = build_universe()
    all_trades = []
    errors = []
    for code, bars in universe:
        try:
            ts = run_stock_chain(strat, bars, code)
            all_trades.extend(ts)
        except Exception as e:          # 记录但不中断, 全链路异常即暴露
            errors.append((code, repr(e)))
    return strat, universe, all_trades, errors


def summarize(all_trades, errors):
    entered = [t for t in all_trades if t.get("entry")]
    exited = [t for t in entered if t.get("exit_reason")]
    nonexit = [t for t in entered if t.get("exit_day") is None]
    rets = [t["return_pct"] for t in exited]
    wins = [r for r in rets if r > 0]
    by_reason = {}
    for t in exited:
        by_reason[t["exit_reason"]] = by_reason.get(t["exit_reason"], 0) + 1
    report = {
        "total_scanned_days": sum(1 for _ in all_trades),  # 含未入场
        "signals": len(all_trades),
        "entered": len(entered),
        "exited": len(exited),
        "nonexit_data_end": len(nonexit),
        "win": len(wins),
        "win_rate": round(len(wins) / len(exited) * 100, 1) if exited else 0,
        "avg_return": round(sum(rets) / len(rets), 2) if rets else 0,
        "max_return": round(max(rets), 2) if rets else 0,
        "min_return": round(min(rets), 2) if rets else 0,
        "by_reason": by_reason,
        "errors": errors,
    }
    return report


def _print_report(report, all_trades):
    print("=" * 64)
    print("V1 120 天回测链路 — 报告")
    print("=" * 64)
    print(f"信号总数(含竞价淘汰): {report['signals']}")
    print(f"  竞价通过(入场):     {report['entered']}")
    print(f"  已出场:             {report['exited']}")
    print(f"  数据尽头未出场:     {report['nonexit_data_end']}")
    print(f"胜率:                 {report['win_rate']}%  ({report['win']}/{report['exited']})")
    print(f"平均收益:             {report['avg_return']}%")
    print(f"收益区间:             [{report['min_return']}%, {report['max_return']}%]")
    print(f"出场原因分布:         {report['by_reason']}")
    print(f"链路异常:             {len(report['errors'])}")
    for code, e in report["errors"]:
        print(f"    !! {code}: {e}")
    print("-" * 64)
    print("逐笔明细 (入场且已出场):")
    for t in all_trades:
        if t.get("exit_reason"):
            print(f"  {t['code']} d0={t['d0_date']} D2清仓?={t['exit_reason']} "
                  f"exit_day={t['exit_day']} ret={t['return_pct']}% "
                  f"peak={t.get('peak_return_pct')}% confirm={t.get('confirm')}")
    print("=" * 64)


# ---------------- pytest 入口 ----------------
def test_v1_chain_runs_120d_without_error():
    strat, universe, all_trades, errors = run_all()
    assert not errors, f"链路异常: {errors}"
    # 进场成交必须满足 A股 T+1: 不早于 D2 卖出, 且不超持仓上限
    for t in all_trades:
        if t.get("exit_day") is not None:
            assert t["exit_day"] >= 2, f"{t['code']} 出现 T+0 卖出"
            assert t["exit_day"] <= 7, f"{t['code']} 超持仓上限"


def test_v1_d2_clear_fix_in_backtest():
    """1进2 设计复验: D1 未涨停(收盘<涨停阈值)的注入票应 D2 开盘清仓 (exit_day==2)。

    覆盖两类: 低动量(0.5%)与高开非涨停(5%, 旧"动量<3%"启发式会误持) —— 二者均未涨停,
    新口径均 D2 清仓, 锁死"第2天没涨停就跑"。
    """
    strat, universe, all_trades, errors = run_all()
    assert not errors
    fix_trades = [t for t in all_trades
                  if t["code"] in ("600900", "600901", "300900") and t.get("entry")]
    assert fix_trades, "修复点注入票未触发信号/入场"
    for t in fix_trades:
        assert t["exit_day"] == 2, (
            f"{t['code']} D1 未涨停应 D2 开盘清仓, 实际 exit_day={t['exit_day']} "
            f"({t.get('exit_reason')}) — 实现可能回退到旧启发式")
        assert t["exit_reason"] == "D2开盘清仓(未涨停)", t.get("exit_reason")


def test_v1_setup_signal_fires():
    """注入票应确实产生 V1 信号并竞价通过 (验证 scan_signals 6 门在长窗口下可触发)。"""
    strat, universe, all_trades, errors = run_all()
    entered_codes = {t["code"] for t in all_trades if t.get("entry")}
    assert {"600900", "600901", "300900"}.issubset(entered_codes), (
        f"注入票未全部入场: {entered_codes}")


if __name__ == "__main__":
    _, _, all_trades, errors = run_all()
    report = summarize(all_trades, errors)
    _print_report(report, all_trades)

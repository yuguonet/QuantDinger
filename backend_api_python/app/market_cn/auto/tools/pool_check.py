#!/usr/bin/env python3
"""auto/tools/pool_check.py — 信号级复验闸门 (2026-09-12, 固化「09-12 信号级复验纪律」)

用途: 任何归因发现 (标签级 / 池级 alpha) 要实装成 prefilter 门之前, 必须先对基准策略的
     *实际交易清单* (信号池) 做信号级复验 —— 后处理池切分, 成本≈0 (只 hub 取数,
     不重跑回测)。

根因 (三实例教训): 归因宇宙 (全样本) ≠ 策略宇宙 (规则选择后的信号池)。已有三例
     (turnover 门 / env 深弱门 / LHB 门) 池级显著但信号级不迁移 → 直接否决。
     不迁移 = 该特征在「策略真会选中的股票」上无区分力, 实装即引入选择效应毒药。

判定 (迁移 vs 否决):
  - universe = 窗口内全部交易; kept = 通过全部门的交易; dropped = universe - kept
  - MIGRATE (可实装): kept.avg_ret > universe.avg_ret 且 dropped.avg_ret < universe.avg_ret
    且 kept.n >= MIN_N; 窗内按信号日中位切两段, 两段均改善 → 稳定 (单段改善=过拟合预警)
  - VETO (不实装): 上述任一不满足 → 信号级不迁移, 不接入

门语义:
  - 默认 fail-open: 特征不可知 (None) → 视为通过 (保留), 避免指数/数据故障误杀 env 类门
  - --fail-close: 特征不可知 → 视为不通过 (否决), 用于必要条件门 (如 turnover circ 缺失)

特征注册表 (全 D-1 可知, 经 hub; 模块加载零 IO, hub 调用惰性化):
  - env_ret20       : 指数 20 日收益%            (hub.index_daily)
  - ev20            : 个股 20 交易日龙虎榜上榜次数 (hub.lhb)
  - on_d1           : D-1 恰上榜标志 0/1          (hub.lhb)
  - turnover_d0     : 确认日换手%                (优先交易 turnover_sig, 缺失回退 daily×stock_info)
  - fflow_main_net  : 指数主力净额累计(元) D-1 收盘 (hub.index_fflow)  ← 新资金流通道
  - fflow_main_ratio: 主力净额占四档绝对值和之比% (归一, 无单位)          (hub.index_fflow)

用法:
  # 消费已有清单 (推荐: 先把 backtest 结果 json.dump 出来)
  python pool_check.py --trades tmp/break_v1_600d_trades.json --gate "ev20>=6" --fail-close
  # 多门 AND + 双窗
  python pool_check.py --trades T.json --gate "env_ret20<=-0.05" --gate "fflow_main_net>0"
  # 重跑基准策略并复验 (重任务, 交用户终端跑, 沙箱会爆内存)
  python pool_check.py --strategy break --days 600 --gate "env_ret20<=-0.05"
  # 演示 / 自测 (无 DB, 合成清单, 验证切分+裁定逻辑)
  python pool_check.py --demo
输出: stdout + tmp/pool_check_<GATE>.md
"""
from __future__ import annotations

import argparse
import json
import os
import sys

PROJ = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
if PROJ not in sys.path:
    sys.path.insert(0, PROJ)

MIN_N = 10          # kept 池最小样本, 小于此视为噪声不可信
DEFAULT_WINDOWS = (600, 300)


# ================================================================
# 特征注册表 (惰性 hub 调用, 模块加载零 IO)
# ================================================================

def _feat_env_ret20(code, d0, trade, index_code="000300"):
    try:
        from app.market_cn.auto.data.hub import index_daily
        bars = index_daily(index_code, days=60, as_of=d0) or []
        c = [float(b["close"]) for b in bars]
        if len(c) >= 21 and c[-21] > 0:
            return (c[-1] / c[-21] - 1) * 100
    except Exception:
        return None
    return None


def _feat_ev20(code, d0, trade, index_code="000300"):
    try:
        from app.market_cn.auto.data.hub import lhb
        evs = lhb(code, days=40, as_of=d0) or []     # D-1 及以前的上榜事件
        # 取截至 D-1 前 20 交易日内次数 (用事件日期 <= d0 且落在窗口)
        cnt = 0
        for e in evs:
            ed = str(e.get("trade_date") or e.get("date") or "")[:10]
            if ed <= str(d0)[:10]:
                cnt += 1
            if cnt >= 40:
                break
        return float(cnt)
    except Exception:
        return None
    return None


def _feat_on_d1(code, d0, trade, index_code="000300"):
    try:
        from app.market_cn.auto.data.hub import lhb
        evs = lhb(code, days=5, as_of=d0) or []
        for e in evs:
            ed = str(e.get("trade_date") or e.get("date") or "")[:10]
            if ed == str(d0)[:10]:
                return 1.0
    except Exception:
        return 0.0
    return 0.0


def _feat_turnover_d0(code, d0, trade, index_code="000300"):
    # 优先用交易自身已算好的 turnover_sig (信号时已知)
    v = trade.get("turnover_sig")
    if v is not None:
        try:
            return float(v)
        except Exception:
            pass
    # 回退: daily 成交量 / 流通股本
    try:
        from app.market_cn.auto.data.hub import daily, stock_info
        bars = daily(code, 30, as_of=d0) or []
        info = (stock_info() or {}).get(code) or {}
        circ = float(info.get("circ_shares") or 0)
        if circ > 0 and bars:
            return float(bars[-1]["volume"]) / circ * 100
    except Exception:
        return None
    return None


def _fflow_bars(index_code, as_of):
    from app.market_cn.auto.data.hub import index_fflow
    return index_fflow(index_code, days=10, as_of=as_of) or []


def _feat_fflow_main_net(code, d0, trade, index_code="000300"):
    try:
        bars = _fflow_bars(index_code, d0)
        d0s = str(d0)[:10]
        prev = [b for b in bars if str(b["time"])[:10] < d0s]
        if prev:
            return float(prev[-1]["main_net"])
    except Exception:
        return None
    return None


def _feat_fflow_main_ratio(code, d0, trade, index_code="000300"):
    try:
        bars = _fflow_bars(index_code, d0)
        d0s = str(d0)[:10]
        prev = [b for b in bars if str(b["time"])[:10] < d0s]
        if prev:
            b = prev[-1]
            denom = (abs(b["super_net"]) + abs(b["big_net"]) +
                     abs(b["mid_net"]) + abs(b["small_net"]))
            if denom > 0:
                return float(b["main_net"]) / denom * 100
    except Exception:
        return None
    return None


FEATURES = {
    "env_ret20": _feat_env_ret20,
    "ev20": _feat_ev20,
    "on_d1": _feat_on_d1,
    "turnover_d0": _feat_turnover_d0,
    "fflow_main_net": _feat_fflow_main_net,
    "fflow_main_ratio": _feat_fflow_main_ratio,
}


# ================================================================
# 门解析 / 富集 / 指标 / 裁定
# ================================================================

_OPS = {"<=": lambda a, b: a <= b, ">=": lambda a, b: a >= b,
        "<": lambda a, b: a < b, ">": lambda a, b: a > b,
        "==": lambda a, b: a == b, "!=": lambda a, b: a != b}


def parse_gate(spec):
    """'name>=val' / 'name<=-0.05' → (name, op_fn, val)."""
    for op in ("<=", ">=", "==", "!=", "<", ">"):
        if op in spec:
            name, val = spec.split(op, 1)
            return name.strip(), _OPS[op], float(val.strip())
    raise ValueError(f"无法解析门: {spec!r} (需含 <=/>=/</>/==/!=)")


def enrich(trades, gate_names, index_code="000300"):
    """给每笔交易算特征, 存 trade['_feat']。特征缺失=网络/数据故障, 记 None。"""
    for t in trades:
        d0 = t.get("signal_date") or t.get("entry_date") or ""
        feats = {}
        for name in gate_names:
            fn = FEATURES.get(name)
            if fn is None:
                raise ValueError(f"未知特征: {name} (可用: {sorted(FEATURES)})")
            try:
                feats[name] = fn(t.get("code"), d0, t, index_code)
            except Exception:
                feats[name] = None
        t["_feat"] = feats
    return trades


def _stats(trs):
    if not trs:
        return (0, float("nan"), float("nan"), float("nan"))
    r = [float(t["return_pct"]) for t in trs]
    ws = [x for x in r if x > 0]
    ls = [x for x in r if x <= 0]
    pl = (sum(ws) / len(ws)) / abs(sum(ls) / len(ls)) if ws and ls and sum(ls) else 999.0
    return (len(r), (sum(1 for x in r if x > 0) / len(r) * 100), sum(r) / len(r), pl)


def _pass(trade, gates, fail_close):
    for name, op, val in gates:
        fv = trade["_feat"].get(name)
        if fv is None:
            if fail_close:
                return False          # 必要条件不可知 → 否决
            continue                  # fail-open → 视为通过
        if not op(fv, val):
            return False
    return True


def _window(trades, n):
    dates = sorted({str(t.get("signal_date") or "")[:10] for t in trades} - {""})
    if len(dates) > n:
        start = dates[len(dates) - n]
        return [t for t in trades if (str(t.get("signal_date") or "")[:10]) >= start]
    return trades


def verdict(universe, kept, dropped):
    """返回 (label, reasons[])。"""
    nu, wu, au, _ = _stats(universe)
    nk, wk, ak, _ = _stats(kept)
    nd, wd, ad, _ = _stats(dropped)
    reasons = []
    migrate = True
    if nk < MIN_N:
        migrate = False
        reasons.append(f"kept.n={nk} < MIN_N={MIN_N} (样本不足, 噪声)")
    if not (ak > au):
        migrate = False
        reasons.append(f"kept.avg_ret={ak:+.2f} 未优于 universe={au:+.2f}")
    if not (ad < au):
        # dropped 不差于 universe → 门在删好交易 (毒药)
        reasons.append(f"dropped.avg_ret={ad:+.2f} 未劣于 universe={au:+.2f} (删好交易预警)")
        if ad >= au:
            migrate = False
    return ("MIGRATE" if migrate else "VETO", reasons)


# ================================================================
# 报告
# ================================================================

def _fmt_row(tag, trs):
    n, w, a, pl = _stats(trs)
    if n == 0:
        return f"| {tag} | 0 | - | - | - |"
    return f"| {tag} | {n} | {w:.1f}% | {a:+.2f} | {pl:.2f} |"


def render_md(gate_spec, index_code, windows, fail_close, per_window):
    lines = [f"# 信号级复验: {gate_spec} (index={index_code}, "
             f"{'fail-close' if fail_close else 'fail-open'})\n",
             f"- 纪律: 归因宇宙(全样本)≠策略宇宙(信号池); 门须信号级迁移才可实装",
             f"- 裁定: kept.avg_ret>universe 且 dropped.avg_ret<universe 且 kept.n>={MIN_N}; "
             f"两段全正才稳定\n"]
    for wname, (universe, kept, dropped, vlabel, vreasons) in per_window:
        lines.append(f"## 窗口 {wname} 交易日\n")
        lines.append("| 池 | 笔数 | 胜率 | 均收 | PL |")
        lines.append("|---|---|---|---|---|")
        lines.append(_fmt_row("universe(基线)", universe))
        lines.append(_fmt_row("kept(过门)", kept))
        lines.append(_fmt_row("dropped(被删)", dropped))
        lines.append(f"\n**裁定: {vlabel}**")
        for r in vreasons:
            lines.append(f"- {r}")
        # 两段稳定性
        if universe:
            ds = sorted(universe, key=lambda t: str(t.get("signal_date") or ""))
            mid = ds[len(ds) // 2]
            seg_u1 = [t for t in universe if str(t.get("signal_date") or "") <= str(mid.get("signal_date") or "")]
            seg_u2 = [t for t in universe if str(t.get("signal_date") or "") > str(mid.get("signal_date") or "")]
            seg_k1 = [t for t in kept if str(t.get("signal_date") or "") <= str(mid.get("signal_date") or "")]
            seg_k2 = [t for t in kept if str(t.get("signal_date") or "") > str(mid.get("signal_date") or "")]
            _, _, ak1, _ = _stats(seg_k1)
            _, _, ak2, _ = _stats(seg_k2)
            _, _, au1, _ = _stats(seg_u1)
            _, _, au2, _ = _stats(seg_u2)
            stable = ak1 > au1 and ak2 > au2
            lines.append(f"- 两段稳定性: seg1 kept {ak1:+.2f} vs {au1:+.2f}, "
                         f"seg2 kept {ak2:+.2f} vs {au2:+.2f} → "
                         f"{'✅两段均改善' if stable else '⚠仅一段改善(过拟合风险)'}")
        lines.append("")
    return "\n".join(lines) + "\n"


# ================================================================
# 主流程
# ================================================================

def load_trades(args):
    if args.trades:
        return json.load(open(args.trades, encoding="utf-8"))
    if args.strategy:
        from app.market_cn.auto.backtest import run_all
        res = run_all(strategy=args.strategy, days=args.days or 600)
        trades = res["trades"]
        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                json.dump(trades, f, ensure_ascii=False)
            print(f"基准清单写出: {args.out} ({len(trades)} 笔)")
        return trades
    raise SystemExit("--trades 或 --strategy 至少给一个")


def main():
    ap = argparse.ArgumentParser(description="信号级复验闸门 (归因发现实装前的强制信号池验证)")
    ap.add_argument("--trades", default="", help="交易清单 JSON (backtest 输出格式)")
    ap.add_argument("--strategy", default="", help="重跑基准策略 key (重任务, 交用户终端)")
    ap.add_argument("--days", type=int, default=600)
    ap.add_argument("--gate", action="append", default=[], help="门 'name>=val', 可多次(AND)")
    ap.add_argument("--fail-close", action="store_true", help="特征不可知→否决(必要条件门)")
    ap.add_argument("--index", default="000300", help="环境指数码 (默认沪深300)")
    ap.add_argument("--windows", default="600,300", help="交易日的窗口列表, 逗号分隔")
    ap.add_argument("--out", default="", help="重跑基准时写出清单路径")
    ap.add_argument("--report", default="", help="报告输出路径 (默认 tmp/pool_check_<GATE>.md)")
    ap.add_argument("--demo", action="store_true", help="无 DB 自测 (合成清单验证切分+裁定)")
    a = ap.parse_args()

    if a.demo:
        return _demo()

    if not a.gate:
        raise SystemExit("--gate 至少给一个 (如 --gate 'ev20>=6')")
    gates = [parse_gate(g) for g in a.gate]
    gate_names = sorted({g[0] for g in gates})
    windows = [int(x) for x in a.windows.split(",") if x.strip()]

    trades = load_trades(a)
    trades = enrich(trades, gate_names, a.index)
    per_window = []
    for w in windows:
        universe = _window(trades, w)
        kept = [t for t in universe if _pass(t, gates, a.fail_close)]
        dropped = [t for t in universe if t not in kept]
        vlabel, vreasons = verdict(universe, kept, dropped)
        per_window.append((f"{w}", universe, kept, dropped, vlabel, vreasons))
        n, wn, av, pl = _stats(universe)
        nk, wk, ak, _ = _stats(kept)
        print(f"[{w}d] universe {n}笔 {wn:.1f}%/{av:+.2f} | kept {nk}笔 {wk:.1f}%/{ak:+.2f} "
              f"→ {vlabel}")

    out = a.report or os.path.join(
        PROJ, "tmp", f"pool_check_{'_'.join(a.gate).replace('>=','ge').replace('<=','le')}.md")
    txt = render_md(" AND ".join(a.gate), a.index, windows, a.fail_close, per_window)
    with open(out, "w", encoding="utf-8") as f:
        f.write(txt)
    print(f"-> {out}")
    return 0


def _demo():
    """合成清单自测: 验证切分+裁定逻辑 (无 DB)。"""
    print("=== pool_check --demo (合成清单, 无 DB) ===")
    # 构造: universe 均收 +2, 其中 env_ret20<=-5 的子集均收 +6 (优), 其余 +0 (劣)
    trades = []
    import random
    random.seed(1)
    for i in range(60):
        feat = -6.0 if i % 3 == 0 else 2.0      # 1/3 深弱
        ret = random.gauss(6.0 if feat <= -5 else 0.0, 3.0)
        trades.append({"code": f"00000{i:02d}", "signal_date": f"2026-{1+i//30:02d}-1{i%9+10}",
                       "return_pct": round(ret, 2),
                       "_feat": {"env_ret20": feat}})
    gates = [parse_gate("env_ret20<=-5")]
    universe = trades
    kept = [t for t in universe if _pass(t, gates, False)]
    dropped = [t for t in universe if t not in kept]
    vlabel, vreasons = verdict(universe, kept, dropped)
    print(_fmt_row("universe", universe))
    print(_fmt_row("kept(env_ret20<=-5)", kept))
    print(_fmt_row("dropped", dropped))
    print(f"裁定: {vlabel} | {vreasons}")
    assert vlabel == "MIGRATE", "demo 应判定 MIGRATE"
    # 反例: 门在删好交易 (dropped 优于 universe)
    trades2 = []
    for i in range(60):
        feat = -6.0 if i % 3 == 0 else 2.0
        ret = random.gauss(0.0 if feat <= -5 else 6.0, 3.0)   # 深弱池反而差
        trades2.append({"code": f"00000{i:02d}", "signal_date": f"2026-{1+i//30:02d}-1{i%9+10}",
                        "return_pct": round(ret, 2), "_feat": {"env_ret20": feat}})
    kept2 = [t for t in trades2 if _pass(t, gates, False)]
    dropped2 = [t for t in trades2 if t not in kept2]
    v2, r2 = verdict(trades2, kept2, dropped2)
    print(f"\n反例: 深弱池实差 → 裁定 {v2} | {r2}")
    assert v2 == "VETO", "反例应判定 VETO"
    print("\n✅ 自测通过: MIGRATE/VETO 裁定逻辑正确")
    return 0


if __name__ == "__main__":
    sys.exit(main())

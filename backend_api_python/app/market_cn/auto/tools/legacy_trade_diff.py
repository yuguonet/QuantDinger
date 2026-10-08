"""replay trades vs 旧回测 trades **逐笔对拍** —— 改进方案 §5.1 删除开关的执行体。

为什么要有这个工具
------------------
改进方案 §5.1 第 1 条（删除旧代码的**唯一开关**）原文：

    **golden 逐笔等价**：replay trades == 旧回测逐条（1e-12；门判定逐位）

但在此工具之前，该能力**并不存在**：`tools/golden_check.py` 的 `run_truth` 做的是
「回放 vs 生产库 `qd_dragon_signals` 的 **ready 日**」对账（见其 docstring），
与 §5.1 要求的「**trades** vs **旧回测**」不是同一件事 ⇒ 删除开关一直没有执行体，
P6 清理收量也就拿不到通行证。

本工具补上这一路：同一批 bars，分别跑
  · 旧回测 = 策略覆写的 `backtest_stock`（第二份回测，待删对象）
  · 新路径 = `core.replay` + `TradesCollector`（折叠事件链）
再逐笔比对核心字段，容差 1e-12。

用法::

    python -m app.market_cn.auto.tools.legacy_trade_diff --strategy break --n 20 --days 300

    # 外部参照模式（§2.2.3）：旧侧来自**独立旧版树**（P6 删除树内旧代码后照跑）
    python -m app.market_cn.auto.tools.legacy_trade_diff --strategy break \
        --ref D:\\ref\\QuantDinger-auto展示层正常
    # 等价：export AUTO_SLIM_REF=<旧版树根>  后省略 --ref

退出码: 0 = 逐笔一致（可删）；1 = 有差异或样本为空。

★ 删除开关的升级（§2.2.3 落地约定，2026-10-08）：
    原：冻结 JSON 全绿（tests/golden/test_golden_parity）。
    升级为：**冻结 JSON 全绿 + 外部旧版参照对拍全绿**（本工具 --ref 模式 /
    tests/golden/test_ref_parity）。旧整树归档在 GitHub main 的
    `QuantDinger-auto展示层正常.zip`，删除旧代码因此**可逆**。

⚠ 口径：差异一旦出现，按 §2.2.1 裁定「缺陷不入基线」⇒ **以 replay 为准**，
  不得为了对拍通过而把旧回测的缺陷（如 T+1 违规）焊进新引擎。
⚠ §2.2.2 边界：外部参照用于验证**架构等价**；R1/R2 是有意的规则变更
  （末日平仓收尾 / 出场起点 D2），参照树是变更前版本 ⇒ 该两类差异属**已登记**，
  以重冻基线为准，不以旧版为准（详见 docs/口径差异报告.md）。

外部参照模式的隔离设计（为什么走子进程）
----------------------------------------
旧版树的 `from app.market_cn.auto...` 与当前树**同包名**，进程内 import 必串包。
⇒ `--ref` 把旧侧放进**独立子进程**：PYTHONPATH 指向旧版树的 backend_api_python，
旧代码整体在自己的包空间里跑，零污染、零依赖当前树（P6 删完也照跑）。参照树**只读**。
"""
from __future__ import annotations

import sys

# ⚠ Windows 下后台运行 / 重定向到文件时 stdout 可能是 GBK（PYTHONIOENCODING 未设）
#   ⇒ 下面 print 里的 ⇒ / ★ 等字符会 UnicodeEncodeError，**整把工具直接崩掉**
#   (2026-10-07 实测: 后台跑 `--strategy g56` 在第一行提示就挂了, 日志全空)。
#   显式重配 stdout, 不依赖调用方环境。errors="replace" ⇒ 极端编码下退化而非崩。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import argparse
import json
import os
import subprocess

#: §5.1 规定的容差
EPS = 1e-12

#: 字段映射：(旧回测键, canonical/replay 键)。只比**两侧都客观存在**的字段 ——
#: 旧回测没有 exit_date（只有相对天数 exit_day），故不进比对面，避免制造假差异。
_FIELDS = (
    ("signal_date", "d0_date"),
    ("entry_date", "entry_date"),
    ("entry_price", "entry_price"),
    ("exit_price", "exit_price"),
    ("return_pct", "return_pct"),
)

#: 子进程里跑旧引擎的脚本（独立解释器 + PYTHONPATH=旧版树 ⇒ 包空间隔离）。
_REF_RUNNER = r"""
import json, sys
data = json.load(sys.stdin)
from app.market_cn.auto import strategies as reg
reg.autodiscover()
s = reg.get_strategy(data["strategy"])
if s is None:
    raise SystemExit("strategy not registered in ref tree: " + data["strategy"])
kw = data.get("kwargs") or {}
si = data.get("stock_info")
try:
    if si is not None:
        out = s.backtest_stock(data["bars"], data["code"], stock_info=si, **kw)
    else:
        out = s.backtest_stock(data["bars"], data["code"], **kw)
except TypeError:
    out = s.backtest_stock(data["bars"], data["code"])   # 旧签名不收 kwargs
if isinstance(out, dict):
    out = out.get("trades") or []
json.dump(list(out or []), sys.stdout)
"""


def resolve_ref_backend(ref_root):
    """旧版树根 → backend_api_python 路径（接受仓库根或 backend 目录两种）。"""
    ref_root = os.path.abspath(ref_root)
    cand = os.path.join(ref_root, "backend_api_python")
    return cand if os.path.isdir(os.path.join(cand, "app")) else ref_root


def ref_trades(ref_root, strategy, code, bars, *, stock_info=None, kwargs=None,
               timeout=300):
    """外部参照树的 `backtest_stock` 输出（独立子进程跑，参照树只读）。

    返回 trades list（dict 未规范化，与 legacy_trades 同形状）；失败抛 RuntimeError
    （含子进程 stderr，**不静默**）。
    """
    backend = resolve_ref_backend(ref_root)
    if not os.path.isdir(os.path.join(backend, "app")):
        raise RuntimeError(f"参照树不是有效工程: {ref_root}（resolve→{backend}）")
    payload = {"strategy": strategy, "code": code, "bars": bars,
               "stock_info": stock_info, "kwargs": kwargs or {}}
    env = dict(os.environ)
    env["PYTHONPATH"] = backend + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        p = subprocess.run(
            [sys.executable, "-c", _REF_RUNNER],
            input=json.dumps(payload), capture_output=True, text=True,
            encoding="utf-8", errors="replace", cwd=backend, env=env,
            timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"参照树子进程超时 ({timeout}s): {strategy}/{code}")
    if p.returncode != 0:
        raise RuntimeError(f"参照树子进程失败 ({strategy}/{code}): "
                           f"{p.stderr.strip()[-500:]}")
    return json.loads(p.stdout or "[]")


def _norm_date(v):
    return str(v)[:10] if v else None


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def legacy_trades(strat, code, bars):
    """旧回测（策略覆写的 `backtest_stock`）产出的 trades 列表。"""
    out = strat.backtest_stock(bars, code)
    if isinstance(out, dict):          # 少数策略返回 {"trades": [...]}
        out = out.get("trades") or []
    return list(out or [])


def replay_trades(strat, code, bars, mk, skey):
    """新路径（`core.replay` + TradesCollector）产出的 trades 列表。"""
    from app.market_cn.auto.core.replay import DailyFeed, TradesCollector, replay
    from app.market_cn.auto.core.replay.intraday import make_feed

    intraday = getattr(getattr(strat, "scan_spec", None), "kind", "") == "intraday_window"
    feed = (make_feed(strat, code, bars, mkt_map=mk) if intraday
            else DailyFeed(bars, mkt_map=mk))
    col = TradesCollector(code, skey)
    replay(strat, code, feed, collectors=[col])
    return list(col.trades)


def _key(t, d0_field):
    """配对键 = (信号日, 入场日) —— 两个都不依赖出场，出场不同才不会配不上。"""
    return (_norm_date(t.get(d0_field)), _norm_date(t.get("entry_date")))


def diff_trades(legacy, rep):
    """→ (配对数, 字段差异 list[(key, 字段, 旧值, 新值)], 只旧有, 只回放有)。"""
    li = {_key(t, "signal_date"): t for t in legacy}
    ri = {_key(t, "d0_date"): t for t in rep}
    only_legacy, only_replay = sorted(set(li) - set(ri)), sorted(set(ri) - set(li))
    diffs = []
    for k in sorted(set(li) & set(ri)):
        a, b = li[k], ri[k]
        for lf, rf in _FIELDS:
            av, bv = a.get(lf), b.get(rf)
            if lf.endswith("_date"):
                av, bv = _norm_date(av), _norm_date(bv)
                if av != bv:
                    diffs.append((k, lf, av, bv))
                continue
            av, bv = _num(av), _num(bv)
            if av is None and bv is None:
                continue
            if av is None or bv is None or abs(av - bv) > EPS:
                # 诊断用（不参与判定）：出场日与出场规则，用来判断差异出自哪条出场腿
                diag = (f"day {a.get('exit_day')}→{b.get('exit_day')} "
                        f"rule {a.get('exit_reason') or a.get('exit_rule')}"
                        f"→{b.get('exit_reason')}")
                diffs.append((k, f"{lf} [{diag}]", av, bv))
    return len(set(li) & set(ri)), diffs, only_legacy, only_replay


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="replay vs 旧回测 逐笔对拍 (§5.1 删除开关)")
    ap.add_argument("--strategy", default="break")
    ap.add_argument("--n", type=int, default=20, help="样本票数")
    ap.add_argument("--days", type=int, default=300)
    ap.add_argument("--top", type=int, default=12, help="差异明细打印条数")
    ap.add_argument("--ref", default=os.environ.get("AUTO_SLIM_REF"),
                    help="外部旧版树根（§2.2.3）；默认取 $AUTO_SLIM_REF。"
                         "给定后旧侧 = 参照树的 backtest_stock（独立子进程），"
                         "P6 删除树内旧代码后照跑")
    a = ap.parse_args(argv)

    from app.market_cn.auto import strategies as reg
    from app.market_cn.auto.core.data.hub import all_codes, daily
    from app.market_cn.auto.core.replay import load_market_gain

    reg.autodiscover()
    strat = reg.get_strategy(a.strategy)
    if strat is None:
        print(f"[{a.strategy}] 未注册")
        return 1

    mk = load_market_gain("2000-01-01", "2099-12-31")

    # 横截面策略（覆写 `begin_day`，如 g56）：单票 `replay` **不调 begin_day** ⇒ 当日
    # 池为空 ⇒ `_g56_gate` 宁缺勿滥直接 return False ⇒ 恒零信号（假象：看着像"折叠
    # 契约没接上"，其实契约完好，缺的是池）。判据与 golden_check 一致，但这里是
    # **一次 batch 跑完整批票**，而不是逐票各建一次陪跑池（O(n²) 取数）。
    from app.market_cn.auto.strategies.base import StrategyBase
    pooled = getattr(type(strat), "begin_day", None) is not getattr(
        StrategyBase, "begin_day", None)
    if pooled:
        print(f"[{a.strategy}] 横截面策略 (覆写 begin_day) ⇒ 走 replay_batch")
        if a.ref:
            print(f"[{a.strategy}] ⚠ 外部参照模式下旧侧为**单票**旧引擎（无池接线），"
                  f"横截面策略的参照对拍仅供定性，等价结论以冻结基线为准")

    bars_map = {}
    for c in (all_codes() or [])[:a.n]:
        try:
            b = daily(c, a.days)
        except Exception:
            continue
        if b and len(b) >= 60:
            bars_map[c] = b

    batch_trades = {}
    if pooled:
        from app.market_cn.auto.core.replay import TradesCollector, replay_batch
        cols = {c: [TradesCollector(c, a.strategy)] for c in bars_map}
        replay_batch(strat, bars_map, collectors=cols)
        batch_trades = {c: list(cols[c][0].trades) for c in bars_map}

    if a.ref:
        print(f"[{a.strategy}] 外部参照模式: {resolve_ref_backend(a.ref)}"
              f"（§2.2.3；参照树只读；R1/R2 已登记差异见 docs/口径差异报告.md）")

    n_leg = n_rep = n_only_l = n_only_r = n_bad = 0
    rows = []
    for code, bars in bars_map.items():
        try:
            lg = (ref_trades(a.ref, a.strategy, code, bars) if a.ref
                  else legacy_trades(strat, code, bars))
        except Exception as e:                      # 旧引擎炸了也要记账，不能静默
            n_bad += 1
            rows.append((code, "<legacy>", f"{type(e).__name__}: {e}", ""))
            continue
        try:
            rp = (batch_trades.get(code) if pooled
                  else replay_trades(strat, code, bars, mk, a.strategy))
        except Exception as e:
            n_bad += 1
            rows.append((code, "<replay>", f"{type(e).__name__}: {e}", ""))
            continue
        n_leg += len(lg)
        n_rep += len(rp)
        _n, d, ol, orr = diff_trades(lg, rp)
        n_only_l += len(ol)
        n_only_r += len(orr)
        for k, f, av, bv in d:
            rows.append((code, f"{k[0]}|{k[1]} {f}", av, bv))

    print(f"[{a.strategy}] 旧回测 {n_leg} 笔 / replay {n_rep} 笔"
          f" / 只旧有 {n_only_l} / 只回放有 {n_only_r} / 异常 {n_bad}")
    print(f"[{a.strategy}] 字段差异 {len(rows)} 处 (容差 {EPS:g})")
    for r in rows[:a.top]:
        print("    ", r)

    if not n_leg and not n_rep:
        # 两侧皆空 = 无证据。与 golden_check 同口径：空集对空集不算通过。
        print(f"[{a.strategy}] NO_SAMPLE 两侧皆空 —— 不算通过（无证据）")
        return 1
    if n_leg and not n_rep:
        print(f"[{a.strategy}] ⚠ replay 零产出而旧回测有 {n_leg} 笔 ⇒ 折叠契约未接上，不是等价")
        return 1
    return 1 if (rows or n_only_l or n_only_r) else 0


if __name__ == "__main__":
    raise SystemExit(main())

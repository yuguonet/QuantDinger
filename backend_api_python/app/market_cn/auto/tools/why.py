#!/usr/bin/env python3
"""auto/tools/why.py — 一键策略调试 (2026-09-26 T17)

回答三个最常问的问题, 一条命令搞定:
  1. 这票这几天为什么(没)出信号?     --days N  多日扫描 + 命中摘要
  2. 这一天到底卡在哪道门?           --date D  单日全量 (转交 tools/debug)
  3. 改个参数会怎样?                 --params '{"stop":-5}'  覆盖试调 (不写 config)

外加库对照: --db 列出 qd_dragon_signals 里该 (strategy, code) 的近几行,
           对照"应然判定"与"库里落账"。

用法:
  python -m app.market_cn.auto.tools.why --strategy t_hilo --code 600519
  python -m app.market_cn.auto.tools.why --strategy v1 --code 000001 --days 15
  python -m app.market_cn.auto.tools.why --strategy break --code 000032 --date 2026-09-18
  python -m app.market_cn.auto.tools.why --strategy knife_catch --code 300059 --date 2026-09-18
  python -m app.market_cn.auto.tools.why --strategy t_hilo --code 600519 --params '{"entry_gain_min":1}' --days 10

与 debug 的关系: debug=显微镜 (逐门值/TRACE); why=听诊器 (先扫一遍再决定看哪天)。
判定逻辑零复制 —— 多日走 scan_signals/scan_days, 单日转交 debug.report_*。
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def _load_env():
    try:
        from dotenv import load_dotenv
        for _p in (os.path.join(os.getcwd(), ".env"),
                   os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
                       os.path.dirname(os.path.abspath(__file__))))), ".env")):
            if os.path.isfile(_p):
                load_dotenv(_p, override=False)
                break
    except Exception:
        pass


_ALIAS = {"dragon": "dragon_callback", "dragon2": "dragon_v2", "break_buy": "break"}


def _parse_params(s):
    if not s:
        return {}
    try:
        d = json.loads(s)
        return d if isinstance(d, dict) else {}
    except Exception as e:
        raise SystemExit(f"--params 不是合法 JSON dict: {e}")


def _apply_params(strat, params):
    """临时参数覆盖: 挂到实例 merged_params 链 (不动 config, 不写文件)。"""
    if not params:
        return
    # merged_params(override) 合并 config > 代码; 试调优先于二者
    orig = strat.merged_params

    def _mp(override=None):
        base = orig(override)
        base.update(params)
        return base

    strat.merged_params = _mp


def _recent_db_rows(key, code, limit=8):
    try:
        from app.market_cn.auto.store import list_signals
        rows = list_signals(days=60)
        hit = [r for r in rows
               if r.get("strategy") == key and r.get("code") == code]
        return hit[-limit:]
    except Exception as e:
        return [{"error": str(e)}]


def scan_days_brief(strat, key, bars, code, days):
    """多日粗扫: 优先 scan_days (一次预计算); 失败则逐日截断 scan_signals。"""
    n = len(bars)
    if n < 30:
        return {"hits": [], "scanned": 0, "note": "bars 不足 30 根"}
    hits = []
    last_reject = None
    scanned = 0
    try:
        sigs_all = strat.scan_days(bars, code) or []
        for s in sigs_all:
            hits.append({
                "date": str(s.time)[:10],
                "score": getattr(s, "score", None),
                "label": getattr(s, "label", ""),
                "price": getattr(s, "price", None),
            })
        scanned = n
    except Exception:
        lo = max(25, n - max(1, min(days, n - 1)))
        for i in range(lo, n):
            scanned += 1
            try:
                sigs = strat.scan_signals(bars[:i + 1], code) or []
            except Exception as e:
                last_reject = {"date": str(bars[i]["time"])[:10], "err": str(e)}
                continue
            if sigs:
                s = sigs[0]
                hits.append({
                    "date": str(bars[i]["time"])[:10],
                    "score": getattr(s, "score", None),
                    "label": getattr(s, "label", ""),
                    "price": getattr(s, "price", None),
                })
                last_reject = None
            else:
                last_reject = {"date": str(bars[i]["time"])[:10],
                               "reason": "scan_signals 空"}
    return {"hits": hits, "scanned": scanned, "last_miss": last_reject}


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="qd why",
        description="一键策略调试: 多日扫描 / 单日深潜 / 参数试调 / 库对照")
    ap.add_argument("--strategy", required=True, help="策略 key (支持 dragon/break_buy 别名)")
    ap.add_argument("--code", required=True, help="股票代码")
    ap.add_argument("--date", default="", help="单日深潜 (转 debug); 不给则多日粗扫")
    ap.add_argument("--days", type=int, default=15, help="多日粗扫窗口 (自然日, 默认 15)")
    ap.add_argument("--bars-days", type=int, default=300, help="取数窗口 (默认 300)")
    ap.add_argument("--params", default="", help='临时参数 JSON, 如 \'{"stop":-5}\' (不写 config)')
    ap.add_argument("--db", action="store_true", help="对照 qd_dragon_signals 近几行")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--max-lu", type=int, default=3, help="单日 limit_up 详展数")
    args = ap.parse_args(argv)

    _load_env()
    key = _ALIAS.get(args.strategy, args.strategy)
    p_over = _parse_params(args.params)

    from app.market_cn.auto import strategies as reg
    reg.autodiscover()
    strat = reg.get_strategy(key)
    if strat is None:
        from app.market_cn.auto.registry import strategy_keys
        print(f"策略 {key} 未注册。可用: {', '.join(sorted(strategy_keys()))}")
        return 2

    _apply_params(strat, p_over)
    name = getattr(strat, "name", key)
    kind = getattr(getattr(strat, "scan_spec", None), "kind", "")
    print(f"=== why | {key} ({name}) | {args.code} | kind={kind} ===")
    if p_over:
        print(f"参数试调: {json.dumps(p_over, ensure_ascii=False)}  (仅本次进程)")
    print()

    # ---- 库对照 ----
    if args.db:
        print("--- 库内 qd_dragon_signals (近几行) ---")
        for r in _recent_db_rows(key, args.code):
            if "error" in r:
                print(f"  [db 不可用] {r['error']}")
                break
            print(f"  {r.get('trade_date')} state={r.get('state')} "
                  f"score={r.get('score')} entry={r.get('entry_date')} "
                  f"exit={r.get('exit_reason') or '-'}")
        print()

    # ---- 单日深潜: 转交 debug ----
    if args.date:
        from app.market_cn.auto.tools import debug as dbg
        argv2 = ["--strategy", key, "--code", args.code,
                 "--date", args.date, "--days", str(args.bars_days),
                 "--max-lu", str(args.max_lu)]
        if args.json:
            argv2.append("--json")
        if p_over:
            # debug 未接 --params; 已挂在实例上, 其内部 merged_params 会吃到
            pass
        return _run_debug(dbg, argv2)

    # ---- 多日粗扫 ----
    from app.market_cn.auto.tools.debug import _bars_and_info, _board_name
    bars, info = _bars_and_info(args.code, args.bars_days)
    if not bars:
        print(f"{args.code}: 无日线数据")
        return 1
    print(f"bars {len(bars)} 根 | {str(bars[0]['time'])[:10]} ~ {str(bars[-1]['time'])[:10]} "
          f"| 板块 {_board_name(args.code)}")

    if kind == "intraday_window":
        print("盘中策略 → 多日粗扫用 debug --date 单日逐触发; 此处只报参数与库。")
        result = {"strategy": key, "code": args.code, "kind": kind,
                  "params": {k: strat.merged_params(None).get(k)
                             for k in ("t_gain_pct", "stop", "hold") if hasattr(strat, 'merged_params')}}
    else:
        result = scan_days_brief(strat, key, bars, args.code, args.days)
        hits = result.get("hits") or []
        print(f"--- 扫描 {result.get('scanned')} 日, 命中 {len(hits)} ---")
        for h in hits[-10:]:
            print(f"  ✓ {h['date']}  score={h.get('score')}  {h.get('label')}")
        if not hits:
            lm = result.get("last_miss") or {}
            print(f"  ✗ 无信号。最后一日 {lm.get('date')}: {lm.get('reason') or lm.get('err') or '未命中'}")
            print("  → 看这一天卡在哪: "
                  f"python -m app.market_cn.auto.tools.why --strategy {key} "
                  f"--code {args.code} --date {lm.get('date') or str(bars[-1]['time'])[:10]}")

    # ---- 底线 ----
    print()
    print("--- bottom line ---")
    if kind == "intraday_window":
        print(f"盘中策略; 用 --date 看单日触发。params 摘要见上。")
    else:
        hits = (result.get("hits") if isinstance(result, dict) else None) or []
        if hits:
            print(f"最近命中 {len(hits)} 次, 最新 {hits[-1]['date']}。"
                  f"深挖该日加 --date {hits[-1]['date']}")
        else:
            print("窗口内零命中。建议: ① --date 看末日门漏斗 ② --params 放宽阈值试调 ③ --db 看历史是否曾命中")
    if args.json:
        print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    return 0


def _run_debug(dbg_mod, argv2):
    """把参数塞回 sys.argv 调 debug.main (argparse 唯一出处)。"""
    old = sys.argv
    try:
        sys.argv = ["debug"] + argv2
        return int(dbg_mod.main() or 0)
    finally:
        sys.argv = old


if __name__ == "__main__":
    sys.exit(main())

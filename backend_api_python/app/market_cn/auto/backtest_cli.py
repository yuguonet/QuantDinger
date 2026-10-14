#!/usr/bin/env python3
"""Standalone CLI shell for the official automatic-strategy backtest pipeline.

Run from backend_api_python with:
    python -m app.market_cn.auto.backtest_cli --strategy break --days 120

For an exact date range, use --start-date and --end-date instead of --days.
Daily strategies use the official run_all pipeline and filter its trades by entry
date; intraday strategies pass the range to run_all directly. Daily date slices
may need a wider source window for strategy warm-up and post-entry settlement.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any


_BACKEND_ROOT = Path(__file__).resolve().parents[3]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

_ALIASES = {"dragon": "dragon_callback"}


def _date_arg(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"日期必须是 YYYY-MM-DD 格式: {value!r}") from exc


def _codes_arg(value: str | None) -> list[str] | None:
    if not value:
        return None
    import re
    codes = [item for item in re.split(r"[,\s]+", value.strip()) if item]
    invalid = [code for code in codes if not code.isdigit()]
    if invalid:
        raise ValueError(f"股票代码应为数字: {', '.join(invalid)}")
    return codes or None


def _entry_date(trade: dict[str, Any]) -> str:
    return str(trade.get("entry_date") or "")[:10]


def _activity(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group the selected entry cohort by actual buy and sell dates."""
    by_date: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for trade in trades:
        entry_date = _entry_date(trade)
        exit_date = str(trade.get("exit_date") or "")[:10]
        if entry_date:
            by_date.setdefault(entry_date, {"buys": [], "sells": []})["buys"].append(trade)
        if exit_date:
            by_date.setdefault(exit_date, {"buys": [], "sells": []})["sells"].append(trade)

    result = []
    for day in sorted(by_date):
        groups = by_date[day]
        result.append({
            "date": day,
            "buys": [_trade_activity_item(t, "buy") for t in groups["buys"]],
            "sells": [_trade_activity_item(t, "sell") for t in groups["sells"]],
        })
    return result


def _trade_activity_item(trade: dict[str, Any], side: str) -> dict[str, Any]:
    return {
        "code": str(trade.get("code") or ""),
        "name": trade.get("name"),
        "price": trade.get("entry_price") if side == "buy" else trade.get("exit_price"),
        "return_pct": trade.get("return_pct"),
        "reason": trade.get("exit_reason"),
        "entry_date": _entry_date(trade),
        "exit_date": str(trade.get("exit_date") or "")[:10],
    }


def _build_report(result: dict[str, Any], strategy: str, start: str, end: str) -> dict[str, Any]:
    from app.market_cn.auto.core.backtest import _summary

    trades = result.get("trades") or []
    # Performance is measured on the cohort bought inside the selected range.
    cohort = [t for t in trades if start <= _entry_date(t) <= end]
    return {
        "strategy": strategy,
        "start_date": start,
        "end_date": end,
        "source_window_days": result.get("meta", {}).get("days"),
        "codes_ok": result.get("codes_ok"),
        "elapsed_seconds": result.get("elapsed"),
        "stats": _summary(cohort),
        "daily_activity": _activity(cohort),
        "trades": cohort,
    }


def _run(args: argparse.Namespace) -> dict[str, Any]:
    from app.market_cn.auto.core._paths import load_env_first_found
    from app.market_cn.auto.core.backtest import run_all
    from app.market_cn.auto.store import today_str
    from app.market_cn.auto import strategies as strategy_registry

    load_env_first_found(os.path.join(os.getcwd(), ".env"))
    strategy_registry.autodiscover()
    strategy = _ALIASES.get(args.strategy, args.strategy)
    if strategy_registry.get_strategy(strategy) is None:
        available = sorted(strategy_registry.all_strategies())
        raise ValueError(f"未注册策略 {strategy!r}; 可用策略: {', '.join(available)}")

    codes = _codes_arg(args.codes)
    if args.start_date:
        start = args.start_date
        end = args.end_date or _date_arg(today_str())
        if start > end:
            raise ValueError("--start-date 不能晚于 --end-date")
        today = _date_arg(today_str())
        if end > today:
            raise ValueError(f"--end-date 不能晚于当前北京时间日期 {today}")
        strat = strategy_registry.get_strategy(strategy)
        if strat.scan_spec.kind == "intraday_window":
            days = (end - start).days + 1
            engine_start = start.isoformat()
            engine_end = end.isoformat()
        else:
            # run_all's daily path anchors its source window at today and has no
            # date-bound input yet. Fetch far enough back for the requested
            # interval plus strategy seed history, then select the entry cohort.
            calendar_lookback = max(0, (today - start).days)
            days = max(1, math.ceil(calendar_lookback / 1.5) + 200)
            engine_start = None
            engine_end = end.isoformat()
    else:
        start = end = None
        days = args.days
        engine_start = engine_end = None

    result = run_all(
        strategy=strategy,
        days=days,
        codes=codes,
        start_date=engine_start,
        end_date=engine_end,
        exec_engine=args.exec_engine,
        progress_every=args.progress_every,
    )
    if start is None:
        end_day = _date_arg(today_str())
        # --days is a calendar-day lookback; use the same interval for reporting.
        start_day = end_day.fromordinal(end_day.toordinal() - days + 1)
        start, end = start_day.isoformat(), end_day.isoformat()
    else:
        start, end = start.isoformat(), end.isoformat()
    return _build_report(result, strategy, start, end)


def _fmt(value: Any, suffix: str = "") -> str:
    return "n/a" if value is None else f"{value}{suffix}"


def _print_report(report: dict[str, Any], trades_limit: int) -> None:
    stats = report["stats"]
    print(f"策略: {report['strategy']} | 区间: {report['start_date']} ~ {report['end_date']}")
    print(f"有效代码: {_fmt(report['codes_ok'])} | 选中样本: {stats.get('n', 0)} "
          f"(有效收益 {stats.get('n_valid', 0)}) | 耗时: {_fmt(report['elapsed_seconds'], 's')}")
    print(f"胜率: {_fmt(stats.get('winrate'), '%')} | 盈亏比: {_fmt(stats.get('pl_ratio'))} "
          f"| 平均收益: {_fmt(stats.get('avg_ret'), '%')}")
    if stats.get("n_invalid"):
        print(f"未结算/缺少收益: {stats['n_invalid']} 笔（不计入胜率和盈亏比）")

    print("\n按日买卖:")
    activity = report["daily_activity"]
    if not activity:
        print("  区间内没有已入场交易。")
    for item in activity:
        buys = ", ".join(f"{t['code']}@{_fmt(t['price'])}" for t in item["buys"]) or "-"
        sells = ", ".join(
            f"{t['code']}@{_fmt(t['price'])}({_fmt(t['return_pct'], '%')})"
            for t in item["sells"]) or "-"
        print(f"  {item['date']} | 买入 {len(item['buys'])}: {buys} "
              f"| 卖出 {len(item['sells'])}: {sells}")

    if trades_limit:
        trades = report["trades"]
        print(f"\n逐笔交易（{min(len(trades), trades_limit)}/{len(trades)}）:")
        for trade in trades[:trades_limit]:
            print("  {code} 买 {entry}@{buy} → 卖 {exit}@{sell} | {ret}% | {reason}".format(
                code=trade.get("code", ""),
                entry=_entry_date(trade) or "-",
                buy=_fmt(trade.get("entry_price")),
                exit=str(trade.get("exit_date") or "-")[:10],
                sell=_fmt(trade.get("exit_price")),
                ret=_fmt(trade.get("return_pct")),
                reason=trade.get("exit_reason") or trade.get("reason") or "-",
            ))
        if len(trades) > trades_limit:
            print(f"  ... 其余 {len(trades) - trades_limit} 笔见 JSON 输出")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="自动策略系统官方回测 CLI（core.backtest.run_all 薄壳）")
    parser.add_argument("--strategy", required=True, help="策略 key（例如 break、g56、v1）")
    window = parser.add_mutually_exclusive_group()
    window.add_argument("--days", type=int, default=120, help="回看自然日数（默认 120）")
    window.add_argument("--start-date", type=_date_arg, help="日期切片起点 YYYY-MM-DD")
    parser.add_argument("--end-date", type=_date_arg, help="日期切片终点 YYYY-MM-DD；与 --start-date 配合")
    parser.add_argument("--codes", default="", help='可选代码池，逗号/空格分隔；PowerShell 请加引号')
    parser.add_argument("--exec-engine", choices=("daily", "intraday", "auto"), default="daily",
                        help="成交精修口径（默认 daily）")
    parser.add_argument("--progress-every", type=int, default=500,
                        help="每处理多少只代码输出进度；0 关闭（默认 500）")
    parser.add_argument("--trades-limit", type=int, default=100,
                        help="终端最多打印的逐笔数；0 关闭（默认 100）")
    parser.add_argument("--json", action="store_true", help="将完整报告 JSON 输出到 stdout")
    parser.add_argument("--out", help="另存完整报告 JSON 文件")
    args = parser.parse_args(argv)

    if args.days is not None and args.days < 1:
        parser.error("--days 必须是正整数")
    if args.progress_every < 0 or args.trades_limit < 0:
        parser.error("--progress-every 和 --trades-limit 不能为负数")
    if args.start_date is None and args.end_date is not None:
        parser.error("--end-date 必须与 --start-date 一起使用")

    try:
        report = _run(args)
    except (ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str),
                            encoding="utf-8")
        if not args.json:
            print(f"完整 JSON 报告已写入: {out_path}")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        _print_report(report, args.trades_limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

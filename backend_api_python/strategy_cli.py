#!/usr/bin/env python3
"""strategy_cli.py — 自动策略 IDE 命令行壳 (上层接线层)

用途: 调试策略的**单一入口** —— "写策略、调试、看结果"三件事都从这条命令进
      (架构 §7)。它只是**壳**: 做参数解析 + 分发 + 结果排版, 引擎逻辑一律留在
      auto/ 内。core/(内核) 与 scan.py(实盘路径) **一行未改**。

设计点:
  - 分层: 本文件在 backend_api_python/ 顶层 (与 run.py 同级) = 接线层;
    向下只调**已注册的公开入口** —— core.backtest.run_all 与 tools/*.main(),
    不复制任何策略/判定逻辑, 因此不产生第二事实源。
  - 注册表分发: 冻结 .py 策略 (dragon_callback/v1/break/...) 与 YAML 门表策略
    都经 strategies 注册表拿; 本壳不需要知道任何策略细节 → 加策略不改本文件。
  - 转发用 sys.argv 补丁调既有 tools 的 main(): 各工具的 argparse 保持唯一出处。
  - 板块中文名走 core.market.get_board_name (spec 驱动), 不硬编码"沪主板/深主板"。

易错点:
  - `--days` 语义由引擎决定, 本壳原样透传: 日线路径 = 自然日窗口
    (fetch_kline_db(code, days)); 盘中路径(intraday_window) = 日历回看天数,
    进度按**交易日**推进 (不是按股票), 别被 "[k/N]" 的数字误读成股票计数。
  - `--today/--today-date` 口径 = 按**买入日 entry_date** 过滤 (与 test_dragon.py
    的 --today 同口径), 不是信号日。
  - 策略 key 别名只有本壳认 (dragon→dragon_callback 等); 直接跑 core 的 __main__
    不认别名。
  - PowerShell 传参: 未加引号的 `--codes a,b,c` 会被 PS 解析成**数组**; 全是数字的
    元素还会被当数值, **前导零直接丢失**(`000533`→`533`) → 代码全废, **静默 0 结果**。
    故 PowerShell 下请加引号: `--codes "000533,000859"`。切分兼容逗号与空格两种
    (见 _split_codes), 加引号后两种写法都对。
  - 冻结 .py(registry 路径) 与 YAML 门表(`load_strategy` 路径) 的 trade dict 键不同:
    前者如 v1 用 `d0_date` 且**无** `signal_date`/`exit_reason`; 打印按"有则显示"处理。
"""
from __future__ import annotations

import os
import sys

# --- Windows 控制台 UTF-8: 默认 GBK 会在中文输出时 UnicodeEncodeError (与 run.py 同法) ---
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# --- 让 `python backend_api_python/strategy_cli.py` 与 `cd backend_api_python && python strategy_cli.py` 都能 import app ---
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# ---- 子命令 → 既有工具模块 (转发; 各自 argparse 保持唯一出处) ----
_FORWARD = {
    "replay":   "app.market_cn.auto.tools.replay",      # 单股逐笔重放 (判定特征+出场原因)
    "debug":    "app.market_cn.auto.tools.debug",       # 单候选逐门追踪 (门表值/插件TRACE/盘中)
    "gates":    "app.market_cn.auto.tools.rule_audit",  # 门级审计 (拦截表+判别力)
    "audit":    "app.market_cn.auto.tools.rule_audit",
    "stats":    "app.market_cn.auto.tools.rule_stats",  # 入场规则归因 (固定持有口径)
    "grid":     "app.market_cn.auto.tools.param_scan",  # 参数网格 + 阈值敏感性
    "explain":  "app.market_cn.auto.tools.explain",     # 结构化报告 (JSON+MD)
    "pool":     "app.market_cn.auto.tools.pool_check",  # 信号级复验闸门
    "proposal": "app.market_cn.auto.tools.proposal",    # 建议产物 校验/应用/回滚
}

# 旧 CLI 别名 (与 tools/replay.py 保持一致)
_ALIAS = {"dragon": "dragon_callback", "dragon2": "dragon_v2", "break_buy": "break"}

_USAGE = """strategy_cli.py — 自动策略 IDE 命令行壳 (上层接线; core/ 与 scan.py 一行未改)

用法 (本文档 §7/§8 里简写为 `qd`):
  python strategy_cli.py run <策略> [--days N] [--today] [--today-date D] [--codes ...] [--json] [--out F]
  python strategy_cli.py today <策略> [--today-date D] [--days N]  # = run --today 的简写
  python strategy_cli.py list                                      # 列出已注册策略 (key/名称/类型)
  python strategy_cli.py replay <策略> --code 000001 [--days N]    # 单股逐笔重放
  python strategy_cli.py debug <策略> --code 000001 [--date D]     # 单候选逐门追踪 (为什么没出信号)
  python strategy_cli.py gates <策略> [--days N]                   # 门级审计 (拦截表 + 判别力)
  python strategy_cli.py stats <策略>                              # 入场规则归因 (固定持有口径)
  python strategy_cli.py grid <策略> --grid '{"k":[v1,v2]}'        # 参数网格 + 敏感性
  python strategy_cli.py explain <策略> [--days N]                 # 结构化报告 (JSON + MD)
  python strategy_cli.py pool ... / proposal ...                   # 信号级复验 / 建议产物

示例:
  python strategy_cli.py run v1 --days 300
  python strategy_cli.py run dragon_callback --days 120 --today
  python strategy_cli.py run v1 --days 300 --today-date 2026-09-18
  python strategy_cli.py replay v1 --code 000001 --days 400

说明:
  --days      回看窗口。日线路径 = 自然日窗口; 盘中(intraday_window)路径 = 日历回看天数,
              进度按**交易日**推进。
  --today     附: 只列指定日(默认今天)出现买点的股票 (按买入日 entry_date 过滤)。
  --codes     股票池, 逗号或空格分隔; 留空 = 全市场。
              PowerShell 下请加引号: --codes "000533,000859"
              (未加引号会被 PS 当数组解析, 数字前导零丢失 → 静默 0 结果)
"""


# ================================================================
# 公共: 环境 / 注册表 / 结果排版
# ================================================================

def _load_env() -> None:
    """加载 .env (走 core._paths 唯一锚点, 与各工具同源; 幂等无害)。"""
    from app.market_cn.auto.core._paths import load_env_first_found
    load_env_first_found(os.path.join(os.getcwd(), ".env"))


def _keys() -> list:
    """全部策略 key: config.json ∪ autodiscover (与 store 的查询范围同源)。"""
    from app.market_cn.auto import strategies as reg
    reg.autodiscover()
    from app.market_cn.auto.registry import strategy_keys
    return list(strategy_keys())


def _board_name(code: str) -> str:
    from app.market_cn.auto.core.market import get_board_name
    return get_board_name(code)


def _num(v, fmt: str = "{:+.2f}") -> str:
    return "n/a" if v is None else fmt.format(v)


def _d10(v) -> str:
    """日期字段 → YYYY-MM-DD (缺失返回空串)。"""
    return str(v)[:10] if v else ""


def _val(v) -> str:
    return "" if v is None else str(v)


def _split_codes(raw: str):
    """`--codes` 解析: **逗号或空格**均可作分隔。

    为什么两种都收: PowerShell 把未加引号的 `a,b,c` 当数组、再用 $OFS(空格) 拼成
    **单个参数** `"a b c"` —— 只按逗号切就会把整串当成一个非法代码, 静默 0 结果。
    同时接受空格分隔, 加不加引号都不会踩坑。
    """
    import re
    return [c for c in re.split(r"[,\s]+", raw or "") if c] or None


def _print_stats(key: str, stats: dict, elapsed=None, codes_ok=None) -> None:
    if not stats or not stats.get("n"):
        print(f"[{key}] 窗口内无逐笔成交 (n=0)")
        return
    pk = stats.get("peak") or {}
    print(f"[{key}] {stats['n']} 笔 | 胜率 {stats['winrate']}% | 均收 {stats['avg_ret']}% "
          f"| 盈亏比 {stats.get('pl_ratio')} | 月均 {stats.get('monthly_avg')} 笔 "
          f"| 日均 {stats.get('ret_per_day')}%")
    print(f"        两段胜率 {stats.get('winrate_1st_half')}% / "
          f"{stats.get('winrate_2nd_half')}% | 峰值均值 {pk.get('mean')}% "
          f"(<10:{pk.get('lt10')} 10~20:{pk.get('10_20')} >=20:{pk.get('ge20')}) "
          f"| 五分桶 {stats.get('ret_buckets')}")
    if elapsed is not None:
        print(f"        耗时 {elapsed}s | codes_ok {codes_ok}")


def _trade_line(t: dict) -> str:
    """逐笔单行。字段按"有则显示"拼接 —— 冻结 .py 与 YAML 门表两路的 trade dict 键
    并不一致 (如 v1 有 `d0_date` 而无 `signal_date`/`exit_reason`), 缺字段就不印, 不印 `?` 噪音。

    板块一律走 `get_board_name(code)` (spec 驱动), 不依赖 trade 里可能叫 `board`/`board_type`
    且中英不一的字段 —— 两条路径的显示口径因此统一。
    """
    code = str(t.get("code", "?"))
    bits = [f"    {code:<8} {_board_name(code):<6}"]
    sig = _d10(t.get("signal_date") or t.get("d0_date") or t.get("lu_date"))
    if sig:
        bits.append(f"信号 {sig}")
    bits.append(f"买入 {_d10(t.get('entry_date')) or '?'}@{_val(t.get('entry_price'))}")
    hold = t.get("exit_day")
    hold = f"{hold}日" if hold is not None else ""
    reason = str(t.get("exit_reason") or "")
    seg = " ".join(x for x in (reason, hold) if x)
    bits.append(f"出场 {seg}@{_val(t.get('exit_price'))}" if seg else f"出场 @{_val(t.get('exit_price'))}")
    bits.append(f"| 收益 {_num(t.get('return_pct'))}% 峰值 {_num(t.get('peak_return_pct'))}%")
    return " ".join(bits)


def _print_trades(trades: list, limit: int = 40) -> None:
    if not trades:
        return
    print(f"  ---- 逐笔 (前 {min(limit, len(trades))} / {len(trades)}) ----")
    for t in trades[:limit]:
        print(_trade_line(t))
    if len(trades) > limit:
        print(f"    ... 另有 {len(trades) - limit} 笔 (全量见 --json 或 --out)")


def _print_today(trades: list, date_str: str, limit: int = 60) -> None:
    """指定日买点清单 (按买入日 entry_date 过滤, 与 test_dragon --today 同口径)。"""
    hits = [t for t in trades if str(t.get("entry_date") or "")[:10] == date_str]
    print("=" * 72)
    print(f"{date_str} 买点统计")
    print("=" * 72)
    if not hits:
        print("  该日无买点。")
        days = sorted({str(t.get("entry_date"))[:10] for t in trades if t.get("entry_date")})
        if days:
            print(f"  窗口内有买点的交易日 (最近 8 个): {', '.join(days[-8:])}")
        return
    by_board: dict = {}
    for t in hits:
        by_board.setdefault(_board_name(str(t.get("code", ""))), []).append(t)
    print(f"  共 {len(hits)} 只 | 板块分布: " + " ".join(
        f"{b} {len(ts)}" for b, ts in sorted(by_board.items(), key=lambda kv: -len(kv[1]))))
    print(f"  代码: {', '.join(sorted({str(t.get('code', '')) for t in hits}))}")
    print(f"  ---- 明细 (按收益降序) ----")
    ordered = sorted(hits, key=lambda x: (x.get("return_pct") is None,
                                          -(x.get("return_pct") or 0.0)))
    for t in ordered[:limit]:
        print(_trade_line(t))
    if len(ordered) > limit:
        print(f"    ... 另有 {len(ordered) - limit} 只 (全量见 --json 或 --out)")


# ================================================================
# 子命令
# ================================================================

def _cmd_list() -> int:
    from app.market_cn.auto import strategies as reg
    reg.autodiscover()
    print("已注册策略 (config.json ∪ 磁盘插件):")
    for k in _keys():
        s = reg.get_strategy(k)
        if s is None:
            print(f"  {k:<16} {'':<10} (无插件: 历史行仍可查)")
            continue
        spec = getattr(s, "scan_spec", None)
        print(f"  {k:<16} {getattr(s, 'name', '') or '':<10} {getattr(spec, 'kind', '')}")
    print("\n别名: " + ", ".join(f"{a}→{b}" for a, b in _ALIAS.items()))
    return 0


def _forward(cmd: str, rest: list):
    """转发到既有工具: 补 sys.argv 后调其 main() (不动各工具的 argparse)。

    位置式策略名归一: `_USAGE` 里写的是 `qd replay <策略> --code ...`, 但各工具的
    argparse 只认 `--strategy <key>` —— 直接转发会把 `v1` 当**未知位置参数**报错
    (2026-09-21 发现该文档与实现不一致)。这里统一把首个非选项 token 转成
    `--strategy <key>`(**含别名解析**), 一处修好 8 个转发子命令, 且工具侧 argparse
    仍是唯一出处 (不复制参数定义)。各转发工具的 positionals 集为空, 故归一安全。
    """
    import importlib
    rest = list(rest)
    if rest and not rest[0].startswith("-"):
        rest = ["--strategy", _ALIAS.get(rest[0], rest[0])] + rest[1:]
    mod = importlib.import_module(_FORWARD[cmd])
    old = list(sys.argv)
    sys.argv = [cmd] + rest
    try:
        rc = mod.main()
        return int(rc) if isinstance(rc, int) else 0
    finally:
        sys.argv = old


def _cmd_run(argv: list, today_default: bool = False) -> int:
    import argparse
    import json
    import time

    ap = argparse.ArgumentParser(prog="qd run",
                                 description="回测调试 (策略经注册表分发, 判定路径与实盘同源)")
    ap.add_argument("strategy", nargs="?", default="dragon_callback",
                    help="策略 key, 默认 dragon_callback (别名 dragon 亦可); 见 `qd list`")
    ap.add_argument("--days", type=int, default=300, help="回看窗口 (默认 300)")
    ap.add_argument("--today", action="store_true",
                    help="附: 只列指定日(默认今天)出现买点的股票")
    ap.add_argument("--today-date", default="", help="--today 的目标日 YYYY-MM-DD (默认今天)")
    ap.add_argument("--codes", default="", help="逗号分隔股票池; 空=全市场")
    ap.add_argument("--start-date", default="", help="窗口起点 (盘中策略精确复现)")
    ap.add_argument("--end-date", default="", help="窗口终点")
    ap.add_argument("--no-prefilter", action="store_true", help="关闭 U1~U4 预筛 (日线路径)")
    ap.add_argument("--progress-every", type=int, default=0, help="每 N 只打一行进度 (0=引擎默认)")
    ap.add_argument("--json", action="store_true", help="只打印逐笔 JSON (供 AI/对账)")
    ap.add_argument("--out", default="", help="逐笔 JSON 落盘路径 (+ 同名 .meta.json)")
    a = ap.parse_args(argv)

    key = _ALIAS.get(a.strategy, a.strategy)
    keys = _keys()
    if key not in keys:
        print(f"策略 {key} 未注册。可用: {', '.join(keys)}")
        return 2

    from app.market_cn.auto.core.backtest import run_all
    codes = _split_codes(a.codes)
    kw = dict(strategy=key, days=a.days, codes=codes,
              use_prefilter=not a.no_prefilter,
              start_date=a.start_date or None, end_date=a.end_date or None)
    if a.progress_every:
        kw["progress_every"] = a.progress_every

    res = run_all(**kw)
    trades = res.get("trades") or []

    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(trades, f, ensure_ascii=False)
        with open(a.out + ".meta.json", "w", encoding="utf-8") as f:
            json.dump(res.get("meta", {}), f, ensure_ascii=False, indent=2)
        print(f"已写出: {a.out} | meta: {a.out}.meta.json")

    if a.json:
        print(json.dumps(trades, ensure_ascii=False, indent=1))
        return 0

    _print_stats(key, res.get("stats") or {}, res.get("elapsed"), res.get("codes_ok"))
    _print_trades(trades)
    if a.today or today_default or a.today_date:
        _print_today(trades, a.today_date or time.strftime("%Y-%m-%d"))
    else:
        print("提示: 加 --today 看指定日买点清单 (或 `qd today <策略>`)。")
    return 0


# ================================================================

def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(_USAGE)
        return 0
    cmd, rest = argv[0], argv[1:]

    _load_env()

    if cmd in ("list", "ls", "strategies"):
        return _cmd_list()
    if cmd in _FORWARD:
        return _forward(cmd, rest)
    if cmd in ("run", "backtest", "bt", "today"):
        return _cmd_run(rest, today_default=(cmd == "today"))

    print(f"未知子命令: {cmd}\n")
    print(_USAGE)
    return 2


if __name__ == "__main__":
    sys.exit(main())

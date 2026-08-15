#!/usr/bin/env python3
"""
market_screener 独立调试 & 回测工具

═══════════════════════════════════════════════════════════════════
  用途
═══════════════════════════════════════════════════════════════════

  对 backend_api_python/app/agent/skills/market_screener/ 进行:
  1. 独立调试 — 不启动 Flask/smolagents，直接调用 skill 内部函数
  2. 策略回测 — 对筛选结果做 T+N 回测，验证选股质量
  3. 分策略测试 — 单独测试 early/intraday/eod/post_market
  4. 全流程测试 — pre_screen → filter → deep_analyze 完整链路

═══════════════════════════════════════════════════════════════════
  使用方式
═══════════════════════════════════════════════════════════════════

  # 测试完整 run() 流程
  python test_market_screener.py run

  # 测试 pre_screen（获取候选股）
  python test_market_screener.py prescreen --strategy intraday

  # 测试 deep_analyze（深入分析指定股票）
  python test_market_screener.py deep --codes 000001,600519,300750

  # 回测模式：对筛选结果做 T+N 回测
  python test_market_screener.py backtest --days 300 --max-hold 10

  # 全市场扫描回测（耗时较长）
  python test_market_screener.py backtest --source db --days 300

  # 测试龙回头检测
  python test_market_screener.py dragon

  # 测试市场状态评估
  python test_market_screener.py market

═══════════════════════════════════════════════════════════════════
"""
from __future__ import annotations
import json, time, argparse, os, sys, traceback
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

# ================================================================
# 路径初始化 — 确保 backend_api_python 和 app/agent 在 sys.path 中
# ================================================================
_backend_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend_api_python")
_agent_root = os.path.join(_backend_root, "app", "agent")
for _p in [_agent_root, _backend_root]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

def _load_env():
    """加载 .env 环境变量（按优先级查找）"""
    try:
        from dotenv import load_dotenv
        for p in [os.path.join(_backend_root, '.env'),
                  os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')]:
            if os.path.isfile(p):
                load_dotenv(p, override=False)
                break
    except Exception:
        pass

_load_env()

# ================================================================
# 延迟导入 market_screener 模块
# ================================================================
_screener_cache = {}

def _import_screener():
    """延迟导入 market_screener 模块，避免启动时就依赖全部 app 包。"""
    if "run" in _screener_cache:
        return _screener_cache
    try:
        from skills.market_screener import run as screener_run
        from skills.market_screener.common import (
            SkillReport, SkillResult, FactorItem,
            fetch_kline, compute_ma, compute_rsi, compute_macd,
            compute_volume_ratio, compute_kdj, compute_atr,
            scan_dragon_pullback, fetch_zt_pool, fetch_dt_pool,
            fetch_broken_board, fetch_hot_sectors, fetch_hot_stocks_with_reason,
            get_limit_pct, is_limit_locked, _get_writer, _get_basic_db,
        )
        from skills.market_screener._helpers import (
            select_strategy, analyze_batch, build_report, resolve_names,
            filter_candidates,
        )
        from skills.market_screener.run import (
            pre_screen, deep_analyze, run,
        )
        _screener_cache.update({
            "run_module": screener_run,
            "SkillReport": SkillReport, "SkillResult": SkillResult, "FactorItem": FactorItem,
            "fetch_kline": fetch_kline,
            "compute_ma": compute_ma, "compute_rsi": compute_rsi,
            "compute_macd": compute_macd, "compute_volume_ratio": compute_volume_ratio,
            "compute_kdj": compute_kdj, "compute_atr": compute_atr,
            "scan_dragon_pullback": scan_dragon_pullback,
            "fetch_zt_pool": fetch_zt_pool, "fetch_dt_pool": fetch_dt_pool,
            "fetch_broken_board": fetch_broken_board,
            "fetch_hot_sectors": fetch_hot_sectors,
            "fetch_hot_stocks_with_reason": fetch_hot_stocks_with_reason,
            "get_limit_pct": get_limit_pct, "is_limit_locked": is_limit_locked,
            "select_strategy": select_strategy,
            "analyze_batch": analyze_batch, "build_report": build_report,
            "resolve_names": resolve_names, "filter_candidates": filter_candidates,
            "pre_screen": pre_screen, "deep_analyze": deep_analyze,
            "run_full": run,
            "_get_writer": _get_writer, "_get_basic_db": _get_basic_db,
        })
    except ImportError as e:
        print(f"  导入失败: {e}")
        print(f"   确保 backend_api_python/app 在 sys.path 中")
        print(f"   当前 sys.path[0]: {sys.path[0] if sys.path else 'empty'}")
        sys.exit(1)
    return _screener_cache


# ================================================================
# DB 数据加载（复用 test_bb_indicator.py 模式）
# ================================================================
_writer_cache = None
def _get_writer():
    global _writer_cache
    if _writer_cache is not None:
        return _writer_cache
    _load_env()
    from app.utils.db_market import get_market_kline_writer
    _writer_cache = get_market_kline_writer()
    return _writer_cache

_basic_db_cache = None
def _get_basic_db():
    global _basic_db_cache
    if _basic_db_cache is not None:
        return _basic_db_cache
    _load_env()
    from app.utils.basicinfo_db import get_stock_basic_db
    _basic_db_cache = get_stock_basic_db()
    return _basic_db_cache

def get_all_codes_basicinfo(filter_st=True):
    """从 basicinfo_db 获取全市场活跃股票代码列表。"""
    db = _get_basic_db()
    stocks = db.get_all_stocks(status="active")
    if filter_st:
        stocks = [s for s in stocks if "ST" not in s.get("name", "").upper()]
    return [s["symbol"] for s in stocks]

def get_stock_name_map():
    """获取全市场股票 code→name 映射"""
    db = _get_basic_db()
    stocks = db.get_all_stocks(status="active")
    return {s["symbol"]: s["name"] for s in stocks}

def fetch_kline_db(code, days=300):
    """从 db_market 获取 K 线数据"""
    from app.data_sources.provider.adjustment import unadj_to_qfq
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=int(days * 1.5))).strftime("%Y-%m-%d")
    try:
        writer = _get_writer()
        data = writer.query("CNStock", code, "1D", start_time=start, end_time=end, limit=0)
        if not data:
            return []
        bars = [{
            "time": str(r["time"])[:10], "open": float(r["open"]),
            "high": float(r["high"]), "low": float(r["low"]),
            "close": float(r["close"]), "volume": float(r["volume"]),
        } for r in data]
        return unadj_to_qfq(bars, code)
    except Exception:
        return []

# kline_cache 备用
try:
    from kline_cache import fetch_kline as _fetch_kline_cache
except ImportError:
    _fetch_kline_cache = None


# ================================================================
# 回测引擎（复用 test_bb_indicator.py 的出场逻辑）
# ================================================================
def run_backtest(bars, entry_idx, entry_price,
                 trailing_stop_pct=-10.0, max_hold_days=10,
                 early_stop_pct=-5.0, early_stop_days=3):
    """
    通用回测引擎 — 按优先级检查出场条件。

    出场规则:
      ① 跟踪止损: 从持仓期间最高点回撤超过 trailing_stop_pct → 出场
      ①b 持仓 early_stop_days 天内亏损超过 early_stop_pct → 早期止损
      ② 持仓天数上限
    """
    if entry_price <= 0 or entry_idx >= len(bars):
        return None

    peak = entry_price
    exit_p = entry_price
    exit_d = 0
    exit_reason = ""
    max_d = len(bars) - entry_idx - 1

    for d in range(0, max_d + 1):
        idx = entry_idx + d
        if idx >= len(bars):
            break
        b = bars[idx]
        if b['high'] > peak:
            peak = b['high']

        exit_p = b['close']
        exit_d = d
        exit_reason = "持仓中"

        # d=0 为入场当天，只更新峰值
        if d == 0:
            continue

        # ① 跟踪止损
        trailing_ref = peak * (1 + trailing_stop_pct / 100)
        if b['low'] <= trailing_ref:
            exit_p = trailing_ref
            exit_d = d
            exit_reason = "跟踪止损"
            break

        # ①b 早期止损
        if d <= early_stop_days and entry_price > 0:
            early_stop_price = entry_price * (1 + early_stop_pct / 100)
            if b['low'] <= early_stop_price:
                exit_p = early_stop_price
                exit_d = d
                exit_reason = "早期止损"
                break

        # ② 持仓天数上限
        if max_hold_days > 0 and d >= max_hold_days:
            exit_p = b['close']
            exit_d = d
            exit_reason = "持仓到期"
            break

    return {
        'exit_price': round(exit_p, 3),
        'exit_day': exit_d,
        'exit_reason': exit_reason,
        'return_pct': round((exit_p / entry_price - 1) * 100, 2),
        'peak_return_pct': round((peak / entry_price - 1) * 100, 2),
    }


# ================================================================
# 工具函数
# ================================================================
def get_board_name(code):
    c = str(code)[:3]
    if c.startswith("68"): return "科创板"
    elif c.startswith("30"): return "创业板"
    elif c.startswith("6"): return "沪主板"
    elif c.startswith(("0", "2")): return "深主板"
    return "未知"


def print_stats(trades, label):
    """输出交易统计"""
    if not trades:
        print(f"  {label}: 无交易")
        return
    n = len(trades)
    wins = [t for t in trades if t['return_pct'] > 0]
    losses = [t for t in trades if t['return_pct'] <= 0]
    wr = len(wins) / n * 100
    avg = sum(t['return_pct'] for t in trades) / n
    peak = sum(t['peak_return_pct'] for t in trades) / n
    if wins and losses:
        avg_win = sum(t['return_pct'] for t in wins) / len(wins)
        avg_loss = abs(sum(t['return_pct'] for t in losses) / len(losses))
        pl = avg_win / avg_loss if avg_loss > 0 else 999.0
    elif wins:
        pl = 999.0
    else:
        pl = 0.0
    total_ret = sum(t['return_pct'] for t in trades)
    max_dd = min(t['return_pct'] for t in trades)
    # 持仓天数统计
    avg_hold = sum(t.get('exit_day', 0) for t in trades) / n
    # 单位时间收益率 = (胜率×平均盈利 - 败率×平均亏损) / 平均持仓天数
    avg_win_pct = sum(t['return_pct'] for t in wins) / len(wins) if wins else 0
    avg_loss_pct = abs(sum(t['return_pct'] for t in losses) / len(losses)) if losses else 0
    return_per_day = (wr / 100 * avg_win_pct - (1 - wr / 100) * avg_loss_pct) / avg_hold if avg_hold > 0 else 0

    print(f"  {label}:")
    print(f"    笔数: {n}  胜率: {wr:.1f}%  均收益: {avg:+.2f}%  均峰值: {peak:+.2f}%")
    print(f"    盈亏比: {pl:.2f}  总收益: {total_ret:+.2f}%  最大单笔: {max_dd:+.2f}%")
    print(f"    均持仓: {avg_hold:.1f}天  单位时间收益率: {return_per_day:+.4f}%")

    # 出场原因分布
    reasons = {}
    for t in trades:
        r = t.get('exit_reason', '未知')
        reasons[r] = reasons.get(r, 0) + 1
    print(f"    出场分布: ", end="")
    print(" | ".join(f"{r}:{c}" for r, c in sorted(reasons.items(), key=lambda x: -x[1])))


# ================================================================
# 命令: run — 完整 run() 流程测试
# ================================================================
def cmd_run(args):
    """测试 market_screener.run() 完整流程"""
    m = _import_screener()
    print(f"{'=' * 80}")
    print(f"market_screener.run() 完整流程测试")
    print(f"{'=' * 80}")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"策略: {m['select_strategy']()}")
    print()

    t0 = time.time()
    try:
        result = m['run_full']()
        elapsed = time.time() - t0
        print(f"  执行完成 ({elapsed:.1f}s)")
        print(f"\n{'─' * 80}")
        print(result)
        print(f"{'─' * 80}")
    except Exception as e:
        print(f"  执行失败: {e}")
        traceback.print_exc()


# ================================================================
# 命令: prescreen — 测试 pre_screen()
# ================================================================
def cmd_prescreen(args):
    """测试 pre_screen() 获取候选股"""
    m = _import_screener()
    strategy = args.strategy or m['select_strategy']()

    print(f"{'=' * 80}")
    print(f"pre_screen() 测试 — 策略: {strategy}")
    print(f"{'=' * 80}")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    t0 = time.time()
    try:
        result = m['pre_screen']()
        elapsed = time.time() - t0

        if isinstance(result, dict):
            strat = result.get("strategy", strategy)
            market = result.get("market", {})
            candidates = result.get("candidates", [])
            themes = result.get("main_themes", [])

            print(f"  执行完成 ({elapsed:.1f}s)")
            print(f"\n  策略: {strat}")
            print(f"  市场状态:")
            if market:
                print(f"    情绪: {market.get('mood', '-')} ({market.get('mood_score', 0)})")
                print(f"    涨停: {market.get('zt_count', 0)}  跌停: {market.get('dt_count', 0)}")
                print(f"    资金流: {market.get('fund_flow', 0)}")
                print(f"    炸板率: {market.get('broken_rate', 0)}%")
            else:
                print(f"    (无数据)")

            print(f"\n  主线题材: {len(themes)} 个")
            for t in themes[:5]:
                if isinstance(t, (list, tuple)) and len(t) >= 2:
                    print(f"    {t[0]} ({t[1]})")

            print(f"\n  候选股: {len(candidates)} 只")
            for c in candidates[:20]:
                src = c.get("source", "")
                name = c.get("name", "")
                code = c.get("code", "")
                change = c.get("change_pct", 0) or 0
                trn = c.get("turnover_pct", 0) or 0
                price = c.get("price") or c.get("close") or 0
                reason = c.get("reason", "")
                print(f"    {code:<8} {name:<8} {src:<10} "
                      f"涨跌:{change:+.1f}% 换手:{trn:.1f}% 价:{price:.2f}"
                      f"{' | ' + reason if reason else ''}")

            # 筛选测试
            print(f"\n{'─' * 80}")
            print(f"filter_candidates() 筛选测试:")
            codes = m['filter_candidates'](result)
            if codes:
                code_list = codes.split(",")
                print(f"  筛选结果: {len(code_list)} 只 → {codes}")
            else:
                print(f"  筛选结果: 无符合条件的股票")

        else:
            print(f"  返回类型异常: {type(result)}")
            print(f"  内容: {result}")

    except Exception as e:
        print(f"  执行失败: {e}")
        traceback.print_exc()


# ================================================================
# 命令: deep — 测试 deep_analyze()
# ================================================================
def cmd_deep(args):
    """测试 deep_analyze() 深入分析"""
    m = _import_screener()
    codes = args.codes
    if not codes:
        print("  请指定 --codes 参数，如 --codes 000001,600519,300750")
        return

    print(f"{'=' * 80}")
    print(f"deep_analyze() 深入分析测试")
    print(f"{'=' * 80}")
    print(f"股票: {codes}")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    t0 = time.time()
    try:
        result = m['deep_analyze'](codes)
        elapsed = time.time() - t0

        if isinstance(result, dict):
            score = result.get("score", 0)
            direction = result.get("direction", "neutral")
            confidence = result.get("confidence", 0)
            signal = result.get("signal", "")
            analyzed = result.get("analyzed", [])
            strategy = result.get("strategy", "")

            direction_cn = {"bullish": "看多", "bearish": "看空", "neutral": "中性"}.get(direction, direction)

            print(f"  执行完成 ({elapsed:.1f}s)")
            print(f"\n  策略: {strategy}")
            print(f"  综合评分: {score}/100")
            print(f"  方向: {direction_cn}")
            print(f"  置信度: {confidence}")
            print(f"  信号: {signal}")

            if analyzed:
                print(f"\n  逐只分析结果:")
                print(f"  {'代码':<10} {'名称':<10} {'评分':>6} {'方向':<8} {'置信度':>6} {'信号'}")
                print(f"  {'-' * 70}")
                for a in analyzed:
                    a_code = a.get("code", "")
                    a_name = a.get("name", "")
                    a_score = a.get("score", 0)
                    a_dir = a.get("direction", "neutral")
                    a_dir_cn = {"bullish": "看多", "bearish": "看空", "neutral": "中性"}.get(a_dir, a_dir)
                    a_conf = a.get("confidence", 0)
                    a_signal = a.get("signal", "")
                    levels = a.get("levels", {})
                    print(f"  {a_code:<10} {a_name:<10} {a_score:>6.1f} {a_dir_cn:<8} {a_conf:>6.2f} {a_signal}")
                    if levels:
                        print(f"{'':>36} 压力:{levels.get('resistance', '-')} "
                              f"支撑:{levels.get('support', '-')} "
                              f"上:{levels.get('upside_pct', '-')}% "
                              f"下:{levels.get('downside_pct', '-')}%")

            # 导出
            if args.export:
                out_file = args.export
                with open(out_file, "w", encoding="utf-8") as f:
                    json.dump(result, f, ensure_ascii=False, indent=2, default=str)
                print(f"\n  导出: {out_file}")
        else:
            print(f"  返回类型异常: {type(result)}")

    except Exception as e:
        print(f"  执行失败: {e}")
        traceback.print_exc()


# ================================================================
# 命令: backtest — 对筛选结果做 T+N 回测
# ================================================================
def cmd_backtest(args):
    """对 market_screener 筛选结果做 T+N 回测"""
    m = _import_screener()

    # 确定股票列表
    use_db = False
    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
        stock_source = "手动指定"
    elif args.source == "db":
        use_db = True
        print("  全市场扫描模式: 从 basicinfo_db 加载股票列表...")
        codes = get_all_codes_basicinfo(filter_st=True)
        stock_source = f"basicinfo_db"
        print(f"   {stock_source}: {len(codes)} 只股票")
    else:
        # 默认用一组测试股票
        codes = [
            "000001", "000002", "000063", "000066", "000100", "000157",
            "000333", "000402", "000425", "000538", "000553", "000568",
            "000586", "000601", "000625", "000637", "000651", "000720",
            "000753", "000767", "000783", "000800", "000858", "000925",
            "000950", "001208", "001259", "001316", "002010", "002011",
            "002012", "002013", "002014", "002015", "002016", "002017",
            "002018", "002019", "002020", "002021", "002022", "002023",
            "002024", "002025", "002026", "002027", "002028", "002029",
            "002030", "002031", "300001", "300002", "300003", "300004",
            "300005", "300006", "300007", "300008", "300009", "300010",
            "300014", "300015", "300024", "300025", "300027", "300033",
            "300059", "300106", "300124", "300152", "600000", "600009",
            "600016", "600019", "600028", "600030", "600031", "600036",
            "600048", "600050", "600061", "600085", "600089", "600104",
            "600109", "600111", "600115", "600118", "600150", "600153",
            "600170", "600176", "600183", "600196", "600201", "600208",
            "600219", "600233", "600256", "600271", "600276", "600309",
            "600332", "600346", "600352", "600362", "600372", "600383",
            "600390", "600398", "600406", "600426", "600436", "600438",
            "600460", "600482", "600487", "600489", "600498", "600500",
            "600519", "600522", "600547", "600570", "600583", "600585",
            "600588", "600600", "600606", "600655", "600660", "600690",
            "600703", "600741", "600745", "600760", "600795", "600809",
            "600837", "600845", "600862", "600867", "600885", "600886",
            "600887", "600893", "600900", "600918", "600919", "600926",
            "600938", "600941", "600988", "601006", "601009", "601012",
            "601016", "601021", "601058", "601066", "601077", "601088",
            "601100", "601108", "601111", "601117", "601127", "601138",
            "601155", "601162", "601166", "601169", "601186", "601211",
            "601225", "601228", "601229", "601231", "601236", "601238",
            "601288", "601298", "601318", "601319", "601328", "601336",
            "601360", "601377", "601390", "601398", "601600", "601601",
            "601607", "601618", "601628", "601633", "601658", "601668",
            "601669", "601688", "601698", "601700", "601728", "601766",
            "601788", "601799", "601800", "601808", "601818", "601838",
            "601857", "601877", "601878", "601881", "601888", "601899",
            "601901", "601916", "601919", "601933", "601939", "601958",
            "601966", "601985", "601988", "601989", "601992", "601998",
            "603019", "603056", "603077", "603087", "603160", "603185",
            "603195", "603198", "603228", "603233", "603259", "603260",
            "603288", "603290", "603345", "603369", "603392", "603444",
            "603486", "603501", "603515", "603517", "603568", "603583",
            "603589", "603596", "603605", "603606", "603613", "603658",
            "603659", "603688", "603707", "603712", "603719", "603737",
            "603799", "603806", "603816", "603833", "603858", "603882",
            "603883", "603885", "603893", "603899", "603986", "603993",
            "688001", "688002", "688003", "688005", "688006", "688007",
            "688008", "688009", "688012", "688015", "688016", "688018",
            "688019", "688020", "688021", "688023", "688025", "688027",
            "688028", "688029", "688030", "688032", "688033", "688035",
            "688036", "688037", "688038", "688039", "688041", "688047",
            "688048", "688050", "688051", "688052", "688053", "688055",
            "688056", "688058", "688059", "688060", "688061", "688062",
            "688063", "688065", "688066", "688067", "688068", "688069",
            "688070", "688071", "688072", "688073", "688075", "688076",
            "688077", "688078", "688079", "688080", "688081", "688082",
            "688083", "688084", "688085", "688087", "688088", "688089",
            "688090", "688091", "688092", "688093", "688095", "688096",
            "688097", "688098", "688099", "688100", "688101", "688102",
            "688103", "688105", "688106", "688107", "688108", "688109",
            "688110", "688111", "688112", "688113", "688114", "688115",
            "688116", "688117", "688118", "688119", "688120", "688121",
            "688122", "688123", "688125", "688126", "688127", "688128",
            "688129", "688130", "688131", "688132", "688133", "688135",
            "688136", "688137", "688138", "688139", "688140", "688141",
            "688143", "688146", "688147", "688148", "688149", "688150",
            "688151", "688152", "688153", "688155", "688156", "688157",
            "688158", "688159", "688160", "688161", "688162", "688163",
            "688165", "688166", "688167", "688168", "688169", "688170",
            "688171", "688172", "688173", "688175", "688176", "688177",
            "688178", "688179", "688180", "688181", "688182", "688183",
            "688184", "688185", "688186", "688187", "688188", "688189",
            "688190", "688191", "688192", "688193", "688195", "688196",
            "688197", "688198", "688199", "688200", "688201", "688202",
            "688203", "688205", "688206", "688207", "688208", "688209",
            "688210", "688211", "688212", "688213", "688215", "688216",
            "688217", "688218", "688219", "688220", "688221", "688222",
            "688223", "688225", "688226", "688227", "688228", "688229",
            "688230", "688231", "688232", "688233", "688234", "688235",
            "688236", "688237", "688238", "688239", "688240", "688241",
            "688242", "688243", "688244", "688245", "688246", "688247",
            "688248", "688249", "688250", "688251", "688252", "688253",
            "688255", "688256", "688257", "688258", "688259", "688260",
            "688261", "688262", "688263", "688265", "688266", "688267",
            "688268", "688269", "688270", "688271", "688272", "688273",
            "688275", "688276", "688277", "688278", "688279", "688280",
            "688281", "688282", "688283", "688285", "688286", "688287",
            "688288", "688289", "688290", "688291", "688292", "688293",
            "688295", "688296", "688297", "688298", "688299", "688300",
            "688301", "688302", "688303", "688305", "688306", "688307",
            "688308", "688309", "688310", "688311", "688312", "688313",
            "688314", "688315", "688316", "688317", "688318", "688319",
            "688320", "688321", "688322", "688323", "688325", "688326",
            "688327", "688328", "688329", "688330", "688331", "688332",
            "688333", "688334", "688335", "688336", "688337", "688338",
            "688339", "688340", "688341", "688343", "688345", "688346",
            "688347", "688348", "688349", "688350", "688351", "688352",
            "688353", "688355", "688356", "688357", "688358", "688359",
            "688360", "688361", "688362", "688363", "688365", "688366",
            "688367", "688368", "688369", "688370", "688371", "688372",
            "688373", "688375", "688376", "688377", "688378", "688379",
            "688380", "688381", "688382", "688383", "688385", "688386",
            "688387", "688388", "688389", "688390", "688391", "688392",
            "688393", "688395", "688396", "688397", "688398", "688399",
            "688400", "688401", "688402", "688403", "688405", "688406",
            "688407", "688408", "688409", "688410", "688411", "688412",
            "688413", "688414", "688415", "688416", "688417", "688418",
            "688419", "688420", "688421", "688422", "688423", "688424",
            "688425", "688426", "688427", "688428", "688429", "688430",
            "688431", "688432", "688433", "688434", "688435", "688436",
            "688437", "688438", "688439", "688440", "688441", "688442",
            "688443", "688444", "688445", "688446", "688447", "688448",
            "688449", "688450", "688451", "688452", "688453", "688454",
            "688455", "688456", "688457", "688458", "688459", "688460",
            "688461", "688462", "688463", "688464", "688465", "688466",
            "688467", "688468", "688469", "688470", "688471", "688472",
            "688473", "688474", "688475", "688476", "688477", "688478",
            "688479", "688480", "688481", "688482", "688483", "688484",
            "688485", "688486", "688487", "688488", "688489", "688490",
            "688491", "688492", "688493", "688494", "688495", "688496",
            "688497", "688498", "688499", "688500", "688501", "688502",
            "688503", "688505", "688506", "688507", "688508", "688509",
            "688510", "688511", "688512", "688513", "688514", "688515",
            "688516", "688517", "688518", "688519", "688520", "688521",
            "688522", "688523", "688525", "688526", "688527", "688528",
            "688529", "688530", "688531", "688532", "688533", "688534",
            "688535", "688536", "688537", "688538", "688539", "688540",
            "688541", "688542", "688543", "688544", "688545", "688546",
            "688547", "688548", "688549", "688550", "688551", "688552",
            "688553", "688555", "688556", "688557", "688558", "688559",
            "688560", "688561", "688562", "688563", "688565", "688566",
            "688567", "688568", "688569", "688570", "688571", "688572",
            "688573", "688575", "688576", "688577", "688578", "688579",
            "688580", "688581", "688582", "688583", "688584", "688585",
            "688586", "688587", "688588", "688589", "688590", "688591",
            "688592", "688593", "688595", "688596", "688597", "688598",
            "688599", "688600", "688601", "688602", "688603", "688605",
            "688606", "688607", "688608", "688609", "688610", "688611",
            "688612", "688613", "688614", "688615", "688616", "688617",
            "688618", "688619", "688620", "688621", "688622", "688623",
            "688625", "688626", "688627", "688628", "688629", "688630",
            "688631", "688632", "688633", "688634", "688635", "688636",
            "688637", "688638", "688639", "688640", "688641", "688642",
            "688643", "688644", "688645", "688646", "688647", "688648",
            "688649", "688650", "688651", "688652", "688653", "688655",
            "688656", "688657", "688658", "688659", "688660", "688661",
            "688662", "688663", "688664", "688665", "688666", "688667",
            "688668", "688669", "688670", "688671", "688672", "688673",
            "688675", "688676", "688677", "688678", "688679", "688680",
            "688681", "688682", "688683", "688684", "688685", "688686",
            "688687", "688688", "688689", "688690", "688691", "688692",
            "688693", "688695", "688696", "688697", "688698", "688699",
            "688700", "688701", "688702", "688703", "688705", "688706",
            "688707", "688708", "688709", "688710", "688711", "688712",
            "688713", "688714", "688715", "688716", "688717", "688718",
            "688719", "688720", "688721", "688722", "688723", "688725",
            "688726", "688727", "688728", "688729", "688730", "688731",
            "688732", "688733", "688734", "688735", "688736", "688737",
            "688738", "688739", "688740", "688741", "688742", "688743",
            "688744", "688745", "688746", "688747", "688748", "688749",
            "688750", "688751", "688752", "688753", "688754", "688755",
            "688756", "688757", "688758", "688759", "688760", "688761",
            "688762", "688763", "688764", "688765", "688766", "688767",
            "688768", "688769", "688770", "688771", "688772", "688773",
            "688774", "688775", "688776", "688777", "688778", "688779",
            "688780", "688781", "688782", "688783", "688784", "688785",
            "688786", "688787", "688788", "688789", "688790", "688791",
            "688792", "688793", "688795", "688796", "688797", "688798",
            "688799", "688800",
        ]
        stock_source = "测试股票池"

    print(f"{'=' * 80}")
    print(f"market_screener 回测")
    print(f"{'=' * 80}")
    print(f"股票来源: {stock_source} ({len(codes)}只)")
    print(f"回测天数: {args.days}  最大持仓: {args.max_hold}天")
    print(f"跟踪止损: {args.trailing_stop}%  早期止损: {args.early_stop}%")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    # ---- 加载 K 线数据 ----
    name_map = {}
    if use_db:
        try:
            name_map = get_stock_name_map()
        except Exception:
            pass

    all_bars = {}
    loaded = 0
    for i, code in enumerate(codes):
        if use_db:
            bars = fetch_kline_db(code, args.days)
        elif _fetch_kline_cache:
            bars = _fetch_kline_cache(code, args.days)
        else:
            bars = fetch_kline_db(code, args.days)
        if bars and len(bars) > 60:
            all_bars[code] = bars
            loaded += 1
        if (i + 1) % 500 == 0:
            print(f"  已加载 {i + 1}/{len(codes)} ({loaded} 有效)")

    print(f"  K线加载完成: {loaded}/{len(codes)} 只有效")

    # ---- 运行 market_screener 获取候选股 ----
    strategy = "unknown"
    prescreen_result = {}
    print(f"\n  运行 market_screener 获取候选股...")
    try:
        prescreen_result = m['pre_screen']()
        candidates = prescreen_result.get("candidates", []) if isinstance(prescreen_result, dict) else []
        strategy = prescreen_result.get("strategy", "unknown") if isinstance(prescreen_result, dict) else "unknown"
        print(f"  策略: {strategy}, 候选: {len(candidates)} 只")
    except Exception as e:
        print(f"  pre_screen 失败: {e}")
        candidates = []

    # 筛选
    screened_codes = []
    try:
        codes_str = m['filter_candidates'](prescreen_result)
        screened_codes = [c.strip() for c in codes_str.split(",") if c.strip()] if codes_str else []
        print(f"  筛选后: {len(screened_codes)} 只")
    except Exception as e:
        print(f"  filter_candidates 失败: {e}")

    # ---- 回测筛选出的股票 ----
    all_trades = []
    if screened_codes:
        print(f"\n  回测筛选结果 ({len(screened_codes)} 只)...")
        for code in screened_codes:
            bars = all_bars.get(code)
            if not bars:
                continue
            trades = _backtest_single(
                bars, code, args.days,
                args.trailing_stop, args.early_stop, args.max_hold,
            )
            # 添加股票名称
            for t in trades:
                t['name'] = name_map.get(code, '')
            all_trades.extend(trades)
    else:
        print(f"\n  无筛选结果，对全部股票做信号扫描回测...")
        for code, bars in all_bars.items():
            trades = _backtest_single(
                bars, code, args.days,
                args.trailing_stop, args.early_stop, args.max_hold,
            )
            for t in trades:
                t['name'] = name_map.get(code, '')
            all_trades.extend(trades)

    # ---- 汇总 ----
    print(f"\n{'=' * 80}")
    print(f"回测结果: {stock_source}, {len(all_bars)} 只")
    print(f"{'=' * 80}")

    if all_trades:
        print_stats(all_trades, "全部交易")

        # 按出场原因分组统计
        reason_groups = {}
        for t in all_trades:
            r = t.get('exit_reason', '未知')
            if r not in reason_groups:
                reason_groups[r] = []
            reason_groups[r].append(t)
        print(f"\n  按出场原因:")
        for reason, trades in sorted(reason_groups.items(), key=lambda x: -len(x[1])):
            print_stats(trades, f"  {reason}")

        # TOP N
        n = min(args.top, len(all_trades))
        print(f"\n  TOP{n} 盈利:")
        for t in sorted(all_trades, key=lambda x: -x['return_pct'])[:n]:
            print(f"    {t['code']:<8} {t['board']:<6} "
                  f"{t['entry_date']}买{t['entry_price']:>7.2f} -> "
                  f"收益{t['return_pct']:>+6.2f}% 峰值{t['peak_return_pct']:>+6.2f}% "
                  f"持仓{t['exit_day']}天 [{t['exit_reason']}]")

        print(f"\n  TOP{n} 亏损:")
        for t in sorted(all_trades, key=lambda x: x['return_pct'])[:n]:
            print(f"    {t['code']:<8} {t['board']:<6} "
                  f"{t['entry_date']}买{t['entry_price']:>7.2f} -> "
                  f"收益{t['return_pct']:>+6.2f}% 峰值{t['peak_return_pct']:>+6.2f}% "
                  f"持仓{t['exit_day']}天 [{t['exit_reason']}]")

    # ---- 导出 ----
    if all_trades:
        out_file = args.export or "test_market_screener_backtest.json"
        export_data = {
            "meta": {
                "source": stock_source,
                "strategy": strategy,
                "total_codes": len(codes),
                "loaded_codes": len(all_bars),
                "screened_codes": len(screened_codes),
                "days": args.days,
                "max_hold": args.max_hold,
                "trailing_stop": args.trailing_stop,
                "timestamp": datetime.now().isoformat(),
            },
            "trades": all_trades,
        }
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(export_data, f, ensure_ascii=False, indent=2)
        print(f"\n  导出: {out_file}")
    else:
        print("\n  无交易记录。")


def _backtest_single(bars, code, days=300, trailing_stop=-10.0, early_stop=-5.0, max_hold=10):
    """对单只股票做简单信号回测（基于技术指标生成入场信号）

    入场信号: RSI<35 且 收盘>MA20 且 量比>1.2
    出场信号: 由 run_backtest 引擎按优先级判断
    """
    if len(bars) < 60:
        return []

    m = _import_screener()
    closes = [b['close'] for b in bars]
    volumes = [b['volume'] for b in bars]

    # 计算入场信号所需指标
    rsi = m['compute_rsi'](closes, 14)
    ma20 = m['compute_ma'](closes, 20)
    vol_ratio = m['compute_volume_ratio'](volumes, 5)

    trades = []
    used_dates = set()

    for i in range(60, len(bars) - 1):
        # 简单入场信号: RSI<35 且 收盘>MA20 且 量比>1.2
        if (rsi[i] < 35
            and ma20[i] is not None and closes[i] > ma20[i]
            and vol_ratio[i] > 1.2
            and bars[i]['time'] not in used_dates):

            entry_price = bars[i + 1]['open']
            entry_idx = i + 1
            entry_date = bars[i + 1]['time']

            if entry_price <= 0:
                continue
            used_dates.add(bars[i]['time'])

            result = run_backtest(
                bars, entry_idx, entry_price,
                trailing_stop_pct=trailing_stop,
                max_hold_days=max_hold,
                early_stop_pct=early_stop,
            )
            if not result:
                continue

            trades.append({
                'code': code,
                'board': get_board_name(code),
                'signal_date': bars[i]['time'],
                'entry_date': entry_date,
                'entry_price': round(entry_price, 3),
                'signal_rsi': round(rsi[i], 2),
                'signal_vol_ratio': round(vol_ratio[i], 3),
                'source': 'market_screener_backtest',
                **result,
            })

    return trades


# ================================================================
# 命令: dragon — 测试龙回头检测
# ================================================================
def cmd_dragon(args):
    """测试龙回头检测"""
    m = _import_screener()
    print(f"{'=' * 80}")
    print(f"龙回头检测测试")
    print(f"{'=' * 80}")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    t0 = time.time()
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        candidates = m['scan_dragon_pullback'](today)
        elapsed = time.time() - t0

        print(f"  执行完成 ({elapsed:.1f}s)")
        print(f"  龙回头候选: {len(candidates)} 只")

        if candidates:
            print(f"\n  {'代码':<10} {'名称':<10} {'连板':>4} {'回撤':>6} {'强度':>4} {'信号'}")
            print(f"  {'-' * 70}")
            for c in candidates[:20]:
                print(f"  {c.get('code',''):<10} {c.get('name',''):<10} "
                      f"{c.get('max_continuous_days',0):>4} "
                      f"{c.get('pullback_pct',0):>5.1f}% "
                      f"{c.get('strength_score',0):>4} "
                      f"{', '.join(c.get('signals', []))}")

            if args.export:
                out_file = args.export
                with open(out_file, "w", encoding="utf-8") as f:
                    json.dump(candidates, f, ensure_ascii=False, indent=2)
                print(f"\n  导出: {out_file}")
        else:
            print("  今日无龙回头候选。")

    except Exception as e:
        print(f"  执行失败: {e}")
        traceback.print_exc()


# ================================================================
# 命令: market — 测试市场状态评估
# ================================================================
def cmd_market(args):
    """测试市场状态评估"""
    m = _import_screener()
    print(f"{'=' * 80}")
    print(f"市场状态评估测试")
    print(f"{'=' * 80}")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    # 涨停池
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        zt_pool = m['fetch_zt_pool'](today)
        print(f"  涨停池: {len(zt_pool)} 只")
        for s in zt_pool[:10]:
            print(f"    {s.get('stock_code',''):<8} {s.get('stock_name',''):<8} "
                  f"连板:{s.get('continuous_zt_days',1)} 原因:{s.get('reason','')}")
    except Exception as e:
        print(f"  涨停池获取失败: {e}")

    # 跌停池
    try:
        dt_pool = m['fetch_dt_pool'](today)
        print(f"\n  跌停池: {len(dt_pool)} 只")
        for s in dt_pool[:5]:
            print(f"    {s.get('stock_code',''):<8} {s.get('stock_name','')}")
    except Exception as e:
        print(f"  跌停池获取失败: {e}")

    # 炸板池
    try:
        broken = m['fetch_broken_board'](today)
        print(f"\n  炸板池: {len(broken)} 只")
        for s in broken[:5]:
            print(f"    {s.get('stock_code',''):<8} {s.get('stock_name','')}")
    except Exception as e:
        print(f"  炸板池获取失败: {e}")

    # 热门板块
    try:
        sectors = m['fetch_hot_sectors']()
        if isinstance(sectors, dict) and not sectors.get("error"):
            print(f"\n  热门行业板块:")
            for s in sectors.get("industry", [])[:10]:
                print(f"    {s.get('name',''):<12} 涨跌:{s.get('change_pct',0):+.2f}%")
            print(f"\n  热门概念板块:")
            for s in sectors.get("concept", [])[:10]:
                print(f"    {s.get('name',''):<12} 涨跌:{s.get('change_pct',0):+.2f}%")
        else:
            print(f"\n  热门板块获取失败: {sectors.get('error', 'unknown')}")
    except Exception as e:
        print(f"  热门板块获取失败: {e}")

    # 强势股
    try:
        hot = m['fetch_hot_stocks_with_reason'](today)
        if isinstance(hot, dict) and not hot.get("error"):
            stocks = hot.get("stocks", [])
            tags = hot.get("hot_tags", [])
            print(f"\n  强势股: {len(stocks)} 只")
            for s in stocks[:10]:
                print(f"    {s.get('code',''):<8} {s.get('name',''):<8} "
                      f"涨跌:{s.get('change_pct',0):+.1f}% 原因:{s.get('reason','')}")
            print(f"\n  热门标签:")
            for tag, count in tags[:10]:
                print(f"    {tag}: {count}")
        else:
            print(f"\n  强势股获取失败: {hot.get('error', 'unknown')}")
    except Exception as e:
        print(f"  强势股获取失败: {e}")


# ================================================================
# 命令: indicators — 测试指标计算
# ================================================================
def cmd_indicators(args):
    """测试 market_screener 的指标计算函数"""
    m = _import_screener()
    code = args.codes.split(",")[0].strip() if args.codes else "000001"

    print(f"{'=' * 80}")
    print(f"指标计算测试 — {code}")
    print(f"{'=' * 80}")

    # 加载K线
    bars = fetch_kline_db(code, args.days) if args.days else []
    if not bars and _fetch_kline_cache:
        bars = _fetch_kline_cache(code, args.days or 120)
    if not bars:
        print(f"  无法加载 {code} 的K线数据")
        return

    print(f"  K线: {len(bars)} 根 ({bars[0]['time']} ~ {bars[-1]['time']})")

    closes = [b['close'] for b in bars]
    volumes = [b['volume'] for b in bars]

    # 计算各指标
    print(f"\n  计算指标...")

    ma5 = m['compute_ma'](closes, 5)
    ma10 = m['compute_ma'](closes, 10)
    ma20 = m['compute_ma'](closes, 20)
    rsi = m['compute_rsi'](closes, 14)
    macd = m['compute_macd'](closes)
    vol_ratio = m['compute_volume_ratio'](volumes, 5)
    kdj = m['compute_kdj'](bars)
    atr = m['compute_atr'](bars)

    # 输出最新值
    i = len(bars) - 1
    print(f"\n  最新数据 ({bars[i]['time']}):")
    print(f"    收盘: {closes[i]:.2f}")
    print(f"    MA5:  {ma5[i]:.2f}" if ma5[i] else "    MA5:  N/A")
    print(f"    MA10: {ma10[i]:.2f}" if ma10[i] else "    MA10: N/A")
    print(f"    MA20: {ma20[i]:.2f}" if ma20[i] else "    MA20: N/A")
    print(f"    RSI(14): {rsi[i]:.2f}")
    print(f"    MACD DIF: {macd['dif'][i]:.4f}  DEA: {macd['dea'][i]:.4f}  BAR: {macd['macd'][i]:.4f}")
    print(f"    量比(5日): {vol_ratio[i]:.2f}")
    print(f"    KDJ K:{kdj['k'][i]:.1f} D:{kdj['d'][i]:.1f} J:{kdj['j'][i]:.1f}")
    print(f"    ATR(14): {atr[i]:.4f}")

    # 最近10根K线的RSI和量比
    print(f"\n  最近10根K线:")
    print(f"  {'日期':<12} {'收盘':>8} {'RSI':>6} {'量比':>6} {'MACD_BAR':>10}")
    print(f"  {'-' * 50}")
    for j in range(max(0, len(bars) - 10), len(bars)):
        print(f"  {bars[j]['time']:<12} {closes[j]:>8.2f} {rsi[j]:>6.2f} "
              f"{vol_ratio[j]:>6.2f} {macd['macd'][j]:>10.4f}")


# ================================================================
# 命令: test — 测试单只股票的分析
# ================================================================
def cmd_test(args):
    """测试单只股票的完整分析流程"""
    m = _import_screener()
    code = args.codes.split(",")[0].strip() if args.codes else "000001"

    print(f"{'=' * 80}")
    print(f"单股分析测试 — {code}")
    print(f"{'=' * 80}")

    # 加载K线
    bars = fetch_kline_db(code, 120)
    if not bars and _fetch_kline_cache:
        bars = _fetch_kline_cache(code, 120)
    if not bars:
        print(f"  无法加载 {code} 的K线数据")
        return

    print(f"  K线: {len(bars)} 根 ({bars[0]['time']} ~ {bars[-1]['time']})")

    closes = [b['close'] for b in bars]
    rsi = m['compute_rsi'](closes, 14)
    ma20 = m['compute_ma'](closes, 20)
    macd = m['compute_macd'](closes)

    # 检查入场信号
    i = len(bars) - 1
    print(f"\n  最新K线信号检查:")
    print(f"    RSI: {rsi[i]:.2f} {'< 35 ✓' if rsi[i] < 35 else '>= 35 ✗'}")
    if ma20[i]:
        print(f"    收盘 {closes[i]:.2f} vs MA20 {ma20[i]:.2f} "
              f"{'收盘 > MA20 ✓' if closes[i] > ma20[i] else '收盘 <= MA20 ✗'}")
    print(f"    MACD BAR: {macd['macd'][i]:.4f} "
          f"{'柱线向上 ✓' if i > 0 and macd['macd'][i] > macd['macd'][i-1] else '柱线向下 ✗'}")

    # 测试 deep_analyze
    print(f"\n  测试 deep_analyze({code})...")
    t0 = time.time()
    try:
        result = m['deep_analyze'](code)
        elapsed = time.time() - t0
        print(f"  完成 ({elapsed:.1f}s)")
        if isinstance(result, dict):
            print(f"  评分: {result.get('score', 0)}")
            print(f"  方向: {result.get('direction', 'neutral')}")
            print(f"  置信度: {result.get('confidence', 0)}")
            print(f"  信号: {result.get('signal', '')}")
            analyzed = result.get('analyzed', [])
            for a in analyzed:
                print(f"  分析结果: {a}")
    except Exception as e:
        print(f"  失败: {e}")
        traceback.print_exc()


# ================================================================
# 主入口
# ================================================================
def main():
    parser = argparse.ArgumentParser(
        description="market_screener 独立调试 & 回测工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
命令说明:
  run          测试 market_screener.run() 完整流程
  prescreen    测试 pre_screen() 获取候选股
  deep         测试 deep_analyze() 深入分析
  backtest     对筛选结果做 T+N 回测
  dragon       测试龙回头检测
  market       测试市场状态评估
  indicators   测试指标计算函数
  test         测试单只股票完整分析流程
        """)
    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # run
    p_run = subparsers.add_parser("run", help="测试 run() 完整流程")

    # prescreen
    p_prescreen = subparsers.add_parser("prescreen", help="测试 pre_screen()")
    p_prescreen.add_argument("--strategy", default="",
                             choices=["early", "intraday", "eod", "post_market", ""],
                             help="强制指定策略（默认自动选择）")

    # deep
    p_deep = subparsers.add_parser("deep", help="测试 deep_analyze()")
    p_deep.add_argument("--codes", required=True, help="逗号分隔的股票代码")
    p_deep.add_argument("--export", default="", help="导出JSON文件路径")

    # backtest
    p_bt = subparsers.add_parser("backtest", help="T+N 回测")
    p_bt.add_argument("--codes", default="", help="逗号分隔的股票代码")
    p_bt.add_argument("--source", choices=["manual", "db"], default="manual",
                      help="数据源: manual(默认), db(全市场)")
    p_bt.add_argument("--days", type=int, default=300, help="K线天数 (默认300)")
    p_bt.add_argument("--max-hold", type=int, default=10, help="最大持仓天数 (默认10)")
    p_bt.add_argument("--trailing-stop", type=float, default=-10.0, help="跟踪止损%% (默认-10)")
    p_bt.add_argument("--early-stop", type=float, default=-5.0, help="早期止损%% (默认-5)")
    p_bt.add_argument("--top", type=int, default=10, help="TOP N 输出 (默认10)")
    p_bt.add_argument("--export", default="", help="导出JSON文件路径")

    # dragon
    p_dragon = subparsers.add_parser("dragon", help="龙回头检测")
    p_dragon.add_argument("--export", default="", help="导出JSON文件路径")

    # market
    p_market = subparsers.add_parser("market", help="市场状态评估")

    # indicators
    p_ind = subparsers.add_parser("indicators", help="指标计算测试")
    p_ind.add_argument("--codes", default="000001", help="股票代码 (默认000001)")
    p_ind.add_argument("--days", type=int, default=120, help="K线天数 (默认120)")

    # test
    p_test = subparsers.add_parser("test", help="单股分析测试")
    p_test.add_argument("--codes", default="000001", help="股票代码 (默认000001)")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    cmd_map = {
        "run": cmd_run,
        "prescreen": cmd_prescreen,
        "deep": cmd_deep,
        "backtest": cmd_backtest,
        "dragon": cmd_dragon,
        "market": cmd_market,
        "indicators": cmd_indicators,
        "test": cmd_test,
    }
    cmd_map[args.command](args)


if __name__ == "__main__":
    main()

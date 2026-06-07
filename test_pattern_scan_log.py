#!/usr/bin/env python3
"""
TSOHLCV 模式匹配 — 历史全量扫描胜率估算

═══════════════════════════════════════════════════════════════════
  原理
═══════════════════════════════════════════════════════════════════

  TSOHLCV 说明:
    T = bar 位置 (0=第一根K线, 1=第二根, ...)
    S = 最低相似度阈值 (0~100)，每条 bar 独立设定
    OHLCV = open/high/low/close/volume 的**百分比比例**值

    OHLCV 对数收益率语义 (×100, 量纲与百分比一致):
        O = ln(open  / pre_close) * 100
        H = ln(high  / pre_close) * 100
        L = ln(low   / pre_close) * 100
        C = ln(close / pre_close) * 100
        V = ln(volume / pre_volume) * 100

  流程:
    1. 定义模板 (最近N根K线的形态特征)
    2. 滑动窗口扫描历史K线，计算每根bar的OHLCV百分比
    3. 所有指定位置都满足阈值 → 记录为匹配
    4. 匹配后统计后续 1/3/5/10/20 天的涨跌幅
    5. 汇总胜率、盈亏比等统计信息

═══════════════════════════════════════════════════════════════════
"""
from __future__ import annotations
import json, time, argparse, os, sys, math
from typing import Any, Dict, List, Optional

# ================================================================
# 路径初始化
# ================================================================
_backend_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend_api_python")
if _backend_root not in sys.path:
    sys.path.insert(0, _backend_root)


def _load_env():
    """加载 .env 环境变量"""
    try:
        from dotenv import load_dotenv
        for p in [os.path.join(_backend_root, '.env'),
                  os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')]:
            if os.path.isfile(p):
                load_dotenv(p, override=False)
                break
    except Exception:
        pass


# ================================================================
# DB K线数据加载
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
    try:
        db = _get_basic_db()
        stocks = db.get_all_stocks(status="active")
        if filter_st:
            stocks = [s for s in stocks if "ST" not in s.get("name", "").upper()]
        return [s["symbol"] for s in stocks]
    except Exception:
        return []


def get_codes_from_csv(csv_path: str) -> list:
    """从 backtest CSV 文件提取去重的股票代码列表。"""
    import csv
    codes = set()
    try:
        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                code = row.get('code', '').strip()
                if code and len(code) == 6 and code.isdigit():
                    codes.add(code)
    except Exception:
        pass
    return sorted(codes)


def get_stock_name_map():
    db = _get_basic_db()
    stocks = db.get_all_stocks(status="active")
    return {s["symbol"]: s["name"] for s in stocks}


def fetch_kline_db(code, days=500):
    from datetime import datetime, timedelta
    from app.data_sources.provider.adjustment import unadj_to_qfq
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=int(days * 1.5))).strftime("%Y-%m-%d")
    try:
        writer = _get_writer()
        data = writer.query("CNStock", code, "1D", start_time=start, end_time=end, limit=0)
        if not data:
            return []
        bars = []
        for r in data:
            bars.append({
                "time": str(r["time"])[:10],
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": float(r["volume"]),
            })
        return unadj_to_qfq(bars, code)
    except Exception:
        return []


from kline_cache import fetch_kline


# ================================================================
# TSOHLCV 模式匹配核心算法 (移植自 tsohlcv.py)
# ================================================================

def _dim_similarity(template_val: float, stock_val: float) -> float:
    """计算单个维度的相似度 (0~100)。"""
    abs_t = abs(template_val)
    diff = abs(template_val - stock_val)
    if abs_t >= 1.0:
        return max(0.0, 100.0 - diff / max(abs_t, 1e-10) * 100.0)
    else:
        return max(0.0, 100.0 - diff * 50.0)


def _bar_similarity(tmpl: dict, o_pct: float, h_pct: float, l_pct: float,
                    c_pct: float, v_pct: float) -> float:
    """计算单根 bar 的 OHLCV 对数收益率相似度 (0~100)。"""
    dims = [
        _dim_similarity(tmpl.get('O', 0), o_pct),
        _dim_similarity(tmpl.get('H', 0), h_pct),
        _dim_similarity(tmpl.get('L', 0), l_pct),
        _dim_similarity(tmpl.get('C', 0), c_pct),
        _dim_similarity(tmpl.get('V', 0), v_pct),
    ]
    return sum(dims) / len(dims)


def _safe_pct(cur: float, prev: float) -> float:
    """安全计算对数收益率 (×100, 与原百分比量纲一致)。"""
    if prev <= 0 or cur <= 0:
        return 0.0
    return math.log(cur / prev) * 100.0


def match_tsohlcvs(
    tsohlcv_data: List[Dict[str, Any]],
    symbol: str = '',
    df=None,
    bars=None,
) -> Dict[str, Any]:
    """在 OHLCV 数据上扫描 TSOHLCV 模式。返回所有匹配位置。
    (使用对数收益率进行相似度比对)
    
    Args:
        tsohlcv_data: TSOHLCV 模板
        symbol: 标的代码
        df: pandas DataFrame (与 bars 二选一)
        bars: list of dicts with open/high/low/close/volume (与 df 二选一)
    """
    empty_result = {'matched': False, 'similarity': 0.0, 'matches': []}

    # 支持 bars list 或 DataFrame 两种输入
    if bars is not None and len(bars) > 0:
        opens = [float(b['open']) for b in bars]
        highs = [float(b['high']) for b in bars]
        lows = [float(b['low']) for b in bars]
        closes = [float(b['close']) for b in bars]
        volumes = [float(b['volume']) for b in bars]
    elif df is not None and len(df) > 0:
        opens = df['open'].astype('float64').values.tolist()
        highs = df['high'].astype('float64').values.tolist()
        lows = df['low'].astype('float64').values.tolist()
        closes = df['close'].astype('float64').values.tolist()
        volumes = df['volume'].astype('float64').values.tolist()
    else:
        return empty_result

    if not tsohlcv_data:
        return empty_result

    required_ts = sorted(set(int(item['T']) for item in tsohlcv_data))
    ts_map = {int(item['T']): item for item in tsohlcv_data}

    if not required_ts:
        return empty_result

    max_t = max(required_ts)
    n = len(opens)

    if n < max_t + 2:
        return empty_result

    matches = []

    for start in range(1, n - max_t):
        pre_close = closes[start - 1]
        pre_volume = volumes[start - 1]

        if pre_close <= 0:
            continue

        all_pass = True
        sim_sum = 0.0
        sim_count = 0

        for t in required_ts:
            idx = start + t
            if idx >= n:
                all_pass = False
                break

            tmpl = ts_map[t]
            o_pct = _safe_pct(opens[idx], pre_close)
            h_pct = _safe_pct(highs[idx], pre_close)
            l_pct = _safe_pct(lows[idx], pre_close)
            c_pct = _safe_pct(closes[idx], pre_close)
            v_pct = _safe_pct(volumes[idx], pre_volume)

            bar_sim = _bar_similarity(tmpl, o_pct, h_pct, l_pct, c_pct, v_pct)
            min_sim = float(tmpl['S'])

            if bar_sim < min_sim:
                all_pass = False
                break

            sim_sum += bar_sim
            sim_count += 1

        if all_pass and sim_count > 0:
            avg_sim = sim_sum / sim_count
            matches.append({
                'bar_index': start,
                'similarity': round(avg_sim, 2),
            })

    result = dict(empty_result)
    if matches:
        result['matched'] = True
        result['matches'] = matches
        result['similarity'] = round(
            sum(m['similarity'] for m in matches) / len(matches), 2
        )

    return result


# ================================================================
# 从最近K线提取模板 (自动模式)
# ================================================================

def extract_template_from_bars(bars: list, template_len: int = 3,
                               similarity_threshold: float = 85.0) -> list:
    """从最近的K线中提取 TSOHLCV 模板。

    以倒数第 template_len+1 根K线的收盘价作为基准，
    计算后续 template_len 根K线的 OHLCV 对数收益率。
    """
    if len(bars) < template_len + 2:
        return []

    # 基准: 倒数第 template_len+1 根的收盘价和成交量
    base_idx = len(bars) - template_len - 1
    pre_close = bars[base_idx]['close']
    pre_volume = bars[base_idx]['volume']

    if pre_close <= 0 or pre_volume <= 0:
        return []

    template = []
    for i in range(template_len):
        idx = base_idx + 1 + i
        bar = bars[idx]
        tmpl = {
            'T': i,
            'S': similarity_threshold,
            'O': round(_safe_pct(bar['open'], pre_close), 2),
            'H': round(_safe_pct(bar['high'], pre_close), 2),
            'L': round(_safe_pct(bar['low'], pre_close), 2),
            'C': round(_safe_pct(bar['close'], pre_close), 2),
            'V': round(_safe_pct(bar['volume'], pre_volume), 2),
        }
        template.append(tmpl)

    return template


# ================================================================
# 胜率统计引擎
# ================================================================

def compute_forward_returns(bars: list, match_bar_index: int,
                            hold_days: list = None) -> Optional[dict]:
    """计算匹配位置之后N天的收益率。

    Args:
        bars: K线数据
        match_bar_index: 匹配的起始 bar index (模板的 T=0 位置)
        hold_days: 要统计的持仓天数列表

    Returns:
        dict: {hold_day: return_pct, ...} 或 None
    """
    if hold_days is None:
        hold_days = [1, 2, 3, 5, 10, 20]

    entry_idx = match_bar_index  # T=0 位置
    if entry_idx >= len(bars):
        return None

    entry_close = bars[entry_idx]['close']
    if entry_close <= 0:
        return None

    # 也计算基于 T=0 后一天开盘价买入的收益
    next_open = None
    if entry_idx + 1 < len(bars):
        next_open = bars[entry_idx + 1]['open']

    results = {}
    peak = entry_close
    max_return = 0.0

    for d in hold_days:
        target_idx = entry_idx + d
        if target_idx >= len(bars):
            results[f'day_{d}'] = None
            continue

        close_price = bars[target_idx]['close']
        ret = (close_price / entry_close - 1) * 100
        results[f'day_{d}'] = round(ret, 2)

        # 更新峰值
        for j in range(entry_idx, min(target_idx + 1, len(bars))):
            if bars[j]['high'] > peak:
                peak = bars[j]['high']

    max_return = round((peak / entry_close - 1) * 100, 2)
    results['max_return'] = max_return

    # 基于次日开盘价的收益
    if next_open and next_open > 0:
        for d in hold_days:
            target_idx = entry_idx + d + 1  # 从次日开始算
            if target_idx >= len(bars):
                results[f'next_open_day_{d}'] = None
                continue
            close_price = bars[target_idx]['close']
            ret = (close_price / next_open - 1) * 100
            results[f'next_open_day_{d}'] = round(ret, 2)

    return results

def aggregate_stats(forward_returns_list: list, hold_days: list = None) -> dict:
    """汇总胜率统计。"""
    if hold_days is None:
        hold_days = [1, 2, 3, 5, 10, 20]

    stats = {}
    for d in hold_days:
        key = f'day_{d}'
        values = [fr[key] for fr in forward_returns_list if fr.get(key) is not None]
        if not values:
            stats[key] = {'count': 0}
            continue

        wins = sum(1 for v in values if v > 0)
        losses = sum(1 for v in values if v <= 0)
        avg_ret = sum(values) / len(values)
        win_values = [v for v in values if v > 0]
        loss_values = [v for v in values if v <= 0]

        avg_win = sum(win_values) / len(win_values) if win_values else 0
        avg_loss = sum(loss_values) / len(loss_values) if loss_values else 0
        profit_loss_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else float('inf')

        # 中位数
        sorted_vals = sorted(values)
        mid = len(sorted_vals) // 2
        median = (sorted_vals[mid] + sorted_vals[mid - 1]) / 2 if len(sorted_vals) % 2 == 0 else sorted_vals[mid]

        stats[key] = {
            'count': len(values),
            'win_rate': round(wins / len(values) * 100, 1),
            'avg_return': round(avg_ret, 2),
            'median_return': round(median, 2),
            'avg_win': round(avg_win, 2),
            'avg_loss': round(avg_loss, 2),
            'profit_loss_ratio': round(profit_loss_ratio, 2),
            'max_return': round(max(values), 2),
            'min_return': round(min(values), 2),
        }

    # max_return 汇总
    max_rets = [fr['max_return'] for fr in forward_returns_list if fr.get('max_return') is not None]
    if max_rets:
        stats['peak'] = {
            'count': len(max_rets),
            'avg_max_return': round(sum(max_rets) / len(max_rets), 2),
            'median_max_return': round(sorted(max_rets)[len(max_rets) // 2], 2),
        }

    # 次日开盘买入统计
    for d in hold_days:
        key = f'next_open_day_{d}'
        values = [fr[key] for fr in forward_returns_list if fr.get(key) is not None]
        if not values:
            continue
        wins = sum(1 for v in values if v > 0)
        win_values = [v for v in values if v > 0]
        loss_values = [v for v in values if v <= 0]
        avg_win = sum(win_values) / len(win_values) if win_values else 0
        avg_loss = sum(loss_values) / len(loss_values) if loss_values else 0

        stats[key] = {
            'count': len(values),
            'win_rate': round(wins / len(values) * 100, 1),
            'avg_return': round(sum(values) / len(values), 2),
            'avg_win': round(avg_win, 2),
            'avg_loss': round(avg_loss, 2),
            'profit_loss_ratio': round(abs(avg_win / avg_loss), 2) if avg_loss != 0 else float('inf'),
        }

    return stats


# ================================================================
# 板块判断
# ================================================================

def get_board_name(code):
    c = str(code)[:3]
    if c.startswith("68"): return "科创板"
    elif c.startswith("30"): return "创业板"
    elif c.startswith("6"): return "沪主板"
    elif c.startswith(("0", "2")): return "深主板"
    return "未知"


# ================================================================
# 统计输出
# ================================================================

def print_aggregate_stats(stats: dict, total_matches: int, total_stocks: int,
                          hold_days: list):
    """打印汇总统计表。"""
    print(f"\n{'=' * 80}")
    print(f"  模式匹配历史胜率统计")
    print(f"{'=' * 80}")
    print(f"  扫描股票: {total_stocks} 只 | 总匹配: {total_matches} 次")

    # ---- 收盘价买入统计 ----
    print(f"\n  ┌─────────────────────────────────────────────────────────────────────┐")
    print(f"  │ 收盘价买入 (T=0 收盘价作为入场价)                                    │")
    print(f"  ├──────┬──────┬────────┬────────┬────────┬────────┬────────┬─────────┤")
    print(f"  │ 持仓 │ 笔数 │ 胜率   │ 均收益 │ 中位数 │ 均盈   │ 均亏   │ 盈亏比  │")
    print(f"  ├──────┼──────┼────────┼────────┼────────┼────────┼────────┼─────────┤")

    for d in hold_days:
        key = f'day_{d}'
        s = stats.get(key, {})
        if s.get('count', 0) == 0:
            continue
        print(f"  │ {d:>3}天 │ {s['count']:>4} │ {s['win_rate']:>5.1f}% │ "
              f"{s['avg_return']:>+6.2f}% │ {s['median_return']:>+6.2f}% │ "
              f"{s['avg_win']:>+5.2f}% │ {s['avg_loss']:>+6.2f}% │ {s['profit_loss_ratio']:>7.2f} │")

    print(f"  └──────┴──────┴────────┴────────┴────────┴────────┴────────┴─────────┘")

    # 峰值统计
    peak = stats.get('peak', {})
    if peak.get('count', 0) > 0:
        print(f"\n  峰值收益 (持仓期间最高点): 均值 {peak['avg_max_return']:+.2f}% | "
              f"中位数 {peak['median_max_return']:+.2f}%")

    # ---- 次日开盘买入统计 ----
    has_next_open = any(f'next_open_day_{d}' in stats for d in hold_days)
    if has_next_open:
        print(f"\n  ┌─────────────────────────────────────────────────────────────────────┐")
        print(f"  │ 次日开盘买入 (T=0 次日开盘价作为入场价)                              │")
        print(f"  ├──────┬──────┬────────┬────────┬────────┬────────┬─────────┤")
        print(f"  │ 持仓 │ 笔数 │ 胜率   │ 均收益 │ 均盈   │ 均亏   │ 盈亏比  │")
        print(f"  ├──────┼──────┼────────┼────────┼────────┼────────┼─────────┤")

        for d in hold_days:
            key = f'next_open_day_{d}'
            s = stats.get(key, {})
            if s.get('count', 0) == 0:
                continue
            print(f"  │ {d:>3}天 │ {s['count']:>4} │ {s['win_rate']:>5.1f}% │ "
                  f"{s['avg_return']:>+6.2f}% │ {s['avg_win']:>+5.2f}% │ "
                  f"{s['avg_loss']:>+6.2f}% │ {s['profit_loss_ratio']:>7.2f} │")

        print(f"  └──────┴──────┴────────┴────────┴────────┴────────┴─────────┘")


def print_top_matches(matches_detail: list, top_n: int = 10, sort_by: str = 'day_5'):
    """打印最佳/最差匹配。"""
    # 按指定天数收益排序
    valid = [m for m in matches_detail if m['forward'].get(sort_by) is not None]
    if not valid:
        return

    print(f"\n  TOP {top_n} 盈利 (按 {sort_by} 收益):")
    for m in sorted(valid, key=lambda x: -x['forward'].get(sort_by, 0))[:top_n]:
        fr = m['forward']
        d1 = fr.get('day_1') or 0
        d3 = fr.get('day_3') or 0
        d5 = fr.get('day_5') or 0
        d10 = fr.get('day_10') or 0
        pk = fr.get('max_return') or 0
        print(f"    {m['code']:<8} {m['board']:<6} {m['date']:<12} "
              f"相似度{m['similarity']:>5.1f}% "
              f"| 1天{d1:>+6.2f}% 3天{d3:>+6.2f}% "
              f"5天{d5:>+6.2f}% 10天{d10:>+6.2f}% "
              f"| 峰值{pk:>+6.2f}%")

    print(f"\n  TOP {top_n} 亏损 (按 {sort_by} 收益):")
    for m in sorted(valid, key=lambda x: x['forward'].get(sort_by, 0))[:top_n]:
        fr = m['forward']
        d1 = fr.get('day_1') or 0
        d3 = fr.get('day_3') or 0
        d5 = fr.get('day_5') or 0
        d10 = fr.get('day_10') or 0
        pk = fr.get('max_return') or 0
        print(f"    {m['code']:<8} {m['board']:<6} {m['date']:<12} "
              f"相似度{m['similarity']:>5.1f}% "
              f"| 1天{d1:>+6.2f}% 3天{d3:>+6.2f}% "
              f"5天{d5:>+6.2f}% 10天{d10:>+6.2f}% "
              f"| 峰值{pk:>+6.2f}%")


# ================================================================
# 主函数
# ================================================================

def main():
    parser = argparse.ArgumentParser(
        description="TSOHLCV 模式匹配 — 历史全量扫描胜率估算",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
模板来源:
  --template-json: 直接传入JSON格式的模板数据
  --template-file: 从文件读取模板JSON
  --ref-code + --ref-days: 从指定股票最近K线自动提取模板
  --template-len: 自动提取的模板长度 (默认3)

示例:
  # 从某股票最近3根K线提取模板，扫描全市场
  python test_pattern_scan.py --ref-code 000001 --template-len 3 --source db

  # 直接传入模板
  python test_pattern_scan.py --template-json '[{"T":0,"S":90,"O":-5,"H":-2,"L":-8,"C":-3,"V":20},{"T":1,"S":90,"O":-3,"H":1,"L":-5,"C":0,"V":50}]'

  # 指定股票池 + 自定义持仓天数
  python test_pattern_scan.py --ref-code 000001 --codes 000001,000002,600000 --hold-days 3,5,10
""")
    parser.add_argument("--codes", default="", help="逗号分隔的股票代码")
    parser.add_argument("--source", choices=["manual", "db", "csv"], default="manual",
                        help="数据源: manual(默认), db(全市场basicinfo_db), csv(backtest_v3_trades.csv)")
    parser.add_argument("--filter-st", action="store_true", default=True)
    parser.add_argument("--no-filter-st", action="store_true")
    parser.add_argument("--days", type=int, default=500, help="加载K线天数 (默认500)")

    # 模板参数
    parser.add_argument("--template-json", type=str, default="",
                        help='TSOHLCV模板JSON字符串')
    parser.add_argument("--template-file", type=str, default="",
                        help='TSOHLCV模板JSON文件路径')
    parser.add_argument("--ref-code", type=str, default="",
                        help="参考股票代码 (从最近K线自动提取模板)")
    parser.add_argument("--template-len", type=int, default=3,
                        help="自动提取的模板K线根数 (默认3)")
    parser.add_argument("--similarity", type=float, default=85.0,
                        help="默认相似度阈值 (默认85)")

    # 回测参数
    parser.add_argument("--hold-days", type=str, default="1,3,5,10,20",
                        help="统计的持仓天数 (逗号分隔, 默认1,3,5,10,20)")
    parser.add_argument("--min-similarity", type=float, default=0.0,
                        help="输出匹配的最低平均相似度 (默认0)")
    parser.add_argument("--top", type=int, default=10, help="TOP N 输出")
    parser.add_argument("--all-matches", action="store_true",
                        help="输出全部匹配明细")
    parser.add_argument("--export", type=str, default="",
                        help="导出JSON文件路径")

    args = parser.parse_args()

    hold_days = [int(d.strip()) for d in args.hold_days.split(",") if d.strip()]

    # ================================================================
    # 1. 获取模板
    # ================================================================
    template = None

    if args.template_json:
        try:
            template = json.loads(args.template_json)
            print(f"  模板来源: 命令行JSON ({len(template)} 条)")
        except json.JSONDecodeError as e:
            print(f"  模板JSON解析失败: {e}")
            return

    elif args.template_file:
        try:
            with open(args.template_file, 'r', encoding='utf-8') as f:
                template = json.load(f)
            print(f"  模板来源: 文件 {args.template_file} ({len(template)} 条)")
        except Exception as e:
            print(f"  模板文件读取失败: {e}")
            return

    elif args.ref_code:
        print(f"  模板来源: 从 {args.ref_code} 最近 {args.template_len} 根K线提取")
        ref_bars = fetch_kline(args.ref_code, args.days)
        if not ref_bars:
            try:
                ref_bars = fetch_kline_db(args.ref_code, args.days)
            except Exception:
                pass
        if not ref_bars or len(ref_bars) < args.template_len + 2:
            print(f"  参考股票 {args.ref_code} 数据不足")
            return
        template = extract_template_from_bars(ref_bars, args.template_len, args.similarity)
        if not template:
            print(f"  模板提取失败")
            return

        # 打印模板详情
        base_idx = len(ref_bars) - args.template_len - 1
        print(f"\n  参考K线 ({args.ref_code}):")
        for i in range(args.template_len + 1):
            idx = base_idx + i
            bar = ref_bars[idx]
            label = "基准" if i == 0 else f"T={i - 1}"
            print(f"    {label}: {bar['time']}  O={bar['open']:.2f} H={bar['high']:.2f} "
                  f"L={bar['low']:.2f} C={bar['close']:.2f} V={bar['volume']:.0f}")

        print(f"\n  提取的模板:")
        for t in template:
            print(f"    T={t['T']} S={t['S']} O={t['O']:+.2f}% H={t['H']:+.2f}% "
                  f"L={t['L']:+.2f}% C={t['C']:+.2f}% V={t['V']:+.2f}%")

    else:
        print("  请指定模板来源: --template-json / --template-file / --ref-code")
        return

    # ================================================================
    # 2. 确定股票列表
    # ================================================================
    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
        use_db = False
        stock_source = "手动指定"
    elif args.source == "db":
        use_db = True
        filter_st = not args.no_filter_st
        print(f"\n  全市场扫描模式: 从 basicinfo_db 加载...")
        codes = get_all_codes_basicinfo(filter_st=filter_st)
        stock_source = f"basicinfo_db ({'排除ST' if filter_st else '含ST'})"
        print(f"   {stock_source}: {len(codes)} 只股票")
    elif args.source == "csv":
        use_db = False
        csv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_v3_trades.csv")
        codes = get_codes_from_csv(csv_path)
        stock_source = f"backtest_v3_trades.csv ({len(codes)} 只)"
        print(f"\n  CSV扫描模式: {stock_source}")
    else:
        # 默认: 如果有 ref-code 就只扫那只，否则用 db
        if args.ref_code:
            codes = [args.ref_code]
            use_db = False
            stock_source = "参考股票"
        else:
            use_db = True
            codes = get_all_codes_basicinfo(filter_st=True)
            stock_source = "basicinfo_db (排除ST)"

    # ================================================================
    # 3. 全量扫描
    # ================================================================
    print(f"\n{'=' * 80}")
    print(f"  开始扫描: {len(codes)} 只股票")
    print(f"  模板长度: {len(template)} 根K线 | 相似度阈值: {args.similarity}%")
    print(f"  统计持仓: {hold_days} 天")
    print(f"{'=' * 80}\n")

    all_matches = []       # 所有匹配明细
    all_forward_returns = []  # 所有前向收益
    total_stocks = 0
    name_map = {}

    if use_db:
        try:
            name_map = get_stock_name_map()
        except Exception:
            pass

    for i, code in enumerate(codes):
        bars = fetch_kline_db(code, args.days) if use_db else fetch_kline(code, args.days)
        if not bars or len(bars) < 10:
            continue

        total_stocks += 1

        result = match_tsohlcvs(
            tsohlcv_data=template,
            symbol=code,
            bars=bars,
        )

        if not result['matched']:
            if (i + 1) % 500 == 0:
                print(f"  已扫描 {i + 1}/{len(codes)} ... (已匹配 {len(all_matches)} 次)")
            continue

        # 计算每个匹配点的前向收益
        for m in result['matches']:
            bar_idx = m['bar_index']
            if bar_idx >= len(bars):
                continue

            forward = compute_forward_returns(bars, bar_idx, hold_days)
            if forward is None:
                continue

            # 过滤: 匹配日期不能太近 (需要有足够后续数据)
            min_data_days = min(hold_days)
            if bar_idx + min_data_days >= len(bars):
                continue

            sname = name_map.get(code, "")
            match_detail = {
                'code': code,
                'name': sname,
                'board': get_board_name(code),
                'date': bars[bar_idx]['time'],
                'bar_index': bar_idx,
                'similarity': m['similarity'],
                'forward': forward,
            }

            all_matches.append(match_detail)
            all_forward_returns.append(forward)

        if (i + 1) % 100 == 0:
            print(f"  已扫描 {i + 1}/{len(codes)} ... (已匹配 {len(all_matches)} 次)")

    # ================================================================
    # 4. 汇总统计
    # ================================================================
    print(f"\n扫描完成: {total_stocks} 只股票, {len(all_matches)} 次匹配")

    if not all_forward_returns:
        print("  无匹配结果。")
        return

    # 过滤低相似度
    if args.min_similarity > 0:
        filtered = [(m, fr) for m, fr in zip(all_matches, all_forward_returns)
                    if m['similarity'] >= args.min_similarity]
        if filtered:
            all_matches_filtered, all_forward_returns_filtered = zip(*filtered)
            all_matches = list(all_matches_filtered)
            all_forward_returns = list(all_forward_returns_filtered)
            print(f"  过滤相似度>={args.min_similarity}%: 剩余 {len(all_matches)} 次匹配")

    stats = aggregate_stats(all_forward_returns, hold_days)
    print_aggregate_stats(stats, len(all_matches), total_stocks, hold_days)

    # 按板块分组统计
    boards = {}
    for m, fr in zip(all_matches, all_forward_returns):
        board = m['board']
        if board not in boards:
            boards[board] = []
        boards[board].append(fr)

    if len(boards) > 1:
        print(f"\n  --- 分板块统计 ---")
        for board, frs in sorted(boards.items()):
            board_stats = aggregate_stats(frs, hold_days)
            day5 = board_stats.get('day_5', {})
            day10 = board_stats.get('day_10', {})
            if day5.get('count', 0) > 0:
                print(f"  {board:<6} {len(frs):>4}笔 | "
                      f"5天: 胜率{day5['win_rate']:>5.1f}% 均收益{day5['avg_return']:>+6.2f}% | "
                      f"10天: 胜率{day10.get('win_rate', 0):>5.1f}% 均收益{day10.get('avg_return', 0):>+6.2f}%")

    # 按相似度区间统计
    sim_ranges = [(90, 100), (85, 90), (80, 85), (70, 80), (0, 70)]
    has_range_data = False
    for sim_min, sim_max in sim_ranges:
        frs = [fr for m, fr in zip(all_matches, all_forward_returns)
               if sim_min <= m['similarity'] < sim_max]
        if frs:
            if not has_range_data:
                print(f"\n  --- 按相似度区间统计 ---")
                has_range_data = True
            rs = aggregate_stats(frs, hold_days)
            day5 = rs.get('day_5', {})
            print(f"  [{sim_min:>3}%,{sim_max:>3}%) {len(frs):>4}笔 | "
                  f"5天: 胜率{day5.get('win_rate', 0):>5.1f}% 均收益{day5.get('avg_return', 0):>+6.2f}%")

    # TOP N
    print_top_matches(all_matches, args.top, sort_by=f'day_{hold_days[0]}')

    # 全部明细
    if args.all_matches:
        print(f"\n  全部匹配明细 ({len(all_matches)} 条):")
        for m in sorted(all_matches, key=lambda x: -x['similarity']):
            fr = m['forward']
            day_strs = []
            for d in hold_days:
                v = fr.get(f'day_{d}')
                if v is not None:
                    day_strs.append(f"{d}天{v:>+.2f}%")
            pk = fr.get('max_return') or 0
            print(f"    {m['code']:<8} {m['board']:<6} {m['date']:<12} "
                  f"相似度{m['similarity']:>5.1f}% | {' '.join(day_strs)} "
                  f"| 峰值{pk:>+6.2f}%")

    # ================================================================
    # 5. 导出
    # ================================================================
    export_path = args.export or "test_pattern_scan_result.json"
    export_data = {
        'template': template,
        'config': {
            'similarity_threshold': args.similarity,
            'hold_days': hold_days,
            'total_stocks': total_stocks,
            'total_matches': len(all_matches),
        },
        'stats': stats,
        'matches': all_matches,
    }
    with open(export_path, 'w', encoding='utf-8') as f:
        json.dump(export_data, f, ensure_ascii=False, indent=2)
    print(f"\n  导出: {export_path}")


if __name__ == "__main__":
    main()

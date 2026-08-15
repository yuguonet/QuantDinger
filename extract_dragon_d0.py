#!/usr/bin/env python3
"""游资D0关键点位数据提取器

逻辑:
  1. 从龙虎榜数据库中，按股票聚合游资买入记录
  2. 每只股票取最近20个交易日内，游资/庄家第一次大量买入日作为D0
  3. 提取D0前5天 + D0后10天的K线数据
  4. 输出JSON供分析

用法:
  python extract_dragon_d0.py                    # 默认最近20天，输出到 stdout
  python extract_dragon_d0.py --days 20          # 指定天数窗口
  python extract_dragon_d0.py --out d0_data.json # 输出到文件
  python extract_dragon_d0.py --min-net 5000     # 最小净买入额(万)
  python extract_dragon_d0.py --code 000001      # 只看某只股票
"""
from __future__ import annotations
import json, argparse, os, sys
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Any, Dict, List, Optional

# ================================================================
# 环境初始化
# ================================================================
_backend_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend_api_python")
if _backend_root not in sys.path:
    sys.path.insert(0, _backend_root)

def _load_env():
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
# 数据库
# ================================================================
_writer_cache = None
def _get_writer():
    global _writer_cache
    if _writer_cache is not None:
        return _writer_cache
    from app.utils.db_market import get_market_kline_writer
    _writer_cache = get_market_kline_writer()
    return _writer_cache

_pool_cache = None
def _get_cnstock_pool():
    global _pool_cache
    if _pool_cache is not None:
        return _pool_cache
    from app.utils.db_market import get_market_db_manager
    mgr = get_market_db_manager()
    _pool_cache = mgr._get_pool("CNStock")
    return _pool_cache

# ================================================================
# 数据加载
# ================================================================

def fetch_dragon_tiger_from_db(limit: int = 10000) -> List[Dict]:
    """从数据库加载龙虎榜数据"""
    try:
        pool = _get_cnstock_pool()
        with pool.connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT trade_date, stock_code, stock_name, reason, "
                "buy_amount, sell_amount, net_amount, change_percent, "
                "close_price, turnover_rate, amount, buy_seat_count, sell_seat_count "
                "FROM cnd_dragon_tiger_list ORDER BY trade_date DESC LIMIT %s",
                (limit,)
            )
            columns = [desc[0] for desc in cur.description]
            rows = cur.fetchall()
            cur.close()
            return [dict(zip(columns, row)) for row in rows]
    except Exception as e:
        print(f"  [DB] 龙虎榜查询失败: {e}", file=sys.stderr)
        return []


def fetch_kline_db(code: str, days: int = 300) -> List[Dict]:
    """从数据库加载K线(前复权)"""
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
    except Exception as e:
        print(f"  [DB] K线加载失败 {code}: {e}", file=sys.stderr)
        return []


# ================================================================
# 辅助函数
# ================================================================

def get_board_name(code: str) -> str:
    c = str(code)[:3]
    if c.startswith("68"): return "科创板"
    elif c.startswith("30"): return "创业板"
    elif c.startswith("6"): return "沪主板"
    elif c.startswith(("0", "2")): return "深主板"
    return "未知"


def calc_vwap(bar: Dict) -> float:
    """用 (O+H+L+C)/4 近似VWAP（日内分时VWAP需要分钟数据，这里用典型价代替）"""
    return round((bar['open'] + bar['high'] + bar['low'] + bar['close']) / 4, 3)


def calc_change_pct(cur: float, prev: float) -> float:
    if prev <= 0:
        return 0
    return round((cur / prev - 1) * 100, 2)


# ================================================================
# 核心: 找D0 + 提取窗口
# ================================================================

def find_d0_per_stock(
    dragon_data: List[Dict],
    window_days: int = 20,
    min_net_amount: float = 0,
) -> Dict[str, Dict]:
    """按股票聚合龙虎榜，找每只股票在最近window_days内的首次大量买入日作为D0

    返回: {stock_code: {d0_date, d0_info, all_records}}
    """
    # 按股票聚合
    by_code: Dict[str, List[Dict]] = defaultdict(list)
    for row in dragon_data:
        code = row.get('stock_code', '')
        if code:
            by_code[code].append(row)

    # 找最近交易日（以龙虎榜最新日期为基准）
    all_dates = sorted(set(r.get('trade_date', '') for r in dragon_data), reverse=True)
    if not all_dates:
        return {}
    latest_date = all_dates[0]

    # 计算窗口起始日期（往前推 window_days 个自然日，粗略）
    try:
        cutoff = (datetime.strptime(latest_date, "%Y-%m-%d") - timedelta(days=window_days * 1.5)).strftime("%Y-%m-%d")
    except Exception:
        cutoff = ""

    result = {}

    for code, rows in by_code.items():
        # 按日期排序
        rows.sort(key=lambda x: x.get('trade_date', ''))

        # 过滤: 净买入额 >= min_net_amount
        if min_net_amount > 0:
            qualified = [r for r in rows
                         if float(r.get('net_amount', 0) or 0) / 10000 >= min_net_amount
                         and r.get('trade_date', '') >= cutoff]
        else:
            qualified = [r for r in rows if r.get('trade_date', '') >= cutoff]

        if not qualified:
            continue

        # D0 = 第一次大量买入日（列表中最早的一条，在窗口内）
        d0_row = qualified[0]
        d0_date = d0_row.get('trade_date', '')

        result[code] = {
            "d0_date": d0_date,
            "stock_name": d0_row.get('stock_name', ''),
            "d0_info": {
                "reason": d0_row.get('reason', ''),
                "change_percent": float(d0_row.get('change_percent', 0) or 0),
                "net_amount_wan": round(float(d0_row.get('net_amount', 0) or 0) / 10000, 2),
                "buy_amount_wan": round(float(d0_row.get('buy_amount', 0) or 0) / 10000, 2),
                "sell_amount_wan": round(float(d0_row.get('sell_amount', 0) or 0) / 10000, 2),
                "close_price": float(d0_row.get('close_price', 0) or 0),
                "turnover_rate": float(d0_row.get('turnover_rate', 0) or 0),
                "buy_seat_count": int(d0_row.get('buy_seat_count', 0) or 0),
                "sell_seat_count": int(d0_row.get('sell_seat_count', 0) or 0),
            },
            # 窗口内所有龙虎榜记录（可能D0当天有多条，或前后也有）
            "all_records_in_window": [
                {
                    "trade_date": r.get('trade_date', ''),
                    "reason": r.get('reason', ''),
                    "change_percent": float(r.get('change_percent', 0) or 0),
                    "net_amount_wan": round(float(r.get('net_amount', 0) or 0) / 10000, 2),
                    "buy_amount_wan": round(float(r.get('buy_amount', 0) or 0) / 10000, 2),
                }
                for r in rows if r.get('trade_date', '') >= cutoff
            ],
        }

    return result


def extract_kline_window(
    bars: List[Dict],
    d0_date: str,
    before: int = 5,
    after: int = 10,
) -> Optional[Dict]:
    """提取D0前before天 + D0当天 + D0后after天的K线数据

    返回: {
        "d0_index": int,
        "d0_date": str,
        "klines_before": [...],
        "kline_d0": {...},
        "klines_after": [...],
        "key_levels": {...}
    }
    """
    # 找D0在K线中的索引
    d0_idx = None
    for i, b in enumerate(bars):
        if b['time'] == d0_date:
            d0_idx = i
            break

    if d0_idx is None:
        # D0日期不在K线中（可能停牌或非交易日），找最近的
        for i, b in enumerate(bars):
            if b['time'] >= d0_date:
                d0_idx = i
                break

    if d0_idx is None or d0_idx < 1:
        return None

    d0_bar = bars[d0_idx]
    prev_close = bars[d0_idx - 1]['close'] if d0_idx > 0 else d0_bar['open']

    # 前before天
    klines_before = []
    for i in range(max(0, d0_idx - before), d0_idx):
        b = bars[i]
        klines_before.append({
            "offset": i - d0_idx,
            "time": b['time'],
            "open": b['open'],
            "high": b['high'],
            "low": b['low'],
            "close": b['close'],
            "volume": int(b['volume']),
            "change_pct": calc_change_pct(b['close'], bars[i-1]['close']) if i > 0 else 0,
        })

    # D0当天
    kline_d0 = {
        "offset": 0,
        "time": d0_bar['time'],
        "open": d0_bar['open'],
        "high": d0_bar['high'],
        "low": d0_bar['low'],
        "close": d0_bar['close'],
        "volume": int(d0_bar['volume']),
        "change_pct": calc_change_pct(d0_bar['close'], prev_close),
        "vwap": calc_vwap(d0_bar),
        "amplitude": round((d0_bar['high'] - d0_bar['low']) / prev_close * 100, 2) if prev_close > 0 else 0,
        "upper_shadow_pct": round((d0_bar['high'] - max(d0_bar['open'], d0_bar['close'])) / prev_close * 100, 2) if prev_close > 0 else 0,
        "lower_shadow_pct": round((min(d0_bar['open'], d0_bar['close']) - d0_bar['low']) / prev_close * 100, 2) if prev_close > 0 else 0,
    }

    # 后after天
    klines_after = []
    for i in range(d0_idx + 1, min(len(bars), d0_idx + after + 1)):
        b = bars[i]
        klines_after.append({
            "offset": i - d0_idx,
            "time": b['time'],
            "open": b['open'],
            "high": b['high'],
            "low": b['low'],
            "close": b['close'],
            "volume": int(b['volume']),
            "change_pct": calc_change_pct(b['close'], bars[i-1]['close']),
        })

    # 关键点位
    pre_bars = bars[max(0, d0_idx - before):d0_idx]
    post_bars = bars[d0_idx + 1: min(len(bars), d0_idx + after + 1)]

    key_levels = {
        "d0_high": d0_bar['high'],
        "d0_low": d0_bar['low'],
        "d0_close": d0_bar['close'],
        "d0_open": d0_bar['open'],
        "d0_vwap": calc_vwap(d0_bar),
        "d0_volume": int(d0_bar['volume']),
        "prev_close": prev_close,
        # 前5天的高低点（区间）
        "pre5_high": max(b['high'] for b in pre_bars) if pre_bars else d0_bar['high'],
        "pre5_low": min(b['low'] for b in pre_bars) if pre_bars else d0_bar['low'],
        # 后10天的高低点
        "post10_high": max(b['high'] for b in post_bars) if post_bars else 0,
        "post10_low": min(b['low'] for b in post_bars) if post_bars else 0,
        # 后10天最高/最低点相对D0的幅度
        "post10_max_gain_pct": 0,
        "post10_max_loss_pct": 0,
    }
    if post_bars and d0_bar['close'] > 0:
        key_levels["post10_max_gain_pct"] = round((key_levels["post10_high"] / d0_bar['close'] - 1) * 100, 2)
        key_levels["post10_max_loss_pct"] = round((key_levels["post10_low"] / d0_bar['close'] - 1) * 100, 2)

    return {
        "d0_index": d0_idx,
        "d0_date": d0_date,
        "klines_before": klines_before,
        "kline_d0": kline_d0,
        "klines_after": klines_after,
        "key_levels": key_levels,
    }


# ================================================================
# 主函数
# ================================================================

def extract_all(
    window_days: int = 20,
    min_net_amount: float = 0,
    before: int = 5,
    after: int = 10,
    code_filter: str = "",
) -> List[Dict]:
    """提取所有股票的D0数据"""
    print(f"📊 加载龙虎榜数据 (窗口={window_days}天, 最小净买入={min_net_amount}万)...", file=sys.stderr)
    dragon_data = fetch_dragon_tiger_from_db()
    print(f"  龙虎榜: {len(dragon_data)}条", file=sys.stderr)

    if not dragon_data:
        return []

    # 找D0
    d0_map = find_d0_per_stock(dragon_data, window_days, min_net_amount)
    print(f"  符合条件的股票: {len(d0_map)}只", file=sys.stderr)

    if code_filter:
        d0_map = {k: v for k, v in d0_map.items() if k == code_filter}
        print(f"  过滤后: {len(d0_map)}只", file=sys.stderr)

    # K线缓存
    kline_cache: Dict[str, List[Dict]] = {}
    results = []

    for code, info in sorted(d0_map.items(), key=lambda x: x[1]['d0_date'], reverse=True):
        # 加载K线
        if code not in kline_cache:
            bars = fetch_kline_db(code, 300)
            if bars:
                kline_cache[code] = bars
            else:
                print(f"  ⚠️ {code} 无K线数据，跳过", file=sys.stderr)
                continue

        bars = kline_cache[code]

        # 提取窗口
        window = extract_kline_window(bars, info['d0_date'], before, after)
        if not window:
            print(f"  ⚠️ {code} D0={info['d0_date']} 不在K线范围内，跳过", file=sys.stderr)
            continue

        # 组装结果
        record = {
            "stock_code": code,
            "stock_name": info['stock_name'],
            "board": get_board_name(code),
            "d0_date": info['d0_date'],
            "d0_info": info['d0_info'],
            "window": window,
            "all_dragon_records": info['all_records_in_window'],
        }
        results.append(record)

        print(f"  ✅ {code} {info['stock_name']} D0={info['d0_date']} "
              f"净买入{info['d0_info']['net_amount_wan']:.0f}万 "
              f"后10天最高{window['key_levels']['post10_max_gain_pct']:+.1f}%"
              f" / 最低{window['key_levels']['post10_max_loss_pct']:+.1f}%",
              file=sys.stderr)

    return results


def main():
    parser = argparse.ArgumentParser(description="游资D0关键点位数据提取器")
    parser.add_argument("--days", type=int, default=20, help="D0搜索窗口(交易日)")
    parser.add_argument("--before", type=int, default=5, help="D0前取几天K线")
    parser.add_argument("--after", type=int, default=10, help="D0后取几天K线")
    parser.add_argument("--min-net", type=float, default=0, help="最小净买入额(万元)")
    parser.add_argument("--code", type=str, default="", help="只提取某只股票")
    parser.add_argument("--out", type=str, default="", help="输出JSON文件路径")
    args = parser.parse_args()

    results = extract_all(
        window_days=args.days,
        min_net_amount=args.min_net,
        before=args.before,
        after=args.after,
        code_filter=args.code,
    )

    if not results:
        print("❌ 无数据", file=sys.stderr)
        return

    # 输出
    output = {
        "meta": {
            "extract_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "window_days": args.days,
            "before": args.before,
            "after": args.after,
            "min_net_amount_wan": args.min_net,
            "total_stocks": len(results),
        },
        "data": results,
    }

    json_str = json.dumps(output, ensure_ascii=False, indent=2)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(json_str)
        print(f"\n💾 已保存到 {args.out} ({len(results)}只股票)", file=sys.stderr)
    else:
        print(json_str)

    # 摘要
    print(f"\n{'=' * 60}", file=sys.stderr)
    print(f"📊 提取完成: {len(results)}只股票", file=sys.stderr)
    print(f"{'=' * 60}", file=sys.stderr)
    for r in results:
        w = r['window']
        kl = w['key_levels']
        print(f"  {r['stock_code']} {r['stock_name']:>6} "
              f"D0={r['d0_date']} 净买入{r['d0_info']['net_amount_wan']:>8.0f}万 "
              f"| 后10天: 最高{kl['post10_max_gain_pct']:>+6.1f}% 最低{kl['post10_max_loss_pct']:>+6.1f}%",
              file=sys.stderr)


if __name__ == "__main__":
    main()

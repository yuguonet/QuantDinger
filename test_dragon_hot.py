#!/usr/bin/env python3
"""龙虎榜涨停强势策略 v5

核心逻辑:
  入场: D0涨停 + 技术分>95 + D1跳空高开(>5%) + 排除机构2~3家买入
  出场:
    1. D1日内动量 < -3%% → D2开盘清仓 (高开低走止损)
    2. D2~D7 每天检查:
       主板: 涨幅<10%% → 当天收盘清仓
       创科板: 涨幅<3%% OR 日内动量<-3%% → 当天收盘清仓
    3. 持仓上限7天 (兜底)

时间线:
  D0 涨停 (盘后确认信号)
  D1 买入日 (开盘买入)
  D2~D7 持有期 (每天检查涨幅/日内动量)

数据验证:
  排除机构2~3家买入: 日均+0.891%% (全部+0.605%%, 提升47%%)
  主板: D2涨停率24%%, 涨幅<10%%清仓 日均+1.111%%
  创科板: 涨幅<3%% OR 日内<-3%%清仓 日均+0.699%%
"""
from __future__ import annotations
import json, time, argparse, os, sys, re
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
        for p in [os.path.join(_backend_root, '.env'), os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')]:
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

def fetch_dragon_tiger_from_db(days: int = 30) -> List[Dict]:
    try:
        pool = _get_cnstock_pool()
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        with pool.connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT trade_date, stock_code, stock_name, reason, "
                "buy_amount, sell_amount, net_amount, change_percent, "
                "close_price, turnover_rate, amount, buy_seat_count, sell_seat_count "
                "FROM cnd_dragon_tiger_list WHERE trade_date >= %s ORDER BY trade_date DESC",
                (cutoff,)
            )
            columns = [desc[0] for desc in cur.description]
            rows = cur.fetchall()
            cur.close()
            return [dict(zip(columns, row)) for row in rows]
    except Exception as e:
        print(f"  [DB] 龙虎榜查询失败: {e}")
        return []


def fetch_kline_db(code: str, days: int = 300) -> List[Dict]:
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


def get_board_type(code: str) -> str:
    c = str(code)[:3]
    return "gem_star" if c.startswith("30") or c.startswith("68") else "main"


def get_board_name(code: str) -> str:
    c = str(code)[:3]
    if c.startswith("68"): return "科创板"
    elif c.startswith("30"): return "创业板"
    elif c.startswith("6"): return "沪主板"
    elif c.startswith(("0", "2")): return "深主板"
    return "未知"


def is_limit_up(close: float, prev_close: float, board_type: str) -> bool:
    threshold = 0.098 if board_type == "main" else 0.198
    if prev_close <= 0:
        return False
    return (close / prev_close - 1) >= threshold * 0.98


# ================================================================
# 技术指标
# ================================================================

def calc_ma(closes: List[float], period: int) -> Optional[float]:
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def calc_rsi(closes: List[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0
    return 100 - 100 / (1 + avg_g / avg_l)


def calc_obv_trend(bars: List[Dict], period: int = 5) -> str:
    if len(bars) < period + 1:
        return "平"
    obv = 0
    obv_list = [0]
    for i in range(1, len(bars)):
        if bars[i]['close'] > bars[i-1]['close']:
            obv += bars[i]['volume']
        elif bars[i]['close'] < bars[i-1]['close']:
            obv -= bars[i]['volume']
        obv_list.append(obv)
    change = obv_list[-1] - obv_list[-period]
    if change > 0:
        return "上升"
    elif change < 0:
        return "下降"
    return "平"


def calc_vol_ratio(bars: List[Dict], idx: int, period: int = 5) -> float:
    if idx < period or period <= 0:
        return 1.0
    avg_vol = sum(bars[i]['volume'] for i in range(idx - period, idx)) / period
    if avg_vol <= 0:
        return 1.0
    return bars[idx]['volume'] / avg_vol


def analyze_tech(bars: List[Dict], idx: int) -> Dict:
    if idx < 20 or idx >= len(bars):
        return {"tech_score": 0}

    closes = [bars[i]['close'] for i in range(max(0, idx - 60), idx + 1)]
    bar = bars[idx]
    prev_bar = bars[idx - 1] if idx > 0 else None

    ma5 = calc_ma(closes[-5:], 5)
    ma10 = calc_ma(closes[-10:], 10)
    ma20 = calc_ma(closes[-20:], 20)
    rsi14 = calc_rsi(closes, 14)
    obv_trend = calc_obv_trend(bars[:idx + 1])
    vol_ratio = calc_vol_ratio(bars, idx)

    change_pct = 0
    if prev_bar and prev_bar['close'] > 0:
        change_pct = (bar['close'] / prev_bar['close'] - 1) * 100

    ma_align = "交叉"
    if ma5 and ma10 and ma20:
        if ma5 > ma10 > ma20:
            ma_align = "多头排列"
        elif ma5 < ma10 < ma20:
            ma_align = "空头排列"

    score = 50
    if ma5 and ma10 and ma5 > ma10:
        score += 10
    if ma10 and ma20 and ma10 > ma20:
        score += 10
    if ma_align == "多头排列":
        score += 10
    elif ma_align == "空头排列":
        score -= 15
    if rsi14:
        if 40 <= rsi14 <= 60:
            score += 5
        elif rsi14 > 70:
            score -= 10
        elif rsi14 < 30:
            score += 10
    if obv_trend == "上升":
        score += 10
    elif obv_trend == "下降":
        score -= 10
    if 1.0 <= vol_ratio <= 2.0:
        score += 5
    elif vol_ratio > 3.0:
        score -= 5

    return {
        "ma5": round(ma5, 2) if ma5 else 0,
        "ma10": round(ma10, 2) if ma10 else 0,
        "ma20": round(ma20, 2) if ma20 else 0,
        "ma_align": ma_align,
        "rsi14": round(rsi14, 1) if rsi14 else 0,
        "obv_trend": obv_trend,
        "vol_ratio": round(vol_ratio, 2),
        "change_pct": round(change_pct, 2),
        "tech_score": max(0, min(100, score)),
    }


# ================================================================
# 窗口数据
# ================================================================

def extract_window(bars: List[Dict], center_idx: int, before: int = 5, after: int = 10) -> List[Dict]:
    start = max(0, center_idx - before)
    end = min(len(bars), center_idx + after + 1)
    window = []
    for i in range(start, end):
        b = bars[i]
        window.append({
            "time": b['time'],
            "open": round(b['open'], 3),
            "high": round(b['high'], 3),
            "low": round(b['low'], 3),
            "close": round(b['close'], 3),
            "volume": int(b['volume']),
            "offset": i - center_idx,
        })
    return window


# ================================================================
# 回测引擎 (D1买入, 连板出场)
# ================================================================

def _is_limit_up(close, prev_close, limit_pct):
    """判断是否涨停"""
    if prev_close <= 0:
        return False
    return (close / prev_close - 1) * 100 >= limit_pct * 0.95


def run_backtest_v4(bars: List[Dict], d0_idx: int, buy_price: float,
                    d1_intraday: float,
                    hold_days: int = 7, stop_loss: float = -8.0,
                    trailing_stop: float = -5.0, take_profit: float = 100.0,
                    min_intraday: float = 0.0,
                    board_type: str = 'main') -> Optional[Dict]:
    """v5回测: D1买入, 涨幅/日内动量出场

    出场规则:
      1. D1日内动量 < -3%% → D2开盘清仓 (高开低走止损)
      2. D2~D7 每天检查:
         主板: 涨幅<10%% → 清仓
         创科板: 涨幅<3%% OR 日内<-3%% → 清仓
      3. 持仓上限7天 (兜底)

    Args:
        d0_idx: D0(涨停日)索引
        buy_price: D1开盘价
        d1_intraday: D1日内动量 = (D1收盘-D1开盘)/D1开盘*100
    """
    d1_idx = d0_idx + 1
    if buy_price <= 0 or d1_idx >= len(bars):
        return None

    d1 = bars[d1_idx]
    d1_change = (d1['close'] / buy_price - 1) * 100
    peak = max(buy_price, d1['high'])

    limit_pct = 20.0 if board_type == 'gem_star' else 10.0

    # === 规则1: D1日内动量 < -3%% → D2开盘清仓 ===
    if d1_intraday < -3:
        if d1_idx + 1 < len(bars):
            d2 = bars[d1_idx + 1]
            exit_p = d2['open']
            exit_d = 2
            exit_reason = "高开低走(D1日内{:.1f}%)".format(d1_intraday)
        else:
            exit_p = d1['close']
            exit_d = 1
            exit_reason = "持仓到期"
        return_pct = (exit_p / buy_price - 1) * 100
        peak_pct = (peak / buy_price - 1) * 100
        return {
            'exit_price': round(exit_p, 3),
            'exit_day': exit_d,
            'exit_reason': exit_reason,
            'return_pct': round(return_pct, 2),
            'peak_return_pct': round(peak_pct, 2),
            'drawdown': round(peak_pct - return_pct, 2),
            'd1_change': round(d1_change, 2),
            'd1_intraday': round(d1_intraday, 2),
            'daily_returns': [round(d1_change, 2)],
            'ohlcv_window': extract_window(bars, d1_idx, before=5, after=10),
        }

    # === 规则2: D2~D7 任一天未涨停 → 当天收盘清仓 ===
    exit_p = d1['close']
    exit_d = 1
    exit_reason = "持仓到期"
    daily_returns = [round(d1_change, 2)]

    for d in range(2, hold_days + 2):
        idx = d1_idx + d - 1
        if idx >= len(bars):
            break
        b = bars[idx]
        prev_b = bars[idx - 1]
        if b['high'] > peak:
            peak = b['high']

        daily_ret = (b['close'] / buy_price - 1) * 100
        daily_returns.append(round(daily_ret, 2))

        # 硬止损 (兜底)
        if b['low'] <= buy_price * (1 + stop_loss / 100):
            exit_p = buy_price * (1 + stop_loss / 100)
            exit_d = d
            exit_reason = "止损"
            break

        # 未达标 → 清仓
        # 主板: 涨幅<10% (涨停阈值)
        # 创科板: 涨幅<3% OR 日内动量<-3% (20%涨停太难连续)
        if limit_pct >= 15.0:
            # 创科板: 涨幅<3% OR 日内<-3%
            intraday = (b['close'] - b['open']) / b['open'] * 100 if b['open'] > 0 else 0
            chg = (b['close'] / prev_b['close'] - 1) * 100 if prev_b['close'] > 0 else 0
            if chg < 3 or intraday < -3:
                exit_p = b['close']
                exit_d = d
                exit_reason = "D{}涨幅{:+.1f}%日内{:+.1f}%".format(d, chg, intraday)
                break
        else:
            # 主板: 涨幅<10%
            if not _is_limit_up(b['close'], prev_b['close'], limit_pct):
                exit_p = b['close']
                exit_d = d
                exit_reason = "D{}涨幅不足".format(d)
                break

        exit_p = b['close']
        exit_d = d

    return_pct = (exit_p / buy_price - 1) * 100
    peak_pct = (peak / buy_price - 1) * 100

    return {
        'exit_price': round(exit_p, 3),
        'exit_day': exit_d,
        'exit_reason': exit_reason,
        'return_pct': round(return_pct, 2),
        'peak_return_pct': round(peak_pct, 2),
        'drawdown': round(peak_pct - return_pct, 2),
        'd1_change': round(d1_change, 2),
        'd1_intraday': round(d1_intraday, 2),
        'daily_returns': daily_returns,
        'ohlcv_window': extract_window(bars, d1_idx, before=5, after=10),
    }


# ================================================================
# 统计
# ================================================================

def calc_stats(trades: List[Dict]) -> Dict:
    if not trades:
        return {}
    total = len(trades)
    wins = [t for t in trades if t['return_pct'] > 0]
    losses = [t for t in trades if t['return_pct'] <= 0]
    win_rate = len(wins) / total * 100
    avg_ret = sum(t['return_pct'] for t in trades) / total
    avg_win = sum(t['return_pct'] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t['return_pct'] for t in losses) / len(losses) if losses else 0
    plr = abs(avg_win / avg_loss) if avg_loss != 0 else float('inf')

    exit_reasons = defaultdict(int)
    for t in trades:
        exit_reasons[t.get('exit_reason', '未知')] += 1

    return {
        'total': total, 'wins': len(wins), 'losses': len(losses),
        'win_rate': round(win_rate, 1),
        'avg_return': round(avg_ret, 2),
        'avg_win': round(avg_win, 2), 'avg_loss': round(avg_loss, 2),
        'plr': round(plr, 2),
        'avg_hold': round(sum(t['exit_day'] for t in trades) / total, 1),
        'max_return': round(max(t['return_pct'] for t in trades), 2),
        'min_return': round(min(t['return_pct'] for t in trades), 2),
        'exit_reasons': dict(exit_reasons),
    }


def print_stats(stats: Dict, label: str):
    if not stats:
        print(f"  {label}: 无数据")
        return
    print(f"  {label}: {stats['total']}笔")
    print(f"    胜率: {stats['win_rate']}% | 盈亏比: {stats['plr']}")
    print(f"    均收益: {stats['avg_return']:+.2f}% | 均盈利: {stats['avg_win']:+.2f}% | 均亏损: {stats['avg_loss']:+.2f}%")
    print(f"    最大: {stats['max_return']:+.2f}% | 最小: {stats['min_return']:+.2f}%")
    print(f"    均持仓: {stats['avg_hold']:.1f}天")
    if stats.get('exit_reasons'):
        print(f"    出场: {' | '.join(f'{k}:{v}' for k, v in stats['exit_reasons'].items())}")


# ================================================================
# 策略
# ================================================================

def strategy_v4(dragon_data: List[Dict], kline_cache: Dict[str, List[Dict]],
                min_tech_score: int = 85, min_intraday: float = 0.0,
                hold_days: int = 7, stop_loss: float = -8.0,
                trailing_stop: float = -5.0, take_profit: float = 100.0,
                show_tech: bool = False, today_only: bool = False,
                no_inst_filter: bool = False) -> List[Dict]:
    """v5策略

    入场: D0涨停 + 技术分>95 + D1高开(>5%) + 排除机构2~3家买入
    出场: D1日内<-3%清仓 | 主板:涨幅<10% 创科:涨幅<3%+日内<-3% | 上限7天
    """
    trades = []

    by_code = defaultdict(list)
    for row in dragon_data:
        code = row.get('stock_code', '')
        if code:
            by_code[code].append(row)

    for code, rows in by_code.items():
        rows.sort(key=lambda x: x.get('trade_date', ''))

        bars = kline_cache.get(code)
        if not bars:
            bars = fetch_kline_db(code, 300)
            if bars:
                kline_cache[code] = bars
        if not bars:
            continue

        board_type = get_board_type(code)

        for row in rows:
            trade_date = row.get('trade_date', '')
            change_pct = float(row.get('change_percent', 0) or 0)
            net_amount = float(row.get('net_amount', 0) or 0)
            buy_seats = int(row.get('buy_seat_count', 0) or 0)
            reason = row.get('reason', '')

            # 条件1: D0涨停
            if change_pct < 9.5:
                continue

            # 过滤: 排除机构2~3家买入 (出货信号)
            if not no_inst_filter:
                _inst_m = re.search(r'(\d+)家机构买入', reason)
                if _inst_m and int(_inst_m.group(1)) in [2, 3]:
                    continue

            # 找到D0索引
            d0_idx = None
            for j, b in enumerate(bars):
                if b['time'] == trade_date:
                    d0_idx = j
                    break
            if d0_idx is None:
                continue

            # 验证涨停
            if d0_idx > 0:
                prev_close = bars[d0_idx - 1]['close']
                if not is_limit_up(bars[d0_idx]['close'], prev_close, board_type):
                    continue

            # 条件2: 技术分>min_tech_score
            tech = analyze_tech(bars, d0_idx)
            if tech.get('tech_score', 0) < min_tech_score:
                continue

            d0_close = bars[d0_idx]['close']

            # === today_only模式: D0盘后, 不需要D1数据 ===
            if today_only:
                buy_price = d0_close  # D1开盘价未知, 用D0收盘价近似

                score = tech.get('tech_score', 50)
                if net_amount > 50000000:
                    score += 10
                if buy_seats > 0:
                    score += 5

                trades.append({
                    'code': code,
                    'board': get_board_name(code),
                    'strategy': 'v4',
                    'signal_date': trade_date,
                    'signal_type': '涨停+强势',
                    'score': min(100, score),
                    'd0_change': round(change_pct, 2),
                    'd1_change': 0,
                    'd1_intraday': 0,
                    'net_amount': round(net_amount / 10000, 2),
                    'buy_seats': buy_seats,
                    'entry_date': '',  # D1日期未知
                    'entry_price': round(buy_price, 3),
                    'tech': tech,
                })
                continue

            # === 正常回测模式 ===
            if d0_idx + 1 >= len(bars):
                continue

            d1 = bars[d0_idx + 1]
            buy_price = d1['open']
            buy_idx = d0_idx + 1

            if buy_price <= 0:
                continue

            # D1日内动量 = (D1收盘 - D1开盘) / D1开盘 * 100
            d1_intraday = (d1['close'] - d1['open']) / d1['open'] * 100 if d1['open'] > 0 else 0

            # D1开盘涨幅
            d1_gap = (buy_price / d0_close - 1) * 100

            # D1低开放弃, 只买高开
            if d1_gap <= 5:
                continue

            # 回测
            result = run_backtest_v4(bars, d0_idx, buy_price, d1_intraday,
                                      hold_days, stop_loss, trailing_stop, take_profit,
                                      min_intraday, board_type)
            if not result:
                continue

            score = tech.get('tech_score', 50)
            if net_amount > 50000000:
                score += 10
            if buy_seats > 0:
                score += 5

            trades.append({
                'code': code,
                'board': get_board_name(code),
                'strategy': 'v4',
                'signal_date': trade_date,
                'signal_type': '涨停+强势',
                'score': min(100, score),
                'd0_change': round(change_pct, 2),
                'd1_change': round(result['d1_change'], 2),
                'd1_intraday': round(d1_intraday, 2),
                'd1_gap': round(d1_gap, 2),
                'net_amount': round(net_amount / 10000, 2),
                'buy_seats': buy_seats,
                'entry_date': bars[buy_idx]['time'],
                'entry_price': round(buy_price, 3),
                'tech': tech,
                **result,
            })

            if show_tech:
                print(f"    {code} {trade_date}涨停{change_pct:.1f}% -> "
                      f"D1开{d1_gap:+.1f}% 日内{d1_intraday:+.1f}% "
                      f"技术{tech.get('tech_score', 0)} -> "
                      f"{result['exit_reason']} 收益{result['return_pct']:+.1f}%")

    return trades


# ================================================================
# 主函数
# ================================================================

def main():
    parser = argparse.ArgumentParser(description="龙虎榜涨停强势策略 v4")
    parser.add_argument("--days", type=int, default=30, help="分析最近N天")
    parser.add_argument("--hold-days", type=int, default=7, help="持仓天数")
    parser.add_argument("--stop-loss", type=float, default=-8.0, help="止损%%")
    parser.add_argument("--trailing-stop", type=float, default=-5.0, help="追踪止损%%")
    parser.add_argument("--take-profit", type=float, default=100.0, help="止盈%% (100=不启用)")
    parser.add_argument("--min-tech", type=int, default=95, help="最低技术分")
    parser.add_argument("--min-intraday", type=float, default=0.0, help="D1日内动量阈值%% (0=不启用)")
    parser.add_argument("--no-inst-filter", action="store_true", help="不排除机构2~3家买入")
    parser.add_argument("--today", action="store_true", help="D0盘后信号")
    parser.add_argument("--today-date", type=str, default="", help="指定D0日期")
    parser.add_argument("--all-trades", action="store_true", help="输出交易明细")
    parser.add_argument("--tech", action="store_true", help="输出技术分析")
    parser.add_argument("--compare", action="store_true", help="对比不同参数")
    args = parser.parse_args()

    print("=" * 80)
    print("龙虎榜涨停强势策略 v5")
    print("入场: D0涨停 + 技术分>95 + D1跳空高开(>5%) + 排除机构2~3家买入")
    print("出场: D1日内<-3%清仓 | 主板:涨幅<10%清仓 创科:涨幅<3%+日内<-3%清仓 | 上限7天")
    print("=" * 80)

    print("\n📊 加载龙虎榜数据...")
    dragon_data = fetch_dragon_tiger_from_db(args.days)
    print(f"  龙虎榜: {len(dragon_data)}条")

    if not dragon_data:
        print("\n❌ 无数据")
        return

    kline_cache = {}

    # 对比模式
    if args.compare:
        print(f"\n{'=' * 80}")
        print(f"📊 参数对比")
        print(f"{'=' * 80}")

        for min_tech in [70, 80, 85, 90, 95]:
            trades = strategy_v4(dragon_data, kline_cache,
                                  min_tech_score=min_tech,
                                  hold_days=args.hold_days, stop_loss=args.stop_loss,
                                  trailing_stop=args.trailing_stop, take_profit=args.take_profit,
                                  no_inst_filter=args.no_inst_filter)
            if trades:
                stats = calc_stats(trades)
                total_ret = sum(t['return_pct'] for t in trades)
                total_days = sum(t['exit_day'] for t in trades)
                rpd = total_ret / total_days if total_days > 0 else 0
                print(f"  技术>{min_tech:>2}: "
                      f"{stats['total']:>4}笔 胜率{stats['win_rate']:>5.1f}% "
                      f"均收益{stats['avg_return']:>+6.2f}% 日均{rpd:>+.3f}%")
        return

    # === today模式 ===
    if args.today:
        d0_str = args.today_date or datetime.now().strftime("%Y-%m-%d")

        print(f"\n🔄 筛选 {d0_str} 涨停信号 (技术分>{args.min_tech})...")
        trades = strategy_v4(dragon_data, kline_cache,
                              min_tech_score=args.min_tech, min_intraday=args.min_intraday,
                              today_only=True,
                              no_inst_filter=args.no_inst_filter)
        trades = [t for t in trades if t.get('signal_date') == d0_str]

        print(f"\n{'=' * 80}")
        print(f"📅 {d0_str} 盘后信号 -> D1开盘买入")
        print(f"   {len(trades)} 只股票符合条件")
        print(f"{'=' * 80}")

        if trades:
            trades.sort(key=lambda x: -x.get('score', 0))

            print(f"\n  {'排名':>4} {'代码':>8} {'板块':>6} {'评分':>4} {'D0涨幅':>7} {'建议买入价':>10} {'D1预判':>8} {'技术详情'}")
            print(f"  {'-' * 85}")

            for rank, t in enumerate(trades, 1):
                tech = t.get('tech', {})
                d0_chg = t.get('d0_change', 0)
                entry_price = t.get('entry_price', 0)

                tech_str = (f"{tech.get('ma_align', '')[:4]} "
                           f"RSI{tech.get('rsi14', 0):.0f} "
                           f"OBV{tech.get('obv_trend', '')[:2]} "
                           f"量{tech.get('vol_ratio', 0):.1f}x")

                d1_hint = "看多"
                if tech.get('obv_trend') == '下降':
                    d1_hint = "谨慎"
                if tech.get('rsi14', 50) > 70:
                    d1_hint = "高风险"

                print(f"  {rank:>4} {t['code']:>8} {t['board']:>6} {t['score']:>4} "
                      f"{d0_chg:>+6.1f}% {entry_price:>9.2f} {d1_hint:>8} {tech_str}")

            print(f"\n  操作建议:")
            print(f"  1. D1开盘价 <= 建议买入价 才入场")
            print(f"  2. D1开盘涨幅 > 5% 不追")
            print(f"  3. 持仓规则:")
            print(f"     - D1日内<-3%: D2开盘清仓")
            print(f"     - D2~D7: 主板涨幅<10%清仓 创科涨幅<3%+日内<-3%清仓")
            print(f"     - 持仓上限7天")
        else:
            print(f"  今日无符合条件的信号")
        return

    # === 正常回测 ===
    print(f"  参数: 技术分>{args.min_tech} 持仓上限{args.hold_days}天")

    print(f"\n🔄 运行策略...")
    trades = strategy_v4(dragon_data, kline_cache,
                          min_tech_score=args.min_tech,
                          hold_days=args.hold_days, stop_loss=args.stop_loss,
                          trailing_stop=args.trailing_stop, take_profit=args.take_profit,
                          show_tech=args.tech,
                          no_inst_filter=args.no_inst_filter)

    # 结果
    print(f"\n{'=' * 80}")
    print(f"📊 回测结果")
    print(f"{'=' * 80}")

    stats = calc_stats(trades)
    print_stats(stats, "v4策略")

    if trades:
        total_ret = sum(t['return_pct'] for t in trades)
        total_days = sum(t['exit_day'] for t in trades)
        rpd = total_ret / total_days if total_days > 0 else 0
        print(f"\n    单位时间: 日均{rpd:+.3f}% | 年化{rpd * 250:+.1f}%")

    # 按出场原因分组
    if trades:
        print(f"\n  出场原因:")
        reason_groups = {
            '高开低走': [], 'D2': [], 'D3': [],
            'D4': [], 'D5': [], 'D6': [],
            'D7': [], '止损': [], '持仓到期': []
        }
        for t in trades:
            r = t.get('exit_reason', '')
            matched = False
            # 高开低走
            if r.startswith('高开低走'):
                reason_groups['高开低走'].append(t)
                matched = True
            # 止损
            elif r == '止损':
                reason_groups['止损'].append(t)
                matched = True
            # 持仓到期
            elif r == '持仓到期':
                reason_groups['持仓到期'].append(t)
                matched = True
            # D{N}开头的出场原因
            if not matched:
                for d in range(2, 8):
                    if r.startswith('D{}'.format(d)):
                        reason_groups['D{}'.format(d)].append(t)
                        matched = True
                        break
            if not matched:
                reason_groups.setdefault('其他', []).append(t)

        for reason, seg in reason_groups.items():
            if not seg:
                continue
            s = calc_stats(seg)
            seg_rpd = sum(t['return_pct'] for t in seg) / sum(t['exit_day'] for t in seg) if sum(t['exit_day'] for t in seg) > 0 else 0
            print(f"    {reason}: {s['total']:>3}笔 胜率{s['win_rate']:>5.1f}% "
                  f"均收益{s['avg_return']:>+6.2f}% 均持仓{s['avg_hold']:.1f}天 日均{seg_rpd:>+.3f}%")

    # 交易明细
    if args.all_trades and trades:
        print(f"\n{'=' * 80}")
        print(f"📋 交易明细 ({len(trades)}笔)")
        print(f"{'=' * 80}")
        for t in sorted(trades, key=lambda x: x['entry_date']):
            emoji = '✅' if t['return_pct'] > 0 else '❌'
            print(f"  {emoji} {t['code']:>8} {t['board']:>6} "
                  f"{t['signal_date']}涨停{t['d0_change']:.0f}% -> "
                  f"{t['entry_date']}买{t['entry_price']:>7.2f} "
                  f"D1日内{t['d1_intraday']:>+5.1f}% "
                  f"-> {t['exit_reason']} 收益{t['return_pct']:>+6.2f}%")

    # 导出
    if trades:
        outfile = "test_dragon_hot_result.json"
        with open(outfile, "w", encoding="utf-8") as f:
            json.dump(trades, f, ensure_ascii=False, indent=2)
        print(f"\n💾 {outfile} ({len(trades)}笔)")


if __name__ == "__main__":
    main()

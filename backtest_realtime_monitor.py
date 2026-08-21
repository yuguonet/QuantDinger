#!/usr/bin/env python3
"""
realtime_monitor 盘中策略回测 (V2 多维过滤版, 基于15分钟K线)

用法:
  python backtest_realtime_monitor.py --days 60          # 最近60个交易日
  python backtest_realtime_monitor.py --days 120 --all   # 输出每笔明细
  python backtest_realtime_monitor.py --days 30 --category 首板新  # 只测某一类
  python backtest_realtime_monitor.py --compare          # 参数对比模式
  python backtest_realtime_monitor.py --take-profit 8    # 止盈+8%
  python backtest_realtime_monitor.py --tech-filter      # 启用技术分过滤 (V2)
  python backtest_realtime_monitor.py --tech-filter --min-tech 70  # 技术分>=70

回测逻辑:
  1. 日线识别候选: 首板(新)/首板(旧)/昨日2连板/非昨日2连板 (排除ST, 放量)
  2. 可选: 技术分过滤 (tech_score >= min_tech, 默认60)
  3. 次日用15mK线模拟盘中信号规则
  4. 信号触发后追踪出场 (止损 / 追踪止损 / 止盈 / 持仓上限)

出场规则:
  止损: 跌破 entry × (1 + stop_loss/100)
  追踪止损: 从峰值回撤 trailing_stop%
  止盈: 峰值达到 entry × (1 + take_profit/100) 时, 追踪止损收紧一半锁定利润
  持仓到期: 最后一根bar收盘

信号规则 (V2, 与 realtime_monitor.py V2 一致):
  前置条件 — tech_score >= 60 (启用 --tech-filter 时) + 日内动量 > -2%
  首板(新)   — 今日动量 > 昨日动量 × 1.5 + OBV不下降
  首板(旧)   — 突破VWAP站稳>2根bar(≈30min) + 多头排列 + RSI>50
  昨日2连板  — 高开>0% + 收盘>VWAP + 缩量 + 多头排列
  非昨日2连板 — 突破VWAP站稳>2根bar(≈30min) + 多头排列 + RSI>50
"""
from __future__ import annotations
import argparse, os, sys, json
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

# ================================================================
# DB 初始化 (与 test_dragon.py 相同)
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

_writer_cache = None
def _get_writer():
    global _writer_cache
    if _writer_cache is not None:
        return _writer_cache
    _load_env()
    from app.utils.db_market import get_market_kline_writer
    _writer_cache = get_market_kline_writer()
    return _writer_cache

def get_all_codes_db() -> List[str]:
    writer = _get_writer()
    stats = writer.stats("CNStock")
    return stats.get("symbol_list", []) if stats.get("exists") else []

def get_board_type(code: str) -> str:
    c = str(code)[:3]
    return "gem_star" if c.startswith("30") or c.startswith("68") else "main"

def get_limit_pct(code: str) -> float:
    return 20.0 if get_board_type(code) == "gem_star" else 10.0

def is_limit_up(close, prev_close, board_type):
    threshold = 0.098 if board_type == "main" else 0.198
    if prev_close <= 0: return False
    return (close / prev_close - 1) >= threshold * 0.98

def find_limit_up_indices(bars, board_type):
    result = []
    for i in range(1, len(bars)):
        if is_limit_up(bars[i]['close'], bars[i-1]['close'], board_type):
            result.append(i)
    return result

# ================================================================
# 数据加载
# ================================================================

def _load_st_codes() -> set:
    try:
        from app.utils.basicinfo_db import get_stock_basic_db
        db = get_stock_basic_db()
        pool = db._get_pool()
        with pool.cursor() as cur:
            cur.execute("SELECT symbol FROM stock_basic_info WHERE status='active' AND name ILIKE '%%ST%%'")
            return {row[0] for row in cur.fetchall()}
    except Exception:
        return set()

def fetch_daily_kline(code: str, days: int = 120) -> List[Dict]:
    """日线 (前复权)"""
    from app.data_sources.provider.adjustment import unadj_to_qfq
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=int(days * 1.5))).strftime("%Y-%m-%d")
    try:
        writer = _get_writer()
        data = writer.query("CNStock", code, "1D", start_time=start, end_time=end, limit=0)
        if not data:
            return []
        bars = [{"time": str(r["time"])[:10], "open": float(r["open"]), "high": float(r["high"]),
                 "low": float(r["low"]), "close": float(r["close"]), "volume": float(r["volume"])}
                for r in data]
        return unadj_to_qfq(bars, code)
    except Exception:
        return []

def fetch_15m_kline(code: str, start_date: str, end_date: str) -> List[Dict]:
    """15分钟K线 (前复权), 指定日期范围"""
    from app.data_sources.provider.adjustment import unadj_to_qfq
    try:
        writer = _get_writer()
        data = writer.query("CNStock", code, "15m",
                            start_time=start_date, end_time=end_date, limit=0)
        if not data:
            return []
        bars = [{"time": str(r["time"]), "open": float(r["open"]), "high": float(r["high"]),
                 "low": float(r["low"]), "close": float(r["close"]), "volume": float(r["volume"])}
                for r in data]
        return unadj_to_qfq(bars, code)
    except Exception:
        return []

# ================================================================
# 技术指标 (移植自 dragon_d0_alert.py / realtime_monitor.py V2)
# ================================================================

def calc_rsi(closes, period=14):
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
    return 100 - 100 / (1 + avg_g / avg_l) if avg_l > 0 else 100.0


def calc_kdj_k(closes, highs, lows, period=9):
    if len(closes) < period:
        return None
    rsvs = []
    for i in range(period - 1, len(closes)):
        hn = max(highs[i-period+1:i+1])
        ln = min(lows[i-period+1:i+1])
        c = closes[i]
        rsvs.append((c - ln) / (hn - ln) * 100 if hn != ln else 50)
    k_val = 50.0
    for rsv in rsvs:
        k_val = 2/3 * k_val + 1/3 * rsv
    return k_val


def calc_obv_trend(bars, period=5):
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


def calc_ma5_angle(closes, period=5, days=3):
    if len(closes) < period + days:
        return None
    ma = [sum(closes[i-period+1:i+1]) / period for i in range(period - 1, len(closes))]
    if len(ma) < days:
        return None
    recent = ma[-days:]
    n = days
    sum_x = n * (n - 1) / 2
    sum_y = sum(recent)
    sum_xy = sum(i * recent[i] for i in range(n))
    sum_x2 = n * (n - 1) * (2 * n - 1) / 6
    denom = n * sum_x2 - sum_x * sum_x
    slope = (n * sum_xy - sum_x * sum_y) / denom if denom != 0 else 0
    return slope / recent[-1] * 100 if recent[-1] else None


def calc_tech_score(bars, idx):
    """综合技术评分 (0-100)"""
    if idx < 20 or idx >= len(bars):
        return 0
    closes = [bars[i]['close'] for i in range(max(0, idx - 60), idx + 1)]
    highs = [bars[i]['high'] for i in range(max(0, idx - 60), idx + 1)]
    lows = [bars[i]['low'] for i in range(max(0, idx - 60), idx + 1)]
    ma5 = sum(closes[-5:]) / 5 if len(closes) >= 5 else None
    ma10 = sum(closes[-10:]) / 10 if len(closes) >= 10 else None
    ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else None
    rsi14 = calc_rsi(closes, 14)
    kdj_k = calc_kdj_k(closes, highs, lows, 9)
    obv_trend = calc_obv_trend(bars[:idx + 1])
    vol_ratio = bars[idx]['volume'] / (sum(bars[j]['volume'] for j in range(max(0, idx-5), idx)) / 5) if idx >= 5 and sum(bars[j]['volume'] for j in range(max(0, idx-5), idx)) > 0 else 1.0
    angle = calc_ma5_angle(closes[-20:], 5, 3) if len(closes) >= 20 else None
    score = 50
    if ma5 and ma10 and ma20:
        if ma5 > ma10 > ma20: score += 20
        elif ma5 < ma10 < ma20: score -= 15
        elif ma5 > ma10: score += 10
    if rsi14 is not None:
        if 40 <= rsi14 <= 60: score += 5
        elif rsi14 > 70: score -= 10
        elif rsi14 < 30: score += 10
    if obv_trend == "上升": score += 10
    elif obv_trend == "下降": score -= 10
    if 1.0 <= vol_ratio <= 2.0: score += 5
    elif vol_ratio > 3.0: score -= 5
    if angle is not None:
        if angle > 0.5: score += 10
        elif angle > 0.3: score += 5
    if kdj_k is not None:
        if kdj_k > 90: score += 10
        elif kdj_k > 80: score += 5
    return max(0, min(100, score))


def calc_daily_tech_details(bars, idx):
    if idx < 20 or idx >= len(bars):
        return {}
    closes = [bars[i]['close'] for i in range(max(0, idx - 60), idx + 1)]
    highs = [bars[i]['high'] for i in range(max(0, idx - 60), idx + 1)]
    lows = [bars[i]['low'] for i in range(max(0, idx - 60), idx + 1)]
    ma5 = sum(closes[-5:]) / 5 if len(closes) >= 5 else None
    ma10 = sum(closes[-10:]) / 10 if len(closes) >= 10 else None
    ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else None
    ma_bull = bool(ma5 and ma10 and ma20 and ma5 > ma10 > ma20)
    rsi14 = calc_rsi(closes, 14)
    return {
        'ma_bull': ma_bull,
        'rsi14': round(rsi14, 1) if rsi14 else None,
        'obv_trend': calc_obv_trend(bars[:idx + 1]),
        'tech_score': calc_tech_score(bars, idx),
    }


# ================================================================
# 候选识别 (与 realtime_monitor.py 一致)
# ================================================================

def calc_vol_ratio(daily_bars: List[Dict], limit_idx: int, window: int = 5) -> float:
    if limit_idx < window + 1:
        return 0.0
    avg = sum(daily_bars[j]['volume'] for j in range(limit_idx - window, limit_idx)) / window
    return daily_bars[limit_idx]['volume'] / avg if avg > 0 else 0.0

def has_recent_limit_up(daily_bars, before_idx, lookback, bt):
    start = max(0, before_idx - lookback)
    for i in range(start, before_idx):
        if is_limit_up(daily_bars[i]['close'], daily_bars[i-1]['close'], bt):
            return True
    return False

def classify_candidates(daily_bars: List[Dict], code: str, tech_filter: bool = False, min_tech: int = 60) -> List[Dict]:
    """对某只股票的日线, 找出所有符合条件的候选日并分类

    返回: [{date, source, yesterday_momentum, ...}, ...]
    tech_filter: 启用技术分过滤 (V2)
    min_tech: 最低技术分阈值 (默认60)
    """
    bt = get_board_type(code)
    if len(daily_bars) < 8:
        return []

    candidates = []
    # 从第8天开始 (需要前5天判断是否有涨停)
    for idx in range(7, len(daily_bars)):
        bar = daily_bars[idx]
        prev = daily_bars[idx - 1]
        prev2 = daily_bars[idx - 2] if idx >= 2 else None

        yesterday_lu = is_limit_up(bar['close'], prev['close'], bt)
        day_before_lu = prev2 and is_limit_up(prev['close'], prev2['close'], bt)

        # 昨日动量
        ym = (bar['close'] - bar['open']) / prev['close'] * 100 if prev['close'] > 0 else 0

        if yesterday_lu and not day_before_lu:
            # 首板
            vr = calc_vol_ratio(daily_bars, idx, 5)
            if vr < 1.5:
                continue
            has_recent = has_recent_limit_up(daily_bars, idx - 1, 5, bt)
            cat = '首板(新)' if not has_recent else '首板(旧)'
            candidates.append({
                'date': bar['time'], 'source': cat, 'close': bar['close'],
                'prev_close': prev['close'], 'open': bar['open'],
                'yesterday_momentum': round(ym, 2), 'vol_ratio': round(vr, 2),
                'last_volume': bar['volume'],
            })

        elif yesterday_lu and day_before_lu:
            # 连板 — 排除3连板以上: 前天之前一天不能是涨停
            prev2 = daily_bars[idx - 3] if idx >= 3 else None
            day_before2_lu = prev2 and is_limit_up(daily_bars[idx-2]['close'], prev2['close'], bt)
            if day_before2_lu:
                continue  # 3连板以上, 跳过
            first_lu_idx = idx - 1  # 首板日
            vr = calc_vol_ratio(daily_bars, first_lu_idx, 5)
            if vr < 1.5:
                continue
            candidates.append({
                'date': bar['time'], 'source': '昨日2连板', 'close': bar['close'],
                'prev_close': prev['close'], 'open': bar['open'],
                'yesterday_momentum': round(ym, 2), 'vol_ratio': round(vr, 2),
                'streak': 2, 'last_volume': bar['volume'],
            })

        else:
            # 非昨日2连板: 近期有恰好2连板
            limit_indices = find_limit_up_indices(daily_bars[:idx + 1], bt)
            if len(limit_indices) < 2:
                continue
            max_streak = 1
            cur_streak = 1
            best_start = limit_indices[0]
            for j in range(1, len(limit_indices)):
                if limit_indices[j] == limit_indices[j-1] + 1:
                    cur_streak += 1
                    if cur_streak > max_streak:
                        max_streak = cur_streak
                        best_start = limit_indices[j - cur_streak + 1]
                else:
                    cur_streak = 1
            if max_streak != 2:
                continue
            # 额外校验: 确保是真正的2连板
            if not is_limit_up(daily_bars[best_start]['close'], daily_bars[best_start-1]['close'], bt):
                continue
            if not is_limit_up(daily_bars[best_start+1]['close'], daily_bars[best_start]['close'], bt):
                continue
            # 前一天不能是涨停 (否则是3连板)
            if best_start >= 2 and is_limit_up(daily_bars[best_start-1]['close'], daily_bars[best_start-2]['close'], bt):
                continue
            vr = calc_vol_ratio(daily_bars, best_start, 5)
            if vr < 1.5:
                continue
            candidates.append({
                'date': bar['time'], 'source': '非昨日2连板', 'close': bar['close'],
                'prev_close': prev['close'], 'open': bar['open'],
                'yesterday_momentum': round(ym, 2), 'vol_ratio': round(vr, 2),
                'last_volume': bar['volume'],
            })

    # V2: 注入技术分并过滤
    if tech_filter and candidates:
        filtered = []
        for c in candidates:
            cand_idx = None
            for i, b in enumerate(daily_bars):
                if b['time'] == c['date']:
                    cand_idx = i
                    break
            if cand_idx is not None and cand_idx >= 20:
                tech = calc_daily_tech_details(daily_bars, cand_idx)
                c['tech_score'] = tech.get('tech_score', 0)
                c['ma_bull'] = tech.get('ma_bull', False)
                c['rsi14'] = tech.get('rsi14', 0)
                c['obv_trend'] = tech.get('obv_trend', '平')
                if c['tech_score'] >= min_tech:
                    filtered.append(c)
            else:
                c['tech_score'] = 0
        return filtered

    return candidates

# ================================================================
# 15m VWAP 计算
# ================================================================

def calc_vwap_15m(bars_15m: List[Dict]) -> List[float]:
    """计算15m级别VWAP序列"""
    vwap = []
    total_pv = 0.0
    total_vol = 0.0
    for b in bars_15m:
        typical = (b['high'] + b['low'] + b['close']) / 3
        total_pv += typical * b['volume']
        total_vol += b['volume']
        vwap.append(total_pv / total_vol if total_vol > 0 else 0.0)
    return vwap

# ================================================================
# 盘中信号检测 (15m版本)
# ================================================================

def detect_signal_15m(bars_15m: List[Dict], candidate: Dict) -> Optional[Dict]:
    """用15mK线检测信号 (V2 多维过滤版, 时间精度15m)

    规则 (V2):
      前置条件 — 日内动量 > -2%
      首板(新)   — 今日动量 > 昨日动量 × 1.5 + OBV不下降
      首板(旧)   — 突破VWAP站稳>2根bar(≈30min) + 多头排列 + RSI>50
      昨日2连板  — 高开>0% + 第一根bar收盘>VWAP + 缩量 + 多头排列
      非昨日2连板 — 突破VWAP站稳>2根bar(≈30min) + 多头排列 + RSI>50
    """
    if len(bars_15m) < 3:
        return None

    source = candidate['source']
    prev_close = candidate['prev_close']
    yesterday_momentum = candidate['yesterday_momentum']
    today_open = bars_15m[0]['open']

    if prev_close <= 0 or today_open <= 0:
        return None

    open_gap = (today_open / prev_close - 1) * 100
    vwap_seq = calc_vwap_15m(bars_15m)

    for i in range(2, len(bars_15m)):
        current = bars_15m[i]
        current_price = current['close']
        vwap = vwap_seq[i]
        today_momentum = (current_price - today_open) / prev_close * 100

        # V2 前置: 日内动量 < -2% 直接拒绝
        if today_momentum < -2.0:
            continue

        signal = None

        if source == '昨日2连板':
            # 高开>0% + 第一根bar收盘>VWAP + 缩量 + 多头排列
            if open_gap > 0 and bars_15m[0]['close'] > vwap_seq[0]:
                if not candidate.get('ma_bull', False):
                    continue
                today_vol = sum(b['volume'] for b in bars_15m[:i + 1])
                yesterday_vol = candidate.get('last_volume', 0)
                vol_shrink = today_vol / yesterday_vol if yesterday_vol > 0 else 1.0
                if vol_shrink < 0.7:
                    signal = {'type': '2连板_高开缩量', 'vol_shrink': round(vol_shrink, 2)}

        elif source == '首板(新)':
            if yesterday_momentum > 0 and today_momentum > yesterday_momentum * 1.5:
                # V2: OBV不下降
                obv = candidate.get('obv_trend', '平')
                if obv == '下降':
                    continue
                signal = {'type': '首板新_动量加速'}

        else:
            # 首板(旧) / 非昨日2连板 — 站稳VWAP超2根bar + 多头 + RSI>50
            if not candidate.get('ma_bull', False):
                continue
            rsi14 = candidate.get('rsi14', 0)
            if rsi14 and rsi14 < 50:
                continue
            if current_price >= vwap:
                above_count = 0
                for j in range(i, -1, -1):
                    if bars_15m[j]['close'] >= vwap_seq[j]:
                        above_count += 1
                    else:
                        break
                if above_count >= 2:
                    signal = {'type': f'{source}_站稳均线'}

        if signal:
            signal.update({
                'bar_idx': i,
                'entry_price': current_price,
                'entry_time': current['time'],
                'vwap': vwap,
                'open_gap': round(open_gap, 2),
                'today_momentum': round(today_momentum, 2),
                'yesterday_momentum': yesterday_momentum,
            })
            return signal

    return None

# ================================================================
# 出场回测 (15m)
# ================================================================

def run_exit_backtest(bars_15m: List[Dict], entry_idx: int, entry_price: float,
                      stop_loss: float = -5.0, trailing_stop: float = -5.0,
                      take_profit: float = 0, max_hold_bars: int = 32) -> Dict:
    """15m级别出场回测

    max_hold_bars: 32根15m bar ≈ 7个交易日 (32×15min=480min=8h=1天, ×7≈7天)

    出场规则:
      1. 止盈: 峰值达到 entry_price × (1 + take_profit/100) 时锁定
      2. 止损: 跌破 entry_price × (1 + stop_loss/100)
      3. 追踪止损: 从峰值回撤 trailing_stop%
      4. 持仓到期: 最后一根bar收盘
    """
    if entry_price <= 0 or entry_idx >= len(bars_15m):
        return {}

    peak = entry_price
    exit_price = entry_price
    exit_bar = 0
    take_profit_hit = False

    for d in range(max_hold_bars):
        idx = entry_idx + d
        if idx >= len(bars_15m):
            break
        b = bars_15m[idx]
        if b['high'] > peak:
            peak = b['high']

        # 止盈: 峰值达到目标, 用追踪止损锁定利润
        if take_profit and peak >= entry_price * (1 + take_profit / 100):
            take_profit_hit = True
            # 进入追踪止损模式, 用更紧的回撤阈值
            tp_trailing = trailing_stop / 2  # 止盈后追踪止损收紧一半
            if b['low'] <= peak * (1 + tp_trailing / 100):
                exit_price = peak * (1 + tp_trailing / 100)
                exit_bar = d
                break
            exit_price = b['close']
            exit_bar = d
            continue

        # 追踪止损
        if d > 0 and b['low'] <= peak * (1 + trailing_stop / 100):
            exit_price = peak * (1 + trailing_stop / 100)
            exit_bar = d
            break

        # 止损
        if b['low'] <= entry_price * (1 + stop_loss / 100):
            exit_price = entry_price * (1 + stop_loss / 100)
            exit_bar = d
            break

        exit_price = b['close']
        exit_bar = d

    ret = (exit_price / entry_price - 1) * 100
    peak_ret = (peak / entry_price - 1) * 100

    return {
        'exit_price': round(exit_price, 3),
        'exit_bar': exit_bar,
        'return_pct': round(ret, 2),
        'peak_return_pct': round(peak_ret, 2),
        'exit_reason': 'take_profit' if take_profit_hit and ret > 0 else (
            'trailing_stop' if ret < 0 and exit_bar > 0 else (
                'stop_loss' if ret < 0 and exit_bar <= 1 else 'max_hold')),
    }

# ================================================================
# 单股回测
# ================================================================

def backtest_stock(code: str, daily_bars: List[Dict],
                   hold_bars: int = 32, stop_loss: float = -5.0,
                   trailing_stop: float = -5.0, take_profit: float = 0,
                   tech_filter: bool = False, min_tech: int = 60) -> List[Dict]:
    """单股回测: 识别候选日 → 拉15m数据 → 检测信号 → 模拟出场"""
    candidates = classify_candidates(daily_bars, code, tech_filter=tech_filter, min_tech=min_tech)
    if not candidates:
        return []

    bt = get_board_type(code)
    trades = []

    for cand in candidates:
        cand_date = cand['date']

        # 拉取候选日+后续10个交易日的15m数据 (用于信号检测+持仓)
        next_date = (datetime.strptime(cand_date, "%Y-%m-%d") + timedelta(days=15)).strftime("%Y-%m-%d")
        bars_15m = fetch_15m_kline(code, cand_date, next_date)
        if not bars_15m or len(bars_15m) < 4:
            continue

        # 按交易日分组 (15m bar的日期)
        day_groups = defaultdict(list)
        for b in bars_15m:
            day = b['time'][:10]
            day_groups[day].append(b)

        # 信号日 = 候选日的下一个交易日
        sorted_days = sorted(day_groups.keys())
        signal_day_idx = None
        for i, d in enumerate(sorted_days):
            if d > cand_date:
                signal_day_idx = i
                break
        if signal_day_idx is None:
            continue

        signal_day = sorted_days[signal_day_idx]
        signal_bars = day_groups[signal_day]

        # 检测信号
        signal = detect_signal_15m(signal_bars, cand)
        if signal is None:
            continue

        # 合并后续bar用于出场回测
        all_bars_after = []
        for d in sorted_days[signal_day_idx:]:
            all_bars_after.extend(day_groups[d])

        # 出场回测 (信号触发bar之后入场)
        entry_idx = signal['bar_idx']
        entry_price = signal['entry_price']
        exit_result = run_exit_backtest(all_bars_after, entry_idx, entry_price,
                                        stop_loss=stop_loss, trailing_stop=trailing_stop,
                                        take_profit=take_profit,
                                        max_hold_bars=hold_bars)
        if not exit_result:
            continue

        trades.append({
            'code': code,
            'source': cand['source'],
            'candidate_date': cand_date,
            'signal_date': signal_day,
            'signal_type': signal['type'],
            'entry_price': round(entry_price, 3),
            'yesterday_momentum': cand['yesterday_momentum'],
            'today_momentum': signal['today_momentum'],
            'open_gap': signal['open_gap'],
            'vol_ratio': cand['vol_ratio'],
            **exit_result,
        })

    return trades

# ================================================================
# 统计输出
# ================================================================

def print_stats(trades: List[Dict], label: str = ""):
    if not trades:
        print(f"  {label}: 无交易")
        return

    total = len(trades)
    wins = sum(1 for t in trades if t['return_pct'] > 0)
    wr = wins / total * 100
    avg_ret = sum(t['return_pct'] for t in trades) / total
    avg_peak = sum(t['peak_return_pct'] for t in trades) / total

    ws = [t['return_pct'] for t in trades if t['return_pct'] > 0]
    ls = [t['return_pct'] for t in trades if t['return_pct'] <= 0]
    if ws and ls:
        pl = (sum(ws)/len(ws)) / (abs(sum(ls))/len(ls))
    elif ws:
        pl = 999.0
    else:
        pl = 0.0

    # 出场原因分布
    reasons = {}
    for t in trades:
        r = t.get('exit_reason', '?')
        reasons[r] = reasons.get(r, 0) + 1
    dist = ' '.join(f"{k}:{v}" for k, v in sorted(reasons.items()))

    print(f"  {label}: {total}笔 胜率{wr:.1f}% 均收益{avg_ret:+.2f}% 均峰值{avg_peak:+.2f}% 盈亏比{pl:.2f} | {dist}")

def print_detail(trades: List[Dict]):
    if not trades:
        return
    print(f"\n  {'代码':>8} {'分类':>10} {'信号日':>12} {'入价':>8} {'出价':>8} {'收益':>7} {'峰值':>7} {'昨动量':>7} {'今动量':>7}")
    print(f"  {'-'*90}")
    for t in sorted(trades, key=lambda x: x['return_pct'], reverse=True):
        ret_emoji = '✅' if t['return_pct'] > 0 else '❌'
        print(f"  {t['code']:>8} {t['source']:>10} {t['signal_date']:>12} "
              f"{t['entry_price']:>8.2f} {t['exit_price']:>8.2f} "
              f"{t['return_pct']:>+6.1f}%{ret_emoji} {t['peak_return_pct']:>+6.1f}% "
              f"{t['yesterday_momentum']:>+6.1f}% {t['today_momentum']:>+6.1f}%")

# ================================================================
# 主函数
# ================================================================

def main():
    parser = argparse.ArgumentParser(description="realtime_monitor 盘中策略回测 (15m)")
    parser.add_argument("--days", type=int, default=60, help="回看交易日数 (默认60)")
    parser.add_argument("--codes", default="", help="指定股票代码, 逗号分隔")
    parser.add_argument("--category", default="all",
                        choices=["all", "首板新", "首板旧", "昨日2连板", "非昨日2连板"],
                        help="只测某一类")
    parser.add_argument("--hold", type=int, default=32, help="最大持仓bar数 (默认32≈7天)")
    parser.add_argument("--stop-loss", type=float, default=-5.0, help="止损百分比 (默认-5)")
    parser.add_argument("--trailing-stop", type=float, default=-5.0, help="追踪止损百分比 (默认-5)")
    parser.add_argument("--take-profit", type=float, default=0, help="止盈百分比 (默认0=不禁用)")
    parser.add_argument("--tech-filter", action="store_true", help="V2: 启用技术分过滤 (tech_score >= min-tech)")
    parser.add_argument("--min-tech", type=int, default=60, help="V2: 最低技术分 (默认60)")
    parser.add_argument("--compare", action="store_true", help="参数对比模式, 测试多组参数")
    parser.add_argument("--all", action="store_true", help="输出每笔明细")
    args = parser.parse_args()

    cat_map = {'首板新': '首板(新)', '首板旧': '首板(旧)', '昨日2连板': '昨日2连板', '非昨日2连板': '非昨日2连板'}

    print("=" * 70)
    print("  📊 realtime_monitor 盘中策略回测 (V2 多维过滤版, 15m)")
    print("=" * 70)
    if args.tech_filter:
        print(f"  ✅ 技术分过滤: tech_score >= {args.min_tech}")
    else:
        print(f"  ℹ️  技术分过滤: 未启用 (加 --tech-filter 启用 V2 过滤)")

    # 获取股票列表
    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    else:
        codes = get_all_codes_db()
    st_codes = _load_st_codes()
    codes = [c for c in codes if c not in st_codes]
    print(f"  股票: {len(codes)}只 (排除ST)")

    # 预加载所有股票的日线数据 (避免对比模式重复拉取)
    print(f"  加载日线数据...")
    daily_cache = {}
    for i, code in enumerate(codes):
        if (i + 1) % 200 == 0:
            print(f"\r  加载: {i+1}/{len(codes)}...", end="", flush=True)
        daily = fetch_daily_kline(code, args.days)
        if daily:
            daily_cache[code] = daily
    print(f"\r  日线加载完成: {len(daily_cache)}只有数据")

    # ── 参数对比模式 ──
    if args.compare:
        param_sets = [
            {'label': '当前(无止盈)',  'stop_loss': -5.0, 'trailing_stop': -5.0, 'take_profit': 0},
            {'label': '止盈+8%',      'stop_loss': -5.0, 'trailing_stop': -5.0, 'take_profit': 8.0},
            {'label': '止盈+10%',     'stop_loss': -5.0, 'trailing_stop': -5.0, 'take_profit': 10.0},
            {'label': '止盈+8%+追踪-3%', 'stop_loss': -5.0, 'trailing_stop': -3.0, 'take_profit': 8.0},
            {'label': '止盈+10%+追踪-3%', 'stop_loss': -5.0, 'trailing_stop': -3.0, 'take_profit': 10.0},
            {'label': '止损-3%+止盈+8%', 'stop_loss': -3.0, 'trailing_stop': -5.0, 'take_profit': 8.0},
        ]

        tech_str = f", 技术分>={args.min_tech}" if args.tech_filter else ""
        print(f"\n{'='*90}")
        print(f"  📊 参数对比 ({len(daily_cache)}只股票, hold={args.hold}bar{tech_str})")
        print(f"{'='*90}")
        print(f"  {'方案':<22} {'笔数':>6} {'胜率':>7} {'均收益':>8} {'均峰值':>8} {'盈亏比':>7} {'单位时间':>9} {'出场分布':>20}")
        print(f"  {'-'*90}")

        for ps in param_sets:
            all_trades = []
            for code, daily in daily_cache.items():
                trades = backtest_stock(code, daily, hold_bars=args.hold,
                                        stop_loss=ps['stop_loss'],
                                        trailing_stop=ps['trailing_stop'],
                                        take_profit=ps['take_profit'],
                                        tech_filter=args.tech_filter, min_tech=args.min_tech)
                if args.category != 'all':
                    trades = [t for t in trades if t['source'] == cat_map.get(args.category)]
                all_trades.extend(trades)

            if not all_trades:
                print(f"  {ps['label']:<22} {'无交易':>6}")
                continue

            total = len(all_trades)
            pnls = [t['return_pct'] for t in all_trades]
            peaks = [t['peak_return_pct'] for t in all_trades]
            holds = [t['exit_bar'] for t in all_trades]
            wins = [p for p in pnls if p > 0]
            losses = [p for p in pnls if p <= 0]
            wr = len(wins) / total * 100
            avg_ret = sum(pnls) / total
            avg_peak = sum(peaks) / total
            avg_hold = sum(holds) / len(holds)
            avg_win = sum(wins) / len(wins) if wins else 0
            avg_loss = abs(sum(losses)) / len(losses) if losses else 0
            pl = avg_win / avg_loss if avg_loss > 0 else 999
            rpd = ((len(wins)/total * avg_win - len(losses)/total * avg_loss) / max(avg_hold, 1))

            # 出场原因分布
            reasons = {}
            for t in all_trades:
                r = t.get('exit_reason', '?')
                reasons[r] = reasons.get(r, 0) + 1
            dist = ' '.join(f"{k}:{v}" for k, v in sorted(reasons.items()))

            print(f"  {ps['label']:<22} {total:>6} {wr:>6.1f}% {avg_ret:>+7.2f}% {avg_peak:>+7.2f}% {pl:>6.2f}x {rpd:>+8.4f}% {dist}")

        print(f"\n  单位时间收益率 = (胜率×均盈 - 败率×均亏) / 均持仓bar数")
        return

    # ── 单次回测模式 ──
    cat_map = {'首板新': '首板(新)', '首板旧': '首板(旧)', '昨日2连板': '昨日2连板', '非昨日2连板': '非昨日2连板'}

    # 回测
    all_trades = []
    cat_trades = defaultdict(list)

    for i, (code, daily) in enumerate(daily_cache.items()):
        if (i + 1) % 100 == 0:
            print(f"\r  进度: {i+1}/{len(daily_cache)}...", end="", flush=True)

        trades = backtest_stock(code, daily, hold_bars=args.hold,
                                stop_loss=args.stop_loss,
                                trailing_stop=args.trailing_stop,
                                take_profit=args.take_profit,
                                tech_filter=args.tech_filter, min_tech=args.min_tech)

        for t in trades:
            src = t['source']
            if args.category != 'all' and src != cat_map.get(args.category):
                continue
            all_trades.append(t)
            cat_trades[src].append(t)

    print(f"\r  回测完成: {len(daily_cache)}只股票, {len(all_trades)}笔交易")

    # 按分类统计
    print(f"\n{'='*70}")
    print(f"  📈 分类统计")
    print(f"{'='*70}")
    for cat in ['首板(新)', '首板(旧)', '昨日2连板', '非昨日2连板']:
        if cat_trades[cat]:
            print_stats(cat_trades[cat], cat)
    if all_trades:
        print(f"  {'-'*50}")
        print_stats(all_trades, '总计')

    # 明细
    if args.all:
        for cat in ['首板(新)', '首板(旧)', '昨日2连板', '非昨日2连板']:
            if cat_trades[cat]:
                print(f"\n  ── {cat} ──")
                print_detail(cat_trades[cat])

    # 导出
    outfile = "backtest_realtime_monitor_result.json"
    with open(outfile, "w", encoding="utf-8") as f:
        json.dump({
            'params': {'days': args.days, 'hold': args.hold, 'category': args.category,
                      'stop_loss': args.stop_loss, 'trailing_stop': args.trailing_stop,
                      'take_profit': args.take_profit},
            'total_trades': len(all_trades),
            'by_category': {cat: len(trades) for cat, trades in cat_trades.items()},
            'trades': all_trades,
        }, f, ensure_ascii=False, indent=2)
    print(f"\n  💾 明细已导出: {outfile}")


if __name__ == "__main__":
    main()

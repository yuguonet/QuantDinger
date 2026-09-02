#!/usr/bin/env python3
"""
daily_screener 多来源候选池回测

用法:
  python backtest_daily_screener.py --days 60                    # 默认回测
  python backtest_daily_screener.py --days 120 --sources 1,2,3  # 只测前3个来源
  python backtest_daily_screener.py --days 60 --compare          # 对比各来源
  python backtest_daily_screener.py --days 60 --all              # 输出每笔明细
  python backtest_daily_screener.py --days 60 --min-score 8      # 只看高分候选
  python backtest_daily_screener.py --days 60 --signal-mode vwap # 信号模式

回测逻辑:
  1. 对每个历史交易日, 用 daily_screener 的筛选逻辑产出候选池
  2. 对每只候选, 拉次日15m K线, 检测盘中信号
  3. 信号触发后模拟出场 (止损/追踪止损/止盈/持仓到期)
  4. 按来源/评分分组统计胜率

信号模式 (--signal-mode):
  vwap    — VWAP站稳>2bar (默认, 适合首板/连板)
  momentum — 动量加速 (今日动量 > 昨日×1.5, 适合趋势回踩)
  all     — 全部信号都检测
"""
from __future__ import annotations
import argparse, os, sys, json
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

# ================================================================
# DB 初始化
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

_writer_cache = None
def _get_writer():
    global _writer_cache
    if _writer_cache is not None:
        return _writer_cache
    from app.utils.db_market import get_market_kline_writer
    _writer_cache = get_market_kline_writer()
    return _writer_cache

_pool_cache = None
def _get_pool():
    global _pool_cache
    if _pool_cache is not None:
        return _pool_cache
    from app.utils.db_market import get_market_db_manager
    _pool_cache = get_market_db_manager()._get_pool("CNStock")
    return _pool_cache

def get_board_type(code: str) -> str:
    c = str(code)[:3]
    return "gem_star" if c.startswith("30") or c.startswith("68") else "main"

def is_limit_up(close, prev_close, board_type):
    threshold = 0.098 if board_type == "main" else 0.198
    if prev_close <= 0: return False
    return (close / prev_close - 1) >= threshold * 0.98

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

def _load_stock_industries() -> Dict[str, str]:
    try:
        from app.utils.basicinfo_db import get_stock_basic_db
        db = get_stock_basic_db()
        pool = db._get_pool()
        with pool.cursor() as cur:
            cur.execute("SELECT symbol, industry FROM stock_basic_info WHERE status='active' AND industry != ''")
            return {row[0]: row[1] for row in cur.fetchall()}
    except Exception:
        return {}

def fetch_daily_kline(code: str, start_date: str, end_date: str) -> List[Dict]:
    from app.data_sources.provider.adjustment import unadj_to_qfq
    try:
        writer = _get_writer()
        data = writer.query("CNStock", code, "1D",
                            start_time=start_date, end_time=end_date, limit=0)
        if not data:
            return []
        bars = [{"time": str(r["time"])[:10], "open": float(r["open"]), "high": float(r["high"]),
                 "low": float(r["low"]), "close": float(r["close"]), "volume": float(r["volume"])}
                for r in data]
        return unadj_to_qfq(bars, code)
    except Exception as e:
        if not hasattr(fetch_daily_kline, '_err_shown'):
            fetch_daily_kline._err_shown = True
            print(f"  ⚠️ K线加载失败({code}): {type(e).__name__}: {e}")
        return []

def fetch_15m_kline(code: str, start_date: str, end_date: str) -> List[Dict]:
    """获取 15m K 线（从 1m DB 读取后聚合）。

    改造说明：原直接查 kline_15m_YYYY 表，现统一从 kline_1m_YYYY 读取后聚合到 15m。
    """
    from app.data_sources.provider.adjustment import unadj_to_qfq
    try:
        writer = _get_writer()
        data = writer.query("CNStock", code, "1m",
                            start_time=start_date, end_time=end_date, limit=0)
        if not data:
            return []
        bars = [{"time": str(r["time"]), "open": float(r["open"]), "high": float(r["high"]),
                 "low": float(r["low"]), "close": float(r["close"]), "volume": float(r["volume"])}
                for r in data]
        # 1m → 15m 聚合
        bars = _aggregate_1m_to_15m(bars)
        return unadj_to_qfq(bars, code)
    except Exception:
        return []


def _aggregate_1m_to_15m(bars: List[Dict]) -> List[Dict]:
    """将 1m bar 按日期分组，每 15 根聚合为 1 根 15m bar。"""
    if not bars:
        return bars
    result = []
    cur_date = None
    group = []
    for bar in bars:
        d = bar["time"][:10] if isinstance(bar["time"], str) else str(bar["time"])[:10]
        if d != cur_date:
            if group:
                result.append(_merge_group_15m(group))
            cur_date = d
            group = [bar]
        else:
            group.append(bar)
        if len(group) == 15:
            result.append(_merge_group_15m(group))
            group = []
    if group:
        result.append(_merge_group_15m(group))
    return result


def _merge_group_15m(group: List[Dict]) -> Dict:
    return {
        "time": group[0]["time"],
        "open": group[0]["open"],
        "high": max(b["high"] for b in group),
        "low": min(b["low"] for b in group),
        "close": group[-1]["close"],
        "volume": round(sum(b["volume"] for b in group), 2),
    }

def get_all_codes() -> List[str]:
    writer = _get_writer()
    stats = writer.stats("CNStock")
    return stats.get("symbol_list", []) if stats.get("exists") else []

# ================================================================
# 技术指标 (共用)
# ================================================================

def calc_rsi(closes, period=14):
    if len(closes) < period + 1: return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]; gains.append(max(d, 0)); losses.append(max(-d, 0))
    avg_g = sum(gains[:period]) / period; avg_l = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    return 100 - 100 / (1 + avg_g / avg_l) if avg_l > 0 else 100.0

def calc_obv_trend(bars, period=5):
    if len(bars) < period + 1: return "平"
    obv = 0; obv_list = [0]
    for i in range(1, len(bars)):
        if bars[i]['close'] > bars[i-1]['close']: obv += bars[i]['volume']
        elif bars[i]['close'] < bars[i-1]['close']: obv -= bars[i]['volume']
        obv_list.append(obv)
    change = obv_list[-1] - obv_list[-period]
    return "上升" if change > 0 else ("下降" if change < 0 else "平")

def calc_tech_score(bars, idx):
    if idx < 20 or idx >= len(bars): return 0
    closes = [bars[i]['close'] for i in range(max(0, idx - 60), idx + 1)]
    highs = [bars[i]['high'] for i in range(max(0, idx - 60), idx + 1)]
    lows = [bars[i]['low'] for i in range(max(0, idx - 60), idx + 1)]
    ma5 = sum(closes[-5:]) / 5 if len(closes) >= 5 else None
    ma10 = sum(closes[-10:]) / 10 if len(closes) >= 10 else None
    ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else None
    rsi14 = calc_rsi(closes, 14)
    obv = calc_obv_trend(bars[:idx + 1])
    avg5 = sum(bars[j]['volume'] for j in range(max(0, idx-5), idx)) / 5 if idx >= 5 else 1
    vr = bars[idx]['volume'] / avg5 if avg5 > 0 else 1.0
    score = 50
    if ma5 and ma10 and ma20:
        if ma5 > ma10 > ma20: score += 20
        elif ma5 < ma10 < ma20: score -= 15
        elif ma5 > ma10: score += 10
    if rsi14 is not None:
        if 40 <= rsi14 <= 60: score += 5
        elif rsi14 > 70: score -= 10
        elif rsi14 < 30: score += 10
    if obv == "上升": score += 10
    elif obv == "下降": score -= 10
    if 1.0 <= vr <= 2.0: score += 5
    elif vr > 3.0: score -= 5
    return max(0, min(100, score))

# ================================================================
# 历史候选池生成 (模拟 daily_screener 在每个历史日的筛选)
# ================================================================

def screen_day(daily_all: Dict[str, List[Dict]], target_date: str,
               st_codes: set, industry_map: Dict[str, str],
               enabled_sources: set) -> List[Dict]:
    """对某个历史交易日, 模拟 daily_screener 的5条线筛选

    Args:
        daily_all: {code: bars} 全量日线缓存
        target_date: 目标日期 (模拟该日盘后筛选)
        st_codes: ST股集合
        industry_map: {code: industry}
        enabled_sources: 启用的来源编号集合

    Returns:
        候选列表 [{code, source, quality_score, ...}]
    """
    candidates = []

    for code, bars in daily_all.items():
        if code in st_codes:
            continue
        # 找到 target_date 对应的 idx
        idx = None
        for i, b in enumerate(bars):
            if str(b['time'])[:10] == target_date:
                idx = i
                break
        if idx is None or idx < 25:
            continue

        bt = get_board_type(code)
        bar = bars[idx]
        prev = bars[idx - 1]
        tech = calc_tech_score(bars, idx)

        # ---- 来源2: 首板/连板放量 ----
        if 2 in enabled_sources:
            if is_limit_up(bar['close'], prev['close'], bt):
                avg5 = sum(bars[j]['volume'] for j in range(max(0, idx-5), idx)) / 5 if idx >= 5 else 1
                vr = bar['volume'] / avg5 if avg5 > 0 else 0
                if vr >= 1.5:
                    prev2 = bars[idx - 2] if idx >= 2 else None
                    day_before_lu = prev2 and is_limit_up(prev['close'], prev2['close'], bt)
                    if day_before_lu:
                        prev3 = bars[idx - 3] if idx >= 3 else None
                        if not (prev3 and is_limit_up(prev2['close'], prev3['close'], bt)):
                            score = 8 + int(tech / 20)
                            candidates.append({
                                'code': code, 'source': '2连板', 'source_id': 2,
                                'quality_score': min(15, score), 'vol_ratio': round(vr, 2),
                                'tech_score': tech, 'idx': idx,
                            })
                    else:
                        has_recent = any(is_limit_up(bars[j]['close'], bars[j-1]['close'], bt)
                                        for j in range(max(1, idx-5), idx))
                        src = '首板(新)' if not has_recent else '首板(旧)'
                        score = (5 if not has_recent else 6) + int(tech / 20)
                        candidates.append({
                            'code': code, 'source': src, 'source_id': 2,
                            'quality_score': min(15, score), 'vol_ratio': round(vr, 2),
                            'tech_score': tech, 'idx': idx,
                        })

        # ---- 来源3: V1强趋势回踩 ----
        if 3 in enabled_sources and idx >= 20:
            ret20 = (bar['close'] / bars[idx-20]['close'] - 1) * 100 if bars[idx-20]['close'] > 0 else 0
            if ret20 >= 25 and idx >= 3:
                d3_close = bars[idx-3]['close']
                pullback = (bar['close'] / d3_close - 1) * 100 if d3_close > 0 else 0
                if -12 <= pullback < -2 and tech >= 40:
                    obv = calc_obv_trend(bars[:idx + 1])
                    score = 7
                    if ret20 >= 40: score += 2
                    if obv == '上升': score += 2
                    if tech >= 60: score += 1
                    candidates.append({
                        'code': code, 'source': 'V1回踩', 'source_id': 3,
                        'quality_score': min(15, score), 'ret20': round(ret20, 2),
                        'pullback': round(pullback, 2), 'tech_score': tech, 'idx': idx,
                    })

        # ---- 来源4: 技术形态突破 ----
        if 4 in enabled_sources and idx >= 20:
            closes = [bars[i]['close'] for i in range(max(0, idx-19), idx+1)]
            highs = [bars[i]['high'] for i in range(max(0, idx-19), idx+1)]
            lows = [bars[i]['low'] for i in range(max(0, idx-19), idx+1)]
            ma5 = sum(closes[-5:]) / 5; ma10 = sum(closes[-10:]) / 10; ma20 = sum(closes) / 20
            ma_bull = ma5 > ma10 > ma20
            rsi14 = calc_rsi(closes, 14)
            obv = calc_obv_trend(bars[:idx + 1])
            high5 = max(bars[i]['high'] for i in range(max(0, idx-4), idx+1))
            near_high5 = bar['close'] >= high5 * 0.98
            if ma_bull and rsi14 and 50 <= rsi14 <= 70 and obv == '上升' and near_high5 and tech >= 55:
                score = 6
                if tech >= 70: score += 2
                candidates.append({
                    'code': code, 'source': '技术突破', 'source_id': 4,
                    'quality_score': min(15, score), 'tech_score': tech, 'idx': idx,
                })

    # ---- 来源1: 龙虎榜周期 (需要单独处理, 依赖龙虎榜数据) ----
    # 简化: 用日线涨停频率代替 (不需要龙虎榜DB)
    if 1 in enabled_sources:
        # 用"近期多次涨停"作为龙虎榜周期的近似
        for code, bars in daily_all.items():
            if code in st_codes:
                continue
            idx = None
            for i, b in enumerate(bars):
                if str(b['time'])[:10] == target_date:
                    idx = i; break
            if idx is None or idx < 30:
                continue
            bt = get_board_type(code)
            # 统计过去30天涨停次数
            lu_count = sum(1 for i in range(max(1, idx-29), idx+1)
                          if is_limit_up(bars[i]['close'], bars[i-1]['close'], bt))
            if lu_count >= 2:
                # 最后一次涨停距今天数
                last_lu = None
                for i in range(idx, max(0, idx-30), -1):
                    if i > 0 and is_limit_up(bars[i]['close'], bars[i-1]['close'], bt):
                        last_lu = i; break
                if last_lu is not None:
                    days_since = idx - last_lu
                    if 3 <= days_since <= 15:
                        tech = calc_tech_score(bars, idx)
                        score = 5 + lu_count
                        if tech >= 60: score += 2
                        candidates.append({
                            'code': code, 'source': '龙虎榜周期', 'source_id': 1,
                            'quality_score': min(15, score),
                            'detail': f"涨停{lu_count}次 安静{days_since}天",
                            'tech_score': tech, 'idx': idx,
                        })

    # ---- 来源5: 板块龙头 ----
    if 5 in enabled_sources:
        # 先统计当天各板块涨停数
        sector_limits = defaultdict(int)
        for code, bars in daily_all.items():
            idx = None
            for i, b in enumerate(bars):
                if str(b['time'])[:10] == target_date:
                    idx = i; break
            if idx is None or idx < 1: continue
            bt = get_board_type(code)
            if is_limit_up(bars[idx]['close'], bars[idx-1]['close'], bt):
                ind = industry_map.get(code, '')
                if ind: sector_limits[ind] += 1
        hot_sectors = {ind for ind, cnt in sector_limits.items() if cnt >= 2}

        for code, bars in daily_all.items():
            if code in st_codes: continue
            ind = industry_map.get(code, '')
            if ind not in hot_sectors: continue
            idx = None
            for i, b in enumerate(bars):
                if str(b['time'])[:10] == target_date:
                    idx = i; break
            if idx is None or idx < 20: continue
            # 非涨停 + 涨幅>3%
            bt = get_board_type(code)
            if is_limit_up(bars[idx]['close'], bars[idx-1]['close'], bt): continue
            change = (bars[idx]['close'] / bars[idx-1]['close'] - 1) * 100 if bars[idx-1]['close'] > 0 else 0
            if change < 3: continue
            tech = calc_tech_score(bars, idx)
            if tech < 55: continue
            score = 5
            if change >= 5: score += 2
            if tech >= 70: score += 2
            candidates.append({
                'code': code, 'source': '板块龙头', 'source_id': 5,
                'quality_score': min(15, score), 'tech_score': tech,
                'change': round(change, 2), 'idx': idx,
            })

    # 去重 (保留最高分)
    by_code = {}
    for c in candidates:
        code = c['code']
        if code not in by_code or c['quality_score'] > by_code[code]['quality_score']:
            by_code[code] = c
    return list(by_code.values())

# ================================================================
# 15m 信号检测 + 出场回测 (复用 backtest_realtime_monitor.py 逻辑)
# ================================================================

def calc_vwap_15m(bars_15m):
    vwap = []; total_pv = 0.0; total_vol = 0.0
    for b in bars_15m:
        typical = (b['high'] + b['low'] + b['close']) / 3
        total_pv += typical * b['volume']; total_vol += b['volume']
        vwap.append(total_pv / total_vol if total_vol > 0 else 0.0)
    return vwap

def detect_signal_15m(bars_15m, candidate, signal_mode='all'):
    if len(bars_15m) < 3: return None
    source = candidate.get('source', '')
    vwap_seq = calc_vwap_15m(bars_15m)
    today_open = bars_15m[0]['open']
    prev_close = candidate.get('prev_close', 0)
    if prev_close <= 0 or today_open <= 0: return None
    open_gap = (today_open / prev_close - 1) * 100

    for i in range(2, len(bars_15m)):
        cur = bars_15m[i]; price = cur['close']; vwap = vwap_seq[i]
        mom = (price - today_open) / prev_close * 100
        signal = None

        # VWAP 站稳信号
        if signal_mode in ('vwap', 'all'):
            if price >= vwap:
                above = 0
                for j in range(i, -1, -1):
                    if bars_15m[j]['close'] >= vwap_seq[j]: above += 1
                    else: break
                if above >= 2:
                    signal = {'type': 'vwap站稳', 'above_bars': above}

        # 动量加速信号
        if signal is None and signal_mode in ('momentum', 'all'):
            ym = candidate.get('yesterday_momentum', 0)
            if ym > 0 and mom > ym * 1.5:
                signal = {'type': '动量加速'}

        # 2连板高开缩量
        if signal is None and source == '2连板':
            if open_gap > 0 and bars_15m[0]['close'] > vwap_seq[0]:
                today_vol = sum(b['volume'] for b in bars_15m[:i+1])
                yv = candidate.get('last_volume', 0)
                if yv > 0 and today_vol / yv < 0.7:
                    signal = {'type': '高开缩量'}

        if signal:
            signal.update({
                'bar_idx': i, 'entry_price': price, 'entry_time': cur['time'],
                'vwap': vwap, 'open_gap': round(open_gap, 2),
                'today_momentum': round(mom, 2),
            })
            return signal
    return None

def run_exit_backtest(bars_15m, entry_idx, entry_price,
                      stop_loss=-5.0, trailing_stop=-5.0,
                      take_profit=0, max_hold_bars=32):
    if entry_price <= 0 or entry_idx >= len(bars_15m): return {}
    peak = entry_price; exit_price = entry_price; exit_bar = 0
    tp_hit = False
    for d in range(max_hold_bars):
        idx = entry_idx + d
        if idx >= len(bars_15m): break
        b = bars_15m[idx]
        if b['high'] > peak: peak = b['high']
        if take_profit and peak >= entry_price * (1 + take_profit / 100):
            tp_hit = True
            tp_trail = trailing_stop / 2
            if b['low'] <= peak * (1 + tp_trail / 100):
                exit_price = peak * (1 + tp_trail / 100); exit_bar = d; break
            exit_price = b['close']; exit_bar = d; continue
        if d > 0 and b['low'] <= peak * (1 + trailing_stop / 100):
            exit_price = peak * (1 + trailing_stop / 100); exit_bar = d; break
        if b['low'] <= entry_price * (1 + stop_loss / 100):
            exit_price = entry_price * (1 + stop_loss / 100); exit_bar = d; break
        exit_price = b['close']; exit_bar = d
    ret = (exit_price / entry_price - 1) * 100
    return {
        'exit_price': round(exit_price, 3), 'exit_bar': exit_bar,
        'return_pct': round(ret, 2),
        'peak_return_pct': round((peak / entry_price - 1) * 100, 2),
        'exit_reason': 'take_profit' if tp_hit and ret > 0 else (
            'trailing_stop' if ret < 0 and exit_bar > 0 else (
                'stop_loss' if ret < 0 and exit_bar <= 1 else 'max_hold')),
    }

# ================================================================
# 主回测循环
# ================================================================

def backtest(daily_all, trade_dates, st_codes, industry_map,
             enabled_sources, signal_mode, min_score,
             hold_bars, stop_loss, trailing_stop, take_profit,
             show_detail=False):
    all_trades = []
    src_trades = defaultdict(list)
    daily_stats = []

    for di, date in enumerate(trade_dates):
        if (di + 1) % 20 == 0:
            print(f"\r  进度: {di+1}/{len(trade_dates)} ({date})...", end="", flush=True)

        # 当日盘后筛选
        candidates = screen_day(daily_all, date, st_codes, industry_map, enabled_sources)
        if not candidates:
            continue

        # 次日 = 信号日
        if di + 1 >= len(trade_dates):
            continue
        next_date = trade_dates[di + 1]

        day_trades = []
        for cand in candidates:
            if cand.get('quality_score', 0) < min_score:
                continue
            code = cand['code']
            bars = daily_all.get(code)
            if not bars:
                continue

            # 次日的前收盘价
            prev_close = 0
            last_vol = 0
            for i, b in enumerate(bars):
                if b['time'] == date:
                    prev_close = bars[i]['close'] if i > 0 else 0
                    last_vol = bars[i]['volume']
                    break
            if prev_close <= 0:
                continue
            cand['prev_close'] = prev_close
            cand['last_volume'] = last_vol
            # 昨日动量 (用候选日的K线)
            for i, b in enumerate(bars):
                if b['time'] == date and i > 0:
                    cand['yesterday_momentum'] = (bars[i]['close'] - bars[i]['open']) / bars[i-1]['close'] * 100
                    break

            # 拉次日15m数据
            next_end = (datetime.strptime(next_date, "%Y-%m-%d") + timedelta(days=3)).strftime("%Y-%m-%d")
            bars_15m = fetch_15m_kline(code, next_date, next_end)
            if not bars_15m or len(bars_15m) < 4:
                continue

            # 按日分组
            day_groups = defaultdict(list)
            for b in bars_15m:
                day_groups[b['time'][:10]].append(b)
            sorted_days = sorted(day_groups.keys())
            signal_bars = day_groups.get(sorted_days[0], []) if sorted_days else []
            if len(signal_bars) < 3:
                continue

            # 信号检测
            signal = detect_signal_15m(signal_bars, cand, signal_mode)
            if signal is None:
                continue

            # 出场回测
            all_bars = []
            for d in sorted_days:
                all_bars.extend(day_groups[d])
            entry_idx = signal['bar_idx']
            entry_price = signal['entry_price']
            exit_result = run_exit_backtest(all_bars, entry_idx, entry_price,
                                            stop_loss=stop_loss, trailing_stop=trailing_stop,
                                            take_profit=take_profit, max_hold_bars=hold_bars)
            if not exit_result:
                continue

            trade = {
                'code': code, 'source': cand['source'],
                'quality_score': cand['quality_score'],
                'tech_score': cand.get('tech_score', 0),
                'signal_date': next_date,
                'signal_type': signal['type'],
                'entry_price': round(entry_price, 3),
                'open_gap': signal['open_gap'],
                'today_momentum': signal['today_momentum'],
                **exit_result,
            }
            day_trades.append(trade)
            all_trades.append(trade)
            src_trades[cand['source']].append(trade)

        daily_stats.append({
            'date': date, 'candidates': len(candidates),
            'signals': len(day_trades),
        })

    print(f"\r  回测完成: {len(trade_dates)}天, {len(all_trades)}笔交易")
    return all_trades, src_trades, daily_stats

# ================================================================
# 统计输出
# ================================================================

def print_stats(trades, label):
    if not trades:
        print(f"  {label}: 无交易"); return
    n = len(trades)
    wins = [t for t in trades if t['return_pct'] > 0]
    losses = [t for t in trades if t['return_pct'] <= 0]
    wr = len(wins) / n * 100
    avg = sum(t['return_pct'] for t in trades) / n
    avg_pk = sum(t['peak_return_pct'] for t in trades) / n
    avg_w = sum(t['return_pct'] for t in wins) / len(wins) if wins else 0
    avg_l = abs(sum(t['return_pct'] for t in losses)) / len(losses) if losses else 0
    pl = avg_w / avg_l if avg_l > 0 else 999
    reasons = defaultdict(int)
    for t in trades: reasons[t.get('exit_reason', '?')] += 1
    dist = ' '.join(f"{k}:{v}" for k, v in sorted(reasons.items()))
    print(f"  {label}: {n}笔 胜率{wr:.1f}% 均收益{avg:+.2f}% 均峰值{avg_pk:+.2f}% 盈亏比{pl:.2f} | {dist}")

def print_detail(trades):
    if not trades: return
    print(f"\n  {'代码':>8} {'来源':>10} {'信号日':>12} {'评分':>4} {'入价':>8} {'出价':>8} {'收益':>7} {'峰值':>7}")
    print(f"  {'-'*75}")
    for t in sorted(trades, key=lambda x: x['return_pct'], reverse=True):
        e = '✅' if t['return_pct'] > 0 else '❌'
        print(f"  {t['code']:>8} {t['source']:>10} {t['signal_date']:>12} "
              f"{t['quality_score']:>4} {t['entry_price']:>8.2f} {t['exit_price']:>8.2f} "
              f"{t['return_pct']:>+6.1f}%{e} {t['peak_return_pct']:>+6.1f}%")

# ================================================================
# 主函数
# ================================================================

def main():
    parser = argparse.ArgumentParser(description="daily_screener 多来源候选池回测")
    parser.add_argument("--days", type=int, default=60, help="回看交易日数")
    parser.add_argument("--sources", default="1,2,3,4,5", help="启用来源 (逗号分隔)")
    parser.add_argument("--min-score", type=int, default=0, help="最低质量分")
    parser.add_argument("--signal-mode", default="all", choices=["vwap", "momentum", "all"])
    parser.add_argument("--hold", type=int, default=32, help="最大持仓bar数")
    parser.add_argument("--stop-loss", type=float, default=-5.0)
    parser.add_argument("--trailing-stop", type=float, default=-5.0)
    parser.add_argument("--take-profit", type=float, default=0)
    parser.add_argument("--compare", action="store_true", help="对比各来源单独的胜率")
    parser.add_argument("--all", action="store_true", help="输出每笔明细")
    args = parser.parse_args()

    enabled_sources = set(int(x) for x in args.sources.split(",") if x.strip().isdigit())

    print("=" * 70)
    print("  📊 daily_screener 多来源候选池回测")
    print("=" * 70)
    src_names = {1:'龙虎榜周期', 2:'首板/连板', 3:'V1回踩', 4:'技术突破', 5:'板块龙头'}
    print(f"  来源: {', '.join(src_names.get(s, '?') for s in sorted(enabled_sources))}")
    print(f"  信号: {args.signal_mode} | 持仓: {args.hold}bar | 止损: {args.stop_loss}% | 追踪: {args.trailing_stop}%")

    # 加载数据
    print(f"\n  [1/3] 加载全市场日线...")
    all_codes = get_all_codes()
    st_codes = _load_st_codes()
    industry_map = _load_stock_industries()
    codes = [c for c in all_codes if c not in st_codes]
    print(f"        {len(codes)}只 (排除ST)")

    # 批量加载日线 (用一个大的日期范围)
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=int(args.days * 2.5))).strftime("%Y-%m-%d")
    print(f"  日期范围: {start_date} ~ {end_date}")
    daily_all = {}
    for i, code in enumerate(codes):
        if (i + 1) % 500 == 0:
            print(f"\r  加载: {i+1}/{len(codes)}...", end="", flush=True)
        bars = fetch_daily_kline(code, start_date, end_date)
        if bars and len(bars) >= 30:
            daily_all[code] = bars
    print(f"\r  日线: {len(daily_all)}只有数据")

    if not daily_all:
        # 诊断: 用第一只股票查看数据
        if codes:
            test_bars = fetch_daily_kline(codes[0], start_date, end_date)
            print(f"  ⚠️ 诊断: {codes[0]} 返回 {len(test_bars)} 根K线")
            if test_bars:
                print(f"         首根: {test_bars[0]['time']} 末根: {test_bars[-1]['time']}")
        print(f"  ❌ 无数据, 请检查数据库连接和日期范围")
        return

    # 提取交易日列表
    all_dates = set()
    for bars in daily_all.values():
        for b in bars:
            all_dates.add(str(b['time'])[:10])
    trade_dates = sorted(all_dates)[-args.days:]
    if not trade_dates:
        print(f"  ❌ 无交易日数据")
        return
    print(f"  交易日: {len(trade_dates)}天 ({trade_dates[0]} ~ {trade_dates[-1]})")

    # ── 对比模式 ──
    if args.compare:
        print(f"\n{'='*80}")
        print(f"  📊 来源对比")
        print(f"{'='*80}")

        for src_id in sorted(enabled_sources):
            src_name = src_names.get(src_id, '?')
            trades, _, _ = backtest(
                daily_all, trade_dates, st_codes, industry_map,
                {src_id}, args.signal_mode, args.min_score,
                args.hold, args.stop_loss, args.trailing_stop, args.take_profit)
            print_stats(trades, src_name)

        # 全部来源合并
        all_t, _, _ = backtest(
            daily_all, trade_dates, st_codes, industry_map,
            enabled_sources, args.signal_mode, args.min_score,
            args.hold, args.stop_loss, args.trailing_stop, args.take_profit)
        print(f"  {'-'*50}")
        print_stats(all_t, '全部合并')
        return

    # ── 单次回测 ──
    all_trades, src_trades, daily_stats = backtest(
        daily_all, trade_dates, st_codes, industry_map,
        enabled_sources, args.signal_mode, args.min_score,
        args.hold, args.stop_loss, args.trailing_stop, args.take_profit,
        show_detail=args.all)

    # 分来源统计
    print(f"\n{'='*70}")
    print(f"  📈 分来源统计")
    print(f"{'='*70}")
    for src_id in sorted(enabled_sources):
        src_name = src_names.get(src_id, '?')
        trades_in_src = [t for t in all_trades if t['source'] in (
            k for k, v in src_names.items() if v == src_name)]
        if not trades_in_src:
            # 按 source 字段匹配
            for src_label, ts in src_trades.items():
                if any(s in src_label for s in [src_name[:2]]):
                    trades_in_src = ts; break
        if trades_in_src:
            print_stats(trades_in_src, src_name)

    print(f"  {'-'*50}")
    print_stats(all_trades, '总计')

    # 质量分分层
    if all_trades:
        high = [t for t in all_trades if t.get('quality_score', 0) >= 10]
        mid = [t for t in all_trades if 5 <= t.get('quality_score', 0) < 10]
        low = [t for t in all_trades if t.get('quality_score', 0) < 5]
        print(f"\n  质量分分层:")
        if high: print_stats(high, '  高分(>=10)')
        if mid: print_stats(mid, '  中分(5~9)')
        if low: print_stats(low, '  低分(<5)')

    # 明细
    if args.all:
        print_detail(all_trades)

    # 导出
    outfile = "backtest_daily_screener_result.json"
    with open(outfile, 'w', encoding='utf-8') as f:
        json.dump({
            'params': vars(args), 'total_trades': len(all_trades),
            'by_source': {src: len(ts) for src, ts in src_trades.items()},
            'trades': all_trades,
        }, f, ensure_ascii=False, indent=2)
    print(f"\n  💾 {outfile}")


if __name__ == "__main__":
    main()

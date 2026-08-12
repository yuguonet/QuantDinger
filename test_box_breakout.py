#!/usr/bin/env python3
"""
底部箱体突破策略 - 独立回测

═══════════════════════════════════════════════════════════════════
  核心逻辑
═══════════════════════════════════════════════════════════════════

  市场含义：
    一只股票先涨了 ~10%（小峰），然后下跌洗盘消化获利盘，
    洗盘期间成交萎缩，说明卖压枯竭。
    某天温和放量突破小峰，说明新的买盘进入，趋势正式启动。

  ① 前期小峰：过去 lookback 日内有一个局部高点（小峰）
     - 小峰涨幅约 5-20%（从区间低点算起）
     - 小峰不能太远（peak_within_days 以内）

  ② 下跌洗盘：小峰之后股价缩量回调
     - 洗盘天数 >= min_pullback_days
     - 回调幅度：从峰值回落 pullback_min_pct ~ pullback_max_pct %
     - 成交缩量：洗盘期间成交量 < 小峰日成交量的一定比例

  ③ 突破小峰：连续多日缓步上涨，累积突破小峰
     - 连续 breakout_days 天收盘价递增
     - 最终收盘价 > 小峰高点
     - 温和放量：突破日成交量 = 洗盘期间均量的 1.0-3.0 倍
     - 收阳线：当日涨幅 > 0%

  ④ 站稳确认（可选，默认关闭）：突破后 N 日不跌回小峰下方

  ⑤ 趋势确认（可选，默认关闭）：
     - 均线多头：MA5 > MA10 > MA20
     - 或 MACD 向上：MACD 柱 > 0 且 MACD 柱 > 前一日

  买入方式：站稳确认后的次日开盘买入

═══════════════════════════════════════════════════════════════════
  出场规则
═══════════════════════════════════════════════════════════════════

  ① 止盈：盈利达 X% 出场
  ② 固定止损：从买入价亏损 X% 出场
  ③ 跟踪止损：盈利达激活门槛后，从峰值回撤 X% 出场
  ④ 持仓天数上限

═══════════════════════════════════════════════════════════════════
  使用方法
═══════════════════════════════════════════════════════════════════

  # 内置200只股票快速回测
  python test_box_breakout.py

  # 全市场回测
  python test_box_breakout.py --source db

  # 指定股票
  python test_box_breakout.py --codes 000001,600519

  # 全市场 + 打印全部交易明细
  python test_box_breakout.py --source db --all-trades

  # 调整参数
  python test_box_breakout.py --lookback 60 --min-consolidation 10 --hold-above 2

  # 禁用趋势确认
  python test_box_breakout.py --no-trend-confirm

  # 每天只选1个最优信号
  python test_box_breakout.py --source db --top-per-day 1

═══════════════════════════════════════════════════════════════════
  参数说明
═══════════════════════════════════════════════════════════════════

  --codes              逗号分隔的股票代码（空=使用内置TEST_CODES）
  --source             数据源: manual（默认）或 db（全市场）
  --days               加载K线天数（默认300）
  --lookback           回看窗口：寻找小峰的范围（默认60）
  --peak-min-pct       小峰最小涨幅%（默认5.0）
  --peak-max-pct       小峰最大涨幅%（默认20.0）
  --peak-within-days   小峰距今最大天数（默认45）
  --min-pullback-days 最小洗盘天数（默认3）
  --pullback-min-pct  洗盘最小回调幅度%（默认2.0）
  --pullback-max-pct  洗盘最大回调幅度%（默认8.0）
  --vol-shrink-ratio  洗盘缩量比例（默认0.7，洗盘量<小峰量*此值）
  --vol-expand-min     突破放量下限（默认1.0，温和放量）
  --vol-expand-max     突破放量上限（默认3.0，不要爆量）
  --hold-above-days    站稳确认天数（默认1）
  --hold-above-buffer  站稳容差%（默认2.0，允许跌回小峰下方X%以内）
  --use-trend-confirm  启用趋势确认（默认关闭）
  --no-trend-confirm   禁用趋势确认
  --with-trend-confirm 启用趋势确认
  --stop-loss          固定止损%（默认5.0）
  --trailing-pct       跟踪止损回撤%（默认5.0）
  --trailing-activate  跟踪止损激活门槛%（默认5.0）
  --take-profit        止盈%（默认15.0）
  --max-hold           最大持仓天数（默认15）
  --top-per-day        每天最多选前N个最优信号（默认2，0=不过滤）
  --top                显示TOP N（默认10）
  --all-trades         打印全部交易明细
  --today              仅统计今日买点
  --today-date         指定日期（YYYY-MM-DD）
"""
from __future__ import annotations
import json, time, argparse, os, sys
from datetime import datetime, timedelta
from collections import defaultdict

# ================================================================
# 路径初始化
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

# ================================================================
# DB 数据加载
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

def get_all_codes_db():
    writer = _get_writer()
    stats = writer.stats("CNStock")
    return stats.get("symbol_list", []) if stats.get("exists") else []

def fetch_kline_db(code, days=300):
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=int(days * 1.5))).strftime("%Y-%m-%d")
    try:
        writer = _get_writer()
        data = writer.query("CNStock", code, "1D", start_time=start, end_time=end, limit=0)
        if not data:
            return []
        from app.data_sources.provider.adjustment import unadj_to_qfq
        bars = []
        for row in data:
            bars.append({
                'time': str(row.get('time', ''))[:10],
                'open': float(row.get('open', 0)),
                'high': float(row.get('high', 0)),
                'low': float(row.get('low', 0)),
                'close': float(row.get('close', 0)),
                'volume': float(row.get('volume', 0)),
            })
        bars = unadj_to_qfq(bars, code)
        return bars[-days:] if len(bars) > days else bars
    except Exception:
        return []

def fetch_kline(code, days=300):
    return fetch_kline_db(code, days)

# ================================================================
# 板块名称
# ================================================================
def get_board_name(code):
    if code.startswith('688'):
        return '科创板'
    elif code.startswith('300'):
        return '创业板'
    elif code.startswith('60'):
        return '沪主板'
    elif code.startswith('00') or code.startswith('001') or code.startswith('002'):
        return '深主板'
    return '未知'

# ================================================================
# 指标计算
# ================================================================
def compute_ma(values, period):
    """简单移动平均"""
    n = len(values)
    ma = [0.0] * n
    for i in range(period - 1, n):
        ma[i] = sum(values[i - period + 1:i + 1]) / period
    return ma

def compute_ema(values, period):
    """指数移动平均"""
    n = len(values)
    ema = [0.0] * n
    if n < period:
        return ema
    ema[period - 1] = sum(values[:period]) / period
    k = 2.0 / (period + 1)
    for i in range(period, n):
        ema[i] = values[i] * k + ema[i - 1] * (1 - k)
    return ema

def compute_macd(closes, fast=12, slow=26, signal=9):
    """MACD: 返回 (dif, dea, macd_hist)"""
    ema_fast = compute_ema(closes, fast)
    ema_slow = compute_ema(closes, slow)
    n = len(closes)
    dif = [0.0] * n
    for i in range(n):
        dif[i] = ema_fast[i] - ema_slow[i]
    dea = compute_ema(dif, signal)
    macd_hist = [0.0] * n
    for i in range(n):
        macd_hist[i] = (dif[i] - dea[i]) * 2  # A股常用 *2
    return dif, dea, macd_hist

def compute_atr(highs, lows, closes, period=14):
    """平均真实波幅"""
    n = len(closes)
    tr = [0.0] * n
    for i in range(1, n):
        tr[i] = max(highs[i] - lows[i],
                    abs(highs[i] - closes[i - 1]),
                    abs(lows[i] - closes[i - 1]))
    atr = [0.0] * n
    for i in range(period, n):
        atr[i] = sum(tr[i - period + 1:i + 1]) / period
    return atr

# ================================================================
# 回测引擎
# ================================================================
def run_backtest(bars, entry_idx, entry_price, stop_loss_pct=5.0,
                 trailing_pct=5.0, max_hold_days=15,
                 take_profit_pct=15.0, trailing_activate_pct=5.0):
    """
    出场规则:
      ① 止盈：盈利达 take_profit_pct% 直接出场
      ② 固定止损：从买入价亏 stop_loss_pct% 出场
      ③ 跟踪止损：盈利达 trailing_activate_pct% 后，从峰值回撤 trailing_pct% 出场
      ④ 持仓天数上限
      ⑤ 数据耗尽
    """
    if entry_price <= 0 or entry_idx >= len(bars):
        return None

    peak = entry_price
    exit_p = entry_price
    exit_d = 0
    exit_reason = "data_end"
    max_d = len(bars) - entry_idx - 1

    if max_d <= 0:
        return None

    for d in range(1, max_d + 1):
        idx = entry_idx + d
        if idx >= len(bars):
            break
        b = bars[idx]
        if b['high'] > peak:
            peak = b['high']

        gain_from_entry = (b['close'] / entry_price - 1) * 100

        # ① 止盈
        if gain_from_entry >= take_profit_pct:
            exit_p = b['close']
            exit_d = d
            exit_reason = "take_profit"
            break

        # ② 固定止损
        loss_from_entry = (entry_price - b['close']) / entry_price * 100
        if loss_from_entry >= stop_loss_pct:
            exit_p = b['close']
            exit_d = d
            exit_reason = "stop_loss"
            break

        # ③ 跟踪止损
        peak_gain = (peak / entry_price - 1) * 100
        if peak_gain >= trailing_activate_pct:
            drawdown = (peak - b['close']) / peak * 100
            if drawdown >= trailing_pct:
                exit_p = b['close']
                exit_d = d
                exit_reason = "trail_stop"
                break

        # ④ 持仓天数上限
        if max_hold_days > 0 and d >= max_hold_days:
            exit_p = b['close']
            exit_d = d
            exit_reason = "max_hold"
            break

        exit_p = b['close']
        exit_d = d
        exit_reason = "data_end"

    return {
        'exit_price': round(exit_p, 3),
        'exit_day': exit_d,
        'exit_reason': exit_reason,
        'return_pct': round((exit_p / entry_price - 1) * 100, 2),
        'peak_return_pct': round((peak / entry_price - 1) * 100, 2),
    }

# ================================================================
# 底部箱体突破策略
# ================================================================
def _find_peak_in_range(highs, lows, closes, end_idx, lookback,
                        peak_min_pct, peak_max_pct, peak_within_days):
    """
    在 [end_idx - lookback, end_idx] 范围内寻找小峰。

    小峰定义：
      - 从区间低点到高点涨幅在 [peak_min_pct, peak_max_pct] 范围
      - 小峰高点距 end_idx 不超过 peak_within_days

    返回: (peak_idx, peak_high, trough_low) 或 None
    """
    start = max(0, end_idx - lookback)
    if end_idx - start < 10:
        return None

    # 在窗口内找局部极值
    best_peak = None

    # 从后往前找，优先找最近的小峰
    for i in range(end_idx - 1, start + 3, -1):
        # i 是潜在的小峰位置
        peak_high = highs[i]

        # 找小峰之前的低点（往前找最多 lookback/2 天）
        search_start = max(start, i - lookback // 2)
        trough_low = min(lows[search_start:i])
        if trough_low <= 0:
            continue

        # 计算涨幅
        gain_pct = (peak_high / trough_low - 1) * 100

        if peak_min_pct <= gain_pct <= peak_max_pct:
            # 检查小峰之后是否在震荡（价格没有继续大涨）
            # 后面的收盘价不能超过小峰高点太多（留一点容差）
            post_peak_close_max = max(closes[i + 1:end_idx + 1]) if i + 1 <= end_idx else 0
            if post_peak_close_max > peak_high * 1.03:
                continue  # 已经突破过了，不算

            best_peak = {
                'peak_idx': i,
                'peak_high': peak_high,
                'trough_low': trough_low,
                'gain_pct': round(gain_pct, 2),
            }
            break  # 找到最近的就够了

    return best_peak


def _check_pullback(bars, peak_idx, current_idx, min_days,
                     pullback_min_pct, pullback_max_pct, vol_shrink_ratio):
    """
    检查小峰之后是否下跌洗盘：
      1. 洗盘天数 >= min_days
      2. 回调幅度：从峰值回落 [pullback_min_pct, pullback_max_pct]%（相对小峰涨幅）
      3. 成交缩量：洗盘期间平均成交量 < 小峰日成交量 * vol_shrink_ratio

    pullback_min_pct/pullback_max_pct 是相对小峰涨幅的比例：
      - 小峰涨幅 10%，pullback_min=0.3 → 至少回调 3%
      - 小峰涨幅 10%，pullback_max=0.7 → 最多回调 7%
    """
    if current_idx - peak_idx < min_days:
        return False

    peak_high = bars[peak_idx]['high']
    peak_vol = bars[peak_idx]['volume']
    if peak_vol <= 0 or peak_high <= 0:
        return False

    # 洗盘期间的数据
    pullback_bars = bars[peak_idx + 1:current_idx + 1]
    if len(pullback_bars) < min_days:
        return False

    # 找洗盘期间的最低点
    pullback_low = min(b['low'] for b in pullback_bars)
    if pullback_low <= 0:
        return False

    # 回调幅度（相对峰值）
    pullback_pct = (peak_high - pullback_low) / peak_high * 100

    # 需要知道小峰的涨幅才能计算相对比例
    # 这里用绝对回调幅度判断：小峰涨幅的 30-70%
    # 小峰涨幅在外部已经过滤了 5-15%，所以回调大约 1.5-10.5%
    # 直接用绝对值：回调 2-8% 是合理的
    if pullback_pct < pullback_min_pct or pullback_pct > pullback_max_pct:
        return False

    # 缩量下跌：洗盘期间成交量要缩小
    pullback_avg_vol = sum(b['volume'] for b in pullback_bars) / len(pullback_bars)
    if pullback_avg_vol > peak_vol * vol_shrink_ratio:
        return False

    return True


def _check_trend_confirm(closes, volumes, idx):
    """
    趋势确认（满足任一）：
      1. 均线多头：MA5 > MA10 > MA20
      2. MACD 向上：MACD 柱 > 0 且 > 前一日
    """
    if idx < 26:
        return False

    # 均线多头
    ma5 = sum(closes[idx - 4:idx + 1]) / 5
    ma10 = sum(closes[idx - 9:idx + 1]) / 10
    ma20 = sum(closes[idx - 19:idx + 1]) / 20
    if ma5 > ma10 > ma20:
        return True

    # MACD 向上
    _, _, macd_hist = compute_macd(closes[:idx + 1])
    if idx >= 2 and macd_hist[idx] > 0 and macd_hist[idx] > macd_hist[idx - 1]:
        return True

    return False


def strategy_box_breakout(bars, code,
                          lookback=60, peak_min_pct=5.0, peak_max_pct=20.0,
                          peak_within_days=45, min_pullback_days=3,
                          pullback_min_pct=2.0, pullback_max_pct=8.0,
                          vol_shrink_ratio=0.7,
                          vol_expand_min=1.0, vol_expand_max=3.0,
                          hold_above_days=0, hold_above_buffer=2.0,
                          use_trend_confirm=False,
                          stop_loss_pct=5.0, trailing_pct=5.0,
                          trailing_activate_pct=5.0, take_profit_pct=15.0,
                          max_hold_days=15, top_per_day=2):
    """
    底部箱体突破策略

    入场条件:
      ① 前期小峰：lookback 日内有一个 5-20% 的局部高点
      ② 下跌洗盘：小峰之后至少 min_pullback_days 天缩量回调
      ③ 突破小峰：温和放量（vol_expand_min ~ vol_expand_max 倍）突破小峰高点
      ④ 站稳确认（可选）：突破后 hold_above_days 天不跌回小峰下方
      ⑤ 趋势确认（可选）：均线多头或 MACD 向上

    出场条件:
      ① 止盈：盈利达 take_profit_pct%
      ② 固定止损：亏损 stop_loss_pct%
      ③ 跟踪止损：盈利达 trailing_activate_pct% 后回撤 trailing_pct%
      ④ 持仓上限：max_hold_days 天
    """
    if len(bars) < lookback + hold_above_days + 10:
        return []

    closes = [b['close'] for b in bars]
    highs = [b['high'] for b in bars]
    lows = [b['low'] for b in bars]
    volumes = [b['volume'] for b in bars]

    candidates = []

    # 扫描每个交易日
    for i in range(lookback + hold_above_days, len(bars)):
        # ── 条件①②：找小峰 + 检查横盘 ──
        # 我们检查 i - hold_above_days 天是否突破，然后 i 天是否站稳
        # 即：在 i - hold_above_days 天收盘价突破小峰，之后连续 hold_above_days 天站稳

        breakout_idx = i - hold_above_days
        if breakout_idx < lookback:
            continue

        # 找小峰（以 breakout_idx 为基准往前找）
        peak_info = _find_peak_in_range(highs, lows, closes, breakout_idx,
                                        lookback, peak_min_pct, peak_max_pct,
                                        peak_within_days)
        if not peak_info:
            continue

        peak_idx = peak_info['peak_idx']
        peak_high = peak_info['peak_high']

        # ── 条件②：下跌洗盘确认 ──
        if min_pullback_days > 0 and not _check_pullback(bars, peak_idx, breakout_idx,
                                    min_pullback_days, pullback_min_pct,
                                    pullback_max_pct, vol_shrink_ratio):
            continue

        # ── 条件③：多日累积突破小峰 ──
        # 不是单日爆量突破，而是连续几天缓步上涨，累积突破
        breakout_days = 3  # 连续上涨天数
        if breakout_idx < breakout_days:
            continue

        # 检查最近 breakout_days 天是否趋势向上
        recent_closes = [bars[breakout_idx - j]['close'] for j in range(breakout_days - 1, -1, -1)]
        trend_up = all(recent_closes[k] > recent_closes[k - 1] for k in range(1, len(recent_closes)))
        if not trend_up:
            continue

        # 最终收盘价必须突破小峰
        if bars[breakout_idx]['close'] <= peak_high:
            continue

        # 放量确认：突破日的成交量 > 洗盘期间平均成交量（温和放量，不是爆量）
        pullback_bars = bars[peak_idx + 1:breakout_idx]
        if len(pullback_bars) > 0:
            pullback_avg_vol = sum(b['volume'] for b in pullback_bars) / len(pullback_bars)
        else:
            pullback_avg_vol = bars[peak_idx]['volume'] * 0.5

        vol_ratio_vs_pullback = bars[breakout_idx]['volume'] / pullback_avg_vol if pullback_avg_vol > 0 else 0

        # 温和放量：1.0-3.0 倍（相对洗盘期间）
        if vol_ratio_vs_pullback < vol_expand_min or vol_ratio_vs_pullback > vol_expand_max:
            continue

        # 收阳线
        if breakout_idx >= 1 and bars[breakout_idx]['close'] <= bars[breakout_idx - 1]['close']:
            continue

        # 突破幅度：收盘价相对小峰的超出比例
        breakout_pct = (bars[breakout_idx]['close'] / peak_high - 1) * 100

        # D+1 开盘买
        if i + 1 >= len(bars):
            continue
        entry_price = bars[i + 1]['open']
        if entry_price <= 0:
            continue

        candidates.append({
            'idx': i,
            'signal_date': bars[i]['time'],
            'entry_price': entry_price,
            'entry_idx': i + 1,
            'entry_date': bars[i + 1]['time'],
            'peak_high': round(peak_high, 3),
            'peak_gain_pct': peak_info['gain_pct'],
            'pullback_days': breakout_idx - peak_idx,
            'breakout_close': bars[breakout_idx]['close'],
            'breakout_pct': round(breakout_pct, 2),
            'vol_ratio_vs_pullback': round(vol_ratio_vs_pullback, 2),
        })

    # 同一天按优先级排序
    daily_candidates = defaultdict(list)
    for c in candidates:
        daily_candidates[c['signal_date']].append(c)

    filtered = []
    for date, cands in daily_candidates.items():
        cands.sort(key=lambda c: (
            -c['breakout_pct'],         # 突破幅度越大越好
            -c['vol_ratio_vs_pullback'], # 放量越明显越好
            c['pullback_days'],          # 洗盘越久越好（充分消化）
        ))
        filtered.extend(cands[:top_per_day])

    # 生成交易记录
    trades = []
    for c in filtered:
        result = run_backtest(bars, c['entry_idx'], c['entry_price'],
                              stop_loss_pct, trailing_pct, max_hold_days,
                              take_profit_pct, trailing_activate_pct)
        if not result:
            continue

        trades.append({
            'code': code,
            'board': get_board_name(code),
            'path': 'box_breakout',
            'path_label': '底部箱体突破',
            'signal_date': c['signal_date'],
            'signal_close': closes[c['idx']],
            'peak_high': c['peak_high'],
            'peak_gain_pct': c['peak_gain_pct'],
            'pullback_days': c['pullback_days'],
            'breakout_close': c['breakout_close'],
            'breakout_pct': c['breakout_pct'],
            'vol_ratio_vs_pullback': c['vol_ratio_vs_pullback'],
            'entry_date': c['entry_date'],
            'entry_price': round(c['entry_price'], 3),
            'buy_mode': 'next_open',
            **result,
        })

    return trades

# ================================================================
# 测试股票列表
# ================================================================
TEST_CODES = [
    "000066","000402","000553","000586","000601","000637","000720","000753","000767","000783",
    "000925","000950","001208","001259","001316","002010","002011","002012","002013","002014",
    "002015","002016","002017","002018","002019","002020","002021","002022","002023","002024",
    "002025","002026","002027","002028","002029","002030","002031","002032","002033","002034",
    "002035","002036","002037","002038","002039","002040","002041","002042","002043","002044",
    "002045","002046","002047","002048","002049","002050","002055","002056","002063","002065",
    "002074","002077","002079","002081","002084","002088","002092","002093","002095","002097",
    "002100","002104","002106","002111","002115","002119","002120","002125","002127","002130",
    "002131","002137","002139","002141","002146","002149","002150","002152","002153","002156",
    "002158","002160","002163","002165","002169","002170","002172","002175","002177","002180",
    "002183","002185","002188","002190","002191","002194","002196","002198","002200","002202",
    "002208","002209","002211","002214","002218","002222","002227","002230","002232","002234",
    "002236","002238","002240","002242","002244","002248","002249","002252","002253","002255",
    "002258","002261","002263","002266","002268","002270","002272","002274","002276","002278",
    "002280","002297","002366","002464","002468","002498","002510","002512","002535","002552",
    "002560","002580","002640","002805","002858","002918","002989","300001","300002","300003",
    "300004","300005","300006","300007","300008","300009","300010","300011","300012","300013",
    "300014","300015","300016","300017","300018","300019","300020","300021","300022","300023",
    "300024","300025","300026","300027","300028","300029","300030","300031","300032","300033",
    "300034","300035","300036","300037","300038","300039","300059","300106","300124","300152",
]

# ================================================================
# 统计输出
# ================================================================
def print_stats(trades, label):
    if not trades:
        print(f"  {label}: 无信号")
        return
    rets = [t['return_pct'] for t in trades if t['return_pct'] is not None]
    if not rets:
        print(f"  {label}: 无收益数据")
        return
    win = [r for r in rets if r > 0]
    wr = len(win) / len(rets) * 100
    avg = sum(rets) / len(rets)
    print(f"  {label}: {len(rets)}笔, 胜率{wr:.1f}%, 均值{avg:+.2f}%")

# ================================================================
# 主流程
# ================================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="底部箱体突破策略 独立回测")
    parser.add_argument("--codes", default="", help="逗号分隔的股票代码")
    parser.add_argument("--days", type=int, default=300, help="加载K线天数 (默认300)")
    parser.add_argument("--source", choices=["manual", "db"], default="manual",
                        help="数据源: manual(默认), db")

    # 小峰参数
    parser.add_argument("--lookback", type=int, default=60,
                        help="回看窗口：寻找小峰的范围 (默认60)")
    parser.add_argument("--peak-min-pct", type=float, default=5.0,
                        help="小峰最小涨幅%% (默认5.0)")
    parser.add_argument("--peak-max-pct", type=float, default=20.0,
                        help="小峰最大涨幅%% (默认20.0)")
    parser.add_argument("--peak-within-days", type=int, default=45,
                        help="小峰距今最大天数 (默认45)")

    # 洗盘参数
    parser.add_argument("--min-pullback-days", type=int, default=3,
                        help="最小洗盘天数 (默认3)")
    parser.add_argument("--pullback-min-pct", type=float, default=2.0,
                        help="洗盘最小回调幅度%% (默认2.0)")
    parser.add_argument("--pullback-max-pct", type=float, default=8.0,
                        help="洗盘最大回调幅度%% (默认8.0)")
    parser.add_argument("--vol-shrink-ratio", type=float, default=0.7,
                        help="洗盘缩量比例 (默认0.7，洗盘量<小峰量*此值)")

    # 突破参数
    parser.add_argument("--vol-expand-min", type=float, default=1.0,
                        help="突破放量下限 (默认1.0，相对小峰日)")
    parser.add_argument("--vol-expand-max", type=float, default=3.0,
                        help="突破放量上限 (默认3.0，不要爆量)")

    # 站稳参数
    parser.add_argument("--hold-above-days", type=int, default=0,
                        help="站稳确认天数 (默认0=关闭)")
    parser.add_argument("--hold-above-buffer", type=float, default=2.0,
                        help="站稳容差%% (默认2.0)")

    # 趋势确认
    parser.add_argument("--use-trend-confirm", action="store_true", default=False,
                        help="启用趋势确认 (默认关闭)")
    parser.add_argument("--no-trend-confirm", action="store_false", dest="use_trend_confirm",
                        help="禁用趋势确认")
    parser.add_argument("--with-trend-confirm", action="store_true", dest="use_trend_confirm",
                        help="启用趋势确认")

    # 出场参数
    parser.add_argument("--stop-loss", type=float, default=5.0,
                        help="固定止损%% (默认5.0)")
    parser.add_argument("--trailing-pct", type=float, default=5.0,
                        help="跟踪止损回撤%% (默认5.0)")
    parser.add_argument("--trailing-activate", type=float, default=5.0,
                        help="跟踪止损激活门槛%% (默认5.0)")
    parser.add_argument("--take-profit", type=float, default=15.0,
                        help="止盈%% (默认15.0)")
    parser.add_argument("--max-hold", type=int, default=15,
                        help="最大持仓天数 (默认15)")

    # 其他
    parser.add_argument("--top-per-day", type=int, default=2,
                        help="每天最多选前N个最优信号 (默认2, 0=不过滤)")
    parser.add_argument("--top", type=int, default=10, help="显示TOP N (默认10)")
    parser.add_argument("--all-trades", action="store_true", help="打印全部交易明细")
    parser.add_argument("--today", action="store_true", help="仅统计今日买点")
    parser.add_argument("--today-date", type=str, default="", help="指定日期(YYYY-MM-DD)")
    args = parser.parse_args()

    codes = [c.strip() for c in args.codes.split(",") if c.strip()] if args.codes else TEST_CODES
    use_db = args.source == "db"

    if use_db:
        print("  DB模式: 从数据库加载全市场股票...")
        codes = get_all_codes_db()
        print(f"   全市场: {len(codes)} 只股票")

    print(f"{'=' * 80}")
    print(f"底部箱体突破策略 独立回测")
    print(f"{'=' * 80}")
    print(f"入场条件:")
    print(f"  ① 前{args.lookback}日内有小峰: 涨幅 {args.peak_min_pct}%~{args.peak_max_pct}%")
    print(f"  ② 小峰后下跌洗盘 >= {args.min_pullback_days}天, 回调{args.pullback_min_pct}%~{args.pullback_max_pct}%, 缩量至小峰日的{args.vol_shrink_ratio:.0%}")
    print(f"  ③ 连续3日缓步上涨突破小峰, 温和放量{args.vol_expand_min}x~{args.vol_expand_max}x (相对洗盘期间)")
    print(f"  ④ 站稳确认: 突破后{args.hold_above_days}天不跌回小峰下方{args.hold_above_buffer}%") if args.hold_above_days > 0 else print(f"  ④ 站稳确认: 关闭")
    if args.use_trend_confirm:
        print(f"  ⑤ 趋势确认: MA5>MA10>MA20 或 MACD向上")
    else:
        print(f"  ⑤ 趋势确认: 关闭")
    print(f"出场条件:")
    print(f"  ① 止盈: 盈利{args.take_profit}%")
    print(f"  ② 固定止损: 买入价亏{args.stop_loss}%")
    print(f"  ③ 跟踪止损: 峰值回撤{args.trailing_pct}%（盈利{args.trailing_activate}%后激活）")
    print(f"  ④ 持仓上限: {args.max_hold}天")
    print(f"买入模式: 站稳确认后次日开盘买")
    if args.top_per_day > 0:
        print(f"每日筛选: 同一天最多选前{args.top_per_day}个")
    print(f"股票: {len(codes)}只\n")

    all_trades = []
    success = 0

    for i, code in enumerate(codes):
        bars = fetch_kline_db(code, args.days) if use_db else fetch_kline(code, args.days)
        if not bars:
            continue

        trades = strategy_box_breakout(
            bars, code,
            lookback=args.lookback,
            peak_min_pct=args.peak_min_pct,
            peak_max_pct=args.peak_max_pct,
            peak_within_days=args.peak_within_days,
            min_pullback_days=args.min_pullback_days,
            pullback_min_pct=args.pullback_min_pct,
            pullback_max_pct=args.pullback_max_pct,
            vol_shrink_ratio=args.vol_shrink_ratio,
            vol_expand_min=args.vol_expand_min,
            vol_expand_max=args.vol_expand_max,
            hold_above_days=args.hold_above_days,
            hold_above_buffer=args.hold_above_buffer,
            use_trend_confirm=args.use_trend_confirm,
            stop_loss_pct=args.stop_loss,
            trailing_pct=args.trailing_pct,
            trailing_activate_pct=args.trailing_activate,
            take_profit_pct=args.take_profit,
            max_hold_days=args.max_hold,
            top_per_day=args.top_per_day,
        )
        all_trades.extend(trades)

        if trades:
            print(f"[{i+1}/{len(codes)}] {code} ({get_board_name(code)}) "
                  f"{len(trades)}个信号")
            success += 1

    # ---- 汇总统计 ----
    print(f"\n{'=' * 80}")
    print(f"回测完成: {success}/{len(codes)} 只股票有信号, 共 {len(all_trades)} 笔交易")
    print(f"{'=' * 80}")

    if all_trades:
        print_stats(all_trades, "全部")

        # 按板块统计
        print(f"\n--- 板块统计 ---")
        for board in ['沪主板', '深主板', '创业板', '科创板']:
            seg = [t for t in all_trades if t['board'] == board]
            if seg:
                print_stats(seg, board)

        # 按出场原因统计
        print(f"\n--- 出场原因统计 ---")
        from collections import Counter
        for reason, cnt in Counter(t['exit_reason'] for t in all_trades).most_common():
            seg = [t for t in all_trades if t['exit_reason'] == reason]
            print_stats(seg, reason)

        # 按突破幅度分段
        print(f"\n--- 突破幅度分段 ---")
        for lo, hi in [(0, 1), (1, 3), (3, 5), (5, 100)]:
            seg = [t for t in all_trades if lo <= t['breakout_pct'] < hi]
            if seg:
                print_stats(seg, f"突破[{lo},{hi})%")

        # 按洗盘天数分段
        print(f"\n--- 洗盘天数分段 ---")
        for lo, hi in [(8, 12), (12, 20), (20, 30), (30, 100)]:
            seg = [t for t in all_trades if lo <= t['pullback_days'] < hi]
            if seg:
                print_stats(seg, f"洗盘[{lo},{hi})天")

        # 按放量倍数分段
        print(f"\n--- 放量倍数分段(相对洗盘期间) ---")
        for lo, hi in [(1.0, 1.3), (1.3, 1.5), (1.5, 2.0)]:
            seg = [t for t in all_trades if lo <= t['vol_ratio_vs_pullback'] < hi]
            if seg:
                print_stats(seg, f"放量[{lo},{hi})x")

        # TOP N
        n = min(args.top, len(all_trades))
        print(f"\n--- TOP {n} 最佳交易 ---")
        for t in sorted(all_trades, key=lambda x: -x['return_pct'])[:n]:
            print(f"  {t['code']}({t['board']}) {t['signal_date']} "
                  f"小峰{t['peak_high']} 横盘{t['pullback_days']}天 "
                  f"突破{t['breakout_pct']:.1f}% "
                  f"→ {t['entry_date']}买{t['entry_price']:>7.2f} "
                  f"收益{t['return_pct']:+.2f}% 持仓{t['exit_day']}天")

        print(f"\n--- TOP {n} 最差交易 ---")
        for t in sorted(all_trades, key=lambda x: x['return_pct'])[:n]:
            print(f"  {t['code']}({t['board']}) {t['signal_date']} "
                  f"小峰{t['peak_high']} 横盘{t['pullback_days']}天 "
                  f"突破{t['breakout_pct']:.1f}% "
                  f"→ {t['entry_date']}买{t['entry_price']:>7.2f} "
                  f"收益{t['return_pct']:+.2f}% 持仓{t['exit_day']}天")

    # 今日信号
    if args.today:
        today_str = args.today_date if args.today_date else datetime.now().strftime("%Y-%m-%d")
        today_trades = [t for t in all_trades if t['signal_date'] == today_str]
        if not today_trades:
            print(f"\n  {today_str} 无信号")
        else:
            print(f"\n  {today_str} 信号: {len(today_trades)}只")
            for t in sorted(today_trades, key=lambda x: -x['breakout_pct']):
                print(f"    {t['code']}({t['board']}) "
                      f"小峰{t['peak_high']} 横盘{t['pullback_days']}天 "
                      f"突破{t['breakout_pct']:.1f}% "
                      f"→ 次日开盘买")

    # 保存JSON
    if all_trades:
        out_file = "test_box_breakout_result.json"
        with open(out_file, 'w', encoding='utf-8') as f:
            json.dump(all_trades, f, ensure_ascii=False, indent=2)
        print(f"\n  {out_file} ({len(all_trades)}笔)")

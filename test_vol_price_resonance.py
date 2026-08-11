#!/usr/bin/env python3
"""
量价共振突破策略 - 独立回测文件

═══════════════════════════════════════════════════════════════════
  入场规则（价格突破 + 放量确认 = 量价共振）
═══════════════════════════════════════════════════════════════════

  ① 价格突破 N 日最高价（Donchian 通道上轨）
     - 当日最高价 > 前 N 日最高价
     - 突破意味着创出新高，趋势启动信号

  ② 成交量放大确认
     - 当日成交量 > 近 M 日均量 * 倍数
     - 放量突破 = 主力资金介入，突破有效性高

  ③ 近5日涨幅在合理区间
     - 涨幅下限：排除弱势股（没动的不买）
     - 涨幅上限：排除涨停板（不追涨停）

  ④ 可选：价格在 EMA 上方（趋势过滤）

  买入方式：信号日次日开盘买入（D+1 Open）

═══════════════════════════════════════════════════════════════════
  出场规则
═══════════════════════════════════════════════════════════════════

  ① 固定止损：从买入价亏损 X% 出场
  ② 跟踪止损：从持仓最高点回撤 X% 出场
  ③ 持仓天数上限

═══════════════════════════════════════════════════════════════════
  使用方法
═══════════════════════════════════════════════════════════════════

  # 内置200只股票快速回测
  python test_vol_price_resonance.py

  # 全市场回测
  python test_vol_price_resonance.py --source db

  # 指定股票
  python test_vol_price_resonance.py --codes 000001,600519

  # 全市场 + 打印全部交易明细
  python test_vol_price_resonance.py --source db --all-trades

  # 调整参数
  python test_vol_price_resonance.py --breakout-period 10 --vol-ratio 2.0 --stop-loss 5

  # 全市场 + 股票质量过滤
  python test_vol_price_resonance.py --source db --min-short-score 5 --min-vol-price-corr 1.2 --min-vol-cv 0.5

  # 加趋势过滤（价格在EMA上方）
  python test_vol_price_resonance.py --source db --use-trend-filter

  # 每天只选1个最优信号
  python test_vol_price_resonance.py --source db --top-per-day 1

═══════════════════════════════════════════════════════════════════
  参数说明
═══════════════════════════════════════════════════════════════════

  --codes            逗号分隔的股票代码（空=使用内置TEST_CODES）
  --source           数据源: manual（默认）或 db（全市场）
  --days             加载K线天数（默认300）
  --breakout-period  突破周期：前N日最高价（默认20）
  --vol-ma-period    成交量均线周期（默认20）
  --vol-ratio        放量倍数阈值（默认2.0）
  --min-change-pct   近5日涨幅下限%（默认2.0）
  --max-change-pct   近5日涨幅上限%（默认9.5）
  --use-trend-filter 启用趋势过滤（价格在EMA上方）
  --trend-ema-period 趋势EMA周期（默认20）
  --stop-loss        固定止损%（默认5.0）
  --trailing-pct     跟踪止损回撤%（默认5.0）
  --max-hold         最大持仓天数（默认10）
  --top-per-day      每天最多选前N个最优信号（默认2，0=不过滤）
  --min-short-score  近20日涨幅下限%（默认0.0，>0排除下跌股）
  --min-vol-price-corr 量价同向度下限（默认1.0，涨时量>跌时量）
  --min-vol-cv       成交量变异系数下限（默认0.0，>0排除死水股）
  --top              显示TOP N（默认10）
  --all-trades       打印全部交易明细
  --today            仅统计今日买点
  --today-date       指定日期（YYYY-MM-DD）
"""
from __future__ import annotations
import json, time, argparse, os, sys
from datetime import datetime, timedelta

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

_circ_shares_cache = None
def _get_circ_shares(code):
    global _circ_shares_cache
    if _circ_shares_cache is not None:
        return _circ_shares_cache.get(code, 0.0)
    _load_env()
    try:
        from app.utils.basicinfo_db import get_stock_basic_db
        db = get_stock_basic_db()
        db.ensure_table()
        pool = db._get_pool()
        with pool.cursor() as cur:
            cur.execute("SELECT symbol, circ_shares FROM stock_basic_info WHERE circ_shares > 0")
            _circ_shares_cache = {row[0]: float(row[1]) for row in cur.fetchall()}
        return _circ_shares_cache.get(code, 0.0)
    except Exception:
        _circ_shares_cache = {}
        return 0.0

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
    except Exception as e:
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
# MA 计算
# ================================================================
def compute_ma(closes, period):
    n = len(closes)
    ma = [0.0] * n
    for i in range(period - 1, n):
        ma[i] = sum(closes[i - period + 1:i + 1]) / period
    return ma

def compute_ema(closes, period):
    n = len(closes)
    ema = [0.0] * n
    if n < period:
        return ema
    ema[period - 1] = sum(closes[:period]) / period
    k = 2.0 / (period + 1)
    for i in range(period, n):
        ema[i] = closes[i] * k + ema[i - 1] * (1 - k)
    return ema

# ================================================================
# Donchian 通道（N日最高价/最低价）
# ================================================================
def compute_donchian(highs, lows, period):
    """返回 (upper, lower)：前N日最高价、最低价（不含当日）"""
    n = len(highs)
    upper = [0.0] * n
    lower = [0.0] * n
    for i in range(period, n):
        upper[i] = max(highs[i - period:i])   # 前N日（不含当日）
        lower[i] = min(lows[i - period:i])
    return upper, lower

# ================================================================
# 回测引擎
# ================================================================
def run_backtest(bars, entry_idx, entry_price, stop_loss_pct=5.0,
                 trailing_pct=5.0, max_hold_days=10):
    """
    出场规则:
      ① 固定止损：从买入价亏 stop_loss_pct% 出场
      ② 跟踪止损：从峰值回撤 trailing_pct% 出场（盈利后才激活）
      ③ 持仓天数上限
      ④ 数据耗尽
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

        # ① 固定止损：从买入价亏 X%
        loss_from_entry = (entry_price - b['close']) / entry_price * 100
        if loss_from_entry >= stop_loss_pct:
            exit_p = b['close']
            exit_d = d
            exit_reason = "stop_loss"
            break

        # ② 跟踪止损：从峰值回撤 X%（盈利后才激活）
        if peak > entry_price * 1.02:  # 至少盈利2%才激活
            drawdown = (peak - b['close']) / peak * 100
            if drawdown >= trailing_pct:
                exit_p = b['close']
                exit_d = d
                exit_reason = "trail_stop"
                break

        # ③ 持仓天数上限
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
# 量价共振突破策略
# ================================================================
# ================================================================
# 量价关系评估（stock_stats 预筛选等价）
# ================================================================
def _calc_vol_price_quality(closes, volumes, idx, lookback=20):
    """
    计算个股量价质量（近 lookback 日），返回 (short_score, vol_price_corr, vol_cv)
    short_score:     近期价格动量，>0 表示上涨趋势
    vol_price_corr:  量价同向度，涨的日子量是否更大
    vol_cv:          成交量变异系数，越大说明量能越活跃
    """
    if idx < lookback:
        return 0.0, 0.0, 0.0

    seg_close = closes[idx - lookback + 1:idx + 1]
    seg_vol = volumes[idx - lookback + 1:idx + 1]

    # short_score: 近20日总收益率
    if seg_close[0] > 0:
        short_score = (seg_close[-1] / seg_close[0] - 1) * 100
    else:
        short_score = 0.0

    # vol_price_corr: 涨的日子平均量 / 跌的日子平均量
    up_vol = []
    down_vol = []
    for j in range(1, len(seg_close)):
        if seg_close[j] > seg_close[j - 1]:
            up_vol.append(seg_vol[j])
        elif seg_close[j] < seg_close[j - 1]:
            down_vol.append(seg_vol[j])

    if up_vol and down_vol:
        avg_up = sum(up_vol) / len(up_vol)
        avg_down = sum(down_vol) / len(down_vol)
        vol_price_corr = avg_up / avg_down if avg_down > 0 else 0.0
    else:
        vol_price_corr = 0.0

    # vol_cv: 成交量变异系数
    vol_mean = sum(seg_vol) / len(seg_vol) if seg_vol else 0
    if vol_mean > 0:
        vol_var = sum((v - vol_mean) ** 2 for v in seg_vol) / len(seg_vol)
        vol_cv = (vol_var ** 0.5) / vol_mean
    else:
        vol_cv = 0.0

    return short_score, vol_price_corr, vol_cv


def strategy_vol_price_resonance(bars, code,
                                 breakout_period=20, vol_ma_period=20,
                                 vol_ratio=2.0, min_change_pct=2.0,
                                 max_change_pct=9.5, use_trend_filter=False,
                                 trend_ema_period=20,
                                 stop_loss_pct=5.0, trailing_pct=5.0,
                                 max_hold_days=10, circ_shares=0.0,
                                 top_per_day=2,
                                 min_short_score=0.0, min_vol_price_corr=1.0,
                                 min_vol_cv=0.0):
    """
    量价共振突破策略:

    入场条件:
      ① 当日收盘价 > 前N日最高价（收盘突破，非盘中冲高）
      ② 当日成交量 > 近M日均量 * vol_ratio（放量确认）
      ③ 当日涨幅 > 0%（收阳线）
      ④ 上影线占比 < 30%（强势收盘，不是冲高回落）
      ⑤ 近5日涨幅在 [min_change_pct, max_change_pct] 区间
      ⑥ 可选: 价格在EMA上方（趋势过滤）

    出场条件:
      ① 固定止损: 从买入价亏 stop_loss_pct% 出场
      ② 跟踪止损: 从峰值回撤 trailing_pct% 出场（盈利2%后激活）
      ③ 持仓天数上限
    """
    if len(bars) < max(breakout_period, vol_ma_period, 30) + 5:
        return []

    closes = [b['close'] for b in bars]
    highs = [b['high'] for b in bars]
    lows = [b['low'] for b in bars]
    volumes = [b['volume'] for b in bars]

    # ---- 计算指标 ----
    donchian_upper, donchian_lower = compute_donchian(highs, lows, breakout_period)
    vol_ma = compute_ma(volumes, vol_ma_period)
    ema = compute_ema(closes, trend_ema_period) if use_trend_filter else None

    # ---- 信号检测 ----
    candidates = []
    start_idx = max(breakout_period, vol_ma_period, 20) + 1

    for i in range(start_idx, len(bars)):
        # 条件①: 当日收盘价突破前N日最高价（收盘突破更可靠）
        if donchian_upper[i] <= 0:
            continue
        if closes[i] <= donchian_upper[i]:
            continue

        # 条件②: 成交量放大
        if vol_ma[i] <= 0:
            continue
        cur_vol_ratio = volumes[i] / vol_ma[i]
        if cur_vol_ratio < vol_ratio:
            continue

        # 条件③: 当日收阳（涨幅>0%）
        today_gain = (closes[i] / closes[i - 1] - 1) * 100 if i >= 1 and closes[i - 1] > 0 else 0
        if today_gain <= 0:
            continue

        # 条件④: 上影线占比 < 30%（强势收盘，非冲高回落）
        day_range = highs[i] - lows[i]
        if day_range > 0:
            upper_shadow = highs[i] - closes[i]
            if upper_shadow / day_range > 0.3:
                continue

        # 条件⑤: 近5日涨幅在合理区间
        if i < 5 or closes[i - 5] <= 0:
            continue
        change_5d = (closes[i] / closes[i - 5] - 1) * 100
        if change_5d < min_change_pct or change_5d > max_change_pct:
            continue

        # 条件⑥: 趋势过滤（可选）
        if use_trend_filter and ema is not None:
            if closes[i] <= ema[i]:
                continue

        # 条件⑦⑧⑨: 股票质量过滤（量价关系评估）
        short_score, vp_corr, vol_cv = _calc_vol_price_quality(closes, volumes, i, lookback=20)
        if short_score < min_short_score:       # 近期必须上涨
            continue
        if vp_corr < min_vol_price_corr:        # 涨的日子量要大于跌的日子
            continue
        if vol_cv < min_vol_cv:                 # 成交量要有波动（非死水股）
            continue

        turnover = volumes[i] / circ_shares * 100 if circ_shares > 0 else 0

        # D+1开盘买
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
            'breakout_high': round(donchian_upper[i], 3),
            'signal_close': closes[i],
            'signal_vol_ratio': round(cur_vol_ratio, 3),
            'change_5d': round(change_5d, 2),
            'today_gain': round(today_gain, 2),
            'turnover': round(turnover, 2),
            'signal_volume': volumes[i],
            'short_score': round(short_score, 2),
            'vol_price_corr': round(vp_corr, 3),
            'vol_cv': round(vol_cv, 3),
        })

    # 同一天按优先级排序，每天最多选 top_per_day 个
    # 优先级: 突破幅度大 > 量比高 > 涨幅适中
    from collections import defaultdict
    daily_candidates = defaultdict(list)
    for c in candidates:
        daily_candidates[c['signal_date']].append(c)

    filtered = []
    for date, cands in daily_candidates.items():
        cands.sort(key=lambda c: (
            -c['signal_vol_ratio'],   # 量比越高越好
            -abs(c['today_gain']),    # 当日涨幅越大越好（突破力度）
            c['change_5d'],           # 5日涨幅越小越好（刚起步）
        ))
        filtered.extend(cands[:top_per_day])

    # 生成交易记录
    trades = []
    for c in filtered:
        result = run_backtest(bars, c['entry_idx'], c['entry_price'],
                              stop_loss_pct, trailing_pct, max_hold_days)
        if not result:
            continue

        trades.append({
            'code': code,
            'board': get_board_name(code),
            'path': 'vol_price_resonance',
            'path_label': '量价共振突破',
            'signal_date': c['signal_date'],
            'signal_close': c['signal_close'],
            'signal_vol_ratio': c['signal_vol_ratio'],
            'signal_change_5d': c['change_5d'],
            'signal_today_gain': c['today_gain'],
            'signal_turnover': c['turnover'],
            'signal_volume': c['signal_volume'],
            'breakout_high': c['breakout_high'],
            'short_score': c['short_score'],
            'vol_price_corr': c['vol_price_corr'],
            'vol_cv': c['vol_cv'],
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

def print_today_signals(trades, today_str):
    today = [t for t in trades if t['signal_date'] == today_str]
    if not today:
        print(f"\n  {today_str} 无信号")
        return []
    print(f"\n  {today_str} 信号: {len(today)}只")
    for t in sorted(today, key=lambda x: -x['signal_vol_ratio']):
        print(f"    {t['code']}({t['board']}) "
              f"突破{t['breakout_high']} 量比{t['signal_vol_ratio']:.1f} "
              f"5日涨{t['signal_change_5d']:.1f}% "
              f"→ 次日开盘买")
    return today

# ================================================================
# 主流程
# ================================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="量价共振突破策略 独立回测")
    parser.add_argument("--codes", default="", help="逗号分隔的股票代码")
    parser.add_argument("--days", type=int, default=300, help="加载K线天数 (默认300)")
    parser.add_argument("--source", choices=["manual", "db"], default="manual",
                        help="数据源: manual(默认), db")
    parser.add_argument("--breakout-period", type=int, default=20,
                        help="突破周期: 前N日最高价 (默认20)")
    parser.add_argument("--vol-ma-period", type=int, default=20,
                        help="成交量均线周期 (默认20)")
    parser.add_argument("--vol-ratio", type=float, default=2.0,
                        help="放量倍数阈值 (默认2.0)")
    parser.add_argument("--min-change-pct", type=float, default=2.0,
                        help="近5日涨幅下限%% (默认2.0)")
    parser.add_argument("--max-change-pct", type=float, default=9.5,
                        help="近5日涨幅上限%% (默认9.5)")
    parser.add_argument("--use-trend-filter", action="store_true",
                        help="启用趋势过滤 (价格在EMA上方)")
    parser.add_argument("--trend-ema-period", type=int, default=20,
                        help="趋势EMA周期 (默认20)")
    parser.add_argument("--stop-loss", type=float, default=5.0,
                        help="固定止损%% (默认5.0)")
    parser.add_argument("--trailing-pct", type=float, default=5.0,
                        help="跟踪止损回撤%% (默认5.0)")
    parser.add_argument("--max-hold", type=int, default=10,
                        help="最大持仓天数 (默认10)")
    parser.add_argument("--top-per-day", type=int, default=2,
                        help="每天最多选前N个最优信号 (默认2, 0=不过滤)")
    parser.add_argument("--min-short-score", type=float, default=0.0,
                        help="近20日涨幅下限%% (默认0.0, >0=排除下跌股)")
    parser.add_argument("--min-vol-price-corr", type=float, default=1.0,
                        help="量价同向度下限 (默认1.0, 涨时量>跌时量)")
    parser.add_argument("--min-vol-cv", type=float, default=0.0,
                        help="成交量变异系数下限 (默认0.0, >0排除死水股)")
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
    print(f"量价共振突破策略 独立回测")
    print(f"{'=' * 80}")
    print(f"入场条件:")
    print(f"  ① 当日收盘价 > 前{args.breakout_period}日最高价（收盘突破）")
    print(f"  ② 成交量 > {args.vol_ma_period}日均量 * {args.vol_ratio}（放量确认）")
    print(f"  ③ 当日收阳（涨幅>0%）")
    print(f"  ④ 上影线占比 < 30%（强势收盘）")
    print(f"  ⑤ 近5日涨幅 {args.min_change_pct}%~{args.max_change_pct}%")
    if args.use_trend_filter:
        print(f"  ④ 价格在 EMA({args.trend_ema_period}) 上方")
    print(f"出场条件:")
    print(f"  ① 固定止损: 买入价亏{args.stop_loss}%")
    print(f"  ② 跟踪止损: 峰值回撤{args.trailing_pct}%（盈利2%后激活）")
    print(f"  ③ 持仓上限: {args.max_hold}天")
    print(f"出场模式: stop_loss + trail_stop")
    print(f"买入模式: D+1开盘买")
    if args.top_per_day > 0:
        print(f"每日筛选: 同一天最多选前{args.top_per_day}个 (排序: 量比高>突破力度大>涨幅小)")
    else:
        print(f"每日筛选: 不过滤")
    if args.min_short_score > 0 or args.min_vol_price_corr > 1.0 or args.min_vol_cv > 0:
        print(f"股票质量过滤:")
        if args.min_short_score > 0:
            print(f"  近20日涨幅 > {args.min_short_score}%")
        if args.min_vol_price_corr > 1.0:
            print(f"  量价同向度 > {args.min_vol_price_corr} (涨时量>跌时量)")
        if args.min_vol_cv > 0:
            print(f"  成交量CV > {args.min_vol_cv} (排除死水股)")
    print(f"股票: {len(codes)}只\n")

    all_trades = []
    success = 0

    for i, code in enumerate(codes):
        bars = fetch_kline_db(code, args.days) if use_db else fetch_kline(code, args.days)
        if not bars:
            continue

        trades = strategy_vol_price_resonance(
            bars, code,
            breakout_period=args.breakout_period,
            vol_ma_period=args.vol_ma_period,
            vol_ratio=args.vol_ratio,
            min_change_pct=args.min_change_pct,
            max_change_pct=args.max_change_pct,
            use_trend_filter=args.use_trend_filter,
            trend_ema_period=args.trend_ema_period,
            stop_loss_pct=args.stop_loss,
            trailing_pct=args.trailing_pct,
            max_hold_days=args.max_hold,
            circ_shares=_get_circ_shares(code),
            top_per_day=args.top_per_day,
            min_short_score=args.min_short_score,
            min_vol_price_corr=args.min_vol_price_corr,
            min_vol_cv=args.min_vol_cv,
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

        # 按量比分段统计
        print(f"\n--- 量比分段统计 ---")
        for lo, hi in [(1.5, 2.0), (2.0, 2.5), (2.5, 3.0), (3.0, 5.0), (5.0, 100)]:
            seg = [t for t in all_trades if lo <= t['signal_vol_ratio'] < hi]
            if seg:
                print_stats(seg, f"量比[{lo},{hi})")

        # 按5日涨幅分段统计
        print(f"\n--- 5日涨幅分段统计 ---")
        for lo, hi in [(2, 4), (4, 6), (6, 8), (8, 10)]:
            seg = [t for t in all_trades if lo <= t.get('signal_change_5d', 0) < hi]
            if seg:
                print_stats(seg, f"5日涨幅[{lo},{hi})%")

        # TOP N
        n = min(args.top, len(all_trades))
        print(f"\n--- TOP {n} 最佳交易 ---")
        for t in sorted(all_trades, key=lambda x: -x['return_pct'])[:n]:
            print(f"  {t['code']}({t['board']}) {t['signal_date']} "
                  f"突破{t['breakout_high']} 量比{t['signal_vol_ratio']:.1f} "
                  f"→ {t['entry_date']}买{t['entry_price']:>7.2f} "
                  f"收益{t['return_pct']:+.2f}% 持仓{t['exit_day']}天")

        print(f"\n--- TOP {n} 最差交易 ---")
        for t in sorted(all_trades, key=lambda x: x['return_pct'])[:n]:
            print(f"  {t['code']}({t['board']}) {t['signal_date']} "
                  f"突破{t['breakout_high']} 量比{t['signal_vol_ratio']:.1f} "
                  f"→ {t['entry_date']}买{t['entry_price']:>7.2f} "
                  f"收益{t['return_pct']:+.2f}% 持仓{t['exit_day']}天")

    # 今日信号
    if args.today:
        today_str = args.today_date if args.today_date else datetime.now().strftime("%Y-%m-%d")
        today_trades = print_today_signals(all_trades, today_str)
        if today_trades:
            out_file = f"test_vol_price_resonance_today_{today_str}.json"
            with open(out_file, 'w', encoding='utf-8') as f:
                json.dump(today_trades, f, ensure_ascii=False, indent=2)
            print(f"\n  今日信号已保存: {out_file}")

    # 全部交易明细
    if args.all_trades and all_trades:
        print(f"\n--- 全部交易明细 ---")
        for t in sorted(all_trades, key=lambda x: x['entry_date']):
            print(f"  {t['code']}({t['board']}) {t['signal_date']} "
                  f"量比{t['signal_vol_ratio']:.1f} "
                  f"→ {t['entry_date']}买{t['entry_price']:>7.2f} "
                  f"→ {t['exit_price']:>7.2f} "
                  f"收益{t['return_pct']:+.2f}% 持仓{t['exit_day']}天 [{t['exit_reason']}]")

    # 保存JSON
    if all_trades:
        out_file = "test_vol_price_resonance_result.json"
        with open(out_file, 'w', encoding='utf-8') as f:
            json.dump(all_trades, f, ensure_ascii=False, indent=2)
        print(f"\n  {out_file} ({len(all_trades)}笔)")

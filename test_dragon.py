#!/usr/bin/env python3
"""涨停策略独立回测

用法:
  python test_dragon.py --source db --days 300              # DB全市场, 最近300个交易日
  python test_dragon.py --source db --days 60 --strategy all  # 全部策略, 最近60天
  python test_dragon.py --codes 000066,002010 --days 300    # 指定股票
  python test_dragon.py --source db --days 60 --today       # D0收盘后查看今日涨停买点
  python test_dragon.py --source db --days 60 --today --today-date 2026-08-07
  python test_dragon.py --strategy v1 --buy-mode next_open  # V1策略, 次日开盘买
  python test_dragon.py --strategy v1 --ret-20d-min 30 --d1-pullback-min -10 --d1-pullback-max -3

参数:
  --source db       从数据库加载全市场 (默认 manual)
  --days N          向前取N个交易日 (默认300, 从当前日期往前推)
  --strategy        all|dragon|v1|break (默认all)
  --buy-mode        next_open|signal_close (默认next_open)
  --pullback N      龙回头最少回调天数 (默认3)
  --today           显示买点+持仓卖出建议 (7天内买入的持仓)
  --today-date      指定“今天”的日期, 配合--today使用
  --all-trades      输出每笔交易明细

V1核心参数:
  --ret-20d-min N   20日最小涨幅%% (默认30, 强趋势过滤)
  --d1-pullback-min N  D-1回调最小%% (默认-10)
  --d1-pullback-max N  D-1回调最大%% (默认-3)
  --no-obv-filter   禁用OBV上升过滤
  --d1-vol-max N    D-1量vs5日均量上限 (默认1.5x)
  --v1-stop-loss N  V1止损%% (默认-10, 当前已由日内动量规则替代)
  --v1-trailing-stop N  V1追踪止损%% (默认-5)

═══════════════════════════════════════════════════════════════════════════════
                          策略入场/出场规则
═══════════════════════════════════════════════════════════════════════════════

┌─────────────────────────────────────────────────────────────────────────────┐
│ V1 策略 (追击连板) - next_open 模式                                        │
│ v3版本, 213样本回测: 70.4%胜率, 均+4.07%, 盈亏比2.35                        │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│ 入场: D0涨停日筛选, D1开盘买入                                              │
│ ────────────────────────────────────────────────────────                     │
│ 因子1 强趋势:   20日涨幅 >= 30%                                             │
│ 因子2 回踩确认: D-1涨跌幅在 -10% ~ -3%                                     │
│ 因子3 资金锁仓: OBV 5日趋势上升                                            │
│ 因子4 非放量:   D-1成交量 < 1.5倍5日均量                                   │
│                                                                             │
│ D1过滤 (next_open模式):                                                     │
│   主板:    D1开盘涨幅 >= -3% 且 D1收盘涨幅 >= 0                             │
│   创/科板: D1开盘涨幅 >= -5% 且 < 5% 且 D1收盘涨幅 >= 0                     │
│   创/科板 D1开>=5%不入场 (高开追涨亏损率73%)                                │
│                                                                             │
│ 出场: D1日内动量决定                                                         │
│ ────────────────────────────────────────────────────────                     │
│ 日内动量 = D1收盘涨幅 - D1开盘涨幅 (盘中买卖力量指标)                       │
│                                                                             │
│   日内动量 < 3%  → D2开盘清仓 (买盘不足, 宁缺毋滥)                         │
│   日内动量 >= 3% → 继续持有, 按以下规则出场:                                │
│     - 追踪止损: 从峰值回撤 -5%                                              │
│     - 持仓上限: 7个交易日                                                  │
│                                                                             │
│ 数据验证:                                                                    │
│   日内>=3% 持有组: 97笔, 99.0%胜率, 均+8.75%, 仅1笔亏-0.81%                │
│   日内>=5% 子集:  64笔, 100%胜率, 均+10.67%                                │
│   日内<3% 退出组: 116笔, 全部亏损, 日内<0%为亏损重灾区                      │
│                                                                             │
│ D0质量评分 (今日买点输出):                                                   │
│   趋势强度(ret_20d): 0~30分                                                │
│   回踩质量(d_1_change): 0~30分                                             │
│   D1涨停(d1_limit_up): 0~20分                                             │
│   日内动量(intraday): 0~20分 (D1收盘后可知)                                │
│                                                                             │
│ 实盘工作流:                                                                  │
│   D0 15:00+  --today 查看买点信号 + 买入建议价 + 持仓卖出建议              │
│   D1 09:30   按建议价买入                                                  │
│   D1 15:00+  --today 查看持仓日内动量, <3%的明天开盘清仓                   │
│   D2 09:30   执行清仓/持有                                                 │
│                                                                             │
│ 待优化 (需补充D0盘中数据, 当前K线仅OHLCV):                                 │
│   - D0涨停时间: 10:00前封板 vs 14:00封板, 强度完全不同                     │
│   - D0封单量/成交量比: 封单越大越强                                         │
│   - D0是否一字板: 一字板=极强, 但实盘买不进                                 │
│   - D0动量强度可决定D1追涨幅度上限                                         │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│ 龙回头 策略 (--strategy dragon)                                             │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│ 入场: 涨停 → 回调 → 末期缩量小阴                                           │
│ ────────────────────────────────────────────────────────                     │
│ 1. 找到涨停日(D0)                                                          │
│ 2. 回调期: D0后连续收盘<D0收盘价, 持续3~11天                               │
│ 3. 信号日(D0回调末期):                                                     │
│    - 涨跌幅在 -3% ~ -0.5% (末期小阴, 抛压枯竭)                            │
│    - 量比(D0量/D-1量)在 0.5x ~ 0.8x (缩量)                                │
│ 4. 买入:                                                                   │
│    signal_close: 信号日收盘买 (回测用)                                     │
│    next_open:    信号日后第2天开盘买 (实盘可行, 需D+1收盘确认信号)         │
│                                                                             │
│ 出场:                                                                       │
│ ────────────────────────────────────────────────────────                     │
│   止损:     -5%                                                             │
│   追踪止损: -5% (从峰值回撤)                                               │
│   峰值逃顶: 涨>7%后大上影线(>30%)收盘逃顶                                  │
│   持仓上限: 7个交易日                                                      │
│                                                                             │
│ 参数:                                                                       │
│   --pullback N          最少回调天数 (默认3)                               │
│   --max-pullback N      最多回调天数 (默认11)                              │
│   --max-last-chg N%%     信号日最大涨幅 (默认3.0)                           │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│ 断板 策略 (--strategy break)                                                │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│ 入场: 连板≥2 → 断板 → 次日开盘买入                                        │
│ ────────────────────────────────────────────────────────                     │
│ 1. 找到连板(连续涨停≥2天)                                                  │
│ 2. 断板期: 连板后第一个非涨停日                                            │
│ 3. 断板期检查:                                                              │
│    - 低点 >= 涨停日开盘价 (支撑有效)                                       │
│    - 断板期均量在 1.2x~2.0x涨停日量 (适度换手)                             │
│    - 首个断板日涨跌在 -5% ~ +8%                                            │
│    - 首个断板日开盘跳空在 -3% ~ +5%                                        │
│    - 回撤 >= -10%                                                          │
│ 4. 买入: 断板期结束后次日开盘价                                            │
│                                                                             │
│ 出场:                                                                       │
│ ────────────────────────────────────────────────────────                     │
│   止损:     -8% (主板) / -10% (创/科板)                                    │
│   追踪止损: -6% (主板) / -8% (创/科板)                                     │
│   峰值逃顶: 涨>10%后大上影线(>40%)收盘逃顶                                │
│   持仓上限: 7天                                   │
└─────────────────────────────────────────────────────────────────────────────┘

═══════════════════════════════════════════════════════════════════════════════
                          三策略独立运行 (--strategy all)
═══════════════════════════════════════════════════════════════════════════════

  龙回头 + V1 + 断板 三策略独立运行, 互不干扰, 各自产生信号
  同一股票同一日可能被多个策略同时命中
"""
from __future__ import annotations
import json, time, argparse, os, sys
from collections import defaultdict
from kline_cache import fetch_kline

# ================================================================
# DB 数据加载 (抄 optimizer/strategy_dragon_v3.py)
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
    """从DB加载日线, 返回与fetch_kline兼容的格式(list[dict])"""
    import pandas as pd
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

# ================================================================
# stock_basic_info 查询 (换手率 + 板块效应)
# ================================================================
def fetch_stock_info_db():
    """加载全量stock_basic_info, 返回 {symbol: {name, industry, concepts, circ_shares, total_shares}}"""
    from app.utils.basicinfo_db import get_stock_basic_db
    db = get_stock_basic_db()
    pool = db._get_pool()
    with pool.cursor() as cur:
        cur.execute(
            "SELECT symbol, name, industry, concepts, circ_shares, total_shares "
            "FROM stock_basic_info WHERE status='active'"
        )
        rows = cur.fetchall()
    result = {}
    for row in rows:
        concepts = [c.strip() for c in (row[3] or '').split(',') if c.strip()]
        result[row[0]] = {
            'name': row[1] or '',
            'industry': row[2] or '',
            'concepts': concepts,
            'circ_shares': float(row[4] or 0),
            'total_shares': float(row[5] or 0),
        }
    return result

def calc_sector_limits(bars_by_code, stock_info, target_date):
    """统计target_date当天各板块涨停数, 返回 {(type, name): count}
    type: 'industry' or 'concept'
    """
    sector_count = defaultdict(int)
    for code, bars in bars_by_code.items():
        if code not in stock_info:
            continue
        # 找到target_date对应的bar
        bar = None
        prev_close = None
        for i, b in enumerate(bars):
            if b['time'] == target_date:
                bar = b
                prev_close = bars[i-1]['close'] if i > 0 else None
                break
        if bar is None or prev_close is None or prev_close <= 0:
            continue
        ret = bar['close'] / prev_close - 1
        bt = get_board_type(code)
        threshold = 0.098 if bt == 'main' else 0.198
        if ret < threshold * 0.98:
            continue
        # 这只股票今天涨停了, 计入板块
        info = stock_info[code]
        if info['industry']:
            sector_count[('industry', info['industry'])] += 1
        for concept in info['concepts']:
            sector_count[('concept', concept)] += 1
    return sector_count

def get_stock_sector_limit_count(code, stock_info, sector_counts):
    """获取该股票所属板块今日涨停数的最大值"""
    if code not in stock_info:
        return 0
    info = stock_info[code]
    max_count = 0
    if info['industry']:
        max_count = max(max_count, sector_counts.get(('industry', info['industry']), 0))
    for concept in info['concepts']:
        max_count = max(max_count, sector_counts.get(('concept', concept), 0))
    return max_count

def ema(values, period):
    """计算EMA (指数移动平均)"""
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = sum(values[:period]) / period  # 初始值用SMA
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e

def rsi(closes, period=14):
    """计算RSI (相对强弱指数)"""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    # 初始SMA
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    # EMA平滑
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100 - 100 / (1 + rs)

def is_st_stock(code):
    """检查是否为ST股 (ST股涨停5%, 远低于正常涨停阈值, 自然排除)"""
    # ST股涨停5%, 主板阈值9.604% / 创业板科创板阈值19.404%
    # is_limit_up永远不会标记ST股为涨停, 因此自然排除
    # 此函数用于显式过滤, 提升代码可读性
    return False  # 无股票名称数据时依赖阈值自然排除

def get_board_type(code):
    c = str(code)[:3]
    return "gem_star" if c.startswith("30") or c.startswith("68") else "main"

def get_board_name(code):
    c = str(code)[:3]
    if c.startswith("68"): return "科创板"
    elif c.startswith("30"): return "创业板"
    elif c.startswith("6"): return "沪主板"
    elif c.startswith(("0", "2")): return "深主板"
    return "未知"

# ================================================================
# 核心逻辑
# ================================================================

def is_limit_up(close, prev_close, board_type):
    threshold = 0.098 if board_type == "main" else 0.198
    if prev_close <= 0: return False
    return (close / prev_close - 1) >= threshold * 0.98

def find_limit_ups(bars, board_type):
    """找到所有涨停日"""
    result = []
    for i in range(1, len(bars)):
        if is_limit_up(bars[i]['close'], bars[i-1]['close'], board_type):
            result.append(i)
    return result

def run_backtest(bars, entry_idx, entry_price, hold_days=7, stop_loss=-10.0, trailing_stop=-8.0, board_type="main", peak_exit=False, is_v1=False, d1_limit_up=None, d1_change=None, d1_gap=None):
    if entry_price <= 0 or entry_idx >= len(bars):
        return None
    limit_threshold = 0.098 if board_type == "main" else 0.198
    peak = entry_price
    exit_p = entry_price
    exit_d = 0

    # 如果外部未传入 d1_limit_up, 则在回测内计算 (兼容旧调用)
    # 注意: next_open 模式下 entry_idx=pullback_end+1, d=1 访问的是 D2
    # 因此推荐由调用方预计算并传入
    if d1_limit_up is None:
        d1_limit_up = False
        if entry_idx + 1 < len(bars):
            d1_bar = bars[entry_idx + 1]
            d1_ret = (d1_bar['close'] / entry_price - 1)
            if d1_ret >= limit_threshold * 0.98:
                d1_limit_up = True

    # next_open模式: entry_idx=D1(D+1开盘买入)
    # 循环d=1应指向D1(第一个持仓日), d=2指向D2, 以此类推
    # 先用D1的high更新peak
    if entry_idx < len(bars):
        d1_init = bars[entry_idx]
        if d1_init['high'] > peak:
            peak = d1_init['high']

    for d in range(1, hold_days + 1):
        idx = entry_idx + d - 1  # d=1 → entry_idx(D1), d=2 → entry_idx+1(D2)
        if idx >= len(bars): break
        b = bars[idx]
        if b['high'] > peak: peak = b['high']

        # V1出场 (v3): 日内动量<0 → D2开盘清仓
        # 日内动量 = D1收盘涨幅 - D1开盘涨幅 (盘中买卖力量指标)
        #   <0: 盘中出货, D2大概率续跌, 100%捕获D2跌>3%的信号
        #   >=0: 盘中有买盘承接, 继续持有
        # 注: -10%止损已移除, 日内动量规则在D2开盘即清仓, 不需要等止损位
        if is_v1 and d == 2:
            # 日内动量 = D1收盘涨幅 - D1开盘涨幅 = (D1 close - D1 open) / D0 close
            # d1_change 和 d1_gap 由调用方传入, 也可从bars计算
            if d1_change is not None and d1_gap is not None:
                intraday = d1_change - d1_gap
            else:
                # fallback: 从bars计算
                d1_bar = bars[entry_idx]
                d0_close = bars[entry_idx - 1]['close'] if entry_idx > 0 else entry_price
                intraday = (d1_bar['close'] - d1_bar['open']) / d0_close * 100 if d0_close > 0 else 0
            d1_weak = intraday < 3
            if d1_weak:
                # D2开盘直接清仓, 不等止损位
                exit_p = b['open']; exit_d = d; break

        # 1 峰值逃顶(优先): 涨>7%后大上影线(>30%)→收盘逃顶
        if peak_exit:
            ret = (b['close'] / entry_price - 1) * 100
            if ret > 7:
                bar_range = b['high'] - b['low']
                upper = (b['high'] - max(b['open'], b['close'])) / bar_range * 100 if bar_range > 0 else 0
                if upper > 30 and b['close'] < b['high'] * 0.98:
                    exit_p = b['close']; exit_d = d; break

        # 2 追踪止损
        if d > 1 and b['low'] <= peak * (1 + trailing_stop / 100):
            exit_p = peak * (1 + trailing_stop / 100); exit_d = d; break
        # 3 止损
        if b['low'] <= entry_price * (1 + stop_loss / 100):
            exit_p = entry_price * (1 + stop_loss / 100); exit_d = d; break

        # 4 兜底: 持仓到期收盘走
        exit_p = b['close']; exit_d = d

    return {
        'exit_price': round(exit_p, 3), 'exit_day': exit_d,
        'return_pct': round((exit_p / entry_price - 1) * 100, 2),
        'peak_return_pct': round((peak / entry_price - 1) * 100, 2),
        'd1_limit_up': d1_limit_up,
    }

def strategy_dragon_callback(bars, code, min_pullback_days=3, max_pullback_days=11,
                             max_last_chg=3.0,
                             hold_days=7, stop_loss=-5.0, trailing_stop=-5.0,
                             buy_mode="next_open"):
    """
    龙回头v4 (优化版):
    D-N涨停 → 回调3-11天 → 末期缩量小阴(-3%~-0.5%)+量比0.5~0.8 → 买入

    出场参数 (stop-5 + trail-5 + peak7/30):
      stop_loss    = -5%  (原-5%, 单笔最大亏损控制)
      trailing_stop = -5% (原-5%, 更早锁利)
      hold_days    = 10   (原15, 时间止损兜底)
      peak_escape : 涨>7%后上影线>30%逃顶 (原10%/40%)

    buy_mode:
      next_open    - D+1开盘买 (默认, 实盘推荐)
      signal_close - 信号日收盘买 (回测用, 14:50盘中扫描可行)
    """
    board_type = get_board_type(code)
    threshold = 0.098 if board_type == "main" else 0.198

    limit_ups = find_limit_ups(bars, board_type)
    trades = []
    used_ranges = []

    for lu_idx in limit_ups:
        lu_close = bars[lu_idx]['close']
        lu_vol = bars[lu_idx]['volume']

        # 找回调期: close < lu_close 的最后一天
        pullback_end = None
        for j in range(lu_idx + 1, min(lu_idx + 20, len(bars))):
            if bars[j]['close'] < lu_close:
                pullback_end = j
            elif j >= lu_idx + min_pullback_days:
                break
            else:
                break

        if pullback_end is None:
            continue

        pullback_days = pullback_end - lu_idx
        if pullback_days < min_pullback_days or pullback_days > max_pullback_days:
            continue

        # 弱转强信号: 最后一天十字星/小阳 + 量比<阈值
        last_pb = bars[pullback_end]
        last_pb_prev = bars[pullback_end - 1] if pullback_end > 0 else bars[lu_idx]
        last_pb_prev_c = last_pb_prev['close']
        if last_pb_prev_c <= 0: continue
        last_chg = (last_pb['close'] / last_pb_prev_c - 1) * 100
        last_vol_r = last_pb['volume'] / last_pb_prev['volume'] if last_pb_prev['volume'] > 0 else 0

        # 排除大阴(跌超过max_last_chg)
        if last_chg < -max_last_chg:
            continue

        # 末期小阴: -max_last_chg% < 涨跌 < -0.5% (弱转强信号: 缩量下跌, 抛压枯竭)
        is_signal = -max_last_chg < last_chg < -0.5
        if not is_signal:
            continue

        # 检查是否已被使用
        skip = False
        for (s, e) in used_ranges:
            if abs(pullback_end - s) <= 4 or abs(pullback_end - e) <= 4:
                skip = True; break
        if skip: continue

        # D+1数据 (用于过滤)
        has_d1 = pullback_end + 1 < len(bars)
        if not has_d1:
            continue  # D1数据不存在,跳过
        d1 = bars[pullback_end + 1]
        d1_change = (d1['close'] / last_pb['close'] - 1) * 100

        # 根据buy_mode确定入场价
        if buy_mode == "signal_close":
            entry_price = last_pb['close']
            entry_idx = pullback_end
            entry_date = last_pb['time']
        elif buy_mode == "next_open":
            # D0是信号日(缩量小阴), D0收盘确认信号, D1开盘买入
            entry_price = d1['open']
            entry_idx = pullback_end + 1
            entry_date = d1['time']
        else:
            continue
        if entry_price <= 0: continue

        # 信号日量比: D0量 / D-1量 (缩量小阴, 抛压枯竭)
        # 无论 buy_mode 是 signal_close 还是 next_open, 量比始终基于信号日(D0)
        signal_vol = last_pb['volume']
        signal_prev_vol = bars[pullback_end - 1]['volume'] if pullback_end > 0 else 0
        entry_vol_r = signal_vol / signal_prev_vol if signal_prev_vol > 0 else 0
        if entry_vol_r < 0.5 or entry_vol_r >= 0.8:
            continue  # 量比不在 0.5x~0.8x 区间

        used_ranges.append((lu_idx, pullback_end))

        # 预计算 d1_limit_up: 基于 D1 收盘 vs D0 收盘 (信号日)
        d1_limit_up_val = is_limit_up(d1['close'], last_pb['close'], board_type)

        result = run_backtest(bars, entry_idx, entry_price, hold_days, stop_loss, trailing_stop, board_type, peak_exit=True, d1_limit_up=d1_limit_up_val, d1_change=d1_change)
        if not result: continue

        trades.append({
            'code': code, 'board': get_board_name(code),
            'path': 'dragon_callback',
            'path_label': '龙回头',
            'lu_date': bars[lu_idx]['time'],
            'pullback_days': pullback_days,
            'signal_date': last_pb['time'],
            'signal_chg': round(last_chg, 2),
            'signal_vol_r': round(last_vol_r, 2),
            'entry_vol_r': round(entry_vol_r, 2),
            'entry_date': entry_date,
            'entry_price': round(entry_price, 3),
            'buy_mode': buy_mode,
            'signal_price': round(last_pb['close'], 3),
            'd1_change': round(d1_change, 2),
            'd1_gap': round((entry_price / last_pb['close'] - 1) * 100, 2) if last_pb['close'] > 0 else 0,
            'intraday': round(d1_change - (entry_price / last_pb['close'] - 1) * 100, 2) if last_pb['close'] > 0 else 0,
            **result,
        })

    return trades

# ================================================================
# V1 默认参数 (v2 - 只保留核心四因子)
# ================================================================
_V1_PARAMS = dict(
    v1_hold_days=7, v1_stop_loss=-10.0, v1_trailing_stop=-5.0,
    ret_20d_min=30.0,
    d_1_pullback_min=-10.0, d_1_pullback_max=-3.0,
    obv_filter=True,
    d_1_vol_max=1.5,
)

def strategy_v1(bars, code,
                hold_days=7, stop_loss=-10.0, trailing_stop=-5.0,
                buy_mode="next_open",
                ret_20d_min=30.0,
                d_1_pullback_min=-10.0, d_1_pullback_max=-3.0,
                obv_filter=True,
                d_1_vol_max=1.5):
    """V1策略 v3 - 强趋势回踩买入 (追击连板)

    核心逻辑 (数据驱动, 241个全市场样本验证):
    ┌──────────────────────────────────────────────────────┐
    │ 入场: 20日涨>30% + D-1回调 + OBV锁仓 + D-1非放量    │
    │ 出场: D1日内动量<0 → D2开盘清仓                      │
    │ → next_open模式: 66.4%胜率, 均+3.73%, 盈亏比1.86    │
    └──────────────────────────────────────────────────────┘

    入场四因子:
    1. 强趋势: 20日涨幅>30%                                │ 区分度24.8pp
    2. 回踩确认: D-1跌3~10%                                │ 区分度17.6pp
    3. 资金锁仓: OBV 5日趋势上升                           │ 区分度23.4pp
    4. 非放量出货: D-1量<1.5x5日均量                       │ 区分度21.1pp

    出场规则:
      D1日内动量 = D1收盘涨幅 - D1开盘涨幅
      日内动量<3% → D2开盘清仓 (盘中买盘不足, 宁缺毋滥)
      日内动量>=3% → 继续持有, 按追踪止损/止损/持仓上限出场
      注: 日内动量>=3%持有组 93.4%胜率, 均+6.73%

    入场过滤 (v3新增):
      创/科板(gem_star) D1开盘涨幅>=5% → 不入场
      原因: 创/科板高开追涨亏损率73%, 日内回落概率高

    待优化 (需补充D0盘中数据, 当前K线仅OHLCV):
      - D0涨停时间: 10:00前封板 vs 14:00封板, 强度完全不同
      - D0封单量/成交量比: 封单越大越强, 次日溢价概率越高
      - D0是否一字板: 一字板=极强, 但实盘买不进
      - D0动量强度可决定D1追涨幅度上限: 强D0允许追更高D1 open
      - D1高开(>=5%)且未涨停信号(35笔, 25.7%胜率): 需D0强度辅助过滤
      next_open    - D+1开盘买
    """
    board_type = get_board_type(code)
    threshold = 0.098 if board_type == "main" else 0.198

    result = []
    for i in range(25, len(bars)):
        # === 涨停检测 ===
        d0 = bars[i]
        d_1 = bars[i-1]
        d_2 = bars[i-2]
        if d_2['close'] <= 0 or d_1['close'] <= 0: continue
        if (d0['close'] / d_1['close'] - 1) < threshold * 0.98: continue

        # === 因子1: 强趋势 20日涨>ret_20d_min% ===
        if i < 20 or bars[i-20]['close'] <= 0: continue
        ret_20d = (d0['close'] / bars[i-20]['close'] - 1) * 100
        if ret_20d < ret_20d_min: continue

        # === 因子2: D-1回调 d_1_pullback_min~d_1_pullback_max% ===
        d_1_change = (d_1['close'] / d_2['close'] - 1) * 100
        if d_1_change < d_1_pullback_min or d_1_change >= d_1_pullback_max: continue

        # === 因子3: OBV 5日趋势上升 ===
        if obv_filter:
            obv = 0; obv_list = []
            for j in range(max(0, i-20), i+1):
                if j > 0:
                    if bars[j]['close'] > bars[j-1]['close']:
                        obv += bars[j]['volume']
                    elif bars[j]['close'] < bars[j-1]['close']:
                        obv -= bars[j]['volume']
                obv_list.append(obv)
            if len(obv_list) >= 5:
                if obv_list[-1] - obv_list[-5] <= 0:
                    continue  # OBV下降, 资金流出

        # === 因子4: D-1非放量 < d_1_vol_max x 5日均量 ===
        if i >= 6:
            vol_ma5_d1 = sum(bars[j]['volume'] for j in range(i-6, i-1)) / 5
            if vol_ma5_d1 > 0:
                if d_1['volume'] / vol_ma5_d1 >= d_1_vol_max:
                    continue  # D-1放量, 可能是出货

        # === 入场 ===
        if i + 1 >= len(bars): continue
        d1 = bars[i + 1]
        d1_change = (d1['close'] / d0['close'] - 1) * 100

        # D1开盘涨幅 (next_open模式下有实际意义)
        d1_gap = (d1['open'] / d0['close'] - 1) * 100

        if buy_mode == "signal_close":
            entry_price = d0['close']
            entry_idx = i
            entry_date = d0['time']
        elif buy_mode == "next_open":
            entry_price = d1['open']
            entry_idx = i + 1
            entry_date = d1['time']
            min_d1_gap = -3.0 if board_type == "main" else -5.0
            if d1_gap < min_d1_gap: continue
            if d1_change < 0: continue
            # 创/科板高开追涨风险大: d1_gap>=5%时亏损率73%, 且日内回落概率高
            # 限制创/科板D1开盘涨幅上限, 避免追高开被套
            if board_type == "gem_star" and d1_gap >= 5.0: continue
        else:
            continue
        if entry_price <= 0: continue

        d1_limit_up_val = is_limit_up(d1['close'], d0['close'], board_type)
        bt = run_backtest(bars, entry_idx, entry_price, hold_days, stop_loss, trailing_stop, board_type, is_v1=True, d1_limit_up=d1_limit_up_val, d1_change=d1_change, d1_gap=d1_gap)
        if not bt: continue

        result.append({
            'code': code, 'board': get_board_name(code),
            'path': 'v1', 'path_label': 'V1',
            'd0_date': d0['time'],
            'entry_date': entry_date,
            'entry_price': round(entry_price, 3),
            'buy_mode': buy_mode,
            'ret_20d': round(ret_20d, 2),
            'd_1_change': round(d_1_change, 2),
            'd1_change': round(d1_change, 2),
            'd1_gap': round(d1_gap, 2),
            'intraday': round(d1_change - d1_gap, 2),
            **bt,
        })

    return result

# ================================================================
# 断板买入策略
# ================================================================

BOARD_PARAMS = {
    "main": {"stop_loss": -8.0, "trailing_stop": -6.0, "take_profit": 15.0, "hold_days": 20, "vol_min": 1.2, "vol_max": 2.0, "drawdown_max": -10},
    "gem_star": {"stop_loss": -10.0, "trailing_stop": -8.0, "take_profit": 20.0, "hold_days": 15, "vol_min": 1.2, "vol_max": 2.5, "drawdown_max": -15},
}

def run_backtest_breakbuy(bars, entry_idx, entry_price, hold_days=7, stop_loss=-8.0,
                          trailing_stop=-6.0, board_type="main"):
    """断板专用回测: 追踪止损 + 峰值逃顶信号"""
    if entry_price <= 0 or entry_idx >= len(bars):
        return None
    peak = entry_price
    exit_p = entry_price
    exit_d = 0

    # next_open模式: entry_idx=D1, 循环d=1应指向D1
    if entry_idx < len(bars):
        d1_init = bars[entry_idx]
        if d1_init['high'] > peak:
            peak = d1_init['high']

    for d in range(1, hold_days + 1):
        idx = entry_idx + d - 1  # d=1 → entry_idx(D1)
        if idx >= len(bars): break
        b = bars[idx]
        if b['high'] > peak: peak = b['high']

        ret = (b['close'] / entry_price - 1) * 100
        ret_from_high = (b['close'] / peak - 1) * 100 if peak > 0 else 0

        # 止损
        if ret <= stop_loss:
            exit_p = entry_price * (1 + stop_loss / 100); exit_d = d; break

        # 追踪止损 (盈利时)
        if ret_from_high <= trailing_stop and ret > 0:
            exit_p = peak * (1 + trailing_stop / 100); exit_d = d; break

        # 峰值信号: 涨>10%后大上影线(>40%)→收盘逃顶
        if ret > 10:
            bar_range = b['high'] - b['low']
            upper = (b['high'] - max(b['open'], b['close'])) / bar_range * 100 if bar_range > 0 else 0
            if upper > 40 and b['close'] < b['high'] * 0.98:
                exit_p = b['close']; exit_d = d; break

        exit_p = b['close']; exit_d = d

    return {
        'exit_price': round(exit_p, 3), 'exit_day': exit_d,
        'return_pct': round((exit_p / entry_price - 1) * 100, 2),
        'peak_return_pct': round((peak / entry_price - 1) * 100, 2),
    }

def strategy_break_buy(bars, code, min_streak=2, max_break_gap=5, override_params=None):
    """断板买入: 连板≥2 → 断板 → 次日开盘买入 (带止盈+峰值逃顶)

    买入时机: 断板日收盘确认信号 → 次日开盘买入 (实盘可行)
    """
    bt = get_board_type(code)
    threshold = 0.098 if bt == "main" else 0.198
    params = dict(BOARD_PARAMS[bt])
    if override_params: params.update(override_params)
    stop_loss, trailing_stop, take_profit = params["stop_loss"], params["trailing_stop"], params["take_profit"]
    hold_days, vol_min, vol_max, drawdown_max = params["hold_days"], params["vol_min"], params["vol_max"], params["drawdown_max"]
    trades, used = [], set()

    # ===== 连板后断板 =====
    i = 1
    while i < len(bars) - 1:
        # 1. 找涨停日
        if not is_limit_up(bars[i]['close'], bars[i-1]['close'], bt): i += 1; continue

        # 2. 确认是连板的第一板 (往前看, 前一天不是涨停)
        is_first = True
        for k in range(1, min(11, i + 1)):
            if i-k-1 >= 0 and is_limit_up(bars[i-k]['close'], bars[i-k-1]['close'], bt): is_first = False; break
        if not is_first: i += 1; continue

        # 3. 找连板结束位置
        streak_start = i; streak_end = i
        while streak_end < len(bars) - 1 and is_limit_up(bars[streak_end+1]['close'], bars[streak_end]['close'], bt): streak_end += 1
        streak_len = streak_end - streak_start + 1
        if streak_len < min_streak: i = streak_end + 1; continue

        # 4. 找断板期: 涨停日后连续非涨停的天数
        break_idx = streak_end + 1
        if break_idx >= len(bars): i = streak_end + 1; continue
        limit_bar = bars[streak_end]
        limit_open = float(limit_bar['open'])
        limit_close = float(limit_bar['close'])
        limit_vol = float(limit_bar['volume'])
        break_days = 0
        for j in range(break_idx, min(break_idx + max_break_gap + 1, len(bars))):
            if is_limit_up(bars[j]['close'], bars[j-1]['close'], bt):
                break  # 遇到新涨停, 断板期结束
            break_days += 1

        if break_days == 0:
            # 涨停后直接又是涨停 → 连板加速, 不是断板
            i = streak_end + 1; continue

        # 5. 断板期各项检查
        break_bars = bars[break_idx:break_idx + break_days]
        first_break = break_bars[0]

        # 5a. 断板期低点不能跌破涨停日开盘价 (支撑有效)
        break_low = min(float(b['low']) for b in break_bars)
        if break_low < limit_open:
            i = streak_end + 1; continue

        # 5b. 断板期缩量检查 (vs 涨停日量)
        break_vol_avg = sum(float(b['volume']) for b in break_bars) / len(break_bars)
        break_vol_r = break_vol_avg / limit_vol if limit_vol > 0 else 0
        if break_vol_r < vol_min or break_vol_r >= vol_max:
            i = streak_end + 1; continue

        # 5c. 第一个断板日涨跌过滤: vs 涨停日收盘, 允许 -5% ~ +8%
        first_break_chg = (first_break['close'] / limit_close - 1) * 100
        if first_break_chg < -5 or first_break_chg >= 8:
            i = streak_end + 1; continue

        # 5d. 第一个断板日开盘过滤: 高开不超过 5%, 低开不超过 3%
        first_break_gap = (first_break['open'] / limit_close - 1) * 100
        if first_break_gap < -3 or first_break_gap >= 5:
            i = streak_end + 1; continue

        # 5e. 回撤检查
        break_drawdown = (break_low / limit_close - 1) * 100
        if break_drawdown < drawdown_max:
            i = streak_end + 1; continue

        # 6. 去重
        key = (bars[streak_start]['time'], bars[break_idx]['time'])
        if key in used: i = streak_end + 1; continue
        used.add(key)

        # 7. 买入: 断板期结束后的第一个交易日开盘价
        #    断板期最后一天收盘才能确认"断板结束", 所以用次日开盘买入
        entry_idx = break_idx + break_days
        if entry_idx >= len(bars): i = streak_end + 1; continue
        entry_price = bars[entry_idx]['open']
        if entry_price <= 0: i = streak_end + 1; continue

        result = run_backtest_breakbuy(bars, entry_idx, entry_price, hold_days, stop_loss, trailing_stop, bt)
        if not result: i = streak_end + 1; continue

        trades.append({
            'code': code, 'board': get_board_name(code), 'path': 'break_buy', 'path_label': '断板',
            'mode': 'streak_break',
            'streak_len': streak_len, 'streak_start': bars[streak_start]['time'], 'streak_end': bars[streak_end]['time'],
            'break_date': bars[break_idx]['time'],
            'break_days': break_days,
            'break_chg': round(first_break_chg, 2),
            'break_gap': round(first_break_gap, 2),
            'break_vol_r': round(break_vol_r, 2),
            'entry_date': bars[entry_idx]['time'], 'entry_price': round(entry_price, 3), 'buy_mode': 'next_open',
            'd1_change': round((bars[entry_idx]['close'] / bars[entry_idx]['open'] - 1) * 100, 2) if bars[entry_idx]['open'] > 0 else 0,
            'd1_gap': round((bars[entry_idx]['open'] / bars[entry_idx - 1]['close'] - 1) * 100, 2) if entry_idx > 0 and bars[entry_idx - 1]['close'] > 0 else 0,
            'intraday': round((bars[entry_idx]['close'] - bars[entry_idx]['open']) / bars[entry_idx - 1]['close'] * 100, 2) if entry_idx > 0 and bars[entry_idx - 1]['close'] > 0 else 0,
            **result,
        })
        i = streak_end + 1

    return trades

# ================================================================
# 测试列表 (去蓝筹)
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

def print_stats(trades, label):
    if not trades:
        print(f"  {label}: 无信号"); return
    wr = sum(1 for t in trades if t['return_pct'] > 0) / len(trades) * 100
    avg = sum(t['return_pct'] for t in trades) / len(trades)
    peak = sum(t['peak_return_pct'] for t in trades) / len(trades)
    ws = [t['return_pct'] for t in trades if t['return_pct'] > 0]
    ls = [t['return_pct'] for t in trades if t['return_pct'] <= 0]
    if ws and ls and sum(ls) != 0:
        pl = (sum(ws)/len(ws)) / (abs(sum(ls))/len(ls))
    elif ws:
        pl = 999.0
    else:
        pl = 0.0
    print(f"  {label}: {len(trades):>4}笔 胜率{wr:>5.1f}% 均收益{avg:>+6.2f}% 均峰值{peak:>+6.2f}% 盈亏比{pl:.2f}")

def calc_momentum_score(t):
    """计算V1动量强度评分 (0~100)

    基于D0已知数据:
    - 趋势强度 (ret_20d): 20日涨幅越大趋势越强
    - 回踩质量 (d_1_change): D-1回调深度
    - 涨停质量 (d1_limit_up): D1是否涨停
    """
    score = 0
    # 趋势强度 (0~30分)
    ret = t.get('ret_20d', 0)
    if ret >= 80: score += 30
    elif ret >= 60: score += 25
    elif ret >= 50: score += 20
    elif ret >= 40: score += 15
    elif ret >= 30: score += 10
    # 回踩质量 (0~30分): 回踩越深越好
    pb = t.get('d_1_change', 0)
    if -10 <= pb < -7: score += 30
    elif -7 <= pb < -5: score += 25
    elif -5 <= pb < -3: score += 20
    # D1涨停 (0~20分)
    if t.get('d1_limit_up'): score += 20
    # 日内动量 (0~20分): 仅已知时计入
    intra = t.get('intraday')
    if intra is not None:
        if intra >= 5: score += 20
        elif intra >= 3: score += 15
        elif intra >= 0: score += 10
    return score


def momentum_label(score):
    """动量强度标签"""
    if score >= 80: return '🔴 极强'
    if score >= 60: return '🟠 强'
    if score >= 40: return '🟡 中'
    return '⚪ 弱'


def calc_buy_tiers(d0_close, board_type):
    """基于D0收盘价计算D1多档买入建议价

    D0涨停后, D1开盘可能的跳空区间:
    主板: -3% ~ +5% (过滤条件范围内)
    创/科板: -5% ~ +5% (过滤条件: >=-5% 且 <5%)
    """
    if board_type == 'main':
        gaps = [-3, -2, -1, 0, 1, 2, 3, 4, 5]
    else:
        gaps = [-5, -3, -1, 0, 1, 2, 3, 4]  # 创/科板上限5%
    tiers = []
    for g in gaps:
        price = round(d0_close * (1 + g / 100), 2)
        tiers.append((g, price))
    return tiers


def print_today_signals(all_trades, today_str, buy_mode="next_open"):
    """D0收盘后运行, 显示今日信号 + 次日买入建议

    所有策略按信号日筛选 (信号日=今天):
    - 龙回头: signal_date = D0缩量小阴日
    - V1:     d0_date = D0涨停日
    - 断板:   break_date = 断板确认日

    买入时机: 次日D1开盘
    """
    # 按信号日筛选 (信号日=今天, 买入日=明天)
    dc_today = [t for t in all_trades if t['path'] == 'dragon_callback' and t.get('signal_date') == today_str]
    v1_today = [t for t in all_trades if t['path'] == 'v1' and t.get('d0_date') == today_str]
    bb_today = [t for t in all_trades if t['path'] == 'break_buy' and t.get('break_date') == today_str]
    today_trades = dc_today + v1_today + bb_today

    print(f"\n{'=' * 80}")
    print(f"📅 {today_str} 今日信号 (D0收盘后, 次日D1开盘买入)")
    print(f"{'=' * 80}")

    if not today_trades:
        print(f"  今日无信号")
        return today_trades

    print(f"  共 {len(today_trades)} 只股票出现信号")

    # 龙回头信号
    if dc_today:
        print(f"\n  🐉 龙回头 ({len(dc_today)}只) - 缩量小阴确认, 次日D1开盘买:")
        for t in sorted(dc_today, key=lambda x: x.get('entry_vol_r', 0), reverse=True):
            bt = get_board_type(t['code'])
            signal_price = t.get('signal_price', t['entry_price'])
            tiers = calc_buy_tiers(signal_price, bt)
            tier_str = ' / '.join([f"{g:+.0f}%→{p:.2f}" for g, p in tiers])
            print(f"    {t['code']:<8} {t['board']:<6} 涨停{t['lu_date']} 回调{t['pullback_days']}天 "
                  f"信号{t['signal_date']} {t['signal_chg']:+.1f}% 量比{t['entry_vol_r']:.2f}x")
            print(f"{'':>10} 信号价{signal_price:.2f} 买入建议: {tier_str}")

    # V1信号
    if v1_today:
        print(f"\n  🔥 V1 ({len(v1_today)}只) - D0涨停确认, 次日D1开盘买:")
        print(f"  {'代码':>8} {'板块':>6} {'动量':>6} {'评分':>4} {'D0收':>8} {'D-1回调':>8} {'20日涨':>8} {'买入建议'}")
        print(f"  {'-' * 85}")
        for t in sorted(v1_today, key=lambda x: calc_momentum_score(x), reverse=True):
            code, board = t['code'], t['board']
            bt = get_board_type(code)
            d1_gap = t.get('d1_gap', 0)
            d0_close = t['entry_price'] / (1 + d1_gap / 100) if d1_gap != 0 else t['entry_price']
            score = calc_momentum_score(t)
            label = momentum_label(score)
            tiers = calc_buy_tiers(d0_close, bt)
            tier_str = ' / '.join([f"{g:+.0f}%→{p:.2f}" for g, p in tiers])
            print(f"  {code:>8} {board:>6} {label:>6} {score:>3}  {d0_close:>7.2f} "
                  f"{t['d_1_change']:>+7.1f}% {t['ret_20d']:>+7.1f}%  {tier_str}")

    # 断板信号
    if bb_today:
        print(f"\n  💥 断板 ({len(bb_today)}只) - 连板后断板确认, 次日开盘买:")
        for t in sorted(bb_today, key=lambda x: x.get('streak_len', 0), reverse=True):
            print(f"    {t['code']:<8} {t['board']:<6} {t['streak_len']}板连板 "
                  f"断板{t['break_date']} {t['break_chg']:+.1f}% 量{t['break_vol_r']:.2f}x 预计开盘{t['entry_price']:.2f}")

    # ===== 持仓卖出建议 (7天内买入的持仓) =====
    from datetime import datetime, timedelta
    today_dt = datetime.strptime(today_str, '%Y-%m-%d')
    recent_entries = [t for t in all_trades
                      if t['entry_date'] <= today_str
                      and t['entry_date'] >= (today_dt - timedelta(days=20)).strftime('%Y-%m-%d')]

    if recent_entries:
        # 计算当前收益(用today_date的收盘价)
        # 这里简化: 用d1_change和intraday估算
        sell = [t for t in recent_entries if t.get('intraday', 0) < 3]
        hold = [t for t in recent_entries if t.get('intraday', 0) >= 3]

        print(f"\n{'=' * 80}")
        print(f"📊 持仓分析 (7天内买入, 次日开盘执行)")
        print(f"{'=' * 80}")
        print(f"  持仓: {len(recent_entries)}只 | 卖出: {len(sell)}只 | 持有: {len(hold)}只")

        if sell:
            print(f"\n  🔴 明日开盘清仓 ({len(sell)}只) - 日内动量<3%:")
            print(f"  {'代码':>8} {'板块':>6} {'策略':>6} {'买入日':>12} {'买入价':>8} {'持仓天':>6} {'D1收':>7} {'日内':>7}")
            print(f"  {'-' * 75}")
            for t in sorted(sell, key=lambda x: x.get('intraday', 0)):
                pl = {'dragon_callback': '龙回头', 'v1': 'V1', 'break_buy': '断板', }.get(t['path'], t['path'])
                hold_days = (today_dt - datetime.strptime(t['entry_date'], '%Y-%m-%d')).days if t.get('entry_date') else 0
                print(f"  {t['code']:>8} {t['board']:>6} {pl:>6} {t['entry_date']:>12} "
                      f"{t['entry_price']:>7.2f} {hold_days:>5}天 {t.get('d1_change',0):>+6.1f}% {t.get('intraday',0):>+6.1f}%")

        if hold:
            print(f"\n  🟢 继续持有 ({len(hold)}只) - 日内动量>=3%:")
            print(f"  {'代码':>8} {'板块':>6} {'策略':>6} {'买入日':>12} {'买入价':>8} {'持仓天':>6} {'D1收':>7} {'日内':>7}")
            print(f"  {'-' * 75}")
            for t in sorted(hold, key=lambda x: -x.get('intraday', 0)):
                pl = {'dragon_callback': '龙回头', 'v1': 'V1', 'break_buy': '断板', }.get(t['path'], t['path'])
                hold_days = (today_dt - datetime.strptime(t['entry_date'], '%Y-%m-%d')).days if t.get('entry_date') else 0
                print(f"  {t['code']:>8} {t['board']:>6} {pl:>6} {t['entry_date']:>12} "
                      f"{t['entry_price']:>7.2f} {hold_days:>5}天 {t.get('d1_change',0):>+6.1f}% {t.get('intraday',0):>+6.1f}%")
            print(f"\n    出场规则: 追踪止损-5% / 持仓上限7天")

    return today_trades

def main():
    parser = argparse.ArgumentParser(description="龙回头 + V1 + 断板 三策略回测")
    parser.add_argument("--codes", default="")
    parser.add_argument("--days", type=int, default=300, help="向前取N个交易日 (默认300, 从当前日期往前推)")
    parser.add_argument("--source", choices=["manual", "db"], default="manual",
                        help="数据源: manual=手动指定codes(默认), db=从数据库加载全市场")

    parser.add_argument("--all-trades", action="store_true")
    parser.add_argument("--pullback", type=int, default=3, help="龙回头最少回调天数")
    parser.add_argument("--max-pullback", type=int, default=11, help="龙回头最多回调天数")
    parser.add_argument("--max-last-chg", type=float, default=3.0, help="龙回头末期小阳最大涨幅%%")
    parser.add_argument("--strategy", default="all", choices=["all", "dragon", "v1", "break"],
                        help="运行策略: all=全部, dragon=龙回头, v1=V1, break=断板")
    parser.add_argument("--buy-mode", default="next_open",
                        choices=["signal_close", "next_open"],
                        help="买入模式: next_open=D+1开盘买(默认), signal_close=信号日收盘买(回测用)")
    parser.add_argument("--v1-stop-loss", type=float, default=-10.0, help="V1: 止损%% (默认-10)")
    parser.add_argument("--v1-trailing-stop", type=float, default=-5.0, help="V1: 追踪止损%% (默认-5)")
    # V1 v2 核心四因子
    parser.add_argument("--ret-20d-min", type=float, default=30.0, help="V1: 20日最小涨幅%% (默认30)")
    parser.add_argument("--d1-pullback-min", type=float, default=-10.0, help="V1: D-1回调最小%% (默认-10)")
    parser.add_argument("--d1-pullback-max", type=float, default=-3.0, help="V1: D-1回调最大%% (默认-3)")
    parser.add_argument("--no-obv-filter", action="store_true", help="V1: 禁用OBV上升过滤")
    parser.add_argument("--d1-vol-max", type=float, default=1.5, help="V1: D-1量vs5日均量上限 (默认1.5x)")
    parser.add_argument("--today", action="store_true", help="显示买点+持仓卖出建议 (7天内买入的持仓)")
    parser.add_argument("--today-date", type=str, default="", help="指定日期(YYYY-MM-DD), 默认为当天")
    args = parser.parse_args()
    codes = [c.strip() for c in args.codes.split(",") if c.strip()] if args.codes else TEST_CODES

    # DB模式: 从数据库加载全市场代码
    use_db = args.source == "db"
    if use_db:
        print("📊 DB模式: 从数据库加载全市场股票...")
        codes = get_all_codes_db()
        print(f"   全市场: {len(codes)} 只股票")

    run_dc = args.strategy in ("all", "dragon")
    run_v1 = args.strategy in ("all", "v1")
    run_bb = args.strategy in ("all", "break")

    mode_label = {"signal_close": "信号日收盘买", "next_open": "D+1开盘买"}[args.buy_mode]

    print(f"{'=' * 80}")
    print(f"龙回头 + V1 + 断板 三策略回测")
    print(f"{'=' * 80}")
    print(f"买入模式: {mode_label}")
    labels = []
    
    if run_dc: labels.append(f"龙回头(回调{args.pullback}-{args.max_pullback}天)")
    if run_v1: labels.append("V1")
    if run_bb: labels.append(f"断板(连板≥2)")
    print(f"运行: {' + '.join(labels)}")
    print(f"股票: {len(codes)}只\n")

    dc_trades, v1_trades, bb_trades = [], [], []
    success = 0

    # 加载stock_basic_info (换手率 + 板块效应)
    stock_info = None
    sector_counts_by_date = None
    need_stock_info = False
    if need_stock_info:
        try:
            stock_info = fetch_stock_info_db()
            print(f"📊 加载stock_basic_info: {len(stock_info)}只")
        except Exception as e:
            print(f"⚠️  stock_basic_info加载失败({e}), 跳过换手率/板块过滤")

    # 预加载所有K线, 计算板块涨停统计
    all_bars = {}
    need_sector = False
    if need_sector:
        print(f"📊 预加载K线计算板块效应...")
        for code in codes:
            bars = fetch_kline_db(code, args.days) if use_db else fetch_kline(code, args.days)
            if bars:
                all_bars[code] = bars
        # 按日期统计板块涨停数
        sector_counts_by_date = {}
        for code, bars in all_bars.items():
            if code not in stock_info:
                continue
            bt = get_board_type(code)
            threshold = 0.098 if bt == 'main' else 0.198
            for i in range(1, len(bars)):
                prev_c = bars[i-1]['close']
                if prev_c <= 0: continue
                ret = bars[i]['close'] / prev_c - 1
                if ret < threshold * 0.98:
                    continue
                d = bars[i]['time']
                if d not in sector_counts_by_date:
                    sector_counts_by_date[d] = defaultdict(int)
                info = stock_info[code]
                if info['industry']:
                    sector_counts_by_date[d][('industry', info['industry'])] += 1
                for concept in info['concepts']:
                    sector_counts_by_date[d][('concept', concept)] += 1
        print(f"   板块统计: {len(sector_counts_by_date)}个交易日")

    for i, code in enumerate(codes):
        # 显式过滤ST股 (ST涨停5%, 远低于正常阈值, 会被自然排除)
        if is_st_stock(code):
            continue
        bars = all_bars.get(code) if all_bars else (fetch_kline_db(code, args.days) if use_db else fetch_kline(code, args.days))
        if not bars:
            continue

        parts = []

        if run_dc:
            dc = strategy_dragon_callback(bars, code,
                                           min_pullback_days=args.pullback,
                                           max_pullback_days=args.max_pullback,
                                           max_last_chg=args.max_last_chg,
                                           buy_mode=args.buy_mode)
            dc_trades.extend(dc)
            parts.append(f"龙回头{len(dc)}")
        if run_v1:
            # 如果预加载了K线, 直接用; 否则单独加载
            code_bars = all_bars.get(code) if all_bars else bars
            v1 = strategy_v1(code_bars, code, buy_mode=args.buy_mode,
                             hold_days=args.v1_hold_days if hasattr(args, 'v1_hold_days') else 20,
                             stop_loss=args.v1_stop_loss,
                             trailing_stop=args.v1_trailing_stop,
                             ret_20d_min=args.ret_20d_min,
                             d_1_pullback_min=args.d1_pullback_min,
                             d_1_pullback_max=args.d1_pullback_max,
                             obv_filter=not args.no_obv_filter,
                             d_1_vol_max=args.d1_vol_max)
            v1_trades.extend(v1)
            parts.append(f"V1{len(v1)}")
        if run_bb:
            bb = strategy_break_buy(bars, code)
            bb_trades.extend(bb)
            parts.append(f"断板{len(bb)}")

        has_signal = (run_dc and len(dc) > 0) or (run_v1 and len(v1) > 0) or (run_bb and len(bb) > 0)
        if has_signal:
            print(f"[{i+1}/{len(codes)}] {code} ({get_board_name(code)}) ✓{len(bars)}根 → {' '.join(parts)}")
        success += 1
        if not use_db:
            time.sleep(0.15)

    # ===== 独立结果 =====
    print(f"\n{'=' * 80}")
    print(f"结果: {success}只")
    print(f"{'=' * 80}")

    if run_dc:
        print(f"\n📊 龙回头:")
        print_stats(dc_trades, "龙回头")
        if dc_trades:
            print(f"\n  入场量比(入场日/前一天):")
            for lo, hi, label in [(0,0.5,"<0.5x"), (0.5,0.65,"0.5-0.65x"), (0.65,0.8,"0.65-0.8x")]:
                seg = [t for t in dc_trades if lo <= t['entry_vol_r'] < hi]
                if seg: print_stats(seg, f"    {label}")
            print(f"\n  回调天数分布:")
            for lo, hi, label in [(3,5,"3-4天"), (5,8,"5-7天"), (8,12,"8-11天")]:
                seg = [t for t in dc_trades if lo <= t['pullback_days'] < hi]
                if seg: print_stats(seg, f"    {label}")
            print(f"\n  🏆 龙回头TOP5:")
            for t in sorted(dc_trades, key=lambda x: -x['peak_return_pct'])[:5]:
                print(f"    {t['code']} 涨停{t['lu_date']} 回调{t['pullback_days']}天 → {t['signal_date']}信号{t['signal_chg']:+.1f}% 量{t['signal_vol_r']:.2f}x → {t['entry_date']}买 收益{t['return_pct']:+.1f}% 峰值{t['peak_return_pct']:+.1f}%")
            if len(dc_trades) > 5:
                print(f"\n  💀 龙回头BOTTOM5:")
                for t in sorted(dc_trades, key=lambda x: x['return_pct'])[:5]:
                    print(f"    {t['code']} 涨停{t['lu_date']} 回调{t['pullback_days']}天 → {t['signal_date']}信号{t['signal_chg']:+.1f}% 量{t['signal_vol_r']:.2f}x → {t['entry_date']}买 收益{t['return_pct']:+.1f}% 峰值{t['peak_return_pct']:+.1f}%")

    if run_v1:
        print(f"\n📊 V1:")
        print_stats(v1_trades, "V1")

    if run_bb:
        print(f"\n📊 断板:")
        print_stats(bb_trades, "断板")
        if bb_trades:
            streak_trades = [t for t in bb_trades if t.get('mode') == 'streak_break']
            if streak_trades:
                print(f"\n  连板后断板 ({len(streak_trades)}笔):")
                for sl in sorted(set(t['streak_len'] for t in streak_trades)):
                    seg = [t for t in streak_trades if t['streak_len'] == sl]
                    print_stats(seg, f"    {sl}板后断")


        # 按阶段统计
        for phase, label in [(1, 'V1'), (2, '断板'), (3, '龙回头A'), (4, '龙回头B')]:
            seg = [t for t in bb_trades if t.get('phase') == phase]
            if seg:
                print_stats(seg, f"  阶段{phase} {label}")

    # ===== 混合结果 =====
    all_trades = dc_trades + v1_trades + bb_trades
    if len(all_trades) > max(len(dc_trades), len(v1_trades), len(bb_trades)):
        print(f"\n{'=' * 80}")
        print(f"📊 三策略合并:")
        print_stats(all_trades, "合并")
        dc_keys = {(t['code'], t['entry_date']) for t in dc_trades}
        v1_keys = {(t['code'], t['entry_date']) for t in v1_trades}
        bb_keys = {(t['code'], t['entry_date']) for t in bb_trades}
        overlap = (dc_keys & v1_keys) | (dc_keys & bb_keys) | (v1_keys & bb_keys)
        if overlap:
            print(f"  ⚠️ 重叠信号: {len(overlap)}笔")
        else:
            print(f"  ✅ 零重叠, 三策略完全互补")

    # ===== 今日买点统计 =====
    if args.today:
        today_str = args.today_date or time.strftime("%Y-%m-%d")
        all_for_today = dc_trades + v1_trades + bb_trades
        today_trades = print_today_signals(all_for_today, today_str, buy_mode=args.buy_mode)
        if today_trades:
            with open(f"today_signals_{today_str}.json", "w", encoding="utf-8") as f:
                json.dump(today_trades, f, ensure_ascii=False, indent=2)
            print(f"\n💾 today_signals_{today_str}.json ({len(today_trades)}笔)")

    # 交易明细
    if args.all_trades and dc_trades:
        print(f"\n📋 龙回头交易明细:")
        for t in sorted(dc_trades, key=lambda x: x['entry_date']):
            print(f"  {t['code']:<8} {t['board']:<6} 涨停{t['lu_date']} → 回调{t['pullback_days']}天 → "
                  f"{t['signal_date']}信号{t['signal_chg']:>+5.1f}% → "
                  f"{t['entry_date']}买{t['entry_price']:>7.2f} 量比{t['entry_vol_r']:.2f}x "
                  f"收益{t['return_pct']:>+6.2f}% 峰值{t['peak_return_pct']:>+6.2f}%")

    # 导出
    all_out = dc_trades + v1_trades + bb_trades
    if all_out:
        with open("test_dragon_callback_result.json", "w", encoding="utf-8") as f:
            json.dump(all_out, f, ensure_ascii=False, indent=2)
        print(f"\n💾 test_dragon_callback_result.json ({len(all_out)}笔)")

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
4IN1 统一涨停策略 + 独立策略回测

═══════════════════════════════════════════════════
4IN1 策略 (--strategy 4in1): 一次涨停驱动, 分阶段决策
═══════════════════════════════════════════════════

流程:
  D0  涨停(首板/连板) ──→ V1检查D1入场条件
  D1  涨停 ──→ V1入场, 按V1出场规则持有
       没涨停 ──→ 断板检查D1~D2窗口
  D3~D5  依旧没涨停 ──→ 龙回头A(短回调)
  D6~D11 依旧没涨停 ──→ 龙回头B(长回调)
  D12  放弃

规则:
  - 每个涨停日独立评估, 连板的每个涨停日都走V1判断
  - 前阶段出信号则后续阶段跳过, 一个D0最多一笔交易
  - 各阶段入场策略和出场策略相对独立
  - 默认 next_open 买入, 可选 signal_close

═══════════════════════════════════════════════════
独立策略 (--strategy all / dragon / v1 / break)
═══════════════════════════════════════════════════

  龙回头: 涨停 → 回调3-11天 → 末期缩量小阴 → 买入
  V1:     首板次日放量涨停 → D+1开盘买入
  断板:   连板≥2 → 断板 → 次日开盘买入

  独立策略之间互不干扰, 各自产生信号
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

def run_backtest(bars, entry_idx, entry_price, hold_days=20, stop_loss=-10.0, trailing_stop=-8.0, board_type="main", peak_exit=False, is_v1=False, d1_limit_up=None):
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

        # V1专属: D1没涨停 → D+2开盘跑路 (首板次日未封板=不及预期, 不恋战)
        if is_v1 and d == 2 and not d1_limit_up:
            d1_bar = bars[entry_idx + 1]
            d1_high = d1_bar['high']
            d1_close = d1_bar['close']
            d2_open_gap = (b['open'] / d1_close - 1) * 100 if d1_close > 0 else 0
            # 止损优先
            if b['low'] <= entry_price * (1 + stop_loss / 100):
                exit_p = entry_price * (1 + stop_loss / 100); exit_d = d; break
            # D2高开>2%直接走 (趁高开跑路)
            if d2_open_gap > 2.0:
                exit_p = b['open']; exit_d = d; break
            # D2跌破D1高点×0.99走 (反弹无力)
            exit_trigger = d1_high * 0.99
            if b['low'] <= exit_trigger:
                exit_p = exit_trigger; exit_d = d; break
            # 以上都没触发 → D2收盘走 (无论如何D2了结)
            exit_p = b['close']; exit_d = d; break

        # ① 峰值逃顶(优先): 涨>7%后大上影线(>30%)→收盘逃顶
        if peak_exit:
            ret = (b['close'] / entry_price - 1) * 100
            if ret > 7:
                bar_range = b['high'] - b['low']
                upper = (b['high'] - max(b['open'], b['close'])) / bar_range * 100 if bar_range > 0 else 0
                if upper > 30 and b['close'] < b['high'] * 0.98:
                    exit_p = b['close']; exit_d = d; break

        # ② 追踪止损
        if d > 1 and b['low'] <= peak * (1 + trailing_stop / 100):
            exit_p = peak * (1 + trailing_stop / 100); exit_d = d; break
        # ③ 止损
        if b['low'] <= entry_price * (1 + stop_loss / 100):
            exit_p = entry_price * (1 + stop_loss / 100); exit_d = d; break

        # ④ 兜底: 持仓到期收盘走
        exit_p = b['close']; exit_d = d

    return {
        'exit_price': round(exit_p, 3), 'exit_day': exit_d,
        'return_pct': round((exit_p / entry_price - 1) * 100, 2),
        'peak_return_pct': round((peak / entry_price - 1) * 100, 2),
        'd1_limit_up': d1_limit_up,
    }

def strategy_dragon_callback(bars, code, min_pullback_days=3, max_pullback_days=11,
                             max_last_chg=3.0,
                             hold_days=15, stop_loss=-5.0, trailing_stop=-5.0,
                             buy_mode="signal_close"):
    """
    龙回头v4 (优化版):
    D-N涨停 → 回调3-11天 → 末期缩量小阴(-3%~-0.5%)+量比0.5~0.8 → 买入

    出场参数 (stop-5 + trail-5 + peak7/30):
      stop_loss    = -5%  (原-5%, 单笔最大亏损控制)
      trailing_stop = -5% (原-5%, 更早锁利)
      hold_days    = 10   (原15, 时间止损兜底)
      peak_escape : 涨>7%后上影线>30%逃顶 (原10%/40%)

    buy_mode:
      signal_close — 信号日收盘买 (默认, 14:50盘中扫描可行)
      next_open    — D+1开盘买
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
        if pullback_end + 1 >= len(bars): continue
        d1 = bars[pullback_end + 1]
        d1_change = (d1['close'] / last_pb['close'] - 1) * 100

        # 根据buy_mode确定入场价
        if buy_mode == "signal_close":
            entry_price = last_pb['close']
            entry_idx = pullback_end
            entry_date = last_pb['time']
        elif buy_mode == "next_open":
            # 龙回头信号需D+1收盘后才能确认（末期缩量小阴在D0，
            # 但需D+1收盘数据验证信号有效性），因此D+1开盘时
            # 无法预知是否为买点。实际最早只能在D+2开盘买入。
            if pullback_end + 2 >= len(bars): continue
            d2 = bars[pullback_end + 2]
            entry_price = d2['open']
            entry_idx = pullback_end + 2
            entry_date = d2['time']
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

        result = run_backtest(bars, entry_idx, entry_price, hold_days, stop_loss, trailing_stop, board_type, peak_exit=True, d1_limit_up=d1_limit_up_val)
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
            'd1_change': round(d1_change, 2),
            **result,
        })

    return trades

# ================================================================
# 4IN1 统一策略
# ================================================================

# V1 默认参数
_V1_PARAMS = dict(
    min_vol_ratio=1.5, max_upper_shadow=0.5,
    d1_min_change=2.0, no_limit_lookback=10,
    use_ema_filter=True, use_rsi_filter=True,
    min_turnover=8.0, max_turnover=12.0,
    max_d0_gap=5.0, min_sector_limits=2,
    v1_hold_days=20, v1_stop_loss=-10.0, v1_trailing_stop=-5.0,
)

# 断板默认参数
_BREAK_PARAMS = dict(
    stop_loss=-8.0, trailing_stop=-6.0, take_profit=15.0,
    hold_days=20, vol_min=1.2, vol_max=2.0, drawdown_max=-10,
)

# 龙回头 A 默认参数 (D3~D5, 短回调)
_DRAGON_A_PARAMS = dict(
    stop_loss=-5.0, trailing_stop=-5.0, hold_days=10,
    max_last_chg=3.0, min_vol_ratio=0.5, max_vol_ratio=0.8,
)

# 龙回头 B 默认参数 (D6~D11, 长回调)
_DRAGON_B_PARAMS = dict(
    stop_loss=-5.0, trailing_stop=-5.0, hold_days=10,
    max_last_chg=3.0, min_vol_ratio=0.5, max_vol_ratio=0.8,
)

def strategy_4in1(bars, code,
                  buy_mode="next_open",
                  v1_params=None, break_params=None,
                  dragon_a_params=None, dragon_b_params=None,
                  stock_info=None, sector_counts_by_date=None):
    """
    4IN1 统一策略: 一次涨停驱动, 分阶段决策

    流程:
      D0  涨停 ──→ V1检查D1入场条件
      D1  涨停 ──→ V1入场, 按V1出场规则持有
           没涨停 ──→ 断板检查D1~D2窗口
      D3~D5  没涨停 ──→ 龙回头A
      D6~D11 没涨停 ──→ 龙回头B
      D12  放弃

    每个D0最多产生一笔交易, 前阶段出信号则后续阶段跳过。
    """
    vp = dict(_V1_PARAMS); vp.update(v1_params or {})
    bp = dict(_BREAK_PARAMS); bp.update(break_params or {})
    dap = dict(_DRAGON_A_PARAMS); dap.update(dragon_a_params or {})
    dbp = dict(_DRAGON_B_PARAMS); dbp.update(dragon_b_params or {})

    bt = get_board_type(code)
    threshold = 0.098 if bt == "main" else 0.198
    limit_ups = find_limit_ups(bars, bt)
    trades = []
    used_ranges = []

    for lu_idx in limit_ups:
        # 4IN1: 每个涨停日独立评估, 不做主循环去重
        # (龙回头阶段内部有自己的去重逻辑)

        lu_close = bars[lu_idx]['close']
        lu_vol = bars[lu_idx]['volume']
        lu_prev_close = bars[lu_idx - 1]['close'] if lu_idx > 0 else 0
        if lu_prev_close <= 0:
            continue

        # D1 检查
        if lu_idx + 1 >= len(bars):
            continue
        d1 = bars[lu_idx + 1]
        d1_limit_up = is_limit_up(d1['close'], lu_close, bt)
        d1_change = (d1['close'] / lu_close - 1) * 100

        trade = None

        # ==================== 阶段1: V1 ====================
        if d1_limit_up and d1_change >= vp['d1_min_change']:
            # D0 上影线
            upper_shadow = (bars[lu_idx]['high'] - lu_close) / lu_prev_close * 100
            if upper_shadow >= vp['max_upper_shadow']:
                pass  # V1条件不满足, 继续到下一阶段
            else:
                # D0 量比
                vol_ratio = lu_vol / bars[lu_idx - 1]['volume'] if bars[lu_idx - 1]['volume'] > 0 else 0
                if vol_ratio < vp['min_vol_ratio']:
                    pass
                else:
                    # D0 跳空
                    d0_gap = (bars[lu_idx]['open'] / lu_prev_close - 1) * 100
                    if d0_gap > vp['max_d0_gap']:
                        pass
                    else:
                        # 4IN1: 不做前N日无涨停过滤, 连板的每个涨停日都走V1判断
                        # EMA 趋势
                        ema_ok = True
                        if vp['use_ema_filter'] and lu_idx >= 20:
                            closes = [bars[j]['close'] for j in range(lu_idx - 20, lu_idx + 1)]
                            ema10 = ema(closes, 10)
                            ema20 = ema(closes, 20)
                            if ema10 and ema20 and ema10 <= ema20:
                                ema_ok = False
                        if not ema_ok:
                            pass
                        else:
                            # RSI
                            rsi_ok = True
                            if vp['use_rsi_filter'] and lu_idx >= 15:
                                closes = [bars[j]['close'] for j in range(lu_idx - 15, lu_idx + 1)]
                                r = rsi(closes, 14)
                                if r and (r <= 30 or r >= 70):
                                    rsi_ok = False
                            if not rsi_ok:
                                pass
                            else:
                                # 换手率
                                tr_ok = True
                                if stock_info and code in stock_info:
                                    circ = stock_info[code]['circ_shares']
                                    if circ > 0:
                                        turnover = lu_vol / circ * 100
                                        if turnover < vp['min_turnover'] or turnover > vp['max_turnover']:
                                            tr_ok = False
                                    else:
                                        tr_ok = False
                                if not tr_ok:
                                    pass
                                else:
                                    # 板块效应
                                    sec_ok = True
                                    if sector_counts_by_date and stock_info and code in stock_info:
                                        sc = sector_counts_by_date.get(bars[lu_idx]['time'], {})
                                        sec_max = get_stock_sector_limit_count(code, stock_info, sc)
                                        if sec_max < vp['min_sector_limits']:
                                            sec_ok = False
                                    if not sec_ok:
                                        pass
                                    else:
                                        # === V1 信号确认 ===
                                        if buy_mode == "signal_close":
                                            entry_price = lu_close
                                            entry_idx = lu_idx
                                            entry_date = bars[lu_idx]['time']
                                        else:  # next_open
                                            entry_price = d1['open']
                                            entry_idx = lu_idx + 1
                                            entry_date = d1['time']

                                        if entry_price > 0:
                                            turnover_rate = 0.0
                                            if stock_info and code in stock_info:
                                                circ = stock_info[code]['circ_shares']
                                                if circ > 0:
                                                    turnover_rate = lu_vol / circ * 100

                                            result = run_backtest(
                                                bars, entry_idx, entry_price,
                                                vp['v1_hold_days'], vp['v1_stop_loss'],
                                                vp['v1_trailing_stop'], bt,
                                                is_v1=True, d1_limit_up=d1_limit_up)
                                            if result:
                                                trade = {
                                                    'code': code, 'board': get_board_name(code),
                                                    'path': '4in1_v1', 'path_label': '4IN1-V1',
                                                    'phase': 1, 'phase_label': 'V1',
                                                    'd0_date': bars[lu_idx]['time'],
                                                    'entry_date': entry_date,
                                                    'entry_price': round(entry_price, 3),
                                                    'buy_mode': buy_mode,
                                                    'vol_ratio': round(vol_ratio, 2),
                                                    'turnover_rate': round(turnover_rate, 2),
                                                    'd0_gap': round(d0_gap, 2),
                                                    'd1_change': round(d1_change, 2),
                                                    **result,
                                                }

        # ==================== 阶段2: 断板 (D1~D2) ====================
        if trade is None and not d1_limit_up:
            break_days_since_lu = 0
            for j in range(lu_idx + 1, min(lu_idx + 3, len(bars))):
                if is_limit_up(bars[j]['close'], bars[j-1]['close'], bt):
                    break  # 遇到新涨停, 断板期结束
                break_days_since_lu += 1

            if break_days_since_lu > 0:
                break_bars_list = bars[lu_idx + 1:lu_idx + 1 + break_days_since_lu]
                break_low = min(float(b['low']) for b in break_bars_list)
                break_vol_avg = sum(float(b['volume']) for b in break_bars_list) / len(break_bars_list)
                break_vol_r = break_vol_avg / lu_vol if lu_vol > 0 else 0

                limit_open = float(bars[lu_idx]['open'])
                break_drawdown = (break_low / lu_close - 1) * 100

                # 逐日检查断板信号
                for bi, bbar in enumerate(break_bars_list):
                    bchg = (bbar['close'] / lu_close - 1) * 100
                    bgap = (bbar['open'] / lu_close - 1) * 100

                    checks = (
                        break_low >= limit_open
                        and bp['vol_min'] <= break_vol_r < bp['vol_max']
                        and -5 <= bchg < 8
                        and -3 <= bgap < 5
                        and break_drawdown >= bp['drawdown_max']
                    )
                    if not checks:
                        continue

                    if buy_mode == "signal_close":
                        entry_price = bbar['close']
                        entry_idx = lu_idx + 1 + bi
                        entry_date = bbar['time']
                    else:  # next_open
                        next_idx = lu_idx + 1 + bi + 1
                        if next_idx >= len(bars):
                            continue
                        entry_price = bars[next_idx]['open']
                        entry_idx = next_idx
                        entry_date = bars[next_idx]['time']

                    if entry_price <= 0:
                        continue

                    result = run_backtest_breakbuy(
                        bars, entry_idx, entry_price,
                        bp['hold_days'], bp['stop_loss'],
                        bp['trailing_stop'], bt)
                    if result:
                        trade = {
                            'code': code, 'board': get_board_name(code),
                            'path': '4in1_break', 'path_label': '4IN1-断板',
                            'phase': 2, 'phase_label': '断板',
                            'd0_date': bars[lu_idx]['time'],
                            'break_date': bbar['time'],
                            'break_days': break_days_since_lu,
                            'break_chg': round(bchg, 2),
                            'break_gap': round(bgap, 2),
                            'break_vol_r': round(break_vol_r, 2),
                            'entry_date': entry_date,
                            'entry_price': round(entry_price, 3),
                            'buy_mode': buy_mode,
                            **result,
                        }
                        break  # 断板信号找到, 停止逐日检查

        # ==================== 阶段3: 龙回头 A (D3~D5) ====================
        if trade is None and not d1_limit_up:
            trade = _dragon_phase(bars, code, lu_idx, 3, 5, dap, bt, threshold,
                                  buy_mode, '4in1_dragon_a', '4IN1-龙回头A', 3, used_ranges)

        # ==================== 阶段4: 龙回头 B (D6~D11) ====================
        if trade is None and not d1_limit_up:
            trade = _dragon_phase(bars, code, lu_idx, 6, 11, dbp, bt, threshold,
                                  buy_mode, '4in1_dragon_b', '4IN1-龙回头B', 4, used_ranges)

        # ==================== 记录交易 ====================
        if trade is not None:
            # 龙回头阶段: 用 pullback_idx 去重, 避免连板的涨停日互相干扰
            if trade.get('path', '').startswith('4in1_dragon') and 'pullback_idx' in trade:
                used_ranges.append((trade['pullback_idx'], trade['pullback_idx']))
            trades.append(trade)

    return trades


def _dragon_phase(bars, code, lu_idx, min_pb, max_pb, params, bt, threshold,
                  buy_mode, path, path_label, phase, used_ranges):
    """4IN1 龙回头阶段通用检查 (阶段3/4共用)

    检查 lu_idx 后 min_pb~max_pb 天内是否出现龙回头信号:
      涨停 → 回调 → 末期缩量小阴(-3%~-0.5%) → 买入
    """
    lu_close = bars[lu_idx]['close']
    lu_vol = bars[lu_idx]['volume']

    # 找回调期: close < lu_close 的最后一天
    pullback_end = None
    for j in range(lu_idx + 1, min(lu_idx + 20, len(bars))):
        if bars[j]['close'] < lu_close:
            pullback_end = j
        elif j >= lu_idx + min_pb:
            break
        else:
            break

    if pullback_end is None:
        return None

    pullback_days = pullback_end - lu_idx
    if pullback_days < min_pb or pullback_days > max_pb:
        return None

    # 信号日检查
    last_pb = bars[pullback_end]
    last_pb_prev = bars[pullback_end - 1] if pullback_end > 0 else bars[lu_idx]
    last_pb_prev_c = last_pb_prev['close']
    if last_pb_prev_c <= 0:
        return None

    last_chg = (last_pb['close'] / last_pb_prev_c - 1) * 100

    # 排除大阴
    if last_chg < -params['max_last_chg']:
        return None

    # 末期小阴: 缩量下跌, 抛压枯竭
    if not (-params['max_last_chg'] < last_chg < -0.5):
        return None

    # 信号日量比 (D0量 / D-1量)
    signal_vol = last_pb['volume']
    signal_prev_vol = bars[pullback_end - 1]['volume'] if pullback_end > 0 else 0
    vol_ratio = signal_vol / signal_prev_vol if signal_prev_vol > 0 else 0
    if vol_ratio < params['min_vol_ratio'] or vol_ratio >= params['max_vol_ratio']:
        return None

    # D1 数据 (用于过滤和 d1_limit_up 预计算)
    if pullback_end + 1 >= len(bars):
        return None
    d1 = bars[pullback_end + 1]
    d1_change = (d1['close'] / last_pb['close'] - 1) * 100

    # 根据 buy_mode 确定入场价
    if buy_mode == "signal_close":
        entry_price = last_pb['close']
        entry_idx = pullback_end
        entry_date = last_pb['time']
    else:  # next_open
        if pullback_end + 2 >= len(bars):
            return None
        d2 = bars[pullback_end + 2]
        entry_price = d2['open']
        entry_idx = pullback_end + 2
        entry_date = d2['time']

    if entry_price <= 0:
        return None

    # 去重
    for (s, e) in used_ranges:
        if abs(pullback_end - s) <= 4 or abs(pullback_end - e) <= 4:
            return None

    d1_limit_up_val = is_limit_up(d1['close'], last_pb['close'], bt)

    result = run_backtest(bars, entry_idx, entry_price,
                          params['hold_days'], params['stop_loss'],
                          params['trailing_stop'], bt, peak_exit=True,
                          d1_limit_up=d1_limit_up_val)
    if not result:
        return None

    phase_label_map = {3: '龙回头A', 4: '龙回头B'}
    return {
        'code': code, 'board': get_board_name(code),
        'path': path, 'path_label': path_label,
        'phase': phase, 'phase_label': phase_label_map.get(phase, path_label),
        'lu_date': bars[lu_idx]['time'],
        'pullback_days': pullback_days,
        'pullback_idx': pullback_end,
        'signal_date': last_pb['time'],
        'signal_chg': round(last_chg, 2),
        'signal_vol_r': round(vol_ratio, 2),
        'entry_vol_r': round(vol_ratio, 2),
        'entry_date': entry_date,
        'entry_price': round(entry_price, 3),
        'buy_mode': buy_mode,
        'd1_change': round(d1_change, 2),
        **result,
    }


def strategy_v1(bars, code, min_vol_ratio=1.5, max_upper_shadow=0.5,
                hold_days=20, stop_loss=-10.0, trailing_stop=-5.0,
                buy_mode="signal_close",
                no_limit_lookback=10, use_ema_filter=True, use_rsi_filter=True,
                d1_min_change=2.0,
                stock_info=None, sector_counts_by_date=None,
                min_turnover=8.0, max_turnover=12.0,
                max_d0_gap=5.0,
                min_sector_limits=2):
    """V1基线策略

    no_limit_lookback: 前N日无涨停过滤 (默认10天, 排除近期有涨停的股票)

    buy_mode:
      signal_close — ⚠️ V1不可行: 涨停股收盘时封板买不到, 仅回测用
      next_open    — D+1开盘买
    """
    board_type = get_board_type(code)
    threshold = 0.098 if board_type == "main" else 0.198

    result = []
    for i in range(2, len(bars)):
        prev_c = bars[i-1]['close']
        if prev_c <= 0: continue
        ret = (bars[i]['close'] / prev_c - 1)
        if ret < threshold * 0.98: continue
        # 前N天不是涨停（排除连板中间板+近期有涨停的股票, 只取"干净"第一板）
        skip = False
        for k in range(1, no_limit_lookback + 1):
            if i - k < 1: break
            prev_k_c = bars[i-k-1]['close']
            if prev_k_c > 0 and (bars[i-k]['close'] / prev_k_c - 1) >= threshold * 0.98:
                skip = True; break
        if skip: continue

        fl = bars[i]
        fl_close = fl['close']
        fl_high = fl['high']
        fl_vol = fl['volume']
        fl_prev_close = bars[i-1]['close']
        fl_prev_vol = bars[i-1]['volume']
        ref = bars[i-2]['close'] if i >= 2 else fl_prev_close

        if ref <= 0: continue

        vol_ratio = fl_vol / fl_prev_vol if fl_prev_vol > 0 else 0
        upper_shadow = (fl_high - fl_close) / ref * 100
        if upper_shadow >= max_upper_shadow: continue

        # 量比过滤
        if vol_ratio < min_vol_ratio:
            continue

        # EMA趋势过滤: EMA10 > EMA20 (多头排列)
        if use_ema_filter and i >= 20:
            closes = [bars[j]['close'] for j in range(i - 20, i + 1)]
            ema10 = ema(closes, 10)
            ema20 = ema(closes, 20)
            if ema10 is not None and ema20 is not None and ema10 <= ema20:
                continue

        # RSI过滤: 30 < RSI < 70 (排除超买超卖)
        if use_rsi_filter and i >= 15:
            closes = [bars[j]['close'] for j in range(i - 15, i + 1)]
            r = rsi(closes, 14)
            if r is not None and (r <= 30 or r >= 70):
                continue

        # 换手率过滤: D0成交量 / 流通股本 (8-12%共振区)
        if stock_info:
            if code not in stock_info:
                continue  # 无数据, 排除
            circ = stock_info[code]['circ_shares']
            if circ > 0:
                turnover = fl_vol / circ * 100
                if turnover < min_turnover or turnover > max_turnover:
                    continue  # 换手率不在共振区
            else:
                continue  # 流通股本为0, 排除

        # D0跳空高开过滤: 排除跳空>5%的(消息刺激一字板/高开太多)
        d0_gap_pct = (fl['open'] / bars[i-1]['close'] - 1) * 100 if bars[i-1]['close'] > 0 else 0
        if d0_gap_pct > max_d0_gap:
            continue

        # 板块效应过滤: 同板块涨停数
        if sector_counts_by_date and stock_info and code in stock_info:
            fl_date = fl['time']
            sc = sector_counts_by_date.get(fl_date, {})
            sector_max = get_stock_sector_limit_count(code, stock_info, sc)
            if sector_max < min_sector_limits:
                continue  # 孤板, 板块没共振

        # D1数据 (用于过滤和回测)
        if i + 1 >= len(bars): continue
        d1 = bars[i + 1]
        d1_change = (d1['close'] / fl_close - 1) * 100

        # D1涨幅过滤: D1涨幅太小说明市场不认可首板, 排除弱信号
        if d1_change < d1_min_change:
            continue

        # 根据buy_mode确定入场价
        if buy_mode == "signal_close":
            entry_price = fl['close']
            entry_idx = i
            entry_date = fl['time']
        elif buy_mode == "next_open":
            entry_price = d1['open']
            entry_idx = i + 1
            entry_date = d1['time']
        else:
            continue
        if entry_price <= 0: continue

        # D1过滤 (仅next_open模式, signal_close已买入不需要)
        if buy_mode == "next_open":
            d1_gap = (entry_price / fl_close - 1) * 100
            min_d1_gap = -3.0 if board_type == "main" else -5.0
            if d1_gap < min_d1_gap:
                continue
            if d1_change < 0: continue  # D1收阴排除

        # 预计算 d1_limit_up: 基于 D1 收盘 vs D0 收盘 (涨停日)
        d1_limit_up_val = is_limit_up(d1['close'], fl_close, board_type)

        # 计算换手率
        turnover_rate = 0.0
        if stock_info and code in stock_info:
            circ = stock_info[code]['circ_shares']
            if circ > 0:
                turnover_rate = fl_vol / circ * 100

        # D0跳空高开幅度
        d0_gap = (fl['open'] / bars[i-1]['close'] - 1) * 100 if bars[i-1]['close'] > 0 else 0

        bt = run_backtest(bars, entry_idx, entry_price, hold_days, stop_loss, trailing_stop, board_type, is_v1=True, d1_limit_up=d1_limit_up_val)
        if not bt: continue

        result.append({
            'code': code, 'board': get_board_name(code),
            'path': 'v1', 'path_label': 'V1',
            'd0_date': fl['time'],
            'entry_date': entry_date,
            'entry_price': round(entry_price, 3),
            'buy_mode': buy_mode,
            'vol_ratio': round(vol_ratio, 2),
            'turnover_rate': round(turnover_rate, 2),
            'd0_gap': round(d0_gap, 2),
            'd1_change': round(d1_change, 2),
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

def run_backtest_breakbuy(bars, entry_idx, entry_price, hold_days=20, stop_loss=-8.0,
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
            'entry_date': bars[entry_idx]['time'], 'entry_price': round(entry_price, 3), 'buy_mode': 'next_open', **result,
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

def print_today_signals(all_trades, today_str):
    """统计今日出现买点的股票"""
    today_trades = [t for t in all_trades if t['entry_date'] == today_str]
    if not today_trades:
        print(f"\n{'=' * 80}")
        print(f"📅 {today_str} 今日买点统计")
        print(f"{'=' * 80}")
        print(f"  今日无买点信号")
        return today_trades

    # 按策略分组
    dc_today = [t for t in today_trades if t['path'] == 'dragon_callback']
    v1_today = [t for t in today_trades if t['path'] == 'v1']
    bb_today = [t for t in today_trades if t['path'] == 'break_buy']

    # 按板块分组
    main_today = [t for t in today_trades if t['board'] in ('沪主板', '深主板')]
    gem_today = [t for t in today_trades if t['board'] == '创业板']
    star_today = [t for t in today_trades if t['board'] == '科创板']

    print(f"\n{'=' * 80}")
    print(f"📅 {today_str} 今日买点统计")
    print(f"{'=' * 80}")
    print(f"  共 {len(today_trades)} 只股票出现买点信号")

    # 策略分布
    print(f"\n  📊 策略分布:")
    if dc_today:
        print(f"    🐉 龙回头: {len(dc_today)} 只")
    if v1_today:
        print(f"    🔥 V1: {len(v1_today)} 只")
    if bb_today:
        print(f"    💥 断板: {len(bb_today)} 只")

    # 板块分布
    print(f"\n  📊 板块分布:")
    if main_today:
        print(f"    🏛️  主板: {len(main_today)} 只")
    if gem_today:
        print(f"    💎 创业板: {len(gem_today)} 只")
    if star_today:
        print(f"    🚀 科创板: {len(star_today)} 只")

    # 股票代码汇总
    all_codes = sorted(set(t['code'] for t in today_trades))
    print(f"\n  📋 今日买点股票代码汇总 ({len(all_codes)}只):")
    print(f"    {', '.join(all_codes)}")

    # 按策略列出股票
    if dc_today:
        print(f"\n  🐉 龙回头信号 ({len(dc_today)}只):")
        for t in sorted(dc_today, key=lambda x: x.get('entry_vol_r', 0), reverse=True):
            print(f"    {t['code']:<8} {t['board']:<6} 涨停{t['lu_date']} 回调{t['pullback_days']}天 "
                  f"信号{t['signal_date']} {t['signal_chg']:+.1f}% "
                  f"买入{t['entry_date']} 量比{t['entry_vol_r']:.2f}x {t['entry_price']:.2f}")

    if v1_today:
        print(f"\n  🔥 V1信号 ({len(v1_today)}只):")
        for t in sorted(v1_today, key=lambda x: x.get('vol_ratio', 0), reverse=True):
            print(f"    {t['code']:<8} {t['board']:<6} 涨停{t['d0_date']} "
                  f"量比{t['vol_ratio']:.2f}x D1{t['d1_change']:+.1f}% "
                  f"买入{t['entry_price']:.2f}")

    if bb_today:
        print(f"\n  💥 断板信号 ({len(bb_today)}只):")
        for t in sorted(bb_today, key=lambda x: x.get('streak_len', 0), reverse=True):
            print(f"    {t['code']:<8} {t['board']:<6} {t['streak_len']}板连板 "
                  f"断板{t['break_date']} {t['break_chg']:+.1f}% 量{t['break_vol_r']:.2f}x "
                  f"买入{t['entry_price']:.2f}")

    return today_trades

def main():
    parser = argparse.ArgumentParser(description="龙回头 + V1 + 断板 三策略回测")
    parser.add_argument("--codes", default="")
    parser.add_argument("--days", type=int, default=300)
    parser.add_argument("--source", choices=["manual", "db"], default="manual",
                        help="数据源: manual=手动指定codes(默认), db=从数据库加载全市场")
    parser.add_argument("--start", type=str, default="2024-01-01", help="DB模式回测开始日期")
    parser.add_argument("--end", type=str, default="2026-05-22", help="DB模式回测结束日期")
    parser.add_argument("--all-trades", action="store_true")
    parser.add_argument("--pullback", type=int, default=3, help="龙回头最少回调天数")
    parser.add_argument("--max-pullback", type=int, default=11, help="龙回头最多回调天数")
    parser.add_argument("--max-last-chg", type=float, default=3.0, help="龙回头末期小阳最大涨幅%%")
    parser.add_argument("--strategy", default="all", choices=["all", "dragon", "v1", "break", "4in1"],
                        help="运行策略: all=全部, dragon=龙回头, v1=V1, break=断板, 4in1=四合一")
    parser.add_argument("--buy-mode", default="signal_close",
                        choices=["signal_close", "next_open"],
                        help="买入模式: signal_close=信号日收盘买(默认), next_open=D+1开盘买")
    parser.add_argument("--no-limit-lookback", type=int, default=10, help="V1: 前N日无涨停过滤 (默认10)")
    parser.add_argument("--min-vol-ratio", type=float, default=1.5, help="V1: 最小量比 (默认1.5)")
    parser.add_argument("--max-upper-shadow", type=float, default=0.5, help="V1: 最大上影线%% (默认0.5)")
    parser.add_argument("--v1-stop-loss", type=float, default=-10.0, help="V1: 止损%% (默认-10)")
    parser.add_argument("--v1-trailing-stop", type=float, default=-5.0, help="V1: 追踪止损%% (默认-5)")
    parser.add_argument("--d1-min-change", type=float, default=2.0, help="V1: D1最小涨幅%%, 低于此值排除 (默认2.0)")
    parser.add_argument("--min-turnover", type=float, default=8.0, help="V1: 最小换手率%% (默认8)")
    parser.add_argument("--max-turnover", type=float, default=12.0, help="V1: 最大换手率%% (默认12)")
    parser.add_argument("--max-d0-gap", type=float, default=5.0, help="V1: D0跳空高开上限%%, 超过排除 (默认5)")
    parser.add_argument("--min-sector-limits", type=int, default=2, help="V1: 同板块最少涨停数, 低于排除 (默认2, 即不是孤板)")
    parser.add_argument("--no-ema-filter", action="store_true", help="V1: 禁用EMA10>EMA20过滤")
    parser.add_argument("--no-rsi-filter", action="store_true", help="V1: 禁用RSI 30-70过滤")
    parser.add_argument("--today", action="store_true", help="仅统计今日出现买点的股票")
    parser.add_argument("--today-date", type=str, default="", help="指定日期(YYYY-MM-DD), 默认为今天")
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
    run_4in1 = args.strategy == "4in1"

    mode_label = {"signal_close": "信号日收盘买", "next_open": "D+1开盘买"}[args.buy_mode]

    print(f"{'=' * 80}")
    if run_4in1:
        print(f"4IN1 统一策略回测 (V1 → 断板 → 龙回头A → 龙回头B)")
    else:
        print(f"龙回头 + V1 + 断板 三策略回测")
    print(f"{'=' * 80}")
    print(f"买入模式: {mode_label}")
    labels = []
    if run_4in1:
        labels.append("4IN1(统一管道)")
    if run_dc: labels.append(f"龙回头(回调{args.pullback}-{args.max_pullback}天)")
    if run_v1: labels.append("V1")
    if run_bb: labels.append(f"断板(连板≥2)")
    print(f"运行: {' + '.join(labels)}")
    print(f"股票: {len(codes)}只\n")

    dc_trades, v1_trades, bb_trades, f4in1_trades = [], [], [], []
    success = 0

    # 加载stock_basic_info (换手率 + 板块效应)
    stock_info = None
    sector_counts_by_date = None
    need_stock_info = (run_v1 and (args.min_turnover > 0 or args.max_turnover < 100 or args.min_sector_limits > 0)) or run_4in1
    if need_stock_info:
        try:
            stock_info = fetch_stock_info_db()
            print(f"📊 加载stock_basic_info: {len(stock_info)}只")
        except Exception as e:
            print(f"⚠️  stock_basic_info加载失败({e}), 跳过换手率/板块过滤")

    # 预加载所有K线, 计算板块涨停统计
    all_bars = {}
    need_sector = (run_v1 and stock_info is not None and args.min_sector_limits > 0) or (run_4in1 and stock_info is not None)
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
        if run_4in1:
            code_bars = all_bars.get(code) if all_bars else bars
            f4 = strategy_4in1(code_bars, code, buy_mode=args.buy_mode,
                               stock_info=stock_info,
                               sector_counts_by_date=sector_counts_by_date)
            f4in1_trades.extend(f4)
            if f4:
                phases = [t['phase_label'] for t in f4]
                parts.append(f"4IN1{len(f4)}({'+'.join(phases)})")
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
                             no_limit_lookback=args.no_limit_lookback,
                             min_vol_ratio=args.min_vol_ratio,
                             max_upper_shadow=args.max_upper_shadow,
                             stop_loss=args.v1_stop_loss,
                             trailing_stop=args.v1_trailing_stop,
                             use_ema_filter=not args.no_ema_filter,
                             use_rsi_filter=not args.no_rsi_filter,
                             d1_min_change=args.d1_min_change,
                             stock_info=stock_info,
                             sector_counts_by_date=sector_counts_by_date,
                             min_turnover=args.min_turnover,
                             max_turnover=args.max_turnover,
                             max_d0_gap=args.max_d0_gap,
                             min_sector_limits=args.min_sector_limits)
            v1_trades.extend(v1)
            parts.append(f"V1{len(v1)}")
        if run_bb:
            bb = strategy_break_buy(bars, code)
            bb_trades.extend(bb)
            parts.append(f"断板{len(bb)}")

        has_signal = (run_4in1 and len(f4) > 0) or (run_dc and len(dc) > 0) or (run_v1 and len(v1) > 0) or (run_bb and len(bb) > 0)
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

    if run_4in1:
        print(f"\n📊 4IN1 统一策略:")
        print_stats(f4in1_trades, "4IN1")
        # 按阶段统计
        for phase, label in [(1, 'V1'), (2, '断板'), (3, '龙回头A'), (4, '龙回头B')]:
            seg = [t for t in f4in1_trades if t.get('phase') == phase]
            if seg:
                print_stats(seg, f"  阶段{phase} {label}")
        if f4in1_trades:
            print(f"\n  🏆 4IN1 TOP5:")
            for t in sorted(f4in1_trades, key=lambda x: -x['peak_return_pct'])[:5]:
                print(f"    {t['code']} {t['phase_label']:<6} {t['entry_date']}买 "
                      f"收益{t['return_pct']:+.1f}% 峰值{t['peak_return_pct']:+.1f}%")

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
        from datetime import datetime, timedelta
        today_str = args.today_date if args.today_date else datetime.now().strftime("%Y-%m-%d")
        all_for_today = dc_trades + v1_trades + bb_trades + f4in1_trades
        today_trades = print_today_signals(all_for_today, today_str)
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
    all_out = dc_trades + v1_trades + bb_trades + f4in1_trades
    if all_out:
        with open("test_dragon_callback_result.json", "w", encoding="utf-8") as f:
            json.dump(all_out, f, ensure_ascii=False, indent=2)
        print(f"\n💾 test_dragon_callback_result.json ({len(all_out)}笔)")

if __name__ == "__main__":
    main()

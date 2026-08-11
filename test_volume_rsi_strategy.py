#!/usr/bin/env python3
"""
量价RSI策略 - 独立回测文件

═══════════════════════════════════════════════════════════════════
  入场规则（三重确认，缺一不可）
═══════════════════════════════════════════════════════════════════

  ① MACD底背离 + SMA(18)斜率>0
     - DIF 在当前点前30根K线内形成局部低谷
     - 当前价格创新低，但 DIF 未创新低 → 底背离
     - 同时 SMA(18) 斜率为正（中期趋势向上）

  ② 前40日到前5日有一日或多日的 RSI < 25（超卖区）
     - 从近到远搜索，取最后一个 RSI<25 的日子
     - 确保曾经深度超跌过

  ③ 从最后一个 RSI<25 日到前一日，区间涨幅在 3%~10% 之间
     - 涨幅 < 3%：反弹力度不足，不入场
     - 涨幅 > 10%（可调 --gain-threshold）：已涨太多，追高风险

  买入方式：信号日次日开盘买入（D+1 Open）

═══════════════════════════════════════════════════════════════════
  出场规则（任一触发即出场）
═══════════════════════════════════════════════════════════════════

  ① RSI > 75 且 量比 > 2.0
     - RSI 进入超买区 + 放量，可能是短期顶部
  ② RSI > 82
     - 极端超买，无论量比多少都出场
  ③ 持仓天数上限（默认60天，可调 --max-hold，0=不限制）
     - 超时未触发出场条件，强制平仓
  ④ 数据耗尽
     - 回测数据结束时强制平仓

═══════════════════════════════════════════════════════════════════
  使用方法
═══════════════════════════════════════════════════════════════════

  # 内置200只股票快速回测
  python test_volume_rsi_strategy.py

  # 全市场回测（从DB加载所有股票）
  python test_volume_rsi_strategy.py --source db

  # 指定股票测试
  python test_volume_rsi_strategy.py --codes 000001,600519,300750

  # 全市场 + 打印全部交易明细
  python test_volume_rsi_strategy.py --source db --all-trades

  # 跟踪止损（从峰值回撤3%出场）
  python test_volume_rsi_strategy.py --source db --exit-mode trail --trail-pct 3

  # 动量出场（RSI回落5点 或 MACD死叉）
  python test_volume_rsi_strategy.py --source db --exit-mode momentum --rsi-drop 5

  # 缩量滞涨（量缩至60%均量 + 价格在峰值2%内）
  python test_volume_rsi_strategy.py --source db --exit-mode vol_stagnation --vol-shrink 0.6

  # 每天只选1个最优信号（RSI最低优先）
  python test_volume_rsi_strategy.py --source db --exit-mode momentum --top-per-day 1

  # 每天选2个 + 动量出场（推荐配置，参数已内置）
  python test_volume_rsi_strategy.py --source db --exit-mode momentum --top-per-day 2

  # 不过滤（同一天所有信号都保留）
  python test_volume_rsi_strategy.py --source db --exit-mode momentum --top-per-day 0

═══════════════════════════════════════════════════════════════════
  每日信号筛选（--top-per-day）
═══════════════════════════════════════════════════════════════════

  同一天可能触发多个信号，资金有限时需选最优的1-2个。
  排序优先级（越靠前越优先）：
    1. RSI 越低越好（更超跌，后续反弹空间大）
    2. MACD柱越负越好（死叉更深，底背离更明显）
    3. 换手率越高越好（关注度高，反弹力度强）
    4. gain_from_rsi30 越小越好（刚起步，还没涨多少）

  --top-per-day 2  每天最多选前2个（默认）
  --top-per-day 1  每天只选最优1个
  --top-per-day 0  不过滤，保留全部信号

═══════════════════════════════════════════════════════════════════
  出场模式说明（--exit-mode）
═══════════════════════════════════════════════════════════════════

  rsi（默认）
    仅用RSI超买出场：RSI>75+量比>2 或 RSI>82
    问题：出场条件太苛刻，89%的交易等不到出场信号

  trail（跟踪止损）
    在RSI出场基础上，增加价格从峰值回撤X%的止损
    参数：--trail-pct（默认3.0%）
    效果：胜率 43.8%→64.4%，均值 -2.15%→+3.76%

  momentum（动量出场）
    在RSI出场基础上，增加动量出场条件：
    - RSI从持仓期峰值回落N点（参数：--rsi-drop，默认5）
    - 从峰值回撤X%强制出场（参数：--drawdown-pct，默认8%）
    优点：出场点接近峰值，保留利润最多

  vol_stagnation（缩量滞涨）
    在RSI出场基础上，增加缩量滞涨出场：
    - 当日量 < 5日均量 * X倍（参数：--vol-shrink，默认0.6）
    - 价格在峰值2%以内（高位横盘）
    - 兜底：峰值回撤 --trail-pct% 出场
    优点：出场最接近峰值，保留利润最多

═══════════════════════════════════════════════════════════════════
  参数说明
═══════════════════════════════════════════════════════════════════

  --codes          逗号分隔的股票代码（空=使用内置TEST_CODES）
  --source         数据源: manual（默认）或 db（全市场）
  --days           加载K线天数（默认300）
  --rsi-len        RSI 周期（默认14）
  --rsi-oversold   RSI 超卖阈值（默认25）
  --rsi-overbought RSI 超买阈值，出场条件①（默认75）
  --rsi-extreme    RSI 极端超买阈值，出场条件②（默认82）
  --max-hold       最大持仓天数（默认15，0=不限制）
  --gain-threshold RSI<30后涨幅阈值%（默认8.0）
  --exit-mode      出场模式: rsi/trail/momentum/vol_stagnation（默认rsi）
  --trail-pct      跟踪止损回撤%（默认3.0，trail/vol_stagnation模式）
  --rsi-drop       RSI回落点数（默认5，momentum模式）
  --drawdown-pct   峰值回撤强制出场%（默认8.0，momentum模式）
  --vol-shrink     缩量阈值（默认0.6，vol_stagnation模式）
  --top-per-day    每天最多选前N个最优信号（默认2，0=不过滤）
  --top            显示TOP N（默认10）
  --all-trades     打印全部交易明细
  --today          仅统计今日买点
  --today-date     指定日期（YYYY-MM-DD）
"""
from __future__ import annotations
import json, time, argparse, os, sys

# ================================================================
# DB 数据加载
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

_circ_shares_cache = None
def _get_circ_shares(code):
    """从 stock_basic_info 表读取流通股本(股)"""
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
# kline_cache 数据加载
# ================================================================
from kline_cache import fetch_kline

# ================================================================
# 工具函数
# ================================================================
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
# RSI 计算 (Wilder 平滑)
# ================================================================
def compute_rsi(closes, rsi_len=14):
    """
    用前 rsi_len 个变化量的简单平均初始化 avg_gain/avg_loss,
    然后用 Wilder 指数平滑递推。
    """
    n = len(closes)
    if n < rsi_len + 1:
        return [50.0] * n

    gains = []
    losses = []
    for i in range(1, rsi_len + 1):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))

    avg_gain = sum(gains) / rsi_len
    avg_loss = sum(losses) / rsi_len

    rsi_out = [50.0] * (rsi_len + 1)
    if avg_loss == 0:
        rsi_out[rsi_len] = 100.0 if avg_gain > 0 else 50.0
    else:
        rs = avg_gain / avg_loss
        rsi_out[rsi_len] = 100.0 - (100.0 / (1.0 + rs))

    alpha = 1.0 / rsi_len
    for i in range(rsi_len + 1, n):
        delta = closes[i] - closes[i - 1]
        g = max(delta, 0.0)
        l = max(-delta, 0.0)
        avg_gain = alpha * g + (1 - alpha) * avg_gain
        avg_loss = alpha * l + (1 - alpha) * avg_loss
        if avg_loss == 0:
            rsi_out.append(100.0 if avg_gain > 0 else 50.0)
        else:
            rs = avg_gain / avg_loss
            rsi_out.append(100.0 - (100.0 / (1.0 + rs)))

    return rsi_out

# ================================================================
# EMA 计算
# ================================================================
def compute_ema(closes, period):
    """计算EMA序列"""
    n = len(closes)
    ema = [0.0] * n
    if n < period:
        return ema

    # 用SMA初始化
    ema[period - 1] = sum(closes[:period]) / period
    k = 2.0 / (period + 1)
    for i in range(period, n):
        ema[i] = closes[i] * k + ema[i - 1] * (1 - k)

    return ema

# ================================================================
# MA 计算
# ================================================================
def compute_ma(closes, period):
    """计算简单移动平均线"""
    n = len(closes)
    ma = [0.0] * n
    for i in range(period - 1, n):
        ma[i] = sum(closes[i - period + 1:i + 1]) / period
    return ma

# ================================================================
# MACD 计算
# ================================================================
def compute_macd(closes, fast=12, slow=26, signal=9):
    """
    计算 MACD 指标
    返回: (dif, dea, macd_hist)
      - dif: 快线EMA - 慢线EMA
      - dea: dif 的 signal 周期 EMA
      - macd_hist: (dif - dea) * 2 (柱状图)
    """
    ema_fast = compute_ema(closes, fast)
    ema_slow = compute_ema(closes, slow)
    n = len(closes)
    dif = [0.0] * n
    for i in range(n):
        dif[i] = ema_fast[i] - ema_slow[i]
    dea = compute_ema(dif, signal)
    macd_hist = [0.0] * n
    for i in range(n):
        macd_hist[i] = (dif[i] - dea[i]) * 2
    return dif, dea, macd_hist

# ================================================================
# 回测引擎
# ================================================================
def run_backtest(bars, entry_idx, entry_price, exit_signals,
                 max_hold_days=15, exit_mode="rsi",
                 rsi_values=None, dif=None, dea=None, macd_hist=None,
                 trail_pct=3.0, rsi_drop=5, vol_shrink=0.6,
                 drawdown_pct=8.0):
    """
    出场规则（由 exit_mode 选择）:

    rsi:  原策略
      ① RSI>75 且量比>2  ② RSI>82  ③ 持仓天数上限  ④ 数据耗尽

    trail: 跟踪止损
      ① RSI原策略出场  ② 价格从峰值回撤 trail_pct% 出场
      ③ 持仓天数上限  ④ 数据耗尽

    momentum: 动量出场
      ① RSI原策略出场  ② RSI从持仓期峰值回落 rsi_drop 点出场
      ③ 价格从峰值回撤 drawdown_pct% 强制出场  ④ 持仓天数上限  ⑤ 数据耗尽

    vol_stagnation: 缩量滞涨
      ① RSI原策略出场  ② 量缩至5日均量 vol_shrink 倍 + 价格在峰值附近(2%内)
      ③ 价格从峰值回撤 trail_pct% 出场（兜底）
      ④ 持仓天数上限  ⑤ 数据耗尽

    max_hold_days=0 表示不限制
    """
    if entry_price <= 0 or entry_idx >= len(bars):
        return None

    peak = entry_price
    exit_p = entry_price
    exit_d = 0
    exit_reason = "data_end"
    max_d = len(bars) - entry_idx - 1

    # 买入当天就是最后一天，无法回测
    if max_d <= 0:
        return None

    # 动量出场：持仓期RSI峰值追踪
    hold_rsi_peak = 0.0

    for d in range(1, max_d + 1):
        idx = entry_idx + d
        if idx >= len(bars):
            break
        b = bars[idx]
        if b['high'] > peak:
            peak = b['high']

        # ---- RSI原策略出场（所有模式共用） ----
        if exit_signals[idx]:
            exit_p = b['close']
            exit_d = d
            exit_reason = "signal_exit"
            break

        # ---- 跟踪止损 ----
        if exit_mode == "trail" and peak > entry_price:
            drawdown = (peak - b['close']) / peak * 100
            if drawdown >= trail_pct:
                exit_p = b['close']
                exit_d = d
                exit_reason = "trail_stop"
                break

        # ---- 动量出场 ----
        if exit_mode == "momentum" and rsi_values is not None:
            cur_rsi = rsi_values[idx]
            # 更新持仓期RSI峰值
            if cur_rsi > hold_rsi_peak:
                hold_rsi_peak = cur_rsi
            # RSI从峰值回落 rsi_drop 点
            if hold_rsi_peak > 50 and (hold_rsi_peak - cur_rsi) >= rsi_drop:
                exit_p = b['close']
                exit_d = d
                exit_reason = "rsi_drop"
                break
            # 从峰值回撤 drawdown_pct% 强制出场
            if peak > entry_price:
                dd = (peak - b['close']) / peak * 100
                if dd >= drawdown_pct:
                    exit_p = b['close']
                    exit_d = d
                    exit_reason = "drawdown_stop"
                    break
            # 绝对亏损止损：从买入价亏了 drawdown_pct% 强制出场
            abs_loss = (entry_price - b['close']) / entry_price * 100
            if abs_loss >= drawdown_pct:
                exit_p = b['close']
                exit_d = d
                exit_reason = "drawdown_stop"
                break

        # ---- 缩量滞涨 ----
        if exit_mode == "vol_stagnation" and rsi_values is not None:
            # 计算近5日均量
            if idx >= 5:
                vol_ma5 = sum(bars[j]['volume'] for j in range(idx - 4, idx + 1)) / 5
            else:
                vol_ma5 = b['volume']
            # 量缩 + 价格在峰值附近（滞涨确认）
            vol_ratio_cur = b['volume'] / vol_ma5 if vol_ma5 > 0 else 1.0
            price_near_peak = (peak - b['close']) / peak * 100 < 2 if peak > 0 else False
            if vol_ratio_cur < vol_shrink and price_near_peak and peak > entry_price * 1.05:
                exit_p = b['close']
                exit_d = d
                exit_reason = "vol_stagnation"
                break
            # 兜底：价格从峰值回撤 trail_pct%
            drawdown = (peak - b['close']) / peak * 100 if peak > 0 else 0
            if drawdown >= trail_pct and peak > entry_price * 1.03:
                exit_p = b['close']
                exit_d = d
                exit_reason = "trail_stop"
                break

        # ---- 持仓天数上限 ----
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
# 量价RSI策略信号生成 + 回测
# ================================================================
def _find_prev_dif_trough(dif, cur_idx, lookback=30):
    """从cur_idx往前找DIF的局部低谷(波谷), 返回低谷索引, 找不到返回-1"""
    if cur_idx < 2:
        return -1
    start = max(0, cur_idx - lookback)
    # 从cur_idx-1往前扫描, 找到DIF开始上升的转折点(即波谷)
    for j in range(cur_idx - 1, start, -1):
        if j < 1:
            break
        # 波谷: dif[j] <= dif[j-1] 且 dif[j] <= dif[j+1]
        # 简化: 从右往左找, 当dif开始上升时, j就是波谷
        if dif[j] < dif[j - 1]:
            continue
        # dif[j] >= dif[j-1], 说明j是波谷
        return j
    return -1


def _check_dif_bottom_divergence(closes, dif, cur_idx, lookback=30):
    """
    DIF底背离检测:
      在cur_idx之前lookback根K线范围内, 寻找DIF的前一个波谷,
      若该波谷处的价格 >= cur_idx处的价格, 但该波谷处的DIF < cur_idx处的DIF,
      则判定为DIF底背离。
    返回 (is_divergent, prev_trough_idx)
    """
    prev_idx = _find_prev_dif_trough(dif, cur_idx, lookback)
    if prev_idx < 0:
        return False, -1
    # 前波谷价格(取close) >= 当前价格, 且 前波谷DIF < 当前DIF
    if closes[prev_idx] >= closes[cur_idx] and dif[prev_idx] < dif[cur_idx]:
        return True, prev_idx
    return False, -1


def strategy_volume_rsi(bars, code, rsi_len=14, rsi_oversold=25,
                        rsi_overbought=75, rsi_extreme=82,
                        max_hold_days=15, gain_threshold=10.0,
                        circ_shares=0.0, exit_mode="rsi",
                        trail_pct=3.0, rsi_drop=5, vol_shrink=0.6,
                        top_per_day=2, drawdown_pct=8.0):
    """
    量价RSI策略:

    入场条件:
      ① MACD底背离 + SMA(18)斜率>0
      ② 前40日到前5日有一日或多日的RSI<25
      ③ 从最后一个RSI<25的日起到前一日区间涨幅在3%~10%之间

    出场条件:
      ① RSI>75 且 量比>2.0
      ② RSI>82
    """
    if len(bars) < 45:  # 需要至少45根K线
        return []

    opens = [b['open'] for b in bars]
    closes = [b['close'] for b in bars]
    highs = [b['high'] for b in bars]
    lows = [b['low'] for b in bars]
    volumes = [b['volume'] for b in bars]

    # ---- 计算指标 ----
    rsi_values = compute_rsi(closes, rsi_len)
    ma20 = compute_ma(closes, 20)
    sma18 = compute_ma(closes, 18)
    dif, dea, macd_hist = compute_macd(closes)

    # ---- 出场信号预计算 ----
    exit_signal = [False] * len(bars)
    for i in range(len(bars)):
        # 条件①: RSI>75 且 量比>2.0 (今日成交量 / 昨日成交量)
        vol_ratio_i = volumes[i] / volumes[i - 1] if i >= 1 and volumes[i - 1] > 0 else 0
        if rsi_values[i] > rsi_overbought and vol_ratio_i > 2.0:
            exit_signal[i] = True
        # 条件②: RSI>82
        if rsi_values[i] > rsi_extreme:
            exit_signal[i] = True

    # ---- 入场信号检测 ----
    buy_signal = [False] * len(bars)
    last_rsi30_date = [None] * len(bars)   # 最后一个RSI<25的日期
    last_rsi30_idx_arr = [-1] * len(bars)  # 最后一个RSI<25的索引
    gain_from_rsi30_arr = [0.0] * len(bars)

    for i in range(30, len(bars)):
        # 条件①: MACD底背离 + SMA(18)斜率>0
        if sma18[i] <= sma18[i - 1]:
            continue
        is_div, _ = _check_dif_bottom_divergence(closes, dif, i, lookback=30)
        if not is_div:
            continue

        # 条件②: 前40日到前5日有RSI<25的日子(从近到远搜索, 取最后一个)
        last_rsi30_idx = -1
        for j in range(i - 5, max(i - 41, -1), -1):
            if j < 0:
                break
            if rsi_values[j] < rsi_oversold:
                last_rsi30_idx = j
                break

        if last_rsi30_idx < 0:
            continue

        # 条件③: 从最后一个RSI<25日到前一日区间涨幅在3%~10%之间
        price_at_rsi30 = closes[last_rsi30_idx]
        if price_at_rsi30 <= 0:
            continue
        gain_pct = (closes[i - 1] / price_at_rsi30 - 1) * 100
        if gain_pct >= gain_threshold or gain_pct <= 3.0:
            continue

        buy_signal[i] = True
        last_rsi30_date[i] = bars[last_rsi30_idx]['time']
        last_rsi30_idx_arr[i] = last_rsi30_idx
        gain_from_rsi30_arr[i] = round(gain_pct, 2)

    # ---- 生成交易记录 ----
    # 先收集所有候选信号，再按优先级排序筛选
    candidates = []
    for i in range(30, len(bars)):
        if not buy_signal[i]:
            continue
        signal_date = bars[i]['time']
        if i + 1 >= len(bars):
            continue
        entry_price = bars[i + 1]['open']
        if entry_price <= 0:
            continue
        vol_ratio = volumes[i] / volumes[i - 1] if i >= 1 and volumes[i - 1] > 0 else 0
        today_gain = (closes[i] / closes[i - 1] - 1) * 100 if i >= 1 and closes[i - 1] > 0 else 0
        turnover = volumes[i] / circ_shares * 100 if circ_shares > 0 else 0
        candidates.append({
            'idx': i,
            'signal_date': signal_date,
            'entry_price': entry_price,
            'entry_idx': i + 1,
            'entry_date': bars[i + 1]['time'],
            'rsi': rsi_values[i],
            'macd': macd_hist[i],
            'vol_ratio': vol_ratio,
            'turnover': turnover,
            'gain_from_rsi30': gain_from_rsi30_arr[i],
            'today_gain': today_gain,
            'signal_close': closes[i],
            'signal_ma20': ma20[i],
            'signal_dif': dif[i],
            'signal_dea': dea[i],
            'signal_volume': volumes[i],
        })

    # 同一天按优先级排序，每天最多选 top_per_day 个
    # 优先级: RSI低 > MACD柱负 > 换手率高 > gain_from_rsi30小
    from collections import defaultdict
    daily_candidates = defaultdict(list)
    for c in candidates:
        daily_candidates[c['signal_date']].append(c)

    filtered_candidates = []
    for date, cands in daily_candidates.items():
        cands.sort(key=lambda c: (
            c['rsi'],                    # RSI越低越好
            c['macd'],                    # MACD柱越负越好
            -c['turnover'],               # 换手率越高越好
            c['gain_from_rsi30'],         # 涨幅越小越好
        ))
        filtered_candidates.extend(cands[:top_per_day])

    # 生成交易记录
    trades = []
    for c in filtered_candidates:
        i = c['idx']
        result = run_backtest(bars, c['entry_idx'], c['entry_price'], exit_signal,
                              max_hold_days, exit_mode=exit_mode,
                              rsi_values=rsi_values, dif=dif, dea=dea,
                              macd_hist=macd_hist,
                              trail_pct=trail_pct, rsi_drop=rsi_drop,
                              vol_shrink=vol_shrink, drawdown_pct=drawdown_pct)
        if not result:
            continue

        trades.append({
            'code': code,
            'board': get_board_name(code),
            'path': 'volume_rsi',
            'path_label': '量价RSI',
            'signal_date': c['signal_date'],
            'signal_rsi': round(c['rsi'], 2),
            'signal_close': c['signal_close'],
            'signal_ma20': round(c['signal_ma20'], 3),
            'signal_dif': round(c['signal_dif'], 4),
            'signal_dea': round(c['signal_dea'], 4),
            'signal_macd': round(c['macd'], 4),
            'signal_volume': c['signal_volume'],
            'signal_vol_ratio': round(c['vol_ratio'], 3),
            'signal_today_gain': round(c['today_gain'], 2),
            'signal_turnover': round(c['turnover'], 2),
            'last_rsi30_date': last_rsi30_date[i],
            'gain_from_rsi30': gain_from_rsi30_arr[i],
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
    n = len(trades)
    wins = [t for t in trades if t['return_pct'] > 0]
    losses = [t for t in trades if t['return_pct'] < 0]
    breakeven = [t for t in trades if t['return_pct'] == 0]
    wr = len(wins) / n * 100 if n > 0 else 0
    avg = sum(t['return_pct'] for t in trades) / n if n > 0 else 0
    peak = sum(t['peak_return_pct'] for t in trades) / n if n > 0 else 0
    avg_win = sum(t['return_pct'] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t['return_pct'] for t in losses) / len(losses) if losses else 0
    pl = avg_win / abs(avg_loss) if avg_loss != 0 else (999.0 if wins else 0.0)
    total_ret = sum(t['return_pct'] for t in trades)
    worst = min(t['return_pct'] for t in trades) if trades else 0
    print(f"  {label}: {n:>4}笔 胜率{wr:>5.1f}% 均收益{avg:>+6.2f}% 均峰值{peak:>+6.2f}% "
          f"盈亏比{pl:.2f} 总收益{total_ret:>+6.2f}% 最大单笔{worst:>+6.2f}%")

def print_today_signals(all_trades, today_str):
    today_trades = [t for t in all_trades if t['entry_date'] == today_str]
    print(f"\n{'=' * 80}")
    print(f"  {today_str} 今日买点统计 (量价RSI)")
    print(f"{'=' * 80}")
    if not today_trades:
        print(f"  今日无买点信号")
        return today_trades

    print(f"  共 {len(today_trades)} 只股票出现买入信号")

    main_today = [t for t in today_trades if t['board'] in ('沪主板', '深主板')]
    gem_today  = [t for t in today_trades if t['board'] == '创业板']
    star_today = [t for t in today_trades if t['board'] == '科创板']

    print(f"\n  板块分布:")
    if main_today:  print(f"    主板: {len(main_today)} 只")
    if gem_today:   print(f"    创业板: {len(gem_today)} 只")
    if star_today:  print(f"    科创板: {len(star_today)} 只")

    print(f"\n  今日信号 ({len(today_trades)}只):")
    for t in sorted(today_trades, key=lambda x: x['signal_rsi']):
        print(f"    {t['code']:<8} {t['board']:<6} RSI={t['signal_rsi']:.1f} "
              f"量比={t['signal_vol_ratio']:.2f} 换手={t['signal_turnover']:.1f}% "
              f"买入{t['entry_price']:.2f} 涨幅={t['gain_from_rsi30']:.1f}%")

    return today_trades

# ================================================================
# 主函数
# ================================================================
def main():
    parser = argparse.ArgumentParser(description="量价RSI策略 独立回测")
    parser.add_argument("--codes", default="", help="逗号分隔的股票代码")
    parser.add_argument("--days", type=int, default=300, help="加载K线天数 (默认300)")
    parser.add_argument("--source", choices=["manual", "db"], default="manual",
                        help="数据源: manual(默认), db")
    parser.add_argument("--rsi-len", type=int, default=14, help="RSI 周期 (默认14)")
    parser.add_argument("--rsi-oversold", type=float, default=25, help="RSI 超卖阈值 (默认25)")
    parser.add_argument("--rsi-overbought", type=float, default=75, help="RSI 超买阈值 (默认75)")
    parser.add_argument("--rsi-extreme", type=float, default=82, help="RSI 极端超买阈值 (默认82)")
    parser.add_argument("--max-hold", type=int, default=15, help="最大持仓天数 (默认15, 0=不限制)")
    parser.add_argument("--all-trades", action="store_true", help="打印全部交易明细")
    parser.add_argument("--today", action="store_true", help="仅统计今日买点")
    parser.add_argument("--today-date", type=str, default="", help="指定日期(YYYY-MM-DD)")
    parser.add_argument("--top", type=int, default=10, help="显示TOP N (默认10)")
    parser.add_argument("--gain-threshold", type=float, default=8.0, help="RSI30后涨幅阈值%% (默认8.0)")
    parser.add_argument("--exit-mode", choices=["rsi", "trail", "momentum", "vol_stagnation"],
                        default="rsi",
                        help="出场模式: rsi(原策略), trail(跟踪止损), momentum(动量出场), vol_stagnation(缩量滞涨)")
    parser.add_argument("--trail-pct", type=float, default=3.0,
                        help="跟踪止损回撤百分比 (默认3.0, 仅trail/vol_stagnation模式)")
    parser.add_argument("--rsi-drop", type=int, default=5,
                        help="RSI从峰值回落点数 (默认5, 仅momentum模式)")
    parser.add_argument("--vol-shrink", type=float, default=0.6,
                        help="缩量阈值: 当日量/5日均量 (默认0.6, 仅vol_stagnation模式)")
    parser.add_argument("--top-per-day", type=int, default=2,
                        help="每天最多选前N个最优信号 (默认2, 0=不过滤)")
    parser.add_argument("--drawdown-pct", type=float, default=8.0,
                        help="从峰值回撤强制出场百分比 (默认8.0, 仅momentum模式)")
    args = parser.parse_args()

    codes = [c.strip() for c in args.codes.split(",") if c.strip()] if args.codes else TEST_CODES
    use_db = args.source == "db"

    if use_db:
        print("  DB模式: 从数据库加载全市场股票...")
        codes = get_all_codes_db()
        print(f"   全市场: {len(codes)} 只股票")

    print(f"{'=' * 80}")
    print(f"量价RSI策略 独立回测")
    print(f"{'=' * 80}")
    print(f"入场条件:")
    print(f"  ① MACD底背离 + SMA(18)斜率>0")
    print(f"  ② 前40日到前5日有RSI<{args.rsi_oversold}")
    print(f"  ③ 从最后一个RSI<{args.rsi_oversold}日起到前一日区间涨幅在3%~{args.gain_threshold}%之间")
    print(f"出场条件:")
    if args.exit_mode == "rsi":
        print(f"  ① RSI>{args.rsi_overbought} 且 量比>2.0")
        print(f"  ② RSI>{args.rsi_extreme}")
    elif args.exit_mode == "trail":
        print(f"  ① RSI>{args.rsi_overbought} 且 量比>2.0 / RSI>{args.rsi_extreme}")
        print(f"  ② 跟踪止损: 峰值回撤{args.trail_pct}%")
    elif args.exit_mode == "momentum":
        print(f"  ① RSI>{args.rsi_overbought} 且 量比>2.0 / RSI>{args.rsi_extreme}")
        print(f"  ② RSI从峰值回落{args.rsi_drop}点")
        print(f"  ③ 峰值回撤{args.drawdown_pct}%强制出场")
    elif args.exit_mode == "vol_stagnation":
        print(f"  ① RSI>{args.rsi_overbought} 且 量比>2.0 / RSI>{args.rsi_extreme}")
        print(f"  ② 缩量滞涨: 量<{args.vol_shrink}*5日均量 + 价格在峰值2%内")
        print(f"  ③ 兜底: 峰值回撤{args.trail_pct}%")
    print(f"风控: 最大持仓{args.max_hold}天")
    print(f"出场模式: {args.exit_mode}")
    print(f"买入模式: D+1开盘买")
    if args.top_per_day > 0:
        print(f"每日筛选: 同一天最多选前{args.top_per_day}个 (排序: RSI低>MACD负>换手率高>涨幅小)")
    else:
        print(f"每日筛选: 不过滤")
    print(f"股票: {len(codes)}只\n")

    all_trades = []
    success = 0

    for i, code in enumerate(codes):
        bars = fetch_kline_db(code, args.days) if use_db else fetch_kline(code, args.days)
        if not bars:
            continue

        trades = strategy_volume_rsi(
            bars, code,
            rsi_len=args.rsi_len,
            rsi_oversold=args.rsi_oversold,
            rsi_overbought=args.rsi_overbought,
            rsi_extreme=args.rsi_extreme,
            max_hold_days=args.max_hold,
            gain_threshold=args.gain_threshold,
            circ_shares=_get_circ_shares(code),
            exit_mode=args.exit_mode,
            trail_pct=args.trail_pct,
            rsi_drop=args.rsi_drop,
            vol_shrink=args.vol_shrink,
            top_per_day=args.top_per_day,
            drawdown_pct=args.drawdown_pct,
        )
        all_trades.extend(trades)

        if trades:
            print(f"[{i+1}/{len(codes)}] {code} ({get_board_name(code)}) "
                  f"  {len(bars)}根 -> 信号{len(trades)}笔")

        success += 1
        if not use_db:
            time.sleep(0.15)

    # ===== 结果统计 =====
    print(f"\n{'=' * 80}")
    print(f"结果: {success}只, 共 {len(all_trades)} 笔交易")
    print(f"{'=' * 80}")

    print_stats(all_trades, "量价RSI策略")

    if all_trades:
        # RSI 分布
        print(f"\n  信号RSI分布:")
        for lo, hi, label in [(0, 20, "RSI<20"), (20, 30, "RSI 20~30"),
                               (30, 40, "RSI 30~40"), (40, 50, "RSI 40~50"),
                               (50, 100, "RSI>=50")]:
            seg = [t for t in all_trades if lo <= t['signal_rsi'] < hi]
            if seg:
                print_stats(seg, f"    {label}")

        # 量比分布
        print(f"\n  信号量比(今日/昨日)分布:")
        for lo, hi, label in [(1.0, 1.5, "量比 1.0~1.5"), (1.5, 2.0, "量比 1.5~2.0"),
                               (2.0, 3.0, "量比 2.0~3.0"), (3.0, 999, "量比 >3.0")]:
            seg = [t for t in all_trades if lo <= t['signal_vol_ratio'] < hi]
            if seg:
                print_stats(seg, f"    {label}")

        # 涨幅分布
        print(f"\n  RSI30以来涨幅分布:")
        for lo, hi, label in [(0, 3, "涨幅 0~3%"), (3, 5, "涨幅 3~5%"),
                               (5, 8, "涨幅 5~8%"), (8, 12, "涨幅 8~12%"),
                               (12, 16, "涨幅 12~16%")]:
            seg = [t for t in all_trades if lo <= t.get('gain_from_rsi30', 0) < hi]
            if seg:
                print_stats(seg, f"    {label}")

        # 换手率分布
        print(f"\n  信号换手率分布:")
        for lo, hi, label in [(5, 10, "换手 5~10%"), (10, 15, "换手 10~15%"),
                               (15, 20, "换手 15~20%"), (20, 30, "换手 20~30%"),
                               (30, 999, "换手 >30%")]:
            seg = [t for t in all_trades if lo <= t.get('signal_turnover', 0) < hi]
            if seg:
                print_stats(seg, f"    {label}")

        # 出场原因分布
        print(f"\n  出场原因分布:")
        for reason, label in [("signal_exit", "信号出场"),
                               ("max_hold", "持仓到期"), ("data_end", "数据耗尽")]:
            seg = [t for t in all_trades if t.get('exit_reason') == reason]
            if seg:
                print_stats(seg, f"    {label}")

        # 板块分布
        print(f"\n  板块分布:")
        for board in ['沪主板', '深主板', '创业板', '科创板']:
            seg = [t for t in all_trades if t['board'] == board]
            if seg:
                print_stats(seg, f"    {board}")

        # TOP N
        n = min(args.top, len(all_trades))
        print(f"\n  TOP{n} 盈利:")
        for t in sorted(all_trades, key=lambda x: -x['return_pct'])[:n]:
            print(f"    {t['code']:<8} {t['board']:<6} RSI={t['signal_rsi']:.1f} "
                  f"量比={t['signal_vol_ratio']:.2f} 换手={t['signal_turnover']:.1f}% "
                  f"{t['entry_date']}买{t['entry_price']:>7.2f} -> "
                  f"收益{t['return_pct']:>+6.2f}% 峰值{t['peak_return_pct']:>+6.2f}% "
                  f"持仓{t['exit_day']}天")

        print(f"\n  TOP{n} 亏损:")
        for t in sorted(all_trades, key=lambda x: x['return_pct'])[:n]:
            print(f"    {t['code']:<8} {t['board']:<6} RSI={t['signal_rsi']:.1f} "
                  f"量比={t['signal_vol_ratio']:.2f} 换手={t['signal_turnover']:.1f}% "
                  f"{t['entry_date']}买{t['entry_price']:>7.2f} -> "
                  f"收益{t['return_pct']:>+6.2f}% 峰值{t['peak_return_pct']:>+6.2f}% "
                  f"持仓{t['exit_day']}天")

    # ===== 今日买点 =====
    if args.today:
        from datetime import datetime
        today_str = args.today_date if args.today_date else datetime.now().strftime("%Y-%m-%d")
        today_trades = print_today_signals(all_trades, today_str)
        if today_trades:
            out_file = f"volume_rsi_today_signals_{today_str}.json"
            with open(out_file, "w", encoding="utf-8") as f:
                json.dump(today_trades, f, ensure_ascii=False, indent=2)
            print(f"\n  {out_file} ({len(today_trades)}笔)")

    # ===== 交易明细 =====
    if args.all_trades and all_trades:
        print(f"\n  全部交易明细:")
        for t in sorted(all_trades, key=lambda x: x['entry_date']):
            print(f"  {t['code']:<8} {t['board']:<6} "
                  f"RSI={t['signal_rsi']:.1f} 量比={t['signal_vol_ratio']:.2f} 换手={t['signal_turnover']:.1f}% -> "
                  f"{t['entry_date']}买{t['entry_price']:>7.2f} "
                  f"-> 收益{t['return_pct']:>+6.2f}% 峰值{t['peak_return_pct']:>+6.2f}% "
                  f"持仓{t['exit_day']}天")

    # ===== 导出 =====
    if all_trades:
        out_file = "test_volume_rsi_strategy_result.json"
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(all_trades, f, ensure_ascii=False, indent=2)
        print(f"\n  {out_file} ({len(all_trades)}笔)")

if __name__ == "__main__":
    main()

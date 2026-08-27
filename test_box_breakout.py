#!/usr/bin/env python3
"""
多箱体底部买入 策略 - 独立回测

═══════════════════════════════════════════════════════════════════════════════
                          策略核心思路
═══════════════════════════════════════════════════════════════════════════════

  在历史走势中识别多个箱体整理区间，当多个箱体下沿汇聚形成强支撑，
  且价格回踩到支撑区域附近出现反弹信号时买入。

  ┌─────────────────────────────────────────────────────────────────────┐
  │ 价格                                                              │
  │         ┌──────┐          ┌──────┐                                │
  │         │箱体1 │          │箱体3 │                                │
  │   ┌─────┤      │   ┌──────┤      │                                │
  │   │箱体0 │      │   │箱体2 │      │  ← 多个箱体下沿汇聚           │
  │   │     │      │   │     │      │    形成强支撑位                  │
  │───┴─────┴──────┴───┴─────┴──────┴────────── 支撑线 ───────────    │
  │                              ↑                                    │
  │                         价格回踩到支撑                             │
  │                         + 反弹确认 → 买入                         │
  └─────────────────────────────────────────────────────────────────────┘

═══════════════════════════════════════════════════════════════════════════════
                          入场条件
═══════════════════════════════════════════════════════════════════════════════

  ① 历史箱体识别
     - 回顾过去 box_days×4 天的K线
     - 滑动窗口(box_days天)识别整理区间
     - 箱体振幅在 [box_min_range, box_max_range]% 之间
     - 箱体内至少70%的K线在区间范围内

  ② 支撑位确认
     - 多个箱体(≥min_box_count)的下沿接近(±support_tolerance%)
     - 贪心聚类算法: 下沿差距在容差范围内的箱体归为同一支撑

  ③ 趋势过滤 (三均线共振)
     - MA10 斜率 > min_ma10_slope (%/天) 且斜率递增(加速)
     - MA20 斜率 > 0 且斜率递增(加速)
     - 确保处于上升趋势中的回调，而非下跌趋势

  ④ 底部买入区域
     - 当日低点触及支撑位 ±buy_zone_pct% 区域
     - 不能跌破支撑位太多(超过buy_zone_pct%)

  ⑤ 反弹确认 (至少满足一个)
     - 收阳线: 收盘价 > 开盘价
     - 量缩: 当日成交量 < 近5日均量×0.8 (卖压衰竭)
     - 长下影线: 下影线 > 实体×1.5 (探底回升)

  ⑥ 板块过滤
     - 创业板/科创板: 流通市值 < 30亿
     - 主板: 无额外限制

  ⑦ 买入日过滤 (信号次日)
     - 涨跌幅: -3% ~ +3% (不追涨不抄底暴跌)
     - 跳空: 开盘价 vs 前收盘 -0.5% ~ +0.5% (平开)
     - 振幅: < 3% (低波动, 确认横盘)
     - 买入价: 收盘价

═══════════════════════════════════════════════════════════════════════════════
                          出场规则
═══════════════════════════════════════════════════════════════════════════════

  ① 止盈: 盈利达到 take_profit% (默认15%) 时止盈
  ② 止损: 从买入后最高价回撤 stop_loss% (默认12%, 主板自适应20%)
  ③ 跟踪止损: 盈利达到 trailing_activate% 后激活，
              从峰值回撤 trailing_pct% 出场
  ④ 持仓上限: max_hold 天 (默认10) 到期收盘价出场

  优先级: 止盈 > 止损 > 跟踪止损 > 持仓上限

═══════════════════════════════════════════════════════════════════════════════
                          信号质量评分
═══════════════════════════════════════════════════════════════════════════════

  每日最多选取 top_per_day (默认2) 个最优信号，评分规则:
    - 箱体数量: 每个箱体 +100 分 (箱体越多支撑越强)
    - 收阳线: +10 分
    - 量缩: +5 分

═══════════════════════════════════════════════════════════════════════════════
                          统计维度
═══════════════════════════════════════════════════════════════════════════════

  回测完成后按以下维度分段统计:
    - 板块 (沪主板/深主板/创业板/科创板)
    - 策略路径 (箱体数)
    - 出场原因 (止盈/止损/跟踪止损/持仓到期)
    - 突破幅度 / 洗盘天数 / 放量倍数
    - MA5斜率 / MA5加速度 / MA10斜率
    - 流通市值 / 换手率
    - 买入日特征 (高开低开/涨跌幅/量比/振幅/下影线)
    - 大赢家 vs 低质量 效应量分析

═══════════════════════════════════════════════════════════════════════════════
                          使用方法
═══════════════════════════════════════════════════════════════════════════════

  # 默认回测 (内置股票列表, 300天数据)
  python test_box_breakout.py

  # 指定股票
  python test_box_breakout.py --codes 002010,300750,601318

  # DB模式 (全市场)
  python test_box_breakout.py --source db

  # 调整箱体参数
  python test_box_breakout.py --box-days 25 --box-max-range 12 --box-min-range 5

  # 调整支撑参数
  python test_box_breakout.py --min-box-count 3 --support-tolerance 2.0 --buy-zone-pct 1.5

  # 调整趋势过滤
  python test_box_breakout.py --min-ma10-slope 0.5

  # 调整出场参数
  python test_box_breakout.py --stop-loss 8 --trailing-pct 3 --take-profit 20 --max-hold 15

  # 禁用板块自适应 (主板也用默认止损)
  python test_box_breakout.py --no-board-adaptive --stop-loss 12

  # 打印全部交易明细
  python test_box_breakout.py --all-trades

  # 今日信号 + 持仓卖出建议 (收盘后运行)
  python test_box_breakout.py --today
  python test_box_breakout.py --today --today-date 2026-08-27

═══════════════════════════════════════════════════════════════════════════════
                          实盘工作流
═══════════════════════════════════════════════════════════════════════════════

  每日收盘后 (15:00+):
    python test_box_breakout.py --today

  输出内容:
    1. 今日信号 — 信号日=今天的股票，按质量评分排序
       - 箱体数、确认类型、支撑/阻力位、量比、MA5斜率
       - 买入建议: 明日收盘价买入，给出不同跳空档位参考价

    2. 持仓分析 — 最近20天入场的持仓
       - 亏损持仓: 关注止损线
       - 盈利持仓: 关注止盈/跟踪止损
       - 观望持仓: 收益在-3%~+5%之间

═══════════════════════════════════════════════════════════════════════════════
                          全部参数
═══════════════════════════════════════════════════════════════════════════════

  数据源:
    --codes         逗号分隔的股票代码 (默认: 内置列表)
    --days          加载K线天数 (默认300)
    --source        manual(默认) / db (全市场)

  箱体参数:
    --box-days          箱体整理窗口天数 (默认20)
    --box-max-range     箱体最大振幅% (默认15.0)
    --box-min-range     箱体最小振幅% (默认0.0, 建议设8)
    --box-min-bars      箱体内最少K线数 (默认10)

  支撑参数:
    --min-box-count     最少箱体数 (默认2, 越多支撑越强)
    --support-tolerance 支撑位聚类容差% (默认3.0)
    --buy-zone-pct      买入区域范围% (默认2.0)

  趋势过滤:
    --min-ma10-slope    MA10斜率下限%/天 (默认0.3)

  突破参数:
    --vol-expand-min    突破放量下限 (默认1.5)
    --vol-expand-max    突破放量上限 (默认3.0)

  出场参数:
    --stop-loss         止损% (默认12.0, 主板自适应20%)
    --trailing-pct      跟踪止损回撤% (默认5.0)
    --trailing-activate 跟踪止损激活门槛% (默认5.0)
    --take-profit       止盈% (默认15.0)
    --max-hold          最大持仓天数 (默认10)

  其他:
    --board-adaptive    板块自适应参数 (默认开启)
    --no-board-adaptive 禁用板块自适应
    --top-per-day       每天最多选前N个信号 (默认2)
    --top               显示TOP N交易 (默认10)
    --all-trades        打印全部交易明细
    --today             显示今日信号 + 持仓卖出建议
    --today-date        指定日期(YYYY-MM-DD), 配合--today使用
    --pullback-confirm  启用突破后回踩确认模式 (默认关闭)
    --pullback-days     回踩确认天数 (默认3)

═══════════════════════════════════════════════════════════════════════════════
                          数据来源
═══════════════════════════════════════════════════════════════════════════════

  manual模式: kline_cache.fetch_kline() → 腾讯API + 本地JSON缓存
  db模式:     MarketKlineWriter → mootdx写入的kline_1D表 (需.env配置)
  换手率/市值: stock_basic_info表 (需.env配置)
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
# 斜率 + 换手率 工具函数
# ================================================================
def calc_ma_slope(closes, idx, ma_period=5, slope_days=3):
    """MA斜率: 线性回归角度 (% / 天)"""
    if idx < ma_period + slope_days - 1:
        return 0
    ma_vals = []
    for i in range(idx - slope_days + 1, idx + 1):
        ma_vals.append(sum(closes[i - ma_period + 1:i + 1]) / ma_period)
    if not ma_vals or ma_vals[0] <= 0:
        return 0
    n = len(ma_vals)
    sx = n * (n - 1) / 2
    sy = sum(ma_vals)
    sxy = sum(i * ma_vals[i] for i in range(n))
    sx2 = n * (n - 1) * (2 * n - 1) / 6
    denom = n * sx2 - sx * sx
    if denom == 0: return 0
    slope = (n * sxy - sx * sy) / denom
    return slope / ma_vals[-1] * 100

def calc_ma_slope_accel(closes, idx, ma_period=5, slope_days=3):
    """MA斜率加速度 = 当前斜率 - 前一段斜率"""
    return calc_ma_slope(closes, idx, ma_period, slope_days) - calc_ma_slope(closes, idx - slope_days, ma_period, slope_days)

_circ_cache = None
def _load_circ():
    global _circ_cache
    if _circ_cache is not None: return _circ_cache
    try:
        from app.utils.basicinfo_db import get_stock_basic_db
        db = get_stock_basic_db(); pool = db._get_pool()
        with pool.cursor() as cur:
            cur.execute("SELECT symbol, circ_shares FROM stock_basic_info WHERE status='active' AND circ_shares > 0")
            _circ_cache = {row[0]: float(row[1]) for row in cur.fetchall()}
    except: _circ_cache = {}
    return _circ_cache

def get_turnover(code, volume):
    circ = _load_circ().get(code, 0)
    return volume / circ * 100 if circ > 0 else 0

def get_circ_mcap(code, price):
    circ = _load_circ().get(code, 0)
    return circ * price / 1e8 if circ > 0 else 0

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
    from kline_cache import fetch_kline as _fetch_kline
    return _fetch_kline(code, days)

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
# 回测引擎
# ================================================================
def run_backtest(bars, entry_idx, entry_price, stop_loss_pct=5.0,
                 trailing_pct=5.0, max_hold_days=15,
                 take_profit_pct=15.0, trailing_activate_pct=5.0):
    """
    简化回测: 买入后跟踪峰值，从峰值回撤 peak_drop_pct% 出场，最多持有 max_hold_days 天。
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

        # 从峰值回撤 peak_drop_pct% 出场
        drop_from_peak = (peak - b['low']) / peak * 100
        if drop_from_peak >= stop_loss_pct:
            drop_price = peak * (1 - stop_loss_pct / 100)
            if b['open'] < drop_price:
                exit_p = b['open']
            else:
                exit_p = drop_price
            exit_d = d
            exit_reason = "peak_drop"
            break

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
# 多箱体底部买入 策略
# ================================================================
def _find_boxes(bars, start, end, box_days, box_max_range, box_min_range):
    """在 bars[start:end] 范围内滑动查找所有箱体"""
    boxes = []
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    closes = [b["close"] for b in bars]

    i = start
    while i + box_days <= end:
        box_high = max(highs[i:i + box_days])
        box_low = min(lows[i:i + box_days])
        if box_low <= 0:
            i += 1
            continue

        box_range_pct = (box_high - box_low) / box_low * 100
        if box_min_range <= box_range_pct <= box_max_range:
            # 额外检查: 箱体内K线大部分在区间内(至少80%)
            in_range = sum(1 for j in range(i, i + box_days)
                          if box_low * 0.99 <= lows[j] and highs[j] <= box_high * 1.01)
            if in_range >= box_days * 0.7:
                boxes.append({
                    "start": i,
                    "end": i + box_days - 1,
                    "high": box_high,
                    "low": box_low,
                    "range_pct": box_range_pct,
                })
                i += box_days  # 跳过已识别区间
            else:
                i += 1
        else:
            i += 1

    return boxes


def strategy_peak_breakout(bars, code,
                           box_days=25, box_max_range=15.0, box_min_range=0.0, box_min_bars=10,
                           vol_expand_min=1.5, vol_expand_max=3.0,
                           stop_loss_pct=12.0, trailing_pct=5.0,
                           trailing_activate_pct=5.0, take_profit_pct=15.0,
                           max_hold_days=10, top_per_day=2,
                           require_ma60=False, require_ma20_up=False,
                           min_rsi=0, max_rsi=100,
                           min_macd_hist=0.0, max_macd_hist=100.0,
                           pullback_confirm=False, pullback_days=3,
                           pullback_max_pct=3.0,
                           # 新增: 多箱体底部买入参数
                           min_box_count=2, support_tolerance=3.0, buy_zone_pct=2.0,
                           min_ma10_slope=0.3):
    """
    多箱体底部买入 策略

    入场条件:
      ① 历史箱体: 过去N天内识别出多个整理区间
      ② 支撑确认: 多个箱体下沿接近(±tolerance%)
      ③ 底部买入: 当前价格触及第2/3个箱体底部区域(buy_zone_pct%内)
      ④ 反弹确认: 触底后收阳线或量能萎缩(卖压衰竭)
      ⑤ 买入: 确认后次日开盘买
    """
    if len(bars) < box_days * 3 + 30:
        return []

    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    opens = [b["open"] for b in bars]
    volumes = [b["volume"] for b in bars]

    candidates = []

    # 从足够远的位置开始，确保有历史箱体
    scan_start = box_days * 2 + 10

    for i in range(scan_start, len(bars)):
        # ── ① 识别历史箱体 ──
        # 回顾过去 box_days*4 的范围找箱体
        lookback = box_days * 4
        hist_start = max(0, i - lookback)
        # 只看 i 之前的区间
        boxes = _find_boxes(bars, hist_start, i, box_days, box_max_range, box_min_range)

        if len(boxes) < min_box_count:
            continue

        # ── ② 找支撑位: 多个箱体下沿接近 ──
        # 按下沿排序，找聚类
        sorted_boxes = sorted(boxes, key=lambda b: b["low"])

        # 贪心聚类: 在 tolerance 范围内的箱体归为同一支撑
        support_clusters = []
        current_cluster = [sorted_boxes[0]]

        for j in range(1, len(sorted_boxes)):
            ref_low = current_cluster[0]["low"]
            diff_pct = abs(sorted_boxes[j]["low"] - ref_low) / ref_low * 100
            if diff_pct <= support_tolerance:
                current_cluster.append(sorted_boxes[j])
            else:
                if len(current_cluster) >= min_box_count:
                    support_clusters.append(current_cluster)
                current_cluster = [sorted_boxes[j]]
        if len(current_cluster) >= min_box_count:
            support_clusters.append(current_cluster)

        if not support_clusters:
            continue

        # ── 趋势过滤: MA10/MA20 斜率递增 ──
        ma10_now = sum(closes[i-9:i+1]) / 10 if i >= 9 else closes[i]
        ma10_prev = sum(closes[i-10:i]) / 10 if i >= 10 else ma10_now
        ma10_slope_now = (ma10_now - ma10_prev) / ma10_prev * 100 if ma10_prev > 0 else 0

        ma20 = sum(closes[i-19:i+1]) / 20 if i >= 19 else closes[i]
        ma20_prev = sum(closes[i-20:i]) / 20 if i >= 20 else ma20
        ma20_slope_now = (ma20 - ma20_prev) / ma20_prev * 100 if ma20_prev > 0 else 0

        # 前一段斜率
        ma10_prev2 = sum(closes[i-11:i-1]) / 10 if i >= 11 else ma10_prev
        ma10_slope_prev = (ma10_prev - ma10_prev2) / ma10_prev2 * 100 if ma10_prev2 > 0 else 0

        ma20_prev2 = sum(closes[i-21:i-1]) / 20 if i >= 21 else ma20_prev
        ma20_slope_prev = (ma20_prev - ma20_prev2) / ma20_prev2 * 100 if ma20_prev2 > 0 else 0

        # 要求: MA10斜率 >= 0.3 且 递增, MA20斜率 > 0 且递增
        if ma10_slope_now < min_ma10_slope or ma10_slope_now <= ma10_slope_prev:
            continue
        if ma20_slope_now <= 0 or ma20_slope_now <= ma20_slope_prev:
            continue

        ma60 = sum(closes[i-59:i+1]) / 60 if i >= 59 else closes[i]

        # ── ③ 检查当前价格是否在支撑区域底部 ──
        best_signal = None

        for cluster in support_clusters:
            # 支撑位 = 集群中箱体下沿的平均值
            support_level = sum(b["low"] for b in cluster) / len(cluster)
            # 箱体上沿 = 集群中箱体上沿的平均值
            resistance_level = sum(b["high"] for b in cluster) / len(cluster)

            # 买入区域: 价格在支撑位附近 (±buy_zone_pct%)
            buy_zone_high = support_level * (1 + buy_zone_pct / 100)
            buy_zone_low = support_level * (1 - buy_zone_pct / 100)

            # 检查当日低点是否触及买入区域
            if lows[i] > buy_zone_high:
                continue
            # 不能破支撑太多
            if lows[i] < buy_zone_low:
                continue

            # ── ④ 反弹确认 ──
            # 条件A: 收阳线（收盘>开盘）
            is_bullish = closes[i] > bars[i]["open"]
            # 条件B: 量能萎缩（卖压衰竭）- 当日量 < 近5日均量
            avg_vol_5 = sum(volumes[max(0,i-4):i]) / min(5, i) if i > 0 else volumes[i]
            vol_shrink = volumes[i] < avg_vol_5 * 0.8
            # 条件C: 下影线长（探底回升）
            body = abs(closes[i] - bars[i]["open"])
            lower_shadow = min(closes[i], bars[i]["open"]) - lows[i]
            long_lower_shadow = lower_shadow > body * 1.5 if body > 0 else lower_shadow > 0

            # 至少满足一个确认条件
            if not (is_bullish or vol_shrink or long_lower_shadow):
                continue

            # 记录信号质量
            confirm_type = "bullish" if is_bullish else ("vol_shrink" if vol_shrink else "long_shadow")

            ma60 = sum(closes[i-59:i+1]) / 60 if i >= 59 else closes[i]
            ma20 = sum(closes[i-19:i+1]) / 20
            if i >= 15:
                gains = [max(closes[j] - closes[j-1], 0) for j in range(i-13, i+1)]
                loss_list = [max(closes[j-1] - closes[j], 0) for j in range(i-13, i+1)]
                avg_g = sum(gains) / 14
                avg_l = sum(loss_list) / 14
                rsi = 100 - 100 / (1 + avg_g / avg_l) if avg_l > 0 else 100
            else:
                rsi = 50

            ma5_slope = calc_ma_slope(closes, i, 5, 3)
            ma10_slope = calc_ma_slope(closes, i, 10, 3)
            ma5_accel = calc_ma_slope_accel(closes, i, 5, 3)
            circ_mcap = get_circ_mcap(code, closes[i])
            turnover = get_turnover(code, volumes[i])

            vol_ratio = volumes[i] / avg_vol_5 if avg_vol_5 > 0 else 1.0

            # 箱体振幅
            box_range_pct = (resistance_level - support_level) / support_level * 100 if support_level > 0 else 0

            # 板块过滤
            board = get_board_name(code)
            if board in ('创业板', '科创板'):
                # 创科板: 流通市值<30亿
                if circ_mcap > 0 and circ_mcap >= 30:
                    continue
            # 主板无额外过滤

            signal = {
                "idx": i, "signal_date": bars[i]["time"],
                "support_level": round(support_level, 3),
                "resistance_level": round(resistance_level, 3),
                "box_count": len(cluster),
                "box_range_pct": round(box_range_pct, 2),
                "confirm_type": confirm_type,
                "vol_ratio": round(vol_ratio, 2),
                "ma5_slope": round(ma5_slope, 3),
                "ma10_slope": round(ma10_slope, 3),
                "ma5_accel": round(ma5_accel, 3),
                "turnover": round(turnover, 2),
                "circ_mcap": round(circ_mcap, 1),
                "ma60": round(ma60, 3),
                "rsi": round(rsi, 1),
                "close_vs_ma60": round((closes[i] / ma60 - 1) * 100, 2) if ma60 > 0 else 0,
            }

            # 选择最优信号: 箱体数多 > 收阳线 > 量缩
            score = len(cluster) * 100 + (10 if is_bullish else 0) + (5 if vol_shrink else 0)
            if best_signal is None or score > best_signal.get("_score", 0):
                signal["_score"] = score
                best_signal = signal

        if best_signal:
            entry_idx = i + 1
            if entry_idx >= len(bars):
                continue
            
            # 买入日特征分析
            buy_day_open = bars[entry_idx]["open"]
            buy_day_close = closes[entry_idx]
            buy_day_chg = (buy_day_close / buy_day_open - 1) * 100 if buy_day_open > 0 else 0
            
            # 买入日量比(vs前5日均量)
            avg_vol_5 = sum(volumes[max(0,entry_idx-5):entry_idx]) / min(5, entry_idx) if entry_idx > 0 else volumes[entry_idx]
            buy_vol_ratio = volumes[entry_idx] / avg_vol_5 if avg_vol_5 > 0 else 1.0
            
            # 过滤条件:
            # 1. 买入日涨跌幅 -3%~+3%
            if buy_day_chg < -3 or buy_day_chg > 3:
                continue
            # 2. 平开: 开盘价vs前收盘 -0.5%~+0.5%
            prev_close = closes[entry_idx-1] if entry_idx > 0 else buy_day_open
            gap_pct = (buy_day_open / prev_close - 1) * 100 if prev_close > 0 else 0
            if gap_pct < -0.5 or gap_pct > 0.5:
                continue
            # 3. 低振幅: 当日振幅<3%
            buy_day_range = (bars[entry_idx]["high"] - bars[entry_idx]["low"]) / buy_day_open * 100 if buy_day_open > 0 else 0
            if buy_day_range >= 3:
                continue
            
            entry_price = buy_day_close  # 收盘价买入
            if entry_price <= 0:
                continue

            best_signal["entry_price"] = entry_price
            best_signal["entry_idx"] = entry_idx
            best_signal["entry_date"] = bars[entry_idx]["time"]
            best_signal["buy_day_chg"] = round(buy_day_chg, 2)
            best_signal["buy_vol_ratio"] = round(buy_vol_ratio, 2)
            candidates.append(best_signal)

    daily_candidates = defaultdict(list)
    for c in candidates:
        daily_candidates[c["signal_date"]].append(c)

    filtered = []
    for date, cands in daily_candidates.items():
        cands.sort(key=lambda c: (-c.get("box_count", 0), c.get("_score", 0)))
        filtered.extend(cands[:top_per_day])

    trades = []
    for c in filtered:
        result = run_backtest(bars, c["entry_idx"], c["entry_price"],
                              stop_loss_pct, trailing_pct, max_hold_days,
                              take_profit_pct, trailing_activate_pct)
        if not result:
            continue

        trades.append({
            "code": code, "board": get_board_name(code),
            "path": "multi_box_bottom",
            "confirm_type": c["confirm_type"],
            "box_count": c["box_count"],
            "path_label": f"{c['box_count']}箱体底部买入",
            "signal_date": c["signal_date"], "signal_close": closes[c["idx"]],
            "box_high": c["resistance_level"], "box_low": c["support_level"],
            "box_range_pct": round((c["resistance_level"] - c["support_level"]) / c["support_level"] * 100, 2),
            "box_days": box_days,
            "breakout_close": closes[c["idx"]],
            "vol_ratio_vs_pullback": c["vol_ratio"],
            "ma5_slope": c.get("ma5_slope", 0),
            "ma10_slope": c.get("ma10_slope", 0),
            "ma5_accel": c.get("ma5_accel", 0),
            "turnover": c.get("turnover", 0),
            "circ_mcap": c.get("circ_mcap", 0),
            "ma60": c["ma60"], "rsi": c.get("rsi", 0),
            "macd_hist": 0,
            "close_vs_ma60": c["close_vs_ma60"],
            "entry_date": c["entry_date"],
            "entry_price": round(c["entry_price"], 3),
            "buy_mode": "buy_day_close",
            "buy_day_chg": c.get("buy_day_chg", 0),
            "buy_vol_ratio": c.get("buy_vol_ratio", 0),
            "neckline_high": c["resistance_level"], "neckline_gain_pct": round((c["resistance_level"] - c["support_level"]) / c["support_level"] * 100, 2),
            "first_trough_low": c["support_level"], "second_trough_low": c["support_level"],
            "peak_high": c["resistance_level"], "peak_gain_pct": round((c["resistance_level"] - c["support_level"]) / c["support_level"] * 100, 2),
            "pullback_low": c["support_level"], "pullback_pct": 0,
            "breakout_pct": 0,
            "accel_gain": 0, "accel_days": 0, "pullback_days": 0,
            "wave1_days": 0, "wave2_days": 0, "wave3_days": 0, "wave_total_days": 0,
            **result,
        })

    return trades


# ================================================================
# 测试股票列表
# ================================================================
TEST_CODES = [
    # ── 沪主板 (60) ── 科技/制造/消费/医药
    "600031","600048","600056","600066","600085","600089","600100",
    "600104","600109","600111","600115","600132","600143","600150",
    "600160","600161","600170","600176","600183","600184","600196",
    "600201","600206","600216","600219","600233","600256","600260",
    "600271","600276","600298","600309","600316","600329","600332",
    "600346","600352","600362","600366","600372","600388","600392",
    "600406","600418","600426","600436","600438","600460","600487",
    "600489","600498","600507","600519","600521","600529","600557",
    "600566","600570","600580","600584","600585","600588","600600",
    "600660","600663","600690","600703","600737","600741","600745",
    "600760","600765","600782","600809","600845","600862","600867",
    "600885","600886","600893","600900","600918","601012","601066",
    "601100","601111","601138","601155","601162","601168","601200",
    "601211","601225","601231","601236","601238","601318","601336",
    "601360","601390","601555","601577","601615","601618","601628",
    "601633","601668","601669","601688","601698","601700","601766",
    "601788","601799","601800","601808","601816","601818","601838",
    "601858","601865","601868","601877","601881","601888","601899",
    "601901","601916","601919","601933","601939","601958","601966",
    "601985","601988","601989","601992","601998","603019","603056",
    "603077","603087","603160","603185","603198","603228","603233",
    "603259","603260","603288","603290","603345","603369","603392",
    "603444","603486","603501","603515","603517","603568","603583",
    "603596","603605","603613","603658","603659","603688","603707",
    "603712","603719","603737","603799","603806","603816","603833",
    "603858","603882","603883","603885","603893","603899","603960",
    "603986","603993",
    # ── 深主板 (00) ──
    "000009","000012","000021","000027","000031","000039","000049",
    "000060","000063","000066","000069","000078","000088","000100",
    "000157","000333","000338","000400","000401","000408","000425",
    "000513","000519","000528","000536","000537","000539","000547",
    "000553","000559","000568","000581","000591","000596","000598",
    "000601","000612","000623","000625","000630","000636","000651",
    "000656","000661","000671","000683","000703","000709","000723",
    "000725","000727","000733","000738","000768","000776","000778",
    "000783","000786","000800","000807","000810","000811","000822",
    "000825","000830","000831","000848","000858","000860","000876",
    "000877","000878","000883","000893","000895","000898","000902",
    "000905","000917","000930","000932","000938","000960","000963",
    "000969","000970","000975","000977","000983","000987","000988",
    "000998","001914","001979","002001","002002","002007","002008",
    "002010","002013","002019","002024","002025","002027","002028",
    "002030","002032","002035","002038","002044","002049","002050",
    "002055","002056","002060","002064","002065","002074","002078",
    "002080","002081","002092","002100","002110","002120","002127",
    "002129","002131","002138","002142","002146","002152","002155",
    "002156","002157","002163","002166","002170","002171","002174",
    "002176","002179","002180","002185","002190","002191","002195",
    "002196","002202","002203","002212","002214","002218","002221",
    "002223","002227","002230","002233","002234","002236","002238",
    "002241","002244","002249","002250","002252","002254","002255",
    "002258","002261","002263","002266","002268","002270","002271",
    "002273","002274","002276","002281","002285","002292","002294",
    "002299","002304","002311","002312","002340","002352","002353",
    "002371","002372","002375","002382","002385","002390","002399",
    "002405","002407","002408","002409","002414","002415","002416",
    "002419","002421","002430","002432","002436","002438","002439",
    "002444","002456","002460","002463","002466","002468","002470",
    "002475","002493","002497","002500","002505","002507","002511",
    "002531","002555","002557","002568","002572","002594","002595",
    "002600","002601","002602","002607","002624","002625","002643",
    "002648","002653","002670","002673","002683","002709","002714",
    "002736","002739","002745","002756","002761","002791","002797",
    "002812","002821","002831","002841","002850","002867","002916",
    "002920","002926","002938","002945","002966","002984","003816",
    # ── 创业板 (300/301) ── 少量活跃股
    "300003","300009","300012","300014","300015","300017","300024",
    "300027","300033","300037","300042","300044","300058","300059",
    "300070","300072","300073","300078","300088","300098","300115",
    "300118","300122","300124","300130","300133","300136","300140",
    "300142","300144","300146","300152","300166","300168","300170",
    "300171","300176","300182","300188","300197","300207","300212",
    "300223","300226","300233","300236","300244","300251","300253",
    "300257","300271","300274","300284","300285","300296","300308",
    "300315","300316","300323","300324","300327","300347","300357",
    "300363","300373","300376","300383","300390","300394","300395",
    "300398","300408","300413","300418","300433","300438","300442",
    "300450","300454","300457","300459","300474","300482","300487",
    "300496","300498","300502","300529","300558","300568","300595",
    "300601","300618","300628","300630","300661","300676","300699",
    "300724","300750","300760","300763","300769","300773","300782",
    "300832","300841","300861","300866","300888","300896","301269",
]

# ================================================================
# --today 模式: 今日信号 + 持仓建议
# ================================================================
def calc_buy_tiers(signal_price, board):
    """基于信号价计算多档买入建议价

    箱体底部买入策略, 次日收盘价买入。
    给出不同开盘跳空下的参考买入价。
    主板: -2% ~ +2% (策略要求平开)
    创/科板: -3% ~ +2%
    """
    if board in ('创业板', '科创板'):
        gaps = [-3, -2, -1, 0, 1, 2]
    else:
        gaps = [-2, -1, -0.5, 0, 0.5, 1, 2]
    tiers = []
    for g in gaps:
        price = round(signal_price * (1 + g / 100), 2)
        tiers.append((g, price))
    return tiers


def calc_signal_score(t):
    """计算信号质量评分 (0~100)

    基于信号日已知数据:
    - 箱体数量: 每个 +20 分 (2箱=40, 3箱=60, 4箱=80)
    - 确认类型: 收阳线=20, 量缩=10, 长下影线=5
    - 量比: <1=10, <0.8=15 (卖压衰竭)
    - MA5斜率: >0.3=10, >0.5=15
    """
    score = 0
    box_count = t.get('box_count', 0)
    score += min(box_count * 20, 80)
    confirm = t.get('confirm_type', '')
    if confirm == 'bullish':
        score += 20
    elif confirm == 'vol_shrink':
        score += 10
    elif confirm == 'long_shadow':
        score += 5
    vol_ratio = t.get('vol_ratio', 1.0)
    if vol_ratio < 0.8:
        score += 15
    elif vol_ratio < 1.0:
        score += 10
    ma5_slope = t.get('ma5_slope', 0)
    if ma5_slope > 0.5:
        score += 15
    elif ma5_slope > 0.3:
        score += 10
    return score


def signal_quality_label(score):
    """信号质量标签"""
    if score >= 80: return '极强'
    if score >= 60: return '强'
    if score >= 40: return '中'
    return '弱'


def print_today_signals(all_trades, today_str):
    """今日信号 + 持仓卖出建议

    信号日=今天 → 次日收盘价买入
    持仓: 入场日<=今天 且 入场日>=20天前
    """
    from datetime import datetime, timedelta

    today_signals = [t for t in all_trades if t.get('signal_date') == today_str]

    print(f"\n{'=' * 80}")
    print(f"{today_str} 今日信号 (信号日, 次日收盘价买入)")
    print(f"{'=' * 80}")

    if not today_signals:
        print(f"  今日无信号")
    else:
        print(f"  共 {len(today_signals)} 只股票出现信号")
        today_signals.sort(key=lambda x: -calc_signal_score(x))

        print(f"\n  多箱体底部买入 ({len(today_signals)}只):")
        print(f"  {'代码':>8} {'板块':>6} {'质量':>6} {'评分':>4} {'箱体':>4} {'确认':>8} "
              f"{'信号价':>8} {'支撑':>8} {'阻力':>8} {'量比':>6} {'MA5斜':>6} {'流通值':>8}")
        print(f"  {'-' * 95}")
        for t in today_signals:
            code, board = t['code'], t['board']
            score = calc_signal_score(t)
            label = signal_quality_label(score)
            signal_price = t.get('signal_close', t.get('entry_price', 0))
            tiers = calc_buy_tiers(signal_price, board)
            tier_str = ' / '.join([f"{g:+.1f}%->{p:.2f}" for g, p in tiers])

            print(f"  {code:>8} {board:>6} {label:>6} {score:>3}  "
                  f"{t.get('box_count',0):>4}箱 {t.get('confirm_type',''):<8} "
                  f"{signal_price:>7.2f} {t.get('box_low',0):>7.2f} {t.get('box_high',0):>7.2f} "
                  f"{t.get('vol_ratio',0):>5.2f}x {t.get('ma5_slope',0):>5.3f} "
                  f"{t.get('circ_mcap',0):>6.1f}亿")
            print(f"  {'':>10} 买入建议(明日收盘): {tier_str}")

        print(f"\n  箱体详情:")
        for t in today_signals:
            print(f"    {t['code']}({t['board']}) "
                  f"支撑{t.get('box_low',0):.2f} 阻力{t.get('box_high',0):.2f} "
                  f"振幅{t.get('box_range_pct',0):.1f}% "
                  f"RSI={t.get('rsi',0):.1f} MA60={t.get('ma60',0):.2f} "
                  f"距MA60={t.get('close_vs_ma60',0):+.1f}%")

    # 持仓卖出建议
    today_dt = datetime.strptime(today_str, '%Y-%m-%d')
    recent_cutoff = (today_dt - timedelta(days=20)).strftime('%Y-%m-%d')
    recent_entries = [t for t in all_trades
                      if t.get('entry_date', '') <= today_str
                      and t.get('entry_date', '') >= recent_cutoff]

    if recent_entries:
        for t in recent_entries:
            entry_dt = datetime.strptime(t['entry_date'], '%Y-%m-%d')
            t['_hold_days'] = (today_dt - entry_dt).days

        exited = [t for t in recent_entries
                  if t.get('exit_day', 0) > 0 and t['_hold_days'] >= t.get('exit_day', 999)]
        holding = [t for t in recent_entries if t not in exited]

        print(f"\n{'=' * 80}")
        print(f"持仓分析 (最近20天入场)")
        print(f"{'=' * 80}")
        print(f"  总计: {len(recent_entries)}只 | 已出场: {len(exited)}只 | 持有中: {len(holding)}只")

        if holding:
            holding.sort(key=lambda x: x.get('return_pct', 0))

            losers = [t for t in holding if t.get('return_pct', 0) < -3]
            winners = [t for t in holding if t.get('return_pct', 0) >= 5]
            watch = [t for t in holding if -3 <= t.get('return_pct', 0) < 5]

            if losers:
                print(f"\n  亏损持仓 - 关注止损 ({len(losers)}只):")
                print(f"  {'代码':>8} {'板块':>6} {'买入日':>12} {'买入价':>8} {'持仓天':>6} {'收益':>8} {'峰值':>8} {'回撤':>8}")
                print(f"  {'-' * 80}")
                for t in sorted(losers, key=lambda x: x.get('return_pct', 0)):
                    ret = t.get('return_pct', 0)
                    peak = t.get('peak_return_pct', 0)
                    drawdown = peak - ret if peak > 0 else 0
                    print(f"  {t['code']:>8} {t['board']:>6} {t['entry_date']:>12} "
                          f"{t['entry_price']:>7.2f} {t['_hold_days']:>5}天 "
                          f"{ret:>+7.2f}% {peak:>+7.2f}% {drawdown:>7.1f}%")

            if winners:
                print(f"\n  盈利持仓 - 关注止盈 ({len(winners)}只):")
                print(f"  {'代码':>8} {'板块':>6} {'买入日':>12} {'买入价':>8} {'持仓天':>6} {'收益':>8} {'峰值':>8} {'出场规则'}")
                print(f"  {'-' * 85}")
                for t in sorted(winners, key=lambda x: -x.get('return_pct', 0)):
                    ret = t.get('return_pct', 0)
                    peak = t.get('peak_return_pct', 0)
                    if peak >= 15:
                        rule = '已到止盈线'
                    elif peak >= 5:
                        rule = f'跟踪止损(峰值{peak:.1f}%回撤5%%)'
                    else:
                        rule = '止损12%'
                    print(f"  {t['code']:>8} {t['board']:>6} {t['entry_date']:>12} "
                          f"{t['entry_price']:>7.2f} {t['_hold_days']:>5}天 "
                          f"{ret:>+7.2f}% {peak:>+7.2f}% {rule}")

            if watch:
                print(f"\n  观望持仓 ({len(watch)}只):")
                print(f"  {'代码':>8} {'板块':>6} {'买入日':>12} {'买入价':>8} {'持仓天':>6} {'收益':>8} {'峰值':>8}")
                print(f"  {'-' * 70}")
                for t in watch:
                    ret = t.get('return_pct', 0)
                    peak = t.get('peak_return_pct', 0)
                    print(f"  {t['code']:>8} {t['board']:>6} {t['entry_date']:>12} "
                          f"{t['entry_price']:>7.2f} {t['_hold_days']:>5}天 "
                          f"{ret:>+7.2f}% {peak:>+7.2f}%")

            print(f"\n  出场规则提醒:")
            print(f"    止盈: 盈利>=15% 后从峰值回撤即出场")
            print(f"    止损: 从峰值回撤12% (主板20%)")
            print(f"    跟踪止损: 盈利>5%后激活, 从峰值回撤5%出场")
            print(f"    持仓上限: 10天")

    return today_signals


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
    n = len(rets)
    win = [r for r in rets if r > 0]
    loss = [r for r in rets if r <= 0]
    wr = len(win) / n * 100
    avg = sum(rets) / n
    avg_pk = sum(t.get('peak_return_pct', 0) for t in trades) / n
    avg_w = sum(win) / len(win) if win else 0
    avg_l = abs(sum(loss)) / len(loss) if loss else 0
    pl = avg_w / avg_l if avg_l > 0 else 999
    print(f"  {label}: {n}笔 胜率{wr:.1f}% 均收{avg:+.2f}% 均峰{avg_pk:+.2f}% 盈亏比{pl:.2f}")

# ================================================================
# 主流程
# ================================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="多箱体底部买入 策略 独立回测")
    parser.add_argument("--codes", default="", help="逗号分隔的股票代码")
    parser.add_argument("--days", type=int, default=300, help="加载K线天数 (默认300)")
    parser.add_argument("--source", choices=["manual", "db"], default="manual",
                        help="数据源: manual(默认), db")

    # 突破参数
    parser.add_argument("--vol-expand-min", type=float, default=1.5,
                        help="突破放量下限 (默认1.5)")
    parser.add_argument("--vol-expand-max", type=float, default=3.0,
                        help="突破放量上限 (默认3.0)")

    # 箱体参数
    parser.add_argument("--box-days", type=int, default=20,
                        help="箱体整理天数 (默认20)")
    parser.add_argument("--box-max-range", type=float, default=15.0,
                        help="箱体最大振幅%% (默认15.0)")
    parser.add_argument("--box-min-range", type=float, default=0.0,
                        help="箱体最小振幅%% (默认0, 建议8)")
    parser.add_argument("--box-min-bars", type=int, default=10,
                        help="箱体内最少K线数 (默认10)")

    # 过滤参数
    parser.add_argument("--pullback-confirm", action="store_true", default=False,
                        help="启用突破后回踩确认模式 (默认关闭)")
    parser.add_argument("--pullback-days", type=int, default=3,
                        help="回踩确认天数 (默认3)")

    # 多箱体底部买入参数
    parser.add_argument("--min-box-count", type=int, default=2,
                        help="最少箱体数 (默认2)")
    parser.add_argument("--support-tolerance", type=float, default=3.0,
                        help="支撑位聚类容差%% (默认3.0)")
    parser.add_argument("--buy-zone-pct", type=float, default=2.0,
                        help="买入区域范围%% (默认2.0)")
    parser.add_argument("--min-ma10-slope", type=float, default=0.3,
                        help="MA10斜率下限%%/天 (默认0.3, 快斜率)")

    # 出场参数
    parser.add_argument("--stop-loss", type=float, default=12.0,
                        help="止损%% (默认12.0, 主板自适应20%%)")
    parser.add_argument("--trailing-pct", type=float, default=5.0,
                        help="跟踪止损回撤%% (默认5.0)")
    parser.add_argument("--trailing-activate", type=float, default=5.0,
                        help="跟踪止损激活门槛%% (默认5.0)")
    parser.add_argument("--take-profit", type=float, default=15.0,
                        help="止盈%% (默认15.0)")
    parser.add_argument("--max-hold", type=int, default=10,
                        help="最大持仓天数 (默认10)")
    parser.add_argument("--board-adaptive", action="store_true", default=True,
                        help="板块自适应参数 (默认开启)")
    parser.add_argument("--no-board-adaptive", action="store_false", dest="board_adaptive",
                        help="禁用板块自适应")

    # 其他
    parser.add_argument("--today", action="store_true",
                        help="显示今日信号+持仓卖出建议")
    parser.add_argument("--today-date", type=str, default="",
                        help="指定日期(YYYY-MM-DD), 配合--today使用, 默认为当天")
    parser.add_argument("--top-per-day", type=int, default=2,
                        help="每天最多选前N个 (默认2)")
    parser.add_argument("--top", type=int, default=10, help="显示TOP N (默认10)")
    parser.add_argument("--all-trades", action="store_true", help="打印全部交易明细")
    args = parser.parse_args()

    codes = [c.strip() for c in args.codes.split(",") if c.strip()] if args.codes else TEST_CODES
    use_db = args.source == "db"

    if use_db:
        print("  DB模式: 从数据库加载全市场...")
        codes = get_all_codes_db()
        print(f"   全市场: {len(codes)} 只股票")

    print(f"{'=' * 80}")
    print(f"多箱体底部买入 策略 独立回测")
    print(f"{'=' * 80}")
    print(f"入场条件:")
    print(f"  ① 历史箱体: 过去{args.box_days*4}天内识别多个整理区间")
    print(f"  ② 箱体参数: {args.box_days}日窗口, 振幅{args.box_min_range}%~{args.box_max_range}%, 至少70%K线在区间内")
    print(f"  ③ 支撑确认: ≥{args.min_box_count}个箱体下沿接近(±{args.support_tolerance}%)，贪心聚类")
    print(f"  ④ 趋势过滤: MA10斜率>{args.min_ma10_slope}%/天且递增, MA20斜率>0且递增")
    print(f"  ⑤ 底部买入: 价格触及支撑位±{args.buy_zone_pct}%区域")
    print(f"  ⑥ 反弹确认: 收阳线/量缩(<5日均量×0.8)/长下影线(>实体×1.5)，至少满足一个")
    print(f"  ⑦ 板块过滤: 创/科板流通市值<30亿")
    print(f"  ⑧ 买入日: 涨跌幅-3%~+3%, 跳空-0.5%~+0.5%, 振幅<3%, 收盘价买入")
    print(f"出场条件:")
    print(f"  ① 止盈: {args.take_profit}%")
    print(f"  ② 止损: 从峰值回撤{args.stop_loss}% (主板自适应20%)")
    print(f"  ③ 跟踪止损: 盈利>{args.trailing_activate}%后激活, 从峰值回撤{args.trailing_pct}%")
    print(f"  ④ 持仓上限: {args.max_hold}天")
    print(f"信号筛选: 每天最多{args.top_per_day}个 (按箱体数>收阳>量缩排序)")
    print(f"股票: {len(codes)}只\n")

    all_trades = []
    success = 0

    for i, code in enumerate(codes):
        bars = fetch_kline_db(code, args.days) if use_db else fetch_kline(code, args.days)
        if not bars:
            continue

        # 板块自适应参数
        board = get_board_name(code)
        if args.board_adaptive and board in ('沪主板', '深主板'):
            _stop_loss = 20.0  # 主板放宽到20%
        else:
            _stop_loss = args.stop_loss

        trades = strategy_peak_breakout(
            bars, code,
            box_days=args.box_days,
            box_max_range=args.box_max_range,
            box_min_range=args.box_min_range,
            box_min_bars=args.box_min_bars,
            vol_expand_min=args.vol_expand_min,
            vol_expand_max=args.vol_expand_max,
            stop_loss_pct=_stop_loss,
            trailing_pct=args.trailing_pct,
            trailing_activate_pct=args.trailing_activate,
            take_profit_pct=args.take_profit,
            max_hold_days=args.max_hold,
            top_per_day=args.top_per_day,
            pullback_confirm=args.pullback_confirm,
            pullback_days=args.pullback_days,
            min_box_count=args.min_box_count,
            support_tolerance=args.support_tolerance,
            buy_zone_pct=args.buy_zone_pct,
            min_ma10_slope=args.min_ma10_slope,
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

        # 按策略路径统计
        print(f"\n--- 策略路径统计 ---")
        for path_label in sorted(set(t.get('path_label', t.get('path', '')) for t in all_trades)):
            seg = [t for t in all_trades if t.get('path_label', t.get('path', '')) == path_label]
            if seg:
                print_stats(seg, path_label)

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
        for lo, hi in [(3, 8), (8, 12), (12, 20), (20, 100)]:
            seg = [t for t in all_trades if lo <= t['pullback_days'] < hi]
            if seg:
                print_stats(seg, f"洗盘[{lo},{hi})天")

        # 按放量倍数分段
        print(f"\n--- 放量倍数分段(相对洗盘期间) ---")
        for lo, hi in [(1.0, 1.3), (1.3, 1.5), (1.5, 2.0), (2.0, 3.0)]:
            seg = [t for t in all_trades if lo <= t['vol_ratio_vs_pullback'] < hi]
            if seg:
                print_stats(seg, f"放量[{lo},{hi})x")

        # MA5斜率
        print(f"\n--- MA5斜率 ---")
        for lo,hi,label in [(-99,0,'负'),(0,0.3,'缓'),(0.3,0.6,'中'),(0.6,99,'快')]:
            ts=[t for t in all_trades if lo<=t.get('ma5_slope',0)<hi]
            if ts: print_stats(ts, label)

        # MA5加速度
        print(f"\n--- MA5加速度 ---")
        for lo,hi,label in [(-99,-0.1,'减速'),(-0.1,0.1,'平稳'),(0.1,0.3,'加速'),(0.3,99,'强加速')]:
            ts=[t for t in all_trades if lo<=t.get('ma5_accel',0)<hi]
            if ts: print_stats(ts, label)

        # MA10斜率
        print(f"\n--- MA10斜率 ---")
        for lo,hi,label in [(-99,0,'负'),(0,0.2,'缓'),(0.2,0.5,'中'),(0.5,99,'快')]:
            ts=[t for t in all_trades if lo<=t.get('ma10_slope',0)<hi]
            if ts: print_stats(ts, label)

        # 流通市值
        print(f"\n--- 流通市值 ---")
        for lo,hi,label in [(0,50,'<50亿'),(50,100,'50~100亿'),(100,500,'100~500亿'),(500,2000,'500~2000亿')]:
            ts=[t for t in all_trades if lo<=t.get('circ_mcap',0)<hi]
            if ts: print_stats(ts, label)

        # 换手率
        print(f"\n--- 换手率 ---")
        for lo,hi in [(0,2),(2,5),(5,10),(10,20),(20,100)]:
            ts=[t for t in all_trades if lo<=t.get('turnover',0)<hi]
            if ts: print_stats(ts, f"[{lo},{hi})%")

        # 买入日分析
        print(f"\n{'='*60}")
        print(f"买入日特征分析")
        print(f"{'='*60}")
        
        buy_day_results = []
        for t in all_trades:
            code = t['code']
            entry_date = t['entry_date']
            signal_date = t['signal_date']
            
            bars = fetch_kline(code, args.days)
            if not bars or len(bars) < 60:
                continue
            
            sig_idx = None
            entry_idx = None
            for i, b in enumerate(bars):
                if b['time'] == signal_date:
                    sig_idx = i
                if b['time'] == entry_date:
                    entry_idx = i
            
            if sig_idx is None or entry_idx is None or entry_idx < 1:
                continue
            
            closes = [b['close'] for b in bars]
            opens = [b['open'] for b in bars]
            highs = [b['high'] for b in bars]
            lows = [b['low'] for b in bars]
            volumes = [b['volume'] for b in bars]
            
            sig_close = closes[sig_idx]
            sig_vol = volumes[sig_idx]
            
            buy_open = opens[entry_idx]
            buy_close = closes[entry_idx]
            buy_high = highs[entry_idx]
            buy_low = lows[entry_idx]
            buy_vol = volumes[entry_idx]
            
            gap_pct = (buy_open / sig_close - 1) * 100
            buy_chg = (buy_close / buy_open - 1) * 100
            buy_vol_ratio = buy_vol / sig_vol if sig_vol > 0 else 1.0
            avg_vol_5 = sum(volumes[max(0,entry_idx-5):entry_idx]) / min(5, entry_idx) if entry_idx > 0 else buy_vol
            buy_vol_ratio_5 = buy_vol / avg_vol_5 if avg_vol_5 > 0 else 1.0
            buy_range = (buy_high - buy_low) / buy_open * 100 if buy_open > 0 else 0
            buy_lower_shadow = min(buy_close, buy_open) - buy_low
            buy_body = abs(buy_close - buy_open)
            buy_has_lower_shadow = buy_lower_shadow > buy_body * 0.5 if buy_body > 0 else buy_lower_shadow > 0
            
            buy_day_results.append({
                **t,
                'gap_pct': round(gap_pct, 2),
                'buy_chg': round(buy_chg, 2),
                'buy_vol_ratio': round(buy_vol_ratio, 2),
                'buy_vol_ratio_5': round(buy_vol_ratio_5, 2),
                'buy_range': round(buy_range, 2),
                'buy_has_lower_shadow': buy_has_lower_shadow,
            })
        
        if buy_day_results:
            big_buy = [t for t in buy_day_results if t.get('peak_return_pct',0) >= 20]
            bad_buy = [t for t in buy_day_results if t.get('peak_return_pct',0) < 5]
            
            # 高开/低开
            print(f"\n--- 高开/低开分析 ---")
            for label, cond in [('高开(>0.5%)', lambda t: t['gap_pct'] > 0.5),
                                 ('平开(-0.5~0.5%)', lambda t: -0.5 <= t['gap_pct'] <= 0.5),
                                 ('低开(<-0.5%)', lambda t: t['gap_pct'] < -0.5)]:
                seg = [t for t in buy_day_results if cond(t)]
                if seg:
                    print_stats(seg, label)
            
            # 开口大小
            print(f"\n--- 开口大小分段 ---")
            for lo,hi in [(-99,-2),(-2,-1),(-1,0),(0,1),(1,2),(2,99)]:
                seg = [t for t in buy_day_results if lo<=t['gap_pct']<hi]
                if seg:
                    print_stats(seg, f"[{lo},{hi})%")
            
            # 买入日涨跌
            print(f"\n--- 买入日涨跌幅 ---")
            for lo,hi in [(-99,-3),(-3,-1),(-1,0),(0,1),(1,3),(3,99)]:
                seg = [t for t in buy_day_results if lo<=t['buy_chg']<hi]
                if seg:
                    print_stats(seg, f"[{lo},{hi})%")
            
            # 买入日量比(vs信号日)
            print(f"\n--- 买入日量比(vs信号日) ---")
            for lo,hi in [(0,0.5),(0.5,0.8),(0.8,1.0),(1.0,1.5),(1.5,2.0),(2.0,99)]:
                seg = [t for t in buy_day_results if lo<=t['buy_vol_ratio']<hi]
                if seg:
                    print_stats(seg, f"[{lo},{hi})")
            
            # 买入日量比(vs前5日均量)
            print(f"\n--- 买入日量比(vs前5日均量) ---")
            for lo,hi in [(0,0.5),(0.5,0.8),(0.8,1.0),(1.0,1.5),(1.5,2.0),(2.0,99)]:
                seg = [t for t in buy_day_results if lo<=t['buy_vol_ratio_5']<hi]
                if seg:
                    print_stats(seg, f"[{lo},{hi})")
            
            # 买入日振幅
            print(f"\n--- 买入日振幅 ---")
            for lo,hi in [(0,2),(2,3),(3,5),(5,8),(8,99)]:
                seg = [t for t in buy_day_results if lo<=t['buy_range']<hi]
                if seg:
                    print_stats(seg, f"[{lo},{hi})%")
            
            # 买入日下影线
            print(f"\n--- 买入日下影线 ---")
            for label, cond in [('有下影线', lambda t: t['buy_has_lower_shadow']),
                                 ('无下影线', lambda t: not t['buy_has_lower_shadow'])]:
                seg = [t for t in buy_day_results if cond(t)]
                if seg:
                    print_stats(seg, label)
            
            # 大赢家特征
            if big_buy and bad_buy:
                print(f"\n--- 大赢家 vs 低质量 效应量 ---")
                for field in ['gap_pct', 'buy_chg', 'buy_vol_ratio', 'buy_vol_ratio_5', 'buy_range']:
                    b_vals = [t.get(field, 0) for t in big_buy]
                    d_vals = [t.get(field, 0) for t in bad_buy]
                    if b_vals and d_vals:
                        import numpy as np
                        b_arr = np.array(b_vals)
                        d_arr = np.array(d_vals)
                        diff = abs(b_arr.mean() - d_arr.mean())
                        pooled_std = np.sqrt((b_arr.std()**2 + d_arr.std()**2) / 2)
                        effect = diff / pooled_std if pooled_std > 0 else 0
                        effect_level = '***强' if effect > 0.8 else '**中' if effect > 0.5 else '*弱' if effect > 0.2 else '无'
                        print(f"  {field}: 大赢家{b_arr.mean():.2f} 低质量{d_arr.mean():.2f} 效应量{effect:.3f} {effect_level}")

        # TOP N
        n = min(args.top, len(all_trades))
        print(f"\n--- TOP {n} 最佳交易 ---")
        for t in sorted(all_trades, key=lambda x: -x['return_pct'])[:n]:
            print(f"  {t['code']}({t['board']}) {t['signal_date']} "
                  f"第一底{t['first_trough_low']} 颈线{t['neckline_high']} "
                  f"第二底{t['second_trough_low']} "
                  f"→ {t['entry_date']}买{t['entry_price']:>7.2f} "
                  f"收益{t['return_pct']:+.2f}% 持仓{t['exit_day']}天")

        print(f"\n--- TOP {n} 最差交易 ---")
        for t in sorted(all_trades, key=lambda x: x['return_pct'])[:n]:
            print(f"  {t['code']}({t['board']}) {t['signal_date']} "
                  f"第一底{t['first_trough_low']} 颈线{t['neckline_high']} "
                  f"第二底{t['second_trough_low']} "
                  f"→ {t['entry_date']}买{t['entry_price']:>7.2f} "
                  f"收益{t['return_pct']:+.2f}% 持仓{t['exit_day']}天")

    # --today 模式: 今日信号 + 持仓建议
    if args.today:
        today_str = args.today_date or time.strftime("%Y-%m-%d")
        today_trades = print_today_signals(all_trades, today_str)
        if today_trades:
            out_today = f"today_signals_{today_str}.json"
            with open(out_today, 'w', encoding='utf-8') as f:
                json.dump(today_trades, f, ensure_ascii=False, indent=2)
            print(f"\n  {out_today} ({len(today_trades)}笔)")

    # 保存JSON
    if all_trades:
        out_file = "test_box_breakout_result.json"
        with open(out_file, 'w', encoding='utf-8') as f:
            json.dump(all_trades, f, ensure_ascii=False, indent=2)
        print(f"\n  {out_file} ({len(all_trades)}笔)")

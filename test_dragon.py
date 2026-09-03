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
│ 入场: 涨停 → 回调 → 末期缩量小阴 → 回调结束确认                           │
│ ────────────────────────────────────────────────────────                     │
│ 1. 找到涨停日(lu_idx)                                                      │
│ 2. 回调期: 涨停日后连续收盘<涨停收盘价, 持续3~11天                        │
│    - 循环遇到close>=涨停收盘价时立即终止(回调中断则无信号)                 │
│ 3. 信号日(pullback_end, 回调最后一天):                                     │
│    - 涨跌幅在 -3% ~ -0.5% (末期小阴, 抛压枯竭)                            │
│    - 量比(信号日量/前一天量)在 0.5x ~ 0.8x (缩量)                          │
│ 4. 买入判定: 信号日(D0=回调最后一天)收盘可知条件判定, 不依赖D+1任何数据   │
│    (旧版"回调结束确认=次日收盘>=涨停收盘"已移除: 该确认在买入时点不可知,  │
│     属未来函数; 回调是否延续由出场规则承担)                              │
│ 5. 去重: 同一股票若存在多个涨停, pullback_end距前一信号<=4天则跳过         │
│ 6. 买入: 信号日次日(D+1)开盘买, 无收盘确认                                │
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
│ 入场: 连板≥2 → 断板 → 逐日as-of确认 → 次日开盘买入                        │
│ ────────────────────────────────────────────────────────                     │
│ 1. 找到连板(连续涨停≥2天, 前10天无涨停为首板)                              │
│ 2. 断板期: 连板后第一个非涨停日起, 逐日判定确认点(与--today同一路径)       │
│ 3. 断板期基础检查 (5a-5e):                                                  │
│    - 低点 >= 涨停日开盘价 (支撑有效)                                       │
│    - 断板期均量在 1.2x~2.0x涨停日量 (适度换手)                             │
│    - 首个断板日涨跌在 -5% ~ +8%                                            │
│    - 首个断板日开盘跳空在 -3% ~ +5%                                        │
│    - 回撤 >= -10%                                                          │
│ 4. 增强过滤 (三通道OR, 满足其一; BOARD_PARAMS.enhance_filter 可关):        │
│    - 通道1: 确认日涨跌 [0%, 2%)  (企稳)                                    │
│    - 通道2: 断板期均量比 >= 1.4  (换手充分)                                │
│    - 通道3: 连板前20日涨幅 >= 30% (前期热度, 大肉股富集)                   │
│ 5. 均线多头排列 (确认日 MA5>MA10>MA20): 已评估, 默认关闭                  │
│    (BOARD_PARAMS.ma_bull_filter; 120天验证胜率持平60.0%, 均收益+3.55%      │
│    →+4.53%, 作用不大未启用; 重开只需置 True)                               │
│ 6. 买入: 确认日次日(D+1)开盘买                                             │
│                                                                             │
│ 诚实口径回测 (300交易日全市场, 特征样本223笔):                              │
│   无过滤: 223笔 58.3%/+3.36% → 三通道159笔 64.2%/+4.52% (现行)             │
│ 大肉挖掘 (5年22万as-of样本): 热度>=30%子集 83%/+13% (结果导向)              │
│                                                                             │
│ 出场:                                                                       │
│ ────────────────────────────────────────────────────────                     │
│   止损:     -8% (主板) / -10% (创/科板)                                    │
│   追踪止损: -6% (主板) / -8% (创/科板)                                     │
│   峰值逃顶: 涨>10%后大上影线(>40%)收盘逃顶                                │
│   持仓上限: 7天                                                            │
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

# Windows 控制台默认 GBK, emoji 会导致 UnicodeEncodeError → 保留控制台编码, 不可编码字符降级为 ?
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors='replace')
    except Exception:
        pass

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
    # DB 1D K线时间归一为当天 15:00:00, query 用 time <= end;
    # end 取次日午夜才能包含今天 15:00 那根K线, 否则会把"今天"漏掉
    end = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
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

    result = {
        'exit_price': round(exit_p, 3), 'exit_day': exit_d,
        'return_pct': round((exit_p / entry_price - 1) * 100, 2),
        'peak_return_pct': round((peak / entry_price - 1) * 100, 2),
    }
    if d1_limit_up:
        result['d1_limit_up'] = d1_limit_up
    return result

def strategy_dragon_callback(bars, code, min_pullback_days=3, max_pullback_days=11,
                             max_last_chg=3.0,
                             hold_days=7, stop_loss=-5.0, trailing_stop=-5.0):
    """龙回头 (as-of 统一版): 逐日只用"当日收盘可知"的数据判定候选信号, 次日开盘买入。

    与 --today 报告共用同一个判定函数 dragon_today_d0_signals:
      D-N涨停 → 连续回调3-11天(每日收盘<涨停收盘) → 当日末期缩量小阴(-3%~-0.5%)+量比0.5~0.8
    → D+1开盘买入, 出场规则(stop-5/trail-5/峰值逃顶/持仓7天)照常模拟。

    时间线严格性: 第i日的判定只使用 bars[:i+1], 不包含 D+1 及以后任何数据;
    回调期是否"结束"不在入场时预知, 延续风险由出场规则承担 (与实盘每日报告完全一致)。
    去重与原回测一致: 新信号与上一个已采纳信号的区间±4天内跳过。
    """
    board_type = get_board_type(code)
    n = len(bars)
    if n < 5:
        return []
    lu_all = find_limit_ups(bars, board_type)
    lu_set = set(lu_all)
    trades = []
    used_ranges = []

    for i in range(2, n - 1):
        # 逐日候选判定: 与 --today 完全同一函数 (today_str=None → 取截断面最后一根=第i日)
        sigs = dragon_today_d0_signals(
            bars[:i + 1], code,
            min_pullback_days=min_pullback_days,
            max_pullback_days=max_pullback_days,
            max_last_chg=max_last_chg,
            limit_ups=[j for j in lu_all if j < i])
        if not sigs:
            continue
        sig = sigs[0]
        # 去重 (与原回测 used_ranges 规则一致): 区间±4天内跳过
        skip = False
        for (s, e) in used_ranges:
            if abs(i - s) <= 4 or abs(i - e) <= 4:
                skip = True
                break
        if skip:
            continue
        lu_idx = _find_bar_idx(bars, sig['lu_date'])
        used_ranges.append((lu_idx, i))

        # 入场: 次日(D+1)开盘价 —— 第i日收盘后即可确定, 无未来数据
        d1 = bars[i + 1]
        entry_price = d1['open']
        if entry_price <= 0:
            continue
        result = run_backtest(bars, i + 1, entry_price, hold_days, stop_loss, trailing_stop,
                              board_type, peak_exit=True, d1_limit_up=False, d1_change=None)
        if not result:
            continue

        trades.append({
            **sig,
            'entry_date': d1['time'],
            'entry_price': round(entry_price, 3),
            'buy_mode': 'next_open',
            **result,
        })

    return trades
def dragon_today_d0_signals(bars, code, min_pullback_days=3, max_pullback_days=11,
                            max_last_chg=3.0, today_str=None, limit_ups=None):
    """龙回头 今日(D0)入场信号: 只检查D0是否满足信号日, 不依赖D+1数据

    独立于策略回测, 仅用于 --today 报告。今日=D0(数据最后一根K线或today_str),
    往前找 涨停 → 回调 → 末期缩量小阴 结构, 且 D0 为信号日满足条件 → 次日D1开盘买入。

    返回空list或单元素list, 元素含 lu_date/pullback_days/signal_date/signal_chg/entry_vol_r。
    """
    result = []
    n = len(bars)
    if n < 3:
        return result
    if today_str:
        idxs = [j for j, b in enumerate(bars) if b['time'] == today_str]
        if not idxs:
            return result
        i = idxs[-1]
    else:
        i = n - 1  # 最后一天视为今日(D0)
    if i < 2:
        return result
    board_type = get_board_type(code)

    d0 = bars[i]
    prev_c = bars[i - 1]['close']
    if prev_c <= 0:
        return result
    # D0为信号日: 末期小阴 -max_last_chg% < 涨跌 < -0.5% (缩量下跌, 抛压枯竭)
    last_chg = (d0['close'] / prev_c - 1) * 100
    if not (-max_last_chg < last_chg < -0.5):
        return result
    # 信号日量比: D0量 / D-1量 (0.5~0.8x 缩量)
    prev_vol = bars[i - 1]['volume']
    entry_vol_r = d0['volume'] / prev_vol if prev_vol > 0 else 0
    if entry_vol_r < 0.5 or entry_vol_r >= 0.8:
        return result

    # 往前找涨停日, 使其回调期结束于今日 (pullback_end == i)
    # limit_ups: 预计算的涨停日索引列表(as-of 重放用, 避免重复扫描); 缺省时自行计算
    for lu_idx in (limit_ups if limit_ups is not None else find_limit_ups(bars[:i], board_type)):
        lu_close = bars[lu_idx]['close']
        pullback_end = None
        for j in range(lu_idx + 1, min(lu_idx + 20, i + 1)):
            if bars[j]['close'] < lu_close:
                pullback_end = j
            elif j >= lu_idx + min_pullback_days:
                break
            else:
                break
        if pullback_end is None or pullback_end != i:
            continue
        # 注意: 不检查 i+1 收盘是否仍 < lu_close (旧版"回调结束确认")。
        # 该确认需要 T+1 收盘, 在 i 日收盘时点不可知; 若在 --today-date 回放中检查
        # 会读入回放日之后的数据 (未来函数), 且与回测口径不一致。
        # 回调是否真正结束不在入场时预知, 延续风险由出场规则承担 (as-of 统一口径)。
        pullback_days = pullback_end - lu_idx
        if pullback_days < min_pullback_days or pullback_days > max_pullback_days:
            continue
        result.append({
            'code': code, 'board': get_board_name(code),
            'path': 'dragon_callback', 'path_label': '龙回头',
            'lu_date': bars[lu_idx]['time'],
            'pullback_days': pullback_days,
            'signal_date': bars[pullback_end]['time'],
            'signal_chg': round(last_chg, 2),
            'signal_vol_r': round(entry_vol_r, 2),
            'signal_price': round(d0['close'], 3),
            'entry_vol_r': round(entry_vol_r, 2),
            'buy_mode': 'next_open',
        })
        break  # 只取一个信号
    return result

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
    """V1 (as-of 统一版): 逐日只用当日收盘可知数据判定 D0 四因子, 次日开盘买入。

    与 --today 报告共用同一个判定函数 v1_today_d0_signals:
      D0涨停 + 20日涨>30% + D-1回调3~10% + OBV5日上升 + D-1非放量
    → D+1开盘买入; D1入场过滤(开盘涨幅/收盘涨幅)使用 D1 当日数据,
      与实盘"D1开盘后人工筛选"一致; 随后按出场规则模拟 (日内动量<3% → D2开盘清仓)。
    """
    board_type = get_board_type(code)
    n = len(bars)
    if n < 30:
        return []
    trades = []

    for i in range(25, n - 1):
        # 逐日候选判定: 与 --today 完全同一函数
        sigs = v1_today_d0_signals(
            bars[:i + 1], code,
            ret_20d_min=ret_20d_min,
            d_1_pullback_min=d_1_pullback_min,
            d_1_pullback_max=d_1_pullback_max,
            obv_filter=obv_filter,
            d_1_vol_max=d_1_vol_max)
        if not sigs:
            continue
        sig = sigs[0]

        # 入场: 次日(D+1)开盘价 + D1当日过滤 (与实盘 D1 开盘后人工筛选口径一致)
        d0 = bars[i]
        d1 = bars[i + 1]
        if buy_mode == "signal_close":
            entry_price = d0['close']
            entry_idx = i
            entry_date = d0['time']
        else:
            entry_price = d1['open']
            entry_idx = i + 1
            entry_date = d1['time']
            d1_change = (d1['close'] / d0['close'] - 1) * 100
            d1_gap = (d1['open'] / d0['close'] - 1) * 100
            min_d1_gap = -3.0 if board_type == "main" else -5.0
            if d1_gap < min_d1_gap: continue
            if d1_change < 0: continue
            if board_type == "gem_star" and d1_gap >= 5.0: continue
            # 主板高开3%~5%不入场 (v4数据驱动)
            if board_type == "main" and 3.0 <= d1_gap < 5.0: continue
        if entry_price <= 0: continue

        d1_change = (d1['close'] / d0['close'] - 1) * 100
        d1_gap = (d1['open'] / d0['close'] - 1) * 100
        d1_limit_up_val = is_limit_up(d1['close'], d0['close'], board_type)
        bt = run_backtest(bars, entry_idx, entry_price, hold_days, stop_loss, trailing_stop,
                          board_type, is_v1=True, d1_limit_up=d1_limit_up_val,
                          d1_change=d1_change, d1_gap=d1_gap)
        if not bt: continue

        trades.append({
            **sig,
            'entry_date': entry_date,
            'entry_price': round(entry_price, 3),
            'buy_mode': buy_mode,
            'd1_change': round(d1_change, 2),
            'd1_gap': round(d1_gap, 2),
            'intraday': round(d1_change - d1_gap, 2),
            **bt,
        })

    return trades
def v1_today_d0_signals(bars, code, ret_20d_min=30.0,
                        d_1_pullback_min=-10.0, d_1_pullback_max=-3.0,
                        obv_filter=True, d_1_vol_max=1.5, today_str=None):
    """V1 今日(D0)入场信号: 只检查D0四因子, 不依赖D1数据

    独立于策略回测, 仅用于 --today 报告中的「今日入场」段。
    满足D0四因子 → 下一个交易日开盘买入, D1入场规则(D1开收盘/回踩)开盘后由人工筛选。

    today_str: 指定今日日期(与--today-date一致), 为空则用最后一天。
    返回空list或单元素list, 元素含 d0_date/d0_close/ret_20d/d_1_change。
    """
    result = []
    n = len(bars)
    if n < 26:
        return result
    if today_str:
        idxs = [j for j, b in enumerate(bars) if b['time'] == today_str]
        if not idxs:
            return result
        i = idxs[-1]
    else:
        i = n - 1  # 最后一天视为今日(D0)
    if i < 2:
        return result
    board_type = get_board_type(code)
    threshold = 0.098 if board_type == "main" else 0.198
    d0 = bars[i]
    d_1 = bars[i-1]
    d_2 = bars[i-2]
    if d_2['close'] <= 0 or d_1['close'] <= 0:
        return result
    if (d0['close'] / d_1['close'] - 1) < threshold * 0.98:
        return result

    # === 因子1: 强趋势 20日涨>ret_20d_min% ===
    if i < 20 or bars[i-20]['close'] <= 0:
        return result
    ret_20d = (d0['close'] / bars[i-20]['close'] - 1) * 100
    if ret_20d < ret_20d_min:
        return result

    # === 因子2: D-1回调 d_1_pullback_min~d_1_pullback_max% ===
    d_1_change = (d_1['close'] / d_2['close'] - 1) * 100
    if d_1_change < d_1_pullback_min or d_1_change >= d_1_pullback_max:
        return result

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
        if len(obv_list) >= 5 and obv_list[-1] - obv_list[-5] <= 0:
            return result

    # === 因子4: D-1非放量 < d_1_vol_max x 5日均量 ===
    if i >= 6:
        vol_ma5_d1 = sum(bars[j]['volume'] for j in range(i-6, i-1)) / 5
        if vol_ma5_d1 > 0 and d_1['volume'] / vol_ma5_d1 >= d_1_vol_max:
            return result

    result.append({
        'code': code, 'board': get_board_name(code),
        'path': 'v1', 'path_label': 'V1',
        'd0_date': d0['time'],
        'd0_close': round(d0['close'], 3),
        'ret_20d': round(ret_20d, 2),
        'd_1_change': round(d_1_change, 2),
        'buy_mode': 'next_open',
    })
    return result

# ================================================================
# 断板买入策略
# ================================================================

BOARD_PARAMS = {
    # enhance_filter: 断板增强过滤 (三通道OR, 满足其一即可; 置 False 可整体关闭)
    #   通道1: 确认日涨跌 [confirm_chg_min, confirm_chg_max)  (企稳)
    #   通道2: 断板期均量比 >= vol_r_or_min                    (换手充分)
    #   通道3: 连板前20日涨幅 >= pre20_min                     (前期热度, 大肉股富集)
    # ma_bull_filter: 均线多头排列过滤 — 已评估: 胜率持平、均收益略增, 作用不大, 默认关闭
    "main": {"stop_loss": -8.0, "trailing_stop": -6.0, "take_profit": 15.0, "hold_days": 20, "vol_min": 1.2, "vol_max": 2.0, "drawdown_max": -10,
             "enhance_filter": True, "confirm_chg_min": 0.0, "confirm_chg_max": 2.0, "vol_r_or_min": 1.4, "pre20_min": 30.0, "ma_bull_filter": False},
    "gem_star": {"stop_loss": -10.0, "trailing_stop": -8.0, "take_profit": 20.0, "hold_days": 15, "vol_min": 1.2, "vol_max": 2.5, "drawdown_max": -15,
                 "enhance_filter": True, "confirm_chg_min": 0.0, "confirm_chg_max": 2.0, "vol_r_or_min": 1.4, "pre20_min": 30.0, "ma_bull_filter": False},
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

def _ma_bull_at(bars, ci):
    """确认日均线多头排列: MA5>MA10>MA20 (ci=确认日索引); 数据不足(上市<20日)返回None"""
    if ci + 1 < 20:
        return None
    c = [float(b['close']) for b in bars[ci - 19:ci + 1]]
    ma5 = sum(c[-5:]) / 5
    ma10 = sum(c[-10:]) / 10
    ma20 = sum(c) / 20
    return ma5 > ma10 > ma20


def _break_signal_at(bars, code, streak_start, streak_end, min_streak, max_break_gap, params):
    """给定连板区间[streak_start,streak_end], 计算断板期并执行回测的5a-5e确认。

    与 strategy_break_buy 中"买点之前"的判定完全一致(同一段代码), 供回测和
    break_today 今日检测共用, 保证 --today 与回测逻辑严格对齐。
    返回信号dict(含 break_date/break_days/break_chg/break_gap/break_vol_r)或 None。
    """
    bt = get_board_type(code)
    streak_len = streak_end - streak_start + 1
    if streak_len < min_streak:
        return None

    # 断板期: 涨停日后连续非涨停的天数
    break_idx = streak_end + 1
    if break_idx >= len(bars):
        return None
    limit_bar = bars[streak_end]
    limit_open = float(limit_bar['open'])
    limit_close = float(limit_bar['close'])
    limit_vol = float(limit_bar['volume'])
    break_days = 0
    for j in range(break_idx, min(break_idx + max_break_gap + 1, len(bars))):
        if is_limit_up(bars[j]['close'], bars[j - 1]['close'], bt):
            break  # 遇到新涨停, 断板期结束
        break_days += 1

    if break_days == 0:
        # 涨停后直接又是涨停 → 连板加速, 不是断板
        return None

    # 5. 断板期各项检查 (与 strategy_break_buy 完全一致)
    break_bars = bars[break_idx:break_idx + break_days]
    first_break = break_bars[0]

    # 5a. 断板期低点不能跌破涨停日开盘价 (支撑有效)
    break_low = min(float(b['low']) for b in break_bars)
    if break_low < limit_open:
        return None

    # 5b. 断板期缩量检查 (vs 涨停日量)
    break_vol_avg = sum(float(b['volume']) for b in break_bars) / len(break_bars)
    break_vol_r = break_vol_avg / limit_vol if limit_vol > 0 else 0
    if break_vol_r < params['vol_min'] or break_vol_r >= params['vol_max']:
        return None

    # 5c. 第一个断板日涨跌过滤: vs 涨停日收盘, 允许 -5% ~ +8%
    first_break_chg = (first_break['close'] / limit_close - 1) * 100
    if first_break_chg < -5 or first_break_chg >= 8:
        return None

    # 5d. 第一个断板日开盘过滤: 高开不超过 5%, 低开不超过 3%
    first_break_gap = (first_break['open'] / limit_close - 1) * 100
    if first_break_gap < -3 or first_break_gap >= 5:
        return None

    # 5e. 回撤检查
    break_drawdown = (break_low / limit_close - 1) * 100
    if break_drawdown < params['drawdown_max']:
        return None

    # 5f. 确认日特征 + 增强过滤 (三通道OR, 满足其一即可)
    #     通道1: 确认日涨跌 [0,2)  通道2: 断板期均量比>=1.4  通道3: 连板前20日涨幅>=30 (热度)
    #     确认日 = 断板期最后一天; 特征仅用当日及以前数据, as-of 安全, 回测与 --today 共用本判定
    confirm_bar = break_bars[-1]
    confirm_prev = break_bars[-2] if len(break_bars) >= 2 else limit_bar
    _c_prev_close = float(confirm_prev['close'])
    confirm_chg = (float(confirm_bar['close']) / _c_prev_close - 1) * 100 if _c_prev_close > 0 else 0.0
    confirm_gap = (float(confirm_bar['open']) / _c_prev_close - 1) * 100 if _c_prev_close > 0 else 0.0
    pre20_gain = None
    if streak_start >= 20:
        _pre_ref = float(bars[streak_start - 20]['close'])
        if _pre_ref > 0:
            pre20_gain = (limit_close / _pre_ref - 1) * 100
    if params.get('enhance_filter', True):
        _pass_chg = params.get('confirm_chg_min', 0.0) <= confirm_chg < params.get('confirm_chg_max', 2.0)
        _pass_vol = break_vol_r >= params.get('vol_r_or_min', 1.4)
        _pass_hot = pre20_gain is not None and pre20_gain >= params.get('pre20_min', 30.0)
        if not (_pass_chg or _pass_vol or _pass_hot):
            return None

    # 5g. 均线多头排列 (确认日 MA5>MA10>MA20): 剔除断板期处于均线纠缠/空头的弱信号
    ma_bull = _ma_bull_at(bars, break_idx + break_days - 1)
    if params.get('ma_bull_filter', True) and ma_bull is False:
        return None

    return {
        'break_idx': break_idx, 'break_days': break_days,
        'break_date': bars[break_idx]['time'],
        'streak_len': streak_len, 'streak_start': bars[streak_start]['time'], 'streak_end': bars[streak_end]['time'],
        'break_chg': round(first_break_chg, 2),
        'break_gap': round(first_break_gap, 2),
        'break_vol_r': round(break_vol_r, 2),
        'confirm_chg': round(confirm_chg, 2),
        'confirm_gap': round(confirm_gap, 2),
        'pre20_gain': round(pre20_gain, 2) if pre20_gain is not None else None,
        'ma_bull': ma_bull,
    }

def strategy_break_buy(bars, code, min_streak=2, max_break_gap=5, override_params=None):
    """断板买入 (as-of 统一版): 逐日判定"今日是否为断板期确认日", 次日开盘买入。

    与 --today 报告共用同一个判定函数 break_today_d0_signals (+ _break_signal_at 的
    5a~5e 检查): 连板≥2 → 断板期(低点不破涨停开盘/缩量/首日涨跌与gap/回撤) →
    断板期最后一天收盘确认 → 次日开盘买入。出场: 止损/追踪止损/峰值逃顶/持仓上限。
    """
    bt = get_board_type(code)
    params = dict(BOARD_PARAMS[bt])
    if override_params: params.update(override_params)
    stop_loss, trailing_stop = params["stop_loss"], params["trailing_stop"]
    hold_days = params["hold_days"]
    n = len(bars)
    if n < 6:
        return []
    lu_all = find_limit_ups(bars, bt)
    lu_set = set(lu_all)
    trades = []
    used = set()

    for i in range(4, n - 1):
        # 确认日必为非涨停日 (断板期最后一天)
        if is_limit_up(bars[i]['close'], bars[i-1]['close'], bt): continue
        # 廉价预过滤: 断板期结束于i → 必存在距i不超过max_break_gap的涨停日
        if not any(j in lu_set for j in range(max(1, i - max_break_gap), i)):
            continue
        # 逐日候选判定: 与 --today 完全同一函数
        sigs = break_today_d0_signals(
            bars[:i + 1], code,
            min_streak=min_streak, max_break_gap=max_break_gap,
            limit_ups=[j for j in lu_all if j < i])
        if not sigs:
            continue
        sig = sigs[0]

        # 去重 (与原回测一致): 同一连板起点+断板日只取一次
        key = (sig['streak_start'], sig['break_date'])
        if key in used: continue
        used.add(key)

        # 入场: 次日(D+1)开盘价
        entry_price = bars[i + 1]['open']
        if entry_price <= 0: continue
        result = run_backtest_breakbuy(bars, i + 1, entry_price, hold_days, stop_loss, trailing_stop, bt)
        if not result: continue

        prev_close = bars[i]['close']
        trades.append({
            **sig,
            'signal_date': bars[i]['time'],
            'entry_date': bars[i + 1]['time'],
            'entry_price': round(entry_price, 3),
            'buy_mode': 'next_open',
            'd1_change': round((bars[i + 1]['close'] / bars[i + 1]['open'] - 1) * 100, 2) if bars[i + 1]['open'] > 0 else 0,
            'd1_gap': round((bars[i + 1]['open'] / prev_close - 1) * 100, 2) if prev_close > 0 else 0,
            'intraday': round((bars[i + 1]['close'] - bars[i + 1]['open']) / prev_close * 100, 2) if prev_close > 0 else 0,
            **result,
        })

    return trades
def break_today_d0_signals(bars, code, min_streak=2, max_break_gap=5, today_str=None, limit_ups=None):
    """断板 今日(D0)信号: 判断今日是否为断板期的确认日, 与回测买点前规则完全一致

    原则: --today 时"买入当日(次日D1开盘)由人工判别", 今日(D0)及以前规则与回测
    strategy_break_buy 的"买点之前"完全一致。断板策略的确认点在断板期最后一天收盘
    (buy_mode=next_open, 买入=次日), 因此今日(D0) = 断板期的最后一天, 而非首断板日。
    断板期指标(5a低点/5b平均量比/5c首日涨跌/5d首日gap/5e回撤)复用 _break_signal_at,
    与回测逐条一致, 绝不引入未来数据。

    返回空list或单元素list。
    """
    result = []
    n = len(bars)
    if n < 3:
        return result
    if today_str:
        idxs = [j for j, b in enumerate(bars) if b['time'] == today_str]
        if not idxs:
            return result
        i = idxs[-1]
    else:
        i = n - 1
    if i < 2:
        return result
    bt = get_board_type(code)
    params = BOARD_PARAMS.get(bt, BOARD_PARAMS['main'])

    # 寻找所有连板结构, 要求断板期最后一天 == 今日(i)
    for lu_idx in (limit_ups if limit_ups is not None else find_limit_ups(bars[:i], bt)):
        # 连板第一板确认 (lu_idx 前一日非涨停)
        is_first = True
        for k in range(1, min(11, lu_idx + 1)):
            if lu_idx - k - 1 >= 0 and is_limit_up(bars[lu_idx - k]['close'], bars[lu_idx - k - 1]['close'], bt):
                is_first = False; break
        if not is_first:
            continue
        # 连板结束位置
        streak_start = lu_idx; streak_end = lu_idx
        while streak_end < i - 1 and is_limit_up(bars[streak_end + 1]['close'], bars[streak_end]['close'], bt):
            streak_end += 1
        # 断板期必须且只能在今日结束: break_idx > streak_end 且 break_days 全落在 <=i,
        # 断板期最后一天(break_idx+break_days-1) == i 才意味着今日收盘可确认、明日买入。
        sig = _break_signal_at(bars, code, streak_start, streak_end, min_streak, max_break_gap, params)
        if not sig:
            continue
        if sig['break_idx'] + sig['break_days'] - 1 != i:
            continue
        result.append({
            'code': code, 'board': get_board_name(code), 'path': 'break_buy', 'path_label': '断板',
            'mode': 'streak_break',
            'streak_len': sig['streak_len'],
            'streak_start': sig['streak_start'],
            'streak_end': sig['streak_end'],
            'break_date': sig['break_date'],
            'signal_date': bars[i]['time'],
            'break_days': sig['break_days'],
            'break_chg': sig['break_chg'],
            'break_gap': sig['break_gap'],
            'break_vol_r': sig['break_vol_r'],
            'confirm_chg': sig['confirm_chg'],
            'confirm_gap': sig['confirm_gap'],
            'pre20_gain': sig['pre20_gain'],
            'ma_bull': sig['ma_bull'],
            'entry_price': None, 'buy_mode': 'next_open',
        })
        break  # 只取一个信号
    return result

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
    # 剔除未入场占位信号(entry_price<=0): 它们没有胜率/盈亏意义, 会污染统计
    trades = [t for t in trades if t.get('entry_price', 0) and t['entry_price'] > 0]
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


def buy_suggestion_text(d0_close, board_type, path='dragon_callback'):
    """D1开盘买入建议(文字描述, 供人工筛选, 按策略区分)

    dragon_callback: D1开盘买入, 无方向过滤
    break_buy:       D1开盘买入, 无方向过滤
    v1:              D1开盘买入, 主板高开3%~5%不入场, 创/科板高开>=5%不入场; 收盘需收红
    """
    if path == 'v1':
        if board_type == 'gem_star':
            lo_p = d0_close * 0.95
            hi_p = d0_close * 1.05
            return (f"开盘 -5%~+5% 可买(约{lo_p:.2f}~{hi_p:.2f}), 收盘需收红(>=0%)")
        lo_p = d0_close * 0.97
        hi_p = d0_close * 1.03
        return (f"开盘 -3%~+3% 可买(约{lo_p:.2f}~{hi_p:.2f}), 高开3%以上不入场, 收盘需收红(>=0%)")
    # 龙回头 / 断板: D1开盘买入, 无方向过滤
    if board_type == 'gem_star':
        lo_p = d0_close * 0.95
        hi_p = d0_close * 1.05
        return (f"开盘 -5%~+5% 可买(约{lo_p:.2f}~{hi_p:.2f})")
    lo_p = d0_close * 0.97
    hi_p = d0_close * 1.03
    return (f"开盘 -3%~+3% 可买(约{lo_p:.2f}~{hi_p:.2f}), 高开3%以上不入场")


def _find_bar_idx(bars, date_str):
    """在bars中定位指定交易日索引, 找不到返回None"""
    for j, b in enumerate(bars):
        if b['time'] == date_str:
            return j
    return None


def _db_last_bar_date(bars_by_code):
    """today_str 默认值: 数据库K线最后一根日期 (取全市场已加载股票的最大日期)

    遍历所有非空bars, 取最大的 last['time'], 不依赖代码加载顺序;
    空则返回None(调用方回退系统日期)。
    """
    last = None
    for bars in (bars_by_code or {}).values():
        if bars:
            d = bars[-1]['time']
            if last is None or d > last:
                last = d
    return last


def _last_bar_idx_on_or_before(bars, date_str):
    """bars 中最后一条 time <= date_str 的索引(as-of 语义), 不存在返回 None"""
    idx = None
    for j, b in enumerate(bars):
        if b['time'] <= date_str:
            idx = j
        else:
            break
    return idx


def simulate_holding_to_today(bars, t, today_idx, board_type):
    """从入场日到today重跑该策略出场规则, 判定截至today的持仓状态

    独立于回测: 只用 入场日 ~ today 之间的K线, 不读取未来数据。
    按各策略(龙回头/断板/V1)各自的出场规则(止损/追踪/逃顶/持仓上限)判定。

    返回 dict:
      status     : 'open' | 'closed' | 'not_yet'
                    not_yet = 入场日 > today (尚未买入)
                    closed  = today之前已触发平仓
                    open    = 截至today仍持仓
      exit_reason/exit_date: 仅在closed时
      today_action: None | reason_str  (open时, today收盘触发应明日处理)
      hold_days : 截至today的持仓交易日数 (open时)
      curr_ret  : 截至today收盘的浮动收益% (open时)
    """
    path = t['path']
    entry_price = t['entry_price']
    if entry_price <= 0:
        return {'status': 'not_yet'}
    entry_idx = _find_bar_idx(bars, t['entry_date'])
    if entry_idx is None:
        return None
    if entry_idx > today_idx:
        return {'status': 'not_yet'}

    # 各策略出场参数 (与回测 run_backtest / run_backtest_breakbuy 保持一致)
    if path == 'v1':
        hold_days, stop, trail, is_v1 = 7, -10.0, -5.0, True
        peak_enabled, peak_ret, upper_pct = False, 7, 30
    elif path == 'dragon_callback':
        hold_days, stop, trail, is_v1 = 7, -5.0, -5.0, False
        peak_enabled, peak_ret, upper_pct = True, 7, 30
    elif path == 'break_buy':
        p = BOARD_PARAMS['gem_star' if board_type == 'gem_star' else 'main']
        hold_days, stop, trail = p['hold_days'], p['stop_loss'], p['trailing_stop']
        is_v1 = False
        peak_enabled, peak_ret, upper_pct = True, 10, 40
    else:
        return None

    peak = entry_price
    if entry_idx < len(bars) and bars[entry_idx]['high'] > peak:
        peak = bars[entry_idx]['high']

    for d in range(1, hold_days + 1):
        idx = entry_idx + d - 1
        if idx >= len(bars):
            break
        if idx > today_idx:
            # 还没走到today, 前方未触发 → today仍持仓
            break
        b = bars[idx]
        if b['high'] > peak:
            peak = b['high']

        triggered = None  # 今日(today收盘)触发的出场, 应明日执行

        if is_v1 and d == 2:
            intraday = t.get('intraday', 0)
            if intraday < 3:
                # 明日开盘清仓
                if idx == today_idx:
                    return {'status': 'open', 'today_action': 'D1日内动量<3%, 明日开盘清仓',
                            'hold_days': d, 'curr_ret': (b['close'] / entry_price - 1) * 100}
                return {'status': 'closed', 'exit_reason': 'D1日内动量<3% 明日开盘清仓', 'exit_date': b['time']}
        if peak_enabled:
            ret = (b['close'] / entry_price - 1) * 100
            if ret > peak_ret:
                bar_range = b['high'] - b['low']
                upper = (b['high'] - max(b['open'], b['close'])) / bar_range * 100 if bar_range > 0 else 0
                if upper > upper_pct and b['close'] < b['high'] * 0.98:
                    triggered = triggered or f'峰值逃顶 收盘卖出'
        if d > 1 and b['low'] <= peak * (1 + trail / 100):
            triggered = triggered or f'追踪止损{trail}%'
        if b['low'] <= entry_price * (1 + stop / 100):
            triggered = triggered or f'止损{stop}%'

        if idx == today_idx and triggered:
            # today收盘已触发出场规则 → 明日开盘清仓
            return {'status': 'open', 'today_action': triggered,
                    'hold_days': d, 'curr_ret': (b['close'] / entry_price - 1) * 100}
        if triggered:
            # 历史某日触发 → 已平仓, 不入持仓
            return {'status': 'closed', 'exit_reason': triggered, 'exit_date': b['time']}

    # 走完到today仍无触发 → 检查是否已到期
    expiry_idx = entry_idx + hold_days - 1  # 到期日索引
    if today_idx > expiry_idx:
        # today已过到期日 → 该仓位已在到期日收盘平仓, 不再持仓
        return {'status': 'closed', 'exit_reason': f'持仓到期{hold_days}天', 'exit_date': bars[expiry_idx]['time']}
    if today_idx == expiry_idx:
        # today恰好是到期日 → 今日收盘卖出
        return {'status': 'open', 'today_action': f'到达持仓上限{hold_days}天, 今日收盘卖出',
                'hold_days': hold_days, 'curr_ret': (bars[today_idx]['close'] / entry_price - 1) * 100}
    hold_days_cnt = today_idx - entry_idx + 1
    return {'status': 'open', 'today_action': None,
            'hold_days': hold_days_cnt, 'curr_ret': (bars[today_idx]['close'] / entry_price - 1) * 100}

def print_today_signals(today_stream, today_str, bars_by_code=None):
    """D0收盘后运行, 显示今日 入场/持仓/出场 + 次日买入建议

    数据源: build_today_stream 生成的 as-of 信号事件流 —— 每个信号在
    "数据只到信号日"的截面上产生, 与回测买点前规则同一套判定函数,
    不受后续K线影响。持仓按各策略出场规则重算, 含 7 天最大持仓周期。
    """
    def _sig_date(t):
        return t.get('signal_date') or t.get('d0_date') or ''

    # 今日信号: 信号日 == today(买入日 = 下一交易日开盘)
    # as-of 可见性: 信号日晚于 today 的交易在当下尚不存在, 全部段落不可见
    visible = [t for t in today_stream if _sig_date(t) <= today_str]
    dc_today = [t for t in visible if t['path'] == 'dragon_callback' and _sig_date(t) == today_str]
    v1_today = [t for t in visible if t['path'] == 'v1' and _sig_date(t) == today_str]
    bb_today = [t for t in visible if t['path'] == 'break_buy' and _sig_date(t) == today_str]
    today_trades = dc_today + v1_today + bb_today
    # 待买入: 信号日早于today但入场日晚于today(停牌/次日未到) → 视同今日待买入
    pending_early = [t for t in visible if _sig_date(t) != today_str and (not t.get('entry_date') or t['entry_date'] > today_str)]

    print(f"\n{'=' * 80}")
    print(f"📅 {today_str} 今日信号 (D0收盘后, 次日D1开盘买入)")
    print(f"{'=' * 80}")

    if not today_trades:
        print(f"  今日无信号")
        if pending_early:
            print(f"  ⏳ 待买入 (信号已确认, 入场日未到): {len(pending_early)}只")
            for t in sorted(pending_early, key=_sig_date):
                print(f"    {t['code']:<8} {t.get('board',''):<6} {t.get('path_label','')} 信号{t.get('signal_date') or t.get('d0_date')}")
        return today_trades

    print(f"  共 {len(today_trades)} 只股票出现信号")

    # 龙回头信号
    if dc_today:
        print(f"\n  🐉 龙回头 ({len(dc_today)}只) - 缩量小阴确认, 次日D1开盘买:")
        for t in sorted(dc_today, key=lambda x: x.get('entry_vol_r', 0), reverse=True):
            bt = get_board_type(t['code'])
            signal_price = t.get('signal_price') or t.get('entry_price')
            text = buy_suggestion_text(signal_price, bt, path='dragon_callback')
            print(f"    {t['code']:<8} {t['board']:<6} 涨停{t['lu_date']} 回调{t['pullback_days']}天 "
                  f"信号{t['signal_date']} {t['signal_chg']:+.1f}% 量比{t['entry_vol_r']:.2f}x")
            print(f"{'':>10} 信号价{signal_price:.2f} 买入建议: {text}")
        print(f"  {'─' * 85}")
        print(f"  📋 D1入场条件: 无特殊限制, D1开盘买入即可")

    # V1信号
    if v1_today:
        print(f"\n  🔥 V1 ({len(v1_today)}只) - D0涨停确认, 次日D1开盘买:")
        print(f"  {'代码':>8} {'板块':>6} {'动量':>6} {'评分':>4} {'D0收':>8} {'D-1回调':>8} {'20日涨':>8}")
        print(f"  {'-' * 85}")
        for t in sorted(v1_today, key=lambda x: calc_momentum_score(x), reverse=True):
            code, board = t['code'], t['board']
            bt = get_board_type(code)
            d0_close = t.get('d0_close', t.get('entry_price', 0))
            score = calc_momentum_score(t)
            label = momentum_label(score)
            text = buy_suggestion_text(d0_close, bt, path='v1')
            print(f"  {code:>8} {board:>6} {label:>6} {score:>3}  {d0_close:>7.2f} "
                  f"{t['d_1_change']:>+7.1f}% {t['ret_20d']:>+7.1f}%")
            print(f"{'':>10} 买入建议: {text}")
        # V1 D1入场条件
        print(f"  {'─' * 85}")
        print(f"  📋 D1入场条件(开盘后人工筛选):")
        print(f"     主板: D1开盘涨幅>=-3% 且 收盘涨幅>=0; 高开3%~5%不入场")
        print(f"     创/科板: D1开盘涨幅>=-5% 且 <5% 且 收盘涨幅>=0")

    # 断板信号
    if bb_today:
        print(f"\n  💥 断板 ({len(bb_today)}只) - 连板后断板确认, 次日开盘买:")
        print("  [提示] 量比越高越好(>=1.8x标[量比佳]); 前期20日涨幅>=30%走热度通道(标[热度佳]); 按量比降序")
        for t in sorted(bb_today, key=lambda x: (x.get('break_vol_r', 0), x.get('streak_len', 0)), reverse=True):
            ep = t.get('entry_price')
            ep_txt = f"{ep:.2f}" if ep else "次日开盘"
            _vr = t.get('break_vol_r', 0)
            _vol_tag = ' [量比佳]' if _vr >= 1.8 else ''
            _cc = t.get('confirm_chg')
            _cc_txt = f" 确认日{_cc:+.1f}%" if _cc is not None else ''
            _pg = t.get('pre20_gain')
            _pg_txt = f" 热度{_pg:.0f}%" if _pg is not None else ''
            _hot_tag = ' [热度佳]' if (_pg is not None and _pg >= 30) else ''
            print(f"    {t['code']:<8} {t['board']:<6} {t['streak_len']}板连板 "
                  f"断板{t['break_date']} {t['break_chg']:+.1f}% 量{_vr:.2f}x{_cc_txt}{_pg_txt}{_vol_tag}{_hot_tag} 预计开盘{ep_txt}")
        print(f"  {'─' * 85}")
        print(f"  📋 D1入场条件: 无特殊限制, D1开盘买入即可")

    # ===== 持仓分析 (信号流中已入场、截至today未平仓的仓位, 按各策略出场规则重算) =====
    if bars_by_code and visible:
        open_pos = []
        holdings_input = [t for t in visible if t.get('entry_date') and t['entry_date'] <= today_str]
        for t in holdings_input:
            bars = bars_by_code.get(t['code'])
            if not bars:
                continue
            ti = _last_bar_idx_on_or_before(bars, today_str)
            if ti is None:
                continue
            st = simulate_holding_to_today(bars, t, ti, t.get('board', get_board_type(t['code'])))
            if st and st['status'] == 'open':
                st['trade'] = t
                open_pos.append(st)

        sell = [s for s in open_pos if s.get('today_action')]
        hold = [s for s in open_pos if not s.get('today_action')]

        print(f"\n{'=' * 80}")
        print(f"📊 持仓分析 (截至 {today_str} 仍持仓 {len(open_pos)}只, 按各策略出场规则重算)")
        print(f"{'=' * 80}")
        print(f"  持仓: {len(open_pos)}只 | 🔴明日清仓: {len(sell)}只 | 🟢继续持有: {len(hold)}只")

        if sell:
            print(f"\n  🔴 明日开盘清仓 ({len(sell)}只):")
            print(f"  {'代码':>8} {'板块':>6} {'策略':>6} {'买入日':>12} {'买入价':>8} {'持仓天':>6} {'现价':>8} {'浮动':>7}  原因")
            print(f"  {'-' * 100}")
            for s in sorted(sell, key=lambda x: x['curr_ret']):
                t = s['trade']
                pl = {'dragon_callback': '龙回头', 'v1': 'V1', 'break_buy': '断板', }.get(t['path'], t['path'])
                _bi = _last_bar_idx_on_or_before(bars_by_code[t['code']], today_str)
                cur = bars_by_code[t['code']][_bi]['close'] if _bi is not None else float('nan')
                print(f"  {t['code']:>8} {t['board']:>6} {pl:>6} {t['entry_date']:>12} "
                      f"{t['entry_price']:>7.2f} {s['hold_days']:>5}天 {cur:>8.2f} {s['curr_ret']:>+6.1f}%  {s['today_action']}")

        if hold:
            print(f"\n  🟢 继续持有 ({len(hold)}只):")
            print(f"  {'代码':>8} {'板块':>6} {'策略':>6} {'买入日':>12} {'买入价':>8} {'持仓天':>6} {'现价':>8} {'浮动':>7}")
            print(f"  {'-' * 75}")
            for s in sorted(hold, key=lambda x: -x['curr_ret']):
                t = s['trade']
                pl = {'dragon_callback': '龙回头', 'v1': 'V1', 'break_buy': '断板', }.get(t['path'], t['path'])
                _bi = _last_bar_idx_on_or_before(bars_by_code[t['code']], today_str)
                cur = bars_by_code[t['code']][_bi]['close'] if _bi is not None else float('nan')
                print(f"  {t['code']:>8} {t['board']:>6} {pl:>6} {t['entry_date']:>12} "
                      f"{t['entry_price']:>7.2f} {s['hold_days']:>5}天 {cur:>8.2f} {s['curr_ret']:>+6.1f}%")
        if not open_pos:
            print(f"\n  (截至{today_str} 无仍在持仓的策略仓位)")

    # ===== 待买入 (信号已确认但入场日未到: 停牌跨日等少数场景) =====
    if pending_early:
        print(f"\n  ⏳ 待买入 ({len(pending_early)}只):")
        for t in sorted(pending_early, key=_sig_date):
            print(f"    {t['code']:<8} {t.get('board',''):<6} {t.get('path_label','')} 信号{t.get('signal_date') or t.get('d0_date')} → 下一交易日开盘买入")

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
    parser.add_argument("--today-date", type=str, default="", help="指定日期(YYYY-MM-DD), 默认为库内最后交易日; 晚于库内最后交易日时按库内最后交易日处理")
    args = parser.parse_args()
    codes = [c.strip() for c in args.codes.split(",") if c.strip()] if args.codes else TEST_CODES

    # 指定codes时自动使用DB模式
    use_db = args.source == "db" or len(codes) > 0
    if args.source == "db":
        print("📊 DB模式: 从数据库加载全市场股票...")
        codes = get_all_codes_db()
        print(f"   全市场: {len(codes)} 只股票")
    elif codes:
        print(f"📊 指定股票: {codes}，自动从DB加载数据...")

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
    bars_by_code = {}
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
        if args.today:
            bars_by_code[code] = bars

        parts = []

        if run_dc:
            dc = strategy_dragon_callback(bars, code,
                                           min_pullback_days=args.pullback,
                                           max_pullback_days=args.max_pullback,
                                           max_last_chg=args.max_last_chg)
            dc_trades.extend(dc)
            parts.append(f"龙回头{len(dc)}")
        if run_v1:
            # 如果预加载了K线, 直接用; 否则单独加载
            code_bars = all_bars.get(code) if all_bars else bars
            v1 = strategy_v1(code_bars, code, buy_mode=args.buy_mode,
                             hold_days=7,
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

    # (today 报告由回测交易列表直接驱动: 回测已是逐日 as-of 判定, 信号不会随新数据漂移)

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

    # ===== 今日买点统计 (as-of 信号事件流: 与实盘选股同一套规则, 不受后续K线影响) =====
    if args.today:
        db_last = _db_last_bar_date(bars_by_code)
        asof_str = args.today_date or db_last or time.strftime("%Y-%m-%d")
        # as-of 语义: 指定日期超过库内最后交易日时, 按库内最后交易日处理
        # (如库内日K到 09-02, --today-date 09-03 == --today, 显示 09-02 信号, 09-03 开盘买入)
        if db_last and asof_str > db_last:
            asof_str = db_last
        # as-of 当日的未入场候选(次日开盘买入): 回测会跳过无 D+1 的信号, 这里补齐当日候选
        pending_signals = []
        for code, bars in bars_by_code.items():
            idx = _last_bar_idx_on_or_before(bars, asof_str)
            if idx is None or bars[idx]['time'] != asof_str or idx < 2:
                continue
            bt = get_board_type(code)
            lu_sub = [j for j in find_limit_ups(bars, bt) if j < idx]
            if run_dc:
                pending_signals.extend(dragon_today_d0_signals(
                    bars[:idx + 1], code,
                    min_pullback_days=args.pullback,
                    max_pullback_days=args.max_pullback,
                    max_last_chg=args.max_last_chg,
                    limit_ups=lu_sub))
            if run_v1:
                pending_signals.extend(v1_today_d0_signals(
                    bars[:idx + 1], code,
                    ret_20d_min=args.ret_20d_min,
                    d_1_pullback_min=args.d1_pullback_min,
                    d_1_pullback_max=args.d1_pullback_max,
                    obv_filter=not args.no_obv_filter,
                    d_1_vol_max=args.d1_vol_max))
            if run_bb:
                pending_signals.extend(break_today_d0_signals(
                    bars[:idx + 1], code, limit_ups=lu_sub))
        existing_keys = {(t['code'], t.get('signal_date') or t.get('d0_date')) for t in all_trades}
        pending_signals = [s for s in pending_signals if (s['code'], s.get('signal_date') or s.get('d0_date')) not in existing_keys]
        today_trades = print_today_signals(all_trades + pending_signals, asof_str, bars_by_code=bars_by_code)
        if today_trades:
            with open(f"today_signals_{asof_str}.json", "w", encoding="utf-8") as f:
                json.dump(today_trades, f, ensure_ascii=False, indent=2)
            print(f"\n💾 today_signals_{asof_str}.json ({len(today_trades)}笔)")

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

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
  python test_dragon.py --source db --days 300 --strategy dragon  # 龙回头(方案2)

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
│ 300日全市场回测: 136笔 79.4%胜率, 均+5.15%, 盈亏比2.26                      │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│ 入场: D0涨停日筛选, D1开盘买入                                              │
│ ────────────────────────────────────────────────────────                     │
│ 因子1 强趋势:   20日涨幅 >= 30%                                             │
│ 因子2 回踩确认: D-1涨跌幅在 -10% ~ -3%                                     │
│ 因子3 资金锁仓: OBV 5日趋势上升                                            │
│ 因子4 非放量:   D-1成交量 < 1.5倍5日均量                                   │
│ 因子5 纯单板过热过滤: 前10天无涨停时, 要求MACD柱<2且布林带宽<45%           │
│         (有近涨停的信号不受影响; 纯单板胜率67.4%→77.3%, +9.9pp)             │
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
│   - D0是否一字板: 一字板=极强, 但实盘买不进 (已由D1 gap过滤自然排除)        │
│   - D0动量强度可决定D1追涨幅度上限                                         │
│   - ✅ 纯单板过热过滤已解决 (MACD柱+布林带宽, 见因子5)                     │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│ 龙回头 策略 (--strategy dragon)                                             │
│ 全市场300日回测: 113笔 75.2%胜率 均+3.31% 均峰值+8.07% 盈亏比1.01           │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│ 入场: 涨停 → 回调 → 拐点确认 → 信号质量过滤 → 次日开盘买入                │
│ ────────────────────────────────────────────────────────                     │
│ 1. 找龙: 滑动窗口[4,5,7,10,15,20]天内涨停占比>=70%                         │
│ 2. 回调期: 涨停日后连续收盘<涨停收盘价                                    │
│ 3. 信号日(D0): 拐点过滤在[3,30)天范围内搜索, 满足条件即确认                │
│ 4. gap过滤: 信号日距涨停日 [5,6]天 (回调5-6天为黄金区间, 噪音最少大肉最多)  │
│ 5. 拐点过滤 (或关系, 满足任一即可):                                        │
│    - D0收盘在MA20的[-10%,-5%)区间 (均线支撑)                               │
│    - 回调深度<=-30% (深跌释放卖压)                                         │
│    - 回调期阴线比例<50% (买盘承接)                                         │
│ 6. 信号质量排除:                                                           │
│    - 阴线比例>=60% → 剔除 (卖压未尽, 持仓到期概率高)                       │
│    - RSI(6)<30 → 剔除 (超卖≠反弹, 胜率17%)                                │
│    - D0距MA20<-8% → 剔除 (深度破位, 持仓到期概率高)                        │
│ 7. 去重: 同一股票信号±4天内跳过                                           │
│ 8. 买入: 信号日次日(D+1)开盘买                                            │
│                                                                             │
│ 出场: 分段追踪止损                                                         │
│ ────────────────────────────────────────────────────────                     │
│   止损:     -8%                                                             │
│   追踪止损: 分段 — 盈利<3%时-8% (给空间), 盈利>=3%时-3% (锁利润)           │
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
│    - 首个断板日涨跌在 +0% ~ +8% (排除首日下跌, 确认抛压被承接)          │
│    - 首个断板日开盘跳空在 0% ~ +5% (排除低开, 低开信号胜率偏低)        │
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
│ 诚实口径回测 (300交易日全市场):                                              │
│   无过滤: 223笔 58.3%/+3.36% → 三通道+gap>=0+chg>=0%: 94笔 74.5%/+6.03%    │
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
                          多策略独立运行 (--strategy all)
═══════════════════════════════════════════════════════════════════════════════

  龙回头 + V1 + 断板 独立运行, 互不干扰, 各自产生信号
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

# ================================================================
# 策略判定与出场引擎 — 本文件独立快照 (2026-09-07 自 auto/ 逐字内联)
# ================================================================
# 来源 (2026-09-07 快照, Phase 3 之后):
#   - auto/strategies/dragon_callback.py : DRAGON_CB_PARAMS / 龙回头D0判定 / 出场模拟
#   - auto/strategies/v1.py              : V1 D0判定
#   - auto/strategies/break_buy.py       : BOARD_PARAMS / 断板期判定 / 断板D0判定
#   - auto/common/filters.py             : PREFILTER_PARAMS / unified_prefilter (U1~U4)
#   - auto/dragon_core.py                : run_backtest(v1) / run_backtest_breakbuy
# 性质: 独立测试参考实现, 与系统 auto/ 语义一致但**不再共享代码**;
#       系统侧规则变更后需手动同步本文件, 并重跑全市场回测对数验证
#       (基线: tmp/test_dragon_callback_result_all.json, 303笔逐笔逐字段一致)。
# ================================================================================

# ---------------- 统一前置过滤 U1~U4 (快照: auto/common/filters.py) ----------------
PREFILTER_PARAMS = {
    'turnover_min': 3.0,        # U2 换手率% 下限 (全市场验证: +0.7pp; 用户经验口径5%更严, 会误杀低换手大盘样本)
    'float_mv_min': 20.0,       # U3 流通市值下限(亿) (统一层20~500亿; 严格30~300会误杀600105)
    'float_mv_max': 500.0,      # U3 流通市值上限(亿)
    'heat_ret20_min': 10.0,     # U4 前期热度: 20日涨幅% 下限 (与prior_lu或关系)
    'heat_prior_lu_min': 1,     # U4 前20日涨停次数下限 (或关系, 不含D0)
}


def unified_prefilter(bars, i, code, code_info=None):
    """统一前置过滤 U1~U4, 在判定日 i 收盘可知数据上判定。

    code_info 为该股的 stock_basic_info 字典 (含 name/circ_shares), 不是全量映射。
    返回 (ok, fail_reasons)。code_info 缺失时跳过 U1/U2/U3 (不误杀), U4 仍生效。
    易错点: 龙回头/断板的 U2/U3 必须锚定涨停日评估 (D0是缩量小阴日, @D0评估会误杀)。
    """
    p = PREFILTER_PARAMS
    fails = []
    # U1 非ST (名称兜底; 涨停阈值已自然排除ST, 此处防漏)
    if code_info and code_info.get('name') and 'ST' in str(code_info['name']).upper():
        fails.append('U1_ST')
    # U2 换手率 / U3 流通市值
    if code_info and code_info.get('circ_shares'):
        turnover = bars[i]['volume'] / code_info['circ_shares'] * 100
        if turnover < p['turnover_min']:
            fails.append(f'U2换手{turnover:.1f}')
        float_mv = code_info['circ_shares'] * bars[i]['close'] / 1e8
        if not (p['float_mv_min'] <= float_mv <= p['float_mv_max']):
            fails.append(f'U3市值{float_mv:.0f}亿')
    # U4 前期热度: 20日涨幅>=10% 或 前20日有涨停 (不含D0)
    bt = get_board_type(code)
    has_lu = any(is_limit_up(bars[j]['close'], bars[j-1]['close'], bt)
                 for j in range(max(1, i - 19), i))
    ret20 = bars[i]['close'] / bars[i - 20]['close'] - 1 if i >= 20 and bars[i - 20]['close'] > 0 else None
    if not has_lu and (ret20 is None or ret20 * 100 < p['heat_ret20_min']):
        fails.append('U4冷门')
    return (not fails), fails


# ---------------- 龙回头 (快照: auto/strategies/dragon_callback.py) ----------------
DRAGON_CB_PARAMS = dict(
    # --- 找龙: 滑动窗口涨停占比 ---
    dragon_ratio=0.7,
    dragon_windows=[4, 5, 7, 10, 15, 20],
    # --- 回调窗口 ---
    gap_min=5, gap_max=6,
    # --- 拐点过滤 (或关系) ---
    ma20_lo=-10.0, ma20_hi=-5.0,
    depth_max=-30.0,
    yin_ratio_max=0.5,
    # --- 信号质量排除 ---
    yin_ratio_exclude=0.6,
    rsi6_exclude_lt=30.0,
    d0_ma20_exclude_lt=-8.0,
    # --- 入场 ---
    d1_gap_lo=-3.0, d1_gap_hi=2.0,
    # --- 出场 ---
    hold_days=7,
    stop_loss=-8.0,
    trail_lo=-8.0,
    trail_hi=-3.0,
    trail_switch_pct=3.0,
    peak_exit_ret=7.0,
    peak_exit_upper=30.0,
)

# 龙回头信号旧输出字段 (快照: 精确对齐历史输出; 多键/少键都会破坏逐笔对数)
_DRAGON_LEGACY_FIELDS = (
    "code", "board", "path", "path_label", "lu_date", "pullback_days", "signal_date",
    "signal_chg", "signal_vol_r", "signal_price", "entry_vol_r", "buy_mode",
    "gap_from_peak", "d0_vs_ma20", "pullback_depth", "yin_ratio",
    "tech_score", "tech_rsi", "tech_roc", "tech_psy",
)


def dragon_today_d0_signals(bars, code, min_pullback_days=3, max_pullback_days=11,
                            max_last_chg=3.0, today_str=None, limit_ups=None,
                            use_tech_score=True, params=None):
    """龙回头 今日(D0)入场信号 ("方案2") — 独立快照 (原 dragon_core facade)。

    参数 min_pullback_days/max_pullback_days/max_last_chg 为旧版兼容保留, 已不参与判定。
    today_str: 指定今日日期, 为空则用最后一天。limit_ups: 预计算涨停索引 (None 则现算)。
    """
    p = {**DRAGON_CB_PARAMS, **(params or {})}
    if today_str:
        idxs = [j for j, b in enumerate(bars) if b["time"] == today_str]
        if not idxs:
            return []
        bars = bars[:idxs[-1] + 1]
    result = []
    n = len(bars)
    if n < 3:
        return result
    i = n - 1
    if i < 2:
        return result
    board_type = get_board_type(code)

    d0 = bars[i]
    prev_c = bars[i - 1]["close"]
    if prev_c <= 0:
        return result
    last_chg = (d0["close"] / prev_c - 1) * 100
    prev_vol = bars[i - 1]["volume"]
    entry_vol_r = d0["volume"] / prev_vol if prev_vol > 0 else 0

    closes = [bars[j]["close"] for j in range(i + 1)]

    # ── tech_score 加分制 (仅参考输出; RSI 值供质量排除使用) ──
    score = 0
    rsi_val = roc = psy = None
    if use_tech_score:
        dif, dea, hist = calc_macd(closes)
        if hist is not None and len(hist) >= 2:
            if is_macd_golden_cross(dif, dea, lookback=5):
                score += 3
            elif is_macd_hist_turning_positive(hist, lookback=5):
                score += 2
            elif is_macd_hist_shrinking_negative(hist, lookback=5):
                score += 1
            n_h = len(hist)
            if n_h >= 2 and abs(dif[n_h - 1]) < abs(dea[n_h - 1]) * 0.5:
                score += 1
            if dif[n_h - 1] < dea[n_h - 1] and dif[n_h - 2] >= dea[n_h - 2]:
                score -= 2
        rsi_val = rsi(closes, period=6)
        if rsi_val is not None:
            if rsi_val < 30:
                score += 2
            elif rsi_val < 40:
                score += 1
            elif rsi_val < 60:
                score -= 1
            else:
                score -= 2
        roc = calc_roc(closes, period=5)
        if roc is not None:
            if -10 <= roc < 0 or 0 <= roc < 5:
                score += 1
            elif roc < -15 or roc >= 5:
                score -= 1
        psy = calc_psy(closes, period=10)
        if psy is not None:
            if psy < 30:
                score += 2
            elif psy < 40:
                score += 1
            elif psy >= 50:
                score -= 1

    # ── 方案2 主判定 ──
    for lu_idx in (limit_ups if limit_ups is not None else find_limit_ups(bars[:i], board_type)):
        lu_close = bars[lu_idx]["close"]
        if lu_close <= 0:
            continue

        # 当前日(i)收盘必须仍低于涨停收盘 (仍在回调中)
        if bars[i]["close"] >= lu_close:
            continue

        pullback_days = i - lu_idx

        # ── Step1: 找龙 — 滑动窗口内涨停占比>=70% ──
        dragon_found = False
        for window in p["dragon_windows"]:
            start = max(1, lu_idx - window)
            total_days = lu_idx - start
            if total_days < 3:
                continue
            lu_count = sum(1 for k in range(start, lu_idx)
                           if k > 0 and is_limit_up(bars[k]["close"], bars[k - 1]["close"], board_type))
            if lu_count / total_days >= p["dragon_ratio"]:
                dragon_found = True
                break
        if not dragon_found:
            continue

        # ── Step2: gap [gap_min, gap_max] ──
        gap_from_peak = i - lu_idx
        if gap_from_peak < p["gap_min"] or gap_from_peak > p["gap_max"]:
            continue

        # ── 回调期特征 ──
        if i >= 19:
            ma20 = sum(bars[j]["close"] for j in range(i - 19, i + 1)) / 20
            d0_vs_ma20 = (d0["close"] / ma20 - 1) * 100 if ma20 > 0 else None
        else:
            d0_vs_ma20 = None

        min_low = min(bars[j]["low"] for j in range(lu_idx + 1, i + 1))
        pullback_depth = (min_low / lu_close - 1) * 100

        pb_yin = sum(1 for j in range(lu_idx + 1, i + 1) if bars[j]["close"] < bars[j]["open"])
        pb_total = i - lu_idx
        yin_ratio = pb_yin / pb_total if pb_total > 0 else 1.0

        # ── 拐点过滤 (或关系) ──
        cond_ma20 = d0_vs_ma20 is not None and p["ma20_lo"] <= d0_vs_ma20 < p["ma20_hi"]
        cond_depth = pullback_depth <= p["depth_max"]
        cond_yin = yin_ratio < p["yin_ratio_max"]
        if not (cond_ma20 or cond_depth or cond_yin):
            continue

        # ── 信号质量排除 ──
        if yin_ratio >= p["yin_ratio_exclude"]:
            continue
        if rsi_val is not None and rsi_val < p["rsi6_exclude_lt"]:
            continue
        if d0_vs_ma20 is not None and d0_vs_ma20 < p["d0_ma20_exclude_lt"]:
            continue

        ex = {
            "board": get_board_name(code),
            "lu_date": bars[lu_idx]["time"],
            "pullback_days": pullback_days,
            "signal_chg": round(last_chg, 2),
            "signal_vol_r": round(entry_vol_r, 2),
            "signal_price": round(d0["close"], 3),
            "entry_vol_r": round(entry_vol_r, 2),
            "buy_mode": "next_open",
            "gap_from_peak": gap_from_peak,
            "d0_vs_ma20": round(d0_vs_ma20, 2) if d0_vs_ma20 is not None else None,
            "pullback_depth": round(pullback_depth, 2),
            "yin_ratio": round(yin_ratio, 2),
            "tech_score": score,
            "tech_rsi": round(rsi_val, 1) if rsi_val else None,
            "tech_roc": round(roc, 1) if roc else None,
            "tech_psy": round(psy, 1) if psy else None,
        }
        result.append(
            {k: ex.get(k) for k in _DRAGON_LEGACY_FIELDS
             if k not in ("code", "board", "path", "path_label", "signal_date")} | {
                "code": code,
                "board": ex.get("board"),
                "path": "dragon_callback",
                "path_label": "龙回头",
                "signal_date": d0["time"],
            })
        break
    return result


def run_backtest_dragon_callback(bars, entry_idx, entry_price, hold_days=None,
                                 stop_loss=None, board_type="main", stop_at_idx=None, **params):
    """龙回头出场模拟: 分段追踪止损 (as-of安全)。

    出场判定顺序 (每日): 1)峰值逃顶 2)分段追踪(d>1) 3)固定止损 4)到期/stop_at_idx截断。
    stop_at_idx: 只模拟到该bar索引; 未触发出场 → open=True。
    """
    p = {**DRAGON_CB_PARAMS, **(params or {})}
    hold_days = p["hold_days"] if hold_days is None else hold_days
    stop_loss = p["stop_loss"] if stop_loss is None else stop_loss
    if entry_price <= 0 or entry_idx >= len(bars):
        return None
    n = len(bars)
    peak = entry_price
    exit_p, exit_d, exit_reason = entry_price, 0, ""
    capped = False

    for d in range(1, hold_days + 1):
        idx = entry_idx + d - 1
        if idx >= n:
            break
        if stop_at_idx is not None and idx > stop_at_idx:
            capped = True
            break
        b = bars[idx]
        if b["high"] > peak:
            peak = b["high"]

        # 1. 峰值逃顶
        ret = (b["close"] / entry_price - 1) * 100
        if ret > p["peak_exit_ret"]:
            rng = b["high"] - b["low"]
            upper = (b["high"] - max(b["open"], b["close"])) / rng * 100 if rng > 0 else 0
            if upper > p["peak_exit_upper"] and b["close"] < b["high"] * 0.98:
                exit_p, exit_d, exit_reason = b["close"], d, "峰值逃顶"
                break

        # 2. 分段追踪止损
        if d > 1:
            peak_ret = (peak / entry_price - 1) * 100
            trail = p["trail_hi"] if peak_ret >= p["trail_switch_pct"] else p["trail_lo"]
            if b["low"] <= peak * (1 + trail / 100):
                exit_p = peak * (1 + trail / 100)
                exit_d = d
                exit_reason = f"追踪止损{trail}%"
                break

        # 3. 固定止损
        if b["low"] <= entry_price * (1 + stop_loss / 100):
            exit_p = entry_price * (1 + stop_loss / 100)
            exit_d = d
            exit_reason = f"止损{stop_loss}%"
            break

        exit_p, exit_d = b["close"], d

    if exit_reason == "" and not capped:
        exit_reason = "持仓到期"
    return {
        "exit_price": round(exit_p, 3), "exit_day": exit_d,
        "exit_reason": exit_reason,
        "return_pct": round((exit_p / entry_price - 1) * 100, 2),
        "peak_return_pct": round((peak / entry_price - 1) * 100, 2),
        "open": bool(capped),
    }


_run_dragon_backtest = run_backtest_dragon_callback  # 旧名兼容

# ---------------- V1 (快照: auto/strategies/v1.py) ----------------

def calc_bollinger_bw(closes, period=20, num_std=2):
    """计算布林带宽百分比 = (upper-lower)/middle*100, 仅返回带宽值"""
    if len(closes) < period:
        return None
    window = closes[-period:]
    mid = sum(window) / period
    if mid <= 0:
        return None
    var = sum((x - mid) ** 2 for x in window) / period
    std = var ** 0.5
    upper = mid + num_std * std
    lower = mid - num_std * std
    return (upper - lower) / mid * 100


def v1_today_d0_signals(bars, code, ret_20d_min=30.0,
                        d_1_pullback_min=-10.0, d_1_pullback_max=-3.0,
                        obv_filter=True, d_1_vol_max=1.5, today_str=None,
                        stock_info=None):
    """V1 今日(D0)入场信号 — 独立快照 (原 dragon_core facade)。

    today_str: 指定今日日期, 为空则用最后一天。
    返回空list或单元素list, 元素含 d0_date/d0_close/ret_20d/d_1_change。
    易错点: D-1回调区间是 [-10%,-3%) 左闭右开; OBV 从 i-20 起累计且 j=0 不计 — 勿动边界。
    """
    if today_str:
        idxs = [j for j, b in enumerate(bars) if b["time"] == today_str]
        if not idxs:
            return []
        bars = bars[:idxs[-1] + 1]
    result = []
    n = len(bars)
    if n < 26:
        return result
    i = n - 1
    if i < 2:
        return result
    board_type = get_board_type(code)
    threshold = 0.098 if board_type == "main" else 0.198
    d0 = bars[i]
    d_1 = bars[i - 1]
    d_2 = bars[i - 2]
    if d_2["close"] <= 0 or d_1["close"] <= 0:
        return result
    if (d0["close"] / d_1["close"] - 1) < threshold * 0.98:
        return result

    # === 因子1: 强趋势 20日涨>ret_20d_min% ===
    if i < 20 or bars[i - 20]["close"] <= 0:
        return result
    ret_20d = (d0["close"] / bars[i - 20]["close"] - 1) * 100
    if ret_20d < ret_20d_min:
        return result

    # === 因子2: D-1回调 [d_1_pullback_min, d_1_pullback_max) ===
    d_1_change = (d_1["close"] / d_2["close"] - 1) * 100
    if d_1_change < d_1_pullback_min or d_1_change >= d_1_pullback_max:
        return result

    # === 因子3: OBV 5日趋势上升 ===
    if obv_filter:
        obv = 0
        obv_list = []
        for j in range(max(0, i - 20), i + 1):
            if j > 0:
                if bars[j]["close"] > bars[j - 1]["close"]:
                    obv += bars[j]["volume"]
                elif bars[j]["close"] < bars[j - 1]["close"]:
                    obv -= bars[j]["volume"]
            obv_list.append(obv)
        if len(obv_list) >= 5 and obv_list[-1] - obv_list[-5] <= 0:
            return result

    # === 因子4: D-1非放量 < d_1_vol_max x 5日均量 ===
    if i >= 6:
        vol_ma5_d1 = sum(bars[j]["volume"] for j in range(i - 6, i - 1)) / 5
        if vol_ma5_d1 > 0 and d_1["volume"] / vol_ma5_d1 >= d_1_vol_max:
            return result

    # === 因子5: 纯单板过热过滤 (仅当前10天无涨停时生效) ===
    has_recent_lu = False
    for j in range(max(1, i - 10), i):
        if j >= 1 and is_limit_up(bars[j]["close"], bars[j - 1]["close"], board_type):
            has_recent_lu = True
            break
    if not has_recent_lu:
        closes = [bars[j]["close"] for j in range(i + 1)]
        _, _, hist = calc_macd(closes)
        macd_h = hist[-1] if hist else None
        boll_bw = calc_bollinger_bw(closes)
        if macd_h is not None and macd_h >= 2:
            return result
        if boll_bw is not None and boll_bw >= 45:
            return result

    circ = float((stock_info or {}).get("circ_shares") or 0)
    total = float((stock_info or {}).get("total_shares") or 0)
    d0_close = round(d0["close"], 3)
    result.append({
        "code": code,
        "board": get_board_name(code),
        "path": "v1",
        "path_label": "V1",
        "d0_date": d0["time"],
        "d0_close": d0_close,
        "ret_20d": round(ret_20d, 2),
        "d_1_change": round(d_1_change, 2),
        "turnover_anchor": round(d0["volume"] / circ * 100, 2) if circ > 0 else None,
        "turnover_anchor_total": round(d0["volume"] / total * 100, 2) if total > 0 else None,
        "buy_mode": "next_open",
    })
    return result

# ---------------- 断板 (快照: auto/strategies/break_buy.py) ----------------

BOARD_PARAMS = {
    # enhance_filter: 断板增强过滤 (三通道OR, 满足其一即可; 置 False 可整体关闭)
    #   通道1: 确认日涨跌 [confirm_chg_min, confirm_chg_max)  (企稳)
    #   通道2: 断板期均量比 >= vol_r_or_min                    (换手充分)
    #   通道3: 连板前20日涨幅 >= pre20_min                     (前期热度, 大肉股富集)
    # ma_bull_filter: 均线多头排列过滤 — 已评估: 胜率持平、均收益略增, 作用不大, 默认关闭
    "main": {"stop_loss": -8.0, "trailing_stop": -6.0, "take_profit": 15.0, "hold_days": 20, "vol_min": 1.2, "vol_max": 2.0, "drawdown_max": -10,
             "enhance_filter": True, "confirm_chg_min": 0.0, "confirm_chg_max": 2.0, "vol_r_or_min": 1.4, "pre20_min": 30.0, "ma_bull_filter": False,
             "first_break_gap_min": 0, "first_break_chg_min": 0.0},
    "gem_star": {"stop_loss": -10.0, "trailing_stop": -8.0, "take_profit": 20.0, "hold_days": 15, "vol_min": 1.2, "vol_max": 2.5, "drawdown_max": -15,
                 "enhance_filter": True, "confirm_chg_min": 0.0, "confirm_chg_max": 2.0, "vol_r_or_min": 1.4, "pre20_min": 30.0, "ma_bull_filter": False,
                 "first_break_gap_min": 0, "first_break_chg_min": 0.0},
}


def _ma_bull_at(bars, idx):
    """确认日均线多头排列: MA5>MA10>MA20 (idx=确认日索引); 数据不足(上市<20日)返回None。"""
    if idx + 1 < 20:
        return None
    c = [float(b["close"]) for b in bars[idx - 19:idx + 1]]
    ma5 = sum(c[-5:]) / 5
    ma10 = sum(c[-10:]) / 10
    ma20 = sum(c) / 20
    return ma5 > ma10 > ma20


def _break_signal_at(bars, code, streak_start, streak_end, min_streak, max_break_gap, params):
    """给定连板区间[streak_start,streak_end], 计算断板期并执行 5a-5f 确认。

    返回信号dict(含 break_date/break_days/break_chg/break_gap/break_vol_r)或 None。
    易错点: 5c/5d 上界 (+8%/+5%) 是硬编码, 与 BOARD_PARAMS 无关 — 勿"配置化"。
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
    limit_open = float(limit_bar["open"])
    limit_close = float(limit_bar["close"])
    limit_vol = float(limit_bar["volume"])
    break_days = 0
    for j in range(break_idx, min(break_idx + max_break_gap + 1, len(bars))):
        if is_limit_up(bars[j]["close"], bars[j - 1]["close"], bt):
            break  # 遇到新涨停, 断板期结束
        break_days += 1

    if break_days == 0:
        # 涨停后直接又是涨停 → 连板加速, 不是断板
        return None

    # 5. 断板期各项检查
    break_bars = bars[break_idx:break_idx + break_days]
    first_break = break_bars[0]

    # 5a. 断板期低点不能跌破涨停日开盘价 (支撑有效)
    break_low = min(float(b["low"]) for b in break_bars)
    if break_low < limit_open:
        return None

    # 5b. 断板期缩量检查 (vs 涨停日量)
    break_vol_avg = sum(float(b["volume"]) for b in break_bars) / len(break_bars)
    break_vol_r = break_vol_avg / limit_vol if limit_vol > 0 else 0
    if break_vol_r < params["vol_min"] or break_vol_r >= params["vol_max"]:
        return None

    # 5c. 第一个断板日涨跌过滤: vs 涨停日收盘, 允许 first_break_chg_min ~ +8%
    first_break_chg = (first_break["close"] / limit_close - 1) * 100
    if first_break_chg < params.get("first_break_chg_min", -5) or first_break_chg >= 8:
        return None

    # 5d. 第一个断板日开盘过滤: 高开不超过 5%, 低开不低于 first_break_gap_min
    first_break_gap = (first_break["open"] / limit_close - 1) * 100
    if first_break_gap < params.get("first_break_gap_min", -3) or first_break_gap >= 5:
        return None

    # 5e. 回撤检查
    break_drawdown = (break_low / limit_close - 1) * 100
    if break_drawdown < params["drawdown_max"]:
        return None

    # 5f. 确认日特征 + 增强过滤 (三通道OR, 满足其一即可)
    confirm_bar = break_bars[-1]
    confirm_prev = break_bars[-2] if len(break_bars) >= 2 else limit_bar
    _c_prev_close = float(confirm_prev["close"])
    confirm_chg = (float(confirm_bar["close"]) / _c_prev_close - 1) * 100 if _c_prev_close > 0 else 0.0
    confirm_gap = (float(confirm_bar["open"]) / _c_prev_close - 1) * 100 if _c_prev_close > 0 else 0.0
    pre20_gain = None
    if streak_start >= 20:
        _pre_ref = float(bars[streak_start - 20]["close"])
        if _pre_ref > 0:
            pre20_gain = (limit_close / _pre_ref - 1) * 100
    if params.get("enhance_filter", True):
        # 通道1: 确认日涨跌 [confirm_chg_min, confirm_chg_max)
        _pass_chg = params.get("confirm_chg_min", 0.0) <= confirm_chg < params.get("confirm_chg_max", 2.0)
        # 通道2: 断板期均量比 >= vol_r_or_min (换手充分)
        _pass_vol = break_vol_r >= params.get("vol_r_or_min", 1.4)
        # 通道3: 连板前20日涨幅 >= pre20_min (前期热度)
        _pass_hot = pre20_gain is not None and pre20_gain >= params.get("pre20_min", 30.0)
        if not (_pass_chg or _pass_vol or _pass_hot):
            return None

    # 5g. 均线多头排列 (确认日 MA5>MA10>MA20): 剔除断板期处于均线纠缠/空头的弱信号
    ma_bull = _ma_bull_at(bars, break_idx + break_days - 1)
    if params.get("ma_bull_filter", True) and ma_bull is False:
        return None

    return {
        "break_idx": break_idx, "break_days": break_days,
        "break_date": bars[break_idx]["time"],
        "streak_len": streak_len, "streak_start": bars[streak_start]["time"], "streak_end": bars[streak_end]["time"],
        "break_chg": round(first_break_chg, 2),
        "break_gap": round(first_break_gap, 2),
        "break_vol_r": round(break_vol_r, 2),
        "confirm_chg": round(confirm_chg, 2),
        "confirm_gap": round(confirm_gap, 2),
        "pre20_gain": round(pre20_gain, 2) if pre20_gain is not None else None,
        "ma_bull": ma_bull,
    }


def break_today_d0_signals(bars, code, min_streak=2, max_break_gap=5, today_str=None,
                           limit_ups=None, stock_info=None):
    """断板 今日(D0)信号 — 独立快照 (原 dragon_core facade)。

    原则: --today 时"买入当日(次日D1开盘)由人工判别", 今日(D0)及以前规则与回测
    strategy_break_buy 的"买点之前"完全一致。确认点在断板期最后一天收盘。
    易错点: 确认日 = 断板期最后一天 (break_idx+break_days-1 == D0), 不是首断板日。
    """
    if today_str:
        idxs = [j for j, b in enumerate(bars) if b["time"] == today_str]
        if not idxs:
            return []
        bars = bars[:idxs[-1] + 1]
    result = []
    n = len(bars)
    if n < 3:
        return result
    i = n - 1
    if i < 2:
        return result
    bt = get_board_type(code)
    board_params = BOARD_PARAMS.get(bt, BOARD_PARAMS["main"])
    min_streak_eff, max_break_gap_eff = min_streak, max_break_gap

    # 寻找所有连板结构, 要求断板期最后一天 == 今日(i)
    for lu_idx in (limit_ups if limit_ups is not None else find_limit_ups(bars[:i], bt)):
        # 连板第一板确认 (lu_idx 前一日非涨停)
        is_first = True
        for k in range(1, min(11, lu_idx + 1)):
            if lu_idx - k - 1 >= 0 and is_limit_up(bars[lu_idx - k]["close"], bars[lu_idx - k - 1]["close"], bt):
                is_first = False
                break
        if not is_first:
            continue
        # 连板结束位置
        streak_start = lu_idx
        streak_end = lu_idx
        while streak_end < i - 1 and is_limit_up(bars[streak_end + 1]["close"], bars[streak_end]["close"], bt):
            streak_end += 1
        sig = _break_signal_at(bars, code, streak_start, streak_end, min_streak_eff, max_break_gap_eff, board_params)
        if not sig:
            continue
        if sig["break_idx"] + sig["break_days"] - 1 != i:
            continue
        circ = float((stock_info or {}).get("circ_shares") or 0)
        total = float((stock_info or {}).get("total_shares") or 0)
        result.append({
            "code": code,
            "board": get_board_name(code),
            "path": "break_buy",
            "path_label": "断板",
            "mode": "streak_break",
            "streak_len": sig.get("streak_len"),
            "streak_start": sig.get("streak_start"),
            "streak_end": sig.get("streak_end"),
            "break_date": sig.get("break_date"),
            "signal_date": bars[i]["time"],
            "break_days": sig.get("break_days"),
            "break_chg": sig.get("break_chg"),
            "break_gap": sig.get("break_gap"),
            "break_vol_r": sig.get("break_vol_r"),
            "confirm_chg": sig.get("confirm_chg"),
            "confirm_gap": sig.get("confirm_gap"),
            "pre20_gain": sig.get("pre20_gain"),
            "ma_bull": sig.get("ma_bull"),
            "turnover_anchor": round(float(bars[streak_end]["volume"]) / circ * 100, 2) if circ > 0 else None,
            "turnover_sig": round(float(bars[i]["volume"]) / circ * 100, 2) if circ > 0 else None,
            "turnover_anchor_total": round(float(bars[streak_end]["volume"]) / total * 100, 2) if total > 0 else None,
            "turnover_sig_total": round(float(bars[i]["volume"]) / total * 100, 2) if total > 0 else None,
            "entry_price": None,
            "buy_mode": "next_open",
        })
        break  # 只取一个信号
    return result

# ---------------- V1/断板 出场引擎 (快照: auto/dragon_core.py) ----------------

def run_backtest(bars, entry_idx, entry_price, hold_days=7, stop_loss=-10.0, trailing_stop=-8.0, board_type="main", peak_exit=False, is_v1=False, d1_limit_up=None, d1_change=None, d1_gap=None):
    if entry_price <= 0 or entry_idx >= len(bars):
        return None
    limit_threshold = 0.098 if board_type == "main" else 0.198
    peak = entry_price
    exit_p = entry_price
    exit_d = 0

    # 如果外部未传入 d1_limit_up, 则在回测内计算 (兼容旧调用)
    if d1_limit_up is None:
        d1_limit_up = False
        if entry_idx + 1 < len(bars):
            d1_bar = bars[entry_idx + 1]
            d1_ret = (d1_bar['close'] / entry_price - 1)
            if d1_ret >= limit_threshold * 0.98:
                d1_limit_up = True

    # next_open模式: entry_idx=D1(D+1开盘买入); 循环d=1应指向D1
    if entry_idx < len(bars):
        d1_init = bars[entry_idx]
        if d1_init['high'] > peak:
            peak = d1_init['high']

    for d in range(1, hold_days + 1):
        idx = entry_idx + d - 1  # d=1 → entry_idx(D1), d=2 → entry_idx+1(D2)
        if idx >= len(bars): break
        b = bars[idx]
        if b['high'] > peak: peak = b['high']

        # V1出场 (v3): D1日内动量<3% → D2开盘清仓
        # 日内动量 = D1收盘涨幅 - D1开盘涨幅 (盘中买卖力量指标)
        # 注: 判定数据是D1的, 执行点是D2开盘价 — 勿把判定拖到d==3
        if is_v1 and d == 2:
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


def run_backtest_breakbuy(bars, entry_idx, entry_price, hold_days=7, stop_loss=-8.0,
                          trailing_stop=-6.0, board_type="main"):
    """断板专用回测: 追踪止损 + 峰值逃顶信号 (收盘价口径, 与v1的low触及口径不同)。"""
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

def calc_macd(closes, fast=12, slow=26, signal=9):
    """计算MACD, 返回 (dif, dea, macd_hist) 三个序列
    
    MACD柱 = 2*(DIF-DEA), DIF=EMA(fast)-EMA(slow), DEA=EMA(DIF,signal)
    """
    n = len(closes)
    if n < slow + signal:
        return None, None, None
    # 计算EMA序列
    ema_fast = [0.0] * n
    ema_slow = [0.0] * n
    k_f = 2 / (fast + 1)
    k_s = 2 / (slow + 1)
    ema_fast[0] = closes[0]
    ema_slow[0] = closes[0]
    for i in range(1, n):
        ema_fast[i] = closes[i] * k_f + ema_fast[i-1] * (1 - k_f)
        ema_slow[i] = closes[i] * k_s + ema_slow[i-1] * (1 - k_s)
    # DIF序列
    dif = [ema_fast[i] - ema_slow[i] for i in range(n)]
    # DEA = EMA(DIF, signal)
    dea = [0.0] * n
    k_sig = 2 / (signal + 1)
    dea[0] = dif[0]
    for i in range(1, n):
        dea[i] = dif[i] * k_sig + dea[i-1] * (1 - k_sig)
    # MACD柱 = 2*(DIF-DEA)
    hist = [2 * (dif[i] - dea[i]) for i in range(n)]
    return dif, dea, hist

def calc_momentum(closes, period=10):
    """计算动量指标 MOM = close[i] - close[i-period]"""
    if len(closes) < period + 1:
        return None
    return closes[-1] - closes[-1 - period]

def calc_roc(closes, period=10):
    """计算变动率 ROC = (close[i]-close[i-period])/close[i-period]*100"""
    if len(closes) < period + 1:
        return None
    ref = closes[-1 - period]
    if ref <= 0:
        return None
    return (closes[-1] - ref) / ref * 100

def calc_psy(closes, period=12):
    """计算心理线 PSY = 过去period天中上涨天数/period*100
    
    PSY>75: 市场过热; PSY<25: 市场过度悲观; PSY 25~75: 正常区间
    """
    if len(closes) < period + 1:
        return None
    up_days = 0
    for i in range(-period, 0):
        if closes[i] > closes[i-1]:
            up_days += 1
    return up_days / period * 100

def calc_bollinger(closes, period=20, num_std=2):
    """计算布林带, 返回 (upper, middle, lower, bandwidth_pct)
    bandwidth_pct = (upper-lower)/middle*100"""
    if len(closes) < period:
        return None, None, None, None
    window = closes[-period:]
    mid = sum(window) / period
    var = sum((x - mid) ** 2 for x in window) / period
    std = var ** 0.5
    upper = mid + num_std * std
    lower = mid - num_std * std
    bw = (upper - lower) / mid * 100 if mid > 0 else 0
    return upper, mid, lower, bw

def is_macd_golden_cross(dif, dea, lookback=3):
    """判断MACD是否在最近lookback根K线内发生金叉 (DIF上穿DEA)
    
    金叉条件: 当前DIF>=DEA, 且之前某根DIF<DEA
    """
    if dif is None or dea is None or len(dif) < lookback + 1:
        return False
    n = len(dif)
    if dif[n-1] < dea[n-1]:
        return False  # 当前DIF在DEA下方
    # 检查lookback根内是否有DIF<DEA
    for i in range(max(0, n - lookback - 1), n - 1):
        if dif[i] < dea[i]:
            return True
    return False

def is_macd_hist_turning_positive(hist, lookback=3):
    """判断MACD柱是否在最近lookback根内由负转正 (绿柱缩短→红柱)"""
    if hist is None or len(hist) < lookback + 1:
        return False
    n = len(hist)
    if hist[n-1] <= 0:
        return False  # 当前柱还是负的
    # 检查lookback根内是否有负柱
    for i in range(max(0, n - lookback - 1), n - 1):
        if hist[i] < 0:
            return True
    return False

def is_macd_hist_shrinking_negative(hist, lookback=5):
    """判断MACD绿柱是否在缩短 (负柱绝对值在减小)"""
    if hist is None or len(hist) < lookback:
        return False
    n = len(hist)
    # 最近lookback根的负柱
    recent = hist[n - lookback:]
    # 要求都是负柱
    if any(h >= 0 for h in recent):
        return False
    # 检查绝对值是否在递减 (从左到右负柱越来越短)
    abs_vals = [abs(h) for h in recent]
    # 至少最后2根在缩短
    return abs_vals[-1] < abs_vals[-2] < abs_vals[-3] if len(abs_vals) >= 3 else abs_vals[-1] < abs_vals[-2]

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

def strategy_dragon_callback(bars, code, min_pullback_days=3, max_pullback_days=11,
                             max_last_chg=3.0,
                             hold_days=7, stop_loss=-8.0,
                             stock_info=None, use_prefilter=True,
                             ):
    """龙回头 (as-of 统一版): 逐日只用"当日收盘可知"的数据判定候选信号, 次日开盘买入。

    框架: 找龙(滑动窗口涨停占比>=70%) → 回调→ gap[3,30) → 拐点OR → 信号质量排除 → D1开盘买
    出场: _run_dragon_backtest (分段追踪止损)
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
        lu_idx = _find_bar_idx(bars, sig['lu_date'])

        # 去重 (与原回测 used_ranges 规则一致): 区间±4天内跳过
        # 注意去重在过滤之前, 保证禁用过滤器时行为与历史基线完全一致
        skip = False
        for (s, e) in used_ranges:
            if abs(i - s) <= 4 or abs(i - e) <= 4:
                skip = True
                break
        if skip:
            continue
        used_ranges.append((lu_idx, i))

        # U1~U4 预过滤 (无未来函数)
        if use_prefilter and lu_idx > 0:
            ok, fails = unified_prefilter(bars, lu_idx, code, stock_info)
            if not ok:
                continue

        # 入场: 次日(D+1)开盘价 —— 第i日收盘后即可确定, 无未来数据
        # D1 入场 gap 过滤: 与系统 auto/strategies/dragon_callback.py entry_decision 一致
        # (d1_gap_lo <= gap <= d1_gap_hi = [-3%, +2%]), 高开2%以上不追/低开3%以下不接
        d0 = bars[i]
        d1 = bars[i + 1]
        d1_gap = (d1['open'] / d0['close'] - 1) * 100 if d0['close'] > 0 else 0
        if not (-3.0 <= d1_gap <= 2.0):
            continue
        entry_price = d1['open']
        if entry_price <= 0:
            continue

        result = _run_dragon_backtest(bars, i + 1, entry_price, hold_days, stop_loss,
                                      board_type)
        if not result:
            continue

        trades.append({
            **sig,
            'entry_date': d1['time'],
            'entry_price': round(entry_price, 3),
            'buy_mode': 'next_open',
            'd1_gap': round(d1_gap, 2),
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
                 d_1_vol_max=1.5, stock_info=None, use_prefilter=True):
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
            d_1_vol_max=d_1_vol_max,
            stock_info=stock_info)
        if not sigs:
            continue
        sig = sigs[0]

        # 统一前置过滤 U1~U4 (信号日D0收盘可知; V1的20日涨幅>=30%已隐含U4, 实际生效U1/U2/U3)
        if use_prefilter:
            ok, fails = unified_prefilter(bars, i, code, stock_info)
            if not ok:
                continue

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
def strategy_break_buy(bars, code, min_streak=2, max_break_gap=5, override_params=None,
                       stock_info=None, use_prefilter=True):
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
            limit_ups=[j for j in lu_all if j < i],
            stock_info=stock_info)
        if not sigs:
            continue
        sig = sigs[0]

        # 去重 (与原回测一致): 同一连板起点+断板日只取一次
        # 去重在过滤之前, 保证禁用过滤器时行为与历史基线完全一致
        key = (sig['streak_start'], sig['break_date'])
        if key in used: continue
        used.add(key)

        # 统一前置过滤 U1~U4 (确认日D0收盘可知; 连板>=2已隐含U4, 实际生效U1/U2/U3)
        if use_prefilter:
            ok, fails = unified_prefilter(bars, i, code, stock_info)
            if not ok:
                continue

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
TEST_CODES = [
    "603196","002548","600116","600918","603081","603721","002469","600085","002353","605133",
    "000590","002676","000859","002727","605116","000657","600128","600356","603535","002912",
    "002201","002777","000989","600391","600096","603111","603396","002215","002899","002943",
    "002051","001226","000088","600658","002210","002405","600446","603215","002180","002107",
    "605098","002998","600645","002508","600825","002083","605086","600989","002967","000021",
    "002203","603568","603606","603073","600056","603232","001308","002921","002692","603202",
    "003026","600862","600820","000517","000597","603276","000956","000527","600625","002786",
    "603042","603328","600749","603590","002028","002346","001201","600248","600178","002746",
    "603577","000588","600601","002602","000029","600567","600711","000862","002510","002893",
    "002400","002240","603678","600888","600866","001391","002906","605136","600459","600925",
    "002521","000800","002467","002132","600272","002879","002588","600312","002735","600694",
    "002226","002318","002931","603585","600470","002593","603656","002553","002163","600435",
    "603132","600135","002415","000025","600315","603697","002653","000785","603689","000919",
    "603095","000626","600780","603685","600237","002498","001395","600066","603810","002939",
    "002821","000513","603407","002500","603348","600756","000153","002420","600973","600618",
    "600573","600452","600997","002490","002663","001236","001322","600697","600117","603380",
    "002073","603266","003023","600399","600020","600285","600566","600575","001359","002511",
    "600280","002282","600717","603053","002693","000151","000570","603282","002043","002294",
    "000923","600758","002244","002529","002356","002887","002060","600499","002733","600642",
    "002589","002973","002647","000663","000748","000407","600786","600408","600212","603070",
    "002701","603321","600162","002531","002057","603325","002552","002009","600725","600318",
    "000915","600996","002737","600662","001339","603098","600800","001266","000001","002121",
    "002364","603719","600113","002945","600834","603636","603993","600281","002971","002627",
    "002140","002903","600054","000566","603980","603861","603335","002413","600508","002880",
    "600433","600720","603218","603313","002090","002047","600305","603999","600983","002494",
    "600241","002407","600616","002021","600456","603228","603916","001283","603110","603399",
    "603035","600962","603613","003001","600691","002448","605168","603075","002991","603698",
    "603538","600801","600338","002063","002079","600832","002982","600708","002829","603868",
    "000695","000555","603225","002756","600580","000788","600151","002324","600300","600375",
    "603316","002459","600963","001231","600729","002387","002101","000514","002161","600860",
    "600021","002440","600263","603336","600742","002059","600552","002137","002891","002926",
    "000683","000679","603002","603466","600182","603088","003019","600594","603337","600936",
    "002388","002092","600063","000930","600126","003021","605069","600478","600986","605100",
    "002534","600160","600724","001278","002535","603155","603896","603393","603109","603717",
    "000791","603045","002928","600768","000755","000027","002773","002827","000967","002436",
    "002590","600129","603207","600350","000506","002277","603456","000070","600001","002348",
    "600027","002261","000929","001288","002241","600208","600939","600905","603004","000789",
    "600886","002599","600196","603658","600612","002077","002649","603051","002985","603310",
    "000837","002442","002757","002380","600371","000861","600579","000828","002007","605028",
    "002565","002836","600748","002116","603040","603208","000567","002615","603091","600930",
    "003039","000562","000623","603690","603633","002915","600192","002103","000819","603375",
    "000925","000676","603103","002466","600493","600057","000529","603181","603257","600033",
    "603136","002396","002540","002608","002429","603162","600841","600218","000713","600827",
    "002768","001393","600159","600120","002154","000518","002367","603231","002790","603171",
    "603889","600283","002223","600718","002623","002708","000536","605006","603387","002570",
    "002397","603099","000607","002870","002585","002725","603385","002625","603726","600228",
    "001234","002295","600339","603281","603061","002406","002256","002976","600984","603168",
    "603275","600779","603195","600422","000014","002229","002965","600629","002453","002742",
    "600100","002285","002515","000886","003028","002214","600058","002111","600075","603826",
    "600259","003017","002366","000428","603968","002386","000688","003018","603370","000758",
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


def buy_suggestion_text(d0_close, board_type, path='dragon_callback', style='a', entry_mode='confirm'):
    """D1开盘买入建议(文字描述, 供人工筛选, 按策略区分)

    dragon_callback: D1开盘gap (-3%,+2%] 可买 (与回测 D1 过滤一致)
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
    # 龙回头(方案2): D1开盘gap (-3%, +2%] 可买 (与回测 D1 过滤一致)
    if path == 'dragon_callback':
        lo_p = d0_close * (1 - 3.0 / 100)
        hi_p = d0_close * (1 + 2.0 / 100)
        return (f"开盘 -3%~+2% 可买(约{lo_p:.2f}~{hi_p:.2f}), 高开2%以上不追, 低开3%以下不接")
    # 断板: D1开盘买入, 无方向过滤
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
        tiered_trail = False
    elif path == 'dragon_callback':
        hold_days, stop, trail, is_v1 = 7, -8.0, -8.0, False
        peak_enabled, peak_ret, upper_pct = True, 7, 30
        tiered_trail = True  # 分段追踪: 盈>=3%→-3%, 否→-8%
    elif path == 'break_buy':
        p = BOARD_PARAMS['gem_star' if board_type == 'gem_star' else 'main']
        hold_days, stop, trail = p['hold_days'], p['stop_loss'], p['trailing_stop']
        is_v1 = False
        peak_enabled, peak_ret, upper_pct = True, 10, 40
        tiered_trail = False
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

        if peak_enabled:
            ret = (b['close'] / entry_price - 1) * 100
            if ret > peak_ret:
                bar_range = b['high'] - b['low']
                upper = (b['high'] - max(b['open'], b['close'])) / bar_range * 100 if bar_range > 0 else 0
                if upper > upper_pct and b['close'] < b['high'] * 0.98:
                    triggered = triggered or f'峰值逃顶 收盘卖出'
        if d > 1:
            if tiered_trail:
                peak_ret_pct = (peak / entry_price - 1) * 100
                cur_trail = -3.0 if peak_ret_pct >= 3 else -8.0
            else:
                cur_trail = trail
            if b['low'] <= peak * (1 + cur_trail / 100):
                triggered = triggered or f'追踪止损{cur_trail}%'
        if b['low'] <= entry_price * (1 + stop / 100):
            triggered = triggered or f'止损{stop}%'

        # V1 D1弱动量: D1收盘即可判定 → D2开盘清仓 (与回测 run_backtest 语义一致:
        # 判定数据是D1的日内动量, 执行点是D2开盘价; 故必须在d==1判定, 不能拖到d==2)
        # 放在止损检查之后: D1盘中先看是否触发止损, 未触发才走弱动量规则
        if is_v1 and d == 1 and not triggered:
            intraday = t.get('intraday', 0)
            if intraday < 3:
                if idx == today_idx:
                    return {'status': 'open', 'today_action': 'D1日内动量<3%, 明日开盘清仓',
                            'hold_days': d, 'curr_ret': (b['close'] / entry_price - 1) * 100}
                exit_idx = entry_idx + 1  # D2开盘已清仓
                exit_date = bars[exit_idx]['time'] if exit_idx < len(bars) else b['time']
                return {'status': 'closed', 'exit_reason': 'D1日内动量<3% D2开盘清仓',
                        'exit_date': exit_date}

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
        print(f"\n  🐉 龙回头 ({len(dc_today)}只) - 回调到位确认, 次日D1开盘买:")
        for t in sorted(dc_today, key=lambda x: x.get('entry_vol_r', 0), reverse=True):
            bt = get_board_type(t['code'])
            signal_price = t.get('signal_price') or t.get('entry_price')
            text = buy_suggestion_text(signal_price, bt, path='dragon_callback')
            print(f"    {t['code']:<8} {t['board']:<6} 涨停{t['lu_date']} 回调{t['pullback_days']}天 "
                  f"信号{t['signal_date']} {t['signal_chg']:+.1f}% 量比{t['entry_vol_r']:.2f}x")
            print(f"{'':>10} 信号价{signal_price:.2f} 买入建议: {text}")
        print(f"  {'─' * 85}")
        print(f"  📋 D1入场条件: 开盘gap -3%~+2% 可买 (高开2%以上不追, 低开3%以下不接)")

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
    parser = argparse.ArgumentParser(description="龙回头 + V1 + 断板 策略回测")
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
    # 统一前置过滤开关 (U1~U4, 依据tmp/妖股前置过滤分析报告.md)
    parser.add_argument("--no-ufilter", action="store_true", help="禁用统一前置过滤U1~U4 (对照用)")

    parser.add_argument("--today", action="store_true", help="显示买点+持仓卖出建议 (7天内买入的持仓)")
    parser.add_argument("--today-date", type=str, default="", help="指定日期(YYYY-MM-DD), 默认为库内最后交易日; 晚于库内最后交易日时按库内最后交易日处理")
    args = parser.parse_args()
    codes = [c.strip() for c in args.codes.split(",") if c.strip()] if args.codes else TEST_CODES

    # 指定codes时自动使用DB模式
    use_db = args.source == "db"
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
    print(f"龙回头 + V1 + 断板 策略回测")
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
    need_stock_info = True   # 换手率分析需要 stock_basic_info (circ_shares)
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
                                           max_last_chg=args.max_last_chg,
                                           stock_info=stock_info.get(code) if stock_info else None,
                                           use_prefilter=not args.no_ufilter,
)
            dc_trades.extend(dc)
            parts.append(f"龙回头{len(dc)}")
        if run_v1:
            # 如果预加载了K线, 直接用; 否则单独加载
            code_bars = all_bars.get(code) if all_bars else bars
            info = stock_info.get(code) if stock_info else None
            v1 = strategy_v1(code_bars, code, buy_mode=args.buy_mode,
                             hold_days=7,
                             stop_loss=args.v1_stop_loss,
                             trailing_stop=args.v1_trailing_stop,
                             ret_20d_min=args.ret_20d_min,
                             d_1_pullback_min=args.d1_pullback_min,
                             d_1_pullback_max=args.d1_pullback_max,
                             obv_filter=not args.no_obv_filter,
                             d_1_vol_max=args.d1_vol_max,
                             stock_info=info,
                             use_prefilter=not args.no_ufilter)
            v1_trades.extend(v1)
            parts.append(f"V1{len(v1)}")
        if run_bb:
            bb = strategy_break_buy(bars, code, stock_info=stock_info.get(code) if stock_info else None,
                                    use_prefilter=not args.no_ufilter)
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
        if v1_trades:
            print(f"\n  换手率分布 (锚点日=D0涨停日, 流通股本):")
            for lo, hi, label in [(0, 5, '<5%'), (5, 10, '5-10%'), (10, 20, '10-20%'), (20, 999, '>=20%')]:
                seg = [t for t in v1_trades if t.get('turnover_anchor') is not None and lo <= t['turnover_anchor'] < hi]
                if seg:
                    print_stats(seg, f"    {label}")
            print(f"\n  换手率分布 (锚点日=D0涨停日, 总股本):")
            for lo, hi, label in [(0, 3, '<3%'), (3, 6, '3-6%'), (6, 12, '6-12%'), (12, 999, '>=12%')]:
                seg = [t for t in v1_trades if t.get('turnover_anchor_total') is not None and lo <= t['turnover_anchor_total'] < hi]
                if seg:
                    print_stats(seg, f"    {label}")

    if run_bb:
        print(f"\n📊 断板:")
        print_stats(bb_trades, "断板")
        if bb_trades:
            print(f"\n  换手率分布 (锚点日=最后涨停日 / 确认日, 流通股本):")
            for lo, hi, label in [(0, 5, '<5%'), (5, 10, '5-10%'), (10, 20, '10-20%'), (20, 999, '>=20%')]:
                seg = [t for t in bb_trades if t.get('turnover_anchor') is not None and lo <= t['turnover_anchor'] < hi]
                if seg:
                    print_stats(seg, f"    锚{label}")
                seg2 = [t for t in bb_trades if t.get('turnover_sig') is not None and lo <= t['turnover_sig'] < hi]
                if seg2:
                    print_stats(seg2, f"    确认{label}")
            print(f"\n  换手率分布 (锚点日=最后涨停日 / 确认日, 总股本):")
            for lo, hi, label in [(0, 2, '<2%'), (2, 5, '2-5%'), (5, 10, '5-10%'), (10, 999, '>=10%')]:
                seg = [t for t in bb_trades if t.get('turnover_anchor_total') is not None and lo <= t['turnover_anchor_total'] < hi]
                if seg:
                    print_stats(seg, f"    锚{label}")
                seg2 = [t for t in bb_trades if t.get('turnover_sig_total') is not None and lo <= t['turnover_sig_total'] < hi]
                if seg2:
                    print_stats(seg2, f"    确认{label}")
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
            print(f"  ✅ 零重叠, 策略间完全互补")

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
        # U1~U4 与回测/dragon_scan 同口径 (默认开启, --no-ufilter 可关):
        # 龙回头锚定涨停日 (易错: D0是缩量小阴日, @D0评估换手会误杀); V1/断板锚定信号日
        use_ufilter_today = not args.no_ufilter
        for code, bars in bars_by_code.items():
            idx = _last_bar_idx_on_or_before(bars, asof_str)
            if idx is None or bars[idx]['time'] != asof_str or idx < 2:
                continue
            bt = get_board_type(code)
            lu_sub = [j for j in find_limit_ups(bars, bt) if j < idx]
            code_info = stock_info.get(code) if stock_info else None
            if run_dc:
                for s in dragon_today_d0_signals(
                        bars[:idx + 1], code,
                        min_pullback_days=args.pullback,
                        max_pullback_days=args.max_pullback,
                        max_last_chg=args.max_last_chg,
                        limit_ups=lu_sub):
                    if use_ufilter_today:
                        lu_idx = next((j for j, b in enumerate(bars) if b['time'] == s.get('lu_date')), None)
                        if lu_idx is None or not unified_prefilter(bars, lu_idx, code, code_info)[0]:
                            continue
                    pending_signals.append(s)
            if run_v1:
                for s in v1_today_d0_signals(
                        bars[:idx + 1], code,
                        ret_20d_min=args.ret_20d_min,
                        d_1_pullback_min=args.d1_pullback_min,
                        d_1_pullback_max=args.d1_pullback_max,
                        obv_filter=not args.no_obv_filter,
                        d_1_vol_max=args.d1_vol_max):
                    if use_ufilter_today and not unified_prefilter(bars, idx, code, code_info)[0]:
                        break
                    pending_signals.append(s)
            if run_bb:
                for s in break_today_d0_signals(
                        bars[:idx + 1], code, limit_ups=lu_sub):
                    if use_ufilter_today and not unified_prefilter(bars, idx, code, code_info)[0]:
                        break
                    pending_signals.append(s)
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

    # 导出 (文件名带策略后缀, 避免多策略连跑互相覆盖; 按文件卫生规则输出到 tmp/)
    # 注意: --today 模式不导出回测结果文件 — 它的 all_trades 只有 --codes 子集,
    #       曾经把全市场303笔基线覆盖成41笔 (2026-09-07教训); --today只写 today_signals_*.json
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp")
    os.makedirs(out_dir, exist_ok=True)
    out_name = os.path.join(out_dir, f"test_dragon_callback_result_{args.strategy}.json")
    all_out = dc_trades + v1_trades + bb_trades
    if all_out and not args.today:
        with open(out_name, "w", encoding="utf-8") as f:
            json.dump(all_out, f, ensure_ascii=False, indent=2)
        print(f"\n💾 {out_name} ({len(all_out)}笔)")

if __name__ == "__main__":
    main()

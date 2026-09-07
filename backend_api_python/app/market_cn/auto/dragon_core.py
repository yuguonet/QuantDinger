#!/usr/bin/env python3
"""龙回头 + V1/断板 核心判定逻辑 —— 单一事实源 (single source of truth)

2026-09-06: 龙回头Pro(dragon2, 八因子评分版)已下线移除 (信号太多无法人工复核),
龙回头规则替换为 test_dragon.py 同步的"方案2"新规则 (规则细节见 DRAGON_CB_PARAMS)。
历史规则与验证证据: 龙回头优化分析_20260906/ (滑动窗口找龙 + 拐点或关系过滤)。

使用方:
  - test_dragon.py (--strategy dragon)   : 回测 (dragon_cb_today_d0_signals /
                                           run_backtest_dragon_callback / unified_prefilter)
  - app/market_cn/auto/dragon_scan.py    : 盘后全市场扫描 (16:30)
  - app/market_cn/auto/dragon_monitor.py : 盘中状态机 (60s)

本模块保持零 IO / 零 print, 只做纯判定。
修改任何规则后必须重跑回测对数 (方案2基线见 龙回头优化分析_20260906/ 分析日志)。

易错点:
  - volume 单位是股, 换手率 = volume/circ_shares*100; 市值用信号日收盘价
  - as-of 安全: 所有判定只用<=当日收盘数据, 回测与 --today 报告共用同一判定函数
"""
from __future__ import annotations

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
    """找到所有涨停日索引。"""
    result = []
    for i in range(1, len(bars)):
        if is_limit_up(bars[i]['close'], bars[i-1]['close'], board_type):
            result.append(i)
    return result


# ================================================================
# 技术指标辅助 (与 test_dragon.py 同名函数逐字一致; 仅 tech_score 参考
# 输出与 RSI 质量排除使用, 不做评分门槛)
# ================================================================

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

def calc_roc(closes, period=10):
    """计算变动率 ROC = (close[i]-close[i-period])/close[i-period]*100"""
    if len(closes) < period + 1:
        return None
    ref = closes[-1 - period]
    if ref <= 0:
        return None
    return (closes[-1] - ref) / ref * 100

def calc_psy(closes, period=12):
    """计算心理线 PSY = 过去period天中上涨天数/period*100"""
    if len(closes) < period + 1:
        return None
    up_days = 0
    for i in range(-period, 0):
        if closes[i] > closes[i-1]:
            up_days += 1
    return up_days / period * 100

def is_macd_golden_cross(dif, dea, lookback=3):
    """判断MACD是否在最近lookback根K线内发生金叉 (DIF上穿DEA)"""
    if dif is None or dea is None or len(dif) < lookback + 1:
        return False
    n = len(dif)
    if dif[n-1] < dea[n-1]:
        return False  # 当前DIF在DEA下方
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
    for i in range(max(0, n - lookback - 1), n - 1):
        if hist[i] < 0:
            return True
    return False

def is_macd_hist_shrinking_negative(hist, lookback=5):
    """判断MACD绿柱是否在缩短 (负柱绝对值在减小)"""
    if hist is None or len(hist) < lookback:
        return False
    n = len(hist)
    recent = hist[n - lookback:]
    if any(h >= 0 for h in recent):
        return False
    abs_vals = [abs(h) for h in recent]
    return abs_vals[-1] < abs_vals[-2] < abs_vals[-3] if len(abs_vals) >= 3 else abs_vals[-1] < abs_vals[-2]


# ================================================================
# 龙回头 (dragon_callback, "方案2") —— 2026-09-06 与 test_dragon.py 同步
# ================================================================
# 规则框架: 找龙(滑动窗口涨停占比>=70%) → 回调 gap[5,25] → 拐点OR → 信号质量排除
#           → U1~U4 统一预过滤(@涨停日) → D1开盘gap过滤 → 次日开盘买
# 依据 (龙回头优化分析_20260906/ 分析日志):
#   - 找龙不需要精确识别"龙", 滑动窗口涨停占比>=70%足够 (覆盖连板+断板两种形态)
#   - 回调到位比识别龙更重要: MA20支撑位 / 深跌释放 / 买盘承接 是真正拐点信号
#   - "中间地带"(温和回调+阴线偏多)是亏损重灾区, 用质量排除兜底
#   - 止损-5%太紧截断收益 → 放宽到-8%; 分段追踪止损锁利润

DRAGON_CB_PARAMS = dict(
    # --- 找龙: 滑动窗口涨停占比 ---
    dragon_ratio=0.7,               # 窗口内涨停占比阈值
    dragon_windows=[4, 5, 7, 10, 15, 20],  # 候选窗口(天), 任一窗口达标即为"龙"
    # --- 回调窗口 ---
    gap_min=5, gap_max=25,          # 信号日距涨停日天数区间 [5,25]
    # --- 拐点过滤 (或关系, 满足任一即可) ---
    ma20_lo=-10.0, ma20_hi=-5.0,    # 拐点1: D0收盘/MA20-1 ∈ [-10%,-5%) (均线支撑)
    depth_max=-30.0,                # 拐点2: 回调期最低价/涨停收盘-1 <= -30% (深跌释放)
    yin_ratio_max=0.5,              # 拐点3: 回调期阴线比例<50% (买盘承接)
    # --- 信号质量排除 (全部为"或关系"通过后的兜底剔除) ---
    yin_ratio_exclude=0.6,          # 阴线比例>=60% 剔除 (卖压未尽, 持仓到期概率高)
    rsi6_exclude_lt=30.0,           # RSI(6)<30 剔除 (超卖≠反弹, 胜率仅~17%)
    d0_ma20_exclude_lt=-8.0,        # D0距MA20<-8% 剔除 (深度破位)
    # --- 入场 ---
    d1_gap_lo=-3.0, d1_gap_hi=2.0,  # D1开盘gap可买区间: 高开>2%不追(追高亏损率高), 低开<-3%不接(破位风险)
    # --- 出场 (run_backtest_dragon_callback) ---
    hold_days=7,                    # 持仓上限(交易日)
    stop_loss=-8.0,                 # 固定止损
    trail_lo=-8.0,                  # 分段追踪: 盈利<3%时-8% (给空间)
    trail_hi=-3.0,                  #           盈利>=3%时-3% (锁利润)
    trail_switch_pct=3.0,           # 分段切换阈值(峰值收益%)
    peak_exit_ret=7.0,              # 峰值逃顶: 涨>7%后
    peak_exit_upper=30.0,           #           上影线>30% 且收盘<高点98% → 收盘逃顶
)


def dragon_cb_today_d0_signals(bars, code, min_pullback_days=3, max_pullback_days=11,
                               max_last_chg=3.0, today_str=None, limit_ups=None,
                               use_tech_score=True, params=None):
    """龙回头 今日(D0)入场信号 ("方案2"): 只检查D0是否满足信号日, 不依赖D+1数据。

    参数 min_pullback_days/max_pullback_days/max_last_chg 为旧版兼容保留, 已不参与判定。
    params: 可选, 覆盖 DRAGON_CB_PARAMS 中的键 (dragon_scan 用)。
    返回空list或单元素list, 元素含 lu_date/gap_from_peak/d0_vs_ma20/pullback_depth/
    yin_ratio/tech_score 等 (均只用<=D0收盘可知数据)。
    """
    result = []
    p = {**DRAGON_CB_PARAMS, **(params or {})}
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
    # D0涨跌幅和量比 (保留用于输出, 不作为过滤条件)
    last_chg = (d0['close'] / prev_c - 1) * 100
    prev_vol = bars[i - 1]['volume']
    entry_vol_r = d0['volume'] / prev_vol if prev_vol > 0 else 0

    closes = [bars[j]['close'] for j in range(i + 1)]

    # ── tech_score 加分制 (仅参考输出; RSI 值供质量排除使用) ──
    score = 0
    rsi_val = roc = psy = None
    if use_tech_score:
        # MACD (最高+4, 最低-2)
        dif, dea, hist = calc_macd(closes)
        if hist is not None and len(hist) >= 2:
            if is_macd_golden_cross(dif, dea, lookback=5):
                score += 3   # 金叉: 强烈看多
            elif is_macd_hist_turning_positive(hist, lookback=5):
                score += 2   # 绿翻红: 趋势转多
            elif is_macd_hist_shrinking_negative(hist, lookback=5):
                score += 1   # 绿柱缩短: 空头衰竭
            n_h = len(hist)
            if n_h >= 2 and abs(dif[n_h-1]) < abs(dea[n_h-1]) * 0.5:
                score += 1   # 零轴附近
            if dif[n_h-1] < dea[n_h-1] and dif[n_h-2] >= dea[n_h-2]:
                score -= 2   # 死叉: 趋势转空
        # RSI(6)
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
        # ROC(5)
        roc = calc_roc(closes, period=5)
        if roc is not None:
            if -10 <= roc < 0 or 0 <= roc < 5:
                score += 1
            elif roc < -15 or roc >= 5:
                score -= 1
        # PSY(10)
        psy = calc_psy(closes, period=10)
        if psy is not None:
            if psy < 30:
                score += 2
            elif psy < 40:
                score += 1
            elif psy >= 50:
                score -= 1
        # 评分门槛已关闭 (实验结论: tech_score 无判别力, 仅输出参考)

    # ── 方案2 主判定 ──
    for lu_idx in (limit_ups if limit_ups is not None else find_limit_ups(bars[:i], board_type)):
        lu_close = bars[lu_idx]['close']
        if lu_close <= 0:
            continue

        # 当前日(i)收盘必须仍低于涨停收盘 (仍在回调中)
        if bars[i]['close'] >= lu_close:
            continue

        pullback_days = i - lu_idx

        # ── Step1: 找龙 — 滑动窗口内涨停占比>=70% ──
        dragon_found = False
        for window in p['dragon_windows']:
            start = max(1, lu_idx - window)
            total_days = lu_idx - start
            if total_days < 3:
                continue
            lu_count = sum(1 for k in range(start, lu_idx)
                           if k > 0 and is_limit_up(bars[k]['close'], bars[k-1]['close'], board_type))
            if lu_count / total_days >= p['dragon_ratio']:
                dragon_found = True
                break
        if not dragon_found:
            continue

        # ── Step2: gap [gap_min, gap_max] ──
        gap_from_peak = i - lu_idx
        if gap_from_peak < p['gap_min'] or gap_from_peak > p['gap_max']:
            continue

        # ── 回调期特征 ──
        if i >= 19:
            ma20 = sum(bars[j]['close'] for j in range(i - 19, i + 1)) / 20
            d0_vs_ma20 = (d0['close'] / ma20 - 1) * 100 if ma20 > 0 else None
        else:
            d0_vs_ma20 = None

        min_low = min(bars[j]['low'] for j in range(lu_idx + 1, i + 1))
        pullback_depth = (min_low / lu_close - 1) * 100

        pb_yin = sum(1 for j in range(lu_idx + 1, i + 1) if bars[j]['close'] < bars[j]['open'])
        pb_total = i - lu_idx
        yin_ratio = pb_yin / pb_total if pb_total > 0 else 1.0

        # ── 拐点过滤 (或关系, 满足任一即可) ──
        cond_ma20 = d0_vs_ma20 is not None and p['ma20_lo'] <= d0_vs_ma20 < p['ma20_hi']
        cond_depth = pullback_depth <= p['depth_max']
        cond_yin = yin_ratio < p['yin_ratio_max']
        if not (cond_ma20 or cond_depth or cond_yin):
            continue

        # ── 信号质量排除 ──
        if yin_ratio >= p['yin_ratio_exclude']:
            continue  # 阴线比例>=60%: 卖压未尽, 持仓到期概率高
        if rsi_val is not None and rsi_val < p['rsi6_exclude_lt']:
            continue  # RSI<30: 超卖≠反弹, 胜率仅~17%
        if d0_vs_ma20 is not None and d0_vs_ma20 < p['d0_ma20_exclude_lt']:
            continue  # D0距MA20超-8%: 深度破位, 持仓到期概率高

        result.append({
            'code': code, 'board': get_board_name(code),
            'path': 'dragon_callback', 'path_label': '龙回头',
            'lu_date': bars[lu_idx]['time'],
            'pullback_days': pullback_days,
            'signal_date': bars[i]['time'],
            'signal_chg': round(last_chg, 2),
            'signal_vol_r': round(entry_vol_r, 2),
            'signal_price': round(d0['close'], 3),
            'entry_vol_r': round(entry_vol_r, 2),
            'buy_mode': 'next_open',
            'gap_from_peak': gap_from_peak,
            'd0_vs_ma20': round(d0_vs_ma20, 2) if d0_vs_ma20 is not None else None,
            'pullback_depth': round(pullback_depth, 2),
            'yin_ratio': round(yin_ratio, 2),
            'tech_score': score,
            'tech_rsi': round(rsi_val, 1) if rsi_val else None,
            'tech_roc': round(roc, 1) if roc else None,
            'tech_psy': round(psy, 1) if psy else None,
        })
        break
    return result


def run_backtest_dragon_callback(bars, entry_idx, entry_price, hold_days=None,
                                 stop_loss=None, board_type="main", stop_at_idx=None):
    """龙回头出场模拟: 分段追踪止损 (as-of安全, 供回测与盘中持仓重放共用)

    出场判定顺序 (每日):
      1) 峰值逃顶: 涨>7%后大上影线(>30%)且收盘<高点98% → 收盘逃顶
      2) 分段追踪止损: 峰值收益>=3% → -3% (锁利润); 否则 -8% (给空间)
      3) 固定止损: -8%
      4) 兜底: 持仓到期收盘卖 / stop_at_idx 截断返回 open=True
    stop_at_idx: 只模拟到该bar索引(盘中重放用); 未触发出场 → open=True
    """
    p = DRAGON_CB_PARAMS
    hold_days = p['hold_days'] if hold_days is None else hold_days
    stop_loss = p['stop_loss'] if stop_loss is None else stop_loss
    if entry_price <= 0 or entry_idx >= len(bars):
        return None
    n = len(bars)
    peak = entry_price
    exit_p, exit_d, exit_reason = entry_price, 0, ''
    capped = False

    for d in range(1, hold_days + 1):
        idx = entry_idx + d - 1
        if idx >= n:
            break
        if stop_at_idx is not None and idx > stop_at_idx:
            capped = True
            break
        b = bars[idx]
        if b['high'] > peak:
            peak = b['high']

        # 1. 峰值逃顶
        ret = (b['close'] / entry_price - 1) * 100
        if ret > p['peak_exit_ret']:
            rng = b['high'] - b['low']
            upper = (b['high'] - max(b['open'], b['close'])) / rng * 100 if rng > 0 else 0
            if upper > p['peak_exit_upper'] and b['close'] < b['high'] * 0.98:
                exit_p, exit_d, exit_reason = b['close'], d, '峰值逃顶'
                break

        # 2. 分段追踪止损
        if d > 1:
            peak_ret = (peak / entry_price - 1) * 100
            trail = p['trail_hi'] if peak_ret >= p['trail_switch_pct'] else p['trail_lo']
            if b['low'] <= peak * (1 + trail / 100):
                exit_p = peak * (1 + trail / 100)
                exit_d = d
                exit_reason = f'追踪止损{trail}%'
                break

        # 3. 固定止损
        if b['low'] <= entry_price * (1 + stop_loss / 100):
            exit_p = entry_price * (1 + stop_loss / 100)
            exit_d = d
            exit_reason = f'止损{stop_loss}%'
            break

        exit_p, exit_d = b['close'], d

    if exit_reason == '' and not capped:
        exit_reason = '持仓到期'
    return {
        'exit_price': round(exit_p, 3), 'exit_day': exit_d,
        'exit_reason': exit_reason,
        'return_pct': round((exit_p / entry_price - 1) * 100, 2),
        'peak_return_pct': round((peak / entry_price - 1) * 100, 2),
        'open': bool(capped),
    }


# ================================================================
# 统一前置过滤 U1~U4 (2026-09-04, 与 test_dragon.py 共用)
# 依据: tmp/妖股前置过滤分析报告.md (18564涨停事件) + tmp/三策略入口过滤改进报告.md
# 关键设计: 全部条件只用判定日收盘可知数据, 无未来函数。
# 易错点: 龙回头/断板的 U2/U3 必须锚定涨停日评估, 不能用缩量信号日
#         (D0是缩量小阴日, 换手天然低, @D0评估会误杀)。
# ================================================================
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


# ================================================================
# V1 / 断板 判定与出场 (2026-09-04 提取, 与 test_dragon.py 共用)
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


def v1_today_d0_signals(bars, code, ret_20d_min=30.0,
                        d_1_pullback_min=-10.0, d_1_pullback_max=-3.0,
                        obv_filter=True, d_1_vol_max=1.5, today_str=None,
                        stock_info=None):
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

    # === 因子5: 纯单板过热过滤 (仅当前10天无涨停时生效) ===
    # 纯单板(前10天无涨停)信号胜率偏低(67.4% vs 有近涨停79.8%),
    # 通过MACD柱<2+布林带宽<45%剔除过热信号, 可将纯单板胜率提升至76.2%。
    # 有近涨停的信号不受影响。
    has_recent_lu = False
    for j in range(max(1, i - 10), i):
        if j >= 1 and is_limit_up(bars[j]['close'], bars[j-1]['close'], board_type):
            has_recent_lu = True
            break
    if not has_recent_lu:
        closes = [bars[j]['close'] for j in range(i + 1)]
        _, _, hist = calc_macd(closes)
        macd_h = hist[-1] if hist else None
        boll_bw = calc_bollinger_bw(closes)
        if macd_h is not None and macd_h >= 2:
            return result
        if boll_bw is not None and boll_bw >= 45:
            return result

    circ = float((stock_info or {}).get('circ_shares') or 0)
    total = float((stock_info or {}).get('total_shares') or 0)
    result.append({
        'code': code, 'board': get_board_name(code),
        'path': 'v1', 'path_label': 'V1',
        'd0_date': d0['time'],
        'd0_close': round(d0['close'], 3),
        'ret_20d': round(ret_20d, 2),
        'd_1_change': round(d_1_change, 2),
        'turnover_anchor': round(d0['volume'] / circ * 100, 2) if circ > 0 else None,
        'turnover_anchor_total': round(d0['volume'] / total * 100, 2) if total > 0 else None,
        'buy_mode': 'next_open',
    })
    return result

# ================================================================
# 断板买入策略
# ================================================================


def break_today_d0_signals(bars, code, min_streak=2, max_break_gap=5, today_str=None,
                           limit_ups=None, stock_info=None):
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
        circ = float((stock_info or {}).get('circ_shares') or 0)
        total = float((stock_info or {}).get('total_shares') or 0)
        turnover_anchor = round(float(bars[streak_end]['volume']) / circ * 100, 2) if circ > 0 else None
        turnover_confirm = round(float(bars[i]['volume']) / circ * 100, 2) if circ > 0 else None
        turnover_anchor_t = round(float(bars[streak_end]['volume']) / total * 100, 2) if total > 0 else None
        turnover_confirm_t = round(float(bars[i]['volume']) / total * 100, 2) if total > 0 else None
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
            'turnover_anchor': turnover_anchor,
            'turnover_sig': turnover_confirm,
            'turnover_anchor_total': turnover_anchor_t,
            'turnover_sig_total': turnover_confirm_t,
            'entry_price': None, 'buy_mode': 'next_open',
        })
        break  # 只取一个信号
    return result

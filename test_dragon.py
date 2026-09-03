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
  python test_dragon.py --source db --days 300 --strategy dragon2  # 龙回头Pro, 八因子评分版

参数:
  --source db       从数据库加载全市场 (默认 manual)
  --days N          向前取N个交易日 (默认300, 从当前日期往前推)
  --strategy        all|dragon|dragon2|v1|break (默认all)
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
│ 龙回头Pro (--strategy dragon2) - 八因子综合评分版                          │
├─────────────────────────────────────────────────────────────────────────────┤
│ 目标: 用经验特征组合提升龙回头胜率与峰值                                    │
│ (基线 300日全市场: 龙回头 1590笔 38.7%胜率 均收益-0.20% 均峰值+4.92%)        │
│                                                                             │
│ 锚点日: 涨停 或 单日大涨 (主板>=7% / 创科板>=12%)                           │
│ 回调: 连续收盘<锚点收盘 3~11天 + 最大回撤>=-15%(主板)/-20%(创科) 硬门槛     │
│                                                                             │
│ 八因子评分 (>=12分入场, 满分23):                                            │
│   1 换手率: 锚点换手>=10% +3 / >=5% +2 / >=3% +1 (硬门槛>=3%); 信号日+1     │
│   2 市值:   流通市值20~300亿 +3 / 10~500亿 +1 (出界剔除)                    │
│   3 锚点:   已含于形态(涨停/大涨)                                           │
│   4 回调:   已含于形态(3~11天)                                              │
│   5 均线:   MA60五日斜率>=0.3% +3 / >=0 +2 (>=-1%硬门槛);                   │
│             MA10上行+1 / MA20上行+1 / MA10>MA20 +1 / 多头排列 +1            │
│   6 支撑:   回调低点触MA10/MA20且守住 +2; 低点不破锚点开盘价 +2             │
│   7 量能:   锚点日量>=1.5x5日均量 +2; 回调均量<=锚点70% +1 / 无放量阴 +1    │
│   8 龙头:   名称含ST/退 直接剔除; 回调位置分 +1                             │
│                                                                             │
│ 入场 (默认确认制三段式, 回测证据: D1强确认 86.9%胜率/盈亏比4.98):           │
│   D0信号: (a)缩量企稳 小阴(-3%~-0.5%) 量比0.5~0.8x                          │
│           (b)放量启动 收阳涨>=2% 量比>=1.5x(比前日明显放量)                 │
│   D1确认: 强确认=涨>=3%且量>=1.5x信号日量 → 后日买;                         │
│           收阴或无力=弱确认 → 放弃; 中性默认放弃 (--d2-allow-ok放宽)        │
│   D2买入: 开盘涨幅-5%~+6%可买, 出界放弃 (追高/破位不入)                     │
│   (--d2-entry d1open 切旧口径: D1开盘直接买, 弱确认D2开盘清仓)              │
│                                                                             │
│ 出场:                                                                       │
│   强确认:   涨>=3%且量>=1.5x → 持仓延至10天, 给连板空间 (确认制=入场前提)   │
│   弱确认:   仅d1open模式: 收阴/无力 → D2开盘清仓 (确认制入场前已挡掉)       │
│   骑板:     持仓中连板>=2 → 涨停日豁免追踪止损, 断板日尾盘卖 (开板预期)     │
│   巨量出货: 量>=2.8x前日且收阴 / 量>=3.5x5日均量且滞涨 → 尾盘卖             │
│   止损:     -5%(主板) / -7%(创科板); 追踪止损同阈值 (涨停日豁免)            │
│   峰值逃顶: 涨>7%后大上影线(>30%)收盘逃顶; 持仓上限7天(强确认10天)          │
│                                                                             │
│ 参数: --d2-min-score / --d2-turnover / --d2-mcap-min/max / --no-d2-*        │
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
                          多策略独立运行 (--strategy all)
═══════════════════════════════════════════════════════════════════════════════

  龙回头 + 龙回头Pro + V1 + 断板 独立运行, 互不干扰, 各自产生信号
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

        # D+1开盘过滤: 高开跳水概率大, 过滤极端开盘
        d0_close = bars[i]['close']
        if d0_close > 0:
            d1_gap = (d1['open'] / d0_close - 1) * 100
            # 高开>2%不入场 (追涨亏损率高)
            if d1_gap > 2.0:
                continue
            # 低开<-3%不入场 (破位风险)
            if d1_gap < -3.0:
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

    # ═══════════════════════════════════════════════════════════════
    # 技术指标加分制 (AND逻辑: 总分必须>=阈值)
    # ═══════════════════════════════════════════════════════════════
    closes = [bars[j]['close'] for j in range(i + 1)]
    score = 0

    # --- MACD (最高+4, 最低-2) ---
    dif, dea, hist = calc_macd(closes)
    if hist is not None and len(hist) >= 2:
        if is_macd_golden_cross(dif, dea, lookback=5):
            score += 3   # 金叉: 强烈看多
        elif is_macd_hist_turning_positive(hist, lookback=5):
            score += 2   # 绿翻红: 趋势转多
        elif is_macd_hist_shrinking_negative(hist, lookback=5):
            score += 1   # 绿柱缩短: 空头衰竭
        # DIF在零轴附近
        n_h = len(hist)
        if n_h >= 2 and abs(dif[n_h-1]) < abs(dea[n_h-1]) * 0.5:
            score += 1   # 零轴附近: 趋势不强但也没崩
        # 死叉扣分
        if dif[n_h-1] < dea[n_h-1] and dif[n_h-2] >= dea[n_h-2]:
            score -= 2   # 死叉: 趋势转空

    # --- RSI(6) (最高+2, 最低-2) ---
    rsi_val = rsi(closes, period=6)
    if rsi_val is not None:
        if rsi_val < 30:
            score += 2   # 超卖: 反弹概率大
        elif rsi_val < 40:
            score += 1   # 偏低: 有空间
        elif rsi_val < 50:
            score += 0   # 中性
        elif rsi_val < 60:
            score -= 1   # 偏高: 空间有限
        else:
            score -= 2   # 超买: 回调风险

    # --- ROC(5) 动量 (最高+2, 最低-2) ---
    roc = calc_roc(closes, period=5)
    if roc is not None:
        if -10 <= roc < 0:
            score += 1   # 温和回调: 正常
        elif -15 <= roc < -10:
            score += 0   # 较深回调: 观望
        elif roc < -15:
            score -= 1   # 深度回调: 风险
        elif 0 <= roc < 5:
            score += 1   # 企稳回升: 好信号
        elif roc >= 5:
            score -= 1   # 已经涨了: 追高风险

    # --- PSY(10) 心理线 (最高+2, 最低-1) ---
    psy = calc_psy(closes, period=10)
    if psy is not None:
        if psy < 30:
            score += 2   # 极度悲观: 反弹概率大
        elif psy < 40:
            score += 1   # 偏悲观: 回调充分
        elif psy < 50:
            score += 0   # 中性
        else:
            score -= 1   # 偏乐观: 回调不充分

    # --- 总分阈值 ---
    MIN_SCORE = 2  # 至少2分才入场
    if score < MIN_SCORE:
        return result

    # 往前找涨停日
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
            'tech_score': score,
            'tech_rsi': round(rsi_val, 1) if rsi_val else None,
            'tech_roc': round(roc, 1) if roc else None,
            'tech_psy': round(psy, 1) if psy else None,
        })
        break
    return result


# ================================================================
# 龙回头Pro (dragon2) — 八因子综合评分版
# ================================================================
# 用户经验特征 → 量化规则映射:
#   特征1 换手率活跃(>5%)   → 锚点日/信号日换手率(成交量/流通股本)评分, 锚点换手>=3%硬门槛
#   特征2 市值20亿~300亿    → 流通市值(circ_shares×close)评分, 10亿~500亿硬门槛
#   特征3 最近涨停/大涨     → 锚点日: 涨停 或 单日涨幅>=7%(主板)/>=12%(创科板)
#   特征4 回调3-11天        → 锚点后连续收盘<锚点收盘 3~11天 (与旧龙回头一致)
#   特征5 MA60持平/向上     → MA60五日斜率>=-1%硬门槛, >=0评分; MA10/20上攻+多头排列评分
#   特征6 回调支撑明显      → 低点触及MA10/MA20且守住 / 不破锚点开盘价 评分; 最大回撤硬门槛
#   特征7 上攻明显放量      → 两种入场: (a)缩量小阴企稳次日买 (b)放量启动日次日买;
#                             D1强确认(涨>=3%且量>=1.5x)延长持仓骑连板, 弱确认D2开盘清仓
#   特征8 龙头少ST          → 股票名称含 ST/退 直接剔除
# 连板特点 → 出场: 涨停日豁免追踪止损(骑板); 连板>=2后断板日尾盘卖;
#                  巨量阴线/天量滞涨尾盘卖(出货一般放巨量); 峰值逃顶保留
# as-of安全: 所有判定只用<=当日收盘数据, 与--today报告共用同一判定函数
DRAGON2_PARAMS = dict(
    # --- 基础形态 ---
    min_pullback_days=3, max_pullback_days=11,
    last_chg_min_abs=0.5,       # (a)信号日小阴下限(绝对值)
    max_last_chg=3.0,           # (a)信号日小阴上限
    vol_r_lo=0.5, vol_r_hi=0.8, # (a)信号日量比区间(缩量)
    b_min_chg=2.0,              # (b)启动日最小涨幅
    b_min_vol_r=1.5,            # (b)启动日最小量比(比前一天明显放量)
    big_gain_main=7.0, big_gain_gem=12.0,   # 大涨锚点阈值
    use_big_gain_anchor=True,
    entry_gap_a=(-3.0, 2.0),    # (a)D1开盘涨幅可买区间(d1open模式)
    entry_gap_b=(-3.5, 5.5),    # (b)D1开盘涨幅可买区间(d1open模式)
    use_mode_a=True, use_mode_b=True,
    # --- 入场模式 ---
    entry_mode='confirm',       # 'confirm'=D1确认后D2开盘买(默认) | 'd1open'=D1开盘直接买(旧口径)
    allow_ok_confirm=False,     # confirm模式: D1中性确认是否也买 (False=只要强确认)
    entry_gap_confirm=(-5.0, 6.0),  # confirm模式: D2开盘涨幅可买区间
    # --- 评分门槛 ---
    min_score=12,
    turnover_hot=10.0, turnover_min=5.0, turnover_hard=3.0,
    mcap_lo=20.0, mcap_hi=300.0, mcap_hard_lo=10.0, mcap_hard_hi=500.0,  # 亿
    ma60_slope_hard=-1.0,
    drawdown_max_main=-15.0, drawdown_max_gem=-20.0,
    anchor_vol_hot=1.5, anchor_vol_min=1.2,
    # --- 出场 ---
    stop_main=-5.0, trail_main=-5.0, stop_gem=-7.0, trail_gem=-7.0,
    hold_days=7, hold_strong=10,
    d1_weak_chg=1.5, d1_weak_vol=1.5,       # D1弱确认: 收阴 或 (涨<1.5%且量<1.5x) → D2开盘清仓
    d1_strong_chg=3.0, d1_strong_vol=1.5,   # D1强确认: 涨>=3%且量>=1.5x → 骑连板模式
    ride_streak=2,              # 持仓中连板数达到2 → 骑板模式(断板尾盘卖)
    big_vol_prev_r=2.8,         # 巨量出货: 量/前一日量
    big_vol_ma5_r=3.5,          # 巨量出货: 量/5日均量 + 滞涨
    big_vol_exit=True,
)


def _sma(closes, period):
    """最近period根简单均线, 数据不足返回None"""
    if closes is None or len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def _sma_at(closes, end_offset, period):
    """截至 end_offset 天前的period均线 (end_offset=0 → 最近一根)"""
    if closes is None:
        return None
    j = len(closes) - end_offset
    if j < period:
        return None
    return sum(closes[j - period:j]) / period


def run_backtest_dragon2(bars, entry_idx, entry_price, board_type="main",
                         sig_close=0.0, sig_vol=0.0, entry_style="a",
                         params=None, stop_at_idx=None,
                         entry_mode=None, pre_d1_chg=None, pre_d1_vol_r=None,
                         pre_d1_confirm=None):
    """龙回头Pro出场模拟 (as-of安全, 供回测与--today持仓重算共用)

    每日出场判定顺序:
      d=1 收盘: D1确认评估 (仅d1open模式; confirm模式下入场前已完成确认, 跳过)
      d>=2:
        1) D1弱确认 → 开盘清仓 (仅d1open模式)
        2) 追踪止损 (当日涨停豁免 → 骑板; 创科板阈值更宽)
        3) 止损
        4) 收盘可知: 连板>=ride_streak后断板 → 尾盘卖
                      巨量出货(放量阴线/天量滞涨) → 尾盘卖
                      峰值逃顶(涨>7%大上影线) → 尾盘卖
        5) 兕底: 持仓到期收盘卖 / 数据截断返回open=True
    stop_at_idx: 只模拟到该bar索引(--today用); 未触发出场 → open=True
    entry_mode='confirm': 入场前已完成D1确认, pre_d1_*传入确认结果,
                          强确认仍延长持仓(hold_strong), 弱确认不会出现(入场前已过滤)
    """
    p = params or DRAGON2_PARAMS
    if entry_price <= 0 or entry_idx >= len(bars):
        return None
    is_gem = board_type == "gem_star"
    stop = p['stop_gem'] if is_gem else p['stop_main']
    trail = p['trail_gem'] if is_gem else p['trail_main']
    hold = p['hold_days']
    peak = entry_price
    exit_p, exit_d, exit_reason = entry_price, 0, ''
    d1_chg = None
    d1_vol_r = None
    d1_confirm = None
    weak_exit = False
    strong = False
    streak = 0
    max_streak = 0
    prev_close = sig_close if sig_close > 0 else entry_price
    vol_hist = []
    capped = False
    mode = entry_mode or p.get('entry_mode', 'confirm')

    d = 1
    while d <= hold:
        idx = entry_idx + d - 1
        if idx >= len(bars):
            break
        if stop_at_idx is not None and idx > stop_at_idx:
            capped = True
            break
        b = bars[idx]
        if b['high'] > peak:
            peak = b['high']
        pc = bars[idx - 1]['close'] if idx > 0 else b['open']
        day_chg = (b['close'] / pc - 1) * 100 if pc > 0 else 0
        is_lu = is_limit_up(b['close'], pc, board_type)
        vol_hist.append(b['volume'])

        # --- d=1 收盘: D1确认 (仅d1open模式) ---
        if d == 1 and mode != 'confirm':
            d1_chg = (b['close'] / prev_close - 1) * 100 if prev_close > 0 else 0
            d1_vol_r = (b['volume'] / sig_vol) if sig_vol > 0 else None
            vr = d1_vol_r if d1_vol_r is not None else 9.9
            if d1_chg >= p['d1_strong_chg'] and vr >= p['d1_strong_vol']:
                d1_confirm = 'strong'
                strong = True
                hold = max(hold, p['hold_strong'])   # 强确认延长持仓, 给连板空间
            elif d1_chg < 0 or (d1_chg < p['d1_weak_chg'] and vr < p['d1_weak_vol']):
                d1_confirm = 'weak'
                weak_exit = True
            else:
                d1_confirm = 'ok'
        elif d == 1 and mode == 'confirm':
            # 入场前已确认: 回填字段, 强确认延长持仓
            d1_chg = pre_d1_chg
            d1_vol_r = pre_d1_vol_r
            d1_confirm = pre_d1_confirm or 'ok'
            if d1_confirm == 'strong':
                strong = True
                hold = max(hold, p['hold_strong'])

        # --- 弱确认: d=2 开盘清仓 (仅d1open模式) ---
        if weak_exit and d == 2 and mode != 'confirm':
            exit_p, exit_d, exit_reason = b['open'], d, 'D1弱确认,D2开盘清仓'
            break

        # --- 追踪止损 (涨停日豁免, 骑板) ---
        if d > 1 and not is_lu and b['low'] <= peak * (1 + trail / 100):
            exit_p, exit_d, exit_reason = peak * (1 + trail / 100), d, f'追踪止损{trail}%'
            break
        # --- 止损 ---
        if b['low'] <= entry_price * (1 + stop / 100):
            exit_p, exit_d, exit_reason = entry_price * (1 + stop / 100), d, f'止损{stop}%'
            break

        # --- 收盘可知出场 ---
        if is_lu:
            streak += 1
            if streak > max_streak:
                max_streak = streak
        else:
            if streak >= p['ride_streak']:
                # 连板后断板 → 尾盘卖 (一致低量结束/开板出货预期)
                exit_p, exit_d, exit_reason = b['close'], d, f'连板{streak}后断板尾盘卖'
                break
            streak = 0
            if p.get('big_vol_exit', True) and d >= 2:
                prev_v = bars[idx - 1]['volume'] if idx > 0 else 0
                prev5 = vol_hist[-6:-1]
                ma5_v = sum(prev5) / len(prev5) if prev5 else 0
                big1 = prev_v > 0 and b['volume'] >= prev_v * p['big_vol_prev_r'] and b['close'] < b['open']
                big2 = ma5_v > 0 and b['volume'] >= ma5_v * p['big_vol_ma5_r'] and day_chg < 3.0
                if big1 or big2:
                    exit_p, exit_d, exit_reason = b['close'], d, '巨量出货尾盘卖'
                    break
            ret_e = (b['close'] / entry_price - 1) * 100
            if ret_e > 7:
                bar_range = b['high'] - b['low']
                upper = (b['high'] - max(b['open'], b['close'])) / bar_range * 100 if bar_range > 0 else 0
                if upper > 30 and b['close'] < b['high'] * 0.98:
                    exit_p, exit_d, exit_reason = b['close'], d, '峰值逃顶'
                    break

        # 兕底: 记录当日收盘 (无信号时)
        exit_p, exit_d = b['close'], d
        d += 1

    if exit_reason == '' and not capped:
        exit_reason = '持仓到期' if d > hold else '数据结束'
    return {
        'exit_price': round(exit_p, 3), 'exit_day': exit_d,
        'exit_reason': exit_reason,
        'return_pct': round((exit_p / entry_price - 1) * 100, 2),
        'peak_return_pct': round((peak / entry_price - 1) * 100, 2),
        'd1_chg': round(d1_chg, 2) if d1_chg is not None else None,
        'd1_vol_r': round(d1_vol_r, 2) if d1_vol_r is not None else None,
        'd1_confirm': d1_confirm,
        'weak_exit': weak_exit,
        'max_streak': max_streak,
        'open': bool(capped),
    }


def dragon2_today_d0_signals(bars, code, stock_info=None, today_str=None, params=None):
    """龙回头Pro 今日(D0)信号: 逐日只用当日收盘可知数据, 与回测共用

    两种入场形态 (同日互斥, 取评分高者):
      (a)缩量企稳: D0=回调末期缩量小阴(-3%~-0.5%), 量比[0.5,0.8) → 次日开盘买
      (b)放量启动: D-1及之前为回调, D0收阳涨>=2%且量比>=1.5x → 次日开盘买
    共同前置: 锚点日(涨停/大涨)后连续回调3~11天 + 八因子评分>=min_score + 硬门槛。
    返回0~1个信号dict。
    """
    result = []
    p = params or DRAGON2_PARAMS
    n = len(bars)
    if n < 66:
        return result
    if today_str:
        idxs = [j for j, b in enumerate(bars) if b['time'] == today_str]
        if not idxs:
            return result
        i = idxs[-1]
    else:
        i = n - 1
    if i < 65:
        return result
    board_type = get_board_type(code)
    is_gem = board_type == 'gem_star'
    limit_thr = 0.198 if is_gem else 0.098

    # 特征8: ST/退市风险剔除
    if stock_info:
        _name = (stock_info.get('name') or '')
        if 'ST' in _name.upper() or '退' in _name:
            return result

    info = stock_info or {}
    circ = float(info.get('circ_shares') or 0)

    d0 = bars[i]
    prev_c = bars[i - 1]['close']
    if prev_c <= 0 or d0['close'] <= 0:
        return result
    last_chg = (d0['close'] / prev_c - 1) * 100
    prev_vol = bars[i - 1]['volume']
    sig_vol_r = d0['volume'] / prev_vol if prev_vol > 0 else 0

    closes = [b['close'] for b in bars]
    # --- 特征5: 均线结构 ---
    ma60_now = _sma(closes, 60)
    ma60_5ago = _sma_at(closes, 5, 60)
    ma10_now = _sma(closes, 10)
    ma10_3ago = _sma_at(closes, 3, 10)
    ma20_now = _sma(closes, 20)
    ma20_3ago = _sma_at(closes, 3, 20)
    ma5_now = _sma(closes, 5)
    ma60_slope = None
    if ma60_now and ma60_5ago and ma60_5ago > 0:
        ma60_slope = (ma60_now / ma60_5ago - 1) * 100
        if ma60_slope < p['ma60_slope_hard']:
            return result   # MA60深度下行 → 硬性剔除

    # --- 特征3: 锚点日(涨停或大涨) — 从最近往回扫, 收集全部候选, 取评分最高 ---
    big_gain_thr = p['big_gain_gem'] if is_gem else p['big_gain_main']
    lu_thr = limit_thr * 0.98
    maxpb = p['max_pullback_days']
    lo_idx = max(1, i - 1 - maxpb)
    cands = []
    for a in range(i - 1, lo_idx - 1, -1):
        a_prev = bars[a - 1]['close']
        if a_prev <= 0:
            continue
        anchor_chg = (bars[a]['close'] / a_prev - 1) * 100
        is_lu_day = anchor_chg >= lu_thr
        is_big = p.get('use_big_gain_anchor', True) and anchor_chg >= big_gain_thr
        if not (is_lu_day or is_big):
            continue
        anchor_close = bars[a]['close']
        anchor_open = bars[a]['open']
        anchor_vol = bars[a]['volume']
        if anchor_close <= 0 or anchor_vol <= 0:
            continue
        pb_a = i - a            # (a)回调天数(含今日)
        pb_b = i - 1 - a        # (b)回调天数(到昨日)
        ok_a = (p.get('use_mode_a', True) and p['min_pullback_days'] <= pb_a <= maxpb
                and all(bars[j]['close'] < anchor_close for j in range(a + 1, i + 1)))
        ok_b = (p.get('use_mode_b', True) and p['min_pullback_days'] <= pb_b <= maxpb
                and all(bars[j]['close'] < anchor_close for j in range(a + 1, i)))
        if not ok_a and not ok_b:
            continue
        modes = []
        if ok_a:
            modes.append(('a', pb_a, i))
        if ok_b:
            modes.append(('b', pb_b, i - 1))
        for style, pullback_days, pb_end in modes:
            # 信号日形态
            if style == 'a' and not (-p['max_last_chg'] < last_chg < -p['last_chg_min_abs']
                                     and p['vol_r_lo'] <= sig_vol_r < p['vol_r_hi']):
                continue
            if style == 'b' and not (last_chg >= p['b_min_chg'] and sig_vol_r >= p['b_min_vol_r']
                                     and d0['close'] > d0['open']):
                continue
            # 回调窗口统计
            pb_lows = [bars[j]['low'] for j in range(a + 1, pb_end + 1)]
            pb_vols = [bars[j]['volume'] for j in range(a + 1, pb_end + 1)]
            pb_low = min(pb_lows) if pb_lows else 0
            pb_vol_avg = sum(pb_vols) / len(pb_vols) if pb_vols else 0
            # 回撤硬门槛 (特征6: 支撑明显=回调不深)
            dd = (pb_low / anchor_close - 1) * 100 if anchor_close > 0 else 0
            dd_max = p['drawdown_max_gem'] if is_gem else p['drawdown_max_main']
            if dd < dd_max:
                continue
            score = 0
            # --- 特征1 换手率 ---
            turn_anchor = turn_sig = None
            if circ > 0:
                turn_anchor = anchor_vol / circ * 100
                turn_sig = d0['volume'] / circ * 100
                if turn_anchor < p['turnover_hard']:
                    continue        # 锚点换手不足 → 不活跃, 剔除
                if turn_anchor >= p['turnover_hot']:
                    score += 3
                elif turn_anchor >= p['turnover_min']:
                    score += 2
                else:
                    score += 1
                if turn_sig is not None:
                    if turn_sig >= (p['turnover_min'] if style == 'b' else 2.0):
                        score += 1
            else:
                score += 1          # 股本缺失 → 中性
            # --- 特征2 流通市值 ---
            mcap_yi = None
            if circ > 0:
                mcap_yi = circ * d0['close'] / 1e8
                if p['mcap_lo'] <= mcap_yi <= p['mcap_hi']:
                    score += 3
                elif p['mcap_hard_lo'] <= mcap_yi <= p['mcap_hard_hi']:
                    score += 1
                else:
                    continue        # 市值出硬门槛范围 → 剔除
            else:
                score += 1
            # --- 特征5 MA60斜率 + 均线结构 ---
            if ma60_slope is None:
                score += 2
            elif ma60_slope >= 0.3:
                score += 3
            elif ma60_slope >= 0:
                score += 2
            elif ma60_slope >= -0.5:
                score += 1
            ma10_up = ma10_now is not None and ma10_3ago is not None and ma10_now > ma10_3ago
            ma20_up = ma20_now is not None and ma20_3ago is not None and ma20_now > ma20_3ago
            ma10_gt_ma20 = ma10_now is not None and ma20_now is not None and ma10_now > ma20_now
            ma_bull = bool(ma5_now and ma10_now and ma20_now and ma5_now > ma10_now > ma20_now)
            score += (1 if ma10_up else 0) + (1 if ma20_up else 0) \
                + (1 if ma10_gt_ma20 else 0) + (1 if ma_bull else 0)
            # --- 特征6 支撑 ---
            ma_touch = ((ma10_now and pb_low <= ma10_now * 1.02)
                        or (ma20_now and pb_low <= ma20_now * 1.02))
            ma_held = (d0['close'] >= ma20_now * 0.99) if ma20_now else True
            sup_ma = bool(ma_touch and ma_held)
            sup_anchor_open = pb_low >= anchor_open
            if sup_ma:
                score += 2
            if sup_anchor_open:
                score += 2
            # --- 启动放量 (锚点日量 vs 5日均量) ---
            if a >= 5:
                pre5 = [bars[j]['volume'] for j in range(a - 5, a)]
                pre5_avg = sum(pre5) / 5 if pre5 else 0
                anchor_vol_r = anchor_vol / pre5_avg if pre5_avg > 0 else 0
            else:
                anchor_vol_r = 0
            if anchor_vol_r >= p['anchor_vol_hot']:
                score += 2
            elif anchor_vol_r >= p['anchor_vol_min']:
                score += 1
            # --- 回调质量 ---
            if pb_vol_avg > 0 and anchor_vol > 0 and pb_vol_avg <= anchor_vol * 0.7:
                score += 1
            big_red = any(bars[j]['volume'] >= anchor_vol and bars[j]['close'] < bars[j]['open']
                          for j in range(a + 1, pb_end + 1))
            if not big_red:
                score += 1
            # --- 位置 ---
            ratio = d0['close'] / anchor_close if anchor_close > 0 else 0
            if style == 'a' and 0.90 <= ratio <= 0.995:
                score += 1
            if style == 'b' and ratio >= 1.0:
                score += 1
            cands.append({
                'code': code, 'board': get_board_name(code),
                'path': 'dragon2', 'path_label': '龙回头Pro',
                'style': style,
                'entry_style': '(a)缩量企稳' if style == 'a' else '(b)放量启动',
                'lu_date': bars[a]['time'],
                'anchor_chg': round(anchor_chg, 2),
                'anchor_vol_r': round(anchor_vol_r, 2),
                'pullback_days': pullback_days,
                'signal_date': d0['time'],
                'signal_chg': round(last_chg, 2),
                'signal_vol_r': round(sig_vol_r, 2),
                'signal_price': round(d0['close'], 3),
                'sig_vol': d0['volume'],
                'buy_mode': 'next_open',
                'score': score,
                'turnover_anchor': round(turn_anchor, 2) if turn_anchor is not None else None,
                'turnover_sig': round(turn_sig, 2) if turn_sig is not None else None,
                'float_mcap_yi': round(mcap_yi, 1) if mcap_yi is not None else None,
                'ma60_slope': round(ma60_slope, 2) if ma60_slope is not None else None,
                'ma_bull': ma_bull,
                'support_ma': sup_ma,
                'support_anchor_open': sup_anchor_open,
                'pullback_drawdown': round(dd, 2),
                'anchor_type': 'LU' if is_lu_day else 'BIG',
                'sig_vs_anchor': round(ratio * 100, 2),
                'sig_vs_ma10': round((d0['close'] / ma10_now - 1) * 100, 2) if ma10_now else None,
                'sig_vs_ma20': round((d0['close'] / ma20_now - 1) * 100, 2) if ma20_now else None,
                'pullback_close_min_ratio': round(min(bars[j]['close'] for j in range(a + 1, pb_end + 1)) / anchor_close * 100, 2) if pb_end > a else None,
            })
    if not cands:
        return result
    best = max(cands, key=lambda c: c['score'])
    if best['score'] < p['min_score']:
        return result
    result.append(best)
    return result


def _dragon2_d1_confirm(bars, i, sig, p):
    """评估D1确认日强度: strong/ok/weak (只用D1收盘可知数据)"""
    d0c, d1 = bars[i], bars[i + 1]
    if d0c['close'] <= 0:
        return None, None, None
    d1_chg = (d1['close'] / d0c['close'] - 1) * 100
    d1_vol_r = d1['volume'] / sig['sig_vol'] if sig.get('sig_vol', 0) > 0 else None
    vr = d1_vol_r if d1_vol_r is not None else 9.9
    if d1_chg >= p['d1_strong_chg'] and vr >= p['d1_strong_vol']:
        d1_confirm = 'strong'
    elif d1_chg < 0 or (d1_chg < p['d1_weak_chg'] and vr < p['d1_weak_vol']):
        d1_confirm = 'weak'
    else:
        d1_confirm = 'ok'
    return d1_confirm, round(d1_chg, 2), round(d1_vol_r, 2) if d1_vol_r is not None else None


def strategy_dragon2(bars, code, stock_info=None, params=None):
    """龙回头Pro 回测 (as-of统一版)

    entry_mode='confirm' (默认, 确认制):
      D0信号(八因子形态) → D1确认日观察: 强确认(涨>=3%且量>=1.5x信号日量)
      → D2开盘买 (中性确认可选, 弱确认丢弃) — 对应用户经验
      "继续上攻比前一天明显放量再介入", 把弱势反弹挡在入场前。
    entry_mode='d1open' (旧口径): D0信号 → D1开盘直接买, D1收盘评估确认,
      弱确认D2开盘清仓。

    与 --today 报告共用 dragon2_today_d0_signals / run_backtest_dragon2,
    保证回测与实盘报告严格对齐。去重: 与上一采纳信号日±4天内跳过。
    """
    board_type = get_board_type(code)
    p = params or DRAGON2_PARAMS
    n = len(bars)
    if n < 67:
        return []
    mode = p.get('entry_mode', 'confirm')
    trades = []
    used_days = []
    for i in range(65, n - 1):
        sigs = dragon2_today_d0_signals(bars[:i + 1], code, stock_info=stock_info, params=p)
        if not sigs:
            continue
        sig = sigs[0]
        # 去重: 新信号与上一个已采纳信号日±4天内跳过
        if any(abs(i - u) <= 4 for u in used_days):
            continue
        used_days.append(i)

        if mode == 'confirm':
            # --- 确认制: D1观察, D2开盘买 ---
            if i + 2 >= n:
                continue
            d1_confirm, d1_chg, d1_vol_r = _dragon2_d1_confirm(bars, i, sig, p)
            if d1_confirm is None or d1_confirm == 'weak':
                continue
            if d1_confirm == 'ok' and not p.get('allow_ok_confirm', False):
                continue
            d1 = bars[i + 1]
            d2 = bars[i + 2]
            entry_price = d2['open']
            if entry_price <= 0:
                continue
            gap = (entry_price / d1['close'] - 1) * 100 if d1['close'] > 0 else 0
            g_lo, g_hi = p['entry_gap_confirm']
            if gap < g_lo or gap > g_hi:
                continue
            r = run_backtest_dragon2(bars, i + 2, entry_price, board_type,
                                     sig_close=d1['close'], sig_vol=d1['volume'],
                                     entry_style=sig['style'], params=p,
                                     entry_mode='confirm', pre_d1_chg=d1_chg,
                                     pre_d1_vol_r=d1_vol_r, pre_d1_confirm=d1_confirm)
            if not r:
                continue
            trades.append({
                **sig,
                'entry_mode': 'confirm',
                'confirm_date': d1['time'],
                'd1_confirm': d1_confirm,
                'd1_chg': d1_chg,
                'd1_vol_r': d1_vol_r,
                'entry_date': d2['time'],
                'entry_price': round(entry_price, 3),
                'entry_gap': round(gap, 2),
                'buy_mode': 'next_open',
                'max_streak': r.get('max_streak'),
                'exit_reason': r.get('exit_reason'),
                'exit_price': r['exit_price'],
                'exit_day': r['exit_day'],
                'return_pct': r['return_pct'],
                'peak_return_pct': r['peak_return_pct'],
            })
        else:
            # --- 旧口径: D1开盘直接买, D1收盘评估确认 ---
            d1 = bars[i + 1]
            entry_price = d1['open']
            if entry_price <= 0:
                continue
            gap = (entry_price / bars[i]['close'] - 1) * 100 if bars[i]['close'] > 0 else 0
            g_lo, g_hi = p['entry_gap_a'] if sig['style'] == 'a' else p['entry_gap_b']
            if gap < g_lo or gap > g_hi:
                continue
            r = run_backtest_dragon2(bars, i + 1, entry_price, board_type,
                                     sig_close=bars[i]['close'], sig_vol=bars[i]['volume'],
                                     entry_style=sig['style'], params=p,
                                     entry_mode='d1open')
            if not r:
                continue
            trades.append({
                **sig,
                'entry_mode': 'd1open',
                'd1_confirm': r.get('d1_confirm'),
                'd1_chg': r.get('d1_chg'),
                'd1_vol_r': r.get('d1_vol_r'),
                'entry_date': d1['time'],
                'entry_price': round(entry_price, 3),
                'entry_gap': round(gap, 2),
                'buy_mode': 'next_open',
                'max_streak': r.get('max_streak'),
                'exit_reason': r.get('exit_reason'),
                'exit_price': r['exit_price'],
                'exit_day': r['exit_day'],
                'return_pct': r['return_pct'],
                'peak_return_pct': r['peak_return_pct'],
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


def buy_suggestion_text(d0_close, board_type, path='dragon_callback', style='a', entry_mode='confirm'):
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
    # 龙回头Pro: 按入场模式与形态给可买区间
    if path == 'dragon2':
        if entry_mode == 'confirm':
            if style == 'b':
                return (f"确认制: 明日D1确认(强:涨>=3%且量>=1.5x)→后日D2开盘买; "
                        f"D2开盘-5%~+6%可买(约{d0_close * 0.95:.2f}~{d0_close * 1.06:.2f}), 收阴/无力即放弃")
            return (f"确认制: 明日D1确认(强:涨>=3%且量>=1.5x)→后日D2开盘买; "
                    f"D2开盘-5%~+6%可买(约{d0_close * 0.95:.2f}~{d0_close * 1.06:.2f}), 收阴/无力即放弃")
        if style == 'b':
            lo_p = d0_close * (1 - 3.5 / 100)
            hi_p = d0_close * (1 + 5.5 / 100)
            return (f"开盘 -3.5%~+5.5% 可买(约{lo_p:.2f}~{hi_p:.2f}), 低开/平开放量更佳, 高开5.5%以上放弃")
        lo_p = d0_close * (1 - 3.0 / 100)
        hi_p = d0_close * (1 + 2.0 / 100)
        return (f"开盘 -3%~+2% 可买(约{lo_p:.2f}~{hi_p:.2f}), 高开2%以上放弃(追高损胜率)")
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

    # 龙回头Pro: 复用回测出场模拟 (stop_at_idx=今日), 保证与回测规则一致
    if path == 'dragon2':
        r = run_backtest_dragon2(bars, entry_idx, entry_price,
                                 'gem_star' if board_type == 'gem_star' else 'main',
                                 sig_close=t.get('signal_price') or 0.0,
                                 sig_vol=t.get('sig_vol') or 0.0,
                                 entry_style=t.get('style', 'a'),
                                 stop_at_idx=today_idx,
                                 entry_mode=t.get('entry_mode', 'confirm'),
                                 pre_d1_chg=t.get('d1_chg'),
                                 pre_d1_vol_r=t.get('d1_vol_r'),
                                 pre_d1_confirm=t.get('d1_confirm'))
        if r is None:
            return None
        if r.get('open'):
            ta = 'D1弱确认, 明日开盘清仓' if r.get('weak_exit') else None
            return {'status': 'open', 'hold_days': r['exit_day'],
                    'curr_ret': r['return_pct'], 'today_action': ta}
        reason = r.get('exit_reason', '')
        exit_idx = entry_idx + r['exit_day'] - 1
        exit_date = bars[exit_idx]['time'] if 0 <= exit_idx < len(bars) else None
        # 开盘/盘中已执行的出场(弱确认/止损/追踪) 或 昨日及以前的收盘出场 → 已平仓
        if exit_idx < today_idx or reason.startswith(('D1弱确认', '止损', '追踪止损')):
            return {'status': 'closed', 'exit_reason': reason, 'exit_date': exit_date}
        # 今日收盘触发的出场 → 明日执行
        return {'status': 'open', 'today_action': f'{reason} (今日收盘触发,明日开盘执行)',
                'hold_days': r['exit_day'], 'curr_ret': r['return_pct']}

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
    d2_today = [t for t in visible if t['path'] == 'dragon2' and _sig_date(t) == today_str]
    v1_today = [t for t in visible if t['path'] == 'v1' and _sig_date(t) == today_str]
    bb_today = [t for t in visible if t['path'] == 'break_buy' and _sig_date(t) == today_str]
    today_trades = dc_today + d2_today + v1_today + bb_today
    # 龙回头Pro·昨日信号今日已确认 (confirm_status 由 main 的 pending 阶段打标)
    d2_conf = [t for t in visible if t['path'] == 'dragon2' and _sig_date(t) != today_str
               and t.get('confirm_status')]
    today_trades = today_trades + d2_conf
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

    # 龙回头Pro信号
    if d2_today:
        print(f"\n  🐲 龙回头Pro ({len(d2_today)}只) - 八因子评分, 次日D1开盘买:")
        for t in sorted(d2_today, key=lambda x: -x.get('score', 0)):
            bt = get_board_type(t['code'])
            signal_price = t.get('signal_price') or t.get('entry_price')
            text = buy_suggestion_text(signal_price, bt, path='dragon2', style=t.get('style', 'a'),
                                       entry_mode=t.get('entry_mode', 'confirm'))
            _ta = f" 换手{t['turnover_anchor']:.1f}%" if t.get('turnover_anchor') is not None else ''
            _ts = f"/信{t['turnover_sig']:.1f}%" if t.get('turnover_sig') is not None else ''
            _mc = f" 市值{t['float_mcap_yi']:.0f}亿" if t.get('float_mcap_yi') is not None else ''
            _s60 = f" MA60斜率{t['ma60_slope']:+.2f}%" if t.get('ma60_slope') is not None else ''
            _tags = ('[多头排列]' if t.get('ma_bull') else '') \
                + ('[MA支撑]' if t.get('support_ma') else '') \
                + ('[不破锚开盘]' if t.get('support_anchor_open') else '')
            print(f"    {t['code']:<8} {t['board']:<6} {t['entry_style']} 涨停{t['lu_date']} 回调{t['pullback_days']}天 "
                  f"信号{t['signal_date']} {t['signal_chg']:+.1f}% 量比{t['signal_vol_r']:.2f}x score{t['score']}")
            print(f"{'':>10}{_ta}{_ts}{_mc}{_s60} {_tags}")
            print(f"{'':>10} 信号价{signal_price:.2f} 买入建议: {text}")
        print(f"  {'─' * 85}")
        print(f"  📋 确认制: 明日D1确认(强:涨>=3%且量>=1.5x信号日量) → 后日D2开盘 -5%~+6% 买入")

    # 龙回头Pro·昨日信号今日确认
    if d2_conf:
        print(f"\n  🐲 龙回头Pro·昨日信号·今日确认 ({len(d2_conf)}只) - 今日为D1确认日:")
        for t in sorted(d2_conf, key=lambda x: -x.get('score', 0)):
            _cs = t.get('confirm_status')
            _cs_txt = {'strong': '强确认(明日开盘买)', 'ok': '中性(默认不买, --d2-allow-ok放宽)'}.get(_cs, _cs)
            _d1c = t.get('d1_chg')
            _d1c_txt = f" D1涨{_d1c:+.1f}%" if _d1c is not None else ''
            _d1v = t.get('d1_vol_r')
            _d1v_txt = f" 量比{_d1v:.2f}x" if _d1v is not None else ''
            print(f"    {t['code']:<8} {t['board']:<6} {t['entry_style']} 信号{t['signal_date']} score{t['score']}"
                  f"{_d1c_txt}{_d1v_txt} → {_cs_txt}")
        print("")

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
                pl = {'dragon_callback': '龙回头', 'dragon2': '龙回头Pro', 'v1': 'V1', 'break_buy': '断板', }.get(t['path'], t['path'])
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
                pl = {'dragon_callback': '龙回头', 'dragon2': '龙回头Pro', 'v1': 'V1', 'break_buy': '断板', }.get(t['path'], t['path'])
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
    parser = argparse.ArgumentParser(description="龙回头 + 龙回头Pro + V1 + 断板 策略回测")
    parser.add_argument("--codes", default="")
    parser.add_argument("--days", type=int, default=300, help="向前取N个交易日 (默认300, 从当前日期往前推)")
    parser.add_argument("--source", choices=["manual", "db"], default="manual",
                        help="数据源: manual=手动指定codes(默认), db=从数据库加载全市场")

    parser.add_argument("--all-trades", action="store_true")
    parser.add_argument("--pullback", type=int, default=3, help="龙回头最少回调天数")
    parser.add_argument("--max-pullback", type=int, default=11, help="龙回头最多回调天数")
    parser.add_argument("--max-last-chg", type=float, default=3.0, help="龙回头末期小阳最大涨幅%%")
    # 龙回头Pro (dragon2) 参数
    parser.add_argument("--d2-min-score", type=float, default=12.0, help="龙回头Pro最低评分 (默认12, 满分23)")
    parser.add_argument("--d2-turnover", type=float, default=5.0, help="龙回头Pro换手评分满档线%% (默认5)")
    parser.add_argument("--d2-mcap-min", type=float, default=20.0, help="龙回头Pro流通市值下限亿 (默认20)")
    parser.add_argument("--d2-mcap-max", type=float, default=300.0, help="龙回头Pro流通市值上限亿 (默认300)")
    parser.add_argument("--d2-entry", choices=["confirm", "d1open"], default="confirm",
                        help="龙回头Pro入场: confirm=D1确认后D2开盘买(默认), d1open=D1开盘直接买")
    parser.add_argument("--d2-allow-ok", action="store_true", help="龙回头Pro: D1中性确认也买入(默认只要强确认)")
    parser.add_argument("--no-d2-big-gain", action="store_true", help="龙回头Pro: 禁用大涨锚点(只认涨停)")
    parser.add_argument("--no-d2-mode-a", action="store_true", help="龙回头Pro: 禁用(a)缩量企稳入场")
    parser.add_argument("--no-d2-mode-b", action="store_true", help="龙回头Pro: 禁用(b)放量启动入场")
    parser.add_argument("--no-d2-d1confirm", action="store_true", help="龙回头Pro: 禁用D1确认规则")
    parser.add_argument("--no-d2-ride", action="store_true", help="龙回头Pro: 禁用骑板规则")
    parser.add_argument("--no-d2-bigvol", action="store_true", help="龙回头Pro: 禁用巨量出货逃顶")
    parser.add_argument("--d2-no-hard", action="store_true", help="龙回头Pro: 关闭全部硬门槛(仅评分)")
    parser.add_argument("--strategy", default="all", choices=["all", "dragon", "dragon2", "v1", "break"],
                        help="运行策略: all=全部, dragon=龙回头, dragon2=龙回头Pro, v1=V1, break=断板")
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
    use_db = args.source == "db"
    if args.source == "db":
        print("📊 DB模式: 从数据库加载全市场股票...")
        codes = get_all_codes_db()
        print(f"   全市场: {len(codes)} 只股票")
    elif codes:
        print(f"📊 指定股票: {codes}，自动从DB加载数据...")

    run_dc = args.strategy in ("all", "dragon")
    run_dc2 = args.strategy in ("all", "dragon2")
    run_v1 = args.strategy in ("all", "v1")
    run_bb = args.strategy in ("all", "break")

    # 龙回头Pro参数: CLI覆盖 → params dict
    d2_params = dict(DRAGON2_PARAMS)
    d2_params.update(
        min_score=args.d2_min_score,
        turnover_min=args.d2_turnover,
        mcap_lo=args.d2_mcap_min, mcap_hi=args.d2_mcap_max,
        use_big_gain_anchor=not args.no_d2_big_gain,
        use_mode_a=not args.no_d2_mode_a,
        use_mode_b=not args.no_d2_mode_b,
        entry_mode=args.d2_entry,
        allow_ok_confirm=args.d2_allow_ok,
    )
    if args.no_d2_d1confirm:
        d2_params['d1_weak_chg'] = -1e9
        d2_params['d1_strong_chg'] = 1e9
    if args.no_d2_ride:
        d2_params['ride_streak'] = 999
    if args.no_d2_bigvol:
        d2_params['big_vol_exit'] = False
    if args.d2_no_hard:
        d2_params.update(turnover_hard=0.0, mcap_hard_lo=0.0, mcap_hard_hi=1e18,
                         ma60_slope_hard=-1e9, drawdown_max_main=-100.0, drawdown_max_gem=-100.0)

    mode_label = {"signal_close": "信号日收盘买", "next_open": "D+1开盘买"}[args.buy_mode]

    print(f"{'=' * 80}")
    print(f"龙回头 + 龙回头Pro + V1 + 断板 策略回测")
    print(f"{'=' * 80}")
    print(f"买入模式: {mode_label}")
    labels = []
    
    if run_dc: labels.append(f"龙回头(回调{args.pullback}-{args.max_pullback}天)")
    if run_dc2: labels.append(f"龙回头Pro(评分>={d2_params['min_score']})")
    if run_v1: labels.append("V1")
    if run_bb: labels.append(f"断板(连板≥2)")
    print(f"运行: {' + '.join(labels)}")
    print(f"股票: {len(codes)}只\n")

    dc_trades, d2_trades, v1_trades, bb_trades = [], [], [], []
    bars_by_code = {}
    success = 0

    # 加载stock_basic_info (换手率 + 板块效应)
    stock_info = None
    sector_counts_by_date = None
    need_stock_info = run_dc2   # 龙回头Pro需要换手率/市值/ST名称过滤
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
        if run_dc2:
            info = stock_info.get(code) if stock_info else None
            d2 = strategy_dragon2(bars, code, stock_info=info, params=d2_params)
            d2_trades.extend(d2)
            parts.append(f"龙回头Pro{len(d2)}")
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

        has_signal = (run_dc and len(dc) > 0) or (run_dc2 and len(d2) > 0) or (run_v1 and len(v1) > 0) or (run_bb and len(bb) > 0)
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

    if run_dc2:
        print(f"\n📊 龙回头Pro:")
        print_stats(d2_trades, "龙回头Pro")
        if d2_trades:
            print(f"\n  入场形态:")
            for st, label in [('a', '(a)缩量企稳'), ('b', '(b)放量启动')]:
                seg = [t for t in d2_trades if t.get('style') == st]
                if seg:
                    print_stats(seg, f"    {label}")
            print(f"\n  评分分布:")
            for lo, hi, label in [(16, 999, '>=16'), (14, 16, '14-15'), (12, 14, '12-13'), (0, 12, '<12')]:
                seg = [t for t in d2_trades if lo <= t.get('score', 0) < hi]
                if seg:
                    print_stats(seg, f"    score {label}")
            print(f"\n  D1确认:")
            for cf, label in [('strong', '强确认(骑板)'), ('ok', '中性'), ('weak', '弱确认(D2清仓)')]:
                seg = [t for t in d2_trades if t.get('d1_confirm') == cf]
                if seg:
                    print_stats(seg, f"    {label}")
            print(f"\n  流通市值分布:")
            for lo, hi, label in [(0, 50, '<50亿'), (50, 100, '50-100亿'), (100, 300, '100-300亿'), (300, 9e18, '>=300亿')]:
                seg = [t for t in d2_trades if t.get('float_mcap_yi') is not None and lo <= t['float_mcap_yi'] < hi]
                if seg:
                    print_stats(seg, f"    {label}")
            print(f"\n  换手率(锚点日)分布:")
            for lo, hi, label in [(0, 5, '<5%'), (5, 10, '5-10%'), (10, 20, '10-20%'), (20, 9e18, '>=20%')]:
                seg = [t for t in d2_trades if t.get('turnover_anchor') is not None and lo <= t['turnover_anchor'] < hi]
                if seg:
                    print_stats(seg, f"    {label}")
            print(f"\n  🏆 龙回头Pro TOP5:")
            for t in sorted(d2_trades, key=lambda x: -x['peak_return_pct'])[:5]:
                print(f"    {t['code']} {t['entry_style']} 涨停{t['lu_date']} 回调{t['pullback_days']}天 score{t['score']} "
                      f"→ {t['entry_date']}买 收益{t['return_pct']:+.1f}% 峰值{t['peak_return_pct']:+.1f}%")
            if len(d2_trades) > 5:
                print(f"\n  💀 龙回头Pro BOTTOM5:")
                for t in sorted(d2_trades, key=lambda x: x['return_pct'])[:5]:
                    print(f"    {t['code']} {t['entry_style']} 涨停{t['lu_date']} 回调{t['pullback_days']}天 score{t['score']} "
                          f"→ {t['entry_date']}买 收益{t['return_pct']:+.1f}% 峰值{t['peak_return_pct']:+.1f}%")

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
    all_trades = dc_trades + d2_trades + v1_trades + bb_trades
    if len(all_trades) > max(len(dc_trades), len(d2_trades), len(v1_trades), len(bb_trades)):
        print(f"\n{'=' * 80}")
        print(f"📊 三策略合并:")
        print_stats(all_trades, "合并")
        dc_keys = {(t['code'], t['entry_date']) for t in dc_trades}
        d2_keys = {(t['code'], t['entry_date']) for t in d2_trades}
        v1_keys = {(t['code'], t['entry_date']) for t in v1_trades}
        bb_keys = {(t['code'], t['entry_date']) for t in bb_trades}
        overlap = (dc_keys & d2_keys) | (dc_keys & v1_keys) | (dc_keys & bb_keys) \
            | (d2_keys & v1_keys) | (d2_keys & bb_keys) | (v1_keys & bb_keys)
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
            if run_dc2:
                pending_signals.extend(dragon2_today_d0_signals(
                    bars[:idx + 1], code,
                    stock_info=(stock_info or {}).get(code),
                    params=d2_params))
                # 昨日信号 → 今日(D1)确认状态 (确认制: 今日强确认 → 明日开盘买)
                if idx >= 66:
                    _ys = dragon2_today_d0_signals(bars[:idx], code,
                                                   stock_info=(stock_info or {}).get(code),
                                                   params=d2_params)
                    for _y in _ys:
                        if any(s['code'] == _y['code'] for s in pending_signals):
                            continue
                        _cs, _yc, _yv = _dragon2_d1_confirm(bars, idx - 1, _y, d2_params)
                        _y['confirm_status'] = _cs or 'weak'
                        _y['d1_chg'], _y['d1_vol_r'] = _yc, _yv
                        if _y['confirm_status'] == 'strong' or (_y['confirm_status'] == 'ok'
                                                                and d2_params.get('allow_ok_confirm')):
                            pending_signals.append(_y)
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
    all_out = dc_trades + d2_trades + v1_trades + bb_trades
    if all_out:
        with open("test_dragon_callback_result.json", "w", encoding="utf-8") as f:
            json.dump(all_out, f, ensure_ascii=False, indent=2)
        print(f"\n💾 test_dragon_callback_result.json ({len(all_out)}笔)")

if __name__ == "__main__":
    main()

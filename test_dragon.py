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
# 龙回头Pro 核心逻辑已提取至 app/market_cn/auto/dragon_core.py (单一事实源)
# 本文件仅保留回测包装 strategy_dragon2 与 CLI
# ================================================================
from app.market_cn.auto.dragon_core import (  # noqa: E402
    DRAGON2_PARAMS, _sma, _sma_at, run_backtest_dragon2,
    dragon2_today_d0_signals, _dragon2_d1_confirm,
    BOARD_PARAMS, run_backtest, run_backtest_breakbuy,
    v1_today_d0_signals, break_today_d0_signals,
)

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
                 d_1_vol_max=1.5, stock_info=None):
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
                       stock_info=None):
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
                             stock_info=info)
            v1_trades.extend(v1)
            parts.append(f"V1{len(v1)}")
        if run_bb:
            bb = strategy_break_buy(bars, code, stock_info=stock_info.get(code) if stock_info else None)
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
            print(f"\n  换手率(信号日)分布:")
            for lo, hi, label in [(0, 1, '<1%'), (1, 2, '1-2%'), (2, 3, '2-3%'), (3, 5, '3-5%'), (5, 9e18, '>=5%')]:
                seg = [t for t in d2_trades if t.get('turnover_sig') is not None and lo <= t['turnover_sig'] < hi]
                if seg:
                    print_stats(seg, f"    {label}")
            print(f"\n  换手率(信号日, 总股本)分布:")
            for lo, hi, label in [(0, 0.5, '<0.5%'), (0.5, 1, '0.5-1%'), (1, 2, '1-2%'), (2, 9e18, '>=2%')]:
                seg = [t for t in d2_trades if t.get('turnover_sig_total') is not None and lo <= t['turnover_sig_total'] < hi]
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

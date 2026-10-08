"""strategies/dragon_callback.py — 龙回头策略 ("方案2", StrategyBase 插件实现, Phase 2 迁移)

实现已迁移至本文件; core.dragon_cb_today_d0_signals / run_backtest_dragon_callback /
DRAGON_CB_PARAMS 为 facade 转发 (test_dragon.py / dragon_scan / dragon_monitor 共用)。

规则框架 (2026-09-06 与 test_dragon.py 同步; 2026-09-16 消融调参, 依据 tmp/dragon_ablation.py):
  找龙(滑动窗口涨停占比>=70%) → 回调 gap[5,7] → 拐点OR(深跌释放 | 阳线承接;
  MA20支撑腿已参数关闭) → 龙强度(连板>=2 | 20日涨幅>=60 | RSI6>=45; 连板度量
  2026-09-28 A6 修复为真实口径, 修复前 ms=3 实际=真实>=2)
  → 信号质量(D0跌幅>-4%企稳; 阴线/RSI6<30/距MA20<-8 三排除门已参数关闭)
  → U1~U4(@涨停日) → D1开盘买 (gap 范围过滤 09-07 移除)
  出场: 分段追踪(-8/-3) + 固定止损-8 + 峰值逃顶 + 到期7天

易错点:
  - U1~U4 锚定涨停日 (@D0 评估换手会误杀 — D0 是缩量小阴日);
  - 找龙窗口 start=max(1, lu_idx-window), total_days<3 跳过 — 边界勿动;
  - exit 重放 stop_at_idx 语义: idx>stop_at_idx 即截断 open=True (盘中重放当天未收盘);
  - tech_score 仅输出参考 (评分门槛已关闭, 实验结论无判别力), 不参与过滤。
"""
from __future__ import annotations

from app.market_cn.auto.sampler import STAGE_RANK as _STAGE_RANK

from app.utils.indicators import (
    calc_macd, calc_psy, calc_roc, is_macd_golden_cross,
    is_macd_hist_shrinking_negative, is_macd_hist_turning_positive, rsi,
)
from app.market_cn.auto.core.market import find_limit_ups, get_board_name, get_board_type, is_limit_up
from app.market_cn.auto.core.runtime.functions import Ctx, register_strategy_funcs
from app.market_cn.auto.strategies import register
from app.market_cn.auto.strategies.base import (
    ConfirmDecision, EntryDecision, ExitDecision, ScanSpec, Signal, StrategyBase,
    data_end_close,
)
# 递推展示层契约 (2026-10-06: 折叠契约已并入生产 StrategyBase):
#   折叠契约已并入生产 StrategyBase（单继承）; 参数合并口径唯一 = params()
#   等生产侧没有的方法。放第二位, 与 knife/tail/g56 同一写法。
from app.market_cn.auto.core.present.contract import (  # noqa: E402
    DayInput, InsufficientHistory, Progress, Stage,
)
from app.utils.indicators import macd_core, macd_state  # noqa: E402

STRATEGY_KEY = "dragon_callback"
STRATEGY_LABEL = "龙回头"

DRAGON_CB_PARAMS = dict(
    # --- 找龙: 滑动窗口涨停占比 ---
    dragon_ratio=0.7,
    dragon_windows=[4, 5, 7, 10, 15, 20],
    # --- 回调窗口 (2026-09-16 gap_max 6→7 用户批准) ---
    # 600d 13变体实验 (tmp/dragon_buyexit_exp.py + dragon_gap_fine.py):
    #   [5,6] 125笔/52.8%/+1.40/总175.6/盈亏1.50/最差-12.98
    #   [5,7] 167笔/53.3%/+1.63/总271.9/盈亏1.62/最差-12.98 — 唯一笔数/胜率/均收/
    #         盈亏比/尾部全改善; 新增42笔 gap=7 票两段稳定(老市场段胜率44→48)。
    #   第8天是边际拐点 ([5,8] 51.9% 且引入-15.17大亏); 纯晚买[7,10]/[7,12] 盈亏比
    #   仅1.24~1.36 并出现-19.32大亏 — 答案是"多给一个入场日"不是"更晚买"。
    # 联动: backtest_stock 廉价预筛读同一参数, 自动同步, 无分叉 (见 L704 注释)。
    gap_min=5, gap_max=7,
    # --- 拐点过滤 (或关系) ---
    # ma20 腿 2026-09-16 关闭 (不删除判定代码): 逐门消融 tmp/dragon_ablation.py 实测
    # 全部候选 d0_vs_ma20∈[+1.6,+47.1], [-10,-5) 支撑区间零触发。hi=lo 使区间退化为
    # 空集, cond_ma20 恒 False; 恢复时把 hi 改回 -5.0 即可。拐点实际只靠 深跌/阳线 两腿。
    ma20_lo=-10.0, ma20_hi=-10.0,
    depth_max=-30.0,
    yin_ratio_max=0.5,
    # --- 信号质量排除 ---
    # 2026-09-16 逐门消融 (300d 真实回测): 下列两道排除门零触发 (前序门已使条件不可达)
    # 或负贡献 —— 用户裁定"尝试关闭, 不删除": 用哨兵参数停用, 判定代码原样保留,
    # 改回原值即恢复。
    #   阴线>=0.6: 关后 36笔/80.6%/+5.44 vs 基线 34笔/79.4%/+5.56 —— 砍掉的2笔是赚钱票;
    #   1.01 = 数学关闭 (yin_ratio∈[0,1] 恒 <1.01)。
    yin_ratio_exclude=1.01,
    #   RSI6<30: 零触发 (rsi6_min=45 之下不可能 <30); -100 哨兵停用。
    rsi6_exclude_lt=-100.0,
    #   距MA20<-8%: 零触发 (候选全部站在 MA20 上方); -100 哨兵停用。
    d0_ma20_exclude_lt=-100.0,
    # D0 企稳门槛 (2026-09-15 批准设立 -3%; 2026-09-16 消融后放宽到 -4%):
    #   300d 实跑: -3% 34笔/79.4%/+5.56/总189.1; -4% 36笔/77.8%/+5.62/总202.3,
    #   两段78/78最稳; -5% 38笔/73.7%/+4.86 (胜率掉得多)。取 -4% 增2笔且总贡献最高。
    #   设立归因 tmp/dragon_winrate_attr.py: D0跌幅<=-3% 是落刀非回调 (旧门该组48%)。
    #   注意: 消融为"逐关一门"投影, 组合口径以 600d 实跑为准 (重锚补位效应)。
    d0_chg_min=-4.0,
    # --- 龙强度门槛 (2026-09-10 设立; 2026-09-16 3→2 实测后回退 3):
    #     09-10 特征判别: 亏损源画像 = streak<=2伪龙 61笔27.9%/-2.4%。
    #     09-16 300d 消融显示放连板"关连板门总贡献+16.7pp", 当日改 2; 但 600d 实跑
    #     (tmp/dragon_combo600_cmp.py) 证伪: 放入的35笔2板票整体34.3%胜/总贡献-0.2pp,
    #     老市场段(2024-06~2025-02)23.5%/-16.3pp, 近市场段也仅44.4% — 300d 结论是
    #     强市场环境偏差。用户裁定回退3。注意: 本策略 600d 前段(2024下半年)各变体均
    #     负收益, 属策略级环境失效而非参数问题。 ---
    # A6 (2026-09-28): _lu_streak off-by-one 修复后度量回归真实连板高度, 3→2 保持
    # 实际行为不变 (旧 buggy ms=3 恰好等价真实>=2, 600d 逐笔等价 167/167 实证)。
    # 注意: 下方 09-10/09-16 历史结论均在 buggy 度量上得出, 其"streak<=2伪龙"实为真实<=1板。
    min_streak=2,        # 锚定涨停日真实连板高度>=2 ("龙"的最低成色; 修复前 ms=3 的实际效果)
    lu_gain20_min=60.0,  # 涨停日20日涨幅>=60% (前期热度; >=100更好但样本锐减)
    rsi6_min=45.0,       # D0 RSI6>=45 (强势回调; rsi6_exclude_lt 已停用, 下界即此值)
    # 三条件合计实测 (09-10): 600d 334→167笔 胜率48.8→51.5% 均收-0.13→+1.09 均峰7.68→9.0
    #   盈亏比1.00→1.39 (两段同向); 300d 117→50笔 50.4→62.0% 均收+0.07→+2.63。
    #   注意: 实跑优于/异于"离线过滤投影"属预期 —— 规则作用在涨停日候选上, 会重锚定
    #   (break@首个通过条件的 lu), 而非简单删旧笔; 回归口径以实跑为准。
    # --- 入场 ---
    # (2026-09-07 用户裁定: 移除 D1 gap 范围过滤 [-3,+2] — 信号本身已筛选,
    #  高开/低开由用户自行判断, 展示更多股票; 旧引擎口径 114笔/74.6% 已废弃 —
    #  2026-09-09 出场引擎现实化(T+1/跳空/跌停)后 116笔/50.9%/+0.21%)
    # --- 出场 ---
    # (2026-09-10 晚 C2 采纳, 用户批准: 出场参数扫描 tmp/龙回头出场参数扫描报告.md —
    #  固定 50 笔入场真引擎重放, 自校验 50/50 逐笔一致; trail_lo -8→-3 (原-8与stop_loss
    #  重合=前段形同虚设) + peak_exit_ret 7→4; 两段同向 (seg1/seg2 均不退化), C6≡C2 非孤点;
    #  +2.63→+2.97/笔 盈亏比1.37→1.58, 改善全来自盈亏比; hold_days/stop_loss 扫描无效故不动。
    #  ⚠️ 实盘执行口径依赖: trail_lo=-3 盘中触发更频繁, "收盘逃顶 vs 追踪线优先"差异被放大
    #  (回测对照 +2.97 vs +2.29), 实盘须人工尾盘盯盘并逐笔记 execution_mode)
    # 2026-09-25: trail_lo/hi -3 → -2 (用户裁定保盈亏比路线)。300d 出场真注入对照:
    #  胜率 72.7% 持平, 均收 5.0→5.14, 盈亏比 2.43→2.52。config.json 同步; YAML 门表
    #  params 段请保持一致 (展示/敏感性读 YAML)。
    hold_days=7,
    stop_loss=-8.0,
    trail_lo=-2.0,
    trail_hi=-2.0,
    trail_switch_pct=3.0,
    peak_exit_ret=4.0,
    peak_exit_upper=30.0,
)


# ================================================================
# 判定原语 (2026-09-23 重构: 单实现 —— python 判定与 YAML 门表共用同一份代码)
# ----------------------------------------------------------------
# 背景: 门表化之前, 判定逻辑全部内联在 scan_signals 里; 若 YAML 再写一份等价表达式,
#       就是双实现 (改一边忘另一边 = 静默漂移)。本段把每个"量"抽成 **bars 级纯函数**
#       (无 self / 无状态 / 参数由调用方传入), scan_signals 与文件尾的门函数适配器
#       (register_strategy_funcs) 都只调用它们 —— 规则只有一份事实源。
# 重构纪律: 逐字搬移, 含 None 语义与边界条件一律不变 (改一行即可能改一笔交易)。
# ================================================================


def _lu_streak(bars, lu_idx, board_type) -> int:
    """涨停日连板高度 (含涨停日本身, 向前连续涨停计数): L板返回L。

    易错点 (A6, 2026-09-28 修复): 初值 1 已计涨停日本身, 循环必须从 j=lu_idx-1 起步 ——
    原实现 j=lu_idx 起步, 首轮 is_limit_up(bars[lu_idx], bars[lu_idx-1]) 恒真再 +1,
    返回值恒 = 真实+1 (1板报2), 展示字段/落库/前端跟着虚高, min_streak 语义错位一档
    (历史 min_streak=3 实际只拦真实1板)。600d 验证: 修复+ms=2 与旧行为逐笔等价
    167/167, 真实分桶: 1板 35.9%/+0.02 (垃圾桶), 2板 50%/+1.25, 3板 52.9%/+2.58
    (tmp/verify_a6_streak.py)。
    """
    streak_h = 1
    j = lu_idx - 1
    while j > 0 and is_limit_up(bars[j]["close"], bars[j - 1]["close"], board_type):
        streak_h += 1
        j -= 1
    return streak_h


def _lu_gain20(bars, lu_idx):
    """涨停日往前 20 日涨幅 %; lu_idx<20 或基价<=0 → None (数据不足, 判 False)。"""
    if lu_idx < 20:
        return None
    lu_close = bars[lu_idx]["close"]
    base = bars[lu_idx - 20]["close"]
    return (lu_close / base - 1) * 100 if base > 0 else None


def _dragon_found(bars, lu_idx, board_type, ratio, windows) -> bool:
    """找龙: 任一滑动窗口内涨停占比 >= ratio 即成立 (窗口 start=max(1, lu-window),
    窗口长度<3 跳过)。"""
    for window in windows:
        start = max(1, lu_idx - window)
        total_days = lu_idx - start
        if total_days < 3:
            continue
        lu_count = sum(1 for k in range(start, lu_idx)
                       if k > 0 and is_limit_up(bars[k]["close"], bars[k - 1]["close"], board_type))
        if lu_count / total_days >= ratio:
            return True
    return False


def _pullback_depth(bars, lu_idx, i) -> float:
    """回调期最深跌幅 % (区间最低 low 相对涨停收盘)。"""
    lu_close = bars[lu_idx]["close"]
    min_low = min(bars[j]["low"] for j in range(lu_idx + 1, i + 1))
    return (min_low / lu_close - 1) * 100


def _yin_ratio(bars, lu_idx, i) -> float:
    """回调期阴线占比 (0~1); 区间为空 → 1.0。"""
    pb_yin = sum(1 for j in range(lu_idx + 1, i + 1) if bars[j]["close"] < bars[j]["open"])
    pb_total = i - lu_idx
    return pb_yin / pb_total if pb_total > 0 else 1.0


def _d0_vs_ma20(bars, i):
    """D0 收盘相对 MA20 偏离 %; i<19 → None。"""
    if i < 19:
        return None
    ma20 = sum(bars[j]["close"] for j in range(i - 19, i + 1)) / 20
    return (bars[i]["close"] / ma20 - 1) * 100 if ma20 > 0 else None


# ================================================================
# 递推展示层: RSI6 增量状态 (Wilder 二元组) + 窗口常量
# ----------------------------------------------------------------
# RSI 参与门判定 (rsi6_min=45) ⇒ 必须**逐位等于**全量 `rsi(closes, 6)`。
# Wilder 递推是马尔可夫的: 只要 avg_g/avg_l 播种与全量一致, 逐步递推恒等
# (实测 46/81/151/301 根与全量逐位相等)。故递推路径不需要重算全序列。
# ================================================================
RING = 40          # 递推窗口: 门窗口 28 + 出场重放 ≤7 天 + 跌停顺延

# anchor_step 与 cross_section 同源 (G1 状态机推进 EMA 锚)
from app.market_cn.auto.core.features.cross_section import _anchor_step  # noqa: E402
# Wilder RSI 递推单一实现 (基座叶子层) —— 本文件只留策略口径包装 (周期 6)
from app.utils.indicators import (  # noqa: E402
    rsi_state as _rsi_state, rsi_step as _rsi_step_i, rsi_value as _rsi_value_i)


def rsi_init(closes, period=6):
    """全量 closes → 增量状态 [avg_g, avg_l]; len<period+1 → None。"""
    return _rsi_state(closes, period)


def rsi_step(st, prev_close, close, period=6):
    """推进一步 (Wilder); st 为 None 时返回 None。"""
    return _rsi_step_i(st, prev_close, close, period)


def rsi_value(st):
    """增量状态 → RSI 值 (与全量 rsi() 同式)。"""
    return _rsi_value_i(st)


def _tech_block(closes, use_tech_score=True, *, macd_triple=None, rsi_val=None):
    """技术面加分块 → (score, rsi_val, roc, psy)。

    score/roc/psy 只进展示字段 (评分门槛已关闭, 实验结论无判别力); **rsi_val 参与判定**
    (龙强度 ③ 与质量排除), 故 use_tech_score=False 时 rsi_val=None → 对应门放行不误杀。

    macd_triple / rsi_val (2026-10-06): 递推路径传入的**已算好**值 (MACD 锚播种 +
    RSI 增量递推, 均与全量逐位一致); None 则按全量 closes 自算。两条路径共用本
    函数 —— 打分只有一份实现, 不会分叉。
    """
    score = 0
    rsi_val = roc = psy = None
    if not use_tech_score:
        return score, rsi_val, roc, psy
    dif, dea, hist = macd_triple if macd_triple is not None else calc_macd(closes)
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
    if rsi_val is None:            # 递推路径已算好且逐位等于全量, 不重算
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
    return score, rsi_val, roc, psy


# ================================================================
# 出场模拟 (原 core.run_backtest_dragon_callback, 原样移植)
# 2026-09-09 现实化修正 (tmp/_dragon_intraday_exit.py E1 口径):
#   ① T+1: 买入当日(d=1)不可卖出 — 仅更新峰值/估值, 全部出场判定从 d=2 起;
#   ② 跳空穿越: 触发日开盘价低于触发价 → 按开盘价成交 (跳空低开只能按开盘卖);
#   ③ 跌停无法卖出: 一字跌停整日跳过; 成交价触及跌停 → 顺延次日开盘强平。
#   注意: 追踪线与止损线同日双触发取 max(价格连续, 先穿过更高触发线);
#         峰值逃顶仍是收盘判定优先 — 若当日盘中已触及追踪线, 现实中会先按
#         追踪线成交, 此处保留"收盘逃顶优先"的原设计语义 (已知理想化)。
# ================================================================

# 跌停价原语分布在 core/exec.py 与 core/market.py (C 阶段); 别名保持调用点不变
from app.market_cn.auto.core.exec import (
    fill_blocked_by_limit_dn,
    fill_on_gap,
    is_one_word_limit_dn,
)
from app.market_cn.auto.core.market import limit_dn_price as _limit_dn_price
from app.market_cn.auto.probe import DayTrace as _DayTrace, \
    sample_feats as _probe_sample_feats   # 探针框架件 (无环; 只提供通用特征/标签)


# ================================================================
# 调试通道 (2026-09-10 用户裁定: 调龙回头只改本文件, 框架层 probe.py 零改动)
# ----------------------------------------------------------------
# 规则:
#   - 仅 debug 模式 (probe 非 None) 才计算并写入 sample.labels; 判定与实盘路径
#     绝不读取本段任何内容 (改这里不影响任何一笔交易)。
#   - 改口径只改本段; **归档键名保持稳定** (离线脚本/存档按键名读取)。
# 标签三组:
#   1) 固定持有 N 日       ret_d{N}c / peak{N} / mae{N} / peak_day
#   2) 峰值回撤出场(多档)  ret_tr{t} / peak_tr{t} / mae_tr{t} / day_tr{t} /
#                          rsn_tr{t} / cap_tr{t}
#   3) 波次视角            wave_amp (整波涨幅) / entry_lag (入场推后天数)
# ================================================================
DEBUG_HOLD_DAYS = 7            # 固定持有交易日数
DEBUG_TRAILS = (4, 6, 8, 12)   # 峰值回撤阈值序列 (一次回测扫多档 = 阈值敏感性前置)
DEBUG_MAX_HOLD = 10            # 无波次窗口时的最大持有交易日
DEBUG_WAVE_DAYS = 20           # 波次窗口长度 (自"第一条规则通过日"起)


def _fixed_hold_labels(bars, i, entry, days=DEBUG_HOLD_DAYS):
    """固定持有 days 日标签: 第 days 日收盘无条件卖出 (排除出场引擎差异)。

    用途: 规则归因 — 用**同一条**出场规则衡量各入场规则的贡献。
    口径: 入场=D+1 开盘; peak/mae 取持有段(含入场日)极值相对入场价%;
    视野不足 (i+days 越界) → ret 记 None (=censored), peak/mae 仍记。
    """
    n = len(bars)
    out = {}
    if not entry or entry <= 0 or i + 1 >= n:
        return out
    last = min(i + days, n - 1)
    highs = [float(bars[k]["high"]) for k in range(i + 1, last + 1)]
    lows = [float(bars[k]["low"]) for k in range(i + 1, last + 1)]
    if highs:
        out[f"peak{days}"] = round((max(highs) / entry - 1) * 100, 2)
        out["peak_day"] = int(highs.index(max(highs)) + 1)     # 第几个持有日见顶(1-based)
        out[f"mae{days}"] = round((min(lows) / entry - 1) * 100, 2)
    if i + days < n:                       # 完整视野才给出场收益 (否则 censored)
        out[f"ret_d{days}c"] = round((float(bars[i + days]["close"]) / entry - 1) * 100, 2)
    return out


def _trail_exit_labels(bars, i, entry, trail_pct, max_days=DEBUG_MAX_HOLD,
                       wave_start=None, wave_days=DEBUG_WAVE_DAYS):
    """峰值回撤出场标签 (路径依赖, 衡量"这笔行情给出多少可捕获空间")。

    为什么需要它: 固定持有 N 日衡量的是"第 N 日收盘的随机点位", 与入场质量关系弱
    (好行情可能因第 N 日恰好回调而记亏)。峰值回撤出场是**可操作**的固定规则 (追踪
    止盈): 涨越高、回撤触发越晚 → 捕获越多; 低峰值票在 peak≈entry 处就被小幅回撤
    扫出 → 天然滤掉"没肉"的票, 一路阴跌则跌满阈值出局 (自带止损)。

    口径: 入场=D+1 开盘 (entry); 从 D+1 起逐日 peak=max(peak, high_k),
      当 close_k <= peak*(1-trail_pct/100) → 当日收盘出场 (rsn=trail);
      始终未触发 → 窗口终点收盘出场 (rsn=expire)。

    wave_start (波次窗口口径, 2026-09-10 用户裁定): "第一条规则(找龙)"通过日的 bar
      索引; 给定时窗口终点 = wave_start + wave_days - 1 (默认 20 交易日), 而非
      i + max_days — 原点固定在行情起点, 让龙头股 (常见 50%+ 涨幅) 有充分时间展开;
      **峰值仍从入场日 i+1 起追踪** (入场前涨幅买不到, 不能算进可捕获空间)。
      推论: 买入日被推后越久 → 剩余窗口越短、入场价越高 → 可捕获空间越小 → 自然淘汰;
      入场日已超出窗口终点 → rsn=late, ret 记 None。
    """
    n = len(bars)
    sf = f"{trail_pct:g}"
    out = {}
    if not entry or entry <= 0 or i + 1 >= n:
        return out
    wnd_end = (int(wave_start) + int(wave_days) - 1) if wave_start is not None \
        else i + max_days
    if i + 1 > wnd_end:                 # 入场日已超出波次窗口 (信号推后太多) → 淘汰
        out[f"rsn_tr{sf}"] = "late"
        return out
    last = min(wnd_end, n - 1)
    complete = wnd_end <= n - 1         # 窗口完整可见才给出场收益 (否则 censored)
    k = trail_pct / 100.0
    peak = mae_px = 0.0
    exit_day = exit_px = None
    reason = None
    for j in range(i + 1, last + 1):
        h = float(bars[j]["high"] or 0)
        lo = float(bars[j]["low"] or 0)
        c = float(bars[j]["close"] or 0)
        if h > 0:
            peak = h if peak == 0 else max(peak, h)
        if lo > 0:
            mae_px = lo if mae_px == 0 else min(mae_px, lo)
        if peak > 0 and c > 0 and c <= peak * (1.0 - k):
            exit_day, exit_px, reason = j - i, c, "trail"
            break
    if exit_day is None and complete:
        exit_day = last - i
        exit_px = float(bars[last]["close"] or 0)
        reason = "expire"
    if peak > 0:
        out[f"peak_tr{sf}"] = round((peak / entry - 1) * 100, 2)
    if mae_px > 0:
        out[f"mae_tr{sf}"] = round((mae_px / entry - 1) * 100, 2)
    if exit_day is not None and exit_px > 0:
        ret = round((exit_px / entry - 1) * 100, 2)
        out[f"ret_tr{sf}"] = ret
        out[f"day_tr{sf}"] = int(exit_day)
        out[f"rsn_tr{sf}"] = reason
        if peak > 0:
            pk = (peak / entry - 1) * 100
            out[f"cap_tr{sf}"] = round(ret / pk, 2) if pk > 0.5 else None
    return out


def _wave_labels(bars, i, wave_start, wave_days=DEBUG_WAVE_DAYS):
    """波次视角标签: 整波涨幅 (行情起点收盘 → 窗口内最高) + 入场推后天数。

    用途: 区分"票本身没肉"与"买晚了 / 出场没兑现" — wave_amp 大但 peak_tr 小 =
    行情有肉却没吃到 (出场问题或入场过晚)。
    """
    out = {}
    n = len(bars)
    if wave_start is None:
        return out
    ws = int(wave_start)
    if not 0 <= ws < n:
        return out
    wend = min(ws + int(wave_days) - 1, n - 1)
    base = float(bars[ws]["close"] or 0)
    wmax = max((float(bars[k]["high"] or 0) for k in range(ws, wend + 1)), default=0)
    if base > 0 and wmax > 0:
        out["wave_amp"] = round((wmax / base - 1) * 100, 2)
    out["entry_lag"] = int(i) - ws      # 入场决策日相对波次起点的推后天数
    return out


def _dragon_debug_labels(bars, i, entry, wave_start=None):
    """本策略调试标签全集 (固定持有 + 多档峰值回撤 + 波次视角)。"""
    out = {}
    if not entry or entry <= 0:
        return out
    if DEBUG_HOLD_DAYS:
        out.update(_fixed_hold_labels(bars, i, entry, days=DEBUG_HOLD_DAYS))
    for t in DEBUG_TRAILS:
        out.update(_trail_exit_labels(bars, i, entry, trail_pct=t,
                                      wave_start=wave_start))
    out.update(_wave_labels(bars, i, wave_start))
    return out


def _dragon_sample_feats(bars, i, code, stock_info=None, wave_start=None):
    """框架通用特征/标签 + 本策略调试标签 (仅 probe 调用; 判定路径不读)。"""
    base = _probe_sample_feats(bars, i, code, stock_info=stock_info)
    labels = base.get("labels") or {}
    if labels.get("entry_d1o"):
        labels.update(_dragon_debug_labels(bars, i, labels["entry_d1o"], wave_start))
    base["labels"] = labels
    return base


def run_backtest_dragon_callback(bars, entry_idx, entry_price, hold_days=None,
                                 stop_loss=None, board_type="main", stop_at_idx=None, **params):
    """龙回头出场 — 2026-09-26 P1-7b: 骨架上收 core.exit_engines.run_trail_stop。

    保留差异: 分段追踪 (trail_hi/lo + switch_pct)、峰值逃顶阈值、stop_at_idx 重放、
    **不启用 trig_prev 守卫** (与历史 dragon 口径逐笔一致; v1 才启用)。
    """
    p = {**DRAGON_CB_PARAMS, **(params or {})}
    hold_days = p["hold_days"] if hold_days is None else hold_days
    stop_loss = p["stop_loss"] if stop_loss is None else stop_loss
    if entry_price <= 0 or entry_idx >= len(bars):
        return None
    from app.market_cn.auto.core.exit_engines import run_trail_stop
    result = run_trail_stop(
        bars, entry_idx, entry_price,
        hold_days=hold_days, stop_loss=stop_loss,
        trails={"hi": p["trail_hi"], "lo": p["trail_lo"],
                "switch_pct": p["trail_switch_pct"]},
        board_type=board_type,
        peak_exit={"ret": p["peak_exit_ret"], "upper": p["peak_exit_upper"]},
        stop_at_idx=stop_at_idx,
        use_trig_prev=False,   # dragon 历史口径: 用当日 peak 的 trig, 无 trig_prev 守卫
        with_reason=True,
    )
    return result


# ================================================================
# StrategyBase 插件实现
# ================================================================

# 旧输出字段 (facade 兼容层精确对齐; 多键/少键都会破坏逐笔对数)
_LEGACY_FIELDS = (
    "code", "board", "path", "path_label", "lu_date", "pullback_days", "signal_date",
    "signal_chg", "signal_vol_r", "signal_price", "entry_vol_r", "buy_mode",
    "gap_from_peak", "streak_h", "lu_gain20", "d0_vs_ma20", "pullback_depth", "yin_ratio",
    "tech_score", "tech_rsi", "tech_roc", "tech_psy",
)


def _signal_to_legacy_dict(sig: Signal, code: str) -> dict:
    """Signal → 旧 dragon_cb_today_d0_signals 的 dict 形态。"""
    ex = sig.extra or {}
    return {k: ex.get(k) for k in _LEGACY_FIELDS if k not in ("code", "board", "path", "path_label", "signal_date")} | {
        "code": code,
        "board": ex.get("board"),
        "path": "dragon_callback",
        "path_label": "龙回头",
        "signal_date": sig.time,
    }


def _dragon_gates(p, cand, d0, probe=None):
    """龙回头门 0a/0b → 1 → 2(gap) → 3 → 4 → 5 → 6(拐点OR) → 7 → 8a~8c —— **唯一实现**。

    2026-10-06: 此前生产 `scan_signals` 循环内逐门内联、展示层 `_signal` 又抄一份 ——
    两套实现可给出相反结果（记忆点名的雷区）。现两侧只负责**各自准备特征**:
      生产 = 全量 bars 索引 (lu_idx/i)；展示层 = 递归 ring 窗口 + 冻结 lu 记录。
    判定顺序/阈值/短路语义全部集中于此；改门只需改这一处。

    Args:
        cand: lu 候选特征 {"close","dragon","streak_h","gain20","gap"}
        d0:   D0 特征 {"c","last_chg","rsi_val","d0_vs_ma20","depth","yin_ratio"}
        probe: 可选 TRACE 回调 (stage, **kw)，仅生产 scan_signals 传。
    Returns:
        (pass: bool, reason: str|None)
    """
    # 0a/0b: 涨停收盘有效 且 D0 收盘仍低于涨停收盘（仍在回调中）
    #   ⚠ 不 trace —— 与旧实现一致（原代码这两门在 _tr 定义之前）
    if not (cand["close"] > 0) or not (d0["c"] < cand["close"]):
        return False, "0ab"
    # 1 找龙（滑动窗口内涨停占比）
    if not cand["dragon"]:
        if probe:
            probe("dragon")
        return False, "dragon"
    # 2 gap ∈ [gap_min, gap_max]
    if cand["gap"] < p["gap_min"] or cand["gap"] > p["gap_max"]:
        if probe:
            probe("gap")
        return False, "gap"
    # 3 连板高度
    if cand["streak_h"] < p["min_streak"]:
        if probe:
            probe("streak")
        return False, "streak"
    # 4 前期热度（None → 拒）
    if cand["gain20"] is None or cand["gain20"] < p["lu_gain20_min"]:
        if probe:
            probe("lu_gain20")
        return False, "lu_gain20"
    # 5 强势回调（rsi 未计算时放行，不误杀）
    rsi_val = d0["rsi_val"]
    if rsi_val is not None and rsi_val < p["rsi6_min"]:
        if probe:
            probe("rsi")
        return False, "rsi"
    # 6 拐点（三腿 OR）
    ma20 = d0["d0_vs_ma20"]
    cond_ma20 = ma20 is not None and p["ma20_lo"] <= ma20 < p["ma20_hi"]
    cond_depth = d0["depth"] <= p["depth_max"]
    cond_yin = d0["yin_ratio"] < p["yin_ratio_max"]
    if not (cond_ma20 or cond_depth or cond_yin):
        if probe:
            probe("turn", d0_vs_ma20=round(ma20, 2) if ma20 is not None else None,
                  pullback_depth=round(d0["depth"], 2), yin_ratio=round(d0["yin_ratio"], 2))
        return False, "turn"
    # 7 D0 企稳（严格 >）
    if d0["last_chg"] <= p["d0_chg_min"]:
        if probe:
            probe("d0_chg", signal_chg=round(d0["last_chg"], 2))
        return False, "d0_chg"
    # 8a/8b/8c 信号质量排除
    if d0["yin_ratio"] >= p["yin_ratio_exclude"]:
        if probe:
            probe("quality", reason="yin_ratio", yin_ratio=round(d0["yin_ratio"], 2))
        return False, "quality_yin"
    if rsi_val is not None and rsi_val < p["rsi6_exclude_lt"]:
        if probe:
            probe("quality", reason="rsi6_lt", rsi6=round(rsi_val, 1))
        return False, "quality_rsi"
    if ma20 is not None and ma20 < p["d0_ma20_exclude_lt"]:
        if probe:
            probe("quality", reason="d0_ma20_lt", d0_vs_ma20=round(ma20, 2))
        return False, "quality_ma20"
    if probe:
        probe("signal")
    return True, None


@register
class DragonCallbackStrategy(StrategyBase):
    key = STRATEGY_KEY
    name = STRATEGY_LABEL
    prefilter_anchor = "limit_up"     # U1~U4 锚定涨停日 (D0 缩量小阴日评估会误杀)
    scan_spec = ScanSpec(kind="daily_close", after_events=("daily_1d", "lhb"))
    default_params = dict(DRAGON_CB_PARAMS)
    # 探针 day-stage 归属 (越靠后=离信号越近; 引擎/回测钩子经 getattr 读取)
    # 展示阶段表（展示层只按此表呈现，不认识门细节）
    stages = (
        Stage("ready", "龙回头·准备", realtime="09:25"),
        Stage("exec", "D1开盘买入"),
        Stage("exit", "出场结算"),
    )
    # ⚠ 必须与 scan_signals 的 use_tech_score 默认(True)一致: 置 False 会让
    #   rsi_val=None ⇒ 门5(rsi6>=45)放行 ⇒ 全市场多日 46 笔 vs 生产 30 笔 (多 34%)。
    use_tech_score = True

    # ---- 截窗警告 (原 `resume_supported` 声明, 2026-10-06 P6 随展示层断点机制移除) ----
    # ⚠️ `enumeration=limit_up`: 候选 = **窗口内**的历史涨停日, 且 lu_idx 是绝对索引
    #    ⇒ 截窗会削减候选本身 (实测 e2e 465→161), 断点**救不回**。
    #    要启用必须先持久化涨停日列表 (另案, 需授权改 `_candidate_lus` 语义)。

    # ---- 信号判定 ----
    def scan_signals(self, bars, code, *, as_of=None, ctx=None, limit_ups=None,
                     use_tech_score=True, probe=None, **params):
        """龙回头 D0 信号 ("方案2") → Signal (至多1笔)。

        as_of=k: 只用 bars[:k+1] 判定; limit_ups: 预计算涨停索引 (回测优化, None 则现算)。
        **params 覆盖 DRAGON_CB_PARAMS 键 (dragon_scan 传 params=dict)。
        probe: 调试探针 (probe.Probe / 同签名 shim), None=零开销 — TRACE 式记录
        每候选各判定步落点 (debug 形态, 数据存档供 AI 分析, 与判定行为无关)。
        """
        p = self.params(params or None)
        if as_of is not None:
            bars = bars[:as_of + 1]
        result = []
        n = len(bars)
        if n < 3:
            return result
        i = n - 1
        if i < 2:
            return result
        board_type = get_board_type(code)
        # 2026-09-29 审计修复 (P2): 涨停日换手率此前从未产出 —— quality_key 承诺的
        # "主要按换手热度"排序与展示层"换手N%"恒失效 (turnover_anchor 只有 break/v1
        # 在产)。口径与 v1 逐字一致 (流通/总股本由调用方经 params["stock_info"] 注入,
        # 策略不做 IO), 锚点 = 涨停日 lu_idx 的成交量。
        _si = params.get("stock_info") or {}
        circ = float(_si.get("circ_shares") or 0)
        total = float(_si.get("total_shares") or 0)

        d0 = bars[i]
        prev_c = bars[i - 1]["close"]
        if prev_c <= 0:
            return result
        last_chg = (d0["close"] / prev_c - 1) * 100
        prev_vol = bars[i - 1]["volume"]
        entry_vol_r = d0["volume"] / prev_vol if prev_vol > 0 else 0

        closes = [bars[j]["close"] for j in range(i + 1)]

        # ── tech_score 加分制 (仅参考输出; RSI 值供质量排除使用) ──
        # 2026-09-23: 逐字搬入 _tech_block (与 YAML signal.fields 共用同一实现)
        score, rsi_val, roc, psy = _tech_block(closes, use_tech_score)

        # ── 方案2 主判定 ──
        for lu_idx in (limit_ups if limit_ups is not None else find_limit_ups(bars[:i], board_type)):
            lu_close = bars[lu_idx]["close"]
            if lu_close <= 0:
                continue

            # 当前日(i)收盘必须仍低于涨停收盘 (仍在回调中)
            if bars[i]["close"] >= lu_close:
                continue

            pullback_days = i - lu_idx
            gap_from_peak = pullback_days   # 同一值 (Step2 与探针记录共用旧字段名)

            # ── 龙强度度量 (①连板高度 ②前期热度; 全部只用<=D0收盘数据, as-of 安全) ──
            # 置于 Step1 之前: 各判定门与探针 trace 共用 (纯计算, 判定行为不变)
            # 2026-09-23: 逐字搬入 _lu_streak / _lu_gain20 (与 YAML 门表共用同一实现)
            streak_h = _lu_streak(bars, lu_idx, board_type)
            lu_gain20 = _lu_gain20(bars, lu_idx)

            # 探针 shim (TRACE 宏语义): trace 统一分发（ctx["_trace"].note 优先 /
            # probe.trace 兼容）⇒ M1 采样可不依赖 probe 对象（影子对拍: test_m1_sampler）
            _sink = (ctx or {}).get("_trace")
            if probe is not None or _sink is not None:
                def _tr(stage, **kw):
                    (_sink.note if _sink is not None else probe.trace)(
                        stage, code=code, d0_date=str(bars[i]["time"])[:10],
                                lu_date=str(bars[lu_idx]["time"])[:10],
                                gap_from_peak=gap_from_peak, streak_h=streak_h,
                                lu_gain20=round(lu_gain20, 1) if lu_gain20 is not None else None,
                                **kw)
            else:
                _tr = None

            # ── Step1~8c: 门判定**委托 `_dragon_gates`**（唯一实现，与展示层共用）──
            #   特征在此按全量 bars 索引准备；展示层侧按 ring 窗口 + 冻结 lu 记录准备。
            d0_vs_ma20 = _d0_vs_ma20(bars, i)
            pullback_depth = _pullback_depth(bars, lu_idx, i)
            yin_ratio = _yin_ratio(bars, lu_idx, i)
            ok, _reason = _dragon_gates(p, {
                "close": lu_close,
                "dragon": _dragon_found(bars, lu_idx, board_type,
                                        p["dragon_ratio"], p["dragon_windows"]),
                "streak_h": streak_h, "gain20": lu_gain20, "gap": gap_from_peak,
            }, {
                "c": d0["close"], "last_chg": last_chg, "rsi_val": rsi_val,
                "d0_vs_ma20": d0_vs_ma20, "depth": pullback_depth,
                "yin_ratio": yin_ratio,
            }, probe=_tr)
            if not ok:
                continue
            result.append(Signal(
                code=code,
                time=bars[i]["time"],
                score=0,   # 历史口径: 方案2无评分体系, 库内 score 恒0 (与旧 dragon_scan 后处理一致)
                price=round(d0["close"], 3),
                label="龙回头",
                extra={
                    "board": get_board_name(code),
                    "lu_date": bars[lu_idx]["time"],
                    "pullback_days": pullback_days,
                    "signal_chg": round(last_chg, 2),
                    "signal_vol_r": round(entry_vol_r, 2),
                    "signal_price": round(d0["close"], 3),
                    "entry_vol_r": round(entry_vol_r, 2),
                    "buy_mode": "next_open",
                    "gap_from_peak": gap_from_peak,
                    "streak_h": streak_h,
                    "lu_gain20": round(lu_gain20, 1) if lu_gain20 is not None else None,
                    "d0_vs_ma20": round(d0_vs_ma20, 2) if d0_vs_ma20 is not None else None,
                    "pullback_depth": round(pullback_depth, 2),
                    "yin_ratio": round(yin_ratio, 2),
                    "turnover_anchor": round(bars[lu_idx]["volume"] / circ * 100, 2) if circ > 0 else None,
                    "turnover_anchor_total": round(bars[lu_idx]["volume"] / total * 100, 2) if total > 0 else None,
                    "tech_score": score,
                    "tech_rsi": round(rsi_val, 1) if rsi_val else None,
                    "tech_roc": round(roc, 1) if roc else None,
                    "tech_psy": round(psy, 1) if psy else None,
                },
            ))
            break
        return result

    # ================================================================
    # 递推展示层契约 (2026-10-06: 折叠契约已并入生产 StrategyBase)
    #
    # 门判定  → `_dragon_gates`（全量路径 scan_signals 与递推路径共用一份）
    # 出场    → `run_backtest_dragon_callback`（core.exit_engines 唯一实现）
    # 原语    → `_lu_streak/_lu_gain20/_dragon_found/_tech_block` + 公共层 is_limit_up
    # 递推量  → RSI6 增量二元组 (逐位等于全量) + MACD 锚 (仅 tech_score 展示用)
    #
    # state 结构: {abs_i, lus(涨停日冻结记录), ring(近 RING 根), rsi6, macd 锚, board}
    # ================================================================
    def init_state(self, code: str, bars: list[dict]) -> dict:
        if len(bars) < 30:
            raise InsufficientHistory(f"{code}: bars={len(bars)} < 30")
        bt = get_board_type(code)
        ring = [{"t": str(b["time"])[:10], "o": float(b["open"]), "h": float(b["high"]),
                 "l": float(b["low"]), "c": float(b["close"]), "v": float(b["volume"])}
                for b in bars[-RING:]]
        lus = [self._freeze_lu(bars, k, bt)
               for k in range(1, len(bars))
               if is_limit_up(float(bars[k]["close"]), float(bars[k - 1]["close"]), bt)]
        closes = [float(b["close"]) for b in bars]
        n = len(bars)
        if n > RING:
            anchor = list(macd_state(closes, upto=n - RING - 1))
        else:
            # ring 尚未滑动（ring==全序列）：朴素播种等价锚 (c0, c0, 0)
            anchor = [closes[0], closes[0], 0.0]
        return {"v": 1, "board": bt, "abs_i": n - 1, "lus": lus, "ring": ring,
                "rsi6": rsi_init(closes, 6), "macd": anchor}

    @staticmethod
    def _freeze_lu(bars, k, bt):
        """lu 形成当时冻结属性（as-of 安全：只读 ≤k 的数据）。"""
        return {
            "idx": k, "date": str(bars[k]["time"])[:10],
            "close": float(bars[k]["close"]), "volume": float(bars[k]["volume"]),
            "streak_h": _lu_streak(bars, k, bt),
            "gain20": _lu_gain20(bars, k),
            "dragon": _dragon_found(bars, k, bt, DRAGON_CB_PARAMS["dragon_ratio"],
                                    DRAGON_CB_PARAMS["dragon_windows"]),
        }

    def step(self, state: dict, bar: dict) -> dict:
        ring = list(state["ring"])
        prev_c = ring[-1]["c"]
        rec = {"t": str(bar["time"])[:10], "o": float(bar["open"]), "h": float(bar["high"]),
               "l": float(bar["low"]), "c": float(bar["close"]), "v": float(bar["volume"])}
        abs_i = state["abs_i"] + 1
        lus = list(state["lus"])
        # lu 冻结：新 bar 是否涨停（用 ring 尾作 prev）；属性从 ring+新bar 临时视图算
        if is_limit_up(rec["c"], prev_c, state["board"]):
            tmp = [{"time": r["t"], "open": r["o"], "high": r["h"], "low": r["l"],
                    "close": r["c"], "volume": r["v"]} for r in ring] + \
                  [{"time": rec["t"], "open": rec["o"], "high": rec["h"], "low": rec["l"],
                    "close": rec["c"], "volume": rec["v"]}]
            lu = self._freeze_lu(tmp, len(tmp) - 1, state["board"])
            # ★ idx 必须是**绝对索引**（gap 门/clamp 语义锚）；tmp 是 ring 视图，
            #   属性值在 seed≥RING/2 约定下与全量逐位一致（见模块头）。
            lu["idx"] = abs_i
            lus.append(lu)
        ring = (ring + [rec])[-RING:]
        anchor = tuple(state["macd"]) if state.get("macd") else None
        if anchor is not None and len(state["ring"]) >= RING:
            # ring 滑出队首 → 锚推进（喂被滑出的那根）；未满时锚不动（ring[0] 未变）
            anchor = tuple(_anchor_step(anchor, [state["ring"][0]["c"]]))
        return {
            "v": 1, "board": state["board"], "abs_i": abs_i, "lus": lus, "ring": ring,
            "rsi6": rsi_step(state["rsi6"], prev_c, rec["c"]),
            "macd": list(anchor) if anchor is not None else None,
        }

    def probe(self, state: dict) -> list[tuple[str, float]]:
        r = state["ring"]
        return [(r[0]["t"], r[0]["c"]), (r[-1]["t"], r[-1]["c"])]

    def evaluate(self, state: dict, inp: DayInput, prev: Progress | None) -> list[Progress]:
        """递推路径判定（三分支共用：预处理 / 回测 / 实时）。"""
        p = self.params()
        bar, code = inp.bar, inp.code
        events: list[Progress] = []
        today_abs = state["abs_i"] + 1
        ring_full = state["ring"] + [{
            "t": str(bar.get("time", ""))[:10], "o": float(bar.get("open") or 0),
            "h": float(bar.get("high") or 0), "l": float(bar.get("low") or 0),
            "c": float(bar.get("close") or 0), "v": float(bar.get("volume") or 0)}]
        win = [{"time": r["t"], "open": r["o"], "high": r["h"], "low": r["l"],
                "close": r["c"], "volume": r["v"]} for r in ring_full]
        holding = None

        # ── 持仓出场（逐日重放, stop_at_idx=今日 = 旧 exit_decision）──
        if prev is not None and prev.stage == "exec" and prev.payload.get("buyable") \
                and bar.get("time", "") > prev.date:
            entry_abs = int(prev.payload["entry_abs"])
            d = today_abs - entry_abs + 1
            entry_pos = len(win) - d          # win 末位 = 今日
            r = run_backtest_dragon_callback(
                win, entry_pos, float(prev.payload["entry_price"]),
                board_type=state["board"], stop_at_idx=len(win) - 1)
            if r is not None and not r.get("open") and r["exit_day"] == d \
                    and r.get("exit_reason"):
                events.append(Progress(stage="exit", date=bar.get("time", ""), payload={
                    "entry_date": prev.payload["entry_date"],
                    "entry_price": prev.payload["entry_price"],
                    "exit_date": bar.get("time", ""), "exit_price": r["exit_price"],
                    "exit_day": r["exit_day"], "reason": r["exit_reason"],
                    "return_pct": r["return_pct"],
                    "peak_return_pct": r["peak_return_pct"],
                    "lu_abs": prev.payload.get("lu_abs"), "i_abs": prev.payload.get("i_abs"),
                }, next_realtime=None))
            else:
                holding = prev
        # ── D1 入场（gap 仅展示；09-07 起无 gap 过滤，open>0 即买）──
        elif prev is not None and prev.stage == "ready" \
                and bar.get("time", "") > prev.date:
            open_px = float(bar.get("open") or 0)
            pc = float(prev.payload.get("close_raw") or 0)
            gap = (open_px / pc - 1) * 100 if (open_px > 0 and pc > 0) else None
            ev = Progress(stage="exec", date=bar.get("time", ""), payload={
                "entry_date": bar.get("time", ""),
                "entry_price": round(open_px, 3), "d1_gap": None if gap is None else round(gap, 2),
                "buyable": open_px > 0,
                "entry_abs": today_abs,
                "lu_abs": prev.payload.get("lu_abs"), "i_abs": prev.payload.get("i_abs"),
                # P5-④ 前置 (2026-10-08): 买入当日 15:01 实时确认（持仓 / 当日出场）。
            }, next_realtime="15:01")
            events.append(ev)
            if open_px > 0:
                holding = ev

        # ── 新信号（每日至多 1；去重 ±4 对齐旧 backtest_stock）──
        if holding is None and prev is not None and prev.payload.get("i_abs") is not None:
            i_abs = int(prev.payload["i_abs"])
            lu_abs = int(prev.payload.get("lu_abs") or -999)
            if abs(today_abs - i_abs) <= 4 or abs(today_abs - lu_abs) <= 4:
                return events          # 去重拒绝（不消费 used range，旧行为）
        if holding is None and not code.startswith(("8", "4", "92")):
            sig = self._signal(code, state, ring_full, win, today_abs, bar,
                               inp.ctx or {}, p)
            if sig is not None:
                events.append(sig)
        return events

    def _signal(self, code, state, ring_full, win, today_abs, bar, ctx, p):
        """D0 盘后判定（门 0a..8c；胜者 = [i-7,i-5] 升序首个全门通过）。"""
        i = len(ring_full) - 1
        if i < 2 or ring_full[i - 1]["c"] <= 0:
            return None
        bt = state["board"]
        c_i = ring_full[i]["c"]
        last_chg = (c_i / ring_full[i - 1]["c"] - 1) * 100
        entry_vol_r = ring_full[i]["v"] / ring_full[i - 1]["v"] if ring_full[i - 1]["v"] > 0 else 0
        closes = [r["c"] for r in ring_full]                 # 含今日
        macd_triple = None
        # calc_macd 契约: n < slow+signal(35) → (None,None,None)，下游 macd 分支不激活
        if state.get("macd") and len(closes) >= 35:
            dif, dea, _ = macd_core(closes, anchor=tuple(state["macd"]))
            macd_triple = (dif, dea, [2.0 * (d - e) for d, e in zip(dif, dea)])
        # rsi6 增量口径（播种起点=origin；use_tech_score=False 时置 None → 门 5/8b 放行）
        rsi_val = rsi_value(rsi_step(state["rsi6"], ring_full[i - 1]["c"], c_i)) \
            if state.get("rsi6") else None
        score, rsi_val_t, roc, psy = _tech_block(
            closes, self.use_tech_score, macd_triple=macd_triple, rsi_val=rsi_val)
        if not self.use_tech_score:
            rsi_val = None

        for rec in state["lus"]:
            gap = today_abs - rec["idx"]
            if gap > p["gap_max"]:
                continue                     # 升序：更旧的 gap 更大，可直接剪枝
            if gap < p["gap_min"]:
                continue                     # 更新的还没到回调窗（gap 太小）
            g20 = rec["gain20"]
            # 6 拐点三腿的输入（回调期特征，由 ring 窗口算出）
            lu_rel = len(ring_full) - 1 - gap          # lu 在 ring_full 的相对位置
            seg = ring_full[lu_rel + 1:]
            depth = (min(r["l"] for r in seg) / rec["close"] - 1) * 100 if seg else 0.0
            pb_total = gap
            pb_yin = sum(1 for r in seg if r["c"] < r["o"])
            yin_ratio = pb_yin / pb_total if pb_total > 0 else 1.0
            d0_vs_ma20 = None
            if len(closes) >= 20:
                ma20 = sum(closes[-20:]) / 20
                d0_vs_ma20 = (c_i / ma20 - 1) * 100 if ma20 > 0 else None
            # 门 0a/0b · 1 · 2 · 3 · 4 · 5 · 6 · 7 · 8a~8c —— **委托 `_dragon_gates`**
            #   （生产唯一实现；本侧只负责用 ring 窗口准备同样的特征）
            ok, _reason = _dragon_gates(p, {
                "close": rec["close"], "dragon": rec["dragon"],
                "streak_h": rec["streak_h"], "gain20": g20, "gap": gap,
            }, {
                "c": c_i, "last_chg": last_chg, "rsi_val": rsi_val,
                "d0_vs_ma20": d0_vs_ma20, "depth": depth, "yin_ratio": yin_ratio,
            })
            if (tr := (ctx or {}).get("_trace")) is not None:   # 门原因通道（契约约定）
                tr.gate(ok, _reason, cand=rec["date"], date=str(bar.get("time", ""))[:10])
            if not ok:
                continue
            # ---- 胜者：构造 Signal ----
            si = ctx.get("stock_info") or {}
            circ = float(si.get("circ_shares") or 0)
            total = float(si.get("total_shares") or 0)
            extra = {
                "board": get_board_name(code),
                "lu_date": rec["date"], "pullback_days": gap,
                "signal_chg": round(last_chg, 2),
                "signal_vol_r": round(entry_vol_r, 2),
                "signal_price": round(c_i, 3), "entry_vol_r": round(entry_vol_r, 2),
                "buy_mode": "next_open", "gap_from_peak": gap,
                "streak_h": rec["streak_h"],
                "lu_gain20": round(g20, 1) if g20 is not None else None,
                "d0_vs_ma20": round(d0_vs_ma20, 2) if d0_vs_ma20 is not None else None,
                "pullback_depth": round(depth, 2), "yin_ratio": round(yin_ratio, 2),
                "turnover_anchor": round(rec["volume"] / circ * 100, 2) if circ > 0 else None,
                "turnover_anchor_total": round(rec["volume"] / total * 100, 2) if total > 0 else None,
                "tech_score": score,
                "tech_rsi": round(rsi_val, 1) if rsi_val else None,
                "tech_roc": round(roc, 1) if roc else None,
                "tech_psy": round(psy, 1) if psy else None,
            }
            return Progress(stage="ready", date=str(bar.get("time", "")), payload={
                "price": round(c_i, 3), "score": 0, "label": "龙回头",
                "extra": extra, "close_raw": c_i,
                "lu_abs": rec["idx"], "i_abs": today_abs,
            }, next_realtime="09:25")
        return None

    # ---- D1 竞价处置 ----
    def entry_decision(self, row, snap=None, **params):
        """D1 开盘一律可买 (2026-09-07 移除 gap∈[-3,+2] 范围过滤)。

        gap 仅记录在 reason 供用户参考, 高开/低开由用户自行取舍。"""
        if not snap:
            return EntryDecision(False, "无竞价快照")
        open_px = float(snap.get("open") or snap.get("last") or 0)
        if open_px <= 0:
            return EntryDecision(False, "开盘价缺失")
        prev_close = float(snap.get("previousClose") or row.get("signal_price") or 0)
        if prev_close <= 0:
            return EntryDecision(False, "昨收缺失")
        gap = (open_px / prev_close - 1) * 100
        tag = "高开" if gap > 2 else ("低开" if gap < -3 else "")
        return EntryDecision(True, f"gap={gap:.2f}% 可买{tag}")

    # ---- 15:00 收盘确认 ----
    def confirm_decision(self, row, snap=None, **params):
        """无确认步骤: 买入日收盘直接持仓 (出场由收盘重放判定)。

        snap={"series":[...当日快照序列]}; d1_chg 按 signal_price 基准 (旧 evaluate_confirm 口径)。
        返回 None = 无法判定 (快照缺失), monitor 不做状态转移。
        """
        series = (snap or {}).get("series") if isinstance(snap, dict) else None
        if not series:
            return None
        prev_close = float(row.get("signal_price") or 0)
        if prev_close <= 0:
            return None
        d1_chg = (float(series[-1]["last"] or 0) / prev_close - 1) * 100
        return ConfirmDecision(True, "ok", d1_chg=round(d1_chg, 2),
                               detail={"confirm": "ok", "confirm_strong": False})

    def quality_key(self, row):
        """方案2 质量排序: tech_score(参考) -> 涨停日换手率 (技术分无判别力, 主要按换手热度)。"""
        extra = row.get("extra") or {}
        return (extra.get("tech_score") or 0, extra.get("turnover_anchor") or 0)

    def initial_stop(self, code, entry_price):
        """-8%, 板块不分档 (与回测一致)。"""
        return round(entry_price * (1 + DRAGON_CB_PARAMS["stop_loss"] / 100), 3)

    # ---- 出场判定 ----
    def exit_decision(self, row, snap=None, **params):
        """收盘重放: 复用 run_backtest_dragon_callback (stop_at_idx=今日截断语义)。

        snap={"mode":"day_close","bars":[...],"entry_idx":int}; live 模式 → hold (硬止损在 monitor 主循环)。"""
        if not isinstance(snap, dict) or snap.get("mode") != "day_close":
            return ExitDecision("hold")
        bars = snap.get("bars")
        entry_idx = snap.get("entry_idx")
        entry_price = float(row.get("entry_price") or 0)
        if bars is None or entry_idx is None or entry_price <= 0:
            return ExitDecision("hold")
        board = get_board_type(row.get("code", ""))
        today_idx = len(bars) - 1
        # 2026-09-25: 出场参数同样走 params (与 backtest_stock 一致, 否则实盘/回测口径分叉)
        _ep = self.params(None)
        r = run_backtest_dragon_callback(
            bars, entry_idx, entry_price, board_type=board,
            stop_at_idx=today_idx,
            hold_days=_ep.get("hold_days"),
            stop_loss=_ep.get("stop_loss"),
            trail_lo=_ep.get("trail_lo"),
            trail_hi=_ep.get("trail_hi"),
            trail_switch_pct=_ep.get("trail_switch_pct"),
            peak_exit_ret=_ep.get("peak_exit_ret"),
            peak_exit_upper=_ep.get("peak_exit_upper"),
        )
        if r and not r.get("open"):
            exit_idx = entry_idx + r["exit_day"] - 1
            if exit_idx == today_idx and r.get("exit_reason"):
                return ExitDecision("exit", reason=r["exit_reason"], price=float(r["exit_price"]))
        return ExitDecision("hold")

    # ---- 回测钩子 (2026-09-10 自 backtest.backtest_dragon_stock 逐字搬入, 对数零差异) ----


def _find_bar_idx(bars, date_str):
    """日期串 → bars 索引; 未找到返回 None (回测钩子用, 原 backtest 内联助手)。"""
    for i, b in enumerate(bars):
        if b["time"] == date_str:
            return i
    return None


# ================================================================
# YAML 门表适配器 (2026-09-23 门表化: 门表达式只调用本段函数)
# ----------------------------------------------------------------
# 纪律 (策略自包含): 策略私有门函数**只挂在本策略 key 命名空间**, 不进 core
# (core 只留 Ctx 24 行情原语 + 注册机制, 见 MEMORY 自包含纪律)。本段每个函数都是
# 上方"判定原语"的 Ctx 适配, **不含任何新逻辑** —— 门表与 python 判定共用同一份
# 实现, 杜绝"改 python 忘改 yaml"的双实现漂移。
#
# None 语义 (改这里等于改交易):
#   python 版对"数据不足"的处理分两种 ——
#     · rsi6 / ma20_dev: 数据不足时**放行** (不误杀) → 门函数返回放行值
#       (rsi6 → 100.0 恒过下界; ma20_dev → 0.0, 落在已关闭的 ma20 区间外);
#     · lu_gain20: 数据不足时**判 False** (continue) → 返回 -9999.0。
#   展示字段另用 *_val 变体透传 None (与 _LEGACY_FIELDS 逐字对齐)。
# ================================================================


def _ctx_closes(ctx: Ctx):
    """决策日因果切片 closes[:i+1] (与 scan_signals 的 closes 同口径)。"""
    return [ctx.bars[j]["close"] for j in range(ctx.i + 1)]


def dc_gap_days(ctx: Ctx) -> int:
    """回调天数 = i - lu_idx (python 版 gap_from_peak / pullback_days 同值)。

    与内核 Ctx.pullback_days() 差 1 (内核 = (i-1)-lu_idx): 门表统一用本函数, 勿混用。
    """
    return ctx.i - ctx.lu_idx


def dc_dragon(ctx: Ctx) -> bool:
    """找龙: 任一滑动窗口内涨停占比 >= dragon_ratio。"""
    p = ctx.params
    return _dragon_found(ctx.bars, ctx.lu_idx, ctx.board_type,
                         p.get("dragon_ratio", DRAGON_CB_PARAMS["dragon_ratio"]),
                         p.get("dragon_windows", DRAGON_CB_PARAMS["dragon_windows"]))


def dc_streak(ctx: Ctx) -> int:
    """涨停日连板高度 (>=1)。"""
    return _lu_streak(ctx.bars, ctx.lu_idx, ctx.board_type)


def dc_lu_gain20(ctx: Ctx) -> float:
    """涨停日 20 日涨幅 %; 数据不足 → -9999 (判 False, 与 python continue 同)。"""
    v = _lu_gain20(ctx.bars, ctx.lu_idx)
    return v if v is not None else -9999.0


def dc_lu_gain20_val(ctx: Ctx):
    """展示字段版 (None 透传, 对齐 _LEGACY_FIELDS.lu_gain20)。"""
    return _lu_gain20(ctx.bars, ctx.lu_idx)


def dc_depth(ctx: Ctx) -> float:
    """回调期最深跌幅 % (负)。"""
    return _pullback_depth(ctx.bars, ctx.lu_idx, ctx.i)


def dc_yin(ctx: Ctx) -> float:
    """回调期阴线占比 0~1。"""
    return _yin_ratio(ctx.bars, ctx.lu_idx, ctx.i)


def dc_ma20_dev(ctx: Ctx) -> float:
    """D0 收盘相对 MA20 偏离 %; 数据不足 → 0.0 (放行)。"""
    v = _d0_vs_ma20(ctx.bars, ctx.i)
    return v if v is not None else 0.0


def dc_ma20_dev_val(ctx: Ctx):
    """展示字段版 (None 透传)。"""
    return _d0_vs_ma20(ctx.bars, ctx.i)


def dc_rsi6(ctx: Ctx) -> float:
    """D0 RSI6 (与 scan_signals 同一 app.utils.indicators.rsi, 非内核 Ctx.rsi —— 算法不同)。

    数据不足 → 100.0 (放行, 与 python 版 rsi_val=None 时跳过该门同语义)。
    """
    v = rsi(_ctx_closes(ctx), period=6)
    return v if v is not None else 100.0


def dc_tech_score(ctx: Ctx) -> int:
    """技术面加分 (仅展示, 不参与过滤)。"""
    return _tech_block(_ctx_closes(ctx))[0]


def dc_tech_rsi(ctx: Ctx):
    v = rsi(_ctx_closes(ctx), period=6)
    return round(v, 1) if v else None


def dc_tech_roc(ctx: Ctx):
    v = calc_roc(_ctx_closes(ctx), period=5)
    return round(v, 1) if v else None


def dc_tech_psy(ctx: Ctx):
    v = calc_psy(_ctx_closes(ctx), period=10)
    return round(v, 1) if v else None


register_strategy_funcs(
    "dragon_callback",
    {
        "gap_days": dc_gap_days,
        "dragon": dc_dragon,
        "streak": dc_streak,
        "lu_gain20": dc_lu_gain20,
        "lu_gain20_val": dc_lu_gain20_val,
        "depth": dc_depth,
        "yin": dc_yin,
        "ma20_dev": dc_ma20_dev,
        "ma20_dev_val": dc_ma20_dev_val,
        "rsi6": dc_rsi6,
        "tech_score": dc_tech_score,
        "tech_rsi": dc_tech_rsi,
        "tech_roc": dc_tech_roc,
        "tech_psy": dc_tech_psy,
    },
    offset=set(),
    # 决策日依赖 (展示端 T-1 夜预计算依据): 只读涨停日及之前 → 0; 读到 D0(i) → 1
    d0={"dragon": 0, "streak": 0, "lu_gain20": 0, "lu_gain20_val": 0,
        "gap_days": 1, "depth": 1, "yin": 1, "ma20_dev": 1, "ma20_dev_val": 1,
        "rsi6": 1, "tech_score": 1, "tech_rsi": 1, "tech_roc": 1, "tech_psy": 1},
)


# ---- exit_modes 注册 (2026-09-26 P1-9 层反转): core 不再 import 本模块 ----
def _exit_combo(bars, entry_idx, entry_price, *, code, board_type, params, diag):
    """龙回头 combo 出场 (trail/stop/max_hold/peak/signal) — 供 YAML exit.mode=combo。"""
    return run_backtest_dragon_callback(bars, entry_idx, entry_price, board_type=board_type)


from app.market_cn.auto.core.exit_modes import register_exit as _register_exit
_register_exit("combo", _exit_combo)


# ================================================================
# 门表回测编排 (2026-09-28 分层改造: 自 core/runtime/evaluate.py **纯搬运**下沉)
# ----------------------------------------------------------------
# 为什么搬回来: 编排是策略的一部分 (枚举顺序/去重键/展示字段集/入场腿), 放在 core
# 会让"改 g56 口径"变成改架构层, 且 core 反过来惰性 import strategies.* (层反转)。
# 自注册到 core/runtime/flows 注册表 → core 只查表, 未登记即 fail-fast (不再静默落 v1)。
# ⚠ 搬运要求: 签名与语义**逐字不变**; 逐笔等价回归见
#    analysis_output/auto架构分层_20260928.md
# ================================================================

# ⚠ 本模块顶层用的是 **core.market** 的 find_limit_ups/get_board_name
#    (生产链口径); 该编排必须与 core/runtime/evaluate.py 原实现同源 →
#    显式取 **core.market** 并别名导入。两套同名实现不可混用。
from typing import Any, Dict, List, Tuple

from app.market_cn.auto.core.exit_modes import run_exit
from app.market_cn.auto.core.filters import unified_prefilter
from app.market_cn.auto.core.market import find_limit_ups as _core_find_limit_ups
from app.market_cn.auto.core.market import get_board_name as _core_get_board_name
from app.market_cn.auto.core.runtime.flows import register_enum_flow

def _backtest_limit_up(bars, code, spec, ev, board_type, stock_info, use_prefilter):
    """门表版龙回头全历史回测，返回 trades 列表（与 dragon_callback.backtest_stock 逐笔等价）。

    编排逐字镜像 backtest_stock：枚举候选日 → 廉价预筛 → 资格门 → 遍历 lu_idx 取首个通过
    → 去重±4 → unified_prefilter → 入场=entry_modes(close) → 出场=exit_modes(exit.mode)。
    """
    n = len(bars)
    if n < 5:
        return []
    lu_all = _core_find_limit_ups(bars, board_type, spec.market_spec)
    pd_min = int(spec.params["min_pullback_days"])
    pd_max = int(spec.params["max_pullback_days"])
    params = spec.params
    trades: List[Dict[str, Any]] = []
    used_ranges: List[Tuple[int, int]] = []

    for i in range(2, n - 1):
        # 廉价预筛（数学必要条件超集；与 backtest_stock 同源）
        if not any(pd_min + 1 <= i - j <= pd_max + 1 for j in lu_all):
            continue
        # 资格门（与 lu_idx 无关）
        ok, _ = ev.evaluate_prefilter(bars, i, params)
        if not ok:
            continue
        # 遍历 lu_idx（升序；首个通过即出信号 —— 与 scan_signals 同语义）
        lu_cands = [j for j in lu_all if j < i]
        chosen = None
        for lu_idx in lu_cands:
            ok, _ = ev.evaluate_decision(bars, i, lu_idx, params)
            if ok:
                chosen = lu_idx
                break
        if chosen is None:
            continue
        lu_idx = chosen
        # 去重（±4 天内跳过）
        skip = False
        for (s, e) in used_ranges:
            if abs(i - s) <= 4 or abs(i - e) <= 4:
                skip = True
                break
        if skip:
            continue
        used_ranges.append((lu_idx, i))
        # U1~U4 预过滤（锚定涨停日）
        if use_prefilter and lu_idx > 0:
            ok, _ = unified_prefilter(bars, lu_idx, code, stock_info, spec.market_spec)
            if not ok:
                continue
        # 入场 = D0(反转日)收盘价
        d0 = bars[i]
        d1 = bars[i + 1]
        entry_price = float(d0["close"] or 0)
        if entry_price <= 0:
            continue
        result = run_exit(spec.exit.get("mode", "combo"), bars=bars, entry_idx=i,
                          entry_price=entry_price, code=code, board_type=board_type,
                          params=spec.params, diag={})
        if not result:
            # R1: 数据结束未平 → 末日收盘平仓（本路径入场=D0 收盘，entry_idx=i）
            result = data_end_close(bars, i, entry_price)
        if not result:
            continue
        # 信号附带字段（与 _signal_to_legacy_dict + scan_signals.extra 完全一致）
        d_prev = bars[i - 1]
        d_prev2 = bars[i - 2]
        prev_chg = (float(d_prev["close"]) / float(d_prev2["close"]) - 1) * 100 \
            if float(d_prev2.get("close") or 0) > 0 else 0.0
        prev_vol = float(d_prev["volume"]) / float(d_prev2["volume"]) \
            if float(d_prev2.get("volume") or 0) > 0 else 0.0
        sig = {
            "code": code,
            "board": _core_get_board_name(code, spec.market_spec),
            "path": spec.key,
            "path_label": spec.meta.get("name", spec.key),
            "lu_date": bars[lu_idx]["time"],
            "pullback_days": (i - 1) - lu_idx,
            "signal_date": d0["time"],
            "signal_chg": round(prev_chg, 2),
            "signal_vol_r": round(prev_vol, 2),
            "signal_price": round(entry_price, 3),
            "entry_vol_r": round(prev_vol, 2),
            "buy_mode": "signal_close",
        }
        trades.append({
            **sig,
            "entry_date": d0["time"],
            "entry_price": round(entry_price, 3),
            "buy_mode": "signal_close",
            "d1_gap": round((float(d1["open"]) / entry_price - 1) * 100, 2),
            "d1_change": round((float(d1["close"]) / entry_price - 1) * 100, 2),
            **result,
        })
    return trades


# ================================================================
# 枚举方式 B：day（逐日候选 → D0 信号 → 次日开盘入场）
#   day_flow=v1     : 镜像 v1.backtest_stock（见 tmp/_v1_equivalence.py 验收）
#   day_flow=relay3 : 镜像 relay3.backtest_stock（见 tmp/_relay3_equivalence.py 验收）
# ================================================================

register_enum_flow("limit_up", _backtest_limit_up)

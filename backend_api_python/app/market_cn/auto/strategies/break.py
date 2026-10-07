"""strategies/break.py — 断板接力策略 (StrategyBase 插件实现, Phase 2 迁移)

实现已迁移至本文件; core.break_today_d0_signals / _break_signal_at 为 facade 转发。

入场 (D0 盘后扫描 → D1 竞价):
  连板≥2 → 断板期(≤max_break_gap天) → 确认日=断板期最后一天 → D1 开盘买入
  断板期检查 5a~5f: 低点不破涨停日开盘 / 缩量1.2~2.0x / 首断日涨跌+gap 区间 /
  回撤不破限 / 确认日增强过滤(三通道OR: 企稳[0,2) | 均量比≥1.4 | 前20日涨幅≥30)
  竞价: 无 gap 过滤 (恒可买, gap 判定交给 D1 数据)
评分 = 50 + 确认日涨幅% × 3, clip [0,100] —— **展示分, 非质量分**
  (2026-09-24 由 int(confirm_chg)+10 归一化而来; 原值域 0~17 与它策略不可比。
   实测 corr=-0.084 无判别力, 且 daily_limit=5 从不触发 ⇒ 只作展示/tie-break, 依据见 SCORE_* 处)
  换手率门 (2026-09-11): 确认日换手 < turnover_min (config params, None=关) → 剔除
  U1~U4: prefilter_anchor='signal' (锚定确认日=末根bar; 连板≥2已隐含U4)

出场 (收盘价判定, monitor break 分支 / run_backtest_breakbuy 语义):
  止损 main-8%/gem-10% / 追踪止损(自入场峰值, 需 ret>0) / 峰值逃顶(ret>10%+上影>40%+收盘<high*0.98) / 到期 main20/gem15天

易错点:
  - 确认日 = 断板期最后一天 (break_idx+break_days-1 == D0), 不是首断板日;
  - _break_signal_at 的 5c/5d 上界 (+8%/+5%) 是硬编码, 与 BOARD_PARAMS 无关 — 勿"配置化";
  - exit 是收盘价口径 (close 判定), 与 v1 的 low 触及口径不同 — 勿混用;
  - 追踪止损要求 ret>0 (盈利中才追踪), 与止损分支互斥由 ret<=stop 先拦。
"""
from __future__ import annotations

from app.market_cn.auto.core.market import (
    find_limit_ups, get_board_name, get_board_type, is_limit_up,
)
from app.market_cn.auto.strategies import register
from app.market_cn.auto.core.runtime.functions import Ctx, register_strategy_funcs
from app.market_cn.auto.strategies.base import (
    ConfirmDecision, EntryDecision, ExitDecision, ScanSpec, Signal, StrategyBase,
)
# 递推展示层契约 (2026-10-06): 折叠契约已并入生产 StrategyBase（单继承）。
from app.market_cn.auto.core.present.contract import (  # noqa: E402
    DayInput, InsufficientHistory, Progress, Stage,
)

STRATEGY_KEY = "break"
STRATEGY_LABEL = "断板"

# 板块参数 (策略专用出场参数, 唯一定义在本文件; backtest.py 旧份已删, 2026-09-10 晚下沉; config.json params 可覆盖其键)
BOARD_PARAMS = {
    # exit_mode: 2026-10-07 由 "sweet" → "legacy" (用户裁定 A: 回测默认对齐实盘)。
    #   sweet 与峰值逃顶**互斥** (`_run_backtest_breakbuy`: `if exit_mode != "sweet"
    #   and ret > 10`) ⇒ 默认 sweet 时回测缺实盘那条峰值逃顶腿, 收益口径不可外推。
    #   legacy = 止损/追踪/峰值逃顶/到期, 与 `BreakStrategy.exit_decision` 逐条镜像。
    #   ⚠ config.json 的 break.winrate=71.3 是 **sweet 口径**产物, 需重跑刷新。
    "main": {"stop_loss": -8.0, "trailing_stop": -6.0, "take_profit": 15.0, "hold_days": 7,  # 20→7: 2026-09-22 出场研究定稿(时间上限先行)
             "exit_mode": "legacy", "sweet_pctb": 95.0, "sweet_pctb_core": 100.0,  # E3: 甜点区出场(仅 exit_mode=sweet 时生效); 核心/高板通道阈值100(让利润跑)
             "vol_min": 1.2, "vol_max": 2.0, "drawdown_max": -10,
             "enhance_filter": True, "confirm_chg_min": 0.0, "confirm_chg_max": 2.0,
             "vol_r_or_min": 1.4, "pre20_min": 30.0, "ma_bull_filter": False,
             "first_break_gap_min": 0, "first_break_chg_min": 0.0},
    "gem_star": {"stop_loss": -10.0, "trailing_stop": -8.0, "take_profit": 20.0, "hold_days": 7,  # 15→7: 同上
                 "exit_mode": "legacy", "sweet_pctb": 95.0, "sweet_pctb_core": 100.0,   # 同上 (2026-10-07 sweet→legacy)
                 "vol_min": 1.2, "vol_max": 2.5, "drawdown_max": -15,
                 "enhance_filter": True, "confirm_chg_min": 0.0, "confirm_chg_max": 2.0,
                 "vol_r_or_min": 1.4, "pre20_min": 30.0, "ma_bull_filter": False,
                 "first_break_gap_min": 0, "first_break_chg_min": 0.0},
}

DEFAULT_PARAMS = dict(min_streak=2, max_break_gap=5,
                      # 确认日换手率前置门 (2026-09-11 ML归因反哺, 数学必要条件):
                      # turnover_sig(=D0成交量/流通股本*100, 与样本库 turnover_d0 同口径)
                      # < turnover_min 的候选直接剔除。None=关 (默认, 保持历史行为);
                      # config.json strategies.break.params.turnover_min 开启 (实测 21.7:
                      # align级 Δ+1.76 两段稳 / 日级七档全平坦 / signal级 60.4%/+3.42,
                      # 见 tmp/break_confirm_归因报告.md)。circ 缺失 fail-open 不拦。
                      turnover_min=None)


# ================================================================
# 评分口径 (2026-09-24 归一化: 旧式 `int(confirm_chg) + 10` ⇒ 值域 0~17)
# ----------------------------------------------------------------
# 旧式三个问题 (取证: tmp/_break_score_ic.log · _break_score_db.log):
#   ① **值域塌缩, 跨策略不可比**: 库内 break 仅 9/10/11, 回测 109 笔 min0~max17
#      (87% 落 10~17); 而 g56 67~99 / v1 34~66 / relay3 68~80 / tail 72~93
#      ⇒ 在 0~100 直觉下断板永远垫底, 观感即"评分不对"。
#   ② **int() 向零截断** ⇒ 1pp 分辨率; 通道1(企稳 confirm_chg∈[0,2)) 样本
#      **全部塌缩到 {10,11}** (库内 4 笔中 3 笔正是 10/11); confirm_chg<-10 ⇒ score≤0。
#   ③ **语义是入场成本而非质量**: score 就是确认日涨幅, 确认日涨得多=次日追高。
# 归一化后 ① ② 解决 (分辨率 1pp → 0.33pp, 值域 0~100); ③ **未解决且无法用换键解决** ——
#   实测 corr(score,ret)=-0.084 / 同日截面 IC -0.034 (confirm_chg -0.125), 无正向判别力。
#   ⚠ 故本分**只作展示与同策略内 tie-break, 不得用于资源分配/质量判断**。
#
# ★★ **本分不参与每日名额截断**: `daily_limit=5` 但实测 320 天 **85 个信号日无一日超 5 笔**
#    (109 笔/85 天, 单日最大 n=3) ⇒ `scan.py` 截断从不触发。这是 break 与 g56 的关键差异
#    (g56 单日可达 218 笔, 换键有 ~6.7pp 收益; **break 换键零收益**, 故此处不换因子, 只做
#    展示归一化, 保持与 confirm_chg 单调同向)。
# ================================================================
# 递推展示层 (2026-10-06): 连板增量台账 + 滚动窗口
# ----------------------------------------------------------------
# 生产 scan_signals 的候选枚举是 O(n²) 的全历史重扫:
#     for lu_idx in find_limit_ups(bars[:i], bt):   # 全历史涨停日
#         is_first(前 10 根无涨停) → streak_end 向前延伸到连板末日
# 递推等价的四条判据 (每条都可证, 非近似):
#   ① 只有**连板首日**能通过 is_first —— 段内第 j 个涨停日(j≥1)的前一根
#      必是涨停 ⇒ 必被 is_first 拒。⇒ 候选 == 极大连续涨停段的**起点**。
#   ② is_first 只取决于「上一个涨停日的距离」(>10 才成立) ⇒ 段起点即可定,
#      不需要回头看历史。
#   ③ 有效信号要求 break_idx+break_days-1 == i 且 break_days ∈ [1, max_break_gap]
#      ⇒ 段末 end ∈ [i-max_break_gap, i-1]。⚠ 上界是 i-1 不是 i-2: end=i-1 时
#      break_idx=i, 循环 range(i, min(i+gap, i+1)) = [i] 非空 ⇒ break_days=1
#      (昨日仍涨停 / 今日断板 1 天, 是合法候选)。曾漏成 i-2 ⇒ 实测漏 4 笔。
#      ⇒ 台账只需保留 end ≥ i-8 的段, 与连板多长无关。
#   ④ 段起点的两个远历史读数 —— bars[start]["time"] 与 bars[start-20]["close"]
#      (pre20_gain) —— 在**段起点当时**就地冻结进台账 ⇒ 段再长也不需要长窗口。
#      窗口只须覆盖 MA20/BOLL 的 20 根 + 断板期 5 根, 取 60 留足余量。
# ⚠ 唯一残留边界: 连板长度 > BREAK_WIN-25(≈35) 时 rel_start 越界 —— 由 pad 哑 bar
#   用冻结的 sdate/pre_ref 补齐 (A股历史最长连板 ≈29), 故仍是**严格等价**而非截断。
# ================================================================
BREAK_WIN = 60            # 递推窗口 (MA20/BOLL 20 + 断板期 5 + 余量)
BREAK_MIN_BARS = 30       # 与 scan.py 全市场扫描同一门槛 (len(bars) < 30 跳过)

SCORE_BASE = 50.0        # confirm_chg = 0 对应分
SCORE_PER_PCT = 3.0      # 确认日每 +1% 涨幅对应 +3 分 (实测 p05=-6.77 / p95=+7.36 ⇒ 值域 ≈20~74)


def _score_of(confirm_chg):
    """断板信号评分 0~100 (**展示分**, 口径见 SCORE_* 常量处注释)。

    线性: BASE + confirm_chg * PER_PCT, clip [0,100] 后取整。与 confirm_chg 单调同向,
    故**同策略内的相对排序与旧式一致** (归一化不改变组内次序)。

    NaN/负溢出防御: 比较 `> 0` 对 NaN 为 False ⇒ 落 0, 不污染排序 (与 g56._score_of 同款)。
    """
    v = SCORE_BASE + confirm_chg * SCORE_PER_PCT
    if not (v > 0):
        return 0
    return int(min(100, round(v)))


# ================================================================
# 断板期判定 (原 core._break_signal_at, 原样移植)
# ================================================================

def _break_signal_at(bars, code, streak_start, streak_end, min_streak, max_break_gap, params):
    """给定连板区间[streak_start,streak_end], 计算断板期并执行 5a-5f 确认。

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
    limit_open = float(limit_bar["open"])
    limit_close = float(limit_bar["close"])
    limit_vol = float(limit_bar["volume"])
    break_days = 0
    # 上界 = max_break_gap (2026-09-28 审计 A6: 原为 max_break_gap+1, 使断板期可到 6 天,
    # 与三处同义编码不一致 —— ① break.yaml:30 注释"断板期最长天数" ② 回测预过滤
    # `range(i-max_break_gap, i)` ③ 门表适配器 `break_days > max_break_gap → None`
    # ④ 本文件模块 docstring"断板期(≤max_break_gap天)"。四者均 ≤5, 故生产链的 +1 是 off-by-one。
    # 实测: 1500 只 × 320 日(绕过预过滤) break_days 分布 {1:40,2:9,3:3,4:1} ⇒ 本改为零影响。
    # 注: 真实断板期 > 上界时循环被截断, 该候选由调用方 scan_signals 的"确认日必须
    # 恰好落在 i"对齐检查(bars 索引等式)拒绝, 故截断不会产生错标信号。
    for j in range(break_idx, min(break_idx + max_break_gap, len(bars))):
        if is_limit_up(bars[j]["close"], bars[j - 1]["close"], bt):
            break  # 遇到新涨停, 断板期结束
        break_days += 1

    if break_days == 0:
        # 涨停后直接又是涨停 → 连板加速, 不是断板
        return None

    # 5. 断板期各项检查 (与回测 strategy_break_buy 完全一致)
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


def _ma_bull_at(bars, idx):
    """确认日均线多头排列: MA5>MA10>MA20 (idx=确认日索引); 数据不足(上市<20日)返回None。"""
    if idx + 1 < 20:
        return None
    c = [float(b["close"]) for b in bars[idx - 19:idx + 1]]
    ma5 = sum(c[-5:]) / 5
    ma10 = sum(c[-10:]) / 10
    ma20 = sum(c) / 20
    return ma5 > ma10 > ma20


def _entry_gate(bars, i, streak_len, code):
    """入场通道标签 (2026-09-22 归一四通道研究, 见 tmp/break_plan_A.json)。

    全部用确认日 D0=i 收盘可知数据, 无前视。只标注不过滤 — 展示层用于
    区分历史胜率 (核心 89% / 高板 83% / 温和 80% / 强势 53%)。

    ⚠ `code` 必传 (2026-09-28 审计 A3): 原先从 `bars[i].get("code")` 取板块, 而 bar
    只有 time/open/high/low/close/volume ⇒ 恒取到 "" ⇒ `get_board_type("")` 回落
    `board_default="main"` ⇒ 创业板/科创板按 9.8% 找"前一涨停日"(其真实阈值 19.8%)
    ⇒ `bd`/`entry_gate` 错标; 而 entry_bd 经 sweet_pctb 参与回测出场阈值选择。

    Returns: (gate:str, pctb:float|None, bd:int)
      gate ∈ {"核心", "高板", "温和", "强势", "观察"}
    """
    closes = [float(b["close"]) for b in bars]
    highs = [float(b["high"]) for b in bars]
    lows = [float(b["low"]) for b in bars]
    # %B (BOLL 20,2)
    if i < 19:
        return "观察", None, None
    w = closes[i - 19:i + 1]
    m = sum(w) / 20.0
    sd = (sum((x - m) ** 2 for x in w) / 20.0) ** 0.5
    u, l = m + 2 * sd, m - 2 * sd
    pctb = (closes[i] - l) / (u - l) * 100 if u > l else 50.0
    # 调整天数: D0 前最后一个涨停日 → D0
    # ⚠ 板块必须来自 `code` (审计 A3): 原先读 bars[i]["code"] 恒取不到 ⇒ 恒 main。
    # 涨停口径改用共享 `is_limit_up` (本文件其余位置同口径), 不再内联 0.098/0.198 × 0.98
    # (内联=第二份口径; 且 0.98 容差使主板阈值变 9.6%, 与 is_limit_up/门表不一致)。
    bt = get_board_type(code)
    j, steps, streak_end = i - 1, 0, None
    while j >= 1 and steps <= 5:
        prev = float(bars[j - 1]["close"]) if j >= 1 else 0
        cur = float(bars[j]["close"])
        if is_limit_up(cur, prev, bt):
            streak_end = j
            break
        j -= 1; steps += 1
    if streak_end is None:
        return "观察", round(pctb, 1), None
    bd = i - streak_end
    # 通道判定 (优先级: 核心 > 高板 > 温和 > 强势)
    if 2 <= bd <= 3 and pctb >= 90:
        return "核心", round(pctb, 1), bd
    if streak_len >= 4 and (pctb >= 100 or bd >= 3):
        return "高板", round(pctb, 1), bd
    if bd == 1 and pctb < 80:
        return "温和", round(pctb, 1), bd
    if bd == 1 and pctb >= 95:
        return "强势", round(pctb, 1), bd
    return "观察", round(pctb, 1), bd


def break_entry_gate(bars, i, streak_len, code):
    """入场通道标注的 IDE 侧入口 (薄封装 `_entry_gate`)。

    2026-09-28: 供本模块的 day_flow 编排 (`_backtest_day_flow`, 自 core/runtime/evaluate.py
    下沉而来) 引用。编排已搬回策略层 ⇒ 不再需要 core 惰性 import strategies (层反转已消除)。
    口径与参考版**同一份实现**, 保证 IDE 门表回测的 entry_gate/entry_pctb/entry_bd
    与 .py 生产链逐笔一致。

    Returns: (gate:str, pctb:float|None, bd:int|None)
    """
    return _entry_gate(bars, i, streak_len, code)


def _signal_to_legacy_dict(sig: Signal, code: str) -> dict:
    """Signal → 旧 break_today_d0_signals 的 dict 形态 (facade 兼容层)。

    **易错点**: 必须显式列字段 — 旧输出不含 break_idx (内部变量), 全量透传 extra
    会让回测 trades 多键, 破坏逐笔对数。streak_start/streak_end 是日期字符串。
    """
    ex = sig.extra or {}
    return {
        "code": code,
        "board": get_board_name(code),
        "path": "break_buy",
        "path_label": "断板",
        "mode": "streak_break",
        "streak_len": ex.get("streak_len"),
        "streak_start": ex.get("streak_start"),
        "streak_end": ex.get("streak_end"),
        "break_date": ex.get("break_date"),
        "signal_date": sig.time,
        "break_days": ex.get("break_days"),
        "break_chg": ex.get("break_chg"),
        "break_gap": ex.get("break_gap"),
        "break_vol_r": ex.get("break_vol_r"),
        "confirm_chg": ex.get("confirm_chg"),
        "confirm_gap": ex.get("confirm_gap"),
        "pre20_gain": ex.get("pre20_gain"),
        "ma_bull": ex.get("ma_bull"),
        "entry_gate": ex.get("entry_gate"),
        "entry_pctb": ex.get("entry_pctb"),
        "entry_bd": ex.get("entry_bd"),
        "turnover_anchor": ex.get("turnover_anchor"),
        "turnover_sig": ex.get("turnover_sig"),
        "turnover_anchor_total": ex.get("turnover_anchor_total"),
        "turnover_sig_total": ex.get("turnover_sig_total"),
        "entry_price": None,
        "buy_mode": "next_open",
    }


@register
class BreakStrategy(StrategyBase):
    key = STRATEGY_KEY
    name = STRATEGY_LABEL
    prefilter_anchor = "signal"        # 锚定确认日(末根bar); 连板≥2已隐含U4
    entry_style = "brk"
    scan_spec = ScanSpec(kind="daily_close", after_events=("daily_1d", "lhb"))
    default_params = dict(DEFAULT_PARAMS)
    # 探针 day-stage 归属 (越靠后=离信号越近; 细门在 _break_signal_at 内不单列)
    PROBE_STAGE_RANK = {"confirm": 1, "align": 2, "dedup": 3, "prefilter": 4,
                        "engine_skip": 5, "signal": 6}
    # 展示阶段表: 递推侧只产出**信号日** (ready)。
    # ⚠ 出场生命周期不在递推侧重放 —— break 是多日追踪止损
    #   (exit_decision / core.exit_engines), 与 knife/tail 的 "D1 开盘即平账" 不同。
    stages = (
        Stage("ready", "断板确认·准备", realtime="09:25"),
    )

    # ---- 信号判定 ----
    def scan_signals(self, bars, code, *, as_of=None, ctx=None, limit_ups=None,
                     probe=None, **params):
        """今日是否为断板期确认日 → Signal (至多1笔)。as_of=k: 只用 bars[:k+1]。

        limit_ups: 预计算的涨停日索引列表 (回测/扫描复用, None 则现算 bars[:as_of])。
        probe: 调试探针 (None=零开销) — 门级 TRACE 打点 (粗粒度: 细门在
        _break_signal_at 内, 日级归属够用), 存档供 AI 离线分析。"""
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
        bt = get_board_type(code)
        board_params = BOARD_PARAMS.get(bt, BOARD_PARAMS["main"])
        board_params = {**board_params, **{k: v for k, v in p.items() if k in board_params}}
        min_streak, max_break_gap = p["min_streak"], p["max_break_gap"]

        # 寻找所有连板结构, 要求断板期最后一天 == 今日(i)
        for lu_idx in (limit_ups if limit_ups is not None else _find_limit_ups(bars[:i], bt)):
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
            sig = _break_signal_at(bars, code, streak_start, streak_end, min_streak, max_break_gap, board_params)
            if not sig:
                if probe is not None:
                    probe.trace("confirm", code=code, d0_date=str(bars[i]["time"])[:10],
                                streak_start=str(bars[streak_start]["time"])[:10],
                                streak_end=str(bars[streak_end]["time"])[:10],
                                streak_len=streak_end - streak_start + 1)
                continue
            if sig["break_idx"] + sig["break_days"] - 1 != i:
                if probe is not None:
                    probe.trace("align", code=code, d0_date=str(bars[i]["time"])[:10],
                                break_days=sig.get("break_days"))
                continue
            # 换手率前置门 (ML归因反哺, 数学必要条件; turnover_min=None 时零开销直通)
            circ = float((params.get("stock_info") or {}).get("circ_shares") or 0)
            _tmin = p.get("turnover_min")
            if _tmin and circ > 0:
                _to_sig = float(bars[i]["volume"]) / circ * 100
                if _to_sig < _tmin:
                    if probe is not None:
                        probe.trace("prefilter", code=code, gate="turnover_min",
                                    d0_date=str(bars[i]["time"])[:10],
                                    turnover_sig=round(_to_sig, 2), turnover_min=_tmin)
                    continue
            total = float((params.get("stock_info") or {}).get("total_shares") or 0)
            extra = dict(sig)
            _gate, _gpctb, _gbd = _entry_gate(bars, i, sig.get("streak_len") or 0, code)
            extra.update({
                "entry_gate": _gate,
                "entry_pctb": _gpctb,
                "entry_bd": _gbd,
                "turnover_anchor": round(float(bars[streak_end]["volume"]) / circ * 100, 2) if circ > 0 else None,
                "turnover_sig": round(float(bars[i]["volume"]) / circ * 100, 2) if circ > 0 else None,
                "turnover_anchor_total": round(float(bars[streak_end]["volume"]) / total * 100, 2) if total > 0 else None,
                "turnover_sig_total": round(float(bars[i]["volume"]) / total * 100, 2) if total > 0 else None,
            })
            result.append(Signal(
                code=code,
                time=bars[i]["time"],
                score=_score_of(float(sig.get("confirm_chg", 0) or 0)),
                price=0.0,                                        # 断板信号日不定价 (entry=D1开盘)
                label="断板",
                extra=extra,
            ))
            break  # 只取一个信号
        return result

    # ================================================================
    # 递推展示层契约 (2026-10-06)
    #
    # 门判定  → `_break_signal_at` + `_entry_gate`（全量路径 scan_signals 共用）
    # 原语    → `is_limit_up` / BOARD_PARAMS / `_score_of`（本文件唯一实现）
    # 递推量  → 连板台账 {start,end,sdate,pre_ref,first} + 近 BREAK_WIN 根 OHLCV
    # ================================================================
    @staticmethod
    def _rec(b):
        return {"t": str(b["time"])[:10], "o": float(b["open"]), "h": float(b["high"]),
                "l": float(b["low"]), "c": float(b["close"]),
                "v": float(b.get("volume") or 0)}

    def _blank(self, code):
        return {"v": 1, "board": get_board_type(code), "abs_i": -1,
                "win": [], "runs": [], "last_lu": None, "open_run": False}

    def init_state(self, code: str, bars: list[dict]) -> dict:
        """全量播种 = 逐根走同一个 `_advance`（递推与全量逐位一致的构造性保证）。"""
        if len(bars) < BREAK_MIN_BARS:
            raise InsufficientHistory(f"{code}: bars={len(bars)} < {BREAK_MIN_BARS}")
        st = self._blank(code)
        for b in bars:
            st = self._advance(st, b)
        return st

    def step(self, state: dict, bar: dict) -> dict:
        return self._advance(state, bar)

    def _advance(self, st: dict, bar: dict) -> dict:
        rec = self._rec(bar)
        win = list(st["win"])
        prev_c = win[-1]["c"] if win else None
        abs_i = st["abs_i"] + 1
        is_lu = prev_c is not None and prev_c > 0 and is_limit_up(rec["c"], prev_c, st["board"])
        runs = list(st["runs"])
        last_lu, open_run = st["last_lu"], st["open_run"]
        if is_lu:
            if open_run and runs:
                runs[-1] = {**runs[-1], "end": abs_i}          # 连板延续
            else:
                runs.append({
                    "start": abs_i, "end": abs_i, "sdate": rec["t"],
                    # ② is_first = 上一个涨停日距离 > 10
                    "first": last_lu is None or (abs_i - last_lu) > 10,
                    # ④ pre20 基准在段起点就地冻结 (win[-20] = abs_i-20)
                    "pre_ref": win[-20]["c"] if len(win) >= 20 else None,
                })
            last_lu, open_run = abs_i, True
        else:
            open_run = False
        win = (win + [rec])[-BREAK_WIN:]
        # ③ end < i-5 的段永不可能再是候选 ⇒ 保留 end ≥ abs_i-8 即可
        cut = abs_i - 8
        runs = [r for r in runs if r["end"] >= cut]
        return {"v": 1, "board": st["board"], "abs_i": abs_i, "win": win,
                "runs": runs, "last_lu": last_lu, "open_run": open_run}

    def probe(self, state: dict) -> list[tuple[str, float]]:
        w = state["win"]
        return [(w[0]["t"], w[0]["c"]), (w[-1]["t"], w[-1]["c"])]

    def evaluate(self, state: dict, inp: DayInput, prev: "Progress | None") -> list:
        """信号日判定（门逻辑与 scan_signals 同一份实现，只换特征来源）。"""
        p = self.params()
        bar, code = inp.bar, inp.code
        today = self._rec(bar)
        wb = [{"time": r["t"], "open": r["o"], "high": r["h"], "low": r["l"],
               "close": r["c"], "volume": r["v"]} for r in state["win"]] + [{
                   "time": today["t"], "open": today["o"], "high": today["h"],
                   "low": today["l"], "close": today["c"], "volume": today["v"]}]
        i_abs = state["abs_i"] + 1
        off = i_abs - (len(wb) - 1)                  # wb[0] 的绝对下标
        bt = state["board"]
        board_params = BOARD_PARAMS.get(bt, BOARD_PARAMS["main"])
        board_params = {**board_params, **{k: v for k, v in p.items() if k in board_params}}
        min_streak, max_break_gap = p["min_streak"], p["max_break_gap"]
        si = (inp.ctx or {}).get("stock_info") or {}
        circ = float(si.get("circ_shares") or 0)
        total = float(si.get("total_shares") or 0)

        for run in state["runs"]:
            if not run["first"]:
                continue
            end = run["end"]
            if end < i_abs - max_break_gap or end > i_abs - 1:      # ③
                continue
            rel_s, rel_e = run["start"] - off, end - off
            # ④ 段起点越出窗口时用冻结值补哑 bar（严格等价, 不是截断候选）
            pad = max(0, -rel_s, (20 - rel_s) if run["start"] >= 20 else 0)
            bars_v = wb
            if pad:
                bars_v = [{"time": "", "open": 0.0, "high": 0.0, "low": 0.0,
                           "close": 0.0, "volume": 0.0} for _ in range(pad)] + wb
                bars_v[rel_s + pad]["time"] = run["sdate"]
                if run["start"] >= 20:
                    bars_v[rel_s + pad - 20]["close"] = float(run["pre_ref"] or 0.0)
            rs, re_ = rel_s + pad, rel_e + pad
            i = len(bars_v) - 1                      # 今日在 bars_v 中的位置
            sig = _break_signal_at(bars_v, code, rs, re_, min_streak,
                                   max_break_gap, board_params)
            if not sig:
                continue
            if sig["break_idx"] + sig["break_days"] - 1 != i:
                continue                             # 确认日必须恰好落在今日
            # ⚠ `_break_signal_at` 的 break_idx 是 **bars 下标**: 生产侧是绝对下标,
            #   递推侧是窗口相对下标 ⇒ 落 extra 前换算回绝对 (同名不同义必踩)。
            sig = dict(sig)
            sig["break_idx"] = sig["break_idx"] + off - pad
            # 换手率前置门 (与 scan_signals 同式)
            _tmin = p.get("turnover_min")
            if _tmin and circ > 0:
                if float(bars_v[i]["volume"]) / circ * 100 < _tmin:
                    continue
            extra = dict(sig)
            _gate, _gpctb, _gbd = _entry_gate(bars_v, i, sig.get("streak_len") or 0, code)
            extra.update({
                "entry_gate": _gate,
                "entry_pctb": _gpctb,
                "entry_bd": _gbd,
                "turnover_anchor": round(float(bars_v[re_]["volume"]) / circ * 100, 2) if circ > 0 else None,
                "turnover_sig": round(float(bars_v[i]["volume"]) / circ * 100, 2) if circ > 0 else None,
                "turnover_anchor_total": round(float(bars_v[re_]["volume"]) / total * 100, 2) if total > 0 else None,
                "turnover_sig_total": round(float(bars_v[i]["volume"]) / total * 100, 2) if total > 0 else None,
            })
            return [Progress(stage="ready", date=str(bar.get("time", "")), payload={
                "price": 0.0,                        # 断板信号日不定价 (entry=D1开盘)
                "score": _score_of(float(sig.get("confirm_chg", 0) or 0)),
                "label": "断板", "extra": extra,
            })]
        return []

    # ---- D1 竞价处置 ----
    def entry_decision(self, row, snap=None, **params):
        """break 无开盘 gap 过滤 (恒可买); 快照缺失不可买 (与 monitor skip 一致)。"""
        if not snap:
            return EntryDecision(False, "无竞价快照")
        open_px = float(snap.get("open") or snap.get("last") or 0)
        if open_px <= 0:
            return EntryDecision(False, "开盘价缺失")
        return EntryDecision(True, "断板无gap过滤, 开盘可买")

    # ---- 15:00 收盘确认 ----
    def confirm_decision(self, row, snap=None, **params):
        """无确认步骤 (确认已在 D0 断板期判定完成): 买入次日直接持仓。

        d1_chg 按 signal_price 基准 (旧 evaluate_confirm else 分支口径)。
        返回 None = 无法判定, monitor 不转移。

        A4 修复 (2026-09-28): break 信号不定价 (Signal.price=0 → signal_price=None),
        旧实现遇 None 直接 return None → monitor 15:00 确认对 dec is None 永远
        continue, 每笔 break 买入永久卡 buy_today (进不了 holding/收盘出场重放,
        只剩盘中硬止损兜底, 且确认窗口机会只有一次)。参考价兜底链:
        signal_price → D1 快照 previousClose (=D0 收盘, 同一基准) →
        entry_price/entry_gap 反推; 全部不可得时仍确认 (d1_chg=None) ——
        缺价只影响一个统计字段, 绝不能阻断状态机转移。
        """
        series = (snap or {}).get("series") if isinstance(snap, dict) else None
        if not series:
            return None
        last_px = float(series[-1].get("last") or 0)
        if last_px <= 0:
            return None
        prev_close = float(row.get("signal_price") or 0)
        if prev_close <= 0:
            prev_close = float((series[-1] or {}).get("previousClose") or 0)
        if prev_close <= 0:
            entry = float(row.get("entry_price") or 0)
            gap = float((row.get("extra") or {}).get("entry_gap") or 0)
            denom = 1 + gap / 100
            if entry > 0 and denom != 0:
                prev_close = entry / denom
        d1_chg = round((last_px / prev_close - 1) * 100, 2) if prev_close > 0 else None
        return ConfirmDecision(True, "ok", d1_chg=d1_chg,
                               detail={"confirm": "ok", "confirm_strong": False})

    def initial_stop(self, code, entry_price):
        """创科板 -10% / 主板 -8% (与旧 _entry_stop 分档一致)。"""
        gem = get_board_type(code) == "gem_star"
        return round(entry_price * (1 + (-10.0 if gem else -8.0) / 100), 3)

    # ---- 出场判定 ----
    def exit_decision(self, row, snap=None, **params):
        """收盘价口径 (monitor break 分支 / run_backtest_breakbuy 语义):
        止损 / 追踪止损(ret>0) / 峰值逃顶 / 到期。live 模式 → hold (硬止损在 monitor 主循环)。"""
        if not isinstance(snap, dict) or snap.get("mode") != "day_close":
            return ExitDecision("hold")
        bars = snap.get("bars")
        entry_idx = snap.get("entry_idx")
        entry_price = float(row.get("entry_price") or 0)
        if bars is None or entry_idx is None or entry_price <= 0:
            return ExitDecision("hold")
        bt = get_board_type(row.get("code", ""))
        bp = BOARD_PARAMS["gem_star" if bt == "gem_star" else "main"]
        stop, trail, hold = bp["stop_loss"], bp["trailing_stop"], bp["hold_days"]
        today_idx = len(bars) - 1
        held = today_idx - entry_idx + 1
        entry_seg = bars[entry_idx:today_idx + 1]
        peak = max(float(b["high"]) for b in entry_seg)
        last_bar = bars[-1]
        ret = (last_bar["close"] / entry_price - 1) * 100
        ret_from_high = (last_bar["close"] / peak - 1) * 100 if peak > 0 else 0
        if ret <= stop:
            return ExitDecision("exit", reason=f"止损{stop}%", price=entry_price * (1 + stop / 100))
        if ret_from_high <= trail and ret > 0:
            return ExitDecision("exit", reason=f"追踪止损{trail}%", price=peak * (1 + trail / 100))
        if ret > 10:
            bar_range = last_bar["high"] - last_bar["low"]
            upper = (last_bar["high"] - max(last_bar["open"], last_bar["close"])) / bar_range * 100 if bar_range > 0 else 0
            if upper > 40 and last_bar["close"] < last_bar["high"] * 0.98:
                return ExitDecision("exit", reason="峰值逃顶", price=float(last_bar["close"]))
        if held >= hold:
            return ExitDecision("exit", reason=f"持仓到期{hold}天", price=float(last_bar["close"]))
        return ExitDecision("hold")

    # ---- 回测钩子 (2026-09-10 自 backtest.backtest_break_stock 逐字搬入, 对数零差异) ----
    def backtest_stock(self, bars, code, stock_info=None, use_prefilter=True,
                      probe=None):
        """单股断板全历史回测 (断板期确认日判定, 次日开盘买)。

        出场参数取本插件 BOARD_PARAMS (单一定义, backtest.py 旧份已删);
        出场模拟 _run_backtest_breakbuy 在本文件 (策略专用出场规则, 2026-09-10 晚下沉)。
        """
        from app.market_cn.auto.core.filters import unified_prefilter
        from app.market_cn.auto.probe import DayTrace
        # 入场枚举参数接线 (2026-09-13 修): 原硬编码 2,5 且经 kwargs 传 scan_signals,
        # kwargs 优先级压过实例覆写 → param_scan 网格全然无效 (实证: 全组合 n=94
        # 同数字)。改从 params(None) 取: 默认=代码默认值 (行为零差异), 实例
        # default_params 覆写 (param_scan 唯一调参入口) 即生效。
        _p = self.params(None)
        min_streak, max_break_gap = _p["min_streak"], _p["max_break_gap"]
        # 换手率门 (2026-09-11 config 透传): 实例覆写优先, 未覆写时回落 config —
        # 覆写后 config 不再参与 (实例覆写=回测权威)
        from app.market_cn.auto import strategies as _strat_reg
        _turnover_min = _p.get("turnover_min")
        if "default_params" not in self.__dict__ and _turnover_min is None:
            try:
                _turnover_min = _strat_reg.params_override(self.key).get("turnover_min")
            except Exception:
                pass
        bt_type = get_board_type(code)
        # 出场成交时点 (2026-09-21 新增能力, 默认 "close" = 原行为逐笔不变):
        #   "close"(收盘判定+收盘价成交) / "intraday_stop"(仅硬止损盘中) / "intraday"(两腿盘中)。
        # 入口与 turnover_min 同源: 实例 default_params 覆写优先, 否则回落 config.json。
        fill_mode = str(_p.get("fill_mode") or "")
        if not fill_mode:
            try:
                fill_mode = str(_strat_reg.params_override(self.key).get("fill_mode") or "")
            except Exception:
                pass
        fill_mode = fill_mode or "close"
        params = dict(BOARD_PARAMS[bt_type])
        # config/default_params 覆盖链: 实例 default_params / config params 中与 BOARD_PARAMS 同键的项生效
        _mp = self.params(None)
        params.update({k: v for k, v in _mp.items() if k in params})
        stop_loss, trailing_stop = params["stop_loss"], params["trailing_stop"]
        hold_days = params["hold_days"]
        # 2026-10-07: 默认 "sweet" → "legacy" (与实盘 exit_decision 同规则集, 见
        #   BOARD_PARAMS 注释)。想复现甜点口径需**显式**在 config params 里写
        #   exit_mode="sweet" —— 默认不再是它, 因为回测收益要能外推到实盘。
        exit_mode = str(params.get("exit_mode") or "legacy")
        sweet_pctb = float(params.get("sweet_pctb") or 95.0)
        sweet_pctb_core = float(params.get("sweet_pctb_core") or 100.0)
        n = len(bars)
        if n < 6:
            return []
        lu_all = find_limit_ups(bars, bt_type)
        lu_set = set(lu_all)
        trades = []
        used = set()

        for i in range(4, n - 1):
            # 确认日必为非涨停日 (断板期最后一天)
            if is_limit_up(bars[i]["close"], bars[i - 1]["close"], bt_type):
                continue
            # 廉价预过滤: 断板期结束于i → 必存在距i不超过max_break_gap的涨停日
            if not any(j in lu_set for j in range(max(1, i - max_break_gap), i)):
                continue
            # debug 模式: day_tr 聚合该日判定门落点 (probe=None 零开销)
            day_tr = DayTrace() if probe is not None else None
            # 逐日候选判定: 与实盘 scan 完全同一函数 (切片 as_of 语义; 经 facade 等价路径)
            sigs = [_signal_to_legacy_dict(s, code) for s in self.scan_signals(
                bars[:i + 1], code,
                min_streak=min_streak, max_break_gap=max_break_gap,
                turnover_min=_turnover_min,
                limit_ups=[j for j in lu_all if j < i],
                stock_info=stock_info, probe=day_tr)]
            if not sigs:
                if probe is not None:
                    self._probe_day(probe, day_tr, bars, i, code, stock_info)
                continue
            sig = sigs[0]

            # 去重: 同一连板起点+断板日只取一次 (去重在过滤之前, 对数基线行为)
            key = (sig["streak_start"], sig["break_date"])
            if key in used:
                if probe is not None:
                    self._probe_day(probe, day_tr, bars, i, code, stock_info,
                                    stage="dedup", sig=sig)
                continue
            used.add(key)

            # U1~U4 (确认日D0收盘可知; 连板>=2已隐含U4)
            if use_prefilter:
                ok, fails = unified_prefilter(bars, i, code, stock_info)
                if not ok:
                    if probe is not None:
                        self._probe_day(probe, day_tr, bars, i, code, stock_info,
                                        stage="prefilter", sig=sig, u_fails=fails)
                    continue

            # 入场: 次日(D+1)开盘价
            entry_price = bars[i + 1]["open"]
            if entry_price <= 0:
                continue
            result = _run_backtest_breakbuy(bars, i + 1, entry_price, hold_days,
                                           stop_loss, trailing_stop, bt_type, fill_mode,
                                           exit_mode=exit_mode,
                                           entry_gate=sig.get("entry_gate"),
                                           sweet_pctb=sweet_pctb,
                                           sweet_pctb_core=sweet_pctb_core)
            if not result:
                if probe is not None:
                    self._probe_day(probe, day_tr, bars, i, code, stock_info,
                                    stage="engine_skip", sig=sig)
                continue

            if probe is not None:
                self._probe_day(
                    probe, day_tr, bars, i, code, stock_info, stage="signal",
                    sig=sig, extra={"engine": {k: result.get(k) for k in
                                               ("return_pct", "peak_return_pct",
                                                "exit_reason", "exit_day")}})
            prev_close = bars[i]["close"]
            trades.append({
                **sig,
                "signal_date": bars[i]["time"],
                "entry_date": bars[i + 1]["time"],
                "entry_price": round(entry_price, 3),
                "buy_mode": "next_open",
                "d1_change": round((bars[i + 1]["close"] / bars[i + 1]["open"] - 1) * 100, 2)
                if bars[i + 1]["open"] > 0 else 0,
                "d1_gap": round((bars[i + 1]["open"] / prev_close - 1) * 100, 2)
                if prev_close > 0 else 0,
                "intraday": round((bars[i + 1]["close"] - bars[i + 1]["open"]) / prev_close * 100, 2)
                if prev_close > 0 else 0,
                **result,
            })

        return trades

    # ---- 1m 真实腿出场重放 (P3b/P4, 2026-09-21) ----
    def intraday_replay(self, bars, entry_idx, entry_price, *, code, board_type,
                        minute_by_date, params=None, entry_gate=None):
        """断板出场在 1m 通道上重放: 止损/追踪**逐槽位**(知日内先后), 峰值逃顶/到期仍收盘语义。

        与 `backtest_stock` 共用**同一出场引擎** `_run_backtest_breakbuy` —— 只是多传
        `minute_by_date`, 不新增第二份出场规则。出场参数取本插件 BOARD_PARAMS (单一定义)。
        调用方 (P4 `run_all`) 负责"整笔持仓窗口覆盖一致"与口径标注 (`exec_basis`)。

        ⚠ 2026-10-07: 原实现**漏传** `exit_mode`/`entry_gate`/`sweet_pctb*` ⇒ 恒走
          `_run_backtest_breakbuy` 的函数默认值 (exit_mode="sweet" / sweet_pctb=95 /
          entry_gate=None), 与 `backtest_stock` 和 `_exit_break_combo` **两条路径不同
          口径**: ① exit_mode 不随 BOARD_PARAMS 走; ② 核心/高板通道的甜点阈值恒取
          95 而非 100 ⇒ 该类票在 1m 腿上比日线腿**早出场**。
          (A3 于 2026-09-28 修了门表路径 `_exit_break_combo` 的同一问题, 本方法当时漏网。)
          现与 `_exit_break_combo` 用同一 `_pick` 口径: params → BOARD_PARAMS → default。
        """
        bt = board_type or get_board_type(code)
        bp = BOARD_PARAMS.get(bt, BOARD_PARAMS["main"])
        _p = params if isinstance(params, dict) else {}

        def _pick(name, default):
            """params 覆写优先 → BOARD_PARAMS → default (与 _exit_break_combo 同口径)。"""
            v = _p.get(name)
            if v is None:
                v = bp.get(name)
            return default if v is None else v

        return _run_backtest_breakbuy(
            bars, entry_idx, entry_price, bp["hold_days"], bp["stop_loss"],
            bp["trailing_stop"], bt, "close", minute_by_date,
            exit_mode=str(_pick("exit_mode", "legacy")),
            entry_gate=entry_gate,
            sweet_pctb=float(_pick("sweet_pctb", 95.0)),
            sweet_pctb_core=float(_pick("sweet_pctb_core", 100.0)))


def _find_limit_ups(bars, bt):
    """涨停日索引 (is_limit_up vs 前收; 第0根无前收跳过)。与 core.market find_limit_ups 同语义。"""
    from app.market_cn.auto.core.market import find_limit_ups
    return find_limit_ups(bars, bt)


# ================================================================
# 出场模拟 (2026-09-10 晚自 backtest.py 下沉回归本文件 — 出场规则是策略专用,
# 通用流水线不承载策略专属出场; 逐字搬运, 回归以三策略对数验证)
# ================================================================
from app.market_cn.auto.core.exec import (
    fill_intraday,
    is_one_word_limit_dn,
    limit_dn_price as _limit_dn_price,
    replay_sell_intraday,
)


def _rsi6_series(bars):
    """RSI6 (TDX SMA递推) 全序列; 前6根为 None (与 2026-09-22 出场研究脚本逐位一致)"""
    n = len(bars)
    closes = [float(b["close"]) for b in bars]
    out = [None] * n
    ag = al = 0.0
    for i in range(1, n):
        ch = closes[i] - closes[i - 1]
        g, l = max(ch, 0.0), max(-ch, 0.0)
        if i <= 6:
            ag += g; al += l
            if i == 6:
                ag /= 6; al /= 6
                out[i] = 100.0 if al == 0 else 100.0 - 100.0 / (1 + ag / al)
        else:
            ag = (ag * 5 + g) / 6
            al = (al * 5 + l) / 6
            out[i] = 100.0 if al == 0 else 100.0 - 100.0 / (1 + ag / al)
    return out


def _boll_pctb_series(bars):
    """BOLL(20,2) %B 全序列; 前19根为 None"""
    n = len(bars)
    closes = [float(b["close"]) for b in bars]
    out = [None] * n
    for i in range(19, n):
        w = closes[i - 19:i + 1]
        m = sum(w) / 20.0
        sd = (sum((x - m) ** 2 for x in w) / 20.0) ** 0.5
        u, l = m + 2 * sd, m - 2 * sd
        out[i] = (closes[i] - l) / (u - l) * 100 if u > l else 50.0
    return out


_RSI6_CACHE = {}
_PCTB_CACHE = {}


def _exit_series(bars):
    """RSI6/%B 序列 (按**内容指纹**缓存; 同 bars 多笔交易零重复计算)。

    ⚠ 2026-09-28 审计 P2: 原按 `id(bars)` 作缓存键 —— list 被回收后地址可能被另一只票的
    bars 复用 ⇒ 命中他票序列 (heisenbug; 批量路径 bars 常驻故通常不发作)。
    改内容指纹 (长度 + 首/中/尾日期 + 尾收盘): O(1) 且与对象身份无关。
    """
    if not bars:
        return [], []
    key = (len(bars), str(bars[0].get("time")), str(bars[-1].get("time")),
           float(bars[-1].get("close") or 0),
           float(bars[len(bars) // 2].get("close") or 0))
    if key not in _RSI6_CACHE:
        if len(_RSI6_CACHE) > 64:
            _RSI6_CACHE.clear()
            _PCTB_CACHE.clear()
        _RSI6_CACHE[key] = _rsi6_series(bars)
        _PCTB_CACHE[key] = _boll_pctb_series(bars)
    return _RSI6_CACHE[key], _PCTB_CACHE[key]


def _run_backtest_breakbuy(bars, entry_idx, entry_price, hold_days=7, stop_loss=-8.0,
                          trailing_stop=-6.0, board_type="main", fill_mode="close",
                          minute_by_date=None, exit_mode="legacy", entry_gate=None,
                          sweet_pctb=95.0, sweet_pctb_core=100.0):
    """断板专用回测: 追踪止损 + 峰值逃顶信号。

    现实化 (2026-09-09, 与 test_dragon.py 逐字同步):
    ① T+1 — 买入当日(d=1)不可卖出;
    ② 成交价=收盘价 — 原引擎收盘判定却按触发价成交 (触发价高于判定收盘, 不可实现);
    ③ 跌停 — 一字跌停整日跳过; 收盘触及跌停卖不出 → 顺延次日开盘; 到期顺延。

    出场成交时点 fill_mode (2026-09-21 新增能力; **默认 "close" = 原行为逐笔不变**):
      "close"         : 两腿均"收盘判定 + 收盘价成交"(现行口径, 默认);
      "intraday_stop" : **仅硬止损腿**盘中触发 — 当日 low 触及止损线即按线价成交
                        (开盘已在线下则按开盘价, 跳空不可按线成交), 成交价贴跌停
                        (fill_blocked_by_limit_dn) → 卖不出, 顺延次日开盘强平;
                        追踪腿仍 close。对齐实盘 monitor.py 硬止损的 tick 级语义。
      "intraday"      : 两腿均盘中。追踪线取 **开盘时已知的 peak_prev**
                        (不用当日 high 抬高后再回判, 避免"日内先视"伪影)。
    盘中价一律走 `core/exec.fill_intraday` 原子原语 (内部复用 fill_on_gap /
    fill_blocked_by_limit_dn), 与 v1 / dragon_callback / dragon_v2 同一约定 —— 不新增第二份成交语义。
    配置入口: config.json `strategies.break.params.fill_mode` (未配置 → close)。

    minute_by_date (2026-09-21 P3b, **1m 真实腿**): ``{date: [分钟槽位, ...]}`` (当日升序)。
    给定且该日有槽位 → 止损/追踪两腿改由 `core/exec.replay_sell_intraday` **逐槽位重放**
    (知日内先后, 消除"先跌穿后收回 / 先冲高后跌穿"歧义; 追踪线取开盘时已知峰值)。
    峰值逃顶 / 到期仍是**收盘语义** (与 1m 无关)。``None`` → 行为与既往**逐笔不变**。
    """
    fill_mode = str(fill_mode or "close")
    if entry_price <= 0 or entry_idx >= len(bars):
        return None
    _last_rule = "time"
    _sweet_on = (exit_mode == "sweet")
    if _sweet_on:
        Rs, Ps = _exit_series(bars)
    peak = entry_price
    exit_p = entry_price
    exit_d = 0
    pending_dn = False        # 收盘触跌停卖不出 → 次日开盘强平
    last_unfilled = False     # 末日一字跌停 → 到期顺延
    stop_line = entry_price * (1 + stop_loss / 100.0)   # 硬止损线 (D1 起即存在, 盘中可用)

    # next_open模式: entry_idx=D1, 循环d=1应指向D1
    if entry_idx < len(bars):
        d1_init = bars[entry_idx]
        if d1_init['high'] > peak:
            peak = d1_init['high']

    for d in range(1, hold_days + 1):
        idx = entry_idx + d - 1  # d=1 → entry_idx(D1)
        if idx >= len(bars): break
        b = bars[idx]
        peak_prev = peak        # 开盘时已知的峰值 (当日 high 尚未计入 → 盘中追踪线可用)
        if b['high'] > peak: peak = b['high']
        prev_close = bars[idx - 1]['close'] if idx > 0 else 0
        dn = _limit_dn_price(prev_close, board_type) if prev_close > 0 else None

        # 跌停顺延: 前一交易日无法卖出 → 今日开盘强平
        if pending_dn:
            exit_p, exit_d = b['open'], d
            # 已成交 → 清标志。不清则尾部"末日顺延"块 (if last_unfilled or pending_dn)
            # 会再顺延一次, 且其起点 nxt = entry_idx + exit_d + 1 还多跳一天 →
            # 出场被整体推后 2 个交易日 (实测 300d 全市场 94 笔中 9 笔受影响)。
            # 同类引擎 (dragon_callback/v1/dragon_v2) 用 `if exit_reason == "":` 守卫尾部块,
            # 本引擎无 exit_reason → 以清标志达成同一守卫 (2026-09-20 修)。
            pending_dn = False
            break

        # 一字跌停: 全天无成交可能, 持仓顺延
        if is_one_word_limit_dn(b, dn):
            last_unfilled = True
            continue
        last_unfilled = False

        ret = (b['close'] / entry_price - 1) * 100
        ret_from_high = (b['close'] / peak - 1) * 100 if peak > 0 else 0
        # 甜点区出场 (E3, 2026-09-22 消融最优): d≥2 且 RSI6∈[80,92] 且 %B≥阈值
        # 通道差异: 核心/高板 → %B≥100 (让利润跑); 温和/强势 → %B≥95 (尽快落袋)
        _in_sweet = False
        if _sweet_on and d >= 2:
            r6, pb = Rs[idx], Ps[idx]
            if r6 is not None and pb is not None:
                _thr = sweet_pctb_core if (entry_gate or "") in ("核心", "高板") else sweet_pctb
                _in_sweet = 80.0 <= r6 <= 92.0 and pb >= _thr

        # T+1: 买入当日(d=1)不可卖出, 仅记录估值
        if d > 1:
            _mbars = ((minute_by_date or {}).get(str(b["time"])[:10])
                      if minute_by_date else None)
            if _mbars:
                # P3b 1m 真实腿: 逐槽位重放止损/追踪 (知日内先后; 线用开盘时已知峰值)
                _fill, _mi, _peak_after = replay_sell_intraday(
                    _mbars, entry_price=entry_price, stop_line=stop_line,
                    trail_pct=trailing_stop, require_profit=entry_price,
                    dn=dn, peak=peak_prev)
                if _peak_after > peak:
                    peak = _peak_after
                if _fill is not None:
                    exit_p, exit_d = _fill, d
                    break
                # 1m 腿未触发 → **不**落回日线 close 口径的止损/追踪: 那条路用"当日 high
                # 抬高后的峰值 + 单点 low", 会把分钟腿刚排除的**日内先视**再引回来。
                # 收盘语义的两条腿 (峰值逃顶 / 到期) 与 1m 无关, 继续在下方判定。
            else:
                # 止损 — 盘中模式: 当日 low 触及线即按线价成交 (跳空按开盘; 贴跌停 → 顺延)
                # 止损线自 D1 起就存在, 无"线在开盘后才形成"的问题 → 不需要 trig_prev 守卫。
                if fill_mode != "close":
                    _fill, _filled = fill_intraday(b, stop_line, side="sell", dn=dn)
                    if _fill is not None:
                        if _filled:
                            exit_p, exit_d = _fill, d
                            _last_rule = "stop"
                            break
                        pending_dn = True       # 成交价贴跌停 → 卖不出
                        continue

                # 止损 (收盘判定 → 收盘价成交)
                if ret <= stop_loss:
                    if dn is not None and b['close'] <= dn * 1.002:
                        pending_dn = True   # 收盘封死跌停 → 卖不出
                        continue
                    exit_p, exit_d = b['close'], d
                    _last_rule = "stop"
                    break

                # 甜点区出场 (E3): 收盘判定 → 收盘价成交
                if _in_sweet:
                    if dn is not None and b['close'] <= dn * 1.002:
                        pending_dn = True
                        continue
                    exit_p, exit_d = b['close'], d
                    _last_rule = "sweet"
                    break

                # 追踪止损 (盈利时) — T3 定稿 (2026-09-22): sweet 模式下保留, 作为浮亏单深跌防线
                # legacy 模式额外支持盘中成交 (fill_mode=intraday)
                if fill_mode == "intraday" and exit_mode != "sweet":
                    # 盘中: 用开盘时已知的 peak_prev 线; 成交价须高于成本 (镜像 ret>0 门)
                    line_prev = peak_prev * (1 + trailing_stop / 100.0)
                    _fill, _filled = fill_intraday(b, line_prev, side="sell", dn=dn)
                    if _filled and _fill > entry_price:
                        exit_p, exit_d = _fill, d
                        _last_rule = "trail"
                        break
                # 追踪止损 (收盘判定 → 收盘价成交)
                if ret_from_high <= trailing_stop and ret > 0:
                    if dn is not None and b['close'] <= dn * 1.002:
                        pending_dn = True
                        continue
                    exit_p, exit_d = b['close'], d
                    _last_rule = "trail"
                    break

                # 峰值逃顶 [仅 legacy 模式]
                if exit_mode != "sweet" and ret > 10:
                    bar_range = b['high'] - b['low']
                    upper = (b['high'] - max(b['open'], b['close'])) / bar_range * 100 if bar_range > 0 else 0
                    if upper > 40 and b['close'] < b['high'] * 0.98:
                        exit_p, exit_d = b['close'], d
                        _last_rule = "escape"
                        break

        exit_p = b['close']; exit_d = d
        _last_rule = "time"

    # 末日落入无法卖出状态 → 顺延下一可交易日开盘强平 (连续一字逐日跳过)
    # 2026-09-26 P1-7b: 骨架走 core.exit_engines.defer_force_open
    if last_unfilled or pending_dn:
        from app.market_cn.auto.core.exit_engines import defer_force_open
        _def = defer_force_open(bars, entry_idx, exit_d,
                                last_unfilled=last_unfilled, pending_dn=pending_dn,
                                board_type=board_type)
        if _def is not None:
            exit_p, exit_d = _def[0], _def[1]

    return {
        'exit_price': round(exit_p, 3), 'exit_day': exit_d,
        'exit_rule': _last_rule,
        'return_pct': round((exit_p / entry_price - 1) * 100, 2),
        'peak_return_pct': round((peak / entry_price - 1) * 100, 2),
    }


# ================================================================
# 以下门表 DSL 私有函数由 strategies 重构从 strategy_funcs 迁入（逐字等价）
# ================================================================
def _bk_ma_bull_at(bars, idx: int):
    """确认日均线多头排列 MA5>MA10>MA20（镜像 break._ma_bull_at）。不足 20 日 → None。"""
    if idx + 1 < 20:
        return None
    c = [Ctx._f(bars[j], "close") for j in range(idx - 19, idx + 1)]
    m5 = sum(c[-5:]) / 5.0
    m10 = sum(c[-10:]) / 10.0
    m20 = sum(c) / 20.0
    return m5 > m10 > m20


def _bk_raw(bars, bt: str, streak_start: int, streak_end: int,
            min_streak: int, max_break_gap: int, asof: int, market=None):
    """断板期『原始结构』—— 镜像 break._break_signal_at 的**结构部分**（不含 5a~5g 判定）。

    返回 None = 结构不成立（连板不足 / 断板期为空 / 越界）。判定（缩量/涨跌/回撤/增强/
    均线）由门表完成；本函数只产出结构量，使门表与参考版逐笔等价且逐门可解释。

    asof: 决策日 i —— 参考版在 bars[:i+1] 上计算（scan_signals 切片），故断板期**只扫到 i**；
          故本函数绝不能用完整 bars 向后扫（否则读到未来 bar，且 break_days 会偏大 → 翻转判定）。
    """
    streak_len = streak_end - streak_start + 1
    if streak_len < min_streak:
        return None
    break_idx = streak_end + 1
    if break_idx >= asof + 1:            # 参考版: break_idx >= len(bars[:i+1])
        return None
    limit_bar = bars[streak_end]
    limit_open = Ctx._f(limit_bar, "open")
    limit_close = Ctx._f(limit_bar, "close")
    limit_vol = Ctx._f(limit_bar, "volume")
    break_days = 0
    # 切片上界 = min(break_idx+max_break_gap, asof+1)，与参考版 bars[:i+1] 逐位一致
    # (2026-09-28 审计 A6: 与参考版同步去掉 +1; 本函数仅由门表适配器调用, 而适配器已先
    #  用 `break_days > max_break_gap → None` 闸过, 故此改动对门表链无行为影响)
    for j in range(break_idx, min(break_idx + max_break_gap, asof + 1)):
        if is_limit_up(Ctx._f(bars[j], "close"), Ctx._f(bars[j - 1], "close"), bt, market):
            break
        break_days += 1
    if break_days == 0:
        return None
    break_bars = bars[break_idx:break_idx + break_days]
    first_break = break_bars[0]
    break_low = min(Ctx._f(b, "low") for b in break_bars)
    break_vol_avg = sum(Ctx._f(b, "volume") for b in break_bars) / len(break_bars)
    break_vol_r = break_vol_avg / limit_vol if limit_vol > 0 else 0.0
    first_break_chg = (Ctx._f(first_break, "close") / limit_close - 1) * 100 if limit_close > 0 else 0.0
    first_break_gap = (Ctx._f(first_break, "open") / limit_close - 1) * 100 if limit_close > 0 else 0.0
    break_drawdown = (break_low / limit_close - 1) * 100 if limit_close > 0 else 0.0
    confirm_bar = break_bars[-1]
    confirm_prev = break_bars[-2] if len(break_bars) >= 2 else limit_bar
    c_pc = Ctx._f(confirm_prev, "close")
    confirm_chg = (Ctx._f(confirm_bar, "close") / c_pc - 1) * 100 if c_pc > 0 else 0.0
    confirm_gap = (Ctx._f(confirm_bar, "open") / c_pc - 1) * 100 if c_pc > 0 else 0.0
    pre20_gain = None
    if streak_start >= 20:
        _ref = Ctx._f(bars[streak_start - 20], "close")
        if _ref > 0:
            pre20_gain = (limit_close / _ref - 1) * 100
    ma_bull = _bk_ma_bull_at(bars, break_idx + break_days - 1)
    return {
        "streak_len": streak_len, "streak_start_idx": streak_start, "streak_end_idx": streak_end,
        "break_idx": break_idx, "break_days": break_days,
        "limit_open": limit_open, "limit_close": limit_close, "limit_vol": limit_vol,
        "break_low": break_low, "break_vol_r": break_vol_r,
        "first_break_chg": first_break_chg, "first_break_gap": first_break_gap,
        "break_drawdown": break_drawdown,
        "confirm_chg": confirm_chg, "confirm_gap": confirm_gap,
        "pre20_gain": pre20_gain, "ma_bull": ma_bull,
        "break_date": bars[break_idx]["time"],
        "streak_start_date": bars[streak_start]["time"],
        "streak_end_date": bars[streak_end]["time"],
    }


def _bk_compute(ctx: Ctx):
    """决策日 i 的断板期候选结构（镜像 break.scan_signals 的 lu_idx 搜索 + 对齐）。

    候选唯一：streak_end = i 之前**最后一个涨停日**（若距 i 超过 max_break_gap 则断板期过长
    → 无候选）；streak_start = 该连板首板（须 is_first：其前 10 日内无涨停）；断板期
    = [streak_end+1, i]，长度须 ≤ max_break_gap 且恰好终止于 i。只读 ≤ i 的 bar（as-of 安全）。
    """
    bars = ctx.bars
    i = ctx.i
    n = ctx.n
    bt = ctx.board_type
    mk = ctx.market
    p = ctx.params
    if i < 2 or i >= n:
        return None
    min_streak = int(p.get("min_streak", 2))
    max_break_gap = int(p.get("max_break_gap", 5))
    # 确认日必为非涨停日（断板期最后一天）
    if is_limit_up(Ctx._f(bars[i], "close"), Ctx._f(bars[i - 1], "close"), bt, mk):
        return None
    # i 之前最后一个涨停日（= streak_end）；断板期 ≤ max_break_gap，故仅需回看该窗口
    streak_end = -1
    j = i - 1
    steps = 0
    while j >= 1 and steps <= max_break_gap:
        if is_limit_up(Ctx._f(bars[j], "close"), Ctx._f(bars[j - 1], "close"), bt, mk):
            streak_end = j
            break
        j -= 1
        steps += 1
    if streak_end < 0:
        return None
    break_days = i - streak_end
    if break_days < 1 or break_days > max_break_gap:
        return None
    # 连板首板（向前回看连续涨停）：仅当前一根 bar 也是涨停时才纳入（与参考版正向延伸对称）
    streak_start = streak_end
    while streak_start - 2 >= 0 and is_limit_up(
            Ctx._f(bars[streak_start - 1], "close"), Ctx._f(bars[streak_start - 2], "close"), bt, mk):
        streak_start -= 1
    # is_first：首板前 10 日内不得有涨停（镜像 break.scan_signals）
    for k in range(1, min(11, streak_start + 1)):
        idx = streak_start - k
        if idx - 1 >= 0 and is_limit_up(Ctx._f(bars[idx], "close"),
                                       Ctx._f(bars[idx - 1], "close"), bt, mk):
            return None
    return _bk_raw(bars, bt, streak_start, streak_end, min_streak, max_break_gap, i, mk)


def bk_struct(ctx: Ctx):
    """决策日 i 的断板期结构（每 Ctx 记忆化：同一天的多个门共享一次计算）。None=非确认日。"""
    cache = ctx.__dict__.setdefault("_bk_cache", {})
    if "s" not in cache:
        cache["s"] = _bk_compute(ctx)
    return cache["s"]


# 非候选日的缺省特征（使各判定门自然失败；候选门 g_candidate 才是真正的闸）
_BK_MISS = {
    "streak_len": 0.0, "break_days": 0.0, "break_vol_r": 0.0,
    "first_break_chg": -1e18, "first_break_gap": -1e18, "break_drawdown": -1e18,
    "confirm_chg": -1e18, "confirm_gap": -1e18, "pre20_gain": -1e18,
    "ma_bull": 0.0, "limit_open": 0.0, "break_low": 0.0, "is_candidate": 0,
}


def bk_feat(ctx: Ctx, name: str):
    """断板期结构特征（供门表表达式引用）。非候选日 → 返回使各门自然失败的缺省值。

    ma_bull 编码: True→1 / None(数据不足)→-1 / False→0 —— 参考版"仅 False 拦截"由门
    表达式 `not ma_bull_filter or bk_feat('ma_bull') != 0` 表达 (None 亦放行)。
    """
    s = bk_struct(ctx)
    if name == "is_candidate":
        return 1 if s is not None else 0
    if s is None:
        return _BK_MISS.get(name, 0.0)
    if name == "pre20_gain":
        v = s["pre20_gain"]
        return v if v is not None else -1e18
    if name == "ma_bull":
        v = s["ma_bull"]
        return 1 if v is True else (-1 if v is None else 0)
    return s.get(name, _BK_MISS.get(name, 0.0))


def pk(ctx: Ctx, name: str):
    """板块感知参数取值：params[name] 为 {board: 值} → 按 ctx.board_type 取；标量原样返回。

    镜像 entry_modes._resolve 的 dict 语义，使同一份门表可对主板/创业板给出不同阈值
    （break 的 vol_max/drawdown_max/stop_loss/... 分板块）。
    """
    v = ctx.params.get(name)
    if isinstance(v, dict):
        if ctx.board_type in v:
            return v[ctx.board_type]
        return v.get("default")
    return v


def turnover_sig(ctx: Ctx) -> float:
    """确认日换手率%(= D0成交量/流通股本*100; 镜像 break 的 turnover_sig 口径)。

    流通股本缺失 (circ<=0) → 返回极大值 = 该门 fail-open 放行（参考版 circ<=0 时跳过该门）。
    """
    circ = float((ctx.stock_info or {}).get("circ_shares") or 0)
    if circ <= 0:
        return 1e18
    return Ctx._f(ctx.bars[ctx.i], "volume") / circ * 100


def break_features(ctx: Ctx, stock_info=None) -> dict:
    """break 信号展示字段（逐字镜像 break._signal_to_legacy_dict + scan_signals 的 extra）。

    门表用 bk_feat 判资格；本函数额外给出**信号展示字段**（连板/断板期/换手率），
    保证与 python 参考版 trades 逐字一致。逻辑单点维护于此。
    """
    s = bk_struct(ctx)
    if s is None:
        return {}
    i = ctx.i
    si = stock_info or {}
    circ = float(si.get("circ_shares") or 0)
    total = float(si.get("total_shares") or 0)
    se = s["streak_end_idx"]
    vol_se = Ctx._f(ctx.bars[se], "volume")
    vol_i = Ctx._f(ctx.bars[i], "volume")
    return {
        "streak_len": s["streak_len"],
        "streak_start": s["streak_start_date"],
        "streak_end": s["streak_end_date"],
        "break_date": s["break_date"],
        "break_days": s["break_days"],
        "break_chg": round(s["first_break_chg"], 2),
        "break_gap": round(s["first_break_gap"], 2),
        "break_vol_r": round(s["break_vol_r"], 2),
        "confirm_chg": round(s["confirm_chg"], 2),
        "confirm_gap": round(s["confirm_gap"], 2),
        "pre20_gain": round(s["pre20_gain"], 2) if s["pre20_gain"] is not None else None,
        "ma_bull": s["ma_bull"],
        "turnover_anchor": round(vol_se / circ * 100, 2) if circ > 0 else None,
        "turnover_sig": round(vol_i / circ * 100, 2) if circ > 0 else None,
        "turnover_anchor_total": round(vol_se / total * 100, 2) if total > 0 else None,
        "turnover_sig_total": round(vol_i / total * 100, 2) if total > 0 else None,
    }

register_strategy_funcs(
    'break',
    {"feat": bk_feat, "turnover_sig": turnover_sig},
)


# ---- exit_modes 注册 (2026-09-26 P1-9 层反转) ----
def _exit_break_combo(bars, entry_idx, entry_price, *, code, board_type, params, diag):
    """断板 combo 出场 (止损/追踪/峰值逃顶/甜点区/到期, 分板块) — 供 YAML exit.mode=break_combo。

    ⚠ 2026-09-28 (A3 配套): 原实现漏传 `exit_mode`/`sweet_pctb`/`sweet_pctb_core`/`entry_gate`
    ⇒ 恒走 `_run_backtest_breakbuy` 默认 (sweet_pctb=95), 而参考版对 **核心/高板** 通道取
    `sweet_pctb_core=100` ⇒ 两链甜点区出场阈值分叉 (实测 000659: 门表 day6/4.44 vs 参考
    day7/4.42), "逐笔等价"破功。口径单源 = 本文件 BOARD_PARAMS (yaml params 同键优先覆写)。
    """
    from app.market_cn.auto.core.exit_modes import _bp
    _bpar = BOARD_PARAMS.get(board_type, BOARD_PARAMS["main"])

    def _pick(name, default):
        """yaml params 覆写优先 (镜像参考版 whitelist update) → BOARD_PARAMS → default。"""
        v = _bp(params, board_type, name)
        if v is None:
            v = _bpar.get(name)
        return default if v is None else v

    return _run_backtest_breakbuy(
        bars, entry_idx, entry_price,
        _bp(params, board_type, "hold_days"),
        _bp(params, board_type, "stop_loss"),
        _bp(params, board_type, "trailing_stop"),
        board_type,
        _bp(params, board_type, "fill_mode"),
        exit_mode=str(_pick("exit_mode", "legacy")),   # 2026-10-07: sweet → legacy (对齐实盘)
        entry_gate=(diag or {}).get("entry_gate"),
        sweet_pctb=float(_pick("sweet_pctb", 95.0)),
        sweet_pctb_core=float(_pick("sweet_pctb_core", 100.0)),
    )


from app.market_cn.auto.core.exit_modes import register_exit as _register_exit
_register_exit("break_combo", _exit_break_combo)


# ================================================================
# 门表回测编排 (2026-09-28 分层改造: 自 core/runtime/evaluate.py **纯搬运**下沉)
# ----------------------------------------------------------------
# 为什么搬回来: 编排是策略的一部分 (枚举顺序/去重键/展示字段集/入场腿), 放在 core
# 会让"改 g56 口径"变成改架构层, 且 core 反过来惰性 import strategies.* (层反转)。
# 自注册到 core/runtime/flows 注册表 → core 只查表, 未登记即 fail-fast (不再静默落 v1)。
# ⚠ 搬运要求: 签名与语义**逐字不变**; 逐笔等价回归见
#    analysis_output/auto架构分层_20260928.md
# ================================================================

from typing import Any, Dict, List
from app.market_cn.auto.core.entry_modes import resolve_entry
from app.market_cn.auto.core.exit_modes import run_exit
from app.market_cn.auto.core.filters import unified_prefilter
from app.market_cn.auto.core.runtime.flows import register_day_flow

def _backtest_day_flow(bars, code, spec, ev, board_type, stock_info, use_prefilter):
    """门表版 break(断板接力) 全历史回测，返回 trades 列表（与 break_buy.backtest_stock 逐笔等价）。

    编排逐字镜像 backtest_stock：确认日必为非涨停 → 廉价预筛(窗口内有涨停) → 门表求值
    → 去重 (streak_start, break_date)（**在 U1~U4 之前**，与参考版同序）→ U1~U4 锚定 D0
    → 入场=entry_modes(open, 无 gap 过滤) → 出场=exit_modes(break_combo)。
    起点 i=4 / 最小长度 6 由 meta.day_start / day_min_n 声明。
    """
    n = len(bars)
    if n < int(spec.meta.get("day_min_n", 6)):
        return []
    # bk_struct / break_features / break_entry_gate 皆为本模块既有实现 (2026-09-28: core 不再惰性 import strategies)
    _p = spec.params
    max_break_gap = int(_p.get("max_break_gap", 5))
    lu_all = find_limit_ups(bars, board_type, spec.market_spec)
    lu_set = set(lu_all)
    trades: List[Dict[str, Any]] = []
    used = set()

    for i in range(int(spec.meta.get("day_start", 4)), n - 1):
        # 确认日必为非涨停日（断板期最后一天）
        if is_limit_up(float(bars[i]["close"]), float(bars[i - 1]["close"]),
                       board_type, spec.market_spec):
            continue
        # 廉价预过滤：断板期结束于 i → 必存在距 i 不超过 max_break_gap 的涨停日
        if not any(j in lu_set for j in range(max(1, i - max_break_gap), i)):
            continue
        # 逐日候选判定（门表一次性求所有门；Ctx 复用给 bk_struct，命中记忆化）
        ctx = Ctx(bars, i, lu_idx=0, params=_p, board_type=board_type,
                  code=code, stock_info=stock_info, market=spec.market_spec)
        ok, _ = ev.evaluate_all(bars, i, _p, ctx=ctx)
        if not ok:
            continue
        s = bk_struct(ctx)
        if not s:
            continue
        # 去重: 同一连板起点+断板日只取一次（在 U1~U4 之前，与参考版同序）
        key = (s["streak_start_date"], s["break_date"])
        if key in used:
            continue
        used.add(key)

        # U1~U4（锚定确认日 D0；信号日 prefilter_anchor='signal'）
        if use_prefilter:
            ok, _ = unified_prefilter(bars, i, code, stock_info, spec.market_spec)
            if not ok:
                continue

        # 入场 = entry_modes（open，break 无 gap 过滤）
        site, _reason = resolve_entry(spec.entry, bars, i, board_type, _p)
        if site is None:
            continue

        # 信号展示字段（镜像 _signal_to_legacy_dict + scan_signals.extra）
        feats = break_features(ctx, stock_info=stock_info)
        # 入场通道标注 (2026-09-28 A3 配套): 与参考版同一 `break_entry_gate` — 板块来自 code、
        # 涨停走共享 is_limit_up。既作展示字段, 又经 diag.entry_gate 决定甜点区阈值
        # (核心/高板 → sweet_pctb_core=100; 其余 → sweet_pctb=95)。修复两链出场分叉。
        _gate, _gpctb, _gbd = break_entry_gate(bars, i, feats.get("streak_len") or 0, code)
        site["diag"]["entry_gate"] = _gate

        # 出场 = exit_modes（break_combo 收盘价口径）
        result = run_exit(spec.exit.get("mode", "break_combo"), bars=bars,
                          entry_idx=site["entry_idx"], entry_price=site["entry_price"],
                          code=code, board_type=board_type, params=_p, diag=site["diag"])
        if not result:
            continue

        d1 = bars[i + 1]
        prev_close = float(bars[i]["close"])
        trades.append({
            "code": code,
            "board": get_board_name(code, spec.market_spec),
            "path": "break_buy",
            "path_label": spec.meta.get("name", spec.key),
            "mode": "streak_break",
            "streak_len": feats.get("streak_len"),
            "streak_start": feats.get("streak_start"),
            "streak_end": feats.get("streak_end"),
            "break_date": feats.get("break_date"),
            "signal_date": bars[i]["time"],
            "break_days": feats.get("break_days"),
            "break_chg": feats.get("break_chg"),
            "break_gap": feats.get("break_gap"),
            "break_vol_r": feats.get("break_vol_r"),
            "confirm_chg": feats.get("confirm_chg"),
            "confirm_gap": feats.get("confirm_gap"),
            "pre20_gain": feats.get("pre20_gain"),
            "ma_bull": feats.get("ma_bull"),
            "entry_gate": _gate,
            "entry_pctb": _gpctb,
            "entry_bd": _gbd,
            "turnover_anchor": feats.get("turnover_anchor"),
            "turnover_sig": feats.get("turnover_sig"),
            "turnover_anchor_total": feats.get("turnover_anchor_total"),
            "turnover_sig_total": feats.get("turnover_sig_total"),
            "entry_price": round(float(site["entry_price"]), 3),
            "buy_mode": "next_open",
            "entry_date": site["entry_date"],
            "d1_change": round((float(d1["close"]) / float(d1["open"]) - 1) * 100, 2)
            if float(d1["open"]) > 0 else 0,
            "d1_gap": round((float(d1["open"]) / prev_close - 1) * 100, 2)
            if prev_close > 0 else 0,
            "intraday": round((float(d1["close"]) - float(d1["open"])) / prev_close * 100, 2)
            if prev_close > 0 else 0,
            **result,
        })
    return trades


# ================================================================
# 枚举方式 B / day_flow=g56（五重共振，D-1 信号 → D0 开盘入场 + 锁仓去重）
# 逐字镜像 g56.G56Strategy.backtest_stock（见 tmp/_g56_equivalence.py 验收）
# ================================================================

register_day_flow("break", _backtest_day_flow)

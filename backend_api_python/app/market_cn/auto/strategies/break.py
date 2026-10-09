"""strategies/break.py — 断板接力策略。

入场 (D0 盘后扫描 → D1 竞价):
  连板≥2 → 断板期(≤max_break_gap天) → 确认日=断板期最后一天 → D1 开盘买入
  断板期检查 5a~5f: 低点不破涨停日开盘 / 缩量1.2~2.0x / 首断日涨跌+gap 区间 /
  回撤不破限 / 确认日增强过滤(三通道OR: 企稳[0,2) | 均量比≥1.4 | 前20日涨幅≥30)
  竞价: 无 gap 过滤 (恒可买, gap 判定交给 D1 数据)
评分 = 50 + 确认日涨幅% × 3, clip [0,100] —— **展示分, 非质量分** (依据见 SCORE_* 处)
  换手率门: 确认日换手 < turnover_min (config params, None=关) → 剔除
  U1~U4: prefilter_anchor='signal' (锚定确认日=末根bar; 连板≥2已隐含U4)

出场 (收盘价判定, monitor break 分支 / run_backtest_breakbuy 语义):
  止损 main-8%/gem-10% / 追踪止损(自入场峰值, 需 ret>0) / 峰值逃顶(ret>10%+上影>40%+收盘<high*0.98) / 到期 main20/gem15天

易错点:
  - 确认日 = 断板期最后一天 (break_idx+break_days-1 == D0), 不是首断板日;
  - 断板期的 5c/5d 上界 (+8%/+5%) 是硬编码, 与 BOARD_PARAMS 无关 — 勿"配置化";
  - exit 是收盘价口径 (close 判定), 与 v1 的 low 触及口径不同 — 勿混用;
  - 追踪止损要求 ret>0 (盈利中才追踪), 与止损分支互斥由 ret<=stop 先拦。

评分值域/判别力实测等依据 → `docs/策略研究依据归档.md#break`。
"""
from __future__ import annotations

from app.market_cn.auto.sampler import build_day_sample

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
BREAK_WIN = 100           # 递推窗口 (判定单源到门表后由门表反推候选: 需覆盖最长连板 + pre20 基准 + is_first; 原 60 + pad 哑 bar 已废)
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


_SPEC: dict = {}


def _break_spec():
    """门表 StrategySpec 单例缓存（判定单源到门表后 evaluate 用它求门）。

    懒加载: 首次调用才 `load_strategy("break")`（读 yaml + as-of 静态校验，较慢），
    之后复用。宏（规则）一经加载即稳定，热重载走 flows 的 replace 机制。
    """
    if "break" not in _SPEC:
        from app.market_cn.auto.core.runtime.evaluate import load_strategy
        _SPEC["break"] = load_strategy("break")
    return _SPEC["break"]


# ================================================================
# 断板期判定 — 已退役 (2026-10-09 P6): 判定单源到门表后无调用点。
# ----------------------------------------------------------------
# 原 `_break_signal_at`(结构+5a~5g 判定) 与私有 helper `_ma_bull_at` 均已删除,
# 其等价实现现为门表 DSL 的 `bk_struct` / `bk_feat` / `_bk_raw`(结构) +
# break.yaml 判定门 (5a~5g)。逐笔等价由门表 runner 背书。
# 关键历史口径 (保留作参考, 勿回退手写):
#   - 断板期上界 = max_break_gap (2026-09-28 审计 A6, 原 +1 是 off-by-one);
#   - 5c/5d 上界 +8%/+5% 硬编码, 与 BOARD_PARAMS 无关;
#   - 确认日 = 断板期最后一天 (break_idx+break_days-1 == D0);
#   - 5g 均线多头: 仅 False 拦截, None(数据不足) 放行。
# ================================================================


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
    """Signal → 展示用 dict 形态。

    **易错点**: 必须显式列字段 — 不含 break_idx (内部变量), 全量透传 extra
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
    # 展示阶段表: 递推侧只产出**信号日** (ready)。
    # ⚠ 出场生命周期不在递推侧重放 —— break 是多日追踪止损
    #   (exit_decision / core.exit_engines), 与 knife/tail 的 "D1 开盘即平账" 不同。
    stages = (
        Stage("ready", "断板确认·准备", realtime="09:25"),
        Stage("exec", "次日开盘买入", realtime="09:31"),
        Stage("exit", "出场", realtime=None, visible=True),
    )

    # ---- 信号判定 ----
    def scan_signals(self, bars, code, *, as_of=None, ctx=None, limit_ups=None,
                     probe=None, **params):
        """今日是否为断板期确认日 → Signal (至多1笔)。as_of=k: 只用 bars[:k+1]。

        判定单源（切口 1b）: 复用 `_scan_one`（门表单日），不再手写 `_break_signal_at`。
        trace 经 ctx["_trace"]（或 probe）打 confirm/prefilter/signal（细门在门表内）。
        """
        # trace 统一分发: ctx["_trace"].note 与 probe.trace 同打点同格式
        _sink = (ctx or {}).get("_trace")

        def _emit(stage, **kw):
            if _sink is not None:
                _sink.note(stage, **kw)
            elif probe is not None:
                probe.trace(stage, **kw)

        if as_of is not None:
            bars = bars[:as_of + 1]
        n = len(bars)
        if n < 3:
            return []
        i = n - 1
        if i < 2:
            return []

        spec = _break_spec()
        from app.market_cn.auto.core.runtime.evaluate import GateEvaluator
        bt = get_board_type(code, spec.market_spec)
        si = (params or {}).get("stock_info")
        ev = GateEvaluator(spec, board_type=bt, code=code, stock_info=si)
        sig = _scan_one(spec, ev, bars, i, bt, si, emit=_emit)
        return [sig] if sig is not None else []

    # ================================================================
    # 递推展示层契约 (2026-10-06)
    #
    # 门判定  → 门表 `bk_struct`/`bk_feat` + `GateEvaluator`（全量/递推共用）
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
        """信号日判定 + 结算链（判定规则单源到门表：候选定位 + 5a~5g 走 bk_struct + GateEvaluator）。

        三段形态：
          - prev=None（stateless / 生产投影）：只产 ready —— 与 scan_signals 同口径。
          - prev=ready：今日 = D1 ⇒ 产 exec（入场价 = 今日开盘）。
          - prev=exec ：持仓中 ⇒ 调 `exit_decision`（唯一出场实现）判定 ⇒ 产 exit。

        ⚠ 结算分支**只在 prev 非 None 时触发**：stateless 下恒不进入 ⇒ 生产投影
        口径零变化（改进方案 §2.6c「ready-only → 策略侧补结算事件」）。
        """
        bar, code = inp.bar, inp.code
        today = self._rec(bar)

        # ---- 结算分支（stateful 专属）----
        if prev is not None:
            stage = getattr(prev, "stage", "")
            if stage == "ready":
                return self._exec_event(state, inp, prev)
            if stage == "exec":
                return self._exit_event(state, inp, prev)

        # ---- 判定分支（stateless / stateful 共用；判定规则单源到门表）----

        wb = [{"time": r["t"], "open": r["o"], "high": r["h"], "low": r["l"],
               "close": r["c"], "volume": r["v"]} for r in state["win"]] + [{
                   "time": today["t"], "open": today["o"], "high": today["h"],
                   "low": today["l"], "close": today["c"], "volume": today["v"]}]
        bt = state["board"]
        si = (inp.ctx or {}).get("stock_info") or {}
        _tr = (inp.ctx or {}).get("_trace")   # 门原因通道（契约约定）：落选/命中进 TraceSink
        # 门诊断通道（Step 1）：ctx["_gate_dbg"] 注入全门向量回调（与链 A gate_dbg 同签名），
        # 无则 None（零开销）。evaluate_all 在 gate_dbg 非 None 时自动 fire phase="all"。
        _gate_dbg = (inp.ctx or {}).get("_gate_dbg")

        # 判定单源（切口 1）: 候选定位 + 5a~5g + 换手率门全部走门表（bk_struct 反推候选
        # + GateEvaluator 求门），不再遍历递推 runs / 手写 _break_signal_at。
        from app.market_cn.auto.core.runtime.evaluate import GateEvaluator, build_signal
        spec = _break_spec()
        ev = GateEvaluator(spec, board_type=bt, code=code, stock_info=si, gate_dbg=_gate_dbg)
        i = len(wb) - 1
        # 候选预筛（fire 时机对齐链 A `_backtest_day_flow`）：确认日非涨停 + 距涨停 ≤
        # max_break_gap。非候选日直接返回，不 fire gate_dbg（否则门诊断 fire 集合与链 A
        # 不一致，explain 漏斗「到达 n」口径失真）。预筛与门表 g_candidate 同必要条件，
        # 判定结论不变（g_candidate 门兜底拦截），仅对齐 fire 粒度。
        _mx = int(spec.params.get("max_break_gap", 5))
        if i >= 1 and is_limit_up(float(wb[i]["close"]), float(wb[i - 1]["close"]),
                                  bt, spec.market_spec):
            return []
        _lu_set = set(find_limit_ups(wb, bt, spec.market_spec))
        if not any(j in _lu_set for j in range(max(1, i - _mx), i)):
            return []
        ctx = Ctx(wb, i, lu_idx=0, params=spec.params, board_type=bt,
                  code=code, stock_info=si, market=spec.market_spec)
        # 门诊断 fire 用**绝对索引**（state["abs_i"] 截至昨日 ⇒ 今日信号日 = abs_i+1），
        # 对齐链 A `_backtest_day_flow` 的 i（信号日绝对索引）；ctx 已给定故 i 只影响
        # gate_dbg fire，不影响求值。
        ok, failed = ev.evaluate_all(wb, state["abs_i"] + 1, spec.params, ctx=ctx)
        if _tr is not None and not ok:
            # 门原因（保持 note 粗打点与旧 evaluate 兼容；细粒度 gate 向量留作后续）
            _tr.note("no_signal", gate=(failed or [None])[0])
        if not ok:
            return []
        if not bk_struct(ctx):
            return []
        feats = break_features(ctx, stock_info=si)
        if not feats:
            return []
        # 展示字段单源到宏 (D6): extra 由 `signal.fields` 经 build_signal 求值产出,
        # 与生产单日 `_scan_one` 同一份宏、同一口径 (逐字段等价由 test_projection 背书)。
        extra = build_signal(ctx, spec)
        if _tr is not None:
            _tr.note("signal", cand=feats.get("break_date"))
        return [Progress(stage="ready", date=str(bar.get("time", "")), payload={
            "price": 0.0,                        # 断板信号日不定价 (entry=D1开盘)
            "score": _score_of(float(feats.get("confirm_chg", 0) or 0)),
            "label": "断板", "extra": extra,
        }, next_realtime="09:25")]

    # ---- 结算事件（P3 §2.6c：break 事件链完备化，仅 stateful 触发）----
    def _exec_event(self, state: dict, inp: DayInput, prev) -> list:
        """prev=ready 且今日为 D1 ⇒ 产 exec（入场价 = 今日开盘，对齐旧 backtest_stock）。

        旧 `backtest_stock` 的入场段只拦 `entry_price <= 0`（无 gap 门），此处同口径。
        """
        bar, code = inp.bar, inp.code
        entry_price = float(bar.get("open") or 0)
        if entry_price <= 0:
            return []
        return [Progress(stage="exec", date=str(bar.get("time", "")), payload={
            "entry_date": str(bar.get("time", ""))[:10],
            "entry_price": entry_price,
            "entry_idx": state["abs_i"] + 1,        # 绝对下标（持仓天数用）
            "buyable": True,
            # P5-④ 前置: 买入当日 15:01 需实时确认（持仓 or 当日出场）—— 与 monitor
            # `W_CONFIRM_LO` 的 15:01 确认窗同一时点。此前 exec 不带锚 ⇒ 确认恒回退。
        }, next_realtime="15:01")]

    def _exit_event(self, state: dict, inp: DayInput, prev) -> list:
        """prev=exec ⇒ 调 `exit_decision`（唯一出场实现）判定今日是否离场。

        `exit_decision` 需要「入场以来至今的 bars 片段 + entry_idx 相对位置」；此处
        由 `state["win"]`（ring 窗口，60 根 ≫ hold_days=7）按 entry_date 定位切片。
        ⚠ 不在本方法内重写任何出场规则 —— 那是第二份回测。
        """
        bar, code = inp.bar, inp.code
        pl = getattr(prev, "payload", None) or {}
        entry_price = float(pl.get("entry_price") or 0)
        if entry_price <= 0:
            return [Progress(stage="exit", date=str(bar.get("time", "")), payload={
                "exit_reason": "入场价缺失", "exit_price": 0.0})]

        today = self._rec(bar)
        win = list(state["win"]) + [today]
        entry_date = str(pl.get("entry_date") or "")[:10]
        pos = next((k for k in range(len(win) - 1, -1, -1)
                    if str(win[k]["t"])[:10] == entry_date), None)
        if pos is None:                              # 窗口已滚掉入场日（>60 根，不应发生）
            pos = max(0, len(win) - 2)
        seg = [{"open": r["o"], "high": r["h"], "low": r["l"], "close": r["c"]}
               for r in win[pos:]]
        dec = self.exit_decision(
            {"code": code, "entry_price": entry_price},
            snap={"mode": "day_close", "bars": seg, "entry_idx": 0})
        if dec.action != "exit":
            return []
        # ⚠ `dec.price` 是**理论触发价**（追踪止损 = peak×0.94，实盘挂单用）；回测默认
        #   fill_mode="close" = 收盘判定 + **收盘价成交**（对齐 `_run_backtest_breakbuy`）。
        #   用 dec.price 成交会高估收益（实测 9.46% vs 3.73%）。
        # 2026-10-07: `fill="open"` = 顺延强平 (昨日封跌停卖不出, 今日开盘成交) ⇒
        #   按**开盘价**成交 (旧引擎 pending_dn 的次日 b['open'])。其余情形维持收盘价
        #   成交 (对齐 fill_mode="close")。
        exit_price = (float(today["o"]) if getattr(dec, "fill", "") == "open"
                      else float(today["c"]))
        peak = max(float(r["h"]) for r in win[pos:])
        return [Progress(stage="exit", date=str(bar.get("time", "")), payload={
            "exit_date": str(bar.get("time", ""))[:10],
            "exit_price": exit_price,
            "exit_reason": dec.reason,
            "exit_day": len(win) - pos,
            "return_pct": round((exit_price / entry_price - 1) * 100, 2),
            "peak_return_pct": round((peak / entry_price - 1) * 100, 2),
        })]

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
    #: 旧引擎 `_run_backtest_breakbuy` 里带「跌停卖不出 ⇒ 顺延次日开盘」(pending_dn) 的腿。
    #: ⚠ 逃顶/到期腿**不在内** —— 旧引擎对这两条腿不设 pending_dn (到期走尾部
    #:   defer_force_open) ⇒ 给它们加判定反而会造出新的不等价。
    _DN_GUARDED_REASONS = ("止损", "追踪止损")

    def exit_decision(self, row, snap=None, **params):
        """收盘价口径 (monitor break 分支 / run_backtest_breakbuy 语义):
        止损 / 追踪止损(ret>0) / 峰值逃顶 / 到期。live 模式 → hold (硬止损在 monitor 主循环)。

        2026-10-07 补「跌停不可卖」—— §5.1 逐笔对拍暴露的 **replay 侧缺陷**:
          旧引擎在止损/甜点/追踪三条腿上都有 `close <= dn*1.002 ⇒ pending_dn` 顺延
          (次日开盘 b['open'] 强平), 且一字跌停 `is_one_word_limit_dn` 整天无法成交;
          本函数此前**一条都没有** ⇒ 贴跌停日仍按收盘价成交, 产生物理上不可能的成交。
          实测 000506: 2026-06-23 收盘 13.54 (跌停价 15.04×0.9=13.536) ⇒ 旧引擎顺延至
          06-24 开盘 13.2 成交, replay 却当日 13.54 成交。现按同口径补齐。
        """
        if not isinstance(snap, dict) or snap.get("mode") != "day_close":
            return ExitDecision("hold")
        bars = snap.get("bars")
        entry_idx = snap.get("entry_idx")
        entry_price = float(row.get("entry_price") or 0)
        if bars is None or entry_idx is None or entry_price <= 0:
            return ExitDecision("hold")
        bt = get_board_type(row.get("code", ""))
        # 出场参数单源到 yaml (spec.params 权威, 分板块 {main/gem_star}); BOARD_PARAMS
        # 降级为兜底默认值 (二者值一致, 2026-10-09)。显式 **params 覆写优先。
        from app.market_cn.auto.core.exit_modes import _bp as _bp_exit
        _sp = _break_spec().params
        _bp_fb = BOARD_PARAMS.get(bt, BOARD_PARAMS["main"])
        stop = _bp_exit(params, bt, "stop_loss") if params else None
        trail = _bp_exit(params, bt, "trailing_stop") if params else None
        hold = _bp_exit(params, bt, "hold_days") if params else None
        if stop is None:
            stop = _bp_exit(_sp, bt, "stop_loss")
            if stop is None:
                stop = _bp_fb["stop_loss"]
        if trail is None:
            trail = _bp_exit(_sp, bt, "trailing_stop")
            if trail is None:
                trail = _bp_fb["trailing_stop"]
        if hold is None:
            hold = _bp_exit(_sp, bt, "hold_days")
            if hold is None:
                hold = _bp_fb["hold_days"]

        def _dn_at(i):
            """bars[i] 的跌停价 (以前一日收盘为基准, 与旧引擎 dn 同式)。"""
            pc = float(bars[i - 1]["close"]) if i > 0 and bars[i - 1].get("close") else 0
            return _limit_dn_price(pc, bt) if pc > 0 else None

        def _decide(end_idx):
            """截至 bars[end_idx] 的出场判定 → (是否出场, reason, price)。"""
            seg = bars[entry_idx:end_idx + 1]
            if not seg:
                return False, "", 0.0
            peak = max(float(b["high"]) for b in seg)
            lb = bars[end_idx]
            r = (lb["close"] / entry_price - 1) * 100
            rfh = (lb["close"] / peak - 1) * 100 if peak > 0 else 0
            held = end_idx - entry_idx + 1
            if r <= stop:
                return True, f"止损{stop}%", entry_price * (1 + stop / 100)
            if rfh <= trail and r > 0:
                return True, f"追踪止损{trail}%", peak * (1 + trail / 100)
            if r > 10:
                bar_range = lb["high"] - lb["low"]
                upper = ((lb["high"] - max(lb["open"], lb["close"])) / bar_range * 100
                         if bar_range > 0 else 0)
                if upper > 40 and lb["close"] < lb["high"] * 0.98:
                    return True, "峰值逃顶", float(lb["close"])
            if held >= hold:
                return True, f"持仓到期{hold}天", float(lb["close"])
            return False, "", 0.0

        today_idx = len(bars) - 1
        if today_idx < entry_idx:
            return ExitDecision("hold")

        # ① 昨日触发但该日封跌停卖不出 ⇒ 今日开盘强平 (旧引擎 pending_dn → 次日 b['open'])
        if today_idx - 1 >= entry_idx:
            y_trig, y_reason, _yp = _decide(today_idx - 1)
            y_dn = _dn_at(today_idx - 1)
            if (y_trig and y_reason.startswith(self._DN_GUARDED_REASONS)
                    and y_dn is not None
                    and float(bars[today_idx - 1]["close"]) <= y_dn * 1.002):
                return ExitDecision("exit", reason=y_reason,
                                    price=float(bars[today_idx]["open"]),
                                    fill="open")      # ⇒ 调用方按开盘价成交

        dn_today = _dn_at(today_idx)
        # ② 一字跌停: 全天无成交可能 ⇒ 持仓顺延 (旧引擎 is_one_word_limit_dn → continue)
        if dn_today is not None and is_one_word_limit_dn(bars[today_idx], dn_today):
            return ExitDecision("hold")

        trig, reason, price = _decide(today_idx)
        if not trig:
            return ExitDecision("hold")

        # ③ 今日触发但收盘贴跌停 ⇒ 当日卖不出, 顺延次日开盘 (旧引擎 pending_dn)
        if (reason.startswith(self._DN_GUARDED_REASONS) and dn_today is not None
                and float(bars[today_idx]["close"]) <= dn_today * 1.002):
            return ExitDecision("hold")
        return ExitDecision("exit", reason=reason, price=price)

    # ---- 回测钩子 (2026-09-10 自 backtest.backtest_break_stock 逐字搬入, 对数零差异) ----
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
    """确认日均线多头排列 MA5>MA10>MA20。不足 20 日 → None。"""
    if idx + 1 < 20:
        return None
    c = [Ctx._f(bars[j], "close") for j in range(idx - 19, idx + 1)]
    m5 = sum(c[-5:]) / 5.0
    m10 = sum(c[-10:]) / 10.0
    m20 = sum(c) / 20.0
    return m5 > m10 > m20


def _bk_raw(bars, bt: str, streak_start: int, streak_end: int,
            min_streak: int, max_break_gap: int, asof: int, market=None):
    """断板期『原始结构』—— 断板期的**结构部分**（不含 5a~5g 判定）。

    返回 None = 结构不成立（连板不足 / 断板期为空 / 越界）。判定（缩量/涨跌/回撤/增强/
    均线）由门表完成；本函数只产出结构量，使门表逐门可解释。

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


# ---- 信号展示字段原语 (2026-10-09 D6 接线: 宏 break.yaml `signal.fields` 唯一事实源) ----
# ⚠ 与门表 `feat(...)` (=bk_feat) 是**两套口径, 勿混用**：
#   bk_feat 为门判定服务 —— 非候选日返回 `_BK_MISS` 失败哨兵 (first_break_chg=-1e18)、
#   ma_bull 编码为 1/-1/0 供门表达式判等；这些值直接展示会失真。
#   展示需**原始值** (None / True|False / 真实 round 值), 故另立展示专用访问原语。
def bkf(ctx: Ctx, name: str):
    """展示字段访问原语：取 `break_features` 的单个字段（原始值, 已 round）。

    .py 提供"词"(结构字段访问), yaml `signal.fields` 决定"展示哪些"。
    `break_features` 是唯一计算点 (生产单日 `_scan_one` 与主干折叠 `evaluate` 共用)。
    """
    return break_features(ctx, stock_info=ctx.stock_info).get(name)


def _bk_streak_len(ctx: Ctx) -> int:
    s = bk_struct(ctx)
    return int((s or {}).get("streak_len") or 0)


def _bk_entry_gate(ctx: Ctx):
    """入场通道 (gate, pctb, bd) —— 每 Ctx 记忆化（三字段共享一次 `_entry_gate`）。"""
    cache = ctx.__dict__.setdefault("_bk_cache", {})
    if "eg" not in cache:
        cache["eg"] = _entry_gate(ctx.bars, ctx.i, _bk_streak_len(ctx), ctx.code)
    return cache["eg"]


def entry_gate(ctx: Ctx):
    """入场通道标签（展示字段; 唯一实现 = `_entry_gate`）。"""
    return _bk_entry_gate(ctx)[0]


def entry_pctb(ctx: Ctx):
    """入场日 %B（展示字段; 唯一实现 = `_entry_gate`）。"""
    return _bk_entry_gate(ctx)[1]


def entry_bd(ctx: Ctx):
    """入场日距前一涨停天数 bd（展示字段; 唯一实现 = `_entry_gate`）。"""
    return _bk_entry_gate(ctx)[2]


register_strategy_funcs(
    'break',
    {"feat": bk_feat, "turnover_sig": turnover_sig,
     "bkf": bkf, "entry_gate": entry_gate, "entry_pctb": entry_pctb, "entry_bd": entry_bd},
)


# ---- exit_modes 注册 (2026-09-26 P1-9 层反转) ----
def _exit_by_decision(bars, entry_idx, entry_price, code, board_type, params=None):
    """逐日调 exit_decision 模拟持仓出场 → dict（等价 _run_backtest_breakbuy 的 legacy close 路径）。

    切口 2（出场单源）: 回测/门表的出场与展示/实时走**同一份** `exit_decision` 判定，
    等价性由 tmp/verify_exit_equivalence.py 背书（300 票 12 笔 0 不一致）。
    参数单源到 yaml (spec.params 权威, 分板块; BOARD_PARAMS 兜底, 值一致, 2026-10-09)。
    """
    from app.market_cn.auto.core.exit_modes import _bp as _bp_exit
    _sp = _break_spec().params
    _hold = _bp_exit(_sp, board_type, "hold_days")
    if _hold is None:
        _hold = BOARD_PARAMS.get(board_type, BOARD_PARAMS["main"])["hold_days"]
    hold = int(_hold)
    strat = BreakStrategy()
    _RULE = {"止损": "stop", "追踪": "trail", "峰值": "escape", "持仓到期": "time"}
    for d in range(1, hold + 1):
        idx = entry_idx + d - 1
        if idx >= len(bars):
            break
        seg = bars[entry_idx:idx + 1]
        snap = {"mode": "day_close", "bars": seg, "entry_idx": 0}
        dec = strat.exit_decision({"entry_price": entry_price, "code": code}, snap)
        if dec.action == "exit":
            px = (float(bars[idx]["open"]) if getattr(dec, "fill", "") == "open"
                  else float(bars[idx]["close"]))
            peak = max(float(b["high"]) for b in seg)
            rule = next((v for k, v in _RULE.items()
                         if str(dec.reason).startswith(k)), "time")
            return {
                "exit_price": round(px, 3), "exit_day": d, "exit_rule": rule,
                "return_pct": round((px / entry_price - 1) * 100, 2),
                "peak_return_pct": round((peak / entry_price - 1) * 100, 2),
            }
    last_idx = min(entry_idx + hold - 1, len(bars) - 1)
    last_px = float(bars[last_idx]["close"])
    peak = max(float(b["high"]) for b in bars[entry_idx:last_idx + 1])
    return {
        "exit_price": round(last_px, 3), "exit_day": hold, "exit_rule": "time",
        "return_pct": round((last_px / entry_price - 1) * 100, 2),
        "peak_return_pct": round((peak / entry_price - 1) * 100, 2),
    }


def _exit_break_combo(bars, entry_idx, entry_price, *, code, board_type, params, diag):
    """断板 combo 出场 (止损/追踪/峰值逃顶/甜点区/到期, 分板块) — 供 YAML exit.mode=break_combo。

    切口 2（出场单源）: 默认（legacy close）走 `_exit_by_decision`（逐日 exit_decision，
    与展示/实时同源）；扩展（sweet 甜点区 / fill_mode=intraday 盘中）保留
    `_run_backtest_breakbuy`。
    """
    from app.market_cn.auto.core.exit_modes import _bp
    _bpar = BOARD_PARAMS.get(board_type, BOARD_PARAMS["main"])

    def _pick(name, default):
        """yaml params 覆写优先 (镜像参考版 whitelist update) → BOARD_PARAMS → default。"""
        v = _bp(params, board_type, name)
        if v is None:
            v = _bpar.get(name)
        return default if v is None else v

    exit_mode = str(_pick("exit_mode", "legacy"))
    fill_mode = _bp(params, board_type, "fill_mode")
    if exit_mode != "sweet" and fill_mode in (None, "", "close"):
        # 默认 legacy close：出场单源到 exit_decision（逐日判定）
        return _exit_by_decision(bars, entry_idx, entry_price, code, board_type, params)

    return _run_backtest_breakbuy(
        bars, entry_idx, entry_price,
        _bp(params, board_type, "hold_days"),
        _bp(params, board_type, "stop_loss"),
        _bp(params, board_type, "trailing_stop"),
        board_type,
        fill_mode,
        exit_mode=exit_mode,
        entry_gate=(diag or {}).get("entry_gate"),
        sweet_pctb=float(_pick("sweet_pctb", 95.0)),
        sweet_pctb_core=float(_pick("sweet_pctb_core", 100.0)),
    )


from app.market_cn.auto.core.exit_modes import register_exit as _register_exit
_register_exit("break_combo", _exit_break_combo)


# ================================================================
# 门表单日判定 (2026-09-28 下沉 / 2026-10-09 终态② Step 3 退役回测编排)
# ----------------------------------------------------------------
# 2026-09-28: 原 core/runtime/evaluate.py 的门表回测编排**纯搬运**回本模块自注册
#   (编排是策略的一部分: 枚举顺序/去重键/展示字段集/入场腿; 放 core 会让"改口径"
#    变成改架构层, 且 core 反过来惰性 import strategies.* = 层反转)。
# 2026-10-09 终态② Step 3: 回测主路径收敛到事件流折叠 (backtest_stock 薄壳 → core.replay)
#   ⇒ 全历史回测编排 `_backtest_day_flow` 与 register_day_flow 注册**退役**;
#   本模块仅保留 `_scan_one` (register_scan_one) 供生产单日判定 (scan_day) 使用。
# 逐笔等价回归见 analysis_output/auto架构分层_20260928.md
# ================================================================

from app.market_cn.auto.core.filters import unified_prefilter
from app.market_cn.auto.core.runtime.flows import register_scan_one


def _scan_one(spec, ev, bars, i, board_type, stock_info, emit=None):
    """break 单日判定 → Signal|None（门表引擎，生产链 scan_day 用）。

    与 `_backtest_day_flow` 的循环体同源，但只做单日判定、不做出场模拟、不去重
    （去重是回测/写库层的事）。Signal.extra 与折叠 `evaluate._signal` 逐字段一致。
    emit: 可选 trace 回调 (stage, **kw)，采样器 trace 用（confirm/prefilter/signal）。
    """
    code = ev.code
    if len(bars) < int(spec.meta.get("day_min_n", 6)):
        return None
    _p = spec.params
    lu_set = set(find_limit_ups(bars, board_type, spec.market_spec))
    if is_limit_up(float(bars[i]["close"]), float(bars[i - 1]["close"]),
                   board_type, spec.market_spec):
        return None
    if not any(j in lu_set for j in range(max(1, i - int(_p.get("max_break_gap", 5))), i)):
        return None
    ctx = Ctx(bars, i, lu_idx=0, params=_p, board_type=board_type,
              code=code, stock_info=stock_info, market=spec.market_spec)
    ok, failed = ev.evaluate_all(bars, i, _p, ctx=ctx)
    if not ok:
        if emit is not None:
            gid = (failed or [None])[0]
            emit("prefilter" if gid == "g_turnover" else "confirm",
                 code=code, d0_date=str(bars[i]["time"])[:10], gate=gid)
        return None
    if not bk_struct(ctx):
        if emit is not None:
            emit("confirm", code=code, d0_date=str(bars[i]["time"])[:10])
        return None
    ok, _ = unified_prefilter(bars, i, code, stock_info, spec.market_spec)
    if not ok:
        if emit is not None:
            emit("prefilter", code=code, d0_date=str(bars[i]["time"])[:10])
        return None
    feats = break_features(ctx, stock_info=stock_info)
    if not feats:
        return None
    # 展示字段单源到宏 (D6): extra 由 `signal.fields` 经 build_signal 求值产出
    # (与折叠 `evaluate` 同一份宏; 逐字段等价由 test_projection 背书)。
    from app.market_cn.auto.core.runtime.evaluate import build_signal
    extra = build_signal(ctx, spec)
    if emit is not None:
        emit("signal", code=code, d0_date=str(bars[i]["time"])[:10])
    return Signal(code=code, time=str(bars[i]["time"])[:10],
                  score=_score_of(float(feats.get("confirm_chg", 0) or 0)),
                  price=0.0, label="断板", extra=extra)


register_scan_one("day", "break", _scan_one)

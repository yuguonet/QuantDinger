"""strategies/g56.py — 五重共振 (D-1 盘后判定 → D0 开盘买; 纯 7d/-8% 出场)。

入场 = 五重共振缺一不可 (精确阈值 = 门表 `g56.yaml` gates; `_g1_mask` 为等价向量化预筛):
  ① 趋势 MA5/MA10 收敛 + rma_chg>0    ② 波动 ATR14% > 板块Q5
  ③ 基因 前20日≥5%大涨≥2次           ④ 金叉 rhist_chg 板块池内前5%
  ⑤ 板块 regime 门 (主板 rmed>0.25 & %b≤49.87 / 20cm score_r>0.65 & dif0≤0)
出场 = 持有期 d≥2 任一日 low≤入场价×0.92 → 止损卖 (跳空按开盘); 否则第 7 日收盘卖。
评分 = 0.5·归一(dist_ma20) + 0.5·归一(−dif0) —— 展示 + 实盘每日限额截断键。

易错点 (改对红线):
  - **全部 G1 指标必须因果** (f[k] 只依赖 ≤k 数据) ⇒ 可用未切片全序列一次算第 k 日特征;
    将来若引入非因果指标 (centered MA / 未来值回填), 此前提失效, 必须回退切片否则前视。
  - 前视防护: 池聚合每票 bars 必须 as_of=pool_target 截断; score_r 滚动分位不含当日。
  - 出场是**结构终点非阈值拟合**, 勿改回追踪; **勿动下方 `_run_backtest` 回调** (含追踪逻辑,
    exit_day 为"兜底残影/触发"混合值, 截断重放下不可区分)。
  - regime 门 (rmed/score_r) 是 G1 池级统计量, 经 `_ensure_pool_daily` 惰性聚合 (失败→
    当日不产信号); 判定单源到门表, 三条路径 (scan_signals/evaluate/scan_days) 全走门表。

研究依据 / 150 天回测结论 / 与研究口径差 / 性能优化史 → `docs/策略研究依据归档.md#g56`。
"""
from __future__ import annotations

import numpy as np

from app.market_cn.auto.core.exec import fill_blocked_by_limit_up
from app.market_cn.auto.core.market import get_board_type, is_limit_up, limit_up_price
from app.market_cn.auto.strategies import register
from app.market_cn.auto.core.runtime.functions import Ctx, register_strategy_funcs
from app.market_cn.auto.strategies.base import (
    ConfirmDecision, EntryDecision, ExitDecision, ScanSpec, Signal, StrategyBase,
)
from app.utils.logger import get_logger

logger = get_logger(__name__)

STRATEGY_KEY = "g56"
STRATEGY_LABEL = "五重共振"   # 2026-09-17 用户命名 (原"56%规则G+"); 5重硬条件:
# 开盘区间指引 (2026-09-22 回测结论, 仅展示/排序用, 不强制过滤信号集):
#   次日开盘跳空 gap ∈ [-3%,+3%] 最优 (甜区 [0%,+2%], 76%~100% 胜率/+8~+10.6% 均收);
#   回避 ≥+4% 高开 (尤其 [4%,5%] 样本全止损 -8%)。gap 在 D0 开盘才可知, 故此处为静态指引。
SIGNAL_OPEN_RANGE_HINT = " · 开[-3%,+3%]优先·回避≥+4%高开"
# |MA5-MA10|≤2.5% / rma_chg>0 / ATR>板块Q5 / 前20日大涨日≥2 / rhist_chg>板块Q5,
# regime 门为横截面环境门不占位; 与祖先策略"三金叉共振"成谱系。key=g56 不变。

# ================================================================
# 冻结参数 (150天研究定稿, 2026-09-17 用户拍板; 证据见 MEMORY.md 行55-63)
# ================================================================
HOLD_DAYS = 7          # 最长持有交易日 (含入场日, 出场模拟 d=1..7)
STOP_LOSS = -8.0       # 硬止损 % (框架 initial_stop 默认同值, 双保险)
# 规则门 (2026-09-25): 信号日收盘相对 MA20 下限 %。与 score **无关** ——
# score 只做排序/展示 (随日变动); 本阈值是硬规则。300d 对照 (n=1191 基线):
#   ma20>=-4 → n=748 / 胜率80.7%(+1.4pp) / 均收8.52(基本持平) / 盈亏比1.83
#   背景: 近期 600613/002437/002412 反抽失败票 dist_ma20 多在 -3.4~-6.8。
#   trail/峰值逃顶经对照 **否决** (均收 8.69→2.5, 大肉被砍; 见 tmp/g56_round1.json)。
DIST_MA20_MIN = -4.0
# D-1 涨停子集紧止损 (2026-09-25): 归因确认信号日涨停呈两极 (大肉/大血), 46 笔中
# 14 笔≤-5% / 13 笔≥+10%。对该子集用 -5% 止损, 其余仍 -8%。
# 320d 对照 (含 DIST_MA20_MIN): 深亏≤-7.5% 103→91, 胜率 80.7→80.2, 均收持平。
# ⚠️ 不是改全体止损 (全体 -5% 胜率掉到 75%); 只收 D-1 涨停尾部。
# 依据: docs/analysis_output/g56_涨停与龙虎榜事件归因_20260924.md §A5
STOP_LOSS_LU = -5.0
R56 = {"main": 0.51, "gem_star": 0.66}        # rhist_chg 门 = 150天池内Q5
from app.market_cn.auto.core.features.cross_section import (  # noqa: E402  (L73 下方, 常量区之后)
    _aggregate, _g1_arrays, _g1_mask, _ensure_pool_daily,
    g1_state_features, g1_state_init, g1_state_step, g1_state_window_bars,
)
from app.market_cn.auto.core.present.contract import (   # 展示层折叠契约
    InsufficientHistory, Progress, Stage,
)
MAIN_RMED_MIN = 0.25     # 主板 regime 门: 池 rhist_chg 中位数 (raw)
MAIN_PCTB_MAX = 49.87    # 主板 boll %b 上限 = 56%池内 P40 (g1deep3 桶边界)
GEM_SCORE_MIN = 0.65     # 20cm regime 门: score_r (滚动相对热度)
# ================================================================
# 评分口径 (2026-09-24 换键: rhist_chg → dist_ma20 / dif0 组合)
# ----------------------------------------------------------------
# score 有两个消费者: ① 前端「评分」展示 ② **实盘每日限额截断键**
#   (scan.py:254 按 score 降序截断到 daily_limit)。② 是要害 —— 旧键 rhist_chg
#   在 1594 笔样本上实盘口径仅 **65.6%**, 低于随机截断×300 的 90% 区间下沿
#   **66.8%** (随机中位 68.4%) ⇒ 不只是"无判别力", 是系统性挑到较差的一批。
#
# 换键依据 (tmp/_g56_factor_ic.py · _g56_key_sim.py · _g56_key_robust.py):
#   · 主样本 1594笔/34信号日 **同日截面** Spearman IC: dist_ma20 +0.173 (t=3.30),
#     ret5 +0.178, pos20 +0.136, big20 +0.145 ; **rhist_chg +0.057 (t=1.00)**
#   · 早期段 87笔 (entry_date<2026-04-20, 与主样本不重叠) 分桶 Δ:
#     dist_ma20 +21.4pp / combo +16.8pp / **现用 score −1.6pp**
#   · ⚠ 陷阱: atr14/amp20/big20/dif0 **全样本 IC 很强但两时段反向** ⇒ 全样本 IC
#     会被"大日子效应"污染, 选因子必须看**同日截面 IC** (本轮踩过, 勿再犯)
#   · 实盘口径(每日Top30) 模拟: 现用 65.6%/+6.39% → dist_ma20 70.9%/+7.09%
#     → 本 combo 72.5%/+8.11% ; 参数 48 组网格全部落在 72.1~73.3% ⇒ 非阈值拟合
#
# 两段含义: dist_ma20 = 收盘相对 MA20 偏离% (位置/动量, 越大越强); −dif0 = MACD
#   柱深度 (越负=柱在零轴下越深=越接近金叉, 与 g56「④金叉预测」维度同向)。
#   二者均**个股级 D-1 已知**, 无需当日全市场截面 ⇒ 与池统计解耦, 单票可算。
SCORE_DIST_LO = -10.0    # dist_ma20 归一下界 (%)
SCORE_DIST_HI = 5.0      # dist_ma20 归一上界 (%)
SCORE_DIF0_LO = 0.0      # −dif0 归一下界 (对应 dif0 = 0)
SCORE_DIF0_HI = 16.0     # −dif0 归一上界 (对应 dif0 = −16)
SCORE_W_DIST = 0.5       # dist_ma20 权重 (dif0 占 1 − w)

DEFAULT_PARAMS = {
    "dist_ma20_min": -4.0,   # 规则门: 信号日收盘相对 MA20 下限 % (2026-09-26 迁入; 原硬编码 DIST_MA20_MIN=-4.0; 300d 对照 n=748 胜率80.7%)
}

G56_WIN = 35        # 递推切片窗口 (= 生产口径 DEFAULT_WIN)
# ★ 2026-10-10 (审计 D-1 收敛): 原 `GAP_LIM = {"main": 0.098, "gem_star": 0.198}`
#   entry 过滤硬编码口径已删 —— 涨停幅度是**市场事实**, 统一走
#   `market.limit_up_price` + `exec.fill_blocked_by_limit_up`（唯一实现）。
#   现值语义逐位不变: 旧 `gap < GAP_LIM` ⟺ 新 `not fill_blocked_by_limit_up(open, up)`
#   （严格达到名义涨停价才拒, 与旧口径零漂移）。
   # 2026-10-06: 原散落在 entry_decision / backtest 两处硬编码,
   # 展示层 evaluate 亦抄了一份 ⇒ 收敛为单一常量。
# score 相关阈值 (SCORE_* / R56 / MAIN_RMED_MIN / GEM_SCORE_MIN 等) 仍冻结为模块常量 ——
# 样本内拟合产物, 不开放 config 覆盖以防误调 (调参须走 tmp 研究链路重验)。


# ================================================================
# 指标 / 单票 G1 特征 / 横截面聚合 —— 2026-09-26 整体下沉 core/features/cross_section.py
#   _g1_arrays / _g1_mask / _ensure_pool_daily 均已从上导入, 此处不再重复定义
# ================================================================

# _g56_gate 已退役 (2026-10-09 P6): 判定单源到门表后无调用点。其判定逻辑现由
# g56.yaml 门表 + GateEvaluator.evaluate_all 唯一承载; 阈值常量 (R56/MAIN_RMED_MIN/
# MAIN_PCTB_MAX/GEM_SCORE_MIN/DIST_MA20_MIN) 保留作研究依据注释 (见上方常量区)。
# 历史性能注记: 逐日循环重算 _g1_mask 占单次 gating 95% (0.0225/0.0238 ms,
# tmp/_g56_hotspot.py), 现由 `_iter_gate_days` 一次性预计算 + O(1) 查表替代 (18x)。


def _iter_gate_days(bars, board, pool, lo_k, hi_k, code):
    """单票逐日列出通过五重共振门的 (k, f, st) —— 判定单源到门表, 一次预计算 + 逐日 O(1)。

    判定单源（切口 2 收尾）: 精确判定走门表 `ev.evaluate_all`（唯一规则事实源），
    `_g1_mask` 仅作**向量化预筛**（其 ①②③+warmup+finite 与门表 g1_trend/atr/big20/
    warmup/finite 门等价, 见 cross_section._g1_mask —— 阈值同步是遗留项）。预筛把
    绝大多数非候选日 O(1) 跳过, 只有 mask 通过的日子才付门表求值开销 ⇒ 实测比折叠
    慢 1.4x、比无预筛快 7x（tmp 性能对比）。

    前视安全: 所有 G1 指标**因果**(实证: 29360 点逐位比对, `f_full[k]` ==
    `_g1_arrays(bars[:k+1])[k]`, 0 不一致, `tmp/_g56_cost.py` P1) ⇒ 可以用未切片
    的全序列一次算出 k 日特征, 与逐日截断重算结果完全相同。

    索引: lo_k..hi_k **含端点**, 越界自动裁剪; 与 `_g1_mask` 的 `m[:68]=False`
    一致, k<68 恒不通过, 无需调用方过滤。
    """
    from app.market_cn.auto.core.runtime.evaluate import GateEvaluator
    n = len(bars)
    lo = max(0, lo_k)
    hi = min(n - 1, hi_k)
    if hi < lo:
        return
    f = _g1_arrays(bars)
    mask = _g1_mask(f, board)          # 向量化预筛（阈值与门表同源，见遗留项注释）
    spec = _g56_spec()
    ev = GateEvaluator(spec, board_type=board, code=code, stock_info={})
    for k in range(lo, hi + 1):
        if not mask[k]:
            continue
        ctx = Ctx(bars, k, lu_idx=0, params=spec.params, board_type=board, code=code,
                  stock_info={}, ext={"g56_feats": f, "g56_pool": pool},
                  market=spec.market_spec)
        ok, _ = ev.evaluate_all(bars, k, spec.params, ctx=ctx)
        if ok:
            st = {"rmed": g56_pool_field(ctx, "rmed"),
                  "score_r": g56_pool_field(ctx, "score_r")}
            yield k, f, st


# ================================================================
# 横截面 regime 门: 2026-09-26 下沉 core/features/cross_section.py
#   _ensure_pool_daily 由 cross_section 模块提供, 本文件顶部已导入
# ================================================================


def _exit_no_trail(bars, s, entry, hold_days=None, stop_loss=None, code=None,
                   stop_loss_lu=None):
    """无追踪出场模拟 — 骨架上收 core.exit_engines.run_hold_stop。

    hold_days / stop_loss / stop_loss_lu: 由调用方注入 (g56.yaml); 传 None 回落模块常量
        (常量降级为兜底默认值, yaml 是权威事实源, 二者值一致)。
    code: 可选, 用于判断信号日是否涨停 (D-1 板) —— 该子集用 stop_loss_lu 紧止损,
        降低 -8% 深亏触发 (2026-09-25)。缺 code 时行为与历史逐笔一致。
    返回 {'exit_day','exit_price','return_pct','peak_return_pct'}。
    """
    _hold = HOLD_DAYS if hold_days is None else int(hold_days)
    _stop = STOP_LOSS if stop_loss is None else float(stop_loss)
    _stop_lu = STOP_LOSS_LU if stop_loss_lu is None else float(stop_loss_lu)
    if code and s >= 1:
        try:
            from app.market_cn.auto.core.market import get_board_type, is_limit_up
            bt = get_board_type(code)
            if is_limit_up(float(bars[s - 1]["close"]), float(bars[s - 2]["close"]), bt):
                _stop = max(_stop, _stop_lu)  # -5 大于 -8 → 更紧
        except Exception:
            pass
    from app.market_cn.auto.core.exit_engines import run_hold_stop
    # with_reason=True: day_flow/trade 需带 exit_reason (对齐 replay canonical, 草案①等价)
    return run_hold_stop(bars, s, entry, hold_days=_hold, stop_loss=_stop, with_reason=True)


def _score_of(dist_ma20, dif0):
    """**预测分** 0~100 (2026-09-26 用户定名) — 唯一构造点 (口径见 SCORE_* 常量)。

    = W·归一(dist_ma20) + (1−W)·归一(−dif0), 两段各 clip [0,1] 后线性加权 ×100 取整。

    语义 (勿混用):
      - 只对 **下一交易日 T+1** 负责 (T 日收盘后算出的分 → 预期 T+1);
      - 持仓期内 **每日更新**, 形成「D0分→D1 / D5分→D6」的心里预期链;
      - **出场后不再适用**本套评分; **出池无效** (全市场任意日 ≈ 抛硬币);
      - 用途 = 排序 / 展示 / 次日胜率校准, **不是**入场规则门。

    NaN 防御: ma20/dif0 在暖机段为 NaN, 比较 `> 0` 为 False ⇒ 落 0.0 (最低分),
    不会污染排序。正常路径下暖机 `m[:68]=False` 已保证 k≥68, 不会走到。
    """
    s1 = (dist_ma20 - SCORE_DIST_LO) / (SCORE_DIST_HI - SCORE_DIST_LO)
    s2 = ((-dif0) - SCORE_DIF0_LO) / (SCORE_DIF0_HI - SCORE_DIF0_LO)
    s1 = 0.0 if not (s1 > 0) else (1.0 if s1 > 1 else s1)
    s2 = 0.0 if not (s2 > 0) else (1.0 if s2 > 1 else s2)
    v = 100 * (SCORE_W_DIST * s1 + (1 - SCORE_W_DIST) * s2)
    return int(min(100, max(0, round(v))))


_SPEC: dict = {}


def _g56_spec():
    """门表 StrategySpec 单例缓存（判定单源到门表后 scan_signals 用它求门）。"""
    if "g56" not in _SPEC:
        from app.market_cn.auto.core.runtime.evaluate import load_strategy
        _SPEC["g56"] = load_strategy("g56")
    return _SPEC["g56"]


def _mk_signal(code, bars, k, f, pool=None):
    """命中日 k → Signal (单日 scan_signals / 批量 scan_days / 折叠 ready **共用的唯一构造点**)。

    抽出的理由: 多处若各写一份, 字段口径迟早分叉 (score 取整/pctb 位数/score_r None
    处理都是易错点)。改任一字段必须只改这里。

    ★ 2026-10-09 (D6/R1 收口): extra 的**展示字段由宏 g56.yaml `signal.fields` 单源产出**
    —— 经 `build_signal` 逐条求值 expr (feat('rhist_chg') / pool_field('rmed') ...)。
    本函数只补结构性键 (`buy_mode`)。宏加字段而引擎无对应 expr 会在加载期(静态 as-of)或
    求值期立即暴露, 不再出现「宏里写着、产物里没有」的静默漂移 (对照旧实现: 硬编码的
    dif0/dist_ma20 与宏声明 nd_* 双向不一致)。
    pool: 横截面池 {board: {date: st}} —— rmed/score_r 的取值来源 (由各调用路径注入;
          `_scan_one`/`scan_days` 有池, 折叠 `_mk_ready` 传当日池)。缺省 {}。
    """
    from app.market_cn.auto.core.runtime.evaluate import build_signal
    spec = _g56_spec()
    board = get_board_type(code)
    ctx = Ctx(bars, k, lu_idx=0, params=spec.params, board_type=board, code=code,
              ext={"g56_feats": f, "g56_pool": pool or {}}, market=spec.market_spec)
    extra = build_signal(ctx, spec)
    extra["buy_mode"] = "next_open"
    dist = (float(bars[k]["close"]) / float(f["ma20"][k]) - 1) * 100
    return Signal(
        code=code,
        time=bars[k]["time"],
        score=_score_of(dist, float(f["dif0"][k])),
        price=float(bars[k]["close"]),
        label=STRATEGY_LABEL + SIGNAL_OPEN_RANGE_HINT,
        extra=extra,
    )


# ================================================================
# 策略插件
# ================================================================

# 2026-10-06: 单继承 StrategyBase —— 折叠契约已并入生产基类，参数合并口径唯一 = params()。
# 折叠契约已并入生产 StrategyBase（单继承）。
class PoolLedger:
    """策略级台账：每 (board, date) 横截面四元组 {date, n, rmed, dmed, smed}。

    score_r 的滚动分位只回看 20 日 ⇒ 台账保留最近 30 条即够（聚合用常数数组
    重建桶，与原始桶逐位等价 —— `_aggregate` 单一实现）。

    ⚠ **不自己落盘**（2026-10-06）：展示层落盘出口唯一 = StateStore（每策略一个
    文件、每轮一次）。共享状态经 `init_shared` / `shared_snapshot` 契约随**本策略
    的切片文件**一起落盘。旧实现自带 `path` 且每次 append 就写一次盘，而默认
    `path=None` 又**从不落盘** ⇒ 池台账与每票 state 生命周期不一致：重启后池
    静默丢失、分位照算不报错 —— 最难发现的一类错误。
    """

    def __init__(self, quads: dict | None = None, keep: int = 30):
        self.keep = keep
        self.quads: dict[str, list] = {
            "main": list((quads or {}).get("main") or []),
            "gem_star": list((quads or {}).get("gem_star") or []),
        }

    def append(self, board: str, quad: dict) -> None:
        qs = self.quads.setdefault(board, [])
        if qs and qs[-1]["date"] >= quad["date"]:
            qs[-1] = quad                       # 同日重跑幂等覆盖
        else:
            qs.append(quad)
        del qs[:-self.keep]

    def window(self, board: str) -> list:
        return self.quads.get(board) or []

    def snapshot(self) -> dict:
        return {b: list(q) for b, q in self.quads.items()}


@register
class G56Strategy(StrategyBase):
    key = STRATEGY_KEY
    name = STRATEGY_LABEL
    entry_style = "g56"
    family = "g56"                     # 自成一族, 不与 triple_resonance 链去重
    scan_spec = ScanSpec(kind="daily_close", after_events=("daily_1d", "lhb"))
    default_params = dict(DEFAULT_PARAMS)
    use_unified_prefilter = False      # 与 tmp 回测口径一致 (无 U1~U4)
    # 展示阶段表（展示层只按此表呈现，不认识门细节）
    stages = (
        Stage("ready", "五重共振·准备", realtime="09:25"),
        Stage("exec", "D0开盘买入"),
        Stage("exit", "出场结算"),
    )

    #: ⚠ 窗口硬下界 35: 低于此 `_g1_arrays` 直接 IndexError (dummy MACD 退化为 0 维数组)。
    #:   (原 `warmup` / `resume_points` 声明于 2026-10-06 P6 随展示层断点机制一并移除;
    #:    三档下界 35/40/240 的实测记录见 .workbuddy/memory)

    # ---- 信号判定: 只判末根bar (D-1); as_of=k 切片用于回测逐日枚举 ----
    # ══ 展示层折叠契约：递推状态机 + 池台账（门/信号构造委托生产唯一实现）══
    def __init__(self, pool_ledger=None):
        self.ledger = pool_ledger or PoolLedger()

    def init_shared(self, shared):
        """用持久化的策略级状态恢复台账（每轮开头调用，幂等）。"""
        if shared:
            self.ledger = PoolLedger(shared.get("quads"))

    def shared_snapshot(self):
        return {"quads": self.ledger.snapshot()}

    def init_state(self, code, bars):
        """seed: 全量历史 → G1 增量状态（MACD 锚 head + win 根 OHLC 窗口 + age）。"""
        if len(bars) < G56_WIN + 1:
            raise InsufficientHistory(f"{code}: bars={len(bars)} < {G56_WIN + 1}")
        return g1_state_init(bars, win=G56_WIN, keep_window=True)

    def step(self, state, bar):
        return g1_state_step(state, [bar])

    def probe(self, state):
        """除权探针: 窗口首尾 (date, close)。"""
        w = state["window"]
        return [(w[0][0], w[0][3]), (w[-1][0], w[-1][3])]

    def begin_day(self, date, states, bars):
        """跨票横截面池（展示层只透传 ctx["_day"]，不认识池内容）。"""
        buckets = {"main": {}, "gem_star": {}}
        for code, st0 in states.items():
            if code.startswith(("8", "4", "92")):
                continue
            board = get_board_type(code)
            if board not in buckets:
                continue
            st_day = self.step(st0, bars[code])
            f = g1_state_features(st_day)
            if _g1_mask(f, board, age=st_day["age"])[-1]:
                b = buckets[board].setdefault(date, [[], [], []])
                b[0].append(float(f["rhist_chg"][-1]))
                b[1].append(float(f["dif0"][-1]))
                b[2].append(float(f["rsi"][-1]))
        pool = {}
        for board in ("main", "gem_star"):
            bucket = buckets[board].get(date)
            if bucket:
                self.ledger.append(board, {
                    "date": date, "n": len(bucket[0]),
                    "rmed": float(np.median(bucket[0])),
                    "dmed": float(np.median(bucket[1])),
                    "smed": float(np.median(bucket[2])),
                })
            by_date = {q["date"]: [[q["rmed"]] * q["n"], [q["dmed"]] * q["n"],
                                   [q["smed"]] * q["n"]]
                       for q in self.ledger.window(board)}
            pool[board] = _aggregate(by_date)
        return {"pool": pool}

    def evaluate(self, state, inp, prev):
        """预处理/回测/实时共用：持仓出场 → D0 入场 → 新信号（五重共振）。"""
        bar, code = inp.bar, inp.code
        events = []
        holding = None

        # ── 持仓出场（d=2..7；T+1 不可卖）──
        if prev is not None and prev.stage == "exec" and prev.payload.get("buyable") \
                and bar.get("time", "") > prev.date:
            entry_price = float(prev.payload["entry_price"])
            entry_age = int(prev.payload["entry_age"])
            d = (state["age"] + 1) - entry_age + 1       # 持仓日（d=1 入场日）
            if d >= 2:
                # 出场单源（切口 2）: 委托 `exit_decision`（唯一逐日出场实现），不在此
                # 手写止损/到期判定。evaluate 的 state 是「截至昨日」（step 在 evaluate 后），
                # window 末位=昨日、bar=今日 ⇒ seg = window 末 (d+1) 根（D-2..昨日）+ 今日 bar，
                # entry_idx=2 指向入场日（seg[0]=D-2 / seg[1]=D-1 信号日 / seg[2]=入场日），
                # 使 exit_decision 的 D-1 涨停子集紧止损（bars[ei-1]/[ei-2]）正常判定 —— 与旧
                # 手写 `prev.payload["stop_use"]`（exec 时 _lu_subset 算好）逐位等价。
                # window 是 [date, high, low, close]（无 open），止损 fill=min(open,止损线)
                # 只在「今日」读 open ⇒ 历史 open 用 close 占位不影响判定。
                win_w = state["window"]
                seg = [{"time": w[0], "open": w[3], "high": w[1], "low": w[2], "close": w[3]}
                       for w in win_w[-(d + 1):]]
                seg.append({"time": str(bar.get("time", ""))[:10],
                            "open": float(bar.get("open") or 0),
                            "high": float(bar.get("high") or 0),
                            "low": float(bar.get("low") or 0),
                            "close": float(bar.get("close") or 0)})
                dec = self.exit_decision(
                    {"code": code, "entry_price": entry_price},
                    snap={"mode": "day_close", "bars": seg, "entry_idx": 2})
                if dec.action != "exit":
                    holding = prev
                else:
                    exit_price = (float(seg[-1]["open"]) if getattr(dec, "fill", "") == "open"
                                  else float(dec.price))
                    peak = max((float(w[1]) for w in win_w[-(d - 1):]),
                               default=float(bar.get("high") or 0))
                    peak = max(peak, float(bar.get("high") or 0))
                    events.append(self._exit_event(prev, bar, d, round(exit_price, 3),
                                                   dec.reason, peak))
            else:
                holding = prev
        # ── D0 入场（gap 过滤 = 旧 entry_decision/backtest 同式）──
        elif prev is not None and prev.stage == "ready" \
                and bar.get("time", "") > prev.date:
            open_px = float(bar.get("open") or 0)
            pc = float(prev.payload.get("price") or 0)   # 信号日收盘
            gap = (open_px / pc - 1) if (open_px > 0 and pc > 0) else None
            # ★ 涨停阻买 (市场事实, 唯一实现): 旧 GAP_LIM 口径逐位等价 (严格达到名义涨停价才拒)
            buyable = (open_px > 0 and pc > 0
                       and not fill_blocked_by_limit_up(
                           open_px, limit_up_price(pc, get_board_type(code))))
            ev = Progress(stage="exec", date=bar.get("time", ""), payload={
                "entry_date": bar.get("time", ""), "entry_price": open_px,
                "signal_date": prev.date, "gap": None if gap is None else round(gap * 100, 2),
                "buyable": buyable,
                "stop_use": self._stop_use(state, code),
                "entry_age": state["age"] + 1,
                # P5-④ 前置 (2026-10-08): 买入当日 15:01 实时确认（持仓 / 当日出场）。
            }, next_realtime="15:01")
            events.append(ev)
            if buyable:
                holding = ev

        # ── 新信号（T 日盘后五重共振；持仓中不重复入场）──
        if holding is None and not code.startswith(("8", "4", "92")):
            st_day = self.step(state, bar)
            f = g1_state_features(st_day)
            board = get_board_type(code)
            day_pool = ((inp.ctx or {}).get("_day") or {}).get("pool") or {}
            # 门诊断通道（Step 1）：ctx["_gate_dbg"] 注入全门向量回调（与链 A gate_dbg 同签名）
            _gate_dbg = (inp.ctx or {}).get("_gate_dbg")
            ok, st = self._gate(f, st_day, board, day_pool, bar.get("time", ""), code,
                                gate_dbg=_gate_dbg)
            if (tr := (inp.ctx or {}).get("_trace")) is not None:   # 门原因通道（契约约定）
                tr.gate(ok, st, date=str(bar.get("time", ""))[:10])
            if ok:
                events.append(self._mk_ready(code, bar, f, day_pool,
                                              g1_state_window_bars(st_day)))
        return events

    def _gate(self, f, st_day, board, day_pool, date, code, gate_dbg=None):
        """五重共振门 —— 判定单源到门表（切口 2）：递推窗口 → Ctx(ext) → GateEvaluator。

        递推路径 bars 只是窗口（G56_WIN=35 根），门表 `feat()`/`finite()` 用 `ctx.i`
        （窗口末位下标）索引窗口特征，`warmup()` 用 `ctx.i_age` 判暖机。
        ⚠ `ctx.i_age` 的口径 = **信号日 0-based 真实绝对索引**（与 `ctx.i` 同单位，见
        Ctx 契约「缺省 None = 视作 i_age == i」）。`st_day["age"]` 是**累计根数**
        (= 索引 + 1，见 g1_state_init: age=n)，故传入时**必须 -1**。
        等价性由 tmp/verify_g56_evaluate.py 背书（14513 判定点 0 不一致，含暖机边界）。
        参数用 spec.params（yaml 是门表唯一事实源），不再走 self.params()（config 覆盖）。
        返回 (bool_pass, st)；st = {rmed, score_r}（门通过时从 ctx 池取，与折叠同源）。

        gate_dbg（Step 1）：非 None 时 evaluate_all 自动 fire phase="all" 全门向量。
        fire 的 i = `st_day["age"] - 1`（信号日 **0-based 绝对索引**）—— 对齐链 A
        `_backtest_day_flow` 的 `i = s-1`（信号日 0-based 绝对索引），门向量聚合 key
        (code, i) 才逐点可比。此位置仅在 gate_dbg 非 None 时被消费（evaluate_all 传了
        ctx ⇒ i 不参与判定），故对判定/交易**零影响**。

        ★ 2026-10-09 修两处「age 口径」off-by-one（链 B 门诊断对齐链 A，见
        tmp/_verify_g56_gatedbg_equiv.py / _e2e.py）：
          ① fire 键：曾用 `st_day["age"]`（= 索引+1）⇒ 全体 fire key 比链 A 大 1，
             门漏斗与链 A 系统性错位（930 点 474 不一致 → 修后 0）。仅诊断影响。
          ② 暖机门：`i_age=st_day["age"]`（计数）使 `warmup()` 判 `age>=68` ⇔ 索引>=67，
             比单源口径（`_g1_mask` / 旧 `_g56_gate` / `scan_*` 均为 **索引>=68**）早一天 ⇒
             信号日 67 被错误放行（潜在多发一笔）。改传 `age-1` 后与全系统一致。
        """
        from app.market_cn.auto.core.runtime.evaluate import GateEvaluator
        spec = _g56_spec()
        si = {}            # 递推路径无 stock_info（g56 门不依赖股本元数据）
        wb = g1_state_window_bars(st_day)
        i = len(wb) - 1
        # 候选预筛（fire 时机对齐链 A `_backtest_day_flow`）：暖机期不 fire gate_dbg。
        # 链 A 的信号日 i=s-1 从 67 起（s=day_start=68 入场日），故 fire 从信号日 67 起
        # ⇒ 递推路径 age<68（= 信号日<67）不 fire。i=67 当日暖机门为 False（正确拒绝），
        # 但链 A 仍 fire 该日向量 ⇒ 此处也须 fire，才能逐点可比。
        # 末 9 日缓冲（day_end=9）属回测窗口边界，递推走到末日，差异在验证记录说明。
        if st_day["age"] < 68:
            return False, None
        ev = GateEvaluator(spec, board_type=board, code=code, stock_info=si,
                           gate_dbg=gate_dbg)
        ctx = Ctx(wb, i, lu_idx=0, params=spec.params, board_type=board, code=code,
                  stock_info=si, ext={"g56_feats": f, "g56_pool": day_pool},
                  market=spec.market_spec, i_age=st_day["age"] - 1)
        ok, _ = ev.evaluate_all(wb, st_day["age"] - 1, spec.params, ctx=ctx)
        if not ok:
            return False, None
        st = {"rmed": g56_pool_field(ctx, "rmed"),
              "score_r": g56_pool_field(ctx, "score_r")}
        return True, st

    def _mk_ready(self, code, bar, f, pool, wb):
        """ready 事件 —— **委托 `_mk_signal`**（唯一信号构造点）。

        wb = 与特征数组 f 对齐的窗口 bars (`g1_state_window_bars(st_day)`)；末位换成真
        `bar`（窗口微缩只有 date/close，真 bar 含完整 time）。这样 `_mk_signal` 内
        build_signal 的 `feat()` 能以 `ctx.i = len-1`（非 -1 —— feat 拒绝负下标）取当日
        特征、`pool_field()` 取当日板块池；`Signal.time/price` 仍来自真 bar（语义不变）。
        """
        bars_ctx = list(wb[:-1]) + [bar]
        sig = _mk_signal(code, bars_ctx, len(bars_ctx) - 1, f, pool=pool)
        return Progress(stage="ready", date=bar["time"], payload={
            "price": sig.price, "score": sig.score, "label": sig.label,
            "extra": sig.extra}, next_realtime="09:25")

    @staticmethod
    def _lu_subset(state, code):
        """D-1 涨停子集紧止损（信号日 T 收盘较 T-1 涨停）。"""
        closes = state["closes"]
        if len(closes) < 2 or closes[-2] <= 0:
            return False
        return is_limit_up(closes[-1], closes[-2], get_board_type(code))

    @staticmethod
    def _stop_use(state, code):
        """出场止损幅度（yaml 权威 + 模块常量兜底，值一致）。

        D-1 涨停子集用 stop_loss_lu（-5% 紧止损），否则 stop_loss（-8%）。
        """
        sp = _g56_spec().params
        stop = float(sp.get("stop_loss", STOP_LOSS))
        stop_lu = float(sp.get("stop_loss_lu", STOP_LOSS_LU))
        return stop_lu if G56Strategy._lu_subset(state, code) else stop

    def _exit_event(self, prev, bar, d, price, reason, peak):
        entry = float(prev.payload["entry_price"])
        return Progress(stage="exit", date=bar.get("time", ""), payload={
            "entry_date": prev.payload["entry_date"], "entry_price": entry,
            "exit_date": bar.get("time", ""), "exit_price": price,
            "exit_day": d, "reason": reason,
            "return_pct": round((price / entry - 1) * 100, 2) if entry > 0 else None,
            "peak_return_pct": round((peak / entry - 1) * 100, 2) if entry > 0 else None,
        }, next_realtime=None)


    def scan_signals(self, bars, code, *, as_of=None, ctx=None, **params):
        if not bars:
            return []
        if code.startswith(("8", "4", "92")):
            return []
        # 板块守卫 (2026-09-28 审计 P2): scan_days 版本有、这里缺 ⇒ 两条路径判据不等价,
        # 且非 A 市场会让 R56[board] 直接 KeyError (而非返回空)。
        board = get_board_type(code)
        if board not in ("main", "gem_star"):
            return []
        # 暖机/越界: m[:68]=False 口径静默返回空（该区间不可能出信号）
        if len(bars) < 68:
            return []
        k = len(bars) - 1 if as_of is None else as_of
        if not (67 <= k < len(bars)):
            return []
        # 门表单日判定（复用 _scan_one，判定单源；池锚=信号日，修前视）
        spec = _g56_spec()
        from app.market_cn.auto.core.runtime.evaluate import GateEvaluator
        si = (params or {}).get("stock_info")
        ev = GateEvaluator(spec, board_type=board, code=code, stock_info=si)
        sig = _scan_one(spec, ev, bars, k, board, si)
        return [sig] if sig is not None else []

    # ---- 横截面预热 (声明制; 编排层调 prewarm 一次, 不硬编码策略 key) ----
    def prewarm(self, bars_map, hi_date):
        """一次建好横截面池 (锚=hi_date), 供本批所有票的 scan_days 复用。

        不预热的后果: `_POOL` 是单槽缓存, 若每票各自调用 `_ensure_pool_daily`, 只要
        target 相同仍会命中 —— 但**停牌票末根早于全市场末日**会让锚跳变 ⇒ 反复重建
        全市场池。编排层统一预热 + `scan_days` 锚取 hi_date ⇒ 全批只建一次。
        """
        # (2026-10-06) 池一律由批量路径自建。曾优先读 `g1_pool_source` 的台账池历史
        #   (3.41s → 0.1s), 但该旁路需 g1_* 三件套 1531 行支撑, 已随 P6 清理删除。
        #   全量建池 3.4s 是**一次性**成本, 按 10-05 裁定「速度非第一诉求」接受。
        _ensure_pool_daily(hi_date, bars_batch=bars_map)

    # ---- 覆盖基类 scan_days (批量契约): 与逐日调 scan_signals 等价, 但 O(n) 而非 O(n²) ----
    def scan_days(self, bars, code, *, lo_date=None, hi_date=None, pool=None, **params):
        """返回该票在 [lo_date, hi_date] 内**所有**命中日的 Signal (不只末根)。

        为什么必须覆盖基类的默认实现: 默认实现逐日调 scan_signals, 而单次调用就是一次
        O(n) `_g1_arrays` ⇒ 逐日枚举 O(n²); 更致命的是 scan_signals 内部按 `bars[-1]`
        取池锚, 逐日截断会让锚每天都变 ⇒ `_POOL` 单槽缓存**每票每天都重建全市场池**
        (5235 票 × 114s)。本方法一次 f+mask 预计算 + 池锚固定 ⇒ 逐日 O(1)。

        等价实证 (`tmp/_g56_pool_anchor.py`, 500 票分层抽样):
          - 池锚: 一次建池(锚=末日) 的第 i 天 vs 逐日建池(锚=i) 的第 i 天 →
            **232 个 (板,日) 点 |Δrmed|=0 |Δscore_r|=0, 阈值翻转 0**
            (`_pctl_roll` 零前视 + EMA/ATR/RSI 因果 ⇒ 池可一次建好逐日查)
          - 端到端: 逐日切片 scan_signals vs 本路径 → **103 个 (code,date) 命中逐位一致, 96.4x**

        ⚠️ 池锚取 hi_date 而非 bars[-1]: 同一批票必须锚在同一天, 停牌票的末根早于全市场
        末日时会让锚跳变 ⇒ 单槽缓存 thrash。调用方显式传 pool 可完全跳过这一步。
        """
        if not bars or len(bars) < 68:
            return []
        if code.startswith(("8", "4", "92")):
            return []
        board = get_board_type(code)
        if board not in ("main", "gem_star"):
            return []
        if pool is None:
            pool = _ensure_pool_daily(hi_date or str(bars[-1]["time"])[:10])
        out = []
        for k, f, _st in _iter_gate_days(bars, board, pool, 0, len(bars) - 1, code):
            d = str(bars[k]["time"])[:10]
            if lo_date and d < lo_date:
                continue
            if hi_date and d > hi_date:
                continue
            out.append(_mk_signal(code, bars, k, f, pool=pool))
        return out

    # ---- D0 竞价处置 (monitor ~09:25): gap≥涨停幅度 → 不可买 (同回测 gap 过滤) ----
    def entry_decision(self, row, snap=None, **params):
        if not snap:
            return EntryDecision(False, "无竞价快照")
        open_px = float(snap.get("open") or snap.get("last") or 0)
        if open_px <= 0:
            return EntryDecision(False, "开盘价缺失")
        prev_close = float(snap.get("previousClose") or row.get("signal_price") or 0)
        if prev_close <= 0:
            return EntryDecision(False, "昨收缺失")
        gap = open_px / prev_close - 1
        # ★ 涨停阻买 (2026-10-10 D-1 收敛): 旧 `lim=0.198/0.098` 硬编码已删,
        #   统一走 limit_up_price + fill_blocked_by_limit_up (严格口径, 零漂移)。
        if fill_blocked_by_limit_up(
                open_px, limit_up_price(prev_close, get_board_type(row.get("code", "")))):
            return EntryDecision(False, f"gap={gap * 100:.2f}%≥涨停幅度, 一字/触板不可买")
        return EntryDecision(True, f"gap={gap * 100:.2f}% 可买")

    # ---- 15:00 收盘确认: v1 引擎无 D1 确认逻辑, 恒持有 ----
    def confirm_decision(self, row, snap=None, **params):
        d1_chg = None
        series = (snap or {}).get("series") if isinstance(snap, dict) else None
        entry = float(row.get("entry_price") or 0)
        if series and entry > 0:
            last_px = float(series[-1].get("last") or 0)
            if last_px > 0:
                d1_chg = round((last_px / entry - 1) * 100, 2)
        return ConfirmDecision(True, "g56_hold", d1_chg=d1_chg,
                               detail={"confirm": "always"})

    def quality_key(self, row):
        """开盘窗口质量排序键 —— **与入库截断键同源于 Signal.score**。

        2026-09-24 修正: 旧实现用 `extra.rhist_chg`, 那正是当日换键前的**旧键**
        (实盘口径仅 65.6%, 低于随机截断×300 的 90% 区间下沿 66.8% ⇒ 有害非无效)。
        当日 `Signal.score` 已换成 dist_ma20/dif0 组合(见文件头), 但本方法**未同步** ⇒
        出现「`scan.py:254` 用新键截断入库、`monitor.py:245` 用旧键分配开盘名额」的
        **两套口径并存**。现统一回落到 `row["score"]`, 两环节完全一致。

        row 是 signals 行(dict), `score` 由 `_score_of` 产出并落库; `or 0` 兜底历史空值。
        """
        return (row.get("score") or 0,)

    # ---- 出场判定 (day_close 重放 = _exit_no_trail 同式, 见文件头"易错点") ----
    def exit_decision(self, row, snap=None, **params):
        if not isinstance(snap, dict):
            return ExitDecision("hold")
        entry_price = float(row.get("entry_price") or 0)
        if entry_price <= 0:
            return ExitDecision("hold")
        # 出场参数单源到 yaml (spec.params 权威, 模块常量兜底, 二者值一致)
        sp = _g56_spec().params
        _hold = int(params.get("hold_days", sp.get("hold_days", HOLD_DAYS)))
        _stop = float(params.get("stop_loss", sp.get("stop_loss", STOP_LOSS)))
        _stop_lu = float(params.get("stop_loss_lu", sp.get("stop_loss_lu", STOP_LOSS_LU)))
        mode = snap.get("mode")
        # D-1 涨停子集紧止损 (2026-09-25, stop_loss_lu); 信号日 = entry_idx-1
        stop_use = _stop
        try:
            ei = snap.get("entry_idx")
            bars_chk = snap.get("bars") or []
            if row.get("code") and isinstance(ei, int) and 2 <= ei < len(bars_chk):
                from app.market_cn.auto.core.market import get_board_type, is_limit_up
                if is_limit_up(float(bars_chk[ei - 1]["close"]),
                               float(bars_chk[ei - 2]["close"]),
                               get_board_type(row["code"])):
                    stop_use = max(_stop, _stop_lu)
        except Exception:
            pass
        if mode == "live":
            # 硬止损兜底 (框架 stop_price 守卫为主; 此处防其缺失)
            series = snap.get("series") or []
            if series:
                last = float(series[-1].get("last") or 0)
                if 0 < last <= entry_price * (1 + stop_use / 100):
                    return ExitDecision("exit", reason=f"硬止损{stop_use:g}%",
                                        price=last)
            return ExitDecision("hold")
        if mode != "day_close":
            return ExitDecision("hold")
        bars = snap.get("bars")
        entry_idx = snap.get("entry_idx")
        if not bars or entry_idx is None or entry_idx >= len(bars):
            return ExitDecision("hold")
        today_idx = len(bars) - 1
        d = today_idx - entry_idx + 1          # 持仓日序号 (d=1=入场日, 引擎同口径)
        if d <= 1:
            return ExitDecision("hold")        # T+1: 入场日不可卖
        b = bars[today_idx]
        stop_line = entry_price * (1 + stop_use / 100)
        if float(b["low"]) <= stop_line:
            fill = min(float(b["open"]), stop_line)   # 跳空穿越按开盘 (模拟同式)
            return ExitDecision("exit", reason=f"止损{stop_use:g}%",
                                price=round(fill, 3))
        if d >= _hold:
            return ExitDecision("exit", reason=f"到期{_hold}天",
                                price=round(float(b["close"]), 3))
        return ExitDecision("hold")

    # ---- 回测钩子 (信号判定走门表统一路径, 指标一次预计算; 出场 _exit_no_trail 无追踪 2026-09-17) ----


# ================================================================
# 以下门表 DSL 私有函数由 strategies 重构从 strategy_funcs 迁入（逐字等价）
# ================================================================
_G56_KEYS = ("rma", "rma_chg", "atr", "rhist_chg", "dif0", "pctb", "rsi")


def g56_feat(ctx: Ctx, name: str) -> float:
    """g56 G1 特征取值（决策日 i）。缺值/暖机 → nan（数值门自然失败）。"""
    f = ctx.ext.get("g56_feats")
    if f is None:
        raise RuntimeError("g56 特征未注入 ctx.ext['g56_feats']（编排层缺失）")
    arr = f.get(name)
    if arr is None:
        raise KeyError(f"未知 g56 特征: {name}")
    j = ctx.i
    if j < 0 or j >= len(arr):
        return float("nan")
    return float(arr[j])


def g56_finite(ctx: Ctx) -> int:
    """G1 判定要求的"全部特征有限"（镜像 _g1_mask 的 np.isfinite 循环）。"""
    import math
    f = ctx.ext.get("g56_feats") or {}
    j = ctx.i
    for k in _G56_KEYS:
        arr = f.get(k)
        if arr is None or j < 0 or j >= len(arr) or not math.isfinite(float(arr[j])):
            return 0
    return 1


def g56_warmup(ctx: Ctx) -> int:
    """暖机下限：镜像 _g1_mask 的 m[:68]=False → 决策日索引 i 必须 >= 68。

    ★ `ctx.i_age` (2026-10-05): 增量/播种路径下 bars 只是**窗口** (可短至
    `G1_WIN_MIN`=21 根), 此时 `ctx.i` 是窗口内下标、恒 < 68 ⇒ 用它会让全市场
    **静默归零** (门判 False 却无报错)。真实数据**0-based 索引**由 `ctx.i_age` 给出
    (与 `ctx.i` 同单位 —— Ctx 契约「缺省 None = 视作 i_age == i」)。
    ⚠ i_age 为 None (生产现状) → 退化为 ctx.i, 与历史逐位一致。
    ⚠ 传的是**索引不是计数**（seeds 侧 `state["age"]` 是累计根数, 传前须 -1）。
    """
    age = ctx.i_age if getattr(ctx, "i_age", None) is not None else ctx.i
    return 1 if age >= 68 else 0


def g56_pool_stat(ctx: Ctx, name: str) -> float:
    """横截面 regime 池统计（决策日 date_k 的板块池值）—— 门用（缺失 → nan → 门失败）。

    镜像参考版 `st = pool.get(board,{}).get(date_k); if st is None: 拦截`。
    """
    pool = ctx.ext.get("g56_pool") or {}
    st = (pool.get(ctx.board_type) or {}).get(str(ctx.bars[ctx.i]["time"])[:10])
    if st is None:
        return float("nan")
    v = st.get(name)
    return float("nan") if v is None else float(v)


def g56_pool_field(ctx: Ctx, name: str):
    """横截面池统计原值 —— 信号展示字段用（缺失/None → None，与参考版 trades 同语义）。"""
    pool = ctx.ext.get("g56_pool") or {}
    st = (pool.get(ctx.board_type) or {}).get(str(ctx.bars[ctx.i]["time"])[:10])
    if st is None:
        return None
    v = st.get(name)
    return None if v is None else float(v)


def board_is_main(ctx: Ctx) -> int:
    """是否主板（镜像参考版 regime 门的 board == 'main' 分支）。"""
    return 1 if ctx.board_type == "main" else 0


def board_is_gem(ctx: Ctx) -> int:
    """是否 20cm（创业板/科创板）。"""
    return 1 if ctx.board_type != "main" else 0


register_strategy_funcs(
    'g56',
    {"feat": g56_feat, "finite": g56_finite, "warmup": g56_warmup, "pool_stat": g56_pool_stat, "pool_field": g56_pool_field, "board_is_main": board_is_main, "board_is_gem": board_is_gem},
    d0={"feat": 0, "finite": 0, "warmup": 0, "pool_stat": 0, "pool_field": 0, "board_is_main": 0, "board_is_gem": 0},
)


# ---- exit_modes 注册 (2026-09-26 P1-9 层反转) ----
def _exit_g56_no_trail(bars, entry_idx, entry_price, *, code, board_type, params, diag,
                       stop_at_idx=None):
    """g56 无追踪 7d/-8% 出场 — 供 YAML exit.mode=g56_no_trail。

    ⚠ `_exit_no_trail` 不接受 `stop_at_idx`：g56 的折叠出场是**逐日 exit_decision**
      （见 `G56Strategy.evaluate` 的 `exit_decision` 分支），重放截断由该分支的
      `d` 参数天然保证（只喂到今日），本适配器仅服务**回测主干**
      （`run_all` → 全量 bars）。
    """
    from app.market_cn.auto.core.exit_modes import _bp
    if stop_at_idx is not None:
        # 折叠路径请走 exit_decision 分支；此处 fail-fast 而非静默忽略,
        # 否则「改了 mode 却没生效」又会变成空承诺（P1-7 病灶）。
        raise NotImplementedError(
            "g56_no_trail 不支持重放截断(stop_at_idx)；折叠出场走 exit_decision 分支")
    return _exit_no_trail(bars, entry_idx, entry_price,
                          _bp(params, board_type, "hold_days"),
                          _bp(params, board_type, "stop_loss"),
                          code=code,
                          stop_loss_lu=_bp(params, board_type, "stop_loss_lu"))


from app.market_cn.auto.core.exit_modes import register_exit as _register_exit
_register_exit("g56_no_trail", _exit_g56_no_trail)


# ================================================================
# 门表单日判定 (2026-09-28 下沉 / 2026-10-09 终态② Step 3 退役回测编排)
# ----------------------------------------------------------------
# 2026-09-28: 原 core/runtime/evaluate.py 的门表回测编排**纯搬运**回本模块自注册。
# 2026-10-09 终态② Step 3: 回测主路径收敛到事件流折叠 (backtest_stock 薄壳 → core.replay)
#   ⇒ 全历史回测编排 `_backtest_day_flow` 与 register_day_flow 注册**退役**;
#   本模块仅保留 `_scan_one` (register_scan_one) 供生产单日判定 (scan_day) 使用。
# 逐笔等价回归见 analysis_output/auto架构分层_20260928.md
# ================================================================

from app.market_cn.auto.core.runtime.flows import register_scan_one


def _scan_one(spec, ev, bars, i, board_type, stock_info):
    """g56 单日判定 → Signal|None（门表引擎，生产链 scan_day 用）。

    判定走门表 expr（`ev.evaluate_all`，ctx 带 ext 池）；Signal 组装复用 `_mk_signal`
    （唯一构造点），展示字段由 `_mk_signal` 经宏 `signal.fields` 产出（rmed/score_r 取自
    注入的 `g56_pool`）——regime 门已保证池命中，故 rmed 非 None。
    """
    from app.market_cn.auto.core.features.cross_section import _ensure_pool_daily, _g1_arrays
    code = ev.code
    if code.startswith(("8", "4", "92")):
        return None
    board = get_board_type(code)
    if board not in ("main", "gem_star"):
        return None
    if len(bars) < 68 or not (67 <= i < len(bars)):
        return None
    _p = spec.params
    ext = {"g56_feats": _g1_arrays(bars),
           "g56_pool": _ensure_pool_daily(str(bars[i]["time"])[:10])}
    ctx = Ctx(bars, i, lu_idx=0, params=_p, board_type=board, code=code,
              stock_info=stock_info, ext=ext, market=spec.market_spec)
    ok, _ = ev.evaluate_all(bars, i, _p, ctx=ctx)
    if not ok:
        return None
    return _mk_signal(code, bars, i, ext["g56_feats"], pool=ext["g56_pool"])


register_scan_one("day", "g56", _scan_one)

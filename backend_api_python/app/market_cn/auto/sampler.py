# app/market_cn/auto/sampler.py
"""M1 实盘采样器 (独立于判定驱动) —— 2026-10-07 P5 前置。

═══════════════════════════════════════════════════════════════════════
为什么独立 (原采集的耦合病):
  原 M1 采集 (2026-09-11) **内联**在 `scan.run_scan` 的判定循环里, 依赖
  `scan_signals(probe=day_tr)` **内部**的门级 trace 填 `day_tr.items`。
  这把「判定驱动」与「采集」耦死: 判定一旦改走折叠内核 (`scan_days`, 而非
  `scan_signals`), trace 不再产生 ⇒ 采集静默归零 (2026-10-06 实测:
  break 3872→0 / dragon 3633→0)。`tmp/probes/` 已积累 371 文件 ≈ 9GB,
  该采集**在役** (离线 ML 归因), 不可静默归零。

本模块把采集**剥离**为独立采样器 —— 判定驱动走 `scan_days` 折叠内核 (不产门级
trace), 由本模块 `observe(...)` **自跑** `scan_signals(probe=)` 取 trace,
与判定驱动解耦。

sample 组装走同一份 `scan_signals` 代码 + 同一份 `_probe_day` ⇒ M1 数据格式/内容
不变 (可与历史 JSONL 直接拼接消费)。

铁律: sample 含未来标签 (sample_feats 的 labels), **只供离线分析**,
      绝不回流判定 / 实盘路径 (probe.py 头注释同款约束)。

⚠ 采样器会在折叠判定之外**再跑一遍 scan_signals** —— 这是「判定与采集解耦」的
  代价 (全市场采样策略算力约翻倍)。它是**过渡桥**:
  终态是门级 trace 进递推路径 (evaluate 经 ctx["_trace"] 打点) 后, 采样器只消费
  TraceCollector, 不再自跑。见 改进方案.md §3.5 / §6-20。
"""
from __future__ import annotations

import inspect

from app.utils.logger import get_logger

logger = get_logger(__name__)


def _accepts_probe(strategy) -> bool:
    """scan_signals 是否显式声明 probe 形参。

    ⚠ 未声明者传 probe 会被 `**params` 静默吞掉 —— 探针对象混进 params 有隐患,
      故此类策略**不采集** (判定行为零变化)。
    """
    try:
        return "probe" in inspect.signature(strategy.scan_signals).parameters
    except (TypeError, ValueError):
        return False


class LiveSampler:
    """每策略一份 Probe 的 M1 实盘采样器。

    生命周期: 构造 (建 probe) → 逐 (code, strategy) `observe` → `close`。
    未启用 (config `live_probe=false`) 或策略不接受 probe ⇒ **全路径零开销**。

    用法 (判定驱动侧):
        sampler = LiveSampler(active, strat_reg.params_override)
        for code in codes:
            for key, strat in active.items():
                sigs = strat.scan_days(bars, code, lo_date=d, hi_date=d)
                kept, u_fails = apply_unified_prefilter(sigs, ...)
                sampler.observe(key, strat, code, bars, stock_info,
                                sigs=sigs, kept=kept, u_fails=u_fails,
                                independent=True)
        sampler.close()
    """

    def __init__(self, active: dict, params_override=None, *,
                 out_dir=None, prefilter=None):
        from app.market_cn.auto import strategies as strat_reg
        self._enabled = strat_reg.live_probe_enabled()
        self._params_override = params_override or (lambda k: {})
        self._out_dir = out_dir
        # 注入点 (测试/解耦): 默认 lazy 取 scan.apply_unified_prefilter (避循环导入)
        self._prefilter = prefilter
        self.probes: dict = {}
        self._ok: dict = {}
        if self._enabled and active:
            from app.market_cn.auto.probe import Probe
            self.probes = {k: Probe(k, tag="live", out_dir=out_dir) for k in active}
            self._ok = {k: _accepts_probe(s) for k, s in active.items()}
            logger.info("[sampler] M1 实盘采集开启: %d 策略 (%d 接受 probe)",
                        len(self.probes), sum(1 for v in self._ok.values() if v))

    # ---- 查询 ----
    @property
    def enabled(self) -> bool:
        return self._enabled

    def wants(self, key: str) -> bool:
        """该策略是否参与采集 (未启用 / 不接受 probe → False)。"""
        return bool(self.probes) and bool(self._ok.get(key))

    # ---- 判定驱动接口 ----
    def observe(self, key, strategy, code, bars, stock_info) -> None:
        """产一行 M1 sample (判定落点非空时)。

        判定走 `scan_days` 折叠内核、不产门级 trace ⇒ 本模块**自跑**
        `scan_signals(probe=)` 取 trace 并重算。**没有**「复用调用方 trace」路径:
        折叠驱动下那条路径没有生产者, 留着只会让人误以为传 sigs 有效。
        """
        if not self.wants(key):
            return
        # 2026-10-08 起走影子路径（trace 经 ctx["_trace"]，与 probe 路径逐字节
        # 同源 —— test_m1_sampler 影子对拍硬断言）。`_self_run` 留作对拍参照，P6 清。
        sigs, kept, u_fails, day_tr = self._self_run_via_trace(
            key, strategy, code, bars, stock_info)
        if sigs is None:
            return
        # 判定落点非空才采 (纯噪声不采, 同 probe.py 约定)
        if day_tr is None or not (day_tr.items or sigs):
            return
        # stage 口径镜像回测: U1~U4 拒=prefilter / 全过=signal / 其余取当日最深判定步
        if sigs and not kept:
            stage, _uf = "prefilter", u_fails
        elif kept:
            stage, _uf = "signal", None
        else:
            stage, _uf = None, None
        from dataclasses import asdict
        try:
            build_day_sample(
                self.probes[key], day_tr, bars, len(bars) - 1, code, stock_info,
                strategy=strategy, stage=stage, u_fails=_uf,
                sig=asdict(kept[0] if kept else sigs[0]) if sigs else None)
        except Exception as e:
            # 采样失败不得影响判定 (判定结果已产出); 但也不能静默 —— 打 WARNING
            logger.warning("[sampler] %s %s 采样组装失败 (该票不采): %s", code, key, e)

    def close(self) -> None:
        """收尾: 关闭全部 probe (打印计数汇总)。"""
        for p in self.probes.values():
            try:
                p.close()
            except Exception as e:
                logger.warning("[sampler] probe.close 失败: %s", e)

    # ---- 独立取 trace ----
    def _self_run(self, key, strategy, code, bars, stock_info):
        """自跑 `scan_signals(probe=DayTrace)` + U1~U4 → (sigs, kept, u_fails, day_tr)。

        与判定驱动的旧直连路径**同一份代码** ⇒ 两条路径 sample 逐字段同源。
        异常时返回全 None (该票不采, 判定行为不受影响)。
        """
        from app.market_cn.auto.probe import DayTrace
        day_tr = DayTrace()
        try:
            sigs = strategy.scan_signals(
                bars, code, probe=day_tr, **self._params_override(key)) or []
        except Exception as e:
            logger.debug("[sampler] %s %s 独立采样自跑异常 (该票不采): %s", code, key, e)
            return None, None, None, None
        prefilter = self._prefilter
        if prefilter is None:
            from app.market_cn.auto.scan import apply_unified_prefilter
            prefilter = apply_unified_prefilter
        try:
            kept, u_fails = prefilter(sigs, bars, code,
                                      (stock_info or {}).get(code), strategy)
        except Exception as e:
            logger.debug("[sampler] %s %s 独立采样预过滤异常 (该票不采): %s", code, key, e)
            return None, None, None, None
        return sigs, kept, u_fails, day_tr


# ================================================================
# 影子路径（P3 迁移，2026-10-08）：trace 经 ctx["_trace"] 收集，不依赖 probe 对象
# ---------------------------------------------------------------
# 策略侧 scan_signals 内的打点已统一分发（`_emit`：ctx["_trace"].note 优先，
# probe.trace 兼容）⇒ 同一份代码、同一 taxonomy、同一候选枚举。本路径与
# `_self_run` 的产出（sigs/kept/u_fails/day_tr.items）**逐字节同源**，由
# test_m1_sampler 的影子对拍硬断言。切换 observe 到本路径后，probe 对象/
# `_probe_day`/PROBE_STAGE_RANK 方可按 P3-④ 退役（组装迁本模块）。
# ================================================================
class _DayTrShim:
    """DayTrace.items 同格式收集器（note(stage, **kw) → {"stage": stage, **kw}）。"""

    def __init__(self):
        self.items = []

    def note(self, stage, **kw):
        self.items.append({"stage": stage, **kw})


def _self_run_via_trace(self, key, strategy, code, bars, stock_info):
    """`_self_run` 的影子路径：trace 走 ctx["_trace"]，其余口径逐字不变。

    返回同四元组 (sigs, kept, u_fails, day_tr)。异常同样吞掉（该票不采）。
    """
    shim = _DayTrShim()
    try:
        sigs = strategy.scan_signals(
            bars, code, ctx={"_trace": shim},
            **self._params_override(key)) or []
    except Exception as e:
        logger.debug("[sampler] %s %s 影子自跑异常 (该票不采): %s", code, key, e)
        return None, None, None, None
    try:
        prefilter = self._prefilter
        if prefilter is None:
            from app.market_cn.auto.scan import apply_unified_prefilter
            prefilter = apply_unified_prefilter
        kept, u_fails = prefilter(sigs, bars, code,
                                  (stock_info or {}).get(code), strategy)
    except Exception as e:
        logger.debug("[sampler] %s %s 影子预过滤异常 (该票不采): %s", code, key, e)
        return None, None, None, None
    return sigs, kept, u_fails, shim


LiveSampler._self_run_via_trace = _self_run_via_trace


# ================================================================
# STAGE_RANK 注册表 + build_day_sample 通用组装（P3-④ 迁移，2026-10-08）
# ---------------------------------------------------------------
# 来源：各策略类属性 PROBE_STAGE_RANK + StrategyBase._probe_day。M1 sample 的
# 「stage 归属 + rule_trace + 特征/标签」组装是**采样器的职责**（调试归内核），
# 策略文件只产判定。格式/内容与旧 `_probe_day` 逐字一致（test_m1_sampler 影子
# 对拍背书）；rank 表是 taxonomy 元数据，按策略 key 集中登记在此。
# ================================================================

#: stage 归属排名（per-strategy taxonomy；"最深判定步"归属用）
STAGE_RANK = {
    "break": {"confirm": 1, "prefilter": 2, "signal": 3},
    "dragon_callback": {"dragon": 1, "gap": 2, "streak": 3, "lu_gain20": 4,
                        "rsi": 5, "turn": 6, "d0_chg": 7, "quality": 7,
                        "dedup": 8, "prefilter": 9, "engine_skip": 9,
                        "signal": 10},
    "knife_catch": {"window": 1, "mkt": 2, "feat": 3, "data": 4, "tail_vw": 5,
                    "daily": 6, "vol": 7, "streak": 8, "pre5": 8,
                    "lu_recent": 9, "signal": 10},
    "tail_oversold": {"window": 1, "limit": 2, "data": 3, "v2": 4, "signal": 5},
}


def build_day_sample(probe, day_tr, bars, i, code, stock_info,
                     strategy=None, stage=None, sig=None, u_fails=None,
                     extra=None):
    """按决策日产出一行 sample（旧 StrategyBase._probe_day 逐字搬入，签名同构）。

    stage=None 时取 day_tr 中 STAGE_RANK[strategy.key] 最深的判定步做 day 级归属。
    """
    rec = {"code": code, "d0_date": str(bars[i]["time"])[:10], "stage": stage}
    if stage is None:
        key = getattr(strategy, "key", "") or ""
        rank = STAGE_RANK.get(key, {})
        rec["stage"] = max((t["stage"] for t in (day_tr.items if day_tr else [])),
                           key=lambda s: rank.get(s, 0), default="no_gate")
    from app.market_cn.auto.probe import sample_feats
    rec.update({"rule_trace": day_tr.items if day_tr is not None else [],
                **sample_feats(bars, i, code, stock_info)})
    if sig is not None:
        rec["sig"] = sig
    if u_fails is not None:
        rec["u_fails"] = list(u_fails)
    if extra:
        rec.update(extra)
    probe.sample(**rec)

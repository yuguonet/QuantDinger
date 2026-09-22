#!/usr/bin/env python3
"""auto/tools/gate_funnel.py — 策略漏斗可调用接口 (2026-09-21)

定位：把 explain.py 的「市场全量逐门漏斗」从 CLI+落文件 封装为 **可调用函数**，
返回 dict（JSON 序列化友好），供 agent 能力层调用。

复用：explain.run_explain_backtest + explain.build_funnel + rule_stats._metrics。
零新计算逻辑 —— 只是把 explain 的输出从文件搬到内存 dict。

接口：
  strategy_funnel(strategy, days=120, codes=None, label="d5", peak="5")
    → {"meta": {...}, "funnel": [...], "stage_counts": {...}, "final": {...}}

红线：
  - 离线 only（前瞻标签不得回流生产路径）
  - 只读（不写 config / 注册表 / 信号表）
  - 本文件独立，不侵入 core/ 或 explain.py

易错点：
  - peak 只支持 "5"（自包含口径），peak_exit 需出场模拟，不在门向量口径内。
  - 全市场调用耗时较长（分钟级），调用方应自行控制频率。
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

from app.market_cn.auto.core._paths import PROJECT_ROOT

# 标签口径（与 explain.py / rule_audit.py 同源）
LABEL_KEYS = {"d1": "ret_d1c", "d5": "ret_d5o", "d10": "ret_d10o"}
PEAK_KEYS = {"5": "peak5"}

# 日线默认窗口（用户裁定 2026-09-21：日线 120 天较安全）
DEFAULT_DAYS_DAILY = 120

# ---------------------------------------------------------------------------
# 成本模型（2026-09-22 真·全市场实测标定，v1 / 日线 days=120 / 5234 股）
#   建行集 228.6s，503,330 行（96.2 行/股），4,795 MB
#   浅层门（到达池 50 万）21.1 s/条；深层门（到达池 172）0.25 s/条
# 用途：把「股池 > 500 就拦」改成「按预估成本拦」——
#   深层门全市场其实很快（0.25s/条），真正贵的是**一次性建行集**（与门深度无关）。
# ---------------------------------------------------------------------------
ROWS_PER_CODE_PER_DAY = 0.80    # 每只股票每个交易日约产生 0.8 行决策样本
BUILD_SEC_PER_ROW = 4.54e-4     # 建行集耗时/行（含 daily() 取数 + sample_feats）
BYTES_PER_ROW = 1.0e4           # 单行常驻内存（features.win 30 根占大头）
TRIAL_MS_PER_ROW = 0.042        # 候选规则试算耗时/行（缓存命中后，纯内存求值）

# 触发确认的门槛（超过任一即要求调用方显式确认）
BUILD_WARN_SEC = 60.0           # 预估建行集 > 60s
MEM_WARN_MB = 2048.0            # 预估常驻内存 > 2GB

# 已确认过的 (strategy, days, pool_sig)：建行集有缓存，同参数不必反复问
_CONFIRMED: set = set()


def _load_env():
    """加载 .env（DB 连接等），静默失败。"""
    try:
        from dotenv import load_dotenv
        for _p in (os.path.join(PROJECT_ROOT, ".env"),
                   os.path.normpath(os.path.join(PROJECT_ROOT, "..", ".env"))):
            if os.path.isfile(_p):
                load_dotenv(_p, override=False)
                break
    except Exception:
        pass


def n_codes_of(codes=None) -> int:
    """股票池规模（None = 全市场）。"""
    if codes is not None:
        return len(codes)
    from app.market_cn.auto.core.data.hub import all_codes
    return len(all_codes())


def estimate_cost(days: int, codes=None) -> Dict[str, Any]:
    """预估本次调用的建行集成本（不需要真正跑）。

    这是「要不要先问用户」的依据。模型由 2026-09-22 全市场实测标定，
    只用于**提示量级**，不追求精确。
    """
    n = n_codes_of(codes)
    rows = n * days * ROWS_PER_CODE_PER_DAY
    build = rows * BUILD_SEC_PER_ROW
    mem_mb = rows * BYTES_PER_ROW / (1024 * 1024)
    return {
        "n_codes": n,
        "est_rows": int(rows),
        "est_build_sec": round(build, 1),
        "est_mem_mb": round(mem_mb, 0),
        "over_budget": bool(build > BUILD_WARN_SEC or mem_mb > MEM_WARN_MB),
        "budget": {"build_sec": BUILD_WARN_SEC, "mem_mb": MEM_WARN_MB},
    }


def guard_cost(strategy: str, days: int, codes, label: str,
                force_full_market: bool = False,
                call: str = "调用") -> Dict[str, Any]:
    """成本守卫：预估超预算且未确认 → 抛错要求显式确认。

    与旧的「股池 > 500 就拦」相比有三处修正：
      1. 按**预估耗时/内存**判断，而不是按股票只数 —— 深层门全市场其实很快，
         真正贵的是一次性建行集；500 只的固定阈值对深层门是误伤。
      2. 覆盖**显式传入大股池**的情况（旧逻辑只在 codes is None 时拦，
         显式传 5000 只反而没人管）。
      3. 同一 (strategy, days, pool) 确认过一次后不再重复问
         —— 行集已缓存，后续调用不再付建行集成本。
    """
    cost = estimate_cost(days, codes)
    if not cost["over_budget"]:
        return cost

    from app.market_cn.auto.tools.gate_funnel import _pool_signature
    sig = _pool_signature(codes)
    key = (strategy, days, sig, label)
    if force_full_market:
        _CONFIRMED.add(key)
        return cost
    if key in _CONFIRMED:
        return cost

    raise RuntimeError(
        f"{call} 预估成本超预算：{cost['n_codes']} 股 / days={days} → "
        f"约 {cost['est_rows']:,} 行，建行集约 {cost['est_build_sec']:.0f}s，"
        f"常驻内存约 {cost['est_mem_mb'] / 1024:.1f}GB"
        f"（预算：建行集 ≤{BUILD_WARN_SEC:.0f}s 且内存 ≤{MEM_WARN_MB / 1024:.0f}GB）。\n"
        f"确认请传 force_full_market=True（同一策略+窗口+股池只需确认一次，"
        f"之后行集走缓存、深层门试算约 {TRIAL_MS_PER_ROW * 1000:.0f}ms/行），"
        f"或用 codes 指定更小的股票池。"
    )


def _pool_signature(codes) -> str:
    """股票池签名（缓存 key / 确认记录用）。"""
    if codes is None:
        return "full"
    import hashlib
    return hashlib.md5(",".join(sorted(codes)).encode()).hexdigest()[:12]


def strategy_funnel(
    strategy: str,
    days: int = DEFAULT_DAYS_DAILY,
    codes: Optional[List[str]] = None,
    label: str = "d5",
    peak: str = "5",
    progress_every: int = 0,
    force_full_market: bool = False,
) -> Dict[str, Any]:
    """市场全量、策略自带门的逐门漏斗。

    Args:
        strategy: 策略 key（strategies/<key>.yaml）
        days: 回看窗口天数（日线默认 120）
        codes: 指定股票池（None = 全市场）
        label: 收益标签口径 d1/d5/d10
        peak: 峰值口径（仅支持 "5"）
        progress_every: 进度打印间隔（0 = 不打印）
        force_full_market: 全市场时跳过警告确认

    Returns:
        dict: {"meta": {...}, "funnel": [...], "stage_counts": {...},
               "final": {...}}
    """
    # 注：不含 two_segment（两段稳定性）。用户裁定 2026-09-22：把成交切前一半/后一半
    # 各算胜率的稳定性检验意义不大，需要时调用方自行用 final / funnel 数据判断。
    from app.market_cn.auto.tools.explain import (
        run_explain_backtest, build_funnel,
    )
    from app.market_cn.auto.core.runtime.evaluate import load_strategy

    _load_env()

    if label not in LABEL_KEYS:
        raise ValueError(f"未知 label: {label!r}, 可选: {list(LABEL_KEYS)}")
    if peak not in PEAK_KEYS:
        raise ValueError(f"未知 peak: {peak!r}, 可选: {list(PEAK_KEYS)}")

    spec = load_strategy(strategy)
    key_ret = LABEL_KEYS[label]
    peak_key = PEAK_KEYS[peak]

    # 成本守卫：按预估耗时/内存判断（旧逻辑按固定 500 股，对深层门是误伤）
    cost = guard_cost(strategy, days, codes, label,
                      force_full_market=force_full_market, call="strategy_funnel")

    t0 = time.time()
    rows, trades, run_meta = run_explain_backtest(
        spec, days, codes, progress_every=progress_every,
    )
    run_meta["days"] = days

    if not rows:
        return {
            "meta": {"strategy": strategy, "days": days, "error": "无门向量样本"},
            "funnel": [], "stage_counts": {}, "final": {},
        }

    gate_order = [g.id for g in spec.enabled_gates]
    funnel_raw, meta = build_funnel(rows, gate_order, key_ret, peak_key)

    # 序列化 funnel
    funnel = []
    for gname, m_in, m_p, m_r in funnel_raw:
        delta = {}
        for k in ("winrate", "avg_ret", "peak_mean", "peak_ge10", "pl_ratio"):
            vp = m_p.get(k)
            vr = m_r.get(k)
            if vp is not None and vr is not None:
                delta[k] = round(vp - vr, 2)
        funnel.append({
            "gate": gname,
            "reach": m_in["n"],
            "pass": m_p["n"],
            "reject": m_r["n"],
            "pass_stats": m_p,
            "reject_stats": m_r,
            "delta": delta,
        })

    return {
        "meta": {
            "strategy": strategy,
            "name": spec.meta.get("name", strategy),
            "market": spec.market_key,
            "days": days,
            "label": key_ret,
            "peak": peak_key,
            "gate_order": gate_order,
            "pool": run_meta["pool_mode"],
            "n_rows": meta["n_rows"],
            "n_trades": len(trades),
            "elapsed": round(time.time() - t0, 1),
        },
        "funnel": funnel,
        "stage_counts": meta["stage_counts"],
        "final": meta["final"],
    }
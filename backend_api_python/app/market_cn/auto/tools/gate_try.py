#!/usr/bin/env python3
"""auto/tools/gate_try.py — 候选规则试算接口 (2026-09-21)

定位：在漏斗第 `at` 道门处，**一次性试算多个候选规则**，返回结构化 JSON。
让 agent 在一条循环里：看漏斗 → 定位差门 → 试候选规则 → 比 Δ → 再试 → 收敛出结论。

核心原理：
  explain 采出的探针行是自包含的（gates 向量 + features + labels），
  候选规则可在内存里对行集求值（expr.evaluate），不碰 DB、不重跑回测。
  每轮 ~O(n_rows) 纯 Python，一次调用试几十个规则可行。

接口：
  gate_try(strategy, at, exprs, days=120, codes=None, label="d5", peak="5",
           base_gates=True, force_full_market=False)
    → {"at": "...", "base": "...", "reach": N, "window": {...}, "trials": [...]}

红线：
  - 离线 only（前瞻标签不得回流生产路径）
  - 只读
  - 候选表达式走 core/runtime/expr（单一真源），禁止在 tools 里重写指标
  - **不侵入 core/**：min_bars 声明表放本文件（见 MIN_BARS_HINT），
    不改 core/runtime/functions.py 的 register_function 签名。

缓存：
  - 按 (strategy, days, pool_signature, label) 做进程内缓存
  - 10GB 级以内可接受，用完退出（进程结束释放）

易错点（都是取证踩过的）：
  - ⚠️ 采样窗口 ≠ days。探针行 features.win 由 probe.sample_feats **写死近 30 根**
    (`bars[max(0,i-29):i+1]`)，与 days 无关。days 只决定回测取样跨度。
  - ⚠️ 窗口不足**不报错**：macd_hist_lt 在 len<35 时 fail-open 返回 True
    → 门"恒过" → 统计偏乐观。故本接口对超出 win 的候选表达式给出 window_ok=False 警告。
  - 切片必须锚定决策日（bars[max(0,i+1-w):i+1]，不能取到序列尾部）。
  - lu_idx 固定 0：引用 lu_* / pullback_days 的候选规则在此口径下无意义。
"""
from __future__ import annotations

import ast
import hashlib
import os
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.market_cn.auto.core._paths import PROJECT_ROOT

# 标签口径
LABEL_KEYS = {"d1": "ret_d1c", "d5": "ret_d5o", "d10": "ret_d10o"}
PEAK_KEYS = {"5": "peak5"}

# 日线默认窗口（用户裁定 2026-09-21：日线 120 天较安全）
DEFAULT_DAYS_DAILY = 120

# ⚠️ probe.sample_feats 写死的 features.win 长度（近 30 根，含决策日）。
# 候选规则是**在这一段**上求值的，与 days 参数无关；改这个值必须同步 probe.py。
PROBE_WIN_BARS = 30

# 注册函数正确求值所需的最少 K 线根数（含决策日）。
# 为什么放这里而不是 core 的 register_function：这是**离线试算窗口推导**用的提示，
# 不是求值契约；放接口文件内可避免为调试能力改动 core（模块化 / 最小侵入）。
# 未声明的函数 → 视为 PROBE_WIN_BARS 内可安全求值。
MIN_BARS_HINT: Dict[str, int] = {
    "obv_rising": 25,      # OBV 累计 max(0,i-20) + window
    "no_lu_last": 11,      # 前 days 日 (i-days, i)
    "macd_hist_lt": 35,    # 慢线 26 + 信号 9 → 窗口不足会 fail-open
    "boll_bw_ok": 20,      # n=20 为默认周期（45 是带宽阈值不是周期）
    "ret_n": 21,           # n=20 需 21 根
    "d1_vol_ratio": 7,     # 前 5 日均量 + 当日
}

# 成本守卫复用 gate_funnel 的模型（单一实现，避免两份阈值漂移）。
# 旧的 FULL_MARKET_WARN=500 固定阈值已于 2026-09-22 废弃：全市场实测显示
# 深层门试算只要 0.25s/条，按股票只数拦是误伤；改为按预估耗时/内存判断。
from app.market_cn.auto.tools.gate_funnel import (  # noqa: E402
    guard_cost, estimate_cost, TRIAL_MS_PER_ROW,
)

# 进程内缓存：key = (strategy, days, pool_sig, label) → (rows, gate_order, t_loaded)
_PROBE_CACHE: Dict[tuple, Tuple[list, list, float]] = {}
_CACHE_BYTES = 0
# 10GB 上限（用户裁定 2026-09-21）
_CACHE_MAX_BYTES = 10 * 1024 * 1024 * 1024


def _estimate_size(obj) -> int:
    """粗估 list 的内存占用（bytes）。

    ⚠️ 实测：全量递归遍历 50 万行要 ~90s（占全市场调用的可观比例），而它只是个
    「缓存水位」估算，不需要精确。故对 list 做**抽样外推**：取首尾 + 均匀抽样
    至多 200 个元素求均值再乘总数，误差通常 <10%，耗时从 O(n) 降到 O(1)。

    抽样刻意取首尾 + 等距：探针行的 features.win 长度一致，元素间差异主要来自
    code/日期字符串，等距抽样足以覆盖。
    """
    import sys
    if not isinstance(obj, list):
        # 非 list（dict/标量等）走原递归路径，规模小
        if isinstance(obj, (str, bytes)):
            return sys.getsizeof(obj)
        if isinstance(obj, dict):
            return sys.getsizeof(obj) + sum(_estimate_size(k) + _estimate_size(v)
                                             for k, v in obj.items())
        if isinstance(obj, (list, tuple)):
            return sys.getsizeof(obj) + sum(_estimate_size(x) for x in obj)
        return sys.getsizeof(obj)

    n = len(obj)
    if n == 0:
        return sys.getsizeof(obj)

    SAMPLE = 200
    if n <= SAMPLE:
        idxs = range(n)
    else:
        step = n / float(SAMPLE)
        idxs = [int(i * step) for i in range(SAMPLE)]

    def _deep(x) -> int:
        if isinstance(x, (str, bytes)):
            return sys.getsizeof(x)
        if isinstance(x, dict):
            return sys.getsizeof(x) + sum(_deep(k) + _deep(v) for k, v in x.items())
        if isinstance(x, (list, tuple)):
            return sys.getsizeof(x) + sum(_deep(v) for v in x)
        return sys.getsizeof(x)

    per = sum(_deep(obj[i]) for i in idxs) / float(len(list(idxs)))
    return sys.getsizeof(obj) + int(per * n)


def _load_env():
    try:
        from dotenv import load_dotenv
        for _p in (os.path.join(PROJECT_ROOT, ".env"),
                   os.path.normpath(os.path.join(PROJECT_ROOT, "..", ".env"))):
            if os.path.isfile(_p):
                load_dotenv(_p, override=False)
                break
    except Exception:
        pass


def _pool_signature(codes) -> str:
    """股票池签名（缓存 key 用）。"""
    if codes is None:
        return "full"
    return hashlib.md5(",".join(sorted(codes)).encode()).hexdigest()[:12]


def expr_min_bars(expr_str: str) -> int:
    """静态提取表达式所需的最少 K 线根数（取引用函数的 max 声明）。

    只做**名字提取**（不重复实现求值器）；未知函数 → 0（不抬高窗口）。
    """
    try:
        tree = ast.parse(expr_str, mode="eval")
    except SyntaxError:
        return 0
    need = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            need = max(need, MIN_BARS_HINT.get(node.func.id, 0))
    return need


def required_window(exprs: Optional[List[str]] = None) -> int:
    """候选表达式组所需的采样窗口（根数）。

    ⚠️ 与 days 无关：探针行可用窗口恒为 PROBE_WIN_BARS(30)。
    本函数返回的是"理想所需"，供调用方判断 30 够不够（见 window_ok）。
    """
    if not exprs:
        return DEFAULT_DAYS_DAILY
    return max([expr_min_bars(e) for e in exprs] + [0]) or DEFAULT_DAYS_DAILY


def _get_probe_rows(strategy: str, days: int, codes, label: str,
                    progress_every: int = 0) -> Tuple[list, list]:
    """获取探针行集（带缓存）。返回 (rows, gate_order)。"""
    global _CACHE_BYTES

    sig = _pool_signature(codes)
    cache_key = (strategy, days, sig, label)

    if cache_key in _PROBE_CACHE:
        rows, gate_order, _ = _PROBE_CACHE[cache_key]
        return rows, gate_order

    from app.market_cn.auto.tools.explain import run_explain_backtest
    from app.market_cn.auto.core.runtime.evaluate import load_strategy

    spec = load_strategy(strategy)
    gate_order = [g.id for g in spec.enabled_gates]

    rows, trades, run_meta = run_explain_backtest(
        spec, days, codes, progress_every=progress_every,
    )

    if not rows:
        return [], gate_order

    # 缓存
    row_size = _estimate_size(rows)
    if _CACHE_BYTES + row_size > _CACHE_MAX_BYTES:
        # 淘汰最旧的
        oldest_key = min(_PROBE_CACHE, key=lambda k: _PROBE_CACHE[k][2])
        old_rows, _, _ = _PROBE_CACHE.pop(oldest_key)
        _CACHE_BYTES -= _estimate_size(old_rows)

    _PROBE_CACHE[cache_key] = (rows, gate_order, time.time())
    _CACHE_BYTES += row_size

    return rows, gate_order


def _order_funnel_local(rows: list, order: list) -> list:
    """逐门漏斗：(gate_id, reach, passed, rejected)。

    与 rule_audit._order_funnel 同口径，但返回结构化列表。
    """
    current = list(rows)
    result = []
    for gid in order:
        passed = [r for r in current if r.get("_g", {}).get(gid) is True]
        rejected = [r for r in current if r.get("_g", {}).get(gid) is False]
        result.append((gid, list(current), passed, rejected))
        current = passed  # 下一道门的到达池 = 本门通过的
    return result


def _compute_metrics(rows: list, key_ret: str, key_peak: str) -> dict:
    """计算一组样本的统计指标。复用 rule_stats._metrics 的口径。"""
    from app.market_cn.auto.tools.rule_stats import _metrics
    # two_seg=False: 跳过「按日期排序切上下半段算胜率」——占本函数 33% 耗时,
    # 且用户裁定该统计意义不大; 需要时调用方自行从 pass/reject 明细判断。
    return _metrics(rows, key_ret, key_peak, "mae5", two_seg=False)


def _win_to_bars(win: list) -> list:
    """probe.sample_feats 产出的 win (list-of-lists) → Ctx 兼容的 list-of-dicts。

    sample_feats.win 格式: [[open, high, low, close, volume_in手], ...]
    Ctx._f(bar, key) 调 bar.get(key) → 需要 dict 格式。
    """
    return [{"open": row[0], "high": row[1], "low": row[2],
             "close": row[3], "volume": row[4] * 100}  # 手→股
            for row in win]


# 分块大小：每行准备好的 (Ctx, funcs) 会常驻到本块所有候选表达式跑完。
# 分块是为了**限制常驻内存** —— 浅层门到达池可达 50 万行，全部预建会 OOM；
# 每次只建 2000 行的上下文（约数十 MB），跑完全部候选规则后即释放。
PREP_CHUNK = 2000


def _prep_block(
    block: list,
    build_funcs_fn: Callable,
    Ctx_cls: type,
    params: dict = None,
) -> list:
    """为一个行块预建求值上下文，返回与 block 等长的 [(Ctx, funcs)]（失败处为 None）。

    **为什么要把「建上下文」从「求值」里提出来**（2026-09-22 实测）：
    逐行开销实测为 `win→bars 转换 0.014ms + Ctx 0.001ms + build_funcs 0.011ms +
    evaluate 0.006ms`。转换/Ctx/funcs 这 0.026ms **只依赖行本身**，与候选表达式无关，
    但原实现每条候选规则都重做一遍 → N 条规则付 N 份。提出来后 N=5 时
    单行成本 0.160ms → 0.056ms（**约 2.9x**）。

    跨表达式复用同一份 funcs 的安全性：`build_funcs` 只是把 ctx 方法绑定成闭包
    （纯绑定，无内部缓存/可变状态），Ctx 亦无 memo 字段；同一行上连续求值多条
    表达式与「各建一次」逐位等价。

    board_type 从行的 features.board_type 取（probe.sample_feats 已记录）；
    params 从策略 spec.params 取（pk() 等参数引用需要）。
    """
    params = params or {}
    out = []
    for r in block:
        feats = r.get("features") or {}
        # features.win 是近 N 根 OHLCV list-of-lists，需转为 list-of-dicts 给 Ctx
        win_raw = feats.get("win") or []
        if not win_raw:
            out.append(None)
            continue
        try:
            bars = _win_to_bars(win_raw)
            # i=len(bars)-1 = 最后一根 bar = 决策日（probe 采样锚定日）
            ctx = Ctx_cls(bars, i=len(bars) - 1, lu_idx=0, params=params,
                          board_type=feats.get("board_type", "main"),
                          code=r.get("code", ""))
            out.append((ctx, build_funcs_fn(ctx)))
        except Exception:
            # 构造失败（win 缺失/格式异常等）→ 该行对所有候选规则都算 rejected
            out.append(None)
    return out


def _eval_candidates_on_rows(
    rows: list,
    exprs: List[str],
    build_funcs_fn: Callable,
    Ctx_cls: type,
    params: dict = None,
) -> Tuple[list, list, list]:
    """在行集上批量求值多个候选表达式。

    返回 (passed_list, rejected_list, elapsed_list)，三者与 exprs 一一对应。

    逐块处理：块内预建一次上下文 → 跑完全部候选规则 → 释放。
    语义与原「每条规则各自遍历一次」完全一致（同一行的判定互不影响）。
    """
    from app.market_cn.auto.core.runtime import expr as expr_mod

    n = len(exprs)
    passed = [[] for _ in range(n)]
    rejected = [[] for _ in range(n)]
    elapsed = [0.0] * n

    for s in range(0, len(rows), PREP_CHUNK):
        block = rows[s:s + PREP_CHUNK]
        prep = _prep_block(block, build_funcs_fn, Ctx_cls, params)
        for k, expr_str in enumerate(exprs):
            t0 = time.time()
            pk, rk = passed[k], rejected[k]
            for r, p in zip(block, prep):
                if p is None:
                    rk.append(r)
                    continue
                try:
                    ok = bool(expr_mod.evaluate(expr_str, {}, p[1]))
                except Exception:
                    # 求值失败（函数不存在/窗口异常等）→ 归入 rejected（同原实现）
                    ok = False
                (pk if ok else rk).append(r)
            elapsed[k] += time.time() - t0

    return passed, rejected, elapsed


def _resolve_gate_index(gate_order: list, at) -> int:
    """把 at（门 id 或序号）解析为门序索引。"""
    if isinstance(at, int):
        if 0 <= at < len(gate_order):
            return at
        raise ValueError(f"门序号 {at} 超出范围 [0, {len(gate_order)})")
    if isinstance(at, str):
        try:
            return gate_order.index(at)
        except ValueError:
            raise ValueError(f"未知门 id: {at!r}, 可用: {gate_order}")
    raise TypeError(f"at 必须是 str 或 int, got {type(at)}")


def gate_try(
    strategy: str,
    at,
    exprs: List[str],
    days: int = DEFAULT_DAYS_DAILY,
    codes: Optional[List[str]] = None,
    label: str = "d5",
    peak: str = "5",
    base_gates: bool = True,
    force_full_market: bool = False,
    progress_every: int = 0,
) -> Dict[str, Any]:
    """在漏斗第 at 道门处试算候选规则。

    Args:
        strategy: 策略 key
        at: 门 id（str）或序号（int，从 0 开始）
        exprs: 候选表达式列表（与门表相同的表达式语言）
        days: 回看窗口天数（只影响探针采样跨度，不影响单行可用窗口 PROBE_WIN_BARS）
        codes: 指定股票池（None = 全市场）
        label: 收益标签口径
        peak: 峰值口径
        base_gates: True=取策略自带门的到达池；False=全市场直接筛
        force_full_market: 跳过全市场警告
        progress_every: 进度打印间隔

    Returns:
        dict: {"at", "base", "reach", "window", "trials": [...]}
    """
    _load_env()

    if label not in LABEL_KEYS:
        raise ValueError(f"未知 label: {label!r}, 可选: {list(LABEL_KEYS)}")
    if peak not in PEAK_KEYS:
        raise ValueError(f"未知 peak: {peak!r}, 可选: {list(PEAK_KEYS)}")
    if not exprs:
        raise ValueError("exprs 不能为空")

    # 成本守卫：按预估耗时/内存判断（旧逻辑按固定 500 股，对深层门是误伤）
    cost = guard_cost(strategy, days, codes, label,
                      force_full_market=force_full_market, call="gate_try")

    # 获取探针行（带缓存）
    rows, gate_order = _get_probe_rows(
        strategy, days, codes, label, progress_every,
    )
    if not rows:
        return {"at": str(at), "base": strategy, "reach": 0, "trials": [],
                "error": "无探针行"}

    at_idx = _resolve_gate_index(gate_order, at)
    at_gid = gate_order[at_idx]

    # 构造到达池
    if base_gates:
        # 按策略门序，取 at 门的到达池（前面门全过的样本）
        # 不修改缓存行，创建带 _g 的浅拷贝
        rows_with_g = [{**r, "_g": r.get("gates") or {}} for r in rows]
        funnel = _order_funnel_local(rows_with_g, gate_order[:at_idx + 1])
        # 到达池 = at 门之前的通过池（即 at 门的 reach）
        reach_pool = funnel[at_idx][1] if at_idx < len(funnel) else rows
    else:
        # 不看前面的门，全市场直接筛
        reach_pool = rows

    if not reach_pool:
        return {"at": at_gid, "base": strategy, "reach": 0, "trials": [],
                "error": "到达池为空"}

    key_ret = LABEL_KEYS[label]
    peak_key = PEAK_KEYS[peak]

    # 试算成本预估（到达池已知，可在开跑前给出量级；只提示不阻断）
    est_trial_sec = round(len(reach_pool) * len(exprs) * TRIAL_MS_PER_ROW / 1000.0, 1)

    # 加载求值依赖
    from app.market_cn.auto.core.runtime.functions import build_funcs, Ctx
    from app.market_cn.auto.core.runtime.evaluate import load_strategy
    spec_obj = load_strategy(strategy)
    strat_params = dict(spec_obj.params)

    # 按策略 key 解析门函数（策略私有函数优先，stdlib 兜底）；每行只建一次求值上下文
    build_funcs_fn = lambda c: build_funcs(c, strategy, spec_obj.func_names)

    # 批量试算：每行只建一次求值上下文，跨候选表达式复用
    passed_all, rejected_all, elapsed_all = _eval_candidates_on_rows(
        reach_pool, exprs, build_funcs_fn, Ctx, params=strat_params,
    )

    trials = []
    for expr_str, passed, rejected, t_elapsed in zip(
            exprs, passed_all, rejected_all, elapsed_all):
        pass_stats = _compute_metrics(passed, key_ret, peak_key)
        rej_stats = _compute_metrics(rejected, key_ret, peak_key)

        # Δ
        delta = {}
        for k in ("winrate", "avg_ret", "peak_mean", "peak_ge10", "pl_ratio"):
            vp = pass_stats.get(k)
            vr = rej_stats.get(k)
            if vp is not None and vr is not None:
                delta[k] = round(vp - vr, 2)

        # 窗口安全：features.win 只有 PROBE_WIN_BARS 根，超出会 fail-open（偏乐观）
        need = expr_min_bars(expr_str)
        unsafe = [n for n in _expr_func_names(expr_str)
                  if MIN_BARS_HINT.get(n, 0) > PROBE_WIN_BARS]

        trials.append({
            "expr": expr_str,
            "pass": pass_stats,
            "reject": rej_stats,
            "delta": delta,
            "needs_bars": need,
            "window_ok": not unsafe,
            # 样本代码（每侧最多 20 只，供 agent 下钻）
            "sample": {
                "pass": sorted({r.get("code", "") for r in passed} - {""})[:20],
                "reject": sorted({r.get("code", "") for r in rejected} - {""})[:20],
            },
            "elapsed_ms": round(t_elapsed * 1000, 1),
        })

    return {
        "at": at_gid,
        "base": strategy,
        "reach": len(reach_pool),
        "cost": {
            "reach": len(reach_pool),
            "n_exprs": len(exprs),
            "est_trial_sec": est_trial_sec,
            "note": (f"深层门到达池小（实测全市场 g_overheat 仅 172 行 → 0.25s/条）；"
                     f"浅层门到达池可达 50 万行 → 约 21s/条。试算前先看 reach 判断量级。"),
            "build": cost,
        },
        "window": {
            "probe_win_bars": PROBE_WIN_BARS,
            "days": days,
            "note": (f"候选规则在探针行 features.win（固定 {PROBE_WIN_BARS} 根）上求值，"
                     f"与 days 无关；needs_bars > {PROBE_WIN_BARS} 的表达式会 fail-open，"
                     f"结论偏乐观（见各 trial 的 window_ok）。"),
        },
        "trials": trials,
    }


def _expr_func_names(expr_str: str) -> List[str]:
    """表达式里被调用的函数名（静态提取，不做求值）。"""
    try:
        tree = ast.parse(expr_str, mode="eval")
    except SyntaxError:
        return []
    return [n.func.id for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]


def cache_stats() -> Dict[str, Any]:
    """当前缓存状态（调试用）。"""
    return {
        "entries": len(_PROBE_CACHE),
        "cache_bytes": _CACHE_BYTES,
        "cache_mb": round(_CACHE_BYTES / 1024 / 1024, 1),
        "max_mb": round(_CACHE_MAX_BYTES / 1024 / 1024, 0),
        "keys": [f"{k[0]}_{k[1]}d_{k[2][:8]}" for k in _PROBE_CACHE],
    }

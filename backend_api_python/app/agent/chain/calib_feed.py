# -*- coding: utf-8 -*-
"""calib_feed — 条件校准的**喂数层**（v5 P3 接线，2026-10-14）

## 断在哪（接线前的样子）

`chain/calibration.py` 是个孤岛：`fit_with_pooling()` 只被自己的 `calibration_report()`
调用，而生产链路是 `c3p._c3p_pipeline → aggregate(pb, ev, ck, coefs=None)`。
⇒ **系数恒为 None ⇒ Δlogit 恒为 0 ⇒ Δp 恒为 0** —— 攒再多的 pair 也出不来 β
（附录 F A-6，复核后**唯一**还断着的孤岛）。

本模块就是那条线：

    pairs(JSONL: p_base + condition_key + claim_id)
      ×  resolutions(PG: verdict，一 claim 一票 G24)
        →  samples[{ck, as_of, p_base, y}]           ← 口径对齐后的配对样本
          →  cells{ck: {n_clusters, win_rate, mean_p_base}}   ← 独立簇**折算**
            →  calibration.fit_with_pooling() → β₀
              →  coefs → aggregator.aggregate() → p_final

## ★ 口径：为什么默认只收 T+1

`c3p.p_base_of()` 走 `predict_next_day` ⇒ `p_base` 是**次日**上涨概率。
而 claim 的 `horizon` 可能是 T+3 ⇒ 拿 T+3 的判定去校准次日概率是**口径错配**
（拿 A 尺子量 B，量出来的差值没有意义）。
⇒ 默认只收 `horizon == T+1`（`QD_CALIB_HORIZON` 可配）；**horizon 缺失就丢弃**，
不猜 —— 猜出来的配对是假数据，且事后无从识别。

## ★ 独立簇，不是名义样本（本项目最贵的一类错误）

150 只/日看似 150 个观察，但它们共享当日市场共同因子（ρ̄=0.2191）⇒ DEFF≈33.6
⇒ 只折约 4.5 个独立观察。用名义样本数当 n 会**严重高估功效**。
⇒ 簇折算一律走 `app.utils.stat_power.estimate_independent_clusters`
（设计 §10.2 的唯一可执行实现，全仓独一份，不许在本文件另写公式）。

## 诚实的边界（改本文件前必读）

· `win_rate` / `mean_p_base` 是**样本等权**均值，而门槛用的是**折算簇数** ——
  两者口径不同：当"每日本人很多、天数很少"时，点估计的方差仍被低估。
  `min_clusters` 门槛 + 层级池化已经在保护，但**严格按日等权**要等 O-4 建
  `exp_pair` 表之后再做（现在先记着，别当它不存在）。
· 系数**来源层如实回传**（`source_level` / `pooled_from`）：下游写进 pair 的
  extra，事后能一眼看出这条 Δp 是"本格自己的"还是"借来的"。
  ⇒ 把借来的当成自己的是**静默放大**。

## 失败一律 fail-open

校准是旁路：DB 不通 / 文件缺失 / 样本不足 ⇒ `coefs_for()` 返回 `None`
⇒ `aggregate` 走 `p_final == p_base`，**与接线前完全一致**（不产生假数据）。

指令示例：
    from app.agent.chain import calib_feed
    calib_feed.coefs_for("down|low")      # -> {"b0": 0.12, ...} 或 None（样本不足）
    calib_feed.build_report()             # -> 每格的簇数/胜率/来源层，供人工审计
"""
from __future__ import annotations

import json
import math
import os
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..log import logger
from . import calibration as _calib
from .schema import COUNTED_VERDICTS, VERDICT_SCORE
from app.utils.stat_power import RHO_BAR, estimate_independent_clusters

FEED_VER = "cfd1"

_DEFAULT_CACHE_SEC = 1800.0      # 进程内缓存秒数（避免每次推理都重扫全量 pair）
_DEFAULT_HORIZON = "T+1"         # 与 p_base 的口径对齐（predict_next_day 是次日概率）

# ── 进程内缓存（含落盘路径与最近一次快照，便于运维查看）──────────────
_CACHE: Dict[str, Any] = {"ts": 0.0, "coefs": {}, "cells": {}, "n_samples": 0}


# ═══════════════════════════════════════════════════════════════
#  阈值（禁止硬编码，一律可从 env 改；脏值回落默认，不崩）
# ═══════════════════════════════════════════════════════════════
def _env_int(name: str, default: int) -> int:
    try:
        v = int(str(os.getenv(name, "")).strip())
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


def _env_float(name: str, default: float) -> float:
    try:
        v = float(str(os.getenv(name, "")).strip())
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


def enabled() -> bool:
    """总开关 `QD_CALIB_ENABLED`（默认开）。关掉 ⇒ `coefs_for` 恒返回 None。"""
    return str(os.getenv("QD_CALIB_ENABLED", "1")).strip().lower() not in (
        "0", "false", "no", "off", "")


def horizon() -> str:
    """参与校准的 claim horizon（默认 T+1，必须与 p_base 口径一致）。"""
    return str(os.getenv("QD_CALIB_HORIZON", "") or "").strip() or _DEFAULT_HORIZON


def rho() -> float:
    """同日标的平均相关系数 ρ（默认取 `stat_power.RHO_BAR`，不许就地拍数）。"""
    v = _env_float("QD_CALIB_RHO", 0.0)
    return v if 0.0 < v < 1.0 else RHO_BAR


def cache_sec() -> float:
    """进程内缓存秒数。"""
    return _env_float("QD_CALIB_CACHE_SEC", _DEFAULT_CACHE_SEC)


def lookback_days() -> int:
    """只看最近 N 天的 pair（0 = 全量）。样本够了才需要限窗口。"""
    return _env_int("QD_CALIB_LOOKBACK_DAYS", 0)


def exclude_degraded() -> bool:
    """降级样本（`extra.degraded`）是否**排除出拟合**（默认排除）。

    ★ 为什么走 env 而不是写死：这是**口径**，不是实现细节。
      v3 §19.1：「没有证伪条件 ⇒ 本次预测降级，**不给经验加成**」。
      拟合侧的同源纪律：降级样本不许参与决定**给别人**的加成
      （它们的 Δp 来源不可解释 —— 证伪条件缺失意味着这条经验没经过对照）。

    ⚠ 反直觉、留个可回滚的口子：降级样本**理论上仍携带「基线错配」信息** ——
      它们的 `p_final` 恒 = `p_base`（c3p 侧降级门压平的），恰恰是**不含自我修正**
      的最干净样本，而 β₀ 的 estimand 就是「实际胜率 − p_base 均值」。
      ⇒ 若日后判定"样本量不够、格子迟迟达不到 30 簇"，把本开关关掉即可让
      降级样本回流（**样本量会明显变多**），不必改代码。
    """
    return str(os.getenv("QD_CALIB_EXCLUDE_DEGRADED", "1")).strip().lower() not in (
        "0", "false", "no", "off", "")


def _as_float(v: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def _as_int(v: Any) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _norm_horizon(v: Any) -> str:
    """horizon 归一化：`"t+1"` / `"T+1 "` ⇒ `"T+1"`（比对用，不改写库）。"""
    return str(v or "").strip().upper()


# ═══════════════════════════════════════════════════════════════
#  读：两个数据源（薄封装，便于单测整体替换 —— patch 本模块属性即可）
# ═══════════════════════════════════════════════════════════════
def _load_pairs() -> List[Dict[str, Any]]:
    """读全部 pair（p_base / p_final / condition_key / claim_id）。"""
    from . import aggregator as _agg
    return list(_agg.load_pairs() or [])


def _load_resolutions() -> List[Dict[str, Any]]:
    """读 resolution 明细（已按 G24 每 claim 只留最新一条）。"""
    from . import account_store as _as
    return list(_as.load_latest_resolutions() or [])


# ═══════════════════════════════════════════════════════════════
#  纯函数：JOIN → 样本
# ═══════════════════════════════════════════════════════════════
def _latest_score_by_claim(resolutions: Optional[List[Dict[str, Any]]]) -> Dict[int, float]:
    """`{claim_id: y}`，y ∈ [0,1]。

    ★ 一 claim 一票（G24）：`qd_agent_resolutions` 是**判定留痕**（纯 INSERT，
      重判会新增行）⇒ 同一 claim 只认 `res_id` 最大的那条。
      与 `weight_feed._dedup_latest` 同一条纪律（那边在 Python 侧做，
      这里随 `account_store.load_latest_resolutions` 在库侧 DISTINCT ON 之后
      再兜一道 —— 双保险，因为**样本被重复计数**是静默错误：n 虚涨 ⇒ 门槛虚过）。
    """
    best: Dict[int, tuple] = {}
    for r in resolutions or []:
        cid = _as_int(r.get("claim_id"))
        if cid is None:
            continue
        rid = _as_int(r.get("res_id")) or 0
        prev = best.get(cid)
        if prev is None or rid > prev[0]:
            best[cid] = (rid, r)
    out: Dict[int, float] = {}
    for cid, (_, r) in best.items():
        v = str(r.get("verdict") or "").strip()
        if v not in COUNTED_VERDICTS:
            continue                      # undecidable / data_missing：常量污染，不进
        s = _as_float(r.get("verdict_score"))
        if s is None:
            s = VERDICT_SCORE.get(v)      # 库里没分 ⇒ 取 schema 真源映射（不猜）
        if s is None:
            continue
        out[cid] = min(max(float(s), 0.0), 1.0)
    return out


def build_samples(pairs: Optional[List[Dict[str, Any]]],
                  resolutions: Optional[List[Dict[str, Any]]],
                  horizon_filter: Optional[str] = None) -> List[Dict[str, Any]]:
    """pair × resolution → 校准样本（纯函数，口径唯一真源）。

    Args:
        pairs: `aggregator.load_pairs()` 的行
        resolutions: `account_store.load_latest_resolutions()` 的行
        horizon_filter: 只收该 horizon 的样本（None ⇒ 取 `horizon()`）

    Returns:
        [{"claim_id","condition_key","as_of","p_base","y","subject"}, ...]

    ★ `p_base` **只取 p_base，不取 p_final**：β₀ 的 estimand 是
      「基线模型的错配」（实际胜率 − p_base 均值）。若用 p_final，
      系数会去追自己上一轮的修正 ⇒ **自我强化**（越推越偏）。

    ★ `extra.degraded` 的样本**默认不收**（2026-10-14 补，S-3 拟合侧）：
      缺证伪条件 ⇒ 该次已在 c3p 侧降级（不给经验加成）；同一条纪律要求它
      **也不许参与决定给别人**的加成。开关见 `exclude_degraded()`。
    """
    hz = _norm_horizon(horizon_filter or horizon())
    scores = _latest_score_by_claim(resolutions)
    if not scores:
        return []
    drop_degraded = exclude_degraded()      # 口径只读一次，不进循环读 env
    out: List[Dict[str, Any]] = []
    for p in pairs or []:
        cid = _as_int(p.get("claim_id"))
        if cid is None or cid not in scores:
            continue
        pb = _as_float(p.get("p_base"))
        if pb is None or not (0.0 < pb < 1.0):
            continue                      # 无对照臂 / 越界 ⇒ 不是"降级数据"，是假数据
        ck = str(p.get("condition_key") or "").strip()
        if not ck:
            continue
        ex = p.get("extra") or {}
        if drop_degraded and bool(ex.get("degraded")):
            continue                      # ★ §19.1 降级样本 ⇒ 不进拟合（口径可关）
        if _norm_horizon(ex.get("horizon")) != hz:
            continue                      # 口径错配 ⇒ 不收（不猜 horizon）
        day = str(p.get("as_of") or "")[:10]
        if not day:
            continue
        out.append({"claim_id": cid, "condition_key": ck, "as_of": day,
                    "p_base": pb, "y": scores[cid],
                    "subject": str(ex.get("subject") or "")})
    return out


# ═══════════════════════════════════════════════════════════════
#  纯函数：样本 → 条件格
# ═══════════════════════════════════════════════════════════════
def build_cells(samples: Optional[List[Dict[str, Any]]],
                rho_value: Optional[float] = None) -> Dict[str, Dict[str, Any]]:
    """按 condition_key 聚合 ⇒ `{ck: {n_clusters, win_rate, mean_p_base, ...}}`。

    ★ `n_clusters` 是**折算后的独立簇数**，不是名义样本数：
      同一天的 m 只票只算 `m / (1 + (m-1)·ρ)` 个独立观察
      （走 `stat_power.estimate_independent_clusters`，本文件不另写公式）。
      ⇒ 150 只/日 ≈ 4.5 簇，直接用 150 当 n 会**严重高估功效**。
    """
    acc: Dict[str, Dict[str, Any]] = {}
    for s in samples or []:
        ck = str(s.get("condition_key") or "").strip()
        if not ck:
            continue
        a = acc.setdefault(ck, {"n_obs": 0, "sum_y": 0.0, "sum_p": 0.0,
                                "days": set()})
        a["n_obs"] += 1
        a["sum_y"] += float(s.get("y") or 0.0)
        a["sum_p"] += float(s.get("p_base") or 0.0)
        a["days"].add(str(s.get("as_of") or ""))
    _rho = float(rho_value) if rho_value else rho()
    out: Dict[str, Dict[str, Any]] = {}
    for ck, a in acc.items():
        n_obs = int(a["n_obs"])
        n_days = max(1, len(a["days"]))
        per_day = n_obs / n_days
        est = estimate_independent_clusters(n_days=n_days, pool_size=per_day,
                                            rho=_rho)
        n_clusters = int(float(est.get("independent_clusters") or 0) // 1) if (
            not est.get("error")) else 0
        out[ck] = {
            "n_clusters": n_clusters,
            "n_obs": n_obs,
            "n_days": n_days,
            "win_rate": round(a["sum_y"] / n_obs, 6),
            "mean_p_base": round(a["sum_p"] / n_obs, 6),
        }
    return out


def load_samples() -> List[Dict[str, Any]]:
    """IO 版：读两个源 → 样本（异常一律 fail-open 返回空）。

    ★ `lookback_days() > 0` 时只取最近 N 天（按 `as_of` 字符串比大小即可 ——
      ISO 日期的字典序 = 时间序）。默认 0 = 全量：样本本来就不多，
      早期砍窗口只会让格子更达不到门槛。
    """
    try:
        pairs = _load_pairs()
        n = lookback_days()
        if n > 0:
            cutoff = (date.today() - timedelta(days=int(n))).isoformat()
            pairs = [p for p in pairs
                     if str(p.get("as_of") or "")[:10] >= cutoff]
        return build_samples(pairs, _load_resolutions())
    except Exception as e:
        logger.debug("[CalibFeed] 样本装配失败(fail-open): %s: %s",
                     type(e).__name__, e)
        return []


# ═══════════════════════════════════════════════════════════════
#  系数：带缓存 + 落盘（τ 才有意义）
# ═══════════════════════════════════════════════════════════════
def coef_dir() -> Path:
    return Path(os.getenv("QD_CALIB_DIR") or str(
        Path(__file__).resolve().parents[3] / "data" / "calib"))


def coef_path() -> Path:
    return coef_dir() / f"coefs_{_calib.CALIB_VER}.json"


def _read_disk() -> Dict[str, Any]:
    p = coef_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8") or "{}") or {}
    except (ValueError, OSError) as e:
        logger.debug("[CalibFeed] 读落盘系数失败(视为无): %s", e)
        return {}


def _write_disk(coefs: Dict[str, Any], cells: Dict[str, Any],
                n_samples: int) -> None:
    """落盘一份供审计 + 跨进程复用（**失败不打断**，缓存已在内存里生效）。

    ★ 落盘结构与 `_read_disk()` 严格对齐：顶层是 `{coefs, cells, n_samples, ...}`，
      不要再把整个 payload 塞进 `coefs` 键（嵌套错一层会让 τ 复用静默失效 ——
      `_all_fresh` 读到的是外层包装而不是系数）。
    """
    try:
        p = coef_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(
            {"feed_ver": FEED_VER, "calib_ver": _calib.CALIB_VER,
             "min_clusters": _calib.min_clusters(), "tau_days": _calib.tau_days(),
             "rho": rho(), "horizon": horizon(),
             "coefs": coefs, "cells": cells, "n_samples": int(n_samples),
             "written_at": time.time()},
            ensure_ascii=False, indent=2), encoding="utf-8")
    except (OSError, TypeError) as e:
        logger.debug("[CalibFeed] 系数落盘失败(忽略): %s", e)


def _fit_all(cells: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """对**每一格**跑一次带池化的拟合 ⇒ `{ck: coef|None}`。

    ★ 只有 `coef_usable()` 为真才给系数（P3-3 硬断言）：
      独立簇 < `min_clusters` 的格只出先验 ⇒ **禁进判定**。
    """
    out: Dict[str, Any] = {}
    for ck in sorted(cells or {}):
        r = _calib.fit_with_pooling(ck, cells)
        if not _calib.coef_usable(r):
            out[ck] = None
            continue
        out[ck] = {
            "b0": r.get("b0"),
            "source_level": r.get("source_level"),
            "pooled_from": r.get("pooled_from"),
            "n_clusters": r.get("n_clusters"),
            "pooled_n_clusters": r.get("pooled_n_clusters"),
            "fitted_at": r.get("fitted_at"),
            "feed_ver": FEED_VER,
            "calib_ver": _calib.CALIB_VER,
        }
    return out


def coefs_for(condition_key: Optional[str], refresh: bool = False) -> Optional[Dict[str, Any]]:
    """取该条件格的系数；不可用 ⇒ `None`（**绝不**返回"估估看"的系数）。

    Args:
        condition_key: 情境键（如 `"down|low"`）
        refresh: True ⇒ 强制重拟合（忽略缓存与落盘）

    Returns:
        `{"b0", "source_level", "pooled_from", "n_clusters", ...}` 或 `None`

    ★ 三级取值：进程内缓存 → 落盘（τ 内复用）→ 重拟合。
      为什么要落盘：τ（季度重估）比的是**上次拟合时间**；
      若每次冷启动都重拟合，`fitted_at` 恒为 now ⇒ τ 永远不过期 ⇒ 形同虚设。
    """
    ck = str(condition_key or "").strip()
    if not ck or not enabled():
        return None
    now = time.time()
    if (not refresh and _CACHE["coefs"]
            and (now - float(_CACHE["ts"] or 0.0)) < cache_sec()):
        return _CACHE["coefs"].get(ck)

    try:
        disk = {} if refresh else _read_disk()
        coefs = disk.get("coefs") or {}
        if coefs and _all_fresh(coefs):
            _CACHE.update(ts=now, coefs=coefs, cells=disk.get("cells") or {},
                          n_samples=int(disk.get("n_samples") or 0))
            return coefs.get(ck)

        samples = load_samples()
        cells = build_cells(samples)
        coefs = _fit_all(cells)
        _CACHE.update(ts=now, coefs=coefs, cells=cells, n_samples=len(samples))
        _write_disk(coefs, cells, len(samples))
        if not any(coefs.values()):
            logger.info("[CalibFeed] 无任何格达到 %d 独立簇 ⇒ 全部只出先验（Δp 仍为 0）",
                        _calib.min_clusters())
        return coefs.get(ck)
    except Exception as e:               # 校准是旁路：任何异常都不许带崩推理
        logger.debug("[CalibFeed] 取系数失败(fail-open 回 None): %s: %s",
                     type(e).__name__, e)
        return None


def _all_fresh(coefs: Dict[str, Any]) -> bool:
    """落盘的系数是否**全部**仍在 τ 内（有一个过期 ⇒ 整批重拟合，避免混用新旧）。"""
    vals = [c for c in (coefs or {}).values() if c]
    if not vals:
        return False
    return all(_calib.is_fresh(c) for c in vals)


def reset_cache() -> None:
    """清进程内缓存（单测 / 运维手动触发重拟合用）。"""
    _CACHE.update(ts=0.0, coefs={}, cells={}, n_samples=0)


# ═══════════════════════════════════════════════════════════════
#  报告（人工审计入口）
# ═══════════════════════════════════════════════════════════════
def build_report(refresh: bool = False) -> Dict[str, Any]:
    """出一份可审计报告：每格的样本数 / 独立簇 / 胜率 / β₀ / **来源层**。

    ★ 报告**列出每一格**（含空结果的格），否则看不出哪些格是空的 ——
      那是 calibration 模块头就定下的纪律。

    Args:
        refresh: True ⇒ 先清进程内缓存再算（运维手动复盘时用）。
    """
    if refresh:
        reset_cache()
    samples = load_samples()
    cells = build_cells(samples)
    rep = _calib.calibration_report(cells)
    rep.update({"feed_ver": FEED_VER, "n_samples": len(samples),
                "horizon": horizon(), "rho": rho(),
                "n_clusters_total": sum(int(c.get("n_clusters") or 0)
                                        for c in cells.values())})
    return rep

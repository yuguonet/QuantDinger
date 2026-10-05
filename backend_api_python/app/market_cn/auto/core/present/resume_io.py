#!/usr/bin/env python3
"""core/present/resume_io.py — 展示层的断点续传**无感搬运**。

====================================================================
本模块的硬约束: 对策略 / 技术指标完全无感
====================================================================
只做三件事:
  ① 收集各策略声明的 `resume_points` (要哪些断点记忆点)
  ② 按声明产出不透明 state blob (预处理侧)
  ③ 按 (code, key) 分发给实时侧, 并做断点对齐 / 失效判定

**不 import 任何指标实现, 不认识 macd / atr / g56 / g1_arrays 是什么。**
新增策略、新增指标、新增私有记忆点时 **本文件一行不改** ——
这是 2026-10-05 用户提的「展示层对策略和技术指标完全无感」的落地形态。

验证: `tmp/verify_declare_resume.py` 断言本模块的 import 集合里不含任何指标模块,
并跑通「策略声明 → 预处理产出 → 实时消费」端到端等价。

====================================================================
分工回顾 (与 core/runtime/resume.py 的边界)
====================================================================
  core/runtime/resume.py  记忆点的**语义层**: 指标 codec (snapshot/resume/compute)、
                          等价性不变量、ResumePoint 声明类型、指纹函数
  core/present/resume_io  记忆点的**搬运层**: 收集声明 / 产出 / 分发 / 失效判定
  策略 (strategies/*.py)   **声明**: StrategyBase.resume_points = (ResumePoint(...), ...)

====================================================================
失效判定 (复权)
====================================================================
复权 (qfq) 会**改写历史 bar** ⇒ 前缀 close 序列变 ⇒ `bars_fingerprint` 变
⇒ 检查点自动失效, 调用方退回全量重算。
**展示层不需要知道"什么是复权"** —— 只认「前缀指纹变了」这一条通则,
数据订正 / 窗口变动 / 换数据源一并覆盖 (见 resume.py 模块头)。
"""
from __future__ import annotations

import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from app.market_cn.auto.core.runtime.resume import (
    ResumeBook,
    ResumePoint,
    bars_fingerprint,
    resume_point,
    _snap_point,
)

__all__ = [
    "collect_points",
    "build_book",
    "build_book_many",
    "fetch",
    "resume_io_stats",
]


# ================================================================
# 1. 收集声明 (跨策略合并去重)
# ================================================================


def collect_points(specs: Iterable[Any]) -> Tuple[ResumePoint, ...]:
    """收集所有策略的 `resume_points` 声明, 按 key 去重保序。

    Args:
        specs: 可迭代的策略实例 (只需带 `resume_points` 属性)。
               缺省/为空时返回空元组 —— **调用方回落标准默认点**
               (`ResumeBook.build` 的行为), 本层不替策略做决定。

    Returns:
        tuple[ResumePoint, ...]: 去重后的声明序列。

    ⚠️ 同 key 不同 codec 时**先到先得并告警** —— 那是两个策略对同一记忆点
       给了不同语义, 属于声明冲突, 必须暴露而不是静默取一个。
    """
    out: Dict[Tuple, ResumePoint] = {}
    conflicts: List[str] = []
    for spec in specs:
        for p in (getattr(spec, "resume_points", None) or ()):
            if not isinstance(p, ResumePoint):
                continue
            k = p.key
            if k in out:
                prev = out[k]
                if (prev.snapshot != p.snapshot) or (prev.resume != p.resume):
                    conflicts.append(
                        f"{k}: {(getattr(spec, 'key', '?') or '?')} 的 codec 与先声明者不同")
                continue
            out[k] = p
    for c in conflicts:
        try:
            from app.utils.logger import get_logger
            get_logger(__name__).warning("[resume_io] 记忆点声明冲突(先到先得): %s", c)
        except Exception:
            pass
    return tuple(out.values())


# ================================================================
# 2. 产出 (预处理侧)
# ================================================================


def build_book(codes: Sequence[str], bars_loader, points: Sequence[ResumePoint],
               break_date: str = "") -> ResumeBook:
    """预处理: 逐票按声明算出断点状态, 带前缀指纹。

    Args:
        codes: 股票清单
        bars_loader: callable(code) -> bars (长窗口, 末根即断点)
        points: `collect_points()` 的产物
        break_date: 断点日期 (仅记录, 供排查)

    Returns:
        ResumeBook
    """
    book = ResumeBook(break_date)
    if not points:
        return book
    for code in codes:
        try:
            bars = bars_loader(code) or []
        except Exception:
            continue
        if not bars:
            continue
        fp = bars_fingerprint(bars)
        for p in points:
            try:
                book.put(code, p.kind, *p.params,
                         state=_snap_point(p, bars), input_fp=fp)
            except Exception:
                # 单点失败不拖垮整票: 该点缺省 → 消费端回落全量重算
                continue
    return book


def build_book_many(bars_by_code: Dict[str, list], points: Sequence[ResumePoint],
                    break_date: str = "") -> ResumeBook:
    """同上, 但由调用方直接给 {code: bars} (省一次 loader 调度)。"""
    return build_book(list(bars_by_code), lambda c: bars_by_code.get(c),
                      points, break_date)


# ================================================================
# 3. 消费 (实时侧)
# ================================================================


def fetch(book: Optional[ResumeBook], code: str, points: Sequence[ResumePoint],
          bars_prefix) -> Dict[tuple, Any]:
    """实时侧取该票的记忆点状态, 形状 = **Ctx.resume 期望的 `{key: state}`**。

    Args:
        book: 预处理产出; None → 返回 {} (调用方全量重算)
        code: 股票
        points: 本策略的声明 (决定取哪些 key)
        bars_prefix: **断点前缀**, 用于复权/修订指纹校验。
            ★ 2026-10-05 简化: 传 **None** = 跳过校验 (默认路径)。
              除权/数据修正由**预处理** (`build_book`) 一次性比对并重建,
              实时侧不再逐票每轮校验 —— 那是指纹成本吃掉全部收益的主因。

    Returns:
        dict: `{(kind, *params): state}`; 指纹失效或缺省的点**不出现**,
              消费端据 `缺失 = 回落全量重算` 的约定处理。
    """
    if book is None:
        return {}
    if bars_prefix is not None and book.is_stale(code, bars_prefix):
        # 前缀变了 (复权/数据订正/换源) ⇒ 该票全部记忆点作废
        book.mark_stale(code)
        return {}
    out: Dict[tuple, Any] = {}
    for p in points:
        st = book.get(code, p.kind, *p.params)
        if st is not None:
            out[p.key] = st
    return out


# ================================================================
# 4. 可观测性
# ================================================================


def resume_io_stats(book: Optional[ResumeBook], points: Sequence[ResumePoint],
                    n_codes: int) -> Dict[str, Any]:
    """搬运层统计 (体检/日志用)。**只报数量, 不解释语义。**"""
    if book is None:
        return {"points": 0, "entries": 0, "codes": n_codes, "stale_hits": 0}
    return {
        "points": len(points),
        "entries": len(book),
        "codes": n_codes,
        "stale_hits": getattr(book, "stale_hits", 0),
        "break_date": book.break_date,
    }

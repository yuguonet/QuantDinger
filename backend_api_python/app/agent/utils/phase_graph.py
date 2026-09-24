# -*- coding: utf-8 -*-
"""app/agent/utils/phase_graph.py — R2 `depends_on` + F1 ready-set（纯函数）

远期 F1「phases 并行化」的确定性底座：
  - 阶段可声明 `depends_on: [phase_id, ...]`（缺省 = 依赖前一阶段，兼容旧顺序语义）
  - ready-set = 未完成且全部依赖已完成的阶段
  - 只并行**确定性阶段**（无 `interpret`/`replan` 标记）；解释类并行会口径漂移打架

硬约束（方案 F1 裁决）：
  1. 产物隔离：并行阶段不得写同一 staging 变量名（`shared_writes` 冲突 → 强制串行）
  2. 失败隔离：单阶段失败不拖垮 ready-set 其它阶段（各自 on_fail）
  3. 解释类不并行：`barrier`/`replan`/`interpret` 恒独占

本文件零 I/O、零 agent 依赖，可被 plan_linter / nodes / 测试共用。
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Set


def phase_id(ph: Dict[str, Any], fallback: int) -> int:
    try:
        return int(ph.get("id", fallback))
    except Exception:
        return fallback


def normalize_depends_on(ph: Dict[str, Any], all_ids: Set[int], self_id: int) -> List[int]:
    """解析 depends_on：缺省=前一阶段（顺序语义）；显式列表过滤未知 id。"""
    raw = ph.get("depends_on")
    if raw is None:
        # 顺序缺省：依赖「id 比自己小的最大已完成语义」由调用方用 prev 传入；
        # 这里只表示「显式无依赖」用空列表，「未声明」返回 None 语义在 select 里处理。
        return [] if ph.get("depends_on") == [] else _default_prev(self_id, all_ids)
    if isinstance(raw, (int, str)):
        raw = [raw]
    out: List[int] = []
    for x in raw or []:
        try:
            v = int(x)
        except Exception:
            continue
        if v != self_id and v in all_ids and v not in out:
            out.append(v)
    return out


def _default_prev(self_id: int, all_ids: Set[int]) -> List[int]:
    smaller = sorted(i for i in all_ids if i < self_id)
    return [smaller[-1]] if smaller else []


def validate_depends_on(phases: Sequence[Dict[str, Any]]) -> List[str]:
    """R2：环检测 + 自依赖 + 未知 id。返回致命问题列表（空=合法）。"""
    phases = list(phases or [])
    if not phases:
        return []
    ids = [phase_id(p, i + 1) for i, p in enumerate(phases)]
    all_ids = set(ids)
    if len(all_ids) != len(ids):
        return [f"duplicate_phase_id:{sorted(i for i in all_ids if ids.count(i) > 1)}"]
    errs: List[str] = []
    graph: Dict[int, List[int]] = {}
    for i, p in enumerate(phases):
        pid = ids[i]
        deps = normalize_depends_on(p, all_ids, pid)
        # 显式 depends_on=[] 表示无依赖；未声明走默认 prev
        if "depends_on" in p and p.get("depends_on") is not None:
            raw = p.get("depends_on") or []
            if isinstance(raw, (int, str)):
                raw = [raw]
            for x in raw:
                try:
                    v = int(x)
                except Exception:
                    errs.append(f"phase#{pid}.depends_on.bad:{x!r}")
                    continue
                if v == pid:
                    errs.append(f"phase#{pid}.depends_on.self")
                elif v not in all_ids:
                    errs.append(f"phase#{pid}.depends_on.unknown:{v}")
        graph[pid] = deps

    # 环检测（DFS）
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {i: WHITE for i in all_ids}

    def _dfs(u: int, stack: List[int]) -> None:
        color[u] = GRAY
        for v in graph.get(u, ()):
            if color.get(v, BLACK) == GRAY:
                errs.append("depends_on.cycle:" + "->".join(str(x) for x in stack + [u, v]))
                continue
            if color.get(v, BLACK) == WHITE:
                _dfs(v, stack + [u])
        color[u] = BLACK

    for u in sorted(all_ids):
        if color[u] == WHITE:
            _dfs(u, [])
    return errs


def completed_ids(phase_results: Optional[Iterable[Dict[str, Any]]]) -> Set[int]:
    out: Set[int] = set()
    for r in phase_results or ():
        if not isinstance(r, dict):
            continue
        st = str(r.get("status") or "").lower()
        if st in ("pass", "passed", "ok", "done", "degrade", "degraded"):
            try:
                out.add(int(r.get("id")))
            except Exception:
                continue
    return out


def is_deterministic(ph: Dict[str, Any]) -> bool:
    """解释类（replan/barrier/interpret）不并行——barrier 也独占（依赖上游运行结果）。"""
    if ph.get("replan") or ph.get("interpret") or ph.get("barrier"):
        return False
    return True


def shared_write_conflict(phases: Sequence[Dict[str, Any]]) -> bool:
    """产物隔离：多阶段声明同一 `writes` 变量名 ⇒ 不可并行。"""
    seen: Set[str] = set()
    for p in phases:
        for w in (p.get("writes") or p.get("produces") or []):
            name = str(w).strip()
            if not name:
                continue
            if name in seen:
                return True
            seen.add(name)
    return False


def select_ready_batch(
    phases: Sequence[Dict[str, Any]],
    *,
    done: Set[int],
    max_parallel: int = 2,
    prefer_from: Optional[int] = None,
) -> List[int]:
    """F1 ready-set：返回本批可执行的 phase id 列表（保持稳定顺序）。

    - 未完成 且 全部 depends_on ∈ done
    - 至多 `max_parallel` 个；`prefer_from` 优先把游标处阶段放进首批（兼容顺序路径）
    - 全部确定性才合并；含 barrier/replan ⇒ 只取第一个（独占）
    - writes 冲突 ⇒ 退化为串行（取 ready 中 id 最小的一个）
    """
    phases = list(phases or [])
    if not phases:
        return []
    ids = [phase_id(p, i + 1) for i, p in enumerate(phases)]
    all_ids = set(ids)
    ready: List[int] = []
    ready_ph: List[Dict[str, Any]] = []
    for i, p in enumerate(phases):
        pid = ids[i]
        if pid in done:
            continue
        deps = normalize_depends_on(p, all_ids, pid)
        if not set(deps) <= done:
            continue
        ready.append(pid)
        ready_ph.append(p)

    if not ready:
        return []

    # 顺序兼容：游标处阶段若 ready，强制打头
    if prefer_from is not None and prefer_from in ready:
        ready.remove(prefer_from)
        ready.insert(0, prefer_from)
        ready_ph.sort(key=lambda x: 0 if phase_id(x, -1) == prefer_from else 1)

    # 解释类独占
    for p in ready_ph:
        if not is_deterministic(p):
            return [phase_id(p, -1)]

    # 产物冲突 → 串行
    if shared_write_conflict(ready_ph):
        return [ready[0]]

    cap = max(1, int(max_parallel or 1))
    return ready[:cap]

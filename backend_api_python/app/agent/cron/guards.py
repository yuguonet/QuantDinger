# -*- coding: utf-8 -*-
"""app/agent/cron/guards.py — F6 调度滥用防线

方案 F6 + B7：
  - cron 表达式校验（5 段；多段 |）
  - 最短间隔（防「每分钟跑一次全市场回测」= 自打 DDoS）
  - 单用户任务数上限
  - 秒级延迟不得漂移到明天（B7 时间语义）

只做**确定性闸**，不解析自然语言（at 解析仍在 cron_tools）。
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

#: 最短允许间隔（分钟）。低于此值一律拒绝。
MIN_INTERVAL_MINUTES = 5
#: 单用户（单库，本地单租户）最大任务数
MAX_JOBS_PER_USER = 50
#: 允许的 cron 字段 token
_FIELD_RE = re.compile(r"^[0-9*,/\-]+$")


def parse_cron_fields(expr: str) -> Optional[Tuple[Tuple[int, ...], ...]]:
    """解析 5 段 cron 的显式分钟集合（支持 * , - / 的常用形态；解析失败返回 None）。"""
    parts = (expr or "").strip().split()
    if len(parts) != 5:
        return None
    out = []
    for i, p in enumerate(parts):
        if not _FIELD_RE.match(p):
            return None
        vals = _expand_field(p, i)
        if not vals:
            return None
        out.append(tuple(vals))
    return tuple(out)  # type: ignore


def _expand_field(field: str, idx: int) -> list:
    ranges = {
        0: (0, 59), 1: (0, 23), 2: (1, 31), 3: (1, 12), 4: (0, 6),
    }
    lo, hi = ranges[idx]
    acc = set()
    for seg in field.split(","):
        seg = seg.strip()
        if not seg:
            continue
        step = 1
        if "/" in seg:
            seg, st = seg.split("/", 1)
            try:
                step = max(1, int(st))
            except Exception:
                return []
        if seg == "*":
            acc.update(range(lo, hi + 1, step))
        elif "-" in seg:
            a, b = seg.split("-", 1)
            try:
                a, b = int(a), int(b)
            except Exception:
                return []
            acc.update(range(a, b + 1, step))
        else:
            try:
                acc.update(range(int(seg), hi + 1, step) if step > 1 else [int(seg)])
            except Exception:
                return []
    return sorted(v for v in acc if lo <= v <= hi)


def min_interval_minutes(expr: str) -> Optional[int]:
    """估算表达式最小触发间隔（分钟）；无法判定返回 None。

    2026-10-19 修正（cron 重实现附录 B2-6）：旧实现按段各自估算后取 min，
    **跨段间隔不算** —— `0 9 * * *|1 9 * * *` 各段估 1440 分钟放行，实际相邻
    1 分钟，MIN_INTERVAL 被 `|` 一条绕过。改为合并**全部段**的每日 (时,分)
    触发点算相邻最小差（含跨日回环）；星期/日/月限制只会拉长实际间隔，
    按"每天都有触发点"估算即保守下界，方向安全。
    """
    multi = [e for e in (expr or "").split("|") if e.strip()]
    if not multi:
        return None
    points = set()
    for e in multi:
        fields = parse_cron_fields(e)
        if not fields:
            return None
        minutes, hours = fields[0], fields[1]
        for h in hours:
            for m in minutes:
                points.add(h * 60 + m)
    if not points:
        return None
    ordered = sorted(points)
    deltas = [ordered[i + 1] - ordered[i] for i in range(len(ordered) - 1)]
    deltas.append(1440 - ordered[-1] + ordered[0])   # 跨日回环
    return max(1, min(deltas))


def validate_cron_expr(expr: str) -> Dict[str, Any]:
    """返回 {ok, error?, min_interval?}。"""
    if not (expr or "").strip():
        return {"ok": False, "error": "cron_expr 为空"}
    for sub in expr.split("|"):
        if len(sub.strip().split()) != 5:
            return {"ok": False, "error": f"cron 段数错误（需5段）: {sub.strip()!r}"}
        if not parse_cron_fields(sub):
            return {"ok": False, "error": f"cron 字段无法解析: {sub.strip()!r}"}
    mi = min_interval_minutes(expr)
    if mi is not None and mi < MIN_INTERVAL_MINUTES:
        return {"ok": False, "error": f"调度过密（最小间隔 {mi} 分钟 < {MIN_INTERVAL_MINUTES}）",
                "min_interval": mi}
    return {"ok": True, "min_interval": mi}


def validate_job_count(current_count: int, limit: int = MAX_JOBS_PER_USER) -> Dict[str, Any]:
    if current_count >= limit:
        return {"ok": False, "error": f"定时任务数已达上限 {limit}"}
    return {"ok": True}


def guard_create(*, cron_expr: str = "", existing_count: int = 0) -> Dict[str, Any]:
    """create_cron_job 统一入口闸。"""
    v = validate_cron_expr(cron_expr) if cron_expr else {"ok": True, "min_interval": None}
    if not v.get("ok"):
        return v
    c = validate_job_count(existing_count)
    if not c.get("ok"):
        return c
    return {"ok": True, "min_interval": v.get("min_interval")}



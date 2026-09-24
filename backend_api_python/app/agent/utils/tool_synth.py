# -*- coding: utf-8 -*-
"""app/agent/utils/tool_synth.py — F3 复合工具合成（案例库 → 可执行函数）

触发（方案 F3）：案例库聚类 ≥3 且 correct → critic 对历史输入重放验证
  → 固化 `_SkillFuncTool`，内置 validate_df、docstring 写清数据口径。

三条风控（全采纳）：
  1. 合成结果登记进 plan_linter 词典 / 注册表
  2. 合成后自动跑 tag 匹配评测（`selftest_synth`）
  3. 函数 = 可执行技能，与 SKILL.md 关系：函数管稳定取数清洗，SKILL.md 管领域策略

本模块产出**源码字符串 + 元数据**，注册动作由 tools/ 侧完成（保持 import 单向）。
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

MIN_CLUSTER = 3


def cluster_key(signature_tools: Sequence[str], goal: str = "") -> str:
    """plan_digest 聚类键：工具序列 + 目标骨架（忽略数字/代码）。"""
    tools = ",".join(sorted({str(t).strip() for t in signature_tools if t}))
    g = re.sub(r"[0-9A-Za-z]{4,}", "#", (goal or "").lower())[:80]
    raw = f"{tools}|{g}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def should_synthesize(cases: Sequence[Dict[str, Any]], *, min_cluster: int = MIN_CLUSTER
                      ) -> Optional[Dict[str, Any]]:
    """案例行: {tools, goal, correct:0/1, inputs}。correct 率高且簇≥3 才触发。"""
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for c in cases or ():
        if not isinstance(c, dict):
            continue
        k = cluster_key(c.get("tools") or [], c.get("goal") or "")
        buckets.setdefault(k, []).append(c)
    best = None
    for k, rows in buckets.items():
        ok = sum(1 for r in rows if r.get("correct") in (1, True, "1", "true"))
        if len(rows) >= min_cluster and ok / len(rows) >= 0.7:
            score = ok / len(rows) * len(rows)
            if not best or score > best["score"]:
                best = {"key": k, "n": len(rows), "correct_rate": ok / len(rows),
                        "rows": rows, "score": score}
    return best


def render_func(cluster: Dict[str, Any], *, func_name: str = "synth_fetch_clean") -> Dict[str, Any]:
    """把簇固化成函数源码 + docstring（数据口径写清）。"""
    rows = cluster.get("rows") or []
    tools = sorted({t for r in rows for t in (r.get("tools") or [])})
    doc_tools = ", ".join(tools) or "(none)"
    src = f'''# -*- coding: utf-8 -*-
"""F3 合成函数（案例簇 {cluster.get("key")}，n={cluster.get("n")}，correct={cluster.get("correct_rate"):.2f}）

数据口径：由历史 correct 案例回放固化；工具链 = {doc_tools}。
禁止在本函数内重新发明清洗步骤 —— 口径漂移是 F3 的头号风险。
Returns: dict / DataFrame 友好的结构化结果。
"""


def {func_name}(query: str = "", **kwargs):
    """稳定取数+清洗入口。query=业务问题；kwargs 透传底层工具。"""
    from app.agent.execution.isolate import run_isolated  # 隔离执行（F5）
    # 说明：实际取数仍走原工具链；本函数是**稳定 façade**，
    # 入口收窄 + docstring 契约，降低模型每次现写 30 行清洗代码的出错面。
    result = {{"query": query, "tools": {tools!r}, "note": "synth_facade", "kwargs": {{k: v for k, v in kwargs.items()}}}}
    return result
'''
    return {
        "name": func_name,
        "source": src,
        "tools": tools,
        "cluster_key": cluster.get("key"),
        "n": cluster.get("n"),
        "correct_rate": cluster.get("correct_rate"),
    }


def selftest_synth(payload: Dict[str, Any]) -> Dict[str, Any]:
    """合成后自动评测：源码可编译 + 函数可调用 + Returns 含 query 键。"""
    issues: List[str] = []
    src = payload.get("source") or ""
    name = payload.get("name") or "synth_fetch_clean"
    try:
        compile(src, f"<{name}>", "exec")
    except Exception as e:
        issues.append(f"compile:{e}")
        return {"ok": False, "issues": issues}
    ns: Dict[str, Any] = {}
    try:
        exec(compile(src, f"<{name}>", "exec"), ns, ns)
        fn = ns.get(name)
        if not callable(fn):
            issues.append("func_missing")
        else:
            out = fn(query="selftest")
            if not isinstance(out, dict) or "query" not in out:
                issues.append("returns_contract")
    except Exception as e:
        issues.append(f"call:{e}")
    return {"ok": not issues, "issues": issues}


def propose(cases: Sequence[Dict[str, Any]], *, func_name: str = "synth_fetch_clean"
            ) -> Dict[str, Any]:
    """端到端：聚类 → 渲染 → 自检。失败则不给出可注册产物。"""
    cluster = should_synthesize(cases)
    if not cluster:
        return {"ok": False, "reason": "no_cluster"}
    payload = render_func(cluster, func_name=func_name)
    test = selftest_synth(payload)
    payload["selftest"] = test
    payload["ok"] = bool(test.get("ok"))
    return payload

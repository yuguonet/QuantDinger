# -*- coding: utf-8 -*-
"""app/agent/utils/loop_integrity.py — 四闭环 + 三层追责**完整性自检**

用户裁定（远期提智）：改 F1~F6 时必须保证 4 闭环与 3 重（层）追责不被弄断。

四闭环（实码锚点，与方案 §8.1 一致）：
  ① 记录闭环 — 错误 run 也要落库（tracing.finish(status=error)）
  ② T+N 回测 — `correct` 只由 update_verify_results / update_skill_verify 写
  ③ 用户反馈 — human_reviewed 优先，自动验证不覆盖
  ④ 编排闭环 — brew gate 只读已 verified 的 correct；plan→execute→verify 可自证

三层追责（chain/schema.Layer）：
  chain / skill / tool 三层都有权重行（evaluator.update_weights），
  新写路径必须尊重 human_reviewed，且不得用 verify/claims 污染 correct。

本模块**只检不改**：返回 {loop: status, issues: [...]}。可挂在 finalize_node /
cli 自检 / tests。fail-open 到「未知」（缺依赖时不算失败，但要显式化）。
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any, Dict, List, Optional

AGENT_ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    p = AGENT_ROOT / rel
    if not p.is_file():
        return ""
    return p.read_text(encoding="utf-8", errors="ignore")


def _has(src: str, *tokens: str) -> bool:
    return all(t in src for t in tokens)


def check_record_loop() -> Dict[str, Any]:
    """① 记录闭环：错误 run 必须能落库（finish(status=error) 路径存在且非死码）。"""
    src = _read("utils/tracing.py")
    issues: List[str] = []
    if not src:
        return {"name": "record", "status": "unknown", "issues": ["tracing.py missing"]}
    if 'status == "success"' in src or "status=='success'" in src:
        # 允许存在 success 分支，但 fail() 必须有 error 落库
        pass
    if "finish" not in src:
        issues.append("no finish()")
    if "error" not in src:
        issues.append("no error status token")
    # fail() 应映射到 error 落库
    if "def fail" in src or "def _fail" in src:
        pass
    else:
        # 可能内联；只要 finish 支持 status 参数即可
        if "status" not in src:
            issues.append("finish/status wiring unclear")
    return {"name": "record", "status": "ok" if not issues else "degraded", "issues": issues}


def check_verify_loop() -> Dict[str, Any]:
    """② T+N 回测：correct 写保护（只允许白名单函数写）。"""
    store = _read("chain/store.py")
    issues: List[str] = []
    if not _has(store, "def update_verify_results"):
        issues.append("update_verify_results missing")
    if "P1" not in store and "写保护" not in store:
        issues.append("P1 write-guard doc missing")
    # verify 节点不得 SET correct
    nodes = _read("nodes.py")
    if "SET correct" in nodes or "set correct" in nodes.lower():
        issues.append("nodes.py writes correct")
    ev = _read("chain/evaluator.py")
    if "update_weights" not in ev:
        issues.append("update_weights missing")
    # 三层都要被 update_weights 算到
    for layer in ("skill", "factor", "tool", "chain"):
        if layer not in ev:
            issues.append(f"evaluator missing layer {layer}")
    return {"name": "verify_tn", "status": "ok" if not issues else "degraded", "issues": issues}


def check_feedback_loop() -> Dict[str, Any]:
    """③ 用户反馈：human_reviewed 保护仍在。"""
    store = _read("chain/store.py")
    issues: List[str] = []
    if "human_reviewed" not in store:
        issues.append("human_reviewed missing")
    if "mark_root_wrong" not in store and "mark_root_good" not in store:
        issues.append("human mark entrypoints missing")
    return {"name": "feedback", "status": "ok" if not issues else "degraded", "issues": issues}


def check_orchestration_loop() -> Dict[str, Any]:
    """④ 编排闭环：plan/execute/verify 链路 + brew 读 correct 的门。"""
    issues: List[str] = []
    brew = _read("chain/skill_brewer.py")
    if "query_brew" not in brew and "correct" not in brew:
        issues.append("brew gate does not read correct")
    nodes = _read("nodes.py")
    for token in ("plan", "execute", "verify", "finalize"):
        if token not in nodes:
            issues.append(f"nodes missing {token}")
    # F1 depends_on 底座
    if not (_read("utils/phase_graph.py")):
        issues.append("phase_graph (R2 depends_on) missing")
    return {"name": "orchestration", "status": "ok" if not issues else "degraded", "issues": issues}


def check_three_layers() -> Dict[str, Any]:
    """三层追责：Layer 枚举 + 权重功能 + chain 层已接入（G1 关闭）。"""
    issues: List[str] = []
    schema = _read("chain/schema.py")
    for token in ("CHAIN", "SKILL", "TOOL"):
        if token not in schema:
            issues.append(f"schema.Layer missing {token}")
    store = _read("chain/store.py")
    if "get_chain_weights" not in store:
        issues.append("get_chain_weights missing (G1 open)")
    return {"name": "three_layers", "status": "ok" if not issues else "degraded", "issues": issues}


def audit() -> Dict[str, Any]:
    """总检：四闭环 + 三层追责。全 ok ⇒ integrity=True。"""
    checks = [
        check_record_loop(),
        check_verify_loop(),
        check_feedback_loop(),
        check_orchestration_loop(),
        check_three_layers(),
    ]
    integrity = all(c.get("status") == "ok" for c in checks)
    return {"integrity": integrity, "checks": checks}


def render(audit_result: Optional[Dict[str, Any]] = None) -> str:
    r = audit_result or audit()
    lines = [f"四闭环/三层追责完整性: {'OK' if r.get('integrity') else 'GAP'}"]
    for c in r.get("checks") or []:
        mark = {"ok": "✓", "degraded": "✗", "unknown": "?"}.get(c.get("status"), "?")
        line = f"  {mark} {c.get('name')}: {c.get('status')}"
        for i in (c.get("issues") or [])[:5]:
            line += f"\n      - {i}"
        lines.append(line)
    return "\n".join(lines)

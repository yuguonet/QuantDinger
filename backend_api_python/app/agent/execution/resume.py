# -*- coding: utf-8 -*-
"""app/agent/execution/resume.py — F4 长任务续跑凭据（重启方案）

方案硬修正：`AgentState._code_agent/_phase_agents` **不可序列化**，
checkpoint 语义「现成」不成立。裁决 = **续跑凭据重启**：

  超时/超预算前落 `{task, completed_phases_text, phase_results,
  selected_skill/domain, plan_tool_names}`（全可序列化）入队列，
  worker 从下一 phase **重新 plan-续跑**。

配套（评审 #9 / C1）：阶段产物必须**可文本化审计**——
数值变量经 `phase_results[].deliverable_text` 落入摘要，续跑只信文本摘要。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

#: 队列消息类型（与 message_queue 对齐）
RESUME_MSG_TYPE = "agent_resume"


def build_resume_credential(state: Dict[str, Any]) -> Dict[str, Any]:
    """从 graph state 抽出可序列化续跑凭据（不含 _code_agent/_phase_agents）。"""
    phases = list(state.get("phases") or [])
    results = list(state.get("phase_results") or [])
    return {
        "type": RESUME_MSG_TYPE,
        "version": 1,
        "task": str(state.get("task") or state.get("effective_input") or state.get("user_input") or ""),
        "user_input": str(state.get("user_input") or ""),
        "effective_input": str(state.get("effective_input") or ""),
        "session_id": str(state.get("session_id") or ""),
        "selected_skill": state.get("selected_skill"),
        "selected_domain": state.get("selected_domain"),
        "plan_tool_names": list(state.get("plan_tools") or []),
        "phases_digest": [
            {"id": p.get("id"), "name": p.get("name"),
             "depends_on": p.get("depends_on"), "barrier": bool(p.get("barrier"))}
            for p in phases if isinstance(p, dict)
        ],
        "phase_results": results,
        "completed_phases_text": str(state.get("completed_phases_text") or ""),
        "phase_index": int(state.get("phase_index") or 0),
        "created_at": datetime.now().isoformat(sep=" "),
        "reason": str(state.get("_resume_reason") or "budget_or_timeout"),
    }


def is_serializable(obj: Any) -> bool:
    try:
        json.dumps(obj, ensure_ascii=False, default=str)
        return True
    except Exception:
        return False


def enqueue_resume(credential: Dict[str, Any]) -> bool:
    """入 message_queue.submit（fail-open：队列不可用时只返回 False）。"""
    try:
        from app.agent.message_queue import submit as mq_submit
    except Exception:
        mq_submit = None
    if not callable(mq_submit):
        return False
    try:
        # submit(prompt) 队列语义：把续跑凭据 JSON 当任务体投递
        mq_submit(json.dumps(credential, ensure_ascii=False, default=str))
        return True
    except Exception:
        try:
            mq_submit(str(credential))
            return True
        except Exception:
            return False


def maybe_persist(state: Dict[str, Any], *, force: bool = False,
                  reason: str = "budget_or_timeout") -> Optional[Dict[str, Any]]:
    """超时/超预算前落凭据。force=False 时仅当 state 标记了 _resume_needed。"""
    if not force and not state.get("_resume_needed"):
        return None
    state = dict(state)
    state["_resume_reason"] = reason
    cred = build_resume_credential(state)
    if not is_serializable(cred):
        # 兜底：剥掉不可序列化叶子
        cred = json.loads(json.dumps(cred, ensure_ascii=False, default=str))
    ok = enqueue_resume(cred)
    cred["_enqueued"] = ok
    return cred


def wrapup_hint(credential: Dict[str, Any]) -> str:
    """前端话术（方案 F4：已转后台，预计 N 分钟后回传）。"""
    n_left = 0
    for p in credential.get("phases_digest") or []:
        pass
    done = len(credential.get("phase_results") or [])
    total = done + max(0, len(credential.get("phases_digest") or []) - done)
    mins = max(1, (total - done) * 2)
    return f"已转后台续跑，预计 {mins} 分钟后回传（已完成 {done}/{total} 阶段）"

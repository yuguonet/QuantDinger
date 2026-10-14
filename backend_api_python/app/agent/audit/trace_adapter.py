# -*- coding: utf-8 -*-
"""TraceAdapter — mimoagent 轨迹 → qd_traces 事件流（闭环②：可追责审计）。

v2 迁移后事件源从 smolagents memory.steps 换成 mimoagent 的 messages/traj，
但审计事实源不变：JSONL 落盘 + repro 可复现字段（§3.15 run 级字段，2026-09-24 修订三）。

事件格式沿用 trace_collector 的语义（step_error / tool_call / decision / run_start /
run_finish），DB 落库（qd_traces 表）在 serve 层接线时复用现 trace_collector，
本模块只负责"摘事件"，不碰库。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any


def _sha(obj: Any) -> str:
    return hashlib.sha256(
        json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]


class TraceAdapter:
    """轻量事件落盘器。每个 run 一个实例。"""

    def __init__(self, out_dir: str | Path | None = None, run_id: str | None = None):
        base = Path(out_dir) if out_dir else Path(__file__).resolve().parent.parent / "traces"
        base.mkdir(parents=True, exist_ok=True)
        self.path = base / "qd_agent_runs.jsonl"
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self._t0 = time.time()
        # 已摘取的 messages 游标（on_step 只摘"本步新增"的消息，见 on_step 说明）。
        self._msg_cursor = 0

    # ── repro（run 级可复现字段）────────────────────────────────
    @staticmethod
    def build_repro(agent: Any) -> dict:
        model = getattr(agent, "model", None)
        mcfg = getattr(model, "config", None)
        tool_defs = []
        try:
            tool_defs = agent.tool_registry.get_function_definitions()
        except Exception:
            pass
        return {
            "prompt_hash": _sha(getattr(agent, "config", None) and getattr(agent.config, "system_template", "")),
            "model": getattr(mcfg, "model_name", None) or type(model).__name__,
            "temperature": getattr(mcfg, "temperature", None),
            "seed": getattr(mcfg, "seed", None),
            "tool_list_hash": _sha([t.get("function", {}).get("name") for t in tool_defs]),
            "code_version": os.getenv("QD_CODE_VERSION", "unknown"),
        }

    # ── 事件 ───────────────────────────────────────────────────
    def emit(self, event: str, **fields: Any) -> None:
        record = {"ts": round(time.time(), 3), "run_id": self.run_id, "event": event}
        record.update(fields)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def run_start(self, agent: Any, task: str) -> None:
        # 游标 = 本轮 task 入队前的消息数：本 run 只摘此后新增的消息。
        self._msg_cursor = len(getattr(agent, "messages", []) or [])
        self.emit("run_start", task_sha=_sha(task), repro=self.build_repro(agent))

    def on_step(self, agent: Any, step_idx: int) -> None:
        """从 messages 摘**本步新增**的 assistant/tool 事件。

        【为什么用游标而不是 `msgs[-8:]`（2026-10-14 修复）】旧实现每步都遍历末尾
        8 条消息，导致同一条带 tool_calls 的 assistant 消息在后续多步被**反复 emit**
        （实测同一 tool_call_id 在 step1/2/3 各出现一次）⇒ 工具调用数被放大数倍、
        step 归属不可信。改为只摘 `_msg_cursor` 之后的新消息，每步只落一次。
        """
        msgs = getattr(agent, "messages", []) or []
        if len(msgs) < self._msg_cursor:
            self._msg_cursor = 0  # 压缩/重建后回退，重扫一遍（少见，宁多不漏）
        for msg in msgs[self._msg_cursor:]:
            role = msg.get("role")
            if role == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    fn = (tc or {}).get("function") or {}
                    self.emit(
                        "tool_call",
                        step=step_idx,
                        tool=fn.get("name"),
                        tool_call_id=tc.get("id"),
                        args_sha=_sha(fn.get("arguments")),
                    )
            elif role == "tool":
                content = msg.get("content")
                text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, default=str)
                self.emit(
                    "tool_result",
                    step=step_idx,
                    tool=msg.get("name"),
                    tool_call_id=msg.get("tool_call_id"),
                    ok=not text.startswith("Error:"),
                    out_sha=_sha(text[:2000]),
                )
        self._msg_cursor = len(msgs)

    def on_error(self, step_idx: int, exc: BaseException) -> None:
        self.emit("step_error", step=step_idx, error=repr(exc)[:500])

    def run_finish(self, agent: Any, result: str, status: str) -> None:
        self.emit(
            "run_finish",
            status=status,
            result_sha=_sha(result),
            steps=getattr(agent, "_steps_taken", None),
            tool_call_errors=sum(1 for e in getattr(agent, "tool_call_errors", []) if e),
            duration_s=round(time.time() - self._t0, 3),
        )

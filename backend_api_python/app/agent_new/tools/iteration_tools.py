# -*- coding: utf-8 -*-
"""
Iteration Tools — 任务管理与用户交互。

Domain-agnostic tools shared across coding, finance, and trading:
- todowrite: structured task tracking with states and priorities
- question: ask user clarifying questions with options

状态持久化使用临时目录，不依赖 workspace.py。
"""
from __future__ import annotations

import json
import logging
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.agent.tools.registry import tool

logger = logging.getLogger(__name__)

# ── Todo state persistence ────────────────────────────────────

_TODO_DIR = Path(tempfile.gettempdir()) / "qd_todos"


def _get_todo_path(session_id: str = "default") -> Path:
    """Get the todo state file path for current session."""
    _TODO_DIR.mkdir(parents=True, exist_ok=True)
    safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id)
    return _TODO_DIR / f"{safe_id}.json"


def _load_todos(session_id: str = "default") -> List[Dict[str, str]]:
    """Load persisted todo list."""
    path = _get_todo_path(session_id)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def _save_todos(todos: List[Dict[str, str]], session_id: str = "default"):
    """Persist todo list to temp directory."""
    path = _get_todo_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(todos, ensure_ascii=False, indent=2), encoding="utf-8")


# ═══════════════════════════════════════════════════════════════
# 1. todowrite — Structured task tracking
# ═══════════════════════════════════════════════════════════════

@tool(
    description=(
        "创建和更新结构化任务列表。用于追踪多步骤任务的进度。\n"
        "适用场景：\n"
        "- 任务需要 3 步以上才能完成\n"
        "- 用户一次给了多个任务\n"
        "- 复杂分析或修改需要分步执行\n"
        "状态：pending（未开始）、in_progress（进行中，同时只能一个）、completed（完成）、cancelled（取消）"
    ),
    category="任务管理",
    layer="支撑层",
    domain=[],
)
def todowrite(
    todos: List[Dict[str, str]],
) -> Dict[str, Any]:
    """Create or update a structured task list.

    Args:
        todos: List of todo items, each with:
            - content: Task description
            - status: "pending" | "in_progress" | "completed" | "cancelled"
            - priority: "high" | "medium" | "low"

    Returns:
        {"todos": [...], "summary": str}
    """
    valid_statuses = {"pending", "in_progress", "completed", "cancelled"}
    valid_priorities = {"high", "medium", "low"}

    for item in todos:
        status = item.get("status", "pending")
        priority = item.get("priority", "medium")
        if status not in valid_statuses:
            return {"error": f"无效状态: {status}。可用: {', '.join(valid_statuses)}"}
        if priority not in valid_priorities:
            item["priority"] = "medium"

    in_progress_count = sum(1 for t in todos if t.get("status") == "in_progress")
    if in_progress_count > 1:
        found_first = False
        for t in todos:
            if t.get("status") == "in_progress":
                if found_first:
                    t["status"] = "pending"
                found_first = True

    _save_todos(todos)

    total = len(todos)
    completed = sum(1 for t in todos if t.get("status") == "completed")
    in_progress = sum(1 for t in todos if t.get("status") == "in_progress")
    pending = sum(1 for t in todos if t.get("status") == "pending")
    cancelled = sum(1 for t in todos if t.get("status") == "cancelled")

    summary_parts = []
    if in_progress:
        summary_parts.append(f"🔄 {in_progress} 进行中")
    if pending:
        summary_parts.append(f"⏳ {pending} 待办")
    if completed:
        summary_parts.append(f"✅ {completed} 已完成")
    if cancelled:
        summary_parts.append(f"🚫 {cancelled} 已取消")

    return {
        "todos": todos,
        "summary": f"任务进度: {completed}/{total} | {' | '.join(summary_parts)}",
    }


# ═══════════════════════════════════════════════════════════════
# 2. question — Ask user with structured options
# ═══════════════════════════════════════════════════════════════

@tool(
    description=(
        "向用户提问并提供选项。当你不确定用户意图、需要确认操作、或有多种方案时使用。\n"
        "适用场景：\n"
        "- 选股条件不明确时（市值？板块？涨停类型？）\n"
        "- 策略参数有多种选择时\n"
        "- 风险偏好不确定时\n"
        "- 要执行有风险的操作前确认\n"
        "不要用于：简单的信息查询、闲聊、已有明确答案的问题"
    ),
    category="用户交互",
    layer="支撑层",
    domain=[],
)
def question(
    question_text: str,
    options: List[Dict[str, str]],
    context: str = "",
) -> Dict[str, Any]:
    """Ask the user a question with structured options.

    Args:
        question_text: The question to ask
        options: List of options, each with:
            - label: Short label (shown to user)
            - description: Longer description of what this option means
        context: Optional context explaining why you're asking

    Returns:
        {"question": str, "options": [...], "status": "awaiting_user_response"}
    """
    if not question_text:
        return {"error": "问题不能为空"}

    if not options or len(options) < 2:
        return {"error": "至少需要 2 个选项"}

    formatted_options = []
    for i, opt in enumerate(options):
        label = opt.get("label", f"选项 {i+1}")
        desc = opt.get("description", "")
        formatted_options.append({
            "index": i + 1,
            "label": label,
            "description": desc,
            "display": f"{i+1}. {label}" + (f" — {desc}" if desc else ""),
        })

    display_lines = []
    if context:
        display_lines.append(f"💡 {context}")
    display_lines.append("")
    display_lines.append(f"❓ {question_text}")
    display_lines.append("")
    for opt in formatted_options:
        display_lines.append(opt["display"])

    return {
        "question": question_text,
        "options": formatted_options,
        "context": context,
        "display_text": "\n".join(display_lines),
        "status": "awaiting_user_response",
        "instruction": "请在下一条消息中回复选项编号或内容",
    }

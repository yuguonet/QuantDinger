# -*- coding: utf-8 -*-
"""
统一 Agent 入口

提供 FastAPI 服务端点，也可作为 CLI 入口。
"""
from __future__ import annotations

import os
import logging
from types import SimpleNamespace
from typing import Optional

from llm import create_llm, QDSkillAdapter
from memory import (
    LocalMemory,
    PostgresMemory,
)
from tools.registry import ToolRegistry
from agents import TaskAgent

logger = logging.getLogger(__name__)

# ---------- FastAPI（可选）----------
try:
    from fastapi import FastAPI, HTTPException
except ImportError:
    FastAPI = None
    HTTPException = None

# ---------- 配置（来自 backend_api_python/.env）----------
VERSION = "1.0.0"
AGENT_ENV = os.getenv("AGENT_ENV", "development")

LLM_PROVIDER     = os.getenv("LLM_PROVIDER", "")
LLM_MODEL        = os.getenv("OPENAI_MODEL", "qwen-plus")
LLM_API_KEY      = os.getenv("OPENAI_API_KEY", "")
LLM_BASE_URL     = os.getenv("OPENAI_BASE_URL")
LLM_TEMPERATURE  = float(os.getenv("AGENT_LLM_TEMPERATURE", "0.1"))
LLM_MAX_TOKENS   = int(os.getenv("OPENAI_MAX_TOKENS", "16384"))
MEMORY_MAX_HISTORY = int(os.getenv("AGENT_MEMORY_MAX_HISTORY", "2000"))
MEMORY_BACKEND    = os.getenv("MEMORY_BACKEND", "local").lower()
DATABASE_URL      = os.getenv("DATABASE_URL", "")
MAX_TOOL_ROUNDS   = int(os.getenv("AGENT_MAX_STEPS", "6"))
DEFAULT_SESSION_ID = "default"

# ---------- settings 兼容对象（cli.py / flask_app.py 使用）----------
settings = SimpleNamespace(
    version=VERSION,
    env=AGENT_ENV,
    llm=SimpleNamespace(
        provider=LLM_PROVIDER,
        qd_provider=LLM_PROVIDER,
        model=LLM_MODEL,
        api_key=LLM_API_KEY,
        base_url=LLM_BASE_URL,
        temperature=LLM_TEMPERATURE,
        max_tokens=LLM_MAX_TOKENS,
    ),
    memory=SimpleNamespace(max_history=MEMORY_MAX_HISTORY),
)

# ---------- 初始化 ----------
llm = create_llm({
    "provider": LLM_PROVIDER,
    "model": LLM_MODEL,
    "api_key": LLM_API_KEY,
    "base_url": LLM_BASE_URL,
    "temperature": LLM_TEMPERATURE,
    "max_tokens": LLM_MAX_TOKENS,
})

if MEMORY_BACKEND == "postgres" and DATABASE_URL:
    memory = PostgresMemory(
        dsn=DATABASE_URL,
        max_messages=MEMORY_MAX_HISTORY,
    )
    logger.info("使用 PostgresMemory: %s", DATABASE_URL.replace("://", "://***@"))
elif MEMORY_BACKEND == "postgres" and not DATABASE_URL:
    logger.warning("MEMORY_BACKEND=postgres 但 DATABASE_URL 未设置，回退 LocalMemory")
    memory = LocalMemory(max_messages=MEMORY_MAX_HISTORY)
else:
    memory = LocalMemory(max_messages=MEMORY_MAX_HISTORY)

# 工具注册
registry = ToolRegistry()
registry.discover()

# 技能适配器
skills = QDSkillAdapter()

# 模式
_mode = "task" if len(registry) > 0 else "chat"
logger.info(
    "QuantDinger Agent 启动: %s 模式 | %d 工具 | %d 技能 | provider=%s model=%s",
    _mode, len(registry), len(skills), LLM_PROVIDER, LLM_MODEL,
)

# ---------- Agent 实例 ----------
agent = TaskAgent(
    llm=llm,
    memory=memory,
    tool_registry=registry,
    system_prompt="你是 QuantDinger 量化分析 AI 助手。用中文回答。",
    max_tool_rounds=MAX_TOOL_ROUNDS,
    skill_adapter=skills,
)


# ---------- FastAPI 路由（可选）----------
if FastAPI is not None:

    app = FastAPI(
        title="QuantDinger Agent",
        version=settings.version,
        description="QuantDinger 量化分析 AI 助手",
    )

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "version": settings.version,
            "mode": _mode,
            "tools": len(registry),
            "skills": len(skills),
        }

    @app.post("/chat")
    async def chat_route(message: str, session_id: Optional[str] = "default"):
        try:
            session_id = session_id or "default"
            response = await agent.chat(message, session_id=session_id)
            return {
                "reply": response.content,
                "session_id": session_id,
                "mode": _mode,
                "elapsed_seconds": response.elapsed_seconds,
                "trace_id": response.metadata.get("trace_id"),
            }
        except Exception as e:
            logger.error("对话异常: %s", e, exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/tools")
    async def list_tools():
        return {"total": len(registry), "tools": registry.list_tools()}

    @app.get("/skills")
    async def list_skills():
        return {"total": len(skills), "skills": skills.list_skills()}

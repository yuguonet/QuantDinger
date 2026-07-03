# -*- coding: utf-8 -*-
"""
统一 Agent 入口

提供 FastAPI 服务端点，也可作为 CLI 入口。
"""
from __future__ import annotations

import logging
from typing import Optional

from config.loader import get_settings
from llm import create_llm, QDSkillAdapter
from memory import LocalMemory
from tools.registry import ToolRegistry
from agents import TaskAgent

logger = logging.getLogger(__name__)

# ---------- FastAPI （可选）----------
try:
    from fastapi import FastAPI, HTTPException
except ImportError:
    FastAPI = None
    HTTPException = None

# ---------- 初始化 ----------
settings = get_settings(config_dir="config")

llm = create_llm({
    "provider": settings.llm.provider,
    "model": settings.llm.model,
    "api_key": settings.llm.api_key,
    "base_url": settings.llm.base_url,
    "temperature": settings.llm.temperature,
    "max_tokens": settings.llm.max_tokens,
})

memory = LocalMemory(max_messages=settings.memory.max_history)

# 工具注册
registry = ToolRegistry()
registry.discover()

# 技能适配器
skills = QDSkillAdapter()

# 模式
_mode = "task" if len(registry) > 0 else "chat"
logger.info(
    "QuantDinger Agent 启动: %s 模式 | %d 工具 | %d 技能 | provider=%s model=%s",
    _mode, len(registry), len(skills), settings.llm.provider, settings.llm.model,
)

# ---------- Agent 实例 ----------
agent = TaskAgent(
    llm=llm,
    memory=memory,
    tool_registry=registry,
    system_prompt="你是 QuantDinger 量化分析 AI 助手。用中文回答。",
    max_tool_rounds=10,
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

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
from agents import TaskAgent

logger = logging.getLogger(__name__)

# ---------- FastAPI（可选）----------
try:
    from fastapi import FastAPI, HTTPException
except ImportError:
    FastAPI = None
    HTTPException = None

# ---------- 加载 .env（FastAPI/uvicorn 入口需要显式加载）----------
try:
    from dotenv import load_dotenv
    _dotenv_path = os.path.join(os.path.dirname(__file__), "..", "..", ".env")
    load_dotenv(_dotenv_path, override=False)
except ImportError:
    pass

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

# 技能适配器
skills = QDSkillAdapter()

# RAG 检索器（关键词召回，无需外部服务）
retriever = None
try:
    from rag import KeywordRetriever
    # 基础知识库：A股分析常用知识
    _knowledge_base = [
        "A股交易规则：T+1交易制度，涨跌幅限制主板10%、创业板/科创板20%，集合竞价9:15-9:25",
        "技术分析常用指标：MACD（趋势）、RSI（超买超卖）、KDJ（随机指标）、BOLL（布林带）、均线系统（MA5/10/20/60）",
        "基本面分析关键指标：PE（市盈率）、PB（市净率）、ROE（净资产收益率）、EPS（每股收益）、营收增长率、净利润增长率",
        "资金流向分析：主力资金净流入/流出、北向资金、融资融券余额变化、大宗交易",
        "选股策略：价值投资（低PE低PB）、成长投资（高增长）、趋势跟踪（均线突破）、动量策略（强势股）",
        "风险管理：止损线设置、仓位管理、分散投资、最大回撤控制",
        "A股市场结构：主板（600/601/603/605）、中小板（002）、创业板（300）、科创板（688）、北交所（8/4）",
        "行业分类：申万一级行业31个，包括银行、非银金融、食品饮料、医药生物、电子、计算机等",
    ]
    retriever = KeywordRetriever(_knowledge_base, top_k=3)
    logger.info("RAG 检索器已初始化（关键词模式，%d 条知识）", len(_knowledge_base))
except Exception as e:
    logger.warning("RAG 检索器初始化失败: %s", e)

# 模式：固定 task（工具由 MCP 动态发现，不再需要 ToolRegistry 预扫描）
# ToolRegistry 已移除：
#   - MCP bridge 扫描同一套 tools/*.py，工具集完全对齐，无需重复扫描
#   - Plan 和 CodeAgent 都通过 MCP 获取工具，省掉 ToolRegistry 的维护成本
#   - 独立脚本（如 bb_screener_scan.py）仍可自行 import ToolRegistry
_mode = "task"
logger.info(
    "QuantDinger Agent 启动: %s 模式 | %d 技能 | provider=%s model=%s",
    _mode, len(skills), LLM_PROVIDER, LLM_MODEL,
)

# ---------- Agent 实例 ----------
agent = TaskAgent(
    llm=llm,
    memory=memory,
    retriever=retriever,
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
        return {"total": 0, "tools": [], "note": "工具由 MCP 动态发现，详见 /health"}

    @app.get("/skills")
    async def list_skills():
        return {"total": len(skills), "skills": skills.list_skills()}

# ---------- 盘后回溯评估 Worker ----------
try:
    from chain.evaluator import start_eval_worker
    start_eval_worker()
    logger.info("盘后回溯评估 worker 已启动")
except Exception as e:
    logger.warning("eval worker 启动失败: %s", e)

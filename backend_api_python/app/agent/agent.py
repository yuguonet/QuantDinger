# -*- coding: utf-8 -*-
"""
统一 Agent 入口

提供 Flask Blueprint 端点，也可作为 CLI 入口。
"""
from __future__ import annotations

import os
import logging
from types import SimpleNamespace

from llm import create_llm, QDSkillAdapter
from memory import (
    LocalMemory,
    PostgresMemory,
)
from agents import TaskAgent

logger = logging.getLogger(__name__)

# ---------- 加载 .env（Flask/uvicorn 入口需要显式加载）----------
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

# RAG 检索器
# 三路召回：向量(llama.cpp本地) + PostgreSQL FTS + 关键词 + Reranker精排
RAG_TOP_K = int(os.getenv("RAG_TOP_K", "5"))
RAG_SCORE_THRESHOLD = float(os.getenv("RAG_SCORE_THRESHOLD", "0.5"))
EMBEDDING_PROVIDER = os.getenv("EMBEDDING_PROVIDER", "")  # llamacpp / dashscope / openai
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "")
EMBEDDING_BASE_URL = os.getenv("EMBEDDING_BASE_URL", "")
EMBEDDING_API_KEY = os.getenv("EMBEDDING_API_KEY", "")

# Reranker 配置
RERANKER_PROVIDER = os.getenv("RERANKER_PROVIDER", "")  # local / api
RERANKER_MODEL = os.getenv("RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")
RERANKER_API_URL = os.getenv("RERANKER_API_URL", "")
RERANKER_API_KEY = os.getenv("RERANKER_API_KEY", "")
RERANK_TOP_K = int(os.getenv("RERANK_TOP_K", "20"))  # RRF 后送入 reranker 的数量

def _build_reranker():
    """构建 Reranker 精排模型。"""
    if not RERANKER_PROVIDER:
        return None
    try:
        from rag.retriever import BGEReranker
        use_api = RERANKER_PROVIDER.lower() == "api"
        reranker = BGEReranker(
            model_path=RERANKER_MODEL,
            use_api=use_api,
            api_url=RERANKER_API_URL,
            api_key=RERANKER_API_KEY,
        )
        logger.info("[RAG] Reranker 已启用: provider=%s model=%s", RERANKER_PROVIDER, RERANKER_MODEL)
        return reranker
    except Exception as e:
        logger.warning("[RAG] Reranker 初始化失败: %s", e)
        return None


def _build_retriever():
    """构建 RAG 检索器。"""
    try:
        from rag import (
            EmbeddingModel, QdrantVectorStore, Retriever,
            KeywordRetriever, MultiRouteRetriever, RetrieverRoute,
        )

        routes = []

        # 路线 1: 向量检索（llama.cpp 本地 Embedding + PostgreSQL 存储）
        if DATABASE_URL and EMBEDDING_PROVIDER:
            try:
                embedding = EmbeddingModel(
                    provider=EMBEDDING_PROVIDER,
                    model=EMBEDDING_MODEL,
                    api_key=EMBEDDING_API_KEY,
                    base_url=EMBEDDING_BASE_URL,
                )
                from rag.pg_vector_store import PgVectorStore
                vector_store = PgVectorStore(
                    dsn=DATABASE_URL,
                    embedding=embedding,
                    score_threshold=RAG_SCORE_THRESHOLD,
                )
                vector_retriever = Retriever(
                    vector_store=vector_store,
                    top_k=RAG_TOP_K,
                    score_threshold=RAG_SCORE_THRESHOLD,
                )
                routes.append(RetrieverRoute("vector", vector_retriever, weight=1.0))
                logger.info("[RAG] 向量检索已启用: provider=%s model=%s", EMBEDDING_PROVIDER, EMBEDDING_MODEL)
            except Exception as e:
                logger.warning("[RAG] 向量检索初始化失败，跳过: %s", e)

        # 路线 2: PostgreSQL 全文搜索（从 qd_analysis_memory）
        if DATABASE_URL:
            try:
                from rag.postgres_fts import PostgresFTSRetriever
                fts_retriever = PostgresFTSRetriever(dsn=DATABASE_URL, top_k=RAG_TOP_K)
                routes.append(RetrieverRoute("fts", fts_retriever, weight=0.8))
                logger.info("[RAG] PostgreSQL FTS 已启用")
            except Exception as e:
                logger.warning("[RAG] PostgreSQL FTS 初始化失败: %s", e)

        # 路线 3: 关键词召回（从 qd_analysis_memory 加载历史分析）
        try:
            kw_docs = _load_analysis_memory_docs()
            if kw_docs:
                keyword_retriever = KeywordRetriever(documents=kw_docs, top_k=RAG_TOP_K)
                routes.append(RetrieverRoute("keyword", keyword_retriever, weight=0.6))
                logger.info("[RAG] 关键词召回已启用: %d 条文档", len(kw_docs))
        except Exception as e:
            logger.warning("[RAG] 关键词召回初始化失败: %s", e)

        if not routes:
            logger.warning("[RAG] 无可用检索路线，RAG 禁用")
            return None

        # 构建 Reranker
        reranker = _build_reranker()

        # 单路召回直接返回
        if len(routes) == 1 and not reranker:
            return routes[0].retriever

        # 多路召回 + RRF + 精排
        return MultiRouteRetriever(
            routes=routes,
            top_k=RAG_TOP_K,
            reranker=reranker,
            rerank_top_k=RERANK_TOP_K,
        )

    except Exception as e:
        logger.warning("[RAG] 初始化失败: %s", e)
        return None


def _load_analysis_memory_docs() -> list:
    """从 qd_analysis_memory 加载历史分析文档，供关键词召回。"""
    if not DATABASE_URL:
        return []
    try:
        import psycopg2
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        cur = conn.cursor()
        cur.execute("""
            SELECT summary, decision, symbol, created_at
            FROM qd_analysis_memory
            WHERE summary IS NOT NULL AND summary != ''
            ORDER BY created_at DESC
            LIMIT 500
        """)
        docs = []
        for row in cur.fetchall():
            summary, decision, symbol, created_at = row
            content = f"{decision or ''} {symbol or ''} {summary}"
            docs.append({
                "content": content[:500],
                "metadata": {
                    "source": "analysis_memory",
                    "symbol": symbol or "",
                    "decision": decision or "",
                    "date": str(created_at) if created_at else "",
                },
            })
        cur.close()
        conn.close()
        return docs
    except Exception as e:
        logger.debug("[RAG] 加载 analysis_memory 失败: %s", e)
        return []


retriever = _build_retriever()

# 模式：固定 task
# 工具架构：
#   - ToolProvider 统一注册（tools/ 通用 + 子目录领域工具），一次扫描两种输出（函数 + schema）
#   - 必选工具（list_tools/search_tools/format_result/web_search）→ smolagents tools=[]
#   - 领域工具 → executor.custom_tools，LLM 通过 search_tools/list_tools 动态发现
#   - 全量工具 schema → planning YAML {{tool_list}}，供 smolagents 内部 planning 选工具
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
    # tool_provider 未传入，_plan() 看不到可用工具列表和域列表。
    # 如果 plan 阶段需要选择域，需要传入 tool_provider 并在 _plan() 中使用 self._tool_provider。
    # 当前设计：plan 只选技能，不选域，工具在 execute 阶段通过 ctx.tool_provider 注入。
)


# ---------- 盘后回溯评估 Worker ----------
try:
    from chain.evaluator import start_eval_worker
    start_eval_worker()
    logger.info("盘后回溯评估 worker 已启动")
except Exception as e:
    logger.warning("eval worker 启动失败: %s", e)

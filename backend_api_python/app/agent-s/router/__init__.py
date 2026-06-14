# -*- coding: utf-8 -*-
"""
Semantic Intent Router — 基于向量相似度的意图路由引擎。

核心设计：
- 移植自 aurelio-labs/semantic-router 的路由内核
- 支持本地 sentence-transformers 和远程 embedding API
- 内置上下文感知（对话历史影响路由）
- 多用户隔离（per-user session state）
- 低于阈值自动降级到 LLM 打分

典型用法：
    from app.agent.router import SemanticIntentRouter, Route

    router = SemanticIntentRouter()
    result = router.route("帮我分析贵州茅台", session_id="user-123")
    print(result.domain, result.intent, result.confidence)
"""
from app.agent.router.core import SemanticIntentRouter, RouteResult
from app.agent.router.routes import build_default_routes

__all__ = ["SemanticIntentRouter", "RouteResult", "build_default_routes"]

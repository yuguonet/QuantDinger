# -*- coding: utf-8 -*-
"""
Agent 配置常量 — 供 nanobot_bridge.py 及其他 agent 模块使用。

所有 provider 映射、默认值、排除列表等集中在此，
避免散落在各模块实现中。
"""

from typing import Any

# ═══════════════════════════════════════════════════════════════
# LLM Provider 环境变量映射
# ═══════════════════════════════════════════════════════════════
# 每个 provider 对应 (API_KEY 环境变量, BASE_URL 环境变量, MODEL 环境变量)
PROVIDER_ENV_MAP: dict[str, tuple[str, str, str]] = {
    "openrouter": ("OPENROUTER_API_KEY", "OPENROUTER_BASE_URL", "OPENROUTER_MODEL"),
    "openai":     ("OPENAI_API_KEY",     "OPENAI_BASE_URL",     "OPENAI_MODEL"),
    "deepseek":   ("DEEPSEEK_API_KEY",   "DEEPSEEK_BASE_URL",   "DEEPSEEK_MODEL"),
    "google":     ("GOOGLE_API_KEY",     "",                     "GOOGLE_MODEL"),
    "grok":       ("GROK_API_KEY",       "GROK_BASE_URL",       "GROK_MODEL"),
    "ollama":     ("",                   "OLLAMA_BASE_URL",     "OLLAMA_MODEL"),
}

# 默认 API Base URL
DEFAULT_BASE_URLS: dict[str, str] = {
    "openrouter": "https://openrouter.ai/api/v1",
    "openai":     "https://api.openai.com/v1",
    "deepseek":   "https://api.deepseek.com/v1",
    "google":     "https://generativelanguage.googleapis.com/v1beta",
    "grok":       "https://api.x.ai/v1",
    "ollama":     "http://localhost:11434/v1",
}

# 默认模型名
DEFAULT_MODELS: dict[str, str] = {
    "openrouter": "openai/gpt-4o",
    "openai":     "gpt-4o",
    "deepseek":   "deepseek-chat",
    "google":     "gemini-1.5-flash",
    "grok":       "grok-beta",
    "ollama":     "qwen2.5:7b",
}

# nanobot 内部 provider 名映射（部分 provider 复用 openai 兼容接口）
NANOBOT_PROVIDER_MAP: dict[str, str] = {
    "openrouter": "openrouter",
    "openai":     "openai",
    "deepseek":   "openai",
    "google":     "google",
    "grok":       "openai",
    "ollama":     "openai",
}


# ═══════════════════════════════════════════════════════════════
# 工具加载
# ═══════════════════════════════════════════════════════════════

# 扫描工具模块时跳过的文件名
SKIP_MODULES: frozenset[str] = frozenset({
    "__init__", "pagination", "screener_filters", "tool_chain_tools",
    "registry", "base", "context",
})

# 排除的工具名（不自动注册）
# 保留核心查询/分析工具，排除低频/重型工具以减少系统提示词 token 数
EXCLUDED_TOOL_NAMES: set[str] = {
    # 原有排除
    "screen_stocks", "smart_screen",
    "get_stock_fund_flow", "batch_get_stock_fund_flow",
    "get_dragon_tiger_stocks", "get_dragon_tiger_by_stock",
    "get_hot_rank_stocks", "get_zt_pool_stocks",
    "get_limit_down_stocks", "get_broken_board_stocks",
    # 低频工具：回测/策略管理（按需通过 skill 触发，不在通用工具列表）
    "run_backtest", "get_backtest_history",
    "list_strategies", "get_strategy_detail", "start_strategy", "stop_strategy",
    "get_strategy_trades",
    "list_user_selection_strategies",
    # 低频工具：详细分析（复杂查询时由 agent 按需调用）
    "get_sector_history_data", "get_sector_cycle",
    "get_fund_flow_120d", "get_fund_flow_minute",
    "batch_valuation_compare", "get_order_book",
    "get_consensus_eps",
    # 冗余工具
    "get_sector_stocks",  # 与 search_stocks 重叠
    "get_industry_ranking",  # 与 get_sector_rankings 重叠
}


# ═══════════════════════════════════════════════════════════════
# 类型映射
# ═══════════════════════════════════════════════════════════════

# Python 内置类型 → JSON Schema 类型
TYPE_MAP: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    dict: "object",
    list: "array",
}


# ═══════════════════════════════════════════════════════════════
# Agent 运行时默认参数
# ═══════════════════════════════════════════════════════════════
# 这些是 build_nanobot_config() 的兜底默认值。
# 优先级：显式传参 > 环境变量 > 下方常量。

# 单次生成最大 token 数（环境变量 OPENROUTER_MAX_TOKENS）
DEFAULT_MAX_TOKENS: int = 16384
# 生成温度（环境变量 OPENROUTER_TEMPERATURE）
DEFAULT_TEMPERATURE: float = 0.1
# 推理力度：low / medium / high / adaptive / none（None = provider 默认）
DEFAULT_REASONING_EFFORT: str | None = None

# 上下文窗口大小（token 数）
DEFAULT_CONTEXT_WINDOW_TOKENS: int = 32768  # 8192太小导致频繁触发consolidation

# 工具返回结果最大字符数（存入 session 时截断）
# 默认 16000 太大，一次对话多个工具调用容易累积到 10000+ tokens
DEFAULT_MAX_TOOL_RESULT_CHARS: int = 4000  # 约 1000 tokens
# 上下文块数上限（None = 不限制）
DEFAULT_CONTEXT_BLOCK_LIMIT: int | None = None

# 最大工具调用迭代次数（环境变量 AGENT_MAX_STEPS）
DEFAULT_MAX_TOOL_ITERATIONS: int = 3
# 最大并发子 agent 数
DEFAULT_MAX_CONCURRENT_SUBAGENTS: int = 2

# 时区
DEFAULT_TIMEZONE: str = "Asia/Shanghai"


# ═══════════════════════════════════════════════════════════════
# Dream 记忆固化
# ═══════════════════════════════════════════════════════════════

DEFAULT_DREAM_ENABLED: bool = True
DEFAULT_DREAM_INTERVAL_H: int = 2

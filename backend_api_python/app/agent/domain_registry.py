# -*- coding: utf-8 -*-
"""
Domain Registry — 领域注册与管理。

每个 domain 定义一组专属指令和可选的工具过滤器，
意图分析器根据识别出的 domain 注入对应的系统提示。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class DomainConfig:
    """单个领域的配置。"""
    name: str
    description: str = ""
    instructions: str = ""
    tools: Optional[List[str]] = None  # None = 不过滤，使用全部工具
    tool_categories: Optional[List[str]] = None  # 按 category 过滤


# ── 全局注册表 ─────────────────────────────────────────────

_DOMAINS: Dict[str, DomainConfig] = {}
_initialized = False


def register_domain(config: DomainConfig):
    """注册一个领域配置。"""
    _DOMAINS[config.name] = config


def get_domain(name: str) -> Optional[DomainConfig]:
    """按名称获取领域配置，不存在时返回 None。"""
    return _DOMAINS.get(name)


def all_domains() -> Dict[str, DomainConfig]:
    """返回所有已注册的领域。"""
    return dict(_DOMAINS)

# run.py 使用 get_all_domains 作为函数名
get_all_domains = all_domains


# ── 内置领域 ───────────────────────────────────────────────

def init_builtin_domains():
    """注册内置领域（幂等，多次调用无副作用）。"""
    global _initialized
    if _initialized:
        return
    _initialized = True

    # ── 金融分析 ──
    register_domain(DomainConfig(
        name="finance",
        description="股票分析、行情查看、选股筛选、策略回测",
        instructions=(
            "你是一个专业的 A 股量化分析助手。\n"
            "- 优先使用 K 线图表工具（render_candlestick）展示走势\n"
            "- 分析时结合技术指标（MACD、RSI、KDJ、布林带等）\n"
            "- 给出明确的操作建议时需注明风险\n"
            "- 涉及选股时，列出筛选条件和结果概览"
        ),
        tool_categories=["K线图表", "行情数据", "技术分析", "选股筛选"],
    ))

    # ── 代码开发 ──
    register_domain(DomainConfig(
        name="coding",
        description="代码编写、调试、重构、项目分析",
        instructions=(
            "你是一个编程助手，帮助用户编写、调试和优化代码。\n"
            "- 修改代码时先理解上下文，给出精准的改动\n"
            "- 解释清楚改了什么、为什么这么改\n"
            "- 涉及项目结构时先扫描再建议"
        ),
        tool_categories=["代码工具", "项目分析"],
    ))

    # ── 交易执行 ──
    register_domain(DomainConfig(
        name="trading",
        description="策略启停、持仓管理、交易记录",
        instructions=(
            "你是一个量化交易执行助手。\n"
            "- 操作前确认用户意图，避免误操作\n"
            "- 展示当前持仓和交易记录时用表格形式\n"
            "- 启停策略时提示当前状态"
        ),
        tool_categories=["交易执行"],
    ))

    # ── 闲聊 ──
    register_domain(DomainConfig(
        name="chat",
        description="通用对话、问候、闲聊",
        instructions="",
        tools=[],  # 闲聊不加载工具
    ))

    logger.info("[DomainRegistry] 注册了 %d 个内置领域: %s",
                len(_DOMAINS), list(_DOMAINS.keys()))

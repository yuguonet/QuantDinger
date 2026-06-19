# -*- coding: utf-8 -*-
"""
Local ToolRegistry for agent.

Auto-discovers tool functions from agent/tools/ and wraps them as
smolagents Tool objects via the smolagents `tool` decorator.

Usage:
    from app.agent.tools import registry
    registry.discover()
    tools = build_smolagent_tools({"deny": [...], "domain": ...})
    spec = registry.get("search_stock_by_name")
    spec.fn(stock_code="600066")
"""
from __future__ import annotations

import importlib
import inspect
import logging
import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from smolagents import tool as smolagents_tool

logger = logging.getLogger(__name__)

# ── Tool category / layer metadata ──────────────────────────────
# Maps tool name → (layer, category)
_TOOL_META: Dict[str, tuple[str, str]] = {
    # Data tools
    "search_stock_by_name": ("数据层", "股票检索"),
    "get_realtime_quote": ("数据层", "实时行情"),
    "agent_get_kline": ("数据层", "K线数据"),
    "get_stock_info": ("数据层", "股票信息"),
    # Analysis tools
    "analyze_trend": ("分析层", "趋势分析"),
    "calculate_ma": ("分析层", "均线分析"),
    "get_volume_analysis": ("分析层", "成交量分析"),
    "analyze_pattern": ("分析层", "形态识别"),
    "get_chip_distribution": ("分析层", "筹码分析"),
    "get_indicator_snapshot": ("分析层", "指标快照"),
    # Market data tools
    "get_dragon_tiger": ("数据层", "龙虎榜"),
    "get_hot_rank": ("数据层", "热门排名"),
    "get_limit_pool": ("数据层", "涨跌停池"),
    "get_market_overview": ("数据层", "市场概览"),
    "get_fund_flow": ("数据层", "资金流向"),
    "get_sector_fund_flow": ("数据层", "板块资金流"),
    "get_concept_fund_flow": ("数据层", "概念资金流"),
    "get_fund_flow_120d": ("数据层", "中期资金流"),
    "get_fund_flow_minute": ("数据层", "分钟资金流"),
    # Capital tools
    "get_capital_summary": ("分析层", "资本面"),
    # Quote tools
    "get_order_book": ("数据层", "盘口数据"),
    "get_index_etf_quote": ("数据层", "指数ETF"),
    "batch_valuation_compare": ("分析层", "估值对比"),
    # Market tools
    "get_market_indices": ("数据层", "市场指数"),
    "get_sector_rankings": ("数据层", "板块排名"),
    # News / Intel tools
    "search_stock_intel": ("情报层", "个股情报"),
    "search_sector_intel": ("情报层", "板块情报"),
    "search_policy_intel": ("情报层", "政策情报"),
    "search_comprehensive_intel": ("情报层", "综合情报"),
    # Research tools
    "get_consensus_eps": ("分析层", "盈利预测"),
    "get_eastmoney_stock_news": ("情报层", "东财新闻"),
    "get_global_finance_news": ("情报层", "全球财经"),
    # Signal tools
    "get_hot_stocks_with_reasons": ("信号层", "热点题材"),
    "get_northbound_flow": ("信号层", "北向资金"),
    "get_stock_concept_blocks": ("信号层", "概念板块"),
    "get_lockup_expiry": ("信号层", "限售解禁"),
    "get_industry_ranking": ("信号层", "行业排名"),
    "get_dragon_tiger_detail": ("信号层", "龙虎榜详情"),
    # Screener tools
    "search_stocks": ("筛选层", "综合选股"),
    "list_user_selection_strategies": ("筛选层", "选股策略"),
    # Filter utils (exposed as tools for agent)
    "build_keyword_from_filters": ("筛选层", "筛选项"),
    "get_screener_presets": ("筛选层", "筛选预设"),
    # Indicator tools
    "list_indicators": ("分析层", "指标管理"),
    "get_indicator_params": ("分析层", "指标参数"),
    # Sector analysis tools
    "get_hot_sectors": ("分析层", "板块分析"),
    "get_sector_trend_analysis": ("分析层", "板块趋势"),
    "get_sector_history_data": ("分析层", "板块历史"),
    "get_sector_prediction": ("分析层", "板块预测"),
    "get_sector_cycle": ("分析层", "板块周期"),
    "get_stock_sector_info": ("分析层", "个股板块"),
    "get_sector_stocks": ("分析层", "板块成分股"),

    # Trading tools
    "list_strategies": ("交易层", "策略管理"),
    "get_strategy_detail": ("交易层", "策略管理"),
    "start_strategy": ("交易层", "策略执行"),
    "stop_strategy": ("交易层", "策略执行"),
    # Pagination tools
    "get_page": ("系统层", "分页"),
    "get_cache_summary": ("系统层", "缓存管理"),
    "get_text_page": ("系统层", "分页"),
}

_SKIP_MODULES: Set[str] = {
    "__init__", "__pycache__", "pagination", "screener_config",
}


class _ToolSpec:
    """薄的 spec 包装，提供 .fn 属性供 skill 脚本调用。"""

    def __init__(self, fn: Callable, name: str, description: str, category: str, layer: str):
        self.fn = fn
        self.name = name
        self.description = description
        self.category = category
        self.layer = layer


class ToolRegistry:
    """本地工具注册表 — 扫描 agent/tools/ 并包装为 smolagents Tool 对象。"""

    def __init__(self):
        self._tools: Dict[str, _ToolSpec] = {}
        self._smolagent_tools: Dict[str, Any] = {}  # name → smolagents Tool instance
        self._discovered = False
        self._tools_dir = Path(__file__).parent.resolve()

    def discover(self):
        """扫描 tools 目录，发现所有公开函数并注册。"""
        if self._discovered:
            return
        self._discovered = True

        for py_file in sorted(self._tools_dir.glob("*.py")):
            module_name = py_file.stem
            if module_name.startswith("_") or module_name in _SKIP_MODULES:
                continue

            try:
                mod = importlib.import_module(f"app.agent.tools.{module_name}")
            except Exception:
                logger.debug("[ToolRegistry] 跳过模块 %s: %s", module_name, exc_info=True)
                continue

            for attr_name in dir(mod):
                if attr_name.startswith("_"):
                    continue
                obj = getattr(mod, attr_name)
                if not callable(obj):
                    continue
                # 必须是普通函数（非 class）
                if not inspect.isfunction(obj):
                    continue
                doc = inspect.getdoc(obj)
                if not doc:
                    continue

                layer, category = _TOOL_META.get(attr_name, ("其他", "未分类"))
                spec = _ToolSpec(
                    fn=obj,
                    name=attr_name,
                    description=doc.split("\n")[0][:500],
                    category=category,
                    layer=layer,
                )
                self._tools[attr_name] = spec

    def _wrap_as_smolagent(self, name: str) -> Any:
        """将指定工具包装为 smolagents Tool（惰性）。"""
        if name in self._smolagent_tools:
            return self._smolagent_tools[name]
        spec = self._tools.get(name)
        if spec is None:
            raise KeyError(f"工具 '{name}' 未注册")
        # smolagents.tool() 要求函数有完整 type hints
        tool_obj = smolagents_tool(spec.fn)
        self._smolagent_tools[name] = tool_obj
        return tool_obj

    def get(self, name: str) -> Optional[_ToolSpec]:
        """获取已注册的工具规格。

        Args:
            name: 工具名称
        """
        """获取工具 spec（含 .fn 属性）。"""
        return self._tools.get(name)

    @property
    def categories(self) -> Dict[str, List[str]]:
        """按分类返回工具名列表。"""
        result: Dict[str, List[str]] = {}
        for name, spec in self._tools.items():
            result.setdefault(spec.category, []).append(name)
        for cat in result:
            result[cat].sort()
        return result

    @property
    def layered_categories(self) -> Dict[str, Dict[str, List[str]]]:
        """按层 → 分类返回工具名列表。"""
        result: Dict[str, Dict[str, List[str]]] = {}
        for name, spec in self._tools.items():
            if spec.layer not in result:
                result[spec.layer] = {}
            result[spec.layer].setdefault(spec.category, []).append(name)
        for layer in result:
            for cat in result[layer]:
                result[layer][cat].sort()
        return result

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools


def build_smolagent_tools(config: Optional[Dict[str, Any]] = None) -> List[Any]:
    """构建 smolagent 兼容工具列表。

    Args:
        config: 可选配置字典，支持:
            - deny: List[str] — 排除的工具名列表
            - domain: str — 领域过滤（当前未实现全部过滤）

    Returns:
        可用于 smolagents CodeAgent/ToolCallingAgent 的工具列表
    """
    registry = ToolRegistry()
    registry.discover()
    config = config or {}
    deny = set(config.get("deny", []) or [])
    domain = config.get("domain", "")

    tools = []
    for name in sorted(registry._tools.keys()):
        if name in deny or name == "final_answer":
            continue
        tool_obj = registry._wrap_as_smolagent(name)
        tools.append(tool_obj)
    return tools


# ── 模块级单例 ──────────────────────────────────────────────────
_registry: Optional[ToolRegistry] = None


def get_local_registry() -> ToolRegistry:
    """获取（或创建）全局 ToolRegistry 单例。"""
    global _registry
    if _registry is None:
        _registry = ToolRegistry()
    return _registry

# -*- coding: utf-8 -*-
"""
ChainExecutionTool — 主 Agent 内部工具，包装 ChainExecutor。

主 Agent 通过调用此工具执行预定义的 Chain 流程（固定流程，省 token）。
Chain 内部会自动调用多个 Skill，返回 DecisionResult JSON。

被调用方：
  agent.py → _build_managed_agents() → Chain 子 Agent 持有此工具

使用示例（主 Agent 调用 Chain 子 Agent）：
  execute_chain(chain_id="evaluate+stock", stock_code="600519", stock_name="贵州茅台")
  → 返回 DecisionResult JSON（action/score/direction + EvalNode 树）
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from smolagents import Tool

logger = logging.getLogger(__name__)


class ChainExecutionTool(Tool):
    """主 Agent 内部工具：执行 Chain 流程。

    构造时绑定 model 和 collector，forward() 调用 ChainExecutor.execute()。
    """

    name = "execute_chain"
    description = (
        "执行预定义的分析链路，传入链路ID和股票代码，返回结构化决策报告。"
        "链路会自动调用多个技能进行全面分析，不需要手动调用其他技能。"
    )
    inputs = {
        "chain_id": {
            "type": "string",
            "description": "链路ID，如 evaluate+stock（完整分析）、screen+stock（选股）、scan+market（大盘扫描）",
        },
        "stock_code": {
            "type": "string",
            "description": "股票代码，如 600519、000858（scan+market 可为空）",
            "nullable": True,
        },
        "stock_name": {
            "type": "string",
            "description": "股票名称（可选），如 贵州茅台",
            "nullable": True,
        },
    }
    output_type = "string"

    def __init__(self, model, collector=None, user_id: int = 1):
        """
        Args:
            model: smolagents Model 实例（用于 call_llm）
            collector: TraceCollector 实例（可选）
            user_id: 用户 ID
        """
        super().__init__()
        self._model = model
        self._collector = collector
        self._user_id = user_id

    # 工具集缓存（避免每次 forward() 重建 96 个工具）
    _tool_cache = None
    _tool_cache_time = 0
    _TOOL_CACHE_TTL = 300  # 5 分钟刷新一次

    def forward(self, chain_id: str, stock_code: str = None, stock_name: str = None) -> str:
        """执行 Chain 流程，返回 DecisionResult JSON。"""
        import time as _time
        from app.agent.chain.chains import get_chain
        from app.agent.chain.executor import ChainExecutor
        from app.agent.skills.registry import skill_registry
        from app.agent.tool_adapter import build_all_tools

        # 防御：子 agent 可能传入字符串 "None" 而非 Python None
        if stock_code in (None, "None", "null", "undefined"):
            stock_code = ""
        if stock_name in (None, "None", "null", "undefined"):
            stock_name = ""

        # 验证 chain_id
        chain_def = get_chain(chain_id)
        if not chain_def:
            return json.dumps({
                "success": False,
                "error": f"未知链路: {chain_id}，可用链路: evaluate+stock, screen+stock, scan+market",
            }, ensure_ascii=False)

        # 发现技能
        skill_registry.discover()

        # 构建工具集（带缓存）
        now = _time.time()
        if ChainExecutionTool._tool_cache is None or (now - ChainExecutionTool._tool_cache_time) > ChainExecutionTool._TOOL_CACHE_TTL:
            ChainExecutionTool._tool_cache = build_all_tools()
            ChainExecutionTool._tool_cache_time = now
        tool_map = {t.name: t for t in ChainExecutionTool._tool_cache}

        # LLM 调用函数
        def call_llm(prompt: str) -> str:
            messages = [{"role": "user", "content": prompt}]
            response = self._model(messages)
            return response.content if hasattr(response, "content") else str(response)

        # 工具调用函数
        def call_tool_fn(tool_name: str, **kwargs) -> Any:
            t = tool_map.get(tool_name)
            if not t:
                raise ValueError(f"Unknown tool: {tool_name}")
            return t(**kwargs)

        # run_skill_fn：调用指定的 BaseSkill
        def run_skill_fn(skill_name: str, scode: str, sname: str, ctx: dict) -> tuple:
            sk = skill_registry.get(skill_name)
            if not sk:
                raise ValueError(f"Unknown skill: {skill_name}")
            return sk.run(
                stock_code=scode,
                stock_name=sname or "",
                context=ctx,
                call_llm=call_llm,
                call_tool_fn=call_tool_fn,
            )

        # 执行 Chain
        try:
            executor = ChainExecutor(
                chain_id=chain_id,
                stock_code=stock_code or "",
                stock_name=stock_name or "",
                user_id=self._user_id,
            )
            chain_result = executor.execute(
                run_skill_fn=run_skill_fn,
                context={"user_query": f"分析 {stock_code or '市场'}"},
                call_llm=call_llm,
            )
        except Exception as e:
            logger.error("[ChainExecution] Chain %s 执行异常: %s", chain_id, e)
            return json.dumps({
                "success": False,
                "chain_id": chain_id,
                "stock_code": stock_code,
                "error": str(e),
            }, ensure_ascii=False)

        # 通知 TraceCollector
        if self._collector and chain_result.root_node:
            try:
                self._collector.on_chain_complete(chain_result.root_node)
            except Exception as e:
                logger.warning("[ChainExecution] TraceCollector 通知失败: %s", e)

        # 返回 DecisionResult JSON
        result_dict = chain_result.to_dict()
        result_dict["success"] = chain_result.success

        # 持久化 EvalNode 树
        if chain_result.root_node:
            try:
                from app.agent.chain import store as chain_store
                from datetime import date
                chain_result.root_node.exec_date = date.today()
                root_id = chain_store.save_tree(chain_result.root_node)
                if root_id:
                    result_dict["execution_id"] = root_id
                    logger.info("[ChainExecution] 写库成功 root_id=%d chain=%s stock=%s",
                                root_id, chain_id, stock_code)
            except Exception as e:
                logger.warning("[ChainExecution] 写库失败（不影响返回）: %s", e)

        return json.dumps(result_dict, ensure_ascii=False, indent=2)

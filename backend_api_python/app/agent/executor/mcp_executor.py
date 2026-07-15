# -*- coding: utf-8 -*-
"""
MCPExecutor — Router 模式，一个工具调所有 MCP 工具。

设计目标：
  - CodeAgent 传 tools=[mcp_router]，只有 1 个工具描述 ~100 tokens
  - 5000 个 MCP 工具 vs 50 个：prompt token 完全一样
  - LLM 运行时通过 mcp(action="list") 发现工具，mcp(action="call") 调用

流程：
  LLM 生成代码:
    tools = mcp(action="list", category="stock_data")
    result = mcp(action="call", tool_name="get_kline", args={"stock_code": "600519"})

  执行时:
    mcp → MCPRouterTool.forward() → MCP bridge → tools/call
"""
from __future__ import annotations

import json
import logging
from typing import Any

from smolagents import Tool as SmolToolBase
from smolagents.local_python_executor import LocalPythonExecutor
import smolagents.local_python_executor as _lpe

logger = logging.getLogger(__name__)

# smolagents 默认30s，5个重型工具串行易超时，改为60s
_lpe.MAX_EXECUTION_TIME_SECONDS = 60


# ═══════════════════════════════════════════════════════════════
#  MCPRouterTool — 一个工具路由到所有 MCP 工具
# ═══════════════════════════════════════════════════════════════

class MCPRouterTool(SmolToolBase):
    """单个 router 工具，通过 action 分发到任意 MCP 工具。

    action="list":  发现可用工具（支持 category 过滤）
    action="call":  调用指定工具

    LLM 在 system prompt 中获知此工具的用法。
    5000 个 MCP 工具 → 只占 1 个工具的 prompt token。
    """
    skip_forward_signature_validation = True

    name = "mcp"
    description = (
        "调用 MCP 后端工具。两种模式：\n"
        "  action='list': 发现可用工具，可选 category 过滤\n"
        "  action='call': 调用指定工具，需 tool_name 和 args"
    )
    output_type = "object"
    inputs = {
        "action": {
            "type": "string",
            "description": "'list' 发现工具 或 'call' 调用工具",
        },
        "tool_name": {
            "type": "string",
            "description": "工具名（action='call' 时必填）",
            "nullable": True,
        },
        "args": {
            "type": "object",
            "description": "工具参数字典（action='call' 时使用）",
            "nullable": True,
        },
        "category": {
            "type": "string",
            "description": "工具类别过滤（action='list' 时可选）",
            "nullable": True,
        },
    }

    def __init__(self, tool_map: dict, tool_catalog: list[dict] | None = None):
        """初始化 router。

        Args:
            tool_map: name → Tool 对象映射（MCP 工具 + 技能工具统一注册）
            tool_catalog: 预加载的工具元数据列表 [{name, description, category, inputs}, ...]
        """
        super().__init__()
        self._tool_map = tool_map
        self._catalog = tool_catalog or []
        self._failed_calls: list[tuple[str, str]] = []  # [(tool_name, description)]

    def get_failed_calls(self) -> list[tuple[str, str]]:
        """返回本次执行中失败的工具调用列表。"""
        return list(self._failed_calls)

    def forward(
        self,
        action: str,
        tool_name: str = "",
        args: dict | None = None,
        category: str = "",
        **kwargs,
    ) -> Any:
        # ── list: 发现工具 ──
        if action == "list":
            return self._list_tools(category)

        # ── call: 调用工具 ──
        if action == "call":
            if not tool_name:
                return {"error": "action='call' 需要 tool_name 参数"}
            return self._call_tool(tool_name, args or {})

        return {"error": f"未知 action: {action}，支持 'list' 或 'call'"}

    def _list_tools(self, category: str = "") -> list[dict]:
        """返回工具列表（含完整参数 schema，LLM 需要知道精确参数名）。"""
        result = []
        for t in self._catalog:
            if category and category.lower() not in (t.get("category", "") + t.get("name", "")).lower():
                continue
            result.append({
                "name": t["name"],
                "description": (t.get("description", "") or "")[:120],
                "inputs": t.get("inputs") or {},
            })
        return result

    def _has_error(self, obj, depth: int = 3) -> bool:
        """递归检查 dict/list 中是否包含 error 字段（避免深层遍历过深）。"""
        if depth <= 0:
            return False
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k.lower() == 'error':
                    return True
                if self._has_error(v, depth - 1):
                    return True
        elif isinstance(obj, (list, tuple)):
            for item in obj:
                if self._has_error(item, depth - 1):
                    return True
        return False

    def _call_tool(self, tool_name: str, args: dict) -> Any:
        """路由到 tool_map 中的 Tool.forward()。失败时返回正确参数名，让 LLM 自我纠正。"""
        try:
            tool = self._tool_map.get(tool_name)
            if tool is None:
                self._record_failure(tool_name, "")
                return {"error": f"Unknown tool: {tool_name}"}
            result = tool.forward(**args)
            if isinstance(result, str):
                try:
                    result = json.loads(result)
                except (json.JSONDecodeError, TypeError):
                    return result
            # 成功时附加可用字段列表，帮助 LLM 正确访问数据
            if isinstance(result, dict):
                # 递归检查是否包含 error
                if self._has_error(result):
                    desc = getattr(tool, 'description', '') or ''
                    self._record_failure(tool_name, desc)
                    result['_failed_tool'] = tool_name  # 标记失败工具名
                public_fields = [k for k in result.keys() if not k.startswith('_')]
                if public_fields:
                    result["_fields"] = public_fields
            return result
        except Exception as e:
            # 记录失败
            tool = self._tool_map.get(tool_name)
            desc = getattr(tool, 'description', '') or '' if tool else ''
            self._record_failure(tool_name, desc)
            # pydantic 验证错误 → 返回正确参数名，让 LLM 纠正
            err_msg = str(e)
            if "validation error" in err_msg.lower() or "Field required" in err_msg:
                if tool:
                    inputs = getattr(tool, "inputs", {}) or {}
                    correct_params = list(inputs.keys())
                    example_args = ", ".join(f"'{k}': '300599'" for k in correct_params)
                    return {
                        "error": f"参数名错误。你用了 {list(args.keys())}，但 {tool_name} 需要 {correct_params}",
                        "correct_code": f"{tool_name}({example_args})",
                        "hint": f"复制上面的 correct_code 重试，注意参数名是 {correct_params} 不是 {list(args.keys())}",
                    }
            return {"error": f"Tool '{tool_name}' failed: {e}"}

    def _record_failure(self, tool_name: str, description: str):
        """记录失败的工具调用（去重）。"""
        for name, _ in self._failed_calls:
            if name == tool_name:
                return
        self._failed_calls.append((tool_name, description))


# ═══════════════════════════════════════════════════════════════
#  MCPExecutor — 注入 router 到 static_tools
# ═══════════════════════════════════════════════════════════════

class MCPExecutor(LocalPythonExecutor):
    """自定义 PythonExecutor，将 mcp router 注入执行命名空间。

    继承 LocalPythonExecutor，override send_tools()：
      static_tools = {**BASE_PYTHON_TOOLS, "mcp": router_tool, ...}

    full_tool_map 在 send_tools 时注入 router，使 router 可路由到全量 MCP 工具。

    ══ 为什么用 router 模式，不把 MCP 工具注册为 smolagents Tool？ ══

    smolagents 的 tools=[Tool1, Tool2, ...] 会把所有工具描述写入 system prompt。
    70+ 个 MCP 工具 × ~100 token/工具 ≈ 7000 token，对小模型是沉重负担。

    router 模式只注册 mcp 一个工具（~100 token），LLM 通过：
      mcp(action="list")                    → 动态发现所有工具
      mcp(action="call", tool_name="...", args={...})  → 调用
    token 成本从 7000 降到 100，且工具数量增长不影响 prompt 长度。

    所以：
    - SmolCodeAgent(tools=[], ...) 是故意的，不要往里塞 MCP 工具
    - MCP 工具全部走 mcp router，不走 smolagents 原生 Tool
    - system prompt 里教 LLM 用 mcp(action="list/call") 即可
    """

    def __init__(self, router_tool: MCPRouterTool, full_tool_map: dict | None = None, **kwargs):
        super().__init__(**kwargs)
        self._router = router_tool
        self._full_tool_map = full_tool_map or {}

    def send_tools(self, tools: dict):
        """Override: 注入全量工具到 router，再注入 router 到 static_tools。"""
        # 先把全量工具注入 router 的 tool_map（运行时可调任何工具）
        self._router._tool_map.update(self._full_tool_map)
        super().send_tools(tools)
        self.static_tools["mcp"] = self._router.forward
        logger.debug("[MCPExecutor] mcp router 已注入 static_tools，可调用 %d 个工具", len(self._router._tool_map))


# ═══════════════════════════════════════════════════════════════
#  辅助：从 MCP 工具列表构建 catalog 缓存
# ═══════════════════════════════════════════════════════════════

def build_tool_catalog(mcp_tools: list) -> list[dict]:
    """从 MCP Tool 对象列表提取轻量 catalog（供 router 的 list 操作使用）。"""
    catalog = []
    for tool in mcp_tools:
        catalog.append({
            "name": getattr(tool, "name", "unknown"),
            "description": (getattr(tool, "description", "") or "")[:100],
            "category": getattr(tool, "category", ""),
            "inputs": getattr(tool, "inputs", {}) or {},
        })
    return catalog

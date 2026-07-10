# -*- coding: utf-8 -*-
"""
MCP Bridge — 零侵入工具桥接层

自动扫描 tools/*.py 的公开函数，暴露为 MCP 工具。
现有工具代码完全不用改，加这一个文件就行。

用法:
    # 启动 MCP server
    python tools/mcp_bridge.py

    # 或指定端口（SSE 模式）
    python tools/mcp_bridge.py --transport sse --port 8765

    # Agent 端连接
    from smolagents import CodeAgent, ToolCollection
    from mcp import StdioServerParameters

    server = StdioServerParameters(command="python", args=["tools/mcp_bridge.py"])
    with ToolCollection.from_mcp(server) as tools:
        agent = CodeAgent(tools=list(tools), model=model)
        result = agent.run("分析茅台技术面")
"""
from __future__ import annotations

import importlib
import inspect
import logging
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# MCP 子进程需要加载 .env，否则拿不到数据库密码等配置
# mcp_bridge.py 位于 app/agent/tools/，.env 位于 backend_api_python/
try:
    from dotenv import load_dotenv
    _env_path = Path(__file__).resolve().parent.parent.parent.parent / ".env"  # backend_api_python/.env
    load_dotenv(_env_path, override=False)
except ImportError:
    pass

logger = logging.getLogger(__name__)

# ── 配置 ──────────────────────────────────────────────────────
# 跳过的文件（框架文件，非工具）
_SKIP_FILES = {"__init__", "base", "registry", "em_utils", "pagination",
               "screener_config", "mcp_bridge", "cache_tools"}

# tools 目录路径
_TOOLS_DIR = Path(__file__).resolve().parent

# MCP server 名称
_SERVER_NAME = "quantdinger-tools"

# ── MCP 实例 ──────────────────────────────────────────────────
mcp = FastMCP(_SERVER_NAME)


# ── 工具目录（用于层级发现）────────────────────────────────
# 结构: {"category_name": {"description": str, "tools": {name: description}}}
_tool_catalog: dict = {}


def _discover_and_register() -> int:
    """
    扫描 tools/*.py，将所有有 docstring 的公开函数注册为 MCP 工具。
    逻辑与 ToolRegistry / QDToolAdapter 一致，确保工具集对齐。
    同时构建 _tool_catalog 用于层级发现。

    Returns:
        注册的工具数量
    """
    global _tool_catalog
    count = 0

    # MCP bridge 作为子进程启动时，sys.path 与 Flask 不同。
    # Flask 启动时 run.py 和 app/__init__.py 会把 app/ 加入 sys.path，
    # 所以 tools.xxx、llm.xxx、memory.xxx 等绝对导入能工作。
    # MCP 子进程需要手动设置相同的路径，否则所有工具模块导入失败（只注册 3 个发现工具）。
    agent_dir = str(_TOOLS_DIR.parent)  # app/agent/
    if agent_dir not in sys.path:
        sys.path.insert(0, agent_dir)
    backend_dir = str(_TOOLS_DIR.parent.parent.parent)  # backend_api_python/
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)

    for py_file in sorted(_TOOLS_DIR.glob("*.py")):
        module_name = py_file.stem
        if module_name.startswith("_") or module_name in _SKIP_FILES:
            continue

        try:
            # 用 tools.xxx 导入（与 Flask 启动时 sys.path 一致）
            mod = importlib.import_module(f"tools.{module_name}")
        except Exception as e:
            logger.debug("[MCP Bridge] 跳过模块 %s: %s", module_name, e)
            continue

        category_tools = {}
        for attr_name in sorted(dir(mod)):
            if attr_name.startswith("_"):
                continue
            obj = getattr(mod, attr_name)
            if not callable(obj) or not inspect.isfunction(obj):
                continue
            # 只要当前模块定义的函数
            if getattr(obj, "__module__", "") != mod.__name__:
                continue
            # 必须有 docstring（ToolRegistry 也要求）
            doc = inspect.getdoc(obj)
            if not doc:
                continue

            try:
                mcp.tool()(obj)
                count += 1
                # 记录到目录（取 docstring 第一行作简短描述）
                short_desc = doc.strip().split("\n")[0][:100]
                category_tools[attr_name] = short_desc
            except Exception as e:
                logger.warning("[MCP Bridge] 注册 %s 失败: %s", attr_name, e)

        if category_tools:
            # 模块 docstring 第一行作分类描述
            mod_doc = (inspect.getdoc(mod) or module_name).strip().split("\n")[0][:80]
            _tool_catalog[module_name] = {
                "description": mod_doc,
                "tools": category_tools,
            }

    return count


# ── 发现工具（MCP 内置工具，Agent 按需查询）──────────────────

@mcp.tool()
def list_categories() -> str:
    """
    列出所有工具分类目录。返回每个分类的名称和一句话描述。
    用于 Agent 快速了解有哪些工具可用，不加载具体工具描述。
    """
    if not _tool_catalog:
        return "暂无工具分类"

    lines = []
    for cat_name, info in sorted(_tool_catalog.items()):
        tool_count = len(info["tools"])
        lines.append(f"- {cat_name}: {info['description']} ({tool_count}个工具)")
    return "\n".join(lines)


@mcp.tool()
def list_tools(category: str) -> str:
    """
    列出指定分类下的所有工具名和简短描述。
    用于 Agent 确定要用哪些工具后，查看具体工具说明。

    Args:
        category: 分类名称（如 'data_tools', 'analysis_tools'），从 list_categories 获取
    """
    cat = _tool_catalog.get(category)
    if not cat:
        available = ", ".join(sorted(_tool_catalog.keys()))
        return f"分类 '{category}' 不存在。可用分类: {available}"

    lines = [f"【{category}】{cat['description']}"]
    for name, desc in sorted(cat["tools"].items()):
        lines.append(f"  - {name}: {desc}")
    return "\n".join(lines)


@mcp.tool()
def search_tools(query: str) -> str:
    """
    按关键词搜索工具。返回匹配的工具名、描述和所属分类。
    用于 Agent 不确定工具在哪個分类时，用关键词快速定位。

    Args:
        query: 搜索关键词（如 '资金流', 'K线', '板块'）
    """
    query_lower = query.lower()
    matches = []

    for cat_name, info in _tool_catalog.items():
        for tool_name, tool_desc in info["tools"].items():
            if (query_lower in tool_name.lower() or
                query_lower in tool_desc.lower()):
                matches.append(f"  - {tool_name} ({cat_name}): {tool_desc}")

    if not matches:
        return f"未找到匹配 '{query}' 的工具"

    return f"找到 {len(matches)} 个匹配工具:\n" + "\n".join(matches[:20])


def main():
    """启动 MCP server。"""
    import argparse

    parser = argparse.ArgumentParser(description="QuantDinger MCP Tool Server")
    parser.add_argument("--transport", default="stdio",
                        choices=["stdio", "sse"],
                        help="传输方式 (默认 stdio)")
    parser.add_argument("--port", type=int, default=8765,
                        help="SSE 模式端口 (默认 8765)")
    args = parser.parse_args()

    # 注册工具
    count = _discover_and_register()
    print(f"[MCP Bridge] 已注册 {count} 个工具，启动 {args.transport} 模式...")

    # 启动
    if args.transport == "sse":
        mcp.run(transport="sse", port=args.port)
    else:
        mcp.run()


if __name__ == "__main__":
    main()

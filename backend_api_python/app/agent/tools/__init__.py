# -*- coding: utf-8 -*-
"""
Agent tools subpackage.

Tool implementations live here as dict-based specs.
Conversion to smolagents Tool objects is handled by app.agent.tool_adapter.
"""
# 注册翻页工具
try:
    from app.agent.tools.pagination import register_page_tool
    register_page_tool()
except Exception as _e:
    import logging
    logging.getLogger(__name__).warning("[Tools] 注册翻页工具失败: %s", _e)

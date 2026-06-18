# -*- coding: utf-8 -*-
"""
agent tools — discovered automatically by local ToolRegistry.
"""
from app.agent.tools.registry import ToolRegistry, build_smolagent_tools

registry = ToolRegistry()

__all__ = ["ToolRegistry", "build_smolagent_tools", "registry"]

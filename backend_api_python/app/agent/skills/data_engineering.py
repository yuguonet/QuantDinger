# -*- coding: utf-8 -*-
"""
Data engineering skill — 代码执行和数据处理专家。

负责：代码执行、数据清洗、自定义分析脚本、批量数据处理。
"""
from app.agent.skills.registry import skill


@skill(
    name="data_agent",
    description="数据工程专家。负责代码执行、数据清洗、自定义分析脚本、批量数据处理。当用户要求写代码、跑脚本、处理数据时调用。",
    instructions="你是数据工程专家。用工作区工具保存和执行脚本，支持迭代优化。长时间任务用 run_background 后台执行。",
    tools=[
        "save_script", "load_script", "list_workspace",
        "shell_exec", "exec_script", "run_background", "poll_task",
        "agent_get_kline", "get_realtime_quote",
    ],
    priority=4,
)
class DataEngineeringSkill:
    """数据工程专家子 Agent。"""
    pass

# -*- coding: utf-8 -*-
"""capabilities/__init__.py — agent 能力发现层（A 阶段, 2026-09-12）

通用机制（不局限金融域）: 显式指定包 -> 扫描公开函数 -> 分类 -> 人工准入 ->
注册为 agent 工具（来源层标记 CAPABILITY_DOMAIN；只由阶段 tools 白名单点名注入，
不占用 planner 的可选工具域）。auto/ 为第一个接入包。

模块分工:
  scanner.py — 能力扫描器（产出报告到 tmp/，绝不写准入配置）
  loader.py  — 准入清单加载 / 护栏包装 / 注册进 ToolProvider

约定:
  - admission.json 是唯一准入事实源（人工过目后固化；修改后随进程重启生效）;
  - 三层闸门之二在本层: 写操作前缀硬复核 + 超时护栏 + 结果体积护栏
    （大结果写 tmp/capability_output/，返回预览+文件路径）;
  - 执行审计走既有 executor/trace 链路，本层不重复。
"""
from .scanner import scan, render_markdown  # noqa: F401
from .loader import register_capabilities, load_admitted  # noqa: F401

__all__ = ["scan", "render_markdown", "register_capabilities", "load_admitted"]

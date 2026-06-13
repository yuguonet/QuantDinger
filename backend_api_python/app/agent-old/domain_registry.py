# -*- coding: utf-8 -*-
"""
Domain Registry — 领域注册与管理。

每个 domain 定义一组专属指令和可选的工具过滤器。
意图分析器（intent_analyzer）根据识别出的 domain 注入对应的系统提示。

内置领域：
  finance  — 金融分析（A股中短线特化）
  trading  — 交易执行（策略启停/持仓管理）
  coding   — 代码开发（项目扫描/文件操作）
  chat     — 闲聊/问候（跳过 agent 直接回复）

被调用方：
  intent_analyzer.py → init_builtin_domains() + get_domain()
  agent.py → _build_instructions() → domain_instructions 注入

公开接口：
  init_builtin_domains() → None
  get_domain(name) → Optional[DomainConfig]
  get_all_domains() → Dict[str, DomainConfig]
  register_domain(config) → None
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
            "\n"
            "## 工作流程\n"
            "1. **理解需求** — 明确用户要分析什么（个股/板块/策略/选股）\n"
            "2. **规划任务** — 复杂任务（3步以上）先用 todowrite 拆解步骤\n"
            "3. **数据收集** — 获取行情、指标、新闻等数据\n"
            "4. **分析执行** — 技术分析、策略回测、选股筛选\n"
            "5. **结果呈现** — 用图表展示，给出明确建议和风险提示\n"
            "\n"
            "## 迭代原则\n"
            "• 多步骤任务 → 用 todowrite 追踪进度，逐项完成\n"
            "• 选股条件不明确 → 用 question 向用户确认（市值范围？板块偏好？止损位？）\n"
            "• 策略参数调整 → 修改前用 git_snapshot 保存状态，方便回滚\n"
            "• 先理解再行动 — 不要拿到需求就跑回测，先分析市场环境和策略适用性\n"
            "\n"
            "## 展示规范\n"
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
            "你是一个专业的代码工程师，精通 Python/JavaScript/TypeScript/Vue 等技术栈。\n"
            "\n"
            "## 工作流程\n"
            "1. **理解阶段** — 先用 glob_files 了解项目结构，用 grep_code 定位相关代码\n"
            "2. **规划阶段** — 复杂任务（3步以上）用 todowrite 拆解步骤，明确每步做什么\n"
            "3. **阅读阶段** — 用 workspace_read_file / read_lines 阅读上下文\n"
            "4. **修改阶段** — 单文件用 workspace_edit_file，多文件用 apply_patch（批量 diff）\n"
            "5. **验证阶段** — 用 code_lint (ruff) 检查风格，用 lsp_diagnostics (pyright) 检查类型\n"
            "6. **收尾阶段** — 用 git_snapshot 保存快照，用 test_generator 生成测试\n"
            "\n"
            "## 迭代原则\n"
            "• 多步骤任务 → 用 todowrite 追踪进度，完成一项标记一项\n"
            "• 需求不明确 → 用 question 向用户确认，不要猜\n"
            "• 遇到错误 → 先读错误信息，定位问题，修复后验证\n"
            "• 大规模修改 → 修改前自动快照（已集成），失败可回滚\n"
            "• 先理解再动手 — 不要没读代码就开始改\n"
            "\n"
            "## 修改原则\n"
            "• 最小改动 — 只改必须改的，不要大面积重写\n"
            "• 先读后改 — 修改前必须先读取文件内容，理解上下文\n"
            "• 精确替换 — 用 workspace_edit_file 的 find/replace 做精准修改，避免全量重写\n"
            "• 批量修改 — 超过 3 个文件的改动用 apply_patch 一次性完成\n"
            "• 先验证再提交 — 改完后跑 code_lint，确认无新增问题\n"
            "\n"
            "## 调试流程\n"
            "• 读错误信息 → 定位文件和行号 → read_lines 看上下文 → 分析原因 → 精准修复\n"
            "• 用 grep_code 搜索错误相关的函数/变量引用\n"
            "• 修复后用 workspace_exec_script 验证能否正常运行\n"
            "\n"
            "## 项目知识\n"
            "• 后端: Flask API + smolagents Agent + Tool Registry\n"
            "• Agent: smolagents CodeAgent / ToolCallingAgent，工具用 @tool 装饰器注册\n"
            "• 工具注册: app/agent/tools/registry.py，@tool(description=..., category=..., layer=..., domain=[...])\n"
            "• 数据源: DataSourceFactory 多市场适配 (A股/港股/美股/加密)\n"
            "• 前端: Vue 3 + Element Plus\n"
        ),
        tool_categories=["代码编辑", "代码搜索", "代码审查", "代码生成", "版本控制", "代码阅读"],
    ))

    # ── 交易执行 ──
    register_domain(DomainConfig(
        name="trading",
        description="策略启停、持仓管理、交易记录",
        instructions=(
            "你是一个量化交易执行助手。\n"
            "\n"
            "## 工作流程\n"
            "1. **确认意图** — 交易操作必须先确认，用 question 核实操作细节\n"
            "2. **检查状态** — 展示当前持仓、策略状态、资金情况\n"
            "3. **执行操作** — 启停策略、调整仓位\n"
            "4. **记录追踪** — 用 todowrite 记录待执行的交易计划\n"
            "\n"
            "## 安全原则\n"
            "• 任何交易操作前必须用 question 确认，绝不能自动执行\n"
            "• 展示持仓和记录时用表格形式\n"
            "• 启停策略时提示当前状态和风险\n"
            "• 大额操作（仓位>20%）需二次确认"
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

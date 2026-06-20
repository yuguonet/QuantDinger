# MEMORY.md — 长期记忆

## 项目：QuantDinger
- **仓库**: https://github.com/yuguonet/QuantDinger
- **本地**: /home/work/.openclaw/workspace/QuantDinger-main/
- **用途**: A股量化交易系统，小资金快速复利
- **技术栈**: Python 后端 (Flask + smolagents CodeAgent) + Vue 前端 + PostgreSQL
- **用户环境**: Windows10 + 40核 64G, D:\QuantDinger\, powershell, vscode

## Agent架构理解
- 三层决策树：Chain(编排/决策) → Skill(专业分析) → Tool(数据/指标)
- **兼容性**: tool和skill完全兼容openAI的的tool标准和Anthropic的SKILL标准
- Agent 自主推理（smolagents CodeAgent），TraceCollector 自动追踪
- EvalNode 树存库 → 盘后回溯验证 → 权重自动迭代
- 核心指标：单位时间收益率（不是胜率）

## 关键设计文档
- AGENT_ACCOUNTABLE.md — 可追责架构（2026-06-20 重写，基于当前代码状态）

## 当前进度

### §17.2 Planner 前置重构（已实施 2026-06-20）
- **改造前**: `_prepare()` 构建全量 Agent → `_chat_locked()` 调 `_try_chain()` → Planner
- **改造后**: 三阶段架构
  - 阶段 1: `_prepare_intent()` — 快速通道 + 意图分析 + stock_code + TraceCollector + 上下文 + Planner
  - 阶段 2: `_execute_phase()` — 构建 Agent + agent.run()（可多轮，工具失效时快速退出）
  - 阶段 3: `_post_process()` — 结果处理 + DecisionCard + 后置评估 + 学习闭环
- `_prepare_intent()` 签名变更为 `_prepare_intent(self, message, session_id, context, user_id=1)`
- `_IntentPrepResult` 新增 `collector`、`enriched`、`chain_context` 字段
- `_chat_locked()` 和 `_chat_stream_locked()` 不再调用 `_try_chain()`
- 新增 `.env` 配置：`PLAN_MAX_PHASES`、`PLAN_PHASE_MAX_RETRIES`、`PLAN_PHASE_FAST_EXIT_STEPS`

### Tool Registry 清理（已实施 2026-06-20）
- 删除 `_TOOL_META` 硬编码字典（80+ 行）
- 删除 `_SKIP_MODULES` 硬编码集合
- `_ToolSpec` 精简为 `fn`/`name`/`description`
- `_generate_tool_catalog()` 改为按模块名自动分组
- 工具插件化：`tools/` 目录下有 docstring 的公开函数自动发现

## 用户偏好
- 做 A 股量化交易
- 用 @skill 装饰器方式（不用 BaseSkill 子类方式）
- 清理遗留文件
- 能通过修改上游解决问题的不修改下游

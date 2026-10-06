# MEMORY.md — 长期记忆

## 项目：QuantDinger
- **本地**: D:/QuantDinger
- **用途**: A股量化交易系统，小资金快速复利
- **技术栈**: Python 后端 (Flask + mimo agents + quantdinger) + Vue 前端 + PostgreSQL
- **用户环境**: Windows10 + 40核 64G, D:\QuantDinger\, powershell, vscode
## 关键设计文档
- docs目录中及系统目录下

- 执行层用上游 **smolagents[openai]>=1.27.0**（CodeAgent）；未引入 langchain / pydantic-ai / agno / openai-agents。
- 差异化内核（不可替换）：`chain/`（EvalNode 树 + 盘后评估/权重迭代）、`capabilities/` 准入、`tools/finance/` 领域工具、`skills/`(SKILL.md)、`rag/`、`resolvers/`、`message_queue`。
- 契约：Tool 兼容 OpenAI Function Calling；Skill 兼容 Anthropic SKILL。
- **实际图结构**（以代码为准）：`chat → plan → execute → finalize` 四节点，注册在 `agents/task_agent.py:1765-1771`，节点工厂在 `nodes.py`（`make_chat_node:374` / `make_plan_node:592` / `make_execute_node:1228` / `make_finalize_node:1400`）。
- ⚠️ **文档与代码有既定落差**：`docs/AGENT_ACCOUNTABLE.md` 头部自述"状态: 实施中（LangGraph 版本）"，其 §二~§七 描述的是**目标架构**（LangGraph StateGraph + prepare/planner/agent 3-LLM + `messages: Annotated[...,add]` + `cached_tools` + `_collectors` + PostgresSaver + 通用 entity_code/entity_type）；而**代码现状**是自研 graph + chat/plan/execute + step_budget/phases + 金融专用。即"换 LangGraph"是项目自己已定的方向，只是**尚未落地**。评估时一律以代码为准。
- `docs/harness改造计划.md` 是上一轮改造计划（用 smolagents 替换旧 executor.py/runner.py/factory.py），**已完成**。
- 项目**只有一套** agent 实现（`app/agent`），不存在 `app/nanobot` 等第二套。
- ⚠️ `AgentState`（`nodes.py:39`）含**不可序列化字段**：`_code_agent`、`_phase_agents`（smolagents CodeAgent 实例）。启用任何"序列化整个 state"的 checkpointer 前，必须先把这些字段移出 state。
- Checkpointer 接口自研且**当前未启用**（`task_agent.py:1765` 附近注明"暂不启用 checkpointer，需要数据库连接池"）→ "可回测/可复现"存在真实缺口。
## 方向和约束
- 做 A 股量化交易,代码修改和迭代,项目研究和评估
- 网上找轮子比自己造轮子更好
- 不使用硬编码的方式写代码
- 不使用兜底方案和打补丁方案解决问题,要找到问题根源
- 模块化设计
- 统一代码风格,比如内部函数/接口使用下划线
- 修复问题不要矫枉过正
- 较大变动先分析再询问是否修改
- 每个文件的特点作用功能应该记录在头部注释中,关键设计点,容易误解和容易出错点都应该将注释,并放在当前代码旁
- 临时文件,结果,日志放在tmp/目录下,尽量不要污染整个项目
- 如需要删除文件,则使用移动指令移到del目录中
- 当前项目中多人在同时协同工作,注意加以区分：共享区域（MEMORY.md、docs/、tmp/、日志等）只增量追加，MEMORY.md文件没有声明追加,禁止追加, 不覆盖他人内容；自己产出的内容需标注来源（任务名/署名/时间）以便区分。
- 禁止执行任何 git 指令（status/add/commit/push/checkout/reset 等），提交一律由用户手工完成。
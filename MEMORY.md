# MEMORY.md — 长期记忆

## 项目：QuantDinger
- **本地**: D:/QuantDinger
- **用途**: A股量化交易系统，小资金快速复利
- **技术栈**: Python 后端 (Flask + smolagents CodeAgent) + Vue 前端 + PostgreSQL
- **用户环境**: Windows10 + 40核 64G, D:\QuantDinger\, powershell, vscode
## 关键设计文档
- docs/AGENT_ACCOUNTABLE.md — 可追责架构（2026-06-20 重写，基于当前代码状态）
- docs目录中及系统目录下
- backend_api_python/app/agent/DESIGN.md — Agent 模块设计（最新 v2.2, 2026-09-13；含"能力发现层断链清单 L1~L7"审计记录与 v2.2 修复说明）
## Agent 架构现状（2026-09-13 核实）
- 编排层 `app/agent/graph.py` 是**自研轻量 StateGraph**，刻意不引入 langgraph 依赖（文件头明确写"不引入 langgraph 依赖，自己实现"）。
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
- 当前项目中多人在同时协同工作,注意加以区分：共享区域（MEMORY.md、.codebuddy/memory/、docs/、tmp/、日志等）只增量追加，不覆盖他人内容；自己产出的内容需标注来源（任务名/署名/时间）以便区分。
- 禁止执行任何 git 指令（status/add/commit/push/checkout/reset 等），提交一律由用户手工完成。
- **领域(domain)必须可扩展**（2026-09-13）：领域不止 finance，未来会有很多领域。凡涉及领域的判定一律用**登记表/目录推导**，禁止在代码里硬编码某个领域名。
  · 工具集域 = `tools/<子目录>` 名（`ToolProvider.get_domains()`）；工具**来源层**（如 `capabilities/` 的准入函数）**不是域**，不得占用 `selected_domain`。
  · "领域特性"（如交易日口径）放登记表：`resolvers/time.py` 的 `_ENTITY_DOMAIN` / `TRADING_CALENDAR_DOMAINS`。
- **领域标准化输出 = 注册表 + 自动发现**（2026-09-13）：新增领域只需放 `formatters/<domain>.py` 并 `@register_formatter("<domain>")`，`formatters/__init__.py` 自动加载（禁止再手写 import 清单）。查找顺序 **domain → entity_type → default**。
- **清单裁剪用相关性**（2026-09-13）：工具/条目超出注入上限时按与本次需求的相关度排序后截断（中文可用 2-gram 打分，无需分词依赖），**不要用字母序**；被裁数量要如实告知，无相关信号时退化为稳定序以避免随机丢弃。
- ⚠️ **本项目高发 bug："声明了但没接线"的静默断链**（2026-09-13 一次审计就查出 5 处）：判据依赖已被清空的字段（技能恒空 → 条件恒真）、参数从未传入（`TimeResolver()` 无参 → 分支恒假）、模块从未 import（formatter 注册表恒空）、注册 key 与查询 key 语义不一致、变量算了不用。共同点是**不报错、不告警、无 trace**。新增/改动这类"注册表/契约"时，务必顺手确认消费端真会命中（可加 `list_formatters()` 这类自检接口）。
  · 2026-09-13 又查出更狠的一例（L8）：**注入对象不满足消费端契约**（注入裸函数，而 `nodes.py` 调 `.resolve()`）→ `AttributeError` 被 `except: logger.debug` 吞掉 ⇒ 整条链**从未执行**。教训：验证时**不能只测组件本身，必须测"注入对象是否满足调用处契约"**；凡 `except` 里只写 debug 的接线处都该怀疑（已把该处升级为 warning）。
- **澄清优先于猜测**（2026-09-13 用户裁定）：**无法准确判断就应该反问，拿到准确信息才执行**。解析器/取数层遇到歧义（标的多个候选、口径不明、相对时间不落交易日…）必须返回 `clarify_question`（见 `resolvers/base.ResolveResult`），**不许猜默认值往下跑**——猜错标的/窗口会让整份分析作废，反问只花一轮对话。新增领域/解析器沿用该契约（`resolvers/composite.CompositeResolver` 组合），无需改 `chat_node`。
- ⚠️ **裸环境验证 agent 代码的坑**（2026-09-13）：本机无 `requests`/`pandas`，`app` 包链会断。**不要**用桩顶掉 `app.utils`——`resolvers/time.py` 的 `_cal()` 依赖 `from app.utils import trading_calendar`，被顶掉会误判成"逻辑没生效"。正确做法：`importlib.util.spec_from_file_location` 按**文件路径**加载那一个真实模块再挂到桩上。
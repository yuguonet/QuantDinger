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

## 用户偏好
- 做 A 股量化交易
- 不使用硬编码的方式
- 模块化设计

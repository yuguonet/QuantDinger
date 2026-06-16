# MEMORY.md — 长期记忆

## 项目：QuantDinger
- **仓库**: https://github.com/yuguonet/QuantDinger
- **本地**: /home/work/.openclaw/workspace/QuantDinger-main/
- **用途**: A股量化交易系统，小资金快速复利
- **技术栈**: Python 后端 (Flask + smolagents CodeAgent) + Vue 前端 + PostgreSQL
- **用户环境**: Windows10 + 40核 64G, D:\QuantDinger\, powershell, vscode

## 架构理解
- 三层决策树：Chain(编排/决策) → Skill(专业分析) → Tool(数据/指标)
- 80+ 工具，16 个内置 Skill
- Agent 自主推理（smolagents CodeAgent），TraceCollector 自动追踪
- EvalNode 树存库 → 盘后回溯验证 → 权重自动迭代
- 核心指标：单位时间收益率（不是胜率）

## 关键设计文档
- AGENT_REDESIGN.md — 三层追责体系设计
- AGENT_ACCOUNTABLE.md — 可追责架构（Phase 1 实施 + 缺陷记录 + 回测设计）
- DESIGN_RESTRUCTURE.md — 已废弃，保留只读
- SEMANTICS_REFACTOR.md — Domain 解耦重构

## 当前进度
- Phase 1 核心组件已实现，集成缺陷已修复
- Domain 解耦 Phase 1-3 已完成
- 待完成：端到端验证、回测引擎、前端可视化

## 用户偏好
- 做 A 股量化交易
- 用 @skill 装饰器方式（不用 BaseSkill 子类方式）
- 清理遗留文件
- 能通过修改上游解决问题的不修改下游

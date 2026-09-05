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
- 架构通用性,不局限在金融领域,所有领域保持通用性
- 金融领域只是agent的强化,不是唯一,不能专门针对金融领域
- 做 A 股量化交易,代码修改和迭代,项目研究和评估,代码需要通用性.只有细节才可以对某些领域进行优化

## 关键设计文档
- AGENT_ACCOUNTABLE.md — 可追责架构（2026-06-20 重写，基于当前代码状态）

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
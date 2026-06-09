# MEMORY.md

## 用户
- 做A股量化交易，目标小资金快速复利
- 本地环境：Windows10 + 40核 64G主机，D:\QuantDinger\, powershell, vscode,FLASK, OLLAMA,postgresSQL
- 项目: https://github.com/yuguonet/QuantDinger
- 项目中所有_(下划线)都是内部函数,不能被外部调用,需要调用前必须先说明
- 前端:QuantDinger-Vue, 后端:backend_api_python
- 配置文件在backend_api_python/.env不上传,上传的是env.spalme
- 第一次进行大量修改需要新建函数时先对后端backend_api_python/app/下扫描所有py文件检查是否有现成的函数
- 工作完成后应该按目录位置打包修改过的或新增的文件放到和USER.md同目录下
- 能通过修改上游解决问题的不修改下游.

## Phase 1 实施记录（2026-06-09）

### 已完成
- AGENT_ACCOUNTABLE.md 架构设计全部组件已实现
- 迁移脚本 qd_traces.sql 已创建（qd_traces + qd_skill_weights + qd_factor_weights）
- TraceCollector / TracedTool / evaluator / CallSkillTool 均已到位
- 集成测试发现并修复 5 个缺陷（详见 AGENT_ACCOUNTABLE.md §十二）

### 未通过的端到端验证
- agent 输出自由文本而非 JSON（工具不可用 + stock_code 未提取）
- call_skill 未被 agent 调用（agent 直接调底层工具）
- 盘后工具返回空数据时 agent 放弃分析
- 非金融域 _try_chain 断裂（planner/executor 已删除）

### 下一步
- 端到端验证：确保 agent 能调用 call_skill 并输出 JSON
- Skill 内部工具调用追踪（Tool 层缺失）
- save_tree 补写 session_id/user_query 元数据
- 非金融域架构决策（恢复 planner 或统一走 agent 自规划）

## 待办
- QuantDinger Agent架构重设计，详见 AGENT_REDESIGN.md（已被 AGENT_ACCOUNTABLE.md 替代）


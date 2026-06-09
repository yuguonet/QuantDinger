# QuantDinger Agent架构重构设计方案

> 日期: 2026-06-09
> 状态: 设计完成，Phase 1 待实施

## 一、现状问题

### 1.1 双轨制
- **Chain 路径**：`ChainExecutor → BaseSkill.run() → 结构化 SkillReport + EvalNode 树` ✅
- **自由路径**：`smolagents ManagedAgent → LLM 自由调工具 → 自由文本输出 → 事后解析` ❌
- 同一个 Skill，两条路径执行逻辑完全不同

### 1.2 自由路径未接入 BaseSkill
- `_build_managed_agents` 把 Skill 的 instructions 和 tools 拆出来塞给 smolagents
- BaseSkill 本身没被调用，smolagents LLM 自己决定调什么工具
- `_save_freeform_to_db` 事后从 LLM 输出硬解析，信息损失大
- `_infer_skill_name` 从 tool_calls 反推 skill 名，不准确

### 1.3 算法化不可行
- 自由路径完全由 LLM 驱动，无法插入算法引擎
- 8 个可纯算法实现的 Skill 每次都烧 token

## 二、设计目标

1. **能用算法不用推理** — 两者合作不互斥
2. **单轨制** — 所有路径统一走 BaseSkill
3. **规划可复现** — LLM 做规划，系统做执行
4. **三层降级** — 固定链路 → LLM 规划 → 兜底（必须告知用户）

## 三、架构设计

```
用户消息
    │
    ▼
Intent Analyzer → 有固定链路（Layer 0）？
    │
    ├── Yes → ChainExecutor 执行固定链路
    │
    └── No → Planner（Layer 1，轻量 LLM 调用）
              │
              ├── 输入：用户问题 + 可用 Skill 列表
              ├── 输出：{steps: [...], stocks: [...], reasoning: "..."}
              ├── 校验：步数 1~5、必须含 technical_agent、去重
              ├── 缓存：相似 query 复用旧规划
              │
              └── ChainExecutor 按规划执行
                        │
                        ├── 规划失败 → Layer 2 降级兜底
                        │   → 默认链路（technical + momentum）
                        │   → ⚠️ 必须告知用户"当前为降级模式，分析可能不完整"
                        │
                        └── 每步 → BaseSkill.run()
                                  → algo_analyze() 优先
                                  → LLM 补位（仅 algo 返回 None 时）
```

### 3.1 BaseSkill 执行流（algo 优先，LLM 补位）

```
Skill.run()
    │
    ▼
Phase 1: 调 tools 取数据
    │
    ▼
Phase 2: algo_analyze(stock_code, stock_name, tool_results)
    │
    ├── 返回 SkillReport → 直接返回（跳过 LLM，0 token）
    │
    └── 返回 None → Phase 3: build_prompt + call_llm + parse
                      → 返回 SkillReport
```

### 3.2 三层定义

| 层 | 触发条件 | LLM 参与 | 可回测性 | Token 消耗 |
|---|---------|---------|---------|-----------|
| Layer 0 | verb+noun 精确匹配 | Chain 决策时 | 高 | 低 |
| Layer 1 | 无固定链路匹配 | 规划 + Skill 内分析 | 高（规划可重放） | 中 |
| Layer 2 | 规划失败 | 全程 | 低（兜底） | 高 |

### 3.3 降级兜底规则（必须告知用户）

```
触发条件：
  - LLM 规划返回无效（步数为0、无有效 Skill）
  - Planner LLM 调用失败（超时/异常）
  - 校验不通过且无法修复

行为：
  - 使用默认链路：[technical_agent, momentum_tracker]
  - 在返回结果中附加警告：
    "⚠️ 当前为降级模式（规划失败：{reason}），仅执行基础分析，结果可能不完整。"
  - 记录日志：降级原因、时间、用户 query
```

## 四、Skill 分类

### 可纯算法实现（8 个，Phase 4 逐步实现 algo_analyze）

| Skill | 算法逻辑 |
|-------|---------|
| technical_agent | MA交叉、MACD金叉死叉、成交量突破、K线形态匹配、筹码峰计算 |
| momentum_tracker | RSI/ATR/ADX 计算、N日新高突破检测、动量百分位排名 |
| indicator_agent | MACD/KDJ/RSI/BOLL 计算 + 金叉/死叉/超买超卖阈值判断 |
| market_data_agent | API 取数据 → 排序/筛选/格式化 |
| screening_agent | 条件过滤 + 打分排序 |
| backtest_agent | K线遍历 → 信号触发 → 买卖撮合 → 统计 |
| lockup_watcher | 查数据库 → 计算解禁比例 → 阈值预警 |
| data_agent | ETL 脚本执行 |

### 必须 LLM（6 个）

| Skill | 原因 |
|-------|------|
| intelligence_agent | 新闻语义理解 |
| policy_analyst | 政策文件解读 |
| bull_researcher | 看涨叙事构建 |
| bear_researcher | 看跌叙事构建 |
| concept_tracker | 题材生命周期判断 |
| trading_agent | 风险提示+自然语言确认 |

### 混合（1 个）

| Skill | 算法部分 | LLM 部分 |
|-------|---------|---------|
| screening_agent | 条件过滤+排序 | 推荐理由（可选） |

## 五、tool_chains.json 结构

```json
{
  "version": 1,
  "chains": [
    {
      "id": "auto_20260609_001",
      "query_hash": "a1b2c3d4",
      "query": "茅台能不能买",
      "created_at": "2026-06-09T12:35:00+08:00",
      "steps": [
        {"agent": "technical_agent", "order": 1},
        {"agent": "momentum_tracker", "order": 2},
        {"agent": "intelligence_agent", "order": 3}
      ],
      "stocks": ["600519"],
      "reasoning": "用户问个股买入决策，技术面+动量是核心，情报辅助验证",
      "hit_count": 3,
      "last_used": "2026-06-09T14:00:00+08:00",
      "backtest_results": {
        "win_rate": 0.65,
        "avg_pnl": 2.3,
        "sample_count": 12
      }
    }
  ]
}
```

## 六、落地计划

### Phase 1: 单轨制（1~2天）
- [x] BaseSkill 加 `algo_analyze` 方法（默认返回 None，向后兼容）
- [x] 创建 `skills/call_skill_tool.py`（CallSkillTool）
- [x] agent.py 移除 `_build_managed_agents`，注入 CallSkillTool
- [ ] 验证 Chain 路径和自由路径都走 BaseSkill

### Phase 2: 规划层（2~3天）
- [x] Planner（轻量 LLM 调用，只选 Skill 不执行）
- [x] tool_chains.json 读写
- [x] 校验（步数限制、必选 Skill、去重）
- [x] 降级兜底 + 用户告知

### Phase 3: 降级 + 缓存（1~2天）
- [x] 相似 query 复用旧规划（关键词 Jaccard + 股票代码 + verb/noun 多维度匹配）
- [x] 规划 TTL 过期清理（24h TTL，保存时自动清理）
- [x] 降级日志记录（结构化 JSON 写入 planner_degrade.log）
- [x] 回测结果集成（update_backtest_result 接口）

### Phase 4: 算法引擎（持续）
- [x] technical_agent.algo_analyze — 趋势50%+量价20%+指标20%+形态10%
- [x] momentum_tracker.algo_analyze — 趋势35%+动量30%+量价20%+突破15%
- [x] indicator_agent.algo_analyze — 执行用户指标，汇总 buy/sell 信号
- [x] market_data_agent.algo_analyze — 大盘30%+板块25%+资金25%+行情20%
- [x] lockup_watcher.algo_analyze — 解禁比例阈值评分
- [ ] screening_agent.algo_analyze — 条件过滤+排序
- [ ] backtest_agent.algo_analyze — 模拟交易引擎
- [ ] data_agent.algo_analyze — ETL 脚本执行

## 七、改动文件清单

| 文件 | 改动类型 | Phase |
|------|---------|-------|
| `agent/skills/base.py` | 修改：加 algo_analyze 方法 | 1 |
| `agent/skills/call_skill_tool.py` | 新增：CallSkillTool | 1 |
| `agent/agent.py` | 修改：移除 managed_agents，注入 CallSkillTool | 1 |
| `agent/skills/technical.py` | 修改：实现 algo_analyze | 4 |
| `agent/skills/momentum.py` | 修改：实现 algo_analyze | 4 |
| `agent/skills/indicator_agent.py` | 修改：实现 algo_analyze | 4 |
| `agent/planner.py` | 新增：LLM 规划器 | 2 |
| `agent/chain/store.py` | 修改：tool_chains.json 读写 | 2 |

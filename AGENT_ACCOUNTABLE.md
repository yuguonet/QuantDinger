# Agent 可追责架构设计

> 最后更新: 2026-06-21
> 状态: 实施中
> 仓库: https://github.com/yuguonet/QuantDinger

## 一、设计原则

### 1. Agent 是唯一的决策者
- smolagents CodeAgent 保持完整推理-行动-观察循环
- Agent 自主决定调什么工具、看什么数据、怎么分析、何时结束
- **Planner 只规划，不执行**：Planner 产出执行计划，注入 Agent 上下文，由 Agent 自己用 read_skill + 工具执行
- **不削弱**：不用 algo_analyze() 替代 LLM 推理
- **兼容性**: tool 和 skill 完全兼容 OpenAI 的 tool 标准和 Anthropic 的 SKILL 标准
  - Tool → OpenAI Function Calling 标准（JSON Schema）
  - Skill → Anthropic Agent Skills 标准（SKILL.md）

### 1.1 设计终极目标
- **最大实现可复测**：同样的输入条件能重放 → 才能验证决策对不对 → 才能迭代权重
- **最终目的**：减小系统 bug 或规则不完善带来的亏钱
- 四个闭环各自堵一个漏洞，复杂但必要

### 2. EvalNode 树是审计日志，不是执行引擎
- Agent 执行过程中，每一步自动构建 EvalNode 树
- 树记录"发生了什么"，不决定"应该发生什么"
- 执行完 → 树存库 → 盘后回溯验证 → 权重迭代

### 3. 三层追责不变
- **Chain 层**：agent 的整体决策（最终 action/score/direction）
- **Skill 层**：每次 call_skill 的分析报告（标准化 SkillReport）
- **Tool 层**：每次工具调用的入参出参

### 4. 可回测 > 可解释 > 可复现
- 优先保证每个决策能和实际盈亏对比
- 其次保证能追溯"为什么做了这个决定"
- 可复现靠数据快照，不靠砍掉 agent 自适应

## 二、架构总览

```
用户消息
    │
    ▼
┌──────────────────────────────────────────────────────────┐
│  Intent Analyzer (LLM #1)                                │
│    → domain / verb / noun / strategy / stock_code        │
│                                                          │
│  _try_chain()                                            │
│    → tool_chains.json 匹配 或 Planner (LLM #2) 规划     │
│    → 返回执行计划文本（不执行）                            │
│    → 注入 Agent 上下文                                    │
│                                                          │
│  smolagents CodeAgent                                    │
│  ┌────────────────────────────────────────────────────┐  │
│  │  推理循环: Observe → Think → Act → Observe → ...   │  │
│  │  Agent 用 read_skill + 工具 自主执行计划            │  │
│  └────────────────────────────────────────────────────┘  │
│                                                          │
│  工具集:                                                  │
│    ① 80+ 普通工具 (get_kline, analyze_trend, ...)        │
│    ② read_skill 工具 (加载 SKILL.md 指令)                │
│    ③ final_answer 工具 (结束并输出最终结论)                │
│                                                          │
│  自动记录层 (TraceCollector):                              │
│    每次 tool_call → EvalNode 子树                         │
│    最终 answer → Chain 根节点                             │
└──────────────────────────────────────────────────────────┘
    │
    ▼
  EvalNode 树 → 存库 → 盘后回溯 → 权重迭代
```

## 三、三阶段执行流程（§17.2）

### 3.1 流程总览

```
用户消息
    │
    ▼
阶段 1: _prepare_intent() ──────────────────────────
│  0. 负面反馈检测 — 惩罚上一轮 chain
│  1. 快速通道判断 — 正则匹配闲聊（0 LLM 调用）
│  2. 意图分析 ← LLM #1（仅非闲聊时）
│  3. 提取 stock_code
│  4. 创建 TraceCollector
│  5. 上下文拼接（历史摘要 + 领域上下文）
│  6. _try_chain() 获取执行计划                              │
│     ├─ tool_chains.json 匹配 → 直接返回计划               │
│     ├─ 未匹配 → Planner (LLM #2) 规划 → 返回计划         │
│     └─ 无 verb/noun 或规划失败 → 返回 None               │
│     计划格式：每个 phase 独立自包含                         │
│     （任务 + 技能 + 工具 + 说明 + 规则）                    │
│     注入 Agent 上下文（enriched）                           │
└──────────────────────────────────────────────────────
    │
    ▼
阶段 2: _execute_plan() ──── 按 phase 顺序循环 ────
│  for phase in plan.phases:
│  ┌────────────────────────────────────────────────┐
│  │  7. 构建 Agent（只加载当前 phase 的内容）       │
│  │     - 当前 phase 的任务描述                     │
│  │     - 当前 phase 的 skill 指令                  │
│  │     - 当前 phase 的工具子集                     │
│  │     - 当前 phase 的规则                         │
│  │  8. agent.run() ← LLM #3（单 phase 执行）      │
│  │     Agent 只看到这 1 步，专注完成               │
│  │  9. 错误检测:                                  │
│  │     ├─ 成功 → 保存结果，进入下一个 phase        │
│  │     └─ 工具失效 → 快速退出，LLM #2 决策        │
│  └────────────────────────────────────────────────┘
│  全部 phase 完成 → 进入阶段 3
└──────────────────────────────────────────────────────
    │
    ▼
阶段 3: _post_process() ────────────────────────────
│ 10. 存消息到 session
│ 11. DecisionCard（traced 策略时）
│     ├─ JSON 提取 → 格式化决策卡
│     └─ 提取失败 → fallback skip 结构
│ 12. TraceCollector 存库
│ 13. 后置评估 + 学习闭环
│ 14. 异步压缩上下文
│ 15. 返回 AgentResult
└──────────────────────────────────────────────────────
```

### 3.2 关键设计点

| 设计点 | 说明 |
|--------|------|
| Planner 只规划不执行 | LLM #2 产出计划文本，注入 Agent 上下文，Agent 自己执行 |
| 计划注入而非调度 | 不经过 ChainExecutor 逐个调 Skill，Agent 用 read_skill + 工具自行执行 |
| 错误快速退出 | 工具连续失败 ≥ 阈值时不跑满 max_steps，快速退出重试 |
| 负面反馈前置 | 在 Planner 之前生效，影响规划决策 |
| 根节点兜底写入 | 非 traced 策略在阶段 3 后写根节点到 qd_traces，保证回溯验证有数据 |

### 3.3 配置项

```bash
# .env 配置
PLAN_MAX_PHASES=3              # 单次规划最大阶段数
PLAN_PHASE_MAX_RETRIES=1       # 单阶段最大重试次数
PLAN_PHASE_FAST_EXIT_STEPS=3   # 工具失效快速退出步数阈值
```

## 四、核心组件
### 4.1a Skill Registry — 技能注册表

**职责**: 扫描 `agent/skills/` 目录，自动发现并注册技能。
**插件化**: 新增技能只需往 `skills/` 目录扔文件夹和skill.md文件，零配置。
### 4.1b Tool Registry — 工具注册表

**职责**: 扫描 `agent/tools/` 目录，自动发现并注册工具。

**发现规则**:
- `tools/` 目录下 `.py` 文件
- 跳过 `_` 开头的文件
- 有 docstring 的公开函数 → 自动注册

**工具定义**: 纯 OpenAI Function Calling 或 Tool Calling 标准
- `name`: 函数名
- `description`: docstring 第一行
- `parameters`: type hints 自动生成

**插件化**: 新增工具只需往 `tools/` 目录扔 `.py` 文件，零配置。

### 4.2 TraceCollector — 执行追踪器

**职责**: Agent 执行过程中自动收集信息，构建 EvalNode 树。

**提取策略**: JSON 优先，正则降级。

**层级**:
- Tool 层: 每次 tool_call → EvalNode 子树
- Skill 层: 每次 call_skill → SkillReport + EvalNode
- Chain 层: 最终 answer → 根节点

### 4.3 Planner — 规划器

**职责**: 当无固定链路匹配时，用 LLM 规划 Skill 执行方案。规划结果注入 Agent 上下文，由 Agent 自己执行。

**设计本质**: Chain 编排层是从用户消息到执行入口的便捷路线，缓存好的思维捷径，省去 LLM #2 的重复思考。不是逐个调度 Skill 的执行器。

**注入内容**:
| 内容 | 来源 | 用途 |
|------|------|------|
| persona | `semantics/persona.md` | 告诉 Planner 它是谁 |
| context | `store.get_context_summary()` | 对话历史摘要 |
| skills | `get_skills_summary_xml()` | 全量 skill 列表+描述 |
| tools | `get_tools_summary_xml()` | 全量 tool 列表+描述 |
| rules | `semantics/planner.md` | 核心哲学+数据陷阱+技能优先级+输出格式 |

**输出格式**:
```json
{
  "phases": [
    {
      "phase": 1,
      "skill": "technical_agent",
      "tools": ["analyze_trend", "get_indicator_snapshot", "agent_get_kline"]
    },
    {
      "phase": 2,
      "skill": "intelligence_agent",
      "tools": ["search_stock_news", "get_market_sentiment"]
    }
  ],
  "stocks": ["600519"],
  "reasoning": "选择理由（50字以内）"
}
```

### 4.4 Intent Analyzer — 意图分析器

**职责**: 分析用户意图，提取 domain/verb/noun/strategy。

**流程**:
1. 快速通道: 正则匹配闲聊（0 LLM 调用）
2. LLM 分析: 仅非闲聊时调用
3. 提取 stock_code: 从消息解析个股代码

**输出**: `IntentResult` 结构体，包含 intent/domain/verb/noun/strategy/confidence。

### 4.5 Evaluator — 评估器

**职责**: 盘后回溯验证，统一驱动权重更新和链路学习。

**数据来源**: `qd_traces` 表 + `qd_skill_weights` + `qd_factor_weights`

**更新逻辑**:
- Skill 权重: 按单位时间收益率聚合（多次 T+N 验证渐进调权）
- 因子权重: 按单位时间收益率聚合（带时间衰减，不同因子半衰期不同）
- 链路学习: 验证正确的链路才写入 tool_chains.json（5 道质量门）

**追踪能力**:
- Skill 维度: `qd_skill_weights.return_per_day` 下降 → 该 Skill 在退化
- 因子维度: `qd_factor_weights.win_rate` 下降 → 该因子在失效
- 链路维度: `tool_chains.json.stats.success_rate` 下降 → 该工具组合在失效
- 工具维度: `qd_traces` 中 tool 层节点的 correct 统计 → 哪个工具在出错

## 五、语义文件结构

```
semantics/
├── persona.md          # 人设 → Planner + Agent
├── intent.md           # 意图分类规则 → IntentAnalyzer
├── planner.md          # 规划规则 → Planner LLM
├── guidance.md         # 执行规则 → Agent instructions
└── skills/
    └── <name>/
        └── SKILL.md    # Skill 定义 → call_skill
```

**职责分离**:
| 文件 | 注入到 | 职责 |
|------|--------|------|
| `planner.md` | Planner LLM | 选什么 Skill、为什么选、输出执行计划 |
| `guidance.md` | Agent instructions | 按顺序执行、怎么调 call_skill |
| `persona.md` | Planner + Agent | 人设 |
| `intent.md` | IntentAnalyzer | 意图分类规则 |

## 六、数据库设计

### 6.1 qd_traces — 执行追踪表

```sql
CREATE TABLE qd_traces (
    id SERIAL PRIMARY KEY,
    session_id VARCHAR(64),
    root_id INTEGER REFERENCES qd_traces(id),
    parent_id INTEGER REFERENCES qd_traces(id),
    layer VARCHAR(16),          -- 'chain' / 'skill' / 'tool'
    name VARCHAR(128),
    step_order INTEGER,
    stock_code VARCHAR(16),
    stock_name VARCHAR(32),
    domain VARCHAR(32),
    input_json JSONB,
    output_json JSONB,
    score FLOAT,
    direction VARCHAR(16),
    confidence VARCHAR(16),
    action VARCHAR(16),
    timeframe VARCHAR(8),
    correct BOOLEAN,
    pnl_pct FLOAT,
    created_at TIMESTAMP DEFAULT NOW(),
    evaluated_at TIMESTAMP
);
```

### 6.2 qd_factor_weights — 因子权重表

```sql
CREATE TABLE qd_factor_weights (
    id SERIAL PRIMARY KEY,
    factor_name VARCHAR(128) UNIQUE,
    weight FLOAT DEFAULT 1.0,
    sample_count INTEGER DEFAULT 0,
    avg_pnl_pct FLOAT DEFAULT 0,
    updated_at TIMESTAMP DEFAULT NOW()
);
```

### 6.3 qd_skill_weights — Skill 权重表

```sql
CREATE TABLE qd_skill_weights (
    id SERIAL PRIMARY KEY,
    skill_name VARCHAR(128) UNIQUE,
    weight FLOAT DEFAULT 1.0,
    win_rate FLOAT DEFAULT 0,
    avg_pnl_pct FLOAT DEFAULT 0,
    sample_count INTEGER DEFAULT 0,
    updated_at TIMESTAMP DEFAULT NOW()
);
```

## 七、四个闭环

四个闭环各自堵一个漏洞，不能去掉任何一个。终极目标：减小系统 bug 或规则不完善带来的亏钱。

### 7.1 记录闭环（数据源）
```
agent.run() → TraceCollector → EvalNode 树存库 → qd_traces
```
**作用**：记录每一步执行过程，是其他三个闭环的数据源。
**去掉后果**：没有数据，回测和溯源都废了。

### 7.2 T+N 回测闭环（概率验证 + 渐进调权）
```
每天盘后 15:30 → evaluate_pending() → 取实际行情 → 写回 correct/pnl_pct
  → update_skill_weights()      # 哪个 Skill 权重在下降
  → update_factor_weights()     # 哪个因子权重在下降
```
**作用**：用实际行情客观验证预测方向，多次验证渐进调权（单次对错无意义，看统计）。
**核心指标**：单位时间收益率（不是胜率）。
**追踪能力**：权重下降 → 识别哪个 Skill/因子在退化。
**去掉后果**：无法区分预测对不对，权重失去客观依据。

### 7.3 用户反馈闭环（加速检测 tool 故障）
```
用户说"不对/垃圾/反了" → detect_feedback_severity()
  → trace 层: mark_root_wrong() / delete_tree()
  → chain 层: penalize_chain() → success_count 扣减 → 累计后删除
```
**作用**：检测执行层故障（tool 更新、数据源挂了、API 变了）。T+N 回测也能通过统计发现，但需要 N 轮，期间持续亏钱。用户反馈是加速器。
**和 T+N 的关系**：不在同一层——用户反馈检测**执行层**（工具坏了），T+N 检测**预测层**（方向对不对）。
**去掉后果**：tool 坏了要等 N 轮 T+N 才能淘汰，期间持续亏钱。

### 7.4 编排路径学习闭环（省去 LLM #2 重复工作量）
```
首次执行成功 → _writeback_chain() → 存入 tool_chains.json
  → 第二次执行 → _try_chain() 匹配 → 直接返回计划 → 注入 Agent 上下文
```
**作用**：首次走一遍后，后续直接使用编排路径，省去 LLM #2（Planner 大脑）的重复思考。
**本质**：Chain 编排层是从用户消息到执行入口的便捷路线，是缓存好的思维捷径。
**质量门**：5 道拦截（步数>5、链长>5、评分<60、工具成功率<50%、旧链已验证），防止学习低质量链路。
**去掉后果**：每次都从零规划，重复消耗 LLM #2 token。

## 八、工具分类

### 8.1 工具层（80+ 工具，按职责分组）

| 分组 | Tools | 职责 |
|------|-------|------|
| 行情数据 | search_stock_by_name, get_realtime_quote, agent_get_kline, get_stock_info, get_order_book | 取K线/实时行情/盘口 |
| 指标计算 | analyze_trend, calculate_ma, get_volume_analysis, analyze_pattern, get_chip_distribution, get_indicator_snapshot, run_indicator_signal | 技术指标/形态/筹码 |
| 市场数据 | get_market_overview, get_market_indices, get_sector_rankings, get_hot_sectors, get_sector_fund_flow, get_concept_fund_flow, get_northbound_flow | 大盘/板块/北向 |
| 情报搜索 | search_stock_intel, search_sector_intel, search_policy_intel, search_comprehensive_intel | 个股/板块/政策/综合情报 |
| 选股筛选 | search_stocks, list_user_selection_strategies, build_keyword_from_filters, get_screener_presets | 综合选股/策略/筛选 |
| 信号捕捉 | get_hot_stocks_with_reasons, get_stock_concept_blocks, get_lockup_expiry, get_industry_ranking, get_dragon_tiger_detail | 热点/概念/解禁/龙虎榜 |
| 研究分析 | get_consensus_eps, get_capital_summary, batch_valuation_compare, get_eastmoney_stock_news, get_global_finance_news | 盈利预测/估值/新闻 |
| 板块分析 | get_hot_sectors, get_sector_trend_analysis, get_sector_history_data, get_sector_prediction, get_sector_cycle, get_stock_sector_info, get_sector_stocks | 板块趋势/周期/成分股 |
| 交易管理 | list_strategies, get_strategy_detail, start_strategy, stop_strategy | 策略管理/执行 |
| 系统工具 | get_page, get_cache_summary, get_text_page | 分页/缓存 |

### 8.2 Skill 层（通过 call_skill 调用）

Skill 从 `semantics/skills/*/SKILL.md` 动态加载，包含:
- 名称、描述、标签
- 优先级、默认权重
- 工具列表
- 执行指令

## 九、配置项

### 9.1 环境变量

```bash
# Agent 模型配置
AGENT_LLM_MODEL=                 # 主 Agent 模型
AGENT_COMPRESS_MODEL=            # 上下文压缩模型
AGENT_EMBED_MODEL=               # Embedding 模型

# §17.2 Planner 多阶段执行配置
PLAN_MAX_PHASES=3                # 单次规划最大阶段数
PLAN_PHASE_MAX_RETRIES=1         # 单阶段最大重试次数
PLAN_PHASE_FAST_EXIT_STEPS=3     # 工具失效快速退出步数阈值

# 意图分析
INTENT_ANALYSIS_ENABLED=true     # 是否启用意图分析

# 代码执行
CODE_EXECUTION_TIMEOUT=120       # 代码执行超时（秒）
```

### 9.2 语义文件

- `semantics/persona.md` — 人设定义
- `semantics/intent.md` — 意图分类规则
- `semantics/planner.md` — Planner 规则
- `semantics/guidance.md` — Agent 执行规则
- `semantics/skills/*/SKILL.md` — Skill 定义

## 十、文件结构

```
backend_api_python/app/agent/
├── agent.py              # 核心 Agent 执行器
├── planner.py            # Planner 规划器
├── intent_analyzer.py    # 意图分析器
├── evaluator.py          # 评估器
├── trace_collector.py    # 执行追踪器
├── traced_tool.py        # 工具追踪包装
├── context_compressor.py # 上下文压缩
├── json_extractor.py     # JSON 提取
├── session_store.py      # Session 存储
├── model.py              # 模型构建
├── tool_context.py       # 工具上下文
├── text_utils.py         # 文本工具
├── run.py                # CLI 入口
├── chain/                # 链路系统
│   ├── chains.py         # 链路定义 (ChainDef/ChainStep)
│   ├── executor.py       # 链路执行器（死代码，已不被调用）
│   ├── evaluator.py      # 回溯评估引擎（T+N 验证 + 权重更新）
│   ├── schema.py         # 数据结构 (EvalNode/SkillReport/FactorItem)
│   ├── store.py          # qd_traces 持久化
│   ├── contract.py       # Skill 输出解析契约
│   └── tool_chains.json  # 学习积累的编排路径
├── semantics/            # 语义文件
│   ├── __init__.py       # 语义加载器
│   ├── persona.md        # 人设
│   ├── intent.md         # 意图规则
│   ├── planner.md        # Planner 规则
│   ├── guidance.md       # Agent 规则
│   └── skills/           # Skill 定义
├── skills/               # Skill 执行
│   ├── call_skill_tool.py
│   └── registry.py
└── tools/                # 工具插件
    ├── registry.py       # 工具注册表
    ├── data_tools.py     # 行情数据
    ├── analysis_tools.py # 分析工具
    ├── indicator_tools.py # 指标工具
    ├── capital_tools.py  # 资本面
    ├── quote_tools.py    # 盘口数据
    ├── market_screen.py  # 市场筛选
    ├── screener_tools.py # 选股筛选
    ├── signal_tools.py   # 信号捕捉
    ├── research_tools.py # 研究分析
    ├── sector_analysis_tools.py # 板块分析
    ├── trading_tools.py  # 交易管理
    └── ...               # 更多工具插件
```

## 十一、已知限制

1. **Planner 输出解析**: 依赖 LLM 输出 JSON 格式，有容错处理但不保证 100% 成功
2. **工具失效检测**: 基于 steps 数量和工具调用成功率，可能误判
3. **多阶段循环**: 当前实现为顺序循环，不支持并行执行
4. **LLM #2 决策**: 工具失效时的 LLM #2 决策逻辑尚未完全实现（TODO）

## 十二、后续迭代

1. **LLM #2 决策完善**: 工具失效时返回 LLM #2 决策（返回/提问/另选路径）
2. **并行阶段执行**: 支持无依赖关系的 phase 并行执行
3. **动态 skill 加载**: 根据 phase 需要动态加载/卸载 skill
4. **回测引擎**: 完整的回测系统设计（见 §十四）

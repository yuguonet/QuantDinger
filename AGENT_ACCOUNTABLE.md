# Agent 可追责架构设计

> 最后更新: 2026-06-20
> 状态: 实施中
> 仓库: https://github.com/yuguonet/QuantDinger

## 一、设计原则

### 1. Agent 是唯一的决策者
- smolagents CodeAgent 保持完整推理-行动-观察循环
- Agent 自主决定调什么工具、看什么数据、怎么分析、何时结束
- **不拆分**：不做 Planner/ChainExecutor 的职责分离
- **不削弱**：不用 algo_analyze() 替代 LLM 推理
- **兼容性**: tool 和 skill 完全兼容 OpenAI 的 tool 标准和 Anthropic 的 SKILL 标准
  - Tool → OpenAI Function Calling 标准（JSON Schema）
  - Skill → Anthropic Agent Skills 标准（SKILL.md）

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
│  smolagents CodeAgent                                     │
│  ┌────────────────────────────────────────────────────┐   │
│  │  推理循环: Observe → Think → Act → Observe → ...   │   │
│  └────────────────────────────────────────────────────┘   │
│                                                           │
│  工具集:                                                  │
│    ① 80+ 普通工具 (get_kline, analyze_trend, ...)        │
│    ② call_skill 工具 (调用 Skill，返回标准化 SkillReport)  │
│    ③ final_answer 工具 (结束并输出最终结论)                │
│                                                           │
│  自动记录层 (TraceCollector):                              │
│    每次 tool_call → EvalNode 子树                         │
│    每次 call_skill → SkillReport + EvalNode               │
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
│  6. Planner ← LLM #2
│     注入: 人设 + 上下文 + 全量 skill + 全量 tool + 规则
│     输出: [{phase, skill, tools}, ...] 全部阶段一次性规划
└──────────────────────────────────────────────────────
    │
    ▼
阶段 2: _execute_plan() ──── 按 phase 顺序循环 ────
│  for phase in plan.phases:
│  ┌────────────────────────────────────────────────┐
│  │  7. 构建精简 Agent                             │
│  │     只加载当前 phase 的 rules/skill/tools      │
│  │  8. agent.run() ← LLM #3 (单 phase 执行)      │
│  │  9. 错误检测:                                  │
│  │     ├─ 成功 → 继续下一个 phase                 │
│  │     └─ 工具失效 + steps ≤ PLAN_PHASE_FAST_EXIT │
│  │        → 快速退出，返回 LLM #2 决策            │
│  │        → LLM #2: 返回结果 / 提问 / 另选路径    │
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
| Planner 前置 | LLM #2 在阶段 1 完成全部规划，拿到完整上下文 |
| 一次规划多阶段 | LLM #2 一次输出全部 phase，无错直接循环 |
| 错误快速退出 | 工具失效时不跑满 max_steps，快速返回 LLM #2 决策 |
| 精简 Agent | 每个 phase 只加载当前需要的 rules/skill/tools |
| 负面反馈前置 | 在 Planner 之前生效，影响规划决策 |

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

**职责**: 当无固定链路匹配时，用 LLM 规划 Skill 执行方案。

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

**职责**: 盘后回溯验证，更新权重。

**数据来源**: `qd_traces` 表 + `qd_skill_weights` + `qd_factor_weights`

**更新逻辑**:
- Skill 权重: 按单位时间收益率聚合
- 因子权重: 按单位时间收益率聚合（带时间衰减）

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
| `planner.md` | Planner LLM | 选什么 Skill、为什么选、用什么工具 |
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

### 7.1 学习闭环
```
agent.run() → TraceCollector 存库 → 盘后回测 → 权重更新
```

### 7.2 编排闭环
```
Planner 规划 → ChainExecutor 执行 → 结果注入 Agent → 决策输出
```

### 7.3 惩罚闭环
```
用户负面反馈 → 惩罚 trace → 惩罚 tool_chains → 删除低质量链路
```

### 7.4 异步回测闭环
```
T+N 天后 → 自动回测 → 评估决策正确性 → 更新权重
```

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
│   ├── chains.py         # 链路定义
│   ├── executor.py       # 链路执行器
│   ├── evaluator.py      # 链路评估器
│   ├── schema.py         # 数据结构
│   ├── store.py          # 链路存储
│   └── tool_chains.json  # 学习积累的链路
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

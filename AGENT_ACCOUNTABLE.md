# Agent 可追责架构设计

> 最后更新: 2026-06-25
> 状态: 实施中
> 仓库: https://github.com/yuguonet/QuantDinger

## 一、设计原则

### 1. Agent 是执行者, Planner 是工具选择器, Judge 是大脑
- smolagents CodeAgent 保持完整推理-行动-观察循环
- **Planner 只选工具,不判断完成**:每步只决定用什么工具,不决定任务是否完成
- **Judge 总结+纠错+控循环**:LLM #4 看原始数据,提炼结论,判断继续/停止,最终输出结构化结果
- **Agent 只执行,不总结**:final_answer() 返回原始数据 + 一句话结论,不做结构化 JSON 输出
- **不削弱**:不用 algo_analyze() 替代 LLM 推理
- **兼容性**: tool 和 skill 完全兼容 OpenAI 的 tool 标准和 Anthropic 的 SKILL 标准
  - Tool → OpenAI Function Calling 标准(JSON Schema)
  - Skill → Anthropic Agent Skills 标准(SKILL.md)

### 1.1 设计终极目标
- **最大实现可复测**:同样的输入条件能重放 → 才能验证决策对不对 → 才能迭代权重
- **最终目的**:减小系统 bug 或规则不完善带来的亏钱
- 四个闭环各自堵一个漏洞,复杂但必要

### 2. EvalNode 树是审计日志,不是执行引擎
- Agent 执行过程中,每一步自动构建 EvalNode 树
- 树记录"发生了什么",不决定"应该发生什么"
- 执行完 → 树存库 → 盘后回溯验证 → 权重迭代

### 3. 三层追责不变
- **Chain 层**:agent 的整体决策(最终 action/score/direction)
- **Skill 层**:每次 call_skill 的分析报告(标准化 SkillReport)
- **Tool 层**:每次工具调用的入参出参

### 4. 可回测 > 可解释 > 可复现
- 优先保证每个决策能和实际盈亏对比
- 其次保证能追溯"为什么做了这个决定"
- 可复现靠数据快照,不靠砍掉 agent 自适应

## 二、架构总览(4-LLM 架构)

```
用户消息
    │
    ▼
┌──────────────────────────────────────────────────────────┐
│  Intent Analyzer (LLM #1)                                │
│    → domain / verb / noun / strategy / stock_code        │
│                                                          │
│  _try_chain(verb, noun, message, session_id, context, user_id)
│    → tool_chains.json 匹配(仅新格式 plan)→ ChainDef   │
│    → 未命中 → Planner (LLM #2) 必须跑一遍              │
│       ├─ 成功 → 返回 ChainDef                           │
│       └─ 失败 → 返回 None                               │
│    → chain_def 通过 meta 传递给 _execute_plan()         │
│                                                          │
│  ┌─ Step Loop ────────────────────────────────────────┐  │
│  │                                                    │  │
│  │  Planner (LLM #2)                                  │  │
│  │    输入: 用户消息 + Judge 上下文摘要               │  │
│  │    输出: 工具选择(tools/skill/rules)             │  │
│  │    职责: 只选工具,不判断任务是否完成              │  │
│  │                                                    │  │
│  │  Agent (LLM #3)                                    │  │
│  │    smolagents CodeAgent                            │  │
│  │    推理循环: Observe → Think → Act → Observe       │  │
│  │    职责: 只执行工具,返回原始数据                  │  │
│  │    final_answer(): 原始数据 + 一句话结论           │  │
│  │                                                    │  │
│  │  Judge (LLM #4)                                    │  │
│  │    输入: Agent 原始数据 + 前序摘要                 │  │
│  │    输出: 摘要 + 纠错 + continue/stop + next_context│  │
│  │    职责: 总结、纠错、控循环                        │  │
│  │                                                    │  │
│  │  ┌──────────────────────────────────────────────┐  │  │
│  │  │ Planner → Agent → Judge → continue?          │  │  │
│  │  │    ↑                    │                    │  │  │
│  │  │    └── next_context ────┘                    │  │  │
│  │  └──────────────────────────────────────────────┘  │  │
│  └────────────────────────────────────────────────────┘  │
│                                                          │
│  循环结束后: Judge (LLM #4 最终模式)                     │
│    → 读取 checkpoint 全量数据                            │
│    → 输出结构化金融分析 JSON                             │
│    → 如发现数据缺失 → 标记 need_rerun                   │
│                                                          │
│  工具集:                                                  │
│    1 55+ 普通工具 (get_kline, analyze_trend, ...)        │
│    2 read_skill 工具 (加载 SKILL.md 指令)                │
│    3 final_answer 工具 (结束并返回原始数据)               │
│                                                          │
│  自动记录层 (TraceCollector):                              │
│    每次 tool_call → EvalNode 子树(TracedTool 包装)      │
│    最终 answer → Chain 根节点                             │
└──────────────────────────────────────────────────────────┘
    │
    ▼
  EvalNode 树 → 存库 → 盘后回溯 → 权重迭代
```

## 三、三阶段执行流程(§17.2)

### 3.1 流程总览

```
用户消息
    │
    ▼
阶段 1: _prepare_intent() ──────────────────────────
│  0. 负面反馈检测 - 惩罚上一轮 chain
│  1. 快速通道判断 - 正则匹配闲聊(0 LLM 调用)
│  2. 意图分析 ← LLM #1(仅非闲聊时)
│  3. 提取 stock_code
│  4. 创建 TraceCollector
│  5. 上下文拼接(历史摘要 + 领域上下文)
│  6. _try_chain(verb, noun) 获取执行计划                     │
│     ├─ 有 verb/noun → tool_chains.json 匹配(仅新格式 plan)│
│     │   ├─ 命中 → 返回 ChainDef                           │
│     │   └─ 未命中 → Planner (LLM #2) 必须跑一遍          │
│     │       ├─ 成功 → 返回 ChainDef                       │
│     │       └─ 失败 → 返回 None + 日志警告                │
│     └─ 无 verb/noun → Planner 始终执行                    │
│     计划格式:phases 数组,progressive 标记递进关系         │
│     chain_def 通过 meta 传递给 _execute_plan()             │
└──────────────────────────────────────────────────────
    │
    ▼
阶段 2: _execute_plan() ──── Step Loop 循环 ────
│  初始化: judge_context = "", judge_summaries = []
│  for loop_step in range(max_loop_steps):
│  ┌────────────────────────────────────────────────────────┐
│  │  7. Planner (LLM #2): 选工具                          │
│  │     输入: 用户消息 + judge_context (Judge 摘要)        │
│  │     输出: StepResult (tools/skill/rules)               │
│  │     注: 只看 Judge 精炼摘要,不看原始数据              │
│  │                                                        │
│  │  7.1 空工具检查:                                       │
│  │     Planner 无工具可选 + 上一步成功 → break (任务完成) │
│  │                                                        │
│  │  8. Agent (LLM #3): 执行工具                           │
│  │     为当前步骤重建 agent(上下文最小化)               │
│  │     agent.run(step_context) → 原始数据                 │
│  │     final_answer() → 原始数据 + 一句话结论             │
│  │                                                        │
│  │  9. 结果存 checkpoint + all_content                    │
│  │                                                        │
│  │  10. Judge (LLM #4): 总结 + 纠错 + 控循环             │
│  │      输入: Agent 原始数据 + 前序摘要 + 剩余步数        │
│  │      输出: StepJudgeResult                             │
│  │        - summary: 一句话关键结论                       │
│  │        - corrections: 纠错建议 (可选)                  │
│  │        - next_context: 传给下一轮 Planner 的上下文     │
│  │        - continue_loop: true/false                     │
│  │                                                        │
│  │      continue=false → break                            │
│  │      continue=true → next_context 传给下一轮 Planner   │
│  └────────────────────────────────────────────────────────┘
│
│  循环结束后:
│  11. Judge (LLM #4 最终模式): 输出结构化金融 JSON         │
│      读取 checkpoint 全量数据 (all_summaries + all_contents)
│      输出: FinalJudgeResult (output/need_rerun)           │
│  12. 清理 checkpoint                                      │
│  13. 存消息 + 后置学习 + 压缩上下文                      │
│  14. 返回 AgentResult                                     │
└──────────────────────────────────────────────────────────
    │
    ▼
阶段 3: _post_process() ────────────────────────────
│ 10. 存消息到 session
│ 11. DecisionCard(traced 策略时)
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
| 4-LLM 分工 | LLM#1 意图、LLM#2 选工具、LLM#3 执行、LLM#4 总结+纠错+控循环 |
| Planner 只选工具 | 每步只决定用什么工具，不判断任务是否完成 |
| Judge 控循环 | 每步结束后 Judge 看原始数据，决定继续/停止 + 传递递进上下文 |
| Agent 只执行不总结 | final_answer() 返回原始数据 + 一句话结论，不做结构化输出 |
| per-step agent 重建 | 每步重建 agent，只加载当前步骤需要的工具，上下文最小化 |
| 支持 skill 和 tool 两种模式 | skill → 读取 SKILL.md 执行；tool → 直接调用工具 |
| 递进上下文传递 | Judge 的 next_context 传递给下一轮 Planner，解决步骤间依赖 |
| 空工具 = 任务完成 | Planner 无工具可选时自动结束循环，无需 LLM 判断 |
| 负面反馈前置 | 在 Planner 之前生效，影响规划决策 |
| 根节点兜底写入 | 非 traced 策略在阶段 3 后写根节点到 qd_traces，保证回溯验证有数据 |
| 规划失败终止 | Planner 失败返回 StepResult(success=False)，循环立即终止 |

### 3.3 配置项

```bash
# .env 配置
MAX_LOOP_STEPS=10              # Step Loop 最大循环次数
PLAN_PHASE_FAST_EXIT_STEPS=3   # 工具失效快速退出步数阈值
```

## 四、核心组件
### 4.1a Skill Registry - 技能注册表

**职责**: 扫描 `agent/skills/` 目录,自动发现并注册技能。
**插件化**: 新增技能只需往 `skills/` 目录扔文件夹和 SKILL.md 文件,零配置。
**命名规范**: SKILL.md 的 `name` 字段使用 PascalCase(如 `market-screener`),目录名使用下划线(如 `market_screener`)。
### 4.1b Tool Registry - 工具注册表

**职责**: 扫描 `agent/tools/` 目录,自动发现并注册工具。

**发现规则**:
- `tools/` 目录下 `.py` 文件
- 跳过 `_` 开头的文件
- 有 docstring 的公开函数 → 自动注册

**工具定义**: 纯 OpenAI Function Calling 或 Tool Calling 标准
- `name`: 函数名
- `description`: docstring 第一行
- `parameters`: type hints 自动生成

**插件化**: 新增工具只需往 `tools/` 目录扔 `.py` 文件,零配置。

**sandbox 兼容**: 模块级 `from app.data_sources.*` 导入改为函数内延迟导入,避免 agent sandbox 执行时 `NameError`。

### 4.2 TraceCollector - 执行追踪器

**职责**: Agent 执行过程中自动收集信息,构建 EvalNode 树。

**提取策略**: JSON 优先,正则降级。

**层级**:
- Tool 层: 每次 tool_call → EvalNode 子树(由 TracedTool 自动触发 `on_tool_call()`)
- Skill 层: `on_skill_call()` 方法已定义但当前未接入,skill 层数据由 `_post_process` 兜底写入
- Chain 层: 最终 answer → 根节点

### 4.3 Planner - 规划器 (LLM #2)

**职责**: 每步只选工具,不判断任务是否完成。接收 Judge 的精炼摘要作为上下文,不直接看原始数据。

**设计本质**: Planner 是轻量决策器--只回答"下一步用什么工具",不回答"任务做完了没"。任务完成由 Judge 和空工具检查决定。

**失败行为**: 规划失败时返回 `StepResult(success=False)`,循环立即终止。

**注入内容**:
| 内容 | 来源 | 用途 |
|------|------|------|
| persona | `semantics/persona.md` | 告诉 Planner 它是谁 |
| judge_context | Judge 产出的 `next_context` | 上一步关键结论(替代 previous_results) |
| skills | `get_skills_summary_xml()` | 全量 skill 列表+描述 |
| tools | `get_tools_summary_xml()` | 全量 tool 列表+描述 |
| rules | `semantics/planner.md` | 输出格式和选工具规则 |

**输出格式**:
```json
{
  "tools": ["tool_name_1", "tool_name_2"],
  "skill": "skill_name 或 null",
  "rules": "执行指令和顺序",
  "confidence": 0.0-1.0,
  "description": "步骤描述",
  "stocks": ["股票代码或null"],
  "reasoning": "选择理由,30字以内"
}
```

**关键变化**(vs 旧版):
- ~~`phases` 数组~~ → 单步输出(每次只选一步工具)
- ~~`progressive` 字段~~ → 不需要(Judge 负责传递递进上下文)
- ~~`previous_results`~~ → 替换为 `judge_context`(精炼摘要,不是全量数据)
- ~~判断任务完成~~ → 不负责(空工具 = 任务完成,由外层检查)

### 4.3a Judge - 循环控制器 (LLM #4)

**职责**: 每步结束后总结、纠错、控循环。所有步骤结束后输出最终结构化结果。

**设计本质**: 从 Agent 的 `final_answer()` 中拆出的专职裁判。Agent 只执行+返回原始数据,Judge 负责理解、判断、输出。

**两种模式**:
| 模式 | 调用时机 | 输入 | 输出 |
|------|----------|------|------|
| 单步模式 `judge_step()` | 每步结束后 | Agent 原始数据 + 前序摘要 | summary + corrections + next_context + continue/stop |
| 最终模式 `judge_final()` | 循环结束后 | checkpoint 全量数据 | 结构化金融 JSON + need_rerun |

**单步输出格式** (`StepJudgeResult`):
```json
{
  "continue": true/false,
  "summary": "一句话关键结论,30字以内",
  "corrections": null 或 "纠错建议",
  "next_context": "传给下一步的上下文,50字以内",
  "reasoning": "判断理由,20字以内"
}
```

**最终输出格式** (`FinalJudgeResult`):
```json
{
  "output": {
    "action": "buy/sell/hold/skip",
    "score": 0-100,
    "direction": "bullish/bearish/neutral",
    "confidence": "high/medium/low",
    "signal": "一句话信号摘要",
    "factors": [...],
    "analysis": "完整分析文字"
  },
  "need_rerun": false,
  "rerun_hint": ""
}
```

**循环控制规则**:
- 成功且有剩余步骤 → continue=true
- 成功且无剩余步骤 → continue=false
- 失败 → continue=false + corrections 说明原因
- 数据矛盾/缺失 → corrections 指出

**与旧架构对比**:
| 旧架构 | 新架构 |
|--------|--------|
| Agent 内部 `final_answer()` 输出结构化 JSON | Agent 只返回原始数据 + 一句话结论 |
| 安全机制(success 布尔值判断) | Judge 基于原始数据智能判断 |
| `done` 字段(已删除) | Judge 的 `continue` 字段 |
| `previous_results` 全量传入 Planner | Judge `next_context` 精炼摘要传入 |

### 4.4 Intent Analyzer - 意图分析器

**职责**: 分析用户意图,提取 domain/verb/noun/strategy。

**流程**:
1. 快速通道: 正则匹配闲聊(0 LLM 调用)
2. LLM 分析: 仅非闲聊时调用
3. 提取 stock_code: **仅金融领域**(`domain in ("finance", "trading")`)执行 3 级提取(context → 正则 → 中文名解析),写入 session。非金融领域跳过。

**输出**: `IntentResult` 结构体,包含 intent/domain/verb/noun/strategy/confidence。

**注意**: stock_code 提取在 `_prepare_intent()` 步骤3 完成,`_try_chain()` 不再提取,直接从 context 读取。

### 4.5 Evaluator - 评估器

**职责**: 盘后回溯验证,统一驱动权重更新和链路学习。

**两个组件**:
- `evaluator.py`(agent 根目录):在线评估,每次 `agent.run()` 后立即执行,纯规则 <1ms
- `chain/evaluator.py`:离线评估,T+N 验证,盘后定时运行

**数据来源**: `qd_traces` 表 + `qd_skill_weights` + `qd_factor_weights`

**更新逻辑**:
- Skill 权重: 按单位时间收益率聚合(多次 T+N 验证渐进调权)
- 因子权重: 按单位时间收益率聚合(带时间衰减,不同因子半衰期不同)
- 链路学习: 验证正确的链路才写入 tool_chains.json(4 道质量门 + phase 完整性守卫)

**追踪能力**:
- Skill 维度: `qd_skill_weights.return_per_day` 下降 → 该 Skill 在退化
- 因子维度: `qd_factor_weights.win_rate` 下降 → 该因子在失效
- 链路维度: `tool_chains.json.stats.success_rate` 下降 → 该工具组合在失效
- 工具维度: `qd_traces` 中 tool 层节点的 correct 统计 → 哪个工具在出错

> **注意**: 代码中引用 `tool_chains.json` 路径名,实际文件为 `chain/tool_chains.py`(Python 封装模块,内部读写 JSON 数据),两者是封装关系而非简单替换。

## 五、语义文件与 Skill 结构

```
semantics/                      # 语义文件
├── persona.md          # 人设 → Planner + Agent
├── intent.md           # 意图分类规则 → IntentAnalyzer
├── planner.md          # 规划规则 → Planner LLM
└── agent_rules.md      # 执行规则 → Agent instructions(原 guidance.md)

skills/                         # Skill 目录(与 semantics 平级)
└── <name>/
    ├── SKILL.md        # Skill 定义 → call_skill
    └── *.py            # Skill 实现
```

**职责分离**:
| 文件 | 注入到 | 职责 |
|------|--------|------|
| `planner.md` | Planner LLM | 选什么 Skill、为什么选、输出执行计划 |
| `agent_rules.md` | Agent instructions | 执行纪律 + 输出要求(原始数据+一句话结论,最终输出由 Judge 统一处理) |
| `persona.md` | Planner + Agent | 人设 |
| `intent.md` | IntentAnalyzer | 意图分类规则 |

**Skill 目录**:`agent/skills/<name>/SKILL.md`,不在 `semantics/` 下。

## 六、数据库设计

### 6.1 qd_traces - 执行追踪表

```sql
CREATE TABLE qd_traces (
    id SERIAL PRIMARY KEY,
    root_id INTEGER REFERENCES qd_traces(id),
    parent_id INTEGER REFERENCES qd_traces(id),
    layer VARCHAR(16),          -- 'chain' / 'skill' / 'tool'
    name VARCHAR(128),
    step_order INTEGER,
    exec_date DATE,
    stock_code VARCHAR(16),
    stock_name VARCHAR(32),
    score FLOAT,
    direction VARCHAR(16),
    action VARCHAR(16),
    signal TEXT,
    confidence FLOAT,
    timeframe VARCHAR(8),
    factors JSONB,              -- [{name, value, score, weight, status}]
    output_summary JSONB,
    analysis TEXT,
    input_params JSONB,
    tools_called TEXT[],        -- PostgreSQL 数组
    missing_data TEXT[],
    data_source VARCHAR(64),
    status VARCHAR(16),         -- 'ok' / 'missing' / 'failed' / 'skipped' / 'veto'
    error TEXT,
    elapsed_ms FLOAT,
    -- 回溯验证(盘后写入)
    exit_date DATE,
    exit_reason VARCHAR(32),    -- 'take_profit' / 'stop_loss' / 'max_hold' / 'signal_change'
    pnl_pct FLOAT,
    hold_days INTEGER,
    correct BOOLEAN,
    calibration FLOAT DEFAULT 1.0,
    -- 人工介入
    human_reviewed BOOLEAN DEFAULT FALSE,
    human_verdict VARCHAR(64),
    created_at TIMESTAMP DEFAULT NOW()
);
```

### 6.2 qd_factor_weights - 因子权重表

```sql
CREATE TABLE qd_factor_weights (
    id SERIAL PRIMARY KEY,
    skill_name VARCHAR(128),
    factor_name VARCHAR(128),
    weight FLOAT DEFAULT 1.0,
    win_rate FLOAT DEFAULT 0,
    sample_count INTEGER DEFAULT 0,
    decay_half_life INTEGER DEFAULT 30,
    last_updated TIMESTAMP DEFAULT NOW(),
    UNIQUE(skill_name, factor_name)
);
```

### 6.3 qd_skill_weights - Skill 权重表

```sql
CREATE TABLE qd_skill_weights (
    id SERIAL PRIMARY KEY,
    skill_name VARCHAR(128) UNIQUE,
    weight FLOAT DEFAULT 1.0,
    win_rate FLOAT DEFAULT 0,
    avg_pnl_pct FLOAT DEFAULT 0,
    avg_hold_days FLOAT DEFAULT 1,
    return_per_day FLOAT DEFAULT 0,
    sample_count INTEGER DEFAULT 0,
    last_updated TIMESTAMP DEFAULT NOW()
);
```

## 七、四个闭环

四个闭环各自堵一个漏洞,不能去掉任何一个。终极目标:减小系统 bug 或规则不完善带来的亏钱。

### 7.1 记录闭环(数据源)
```
agent.run() → TracedTool.forward() → collector.on_tool_call() → tool 节点
  → agent 结束 → _post_process() → collector.on_agent_finish() → store.save_tree(root) → qd_traces
```
**作用**:记录每一步执行过程,是其他三个闭环的数据源。
**实现**:TracedTool 包装所有工具(`if collector: tools = [TracedTool(t, collector)]`),对 agent 透明。非 traced 策略有兜底写入(`if not meta.get("collector")`)。
**去掉后果**:没有数据,回测和溯源都废了。

### 7.2 T+N 回测闭环(概率验证 + 渐进调权)
```
app 启动 → start_eval_worker() → 等到盘后 15:30
  → auto_evaluate()
    → evaluate_pending() → 取实际行情 → 写回 correct/pnl_pct
    → update_skill_weights()      # 哪个 Skill 权重在下降
    → update_factor_weights()     # 哪个因子权重在下降
```
**作用**:用实际行情客观验证预测方向,多次验证渐进调权(单次对错无意义,看统计)。
**核心指标**:单位时间收益率 = (win_rate × avg_win - loss_rate × avg_loss) / avg_hold_days
**追踪能力**:权重下降 → 识别哪个 Skill/因子在退化。
**去掉后果**:无法区分预测对不对,权重失去客观依据。

### 7.3 用户反馈闭环(加速检测 tool 故障)
```
用户说"不对/垃圾/反了" → _check_negative_feedback()
  → detect_feedback_severity()
  → trace 层: mark_root_wrong() / delete_tree()
  → chain 层: penalize_chain() → success_count 扣减 → 累计后删除
```
**作用**:检测执行层故障(tool 更新、数据源挂了、API 变了)。T+N 回测也能通过统计发现,但需要 N 轮,期间持续亏钱。用户反馈是加速器。
**和 T+N 的关系**:不在同一层--用户反馈检测**执行层**(工具坏了),T+N 检测**预测层**(方向对不对)。
**前提条件**:`_post_evaluate()` 在每轮结束时存 `last_verb`/`last_noun` 到 session,下一轮 `_check_negative_feedback()` 读取。verb/noun 为空时闭环断裂(金融分析场景少见)。
**去掉后果**:tool 坏了要等 N 轮 T+N 才能淘汰,期间持续亏钱。

### 7.4 编排路径学习闭环(省去 LLM #2 重复工作量)
```
agent 结束 → _post_evaluate() → learn_from_execution()
  → verdict=="success" + all_phases_completed → _writeback_chain()
    → 有 chain_def → 3 道质量门 → save_chain_plan()(新格式 plan)
    → 无 chain_def → save_tool_chain()(旧格式 steps)
  → 下次 _try_chain() → get_chain_plan() 命中 → 直接返回 ChainDef
```
**作用**:首次走一遍后,后续直接使用编排路径,省去 LLM #2(Planner 大脑)的重复思考。
**本质**:Chain 编排层是从用户消息到执行入口的便捷路线,是缓存好的思维捷径。
**质量门**:3 道拦截(phase步数>5、工具成功率<50%、旧链已验证),防止学习低质量链路。
**前提条件**:verb/noun 非空(`learn_from_execution` 在 verb/noun 为空时跳过)。
**去掉后果**:每次都从零规划,重复消耗 LLM #2 token。

## 八、工具分类

### 8.1 工具层(80+ 工具,按职责分组)

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

### 8.2 Skill 层(通过 call_skill 调用)

Skill 从 `semantics/skills/*/SKILL.md` 动态加载,包含:
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

# Step Loop 配置
MAX_LOOP_STEPS=10              # 最大循环次数
PLAN_PHASE_FAST_EXIT_STEPS=3   # 工具失效快速退出步数阈值

# 意图分析
INTENT_ANALYSIS_ENABLED=true     # 是否启用意图分析

# 代码执行
CODE_EXECUTION_TIMEOUT=120       # 代码执行超时(秒)
```

### 9.2 语义文件

- `semantics/persona.md` - 人设定义
- `semantics/intent.md` - 意图分类规则
- `semantics/planner.md` - Planner 规则
- `semantics/agent_rules.md` - Agent 执行规则(原 `guidance.md`)
- `skills/*/SKILL.md` - Skill 定义

## 十、已知限制

1. **Planner 输出解析**: 依赖 LLM 输出 JSON 格式,有容错处理但不保证 100% 成功
2. **Judge LLM 调用开销**: 每步多一次 Judge 调用(单步模式),但 prompt 短、输出精炼,token 开销有限
3. **Judge 依赖 LLM 质量**: Judge 的总结和纠错能力取决于 LLM,弱模型可能产出低质量摘要
4. **单步循环**: 当前实现为顺序循环,不支持并行执行
5. **per-step agent 重建**: 每步重建 agent 有性能开销,但保证上下文最小化
6. **need_rerun 未实现**: Judge 最终模式可标记 need_rerun,但自动补跑逻辑尚未实现

## 十一、后续迭代

1. **补跑机制**: Judge 标记 need_rerun 时自动触发新一轮循环
2. **并行步骤执行**: 支持无依赖关系的步骤并行执行
3. **动态 skill 加载**: 根据步骤需要动态加载/卸载 skill
4. **回测引擎**: 完整的回测系统设计(见 §十四)

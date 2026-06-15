# 语义描述重构方案

> 目标：将散落在 7+ 处的描述语义统一到 `agent/semantics/` 目录，实现「改描述只改一处」。

## 一、现状问题

描述语义当前散落在以下位置：

| 位置 | 描述类型 | 文件数 | 问题 |
|------|----------|--------|------|
| `skills/*.py` @skill(description=) | Skill 触发描述 | 15+ | 硬编码在装饰器参数 |
| `skills/*.py` instructions | Skill 详细指令 | 15+ | 与 description 分离，有的在装饰器，有的在类属性 |
| `tools/*.py` @tool(description=) | Tool 功能描述 | 60+ | 每个文件独立硬编码 |
| `domain_registry.py` | 领域描述+指令 | 6 个域 | instructions 大段文本塞在 Python 代码里 |
| `router/routes.py` | 路由描述 | ~15 条 | 与路由逻辑混在一起 |
| `chain/chains.py` | 链路+步骤描述 | 3 链×~10 步 | 描述与注册逻辑混写 |
| `agent_preamble.md` | Agent 人设 | 1 | 这个其实还好，已经是独立文件 |
| `intent_analyzer.py` `_INTENT_PROMPT` | 意图分类规则（100+ 行） | 1 | 分类规则内嵌在代码里，改规则要改代码 |
| `intent_analyzer.py` `_INTENT_TOOL_CATEGORIES` | 意图→工具类别映射 | 1 | 映射关系硬编码 |
| `planner.py` `SKILL_CATALOG` | Skill 目录（给 LLM 看的精简版） | 1 | **第三份** skill 描述（与 skills/*.py、base.py 重复） |
| `planner.py` `_llm_plan()` prompt | 规划器 prompt | 1 | 规则内嵌在代码中 |

**核心矛盾**：
1. **同一概念 3-5 处重复描述**：如"龙虎榜"在 tools/signal_tools.py、tools/market_data_tools.py、skills/hot_money.py、chain/chains.py、planner.py SKILL_CATALOG 各写一遍
2. **描述不一致**：SKILL_CATALOG 写"游资/龙虎榜/主力资金"，chain 写"游资追踪：龙虎榜、主力资金动态、游资席位动向"，skill 写"A股游资追踪师"
3. **改描述要改代码**：意图分类规则在 `_INTENT_PROMPT` 字符串里，改规则 = 改 Python 代码 + 重新部署

## 二、设计原则

### 对标 OpenClaw + Nanobot，适配 QuantDinger 分层

**OpenClaw/Nanobot 的核心模式（值得借鉴）：**
- SKILL.md frontmatter = 单一信源（name + description）
- 渐进式加载：system prompt 只放摘要，body 按需读取
- 声明式元数据：描述与代码逻辑分离

**QuantDinger 的独特架构（必须保留）：**
- **Domain 层**：finance/coding/trading/system 先分域，再路由到 skill/tool
- **Chain 层**：verb+noun 触发多 skill 编排（evaluate+stock → 10 步链路）
- **Planner 层**：LLM 动态选择 skill 组合（非固定链路时）
- **Intent 层**：分类规则 + 快速通道正则 + 上下文压缩

**OpenClaw/Nanobot 没有但 QuantDinger 需要的：**
- 领域级 instructions（finance 和 coding 的工作流完全不同）
- 链路编排描述（chain steps 的顺序和依赖）
- 意图分类语义（哪些关键词 → 哪个 domain/intent）
- 规划器 skill 目录（给 LLM 看的精简版选择列表）

### 六条原则

1. **单一信源**：每个描述只在一个地方定义，其他地方引用
2. **分层加载**：元数据（name+description）始终注入 system prompt；详细指令按需加载
3. **声明式优先**：描述用 YAML 声明，代码只负责加载和使用
4. **渐进式披露**：system prompt 只放轻量摘要，减少 token 浪费
5. **保留分层**：domain → skill → tool 三级结构不变，intent/chain/planner 语义统一管理
6. **向后兼容**：不改变现有 @skill/@tool 装饰器的使用方式，只改变描述的来源

## 三、目标结构

### 设计理念：Domain 是作用域

Domain 不是分组标签，是**能力作用域**——定义「在这个域里，能用哪些 skill 和 tool」。

**为什么需要作用域？**

同一个系统里有完全不同类型的能力：
- `analyze_trend`（分析股票趋势）— 金融域
- `workspace_read_file`（读写文件）— 代码域
- `start_strategy`（启动交易策略）— 交易域
- `create_cron_job`（创建定时任务）— 系统域

如果不做域隔离：
- 用户问「帮我写个脚本」，LLM 看到 79 个 tool 里混着股票分析工具，容易误调
- system prompt 塞满所有 tool description，浪费 token
- 金融域的 skill 指令和代码域的 skill 指令互相干扰

做了域隔离后：
- domain=coding → 只加载代码域的 tools（workspace_*），不加载金融工具
- domain=finance → 只加载金融域的 tools（analyze_*、get_*），不加载代码工具
- system prompt 精简，LLM 选择更准确

```
用户消息 → Intent 分析 → 识别出 domain
                              ↓
                    domain 决定作用域：
                    ├── 可用 skills（哪些 skill 可被调用）
                    ├── 可用 tools（哪些 tool 可被使用）
                    └── instructions（该域的工作流指引）
                              ↓
                    Chain/Planner 在作用域内编排
```

类似 Nanobot 的 `skills/finance/SKILL.md` = 一个域的能力集合。
区别：Nanobot 用目录分域，QuantDinger 用 YAML 声明域。

### 文件结构

```
backend_api_python/app/agent/
├── semantics/                    ← 语义描述统一目录（单一信源）
│   ├── __init__.py               ← 加载器
│   │
│   ├── persona.yaml              ← Agent 人设
│   │
│   ├── domains.yaml              ← 领域作用域定义
│   │   finance:
│   │     description: "..."
│   │     instructions: "..."     ← 该域的工作流指引
│   │     skills: [technical_agent, screening_agent, ...]  ← 可用 skill 列表
│   │     tools: [analyze_trend, get_kline, ...]            ← 可用 tool 列表
│   │     tool_categories: [技术分析, 行情数据, ...]         ← 按类别过滤
│   │
│   ├── intent.yaml               ← 意图分类规则 + 意图→领域映射
│   ├── routes.yaml               ← 路由 utterances + 元数据
│   ├── chains.yaml               ← 链路编排 + steps
│   ├── planner.yaml              ← 规划器 skill 目录 + prompt
│   │
│   ├── skills.yaml               ← 所有能力（16 个 skill，平铺）
│   │   skills:
│   │     - name: technical_agent
│   │       domain: [finance]     ← 属于哪个域的作用域
│   │       description: "..."
│   │       tools: [analyze_trend, ...]
│   │       priority: 9
│   │
│   └── tools.yaml                ← 所有能力（79 个 tool，按 category 分组）
│       categories:
│         技术分析:
│           - name: analyze_trend
│             domain: [finance]   ← 属于哪个域的作用域
│             description: "..."
│
├── skills/                       ← 代码逻辑（@skill 装饰器从 semantics 加载描述）
├── tools/                        ← 代码逻辑（@tool 装饰器从 semantics 加载描述）
├── domain_registry.py            ← 从 domains.yaml 加载
├── intent_analyzer.py            ← 从 intent.yaml 加载
├── planner.py                    ← 从 planner.yaml 加载
├── router/routes.py              ← 从 routes.yaml 加载
├── chain/chains.py               ← 从 chains.yaml 加载
└── agent.py                      ← _build_instructions() 从 semantics 组装
```

### 作用域过滤流程

```
用户: "帮我写个脚本读取CSV"
    ↓
Intent 识别 domain=coding
    ↓
domains.yaml → coding 作用域
    ├── skills: [data_agent]
    ├── tools: [workspace_read_file, workspace_write_file, workspace_exec_script, ...]
    └── instructions: "你是一个专业的代码工程师..."
    ↓
system prompt 只注入 coding 域的 tools（~14 个），而非全部 79 个
    ↓
LLM 在精简的能力集内选择，不会误调 analyze_trend、get_kline 等金融工具
```

**对比无作用域：**
```
用户: "帮我写个脚本读取CSV"
    ↓
system prompt 注入全部 79 个 tools + 16 个 skills
    ↓
LLM 在 79 个 tool 里找「读文件」，可能误选 get_kline（也是"读取数据"）
```

### 与 OpenClaw/Nanobot 的架构对比

```
OpenClaw:                          QuantDinger:
  system prompt                      ┌─ 理解层 ──────────────────────┐
  ├─ persona                         │ Domain（领域上下文）            │
  ├─ <available_skills>              │  → domains.yaml               │  ← 同一次 LLM 调用
  │   └─ skill × N                   │ Intent（意图+参数提取）         │     同时产出
  │       (name + description)       │  → intent.yaml                │
  └─ (按需 read SKILL.md body)       └───────────────────────────────┘
                                            ↓ 驱动
Nanobot:                            ┌─ 执行层 ──────────────────────┐
  system prompt                     │ Chain/Planner（编排）           │
  ├─ AGENTS.md                      │  → chains.yaml / planner.yaml │
  ├─ <skills> XML                   │ Skill（分析单元）               │
    └─ skill × N                    │  → skills/*.yaml              │
        (name + description)        │ Tool（原子操作）                │
                                    │  → tools/*.yaml               │
                                    └───────────────────────────────┘
```

**核心差异**：
- OpenClaw/Nanobot 是「平铺式」skill 列表，没有 Domain/Intent 分层
- QuantDinger 有**理解层**（Domain+Intent）和**执行层**（Chain/Planner→Skill→Tool）的分离
- 理解层的语义（domains.yaml、intent.yaml）和执行层的语义（chains/skills/tools/*.yaml）性质不同：
  - 理解层 = **分类规则 + 上下文注入**（给 LLM 判断「你是谁、用户要什么」）
  - 执行层 = **能力描述 + 编排逻辑**（给 LLM 判断「怎么做、用什么做」）

### YAML 文件分层对应

```
semantics/
├── persona.yaml          ← 全局（不区分理解/执行）
│
│  ── 理解层 YAML ──
├── domains.yaml          ← Domain：领域 instructions + tool_categories
├── intent.yaml           ← Intent：分类规则 + 快速通道 + 意图→工具映射
│
│  ── 执行层 YAML ──
├── chains.yaml           ← Chain：链路编排 + steps
├── planner.yaml          ← Planner：skill 精简目录 + 规划 prompt
├── skills/*.yaml         ← Skill：description + instructions + triggers
└── tools/*.yaml          ← Tool：description + category + layer + domain
```

## 四、YAML 文件格式定义

### 4.1 persona.yaml（Agent 人设）

```yaml
# 从 agent_preamble.md 迁移
role: "有20年经验的A股分析师和量化程序员"
identity: "QuantDinger 是你编写的量化分析助手"
mission: "基于真实数据为用户提供专业、客观、可执行的金融分析/交易建议/代码的迭代维护升级改进"
```

### 4.2 domains.yaml（领域作用域）

```yaml
domains:
  finance:
    name: "金融分析"
    description: "股票分析、行情查看、选股筛选、策略回测"
    # 作用域：只加载这些 skill 和 tool
    skills:
      - technical_agent
      - indicator_agent
      - screening_agent
      - short_term_screener
      - post_market_screener
      - eod_screener
      - bb_screener
      - market_data_agent
      - hot_money_tracker
      - intelligence_agent
      - lockup_watcher
      - bear_researcher
      - bull_researcher
      - backtest_agent
    tool_categories: [技术分析, 行情数据, 选股筛选, K线图表, 指标策略, 情报搜索, 回测, 龙虎榜/热榜, 板块分析]
    instructions: |
      你是一个专业的 A 股量化分析助手。
      ...

  coding:
    name: "代码开发"
    description: "代码编写、调试、重构、项目分析"
    skills: [data_agent]
    tool_categories: [工作区]
    instructions: |
      你是一个专业的代码工程师。
      ...

  trading:
    name: "交易执行"
    description: "策略启停、持仓管理、交易记录"
    skills: [trading_agent]
    tool_categories: [交易执行]
    instructions: |
      你是一个量化交易执行助手。
      ...

  system:
    name: "系统管理"
    description: "定时提醒、定时任务管理、系统设置"
    skills: []
    tool_categories: [定时任务]
    instructions: |
      你是一个系统管理助手。
      ...

  chat:
    name: "闲聊"
    description: "通用对话、问候、闲聊"
    skills: []
    tool_categories: []
    instructions: ""
```

### 4.3 skills/*.yaml（Skill 描述）

```yaml
# skills/technical.yaml
name: technical_agent
description: "技术面综合分析（趋势/量价/均线/指标/形态/筹码/动量/突破）"
priority: 9
default_weight: 1.0
tools:
  - analyze_trend
  - get_indicator_snapshot
  - analyze_volume
  - detect_candlestick_patterns
  - analyze_chip_distribution

# 触发条件（给 LLM 判断何时调用）
triggers:
  - "技术分析"
  - "趋势判断"
  - "均线/MACD/RSI/KDJ/布林带"
  - "量价关系"
  - "K线形态"
  - "筹码分布"

# 详细指令（按需加载，不注入 system prompt）
instructions: |
  纯算法技术面 + 动量分析。
  1. 趋势判断：MA排列 + MACD方向
  2. 量价分析：量比 + 放缩量 + 量价背离
  3. 指标共振：RSI + KDJ + BOLL
  4. 形态识别：K线形态 + 突破确认
  5. 筹码分析：获利比例 + 集中度
  6. 动量追踪：连续涨跌 + 加速度

# 标准化输出（是否需要 JSON 输出）
standard_output: true
```

### 4.4 tools/*.yaml（Tool 描述）

```yaml
# tools/data.yaml — 按 category 分组
tools:
  search_stock_by_name:
    description: "根据中文名称或关键词搜索股票代码。支持模糊匹配。"
    category: "名称查询"
    layer: "数据层"
    domain: ["finance"]

  get_stock_quote:
    description: "获取股票或交易对的实时行情（最新价、涨跌幅、成交量、换手率、量比、PE/PB等）。"
    category: "行情数据"
    layer: "数据层"
    domain: ["finance"]

  get_kline:
    description: "获取股票/交易对的K线数据（OHLCV）。支持多周期：1m/5m/15m/30m/1H/4H/1D/1W。"
    category: "行情数据"
    layer: "数据层"
    domain: ["finance"]

  # ... 60+ 个 tool
```

### 4.5 routes.yaml（路由描述）

```yaml
routes:
  - name: "finance/stock_analysis"
    description: "个股分析、技术面分析、行情研判"
    verbs: ["分析", "看", "研判", "走势"]
    nouns: ["股票", "个股", "行情"]

  - name: "finance/chart"
    description: "K线图表、走势可视化"
    verbs: ["画", "展示", "渲染"]
    nouns: ["K线", "图表", "走势图"]

  # ... 15 条路由
```

### 4.6 intent.yaml（意图分类规则）

```yaml
# 从 intent_analyzer.py 的 _INTENT_PROMPT 和 _INTENT_TOOL_CATEGORIES 迁移
# 改规则只改这个文件，不用改代码

# LLM 分类 prompt 模板
classifier_prompt: |
  你是意图分类器。分析用户消息，输出 JSON。

  ## 用户消息
  {message}

  ## 上轮对话摘要（如有）
  {context_summary}

  ## 输出格式（只输出 JSON，不要其他内容）
  ```json
  {{
    "domain": "finance | coding | trading | system | unknown | chat",
    "intent": "...",
    "verb": "...",
    "noun": "...",
    "stock_code": "6位代码或空",
    "stock_name": "股票名称或空",
    "confidence": 0.0-1.0,
    "context_summary": "本轮对话摘要，30字以内"
  }}
  ```

# 分类规则（结构化，可被代码和 LLM 共用）
rules:
  - match: "有股票名称或代码"
    result: { domain: finance, verb: analyze, noun: stock }
  - match: "怎么样/能买吗/跌了/涨了 + 股票"
    result: { domain: finance, intent: stock_analysis }
  - match: "K线/图表"
    result: { domain: finance, intent: chart_view, verb: view, noun: chart }
  - match: "涨停/大盘/板块"
    result: { domain: finance, intent: market_scan, verb: scan, noun: market }
  - match: "选股/推荐"
    result: { domain: finance, intent: screener, verb: filter, noun: stock }
  - match: "回测"
    result: { domain: finance, intent: backtest, verb: backtest, noun: stock }
  - match: "资金流向/主力/北向"
    result: { domain: finance, intent: fund_flow, verb: query, noun: fund_flow }
  - match: "MACD/RSI/指标"
    result: { domain: finance, intent: indicator, verb: query, noun: indicator }
  - match: "买入/卖出/持仓/启停策略"
    result: { domain: trading, intent: trading, verb: execute, noun: trading }
  - match: "市盈率/市值/基本面"
    result: { domain: finance, intent: stock_info, verb: query, noun: stock }
  - match: "概念/术语"
    result: { domain: finance, intent: concept_explain, verb: explain, noun: concept }
  - match: "设置提醒/定时/闹钟/倒计时"
    result: { domain: system, intent: reminder, verb: remind }
  - match: "查看/管理/取消定时任务"
    result: { domain: system, intent: cron_manage }
  - match: "修改系统设置/配置"
    result: { domain: system, intent: settings, verb: configure }
  - match: "闲聊/问候"
    result: { domain: chat }

# 快速通道正则（无需 LLM，直接匹配）
quick_patterns:
  greeting: '^(你好|hi|hello|嗨|hey|在吗|哈喽|嘿|yo)[\s\?\?\.\,\!\~\。\，\！\？\…]*$'
  farewell: '^(再见|拜拜|bye|88|886|晚安|回见)[\s\?\?\.\,\!\~\。\，\！\？\…]*$'
  thanks: '^(谢谢|感谢|多谢|thanks|thank\s*you|thx|3q)[\s\?\?\.\,\!\~\。\，\！\？\…]*$'

# 意图 → 工具类别映射
intent_tool_categories:
  stock_analysis: [名称查询, 行情数据, 技术分析, 情报搜索]
  chart_view: [名称查询, 行情数据, K线图表]
  market_scan: [行情数据, 龙虎榜/热榜]
  screener: [名称查询, 选股, 指标策略]
  backtest: [名称查询, 行情数据, 回测, 指标策略]
  fund_flow: [名称查询, 行情数据]
  indicator: [名称查询, 行情数据, 技术分析, 指标策略]
  trading: [交易, 指标策略]
  stock_info: [名称查询, 行情数据]
  concept_explain: []
  reminder: []
  cron_manage: []
  settings: []
  unknown: []
  code_modify: [工作区]
  code_create: [工作区]
  project_scan: []
```

### 4.7 planner.yaml（规划器 Skill 目录 + 规划规则）

```yaml
# 从 planner.py 的 SKILL_CATALOG 和 _llm_plan() prompt 迁移
# 注意：这里只放一份 skill 描述，semantics/skills/*.yaml 是详细版

# Skill 目录（精简版，给规划器 LLM 选择用）
# 详细描述在 semantics/skills/*.yaml，这里只保留 name + 一句话
skill_catalog:
  - name: technical_agent
    summary: "技术面+动量综合（趋势/均线/量价/形态/筹码/突破/择时）。地基，大多数场景必须包含。"
  - name: indicator_agent
    summary: "用户自定义指标信号（指标IDE策略执行）。指标验证。"
  - name: intelligence_agent
    summary: "情报+政策分析（新闻/事件/舆情/公告/政策面）。信息面分析。"
  - name: hot_money_tracker
    summary: "游资/龙虎榜/主力资金。短线资金面。"
  - name: lockup_watcher
    summary: "解禁/减持/质押。供给端风险。"
  - name: market_data_agent
    summary: "行情+概念+资金（指数/板块/概念热度/涨停池/资金流向）。市场概览。"
  - name: screening_agent
    summary: "条件选股/指标筛选。选股场景。"
  - name: backtest_agent
    summary: "策略回测。验证历史绩效。"
  - name: bull_researcher
    summary: "多头论据构建。多空辩论看涨方。"
  - name: bear_researcher
    summary: "空头论据构建。多空辩论看跌方。"
  - name: data_agent
    summary: "数据工程/脚本执行。数据处理。"
  - name: trading_agent
    summary: "交易执行/策略启停。交易场景。"

# 兼容别名
aliases:
  momentum_tracker: technical_agent
  policy_analyst: intelligence_agent
  concept_tracker: market_data_agent

# 规划器 prompt 模板
planner_prompt: |
  你是量化分析规划器。根据用户问题，选择需要执行的分析技能。

  ## 用户问题
  {query}{stock_info}{intent_info}

  ## 可用技能
  {skill_catalog}

  ## 规则
  - 从上述技能中选择 1~5 个，按执行顺序排列
  - 大多数场景必须包含 technical_agent（技术面地基）
  - 不要选择与问题无关的技能
  - 如果涉及股票但未提供代码，在 stocks 中列出需要的代码

  ## 输出格式（只输出 JSON，不要其他文字）
  ```json
  {
    "steps": [
      {"agent": "technical_agent"},
      {"agent": "intelligence_agent"}
    ],
    "stocks": ["600519"],
    "reasoning": "选择理由（50字以内）"
  }
  ```
```

### 4.8 chains.yaml（链路定义）

```yaml
chains:
  full_analysis:
    name: "完整分析链路"
    description: "游资追踪→解禁监控→情报/政策→技术面/动量→指标信号→选股验证→行情/概念/资金→回测→多空辩论"
    steps:
      - skill: "hot_money_tracker"
        description: "游资追踪：龙虎榜、主力资金动态、游资席位动向（短线定价核心）"
      - skill: "lockup_watcher"
        description: "解禁监控：限售股解禁、减持预警、质押风险（供给端风险）"
      # ...

  stock_screening:
    name: "选股链路"
    description: "条件选股→技术验证→情报过滤→综合排序"
    steps:
      # ...

  market_overview:
    name: "市场概览链路"
    description: "大盘指数→板块排名→涨停池→龙虎榜→资金流向"
    steps:
      # ...
```

## 五、加载器设计

### 5.1 semantics/__init__.py

```python
"""
Semantics Loader — 语义描述统一加载入口。

使用方式：
    from app.agent.semantics import get_skill_meta, get_tool_meta, get_domain_meta

    # 获取 skill 描述
    meta = get_skill_meta("technical_agent")
    print(meta.description)       # "技术面综合分析..."
    print(meta.triggers)          # ["技术分析", "趋势判断", ...]
    print(meta.instructions)      # 完整指令（按需加载）

    # 获取 tool 描述
    meta = get_tool_meta("get_kline")
    print(meta.description)       # "获取K线数据..."

    # 获取 domain 描述
    meta = get_domain_meta("finance")
    print(meta.description)       # "股票分析、行情查看..."
"""
import yaml
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Optional

_SEMANTICS_DIR = Path(__file__).parent

# ── 缓存 ──
_skill_metas: Dict[str, "SkillMeta"] = {}
_tool_metas: Dict[str, "ToolMeta"] = {}
_domain_metas: Dict[str, "DomainMeta"] = {}
_route_metas: List["RouteMeta"] = []
_chain_metas: Dict[str, "ChainMeta"] = {}
_persona: Optional["PersonaMeta"] = None
_loaded = False


def _load_yaml(filename: str) -> dict:
    path = _SEMANTICS_DIR / filename
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@dataclass
class PersonaMeta:
    role: str = ""
    identity: str = ""
    mission: str = ""

@dataclass
class DomainMeta:
    name: str = ""
    description: str = ""
    instructions: str = ""
    tools: Optional[List[str]] = None
    tool_categories: Optional[List[str]] = None

@dataclass
class SkillMeta:
    name: str = ""
    description: str = ""
    priority: int = 0
    default_weight: float = 1.0
    tools: List[str] = field(default_factory=list)
    triggers: List[str] = field(default_factory=list)
    instructions: str = ""
    standard_output: bool = False

@dataclass
class ToolMeta:
    name: str = ""
    description: str = ""
    category: str = ""
    layer: str = ""
    domain: List[str] = field(default_factory=list)

@dataclass
class RouteMeta:
    name: str = ""
    description: str = ""
    verbs: List[str] = field(default_factory=list)
    nouns: List[str] = field(default_factory=list)

@dataclass
class ChainMeta:
    name: str = ""
    description: str = ""
    steps: List[dict] = field(default_factory=list)


def load_semantics():
    """加载所有语义描述文件（幂等，只加载一次）。"""
    global _loaded, _persona
    if _loaded:
        return
    _loaded = True

    # persona
    p = _load_yaml("persona.yaml")
    _persona = PersonaMeta(**p)

    # domains
    for name, cfg in _load_yaml("domains.yaml").get("domains", {}).items():
        _domain_metas[name] = DomainMeta(name=name, **cfg)

    # skills
    skills_dir = _SEMANTICS_DIR / "skills"
    if skills_dir.exists():
        for f in skills_dir.glob("*.yaml"):
            data = _load_yaml(f"skills/{f.name}")
            if data.get("name"):
                _skill_metas[data["name"]] = SkillMeta(**data)

    # tools
    tools_dir = _SEMANTICS_DIR / "tools"
    if tools_dir.exists():
        for f in tools_dir.glob("*.yaml"):
            data = _load_yaml(f"tools/{f.name}")
            for tname, tcfg in data.get("tools", {}).items():
                _tool_metas[tname] = ToolMeta(name=tname, **tcfg)

    # routes
    for r in _load_yaml("routes.yaml").get("routes", []):
        _route_metas.append(RouteMeta(**r))

    # chains
    for name, cfg in _load_yaml("chains.yaml").get("chains", {}).items():
        _chain_metas[name] = ChainMeta(name=name, **cfg)


def get_persona() -> PersonaMeta:
    load_semantics()
    return _persona

def get_domain_meta(name: str) -> Optional[DomainMeta]:
    load_semantics()
    return _domain_metas.get(name)

def get_skill_meta(name: str) -> Optional[SkillMeta]:
    load_semantics()
    return _skill_metas.get(name)

def get_all_skill_metas() -> Dict[str, SkillMeta]:
    load_semantics()
    return dict(_skill_metas)

def get_tool_meta(name: str) -> Optional[ToolMeta]:
    load_semantics()
    return _tool_metas.get(name)

def get_all_tool_metas() -> Dict[str, ToolMeta]:
    load_semantics()
    return dict(_tool_metas)

def get_route_metas() -> List[RouteMeta]:
    load_semantics()
    return list(_route_metas)

def get_chain_meta(name: str) -> Optional[ChainMeta]:
    load_semantics()
    return _chain_metas.get(name)

def get_skills_summary_xml() -> str:
    """生成 skills 摘要 XML，用于注入 system prompt（轻量，只有 name+description）。"""
    load_semantics()
    lines = ["<skills>"]
    for name, meta in sorted(_skill_metas.items(), key=lambda x: x[1].priority, reverse=True):
        lines.append(f'  <skill name="{name}">')
        lines.append(f'    <description>{meta.description}</description>')
        lines.append(f'  </skill>')
    lines.append("</skills>")
    return "\n".join(lines)

def get_tools_summary_xml() -> str:
    """生成 tools 摘要 XML，按 category 分组。"""
    load_semantics()
    by_cat: Dict[str, List[ToolMeta]] = {}
    for meta in _tool_metas.values():
        by_cat.setdefault(meta.category or "其他", []).append(meta)

    lines = ["<tools>"]
    for cat, tools in sorted(by_cat.items()):
        lines.append(f'  <category name="{cat}">')
        for t in sorted(tools, key=lambda x: x.name):
            lines.append(f'    <tool name="{t.name}">{t.description}</tool>')
        lines.append(f'  </category>')
    lines.append("</tools>")
    return "\n".join(lines)
```

## 六、代码层改造

### 6.1 skills/registry.py 改造

**改造前**（硬编码）：
```python
@skill(
    name="technical_agent",
    description="技术面综合分析（趋势/量价/均线/指标/形态/筹码/动量/突破）",
    tools=["analyze_trend", "get_indicator_snapshot", ...],
    instructions="纯算法技术面 + 动量分析...",
    priority=9,
)
class TechnicalSkill:
    pass
```

**改造后**（从 semantics 加载）：
```python
from app.agent.semantics import get_skill_meta

_meta = get_skill_meta("technical_agent")

@skill(
    name=_meta.name,
    description=_meta.description,
    tools=_meta.tools,
    instructions=_meta.instructions,
    priority=_meta.priority,
    default_weight=_meta.default_weight,
)
class TechnicalSkill:
    pass
```

**或者更简洁 —— 装饰器自动加载**：
```python
# registry.py 的 @skill 装饰器增加 auto_load 参数
@skill("technical_agent", auto_load=True)
class TechnicalSkill:
    pass

# 装饰器内部：
def skill(name: str, auto_load: bool = False, **overrides):
    def decorator(cls):
        if auto_load:
            from app.agent.semantics import get_skill_meta
            meta = get_skill_meta(name)
            if meta:
                # 用 semantics 的值作为默认，overrides 可覆盖
                final = {**meta.__dict__, **overrides}
            else:
                raise ValueError(f"Semantics not found for skill: {name}")
        else:
            final = {"name": name, **overrides}
        # ... 原有逻辑
    return decorator
```

### 6.2 tools/registry.py 改造

**改造前**：
```python
@tool(
    description="获取K线数据（OHLCV）...",
    category="行情数据",
    layer="数据层",
    domain=["finance"],
)
def get_kline(...):
    ...
```

**改造后**：
```python
from app.agent.semantics import get_tool_meta

_meta = get_tool_meta("get_kline")

@tool(
    description=_meta.description,
    category=_meta.category,
    layer=_meta.layer,
    domain=_meta.domain,
)
def get_kline(...):
    ...
```

**或者装饰器自动加载**：
```python
@tool(name="get_kline", auto_load=True)
def get_kline(...):
    ...
```

### 6.3 intent_analyzer.py 改造

**改造前**：`_INTENT_PROMPT` 100+ 行字符串内嵌 + `_INTENT_TOOL_CATEGORIES` 硬编码
**改造后**：

```python
from app.agent.semantics import get_intent_meta

_intent_meta = None

def _get_intent_meta():
    global _intent_meta
    if _intent_meta is None:
        _intent_meta = get_intent_meta()
    return _intent_meta

# 分类 prompt 从 semantics 加载
_INTENT_PROMPT = _get_intent_meta().classifier_prompt

# 快速通道正则从 semantics 加载
_quick_patterns = _get_intent_meta().quick_patterns
_GREETING_RE = re.compile(_quick_patterns["greeting"], re.IGNORECASE)
_FAREWELL_RE = re.compile(_quick_patterns["farewell"], re.IGNORECASE)
_THANKS_RE  = re.compile(_quick_patterns["thanks"], re.IGNORECASE)

# 意图→工具类别映射从 semantics 加载
_INTENT_TOOL_CATEGORIES = _get_intent_meta().intent_tool_categories
```

### 6.4 planner.py 改造

**改造前**：`SKILL_CATALOG` 硬编码 + `_llm_plan()` prompt 内嵌
**改造后**：

```python
from app.agent.semantics import get_planner_meta

_planner_meta = None

def _get_planner_meta():
    global _planner_meta
    if _planner_meta is None:
        _planner_meta = get_planner_meta()
    return _planner_meta

# SKILL_CATALOG 从 semantics 动态生成
def _build_skill_catalog() -> str:
    meta = _get_planner_meta()
    lines = ["可用技能（从下列中选择 1~5 个，按执行顺序排列）：\n"]
    for i, s in enumerate(meta.skill_catalog, 1):
        lines.append(f"{i}. {s['name']} — {s['summary']}")
    if meta.aliases:
        lines.append(f"\n兼容别名：{', '.join(f'{k}→{v}' for k,v in meta.aliases.items())}")
    return "\n".join(lines)

SKILL_CATALOG = _build_skill_catalog()

# prompt 模板从 semantics 加载
def _llm_plan(self, query, stock_code, stock_name, verb, noun):
    prompt_template = _get_planner_meta().planner_prompt
    prompt = prompt_template.format(
        query=query, stock_info=stock_info, intent_info=intent_info,
        skill_catalog=SKILL_CATALOG,
    )
    ...
```

### 6.5 domain_registry.py 改造

**改造前**：`init_builtin_domains()` 里 6 个域的大段 description + instructions 硬编码
**改造后**：

```python
from app.agent.semantics import get_domain_meta, load_semantics

def init_builtin_domains():
    global _initialized
    if _initialized:
        return
    _initialized = True
    load_semantics()

    from app.agent.semantics import _domain_metas
    for name, meta in _domain_metas.items():
        register_domain(DomainConfig(
            name=meta.name,
            description=meta.description,
            instructions=meta.instructions,
            tool_categories=meta.tool_categories,
        ))
```

### 6.6 agent.py 改造

**改造前**：`_build_instructions()` 拼接 8 个 section，各自从不同来源取值
**改造后**：

```python
from app.agent.semantics import get_persona, get_skills_summary_xml, get_tools_summary_xml

def _build_instructions(...):
    persona = get_persona()

    parts = [
        f"你是{persona.role}。{persona.identity}。{persona.mission}",
        get_skills_summary_xml(),    # 轻量摘要，不是全文
        get_tools_summary_xml(),     # 按 category 分组的摘要
    ]

    if domain_instructions:
        parts.append(f"## 当前领域: {domain}\n\n{domain_instructions}")

    if skill_instructions:
        parts.append(f"## 激活的交易技能\n\n{skill_instructions}")

    return "\n\n".join(parts)
```

### 6.7 router/routes.py 改造

```python
from app.agent.semantics import get_route_metas

def build_default_routes():
    routes = []
    for meta in get_route_metas():
        routes.append(Route(
            name=meta.name,
            description=meta.description,  # 从 semantics 加载
            ...
        ))
    return routes
```

### 6.8 chain/chains.py 改造

```python
from app.agent.semantics import get_chain_meta

# 注册链路时从 semantics 加载描述
_full = get_chain_meta("full_analysis")
register_chain(Chain(
    name="full_analysis",
    description=_full.description,  # 从 semantics 加载
    steps=[
        Step(skill=s["skill"], description=s["description"])
        for s in _full.steps
    ],
))
```

## 七、分层加载策略（借鉴 Nanobot，适配理解层+执行层分离）

### 加载时机

```
┌─────────────────────────────────────────────────────────────────┐
│ System Prompt（始终注入）                                        │
│                                                                  │
│ 1. persona (persona.yaml)                                       │
│ 2. domain_instructions（根据当前 domain 从 domains.yaml 取对应段）│ ← 理解层产出
│ 3. skills_summary XML（所有 skill 的 name+description 轻量摘要） │
│ 4. tools_summary XML（按 category 分组的 tool 摘要）             │
│ 5. 其他配置 section                                             │
└─────────────────────────────────────────────────────────────────┘

┌─ 理解层（首次 LLM 调用，同时完成）──────────────────────────────┐
│                                                                  │
│ Intent 分析：                                                    │
│   - intent.yaml classifier_prompt + rules → domain/verb/noun    │
│   - 快速通道正则 → 闲聊直接回复（不进执行层）                    │
│   - 上下文压缩 → context_summary 传给下轮                       │
│                                                                  │
│ Domain 路由：                                                    │
│   - 根据 domain → 注入对应 instructions + tool_categories        │
│   - 过滤可用工具集                                               │
└──────────────────────────────────────────────────────────────────┘

┌─ 执行层（后续 LLM 调用）────────────────────────────────────────┐
│                                                                  │
│ Chain/Planner 阶段：                                             │
│   - chains.yaml → verb+noun 匹配 → 执行固定链路                  │
│   - planner.yaml skill_catalog → LLM 动态选择 skill 组合         │
│                                                                  │
│ Skill 执行：                                                     │
│   - skills/*.yaml instructions → 按需加载详细工作流              │
│   - 调用 Tool 完成原子操作                                       │
└──────────────────────────────────────────────────────────────────┘
```

### token 节省估算

| 部分 | 改造前 | 改造后 | 节省 |
|------|--------|--------|------|
| skill instructions（15 个） | ~8000-12000 tokens（全量注入） | ~800-1200 tokens（摘要） | ~85-90% |
| domain instructions | ~2000 tokens（按域注入，不变） | ~2000 tokens | 0%（已经按需） |
| tool descriptions | ~3000 tokens（全量注入） | ~1500 tokens（按 category 分组摘要） | ~50% |
| planner SKILL_CATALOG | ~500 tokens（每次规划都带） | 从 YAML 加载，不占 prompt | 100% |
| **总计** | ~13500-17500 tokens | ~4300-5500 tokens | **~65-70%** |

## 八、迁移步骤

### Phase 1：建立 semantics 目录（不改现有代码）
1. 创建 `agent/semantics/` 目录 + `__init__.py` 加载器
2. 从现有代码中提取描述，写入 YAML 文件：
   - `persona.yaml` ← agent_preamble.md
   - `domains.yaml` ← domain_registry.py
   - `intent.yaml` ← intent_analyzer.py `_INTENT_PROMPT` + `_INTENT_TOOL_CATEGORIES`
   - `routes.yaml` ← router/routes.py
   - `chains.yaml` ← chain/chains.py
   - `planner.yaml` ← planner.py `SKILL_CATALOG`
   - `skills/*.yaml` ← 各 skill 文件的 description + instructions
   - `tools/*.yaml` ← 各 tool 文件的 description
3. 编写单元测试：验证所有 YAML 加载正确、覆盖所有现有 skill/tool/domain

### Phase 2：逐模块切换（最小改动，每步可独立测试）
1. `domain_registry.py` → `init_builtin_domains()` 从 domains.yaml 加载
2. `intent_analyzer.py` → `_INTENT_PROMPT` 从 intent.yaml 加载，`_INTENT_TOOL_CATEGORIES` 从 intent.yaml 加载
3. `planner.py` → `SKILL_CATALOG` 从 planner.yaml 加载，`_llm_plan()` prompt 从 planner.yaml 加载
4. `skills/*.py` → @skill 改为从 semantics 加载 description/instructions
5. `tools/*.py` → @tool 改为从 semantics 加载 description
6. `router/routes.py` → 从 routes.yaml 加载 description
7. `chain/chains.py` → 从 chains.yaml 加载 description + step descriptions
8. `agent.py` → `_build_instructions()` 简化，从 semantics 组装

### Phase 3：分层加载优化
1. system prompt 只注入 skills_summary XML + tools_summary XML
2. 详细 instructions 改为按需加载（Agent 触发 skill 后才读取）
3. 性能测试和 token 对比

### Phase 4：清理
1. 删除各文件中的硬编码描述（Python 代码里只保留逻辑）
2. 删除 planner.py 的 SKILL_CATALOG（已在 planner.yaml）
3. 删除 intent_analyzer.py 的 _INTENT_PROMPT（已在 intent.yaml）
4. 更新文档
5. 回归测试

## 九、风险与缓解

| 风险 | 缓解 |
|------|------|
| YAML 文件过多，维护成本 | 按 category 分组，每个 YAML 管 5-10 个 tool |
| 加载延迟（启动时读文件） | 内存缓存 + 懒加载，只在首次访问时读取 |
| YAML 格式错误导致启动失败 | 启动时校验 + 详细错误信息 |
| 描述与代码不同步 | CI 中加一致性检查脚本 |
| 改造量大 | 分 Phase 进行，每 Phase 可独立上线 |

## 十、收益总结

| 指标 | 改造前 | 改造后 |
|------|--------|--------|
| 描述修改点 | 7+ 处 | **1 处**（对应 YAML） |
| system prompt token（skill 部分） | ~10000 tokens | ~1000 tokens |
| 新增 skill/tool 成本 | 在 .py 中硬编码 + 注册 | 写 YAML + 简单注册 |
| 描述一致性 | 靠人工同步 | **单一信源，自动一致** |
| 可读性 | 散落在代码中 | **集中在一个目录** |

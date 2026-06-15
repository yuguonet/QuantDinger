# 语义描述重构方案（v3 — 三段加载 + SKILL.md 格式）

> 日期: 2026-06-15（v3 重写，对齐 §15 + OpenClaw/Nanobot 三段加载模式）
> 前置: §15 Domain 解耦重构已实施（domain→纯标签，strategy→路由，tags→多值）
> 目标: 将散落在 7+ 处的描述语义统一到 `agent/semantics/` 目录，实现「改描述只改一处」+「按需加载」。
>
> 核心参考：OpenClaw / Nanobot 的两阶段加载模式，扩展为三段加载。
> - 第一段：system prompt 只放能力清单（名字+分类索引）
> - 第二段：意图分析后注入领域指令+相关描述
> - 第三段：call_skill 时按需读取 SKILL.md 完整指令

## 一、现状问题（与 v1 相同，但 §15 已解决路由问题）

描述语义当前散落在以下位置：

| 位置 | 描述类型 | 文件数 | 问题 |
|------|----------|--------|------|
| `skills/*.py` @skill(description=) | Skill 触发描述 | 15+ | 硬编码在装饰器参数 |
| `skills/*.py` instructions | Skill 详细指令 | 15+ | 与 description 分离 |
| `tools/*.py` @tool(description=) | Tool 功能描述 | 60+ | 每个文件独立硬编码 |
| `domain_registry.py` | 领域描述+指令 | 6 个域 | instructions 大段文本塞在 Python 代码里 |
| `router/routes.py` | 路由描述 | ~15 条 | 与路由逻辑混在一起 |
| `chain/chains.py` | 链路+步骤描述 | 3 链×~10 步 | 描述与注册逻辑混写 |
| `agent_preamble.md` | Agent 人设 | 1 | 已是独立文件，还好 |
| `intent_analyzer.py` `_INTENT_PROMPT` | 意图分类规则（100+ 行） | 1 | 分类规则内嵌在代码里 |
| `intent_analyzer.py` `_INTENT_TOOL_CATEGORIES` | 意图→工具类别映射 | 1 | 映射关系硬编码 |

> ⚠️ `planner.py` 已在 AGENT_ACCOUNTABLE.md §11.3 Phase 1 中删除，不再存在。

**§15 已解决的问题**（本方案不再重复）：
- ✅ domain 从三合一降级为纯标签
- ✅ strategy 显式配置替代 domain 路由
- ✅ @skill/@tool 支持 tags 多值
- ✅ TraceCollector/DecisionCard 用 strategy 路由

**本方案要解决的问题**：
- ❌ 同一概念 3-5 处重复描述
- ❌ 描述不一致（SKILL_CATALOG vs chain vs skill 各写一遍）
- ❌ 改描述要改代码（意图分类规则在 Python 字符串里）

## 二、设计原则

### 与 §15 的关系

§15 解耦了**路由**（domain→strategy），本方案解耦了**描述**（代码→YAML）。
两者正交，互不依赖，但组合后效果叠加：

```
§15 前: domain = 路由 + 标签 + 描述注入（三合一）
§15 后: strategy = 路由, domain = 标签 + 描述注入（二合一）
本方案: strategy = 路由, tags = 标签, YAML = 描述注入（完全分离）
```

### 六条原则

1. **单一信源**：每个描述只在一个地方定义，其他地方引用
2. **三段加载**：system prompt 只放能力清单，领域指令按请求注入，Skill 完整指令按需加载
3. **声明式优先**：描述用 YAML/SKILL.md 声明，代码只负责加载和使用
4. **token 预算控制**：每段有明确的 token 上限，不允许某段过重
5. **保留分层**：tags → skill → tool 三级结构不变
6. **向后兼容**：不改变 @skill/@tool 装饰器的使用方式，只改变描述的来源

### 核心概念映射（§15 后）

| 概念 | §15 前 | §15 后 | 本方案 |
|------|--------|--------|--------|
| 路由决策 | domain | strategy | strategy（不变） |
| 能力标签 | domain | tags | tags（不变） |
| 指令注入 | domain_instructions | domain_instructions | YAML → 按 tags 组合 |
| 工具过滤 | domain 过滤 | tags 过滤 | tags 过滤（不变） |
| 描述来源 | Python 硬编码 | Python 硬编码 | **YAML 单一信源** |

## 三、目标结构

### 文件结构

```
backend_api_python/app/agent/
├── semantics/                    ← 语义描述统一目录（单一信源）
│   ├── __init__.py               ← 加载器
│   │
│   ├── persona.yaml              ← Agent 人设
│   ├── domains.yaml              ← 领域描述 + instructions
│   ├── intent.yaml               ← 意图分类规则 + 意图→工具映射
│   ├── routes.yaml               ← 路由 utterances + 元数据
│   ├── chains.yaml               ← 链路编排 + steps
│   │
│   └── skills/                   ← 每个 skill 一个 SKILL.md（Markdown + YAML frontmatter）
│       ├── technical_agent/
│       │   └── SKILL.md
│       ├── indicator_agent/
│       │   └── SKILL.md
│       ├── intelligence_agent/
│       │   └── SKILL.md
│       └── ...（15 个）
│
├── skills/                       ← 代码逻辑（@skill 从 SKILL.md 加载描述）
├── tools/                        ← 代码逻辑（@tool 的 tags 从 semantics 加载，description 保留代码中）
├── domain_registry.py            ← 从 domains.yaml 加载
├── intent_analyzer.py            ← 从 intent.yaml 加载
└── agent.py                      ← 三段加载：_build_instructions() + _prepare() + CallSkillTool
```

> ⚠️ `planner.py` 已在 AGENT_ACCOUNTABLE.md §11.3 Phase 1 中删除，不再需要 `planner.yaml`。
>
> **文件格式决策**：采用 SKILL.md（Markdown + YAML frontmatter），与 OpenClaw/Nanobot 对齐。
> - frontmatter 存元数据（name, description, tags, tools, priority, default_weight）
> - Markdown body 存完整 instructions
> - 每个 skill 独立目录，便于 git 管理和独立修改

### 与 §15 tags 的配合

YAML 中用 `tags` 替代旧的 `domain`：

```yaml
# skills/technical.yaml
name: technical_agent
tags: [finance, technical]     # §15: 多值标签
description: "技术面综合分析"

# tools/data.yaml
tools:
  get_kline:
    tags: [finance]            # §15: tags 替代 domain
    description: "获取K线数据"
```

加载器自动将 YAML 中的 tags 注入到 @skill/@tool 装饰器。

## 四、YAML 文件格式定义

### 4.1 persona.yaml

```yaml
role: "有20年经验的A股分析师和量化程序员"
identity: "QuantDinger 是你编写的量化分析助手"
mission: "基于真实数据为用户提供专业、客观、可执行的金融分析/交易建议/代码的迭代维护升级改进"
```

### 4.2 domains.yaml

```yaml
domains:
  finance:
    name: "金融分析"
    description: "股票分析、行情查看、选股筛选、策略回测"
    instructions: |
      你是一个专业的 A 股量化分析助手。
      ## 工作流程
      1. **理解需求** — 明确用户要分析什么
      2. **规划任务** — 复杂任务用 todowrite 拆解步骤
      3. **数据收集** — 获取行情、指标、新闻等数据
      4. **分析执行** — 技术分析、策略回测、选股筛选
      5. **结果呈现** — 用图表展示，给出明确建议和风险提示
    tool_categories: [技术分析, 行情数据, 选股筛选, K线图表, 指标策略, 情报搜索, 回测, 龙虎榜/热榜, 板块分析]

  coding:
    name: "代码开发"
    description: "代码编写、调试、重构、项目分析"
    instructions: |
      你是一个专业的代码工程师，精通 Python/JavaScript/TypeScript/Vue 等技术栈。
      ## 工作流程
      1. **理解阶段** — 用 workspace_read_file 阅读相关代码
      2. **规划阶段** — 用 todowrite 拆解步骤
      3. **修改阶段** — 用 workspace_edit_file 精准修改
      4. **验证阶段** — 用 code_lint 检查风格
    tool_categories: [工作区]

  trading:
    name: "交易执行"
    description: "策略启停、持仓管理、交易记录"
    instructions: |
      你是一个量化交易执行助手。
      ## 安全原则
      • 任何交易操作前必须确认
      • 大额操作需二次确认
    tool_categories: [交易执行]

  system:
    name: "系统管理"
    description: "定时提醒、定时任务管理、系统设置"
    instructions: |
      你是一个系统管理助手。
    tool_categories: [定时任务]

  chat:
    name: "闲聊"
    description: "通用对话、问候、闲聊"
    instructions: ""
    tool_categories: []
```

### 4.3 skills/{name}/SKILL.md（Markdown + YAML frontmatter）

> 采用与 OpenClaw/Nanobot 相同的 SKILL.md 格式。
> frontmatter 存元数据（供第一段/第二段加载），Markdown body 存完整 instructions（供第三段加载）。

```markdown
---
name: technical_agent
tags: [finance, technical]
description: 技术面综合分析（趋势/量价/均线/指标/形态/筹码/动量/突破）
priority: 9
default_weight: 1.0
standard_output: true
tools:
  - analyze_trend
  - get_indicator_snapshot
  - analyze_volume
  - detect_candlestick_patterns
  - analyze_chip_distribution
---

# 技术面综合分析

纯算法技术面 + 动量分析。

## 分析流程
1. **趋势判断** — MA排列 + MACD方向
2. **量价分析** — 量比 + 放缩量 + 量价背离
3. **指标共振** — RSI + KDJ + BOLL
4. **形态识别** — K线形态 + 突破确认
5. **筹码分析** — 获利比例 + 集中度
6. **动量追踪** — 连续涨跌 + 加速度

## 输出要求
- 每个维度独立评分（0-100）
- 给出综合 direction（bullish/bearish/neutral）
- 缺失数据标注 missing，不猜测
```

**frontmatter 字段说明**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| name | string | ✅ | 技能注册名 |
| tags | list | ✅ | 多值标签（§15） |
| description | string | ✅ | 一句话描述（≤100字，用于第二段摘要） |
| priority | int | ❌ | 优先级（默认 5） |
| default_weight | float | ❌ | 出厂权重（默认 1.0） |
| standard_output | bool | ❌ | 是否需要 JSON 标准化输出 |
| tools | list | ❌ | 依赖的工具名列表 |

**Markdown body**：完整 instructions，只在第三段（call_skill 时）加载。

### 4.4 tools/*.yaml — 只存元数据，description 保留在 @tool 装饰器

> **设计决策**：工具描述不抽到 YAML。原因：
> 1. 80+ 工具的 description 和代码逻辑紧耦合，拆出去反而双份维护
> 2. 工具名本身就是最强语义（`get_kline` > "获取K线数据"），description 是辅助
> 3. `@tool` 装饰器的 description 已经够短（1行），不需要优化
>
> YAML 只记录工具的**分类元数据**（tags、category、layer），供过滤和分组用。

```yaml
# tools/data.yaml — 只存元数据，不存 description
tools:
  search_stock_by_name:
    tags: [finance]
    category: "名称查询"
    layer: "数据层"

  get_kline:
    tags: [finance]
    category: "行情数据"
    layer: "数据层"

  get_realtime_quote:
    tags: [finance]
    category: "行情数据"
    layer: "数据层"
```

### 4.5 intent.yaml

```yaml
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

rules:
  - match: "有股票名称或代码"
    result: { domain: finance, verb: analyze, noun: stock }
  - match: "怎么样/能买吗/跌了/涨了 + 股票"
    result: { domain: finance, intent: stock_analysis }
  - match: "K线/图表"
    result: { domain: finance, intent: chart_view }
  - match: "涨停/大盘/板块"
    result: { domain: finance, intent: market_scan }
  - match: "选股/推荐"
    result: { domain: finance, intent: screener }
  - match: "回测"
    result: { domain: finance, intent: backtest }
  - match: "资金流向/主力/北向"
    result: { domain: finance, intent: fund_flow }
  - match: "MACD/RSI/指标"
    result: { domain: finance, intent: indicator }
  - match: "买入/卖出/持仓/启停策略"
    result: { domain: trading, intent: trading }
  - match: "设置提醒/定时/闹钟"
    result: { domain: system, intent: reminder }
  - match: "闲聊/问候"
    result: { domain: chat }

quick_patterns:
  greeting: '^(你好|hi|hello|嗨|hey|在吗|哈喽|嘿|yo)[\s\?\?\.\,\!\~\。\，\！\？\…]*$'
  farewell: '^(再见|拜拜|bye|88|886|晚安|回见)[\s\?\?\.\,\!\~\。\，\！\？\…]*$'
  thanks: '^(谢谢|感谢|多谢|thanks|thank\s*you|thx|3q)[\s\?\?\.\,\!\~\。\，\！\？\…]*$'

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
  reminder: []
  cron_manage: []
  settings: []
  unknown: []
  code_modify: [工作区]
  code_create: [工作区]
  project_scan: []
```

### 4.7 chains.yaml

```yaml
chains:
  evaluate+stock:
    name: "股票综合评估"
    description: "游资追踪→解禁监控→情报/政策→技术面/动量→指标信号→选股验证→行情/概念/资金→回测→多空辩论"
    trigger_verbs: [analyze, evaluate]
    trigger_nouns: [stock]
    steps:
      - skill: hot_money_tracker
        order: 1
        description: "游资追踪：龙虎榜、主力资金动态"
        required: false
      - skill: lockup_watcher
        order: 2
        description: "解禁监控：限售股解禁、减持预警"
        required: false
      - skill: intelligence_agent
        order: 3
        description: "情报+政策分析"
        required: false
      - skill: technical_agent
        order: 4
        description: "技术面+动量综合判断"
        required: true
      - skill: indicator_agent
        order: 5
        description: "用户指标信号验证"
        required: false
      - skill: screening_agent
        order: 6
        description: "选股验证"
        required: false
      - skill: market_data_agent
        order: 7
        description: "行情+概念+资金流向"
        required: false
      - skill: backtest_agent
        order: 8
        description: "策略回测验证"
        required: false
      - skill: bull_researcher
        order: 9
        description: "多头论证"
        required: false
      - skill: bear_researcher
        order: 10
        description: "空头反驳"
        required: false

  screen+stock:
    name: "选股筛选"
    trigger_verbs: [filter, screen]
    trigger_nouns: [stock, screener]
    steps:
      - skill: screening_agent
        order: 1
        required: true
      - skill: technical_agent
        order: 2
        required: false
      - skill: intelligence_agent
        order: 3
        required: false

  scan+market:
    name: "市场全景扫描"
    trigger_verbs: [view, analyze, scan]
    trigger_nouns: [market]
    steps:
      - skill: market_data_agent
        order: 1
        required: true
      - skill: screening_agent
        order: 2
        required: false
      - skill: market_data_agent
        order: 3
        required: false
```

### 4.8 routes.yaml

```yaml
routes:
  - name: "finance/stock_analysis"
    description: "个股分析、技术面分析"
    verbs: [analyze, evaluate]
    nouns: [stock]

  - name: "finance/chart"
    description: "K线图表、走势可视化"
    verbs: [view]
    nouns: [chart]

  - name: "finance/market_scan"
    description: "大盘、板块、涨停池"
    verbs: [scan, view]
    nouns: [market]

  - name: "finance/screener"
    description: "选股筛选"
    verbs: [filter, screen]
    nouns: [stock, screener]

  - name: "finance/backtest"
    description: "策略回测"
    verbs: [backtest]
    nouns: [stock]
```

## 五、加载器设计

### 5.1 semantics/__init__.py

```python
"""
Semantics Loader — 语义描述统一加载入口（v2，对齐 §15）。

§15 后的职责分离：
  - strategy = 路由决策（IntentAnalyzer 计算）
  - tags = 能力标签（@skill/@tool 注册）
  - semantics = 描述来源（YAML 单一信源）

本模块只管「描述从哪来」，不管「路由怎么走」。
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
_intent_meta: Optional["IntentMeta"] = None
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
    tool_categories: Optional[List[str]] = None

@dataclass
class SkillMeta:
    name: str = ""
    description: str = ""
    tags: List[str] = field(default_factory=list)       # §15: 多值标签
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
    tags: List[str] = field(default_factory=list)       # §15: 多值标签
    category: str = ""
    layer: str = ""

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
    trigger_verbs: List[str] = field(default_factory=list)
    trigger_nouns: List[str] = field(default_factory=list)
    steps: List[dict] = field(default_factory=list)

@dataclass
class IntentMeta:
    classifier_prompt: str = ""
    rules: List[dict] = field(default_factory=list)
    quick_patterns: Dict[str, str] = field(default_factory=dict)
    intent_tool_categories: Dict[str, List[str]] = field(default_factory=dict)


def load_semantics():
    """加载所有语义描述文件（幂等，只加载一次）。"""
    global _loaded, _persona, _intent_meta
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

    # intent
    intent_data = _load_yaml("intent.yaml")
    _intent_meta = IntentMeta(
        classifier_prompt=intent_data.get("classifier_prompt", ""),
        rules=intent_data.get("rules", []),
        quick_patterns=intent_data.get("quick_patterns", {}),
        intent_tool_categories=intent_data.get("intent_tool_categories", {}),
    )


# ── 公开接口 ──

def get_persona() -> PersonaMeta:
    load_semantics()
    return _persona

def get_domain_meta(name: str) -> Optional[DomainMeta]:
    load_semantics()
    return _domain_metas.get(name)

def get_all_domain_metas() -> Dict[str, DomainMeta]:
    load_semantics()
    return dict(_domain_metas)

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

def get_intent_meta() -> IntentMeta:
    load_semantics()
    return _intent_meta


def get_skills_summary_xml() -> str:
    """生成 skills 摘要 XML（轻量，只有 name+description+tags）。"""
    load_semantics()
    lines = ["<skills>"]
    for name, meta in sorted(_skill_metas.items(), key=lambda x: x[1].priority, reverse=True):
        tags_str = ",".join(meta.tags) if meta.tags else ""
        lines.append(f'  <skill name="{name}" tags="{tags_str}">')
        lines.append(f'    <description>{meta.description}</description>')
        lines.append(f'  </skill>')
    lines.append("</skills>")
    return "\n".join(lines)

def get_tools_summary_xml(domain: str = "") -> str:
    """生成 tools 摘要 XML，按 category 分组。可选按 domain 过滤。"""
    load_semantics()
    by_cat: Dict[str, List[ToolMeta]] = {}
    for meta in _tool_metas.values():
        # §15: 用 tags 过滤（tags 优先，降级到无过滤）
        if domain and meta.tags and domain not in meta.tags:
            continue
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

### 6.1 skills/registry.py — @skill 从 semantics 加载

```python
# 改造前
@skill(
    name="technical_agent",
    description="技术面综合分析...",
    tools=["analyze_trend", ...],
    instructions="纯算法技术面...",
    priority=9,
    tags=["finance", "technical"],
)
class TechnicalSkill:
    pass

# 改造后 — 方式 A：装饰器自动加载
@skill("technical_agent", auto_load=True)
class TechnicalSkill:
    pass

# 改造后 — 方式 B：显式引用（更清晰）
from app.agent.semantics import get_skill_meta
_meta = get_skill_meta("technical_agent")

@skill(
    name=_meta.name,
    description=_meta.description,
    tools=_meta.tools,
    instructions=_meta.instructions,
    priority=_meta.priority,
    tags=_meta.tags,               # §15: 从 YAML 加载 tags
    default_weight=_meta.default_weight,
)
class TechnicalSkill:
    pass
```

### 6.2 tools/registry.py — @tool 的 tags 从 semantics 加载，description 保留代码中

```python
# 改造前
@tool(description="获取K线数据...", category="行情数据", tags=["finance"])
def get_kline(...): ...

# 改造后 — tags 从 YAML 加载，description 保持在代码中
from app.agent.semantics import get_tool_meta
_meta = get_tool_meta("get_kline")

@tool(
    description="获取K线数据（OHLCV），支持多周期",  # 保持在代码中，不从 YAML 读
    category=_meta.category if _meta else "行情数据",
    layer=_meta.layer if _meta else "数据层",
    tags=_meta.tags if _meta else ["finance"],       # §15: 从 YAML 加载 tags
)
def get_kline(...): ...
```

### 6.3 intent_analyzer.py — 从 intent.yaml 加载

```python
# 改造前：_INTENT_PROMPT 100+ 行内嵌 + _INTENT_TOOL_CATEGORIES 硬编码

# 改造后
from app.agent.semantics import get_intent_meta

_intent_meta = get_intent_meta()
_INTENT_PROMPT = _intent_meta.classifier_prompt
_INTENT_TOOL_CATEGORIES = _intent_meta.intent_tool_categories

# 快速通道正则从 YAML 加载
_quick_patterns = _intent_meta.quick_patterns
_GREETING_RE = re.compile(_quick_patterns["greeting"], re.IGNORECASE)
_FAREWELL_RE = re.compile(_quick_patterns["farewell"], re.IGNORECASE)
_THANKS_RE  = re.compile(_quick_patterns["thanks"], re.IGNORECASE)
```

### 6.4 domain_registry.py — 从 domains.yaml 加载

```python
# 改造前：init_builtin_domains() 里 6 个域的大段硬编码

# 改造后
from app.agent.semantics import load_semantics, get_all_domain_metas

def init_builtin_domains():
    global _initialized
    if _initialized:
        return
    _initialized = True
    load_semantics()

    for name, meta in get_all_domain_metas().items():
        register_domain(DomainConfig(
            name=meta.name,
            description=meta.description,
            instructions=meta.instructions,
            tool_categories=meta.tool_categories,
        ))
```

### 6.5 chain/chains.py — 从 chains.yaml 加载

```python
# 改造前：链路定义硬编码在 Python 代码中

# 改造后
from app.agent.semantics import get_chain_meta

for chain_id in ["evaluate+stock", "screen+stock", "scan+market"]:
    meta = get_chain_meta(chain_id)
    if meta:
        register_chain(ChainDef(
            chain_id=chain_id,
            name=meta.name,
            description=meta.description,
            trigger_verbs=meta.trigger_verbs,
            trigger_nouns=meta.trigger_nouns,
            steps=[ChainStep(
                name=s["skill"],
                agent=s["skill"],
                order=s["order"],
                description=s.get("description", ""),
                required=s.get("required", False),
            ) for s in sorted(meta.steps, key=lambda x: x["order"])],
        ))
```

### 6.6 agent.py — _build_instructions() 简化

```python
from app.agent.semantics import get_persona, get_skills_summary_xml, get_tools_summary_xml

def _build_instructions(...):
    persona = get_persona()
    parts = [f"你是{persona.role}。{persona.identity}"]

    # skills 摘要（轻量，~500 token）
    parts.append(get_skills_summary_xml())

    # tools 摘要（按 category 分组，按 domain 过滤）
    parts.append(get_tools_summary_xml(domain=domain))

    # 领域指令（从 domains.yaml）
    if domain_instructions:
        parts.append(f"## 当前领域: {domain}\n\n{domain_instructions}")

    # §15: strategy 路由的 JSON 格式指令（保持不变）
    if strategy == "traced":
        parts.append(finance_json_section)

    return "\n\n".join(parts)
```

## 七、分层加载策略

### token 节省估算

| 部分 | 改造前 | 改造后 | 节省 |
|------|--------|--------|------|
| skill instructions（15 个） | ~8000-12000 tokens（全量注入） | ~800-1200 tokens（摘要） | ~85-90% |
| domain instructions | ~2000 tokens（按域注入） | ~2000 tokens | 0% |
| tool descriptions | ~3000 tokens（全量注入） | ~1500 tokens（按 category 摘要） | ~50% |
| intent rules | ~300 tokens | 从 YAML 加载 | 100% |
| **总计** | ~13300-17300 tokens | ~4300-5500 tokens | **~65-70%** |

### 加载时序

```
进程启动
  → load_semantics() 读取所有 YAML（~50ms，一次性）
  → 缓存到内存

每次请求
  → get_skills_summary_xml() 从缓存生成（<1ms）
  → get_tools_summary_xml(domain) 从缓存生成（<1ms）
  → 注入 system prompt

Agent 调用 skill
  → get_skill_meta(name).instructions 从缓存读取（<1ms）
  → 注入 skill prompt
```

## 八、迁移步骤

### Phase 1：建立 semantics 目录（不改现有代码）
1. 创建 `agent/semantics/` 目录 + `__init__.py` 加载器
2. 从现有代码提取描述，写入 YAML 文件
3. 单元测试：验证 YAML 加载正确、覆盖所有 skill/tool/domain

### Phase 2：逐模块切换（每步可独立测试）
1. `domain_registry.py` → 从 domains.yaml 加载
2. `intent_analyzer.py` → 从 intent.yaml 加载
3. `skills/*.py` → @skill 从 semantics 加载
4. `tools/*.py` → @tool 的 tags 从 semantics 加载
5. `chain/chains.py` → 从 chains.yaml 加载
6. `agent.py` → _build_instructions() 简化

### Phase 3：分层加载优化
1. system prompt 只注入摘要 XML
2. 详细 instructions 按需加载
3. 性能测试和 token 对比

### Phase 4：清理
1. 删除 Python 代码中的硬编码描述
2. 更新文档
3. 回归测试

## 九、与 §15 的集成检查清单

- [ ] YAML 中用 `tags` 替代 `domain`
- [ ] 加载器将 YAML tags 注入 @skill/@tool
- [ ] `effective_tags` 逻辑不变（tags 优先，降级到 domain）
- [ ] strategy 路由逻辑不变（IntentAnalyzer 计算）
- [ ] domain 仅用于 instructions 注入（从 domains.yaml 加载）
- [ ] tool 过滤用 tags（build() 中 effective_tags 逻辑不变）

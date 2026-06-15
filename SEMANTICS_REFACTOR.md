# 语义描述重构方案（v4 — 去 domain，两段加载 + SKILL.md 格式）

> 日期: 2026-06-15（v4 重写，基于 v3 删除 domains.yaml，对齐 Nanobot 两段加载模式）
> 前置: §15 Domain 解耦重构已实施（domain→纯标签，strategy→路由，tags→多值）
> 目标: 将散落在 7+ 处的描述语义统一到 `agent/semantics/` 目录，实现「改描述只改一处」+「按需加载」。
>
> 核心参考：OpenClaw / Nanobot 的两阶段加载模式。
> - 第一段：system prompt 只放能力清单（名字+分类索引）+ 通用行为规范
> - 第二段：call_skill 时按需读取 SKILL.md 完整指令（含领域特定指令）
>
> v4 变更：删除 domains.yaml，其内容拆分到 persona.yaml（通用指令）+ SKILL.md body（领域指令）。
> domain 概念彻底移除，chain 触发靠 verb+noun 匹配，不再依赖 domain 路由。

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
- ❌ domain 概念残留（domains.yaml），与 §15 解耦目标矛盾

## 二、设计原则

### 与 §15 的关系

§15 解耦了**路由**（domain→strategy），本方案解耦了**描述**（代码→YAML）。
两者正交，互不依赖，但组合后效果叠加：

```
§15 前: domain = 路由 + 标签 + 描述注入（三合一）
§15 后: strategy = 路由, domain = 标签 + 描述注入（二合一）
本方案: strategy = 路由, tags = 标签, YAML = 描述注入（完全分离）
         ↑ domain 概念彻底移除
```

### 六条原则

1. **单一信源**：每个描述只在一个地方定义，其他地方引用
2. **两段加载**：system prompt 只放能力清单+通用规范，Skill 完整指令按需加载
3. **声明式优先**：描述用 YAML/SKILL.md 声明，代码只负责加载和使用
4. **token 预算控制**：每段有明确的 token 上限，不允许某段过重
5. **保留分层**：tags → skill → tool 三级结构不变
6. **向后兼容**：不改变 @skill/@tool 装饰器的使用方式，只改变描述的来源

### 核心概念映射（v4）

| 概念 | §15 前 | §15 后 | 本方案（v4） |
|------|--------|--------|------------|
| 路由决策 | domain | strategy | strategy（不变） |
| 能力标签 | domain | tags | tags（不变） |
| 指令注入 | domain_instructions | domain_instructions | **persona.yaml（通用）+ SKILL.md body（领域）** |
| 工具过滤 | domain 过滤 | tags 过滤 | tags 过滤（不变） |
| 描述来源 | Python 硬编码 | Python 硬编码 | **YAML 单一信源** |
| domain 概念 | 三合一 | 纯标签+指令 | **彻底移除** |

## 三、目标结构

### 文件结构

```
backend_api_python/app/agent/
├── semantics/                    ← 语义描述统一目录（单一信源）
│   ├── __init__.py               ← 加载器
│   │
│   ├── persona.yaml              ← Agent 人设 + 通用行为规范（吸收原 domains.yaml 通用指令）
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
├── intent_analyzer.py            ← 从 intent.yaml 加载
└── agent.py                      ← 两段加载：_build_instructions() + CallSkillTool
```

> ⚠️ `planner.py` 已在 AGENT_ACCOUNTABLE.md §11.3 Phase 1 中删除，不再需要 `planner.yaml`。
> ⚠️ `domain_registry.py` 已删除，domain 概念移除。
>
> **文件格式决策**：采用 SKILL.md（Markdown + YAML frontmatter），与 OpenClaw/Nanobot 对齐。
> - frontmatter 存元数据（name, description, tags, tools, priority, default_weight）
> - Markdown body 存完整 instructions（含原 domain 的领域特定指令）
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

### 4.1 persona.yaml（扩展版，吸收原 domains.yaml 通用指令）

```yaml
# Agent 人设
role: "有20年经验的A股分析师和量化程序员"
identity: "QuantDinger 是你编写的量化分析助手"
mission: "基于真实数据为用户提供专业、客观、可执行的金融分析/交易建议/代码的迭代维护升级改进"

# 通用行为规范（原 domains.yaml 中各域共享的指令）
behaviors:
  workflow:
    - "理解需求 — 明确用户要做什么"
    - "规划任务 — 复杂任务用 todowrite 拆解步骤"
    - "执行任务 — 调用合适的 skill 和 tool"
    - "结果呈现 — 用图表展示，给出明确建议"
  safety:
    - "任何交易操作前必须确认"
    - "大额操作需二次确认"
    - "缺失数据标注 missing，不猜测"
  coding:
    - "用 workspace_read_file 阅读相关代码"
    - "用 workspace_edit_file 精准修改"
    - "用 code_lint 检查风格"
```

> **设计决策**：persona.yaml 只放跨领域的通用规范。领域特定指令（如"你是A股量化分析师"、"用专业术语"）放到各 SKILL.md body 中，第二段加载时自然带入。

### 4.2 skills/{name}/SKILL.md（Markdown + YAML frontmatter）

> 采用与 OpenClaw/Nanobot 相同的 SKILL.md 格式。
> frontmatter 存元数据（供第一段加载），Markdown body 存完整 instructions（供第二段加载，含领域特定指令）。

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

你是一个专业的 A 股量化技术分析师，精通技术面分析方法论。

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
| description | string | ✅ | 一句话描述（≤100字，用于第一段摘要） |
| priority | int | ❌ | 优先级（默认 5） |
| default_weight | float | ❌ | 出厂权重（默认 1.0） |
| standard_output | bool | ❌ | 是否需要 JSON 标准化输出 |
| tools | list | ❌ | 依赖的工具名列表 |

**Markdown body**：完整 instructions，只在第二段（call_skill 时）加载。
**body 中包含领域特定指令**（如"你是A股量化分析师"），无需单独的 domains.yaml。

### 4.3 tools/*.yaml — 只存元数据，description 保留在 @tool 装饰器

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

### 4.4 intent.yaml

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
    result: { verb: analyze, noun: stock }
  - match: "怎么样/能买吗/跌了/涨了 + 股票"
    result: { intent: stock_analysis }
  - match: "K线/图表"
    result: { intent: chart_view }
  - match: "涨停/大盘/板块"
    result: { intent: market_scan }
  - match: "选股/推荐"
    result: { intent: screener }
  - match: "回测"
    result: { intent: backtest }
  - match: "资金流向/主力/北向"
    result: { intent: fund_flow }
  - match: "MACD/RSI/指标"
    result: { intent: indicator }
  - match: "买入/卖出/持仓/启停策略"
    result: { intent: trading }
  - match: "设置提醒/定时/闹钟"
    result: { intent: reminder }
  - match: "闲聊/问候"
    result: { intent: chat }

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

> **v4 变更**：classifier_prompt 中移除了 `domain` 字段。IntentAnalyzer 只输出 verb/noun/intent，不再输出 domain。路由由 strategy 决定，不依赖 domain。

### 4.5 chains.yaml

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

### 4.6 routes.yaml

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
Semantics Loader — 语义描述统一加载入口（v4，对齐 Nanobot 两段加载）。

v4 变更：
  - 删除 DomainMeta 和 domains.yaml 加载
  - persona.yaml 扩展，吸收通用行为规范
  - 领域特定指令移入各 SKILL.md body

职责分离：
  - strategy = 路由决策（IntentAnalyzer 计算）
  - tags = 能力标签（@skill/@tool 注册）
  - semantics = 描述来源（YAML 单一信源）
"""
import yaml
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Optional

_SEMANTICS_DIR = Path(__file__).parent

# ── 缓存 ──
_skill_metas: Dict[str, "SkillMeta"] = {}
_tool_metas: Dict[str, "ToolMeta"] = {}
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
    behaviors: dict = field(default_factory=dict)  # v4: 通用行为规范

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

    # persona（v4: 扩展版，包含 behaviors）
    p = _load_yaml("persona.yaml")
    _persona = PersonaMeta(
        role=p.get("role", ""),
        identity=p.get("identity", ""),
        mission=p.get("mission", ""),
        behaviors=p.get("behaviors", {}),
    )

    # skills（从 SKILL.md 加载，YAML frontmatter + Markdown body）
    skills_dir = _SEMANTICS_DIR / "skills"
    if skills_dir.exists():
        for skill_dir in skills_dir.iterdir():
            if not skill_dir.is_dir():
                continue
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.exists():
                continue
            content = skill_md.read_text(encoding="utf-8")
            meta, body = _parse_skill_md(content)
            if meta.get("name"):
                _skill_metas[meta["name"]] = SkillMeta(
                    name=meta["name"],
                    description=meta.get("description", ""),
                    tags=meta.get("tags", []),
                    priority=meta.get("priority", 5),
                    default_weight=meta.get("default_weight", 1.0),
                    tools=meta.get("tools", []),
                    instructions=body,  # Markdown body 作为 instructions
                    standard_output=meta.get("standard_output", False),
                )

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


def _parse_skill_md(content: str) -> tuple[dict, str]:
    """解析 SKILL.md，分离 YAML frontmatter 和 Markdown body。"""
    meta = {}
    body = content

    # 检查是否以 --- 开头（YAML frontmatter）
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            # parts[0] = "", parts[1] = YAML, parts[2] = body
            try:
                meta = yaml.safe_load(parts[1]) or {}
            except yaml.YAMLError:
                meta = {}
            body = parts[2].strip()

    return meta, body


# ── 公开接口 ──

def get_persona() -> PersonaMeta:
    load_semantics()
    return _persona

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

def get_tools_summary_xml(tags_filter: Optional[List[str]] = None) -> str:
    """生成 tools 摘要 XML，按 category 分组。可选按 tags 过滤。"""
    load_semantics()
    by_cat: Dict[str, List[ToolMeta]] = {}
    for meta in _tool_metas.values():
        # §15: 用 tags 过滤（tags 优先，降级到无过滤）
        if tags_filter and meta.tags and not any(t in meta.tags for t in tags_filter):
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

### 6.4 chain/chains.py — 从 chains.yaml 加载

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

### 6.5 agent.py — _build_instructions() 简化

```python
from app.agent.semantics import get_persona, get_skills_summary_xml, get_tools_summary_xml

def _build_instructions(...):
    persona = get_persona()
    parts = [f"你是{persona.role}。{persona.identity}"]

    # 通用行为规范（v4: 从 persona.yaml 的 behaviors 加载）
    if persona.behaviors:
        parts.append("## 工作流程")
        for step in persona.behaviors.get("workflow", []):
            parts.append(f"- {step}")
        parts.append("\n## 安全原则")
        for rule in persona.behaviors.get("safety", []):
            parts.append(f"- {rule}")

    # skills 摘要（轻量，~500 token）
    parts.append(get_skills_summary_xml())

    # tools 摘要（按 tags 过滤）
    parts.append(get_tools_summary_xml(tags_filter=active_tags))

    # §15: strategy 路由的 JSON 格式指令（保持不变）
    if strategy == "traced":
        parts.append(finance_json_section)

    return "\n\n".join(parts)
```

## 七、Chain 执行流程（v4，无 domain）

### 改造后的 chain 流程

```
用户消息: "分析一下贵州茅台"
  ↓
IntentAnalyzer（从 intent.yaml 加载 prompt + 规则）
  ↓
输出: { verb: "analyze", noun: "stock", intent: "stock_analysis", ... }
  ↓
verb + noun 匹配 chains.yaml
  ↓
命中 chain: evaluate+stock (trigger_verbs=[analyze], trigger_nouns=[stock])
  ↓
system prompt = persona.yaml（人设 + 通用行为规范）
  + skills 摘要 XML（第一段）
  + tools 摘要 XML
  ↓
执行 chain steps:
  step1: hot_money_tracker → 加载 SKILL.md body（含领域指令"你是游资追踪专家..."）
  step2: lockup_watcher → 加载 SKILL.md body（含领域指令"你是解禁监控专家..."）
  step3: intelligence_agent → 加载 SKILL.md body（含领域指令"你是情报分析师..."）
  step4: technical_agent → 加载 SKILL.md body（含领域指令"你是技术分析师..."）
  ...
  step10: bear_researcher → 加载 SKILL.md body（含领域指令"你是空头研究员..."）
```

**关键变化**：
- ✅ 领域指令从"chain 执行前统一注入"变成"每个 skill 执行时各自带入"
- ✅ 更精准——每个 skill 只加载自己需要的指令，不加载无关领域的指令
- ✅ token 更省——不再一次性注入整个 domain 的 instructions
- ✅ chain 触发完全靠 verb + noun，不依赖 domain

### 与 Nanobot 对齐

Nanobot 的 Skill 加载流程：
```
Agent 启动 → 扫描 SKILL.md frontmatter → 生成 XML 摘要注入 system prompt
  ↓
用户消息 → Agent 匹配 description → 调用 read_file(SKILL.md) 加载完整指令
  ↓
按指令执行
```

本方案（v4）完全对齐：
```
Agent 启动 → 扫描 SKILL.md frontmatter → 生成 XML 摘要注入 system prompt
  ↓
用户消息 → IntentAnalyzer 匹配 verb+noun → 命中 chain → 逐个加载 SKILL.md body
  ↓
按指令执行
```

## 八、容错机制（编排层闭环 + 自适应惩罚）

### 设计原则

1. **编排层是主路径**：系统优先走编排层（chain），编排层决定用什么 skill/tool、什么顺序
2. **执行结果自动反馈**：skill/tool 的成功/失败自动写入编排层，形成闭环
3. **不预设 fallback**：替代方案由编排层动态决定，失败过的 skill/tool 自动降权
4. **意图不明就问**：规划层不猜，问用户
5. **复用现有溯源**：惩罚状态从 TraceCollector + qd_traces 计算，不另建一套

### 溯源能力（已有，直接复用，不改动）

Phase 1 已实现三层溯源（详见 AGENT_ACCOUNTABLE.md），容错机制直接复用，**不改动现有实现**：

| 层级 | 存储 | 容错机制如何用 |
|------|------|---------------|
| **Chain** | qd_traces | 评估 chain 整体成功率 |
| **Skill** | qd_traces + qd_skill_weights | 计算 skill 权重，决定是否降权 |
| **Tool** | qd_traces | 计算 tool 权重，决定是否降权 |

**不改动的部分**：
- TraceCollector / TracedTool / CallSkillTool（保持现状）
- qd_traces 表结构（保持现状）
- qd_skill_weights / qd_factor_weights（保持现状）
- update_skill_weights() / update_factor_weights()（保持现状）

**新增的查询接口**（只读，不写入新表）：
- `get_skill_weight(skill_name)` — 从 qd_traces 聚合计算惩罚权重
- `get_chain_path_scores(chain_id)` — 从 qd_traces 聚合 chain 各 step 的替代路径评分
- `get_skill_reliability(skill_name)` — 综合 qd_traces 执行成功率 + qd_skill_weights 交易胜率

**qd_skill_weights 的复用**：
- 现有的 `update_skill_weights()` 根据历史交易计算 skill 权重
- 容错机制可以直接用这个权重作为"历史可靠性"指标
- 权重低的 skill 在规划时自然降优先级

### 闭环架构

```
                    ┌─────────────────────────────────┐
                    │         编排层 (Chain)           │
                    │                                 │
                    │  chains.yaml + 规划逻辑          │
                    │  ┌───────────────────────────┐  │
                    │  │ qd_traces (已有)          │  │
                    │  │ ├─ chain 执行记录          │  │
                    │  │ ├─ skill 执行记录 + 权重    │  │
                    │  │ └─ tool 执行记录           │  │
                    │  │                           │  │
                    │  │ qd_skill_weights (已有)    │  │
                    │  │ └─ skill 交易胜率权重      │  │
                    │  └───────────────────────────┘  │
                    └──────────┬──────────▲────────────┘
                               │          │
                    规划执行计划 │          │ 自动反馈（TraceCollector）
                               ▼          │
                    ┌─────────────────────────────────┐
                    │         执行层 (Skill/Tool)      │
                    │                                 │
                    │  step1 → step2 → step3 → ...    │
                    │    ✓       ✓       ✗            │
                    └─────────────────────────────────┘
```

**闭环流程**：
1. 用户请求 → 编排层规划执行计划（优先走 chain）
2. 执行层按计划执行 skill/tool
3. TraceCollector 自动记录执行结果到 qd_traces
4. 下次请求 → 编排层从 qd_traces 计算惩罚权重，优先选验证过的路径

### 编排层优先策略

```
用户消息
  ↓
IntentAnalyzer 分析意图
  ↓
编排层决策（优先级从高到低）:
  ├─ 1. 匹配已有 chain → 走 chain（有历史验证的路径）
  ├─ 2. 无 chain 匹配 → 动态规划（组合 skill/tool）
  └─ 3. 无 skill 匹配 → 直接用 tool（最简路径）
```

**编排层的优势**：
- chain 是经过验证的 skill 组合，成功率有保障
- chain 有历史执行数据（哪些 step 成功/失败），可以动态调整
- 动态规划时参考惩罚权重，避开不稳定的 skill/tool

### 自适应惩罚机制

**核心思想**：编排层维护惩罚状态，执行层自动反馈，形成学习闭环。

**惩罚规则**：

| 事件 | 惩罚对象 | 权重变化 | 恢复条件 |
|------|----------|----------|----------|
| skill 执行成功 | 该 skill | 权重 ×1.2（上限 1.0） | — |
| skill 执行失败 | 该 skill | 权重 ×0.5 | 下次成功 ×1.5 |
| tool 调用成功 | 该 tool | 权重 ×1.2（上限 1.0） | — |
| tool 调用失败 | 该 tool | 权重 ×0.5 | 下次成功 ×1.5 |
| skill 连续失败 3 次 | 该 skill | 权重归零（标记不可用） | 手动重置或依赖恢复 |
| tool 连续失败 3 次 | 该 tool | 权重归零（标记不可用） | 手动重置或依赖恢复 |
| chain 某 step 替代成功 | 替代路径 | 路径评分 +1 | — |
| chain 某 step 替代失败 | 替代路径 | 路径评分 -1 | — |

**权重存储**：

> **设计决策**：不新建 penalty_state.json。惩罚状态直接从 qd_traces + qd_skill_weights 计算。
> 溯源数据已有，直接复用，避免数据重复和不一致。

```python
# 权重计算：直接查 qd_traces，不需要额外存储
def get_skill_weight(skill_name):
    """从 qd_traces 计算 skill 的惩罚权重。"""
    rows = db.query("""
        SELECT status, COUNT(*) as cnt
        FROM qd_traces
        WHERE skill_name = %s AND trace_type = 'skill'
        ORDER BY created_at DESC
        LIMIT 20
    """, skill_name)

    if not rows:
        return 1.0  # 无历史，默认权重

    # 连续失败 3 次 → 归零
    recent = rows[:3]
    if all(r.status == 'fail' for r in recent):
        return 0.0

    # 成功率作为权重
    success = sum(r.cnt for r in rows if r.status == 'success')
    total = sum(r.cnt for r in rows)
    return success / total


# chain 路径评分：从 qd_traces 按 chain_id + step_order 聚合
def get_chain_path_scores(chain_id):
    """从 qd_traces 计算 chain 各 step 的替代路径评分。"""
    return db.query("""
        SELECT step_order, skill_name,
               SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) as success,
               SUM(CASE WHEN status='fail' THEN 1 ELSE 0 END) as fail
        FROM qd_traces
        WHERE chain_id = %s AND trace_type = 'skill'
        GROUP BY step_order, skill_name
    """, chain_id)


# skill 可靠性评分：结合 qd_skill_weights（历史交易胜率）
def get_skill_reliability(skill_name):
    """综合 qd_traces 执行成功率 + qd_skill_weights 交易胜率。"""
    exec_weight = get_skill_weight(skill_name)  # 执行成功率
    trade_weight = db.query_one("""
        SELECT weight FROM qd_skill_weights WHERE skill_name = %s
    """, skill_name)

    if trade_weight:
        # 执行成功率 × 交易胜率
        return exec_weight * trade_weight.weight
    return exec_weight
```

**编排层如何使用惩罚状态**：

```python
def plan_with_penalties(intent):
    """编排层规划时参考惩罚状态，优先选验证过的路径。"""
    chain = match_chain(intent)

    if chain:
        # 走 chain，但根据惩罚状态调整 step
        for step in chain.steps:
            weight = get_skill_reliability(step.skill)

            if weight < 0.2:
                # 权重太低，找同 tags 替代
                alt = find_alternative(step.skill, by_tags=True)
                if alt and get_skill_reliability(alt) > weight:
                    step.skill = alt  # 用权重更高的替代

            # 查 chain 路径历史，优先用成功率高的替代
            path_scores = get_chain_path_scores(chain.id)
            if path_scores:
                step_scores = [p for p in path_scores if p.step_order == step.order]
                if step_scores:
                    best = max(step_scores, key=lambda x: x.success / (x.success + x.fail))
                    if best.skill_name != step.skill:
                        step.skill = best.skill_name

    return chain
```

### 执行层自动反馈

> **设计决策**：执行层不需要手动调用惩罚接口。TraceCollector 已经自动记录所有执行结果到 qd_traces。
> 编排层只需要查 qd_traces 计算权重即可。

```python
def execute_chain(chain, collector: TraceCollector):
    """执行 chain，TraceCollector 自动记录结果。"""
    results = []

    for step in chain.steps:
        try:
            result = execute_skill(step.skill, context, collector)
            # TraceCollector.on_skill_call() 自动记录到 qd_traces
            results.append({"skill": step.skill, "status": "success", "result": result})

        except Exception as e:
            # TraceCollector.on_skill_call() 自动记录失败到 qd_traces
            # 回退到编排层重新规划
            new_chain = plan_with_penalties(chain.intent)
            return execute_chain(new_chain, collector)  # 递归执行新计划

    return results
```

**关键点**：
- TraceCollector 已经在 Phase 1 实现，自动拦截所有 skill/tool 调用
- 不需要在容错机制中手动调用 `record_success()` / `record_failure()`
- 编排层直接查 qd_traces 计算权重，数据源是统一的

### 闭环流程示例

```
第 1 次请求: "分析贵州茅台"
  │
  ▼
编排层: 匹配 evaluate+stock chain
  ├─ step3: intelligence_agent (weight=1.0, 无历史)
  └─ 执行计划
  │
  ▼
执行层:
  ├─ step1-2: ✓
  ├─ step3: intelligence_agent → ✗ (API 超时)
  │    │
  │    ▼ 自动反馈: intelligence_agent weight ×0.5
  │    ▼ 回退编排层重新规划
  │    ├─ intelligence_agent (weight=0.5) → 不优先
  │    ├─ 同 tags: market_data_agent (weight=1.0) → 选它
  │    └─ 新执行计划
  │
  ├─ step3(新): market_data_agent → ✓
  │    ▼ 自动反馈: market_data_agent weight ×1.2 (保持1.0)
  │    ▼ 记录路径: evaluate+stock step3 market_data_agent 成功+1
  │
  └─ step4-10: ✓
  └─ 返回结果

第 2 次请求: "分析比亚迪"
  │
  ▼
编排层: 匹配 evaluate+stock chain
  ├─ step3: 查惩罚状态
  │    ├─ intelligence_agent (weight=0.5, 1次失败)
  │    ├─ market_data_agent (weight=1.0, 1次成功)
  │    └─ 路径历史: market_data_agent 成功率 100%
  ├─ step3 → 选 market_data_agent（验证过的路径）
  └─ 执行计划
  │
  ▼
执行层: 全部 ✓（走验证过的路径，成功率更高）
```

### 错误类型与处理策略

| 错误类型 | 发生阶段 | 处理策略 |
|----------|----------|----------|
| 意图不明 | 编排层 | 询问用户，不猜 |
| skill 不存在 | 编排层校验 | 找同 tags 替代，或告知用户 |
| tool 不可用 | 编排层校验 | 找同 category 替代，或告知用户 |
| required step 不可用 | 编排层校验 | 告知用户该 chain 无法完整执行 |
| skill 执行异常 | 执行层 | 自动反馈 + 回退编排层重新规划 |
| tool 调用失败 | 执行层 | 自动反馈 + 回退编排层重新规划 |
| 数据缺失 | 执行层 | skill 内部标注 missing，不猜测 |
| LLM 输出异常 | 执行层 | 回退编排层重新规划 |

### 与现有 required 字段的关系

| 字段 | v3 含义 | v4 含义（含闭环+惩罚） |
|------|---------|----------------------|
| `required: true` | 必须执行，失败则中断 | 必须执行，失败则**自动反馈+回退编排层重新规划**，仍失败则返回部分结果 |
| `required: false` | 可选，失败跳过 | 可选，失败则**自动反馈+回退编排层找替代**，无替代则跳过 |

> **设计决策**：不预设 fallback 字段。编排层根据 tags 相似度 + 惩罚权重 + 路径历史动态决定替代方案。
> 这比人工配置 fallback 更智能——编排层会学习哪些路径更可靠。

## 九、分层加载策略

### token 节省估算

| 部分 | 改造前 | 改造后 | 节省 |
|------|--------|--------|------|
| skill instructions（15 个） | ~8000-12000 tokens（全量注入） | ~800-1200 tokens（摘要） | ~85-90% |
| domain instructions | ~2000 tokens（按域注入） | **0 tokens**（移入 SKILL.md body，按需加载） | **100%** |
| tool descriptions | ~3000 tokens（全量注入） | ~1500 tokens（按 category 摘要） | ~50% |
| intent rules | ~300 tokens | 从 YAML 加载 | 100% |
| **总计** | ~13300-17300 tokens | ~2300-2700 tokens | **~80-85%** |

### 加载时序

```
进程启动
  → load_semantics() 读取所有 YAML/SKILL.md（~50ms，一次性）
  → 缓存到内存

每次请求
  → get_skills_summary_xml() 从缓存生成（<1ms）
  → get_tools_summary_xml(tags) 从缓存生成（<1ms）
  → 注入 system prompt（第一段）

Agent 调用 skill
  → get_skill_meta(name).instructions 从缓存读取（<1ms）
  → 注入 skill prompt（第二段）
```

## 十、迁移步骤

### Phase 1：建立 semantics 目录（不改现有代码）
1. 创建 `agent/semantics/` 目录 + `__init__.py` 加载器
2. 从现有代码提取描述，写入 YAML 文件和 SKILL.md
3. 单元测试：验证 YAML 加载正确、覆盖所有 skill/tool

### Phase 2：逐模块切换（每步可独立测试）
1. `intent_analyzer.py` → 从 intent.yaml 加载
2. `skills/*.py` → @skill 从 SKILL.md 加载（frontmatter + body）
3. `tools/*.py` → @tool 的 tags 从 semantics 加载
4. `chain/chains.py` → 从 chains.yaml 加载
5. `agent.py` → _build_instructions() 简化（移除 domain 注入）

### Phase 3：清理
1. 删除 `domain_registry.py` 和 `domains.yaml`
2. 删除 Python 代码中的硬编码描述
3. 从各 SKILL.md body 中补充原 domain 的领域特定指令
4. 更新文档
5. 回归测试

### Phase 4：分层加载优化
1. system prompt 只注入摘要 XML
2. 详细 instructions 按需加载
3. 性能测试和 token 对比

## 十一、与 §15 的集成检查清单

- [x] YAML 中用 `tags` 替代 `domain`
- [x] 加载器将 YAML tags 注入 @skill/@tool
- [x] `effective_tags` 逻辑不变（tags 优先，降级到 domain）
- [x] strategy 路由逻辑不变（IntentAnalyzer 计算）
- [x] tool 过滤用 tags（build() 中 effective_tags 逻辑不变）
- [x] 删除 domains.yaml 和 domain_registry.py
- [x] 通用行为规范迁移到 persona.md
- [x] 领域特定指令迁移到各 SKILL.md body（Phase 2 auto_load 支持）
- [x] IntentAnalyzer 的 domain_instructions 改为从 persona.md behaviors 生成
- [x] chain 触发完全靠 verb + noun，不依赖 domain

### Phase 2 完成记录（2026-06-15）
- `skills/registry.py`: @skill 新增 `auto_load=True` 模式，从 SKILL.md frontmatter 加载
- `tools/registry.py`: @tool 的 tags/category/layer 自动从 semantics 补全
- `chain/chains.py`: 链路定义从 chains.md frontmatter 加载，硬编码降级为 fallback
- `agent.py`: _load_preamble() 优先从 persona.md 加载 role/identity/mission

### Phase 3 完成记录（2026-06-15）
- `domain_registry.py` → 删除（domain 概念彻底移除）
- `domains.yaml` → 删除
- `intent_analyzer.py`: IntentResult.domain_instructions 改为从 persona.md behaviors 生成
- `agent.py`: 移除 init_builtin_domains() 调用
- `run.py`: 领域信息改从 persona.md 读取
- `semantics/__init__.py`: 删除 RouteMeta、get_route_metas、routes.yaml 加载

### Phase 3.5 统一 Front Matter MD（2026-06-15）
- 所有语义文件统一为 Front Matter MD 格式
- `persona.yaml` → `persona.md`（frontmatter: role/identity/mission, body: 行为规范）
- `intent.yaml` → `intent.md`（frontmatter: rules/patterns/mappings, body: classifier_prompt）
- `chains.yaml` → `chains.md`（frontmatter: chains 定义, body: 说明文档）
- `tools.yaml` → `tools.md`（frontmatter: categories 工具元数据, body: 分类说明）
- 删除废弃文件：domains.yaml.deprecated、skills.yaml、routes.yaml、planner.yaml
- `semantics/__init__.py`: 新增 `_load_frontmatter()` 统一加载函数

### Phase 3.6 选股技能合并（2026-06-15）
- `short_term_screener.md` + `eod_screener.md` + `post_market_screener.md` → `screener.md`
- 用 `names` 列表一个文件注册多个 skill
- `semantics/__init__.py`: 加载逻辑支持 `names` 列表

### 最终文件结构
```
semantics/
├── __init__.py              # 加载器（_load_frontmatter + names 列表支持）
├── persona.md               # 人设 + 行为规范（7 个行为域）
├── intent.md                # 意图分类（15 rules + 3 patterns + prompt body）
├── chains.md                # 链路编排（3 条链路 16 步）
├── tools.md                 # 工具元数据（14 categories, 79 tools）
└── skills/
    ├── screener.md          # 选股技能（names: 3 个场景）
    ├── technical_agent.md
    ├── hot_money_tracker.md
    └── ...（14 个 .md）
```

### 代码改动汇总
| 文件 | 改动 |
|------|------|
| `skills/registry.py` | @skill 新增 auto_load，从 SKILL.md 加载 |
| `tools/registry.py` | @tool tags/category 从 semantics 自动补全 |
| `chain/chains.py` | 链路从 chains.md frontmatter 加载 |
| `agent.py` | _load_preamble 从 persona.md 加载 |
| `intent_analyzer.py` | domain_instructions 从 persona.md behaviors 生成 |
| `run.py` | 领域信息从 persona.md 读取 |
| `semantics/__init__.py` | 统一 _load_frontmatter + names 列表支持 |

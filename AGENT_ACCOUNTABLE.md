# Agent + 可追责 架构设计

> 日期: 2026-06-09
> 状态: 设计中
> 替代: DESIGN_RESTRUCTURE.md（算法化路线，已废弃）
> 继承: AGENT_REDESIGN.md（三层追责体系）+ 现有 agent.py（smolagents CodeAgent）

## 一、设计原则

### 1. Agent 是唯一的决策者
- smolagents CodeAgent 保持完整推理-行动-观察循环
- Agent 自主决定调什么工具、看什么数据、怎么分析、何时结束
- **不拆分**：不做 Planner/ChainExecutor 的职责分离
- **不削弱**：不用 algo_analyze() 替代 LLM 推理

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

### 与 DESIGN_RESTRUCTURE 的关键区别

| 维度 | DESIGN_RESTRUCTURE（废弃） | 本设计 |
|------|--------------------------|--------|
| 决策者 | Planner(LLM) + ChainExecutor(算法) | Agent(LLM，完整推理循环) |
| Skill 间通信 | 无，线性盲执行 | Agent 看到前序结果后自主决定下一步 |
| LLM 调用次数 | Planner 1次 + 每个 Skill 可能 1次 | Agent 内部按需调用（和现在一样） |
| 自适应能力 | 无 | 有（agent 可根据中间结果调整策略） |
| 可追责性 | 有（EvalNode 树） | 有（同样的 EvalNode 树） |

## 三、核心组件

### 3.1 TraceCollector — 执行追踪器

在 agent 执行过程中自动收集信息，构建 EvalNode 树。**对 agent 透明**，agent 不需要知道它的存在。

```python
class TraceCollector:
    """Agent 执行过程中的自动追踪器。
    
    职责：
    1. 拦截 tool_call，自动创建 Tool 层 EvalNode
    2. 拦截 call_skill，自动创建 Skill 层 EvalNode + SkillReport
    3. Agent 结束时，创建 Chain 层根节点，组装完整 EvalNode 树
    4. 存库
    """
    
    def __init__(self, session_id: str, user_query: str):
        self.session_id = session_id
        self.user_query = user_query
        self.stock_code = ""      # 从上下文或 agent 行为中提取
        self.stock_name = ""
        self.tool_nodes: list[EvalNode] = []     # 所有工具调用
        self.skill_nodes: list[EvalNode] = []    # 所有 skill 调用
        self.skill_reports: list[SkillReport] = []
        self.start_time = time.time()
        self.intent_verb = ""
        self.intent_noun = ""
        self.domain = ""
    
    def on_tool_call(self, tool_name: str, arguments: dict, result: Any, 
                     elapsed_ms: float, error: str = None):
        """普通工具调用回调。由 tool wrapper 自动触发。"""
        node = EvalNode(
            layer=Layer.TOOL.value,
            name=tool_name,
            input_params=arguments,
            output_data=BaseSkill._summarize_for_storage(result),
            elapsed_ms=elapsed_ms,
            status=Status.FAILED.value if error else Status.OK.value,
            error=error or "",
        )
        # 自动提取 stock_code（如果工具参数中有）
        if not self.stock_code:
            for key in ("stock_code", "stock", "symbol", "code"):
                if key in arguments and arguments[key]:
                    self.stock_code = str(arguments[key])
                    break
        self.tool_nodes.append(node)
    
    def on_skill_call(self, skill_name: str, report: SkillReport, 
                      skill_node: EvalNode):
        """Skill 调用回调。由 call_skill 工具触发。"""
        self.skill_nodes.append(skill_node)
        self.skill_reports.append(report)
    
    def on_agent_finish(self, final_answer: str, total_steps: int, 
                        total_tokens: int, model: str) -> EvalNode:
        """Agent 结束，构建完整 EvalNode 树并存库。"""
        # 构建根节点
        root = EvalNode(
            layer=Layer.CHAIN.value,
            name=f"{self.intent_verb}+{self.intent_noun}" if self.intent_verb else "agent",
            exec_date=date.today(),
            stock_code=self.stock_code,
            stock_name=self.stock_name,
            input_params={"user_query": self.user_query},
            analysis=final_answer[:2000],
        )
        
        # 挂载 skill 节点
        for skill_node in self.skill_nodes:
            root.add_child(skill_node)
        
        # 挂载不属于任何 skill 的工具节点（agent 直接调用的）
        skill_tool_names = set()
        for sn in self.skill_nodes:
            skill_tool_names.update(sn.tools_called)
        
        orphan_tools = [tn for tn in self.tool_nodes 
                        if tn.name not in skill_tool_names]
        for tool_node in orphan_tools:
            root.add_child(tool_node)
        
        # 计算 chain 层汇总
        valid_reports = [r for r in self.skill_reports 
                         if r.status == "ok" and r.score is not None]
        if valid_reports:
            # 用 agent 的最终结论作为 chain 层决策
            # （不重新计算，agent 本身就是决策者）
            root.score = self._extract_score_from_answer(final_answer)
            root.direction = self._extract_direction_from_answer(final_answer)
            root.action = self._extract_action_from_answer(final_answer)
            root.confidence = self._extract_confidence_from_answer(final_answer)
        
        root.elapsed_ms = (time.time() - self.start_time) * 1000
        
        # 存库
        execution_id = store.save_tree(root)
        root.id = execution_id
        
        return root
    
    def _extract_score_from_answer(self, answer: str) -> float:
        """从 agent 最终回复中提取评分。"""
        # 尝试从结构化输出中提取
        import re
        # 匹配 "评分:75" 或 "score: 75" 等模式
        m = re.search(r'(?:评分|score)[：:\s]*(\d+(?:\.\d+)?)', answer, re.I)
        if m:
            return max(0, min(100, float(m.group(1))))
        # 从 action 推断
        action = self._extract_action_from_answer(answer)
        return {"buy": 70, "sell": 30, "hold": 50, "skip": 20}.get(action, 50)
    
    def _extract_direction_from_answer(self, answer: str) -> str:
        """从 agent 最终回复中提取方向。"""
        answer_lower = answer.lower()
        if any(kw in answer_lower for kw in ["买入", "buy", "看多", "bullish", "建议买"]):
            return "bullish"
        if any(kw in answer_lower for kw in ["卖出", "sell", "看空", "bearish", "建议卖"]):
            return "bearish"
        return "neutral"
    
    def _extract_action_from_answer(self, answer: str) -> str:
        """从 agent 最终回复中提取动作。"""
        answer_lower = answer.lower()
        if any(kw in answer_lower for kw in ["买入", "buy", "建议买"]):
            return "buy"
        if any(kw in answer_lower for kw in ["卖出", "sell", "建议卖"]):
            return "sell"
        if any(kw in answer_lower for kw in ["跳过", "skip", "回避"]):
            return "skip"
        return "hold"
    
    def _extract_confidence_from_answer(self, answer: str) -> float:
        """从 agent 最终回复中提取置信度。"""
        answer_lower = answer.lower()
        if any(kw in answer_lower for kw in ["高度确信", "非常确定", "high confidence"]):
            return 0.8
        if any(kw in answer_lower for kw in ["不太确定", "有风险", "low confidence"]):
            return 0.3
        return 0.5
```

### 3.2 Tool Wrapper — 工具调用拦截

在不修改 agent 逻辑的前提下，包装所有工具，自动触发 TraceCollector 回调。

```python
class TracedTool:
    """包装原始工具，自动记录调用信息。"""
    
    def __init__(self, original_tool, collector: TraceCollector):
        self._tool = original_tool
        self._collector = collector
        # 保持原始工具的所有属性
        self.name = original_tool.name
        self.description = original_tool.description
        self.inputs = getattr(original_tool, 'inputs', {})
        self.output_type = getattr(original_tool, 'output_type', 'text')
    
    def forward(self, **kwargs) -> Any:
        t0 = time.time()
        error = None
        result = None
        try:
            result = self._tool.forward(**kwargs)
        except Exception as e:
            error = str(e)
            raise
        finally:
            elapsed = (time.time() - t0) * 1000
            self._collector.on_tool_call(
                tool_name=self.name,
                arguments=kwargs,
                result=result,
                elapsed_ms=elapsed,
                error=error,
            )
        return result
```

### 3.3 call_skill 工具 — Skill 调用入口

Agent 通过 `call_skill` 工具调用专业分析 Skill。Skill 内部的工具调用也会被 TraceCollector 拦截。

```python
class CallSkillTool(Tool):
    """Agent 调用 Skill 的统一入口。
    
    Agent 自主决定何时调用、调用哪个 Skill。
    调用后返回标准化 SkillReport，agent 可以基于报告继续推理。
    """
    name = "call_skill"
    description = "调用专业分析技能。返回标准化分析报告（评分/方向/信号/因子明细）。"
    inputs = {
        "skill_name": {"type": "string", "description": "技能名称"},
        "stock_code": {"type": "string", "description": "股票代码"},
        "stock_name": {"type": "string", "description": "股票名称（可选）"},
    }
    output_type = "text"
    
    def __init__(self, model, user_id, collector: TraceCollector):
        super().__init__()
        self._model = model
        self._user_id = user_id
        self._collector = collector
    
    def forward(self, skill_name: str, stock_code: str, 
                stock_name: str = "") -> str:
        from app.agent.skills.registry import skill_registry
        skill_registry.discover()
        
        skill = skill_registry.get(skill_name)
        if not skill:
            return f"未知技能: {skill_name}"
        
        # 构建 call_llm 和 call_tool_fn
        def call_llm(prompt):
            response = self._model([{"role": "user", "content": prompt}])
            return response.content if hasattr(response, "content") else str(response)
        
        all_tools = build_all_tools()
        tool_map = {t.name: t for t in all_tools}
        
        def call_tool_fn(tool_name, **kwargs):
            t = tool_map.get(tool_name)
            if not t:
                raise ValueError(f"Unknown tool: {tool_name}")
            return t(**kwargs)
        
        # 执行 Skill
        report, skill_node = skill.run(
            stock_code=stock_code,
            stock_name=stock_name,
            context={},
            call_llm=call_llm,
            call_tool_fn=call_tool_fn,
        )
        
        # 通知 TraceCollector
        self._collector.on_skill_call(skill_name, report, skill_node)
        
        # 返回标准化文本给 agent
        return self._format_report(report)
    
    def _format_report(self, report: SkillReport) -> str:
        """将 SkillReport 格式化为 agent 可读的文本。"""
        direction_cn = {
            "bullish": "看多", "bearish": "看空", "neutral": "中性"
        }.get(report.direction, "中性")
        
        lines = [
            f"## {report.skill_name} 分析报告",
            f"- 评分: {report.score:.0f}/100",
            f"- 方向: {direction_cn}",
            f"- 置信度: {report.confidence:.2f}",
        ]
        if report.signal:
            lines.append(f"- 信号: {report.signal}")
        
        if report.factors:
            lines.append("- 因子明细:")
            for f in report.factors:
                s = f"{f.score:.0f}" if f.score is not None else "—"
                lines.append(f"  - {f.name}: {f.value} ({s}分)")
        
        if report.missing_data:
            lines.append(f"- ⚠️ 缺失数据: {', '.join(report.missing_data)}")
        
        if report.analysis:
            lines.append(f"\n### 详细分析\n{report.analysis[:1500]}")
        
        return "\n".join(lines)
```

### 3.4 Agent Builder — 组装

```python
def get_smolagent(
    skills=None, user_id=1, model=None, provider=None,
    max_steps=10, user_message="", language="zh",
    domain="", domain_instructions="", intent_context="",
    tool_categories=None,
    collector: TraceCollector = None,  # 新增：追踪器
) -> CodeAgent:
    """构建 Agent 实例。"""
    smol_model = build_model(model=model, provider=provider)
    
    # 普通工具（带追踪包装）
    tools = _get_tools(domain)
    if collector:
        tools = [TracedTool(t, collector) for t in tools]
    
    # call_skill 工具
    call_skill = CallSkillTool(
        model=smol_model, user_id=user_id, collector=collector,
    )
    tools.append(call_skill)
    
    # 构建 agent（和现在一样）
    instructions = _build_instructions(...)
    agent = CodeAgent(
        tools=tools,
        model=smol_model,
        max_steps=max_steps,
        instructions=instructions,
        ...
    )
    return agent
```

### 3.5 Agent Executor — 执行 + 追踪

```python
class _AgentExecutor:
    """执行 agent 并收集追踪信息。"""
    
    def _chat_locked(self, message, session_id, context, progress_callback, user_id):
        # 1. 意图分析（和现在一样）
        intent = analyze_intent(message, ...)
        
        # 2. 创建 TraceCollector
        collector = TraceCollector(session_id=session_id, user_query=message)
        collector.intent_verb = intent.verb
        collector.intent_noun = intent.noun
        collector.domain = intent.domain
        
        # 3. 构建 agent（带追踪器）
        agent = get_smolagent(..., collector=collector)
        
        # 4. 执行（agent 自由推理，和现在完全一样）
        result = agent.run(enriched_message, max_steps=self.max_steps)
        
        # 5. 收集结果
        content = str(result.output) if hasattr(result, 'output') else str(result)
        
        # 6. 构建 EvalNode 树 + 存库
        root = collector.on_agent_finish(
            final_answer=content,
            total_steps=len(result.steps),
            total_tokens=result.token_usage.input_tokens + result.token_usage.output_tokens,
            model=str(agent.model.model_id),
        )
        
        # 7. 返回（和现在一样）
        return AgentResult(
            success=True, content=content,
            tool_calls_log=collector.tool_nodes,  # 结构化日志
            total_steps=len(result.steps),
            total_tokens=...,
            charts=charts_b64,
        )
```

## 四、追责体系（继承 AGENT_REDESIGN.md）

### 4.1 执行时记录

每层记录什么：

| 层 | 记录内容 | 来源 |
|---|---------|------|
| **Chain** | 最终 answer、action/score/direction、总耗时、总 token | TraceCollector.on_agent_finish() |
| **Skill** | SkillReport 全量（score/direction/confidence/factors/analysis） | CallSkillTool → TraceCollector.on_skill_call() |
| **Tool** | 入参、出参摘要（1~10条）、耗时、数据源、错误 | TracedTool → TraceCollector.on_tool_call() |

### 4.2 盘后回溯

**全程 SQL + 数学运算，0 token 消耗，不涉及 agent。**

**核心指标：单位时间收益率。胜率只是提高收益率的手段之一。**

```
期望收益率 = 胜率 × 平均盈利 - 败率 × 平均亏损
单位时间收益率 = 期望收益率 / 平均持有天数
```

回溯流程（纯算法，定时任务）：

```python
def evaluate_pending():
    """盘后自动运行，纯 SQL + 数学，0 token。"""
    
    # 1. 查待验证记录
    rows = db.query("""
        SELECT id, stock_code, direction, timeframe, exec_date
        FROM qd_traces
        WHERE layer = 'chain' AND exit_date IS NULL
    """)
    
    for row in rows:
        # 2. 按 timeframe 取实际行情
        exit_price = get_exit_price(row.stock_code, row.exec_date, row.timeframe)
        entry_price = get_entry_price(row.stock_code, row.exec_date)
        
        # 3. 算盈亏
        pnl_pct = (exit_price - entry_price) / entry_price * 100
        hold_days = get_hold_days(row.exec_date, row.timeframe)
        
        # 4. 判断方向是否正确
        if row.direction == "bullish":
            correct = pnl_pct > 0
        elif row.direction == "bearish":
            correct = pnl_pct < 0
        else:
            correct = None  # neutral 不验证
        
        # 5. 写回
        db.execute("""
            UPDATE qd_traces 
            SET exit_date = %s, pnl_pct = %s, hold_days = %s, correct = %s
            WHERE id = %s
        """, [today(), pnl_pct, hold_days, correct, row.id])
    
    # 6. 更新权重
    update_skill_weights()
    update_factor_weights()
```

验证窗口由 timeframe 决定，不用固定天数：

| timeframe | 取哪天的行情 | 说明 |
|-----------|------------|------|
| T+1 | 次日收盘价 | 用户问"明天" |
| T+3 | 第3个交易日收盘价 | 用户问"能不能买" |
| T+5 | 第5个交易日收盘价 | 中线 |
| 1W | 周五收盘价 | 用户问"这周" |
| 1M | 1个月后收盘价 | 中长线 |

**整个过程不需要 agent 参与，不需要 LLM 调用。**

### 4.3 权重迭代

**目标函数：单位时间期望收益率最大化。**

权重不按胜率迭代，按收益率迭代。一个胜率低但赚得多的 Skill，权重应该高于胜率高但赚得少的。

#### Skill 权重计算

```python
def calc_skill_weight(trades: list) -> float:
    """从历史交易记录计算 Skill 权重。
    
    Args:
        trades: [{pnl_pct, hold_days, correct}, ...]
    
    Returns:
        权重值（0.5~2.0）
    """
    if not trades:
        return 1.0
    
    # 胜率
    wins = [t for t in trades if t["correct"]]
    losses = [t for t in trades if not t["correct"]]
    win_rate = len(wins) / len(trades)
    
    # 平均盈利 / 平均亏损
    avg_win = sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0
    avg_loss = abs(sum(t["pnl_pct"] for t in losses) / len(losses)) if losses else 0
    
    # 盈亏比
    profit_loss_ratio = avg_win / avg_loss if avg_loss > 0 else avg_win
    
    # 期望收益率
    expected_return = win_rate * avg_win - (1 - win_rate) * avg_loss
    
    # 平均持有天数
    avg_hold_days = sum(t["hold_days"] for t in trades) / len(trades) if trades else 1
    avg_hold_days = max(avg_hold_days, 1)
    
    # 单位时间期望收益率（核心指标）
    return_per_day = expected_return / avg_hold_days
    
    # 映射到权重（0.5~2.0）
    # return_per_day 在 -5%~+5% 范围内线性映射
    weight = max(0.5, min(2.0, 1.0 + return_per_day * 20))
    
    return round(weight, 3)
```

#### 因子权重计算

同样的逻辑，按因子维度聚合：

```python
def calc_factor_weight(factor_trades: list) -> float:
    """从因子级交易记录计算权重。逻辑同 Skill 权重。"""
    # 和 calc_skill_weight 相同逻辑
    ...
```

#### 时间衰减

权重计算时，近期交易权重更高：

```python
def apply_decay(trades: list, half_life_days: int = 30) -> list:
    """给交易记录加时间衰减权重。"""
    now = date.today()
    for t in trades:
        age_days = (now - t["exec_date"]).days
        decay = 0.5 ** (age_days / half_life_days)
        t["decay_weight"] = decay
    return trades
```

#### 迭代闭环

```
盘后自动运行:
  1. 查找 exit_date IS NULL 的 chain 层记录
  2. 按 timeframe 取实际行情，写回 pnl_pct / hold_days / correct
  3. 聚合每个 Skill 的历史交易 → calc_skill_weight → 写入 qd_skill_weights
  4. 聚合每个因子的历史交易 → calc_factor_weight → 写入 qd_factor_weights
  5. 下次 agent 执行时，权重自动注入到 instructions

## 五、与现有代码的关系

### 保留不变
- `agent.py` 的整体结构（_AgentExecutor、chat/chat_stream）
- `smolagents CodeAgent` 作为 agent 引擎
- `intent_analyzer.py` 意图分析
- `tools/` 目录下 80+ 工具
- `chain/schema.py` EvalNode/SkillReport/FactorItem
- `chain/store.py` 持久化
- `chain/evaluator.py` 回溯评估（重写，按 timeframe + 收益率）
- `skills/base.py` BaseSkill 基类
- `skills/registry.py` 自动发现

### 需要修改
- `agent.py`: 注入 TraceCollector，工具带追踪包装
- `skills/call_skill_tool.py`: 适配 TraceCollector
- `_build_instructions`: 注入历史权重信息到 agent prompt

### 需要新增
- `agent/trace_collector.py`: TraceCollector 类
- `agent/traced_tool.py`: TracedTool 包装类

### 可以删除
- `planner.py`: 不再需要独立规划器（agent 自己规划）
- `agent/chain/executor.py`: 不再需要独立执行器（agent 自己执行）
- `DESIGN_RESTRUCTURE.md`: 算法化路线已废弃

## 六、标准化输出

### 6.1 核心原则

**Agent 只填数据，代码控制格式。**

Agent 的 final_answer 必须是 JSON，由代码格式化为标准卡片给用户。
格式 100% 由代码控制，LLM 不决定排版。

### 6.2 核心概念：direction + score 离开 timeframe 没有意义

同一个股票，不同时间维度方向可以完全相反：

```
贵州茅台:
  T+1:  bullish（短线反弹）  评分 70
  T+3:  neutral（盘整）      评分 50
  T+5:  bearish（回落）      评分 35
  1月:  bullish（基本面支撑） 评分 75
```

因此 **timeframe 是必填字段**，不是可选。

### 6.3 Agent 输出 JSON 规范

Agent 的 final_answer 必须输出以下 JSON（instructions 强制 + final_answer_checks 校验）：

```json
{
  "action": "buy",
  "score": 72,
  "direction": "bullish",
  "confidence": "high",
  "timeframe": "T+3",
  "timeframe_reason": "用户问能不能买，按3天短线维度分析",
  "stock_code": "600519",
  "stock_name": "贵州茅台",
  "signal": "技术面+动量双多，情报面中性",
  "factors": [
    {"name": "技术面", "score": 75, "direction": "bullish"},
    {"name": "动量", "score": 80, "direction": "bullish"},
    {"name": "情报", "score": 45, "direction": "neutral"}
  ],
  "analysis": "MA5上穿MA20形成金叉，成交量放大1.5倍，MACD零轴上方..."
}
```

字段说明：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| action | string | ✅ | buy / sell / hold / skip |
| score | float | ✅ | 0-100，50=中性 |
| direction | string | ✅ | bullish / bearish / neutral |
| confidence | string | ✅ | high / medium / low |
| timeframe | string | ✅ | T+1 / T+3 / T+5 / 1W / 1M / 3M / 1Y |
| timeframe_reason | string | ✅ | 为什么选这个时间维度 |
| stock_code | string | ❌ | 股票代码（个股分析时必填） |
| stock_name | string | ❌ | 股票名称 |
| signal | string | ✅ | 一句话信号摘要 |
| factors | array | ✅ | 各维度评分明细 |
| analysis | string | ✅ | 完整分析文字 |

### 6.4 时间维度推断规则

| 用户问法 | 推断 timeframe | 说明 |
|---------|---------------|------|
| 明天涨势怎样 | T+1 | 用户明确给时间 |
| 这周走势 | 1W | 用户明确给时间 |
| 短线机会 | T+1~T+3 | agent 声明具体维度 |
| 能不能买 | agent 声明 | 按"我按X天维度分析" |
| 中长期怎么样 | 1M~3M | agent 声明具体维度 |
| 未指定 | agent 声明 | 必须在 timeframe_reason 中说明 |

**关键规则**：
- 用户给了时间 → 按用户的来
- 用户没给时间 → agent 自己声明分析维度，不能含糊
- agent 做的是**预测**，不是回顾历史

### 6.5 final_answer_checks 校验

```python
def _check_output_json(answer, memory, agent) -> bool:
    """校验 agent 输出是否为合法 JSON 且字段完整。"""
    import json, re
    
    # 提取 JSON 块
    patterns = [
        r'```json\s*\n?(.*?)\n?\s*```',
        r'(\{[^{}]*"action"[^{}]*"score"[^{}]*\})',
    ]
    for pat in patterns:
        m = re.search(pat, answer, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(1).strip())
            except json.JSONDecodeError:
                continue
            
            # 校验必填字段
            required = {"action", "score", "direction", "confidence", "signal", "factors", "analysis"}
            missing = required - set(data.keys())
            if missing:
                return False
            
            # 校验 action 值
            if data["action"] not in ("buy", "sell", "hold", "skip"):
                return False
            
            # 校验 score 范围
            if not (0 <= data["score"] <= 100):
                return False
            
            # 校验 direction 值
            if data["direction"] not in ("bullish", "bearish", "neutral"):
                return False
            
            return True
    
    return False  # 没找到 JSON 块
```

不通过时，agent 会收到错误提示并重写 final_answer。

### 6.6 代码格式化

拿到合法 JSON 后，代码拼接标准卡片（含时间维度）：

```python
TIMEFRAME_CN = {
    "T+1": "1天", "T+3": "3天", "T+5": "5天",
    "1W": "1周", "1M": "1月", "3M": "3月", "1Y": "1年",
}

def format_decision_card(data: dict) -> str:
    """将 agent 输出的 JSON 格式化为用户可见的标准卡片。"""
    action_cn = {"buy": "买入", "sell": "卖出", "hold": "观望", "skip": "跳过"}
    conf_cn = {"high": "高", "medium": "中", "low": "低"}
    dir_cn = {"bullish": "看多", "bearish": "看空", "neutral": "中性"}
    tf = TIMEFRAME_CN.get(data.get("timeframe", ""), data.get("timeframe", ""))
    
    lines = [
        f"**{action_cn.get(data['action'], '观望')}** {data.get('stock_name', '')}({data.get('stock_code', '')})",
        f"维度:{tf} 评分:{data['score']:.0f} 方向:{dir_cn.get(data['direction'], '中性')} 置信:{conf_cn.get(data['confidence'], '中')}",
    ]
    
    # 因子明细
    if data.get("factors"):
        parts = []
        for f in data["factors"]:
            s = f"{f['score']:.0f}" if f.get("score") is not None else "—"
            parts.append(f"{f['name']}:{s}")
        lines.append(" | ".join(parts))
    
    # 信号
    if data.get("signal"):
        lines.append(f"信号: {data['signal']}")
    
    # 详细分析（折叠）
    if data.get("analysis"):
        lines.append(f"\n<details><summary>详细分析</summary>\n\n{data['analysis']}\n</details>")
    
    return "\n".join(lines)
```

输出示例：
```
**买入** 贵州茅台(600519)
维度:3天 评分:72 方向:看多 置信:高
技术面:75 | 动量:80 | 情报:45
信号: 技术面+动量双多，情报面中性

▸ 详细分析
MA5上穿MA20形成金叉，成交量较5日均量放大1.5倍，
MACD零轴上方红柱扩大。RSI 62处于偏强区间，
ADX 28确认趋势形成。近期无重大利空事件，舆情中性。
综合技术面和动量，短期偏多。
```

### 6.7 与 TraceCollector 的关系

TraceCollector 从 agent 输出的 JSON 中提取 score/action/direction/timeframe，不再用正则从自由文本硬解析：
- `score` → 直接取 `data["score"]`
- `action` → 直接取 `data["action"]`
- `direction` → 直接取 `data["direction"]`
- `timeframe` → 直接取 `data["timeframe"]`

100% 可靠，无解析失败风险。

## 七、Agent 的指令注入

Agent 的 instructions 中注入以下信息，帮助它做出更好的决策：

```markdown
## 可用技能（通过 call_skill 调用）

| 技能 | 用途 | 权重 | 单位时间收益率 | 样本数 |
|------|------|------|-------------|--------|
| technical_agent | 技术面综合 | 1.96 | +0.48%/天 | 120 |
| momentum_tracker | 动量分析 | 2.0 | +1.25%/天 | 80 |
| intelligence_agent | 情报分析 | 0.88 | -0.06%/天 | 60 |
| ... | ... | ... | ... | ... |

## 数据陷阱警告
- 龙虎榜: 盘后公布，游资一日游，追买=接盘
- 资金流向: 滞后，主力可对倒
- 新闻: 你看到时市场已反应
- ...

## 决策规则
- 技术面是地基，其他维度用来验证
- 多维度矛盾时，优先相信量价关系
- A股只能做多，空头信号=回避
- 不确定时说不确定，不要硬给结论

## ⚠️ 输出格式（必须遵守）

你的 final_answer 必须是以下 JSON 格式，否则系统无法解析：

```json
{
  "action": "buy/sell/hold/skip",
  "score": 0-100,
  "direction": "bullish/bearish/neutral",
  "confidence": "high/medium/low",
  "timeframe": "T+1/T+3/T+5/1W/1M/3M/1Y",
  "timeframe_reason": "为什么选这个时间维度",
  "stock_code": "6位代码",
  "stock_name": "股票名称",
  "signal": "一句话信号摘要",
  "factors": [
    {"name": "维度名", "score": 0-100, "direction": "bullish/bearish/neutral"}
  ],
  "analysis": "你的完整分析文字"
}
```

**⚠️ timeframe 规则**：
- 用户给了时间（"明天"/"这周"）→ 按用户的来
- 用户没给时间 → 你必须声明分析维度，不能含糊
- direction 和 score 只在你声明的时间维度内有效
- 不同时间维度方向可能相反，必须明确

不要输出任何 JSON 以外的文字。格式不对会被系统拒绝并要求重写。
```

## 八、实施计划

### Phase 0: 数据库重建（半天）
- [ ] 新建 `qd_traces` 表（替代 qd_evaluations）
- [ ] 新建 `qd_factor_weights` 表（含出厂权重）
- [ ] 新建 `qd_skill_weights` 表（新增）
- [ ] 新建 `chain/store.py` 的 `save_tree` / `load_tree` / `query`（适配新表）

### Phase 1: 标准化输出（1天）
- [ ] `agent.py` — `_build_instructions` 加 JSON 输出规范
- [ ] `agent.py` — `_check_output_json` 校验函数（替换旧 `_check_dashboard_json`）
- [ ] `agent.py` — `format_decision_card` 格式化函数
- [ ] `agent.py` — final_answer → JSON 校验 → 格式化卡片 → 返回用户
- [ ] 删除 agent.py 中的"金融领域标准化输出"事后硬解析代码
- [ ] 验证：agent 输出始终为标准卡片

### Phase 2: 追踪层（1~2天）
- [ ] `trace_collector.py` — TraceCollector 类
- [ ] `traced_tool.py` — TracedTool 包装
- [ ] `agent.py` — 注入 collector，工具包装
- [ ] TraceCollector 从 agent 输出 JSON 直接提取 score/action/direction（不解析自由文本）
- [ ] 验证：agent 执行后自动生成 EvalNode 树

### Phase 3: call_skill 适配（1天）
- [ ] `call_skill_tool.py` — 适配 TraceCollector
- [ ] Skill 内部工具调用也被追踪
- [ ] 验证：call_skill 后 Skill 层和 Tool 层 EvalNode 正确

### Phase 4: 指令注入（1天）
- [ ] 历史权重注入到 agent instructions
- [ ] 数据陷阱警告注入
- [ ] 可用技能列表注入（含准确率）

### Phase 5: 清理旧代码（半天）
- [ ] 删除 `planner.py`
- [ ] 删除 `chain/executor.py`
- [ ] 删除旧 `qd_evaluations` 表相关代码
- [ ] 清理 `agent.py` 中的 `_try_chain`、`_save_freeform_to_db`、`_infer_skill_name`
- [ ] 清理 `agent.py` 中的"金融领域标准化输出"事后硬解析代码
- [ ] 更新 DESIGN_RESTRUCTURE.md → 标记废弃
- [ ] 删除 planner.py
- [ ] 删除 chain/executor.py
- [ ] 清理 agent.py 中的 _try_chain 逻辑
- [ ] 更新 DESIGN_RESTRUCTURE.md → 标记废弃

### Phase 6: 回溯评估引擎（1~2天）
- [ ] `chain/evaluator.py` — 重写，纯 SQL + 数学，0 token，不涉及 agent
- [ ] `chain/evaluator.py` — `evaluate_pending()` 按 timeframe 取行情验证
- [ ] `chain/evaluator.py` — `calc_skill_weight()` 按单位时间收益率计算权重
- [ ] `chain/evaluator.py` — `calc_factor_weight()` 同逻辑
- [ ] `chain/evaluator.py` — 时间衰减（近期交易权重更高）
- [ ] 盘后定时任务：evaluate_pending → update_weights
- [ ] 验证权重迭代闭环

### Phase 7: 端到端验证（1天）
- [ ] agent 执行 → JSON 输出 → 格式化卡片 → 用户看到
- [ ] EvalNode 树存库 → 盘后回溯 → 权重更新
- [ ] 权重注入到 agent instructions → 下次执行生效

## 九、数据库设计（全新）

### 9.1 设计原则

- **一张表存执行树**：agent 每次执行 = 一棵树，树节点 = 一行
- **一张表存因子权重**：盘后回溯聚合
- **一张表存数据快照**：可选，用于可回放
- 不考虑旧表兼容，全部重建

### 9.2 qd_traces — 执行追踪表（核心）

```sql
CREATE TABLE qd_traces (
    -- 身份
    id              SERIAL PRIMARY KEY,
    parent_id       INTEGER REFERENCES qd_traces(id),
    root_id         INTEGER REFERENCES qd_traces(id),
    layer           VARCHAR(10) NOT NULL,    -- 'chain' / 'skill' / 'tool'
    step_order      INTEGER DEFAULT 0,       -- 在父节点中的执行顺序

    -- 标的
    exec_date       DATE NOT NULL,
    stock_code      VARCHAR(10),
    stock_name      VARCHAR(50),

    -- 内容（SkillReport / Tool 结果 / Agent final_answer）
    name            VARCHAR(100) NOT NULL,   -- chain_id / skill_name / tool_name
    score           REAL,                    -- 0-100
    direction       VARCHAR(20),             -- bullish / bearish / neutral
    action          VARCHAR(10),             -- buy / sell / hold / skip（仅 chain 层）
    signal          TEXT,                    -- 一句话信号
    confidence      REAL,                    -- 0.0-1.0
    analysis        TEXT,                    -- 分析文字（截断到 2000 字符）
    factors         JSONB,                   -- [{name, value, score, weight, status}]

    -- 工具调用记录
    input_params    JSONB,                   -- 入参
    output_summary  JSONB,                   -- 出参摘要（1~10 条）
    tools_called    TEXT[],                  -- 调用过的工具名列表
    missing_data    TEXT[],                  -- 缺失的数据

    -- 执行信息
    status          VARCHAR(20) DEFAULT 'ok', -- ok / missing / failed / skipped
    error           TEXT,
    elapsed_ms      REAL DEFAULT 0,
    data_source     VARCHAR(50),             -- 数据源（tool 层）

    -- 回溯验证（盘后写入）
    exit_date       DATE,
    exit_reason     VARCHAR(20),             -- take_profit / stop_loss / max_hold / signal_change
    pnl_pct         REAL,                    -- 盈亏率（%）
    hold_days       INTEGER,
    correct         BOOLEAN,                 -- 方向是否正确
    calibration     REAL DEFAULT 1.0,        -- 校准因子

    -- 元数据
    session_id      VARCHAR(100),
    user_query      TEXT,                    -- 用户原始消息
    model           VARCHAR(100),            -- 使用的模型
    total_tokens    INTEGER,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- 索引
CREATE INDEX idx_traces_root ON qd_traces(root_id);
CREATE INDEX idx_traces_parent ON qd_traces(parent_id);
CREATE INDEX idx_traces_layer ON qd_traces(layer);
CREATE INDEX idx_traces_stock ON qd_traces(stock_code, exec_date);
CREATE INDEX idx_traces_skill ON qd_traces(name, exec_date) WHERE layer = 'skill';
CREATE INDEX idx_traces_pending ON qd_traces(id) WHERE layer = 'chain' AND exit_date IS NULL;
```

### 9.3 qd_factor_weights — 因子权重表

**核心指标：单位时间期望收益率，不是胜率。**

```sql
CREATE TABLE qd_factor_weights (
    id              SERIAL PRIMARY KEY,
    skill_name      VARCHAR(100) NOT NULL,
    factor_name     VARCHAR(100) NOT NULL,
    
    -- 收益率指标
    weight          REAL DEFAULT 1.0,        -- 综合权重（0.5~2.0）
    win_rate        REAL,                    -- 胜率（参考值）
    avg_pnl_pct     REAL,                    -- 平均收益率（%）
    avg_hold_days   REAL,                    -- 平均持有天数
    return_per_day  REAL,                    -- 单位时间期望收益率（%）← 核心指标
    sample_count    INTEGER DEFAULT 0,
    
    -- 时间衰减
    decay_half_life INTEGER DEFAULT 30,      -- 半衰期（天）
    last_updated    TIMESTAMPTZ,
    
    -- 唯一约束
    UNIQUE(skill_name, factor_name)
);

-- 出厂权重
INSERT INTO qd_factor_weights (skill_name, factor_name, weight, decay_half_life) VALUES
-- technical_agent
('technical_agent', '趋势', 1.0, 60),
('technical_agent', '量价', 1.0, 60),
('technical_agent', '指标', 1.0, 30),
('technical_agent', '形态', 1.0, 30),
('technical_agent', '筹码', 1.0, 60),
-- momentum_tracker
('momentum_tracker', '趋势强度', 1.0, 30),
('momentum_tracker', '动量指标', 1.0, 30),
('momentum_tracker', '突破检测', 1.0, 14),
-- indicator_agent
('indicator_agent', 'MACD', 1.0, 30),
('indicator_agent', 'KDJ', 1.0, 30),
('indicator_agent', 'RSI', 1.0, 30),
('indicator_agent', 'BOLL', 1.0, 30),
-- intelligence_agent
('intelligence_agent', '新闻情绪', 0.8, 7),
('intelligence_agent', '事件催化', 0.8, 7),
-- policy_analyst
('policy_analyst', '产业政策', 0.7, 7),
('policy_analyst', '货币政策', 0.7, 14),
-- hot_money_tracker
('hot_money_tracker', '龙虎榜', 0.7, 7),
('hot_money_tracker', '游资动向', 0.7, 7);
```

### 9.4 qd_skill_weights — Skill 权重表

**核心指标：单位时间期望收益率，不是胜率。**

```sql
CREATE TABLE qd_skill_weights (
    skill_name      VARCHAR(100) PRIMARY KEY,
    
    -- 收益率指标
    weight          REAL DEFAULT 1.0,        -- 综合权重（0.5~2.0）
    win_rate        REAL,                    -- 胜率（参考值）
    avg_pnl_pct     REAL,                    -- 平均收益率（%）
    avg_hold_days   REAL,                    -- 平均持有天数
    return_per_day  REAL,                    -- 单位时间期望收益率（%）← 核心指标
    profit_loss_ratio REAL,                  -- 盈亏比
    sample_count    INTEGER DEFAULT 0,
    
    -- 时间衰减
    decay_half_life INTEGER DEFAULT 30,
    last_updated    TIMESTAMPTZ
);

-- 出厂权重（无历史数据时的默认值）
INSERT INTO qd_skill_weights (skill_name, weight) VALUES
('technical_agent', 1.2),
('momentum_tracker', 1.1),
('indicator_agent', 1.1),
('backtest_agent', 1.0),
('screening_agent', 1.0),
('bull_researcher', 1.0),
('bear_researcher', 1.0),
('trading_agent', 1.0),
('market_data_agent', 0.9),
('concept_tracker', 0.9),
('lockup_watcher', 0.8),
('intelligence_agent', 0.8),
('data_engineer', 0.8),
('policy_analyst', 0.7),
('hot_money_tracker', 0.7);
```

### 9.5 权重迭代示例

```
technical_agent 历史 100 笔交易:
  胜率: 65%, 平均盈利: +3.2%, 平均亏损: -1.8%, 平均持有: 3天
  期望收益率 = 0.65 × 3.2 - 0.35 × 1.8 = 1.45%
  单位时间收益率 = 1.45% / 3 = 0.48%/天
  → 权重 = 1.0 + 0.48 × 20 = 1.96 (高权重)

momentum_tracker 历史 80 笔交易:
  胜率: 45%, 平均盈利: +8.0%, 平均亏损: -2.0%, 平均持有: 2天
  期望收益率 = 0.45 × 8.0 - 0.55 × 2.0 = 2.50%
  单位时间收益率 = 2.50% / 2 = 1.25%/天
  → 权重 = 1.0 + 1.25 × 20 = 2.0 (封顶)

intelligence_agent 历史 60 笔交易:
  胜率: 50%, 平均盈利: +2.0%, 平均亏损: -2.5%, 平均持有: 4天
  期望收益率 = 0.50 × 2.0 - 0.50 × 2.5 = -0.25%
  单位时间收益率 = -0.25% / 4 = -0.06%/天
  → 权重 = 1.0 + (-0.06) × 20 = 0.88 (低权重)
```

### 9.6 与旧表的关系

| 旧表 | 新表 | 关系 |
|------|------|------|
| qd_evaluations | qd_traces | 替代，字段精简，去掉旧兼容字段 |
| qd_factor_weights | qd_factor_weights | 重建，按单位时间收益率计算权重 |
| 无 | qd_skill_weights | 新增，按单位时间收益率计算权重 |

### 9.7 数据量控制

**单棵 EvalNode 树总大小 ≤ 50KB**

```python
MAX_TREE_SIZE = 50 * 1024  # 50KB

def _enforce_size_limit(root: EvalNode):
    """确保整棵树不超限。截断策略：优先截 tool 层，再截 skill 层。"""
    total = len(root.to_json())
    if total <= MAX_TREE_SIZE:
        return
    
    # 第一轮：截断 tool 层 output_summary（最大头）
    for tool_node in root.tool_nodes:
        if tool_node.output_data:
            tool_node.output_data = {"truncated": True, "sample": str(tool_node.output_data)[:200]}
    
    total = len(root.to_json())
    if total <= MAX_TREE_SIZE:
        return
    
    # 第二轮：截断 skill analysis
    for skill_node in root.skill_reports:
        if len(skill_node.analysis) > 500:
            skill_node.analysis = skill_node.analysis[:500] + "...(截断)"
    
    total = len(root.to_json())
    if total <= MAX_TREE_SIZE:
        return
    
    # 第三轮：截断 chain 层 analysis
    if len(root.analysis) > 500:
        root.analysis = root.analysis[:500] + "...(截断)"
```

各层存储策略：

| 层 | 存什么 | 不存什么 | 截断规则 |
|---|--------|---------|---------|
| Tool | 入参 + 出参摘要(1~10条) + 元数据 | 原始全量数据 | `_summarize_for_storage` |
| Skill | SkillReport 全量 | 内部 LLM thinking | analysis ≤ 2000 字符 |
| Chain | final_answer JSON | agent 中间步骤 model_output | analysis ≤ 2000 字符 |
| Agent thinking | 不存入 qd_traces | | 调试需要时写日志文件 |

## 十、风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| agent 不调 call_skill，全用普通工具 | Skill 层无数据，回溯粒度粗 | instructions 强调"分析股票时先用 call_skill" |
| agent 调太多 skill，token 爆炸 | 成本高 | max_steps 限制 + instructions 强调"精简" |
| TraceCollector 的 on_tool_call 性能 | 每次工具调用多 ~1ms | 可接受，纯内存操作 |
| agent 输出 JSON 格式不合规 | final_answer_checks 拒绝，需要重写 | instructions 明确格式 + 重试最多 2 次 |
| agent JSON 中 score/action 和实际分析矛盾 | 数据不准 | 回溯时以 SkillReport 为准校验 |

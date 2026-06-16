# Agent + 可追责 架构设计

> 日期: 2026-06-09（初版）→ 2026-06-10（补充关键设计细节 + Skill 自注册 + 因子清理）→ 2026-06-16（§17 Planner 前置重构）
> 适用范围: 仅 domain="finance" 金融领域，其他领域保持 DESIGN_RESTRUCTURE.md 架构
> 替代: DESIGN_RESTRUCTURE.md（算法化路线，已废弃，保留只读参考）
> 继承: AGENT_REDESIGN.md（三层追责体系）+ 现有 agent.py（smolagents）
> 
> ⚠️ 本文档定位：**设计蓝图**。实施状态见 Git 提交记录。
> 万一调坏了，从本文档恢复设计意图，不依赖代码注释。

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

**提取策略：JSON 优先，正则降级。**

Agent 的 final_answer 被强制要求输出标准 JSON（见第六章），TraceCollector 直接 `json.loads` 取字段，100% 可靠。正则匹配仅作为 fallback——万一 Agent 违规输出非 JSON 时兜底，不作为主路径。

```python
class TraceCollector:
    """Agent 执行过程中的自动追踪器。
    
    职责：
    1. 拦截 tool_call，自动创建 Tool 层 EvalNode
    2. 拦截 call_skill，自动创建 Skill 层 EvalNode + SkillReport
    3. Agent 结束时，创建 Chain 层根节点，组装完整 EvalNode 树
    4. 存库

    提取策略：
    - 优先从 Agent 输出的 JSON 中直接取 score/action/direction/timeframe（100% 可靠）
    - 仅当 JSON 解析失败时，降级到正则匹配自由文本（fallback）
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
        # 从 JSON 提取结构化数据（优先），正则 fallback
        extracted = self._extract_from_json(final_answer)

        # 构建根节点
        root = EvalNode(
            layer=Layer.CHAIN.value,
            name=f"{self.intent_verb}+{self.intent_noun}" if self.intent_verb else "agent",
            exec_date=date.today(),
            stock_code=self.stock_code or (extracted.get("stock_code", "")),
            stock_name=self.stock_name or (extracted.get("stock_name", "")),
            input_params={"user_query": self.user_query},
            analysis=extracted.get("analysis", final_answer[:2000]),
            # 从 JSON 直接取（100% 可靠）
            score=extracted.get("score"),
            direction=extracted.get("direction", ""),
            action=extracted.get("action", ""),
            signal=extracted.get("signal", ""),
            confidence=extracted.get("confidence") if isinstance(extracted.get("confidence"), (int, float))
                       else {"high": 0.8, "medium": 0.5, "low": 0.3}.get(extracted.get("confidence", ""), 0.5),
            timeframe=extracted.get("timeframe", ""),
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
        
        # JSON 提取失败时，降级到正则提取（fallback）
        if root.score is None:
            root.score = self._extract_score_from_answer(final_answer)
        if not root.direction:
            root.direction = self._extract_direction_from_answer(final_answer)
        if not root.action:
            root.action = self._extract_action_from_answer(final_answer)
        if not root.signal:
            root.signal = extracted.get("signal", "")
        if root.confidence is None:
            root.confidence = self._extract_confidence_from_answer(final_answer)
        
        root.elapsed_ms = (time.time() - self.start_time) * 1000
        
        # 存库
        execution_id = store.save_tree(root)
        root.id = execution_id
        
        return root
    
    def _extract_from_json(self, answer: str) -> dict:
        """优先从 Agent 输出的 JSON 中直接提取字段（100% 可靠）。

        返回 dict，字段缺失时返回空 dict（触发 fallback）。
        """
        import json as _json
        import re as _re

        # 提取 JSON 块
        patterns = [
            r'```json\s*\n?(.*?)\n?\s*```',
            r'(\{[^{}]*"action"[^{}]*"score"[^{}]*\})',
        ]
        for pat in patterns:
            m = _re.search(pat, answer, _re.DOTALL)
            if m:
                try:
                    data = _json.loads(m.group(1).strip())
                    if isinstance(data, dict) and "action" in data:
                        return data
                except (_json.JSONDecodeError, TypeError):
                    continue
        return {}

    def _extract_score_from_answer(self, answer: str) -> float:
        """从 agent 最终回复中提取评分。

        优先级：JSON 直接取 > 正则 fallback > 从 action 推断
        """
        # 1. JSON 优先
        data = self._extract_from_json(answer)
        if data and "score" in data:
            return max(0, min(100, float(data["score"])))

        # 2. 正则 fallback
        import re
        m = re.search(r'(?:评分|score)[：:\s]*(\d+(?:\.\d+)?)', answer, re.I)
        if m:
            return max(0, min(100, float(m.group(1))))

        # 3. 从 action 推断
        action = self._extract_action_from_answer(answer)
        return {"buy": 70, "sell": 30, "hold": 50, "skip": 20}.get(action, 50)

    def _extract_direction_from_answer(self, answer: str) -> str:
        """从 agent 最终回复中提取方向。

        优先级：JSON 直接取 > 正则 fallback
        """
        # 1. JSON 优先
        data = self._extract_from_json(answer)
        if data and "direction" in data:
            d = data["direction"]
            if d in ("bullish", "bearish", "neutral"):
                return d

        # 2. 正则 fallback
        answer_lower = answer.lower()
        if any(kw in answer_lower for kw in ["买入", "buy", "看多", "bullish", "建议买"]):
            return "bullish"
        if any(kw in answer_lower for kw in ["卖出", "sell", "看空", "bearish", "建议卖"]):
            return "bearish"
        return "neutral"

    def _extract_action_from_answer(self, answer: str) -> str:
        """从 agent 最终回复中提取动作。

        优先级：JSON 直接取 > 正则 fallback
        """
        # 1. JSON 优先
        data = self._extract_from_json(answer)
        if data and "action" in data:
            a = data["action"]
            if a in ("buy", "sell", "hold", "skip"):
                return a

        # 2. 正则 fallback
        answer_lower = answer.lower()
        if any(kw in answer_lower for kw in ["买入", "buy", "建议买"]):
            return "buy"
        if any(kw in answer_lower for kw in ["卖出", "sell", "建议卖"]):
            return "sell"
        if any(kw in answer_lower for kw in ["跳过", "skip", "回避"]):
            return "skip"
        return "hold"

    def _extract_confidence_from_answer(self, answer: str) -> float:
        """从 agent 最终回复中提取置信度。

        优先级：JSON 直接取 > 正则 fallback
        """
        # 1. JSON 优先
        data = self._extract_from_json(answer)
        if data and "confidence" in data:
            c = data["confidence"]
            if isinstance(c, (int, float)):
                return max(0.0, min(1.0, float(c)))
            if isinstance(c, str):
                return {"high": 0.8, "medium": 0.5, "low": 0.3}.get(c, 0.5)

        # 2. 正则 fallback
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
  3. update_skill_weights():
     3a. 自动同步 registry（新 Skill → INSERT 工厂默认值，旧 Skill 保留）
     3b. 聚合每个 Skill 的历史交易 → calc_skill_weight → UPSERT qd_skill_weights
  4. update_factor_weights():
     4a. 聚合每个因子的历史交易 → calc_factor_weight → UPSERT qd_factor_weights
     4b. 清理过期因子（近 N 天内未出现的 → DELETE）
  5. 下次 agent 执行时，权重自动注入到 instructions
```

**Skill 自注册**：新增 `skills/xxx.py` + `@skill` 装饰器 → 进程重启 → `skill_registry.discover()` 自动注册 → `update_skill_weights()` 首次运行时自动 INSERT 到 `qd_skill_weights`。增删 Skill 文件无需手动改表。

**因子自动清理**：`update_factor_weights()` 每次运行时记录 `active_keys`（近 N 天内实际出现的因子），DELETE 不在 `active_keys` 中的旧因子，防止表无限膨胀。

## 五、与现有代码的关系

> **适用范围**：本设计只改 `domain="finance"` 的金融领域。其他领域保持 DESIGN_RESTRUCTURE.md 的 ChainExecutor 架构不变，通过 domain 装饰器隔离。

### 保留不变
- `agent.py` 的整体结构（_AgentExecutor、chat/chat_stream）
- `smolagents CodeAgent` 作为 agent 引擎
- `intent_analyzer.py` 意图分析
- `tools/` 目录下 80+ 工具
- `chain/schema.py` EvalNode/SkillReport/FactorItem（加 timeframe 字段）
- `chain/store.py` 持久化（新建 qd_traces，废弃 qd_evaluations）
- `skills/base.py` BaseSkill 基类
- `skills/registry.py` 自动发现

### 需要修改
- `agent.py`: 金融领域注入 TraceCollector，工具带追踪包装；非金融领域走原 _try_chain 路径
- `skills/call_skill_tool.py`: 适配 TraceCollector
- `_build_instructions`: 金融领域注入 JSON 输出规范 + 历史权重信息
- `chain/schema.py`: EvalNode 加 timeframe 字段
- `chain/store.py`: 新增 qd_traces 表的 save_tree / load_tree（新表结构）

### 需要新增
- `agent/trace_collector.py`: TraceCollector 类（仅金融领域使用）
- `agent/traced_tool.py`: TracedTool 包装类
- `migrations/qd_traces.sql`: 新表 DDL + 出厂权重 INSERT

### 可以删除
- `planner.py`: 不再需要独立规划器（agent 自己规划）
- `agent/chain/executor.py`: 不再需要独立执行器（agent 自己执行）
- `DESIGN_RESTRUCTURE.md`: 标记废弃，保留只读参考

### 旧数据处理
- `qd_evaluations` 旧表保留只读，不迁移数据到 qd_traces
- 冷启动期间权重使用出厂值，有回测数据后自动迭代
- 详见第九章 9.6 节

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

> **Phase 1 一趟水完成**：以下所有改动在一个 Phase 中交付，不做分批。预计 3~5 天。
> 
> 适用范围：仅 `domain="finance"` 金融领域。其他领域保持 DESIGN_RESTRUCTURE.md 架构不变。

### Phase 1 实施状态

全部组件已实现，集成缺陷已修复。

**未完成（3项端到端验证）**：
- [ ] 金融领域：EvalNode 树存库 → 盘后回溯 → 权重更新
- [ ] 金融领域：权重注入到 agent instructions → 下次执行生效
- [ ] 非金融领域：回归测试，确认原 _try_chain 路径不受影响

### Phase 2+（后续迭代）
- [ ] Skill 层 algo_analyze 实现（8 个可纯算法 Skill）
- [ ] Tool 层数据源可靠度追踪
- [ ] 前端 DecisionCard 可视化（含 timeframe 维度切换）
- [ ] 回测结果可视化
- [ ] Skill 权重可视化（各 Skill 的 return_per_day 趋势图）

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
    timeframe       VARCHAR(10),             -- T+1 / T+3 / T+5 / 1W / 1M / 3M / 1Y（仅 chain 层）
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
-- ⚠️ 注意：出厂 INSERT 仅作参考。实际运行时 evaluator.update_skill_weights()
-- 会自动从 skill_registry 发现新 Skill 并 INSERT，无需手动维护此表。
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
| qd_evaluations | qd_traces | **废弃不迁移**，旧表保留只读，新表从零开始 |
| qd_factor_weights | qd_factor_weights | 重建，按单位时间收益率计算权重，出厂 INSERT 覆盖旧数据 |
| 无 | qd_skill_weights | 新增，按单位时间收益率计算权重 |

**旧数据不迁移，冷启动策略：**
- qd_evaluations 旧表保留只读（历史查询用），不迁移数据到 qd_traces
- qd_traces 从零开始，盘后定时任务跑 `evaluate_pending()` + `update_weights()` 逐步积累
- 冷启动期间所有 Skill/因子权重使用出厂值（qd_skill_weights / qd_factor_weights 的 INSERT）
- **新增 Skill 自动注册**：`update_skill_weights()` 每次运行时自动同步 `skill_registry`，新 Skill 首次出现时自动 INSERT 工厂默认值，无需手动改表
- 有回测数据后自动迭代，权重生效周期取决于用户使用频率和回溯窗口（T+3~T+5）
- 预计积累 50+ 笔已验证决策后，权重开始有统计意义

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
| 冷启动期权重全为出厂值 | 无个性化，所有 Skill 等权 | 出厂值已有差异（technical=1.2, policy=0.7）；积累 50+ 笔后生效 |
| 非金融领域 domain 路由错误 | 走错路径（应该走 ChainExecutor 却走了 TraceCollector） | domain 判断在 agent.py 入口处，明确分支 |

## 十一、设计决议

> 以下为设计评审中确认的 6 项关键决策，记录备查。

### 11.1 Domain 隔离方案：装饰器模式

**决策**：金融领域与其他领域共享同一个 agent.py 入口，通过 domain 字段路由不同逻辑。

**依据**：工具层（tools/registry.py）已有 `domain` 标识和过滤机制（`tool_registry.build({"domain": ...})`），Skill、Chain、Agent 配置层复用同一套模式即可，不需要分支隔离。

**实现方式**：
- tools/ 按 domain 注册（已有）
- skills/ 按 domain 注册（`@skill` 装饰器加 `domain` 参数）
- agent.py 的 `_build_instructions()` 按 domain 注入不同 instructions
- AGENT_ACCOUNTABLE 的改动（TraceCollector / JSON 标准化输出 / 权重迭代）只在 `domain="finance"` 生效
- 其他 domain 保持 DESIGN_RESTRUCTURE.md 的 ChainExecutor 架构不变

### 11.2 TraceCollector 提取策略：JSON 优先

**决策**：TraceCollector 的 `_extract_*` 方法优先从 Agent 输出的 JSON 中直接取字段，正则匹配仅作 fallback。

**依据**：
- 用户端需要快速判断做决策，结构化数据比自由文本更可靠
- Agent 被强制输出标准 JSON（instructions + final_answer_checks），JSON 直接取字段 = 100% 可靠
- 正则匹配自由文本存在漏匹配风险（"75分" vs "评分：75" vs "score of 75"）
- JSON 提取失败 = Agent 违规输出，应记录异常日志，而非静默降级

**实现**：
- `_extract_from_json(answer)` → 尝试 `json.loads` 提取 JSON 块
- 各 `_extract_*` 方法：JSON 直接取 → 正则 fallback → 推断默认值
- JSON 解析失败时记录 warning 日志（`logger.warning("[TraceCollector] JSON 解析失败，降级正则")`）

### 11.3 Phase 1 范围：一趟水完成

**决策**：Phase 1 包含所有核心改动，不做分批交付。

**Phase 1 改动清单**：

| 改动 | 涉及文件 | 类型 |
|------|---------|------|
| 新建 qd_traces 表 | migrations/qd_traces.sql | 新增 |
| 新建 qd_skill_weights 表 | migrations/qd_traces.sql | 新增 |
| 重建 qd_factor_weights 表 | migrations/qd_traces.sql | 重建 |
| EvalNode 加 timeframe 字段 | chain/schema.py | 修改 |
| TraceCollector | agent/trace_collector.py | 新增 |
| TracedTool 包装 | agent/traced_tool.py | 新增 |
| JSON 标准化输出（instructions + checks + format） | agent.py | 修改 |
| agent.py 注入 TraceCollector | agent.py | 修改 |
| call_skill_tool 适配 TraceCollector | skills/call_skill_tool.py | 修改 |
| evaluator 重写（按 timeframe 取行情 + 单位时间收益率） | chain/evaluator.py | 重写 |
| instructions 注入权重信息 | agent.py | 修改 |
| 删除 planner.py | agent/planner.py | 删除 |
| 删除 chain/executor.py | agent/chain/executor.py | 删除 |
| 删除旧 qd_evaluations 相关代码 | agent/agent.py, chain/store.py | 清理 |
| DB 初始化脚本更新 | chain/store.py | 修改 |

**Phase 2+（后续迭代）**：
- Skill 层 algo_analyze 实现（8 个可纯算法 Skill）
- Tool 层数据源可靠度追踪
- 前端 DecisionCard 可视化
- 回测结果可视化

### 11.4 旧数据策略：废弃不迁移

**决策**：qd_evaluations 旧数据不迁移到 qd_traces，新表从零开始。

**依据**：
- 旧表结构与新表不兼容（无 timeframe、无 hold_days、无 exit_reason）
- 旧数据中 freeform 路径的迷你树质量低（事后硬解析，字段不全）
- 迁移成本高于收益，且迁移后数据一致性无法保证

**冷启动策略**：
- 出厂权重直接 INSERT（qd_skill_weights / qd_factor_weights）
- 盘后定时任务 `evaluate_pending()` + `update_weights()` 逐步积累
- 预计 50+ 笔已验证决策后权重有统计意义
- 旧表 qd_evaluations 保留只读，仅作历史查询参考

### 11.5 Skill 自注册：evaluator 自动同步 registry

**决策**：增删 Skill 文件无需手动改 `qd_skill_weights` 表，由 `evaluator.update_skill_weights()` 自动同步。

**依据**：
- Skill 通过 `@skill` 装饰器自动注册到 `skill_registry`（`discover()` 扫描 `skills/` 包）
- `update_skill_weights()` 运行时先查表中已有 Skill，再和 `skill_registry.all_names` 对比
- 新 Skill → `INSERT (skill_name, weight=default_weight, sample_count=0)`
- 旧 Skill → 保留（不删除，避免丢失历史权重数据）

**效果**：加 `skills/xxx.py` → 重启 → 自动到位。零维护。

**实现**（`evaluator.py` `update_skill_weights()` 开头）：
```python
cur.execute("SELECT skill_name FROM qd_skill_weights")
existing = {row['skill_name'] for row in cur.fetchall()}
for name in skill_registry.all_names:
    if name not in existing:
        cls = skill_registry.get_class(name)
        default_w = getattr(cls, "default_weight", 1.0) if cls else 1.0
        cur.execute("INSERT INTO qd_skill_weights ... ON CONFLICT DO NOTHING", (name, default_w))
```

### 11.6 因子权重自动清理

**决策**：`update_factor_weights()` 每次运行时自动清理近 N 天内未出现的过期因子。

**依据**：
- 因子名由 Skill 的 `analyze()` 动态产出（如 "趋势"、"量价"、"MACD"），不是固定的
- Skill 重构后可能不再产出某些因子，旧因子残留在 `qd_factor_weights` 表中成为垃圾
- 不清理 → 表只增不减，长期积累无效数据

**实现**：更新时记录 `active_keys = {(skill_name, factor_name)}`，写入完成后 DELETE 不在 `active_keys` 中的记录。

**效果**：因子随 Skill 实际产出自动增减，无需手动清理。

## 十二、关键实施设计（恢复/重写参考）

> 以下设计细节在 Phase 1 实施过程中确定，是代码实现的依据。
> 如果代码被改坏，按这里的设计恢复。

### 12.1 Agent 类型双格式输出

**背景**：smolagents 有两种 Agent 类型，输出格式完全不同。

| Agent 类型 | 环境变量 | 输出格式 | final_answer 调用方式 |
|-----------|---------|---------|---------------------|
| CodeAgent | `AGENT_TYPE=code`（默认） | Python 代码 | `final_answer({...})` |
| ToolCallingAgent | `AGENT_TYPE=tool` | JSON tool call | `{"name":"final_answer","arguments":{...}}` |

**`env.example` 默认 `AGENT_TYPE=tool`**，即生产环境用 ToolCallingAgent。

**设计决策**：`_build_instructions` 根据 `_get_agent_class()` 返回值注入不同格式的指令。

**ToolCallingAgent 格式**：
```json
{
    "name": "final_answer",
    "arguments": {
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
        "analysis": "完整分析文字"
    }
}
```

**CodeAgent 格式**：
```py
final_answer({
    "action": "buy/sell/hold/skip",
    ...同上...
})
```

**数据流**：
```
agent.run() → result.output
  ├─ ToolCallingAgent: output 是 dict（final_answer 工具的返回值）
  │   → 直接保留 dict，不转 str
  │   → format_decision_card(dict) 格式化给用户
  │   → json.dumps(dict) 传给 TraceCollector.on_agent_finish()
  │
  └─ CodeAgent: output 是 str（Python 代码执行结果）
      → 正则提取 JSON 块
      → json.loads → dict
      → format_decision_card(dict)
      → 原始 str 传给 TraceCollector.on_agent_finish()
```

**⚠️ 关键陷阱**：
- `str(dict)` 会转成 Python repr（单引号），JSON 正则匹配不上
- 所以 ToolCallingAgent 的 dict **不能** `str()` 转换
- `store.add_message()` 需要 str，所以统一 `str(content)` 存储
- `TraceCollector.on_agent_finish()` 需要 str，dict 用 `json.dumps()` 转换

### 12.2 筹码分布计算算法

**背景**：`CNStockDataSource` 没有原生筹码分布接口，需要从日K线计算。

**算法：衰减成本分布模型**

```
输入: 120 根日K线 (time, open, high, low, close, volume)
参数: decay_half_life = 30 天

1. 确定价格区间 [min(low), max(high)]
2. 分档: step = (max - min) * 0.5%, 至少100档
3. 逐日处理:
   - days_ago = (today - bar.time) / 86400
   - weight = exp(-ln(2) / half_life * days_ago)    # 衰减
   - low_idx = (bar.low - min) / step
   - high_idx = (bar.high - min) / step
   - chips[low_idx..high_idx] += bar.volume * weight / spread
4. 汇总计算:
   - profit_ratio = sum(chips[i] where price[i] <= current_price) / total
   - avg_cost = sum(chips[i] * price[i]) / total
   - 90%区间 = 去掉上下各5%筹码后的价格范围
   - concentration = 90%区间宽度 / avg_cost * 100
   - peak_price = chips 最大值对应的价格
```

**返回字段**：
```python
{
    "stock_code": str,
    "current_price": float,
    "profit_ratio": float,      # 获利比例 %
    "trapped_ratio": float,     # 套牢比例 % = 100 - profit_ratio
    "avg_cost": float,          # 平均成本
    "peak_price": float,        # 筹码密集峰值价
    "chip_range_90": float,     # 90%筹码价格区间宽度
    "concentration": float,     # 集中度 %（越小越集中）
    "support_1": float,         # 90%筹码下沿（支撑位）
    "resistance_1": float,      # 90%筹码上沿（压力位）
    "lookback_days": int,
    "data_source": "kline_calc" # 标记为计算值
}
```

**调用路径**：`get_chip_distribution(stock_code)` → 数据源原生接口优先 → 不支持时自动走 `_calc_chip_from_kline()`。

### 12.3 format_decision_card 标准卡片

**代码控制格式，agent 只填数据。**

```python
def format_decision_card(data: dict) -> str:
    """
    输入: agent 输出的 JSON dict（必须包含 action/score/direction/confidence/timeframe/signal/factors/analysis）
    输出: 用户可见的标准卡片文本
    """
    # 格式:
    # **买入** 贵州茅台(600519)
    # 维度:3天 评分:72 方向:看多 置信:高
    # 技术面:75 | 动量:80 | 情报:45
    # 信号: 技术面+动量双多，情报面中性
    #
    # <details><summary>详细分析</summary>
    # ...analysis...
    # </details>
```

**中英文映射**：
- action: buy→买入, sell→卖出, hold→观望, skip→跳过
- direction: bullish→看多, bearish→看空, neutral→中性
- confidence: high→高, medium→中, low→低
- timeframe: T+1→1天, T+3→3天, T+5→5天, 1W→1周, 1M→1月, 3M→3月, 1Y→1年

## 十三、Phase 1 实施缺陷记录

> 日期: 2026-06-09
> 以下是 Phase 1 集成测试中发现的设计遗漏和实现缺陷，已全部修复。

### 12.1 TracedTool 缺少 to_code_prompt 代理

**现象**：`'TracedTool object' has no attribute 'to_code_prompt'`

**根因**：smolagents Jinja 模板渲染系统提示词时调用 `tool.to_code_prompt()`，`TracedTool` 不继承 `Tool`（因为 `Tool.__init__` 会校验 `forward` 签名），也未代理此方法。

**修复**：`traced_tool.py` 添加 `to_code_prompt()` 和 `to_tool_calling_prompt()` 代理方法。

**设计补充**：`TracedTool` 应使用 `__getattr__` 兜底代理，防止 smolagents 升级后新增方法再次断裂：

```python
def __getattr__(self, name):
    return getattr(self._tool, name)
```

### 12.2 chain/__init__.py 导入名不匹配

**现象**：`cannot import name 'get_skill_weights' from 'app.agent.chain.store'`

**根因**：`store.py` 中函数名为 `get_skill_weights_from_db`，但 `__init__.py` 导入时使用旧名 `get_skill_weights`。

**修复**：`__init__.py` 添加别名 `get_skill_weights = get_skill_weights_from_db`。

### 12.3 金融域 _try_chain 引用已删除模块

**现象**：`No module named 'app.agent.planner'`

**根因**：Phase 1 删除了 `planner.py` 和 `executor.py`，但 `_try_chain` 方法仍在引用。金融域按设计应由 agent 自规划，不走 Planner/ChainExecutor 路径。

**修复**：`_chat_locked` 和 `_chat_stream_locked` 中，`domain == "finance"` 时跳过 `_try_chain`。

**注意**：非金融域的 `_try_chain` 路径仍然断裂（planner/executor 已删除），需要后续决策：
- 方案 A：恢复 planner/executor（保留非金融域的链路编排能力）
- 方案 B：所有域统一走 agent 自规划（彻底删除 `_try_chain`）

### 12.4 工具缓存污染导致 call_skill 重复

**现象**：`Each tool or managed_agent should have a unique name! You passed these duplicate names: ['call_skill', 'call_skill']`

**根因**：`get_smolagent` 中，首次调用时 `tools` 直接引用缓存列表（非拷贝），`tools.append(call_skill)` 修改了缓存原始列表。后续调用时 `list()` 拷贝出的列表已含 `call_skill`，再 append 一个就重复了。

```python
# 缺陷代码
_tools_cache_by_domain[domain_key] = tools  # 缓存引用
tools.append(call_skill)                     # 修改了缓存！
# 第二次调用
tools = list(_tools_cache_by_domain[...])    # 已含 call_skill
tools.append(call_skill)                     # 重复！
```

**修复**：`list()` 拷贝移到 `if` 块外，每次调用都用独立副本。

### 12.5 默认 timeframe 缺失导致 agent 选择超长周期

**现象**：agent 对"分析宇通客车"输出 `timeframe: "1Y+"`，等于没分析。

**根因**：instructions 中 timeframe 规则只说"必须声明"，未约束默认值。agent 选最安全的超长周期以避免"预测错误"。

**修复**：instructions 中明确：
- 用户未指定时间 → **默认 T+3**（3个交易日短线）
- 禁止 1Y/1Y+ 作为默认值
- 用户明确问中长期时才用 1M/3M/1Y

### 12.6 已知遗留问题（待后续迭代）

| 问题 | 严重度 | 说明 |
|------|--------|------|
| Skill 内部工具调用未被追踪 | 中 | `CallSkillTool` 内部的 `call_tool_fn` 走裸工具，不经过 `TracedTool`，Tool 层追踪缺失 |
| `save_tree` 未写入 session_id/user_query | 中 | `TraceCollector` 构建的元数据在存库时丢失 |
| `_extract_from_json` 正则太窄 | 低 | 裸 JSON（无 ```json 包裹）且含嵌套 `{}` 时可能漏匹配 |
| 权重注入静默失败 | 低 | 数据库异常时 agent 无权重信息运行，应至少 `logger.warning` |
| `_check_output_json` 无降级路径 | 低 | 重试 2 次仍失败则直接报错，应降级为自由文本输出 |
| 非金融域 `_try_chain` 断裂 | 中 | planner/executor 已删除，非金融域链路编排不可用 |

## 十四、回测引擎设计（待实施）

> 日期: 2026-06-10
> 状态: 设计方案，待当前 bug 修复后实施

### 目标
用历史数据快速回测 agent 收益率，迭代因子权重和评分规则，让 agent 处于最佳工作状态。

### 核心矛盾
agent 调一次要烧 token，120 天逐天跑 = 120 倍成本。需要分层设计。

### 三层回测方案

#### 第一层：指标快照回测（0 token，秒级）

**不跑 agent，只跑指标。** 预计算每天的技术指标快照，用规则模拟 agent 的因子评分。

```
历史每天:
  → 计算 MA/MACD/RSI/BOLL/KDJ 等指标
  → 按 agent 的评分规则算 factors 分数
  → 加权得出 score → direction → action
  → 对比 T+3 实际涨跌
```

扩展 evaluator.py 的"验证已有决策"为"全量扫描"，回测任意时间段内 agent 会做哪些决策。

**用途**：快速验证因子权重、评分规则是否有效，筛选最优参数组合。

**实现要点**：
- 从 `cn_stock.get_kline()` 拉取历史日K线
- 逐天计算技术指标（纯 numpy/pandas，不调工具）
- 按 `qd_factor_weights` 的权重加权评分
- 对比 T+3/T+5 实际涨跌，统计胜率/收益率/回撤
- 支持网格搜索：批量跑不同权重组合，找最优

#### 第二层：Skill 级回测（少量 token，分钟级）

只跑 `call_skill`，不跑完整 agent。

```
历史关键节点（金叉/放量/突破等信号日）:
  → 调 technical_agent / momentum_tracker 等 Skill
  → 拿到 SkillReport（score/direction/factors）
  → 对比实际盈亏
```

**用途**：验证每个 Skill 的预测能力，迭代 `qd_skill_weights`。

#### 第三层：全量 Agent 回测（大量 token，小时级）

完整跑 agent，但只挑高价值节点：

```
筛选条件:
  - 技术面出现重大信号（金叉/死叉/突破/放量）
  - 多因子评分出现极端值（>75 或 <25）
  - 市场情绪转折点
```

**用途**：端到端验证 agent 决策质量，成本可控。

### 与现有组件的关系

| 已有组件 | 回测中的角色 |
|---------|------------|
| evaluator.py | 盘后验证已有决策（回测的子集） |
| qd_skill_weights | 回测的目标优化对象 |
| qd_factor_weights | 回测的权重参数 |
| qd_traces | 存储回测结果 |
| cn_stock.get_kline() | 历史数据源 |
| 技术指标工具 | 第一层回测的指标计算引擎 |

### 需要新增

| 组件 | 说明 |
|------|------|
| `chain/backtester.py` | 回测引擎主模块 |
| `qd_backtest_runs` 表 | 回测运行记录（参数/结果/耗时） |
| `qd_backtest_trades` 表 | 回测交易明细（每笔决策+实际盈亏） |
| 回测 API | `POST /api/backtest/run` 触发回测 |
| 回测可视化 | 前端展示回测曲线/统计指标 |

### 关键指标

```
总收益率、年化收益率、最大回撤、夏普比率
胜率、盈亏比、单位时间收益率
按 Skill/因子维度拆解贡献度
```

### 实施优先级

1. **第一层先行**：0 token，纯数学，能快速验证权重迭代逻辑
2. 第二层补充：验证 Skill 层预测能力
3. 第三层收尾：端到端验证

## 十五、Domain 解耦重构（Phase 1-3 已完成）

> 日期: 2026-06-15（初稿）→ 2026-06-15（Phase 1-3 完成）
> 状态: **已完成**（Phase 1-3 + 统一 Front Matter MD + 选股技能合并）
> 背景: domain 同时承担「路由开关」「能力注册」「context 裁剪阀」三重职责，
>       导致 skill/tool 更纠缠而非更清晰，且 cron 等跨域场景被迫选边。

### 15.1 矛盾分析

domain 设计初衷是按领域特化执行路径，但实际上同时承担了三个职责：

| 职责 | 例子 | 问题 |
|------|------|------|
| 路由开关 | domain="finance" → 走 TraceCollector | cron 任务想用 finance agent 得绕 domain 判断 |
| 能力注册 | @skill(domain="finance") | skill 被标签锁死，跨域复用要改装饰器 |
| context 裁剪 | domain 过滤 → 只加载该域的工具描述 | 这是唯一合理的用途，但实现方式太粗暴 |

**根源**：domain 既是「这个能力属于哪」（标签），又是「这个任务怎么跑」（策略），
又是「context 加载什么」（裁剪）。三件事绑一个字段，耦合不可避免。

### 15.2 解耦方案（已实施）

**核心思路：domain 从「路由+注册+裁剪」三合一 → 降级为纯「标签」→ 彻底移除。**

#### ① 能力层：skill/tool 只打 tags，domain 概念移除 ✅

```python
# 已完成
@skill("technical_agent", auto_load=True)
class TechnicalAgent(BaseSkill): ...

# tags 从 SKILL.md frontmatter 自动加载
# tools/category 从 semantics 自动补全
```

- tags 是多值列表，一个 skill 可以属于多个标签组
- 任何执行路径都能按 tags 组合调用，不被 domain 锁死

#### ② 策略层：executor 级显式配置 ✅

```python
# 已完成
strategy = intent.strategy  # "traced" / "chain" / "direct"
if strategy == "traced":
    collector = TraceCollector(...)
elif strategy == "chain":
    executor = ChainExecutor(...)
elif strategy == "direct":
    pass  # 纯 agent 自由推理
```

- strategy 由 IntentAnalyzer 计算，不从 domain 推断
- finance/trading → strategy="traced"
- 其他 → strategy="direct"

#### ③ context 裁剪：Front Matter MD 两段加载 ✅

```
第一段：system prompt 注入（~500 token）
  skills_summary_xml — 每个 skill 一行（名称 + 描述 + tags）
  tools_summary_xml — 按 category 分组的工具索引

第二段：call_skill 时按需加载
  Agent 调 call_skill("technical_agent")
    → 从 SKILL.md body 加载完整 instructions
    → 返回 SkillReport
```

#### ④ domain 概念彻底移除 ✅

- `domain_registry.py` → 已删除
- `domains.yaml` → 已删除
- IntentAnalyzer 的 `domain_instructions` → 从 `persona.md` behaviors 生成
- Agent 的 `init_builtin_domains()` → 已移除

### 15.3 改动清单（已实施）

| 改动 | 涉及文件 | 状态 |
|------|---------|------|
| domain 降级为 tags | skills/registry.py, tools/registry.py | ✅ 已完成 |
| @skill 支持 auto_load | skills/registry.py | ✅ 已完成 |
| @tool tags 从 semantics 补全 | tools/registry.py | ✅ 已完成 |
| strategy 配置化 | agent.py (_AgentExecutor) | ✅ 已完成 |
| 分层描述注入 | agent.py (_build_instructions) | ✅ 已完成 |
| domain_registry 删除 | domain_registry.py | ✅ 已删除 |
| domains.yaml 删除 | domains.yaml | ✅ 已删除 |
| persona.md 统一行为规范 | semantics/persona.md | ✅ 已完成 |
| intent.md 统一意图规则 | semantics/intent.md | ✅ 已完成 |
| chains.md 统一链路定义 | semantics/chains.md | ✅ 已完成 |
| tools.md 统一工具元数据 | semantics/tools.md | ✅ 已完成 |
| SKILL.md 统一为单文件 | semantics/skills/*.md | ✅ 已完成 |
| 选股技能合并 | semantics/skills/screener.md | ✅ 已完成 |

### 15.4 语义文件结构（最终）

```
semantics/
├── __init__.py              # 加载器（_load_frontmatter + names 列表支持）
├── persona.md               # 人设 + 行为规范（7 个行为域）
├── intent.md                # 意图分类（15 rules + 3 patterns + prompt body）
├── chains.md                # 链路编排（3 条链路 16 步）
├── tools.md                 # 工具元数据（14 categories, 79 tools）
└── skills/
    ├── screener.md          # 选股技能（names: short_term/eod/post_market）
    ├── technical_agent.md
    ├── hot_money_tracker.md
    ├── indicator_agent.md
    ├── intelligence_agent.md
    ├── market_data_agent.md
    ├── backtest_agent.md
    ├── bull_researcher.md
    ├── bear_researcher.md
    ├── screening_agent.md
    ├── lockup_watcher.md
    ├── data_agent.md
    ├── bb_screener.md
    └── trading_agent.md
```

**统一格式**：全部 Front Matter MD
- frontmatter 存结构化元数据（name/names/tags/tools/priority 等）
- body 存文本内容（instructions/prompt/说明文档）
- 改描述只改 .md 文件，不改 Python 代码

**names 列表**：一个 .md 文件可注册多个 skill（如 screener.md 注册 3 个）

### 15.5 代码改动汇总

| 文件 | 改动 |
|------|------|
| `skills/registry.py` | @skill 新增 auto_load，从 SKILL.md 加载 |
| `tools/registry.py` | @tool tags/category 从 semantics 自动补全 |
| `chain/chains.py` | 链路从 chains.md frontmatter 加载 |
| `agent.py` | _load_preamble 从 persona.md 加载 |
| `intent_analyzer.py` | domain_instructions 从 persona.md behaviors 生成 |
| `run.py` | 领域信息从 persona.md 读取 |
| `semantics/__init__.py` | 统一 _load_frontmatter + names 列表支持 |

### 15.6 与现有设计的关系

| 现有设计 | 影响 |
|---------|------|
| AGENT_ACCOUNTABLE §三 TraceCollector | 不变，由 strategy="traced" 触发 |
| AGENT_ACCOUNTABLE §四 权重迭代 | 不变，按 skill_name 聚合，不依赖 domain |
| AGENT_ACCOUNTABLE §六 JSON 标准化输出 | 不变 |
| AGENT_ACCOUNTABLE §七 instructions 注入 | 改为两段加载，不再按 domain 整段注入 |
| AGENT_ACCOUNTABLE §十一 Domain 隔离方案 | **被本节替代** |

## 十六、四个闭环

> 日期: 2026-06-16
> 状态: 已验证，代码已实现

### 总览

```
用户消息 ──→ Agent 处理 ──→ 返回用户
                │
                ├──→ 在线评估 ──→ 学习闭环（写 tool_chains）
                │
                ├──→ 写入 DB（EvalNode 树）
                │         │
                │         └──→ 异步回测闭环（T+N 验证 → 权重迭代）
                │
                └──→ 用户反馈 ──→ 惩罚闭环（负面 → 扣减/删除链路）

意图分析 ──→ 编排闭环（读 tool_chains → 编排执行）
```

### 16.1 学习闭环

**流程**: Agent 自由发挥 → 在线评估 → 写入 tool_chains.json

**触发**: 每次 `agent.run()` 结束后自动调用（`_post_evaluate()`）

**实现**:
- `evaluator.py:evaluate()` — 纯规则评估（<1ms，0 token）
  - 工具调用成功率
  - 工具链遵循度（实际调用 vs 预期 chain 匹配度）
  - 步骤效率（实际步数 vs chain 长度）
  - final_answer 是否生成
  - 响应是否包含实际数据
- `evaluator.py:learn_from_execution()` — 闭环动作
  - success + agent 遵循 chain → `update_chain_stats()` 强化统计
  - success + agent 偏离 chain → 质量门检查 → 通过后才 `save_tool_chain()`
  - failure → `_record_failure()` 写入 tool_chain_failures.json
- `evaluator.py:_writeback_chain()` — 质量门（5 项拦截，全部放行才写入）
  1. 步数 > 5 → 拦截（太低效）
  2. 新链长度 > 5 个工具 → 拦截（太臃肿）
  3. 评分 < 60 → 拦截（质量不足）
  4. 工具成功率 < 50% → 拦截（工具不可靠）
  5. 旧链执行 ≥ 10 次且成功率 ≥ 80% → 拦截（旧链已验证，不轻易替换）
- `router/tool_chains.py:update_chain_stats()` — 增量均值算法更新 avg_steps / executions / success_rate

**数据流**:
```
agent.run() 完成
  → evaluate(result, chain, verb, noun) → EvalResult(verdict/score)
  → learn_from_execution(eval_result, verb, noun)
      ├─ success + 偏离 → save_tool_chain(verb, noun, actual_tools)
      ├─ success + 遵循 → update_chain_stats(verb, noun, steps, True)
      └─ failure → update_chain_stats(verb, noun, steps, False) + _record_failure()
```

**文件**: `evaluator.py` L491-580, `router/tool_chains.py`

### 16.2 编排闭环

**流程**: 读取 tool_chains.json → 编排执行 → 结果交给 Agent

**触发**: 每次用户消息进入 `_try_chain()`

**实现**:
- `intent_analyzer.py` — 意图分析时读取 chain stats
  - `get_tool_chain(verb, noun)` — 获取预配置的工具链步骤
  - `get_chain_stats(verb, noun)` — 获取链路统计（成功率、平均步数）
- `agent.py._try_chain()` — 链路执行
  - 匹配 verb+noun → 按 tool_chains.json 的 steps 顺序执行
  - 未匹配 → `planner.py` LLM 规划 → 存入 tool_chains.json
  - 规划失败 → 返回 None（让 agent 自由处理）
- `router/tool_chains.py:get_tool_chain()` — 读取链路配置

**数据流**:
```
用户消息
  → intent_analyzer → verb + noun
  → _try_chain(verb, noun)
      ├─ tool_chains.json 有链路 → 按 steps 执行 → 返回 AgentResult
      ├─ 无链路 → planner LLM 规划 → save_tool_chain() → 执行
      └─ 规划失败 → 返回 None → agent 自由处理
```

**文件**: `agent.py:_try_chain()`, `intent_analyzer.py`, `planner.py`, `router/tool_chains.py`

### 16.3 惩罚闭环

**流程**: 用户负面反馈 → 惩罚 tool_chains → 低质量链路被清除

**触发**: 每次用户消息自动检测（`_check_negative_feedback()`）

**实现**:
- `router/tool_chains.py:detect_feedback_severity()` — 关键词检测
  - 轻度: "不对"、"不好"、"不准"、"有问题" → success_count - 1
  - 重度: "完全不对"、"大错特错"、"离谱"、"垃圾" → success_count - 2
- `router/tool_chains.py:penalize_chain()` — 执行惩罚
  - success_count <= 0 或 success_rate < 0.2 → 删除链路
- `agent.py._check_negative_feedback()` — 双层惩罚
  - tool_chains 层: penalize_chain() 扣减/删除
  - trace 层: mark_root_wrong() 标记错误，累计 3 次删除整棵 trace 树

**数据流**:
```
用户消息（含负面关键词）
  → detect_feedback_severity(message) → "mild" / "severe" / None
  → _check_negative_feedback(message, session_id)
      ├─ trace 层: mark_root_wrong(root_id) / delete_tree(root_id)
      └─ tool_chains 层: penalize_chain(verb, noun, severity)
            ├─ 轻度 → success_count -= 1
            ├─ 重度 → success_count -= 2
            └─ 累计触发 → 删除链路
```

**文件**: `agent.py:_check_negative_feedback()` L1193-1260, `router/tool_chains.py:penalize_chain()`

### 16.4 异步回测闭环

**流程**: 用户消息 → Agent 处理 → 写入 DB → (T+N 天) → 获取实际行情 → 验证结果 → 修改权重

**触发**: 后台 Worker 每 4 小时自动运行（`start_eval_worker()`），或 API 手动触发

**实现**:
- `chain/store.py:save_tree()` — Agent 执行时 EvalNode 树存库（含 action / score / direction / timeframe）
- `chain/evaluator.py:evaluate_pending()` — 查找 exit_date IS NULL 的记录
  - 按 timeframe 映射持有天数: T+1=1, T+3=3, T+5=5, 1W=5, 1M=22
  - `_get_actual_return()` — 从数据源获取实际涨跌
  - `is_direction_correct()` — 预测方向 vs 实际方向对比
  - `update_verify_results()` — 写回 exit_date / pnl_pct / hold_days / correct
- `chain/evaluator.py:update_skill_weights()` — 按单位时间收益率更新 Skill 权重
  - 核心指标: `return_per_day = (win_rate × avg_win - loss_rate × avg_loss) / avg_hold_days`
  - 映射到权重: `weight = clamp(1.0 + return_per_day × 20, 0.5, 2.0)`
  - 自动同步 registry: 新 Skill → INSERT 工厂默认值
- `chain/evaluator.py:update_factor_weights()` — 按因子维度聚合权重（带时间衰减）
  - 半衰期: 政策/消息=7天, 游资/资金=14天, 概念/板块=21天, 技术指标=30天, 量价=60天
  - 自动清理过期因子（近 N 天内未出现的 → DELETE）
- 权重注入: `_build_instructions()` → `get_skill_weights()` → 注入 agent instructions

**数据流**:
```
T日: 用户问 "600519 能不能买"
  → Agent 执行 → EvalNode 树存库
      root: chain, action=buy, score=72, direction=bullish, timeframe=T+3
        ├─ skill: technical_agent, score=75, direction=bullish
        │   ├─ tool: agent_get_kline
        │   └─ tool: analyze_trend
        └─ skill: momentum_tracker, score=80, direction=bullish

T+3: 后台 Worker 自动运行（每 4 小时）
  → evaluate_pending()
      → _get_actual_return("600519", T日, 3) → pnl_pct=+5.2%
      → is_direction_correct("bullish", "bullish") → correct=True
      → update_verify_results(root_id, exit_date, pnl_pct, correct)
  → update_skill_weights()
      → technical_agent: 100笔, 胜率65%, 单位时间收益率+0.48%/天 → weight=1.96
      → momentum_tracker: 80笔, 胜率45%, 单位时间收益率+1.25%/天 → weight=2.0
  → update_factor_weights()
      → 趋势因子: 0.48%/天 → weight=1.96
      → 量价因子: 0.35%/天 → weight=1.70

下次执行: 权重自动注入 agent instructions
  → "technical_agent | 权重 1.96 | +0.48%/天 | 120 笔"
```

**文件**: `chain/evaluator.py`, `chain/store.py`, `app/__init__.py` (启动 worker), `routes/agent_blueprint.py` (API 触发)

### 16.5 闭环间的关系

| 闭环 | 时间尺度 | token 消耗 | 数据载体 |
|------|---------|-----------|---------|
| 学习闭环 | 实时（每次执行后） | 0（纯规则） | tool_chains.json |
| 编排闭环 | 实时（每次用户消息） | 0（读 JSON） | tool_chains.json |
| 惩罚闭环 | 实时（每次用户消息） | 0（关键词匹配） | tool_chains.json + qd_traces |
| 异步回测闭环 | T+N 天（每 4 小时检查） | 0（纯 SQL + 数学） | qd_traces + qd_skill_weights + qd_factor_weights |

**学习闭环** 和 **惩罚闭环** 共同维护 tool_chains.json 的质量：
- 学习 → 成功路径写入/强化
- 惩罚 → 失败路径扣减/清除

**异步回测闭环** 独立于前三者，维护权重表：
- tool_chains.json 管"怎么走"（路径选择）
- qd_skill_weights / qd_factor_weights 管"信多少"（权重校准）

## 十七、Phase 1 流程重构（Planner 前置）

> 日期: 2026-06-16
> 状态: 已实施
> 目标: Planner 成为唯一编排器，Agent 变为纯执行器

### 17.1 设计原则

**第一阶段只有一个语义注入点：Planner。**

```
_prepare_intent() ─────────────────────────────────────
│  1. 意图分析      ← 纯分类器，无语义（只有 intent.md 的规则）
│  2. 快速通道判断   ← 正则，无语义
│  3. 提取 stock_code ← 正则，无语义
│  4. 创建 TraceCollector ← 数据结构，无语义
│  5. 上下文拼接     ← 纯数据拼接，无语义
│  6. Planner        ← 唯一语义注入点
│     ├─ persona（人设）
│     ├─ context（对话历史）
│     ├─ skills（全量 skill 摘要）
│     ├─ tools（全量 tool 摘要）
│     └─ rules（planner.md 规则）
│     → 输出: 执行计划
└──────────────────────────────────────────────────────
```

其余步骤都是纯数据/正则操作，不涉及任何语义描述。

### 17.2 流程对比

**改造前**：
```
用户消息
    │
    ▼
_prepare() ─────────────────────────────────────────
│  1. 意图分析 (analyze_intent) ← LLM #1
│  2. 快速通道判断
│  3. 提取 stock_code
│  4. 创建 TraceCollector
│  5. 上下文拼接
│  6. 构建 Agent (get_smolagent) ← 全量 skill/tool
└──────────────────────────────────────────────────────
    │
    ▼
_chat_locked() ─────────────────────────────────────
│  7. 负面反馈检测
│  8. _try_chain() ← LLM #2 (Planner，无固定链路时)
│  9. 链路结果注入 enriched
│ 10. agent.run() ← LLM #3
│ 11. 结果处理 + DecisionCard
│ 12. 后置评估 + 学习闭环
└──────────────────────────────────────────────────────
```

**问题**：
- 步骤6在步骤8之前，Agent 全量构建后才知道需要什么
- Planner 只拿到 SKILL_CATALOG（硬编码12个skill名），没有人设、工具、上下文

**改造后**：
```
用户消息
    │
    ▼
_prepare_intent() ─────────────────────────────────
│  1. 意图分析 ← LLM #1
│  2. 快速通道判断
│  3. 提取 stock_code
│  4. 创建 TraceCollector
│  5. 上下文拼接
│  6. Planner ← LLM #2
│     注入: 人设+上下文+全量skill+全量tool+规则
│     输出: [{skill, tools}, ...] 执行计划
└──────────────────────────────────────────────────────
    │
    ▼
_chat_locked() ─────────────────────────────────────
│  7. 负面反馈检测
│  8. 构建精简Agent (只加载计划中的 skill/tool)
│  9. agent.run() ← LLM #3 (纯执行，不编排)
│ 10. 结果处理 + DecisionCard
│ 11. 后置评估 + 学习闭环
└──────────────────────────────────────────────────────
```

**改进**：
- Planner 是唯一的编排决策者，拿到完整上下文
- Agent 只加载需要的 skill/tool，不再全量
- Agent 变为纯执行器，不需要思考"为什么选这个 Skill"

### 17.3 Planner 注入内容

Planner 的 prompt 包含：

| 内容 | 来源 | 用途 |
|------|------|------|
| persona | `semantics/persona.md` | 告诉 Planner 它是谁 |
| context | `store.get_context_summary()` | 对话历史摘要 |
| skills | `get_skills_summary_xml()` | 全量 skill 列表+描述 |
| tools | `get_tools_summary_xml()` | 全量 tool 列表+描述 |
| rules | `semantics/planner.md` | 核心哲学+数据陷阱+技能优先级+输出格式 |

### 17.4 语义文件职责分离

| 文件 | 注入到 | 职责 |
|------|--------|------|
| `planner.md` | Planner LLM | 选什么 Skill、为什么选、用什么工具 |
| `guidance.md` | Agent instructions | 按顺序执行、怎么调 call_skill |
| `persona.md` | Planner + Agent | 人设 |
| `intent.md` | IntentAnalyzer | 意图分类规则 |

**planner.md 内容**：
- 核心哲学：价格折扣一切
- 数据陷阱警告（影响 Skill 权重选择）
- 技能优先级表（从高到低）
- 场景 → 技能组合示例
- 输出格式（JSON，包含 skill + tools）

**guidance.md 内容**：
- 执行规则（按计划顺序执行）
- call_skill 用法

### 17.5 Planner 输出格式

Planner 输出执行计划，包含每个步骤的 skill 和 tools：

```json
{
  "steps": [
    {
      "skill": "technical_agent",
      "tools": ["analyze_trend", "get_indicator_snapshot", "agent_get_kline"]
    },
    {
      "skill": "intelligence_agent",
      "tools": ["search_stock_news", "get_market_sentiment"]
    }
  ],
  "stocks": ["600519"],
  "reasoning": "选择理由（50字以内）"
}
```

### 17.6 学习闭环质量门

`_writeback_chain()` 新增 5 道质量门，防止脏数据写入 tool_chains.json：

| 门 | 条件 | 拦截原因 |
|---|------|----------|
| 1 | 步数 > 5 | 太低效 |
| 2 | 新链长度 > 5 | 太臃肿 |
| 3 | 评分 < 60 | 质量不足 |
| 4 | 工具成功率 < 50% | 工具不可靠 |
| 5 | 旧链 ≥10次 且 成功率 ≥80% | 旧链已验证，不轻易替换 |

全部通过才写入 `tool_chains.json`，每道门都有日志记录拦截原因。

### 17.7 涉及文件

| 文件 | 改动 |
|------|------|
| `agent.py` | `_prepare()` → `_prepare_intent()`，Planner 前置，Agent 构建后移 |
| `planner.py` | 注入 persona + context + 全量 skills + 全量 tools |
| `evaluator.py` | `_writeback_chain()` 新增 5 道质量门 |
| `semantics/planner.md` | 新增核心哲学、数据陷阱、技能优先级 |
| `semantics/guidance.md` | 精简为纯执行规则 |

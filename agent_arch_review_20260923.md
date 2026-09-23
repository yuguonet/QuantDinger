# QuantDinger app/agent 深度评估报告

> 来源：任务"app/agent 系统架构深度评估" | 日期：2026-09-23 | 署名：OpenClaw agent（外部评审）
> 依据：AGENT_DESIGN.md v8.0（用户上传）× backend_api_python/app/agent 全量核心代码交叉比对
> 约定：行号会漂移，定位以**函数/类名**为准；行号为 2026-09-23 快照参考。
> 本文为纯分析产物，生产代码零改动。

---

## 0. 总评

这套系统的**工程纪律在同类项目里属于罕见的高水平**：注释即档案、事故即契约、AUDIT-MASK 可追溯、
"静默断链"被识别为头号 bug 模式并建立防御原则。架构上"自研轻量 StateGraph + smolagents 执行层 +
能力层/技能层/追责链"的分层是清晰的，酿造-回测-修订的自进化闭环方向正确。

但本轮审计的核心发现是：**你们最引以为傲的防御原则，正在被自己的代码再次违反**——
最严重的一批 bug 全部是既有教训的复发（"except 只写 debug 的接线处都该怀疑"、"声明了但没接线"、
"示例权重压过规则"、"共享列不许一列两用"）。这说明问题已不是认知不足，而是**缺少把这些原则
变成机器可检出项的最后一环**。报告 C-5 给出闭环方案。

严重度分布：**P0 级 4 项（功能实际失效/闭环数据互踩）、P1 级 7 项、P2 级 9 项、文档漂移 8 项、
架构演进 7 条**。

---

## A. P0：功能实际失效的 bug

### A1. 数字溯源（幻觉数字防线）整体失效 —— 拒收被自己的 except 吞掉 ⭐最重

- **位置**：`agents/task_agent.py::_check_final_answer`（约 2033-2100 行）
- **机制**：两处 `raise ValueError("数字溯源失败…")`（总阈值分支 + 价格/金额分支）写在
  `try:` 块**内部**，而该 try 的收口是 `except Exception as e: logger.debug("…检查跳过", e)`，
  之后 `return True`。⇒ 拒收异常永远走不到 smolagents 的"要求重写"通道，
  **函数事实上恒返回 True**。
- **后果**：
  1. 2026-09-16 的"数字溯源"、2026-09-19 的"价格/金额类小数加严"两个修复**全部无效**；
     "tmp/1.txt 选股报告幻觉"那一类编造价格仍会被放行；
  2. 日志里能看到 `数字溯源失败…拒收要求重写` 的 warning，但模型从未被要求重写——
     **告警与行为脱节**，排查者会被日志误导以为防线在工作；
  3. 这是 MEMORY 里 L8 教训（"`except` 里只写 debug 的接线处都该怀疑"）在**防幻觉闸门上**的复发。
- **修复方向**（分析，不动代码）：两个 `raise` 移出 try 的捕获范围（或 except 里对
  `ValueError` 重新 raise）；同时注意 A1b。
- **A1b 连带问题**：溯源语料取自 `agent.memory.steps[].observations`，而 `_truncate_observations`
  已把历史 obs 截到 400 字符 ⇒ 即使拒收修好，全量真实数值在 `executor.state` 的 `_r_*` 与模型变量里，
  obs 语料是残缺的，会**误伤**（可溯源数值被判编造）。修 A1 时溯源语料应改从
  `executor.state`（非下划线变量 + `_r_*`）取。

### A2. brew_state 一行两用：fail_streak 与 low_streak 共享 weight 列，互相覆盖

- **位置**：`chain/store.py::get_brew_states / set_brew_state / get_skill_revision / set_skill_revision`
- **机制**：同一 `qd_agent_weights(layer='brew_state')` 行的 **weight 列**被两套状态机复用：
  - 酿造节拍：`get_brew_states` 读 weight 为 `fail_streak`，`set_brew_state` 写 weight=fail_streak；
  - 修订节拍：`get_skill_revision` 读 weight 为 `low_streak`，`set_skill_revision` 写 weight=low_streak。
  sample_count 同理被 revision 复用（此部分无冲突，冲突只在 weight）。
- **后果**：
  1. 修订失败 `low_streak+1` 写入 weight → 酿造通道③读到 `fail_streak≥3` → **该链酿造被误冷却**；
  2. 酿造失败 `fail_streak+1` 写入 weight → `maybe_revise` 读到 `low_streak≥2` → **该技能被误判
     "连续修订仍差→转淘汰"**（护栏 5 被伪造触发）；
  3. 两个独立状态机互相投毒，行为随写入顺序漂移，且无任何告警。
- **修复方向**：拆列（低改动：low_streak 存 weight 的负值/万分位偏移都行，但**必须单一语义**），
  或干脆拆行（`brew_state:` / `revise_state:` 两个 name 前缀）。这正是"复用列省 DDL"省出的债。

### A3. maybe_revise 的 revision 归零 bug —— 护栏 5「修差转淘汰」是死逻辑

- **位置**：`chain/skill_brewer.py::maybe_revise`（`if r["status"] == "revised": _ssr(real_chain,
  revision=r.get("revision", 0), ...)`）
- **机制**：`revise_skill` 的返回 dict 只有 `chain_name/status/skill_dir/version`，**没有 "revision" 键**；
  `r.get("revision", 0)` → 0 → 在 `revise_skill` 刚写完 `revision=rev+1` 之后**再写一次 revision=0** 覆盖。
- **后果**：revision 计数恒 0；虽然护栏 5 用的是 low_streak，但 revision 永不增长意味着
  "minor 位 +1 的版本演进史"与 header 注释的 `revision=N`（revise_skill 内部写的是 rev+1，
  与 DB 不一致）**三方对不上**；且 `set_skill_revision` 与 `revise_skill` 内部的重复写入本身违反单一事实源。
- **修复方向**：删除 maybe_revise 里的二次 `_ssr`（revise_skill 已写好），或让 revise_skill 返回 revision。

### A4. revise_skill 的"增量轨迹"实际喂了全量 —— since_id 算了不用

- **位置**：`chain/skill_brewer.py::revise_skill`
- **机制**：`m = re.search(r"from root_id=(\d+)")` → `since_id` **此后零引用**；
  `get_delta_digest(chain_name, since_date=None)` 取的是该链**全部历史 run**。
- **后果**：护栏 1（"原料是增量轨迹 + 旧文档，不是从零重编译"）名存实亡——旧文档已消化过的样本
  每轮修订反复进 prompt，造成重复强化与漂移；"只更新有依据的部分"的依据集不受控。
  典型的"变量算了不用"（自家高发 bug 模式清单第 5 条）。
- **修复方向**：`get_delta_digest` 支持 `since_root_id`（SQL 已有 id 列，加 `AND t.id > %s` 即可），
  修订原料 = 旧文档 + origin root_id 之后的 run。

### A5. skill_brewer 手动入口必崩：`os` 未导入的 NameError

- **位置**：`chain/skill_brewer.py::__main__`（`sys.path.insert(0, os.path.join(_bp, "app", "agent"))`）
- **机制**：模块头只 import 了 logging/re/shutil/date/Path/Optional，**没有 `import os`**。
- **后果**：设计文档 §3.16 承诺的手动酿造入口
  `python -m app.agent.chain.skill_brewer [min_runs]` **启动即 NameError**。
  与 §7.11 env 死声明同型——"文档声明的入口/配置，实际不可用"。
- **修复方向**：补 `import os`；并给该入口加一条最小冒烟（哪怕 `--dry-run`）。

### A6（升级为 P0-P1 之间）. 错误 run 零入库 —— 两处 docstring 承诺"留错误痕迹"，实际什么都不写

- **位置**：`utils/tracing.py::finish / fail`；`nodes.py::finalize_node`
- **机制**：`finish()` 只在 `final_answer and status == "success"` 时调 `_write_qd_traces`；
  `fail()` 传 `final_answer=None` ⇒ **error run 完全不写 qd_traces**。
  但 `fail()` 的 docstring 明写"qd_traces 会留一条带错误信息的根节点（供排查）"，
  finalize 注释也写"失败 run 只留错误痕迹"。**两处声明，零处兑现**。
- **后果**：网关故障/执行异常的 run 无任何 DB 痕迹，只在 JSONL（默认开）里；
  排查"某天为什么没分析"时 DB 视图是空白；`status='error'` 列成为死字段。
- **修复方向**：`_write_qd_traces` 拆两步——结构化决策提取仅 success，**根节点落库不论成败**
  （status/error 带上）。EvalNode.status 字段已在，DDL 就绪。

---

## B. P1：并发安全 / 配置面 / 契约质量

### B1. 多 worker 并发竞态：全局槽位 save/restore 模式互相覆盖（默认 4 worker）

- **位置**：`message_queue.py::_run_with_events`；`agents/task_agent.py` 的
  `_INTERRUPT_CHECKS`、`TaskAgent._current_event_cb`、`TaskAgent._user_step_callbacks`、
  `TaskAgent._active_code_agent`
- **机制**：`_prev_checks = list(...)` → append → `finally: _INTERRUPT_CHECKS[:] = _prev_checks`
  是经典的并发不安全 save/restore。A、B 两任务并发时：
  - A 先结束 → 恢复快照把 B 的探针一并抹掉 → **B 的"立即停止"静默失效**；
  - B 后结束 → 恢复出 A 的陈旧探针 → **探针泄漏**，且陈旧探针闭包着 A 的 session_id：
    A 的下一次 request_stop 会在 **B 的 run 里**抛 `_UserInterruptError` → **误杀别的会话**。
  - `_current_event_cb` 同款：并发 SSE 请求事件互相串流/丢失。
  - `_active_code_agent` 全局单槽 + `interrupt_switch` 检查不区分会话 → 一次 interrupt 全局生效。
- **触发条件**：Flask/Cron 并发 ≥2（mq worker 默认 4）；单用户 CLI 不易复现——与历史上
  "本地正常、远端 500"同类的隐蔽性。
- **修复方向**：这三个槽位应收进**每请求的 NodeContext / ExecutionContext**（NodeContext 已是
  per-request 的，P1-7 的 plan 变量就迁过一次）；探针列表改成 session→probe 的 dict。

### B2. `AGENT_MAX_STEPS` 一个变量三个默认值（6 / 20 / 5）

- **位置**：`agent.py:47`（默认 6）、`agents/task_agent.py::_plan`（默认 20，step_budget 上钳）、
  `nodes.py::make_plan_node`（默认 5，单段硬上限）
- **机制**：env 未配置时三个消费点各自为政；配置后一个变量承载三个语义
  （CodeAgent 初始轮数 / planner 预算上钳 / 单段上限）。§7.11 刚清理完"没人读的死声明"，
  这里是变体"三个读、三个样"。
- **修复方向**：单一常量源（如 `chain/constants.py` 同款思路），三处引用同一解析函数。

### B3. 真 exec 执行器 = 零安全边界，且 .env 就在工作目录

- **位置**：`infra/guided_executor.py::GuidedCPythonExecutor`
- **机制**：全量 builtins + import 全放行 + 无破坏性拦截（2026-09-18 起），文件头已如实声明
  "不是安全边界"。但 `backend_api_python/.env`（DATABASE_URL / OPENAI_API_KEY）与执行器
  同树，模型代码 `open('.env')` 或 `os.environ` 即可读到凭据；web_search 有 `_sanitize_result`
  注入消毒，**执行环境这侧反而没有对应的出口管控**。对外暴露 `/api/agent-v2/chat` 时这是真实攻击面。
- **修复方向**（按代价排序）：
  1. 最小：文件/网络/进程三类危险调用的**审计留痕**（不拦截，但写 trace，可追责）；
  2. 中：危险面 deny-list（.env/.ssh/凭据路径、socket 出口白名单）；
  3. 大：按官方路径换 `executor_type="e2b/docker"` 真隔离。
  并给"何时必须上隔离"定触发条件（如对外开放多租户前）。

### B4. 决策提取 regex 是整个自进化闭环的地基，但无质量保障

- **位置**：`utils/tracing.py::_extract_direction / _extract_action / _extract_score / _extract_confidence`
- **机制**：关键词命中即定方向——"**不**建议买入"照样命中"买入"→ bullish；
  "卖出"出现在风险提示里 → bearish。这些字段直接决定 T+N 的 `correct`、
  skill/tool 权重、酿造候选筛选（`win_rate≥0.7`）。
- **后果**：标注噪声以"数据"的身份进入权重与酿造原料，且**无法与真实错误区分**。
  结合 A2/A3，自进化闭环的数据质量链是：regex 提取（无校验）→ 二值 correct（无归因）→
  权重（共享列）→ 修订（全量原料）。每一环都有损耗。
- **修复方向**：① 提取时带置信度（JSON 结构化输出 > 关键词 regex，finalize 已要求模型输出
  JSON 的通道可复用）；② 否定语境过滤（"不建议/不宜/回避"前缀）；③ 抽样人工校准集，
  定期量测提取准确率。

### B5. code_agent.yaml 仍有幻觉工具名 `wikipedia_search`（§7.17-4 复发）

- **位置**：`prompts/code_agent.yaml`（规则 3 示例：`answer = wikipedia_search(query=...)`）
- **机制**：全项目零定义零注册（grep 验证），正是 §7.17-4 自己定义的"示例里的幻觉名会被模型
  当模板照抄"。§7.17 修掉了 `get_daily`，但同型的 `wikipedia_search` 留在规则 3 的**语法教学示例**里
  ——示例权重压过规则（§7.17 第 1 条），模型照抄即 NameError。
- **修复方向**：换成真实工具（如 `web_search(query=...)`）；把"yaml/plan_system 提取调用名 ×
  provider 注册表求差集"脚本化进 CI（§7.17 自查方法已写，差一个自动化）。

### B6. 领域硬编码违反自家红线

- **位置**：`agents/task_agent.py::_plan` 两处：`_dom = "finance"  # 唯一可选域`（编排缓存查询键）、
  域兜底 `selected_domain = "finance"`。
- **机制**：MEMORY 明令"凡涉及领域的判定一律用登记表/目录推导，禁止硬编码领域名"。
  tracing 的 `_VERB_CLASSIFY` 是登记表（合规）；这两处是裸字面量（违规）。
- **修复方向**：`_dom` 从 `plan_ctx._plan_domain`（selected_domain 已知后）或 `get_domains()` 推导；
  域兜底改为"task_type 登记表 → 默认域"映射表（放 resolvers/time._ENTITY_DOMAIN 同级）。

### B7. cron 拦截的时间语义坑：秒/分钟级延迟会漂到明天

- **位置**：`agents/task_agent.py::_try_intercept_cron` 模式 9 + `cron/cron_tools.py::_parse_at_time`
  纯时间分支
- **机制**："N 分钟以后"只把 target 压成 `HH:MM` 传给 `_parse_at_time`，后者解析时
  `if target <= now: target += 1 天` ——秒级/分钟级延迟在同一分钟内解析完成时 target≤now 成立
  ⇒ **"30 秒后提醒我"变成明天**。另"开盘期间,每N分钟" 的 at 字符串与 `_parse_at_time`
  的正则双处耦合，改一处格式即断。
- **修复方向**：模式 9 直接产出 ISO 绝对时间传 `YYYY-MM-DD HH:MM`（解析分支已支持），
  不走 HH:MM 相对语义。

### B8. ToolCircuitBreaker：docstring 承诺"按 agent 实例隔离"，实现是全局单例

- **位置**：`infra/breaker.py`（头注释 vs `_global_breaker`）
- **机制**：注释说"`_breakers` dict 按 agent 实例隔离、不跨任务污染"——`_breakers` 不存在，
  实际是模块级单例全局共享。一个数据源抖动 → **所有会话**的该工具熔断 5 分钟。
  另半开试探无并发闸（多线程同时 is_open 都放行）。
- **修复方向**：二选一——按 run_scope 隔离（对齐注释），或保留全局但改注释并明示共享语义。

### B9. `_LLMAdapter.generate` 的 length 报错消息是乱码

- **位置**：`agents/task_agent.py::_LLMAdapter.generate`（`finish_reason == "length"` 分支）
- **机制**：`raise RuntimeError("????????…CODE_AGENT_MAX_TOKENS ?????")` ——源文件历史编码损坏，
  同段注释也是 `??????`。用户可见错误信息不可读。
- **修复方向**：重写该消息（"输出被 max_tokens 截断，请调大 CODE_AGENT_MAX_TOKENS 或简化任务"）。

---

## C. P2：低危 / 死代码 / 契约漂移

| # | 问题 | 位置 | 说明 |
|---|------|------|------|
| C1 | `run_scope` 的 `or "run_default"` 是死兜底 | nodes.py 3 处 | `"run_" + ""` = `"run_"` 恒真，`or` 永不触发；`_start_time` 缺失时所有 run 共享 scope "run_" → 变量串台。应先 re.sub 再拼 or |
| C2 | `_sandbox_state_digest` 里 `if wl and k not in wl: pass` | nodes.py | 变量算了不用（whitelist 参数形同虚设），删或接 |
| C3 | `_execute_phase` 死代码且返回类型不一致 | task_agent.py | 零调用点；except 返回 tuple、正常返回 str |
| C4 | `_infer_var_type` / `_is_serializable` 死代码 | task_agent.py | 零调用点 |
| C5 | `check_exit.py` 孤儿脚本 | app/agent/ | 全项目零引用 |
| C6 | `Tool.parameters = field(...) if False else {}` | tools/base.py | 诡异的死三目 + 类属性可变默认值 |
| C7 | `_extract_failed_tools` 把 error 值当工具名 | nodes.py | `{"error":"timeout"}` → 记 "timeout" 为失败工具 → 权重/归因脏名 |
| C8 | AgentState TypedDict 与真实 state 漂移 | nodes.py | `_phase_replan_request/_trace/_start_time/_run_error/final_output` 均未声明，total=False 掩盖 |
| C9 | `_calc_skill_weight_from_trades` 字段名撒谎 | chain/evaluator.py | 返回的 `avg_pnl_pct` 实为 expected_return（胜率加权期望），落库即语义漂移 |

---

## D. 设计文档 v8.0 vs 代码的漂移（文档自身要修的）

| # | 文档声明 | 代码实际 |
|---|----------|----------|
| D1 | §3.3.3 记忆截断"保留最近 2 步完整" | `keep_recent=0, keep_recent_mo=1`（task_agent） |
| D2 | §3.3.3/§7.10 `additional_authorized_imports=["*"]` | GuidedCPythonExecutor 已无此参数（§7.20 是新真相，旧节未删） |
| D3 | §2.2 行数 2085/2420/235 | 2223/2637/285；文档自己说"行数已移除"却又保留 |
| D4 | §5.1 AGENT_MAX_STEPS 默认 6 | 三处默认 6/20/5（见 B2） |
| D5 | §3.16 手动入口命令可用 | NameError（见 A5） |
| D6 | §7.19"待补的结构性加固"三条 | selected_domain 空串告警 / list_tools 恒等沙箱实持 / 空域+金融意图判退化——均未见落地 |
| D7 | tracing.fail() docstring：错误 run 留根节点 | 零写入（见 A6） |
| D8 | breaker docstring：按实例隔离 | 全局单例（见 B8） |

---

## E. 架构缺陷与"更高级智能化 / 模块化进化"路线

### E1. God-file 收敛（模块化）
nodes.py（2223 行）与 task_agent.py（2637 行）承载 ≥7 种职责（编排/工具注入/执行器构建/
截断策略/事件钩子/trace 提取/验收）。对照 chain//infra//resolvers/ 的健康模块化，建议拆：
- `execution/builder.py`（_build_code_agent 全家）、`execution/truncation.py`（截断策略族）、
  `execution/acceptance.py`（_check_phase_acceptance + 归因）、`routing.py`（三路由 + 常量）。
- 拆分红线：**包装顺序敏感**（契约渲染→stage_guard→breaker→install）必须整块迁移并写不变量测试。

### E2. 上下文重放是结构性成本，截断只是止血
smolagents `write_memory_to_messages` 每步全量重放是 O(n²) 根源（§7.16 已定量）。
三档根治：
1. 近期：把 `_truncate_observations` 系列抽成**策略对象**（按模型/任务调档），并把"单步 input
   增长斜率"做成 run 级指标入 trace（回归自动报警）；
2. 中期：自管 message list（system + task + 近 1 步 code + state 摘要），放弃原生重放；
3. 长期：评估支持原生摘要记忆/KV 复用的执行框架（或 smolagents 后续版本的官方截断开关）。

### E3. 决策点收敛（§8.3 提过，加码）
现状 4 个 LLM 决策面：意图分类、外部 planner、smolagents 内部 planning、阶段验收。
建议收敛为 3：**内部 planning_interval 默认关**（单块执行纪律下收益为负——每 2 步强制 replan
正是 REPL 习惯的推手之一，§7.12）；验收判官只对**有 acceptance 的阶段**启用（现状已如此，
但引擎默认验收的 `_has_final_answer` 判定可更早——run_error 时不必再问判官）。
省下的预算给"事实权威层"（§8.1 state["facts"]，强烈建议做——时间口径被模型改写是金融域大忌）。

### E4. 自进化闭环升级：从"二值打分"到"结构化归因驱动修复"
现状闭环：regex 提取 → 二值 correct → 权重 → SKILL.md 修订。四个升级点（按价值排序）：
1. **verdict 结构化**：方向错 / 数据缺 / 数字编造 / 格式错 / 意图错 五类归因进 qd_traces
   （新列或 error 结构化），归因决定修复动作：工具降权 vs 技能修订 vs 提示修订 vs 提阈值。
   现在 agent_fault/tool_data_fault 二分只活在阶段验收，没进 T+N。
2. **regime/时间分层进权重**：MEMORY 里策略研究的最大教训（"任何规则上线前必须知道它赚的是
   哪段行情的钱"、600 天被 2024 污染）同样适用于 skill/chain 权重——现在是 90 天窗全窗平均，
   与 g56 的月度结构检查同构，建议 evaluator 报告加**月度分层 win_rate**，波动过大自动降权。
3. **首酿原料升级**：现在取 `MIN(id)` 一条 run 当模板（质量随机），改"信号分最高的 run + 失败样本对照"。
4. **golden-task 回归集**：固化 10~20 个典型任务 + 期望工具链 + 期望产出要点，
   每次 prompt/模板/工具变更跑回归出分——把"改 prompt 赌 roll"（§7.12/8.10）变成可量化工程。
   这是治"roll 方差"的工程正解，比换强模型便宜。

### E5. "接线自检"仪表盘 + 静态审计进 CI（治本之策）
本报告 A 组 bug 全部是你们已识别模式的复发 → 需要把原则变成机器规则：
1. **启动自检聚合**：capabilities 注册数、formatters 注册表（`list_formatters()` 已有）、
   skill 清单、returns 契约覆盖率、breaker 状态、eval worker 健康——一个 wiring 报告打进启动日志/health；
2. **CI 静态检查三条**（都能 AST 化）：
   - 提示词文件里的调用名 × provider 注册表求差集（§7.17 自查方法脚本化）；
   - `except` 分支仅 `logger.debug` 后 `return <常量>` 的函数（A1 就是它能抓的）；
   - 共享列/共享全局的"一个名字两个语义"（A2/B8 可用 grep 启发式：同一属性名在两个 getter 有不同注释语义）；
3. **契约不变量测试**：`list_tools` 视图 ≡ 沙箱实持（§7.0 第 4 条）做成单测，而非靠人看日志。

### E6. 并发模型：全局单例 → per-session ExecutionContext（见 B1 的架构面）
`_SHARED_TOOL_PROVIDER`（只读，可留）之外，把 `_current_event_cb/_INTERRUPT_CHECKS/
_active_code_agent/staging scope/stop flags` 收进 session 级 context 对象；
staging 的 run_scope 建议加入 session_id 维度（现为时间戳尾 8 字符，理论可撞）。

### E7. 工具层"一次声明，全链生成"
tools/ 现状 = 薄封装（§7.21 三对账教训）+ 三层事后包装（契约渲染→stage_guard→breaker），
顺序即语义（§7.2 易错点）。长期把工具声明收敛为**一个装饰器/数据类**：
签名 + Returns: + 准入 + 护栏参数 + 权重钩子一次声明，
由注册表生成 OpenAI schema / returns 契约 / 包装链——消灭顺序敏感性与 docstring 抽取的脆性。

---

## F. 修复优先级路线图

| 优先 | 项 | 理由 |
|------|----|------|
| **P0** | A1(+A1b) 幻觉数字防线失效 | 防幻觉闸门形同虚设，且告警误导排查 |
| **P0** | A2+A3+A4 酿造/修订状态机互踩 | 自进化闭环的数据在互相污染，越跑越歪 |
| **P0** | A5 手动入口崩溃 + A6 错误 run 无痕 | 声明的入口/留痕承诺不可用 |
| **P1** | B1 并发竞态、B2 配置三读、B5 幻觉工具名 | 多用户/配置变更/提示变更时必踩 |
| **P1** | B3 安全边界决策（先做审计留痕）、B4 决策提取质量 | 自进化地基 + 对外暴露面 |
| **P2** | B6~B9、C 组、D 组文档批修 | 一次性清扫，建议开一个"契约清扫"批次 |
| **P3** | E1~E7 演进 | 按 E5（自检+CI）最先做——它是防止前三组复发的免疫系统 |

---

*完。如需对任一项出修复 patch（较大变动按约定先评审后动），指路即可。*

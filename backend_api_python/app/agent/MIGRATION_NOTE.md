# app/agent 迁移说明（mimoagent 执行核 · 一步到位版）

> 2026-09-30 · 对应迁移映射文档 v2/v3 定调：mimoagent 全盘搬入，只补专业域。
> 原则：代码是怎样，文档就是怎样。

## 执行核

> **路线定稿（2026-10-01，用户拍板）：A = white-box（mimoagent MimocodeAgent）为长期主线。**
> 全 org 18 仓复盘结论：mimoagent 是唯一可嵌 Python 的 agent 核；产品级能力
> （持久记忆/上下文重建//dream）在 MiMo-Code 本体（TS CLI），官方组合方式是
> mimoagent 的 blackbox/mimocode.py 适配器驱动。备选路线 B（MiMo-Code 当大脑 +
> 金融工具 MCP 化）已评估未采纳，仅在需要长程编码类子代理时再启。

- **mimoagent**（XiaomiMiMo/mimoagent，**`mimo-oss` 分支**，`main` 是上游 miniswe-agent 基线别拿错）
- 白盒 Agent：**MimocodeAgent**（MiMo Desktop 同源引擎）——10KB 精调核心 prompt、
  task 子代理分解、compact 模型自主上下文压缩、<context_usage> 预算感知、
  AntiHackGuard、工具入参 schema 硬校验。system_template = 原版 _CORE_PROMPT +
  金融域覆盖层（prompts/qd_system.txt，后置指令优先）。
  注：2026-09-30 初版曾用基线 DefaultAgent，当晚升级到 MimocodeAgent（智能度主因）。
- Python 3.12（Dockerfile `python:3.12-slim` 已满足）；`pip install -e mimoagent` 或将其 `src/` 入 PYTHONPATH
- smolagents 不再是执行核依赖（仅 `tools/mcp_bridge.py` 惰性引用，用到 MCP 时才需要）

## 新增文件

| 路径 | 作用 |
|---|---|
| `qd_agent.py` | `QDAgent(DefaultAgent)`：mimo 原版循环 + 金融增强（数字溯源门/交易确认闸/危险面守卫）+ 会话钩子表（API 与旧 task_agent 同名） |
| `qd_service.py` | 服务门面：会话管理、模型构建（mimoagent models）、自动预取（记忆+RAG 注入）、chat() 契约 |
| `tools/fn_adapter.py` | FnToolAdapter：金融裸函数零改动挂载为 BaseTool |
| `tools/capability_tools.py` | 能力层低优先级派发：list/search/call_capability（工具层 > 能力层，同名硬让位） |
| `tools/memory_tools.py` | remember/recall 长期记忆工具 |
| `tools/skill_tools.py` | list_skills/read_skill 技能读取工具 |
| `audit/` | trace_adapter：轨迹 → qd_traces 事件 + repro 字段（闭环②） |
| `prompts/qd_system.txt` | 增强版 system template（mimo 基座句 + 金融纪律） |
| `scripts/qd_smoke.py` | 免 API key 机制验证（70/70 通过） |
| `scripts/qd_prompt_eval.py` + `scripts/prompt_tasks.yaml` | **Prompt 回归任务集**（真实端点跑批 + baseline diff） |
| `tools/availability.py` | 启动期工具可用性探测（写进系统提示，防模型被放鸽子） |
| `tools/tool_discovery.py` | 按需层发现元工具：list_tools / search_tools / activate_tools |

## 修改文件

| 路径 | 变化 |
|---|---|
| `agent.py` | TaskAgent → QDAgentService；其余配置/RAG/记忆/技能装配保留 |
| `message_queue.py` | 会话钩子表 import 换到 qd_agent（一行） |
| `agents/__init__.py` | 只导出 AgentBase/AgentResponse 契约 |
| `tools/returns_sampler.py` | WRITE_PREFIXES 内联（原 capabilities/scanner 真源随能力层删除） |

## 删除（旧执行核 + 孤儿件，原始仓库 .openclaw/tmp/quantdinger/ 有备份）

graph.py、nodes.py、agents/task_agent.py、agents/routing_policy.py、
infra/、execution/、formatters/、utils/smol_log.py、utils/tool_synth.py、
check_exit.py、feedback.py、cache.py（无人引用）、
utils/{data_check,facts,failure_memory,finance_checks,golden_tests,loop_integrity,
phase_graph,plan_critic,plan_linter,prompt_loader,standing_checks}.py（编排伴生孤儿）、
prompts/{plan_system.txt, intent_classifier.txt, code_agent.yaml, numeric_rules.txt}

## 保留（专业域 + 三闭环）

- `tools/`（20 金融工具 + knowledge/web/filesystem/mcp）、`skills/`、`chain/`（技能酿造闭环①）
- `capabilities/` + `tools/capability_tools.py`（能力层：低优先级按需派发；
  **admission.json 是人工准入清单、不随仓库分发——把你们的那份放回 capabilities/ 即接入**，
  缺失时注册 0 个不报错）
- `audit/` + `trace_collector.py` + `utils/grounding.py`（数字溯源/审计闭环②：
  JSONL 事件 + TraceCollector → chain.store → qd_traces DB 双落）
- trading_tools confirm 硬闸 + ActionInterceptor（交易确认闭环③）
- `rag/`、`memory/`、`resolvers/`（预取与工具的领域依赖）、`llm/`（skill_brewer/evaluator 离线作业客户端）
- `flask_app.py`、`cli.py`、`message_queue.py`、`cron/`（对外入口形态不变：CLI + Web 路由/SSE）

## 运行

```bash
# 1) 安装执行核（Python 3.12.x；一行，无需 git）：
pip install "mimoagent @ https://github.com/XiaomiMiMo/mimoagent/archive/refs/heads/mimo-oss.tar.gz"
# 或 git：pip install "mimoagent @ git+https://github.com/XiaomiMiMo/mimoagent.git@mimo-oss"
# 或源码：克隆 mimo-oss 分支后 set MIMOAGENT_SRC=<path>\mimoagent\src
# （依赖解析统一走 app/agent/mimo_boot.py：安装包 → MIMOAGENT_SRC → 相对路径候选 → 人话报错）

# 2) 其余依赖（flask/openai/psycopg2 等原样；requirements.txt 已含 mimoagent 引用）
pip install -r requirements.txt

# 3) 自检与启动
python app/agent/scripts/qd_smoke.py        # 免 key 机制自检（70 项）
python app/agent/cli.py                     # CLI
# Web：flask 路由注册方式不变（app/routes/agent_blueprint.py → flask_app.agent_v2_bp）
```

## 改 prompt 后的回归（必做）

`qd_smoke.py` 用的是**脚本化假模型**，只证明机制通，证明不了"模型看了 prompt 会怎么答"。
所以每次改 `prompts/qd_system.txt` 或工具面之后，必须跑真实端点回归：

```bash
python app/agent/scripts/qd_prompt_eval.py                  # 跑全量 + 对 baseline diff
python app/agent/scripts/qd_prompt_eval.py --only weather_realtime
python app/agent/scripts/qd_prompt_eval.py --update-baseline  # 确认新行为是对的，再存基线
python app/agent/scripts/qd_prompt_eval.py --validate        # 只校验任务集，不调 LLM
```

- 任务集：`scripts/prompt_tasks.yaml`（跑马灯/天气/选股/续聊/交易确认，每条断言都标注了它防的是哪个历史事故）
- 报告：`scripts/prompt_eval/latest.json`；基线 `baseline.json`（PASS→FAIL 即回归，非零退出）
- 依赖外部凭据的任务声明 `requires_availability`，探测不可用记 SKIP 而非 FAIL
- 跑批**必须限步**（默认 `--step-limit 8`）：不限步时选股任务曾跑到 57.5 万 input token 仍未收敛

## 工具分层与可用性（2026-10-01）

- **必注入层**（每轮下发 schema）：mimo 原生 + `tools/` 顶层（common 域）+ 元工具
  （`web_search`/`format_result`，domain=meta）+ 三个发现元工具。
- **按需层**（默认**不**下发）：`tools/finance/*`、`tools/knowledge/*`。
  模型用 `search_tools` 检索 / `activate_tools` 点名激活后，schema 才进入后续请求。
- 依据：`mimoagent/agents/default.py` 每轮把 registry **全量** definitions 下发，
  88 个工具 ≈ 21.2k tokens/请求；分层后首轮 ≈ 8.7k（同一天气任务总计 54.6k → 26.7k，-51%）。
- 开关：`QD_TOOL_TIER_MODE=all`（退回全量）、`QD_MAX_ACTIVE_TOOLS`（默认 12）、
  `QD_EXTRA_CORE_TOOLS`、`QD_ON_DEMAND_TOOLS`（默认 actor）、`QD_TOOL_PROBE_LIVE=1`（真实连通性探测）。

### 工具预选 —— 外置 plan + 确定性 lint（对齐旧系统 plan_system/plan_linter）

分层把 token 成本转嫁成了轮次（模型要 `search_tools`→`list_tools`→`activate_tools`
三轮才能摸到一个金融工具）。预选 = run 前用 **1 次轻量 LLM** 声明本轮工具面，
直接激活进首轮，替代静态白名单 `QD_EXTRA_CORE_TOOLS`（后者换领域就失效）。

#### 为什么不是"LLM 路由器"（首版的方向性错误）

首版让 LLM 直接回答"你会用哪些工具？输出 JSON 数组"——**在真空里猜**，没有任务锚定，
只能靠"宁可多选 1-2 个"兜底。实测：选对省 25%，**选错费 136%**（天气任务被硬塞 8 个
股票工具），同任务三次 +21%/−25%/+31%，**方差大于效应**。

旧系统（`agent_smolagents/prompts/plan_system.txt` + `utils/plan_linter.py`）的做法是：
1. **工具面是"任务书的副产品"**：planner 一次输出 `{goal, deliverables, tools, …}`，
   工具 = 完成该 goal、交付这些要素的**最小工具面**。先想清目标，工具被锚定。
2. **双向标准**：「只选主链路必须经过的，不凑数」+「关键链路不能断（宁多不可断）」，
   判据 = **承诺交付的每条信息都有对应取数工具**——可反向校验。
3. **确定性 lint 兜底**（稳定性的真正来源）：`lint_selection()` 不调 LLM，
   用「域词典」识别需求 → 缺覆盖则补首选工具（R1 加法）；与 plan 自述目标
   **零相关**则裁（R4 减法）；几乎全零相关 ⇒ 整体作废（R4b）。
   **LLM 不稳定没关系，确定性层纠正它。**

#### ★ 第二个根因：预选输入被上下文污染

首版把 **prefetch 增强后的 task**（里面塞着记忆/RAG/技能块）直接喂给预选。
实测：问"今天北京天气"，记忆里存着前几轮的个股分析 ⇒ 预选的 goal 被写成
"在用户给出单只 A 股代码后…形成综合诊断" ⇒ 点名 7 个股票工具 ⇒ 该任务 +136%。
**看起来像"模型随机/不稳定"，实为输入污染。**
已修：`_preselect_query()` 只用**用户原始问题**（service 挂在 `agent.last_user_query`，
回退时从 task 的 `[用户问题]` 段截取）。修完 goal 与 query 逐字对应。

#### 实测（基线 = 无预选，同一任务集/同一模型）

| 任务 | 基线(步) | 现在(步) | Δ tokens | 说明 |
|---|---|---|---|---|
| stock_screen | 153,960 (6) | **43,318 (2)** | **−72%** | 发现轮消失，只调 `get_sector_fund_flow` |
| trade_confirm | 100,150 (6) | **34,976 (2)** | **−65%** | 只调 `start_strategy` |
| demo_marquee | 14,150 (1) | 16,452 (1) | +16% | 本无发现轮 ⇒ 无收益，波动 |
| weather_realtime | 30,028 (2) | 58,961 (3) | +96% | 闸门拦住预选；波动来自多搜一轮 |
| continuation | 41,313 (1) | 56,713 (1) | +37% | 本无发现轮；且每跑一轮回归记忆库就多一轮 |

**可解释的结论**：token 主要由**步数**驱动。预选的收益 = 消掉发现轮。
有发现轮的重任务（6 步 → 2 步）稳定省 65~72%；本来就没有发现轮的轻任务
（1~2 步、只调 web_search/remember）拿不到这份收益，剩下的波动是模型自身行为
+ 跑批自污染（continuation 每跑一次就往记忆库写一round），**与预选无关**。

#### 四重保护（`smoke test_21` + `test_23`）

1. **确定性 lint**：R1 补位 / R4 裁剪 / R4b 整体作废；**无语义基准时保守不裁**
   （模型只回裸数组 ⇒ 没有证据说某个工具无关，"判不了"≠"判了是无关"）。
   词典自检 `check_domain_dict()` 由 test_23 每次跑，防止词典与注册表漂移
   （旧系统踩过"词典陈旧无人报警"的坑）。
2. **域特征闸门** `has_domain_hint()`（成本闸）：无领域特征根本不发那次 LLM。
   词表改 `QD_PRESELECT_HINTS`。注意它**只管成本、不管质量**，质量归 lint。
3. **fail-open**：预选调用失败/超时/解析异常一律返回空，退回 `search_tools` 老路。
4. **只做加法**：不清空已激活项；模型幻觉出的工具名对照注册表过滤掉。

开关：`QD_TOOL_PRESELECT`（默认 1）、`QD_PRESELECT_GATE`（默认 1，闸门）、
`QD_PRESELECT_MAX`（默认 8）。

★ 配套修复：`_tool_map_text` 原先把按需层一律标注"需先 search_tools"，
导致预选激活了模型也不知道、照旧走三轮发现（首版 stock_screen 发现轮一次没少）。
现已加 `[已激活·可直接调用，不要再 search_tools/activate_tools]` 段。

### 上下文窗口

- `QD_CONTEXT_WINDOW`（默认 65536）、`QD_COMPACT_RATIO`（默认 0.55）、`QD_COMPACT_THRESHOLD`（显式覆盖）。
- ⚠ `.env` 里的 `OPENAI_MAX_TOKENS=32768` 是**单次输出上限**（`agent.py:44` 按 `max_tokens` 消费），
  **不是**上下文窗口。硬实证：`continuation` 任务单轮 input 已达 **41,313** 仍请求成功 ⇒ 真实窗口 > 41k。
  故兜底值不用它（否则阈值会低到 18k，在正常的 41k 任务上误触发压缩）。
- compaction 与 wrap_up **同源**（首版用了两个 env 名 `QD_COMPACT_WINDOW`/`QD_CONTEXT_WINDOW`，会打架，已统一）。
- 元工具契约：`FnToolAdapter` 会把执行上下文注入签名里的 `_context`（下划线开头，不进 schema），
  `tools/tool_discovery.py` 靠它拿 `context["agent"]` 登记激活 —— 这是"工具回调 agent"的唯一通道。

模型端点：.env 的 `OPENAI_MODEL / OPENAI_API_KEY / OPENAI_BASE_URL / AGENT_LLM_TEMPERATURE`（复用原变量名）。

## 已知边界

1. 会话上下文在进程内（session_id → QDAgent messages）；进程重启丢会话内上下文，
   长期记忆走 memory/（Postgres 可持久）。
2. 技能 run.py 执行入口暂未入工具面（模型可读 skill 正文/小节/资源；批量执行走原 skills 调用方）。
3. dashscope 方言端点如 openai_chat 协议不兼容，需自定义 mimoagent Model 子类（接口仅 query()）。
4. 能力层接入后需放回 admission.json 才有能力点（清单是人工审核件，不在仓库）；
   无清单时 search/list 返回空、不报错。

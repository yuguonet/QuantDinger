# Agent 系统审计报告

- **审计日期**：2026-10-14
- **范围**：`backend_api_python/app/agent`，包括 Python 源码、技能与提示词、配置、文档和运行记录；并检查了相关测试。
- **结论**：确认 2 项高优先级并发/超时风险、3 项中等风险（预算接线、因子校准、运行记录留存），以及多处会误导维护者的文档状态漂移。未发现 Python 语法错误；由于核心依赖缺失，运行时回归未能完整执行。

## 已确认风险

| 编号 | 严重度 | 位置 | 风险与证据 | 影响 / 建议 |
|---|---|---|---|---|
| A-01 | **P1 高** | `backend_api_python/app/agent/qd_service.py:298-319, 554, 568-570`；`qd_agent.py:203-213`；`qd_hooks.py:38-59`；`message_queue.py:124, 249-260` | 按 `session_id` 缓存并复用同一个 `QDAgent`，但请求执行是在 `asyncio.to_thread()` 中进行，未被会话级互斥锁保护。服务层的锁只保护 Agent 查找/创建，不保护 `agent.run()`。队列可启动多个 worker，钩子也以 session ID 为唯一键注册。 | 同一会话的并发请求可交错更新消息、预算、计划及其他 run 状态；钩子可能互相覆盖，先结束的请求还可能注销后一个请求的钩子。按 session 串行化执行，或为每个 run 隔离 Agent 状态和钩子；补同会话并发回归测试。**置信度：高**。 |
| A-02 | **P1 高** | `backend_api_python/app/agent/message_queue.py:229-277`；`qd_service.py:568-569`；`qd_agent.py:114-117, 174-178`；`agent.py:326-350` | 队列通过 `asyncio.wait_for()` 等待由 `asyncio.to_thread()` 执行的同步任务。超时取消等待协程并不终止已运行的线程；队列的 `finally` 会先注销该 session 的停止钩子，之后才设置停止标记。Agent 的协作停止检查依赖这些钩子。`run_agent()` 也有 `wait_for()` 包裹线程任务的入口。 | 超时返回后，后台线程可能继续调用模型、改写会话状态、写记忆或产出副作用；停止标记无法再通过已注销的钩子被执行体读取。应让 worker 生命周期与请求状态关联，在线程真正退出前保留可寻址的取消信号，并防止超时后继续提交副作用。**置信度：高**。 |
| A-03 | **P2 中** | `backend_api_python/app/agent/utils/budget.py:63-94`；`qd_budgeting.py` 的预算调用路径 | `add_day_usage()` / `get_day_usage()` 在预算模块中存在，但当前包内没有调用方；预算路径调用 `check_budget()` 时没有传入当日用量，而日配额判断依赖 `day_tokens`（缺省为 0）。设计文档也承认日配额未接线（`AGENT_DESIGN_v3.md:1560, 1669`）。 | 若运维或用户认为已设置“日 token 配额”，当前路径实际上不会累计并据此熔断；长时间或高并发使用可能越过日限额。接通记账、跨进程一致的限额检查，并增加跨 run/跨 worker 测试。**置信度：高**。 |
| A-04 | **P2 中** | `backend_api_python/app/agent/traces/agent_runs.jsonl:1-708`；`backend_api_python/app/agent/traces/qd_agent_runs.jsonl`；`backend_api_python/app/agent/traces/preselect_pairs.jsonl` | 这些 JSONL 是 Git 跟踪文件。审计确认 `agent_runs.jsonl` 的 708 条记录均含非空 `user_input`；其他运行记录中也存在 `query` 字段。 | 用户原始输入可能包含个人、账户或业务敏感信息，直接留在仓库会扩大保留、访问和意外外传面。明确日志数据分类与保留期限；优先脱敏或只保存必要的摘要/哈希，并评估既有历史记录是否需要清理。**置信度：高（字段存在）；内容是否含敏感信息需数据所有者判断**。 |
| A-05 | **P2 中** | `backend_api_python/app/agent/tools/finance/bull_bear_research.py:349-365`；`chain/store.py:710-727`；`chain/evaluator.py:724-779` | 因子权重 getter 并非“恒空”：源码实现了 `layer='factor'` 查询及写入；但 `bull_bear_research` 调用处仍记载因子名称与 `ma/macd/...` 维度不匹配，并可能回退默认权重。 | bull/bear 研究路径的因子级校准可能未实际生效，尽管全局 factor 权重管道已经存在。统一写入键与读取键，并用真实样本验证查询命中，而不是仅验证 getter 可调用。**置信度：中**（代码路径存在；数据库实际命中率未核实）。 |

## 文档与实现状态漂移

这些问题不一定直接改变当前运行行为，但会导致重复施工、漏修或错误判断：

1. `AGENT_DESIGN_v3.md:1881` 将 P3 校准标为“已接线”，同一表格却把状态列标为 `③`（未落地）；文档后段 `:1967-1973` 又说明接线已完成、冷启动时因样本不足而 `coefs=None`。应统一状态含义，区分“生产接线完成”和“尚未达到有非零系数的样本门槛”。
2. `数据流模拟_20261014.md:24-30, 53-61` 仍将 `aggregate(coefs=None)` 和“P3 孤岛”写成当前状态，并把降级门问题描述为 P3 接线前置；但 `AGENT_DESIGN_v3.md:1368-1376, 1778-1805` 记录了 2026-10-14 P3 已接线。该数据流报告至少有部分内容已过时；其中 S-1、S-2、S-3 等代码问题本轮没有逐项重演，应先复核现行实现再按缺陷处理。
3. `AGENT_DESIGN_v3.md:1649` 仍记 `get_factor_weights`“恒返回空”，与当前 getter/写入实现不符；`:1934` 仍称 R2 静态守卫缺失，但 `backend_api_python/tests/test_v5_aggregator.py:140-151`、`tests/test_v5_c3p.py:260-270` 及 `app/agent/scripts/qd_smoke.py:2375-2393, 2437-2455` 已包含相关守卫。守卫是否每次 CI 执行尚未核实，文档应区分“测试存在”和“CI 已执行”。
4. `agent差距与改进路线.md:4` 标注最后更新为 2026-10-11，早于当前设计文档中的 2026-10-14 校准及预算状态；`领域domain作用梳理_20261014.md` 对旧 `domain_meta` 词典/API 的描述也应与当前源码核对。审计复核发现 `domain_registry.py` 的部分旧 API 已删除，但模块仍有活跃消费者；不可将“旧 API 无消费者”扩大成“整个模块是死代码”。

**处理原则**：先修订文档状态和日期，再以当前源码、测试及部署配置复核历史审计中的遗留项；不要根据已过期的缺陷表重复新增守卫或改动已完成的 P3 接线。

## 历史审计待复核项

`数据流模拟_20261014.md:41-95` 记录 S-1 至 S-8（显式预测视界被口语词覆盖、多 claim 证据复用、降级样本仍可能参与经验加权、案例回填依赖旧链路、采样数据无下游消费、pair 记录需多表/文件 JOIN、聚合器使用 count-based 近似、`degrade_reason` 恒空）。该报告部分状态已被 P3 后的设计文档更新。本次审计未逐条重新复现这些问题，因此它们不作为本报告的“已确认缺陷”；建议按数据污染/校准影响优先检查 S-1 至 S-3，再核实其余项是否仍适用。

## 死代码与冗余结论

- 本次没有足够证据建议删除某个仍存在且被引用的模块。审阅材料提到的 `domain_registry` 旧 API 和旧 `_DATA_DOMAINS` 词典已部分删除；剩余 registry 能力有真实消费方，不应整模块判死。
- 当前能确认的未接入逻辑是 A-03 的日用量记账 API；在接入或明确放弃日配额前，应避免把该 API 当作已生效保护。
- `AGENT_DESIGN_v3.rar` 是目录内的二进制归档；环境未提供 RAR 检视工具，本次未解包，不能确认其是否为重复/过期内容。

## 验证与限制

- **Python 语法**：审计覆盖的 140 个 Python 文件均通过 AST 语法解析。
- **YAML 配置**：目录内 4 个 YAML 文件均可由 YAML parser 解析。
- **JSONL 记录**：目录内 11 个 JSONL 文件全部可解析，未发现格式错误。
- **测试**：`python -m pytest tests\test_agent_attr_namespace.py -q` → 2 passed、2 failed；失败原因均为环境缺少 `mimoagent`。`python -m pytest tests\test_agent_attr_namespace.py tests\test_v6_qd_agent_split.py -q` → 测试收集因同一缺失依赖中断。因此，本次不能声称并发、超时或框架集成行为已通过运行时测试。
- **覆盖边界**：140 个 Python 文件完成语法检查，但源码审阅重点集中于服务入口、请求队列、Agent 状态、钩子与预算/校准关键路径；并非 140 个文件逐行人工审阅。运行日志只检查结构和文本字段是否存在，没有输出或逐条检查用户输入内容。

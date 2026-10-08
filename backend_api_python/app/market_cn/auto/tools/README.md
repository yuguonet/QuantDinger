# tools/ — 调试与研发工具索引

`auto/tools/` 现 **18** 个模块（2026-10-07 归档 4 个 T3 + 新增 `projection_shadow`；
2026-10-08 归档 5 个 T4）。
本表是**唯一权威索引**：按"我要回答什么问题"找工具，不靠记文件名。旧文档 §7.2 的清单
只列到 21 个且已过期，以本表为准。

## 一、"这票为什么（没）出信号" —— 4 个入口怎么选

4 个入口角度不同，选错会白跑一遍。按**从粗到细**的顺序：

| 我想知道 | 用 | 它给什么 |
|---|---|---|
| 这票最近 N 天为什么没出信号（先看这个） | `why` | 多日摘要：每天卡在哪，一眼看出是"长期不满足"还是"某天差一点" |
| 某**一天**的判定过程（门级逐步） | `debug` | 单候选逐门追踪：每个门的入参/出参/是否通过（门表策略走公式求值器；g56 等插件策略走 replay+TraceCollector 门原因链） |
| 单股单策略**全过程**逐笔重放（含盘中策略历史复现） | `replay` | 逐笔重放，回答"历史上这笔到底怎么走的" |
| 策略的**门表结构**本身（静态，不看某票） | `explain` | 门表机器可读报告：门槛值、顺序、依赖关系 |

顺序口诀：**先 why 定位日期 → 再 debug 追单日 → 要逐笔细节用 replay → 改规则前看 explain**。

## 二、按问题分类

**验收 / 闸门（改动后该跑哪些）**

| 工具 | 用途 |
|---|---|
| `path_parity` | 三路径同源自检（M2 验收件：scan_signals / scan_days / fold 口径一致） |
| `present_verify` | 展示层单票对账（折叠输出 vs 旧路径逐位对账） |
| `projection_shadow` | **P5 影子对账**：① `--replay` **历史回放**「生产 `scan_days` vs 投影 `Record.ready`」判定日集合（★ 影子期的正路 —— 数据/结果可复现，**不等日历天数**）；② 与 `qd_dragon_signals` 库现状双向 diff（切 writer 前置；带覆盖度护栏，`--apply` 才落库）。退出码 0 = 0 不一致 |
| `doctor` | 一键三方对账（config ↔ 注册表 ↔ 磁盘插件、层反转、做T 约束等结构检查） |
| `pool_check` | 信号级复验闸门（固化"信号级复验纪律"） |


**判定与规则研发**

| 工具 | 用途 |
|---|---|
| `param_scan` | 参数网格扫描 + 敏感性报告 |
| `rule_audit` | 门级规则审计：拦截表 + 判别力 + 换序稳定性 |
| `rule_stats` | 入场规则归因统计（低胜率 / 低盈亏比排查） |
| `gate_try` | 候选规则试算（改门前先试算） |
| `gate_funnel` | 策略漏斗可调用接口（各门通过率） |
| `proposal` | LLM 建议产物 + 红线校验 + 应用 + 回滚 |
| `chat_why` | 口语化策略调试聊天（同一套调试能力的对话入口） |

**数据通道质量**

_（T4 归档后暂空——`snapshot_quality` 已移 del/）_

## 三、已归档

### T4（2026-10-08，P6 收量）

移到 `del/20261008_tools_t4/`：

| 原工具 | 用途 | 归档理由 |
|---|---|---|
| `selftest` | 策略契约测试闸门 | pytest 套件已覆盖（tests/present/ 全量 261 绿），独立 CLI 无外部调用 |
| `verify_prefilter` | 预筛等价验证 | 同上，等价断言已进 pytest |
| `bench_scan` | 盘中扫描 50s 基准 | 性能基准脚本，零 import 零 CLI 调用 |
| `new_strategy` | 新策略脚手架 | 生成器，按需可从 del/ 取回 |
| `snapshot_quality` | 快照拼接质量诊断 | 零引用；quality 分级已入数据层 |

归档前核验：**零 import、零 `python -m` 调用**（全树扫描；命中的 2~5 处引用均为
历史设计文档 .md）。`chat_why` 因被活的 `agent/skills/strategy_debug/SKILL.md`
点名为备用 CLI，**保留**。

### T3（2026-10-07）

移到 `del/20261007_tools_t3/`（`del/` 批次约定，与 `del/_archive/` 同一套标准）：

| 原工具 | 用途 | 归档理由 |
|---|---|---|
| `ml_baseline` | M4 GBDT 基线 | ML 旁支冻结 |
| `sample_build` | M2 统一样本构建器 | ML 旁支冻结 |
| `m6_check` | M6「LLM 分析闭环」验收自检 | M6 验收已过，只留档 |
| `market_spec_check` | M5 市场适配层（MarketSpec）自检 | M5 验收已过，只留档 |

归档前已核验这 4 个**零被引用**（无 `import`、无 CLI 注册），断链风险为 0。

⚠ **`rule_stats` 最初被列为 T3 候选，核验后取消归档** —— 它不是孤立工具，而是
一批工具的**统计口径底座**：`rule_audit` / `gate_try` / `gate_funnel` / `m6_check`
都 import 它的 `_load_rows` / `_metrics`，且 `strategy_cli.py` 的 `stats` 子命令指向它。
归档它 = 一次断 4 处 + 一个 CLI 入口。**低频 ≠ 可归档，先查 import。**

# tools/ — 调试与研发工具索引

`auto/tools/` 共 25 个模块 ≈ 7700 行（全仓最大单层）。本表是**唯一权威索引**：
按"我要回答什么问题"找工具，不靠记文件名。旧文档 §7.2 的清单只列到 21 个且已过期，
以本表为准。

## 一、"这票为什么（没）出信号" —— 4 个入口怎么选

4 个入口角度不同，选错会白跑一遍。按**从粗到细**的顺序：

| 我想知道 | 用 | 它给什么 |
|---|---|---|
| 这票最近 N 天为什么没出信号（先看这个） | `why` | 多日摘要：每天卡在哪，一眼看出是"长期不满足"还是"某天差一点" |
| 某**一天**的判定过程（门级逐步） | `debug` | 单候选逐门追踪：每个门的入参/出参/是否通过 |
| 单股单策略**全过程**逐笔重放（含盘中策略历史复现） | `replay` | 逐笔重放，回答"历史上这笔到底怎么走的" |
| 策略的**门表结构**本身（静态，不看某票） | `explain` | 门表机器可读报告：门槛值、顺序、依赖关系 |

顺序口诀：**先 why 定位日期 → 再 debug 追单日 → 要逐笔细节用 replay → 改规则前看 explain**。

## 二、按问题分类

**验收 / 闸门（改动后该跑哪些）**

| 工具 | 用途 |
|---|---|
| `selftest` | 策略契约测试闸门（策略改动后第一道） |
| `path_parity` | 三路径同源自检（M2 验收件：scan_signals / scan_days / fold 口径一致） |
| `present_verify` | 展示层单票对账（折叠输出 vs 旧路径逐位对账） |
| `doctor` | 一键三方对账（含 `_archive` 未进注册表等结构检查） |
| `pool_check` | 信号级复验闸门（固化"信号级复验纪律"） |
| `verify_prefilter` | 预筛等价验证（预筛前后口径是否等价） |
| `bench_scan` | 盘中扫描 50s 预算基准（性能闸门） |
| `m6_check` | M6「LLM 分析闭环」验收自检 |
| `market_spec_check` | M5 验收闸门：市场适配层（MarketSpec）自检 |

**判定与规则研发**

| 工具 | 用途 |
|---|---|
| `param_scan` | 参数网格扫描 + 敏感性报告 |
| `rule_audit` | 门级规则审计：拦截表 + 判别力 + 换序稳定性 |
| `rule_stats` | 入场规则归因统计（低胜率 / 低盈亏比排查） |
| `gate_try` | 候选规则试算（改门前先试算） |
| `gate_funnel` | 策略漏斗可调用接口（各门通过率） |
| `new_strategy` | 新策略脚手架生成器 |
| `proposal` | LLM 建议产物 + 红线校验 + 应用 + 回滚 |
| `chat_why` | 口语化策略调试聊天（同一套调试能力的对话入口） |

**数据通道质量**

| 工具 | 用途 |
|---|---|
| `snapshot_quality` | 快照拼接通道质量诊断 |

**ML 旁支（低频，冻结中）**

| 工具 | 用途 |
|---|---|
| `ml_baseline` | M4 GBDT 基线（ML 旁支，依赖 M2 样本） |
| `sample_build` | M2 统一样本构建器（ML 旁支） |

## 三、分级（2026-10-07 提议，待确认后执行归档）

按"多久用一次"分三档，供后续归档/停更决策用：

- **T1 一线日常**：`why` `debug` `replay` `explain` `doctor` `selftest`
- **T2 改动/验收时**：`path_parity` `present_verify` `pool_check` `verify_prefilter`
  `param_scan` `rule_audit` `gate_try` `gate_funnel` `bench_scan` `new_strategy`
  `snapshot_quality` `chat_why` `proposal`
- **T3 低频 / 可归档候选**：`rule_stats`（归因排查专用）、`m6_check` 与
  `market_spec_check`（M5/M6 验收已过，只留档）、`ml_baseline` `sample_build`
  （ML 旁支冻结）

⚠ T3 的处置（移 `del/` 或标停更）需与 `strategies/_archive/` 的处置一起定，
避免两套标准。定之前本表只做标注，不动文件。

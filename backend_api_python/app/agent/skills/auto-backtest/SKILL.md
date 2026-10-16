---
name: auto-backtest
description: 全盘量级回测技能：把策略假设（proposal 改参/加门、或全新 agentbt_ 策略、或现有策略对照）送 auto 折叠回测官方口径（run_all），十分钟级，产物落盘回传预览。个股/小集合（分钟级）不要用本技能，用工具 agent_backtest_strategy。
metadata:
  version: 1.0.0
  tags: auto, backtest, strategy, proposal, a_share
---

# auto-backtest（agent × auto 回测桥 · 全盘档）

## 何时用我

- 需要全市场（scope=all）跑一个策略假设的胜率/盈亏比；
- 个股/小集合验证请改用工具 `agent_backtest_strategy`（分钟级）。

## 参数（经 run_skill 派活：arguments='{"fn":"run","kwargs":{...}}'）

| kwargs 键 | 类型/默认 | 说明 |
| --- | --- | --- |
| `scope` | `"all"`（默认）\|`"codes"` | 全盘 / 指定代码集合（codes 必填） |
| `strategy_key` | str | 现有策略 key（break/g56/dragon_callback）或 agentbt_ 前缀新策略 |
| `proposal_json` | str（JSON） | 对现有策略 yaml 的最小改动提案（改选择集须附 evidence.stability/pool_check） |
| `strategy_py` / `strategy_yaml` | str（成对） | 全新策略载体：.py 全文 + yaml 的 JSON 串（key 必须 agentbt_ 前缀） |
| `codes` | str | 逗号分隔代码，仅 scope=codes 时用 |
| `days` | int，默认 365 | 回看自然日数（上限 QD_BT_MAX_DAYS） |

载体三选一：`proposal_json`（①改参/②加门）｜`strategy_py`+`strategy_yaml`（③全新）｜只传
`strategy_key`（existing 对照组，零改动回测现有策略）。

`proposal_json` 最小 schema（真源：`app/market_cn/auto/tools/proposal.py`）：

```json
{"strategy": "break", "edits": [{"kind": "param", "target": "x", "before": 1, "after": 2}]}
```

载体与入参规整统一走桥 `parse_carrier` 单源（畸形 JSON / ③档不成对 / days 非整数
一律归一为 `{"error": ...}`，不会以裸异常逃逸或静默降级）。

## 产物与红线

- 返回 `preview`（O(摘要) 一段文本）+ 桥 summary 全键（stats/overlay_digest/git_sha…）；
  全量逐笔在 `report_path`（agent 引用路径，不搬进上下文）
- 生产树只读、零写共享表；回滚 = 删作业目录 `data/backtest_bridge/jobs/<job_id>/`
- 结论解读后写进 answer —— T+N 问责由 intake 主流程自动接住（桥不记 claim）
- 预算：`QD_BT_MAX_DAYS/MAX_CODES/TIMEOUT/MAX_CONCURRENT/JOB_TTL_DAYS`（env，§2 契约）

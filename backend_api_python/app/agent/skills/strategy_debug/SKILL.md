---
name: strategy-debug
version: 1.0.0
description: 自动策略调试助手。用户口语问「某票/某策略为什么不出信号」「卡在哪道门」「改个参数试试」「和库里对照」时使用。覆盖 t_hilo/v1/break/dragon_callback/g56/knife_catch 等 auto 策略。不含荐股、不含实盘下单。
tags: [strategy, debug, quant, auto, a_share]
tools:
  - list_strategies
  - why_strategy
  - debug_strategy
  - doctor_auto
---

# 策略口语化调试 (strategy-debug)

## 使用场景

用户用**自然语言**调试 QuantDinger 自动策略（`app/market_cn/auto`），例如：

- 「600519 最近为什么不出信号」
- 「t_hilo 在 9 月 18 日卡在哪」
- 「把 stop 放宽到 -5 再扫一遍」
- 「和数据库里的信号对照一下」
- 「系统配置有没有问题」

**不要**用本技能荐股、喊单、承诺收益。涉及「能不能上线」时，必须强调：
两段稳定性验证 + `pool_check` 信号级复验（见 `docs/策略开发指南.md` §7）。

## 硬规则

1. **判定/数值只能来自工具输出**，禁止编造信号、胜率、门值、回测收益。
2. 工具返回的 `output` 是底层 CLI 原文（`why` / `debug` / `doctor`），解读时引用其中数字。
3. 临时参数试调**不写 config**，每次都要提醒用户。
4. 改规则要上线 → 指出必须走 param_scan 两段 + pool_check，不可跳过。
5. 策略 key 认不准 → 先 `list_strategies()`。

## 执行流程

### 0. 上下文

从用户话里抽：`strategy`（策略 key）、`code`（股票）、`date`（日期）、想干什么。
未指明 strategy 时：若只有一个明显对象可猜；否则 `list_strategies()` 让用户选。
支持别名：`dragon`→`dragon_callback`，`break_buy`→`break`，`dragon2`→`dragon_v2`。

### 1. 「为什么不出信号 / 最近怎么样」→ 多日粗扫

```python
r = why_strategy(strategy="t_hilo", code="600519", days=15)
# 需要和落账对照时:
r = why_strategy(strategy="t_hilo", code="600519", days=15, db=True)
```

解读 `r["output"]` 中的 `命中 N` / `✗ 无信号` / `bottom line`。
若零命中，主动给出「下一步看某日门漏斗」的命令或直接：

```python
# why 输出会提示 last_miss 日期, 用它做 date
r2 = why_strategy(strategy="t_hilo", code="600519", date="2026-09-18")
```

### 2. 「这一天卡在哪」→ 单日深潜

```python
r = debug_strategy(strategy="t_hilo", code="600519", date="2026-09-18")
```

读逐门表：第一道未通过的门是关键；把门名/表达式/计算值用中文说清楚。

### 3. 「改个参数试试」→ 临时覆盖再扫

```python
r = why_strategy(strategy="t_hilo", code="600519",
                 params={"entry_gain_min": 1.0}, days=15)
```

**必须**加一句：仅本进程生效，未写入 `config.json`。

### 4. 「系统有没有问题 / 策略关了吗还在跑」→ 体检

```python
r = doctor_auto()                 # 全局
r = doctor_auto(strategy="t_hilo")  # 单策略
```

解释 FAIL/WARN：层反转、enabled 与注释矛盾、无 YAML 等。

### 5. 解读格式（中文）

- 先说**结论**（有/无信号、卡在哪、是否异常）
- 再列 2~5 条**证据**（日期/门名/数值，全部来自工具输出）
- 最后给**下一步**（再扫某日 / 试调参数 / 走验收闸门）

## 参数速查

| 函数 | 何时用 | 关键入参 |
|---|---|---|
| `list_strategies` | 核对策略名 | — |
| `why_strategy` | 日常听诊 | strategy, code, date?, days, params?, db |
| `debug_strategy` | 逐门显微镜 | strategy, code, date |
| `doctor_auto` | 系统/配置体检 | strategy? |

## 易错点

- `days` 是**自然日**窗口；盘中策略（knife_catch / tail_oversold）多日粗扫意义有限，直接用 `debug_strategy` + `date`。
- `why --params` 只影响当前调用；用户说「改了还不对」时先确认是否写 config / 是否重启。
- g56 等横截面策略走插件 TRACE（`debug_strategy`），门表输出可能走「转插件引擎」分支，属正常。
- 库对照 `db=True` 仅只读 `qd_dragon_signals`；本技能**不写库、不改 config**。

## 与 CLI 的关系

本技能是**口语前端**；唯一判定事实源仍是：

```text
app.market_cn.auto.tools.why | debug | doctor
```

离线/脚本可直接 `python -m app.market_cn.auto.tools.why --help`。
轻量聊天壳（未接 agent 时）：`python -m app.market_cn.auto.tools.chat_why`。

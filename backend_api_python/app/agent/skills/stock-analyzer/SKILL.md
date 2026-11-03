---
name: stock-analyzer
description: 对单只 A 股做 T+1~T+20 全周期诊断——分周期方向、置信度、触发/证伪条件与关键价位。用户问「分析/诊断/怎么看某只股票」「XX 怎么样」「XX 能拿多久」「XX 该不该走」时使用。**不做选股**：「今天买什么」「有什么好股票」「短线选什么」归 market-screener，两者不要混用。
metadata:
  version: 1.0.0
  tags: stock, analysis, single-stock, multi-horizon, a_share
  tools: run_skill, agent_get_kline, get_realtime_quote, technical_analysis, get_fund_flow, get_chip_distribution, get_dragon_tiger, get_stock_sector_info, get_limit_pool, search_stocks, search_intel
---

# 单只个股全周期诊断（stock-analyzer）

## 何时用 / 何时不用

| 用户问法 | 用哪个 |
|---|---|
| 分析一下 600519 / 茅台怎么样 / 这只票能拿多久 | **本技能** |
| 今天买什么 / 有什么好股票 / 短线选什么 | `market-screener` |
| 茅台现在多少钱 / 今天涨了多少 | `auto-finance-query-stock` |

本技能**只诊断被点名的那一只**，不产出候选池、不做横向比较。用户一口气报多只时逐只调用。

## 调用方式

```
run_skill("stock-analyzer", {"fn": "analyze", "kwargs": {"code": "600519"}})
```
`deep=true` 追加龙虎榜席位与板块排名（更慢，默认不开）。
返回核心是 `horizons` 五档：

| 周期 | 含义 | 能不能给概率 |
|---|---|---|
| T+1 | 次日 | **能**，用 `p_up`（唯一标定数字） |
| T+3 | 3 个交易日内 | 不能，只给方向 |
| T+5 | 1 周 | 不能 |
| T+10 | 2 周 | 不能 |
| T+20 | 1 个月（封顶） | 不能 |

天数取 `app.agent.chain.label_spec.HOLDS` 的**唯一真源**，本文不写具体天数；
封顶 T+20 ≈ 1M 是 2026-10-23 的用户裁定，不要自行外推到 T+30/T+60。

## 两个置信度字段（不要混为一谈）

- `evidence`：**证据完整度** = 拿到的维度数 / 计入维度数（quote/tech/prediction/patterns/flow/theme）。
  它回答的是「数据齐不齐」，不是「我对不对」。
- `confidence`：**该周期方向的把握度** = 档位上限 × evidence × 信号强度。
  T+1 上限最高（有标定），越远的档位上限越低。

`confidence` 低 ≠ 看空。低只是说证据不足。

## 每周期必须落到四要素（硬纪律）

**禁止**「可能上涨」「建议关注」「谨慎乐观」「或有压力」这类措辞——它们没有可执行信息，
正是用户被迫二次分析的原因。每一档必须写全：

1. **方向**：看多 / 看空 / 中性 / 不适用（四选一）
2. **触发条件**：什么情况才算成立（如「回踩 ma10(12.30) 不破且量能转强」）
3. **证伪条件**：跌破/站上哪个价位就算判断错了
4. **关键价位**：从 `levels` 取（贴住的均线支撑、MA20、近 20 日高低）

数据不足时写「不适用」并说明缺什么——**这比编一段更合格**。

## 可以推翻模型，但要写明依据

`horizons[].basis` 列出了该档方向命中的条件。你不认同时必须写明理由，
且理由要落在可观测事实上（支撑是否守住、量能、资金方向、20 日位置），
不能只说「我觉得」。支撑与结构的优先级 ≫ 涨跌幅。

## 失败模式（别踩）

- 把 `p_up` 外推成 T+5 / T+20 的胜率 —— 它只覆盖 T+1
- 把「不适用」当成看空信号 —— 它只是没有足够证据
- 把低 `confidence` 当成风险信号 —— 风险看 `risks` 字段
- 编造数值：工具没给出的数字不允许出现在结论里
- 用 VWAP / 分时均价当买点 —— 价位只写日线均线与支撑

### 证据地基（`deep_cards`）

```
run_skill("stock-analyzer", {"fn": "deep_cards", "kwargs": {"codes": "600519,000858"}})
```

`deep_cards` 是 `analyze` 下面的**证据装配层**（自 market-screener 的 deep 层完整复制，见
`_deep.py` 顶部说明）：只取事实、不做判断、不给方向，返回两档

- `analyzed_rank[]`：**全部票**的横向可比字段（价位/均线/支撑压力/封板/量比/连板/资金/风险），不截断；
- `analyzed[]`：满卡，按字符预算截断，**与 rank 同序**（p_up 降序、无预测沉底），故满卡即证据最强的 N 只。
  分两档：前 3 只**标准卡**（全字段），其后为 `card_tier="compact"` 的**精简卡**
  （剔 `tech.support.supports/resists`、`flow.recent`、`shape`，这三类在 rank 行或 `inspect_stock` 里可复得）。
  实测 20 只票、默认预算：满卡 11 张（3 标准 + 8 精简），未分层时只有 9 张。

分工：`analyze` 给结论（方向/触发/证伪/价位），`deep_cards` 给证据。
两者不互相调用；需要「结论 + 全量证据」时先 `deep_cards` 再逐只 `analyze`。

字段口径与 market-screener 的 `deep_analyze` **完全一致**（同一份复制的 `_REPORT_GUIDE`）；
两份实现独立演化——改本技能的 `_deep.py` 不会影响选股技能，反之亦然。

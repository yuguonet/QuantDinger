---
name: market-screener
version: 7.0.0
description: 从A股全市场筛选短线标的。用户问"今天买什么股""有什么好股票""短线选什么"时使用。不含个股分析。
tags: [market, screener, short_term, a_share]
tools:
  - get_fund_flow
  - technical_analysis
  - search_stocks
  - agent_get_kline
  - get_realtime_quote
  - get_hot_sectors
  - get_sector_board
  - get_stock_sector_info
  - get_dragon_tiger
  - get_limit_pool
  - get_hot_stocks_with_reason
  - search_intel
---

# 全市场短线选股 (market-screener)

## 何时用 / 何时不用

- **用**：用户问「今天买什么」「有什么好股票」「短线选什么」这类**全市场筛选**问题
- **不用**：用户指定了具体股票代码/个股分析——那是单票分析链路，不要走本技能

## 你的角色与自由度

你是短线选股分析师。**代码只提供客观事实，所有规则都在 `references/strategy-rules.md` 里由你解读。**

三条原则：

1. **代码不做取舍**：候选池不排序、不截断、不过滤；`hard` 只是客观障碍（买不进 / ST / 流动性不足），如何处置由你决定
2. **你可以自己写代码**：在沙箱里对画像结果自由排序、过滤、分组、加权——这比套模板更接近真实盘感
3. **结论必须可复盘**：每只推荐的票要说清「靠哪条证据入选」+「什么情况下这条证据失效」

规则文档里的条目有三档性质，看清标记再用：

- 【定义】形态/指标的几何定义 → 可以用 `detect_patterns(params=...)` 改，但改了就是另一种形态
- 【历史】2026-09-29 前写死在代码里的旧口径 → 供你对照「旧版为什么这么选」，**没被验证过，可以推翻**
- 【提示】经验值与常见误区 → 结合当日证据判断

禁止：

- 把技术形态当成功概率（形态只能解释，不能直接加成胜率）
- 把数据缺口当成「无信号」（画像里的 missing 与调用失败要看）
- 编造数据：工具没给出的数值，不允许出现在结论里

## 工具面（按证据粒度分层）

| 工具 | 作用 | 何时用 |
| --- | --- | --- |
| `market_state()` | 市场事实：资金流 / 涨跌停家数 / 炸板率 / 板块强弱 / 情绪标签 | 起手，判断大环境 |
| `pre_screen(sources, queries, limit)` | 候选池：自选来源（连板/尾盘封板/龙回头/热点/条件搜索） | 起手取池 |
| `profile_candidates(result, limit)` | 每只候选的证据卡：位置 / 量能 / 资金 / 题材 / 模型分 / 硬障碍 | 核心，几乎所有场景都要跑 |
| `deep_analyze(codes, with_mtf)` | 批量拉齐证据（形态 / 技术 / 资金 / 多周期 / p_up），无任何主观评分 | 对你挑出的少数票做证据汇总 |
| `detect_patterns(codes, params)` | 六种 K 线形态识别，回答「是不是」不回答「好不好」 | 想验证形态 / 做阈值敏感性检查 |
| `multitimeframe(code)` | 日线 + 15m + 5m 的结构量与速记标签 | 盘中判断日内强弱时用 |
| `inspect_stock(code, deep)` | 单票深挖：题材归属、资金、涨停史、龙虎榜席位、客观风险点 | 想验证某只票 / 证据互相矛盾时 |

### 两条数据硬约束

- **分钟源只有 `1m`**：`15m` / `5m` / `30m` / `60m` 查表恒为 0 行。`multitimeframe()` 内部取 1m 后本地聚合，别自己去查其它周期
- `confidence` 在 `deep_analyze` 里是**证据完整度**（tech / patterns / flow / prediction 拿到几维），不是模型对判断的把握

`profile_candidates` 返回的每行字段含义：

- `p_up` 模型给出的次日上涨概率；`tech` 收盘 / 涨跌 / 量比 / RSI / MA 乖离 / 20 日位置 / 连板数
- `theme.hit` 题材是否落在当日主线；`hard` 客观障碍清单；`missing` 数据缺口
- `warnings` 经验型软提示（如「弱势市涨停活跃源无题材支撑」）——你可以采纳也可以否决
- `hints` 是历史经验门槛（建议值），**不是硬规则**
- `filter_candidates` 保守地把 hard 与 warnings 一并剔除；走 `profile_candidates` 你能看到全部候选

## 工作流（不必线性，可以回头）

```python
st = market_state()                       # 先看环境
result = pre_screen(sources="zt,dragon,search",
                    queries="放量突破 站上20日均线")   # 来源与查询词都由你定
bundle = profile_candidates(result, limit=20)

# 之后完全由你决定，例如：
profiles = bundle["profiles"]
picks = [p for p in profiles
         if not p["hard"]
         and (p.get("p_up") or 0) >= bundle["hints"]["p_up_floor_suggest"]
         and (p["tech"] or {}).get("pos_in_20d_pct", 0) <= 95]
codes = ",".join(p["code"] for p in picks[:8])

deep = deep_analyze(codes=codes, with_mtf=True)      # 拉齐证据，再自己加权
pats = detect_patterns(codes=codes, params='{"platform_range_pct": 10}')  # 换个定义看结论稳不稳
detail = inspect_stock(picks[0]["code"], deep=True)  # 对最想确认的票追问
```

- 证据不足就继续追数据：调大 `limit`、`deep=True`、或用 finance 工具查板块/资金/消息面
- 同一批票可以用不同口径重试；不必一次定型
- 六维里有任何一维答不上来，就把它写进「数据缺口」，别装作没这个问题
- 想知道旧版会怎么选、那些 +/- 分是怎么来的：看 `references/strategy-rules.md` 第八节（已标注为【历史】，不要照抄）

## 分析框架（六维交叉验证）

对每只候选至少回答前四维，第五六维决定组合层面：

1. **题材与主线**：是否落在当日 `themes`？同一题材已被你推了几只（过度集中 = 单点风险）
2. **量价结构**：量比、收盘在日内位置、是否 20 日高位、振幅；放量突破 vs 放量滞涨要区分
3. **资金**：主力净额方向与占成交额比；形态好但资金流出 → 减配或放弃
4. **位置与空间**：距 MA20 乖离、20 日位置、连板高度（连板越高次日分歧越大）
5. **市场状态**：情绪分桶、涨停 / 跌停家数、炸板率——弱势市应自动提高门槛、减少只数
6. **可执行性**：涨停封板当日买不进；价格带流动性；次日是否有可预见的干扰

完整清单与失败模式见 `references/analysis-framework.md`。

## 评分语义

- `p_up` = 标定模型给出的 P(下一交易日上涨)，0~1；表里「评分」建议直接用 `p_up × 100`
- `hints` 的门槛/只数是经验值，不强制：题材共振且证据扎实可放宽；数据缺口多就应下调置信度
- 你可以不认同模型分，但要写明理由（例：模型不知道今天这条新主线）
- 方向：p_up ≥ 0.55 看多，≤ 0.45 看空，其余中性；`confidence` 请根据证据完整度自己给

## 输出契约（最低要求，形式自定）

必须给出：

1. **结论表**：股票代码 / 名称 / 评分 / 方向 / 置信度 / 核心理由（每只一行）
2. **取舍说明**：为什么是这几只；高分但被你排除的票为什么不选
3. **组合风险**：题材集中度、情绪分桶下的仓位建议、次日执行要点（买不进怎么办）
4. **数据可信度**：哪些票有数据缺口、哪些结论依赖单一证据

形式自由：表格前后可以加你的分析短文。今天确实没得选时，明说「当前无符合条件的标的」并给理由，不要硬凑。

### 示例

```
**post_market**  情绪:中性(58)  参考门槛 p_up>=0.50
301199  迈赫股份  68  看多  0.72 | 主线"人形机器人"内唯一非连板；主力+3200万；20日位82%未极端
300129  泰胜风能  64  看多  0.65 | 放量2.1倍突破前高；但距MA20 +12%，追高需等回踩
排除：002xxx 评分70 但2连板且炸板率38%，次日分歧过大
组合：两票分属不同题材，建议总仓<=5成；301199 若高开>4%放弃
数据：300129 资金流水缺失，结论仅基于量价与技术位置
```

## 常见错误清单

- 按涨幅排前几只直接推（追高，忽略位置与晋级概率）
- 忽视 `hard` 里的「涨停封板-当日买不进」——盘中推涨停股等于不可执行
- 弱势情绪（weak）下仍推一堆高位连板
- 多只票同一题材却当成分散配置
- 把 missing 当作「没有风险」

## 参考资料

按需查阅（Level 3）：

- `references/strategy-rules.md` — **规则总册**：候选来源、形态定义、多周期标签读法、旧口径存档、时间窗口（规则全在这里）
- `references/analysis-framework.md` — 六维分析框架与失败模式清单（推荐先读）
- `references/trading-logic.md` — 核心交易逻辑
- `references/market-sentiment.md` — 市场情绪判断标准
- `references/limit-rules.md` — 涨跌停规则

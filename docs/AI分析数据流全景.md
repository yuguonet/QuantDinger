数据流向我已经全追清楚了。**是的，最终都交给 AI 了**，而且是双重使用：

---

## 数据流全景

```
sentiment.py (7个指标采集)
    │
    ├──→ 规则引擎评分 (代码硬编码)
    │       └── _calculate_macro_score(): VIX/DXY/利率 → 加减分
    │       └── _calculate_sentiment_score(): 新闻情绪 + 地缘政治惩罚
    │       └── 最终合成 objective_score (技术40% + 基本面35% + 情绪25%)
    │
    └──→ 喂给 LLM (GPT-4o) 作为 prompt 上下文
            └── fast_analysis.py 构建 system_prompt + user_prompt
            └── LLM 输出 BUY/SELL/HOLD + 置信度 + 分析
```

### 1. 规则引擎（硬编码打分）

`fast_analysis.py` 里的 `_calculate_macro_score()` 直接用 VIX/DXY 做数值评分：

| VIX 值 | 扣分 |
|--------|------|
| >35 | -50（极度恐慌，严重利空）|
| >30 | -40 |
| >25 | -30 |
| >20 | -15 |
| <12 | +20（低恐慌，利多）|

| DXY 变动 | 扣分 |
|----------|------|
| 涨 >2% | -30（美元大幅走强，严重利空）|
| 涨 >1% | -20 |
| 跌 >2% | +30 |
| 跌 >1% | +20 |

这些分数和新闻情绪分、技术分加权合成一个 `objective_score`，**用来校准 LLM 的输出**——如果 AI 的结论和规则引擎分数冲突很大，会触发修正逻辑。

### 2. 喂给 LLM 的 Prompt

`fast_analysis.py` 构建的 system_prompt 明确告诉 AI：

> - **Macro Environment (DXY, VIX, interest rates, geopolitical events)**: Can override technical signals
> - **Market Sentiment**: News sentiment, fear/greed index, market mood
> - Strong USD (DXY↑) usually negative for crypto/commodities → Consider SELL
> - High VIX (>30) indicates fear → Consider SELL or HOLD, avoid BUY

然后在 user prompt 里注入格式化后的实时数据：

```
- USD Index: 104.52 (↑0.35%)
  ⚠️ 美元↑ 利空加密货币
- VIX: 28.65 - 较高恐慌 (20-30)
- 10Y Treasury: 4.325% (↑)
  ⚠️ 高利率环境，对估值不利
- Gold: $2,345.20 (↑1.25%)
```

### 3. AI 输出也包含情绪分

LLM 被要求返回 JSON，其中必须包含 `sentiment_score: 0-100`，和 `technical_score`、`fundamental_score` 并列。

### 4. 校准机制

`_calculate_objective_score()` 合成四个维度：
- 技术分 40%
- 基本面分 35%
- **情绪分 25%**（包括新闻情绪 + 宏观 VIX/DXY）
- 地缘政治事件惩罚（可额外扣最多 -55 分）

如果 `objective_score >= +20` → 规则引擎建议 BUY；`<= -20` → SELL。AI 的最终决策应该和这个分数对齐。

---

### 一句话总结

**sentiment.py 采集的 7 个指标不是给前端展示就完了——它们同时被两条管道消费：**
1. **硬编码规则引擎**算出一个客观分数
2. **作为 LLM prompt 的上下文**让 GPT-4o 做综合判断

两条管道的输出还会互相校验。这其实是个不错的架构——纯靠 AI 容易 hallucinate，纯靠规则又太死板，两者结合算是取了个折中。
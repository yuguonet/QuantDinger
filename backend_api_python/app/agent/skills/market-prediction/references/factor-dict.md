# market-prediction 因子字典

> 所有因子的定义、计算口径、数据源、更新频率、取值范围。

---

## 1. 技术面因子

| 因子名 | 定义 | 计算口径 | 数据源 | 更新频率 | 取值范围 | 权重 |
|--------|------|----------|--------|----------|----------|------|
| ma_trend | 均线多头排列度 | MA5/10/20/60 相对位置离散档 | `app.market_cn.index.get_index_daily_kline("000001")` | 日收盘后 | 0-100 | 30% |
| ma_slope | MA20 斜率 | (MA20[-1] - MA20[-6]) / MA20[-6] * 100 | 同上 | 日收盘后 | 0-100 | 15% |
| macd | MACD 信号强度 | DIF/DEA/HIST 离散档（金叉/死叉/扩张/收缩） | 同上 | 日收盘后 | 0-100 | 20% |
| bollinger | 布林带位置 | (Close - Lower) / (Upper - Lower) * 100 | 同上 | 日收盘后 | 0-100 | 15% |
| trend_strength | 趋势强度 | 简化 ADX：+DM/-DM/TR 计算 DX | 同上 | 日收盘后 | 0-100 | 10% |
| key_level_break | 关键位突破 | 60日新高/新低、20日高低、整数关口距离 | 同上 | 日收盘后 | 0-100 | 10% |

---

## 2. 资金面因子

| 因子名 | 定义 | 计算口径 | 数据源 | 更新频率 | 取值范围 | 权重 |
|--------|------|----------|--------|----------|----------|------|
| north_flow | 北向资金连续性 | 5日/10日净流入(万) + 连续流入天数 | `app.market_cn.fund_flow_local` | 盘中实时/收盘定格 | 0-100 | 35% |
| margin_flow | 融资余额变化 | 5日/10日融资余额变化率(%) | 同上 | 日收盘后 | 0-100 | 25% |
| volume_trend | 成交量趋势 | 5日/20日/60日均量比 | `app.market_cn.index.get_index_daily_kline` | 日收盘后 | 0-100 | 20% |
| persistence | 资金持续性 | 20日内净流入天数占比 + 最大连续同向天数 | `app.market_cn.fund_flow_local` | 日收盘后 | 0-100 | 20% |

---

## 3. 情绪面因子

| 因子名 | 定义 | 计算口径 | 数据源 | 更新频率 | 取值范围 | 权重 |
|--------|------|----------|--------|----------|----------|------|
| limit_up_down | 涨跌停情绪 | 涨停数 + 涨跌停比 + 跌停数惩罚 | `app.market_cn.emotion.get_emotion_snapshot()` | 盘中实时/收盘定格 | 0-100 | 40% |
| broken_rate | 炸板率惩罚 | 炸板数 / (涨停数 + 炸板数)，反向评分 | 同上 | 盘中实时/收盘定格 | 0-100 | 20% |
| fear_greed | 贪恐指数 | 7维度综合贪恐指数 composite_score | `app.market_cn.fear_greed_index.fear_greed_index()` | 日收盘后 | 0-100 | 25% |
| new_highs_lows | 新高新低广度 | 新高数奖励 - 新低数惩罚 | `app.market_cn.emotion` | 日收盘后 | 0-100 | 15% |

---

## 4. 估值面因子

| 因子名 | 定义 | 计算口径 | 数据源 | 更新频率 | 取值范围 | 权重 |
|--------|------|----------|--------|----------|----------|------|
| pe_percentile | PE 历史分位 | 当前 PE 在 3年历史中的百分位，反向评分 | `app.market_cn.index.get_index_valuation_history()` | 日收盘后 | 0-100 | 35% |
| pb_percentile | PB 历史分位 | 当前 PB 在 3年历史中的百分位，反向评分 | 同上 | 日收盘后 | 0-100 | 25% |
| yield_gap | 股债利差 | 股息率 - 10年国债收益率，分档评分 | 同上 | 日收盘后 | 0-100 | 25% |
| mean_reversion | 估值均值回归 | (PE/PE_MA250 + PB/PB_MA250) / 2，偏离度评分 | 同上 | 日收盘后 | 0-100 | 15% |

---

## 5. 模型输出字段

| 字段 | 定义 | 来源 | 取值范围 |
|------|------|--------|----------|
| p_up | P(大盘次日上涨) | watchlist.predict.market_p_up + 四维因子融合 | 0.0-1.0 |
| logit | Logistic 模型 logit 值 | 同上 | -8 到 8 (已钳制) |
| direction | 方向标签 | p_up 阈值判定 | bullish/neutral/bearish |
| confidence | 模型置信度 | 模型一致性 + 因子完整度 | 0.1-0.95 |
| exp_ret_bp | 预期收益(基点) | 45 * (p_up - 0.5) * 2，钳制 ±180 | -180 到 180 |
| breakdown | 贡献分解 | 模型项 + 因子项 + 权重 | dict |

---

## 6. 仓位分档映射

| 综合分 | 档位名 | 最大仓位 | 操作建议 |
|--------|--------|----------|----------|
| 80-100 | aggressive | 100% | 积极进取 |
| 60-79 | half | 60% | 择机买入 |
| 40-59 | light | 30% | 观望为主 |
| 0-39 | defensive | 10% | 严防风险 |

---

## 7. 多周期方向衰减逻辑

| 周期 | p_up 计算 | 置信度衰减 |
|------|-----------|------------|
| T+1 | 模型直接输出 | 1.0x |
| T+3 | p_up * 0.9 + 0.05 | 0.85x |
| T+5 | p_up * 0.8 + 0.1 | 0.7x |

*注：向 0.5 均值回归，置信度随周期衰减*

---

## 8. 关键位计算

| 位置类型 | 计算逻辑 |
|----------|----------|
| 压力位 | MA20/MA60(>当前价)、整数关口(>当前价且<当前价*1.1) |
| 支撑位 | MA20/MA60(<当前价)、整数关口(<当前价且>当前价*0.9) |

*取前 5 个，去重排序*

---

## 9. 风险标记触发条件

| 标记 | 触发条件 |
|------|----------|
| 模型特征漂移预警 | `health.drift_alert == true` (PSI > 0.2) |
| 信号衰减预警 | `health.signal_decay == true` (IC < 0.02) |
| 多维度分歧大 | max(四维分) - min(四维分) > 40 |
| 布林带上轨压力 | `technical.details.bollinger > 85` |
| 布林带下轨支撑 | `technical.details.bollinger < 15` |
| 北向资金持续流出 | `fund_flow.details.north_flow < 30` |
| 炸板率过高 | `sentiment.details.broken_rate < 30` |
| PE历史极低分位 | `valuation.details.pe_percentile < 20` |
| PE历史极高分位 | `valuation.details.pe_percentile > 90` |

---

## 10. 数据依赖清单

| 模块 | 函数 | 用途 | 备选 |
|------|------|------|------|
| `app.market_cn.index` | `get_index_daily_kline` | 指数日线、成交量 | 无 |
| `app.market_cn.index` | `get_index_valuation_history` | 估值历史(PE/PB/股息率/国债) | 无 |
| `app.market_cn.fund_flow_local` | `get_fund_flow_summary` | 北向/融资/日度资金流 | 无 |
| `app.market_cn.emotion` | `get_emotion_snapshot` | 涨跌停/炸板/新高新低 | 无 |
| `app.market_cn.fear_greed_index` | `fear_greed_index` | 7维贪恐指数 | 无 |
| `app.watchlist.predict` | `load_market_klines/market_raw_features/market_score_from_features` | 大盘预测模型 | 无 |

---

*文档版本：v1.0 | 更新：2026-11-27 | 维护：market-prediction 技能组*
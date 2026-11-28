# market-prediction 因子字典

> 所有因子的定义、计算口径、数据源、更新频率、取值范围。
> 2026-11-28 v1.1：北向/两融彻底剔除；估值改为技能内直连 akshare 长短周期融合。

---

## 1. 技术面因子

| 因子名 | 定义 | 计算口径 | 数据源 | 更新频率 | 取值范围 | 权重 |
|--------|------|----------|--------|----------|----------|------|
| ma_trend | 均线多头排列度 | MA5/10/20/60 相对位置离散档 | `app.market_cn.index.get_index_daily_kline("000001")` | 日收盘后 | 0-100 | 30% |
| ma_slope | MA20 斜率 | (MA20[-1] - MA20[-21]) / MA20[-21] * 100，×10 映射 | 同上 | 日收盘后 | 0-100 | 15% |
| macd | MACD 信号强度 | DIF/DEA/HIST 离散档（金叉/死叉/扩张/收缩） | 同上 | 日收盘后 | 0-100 | 20% |
| bollinger | 布林带位置 | (Close - Lower) / (Upper - Lower) * 100，中轨附近高分 | 同上 | 日收盘后 | 0-100 | 15% |
| trend_strength | 趋势强度 | 简化 ADX：+DM/-DM/TR 计算 DX | 同上 | 日收盘后 | 0-100 | 10% |
| key_level_break | 关键位突破 | 60日新高/新低、20日高低、动态整数关口距离 | 同上 | 日收盘后 | 0-100 | 10% |

---

## 2. 资金面因子（北向/两融已彻底剔除）

| 因子名 | 定义 | 计算口径 | 数据源 | 更新频率 | 取值范围 | 权重 |
|--------|------|----------|--------|----------|----------|------|
| net_flow | 主力净流入强度 | net_pct（净流入/成交额 %）分档 + 方向修正 | `app.market_cn.fund_flow_api.market_flow_summary(days=20)` | 收盘定格（含 freshness 标记） | 0-100 | 40% |
| persistence | 资金持续性 | direction_seq 近20日最大连续流入/流出 + 流入天数占比 | 同上（history.direction / net_yi） | 日收盘后 | 0-100 | 35% |
| volume_trend | 成交量趋势 | 5/20/60 日均量比 | `app.market_cn.index.get_index_daily_kline("000001")` | 日收盘后 | 0-100 | 25% |

> 北向资金自 2024-08-16 停更（滞后 551+ 交易日）；全市场两融余额无数据源 ⇒ 两者均不再作为因子，避免旧值伪装成有效信号。

---

## 3. 情绪面因子

| 因子名 | 定义 | 计算口径 | 数据源 | 更新频率 | 取值范围 | 权重 |
|--------|------|----------|--------|----------|----------|------|
| limit_up_down | 涨跌停情绪 | 涨停数 + 涨跌停比 + 跌停数惩罚 | `app.market_cn.dragon_limit.get_zt_pool/get_dt_pool`（当日协调器池），空池回退情绪快照 ztjs/dtjs | 盘中实时/收盘定格 | 0-100 | 40% |
| broken_rate | 炸板率惩罚 | 炸板数 / (涨停数 + 炸板数)，反向评分 | `get_broken_board()`，回退快照 zbjs | 盘中实时/收盘定格 | 0-100 | 20% |
| fear_greed | 贪恐指数 | composite_score（顶层字段，非 data 包裹）分档，极端贪婪/恐惧反向修正 | `app.market_cn.fear_greed_index.fear_greed_index()` | 5分钟缓存 | 0-100 | 25% |
| breadth | 市场宽度 | 上涨家数占比 szbl（%）分档 | `app.market_cn.emotion.fetch_emotion_cycle().data.emotion.szbl` | 日收盘后 | 0-100 | 15% |

---

## 4. 估值面因子（技能内直连 akshare，用户 2026-11-28 拍板）

| 因子名 | 定义 | 计算口径 | 数据源 | 更新频率 | 取值范围 | 权重 |
|--------|------|----------|--------|----------|----------|------|
| pe_percentile | PE 分位融合 | 长周期(沪深300滚动PE 260点)定方向 0.6 + 短周期(上证平均PE 336点)定节奏 0.4；分位越低分越高 | `ak.stock_index_pe_lg("沪深300")` + `ak.stock_market_pe_lg("上证")`（技能内直连，30分钟缓存） | 日收盘后 | 0-100 | 35% |
| pb_percentile | PB 分位融合 | 同 PE；沪深300市净率 260点 + 上证市净率 263点 | `ak.stock_index_pb_lg("沪深300")` + `ak.stock_market_pb_lg("上证")` | 日收盘后 | 0-100 | 25% |
| yield_gap | 股债利差 | 股息率1(沪深300 csindex 20日最新) - 中国10年期国债收益率，分档 | `ak.stock_zh_index_value_csindex("000300")` + `ak.bond_zh_us_rate()` | 日收盘后 | 0-100 | 25% |
| mean_reversion | 估值中枢偏离 | 当前值 / 历史中位数（月频序列，不用假 250 日均线），PE/PB 平均 | 同 PE/PB 历史 | 日收盘后 | 0-100 | 15% |

> `akshare` 直连仅在本技能内部，不动 app/data_sources 数据层；所有网络数据 30 分钟进程内缓存，失败如实记入 `details.warnings` 并降置信度。

---

## 5. 模型输出字段

| 字段 | 定义 | 来源 | 取值范围 |
|------|------|------|----------|
| p_up | P(大盘次日上涨) | watchlist.predict.market_score_from_features + 可用维度重新归一融合 | 0.0-1.0 |
| logit | Logistic 模型 logit 值 | 同上 | -8 到 8 (已钳制) |
| direction | 方向标签 | p_up 阈值判定 | bullish/neutral/bearish |
| confidence | 模型置信度 | 一致性 × 完整度折扣 + 完整度，降级维度不伪装一致 | 0.1-0.95 |
| exp_ret_bp | 预期收益(基点) | 45 * (p_up - 0.5) * 2，钳制 ±180 | -180 到 180 |
| breakdown.available_dimensions / degraded_dimensions | 因子可用/降级维度 | compute_all_factors() | list |
| breakdown.weights | 动态权重（因子全降级时 model=1.0） | predict_market_direction() | dict |

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

*注：向 0.5 均值回归，置信度随周期衰减；健康度未实测时最终置信度上限 0.75，原始一致性存于 `confidence_raw`，报告文案标注“已折减”。*

---

## 8. 关键位计算

| 位置类型 | 计算逻辑 |
|----------|----------|
| 压力位 | MA20/MA60(>当前价)、动态整数关口(当前价±5档，按点位自动选步长 100/10/1) |
| 支撑位 | MA20/MA60(<当前价)、动态整数关口 |

*取前 5 个，去重排序；不再写死 2500-4400*

---

## 9. 风险标记触发条件

| 标记 | 触发条件 |
|------|----------|
| 模型特征漂移预警 | `health.drift_alert == true` |
| 信号衰减预警 | `health.signal_decay == true` |
| 多维度分歧大 | max(四维分) - min(四维分) > 40 |
| 布林带上轨压力 | `technical.details.bollinger > 85` |
| 布林带下轨支撑 | `technical.details.bollinger < 15` |
| 主力资金持续性偏弱 | `fund_flow.details.persistence < 30` |
| 资金流数据滞后 | `fund_flow.details.freshness == "stale"` |
| 炸板率过高 | `sentiment.details.broken_rate < 30` |
| PE历史极低/极高分位 | `valuation.details.pe_percentile < 20 / > 90` |

---

## 10. 数据依赖清单

| 模块 | 函数 | 用途 | 备选 |
|------|------|------|------|
| `app.market_cn.index` | `get_index_daily_kline` | 指数日线、成交量 | 无 |
| `app.market_cn.fund_flow_api` | `market_flow_summary` | 主力资金净流入/持续性 | 无 |
| `app.market_cn.dragon_limit` | `get_zt_pool/get_dt_pool/get_broken_board` | 涨跌停/炸板池 | 情绪快照字段回退 |
| `app.market_cn.emotion` | `fetch_emotion_cycle` | 市场宽度/情绪快照 | 无 |
| `app.market_cn.fear_greed_index` | `fear_greed_index` | 贪恐指数（顶层 composite_score） | 无 |
| akshare（技能内直连） | `stock_market_{pe,pb}_lg / stock_index_{pe,pb}_lg / stock_zh_index_value_csindex / bond_zh_us_rate` | 估值分位/股债利差 | 失败则该维度降级 |
| `app.watchlist.predict` | `load_market_klines/market_raw_features/market_score_from_features` | 大盘预测模型 | 无 |

---

*文档版本：v1.1 | 更新：2026-11-28 | 维护：market-prediction 技能组*

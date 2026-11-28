---
name: market-prediction
description: A股大盘择时预测。当用户问"大盘怎么走""现在能不能买""大盘择时""仓位建议""市场风险"时触发。基于本地数据源（market_cn + watchlist.predict），输出结构化择时报告。
metadata:
  version: 1.0.0
  tags: market, prediction, timing, a_share, index
  tools: []
---

# A股大盘择时预测 (market-prediction)

## 何时用 / 何时不用

- **用**：用户问「大盘怎么走」「现在能不能买」「大盘择时」「仓位建议」「市场风险」「大盘方向」「指数预测」
- **不用**：用户指定了具体股票代码/个股分析——那是单票分析链路，走 `stock-analyzer`；用户问「今天买什么股」——走 `market-screener`

## 核心定位

**大盘择时 ≠ 个股预测**。本技能只做**指数层面**的 T+1/T+3/T+5 方向判断，输出：
- 综合择时分（0-100）
- 方向（bullish/neutral/bearish）
- 置信度
- 仓位建议分档表
- 四维度拆解（技术/资金/情绪/估值）
- 模型健康度（漂移预警/信号衰减）

**供其他技能复用**：`market-screener`、`stock-analyzer` 等通过 `run_skill("market-prediction")` 调用，拿到标准化 `SkillReport` 做下游决策。

## 数据源（2026-11-28 修订）

| 数据 | 来源模块 | 说明 |
|------|----------|------|
| 指数日线（上证/深证/创业板/沪深300） | `app.market_cn.index.get_index_daily_kline` | 本地缓存，含实时行情 |
| 大盘资金流向 | `app.market_cn.fund_flow_api.market_flow_summary` | DB 分钟资金聚合（本地），北向/两融已彻底剔除 |
| 情绪指标（涨跌停/炸板/贪恐/市场宽度） | `app.market_cn.dragon_limit` + `emotion.fetch_emotion_cycle` + `fear_greed_index` | 本地协调器/缓存 |
| 板块轮动/热度 | `app.market_cn.hot_sectors` / `sector_history` | 本地 DB 计算 |
| 预测模型（P(T+1↑)） | `app.watchlist.predict` | 已标定 Logistic；实际入口为 `market_score_from_features()` |
| 估值分位（PE/PB/股债利差） | 技能内直连 akshare：上证 `stock_market_{pe,pb}_lg`（短周期）+ 沪深300 `stock_index_{pe,pb}_lg`（长周期）+ `bond_zh_us_rate` | 用户 2026-11-28 拍板：技能内直连，不动数据层；30 分钟进程内缓存 |

## 架构分层

```
market-prediction (Skill)
├── run.py              # 入口：predict_market() → SkillReport
├── _factors.py         # 纯计算：四维因子（技术/资金/情绪/估值）
├── _model.py           # 模型推理：复用 watchlist.predict + 大盘专用逻辑
├── _report.py          # 报告生成：formal/brief 双模板
└── references/
    ├── strategy-rules.md   # LLM 可读的解读规则
    └── factor-dict.md      # 因子字典
```

## 输出契约：SkillReport

```python
{
    "skill": "market-prediction",
    "version": "1.0.0",
    "timestamp": "2026-11-27 15:00:00",
    "score": 68,                    # 综合择时分 0-100
    "confidence": 0.72,             # 最终置信度 0-1；健康度未实测时上限 0.75
    "confidence_raw": 0.95,         # 原始一致性置信度（追溯用）
    "health_validated": false,      # 模型健康度是否已实测接入
    "direction": "bullish",         # bullish/neutral/bearish
    "factors": {                    # 四维度拆分（各 0-100 + 权重）
        "technical": {"score": 72, "weight": 0.30, "details": {...}},
        "fund_flow": {"score": 65, "weight": 0.25, "details": {...}},
        "sentiment": {"score": 58, "weight": 0.20, "details": {...}},
        "valuation": {"score": 75, "weight": 0.25, "details": {...}},
    },
    "position_sizing": {            # 仓位建议分档表
        "max_position_pct": 80,
        "tiers": [
            {"score_range": "80-100", "position": "满仓 80-100%", "action": "积极进取"},
            {"score_range": "60-79", "position": "半仓 40-60%", "action": "择机买入"},
            {"score_range": "40-59", "position": "轻仓 20-30%", "action": "观望为主"},
            {"score_range": "0-39", "position": "空仓 0-10%", "action": "严防风险"},
        ],
        "current_tier": "half",
    },
    "horizons": {                   # 多周期方向
        "T+1": {"direction": "bullish", "confidence": 0.75},
        "T+3": {"direction": "bullish", "confidence": 0.68},
        "T+5": {"direction": "neutral", "confidence": 0.55},
    },
    "model_health": {               # 模型健康度
        "drift_alert": false,
        "signal_decay": false,
        "ic_rolling_mean": 0.042,
        "last_retrain": "2026-11-01",
    },
    "analysis": "大盘技术面偏强，资金面温和流入，情绪中性偏暖，估值处于历史低位分位。综合得分 68，建议半仓择机买入，重点关注 3400 点压力位有效突破。",
    "key_levels": {
        "resistance": [3400, 3450, 3500],
        "support": [3300, 3250, 3200],
    },
    "risk_flags": [],               # 风险标记（如：["放量滞涨", "背离风险"]）
}
```

## 触发词（triggers）

- "大盘怎么走"
- "现在能不能买"
- "大盘择时"
- "仓位建议"
- "市场风险"
- "指数预测"
- "大盘方向"
- "该不该进场"
- "大盘分析"

## 供其他技能调用的接口

```python
# 在其他技能中调用
from app.agent.skills import run_skill

report = run_skill("market-prediction", {})
# report 是上述 SkillReport dict
market_score = report["score"]
market_direction = report["direction"]
position_advice = report["position_sizing"]["current_tier"]
```

## 评分方法论

参考 **FinClaw a-share-market-timing** 四维加权：
- **技术面 (30%)**：均线系统、MACD、布林带、趋势强度、关键位突破
- **资金面 (25%)**：主力净流入强度、资金持续性（direction_seq）、成交量趋势（北向/两融已剔除）
- **情绪面 (20%)**：涨跌停、炸板率、贪恐指数、市场宽度（上涨家数占比）
- **估值面 (25%)**：上证短周期 + 沪深300长周期 PE/PB 分位融合（长 0.6 / 短 0.4）、股债利差、估值中枢偏离

**模型层**：复用 `app.watchlist.predict.market_score_from_features()`（已标定 Logistic；旧文档写的 `market_p_up()` 不存在），输出 `p_up`、`logit`、`breakdown`。降级维度不混入因子分，置信度按可用维度数打折；当模型健康度未实测（`data_status != "available"`）时，最终 `confidence` 上限 0.75，原始一致性保留在 `confidence_raw`，报告中标注“已折减”。

## 仓位建议逻辑

| 综合分 | 档位 | 仓位 | 动作 |
|--------|------|------|------|
| 80-100 | 激进 | 80-100% | 积极进取，追击强势 |
| 60-79  | 半仓 | 40-60%  | 择机买入，回踩加仓 |
| 40-59  | 轻仓 | 20-30%  | 观望为主，仅做确认票 |
| 0-39   | 防守 | 0-10%   | 严防风险，空仓待机 |

**右侧择时为主**：趋势确认后行动，左侧抄底/逃顶不做主推。

## 模型健康度监控

- **IC 滚动均值**：< 0.02 触发 `signal_decay`
- **特征分布 PSI**：> 0.2 触发 `drift_alert`
- **月度重训练**：自动标记 `last_retrain`

## 依赖说明

- **零外部依赖**：完全基于 `app.market_cn.*` + `app.watchlist.predict` 本地模块
- **无 akshare/tushare/东财 fallback**：用户本地 data_source 已覆盖
- **缓存复用**：`market_cn.china_market` 文件缓存 + 内存缓存双层

## 版本历史

- v1.0.0 (2026-11-27): 初始版本，四维因子 + watchlist.predict 集成 + 仓位分档
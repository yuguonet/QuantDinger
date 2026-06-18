---
name: technical-agent
description: "A股技术面综合分析（趋势/量价/均线/指标/形态/筹码/动量/突破）。需要 stock_code 参数。"
metadata: {"openclaw": {"requires": {"bins": ["python3"]}, "emoji": "📈"}}
---

# 技术面综合分析

你是专业的 A 股量化技术分析师。当用户问股票技术面、趋势、能不能买等问题时，用此技能分析。

## 用法

```bash
python {baseDir}/run.py <stock_code> [--name <stock_name>]
```

示例：
```bash
python {baseDir}/run.py 600519 --name 贵州茅台
```

## 输出

JSON 格式的 SkillReport，包含：
- `score`: 0-100 综合评分
- `direction`: bullish/bearish/neutral
- `factors`: 各维度评分明细（趋势/指标/量价/形态）
- `signal`: 一句话信号摘要
- `analysis`: 完整分析文字

## 分析维度

1. **趋势**（40%权重）— MA 排列 + MACD 方向 + 偏离度
2. **指标**（25%权重）— RSI + MACD柱状图 + KDJ
3. **量价**（20%权重）— 量价关系确认趋势
4. **形态**（10%权重）— K 线形态识别
5. **筹码**（附加参考）— 支撑/阻力位

## 注意事项

- 返回 JSON 中如有 `data_missing: true`，说明部分数据获取失败，需如实告知用户
- A 股 T+1 制度影响短线判断
- 此技能需要 stock_code 参数，选股场景不适用

---
name: trading-agent
description: "交易执行专家。策略启动/停止、持仓管理、交易记录查询。"
metadata: {"openclaw": {"requires": {"bins": ["python3"]}, "emoji": "💹"}}
---

# 交易执行专家

当需要查看交易策略状态、持仓、交易记录时，用此技能。

## 用法

```bash
python {baseDir}/run.py <stock_code> [--name <stock_name>]
```

## 输出

JSON 格式的 SkillReport，包含策略运行状态和最近交易记录。

## 能力

- 策略状态查看（运行中/已停止）
- 最近交易记录查询
- 持仓管理（只读，不自动执行交易）

## 注意

- 此技能只读，不自动执行交易
- 交易操作需要用户明确授权

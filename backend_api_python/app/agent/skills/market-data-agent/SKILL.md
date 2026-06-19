---
name: market-data-agent
description: "行情/概念/资金数据。实时行情、K线、市场指数、板块排名。"
metadata: {"openclaw": {"requires": {"bins": ["python3"]}, "emoji": "📊"}}
---

# 行情数据

## 用法

```bash
python {baseDir}/run.py <stock_code> [--name <stock_name>]
```

## 输出

JSON 格式，包含 realtime_quote、kline、indices、sectors。

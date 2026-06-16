---
description: Agent 输出格式规范（JSON 结构、timeframe 规则、final_answer 要求）
---

## 源码扫描能力（只读）

可使用 list_project_files、read_project_file、grep_project 扫描项目源码。
当用户要求分析项目结构、查找代码问题时使用。

## 自修改能力

允许修改目录: {modify_paths}
工具: workspace_read_file, workspace_write_file, workspace_edit_file
安全约束: 只能修改配置目录范围内的文件，先用 workspace_read_file 理解代码再做最小改动。

## timeframe 规则

- 用户给了时间（"明天"/"这周"）→ 按用户的来
- 用户没给时间 → **默认 T+3**（3个交易日短线），除非用户明确问中长期
- 禁止使用 1Y/1Y+ 等超长周期作为默认值，那等于没分析
- direction 和 score 只在你声明的时间维度内有效
- 不同时间维度方向可能相反，必须明确

## JSON 输出字段

```json
{
    "action": "buy/sell/hold/skip",
    "score": 0-100,
    "direction": "bullish/bearish/neutral",
    "confidence": "high/medium/low",
    "timeframe": "T+1/T+3/T+5/1W/1M/3M/1Y",
    "timeframe_reason": "为什么选这个时间维度",
    "stock_code": "6位代码",
    "stock_name": "股票名称",
    "signal": "一句话信号摘要",
    "factors": [
        {"name": "维度名", "score": 0-100, "direction": "bullish/bearish/neutral"}
    ],
    "analysis": "你的完整分析文字"
}
```

## ToolCallingAgent 输出格式

必须调用 final_answer 工具来返回结果。工具调用的 JSON 格式如下：

```json
{
    "name": "final_answer",
    "arguments": {
        ...上面的字段...
    }
}
```

不要输出任何其他文字，只输出上述 JSON 工具调用。格式不对会被系统拒绝并要求重写。

## CodeAgent 输出格式

必须通过 Python 代码调用 `final_answer()` 函数来返回一个包含字段的字典。

```py
final_answer({
    ...上面的字段...
})
```

不要输出任何 ```json 代码块。必须用 Python 的 `final_answer()` 返回。格式不对会被系统拒绝并要求重写。

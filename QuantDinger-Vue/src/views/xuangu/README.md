# 本地选股工具

基于东方财富条件选股 API 的本地选股工具，输入关键词即可筛选股票。

## 使用方式

### 方式一：直接打开
双击 `index.html` 即可在浏览器中使用。

### 方式二：本地服务器
```bash
python3 -m http.server 8899
# 打开 http://localhost:8899
```

## 功能

- 输入选股关键词（如"放量突破"、"MACD金叉"、"底部放量"等）
- 返回：股票代码、名称、最新价、涨跌幅、换手率、量比、成交额、市盈率、总市值
- 预设 8 个热门关键词快捷按钮
- 支持回车键快速搜索

## 技术原理

直接调用东方财富条件选股 API：
```
POST https://np-tjxg-b.eastmoney.com/api/smart-tag/stock/v3/pw/search-code
```

关键请求参数：
- `keyWordNew`: 搜索关键词
- `customDataNew`: 条件数据（JSON 字符串）
- `pageSize`: 每页数量
- `pageNo`: 页码

## 文件结构

```
stock-screener/
├── index.html    # 主页面（单文件，零依赖）
└── README.md     # 说明文档
```

## 浏览器兼容性

支持所有现代浏览器（Chrome、Firefox、Edge、Safari）。
无外部依赖，纯原生 HTML + CSS + JavaScript。

# 部署说明

## 文件 → 目标

| 文件 | 目标 |
|------|------|
| sina.py | app/data_sources/sina.py |
| eastmoney.py | app/data_sources/eastmoney.py |
| cn_stock.py | app/data_sources/cn_stock.py |
| cn_stock_extent.py | app/interfaces/cn_stock_extent.py |
| interfaces___init__.py | app/interfaces/__init__.py |

## 一键部署

```bash
cd QuantDinger/backend_api_python
cp app/data_sources/cn_stock.py{,.bak}
cp sina.py app/data_sources/sina.py
cp eastmoney.py app/data_sources/eastmoney.py
cp cn_stock.py app/data_sources/cn_stock.py
cp cn_stock_extent.py app/interfaces/cn_stock_extent.py
cp interfaces___init__.py app/interfaces/__init__.py
python3 -c "import ast; [ast.parse(open(f).read()) for f in ['app/data_sources/sina.py','app/data_sources/eastmoney.py','app/data_sources/cn_stock.py','app/interfaces/cn_stock_extent.py']]; print('OK')"
python3 test_cn_datasources.py 600519
```

## Fallback 链

| 方法 | Fallback | 缓存 TTL |
|------|----------|----------|
| get_ticker | 腾讯→东财→新浪 | 600s |
| get_kline | Twelve→腾讯→东财→新浪→yfinance→AkShare | 120-300s |
| get_index_quotes | 东财→腾讯→新浪→AkShare | 120s |
| get_market_snapshot | 东财全量→AkShare | 120s |
| get_stock_info | 东财→AkShare | 3600s |
| get_all_stock_codes | 东财→AkShare | 86400s |
| get_stock_fund_flow | 东财→AkShare | 300s |

无新增依赖。

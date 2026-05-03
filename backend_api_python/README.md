# 2026-05-03 修改备份

## 修改清单

| 文件 | 修改内容 |
|------|---------|
| `app/routes/xuangu.py` | 删除 `/sync` 端点（移除对不存在的 `stock_selection_sync` 的引用） |
| `app/market_store/__init__.py` | 新建空文件，确保 `from app.market_store.plugin_api` 正常导入 |
| `app/services/symbol_name.py:24` | `normalize_cn_code`/`normalize_hk_code` 改走 `app.data_sources.normalizer` |
| `app/services/market_data_collector.py` | 5处 normalize 引用改走 `normalizer`（行 654, 684, 1327, 1824, 1855） |
| `app/interfaces/cn_stock_extent.py` | 1) normalize + _em_secid_from_cn 改走 normalizer; 2) 移除 cn_stock 继承，改为独立类; 3) 内联 _fetch_with_timeout/_get_timeout |

## 删除的文件

- `app/data_sources/test_cn_datasources.py` — 用户确认无用
- `app/data_sources/cn_stock.py` — 死代码分析确认，主链路完全绕开

## 日志

- `memory/2026-05-03.md` — 完整工作日志
- `memory/MEMORY.md` — 长期记忆

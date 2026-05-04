# QuantDinger Provider 层改造日志

日期: 2026-05-04
操作人: AI Assistant

## 一、改造内容

### 1. 新建文件

| 文件 | 说明 |
|---|---|
| `provider/twelve_data.py` | Twelve Data 海外付费兜底源 Provider (priority=100) |

### 2. 修改文件

| 文件 | 改动 |
|---|---|
| `provider/__init__.py` | 新增 `NotSupportedResult` 类、`is_not_supported()` 工具函数；`BaseDataSource` Protocol 文档更新 |
| `provider/tencent.py` | 补齐 `fetch_kline_batch` → NotSupportedResult |
| `provider/sina.py` | 补齐 `fetch_kline_batch` → NotSupportedResult |
| `provider/eastmoney.py` | 补齐 `fetch_kline_batch` → NotSupportedResult |
| `provider/akshare.py` | 补齐 `fetch_kline_batch` → NotSupportedResult |
| `provider/tdx.py` | 补齐 `fetch_kline_batch` → NotSupportedResult |
| `provider/efinance.py` | 补齐 `fetch_kline_batch` → NotSupportedResult |
| `provider/huatai.py` | 补齐 `fetch_kline_batch` → NotSupportedResult |
| `provider/baostock.py` | 补齐 `fetch_kline_batch` → NotSupportedResult |
| `provider/vnpy_datafeed.py` | 补齐 `fetch_kline_batch` → NotSupportedResult |
| `provider/joinquant.py` | 补齐 `fetch_kline_batch` → NotSupportedResult |
| `provider/hk_stock.py` | 补齐 `fetch_kline_batch` → NotSupportedResult |
| `coordinator.py` | 新增 `preferred_source` 参数（coordinate_kline / coordinate_ticker）；新增 `_get_preferred_available()` 方法 |
| `source_config.py` | 新增 tdx/efinance/huatai/baostock/vnpy/joinquant 的 SourceConfig 条目 |

## 二、Provider 完整列表（12个自注册源）

| Priority | Name | 文件 | kline | kline_batch | quote | batch_quote |
|---|---|---|---|---|---|---|
| 10 | tencent | tencent.py | ✅ | NSR | ✅ | ✅ |
| 20 | sina | sina.py | ✅ | NSR | ✅ | ✅ |
| 25 | tdx | tdx.py | ✅ | NSR | ✅ | ✅ |
| 30 | eastmoney | eastmoney.py | ✅ | NSR | ✅ | ✅ |
| 35 | efinance | efinance.py | ✅ | NSR | ✅ | ✅ |
| 40 | baostock | baostock.py | ✅ | NSR | ✅ | ✅ |
| 40 | hk_stock | hk_stock.py | ✅ | NSR | ✅ | ✅ |
| 45 | joinquant | joinquant.py | ✅ | NSR | ✅ | ✅ |
| 50 | akshare | akshare.py | ✅ | NSR | ✅ | ✅ |
| 50 | vnpy | vnpy_datafeed.py | ✅ | NSR | ✅ | ✅ |
| 55 | huatai | huatai.py | ✅ | NSR | ✅ | ✅ |
| 100 | twelvedata | twelve_data.py | ✅ | NSR | ✅ | NSR |

NSR = NotSupportedResult（不支持该接口）

## 三、遗留问题（待处理）

### 核心问题：`cn_stock.py` 的 `_build_sources()` 绕过了 Provider 层

调用链：
```
Route (kline.py)
  → KlineService.get_kline() / prewarm_all()
    → DataSourceFactory.get_kline() / get_kline_batch()
      → CNStockDataSource.get_kline() / get_kline_batch()
        → _build_sources()         ← 🔴 硬编码旧函数，不走 Provider
        → Coordinator.coordinate_kline()
```

`_build_sources()` 直接调用 `data_sources/` 根目录下的旧函数（tencent.py、sina.py、eastmoney.py、asia_stock_kline.py），
完全不使用 `provider/` 目录下的标准化 Provider。

### 导致的后果

1. **Provider 标准化4接口** → 无效，`_build_sources()` 不调 Provider
2. **`NotSupportedResult` 机制** → 无效，Coordinator 收到的是旧函数返回值
3. **`preferred_source` 参数** → 对 kline 无效，源列表由 `_build_sources` 硬编码
4. **接口优先级 `{capability}_priority`** → 无效，不通过 `get_providers()` 排序
5. **`source_config.py` 动态权重** → Coordinator 使用，但源列表不含新 Provider

### 待修复方案

`cn_stock.py` 的 `_build_sources()` 应改为从 Provider 层获取源列表：

```python
# 改造前（当前）：
def _build_sources(tf, lim):
    sources = []
    sources.append(("tencent", lambda sym, _tf, _lim: 
        tencent_kline_rows_to_dicts(fetch_kline(sym, ...))))  # 硬编码
    ...

# 改造后（目标）：
def _build_sources(tf, lim):
    from app.data_sources.provider import get_providers
    providers = get_providers(capability="kline", timeframe=tf, market="CNStock")
    sources = []
    for p in providers:
        sources.append((
            p.name,
            lambda sym, _tf, _lim, _p=p: _p.fetch_kline(sym, _tf, _lim)
        ))
    return sources
```

这样 `preferred_source`、`NotSupportedResult`、接口优先级才能真正生效。

### 其他注意事项

- Referer 池（tencent/sina/eastmoney）保留，零开销，防反爬
- 接口优先级保留，是 Provider 层核心调度机制
- 所有源的 K 线 API 都是 per-symbol 设计，无原生批量接口
- `fetch_kline_batch` 返回 `NotSupportedResult` 是正确的，批量通过 Coordinator 并发单只实现

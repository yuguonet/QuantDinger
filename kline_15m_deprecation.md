# kline_15m_YYYY 废弃 / 改用 kline_1m_YYYY 设计日志

> 记录日期: 2026-08-31
> 状态: 待处理（调查完成 + 前置 bug 已修，主改造未实施）

## 背景与目标

去掉 kline_15m_YYYY 表的读写，改用 kline_1m_YYYY：
- 15m/30m/1h/2h/4h 盘后数据改为「读取时按需从 1m 聚合」
- 不再落 15m 存储

## 现状：kline_15m_YYYY 用途

- 表结构: kline_{tf}_{year} 年分表（`app/utils/db_multi.py` `_table_name`）
- 数据: 每天每股 16 根 15m bar（bar 时间为收盘时刻, 不含 9:30 集合竞价）
- 写入: 每天盘后 mootdx 全量覆写; 历史用 source_sync 回填
- 读取: 盘后 K 线 API + 回测/信号脚本

## 写 kline_15m_YYYY 的位置

| 文件 | 说明 |
|---|---|
| `backend_api_python/app/data_sources/backfill_db.py` | `run_15m()` (L403) → `sync_tf("15m")`，盘后每天覆写 |
| `backend_api_python/app/market_cn/scheduler.py` | `_refresh_backfill_15m()` (L150)，在 `_post_market_batch()`(L195) 每天触发 |
| `fix/source_sync.py` | `write_stock_data`/`write_batch_data` 泛型 `kline_{tf}_{year}`（`-T 15m` 时写 15m 表），历史回填工具 |
| `backend_api_python/app/utils/db_multi.py` | `_init_market_schema()`(L485) 初始化近 5 年 15m 表 |
| `backend_api_python/app/utils/db_market.py` | `MarketKlineWriter`（泛型 upsert/bulk_write 接口, 可传 "15m"） |

## 读 kline_15m_YYYY 的位置

| 文件 | 说明 |
|---|---|
| `backend_api_python/app/data_sources/cn_stock.py` | 核心读方：盘后 15m/30m/1h/2h/4h API 链路 `_get_kline_15m_based`(L536) → `_read_db_15m`(L614)、`_check_15m_fresh`(L559)、`_aggregate_from_15m`(L646)、`_TF_BAR_COUNT`(L534)={\"15m\":1,\"30m\":2,\"1h\":4,\"2h\":8,\"4h\":16}、`need_bars=limit*bar_count+16`(L547) |
| `backend_api_python/app/agent/skills/market_screener/intraday.py` | `fetch_intraday_kline(code,\"15m\")` (L41, L387)，via `writer.query(\"CNStock\",code,\"15m\")` |
| `backtest_daily_screener.py` | `fetch_15m_kline` (L121)，via `writer.query(\"CNStock\",code,\"15m\")` |
| `backtest_realtime_monitor.py` | `fetch_15m_kline` (L123)，via `writer.query(\"CNStock\",code,\"15m\")` |
| `test_dragon_hot_v3.py` | 直连 SQL `kline_15m_{year}` (L179)，大段 15m 信号检测逻辑；已有 `test_dragon_hot_v4.py`（1m 版） |
| `fix/source_sync.py` | `query_batch_existing` 泛型按 tf 查表 |
| `optimizer/runner.py` | 仅 L312 注释提及；L327+ 泛型按 timeframe 查 distinct symbol |

## 前置 bug（已修复 2026-08-31）

`backfill_db.py` 原 L317 `count = 16 if tf == "15m" else 1`：`run_1m()`/`sync_tf("1m")` 每只只拉最新 1 根 1m bar，
与文件头注释「每标的 240 条」矛盾。

核实依据：
1. mootdx 0.11.7 `Quotes.bars()`：`offset=N` = 返回最近 N 条 bar（无起始日期参数），上限 800；
   `_fetch_kline`(L57) 传的就是 `count`。
2. 实测定点 000001：`count=1` → 1 根 (15:00 收盘 bar)；`count=240` → 240 根 (09:31~15:00)；15m `count=16` → 16 根。
3. DB 实测：kline_1m_2026 每标的正 240 根/日 → 来自 `fix/backfill_1m_mootdx.py`（手动翻页工具），而非 run_1m()。

修复：L317 改为 `count = {"1D": 1, "15m": 16, "1m": 240}.get(tf, 1)`，run_1m() 现在拉全市场每标的当日 240 根。

连带修复（同次）：`_count_existing` 由 `COUNT(DISTINCT symbol)` 改为 `COUNT(*)`（bar 总数），
`sync_tf` 跳过判定按 `expected = symbols × 每标当日 bar 数` 折算完整性（>90% 才跳过）。
因为原判定只看「symbol 是否出现过」，若老 bug 已写入 1 根/标的，会误判为已完成 → 240 根覆写永不生效。
`_TF_DAILY_BAR_COUNT = {"1D":1,"15m":16,"1m":240}` 抽为模块常量，拉取数量与完整性校验共用。

并发改造（2026-08-31 已实施）：
- 抓取从单 worker 改为有界线程池 `BACKFILL_FETCH_WORKERS`（默认 **3**，env 可调）；
  每个 worker 线程持独立 mootdx 连接（`mootdx_client.get_thread_client` 线程级单例），单条卡死只影响该 worker。
- **防封 IP 设计**：总请求量不变（全市场 ~5000 只 ÷ 3 线程 顺次拉），仅并发度提高；
  默认 3 保守取值，且线程创建连接时按 `server_rotor` 轮转打散到不同 live 服务器，
  单台服务器只见 1~3 条连接，避免多连接压同一台触发通达信限流。/ 每线程连接复用整个 run。
- 核实 pytdx `TdxHq_API.connect` 会 `sock.settimeout(10)`（来自 Quotes.factory timeout=10），
  socket 层已有 10s 超时，recv 停滞自行抛错，不会永久挂死。
- `sync_tf` 并行 submit 全部抓取，`wait(..., timeout=BACKFILL_SYNC_TIMEOUT)`（默认 1800s）兜底；
  超预算未返回的 worker 按失败计并 `_recreate_executor()` 丢弃僵尸线程。
- 修复后不再有「单 worker 卡死 → 全部分片级联超时」的路径；外层 future.result 超时与
  MAX_CONSECUTIVE_FAIL 提前退出逻辑已删除（并行下无连续语义）。
- 全局共享单例 `get_client()` 保持不变（index/finance/tape 仍用它），backfill 改为线程本地连接。

## 替换设计要点

1. **cn_stock.py 聚合改造**：
   - `_read_db_15m` → `_read_db_1m`（读 kline_1m_YYYY）
   - `_aggregate_from_15m` → 1m 聚合：bar_count = {15m:15, 30m:30, 1h:60, 2h:120, 4h:240}
   - 注意 1m 有 ±1 根/天容差（validate_stock），按时间对齐分桶比按数量分组更稳（规避缺 bar 偏移）
   - `_check_15m_fresh` → 改查 1m 表最后 bar 必须为当日 15:00
   - `need_bars` 余量从 16 改为 240（留 1 天）
2. **backfill_db.py**：删 `run_15m()` 路径（若 15m 表废弃）；1m count 已修（见「前置 bug」）
3. **db_multi.py `_init_market_schema`**：不再建 15m 表，改建 1m 表
4. **scheduler.py**：`_post_market_batch` 中删 `_refresh_backfill_15m()`（保留 run_1m）
5. **各脚本**（intraday.py / backtest_* / test_dragon_hot_v3）统一走新的 1m 读取+聚合接口，或提供公共聚合函数复用
6. **存储量**：1m ≈ 15× 15m（240 条/天/股）

## 未决决策点（待确认）

- [ ] 现有 kline_15m_YYYY 历史数据：保留冻结 / 从 1m 重建后删 / 直接删
- [ ] 读写方是否统一改为按需从 1m 聚合（不再物化 15m）
- [ ] test_dragon_hot_v3.py / backtest_daily_screener.py / backtest_realtime_monitor.py 处理方式（统一改造 / v3 废弃 / 本次不动）
- [ ] source_sync.py 是否保留 `-T 15m` 历史回填能力
- [ ] 历史回填：`fix/backfill_1m_mootdx.py` 已能拉 1m 全历史（TDX 仅存 ~4-5 个月），15m 历史更长，切换需评估覆盖

## 参考

- `test_dragon_hot_v4.py` `fetch_kline_1m`(L137) 已有 1m 直读范式
- `app/utils/db_multi.py` `_table_name` / `ensure_year_table` 泛型支持任意 tf
- `fix/backfill_1m_mootdx.py` `_FETCH_BATCH_SIZE=800` / `_MAX_OFFSET=800*60` 翻页模式
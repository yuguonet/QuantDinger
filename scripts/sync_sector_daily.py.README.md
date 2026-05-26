# sync_sector_daily.py - 板块热度每日统计（行业 + 概念）

## 背景
V1策略(追连板)需要板块热度数据作为过滤维度。实时计算成本高，改为每日收盘后预计算存表。

同时覆盖**行业板块**和**概念板块**，sector_type 字段区分。

## 表结构

```sql
CREATE TABLE sector_daily_stats (
    date        VARCHAR(10)  NOT NULL,   -- 日期 YYYY-MM-DD
    sector_type VARCHAR(10)  NOT NULL,   -- 'industry' 或 'concept'
    sector_name VARCHAR(50)  NOT NULL,   -- 板块名
    stock_count     INT DEFAULT 0,       -- 板块内股票总数
    limit_up_count  INT DEFAULT 0,       -- 涨停数
    limit_down_count INT DEFAULT 0,      -- 跌停数
    advance_count   INT DEFAULT 0,       -- 上涨数
    decline_count   INT DEFAULT 0,       -- 下跌数
    total_volume    DOUBLE PRECISION DEFAULT 0,  -- 总成交量
    avg_return      DOUBLE PRECISION DEFAULT 0,  -- 平均涨幅%
    advance_pct     DOUBLE PRECISION DEFAULT 0,  -- 上涨占比%
    heat_score      DOUBLE PRECISION DEFAULT 0,  -- 综合热度(加权)
    updated_at  TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (date, sector_type, sector_name)
);

CREATE INDEX idx_sds_date ON sector_daily_stats (date);
CREATE INDEX idx_sds_heat ON sector_daily_stats (date, heat_score DESC);
```

## heat_score 计算
```
heat_score = limit_up_count * 3 + advance_pct * 0.5 + avg_return * 0.2
```
- 涨停数权重最高(连板的核心)
- 上涨占比反映板块广度
- 平均涨幅反映强度

## 涨跌停判定规则
| 板块 | 幅度 |
|------|------|
| 主板 (60/00) | 10% |
| 创业板 (300/301) / 科创板 (688/689) | 20% |
| 北交所 (43/82/83/87/88) | 30% |
| ST 股 | 5% |

判定条件：收盘价 ≈ 涨停价 且 最高价 ≈ 涨停价（±0.02 容差）

## 数据来源
- 股票-板块映射: stock_basic_info (industry + concepts，概念逗号分隔)
- K线数据: CNStock_db 的 kline_1D_YYYY 分区表

## 用法
```bash
# 每日收盘后运行（默认当日）
python scripts/sync_sector_daily.py

# 指定日期
python scripts/sync_sector_daily.py --date 2026-05-26

# 回填历史
python scripts/sync_sector_daily.py --backfill 2024-01-01

# 只看不写
python scripts/sync_sector_daily.py --dry-run --date 2026-05-26
```

## V1策略集成
查询当日板块热度, 作为过滤条件:
```python
# 伪代码 — 行业和概念都可以查
heat = db.query("SELECT heat_score FROM sector_daily_stats WHERE date=%s AND sector_type='industry' AND sector_name=%s", d0_date, industry)
if heat < threshold:
    skip  # 板块太冷, 不追

# 也可以查概念热度
heat = db.query("SELECT heat_score FROM sector_daily_stats WHERE date=%s AND sector_type='concept' AND sector_name=%s", d0_date, concept_name)
```

## 文件位置
`scripts/sync_sector_daily.py` (与 sync_industry_concept.py 同级)

-- 龙虎榜 PostgreSQL 表（持久存储，不删除历史数据）
-- 此表由 dragon_tiger.py 在首次使用时自动创建（CREATE TABLE IF NOT EXISTS）

CREATE TABLE IF NOT EXISTS cnd_dragon_tiger_list (
    id SERIAL PRIMARY KEY,
    trade_date VARCHAR(10) NOT NULL,        -- 上榜日期 YYYY-MM-DD
    stock_code VARCHAR(10) NOT NULL,        -- 股票代码
    stock_name VARCHAR(50) DEFAULT '',      -- 股票名称
    reason VARCHAR(200) DEFAULT '',         -- 上榜原因
    buy_amount DOUBLE PRECISION DEFAULT 0,  -- 买入金额 (元)
    sell_amount DOUBLE PRECISION DEFAULT 0, -- 卖出金额 (元)
    net_amount DOUBLE PRECISION DEFAULT 0,  -- 净买入额 (元)
    change_percent DOUBLE PRECISION DEFAULT 0, -- 涨跌幅 %
    close_price DOUBLE PRECISION DEFAULT 0,    -- 收盘价
    turnover_rate DOUBLE PRECISION DEFAULT 0,  -- 换手率 %
    amount DOUBLE PRECISION DEFAULT 0,         -- 成交额 (元)
    buy_seat_count INTEGER DEFAULT 0,      -- 买入席位数
    sell_seat_count INTEGER DEFAULT 0,     -- 卖出席位数
    fetch_time VARCHAR(20) DEFAULT '',     -- 拉取时间
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(trade_date, stock_code, reason)  -- 去重
);

CREATE INDEX IF NOT EXISTS idx_dt_trade_date ON cnd_dragon_tiger_list(trade_date);
CREATE INDEX IF NOT EXISTS idx_dt_stock_code ON cnd_dragon_tiger_list(stock_code);
CREATE INDEX IF NOT EXISTS idx_dt_date_code ON cnd_dragon_tiger_list(trade_date, stock_code);

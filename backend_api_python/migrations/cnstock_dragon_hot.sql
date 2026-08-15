-- 龙虎榜 & 热榜 持久化表 (CNStock_db)

CREATE TABLE IF NOT EXISTS cnd_dragon_tiger_list (
    id              SERIAL PRIMARY KEY,
    trade_date      VARCHAR(10) NOT NULL,
    stock_code      VARCHAR(10) NOT NULL,
    stock_name      VARCHAR(50) DEFAULT '',
    reason          VARCHAR(200) DEFAULT '',
    buy_amount      DOUBLE PRECISION DEFAULT 0,
    sell_amount     DOUBLE PRECISION DEFAULT 0,
    net_amount      DOUBLE PRECISION DEFAULT 0,
    change_percent  DOUBLE PRECISION DEFAULT 0,
    close_price     DOUBLE PRECISION DEFAULT 0,
    turnover_rate   DOUBLE PRECISION DEFAULT 0,
    amount          DOUBLE PRECISION DEFAULT 0,
    buy_seat_count  INTEGER DEFAULT 0,
    sell_seat_count INTEGER DEFAULT 0,
    UNIQUE(trade_date, stock_code, reason)
);

CREATE INDEX IF NOT EXISTS idx_dt_trade_date ON cnd_dragon_tiger_list(trade_date);
CREATE INDEX IF NOT EXISTS idx_dt_stock_code ON cnd_dragon_tiger_list(stock_code);
CREATE INDEX IF NOT EXISTS idx_dt_date_code ON cnd_dragon_tiger_list(trade_date, stock_code);

CREATE TABLE IF NOT EXISTS cnd_hot_rank_list (
    id                  SERIAL PRIMARY KEY,
    trade_date          VARCHAR(10) NOT NULL,
    rank                INTEGER DEFAULT 0,
    stock_code          VARCHAR(10) NOT NULL,
    stock_name          VARCHAR(50) DEFAULT '',
    price               DOUBLE PRECISION DEFAULT 0,
    change_percent      DOUBLE PRECISION DEFAULT 0,
    popularity_score    DOUBLE PRECISION DEFAULT 0,
    rank_change         VARCHAR(20) DEFAULT '',
    UNIQUE(trade_date, stock_code)
);

CREATE INDEX IF NOT EXISTS idx_hr_trade_date ON cnd_hot_rank_list(trade_date);
CREATE INDEX IF NOT EXISTS idx_hr_stock_code ON cnd_hot_rank_list(stock_code);
CREATE INDEX IF NOT EXISTS idx_hr_rank ON cnd_hot_rank_list(trade_date, rank);

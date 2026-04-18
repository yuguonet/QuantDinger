-- eQuant Features Migration
-- Tables needed for: Stock Picker (选股器), Market Dashboard (市场看板), AI Agent (AI智能体)

-- =============================================================================
-- 1. Stock Selection Data (选股器数据表)
-- Stores daily stock screening data from various sources
-- =============================================================================
CREATE TABLE IF NOT EXISTS cnstock_selection (
    id SERIAL PRIMARY KEY,
    date VARCHAR(20),
    code VARCHAR(20),
    name VARCHAR(100),
    secucode VARCHAR(20),
    new_price DECIMAL(10,2),
    change_rate DECIMAL(10,2),
    high_price DECIMAL(10,2),
    low_price DECIMAL(10,2),
    pre_close_price DECIMAL(10,2),
    amplitude DECIMAL(10,2),
    volume BIGINT,
    amount DECIMAL(20,2),
    turnoverrate DECIMAL(10,4),
    volume_ratio DECIMAL(10,2),
    industry VARCHAR(100),
    concept VARCHAR(500),
    pe9 DECIMAL(10,2),
    pbnewmrq DECIMAL(10,2),
    total_mv DECIMAL(20,2),
    circ_mv DECIMAL(20,2),
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cnstock_selection_code ON cnstock_selection(code);
CREATE INDEX IF NOT EXISTS idx_cnstock_selection_date ON cnstock_selection(date);
CREATE INDEX IF NOT EXISTS idx_cnstock_selection_industry ON cnstock_selection(industry);

-- =============================================================================
-- 2. User Saved Strategies (用户保存的选股策略)
-- =============================================================================
CREATE TABLE IF NOT EXISTS qd_user_strategies (
    id SERIAL PRIMARY KEY,
    user_id INTEGER DEFAULT 1 REFERENCES qd_users(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    conditions TEXT,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_user_strategies_user_id ON qd_user_strategies(user_id);

-- =============================================================================
-- Completion
-- =============================================================================
DO $$
BEGIN
    RAISE NOTICE 'eQuant features migration completed successfully!';
END $$;

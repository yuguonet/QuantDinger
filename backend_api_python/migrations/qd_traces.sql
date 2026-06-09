-- QuantDinger Agent 可追责架构 — 数据库迁移
-- 日期: 2026-06-09
-- 适用: domain="finance" 金融领域
-- 旧表 qd_evaluations 保留只读，不迁移数据

-- ═══════════════════════════════════════════════════════════
-- 1. qd_traces — 执行追踪表（替代 qd_evaluations）
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS qd_traces (
    -- 身份
    id              SERIAL PRIMARY KEY,
    parent_id       INTEGER REFERENCES qd_traces(id),
    root_id         INTEGER REFERENCES qd_traces(id),
    layer           VARCHAR(10) NOT NULL,    -- 'chain' / 'skill' / 'tool'
    step_order      INTEGER DEFAULT 0,

    -- 标的
    exec_date       DATE NOT NULL,
    stock_code      VARCHAR(10),
    stock_name      VARCHAR(50),

    -- 内容
    name            VARCHAR(100) NOT NULL,
    score           REAL,
    direction       VARCHAR(20),
    action          VARCHAR(10),
    signal          TEXT,
    confidence      REAL,
    timeframe       VARCHAR(10),
    analysis        TEXT,
    factors         JSONB,

    -- 工具调用记录
    input_params    JSONB,
    output_summary  JSONB,
    tools_called    TEXT[],
    missing_data    TEXT[],

    -- 执行信息
    status          VARCHAR(20) DEFAULT 'ok',
    error           TEXT,
    elapsed_ms      REAL DEFAULT 0,
    data_source     VARCHAR(50),

    -- 回溯验证（盘后写入）
    exit_date       DATE,
    exit_reason     VARCHAR(20),
    pnl_pct         REAL,
    hold_days       INTEGER,
    correct         BOOLEAN,
    calibration     REAL DEFAULT 1.0,

    -- 元数据
    session_id      VARCHAR(100),
    user_query      TEXT,
    model           VARCHAR(100),
    total_tokens    INTEGER,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_traces_root ON qd_traces(root_id);
CREATE INDEX IF NOT EXISTS idx_traces_parent ON qd_traces(parent_id);
CREATE INDEX IF NOT EXISTS idx_traces_layer ON qd_traces(layer);
CREATE INDEX IF NOT EXISTS idx_traces_stock ON qd_traces(stock_code, exec_date);
CREATE INDEX IF NOT EXISTS idx_traces_skill ON qd_traces(name, exec_date) WHERE layer = 'skill';
CREATE INDEX IF NOT EXISTS idx_traces_pending ON qd_traces(id) WHERE layer = 'chain' AND exit_date IS NULL;

-- ═══════════════════════════════════════════════════════════
-- 2. qd_skill_weights — Skill 权重表
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS qd_skill_weights (
    skill_name      VARCHAR(100) PRIMARY KEY,
    weight          REAL DEFAULT 1.0,
    win_rate        REAL,
    avg_pnl_pct     REAL,
    avg_hold_days   REAL,
    return_per_day  REAL,
    profit_loss_ratio REAL,
    sample_count    INTEGER DEFAULT 0,
    decay_half_life INTEGER DEFAULT 30,
    last_updated    TIMESTAMPTZ
);

INSERT INTO qd_skill_weights (skill_name, weight) VALUES
('technical_agent', 1.2),
('momentum_tracker', 1.1),
('indicator_agent', 1.1),
('backtest_agent', 1.0),
('screening_agent', 1.0),
('bull_researcher', 1.0),
('bear_researcher', 1.0),
('trading_agent', 1.0),
('market_data_agent', 0.9),
('concept_tracker', 0.9),
('lockup_watcher', 0.8),
('intelligence_agent', 0.8),
('data_engineer', 0.8),
('policy_analyst', 0.7),
('hot_money_tracker', 0.7)
ON CONFLICT (skill_name) DO NOTHING;

-- ═══════════════════════════════════════════════════════════
-- 3. qd_factor_weights — 因子权重表（重建）
-- ═══════════════════════════════════════════════════════════

DROP TABLE IF EXISTS qd_factor_weights CASCADE;

CREATE TABLE qd_factor_weights (
    id              SERIAL PRIMARY KEY,
    skill_name      VARCHAR(100) NOT NULL,
    factor_name     VARCHAR(100) NOT NULL,
    weight          REAL DEFAULT 1.0,
    win_rate        REAL,
    avg_pnl_pct     REAL,
    avg_hold_days   REAL,
    return_per_day  REAL,
    sample_count    INTEGER DEFAULT 0,
    decay_half_life INTEGER DEFAULT 30,
    last_updated    TIMESTAMPTZ,
    UNIQUE(skill_name, factor_name)
);

INSERT INTO qd_factor_weights (skill_name, factor_name, weight, decay_half_life) VALUES
('technical_agent', '趋势', 1.0, 60),
('technical_agent', '量价', 1.0, 60),
('technical_agent', '指标', 1.0, 30),
('technical_agent', '形态', 1.0, 30),
('technical_agent', '筹码', 1.0, 60),
('momentum_tracker', '趋势强度', 1.0, 30),
('momentum_tracker', '动量指标', 1.0, 30),
('momentum_tracker', '突破检测', 1.0, 14),
('indicator_agent', 'MACD', 1.0, 30),
('indicator_agent', 'KDJ', 1.0, 30),
('indicator_agent', 'RSI', 1.0, 30),
('indicator_agent', 'BOLL', 1.0, 30),
('intelligence_agent', '新闻情绪', 0.8, 7),
('intelligence_agent', '事件催化', 0.8, 7),
('policy_analyst', '产业政策', 0.7, 7),
('policy_analyst', '货币政策', 0.7, 14),
('hot_money_tracker', '龙虎榜', 0.7, 7),
('hot_money_tracker', '游资动向', 0.7, 7);

-- QuantDinger Agent 可追责架构 — 统一迁移（重建版）
-- 日期: 2026-06-17
-- 适用: nanobot 内核 + 金融领域
-- 旧表 qd_evaluations 保留只读，不迁移数据
--
-- 三张表:
--   qd_traces         — 执行追踪树（Agent 每次执行 = 一棵树）
--   qd_skill_weights  — Skill 权重（按单位时间收益率迭代）
--   qd_factor_weights — 因子权重（按因子维度聚合，带时间衰减）

-- ═══════════════════════════════════════════════════════════
-- 1. qd_traces — 执行追踪表
-- ═══════════════════════════════════════════════════════════

DROP TABLE IF EXISTS qd_traces CASCADE;
CREATE TABLE qd_traces (
    id              SERIAL PRIMARY KEY,
    parent_id       INTEGER REFERENCES qd_traces(id),
    root_id         INTEGER REFERENCES qd_traces(id),
    layer           VARCHAR(10) NOT NULL,
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

    -- 工具调用
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

CREATE INDEX idx_traces_root    ON qd_traces(root_id);
CREATE INDEX idx_traces_parent  ON qd_traces(parent_id);
CREATE INDEX idx_traces_layer   ON qd_traces(layer);
CREATE INDEX idx_traces_stock   ON qd_traces(stock_code, exec_date);
CREATE INDEX idx_traces_skill   ON qd_traces(name, exec_date) WHERE layer = 'skill';
CREATE INDEX idx_traces_pending ON qd_traces(id) WHERE layer = 'chain' AND exit_date IS NULL;


-- ═══════════════════════════════════════════════════════════
-- 2. qd_skill_weights — Skill 权重表
-- 核心指标: 单位时间期望收益率（return_per_day），不是胜率
-- ═══════════════════════════════════════════════════════════

DROP TABLE IF EXISTS qd_skill_weights CASCADE;
CREATE TABLE qd_skill_weights (
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

-- 出厂权重
INSERT INTO qd_skill_weights (skill_name, weight) VALUES
('technical_agent', 1.2),
('indicator_agent', 1.1),
('intelligence_agent', 0.8),
('hot_money_tracker', 0.7),
('lockup_watcher', 0.8),
('market_data_agent', 0.9),
('screening_agent', 1.0),
('backtest_agent', 1.0),
('bull_researcher', 1.0),
('bear_researcher', 1.0),
('trading_agent', 1.0),
('data_agent', 0.8);


-- ═══════════════════════════════════════════════════════════
-- 3. qd_factor_weights — 因子权重表
-- 按因子维度聚合，带时间衰减
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
('indicator_agent', 'MACD', 1.0, 30),
('indicator_agent', 'KDJ', 1.0, 30),
('indicator_agent', 'RSI', 1.0, 30),
('indicator_agent', 'BOLL', 1.0, 30),
('intelligence_agent', '新闻情绪', 0.8, 7),
('intelligence_agent', '事件催化', 0.8, 7),
('hot_money_tracker', '龙虎榜', 0.7, 7),
('hot_money_tracker', '游资动向', 0.7, 7);

-- QuantDinger Agent v3 — 完整迁移（重建版）
-- 日期: 2026-06-18
-- 适用: 可追责架构 + skill_runner + tool_chains.json 链路路由
--
-- 三张表:
--   qd_traces         — 执行追踪树（Agent 每次执行 = 一棵树）
--   qd_skill_weights  — Skill 权重（按单位时间收益率迭代）
--   qd_factor_weights — 因子权重（按因子维度聚合，带时间衰减）
--
-- 使用: 直接执行即可，不需要先执行 agent_v2.sql 或 qd_traces.sql

-- ═══════════════════════════════════════════════════════════
-- 1. qd_traces — 执行追踪表
-- ═══════════════════════════════════════════════════════════

DROP TABLE IF EXISTS qd_traces CASCADE;
CREATE TABLE qd_traces (
    id              SERIAL PRIMARY KEY,
    parent_id       INTEGER REFERENCES qd_traces(id) ON DELETE CASCADE,
    root_id         INTEGER REFERENCES qd_traces(id) ON DELETE CASCADE,
    layer           VARCHAR(10) NOT NULL,           -- 'chain' / 'skill' / 'tool'
    step_order      INTEGER DEFAULT 0,

    -- 标的
    exec_date       DATE NOT NULL,
    stock_code      VARCHAR(10),
    stock_name      VARCHAR(50),

    -- 内容
    name            VARCHAR(100) NOT NULL,           -- chain_id / skill_name / tool_name
    score           REAL,                            -- 0-100
    direction       VARCHAR(20),                     -- bullish / bearish / neutral
    action          VARCHAR(10),                     -- buy / sell / hold / skip
    signal          TEXT,                            -- 一句话信号
    confidence      REAL,                            -- 0.0-1.0
    timeframe       VARCHAR(10),                     -- T+1 / T+3 / T+5 / 1W / 1M
    analysis        TEXT,                            -- 分析文字
    factors         JSONB,                           -- [{name, value, score, weight, status}]

    -- 工具调用
    input_params    JSONB,                           -- 入口参数
    output_summary  JSONB,                           -- 输出数据（schema 中叫 output_data）
    tools_called    TEXT[],                          -- 调用过的工具列表
    missing_data    TEXT[],                          -- 缺失的数据

    -- 执行信息
    status          VARCHAR(20) DEFAULT 'ok',        -- ok / missing / failed / skipped / veto
    error           TEXT,
    elapsed_ms      REAL DEFAULT 0,
    data_source     VARCHAR(50),

    -- 回溯验证（异步回测闭环写入）
    exit_date       DATE,                            -- 实际退出日期
    exit_reason     VARCHAR(20),                     -- 退出原因
    pnl_pct         REAL,                            -- 实际盈亏百分比
    hold_days       INTEGER,                         -- 持有天数
    correct         BOOLEAN,                         -- 方向预测是否正确
    calibration     REAL DEFAULT 1.0,                -- 校准因子

    -- 用户反馈（惩罚闭环写入）
    human_reviewed  BOOLEAN DEFAULT FALSE,
    human_verdict   VARCHAR(50),                     -- negative_feedback 等

    -- 元数据
    session_id      VARCHAR(100),
    user_query      TEXT,
    model           VARCHAR(100),
    total_tokens    INTEGER,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- 索引
CREATE INDEX idx_traces_root    ON qd_traces(root_id);
CREATE INDEX idx_traces_parent  ON qd_traces(parent_id);
CREATE INDEX idx_traces_layer   ON qd_traces(layer);
CREATE INDEX idx_traces_stock   ON qd_traces(stock_code, exec_date);
CREATE INDEX idx_traces_skill   ON qd_traces(name, exec_date) WHERE layer = 'skill';
CREATE INDEX idx_traces_pending ON qd_traces(id) WHERE layer = 'chain' AND exit_date IS NULL;
CREATE INDEX idx_traces_penalty ON qd_traces(stock_code) WHERE human_verdict = 'negative_feedback';


-- ═══════════════════════════════════════════════════════════
-- 2. qd_skill_weights — Skill 权重表
-- 核心指标: 单位时间收益率（return_per_day），不是胜率
-- ═══════════════════════════════════════════════════════════

DROP TABLE IF EXISTS qd_skill_weights CASCADE;
CREATE TABLE qd_skill_weights (
    skill_name        VARCHAR(100) PRIMARY KEY,
    weight            REAL DEFAULT 1.0,
    win_rate          REAL,
    avg_pnl_pct       REAL,
    avg_hold_days     REAL,
    return_per_day    REAL,
    profit_loss_ratio REAL,
    sample_count      INTEGER DEFAULT 0,
    decay_half_life   INTEGER DEFAULT 30,
    last_updated      TIMESTAMPTZ
);

-- 出厂权重（与 semantics/skills/*.md 的 default_weight 一致）
INSERT INTO qd_skill_weights (skill_name, weight) VALUES
('technical_agent',    1.2),
('indicator_agent',    1.1),
('intelligence_agent', 0.8),
('hot_money_tracker',  0.7),

('market_data_agent',  0.9),
('screening_agent',    1.0),
('backtest_agent',     1.0),
('researcher',        1.0),
('trading_agent',      1.0),
('data_agent',         0.8),
('market_screener',    1.0),
('bb_screener',        1.0);


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

-- 出厂因子权重（与 chain/evaluator.py 的半衰期一致）
INSERT INTO qd_factor_weights (skill_name, factor_name, weight, decay_half_life) VALUES
('technical_agent',    '趋势',     1.0, 60),
('technical_agent',    '量价',     1.0, 60),
('technical_agent',    '指标',     1.0, 30),
('technical_agent',    '形态',     1.0, 30),
('technical_agent',    '筹码',     1.0, 60),
('indicator_agent',    'MACD',     1.0, 30),
('indicator_agent',    'KDJ',      1.0, 30),
('indicator_agent',    'RSI',      1.0, 30),
('indicator_agent',    'BOLL',     1.0, 30),
('intelligence_agent', '新闻情绪', 0.8, 7),
('intelligence_agent', '事件催化', 0.8, 7),
('hot_money_tracker',  '龙虎榜',   0.7, 7),
('hot_money_tracker',  '游资动向', 0.7, 7);

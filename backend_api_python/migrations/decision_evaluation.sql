-- Decision Evaluation System — 决策评估系统
-- 重新设计，不兼容旧版 chain_evaluation.sql

-- =============================================================================
-- 1. 决策执行记录
-- =============================================================================
CREATE TABLE IF NOT EXISTS qd_agent_decisions (
    id              SERIAL PRIMARY KEY,
    exec_date       DATE NOT NULL,
    stock_code      VARCHAR(20) NOT NULL,
    stock_name      VARCHAR(100) DEFAULT '',
    chain_id        VARCHAR(50) NOT NULL,
    action          VARCHAR(10) NOT NULL DEFAULT 'hold',  -- buy/sell/hold/skip
    score           FLOAT DEFAULT 0,
    coverage        FLOAT DEFAULT 0,       -- 覆盖度 0-1
    confidence      VARCHAR(10) DEFAULT 'low',  -- high/medium/low/reject
    decision_card   JSONB NOT NULL DEFAULT '{}',  -- 完整决策卡
    user_id         INTEGER DEFAULT 1,
    created_at      TIMESTAMP DEFAULT NOW(),
    UNIQUE(exec_date, stock_code, chain_id)
);

CREATE INDEX IF NOT EXISTS idx_decisions_date ON qd_agent_decisions(exec_date);
CREATE INDEX IF NOT EXISTS idx_decisions_stock ON qd_agent_decisions(stock_code);
CREATE INDEX IF NOT EXISTS idx_decisions_chain ON qd_agent_decisions(chain_id);
CREATE INDEX IF NOT EXISTS idx_decisions_action ON qd_agent_decisions(action);

-- =============================================================================
-- 2. 步骤详情
-- =============================================================================
CREATE TABLE IF NOT EXISTS qd_agent_decision_steps (
    id              SERIAL PRIMARY KEY,
    decision_id     INTEGER NOT NULL REFERENCES qd_agent_decisions(id) ON DELETE CASCADE,
    step_name       VARCHAR(50) NOT NULL,
    step_order      INTEGER NOT NULL,
    agent_name      VARCHAR(50) DEFAULT '',
    status          VARCHAR(15) NOT NULL DEFAULT 'ok',  -- ok/missing/failed/skipped/veto
    direction       VARCHAR(10) DEFAULT 'neutral',       -- bullish/bearish/neutral
    confidence      FLOAT DEFAULT 0,
    score           FLOAT DEFAULT NULL,      -- 0-100, NULL=缺失
    signal          TEXT DEFAULT '',
    factors         JSONB DEFAULT '[]',      -- [{name, value, score, status}]
    tools_called    JSONB DEFAULT '[]',      -- ["tool_name", ...]
    raw_output      TEXT DEFAULT '',
    elapsed_ms      FLOAT DEFAULT 0,
    error           TEXT DEFAULT '',
    -- 子项级验证：每个步骤独立跟实际方向对比
    actual_direction VARCHAR(10) DEFAULT NULL,  -- 实际方向 (bullish/bearish/neutral)
    step_correct    BOOLEAN DEFAULT NULL,        -- 该步骤方向是否正确
    calibration_factor FLOAT DEFAULT NULL,       -- 校准因子: 正确+自信=大奖, 正确+不自信=小奖, 错误+自信=重罚
    UNIQUE(decision_id, step_name)
);

CREATE INDEX IF NOT EXISTS idx_decision_steps_id ON qd_agent_decision_steps(decision_id);

-- =============================================================================
-- 3. 事后验证（T+N 实际涨跌）
-- =============================================================================
CREATE TABLE IF NOT EXISTS qd_agent_decision_results (
    id                  SERIAL PRIMARY KEY,
    decision_id         INTEGER NOT NULL REFERENCES qd_agent_decisions(id) ON DELETE CASCADE,
    actual_return_1d    FLOAT,
    actual_return_3d    FLOAT,
    actual_return_5d    FLOAT,
    actual_direction_1d VARCHAR(10),
    actual_direction_3d VARCHAR(10),
    actual_direction_5d VARCHAR(10),
    correct_1d          BOOLEAN,
    correct_3d          BOOLEAN,
    correct_5d          BOOLEAN,
    evaluated_at        TIMESTAMP DEFAULT NOW(),
    UNIQUE(decision_id)
);

CREATE INDEX IF NOT EXISTS idx_decision_results_id ON qd_agent_decision_results(decision_id);

-- =============================================================================
-- 4. 因子权重（可迭代）
-- =============================================================================
CREATE TABLE IF NOT EXISTS qd_agent_factor_weights (
    id              SERIAL PRIMARY KEY,
    chain_id        VARCHAR(50) NOT NULL,
    factor_name     VARCHAR(50) NOT NULL,
    weight          FLOAT DEFAULT 1.0,
    accuracy_1d     FLOAT DEFAULT 0,
    accuracy_3d     FLOAT DEFAULT 0,
    accuracy_5d     FLOAT DEFAULT 0,
    sample_count    INTEGER DEFAULT 0,
    decay_half_life INTEGER DEFAULT 30,   -- 因子级衰减半衰期（天），由 evaluator 自动推断
    last_updated    TIMESTAMP DEFAULT NOW(),
    UNIQUE(chain_id, factor_name)
);

CREATE INDEX IF NOT EXISTS idx_factor_weights_chain ON qd_agent_factor_weights(chain_id);

-- =============================================================================
-- 5. 工具评估
-- =============================================================================
CREATE TABLE IF NOT EXISTS qd_agent_tool_eval (
    id              SERIAL PRIMARY KEY,
    tool_name       VARCHAR(50) NOT NULL,
    chain_id        VARCHAR(50) NOT NULL,
    calls           INTEGER DEFAULT 0,
    successes       INTEGER DEFAULT 0,
    useful_count    INTEGER DEFAULT 0,   -- 调用该工具的步骤最终正确的次数
    avg_latency_ms  FLOAT DEFAULT 0,
    last_updated    TIMESTAMP DEFAULT NOW(),
    UNIQUE(tool_name, chain_id)
);

CREATE INDEX IF NOT EXISTS idx_tool_eval_chain ON qd_agent_tool_eval(chain_id);

-- =============================================================================
-- 迁移：已存在的表添加新字段
-- =============================================================================

-- qd_agent_factor_weights 添加 decay_half_life 列（P1-1: 因子级独立衰减半衰期）
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'qd_agent_factor_weights' AND column_name = 'decay_half_life'
    ) THEN
        ALTER TABLE qd_agent_factor_weights ADD COLUMN decay_half_life INTEGER DEFAULT 30;
        RAISE NOTICE 'Added decay_half_life column to qd_agent_factor_weights';
    END IF;
END $$;

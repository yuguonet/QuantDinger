-- Hierarchical Prediction Tracker — 决策→Chain→Skill→Tool 四层归因
-- 核心：记录每层预测，T+N 验证后从上到下归因，驱动权重迭代

-- =============================================================================
-- 1. 组件预测记录（四层通用表）
-- =============================================================================
CREATE TABLE IF NOT EXISTS qd_component_predictions (
    id              SERIAL PRIMARY KEY,
    exec_date       DATE NOT NULL,
    stock_code      VARCHAR(20) NOT NULL,
    stock_name      VARCHAR(100) DEFAULT '',

    -- 层级
    level           VARCHAR(10) NOT NULL,        -- decision / chain / skill / tool
    parent_id       INTEGER REFERENCES qd_component_predictions(id) ON DELETE CASCADE,
    component_name  VARCHAR(50) NOT NULL,        -- 组件名

    -- 预测
    direction       VARCHAR(10) NOT NULL,        -- bullish / bearish / neutral
    score           FLOAT,                       -- 0-100（tool 级可为 NULL）
    confidence      FLOAT DEFAULT 0,             -- 0-1

    -- 详情（skill 级存 factors，tool 级存 success/duration）
    detail          JSONB DEFAULT '{}',

    -- 关联
    chain_id        VARCHAR(50) DEFAULT '',
    session_id      VARCHAR(100) DEFAULT '',
    user_id         INTEGER DEFAULT 1,

    -- T+N 验证
    actual_return_1d FLOAT,
    actual_return_3d FLOAT,
    actual_return_5d FLOAT,
    correct_1d      BOOLEAN,
    correct_3d      BOOLEAN,
    correct_5d      BOOLEAN,
    evaluated_at    TIMESTAMP,

    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_comp_pred_date ON qd_component_predictions(exec_date);
CREATE INDEX IF NOT EXISTS idx_comp_pred_stock ON qd_component_predictions(stock_code);
CREATE INDEX IF NOT EXISTS idx_comp_pred_level ON qd_component_predictions(level);
CREATE INDEX IF NOT EXISTS idx_comp_pred_parent ON qd_component_predictions(parent_id);
CREATE INDEX IF NOT EXISTS idx_comp_pred_pending ON qd_component_predictions(exec_date, level)
    WHERE actual_return_1d IS NULL AND level = 'decision';

-- =============================================================================
-- 2. 组件权重（四层各自的权重）
-- =============================================================================
CREATE TABLE IF NOT EXISTS qd_component_weights (
    id              SERIAL PRIMARY KEY,
    level           VARCHAR(10) NOT NULL,        -- decision / chain / skill / tool
    component_name  VARCHAR(50) NOT NULL,
    weight          FLOAT DEFAULT 1.0,
    accuracy_3d     FLOAT DEFAULT 0,
    avg_confidence  FLOAT DEFAULT 0,
    avg_score       FLOAT DEFAULT 50,
    sample_count    INTEGER DEFAULT 0,
    last_updated    TIMESTAMP DEFAULT NOW(),
    UNIQUE(level, component_name)
);

CREATE INDEX IF NOT EXISTS idx_comp_weight_level ON qd_component_weights(level);

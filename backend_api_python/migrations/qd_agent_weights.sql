-- qd_agent_weights — 统一权重表（skill + factor）
-- 替代原 qd_skill_weights + qd_factor_weights

CREATE TABLE IF NOT EXISTS qd_agent_weights (
    layer           VARCHAR(10)   NOT NULL,  -- 'skill' / 'factor'
    name            VARCHAR(100)  NOT NULL,  -- skill_name 或 factor_name
    skill_name      VARCHAR(100),           -- factor 所属 skill（skill 层为 NULL）
    weight          FLOAT         NOT NULL DEFAULT 1.0,
    win_rate        FLOAT,
    sample_count    INT           NOT NULL DEFAULT 0,
    -- skill 专用
    avg_pnl_pct     FLOAT,
    avg_hold_days   FLOAT,
    return_per_day  FLOAT,
    -- factor 专用
    decay_half_life FLOAT,
    -- 通用
    last_updated    TIMESTAMP     NOT NULL DEFAULT NOW(),
    PRIMARY KEY (layer, name, skill_name)
);

CREATE INDEX IF NOT EXISTS idx_agent_weights_layer ON qd_agent_weights(layer);

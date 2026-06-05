-- Chain Evaluation System - PostgreSQL Schema
-- 链路执行记录 + 步骤详情 + 评估打分

-- =============================================================================
-- 1. 链路执行记录（按 日期+股票+链路 去重）
-- =============================================================================
CREATE TABLE IF NOT EXISTS qd_chain_executions (
    id              SERIAL PRIMARY KEY,
    exec_date       DATE NOT NULL,
    stock_code      VARCHAR(20) NOT NULL,
    stock_name      VARCHAR(100) DEFAULT '',
    chain_id        VARCHAR(50) NOT NULL,
    user_id         INTEGER DEFAULT 1,
    final_direction VARCHAR(10) DEFAULT 'neutral',  -- bullish/bearish/neutral
    final_confidence FLOAT DEFAULT 0,
    summary         TEXT DEFAULT '',
    evaluated       BOOLEAN DEFAULT FALSE,
    eval_timestamp  TIMESTAMP,
    created_at      TIMESTAMP DEFAULT NOW(),
    updated_at      TIMESTAMP DEFAULT NOW(),
    UNIQUE(exec_date, stock_code, chain_id)
);

CREATE INDEX IF NOT EXISTS idx_chain_exec_date ON qd_chain_executions(exec_date);
CREATE INDEX IF NOT EXISTS idx_chain_exec_stock ON qd_chain_executions(stock_code);
CREATE INDEX IF NOT EXISTS idx_chain_exec_chain ON qd_chain_executions(chain_id);
CREATE INDEX IF NOT EXISTS idx_chain_exec_evaluated ON qd_chain_executions(evaluated);

-- =============================================================================
-- 2. 各步骤详情（每个步骤一行）
-- =============================================================================
CREATE TABLE IF NOT EXISTS qd_chain_steps (
    id              SERIAL PRIMARY KEY,
    execution_id    INTEGER NOT NULL REFERENCES qd_chain_executions(id) ON DELETE CASCADE,
    step_name       VARCHAR(50) NOT NULL,
    step_order      INTEGER NOT NULL,
    agent_name      VARCHAR(50) DEFAULT '',
    conclusion      TEXT DEFAULT '',
    direction       VARCHAR(10) DEFAULT 'neutral',
    confidence      FLOAT DEFAULT 0,
    tools_called    TEXT DEFAULT '',       -- JSON array: ["run_backtest", "get_kline"]
    tools_detail    TEXT DEFAULT '',       -- JSON: [{"name":"run_backtest","ok":true,"ms":1200},...]
    created_at      TIMESTAMP DEFAULT NOW(),
    UNIQUE(execution_id, step_name)
);

CREATE INDEX IF NOT EXISTS idx_chain_steps_exec ON qd_chain_steps(execution_id);

-- =============================================================================
-- 3. 步骤评分（评估后生成）
-- =============================================================================
CREATE TABLE IF NOT EXISTS qd_chain_step_scores (
    id              SERIAL PRIMARY KEY,
    step_id         INTEGER NOT NULL REFERENCES qd_chain_steps(id) ON DELETE CASCADE,
    execution_id    INTEGER NOT NULL REFERENCES qd_chain_executions(id) ON DELETE CASCADE,
    actual_dir_1d   VARCHAR(10) DEFAULT '',
    actual_dir_3d   VARCHAR(10) DEFAULT '',
    actual_dir_5d   VARCHAR(10) DEFAULT '',
    actual_return_1d FLOAT DEFAULT 0,
    actual_return_3d FLOAT DEFAULT 0,
    actual_return_5d FLOAT DEFAULT 0,
    correct_1d      BOOLEAN,
    correct_3d      BOOLEAN,
    correct_5d      BOOLEAN,
    score           FLOAT DEFAULT 0,
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_step_scores_exec ON qd_chain_step_scores(execution_id);
CREATE INDEX IF NOT EXISTS idx_step_scores_step ON qd_chain_step_scores(step_id);

-- =============================================================================
-- 4. 链路评估汇总（每次评估任务的聚合结果）
-- =============================================================================
CREATE TABLE IF NOT EXISTS qd_chain_eval_summary (
    id              SERIAL PRIMARY KEY,
    eval_date       DATE NOT NULL,
    chain_id        VARCHAR(50) NOT NULL,
    total_executions INTEGER DEFAULT 0,
    evaluated_count INTEGER DEFAULT 0,
    overall_accuracy_1d FLOAT DEFAULT 0,
    overall_accuracy_3d FLOAT DEFAULT 0,
    overall_accuracy_5d FLOAT DEFAULT 0,
    step_accuracies TEXT DEFAULT '',       -- JSON: {"screening": 0.6, "intelligence": 0.55}
    skill_accuracies TEXT DEFAULT '',      -- JSON: {"technical_agent": 0.7, "screening_agent": 0.5}
    tool_stats      TEXT DEFAULT '',       -- JSON: {"run_backtest":{"calls":10,"ok":9,"useful":7}}
    created_at      TIMESTAMP DEFAULT NOW(),
    UNIQUE(eval_date, chain_id)
);

CREATE INDEX IF NOT EXISTS idx_eval_summary_date ON qd_chain_eval_summary(eval_date);

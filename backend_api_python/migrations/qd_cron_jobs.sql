-- qd_cron_jobs — Agent 定时任务表
-- 由 Agent 通过 create_cron_job 工具创建，cron_worker 后台扫描执行。

CREATE TABLE IF NOT EXISTS qd_cron_jobs (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(128) NOT NULL,           -- 任务名称（Agent 可读）
    cron_expr       VARCHAR(64) NOT NULL,            -- 5 段式 cron: 分 时 日 月 周
    mode            VARCHAR(16) NOT NULL DEFAULT 'prompt',  -- 'prompt' | 'function'
    prompt          TEXT,                            -- mode=prompt 时的 Agent 消息
    function_path   VARCHAR(256),                    -- mode=function 时的 Python 函数路径
    description     TEXT,                            -- 任务描述
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    last_run_at     TIMESTAMPTZ,
    last_success_at TIMESTAMPTZ,
    last_error      TEXT,
    error_count     INTEGER NOT NULL DEFAULT 0,
    total_runs      INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cron_jobs_enabled ON qd_cron_jobs(enabled) WHERE enabled = TRUE;

COMMENT ON TABLE qd_cron_jobs IS 'Agent 定时任务';
COMMENT ON COLUMN qd_cron_jobs.cron_expr IS '5段式: 分 时 日 月 周, 如 0 18 * * 1-5';
COMMENT ON COLUMN qd_cron_jobs.mode IS 'prompt=调agent.chat, function=直接调Python函数';
COMMENT ON COLUMN qd_cron_jobs.function_path IS '点分路径: app.agent.chain.evaluator.auto_evaluate';

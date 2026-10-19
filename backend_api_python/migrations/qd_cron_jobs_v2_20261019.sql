-- qd_agent_cron_jobs v2 扩展 + qd_agent_cron_runs 运行历史表
-- cron 重实现（2026-10-19，方案见《cron_重实现方案_20261019.md》附录 A/B/C）
--
-- 迁移双路（附录 B1-1：仓库无增量 migration runner）：
--   ① 本文件走 docker 首启（docker-entrypoint-initdb.d，仅新库执行一次）
--   ② 存量库由 app/agent/cron/cron_worker._ensure_cron_schema() 启动幂等 ensure
--     （与本文件 DDL 同源，测试钉 IF NOT EXISTS）
-- 全部 DDL 幂等，可重复执行。

-- ── jobs 表新增列 ────────────────────────────────────────────
ALTER TABLE qd_agent_cron_jobs ADD COLUMN IF NOT EXISTS session_id          VARCHAR(128);  -- 覆盖默认 cron:<id> 会话
ALTER TABLE qd_agent_cron_jobs ADD COLUMN IF NOT EXISTS misfire_grace_time  INTEGER;       -- 秒；NULL=用全局默认(QD_CRON_MISFIRE_DEFAULT, 300)
ALTER TABLE qd_agent_cron_jobs ADD COLUMN IF NOT EXISTS trading_days_only   BOOLEAN NOT NULL DEFAULT FALSE;  -- 仅交易日执行(附录A1-4)
ALTER TABLE qd_agent_cron_jobs ADD COLUMN IF NOT EXISTS timeout_seconds     INTEGER NOT NULL DEFAULT 300;    -- 执行超时(附录A2-8)
ALTER TABLE qd_agent_cron_jobs ADD COLUMN IF NOT EXISTS locked_until        TIMESTAMPTZ;   -- claim TTL(附录A1-1/A7-1)
ALTER TABLE qd_agent_cron_jobs ADD COLUMN IF NOT EXISTS run_token           VARCHAR(64);   -- claim 归属令牌
ALTER TABLE qd_agent_cron_jobs ADD COLUMN IF NOT EXISTS next_run            TIMESTAMPTZ;   -- 仅展示，非调度真源(附录A2-6)

COMMENT ON COLUMN qd_agent_cron_jobs.session_id IS '覆盖默认会话 cron:<id>；NULL=默认';
COMMENT ON COLUMN qd_agent_cron_jobs.misfire_grace_time IS '错过触发的补跑宽限秒数；NULL=全局默认';
COMMENT ON COLUMN qd_agent_cron_jobs.trading_days_only IS 'TRUE=仅交易日执行，日历不可用时 fail-open 照跑(附录B2-9)';
COMMENT ON COLUMN qd_agent_cron_jobs.timeout_seconds IS '执行超时秒数；超时记 status=timeout，锁由执行线程自然结束时释放(附录B2-7)';
COMMENT ON COLUMN qd_agent_cron_jobs.locked_until IS 'claim TTL：执行权持有到期时刻；NULL=空闲';
COMMENT ON COLUMN qd_agent_cron_jobs.run_token IS 'claim 归属令牌，释放锁时校验';
COMMENT ON COLUMN qd_agent_cron_jobs.next_run IS '下次触发时刻（展示用，由表达式推导回写，非调度真源）';

-- ── 运行历史表（附录 A2-5：cron 层的证据链）──────────────────
CREATE TABLE IF NOT EXISTS qd_agent_cron_runs (
    id                SERIAL PRIMARY KEY,
    job_id            INTEGER,                         -- 不加 FK：任务可被删（one_shot）
    job_name          VARCHAR(128),
    trigger_source    VARCHAR(16) NOT NULL DEFAULT 'cron',  -- cron | manual | catchup
    fire_time         TIMESTAMPTZ,                     -- 计划触发时刻
    started_at        TIMESTAMPTZ,
    finished_at       TIMESTAMPTZ,
    status            VARCHAR(24) NOT NULL,            -- ok|error|timeout|skipped_lock|skipped_nontrading|missed
    is_misfire        BOOLEAN NOT NULL DEFAULT FALSE,  -- 补跑标记（附录A1-3）
    queue_wait_seconds REAL,                           -- 并发预算排队时长（附录C1-15）
    duration_seconds  REAL,
    output_preview    TEXT,
    error             TEXT
);

CREATE INDEX IF NOT EXISTS idx_cron_runs_job    ON qd_agent_cron_runs(job_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_cron_runs_started ON qd_agent_cron_runs(started_at DESC);

COMMENT ON TABLE qd_agent_cron_runs IS 'Agent 定时任务运行历史（含 skipped/missed，附录A2-5）';
COMMENT ON COLUMN qd_agent_cron_runs.status IS 'ok|error|timeout|skipped_lock|skipped_nontrading|missed';
COMMENT ON COLUMN qd_agent_cron_runs.is_misfire IS 'TRUE=错过触发后宽限期内补跑';

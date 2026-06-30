-- qd_agent_path_cache — 编排路径缓存（增量聚合）
-- 按 (domain, verb, noun, tool_signature) 归一化，
-- 只存累加器，派生值查询时实时计算。

CREATE TABLE IF NOT EXISTS qd_agent_path_cache (
    domain          VARCHAR(50)   NOT NULL DEFAULT '',
    verb            VARCHAR(50)   NOT NULL,
    noun            VARCHAR(50)   NOT NULL,
    tool_signature  VARCHAR(64)   NOT NULL,   -- md5(tools_called)
    tools           TEXT[]        NOT NULL,    -- 工具列表
    -- 累加器（每次验证 +1）
    total_runs              INT     NOT NULL DEFAULT 0,
    verified_runs           INT     NOT NULL DEFAULT 0,
    total_verified_correct  INT     NOT NULL DEFAULT 0,
    total_verified_pnl      FLOAT   NOT NULL DEFAULT 0.0,
    total_hold_days         INT     NOT NULL DEFAULT 0,
    -- 元数据
    last_root_id    INT,
    last_success_at TIMESTAMP,
    updated_at      TIMESTAMP     NOT NULL DEFAULT NOW(),
    PRIMARY KEY (domain, verb, noun, tool_signature)
);

CREATE INDEX IF NOT EXISTS idx_path_cache_lookup
    ON qd_agent_path_cache(domain, verb, noun);

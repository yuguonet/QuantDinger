-- qd_agent_cases — Agent 案例库（T+N 已定论历史结论）
-- 来源：app/agent/utils/case_memory.py（运行时自建表，补正式 migration）
-- 设计：
--   - 根因裁决闭环（决策树 root_id + outcome T+N 验证结果）
--   - 向量优先、词面兜底的案例召回（embedding JSONB 存向量）
--   - additive schema：后续新增列只加默认值，不破坏历史数据
--
-- 2026-09-26：qd_cases → qd_agent_cases（命名规范），已有数据 29 行通过 rename migration 迁移

CREATE TABLE IF NOT EXISTS qd_agent_cases (
    case_id      VARCHAR(64) PRIMARY KEY,
    root_id      BIGINT,
    task_summary TEXT NOT NULL,
    level        VARCHAR(4),                           -- 裁决等级（A/B/C/D）
    tags         JSONB DEFAULT '[]',
    plan_digest  JSONB DEFAULT '[]',                   -- 对应 chain plan 摘要
    outcome      JSONB DEFAULT '{"label": "pending", "t_plus_n": null, "confidence": null}',
    failure_modes JSONB DEFAULT '[]',
    cost         JSONB DEFAULT '{}',
    embedding    JSONB,                                -- 向量（JSONB，非 pgvector，零外部依赖）
    created_at   TIMESTAMP DEFAULT NOW(),
    updated_at   TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_qd_agent_cases_root   ON qd_agent_cases (root_id);
CREATE INDEX IF NOT EXISTS idx_qd_agent_cases_created ON qd_agent_cases (created_at DESC);

COMMENT ON TABLE qd_agent_cases IS 'Agent 案例库（T+N 裁决结果，供酿造/召回/评估用）';
COMMENT ON COLUMN qd_agent_cases.embedding IS 'JSONB 存 embedding 向量（长度视 provider 而定），零 pgvector 依赖';

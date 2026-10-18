-- agent_v7_skill_registry.sql — 技能注册表 / 版本（2026-10-19，skill 治理改造阶段 1）
-- 对应 tmp/skill_system_rework_implementation_plan_20261018.md §8：
--   · DB 只存**治理元数据**（版本/验证记录/反馈统计的前两张），不复制正文作第二真源；
--     `body_uri` 指向受控 SKILL.md 文件位置。
--   · 人工技能不强制迁入（文件系统提供索引元数据即可）；自动管理技能（auto- 前缀）
--     由酿造器登记。
--   · 纯 additive：不修改 qd_agent_weights / qd_agent_cases / qd_agent_traces 既有语义。
--   · 幂等可重跑（IF NOT EXISTS + ON CONFLICT upsert）。

CREATE TABLE IF NOT EXISTS qd_skill_registry (
    skill_id        VARCHAR(100) PRIMARY KEY,           -- 稳定 ID（规范化目录名）
    display_name    VARCHAR(100) NOT NULL,              -- 对外/API 兼容显示名
    source_kind     VARCHAR(20)  NOT NULL DEFAULT 'auto', -- auto / human
    lifecycle_state VARCHAR(20)  NOT NULL DEFAULT 'active',
        -- generated / candidate / validated / active / rejected / superseded / disabled / archived
    active_version  VARCHAR(80),                        -- 内容摘要版本 ID（§15.3 双层标识）
    family          VARCHAR(100),                       -- 可选家族（v1 无硬配额，仅记录）
    aliases         JSONB,                              -- 历史别名（显示名/目录名归一）
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_skill_registry_state
    ON qd_skill_registry(source_kind, lifecycle_state);

CREATE TABLE IF NOT EXISTS qd_skill_versions (
    skill_id          VARCHAR(100) NOT NULL
        REFERENCES qd_skill_registry(skill_id) ON DELETE CASCADE,
    version           VARCHAR(80)  NOT NULL,            -- 内容摘要版本 ID（不可变）
    content_digest    VARCHAR(80)  NOT NULL,            -- 与 version 同值（显式留档）
    description       TEXT,
    facets            JSONB,                            -- 可选分面（§5.1，缺失允许）
    declared_tools    JSONB,                            -- frontmatter 声明工具（真实性已校验）
    body_uri          TEXT,                             -- SKILL.md 位置（正文真源仍在文件）
    validation_state  VARCHAR(20)  NOT NULL DEFAULT 'validated',
    validation_report JSONB,                            -- 验证器版本/用例/结果/耗时/失败原因
    source_chain      VARCHAR(200),                     -- 溯源 chain 名
    semver            VARCHAR(20),                      -- 显示用 SemVer（不作唯一身份）
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (skill_id, version)
);

CREATE INDEX IF NOT EXISTS idx_skill_versions_skill
    ON qd_skill_versions(skill_id, created_at DESC);

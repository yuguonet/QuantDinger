-- agent_v8_skill_events.sql — 技能选择事件链 + 版本级效果反馈（2026-10-19，阶段 4）
-- 对应 tmp/skill_system_rework_implementation_plan_20261018.md §6/§8：
--   · qd_skill_selection_events：每请求一条选择事件，候选/选中/注入/执行/标签
--     **分状态**（不把"只被召回"记作使用）；版本在选择时冻结，后续文件变更不改写
--     历史事件；关联 trace root；幂等（event_id 主键 + 标签先到先得）。
--   · qd_skill_feedback：版本级统计（eligible/correct/partial/incorrect/排除原因），
--     与案例库（qd_agent_cases）、domain 权重（qd_agent_weights layer='domain'）、
--     旧 layer='skill' 权重**三分离**，不混算（§6.1）。
--   · 纯 additive：不修改任何既有表语义；幂等可重跑（IF NOT EXISTS + upsert）。
--   · DB 只存治理元数据，不复制技能正文（body_uri 见 agent_v7）。

CREATE TABLE IF NOT EXISTS qd_skill_selection_events (
    event_id           VARCHAR(40) PRIMARY KEY,      -- uuid4 hex（写入侧生成）
    request_id         VARCHAR(80) NOT NULL DEFAULT '', -- 本次请求标识（v1 = run 级 uuid）
    session_id         VARCHAR(80) NOT NULL DEFAULT '',
    trace_root_id      INTEGER,                      -- flush 后回填（qd_agent_traces.id）
    query_fingerprint  VARCHAR(64) NOT NULL DEFAULT '', -- 查询规范化 SHA-256 前16hex
    index_version      VARCHAR(40) NOT NULL DEFAULT '', -- 候选索引版本
    ranker_version     VARCHAR(40) NOT NULL DEFAULT '', -- 排序器版本（复现实验用）
    candidates         JSONB NOT NULL DEFAULT '[]',  -- [{skill_id,version,base_score,exposed,reasons}]
    selected_skill_id  VARCHAR(100) NOT NULL DEFAULT '', -- planner 点名（预选）
    selected_version   VARCHAR(80)  NOT NULL DEFAULT '', -- 选择时冻结的版本
    injected_skill_id  VARCHAR(100) NOT NULL DEFAULT '', -- 实际注入（可能关键词回落）
    injected_version   VARCHAR(80)  NOT NULL DEFAULT '',
    injected           BOOLEAN NOT NULL DEFAULT FALSE,
    executed           BOOLEAN NOT NULL DEFAULT FALSE, -- trace 出现该技能节点
    execution_status   VARCHAR(40) NOT NULL DEFAULT '',  -- ok / error / ...
    label              VARCHAR(40) NOT NULL DEFAULT '',  -- correct/partial/incorrect/排除态
    exclusion_reason   VARCHAR(80) NOT NULL DEFAULT '',
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_skill_events_root
    ON qd_skill_selection_events(trace_root_id);
CREATE INDEX IF NOT EXISTS idx_skill_events_injected
    ON qd_skill_selection_events(injected_skill_id, injected_version, label);
CREATE INDEX IF NOT EXISTS idx_skill_events_session
    ON qd_skill_selection_events(session_id, created_at DESC);

CREATE TABLE IF NOT EXISTS qd_skill_feedback (
    skill_id        VARCHAR(100) NOT NULL,
    version         VARCHAR(80)  NOT NULL,           -- 内容摘要版本 ID（§15.3）
    eligible_count  INTEGER NOT NULL DEFAULT 0,      -- 可归因且已执行的有效样本数
    correct_count   INTEGER NOT NULL DEFAULT 0,
    partial_count   INTEGER NOT NULL DEFAULT 0,
    incorrect_count INTEGER NOT NULL DEFAULT 0,
    excluded_counts JSONB NOT NULL DEFAULT '{}',     -- {pending: n, unattributable: n, ...}
    weight          DOUBLE PRECISION NOT NULL DEFAULT 1.0, -- 中性 1.0（低于门槛恒 1.0）
    uncertainty     JSONB NOT NULL DEFAULT '{}',     -- {score, confidence, min_samples}
    policy_version  VARCHAR(40) NOT NULL DEFAULT '',
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (skill_id, version)
);

-- 说明：
--   · 保留策略（归档 365 天）与并发 upsert 口径见 skill_events.py 聚合函数；
--     聚合按 event_id 去重（幂等），worker 重跑不重复计数（§6.4）。
--   · 未选中/未曝光事件同样落库（测量召回/覆盖），但不参与 skill 成功率（§6.2）。

-- ═══════════════════════════════════════════════════════════════════════
--  Agent 追责系统 v1.1（2026-10-01）
--  设计文档：docs/追责系统重设计方案_20261001.md（§10 修正纪要 v1.1）
--
--  v1.1 相对 v1 的三条修正：
--    ① 入库前置闸门（intake gate）：不需要追责的决策**不落库**——不是"落进去再
--       标 claim_count=0"，而是压根不写 decisions/claims。漏追责可接受（慢调），
--       脏数据不行。闸门判定见 chain/claims.py::intake_gate。
--    ② 追责是慢调、允许少量误判：judge 阈值放宽（方向对即 hit ≥0.7），
--       不设"人工一致率 ≥80%"的强制切流门槛；权重侧用最小样本 + EMA 慢调吸收噪声。
--       ★ 但 undecidable / data_missing **仍不计入权重**——那是常量污染，
--         与"容忍误判"是两回事。
--    ③ 多领域通用留位：qd_domain_resolvers 是配置驱动的策略表，代码里
--       **不得** if domain == 'finance' 硬编码；v1 只启用 finance，
--       code/general/data 三行以 enabled=false 预留（v2 直接翻开关，不加字段）。
--
--  ★ 与旧表关系：qd_agent_traces **不删**，本文件纯增量。
--    所有语句 CREATE TABLE IF NOT EXISTS / INSERT ... ON CONFLICT，可重复执行。
-- ═══════════════════════════════════════════════════════════════════════

-- ── 1) 决策头：完全领域无关 ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS qd_agent_decisions (
    id              BIGSERIAL PRIMARY KEY,
    session_id      VARCHAR(100),
    trace_root_id   INTEGER,          -- 回指 qd_agent_traces(id)（新执行核仍写该表）
    run_id          VARCHAR(64),      -- 回指 TraceAdapter 的 jsonl run_id（双通道对照）
    domain          VARCHAR(32) NOT NULL,   -- finance / code / general / data
    intent          VARCHAR(64),            -- analyze / screen / trade / write / qa …
    user_query      TEXT NOT NULL,          -- ★ 恒不为空（复盘第一入口）
    answer          TEXT,
    model           VARCHAR(100),
    total_tokens    INTEGER,
    latency_ms      INTEGER,
    created_at      TIMESTAMPTZ DEFAULT NOW(),

    -- 可回溯性汇总（由 claims 反算，避免每次 join）
    claim_count     INTEGER DEFAULT 0,
    resolve_status  VARCHAR(16) DEFAULT 'none',  -- none/pending/partial/resolved/unresolvable
    due_date        DATE,                       -- 最晚的一个 claim 到期日
    resolved_at     TIMESTAMPTZ,

    -- v1.1：入库闸门留痕（gate 版本 + 通过理由，便于回溯"为什么这条进了/没进"）
    gate_ver        VARCHAR(16),
    gate_reason     VARCHAR(64)
);
CREATE INDEX IF NOT EXISTS idx_dec_domain_due   ON qd_agent_decisions(domain, resolve_status, due_date);
CREATE INDEX IF NOT EXISTS idx_dec_created      ON qd_agent_decisions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_dec_trace        ON qd_agent_decisions(trace_root_id);

-- ── 2) 可验证声明：追责的最小单元 ────────────────────────────────────
--    ★ 大盘/个股/板块 的差别只在 subject，不在表结构（P1：只分领域，不分类别）
CREATE TABLE IF NOT EXISTS qd_agent_claims (
    id              BIGSERIAL PRIMARY KEY,
    decision_id     BIGINT NOT NULL REFERENCES qd_agent_decisions(id) ON DELETE CASCADE,
    seq             SMALLINT DEFAULT 0,

    claim_type      VARCHAR(24) NOT NULL,
    --   direction 方向（涨/跌/震荡）
    --   range     区间（收盘价落在 [lo,hi]）
    --   magnitude 幅度（±x%）
    --   level     点位（触及某价位 / 不跌破某价）
    --   event     事件（某事是否发生）
    --   ranking   排序（A 强于 B）
    --   code_run  代码可执行性（跑测试用例）

    subject         VARCHAR(128),     -- '600519' / '000001.SH' / 'sector:半导体' / 'func:position_size'
    subject_kind    VARCHAR(16),      -- stock / index / sector / symbol / free
    horizon         VARCHAR(8) NOT NULL,        -- T+1 / T+3 / T+5 / 1W / 1M / n/a
    due_date        DATE,

    predicted       JSONB NOT NULL,   -- {"dir":"bearish"} / {"lo":11.55,"hi":12.15} / {"pct":-3.0}
    confidence      REAL,             -- ★ 允许 NULL（模型没说就是没说，不填 0.5）
    evidence        TEXT,
    source_quote    TEXT,             -- 该 claim 从 answer 的哪句话抽出来的

    status          VARCHAR(16) DEFAULT 'pending',  -- pending/resolved/unresolvable/waived
    extractor_ver   VARCHAR(16),      -- 提取器版本（换提取逻辑可整体重抽）
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_claim_due      ON qd_agent_claims(status, due_date);
CREATE INDEX IF NOT EXISTS idx_claim_decision ON qd_agent_claims(decision_id);
CREATE INDEX IF NOT EXISTS idx_claim_subject  ON qd_agent_claims(subject_kind, subject);

-- ── 3) 比对结果：分维度偏差 + 归因 ───────────────────────────────────
CREATE TABLE IF NOT EXISTS qd_agent_resolutions (
    id              BIGSERIAL PRIMARY KEY,
    claim_id        BIGINT NOT NULL REFERENCES qd_agent_claims(id) ON DELETE CASCADE,
    resolved_at     TIMESTAMPTZ DEFAULT NOW(),

    -- 真实值（代码侧取，不经过 LLM，保证可复现）
    actual          JSONB NOT NULL,
    actual_as_of    DATE,
    data_source     VARCHAR(64),

    verdict         VARCHAR(16) NOT NULL,       -- hit / miss / partial / undecidable
    verdict_score   REAL,                       -- 0~1 连续分（替代布尔 correct）

    -- ★ 偏差细则
    deviation       JSONB,
    attribution     VARCHAR(32),                -- data_missing/logic_error/black_swan/timing/caliber/noise
    attribution_note TEXT,

    resolver_kind   VARCHAR(16) NOT NULL,       -- market_data / llm_judge / test_run / human
    resolver_model  VARCHAR(100),
    resolver_ver    VARCHAR(32),                -- prompt 版本号（判据改动留痕）
    judge_raw       TEXT,                       -- LLM 原始输出（换 prompt 可整体重判）
    cost_tokens     INTEGER,
    sampled         BOOLEAN DEFAULT FALSE,      -- 是否被抽样送 judge（成本控制留痕）

    human_verdict   VARCHAR(32),
    human_note      TEXT
);
CREATE INDEX IF NOT EXISTS idx_res_claim   ON qd_agent_resolutions(claim_id);
CREATE INDEX IF NOT EXISTS idx_res_verdict ON qd_agent_resolutions(verdict, resolved_at DESC);

-- ── 4) 领域解析策略：跨域扩展只加一行，不加字段 ──────────────────────
CREATE TABLE IF NOT EXISTS qd_domain_resolvers (
    domain          VARCHAR(32) PRIMARY KEY,
    subject_kinds   TEXT[] NOT NULL,
    claim_types     TEXT[] NOT NULL,
    resolver_kind   VARCHAR(16) NOT NULL,
    fact_source     VARCHAR(128),
    horizon_default VARCHAR(8) DEFAULT 'T+3',
    judge_enabled   BOOLEAN DEFAULT TRUE,
    judge_model     VARCHAR(100),
    -- v1.1：入库闸门开关。enabled=false ⇒ 该域**不入库**（拦一道）
    enabled         BOOLEAN DEFAULT TRUE,
    note            TEXT
);

-- v1 只启用 finance；其余三域以 enabled=false 预留（v2 翻开关即可，不改表结构）
INSERT INTO qd_domain_resolvers(domain, subject_kinds, claim_types, resolver_kind,
                                fact_source, judge_enabled, enabled, note) VALUES
 ('finance', ARRAY['stock','index','sector','symbol'],
             ARRAY['direction','range','magnitude','level','ranking'],
             'market_data','CNStock_db.kline_1D_*',TRUE,TRUE,
             'v1 唯一启用域：大盘/个股/板块同一套，只换 subject 不换字段'),
 ('code',    ARRAY['func','file','snippet'], ARRAY['code_run'],
             'test_run','pytest',FALSE,FALSE,
             'v2 启用：跑测试用例即可，纯确定性，不需要 LLM judge'),
 ('general', ARRAY['free'], ARRAY['event','ranking'],
             'llm_judge',NULL,TRUE,FALSE,
             'v2 启用：无客观真值源，标记弱证据，单独出校准报告不与 finance 混算'),
 ('data',    ARRAY['free'], ARRAY[]::TEXT[], 'market_data',NULL,FALSE,FALSE,
             '纯查询类：不产生 claim，闸门直接拦掉，天然不进闭环（不算缺陷）')
ON CONFLICT (domain) DO UPDATE SET
    subject_kinds = EXCLUDED.subject_kinds,
    claim_types   = EXCLUDED.claim_types,
    resolver_kind = EXCLUDED.resolver_kind,
    fact_source   = EXCLUDED.fact_source,
    judge_enabled = EXCLUDED.judge_enabled,
    note          = EXCLUDED.note;
-- ★ enabled 故意**不**被 UPDATE 覆盖：运维在库里手工关掉的域，重跑迁移不会被翻回来。

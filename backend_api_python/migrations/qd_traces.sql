-- QuantDinger Agent 可追责架构 — 数据库迁移
-- 日期: 2026-06-09（初版）→ 2026-06-10（清理别名，统一注册名）
-- 适用: domain="finance" 金融领域
-- 旧表 qd_evaluations 保留只读，不迁移数据
--
-- Skill 清单（12个，无别名）：
--   technical_agent    技术面+动量（合并原 momentum_tracker）
--   indicator_agent    指标信号
--   intelligence_agent 情报+政策（合并原 policy_analyst）
--   hot_money_tracker  游资追踪
--   lockup_watcher     解禁监控
--   market_data_agent  行情+概念+资金（合并原 concept_tracker）
--   screening_agent    选股筛选
--   backtest_agent     策略回测
--   bull_researcher    多头论证
--   bear_researcher    空头反驳
--   trading_agent      交易执行
--   data_agent         数据工程（原 data_engineer，注册名为 data_agent）

-- ═══════════════════════════════════════════════════════════
-- 1. qd_agent_traces — 执行追踪表（替代 qd_evaluations）
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS qd_agent_traces (
    -- 身份
    id              SERIAL PRIMARY KEY,
    parent_id       INTEGER REFERENCES qd_agent_traces(id),
    root_id         INTEGER REFERENCES qd_agent_traces(id),
    layer           VARCHAR(10) NOT NULL,    -- 'chain' / 'skill' / 'tool'
    step_order      INTEGER DEFAULT 0,

    -- 标的
    exec_date       DATE NOT NULL,
    stock_code      VARCHAR(10),
    stock_name      VARCHAR(50),

    -- 内容
    name            VARCHAR(100) NOT NULL,
    score           REAL,
    direction       VARCHAR(20),
    action          VARCHAR(10),
    signal          TEXT,
    confidence      REAL,
    timeframe       VARCHAR(10),
    analysis        TEXT,
    factors         JSONB,

    -- 工具调用记录
    input_params    JSONB,
    output_summary  JSONB,
    tools_called    TEXT[],
    missing_data    TEXT[],

    -- 执行信息
    status          VARCHAR(20) DEFAULT 'ok',
    error           TEXT,
    elapsed_ms      REAL DEFAULT 0,
    data_source     VARCHAR(50),

    -- 回溯验证（盘后写入）
    exit_date       DATE,
    exit_reason     VARCHAR(20),
    pnl_pct         REAL,
    hold_days       INTEGER,
    correct         BOOLEAN,
    calibration     REAL DEFAULT 1.0,

    -- 元数据
    plan            TEXT,               -- smolagents 最终规划
    session_id      VARCHAR(100),
    user_query      TEXT,
    model           VARCHAR(100),
    total_tokens    INTEGER,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_traces_root ON qd_agent_traces(root_id);
CREATE INDEX IF NOT EXISTS idx_traces_parent ON qd_agent_traces(parent_id);
CREATE INDEX IF NOT EXISTS idx_traces_layer ON qd_agent_traces(layer);
CREATE INDEX IF NOT EXISTS idx_traces_stock ON qd_agent_traces(stock_code, exec_date);
CREATE INDEX IF NOT EXISTS idx_traces_skill ON qd_agent_traces(name, exec_date) WHERE layer = 'skill';
CREATE INDEX IF NOT EXISTS idx_traces_pending ON qd_agent_traces(id) WHERE layer = 'chain' AND exit_date IS NULL;

-- 存量库补列（设计文档 §8.3）：chain/store.py 的 INSERT/UPDATE 都会写 plan，
-- 缺列会让整条 INSERT 报错 ⇒ qd_agent_traces 一条都写不进去（声明了却静默断链）。
-- CREATE TABLE IF NOT EXISTS 不会改动已存在的表，故此处单独幂等补列。
ALTER TABLE qd_agent_traces ADD COLUMN IF NOT EXISTS plan text;

-- QuantDinger Agent 三层架构 — 数据库 Schema
-- 一棵树: qd_evaluations (自引用) + qd_factor_weights (聚合)
-- 不考虑旧版兼容, 全新设计

-- ============================================================
-- 主表: 三层统一评估树
-- ============================================================
CREATE TABLE IF NOT EXISTS qd_evaluations (
    id              SERIAL PRIMARY KEY,
    parent_id       INTEGER REFERENCES qd_evaluations(id) ON DELETE CASCADE,
    root_id         INTEGER,                        -- 根节点 id (chain 的 id, 方便查整棵树)
    layer           VARCHAR(10) NOT NULL,           -- 'chain' / 'skill' / 'tool'
    name            VARCHAR(80) NOT NULL,           -- chain_id / skill_name / tool_name
    step_order      INTEGER,                        -- 在父节点中的执行顺序

    -- 时间/标的 (根节点填写, 子节点继承)
    exec_date       DATE,
    stock_code      VARCHAR(10),
    stock_name      VARCHAR(50),

    -- 正向: 内容主体 + 数值buff叠加
    score           REAL,                           -- 0-100 叠加buff
    direction       VARCHAR(10),                    -- bullish/bearish/neutral 叠加buff
    action          VARCHAR(10),                    -- buy/sell/hold/skip (仅 chain 层)
    signal          TEXT,                           -- 一句话信号
    confidence      REAL,                           -- 0.0-1.0 叠加buff

    -- 正向: 内容 (全量记录)
    factors         JSONB,                          -- skill: [{name, value, score, weight, status}]
    output_data     JSONB,                          -- chain: 决策卡+建议, skill: 分析报告, tool: 1~10条dict
    analysis        TEXT,                           -- 分析文字（内容主体）

    -- 正向: 调用信息
    input_params    JSONB,                          -- 入口参数 (chain: 用户消息+意图, skill: 分析对象, tool: 完整参数)
    tools_called    JSONB,                          -- skill 调用了哪些 tools
    missing_data    JSONB,                          -- 缺失的数据
    data_source     VARCHAR(50),                    -- tool 实际命中的数据源

    -- 执行信息
    status          VARCHAR(10) DEFAULT 'ok',       -- ok/missing/failed/skipped/veto
    error           TEXT,
    elapsed_ms      REAL,

    -- 反向: 回测验证 (回溯时写入)
    actual_return_1d    REAL,
    actual_return_3d    REAL,
    actual_return_5d    REAL,
    actual_direction_3d VARCHAR(10),                 -- 实际方向
    correct_3d      BOOLEAN,                        -- predicted direction vs actual
    calibration     REAL DEFAULT 1.0,               -- 校准因子 1.00~1.05

    -- 人工介入 (数据异常时)
    human_reviewed  BOOLEAN DEFAULT FALSE,
    human_verdict   VARCHAR(20),                    -- confirmed_anomaly / false_positive / null

    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- 根节点索引 (查整棵树)
CREATE INDEX IF NOT EXISTS idx_eval_root ON qd_evaluations(root_id);
-- 按层查询
CREATE INDEX IF NOT EXISTS idx_eval_layer ON qd_evaluations(layer, exec_date);
-- 按标的查询
CREATE INDEX IF NOT EXISTS idx_eval_stock ON qd_evaluations(stock_code, exec_date);
-- 回测验证查询
CREATE INDEX IF NOT EXISTS idx_eval_verify ON qd_evaluations(layer, correct_3d) WHERE correct_3d IS NOT NULL;
-- 父节点查询
CREATE INDEX IF NOT EXISTS idx_eval_parent ON qd_evaluations(parent_id);


-- ============================================================
-- 因子权重表 (跨决策的聚合, 独立于树)
-- ============================================================
CREATE TABLE IF NOT EXISTS qd_factor_weights (
    chain_id        VARCHAR(50) NOT NULL,
    skill_name      VARCHAR(50) NOT NULL,
    factor_name     VARCHAR(100) NOT NULL,
    weight          REAL DEFAULT 1.0,              -- 当前权重 (衰减后)
    accuracy_3d     REAL,                          -- 3日方向准确率
    sample_count    INTEGER DEFAULT 0,
    decay_half_life INTEGER DEFAULT 30,            -- 衰减半衰期(天)
    last_updated    TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (chain_id, skill_name, factor_name)
);

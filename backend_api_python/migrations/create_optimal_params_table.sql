-- qd_indicator_optimal_params: 指标参数优化结果表
-- 每条记录 = 一个指标 × 一只股票 × 一个时间周期 的最优参数

CREATE TABLE IF NOT EXISTS qd_indicator_optimal_params (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER NOT NULL DEFAULT 1,
    indicator_id    INTEGER NOT NULL,
    symbol          VARCHAR(20) NOT NULL,
    market          VARCHAR(20) NOT NULL DEFAULT 'CNStock',
    timeframe       VARCHAR(10) NOT NULL,
    best_params     JSONB NOT NULL DEFAULT '{}',
    score           DOUBLE PRECISION DEFAULT 0,
    win_rate        DOUBLE PRECISION DEFAULT 0,
    total_return    DOUBLE PRECISION DEFAULT 0,
    sharpe_ratio    DOUBLE PRECISION DEFAULT 0,
    max_drawdown    DOUBLE PRECISION DEFAULT 0,
    total_trades    INTEGER DEFAULT 0,
    combos_tested   INTEGER DEFAULT 0,
    updated_at      TIMESTAMP DEFAULT NOW(),
    created_at      TIMESTAMP DEFAULT NOW(),

    -- 同一指标×同一股票×同一周期只保留一条（upsert 用）
    UNIQUE (indicator_id, symbol, timeframe)
);

-- 常用查询索引
CREATE INDEX IF NOT EXISTS idx_optimal_params_indicator
    ON qd_indicator_optimal_params (indicator_id);

CREATE INDEX IF NOT EXISTS idx_optimal_params_symbol
    ON qd_indicator_optimal_params (symbol);

CREATE INDEX IF NOT EXISTS idx_optimal_params_user
    ON qd_indicator_optimal_params (user_id);

COMMENT ON TABLE qd_indicator_optimal_params IS '指标参数优化结果：每个指标×股票×周期的最优参数';
COMMENT ON COLUMN qd_indicator_optimal_params.best_params IS '最优参数 JSON，如 {"ma_fast": 10, "ma_slow": 50, "threshold": 35}';
COMMENT ON COLUMN qd_indicator_optimal_params.score IS '综合评分（Sharpe*0.3 + 收益*0.3 + 胜率*1.0 + 盈亏比*0.3 - 回撤*1.0）';
COMMENT ON COLUMN qd_indicator_optimal_params.combos_tested IS '本次优化测试的参数组合总数';

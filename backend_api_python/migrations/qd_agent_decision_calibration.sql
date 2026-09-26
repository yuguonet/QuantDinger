-- qd_agent_decision_calibration — Agent 决策阈值校准
-- 来源：app/services/ai_calibration.py（运行时自建表，补正式 migration）
-- 设计：
--   - 按 market 维度存储 buy/sell/hold 阈值
--   - 每行一个校准版本，ORDER BY validated_at DESC LIMIT 1 取最新
--   - 盘后 evaluator 扫描历史交易 + AI 评分，自动迭代写入新行
--
-- 2026-09-26：qd_ai_calibration → qd_agent_decision_calibration（命名规范），
--             已有数据通过 rename migration 迁移

CREATE TABLE IF NOT EXISTS qd_agent_decision_calibration (
    id                          SERIAL PRIMARY KEY,
    market                      VARCHAR(50) NOT NULL,
    buy_threshold               DECIMAL(10,4) NOT NULL,
    sell_threshold              DECIMAL(10,4) NOT NULL,
    min_consensus_abs_override  DECIMAL(10,4) NOT NULL,
    quality_hold_threshold      DECIMAL(10,4) NOT NULL,
    validated_at                TIMESTAMP DEFAULT NOW(),
    created_at                  TIMESTAMP DEFAULT NOW()
);

-- latest lookup: 按 market 取最近一次校验结果
CREATE INDEX IF NOT EXISTS idx_agent_decision_calibration_market_validated_at
    ON qd_agent_decision_calibration (market, validated_at DESC);

COMMENT ON TABLE qd_agent_decision_calibration IS 'Agent 决策阈值校准（market 维度，盘后自动迭代）';

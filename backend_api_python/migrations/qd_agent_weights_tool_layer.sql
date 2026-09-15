-- 2026-09-15: qd_agent_weights 主键修正 + tool 层支持
-- 原 PK (layer, name, skill_name) 的 skill_name 在 PG 中被强制 NOT NULL，
-- 而 skill 层（skill_name=NULL）与 tool 层（skill_name=NULL）都需要可空。
-- PG 主键列不能为 NULL → 改用 UNIQUE INDEX 实现同等约束，skill_name 保持可空。
ALTER TABLE qd_agent_weights DROP CONSTRAINT IF EXISTS qd_agent_weights_pkey;
ALTER TABLE qd_agent_weights ALTER COLUMN skill_name DROP NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_weights_key
    ON qd_agent_weights (layer, name, COALESCE(skill_name, ''));
CREATE INDEX IF NOT EXISTS idx_agent_weights_tool ON qd_agent_weights(layer, name) WHERE layer = 'tool';

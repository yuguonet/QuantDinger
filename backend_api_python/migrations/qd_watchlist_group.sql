-- 自选股分组支持 (group_name 字段方案) — 幂等,可重复执行
-- 存量数据自动落入 DEFAULT '默认自选'
ALTER TABLE qd_watchlist ADD COLUMN IF NOT EXISTS group_name VARCHAR(50) NOT NULL DEFAULT '默认自选';
CREATE INDEX IF NOT EXISTS idx_qdwl_group ON qd_watchlist(user_id, group_name);

-- 清理废弃 Agent 专用表（零代码引用，已被新表替代）
-- 执行前请确认：这些表不被任何 Python 代码引用，DROP 是安全的
-- 幂等：IF EXISTS，可重复执行
--
-- 表清单：
--   qd_evaluations        被 qd_agent_traces 替代
--   qd_skill_weights      被 qd_agent_weights(layer='skill') 替代
--   qd_factor_weights     被 qd_agent_weights(layer='factor') 替代
--   qd_component_predictions  四层归因方案，全仓零引用
--   qd_component_weights      四层权重，全仓零引用
--   qd_agent_path_cache       编排路径缓存，全仓零 SQL 读写
--
-- 来源：2026-09-26 agent 表命名规范盘点（docs/agent_专用数据库表_盘点与命名规范_20260926.md）

BEGIN;

DROP TABLE IF EXISTS qd_evaluations CASCADE;
DROP TABLE IF EXISTS qd_skill_weights CASCADE;
DROP TABLE IF EXISTS qd_factor_weights CASCADE;
DROP TABLE IF EXISTS qd_component_predictions CASCADE;
DROP TABLE IF EXISTS qd_component_weights CASCADE;
DROP TABLE IF EXISTS qd_agent_path_cache CASCADE;

COMMIT;

-- 执行后验证：SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_name LIKE 'qd_%' ORDER BY table_name;
-- 期望：不再有上述 6 张表

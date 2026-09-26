-- =============================================================================
-- 移除 stock_basic_info 表中的 pe_ratio / pb_ratio 列
-- 日期: 2026-09-26
-- 原因: pe_ratio / pb_ratio 是实时变动的估值指标，不应存静态基本信息表
--       实时 PE/PB 由 agent/tools/finance/data_tools.py 的腾讯实时估值获取
-- 执行方式: 在 CNStock_db 库中手动执行本 SQL
--           （stock_basic_info 表位于 CNStock_db，不在主库 postgres）
-- =============================================================================

-- 幂等：IF EXISTS 保护，重复执行不会报错
ALTER TABLE stock_basic_info DROP COLUMN IF EXISTS pe_ratio;
ALTER TABLE stock_basic_info DROP COLUMN IF EXISTS pb_ratio;

-- qd_agent_cron_jobs.cron_expr 列宽放宽 64 → 255（2026-10-28）
--
-- 背景（实测证据）：
--   · 建表时 cron_expr 是 VARCHAR(64)，假设成立前提是「单条 5 段式表达式」。
--   · 但 app/agent/cron/cron_tools.py::_parse_at_time 的「开盘期间/交易时间/盘中
--     + 每N分钟」分支按 A 股交易时段拆成 4 段并用 | 拼接（9:30-11:30 + 13:00-15:00），
--     2026-10-28 补 15:00 收盘点后实测 82~93 字符。
--   · 结果：at="开盘期间,每5分钟" 走 create_cron_job / create_alert_job 必抛
--     psycopg2.errors.StringDataRightTruncation: 值太长了(64)，且报错只指向列、
--     不指向语义，调用方（LLM）会误判成 message/channels 有问题（10-28 实测误判）。
--
-- 迁移双路（与 qd_cron_jobs_v2_20261019.sql 同规则）：
--   ① 本文件走 docker 首启（docker-entrypoint-initdb.d）
--   ② 存量库由 app/agent/cron/cron_worker._ensure_cron_expr_width() 启动幂等 ensure
--     （ALTER COLUMN TYPE 无 IF NOT EXISTS 语法，故用 information_schema 条件判断）
-- 重复执行安全：宽度已达标时 PG 视为同类型变更，不重写表。

ALTER TABLE qd_agent_cron_jobs
    ALTER COLUMN cron_expr TYPE VARCHAR(255);

COMMENT ON COLUMN qd_agent_cron_jobs.cron_expr IS
    '5段式cron: 分 时 日 月 周；支持 | 多段（如交易时段拆分：30-59/5 9 * * 1-5|*/5 10 * * 1-5…）';

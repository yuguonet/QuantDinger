-- qd_agent_claims 重试治理（追责系统 v1.1 → v1.2）
-- 日期：2026-10-12
-- 起因（实测）：6 条 claim 因国庆假期窗口被提前扫描，取不到数据即被写成
--               status='unresolvable'（终态，永不重试）；而数据在 10/9 已可得。
--               根因是「暂时性失败」被当成「永久性失败」。
--
-- 本迁移只加两列 + 一个索引，不改任何既有列语义（additive，不破坏历史数据）：
--   verify_fail_count  该 claim 连续判定失败次数（取数不可用 / 窗口截断）
--   last_attempt_at    最近一次判定尝试时间（诊断用：看是否在推进）
--
-- 配合代码（chain/account_store.py）：
--   · 失败一次 → verify_fail_count += 1，状态**保持 pending**（可重试）
--   · 达到上限（QD_CLAIM_MAX_RETRY，默认 5）→ 才置 unresolvable
--   上限语义沿用 chain/evaluator 既有毒丸机制（连续 5 次出队），不新造规则。

ALTER TABLE qd_agent_claims
    ADD COLUMN IF NOT EXISTS verify_fail_count INTEGER NOT NULL DEFAULT 0;

ALTER TABLE qd_agent_claims
    ADD COLUMN IF NOT EXISTS last_attempt_at TIMESTAMPTZ;

-- 到期扫描走 (status, due_date)，这里补 fail_count 以便「排除已耗尽重试的」走索引
CREATE INDEX IF NOT EXISTS idx_claim_due_retry
    ON qd_agent_claims (status, due_date, verify_fail_count);

COMMENT ON COLUMN qd_agent_claims.verify_fail_count IS
    '连续判定失败次数（取数不可用/窗口截断）；达上限才转 unresolvable。暂时性失败不再永久出队（2026-10-12）';
COMMENT ON COLUMN qd_agent_claims.last_attempt_at IS
    '最近一次判定尝试时间；用于诊断判定链路是否在推进（2026-10-12）';

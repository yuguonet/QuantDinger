-- qd_tail_scores.sql — P0-A GBDT 粗筛分数落库（2026-11-23，接入闭环 [a]）
-- 对应 P0A_接入闭环方案_20261123.md §2：
--   · 盘后批量产分数（不占 LLM 往返），供 [c] hint 注入与复评溯源；
--   · 唯一键含 model_ver —— 同一天多版本模型分数并存，不互相覆盖（复评对得上号）；
--   · factor_sig = 前复权快照指纹（mtime:size），追溯历史 bar 静默重算；
--   · 纯 additive：新表，不修改任何既有表；幂等可重跑（IF NOT EXISTS + upsert）。

CREATE TABLE IF NOT EXISTS qd_tail_scores (
    id          BIGSERIAL PRIMARY KEY,
    trade_date  DATE        NOT NULL,
    instrument  VARCHAR(16) NOT NULL,           -- 6 位代码
    p_tail      DOUBLE PRECISION NOT NULL,      -- 尾部概率 P(涨幅>7% 或涨停)，未校准原始分数
    rank        INTEGER     NOT NULL,           -- 当日横截面名次（1 = 最高分）
    board       VARCHAR(16) NOT NULL DEFAULT '',
    model_ver   VARCHAR(80) NOT NULL,           -- tag:data_last:best_iteration
    factor_sig  VARCHAR(80) NOT NULL DEFAULT '',-- 前复权因子快照指纹
    data_asof   DATE,                           -- 模型数据截止日
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (trade_date, instrument, model_ver)
);

CREATE INDEX IF NOT EXISTS idx_tail_scores_date_rank
    ON qd_tail_scores(trade_date, rank);
CREATE INDEX IF NOT EXISTS idx_tail_scores_model
    ON qd_tail_scores(model_ver, trade_date);

-- 说明：
--   · p_tail 是粗筛排序分，不是方向概率 p_up —— 不进 p_base/Δp 链（方案冲突 A 拍板 A1）。
--   · 写入侧 app/agent/wheels/gbdt_tail/store.py 用 ON CONFLICT 幂等 upsert，失败 fail-open。

-- =============================================================================
-- 自选股标签（watchlist label）—— 统一产出表
--
-- 设计依据: docs/自选股标签统一产出方案.md §4
-- 建表入口唯一: 本文件。禁止在 app/ 内散落 CREATE TABLE / ALTER TABLE。
--
-- 要点:
--   1) 与 qd_watchlist **无外键、无列耦合**（只通过 (market, symbol) 关联）
--      ⇒ qd_watchlist 保持纯关系表, 不在其上加 label 列
--   2) UNIQUE (market, symbol, trade_date, source) 是「低级别不更新高级别」的
--      **结构性保证**: system 的 upsert 物理上不可能触到 auto/agent 的行
--   3) 失效唯一依据 = expires_at（NULL = 永不过期, 由提交方决定）; TTL 不落"天数"列
--   4) 只落事实 + 结构化单元, **不落渲染结果**（配色/行序/HTML 一律请求时渲染）
--   5) 不落 name（权威源在 basicinfo 侧, 避免第三处漂移）
--   6) **时间列语义统一 = 市场本地时间(Asia/Shanghai)的 naive 值, 单一时钟**:
--      库会话 TimeZone 是 UTC, 若用裸 NOW() 则比应用本地时钟早 8 小时, 而读路径
--      (`api._is_expired` / `render.trading_age_days`) 用的是 Python 本地时钟 ⇒ 两钟并存
--      会让"已失效 / 未更新天数"在本地 00:00~08:00 出现分歧。故默认值也带 AT TIME ZONE,
--      且应用侧一律显式写入 `store._now()`（默认值只是兜底）。
-- =============================================================================

CREATE TABLE IF NOT EXISTS qd_watchlist_label (
    id            SERIAL PRIMARY KEY,
    market        VARCHAR(50)  NOT NULL,
    symbol        VARCHAR(50)  NOT NULL,
    trade_date    DATE         NOT NULL,   -- 答案针对的交易日
    source        VARCHAR(20)  NOT NULL,   -- system | agent | auto
    grade         SMALLINT     NOT NULL,   -- 1 | 2 | 3（空白不落行）
    score         NUMERIC(5,2) CHECK (score IS NULL OR (score >= 0 AND score <= 100)),
    score_version INTEGER,                 -- 该 source 内的口径版本（跨 source 不可比）
    supports      JSONB,                   -- [{price, strength, dist_pct, origin}, ...]
    resistances   JSONB,                   -- 同上
    extras        JSONB,                   -- 扩展段单元列表（fields / table）
    facts_asof    TIMESTAMP,               -- 所依据 bar 的日期
    expires_at    TIMESTAMP,               -- 失效唯一依据; NULL = 永不过期
    created_at    TIMESTAMP DEFAULT (NOW() AT TIME ZONE 'Asia/Shanghai'),
    updated_at    TIMESTAMP DEFAULT (NOW() AT TIME ZONE 'Asia/Shanghai'),
    UNIQUE (market, symbol, trade_date, source)
);

CREATE INDEX IF NOT EXISTS idx_qwl_label_lookup ON qd_watchlist_label(market, symbol, trade_date DESC);
CREATE INDEX IF NOT EXISTS idx_qwl_label_day    ON qd_watchlist_label(trade_date);
CREATE INDEX IF NOT EXISTS idx_qwl_label_exp    ON qd_watchlist_label(expires_at);

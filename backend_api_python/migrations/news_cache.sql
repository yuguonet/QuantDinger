-- =============================================================================
-- 新闻缓存明细表 (单表设计)
-- 每条新闻一行, 通过 (symbol, market) 关联
-- =============================================================================

CREATE TABLE IF NOT EXISTS qd_news_cache_items (
    id              SERIAL PRIMARY KEY,
    symbol          VARCHAR(20) NOT NULL,
    market          VARCHAR(50) NOT NULL DEFAULT 'CNStock',
    title           TEXT NOT NULL DEFAULT '',             -- 新闻标题
    snippet         TEXT DEFAULT '',                      -- 摘要/内容片段
    url             TEXT DEFAULT '',                      -- 原文链接
    source          VARCHAR(100) DEFAULT '',              -- 来源 (东方财富/新浪/腾讯等)
    published_date  VARCHAR(40) DEFAULT '',               -- 原文发布时间
    sentiment       VARCHAR(20) DEFAULT 'neutral',        -- positive/negative/neutral
    created_at      TIMESTAMP DEFAULT NOW(),              -- 入库时间 (用于过期清理)
    UNIQUE(symbol, market, title)                         -- 同一股票不存重复标题
);

-- 按股票查新闻
CREATE INDEX IF NOT EXISTS idx_news_items_stock ON qd_news_cache_items(symbol, market);
-- 按入库时间清理过期数据
CREATE INDEX IF NOT EXISTS idx_news_items_created ON qd_news_cache_items(created_at);
-- 按发布时间排序
CREATE INDEX IF NOT EXISTS idx_news_items_published ON qd_news_cache_items(published_date);

COMMENT ON TABLE qd_news_cache_items IS '新闻缓存: 每条新闻一行, 15天过期, 24h去重';

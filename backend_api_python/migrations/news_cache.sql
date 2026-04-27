-- =============================================================================
-- 新闻缓存明细表 (单表设计) v2.2
-- 每条新闻一行, 通过 (symbol, market) 关联
-- 新增: sentiment_score 数值评分, 重大负面一票否决 (-999)
-- 新增: 15天过期自动清理 (应用层 + 可选 DB 定时任务)
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
    sentiment_score REAL DEFAULT 5.0,                     -- 数值评分 0-10, 重大负面=-999 (一票否决)
    created_at      TIMESTAMP DEFAULT NOW(),              -- 入库时间 (用于过期清理)
    UNIQUE(symbol, market, title)                         -- 同一股票不存重复标题
);

-- 按股票查新闻
CREATE INDEX IF NOT EXISTS idx_news_items_stock ON qd_news_cache_items(symbol, market);
-- 按入库时间清理过期数据 (15天清理依赖此索引)
CREATE INDEX IF NOT EXISTS idx_news_items_created ON qd_news_cache_items(created_at);
-- 按发布时间排序
CREATE INDEX IF NOT EXISTS idx_news_items_published ON qd_news_cache_items(published_date);

COMMENT ON TABLE qd_news_cache_items IS '新闻缓存: 每条新闻一行, 24h去重, 15天过期清理, sentiment_score支持一票否决';

-- =============================================================================
-- 可选: DB 级定时清理函数 (配合 pg_cron 使用)
-- 如果应用层已在 save/get 时清理, 此函数作为兜底保障
-- 用法: SELECT purge_expired_news_cache();
-- 或配合 pg_cron: SELECT cron.schedule('purge-news', '0 3 * * *', 'SELECT purge_expired_news_cache()');
-- =============================================================================
CREATE OR REPLACE FUNCTION purge_expired_news_cache()
RETURNS INTEGER AS $$
DECLARE
    deleted_count INTEGER;
BEGIN
    DELETE FROM qd_news_cache_items
    WHERE created_at < NOW() - INTERVAL '15 days';
    GET DIAGNOSTICS deleted_count = ROW_COUNT;
    RAISE NOTICE 'purge_expired_news_cache: deleted % rows', deleted_count;
    RETURN deleted_count;
END;
$$ LANGUAGE plpgsql;

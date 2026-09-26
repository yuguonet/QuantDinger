-- qd_agent_messages — Agent 会话消息表
-- 来源：app/agent/memory/postgres_memory.py（运行时自建表，补正式 migration）
-- 设计：
--   - 会话消息持久化（session_id / role / content）
--   - TTL 滑动窗口 + 最大消息裁剪
--   - PostgreSQL tsvector + simple 分词 GIN 索引实现聊天全文搜索
--
-- 2026-09-26：agent_messages → qd_agent_messages（命名规范），已有数据 224 行通过 rename migration 迁移

CREATE TABLE IF NOT EXISTS qd_agent_messages (
    id          BIGSERIAL PRIMARY KEY,
    session_id  TEXT NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    fts_vector  tsvector                            -- 聊天全文搜索（simple 分词）
);

-- 会话查找：按 session_id 取最近 N 条（DESC OFFSET LIMIT）
CREATE INDEX IF NOT EXISTS idx_qd_agent_messages_lookup
    ON qd_agent_messages (session_id, id DESC);

-- 全文搜索 GIN 索引
CREATE INDEX IF NOT EXISTS idx_qd_agent_messages_fts
    ON qd_agent_messages USING gin (fts_vector);

COMMENT ON TABLE qd_agent_messages IS 'Agent 会话消息持久化（含 FTS 全文搜索）';
COMMENT ON COLUMN qd_agent_messages.role IS 'user / assistant / system';
COMMENT ON COLUMN qd_agent_messages.fts_vector IS 'tsvector，simple 配置分词，中文按单字+双字 gram 预处理';

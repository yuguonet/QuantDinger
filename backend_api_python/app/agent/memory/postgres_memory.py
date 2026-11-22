"""
PostgreSQL 持久化记忆

基于 app.utils.db（psycopg2 线程安全连接池），通过 run_in_executor
将同步 DB 操作适配为 async 接口，避免事件循环绑定问题。

支持：
- 自动建表 + 索引
- 滑动窗口（只保留最近 N 条消息）
- TTL 自动过期
- 多进程/多实例共享

环境变量：
    DATABASE_URL=postgresql://user:password@host:port/dbname
    MEMORY_TTL_DAYS=1095  （消息保留天数，默认 3 年；覆盖 DEFAULT_TTL_DAYS）
    MEMORY_RECENCY_TAU_DAYS=365  （全文检索时间衰减常数，<=0 关闭；覆盖 DEFAULT_RECENCY_TAU_DAYS）
"""
import asyncio
import logging
import os
from typing import Optional

from .base import MemoryBase, MemoryMessage
from typing import Optional

logger = logging.getLogger(__name__)

# 默认表名
DEFAULT_TABLE = "qd_agent_messages"
# 默认 TTL：3 年（1095 天），可由 MEMORY_TTL_DAYS 覆盖；落到 SQL 时才换算成秒
DEFAULT_TTL_DAYS = int(os.getenv("MEMORY_TTL_DAYS", "1095"))
# 默认时间衰减常数（天）：tau<=0 时关闭时间衰减
DEFAULT_RECENCY_TAU_DAYS = float(os.getenv("MEMORY_RECENCY_TAU_DAYS", "365"))


# ── 中文分词辅助（复用 postgres_fts.py 的思路）──
import re as _re

def _tokenize_chinese(text: str) -> str:
    """简单中文分词：单字 + 双字 gram，用空格连接。

    PostgreSQL 'simple' 配置按空格分词，所以需要手动预处理。
    与 rag/postgres_fts.py 的实现保持一致。
    """
    text = (text or "").strip()
    if not text:
        return ""
    cjk = _re.findall(r"[\u4e00-\u9fff]", text)
    words = _re.findall(r"[a-zA-Z0-9]+", text.lower())
    grams = list(cjk)
    for i in range(len(cjk) - 1):
        grams.append(cjk[i] + cjk[i + 1])
    return " ".join(grams + words)


def _raw_cursor(conn):
    """获取底层 psycopg2 cursor，绕过 PostgresCursor 包装器。

    PostgresCursor.execute() 会做字符串预处理（_rewrite_insert_or_ignore），
    不接受 psycopg2.sql.Composed 对象。需要直接用底层 cursor。

    返回的 cursor 必须在用完后 close()，否则连接归还池时 cursor 泄漏。
    """
    pg_conn = conn._conn  # PostgresConnection._conn 是原始 psycopg2 connection
    from psycopg2.extras import RealDictCursor
    return pg_conn.cursor(cursor_factory=RealDictCursor)


class PostgresMemory(MemoryBase):
    """
    PostgreSQL 持久化记忆（基于 app.utils.db 同步连接池）

    使用示例：
        memory = PostgresMemory()
        await memory.add("session_1", "user", "你好")
        history = await memory.get_history("session_1", limit=10)
    """

    def __init__(
        self,
        dsn: str = "",          # 保留参数，实际由 app.utils.db 从 DATABASE_URL 读取
        *,
        max_messages: int = 100,
        table_name: str = DEFAULT_TABLE,
        ttl_days: int = DEFAULT_TTL_DAYS,
        recency_tau_days: float = DEFAULT_RECENCY_TAU_DAYS,
    ):
        self._max_messages = max_messages
        self._table_name = table_name
        self._ttl_days = ttl_days
        self._ttl_seconds = ttl_days * 86400
        self._recency_tau_days = recency_tau_days
        self._initialized = False
        self._init_lock = asyncio.Lock()

    # ── 同步 DB 辅助 ────────────────────────────────────────────

    @staticmethod
    def _run_sync(fn):
        """在默认执行器中运行同步函数，返回 future"""
        loop = asyncio.get_running_loop()
        return loop.run_in_executor(None, fn)

    def _get_connection(self):
        """延迟导入并获取 app.utils.db 连接"""
        from app.utils.db import get_db_connection
        return get_db_connection()

    # ── 初始化（惰性） ──────────────────────────────────────────

    async def _ensure_init(self):
        """惰性初始化：建表 + 旧数据清理"""
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return

            from psycopg2 import sql
            tbl = sql.Identifier(self._table_name)
            idx = sql.Identifier(f"idx_{self._table_name}_lookup")

            def _init():
                with self._get_connection() as conn:
                    c = _raw_cursor(conn)
                    try:
                        c.execute(sql.SQL("""
                            CREATE TABLE IF NOT EXISTS {} (
                                id          BIGSERIAL PRIMARY KEY,
                                session_id  TEXT NOT NULL,
                                role        TEXT NOT NULL,
                                content     TEXT NOT NULL,
                                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
                            )
                        """).format(tbl))
                        c.execute(sql.SQL("""
                            CREATE INDEX IF NOT EXISTS {} ON {}(session_id, id DESC)
                        """).format(idx, tbl))
                        c.execute(sql.SQL("""
                            DELETE FROM {} WHERE created_at < NOW() - make_interval(secs => %s)
                        """).format(tbl), (self._ttl_seconds,))

                        # ── FTS: 为聊天记录全文检索添加 tsvector 列 + GIN 索引 ──
                        fts_col = sql.Identifier("fts_vector")
                        fts_idx = sql.Identifier(f"idx_{self._table_name}_fts")
                        # Python 层检查列是否存在（PL/pgSQL 内 sql.Identifier 引号会导致 information_schema 匹配失败）
                        c.execute(
                            "SELECT 1 FROM information_schema.columns "
                            "WHERE table_name = %s AND column_name = 'fts_vector'",
                            (self._table_name,),
                        )
                        if c.fetchone() is None:
                            c.execute(sql.SQL(
                                "ALTER TABLE {} ADD COLUMN fts_vector tsvector"
                            ).format(tbl))
                        c.execute(sql.SQL("""
                            CREATE INDEX IF NOT EXISTS {} ON {} USING gin({})
                        """).format(fts_idx, tbl, fts_col))
                        # 回填已有数据的 tsvector
                        c.execute(sql.SQL("""
                            UPDATE {} SET fts_vector =
                                to_tsvector('simple', COALESCE(content, ''))
                            WHERE fts_vector IS NULL
                        """).format(tbl))

                        conn.commit()
                    finally:
                        c.close()

            await self._run_sync(_init)
            self._initialized = True
            logger.info(
                "PostgresMemory 就绪: table=%s max_messages=%d ttl=%dd",
                self._table_name, self._max_messages, self._ttl_days,
            )

    # ── MemoryBase 接口 ─────────────────────────────────────────

    async def add(self, session_id: str, role: str, content: str) -> bool:
        """添加消息并裁剪旧消息（滑动窗口）"""
        try:
            await self._ensure_init()
        except Exception as e:
            logger.error("PostgresMemory 初始化失败: %s", e, exc_info=True)
            return False

        from psycopg2 import sql
        tbl = sql.Identifier(self._table_name)

        def _add():
            with self._get_connection() as conn:
                c = _raw_cursor(conn)
                try:
                    c.execute(sql.SQL(
                        "INSERT INTO {} (session_id, role, content, fts_vector) "
                        "VALUES (%s, %s, %s, to_tsvector('simple', %s))"
                    ).format(tbl), (session_id, role, content, content))
                    c.execute(sql.SQL("""
                        DELETE FROM {}
                        WHERE session_id = %s
                          AND id <= (
                              SELECT id FROM {}
                              WHERE session_id = %s
                              ORDER BY id DESC
                              OFFSET %s
                              LIMIT 1
                          )
                    """).format(tbl, tbl), (session_id, session_id, self._max_messages))
                    conn.commit()
                finally:
                    c.close()
            return True

        try:
            return await self._run_sync(_add)
        except Exception as e:
            logger.error(
                "PostgresMemory.add 失败: session=%s role=%s - %s",
                session_id, role, e, exc_info=True,
            )
            return False

    async def get_history(
        self, session_id: str, limit: int = 10
    ) -> list[MemoryMessage]:
        """获取对话历史（最新 N 条，按创建时间正序）"""
        try:
            await self._ensure_init()
        except Exception as e:
            logger.error("PostgresMemory 初始化失败: %s", e, exc_info=True)
            return []

        from psycopg2 import sql
        tbl = sql.Identifier(self._table_name)

        def _get():
            with self._get_connection() as conn:
                c = _raw_cursor(conn)
                try:
                    c.execute(sql.SQL("""
                        SELECT role, content FROM (
                            SELECT role, content, id FROM {}
                            WHERE session_id = %s ORDER BY id DESC LIMIT %s
                        ) sub ORDER BY id ASC
                    """).format(tbl), (session_id, limit))
                    rows = c.fetchall()
                    return [MemoryMessage(role=r["role"], content=r["content"]) for r in rows]
                finally:
                    c.close()

        try:
            return await self._run_sync(_get)
        except Exception as e:
            logger.error(
                "PostgresMemory.get_history 失败: session=%s - %s",
                session_id, e, exc_info=True,
            )
            return []

    async def clear(self, session_id: str) -> bool:
        """清除指定会话的所有消息"""
        try:
            await self._ensure_init()
        except Exception as e:
            logger.error("PostgresMemory 初始化失败: %s", e, exc_info=True)
            return False

        from psycopg2 import sql
        tbl = sql.Identifier(self._table_name)

        def _clear():
            with self._get_connection() as conn:
                c = _raw_cursor(conn)
                try:
                    c.execute(sql.SQL("DELETE FROM {} WHERE session_id = %s").format(tbl),
                              (session_id,))
                    conn.commit()
                finally:
                    c.close()
            return True

        try:
            return await self._run_sync(_clear)
        except Exception as e:
            logger.error(
                "PostgresMemory.clear 失败: session=%s - %s",
                session_id, e, exc_info=True,
            )
            return False

    # ── 扩展方法 ────────────────────────────────────────────────

    async def count_messages(self, session_id: str) -> int:
        """获取会话的消息数量"""
        await self._ensure_init()
        from psycopg2 import sql
        tbl = sql.Identifier(self._table_name)

        def _count():
            with self._get_connection() as conn:
                c = _raw_cursor(conn)
                try:
                    c.execute(sql.SQL("SELECT COUNT(*) FROM {} WHERE session_id = %s").format(tbl),
                              (session_id,))
                    row = c.fetchone()
                    return row["count"] if row else 0
                finally:
                    c.close()

        return await self._run_sync(_count)

    async def list_sessions(self) -> list[str]:
        """列出所有有消息的 session_id（按最后活动时间降序）"""
        await self._ensure_init()
        from psycopg2 import sql
        tbl = sql.Identifier(self._table_name)

        def _list():
            with self._get_connection() as conn:
                c = _raw_cursor(conn)
                try:
                    c.execute(sql.SQL("""
                        SELECT session_id FROM {}
                        GROUP BY session_id
                        ORDER BY MAX(created_at) DESC
                    """).format(tbl))
                    return [r["session_id"] for r in c.fetchall()]
                finally:
                    c.close()

        return await self._run_sync(_list)

    async def get_stats(self) -> dict:
        """获取存储统计"""
        await self._ensure_init()
        from psycopg2 import sql
        tbl = sql.Identifier(self._table_name)

        def _stats():
            with self._get_connection() as conn:
                c = _raw_cursor(conn)
                try:
                    c.execute(sql.SQL("SELECT COUNT(*) AS count FROM {}").format(tbl))
                    total = c.fetchone()["count"] or 0
                    c.execute(sql.SQL("SELECT COUNT(DISTINCT session_id) AS count FROM {}").format(tbl))
                    sessions = c.fetchone()["count"] or 0
                    c.execute(sql.SQL("SELECT MIN(created_at) AS oldest FROM {}").format(tbl))
                    oldest = c.fetchone()["oldest"]
                    c.execute(sql.SQL("SELECT MAX(created_at) AS newest FROM {}").format(tbl))
                    newest = c.fetchone()["newest"]
                    return {
                        "total_messages": total,
                        "total_sessions": sessions,
                        "oldest_message": str(oldest) if oldest else None,
                        "newest_message": str(newest) if newest else None,
                    }
                finally:
                    c.close()

        return await self._run_sync(_stats)

    async def search(
        self,
        query: str,
        limit: int = 5,
        session_id: Optional[str] = None,
    ) -> list[dict]:
        """全文搜索历史聊天记录。

        使用 PostgreSQL tsvector + tsquery 实现中文检索。
        返回按「相关度 × 时间衰减」排序的结果：score = rank * exp(-age_days / tau)。
        tau <= 0 时关闭衰减，退化为纯 rank 排序。时间只影响排序，不参与召回过滤。

        Args:
            query: 搜索关键词（支持中文，内部做分词处理）
            limit: 最大返回条数
            session_id: 可选，限定在某个会话内搜索

        Returns:
            list[dict]: [{"role", "content", "session_id", "created_at", "score"}]
        """
        await self._ensure_init()

        tokens = _tokenize_chinese(query)
        if not tokens:
            return []

        from psycopg2 import sql
        tbl = sql.Identifier(self._table_name)

        # 构建 tsquery（simple 配置，OR 逻辑）
        # tokens 已是空格分隔的 gram，拆开用 | 连接
        tsquery = " | ".join(tokens.split())

        # tau>0：相关度按距今天数指数衰减；GREATEST 防未来时间戳反向加分。
        # tau<=0：衰减因子恒 1，排序退化为纯 rank（回退开关）。
        _tau = float(self._recency_tau_days)
        if _tau > 0:
            score_sql = (
                "ts_rank(fts_vector, q) * "
                f"exp(-GREATEST(0, EXTRACT(EPOCH FROM (NOW() - created_at)) / 86400.0) / {_tau})"
            )
        else:
            score_sql = "ts_rank(fts_vector, q)"

        def _search():
            with self._get_connection() as conn:
                c = _raw_cursor(conn)
                try:
                    if session_id:
                        c.execute(sql.SQL("""
                            SELECT role, content, session_id, created_at, ({score}) AS score
                            FROM {tbl}, to_tsquery('simple', %s) AS q
                            WHERE fts_vector @@ q AND session_id = %s
                            ORDER BY score DESC, created_at DESC
                            LIMIT %s
                        """).format(tbl=tbl, score=sql.SQL(score_sql)), (tsquery, session_id, limit))
                    else:
                        c.execute(sql.SQL("""
                            SELECT role, content, session_id, created_at, ({score}) AS score
                            FROM {tbl}, to_tsquery('simple', %s) AS q
                            WHERE fts_vector @@ q
                            ORDER BY score DESC, created_at DESC
                            LIMIT %s
                        """).format(tbl=tbl, score=sql.SQL(score_sql)), (tsquery, limit))
                    rows = c.fetchall()
                    return [
                        {
                            "role": r["role"],
                            "content": r["content"],
                            "session_id": r["session_id"],
                            "created_at": str(r["created_at"]) if r["created_at"] else "",
                            "score": float(r["score"]) if r["score"] else 0.0,
                        }
                        for r in rows
                    ]
                finally:
                    c.close()

        try:
            results = await self._run_sync(_search)
            logger.info(
                "[PostgresMemory] 搜索 '%s' → %d 条结果 (session=%s)",
                query[:30], len(results), session_id or "all",
            )
            return results
        except Exception as e:
            logger.error("[PostgresMemory] search 失败: %s", e, exc_info=True)
            return []

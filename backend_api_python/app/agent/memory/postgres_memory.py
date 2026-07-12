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
"""
import asyncio
import logging
from typing import Optional

from memory.base import MemoryBase, MemoryMessage

logger = logging.getLogger(__name__)

# 默认表名
DEFAULT_TABLE = "agent_messages"
# 默认 TTL：7 天
DEFAULT_TTL_SECONDS = 7 * 24 * 3600


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
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ):
        self._max_messages = max_messages
        self._table_name = table_name
        self._ttl_seconds = ttl_seconds
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
                        conn.commit()
                    finally:
                        c.close()

            await self._run_sync(_init)
            self._initialized = True
            logger.info(
                "PostgresMemory 就绪: table=%s max_messages=%d ttl=%ds",
                self._table_name, self._max_messages, self._ttl_seconds,
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
                        "INSERT INTO {} (session_id, role, content) VALUES (%s, %s, %s)"
                    ).format(tbl), (session_id, role, content))
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

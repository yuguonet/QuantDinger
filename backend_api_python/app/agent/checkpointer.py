# -*- coding: utf-8 -*-
"""
checkpointer.py — 状态持久化（PostgreSQL）

LangGraph 的 Checkpointer 是核心设计模式之一：
  - 每个节点执行完自动保存状态
  - 进程崩了可从最近 checkpoint 恢复
  - 支持会话隔离（thread_id）

本模块实现最小可用版本：
  - PostgresCheckpointer：存到 qd_checkpoints 表
  - save(state, node)：保存当前状态
  - load(thread_id)：恢复最近状态
  - cleanup(days)：清理过期数据

建表 DDL：
    CREATE TABLE IF NOT EXISTS qd_checkpoints (
        id          SERIAL PRIMARY KEY,
        thread_id   VARCHAR(64) NOT NULL,
        node        VARCHAR(64) NOT NULL,
        state       JSONB NOT NULL,
        created_at  TIMESTAMP DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_checkpoints_thread
        ON qd_checkpoints (thread_id, created_at DESC);
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# 建表 DDL
_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS qd_checkpoints (
    id          SERIAL PRIMARY KEY,
    thread_id   VARCHAR(64) NOT NULL,
    node        VARCHAR(64) NOT NULL,
    state       JSONB NOT NULL,
    created_at  TIMESTAMP DEFAULT NOW()
);
"""

_CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_checkpoints_thread
    ON qd_checkpoints (thread_id, created_at DESC);
"""


class PostgresCheckpointer:
    """PostgreSQL 状态持久化。

    用法：
        from checkpointer import PostgresCheckpointer

        # 初始化（复用现有连接池）
        checkpointer = PostgresCheckpointer(pool)

        # 保存（每个节点执行完自动调用）
        await checkpointer.save(state, "execute_node")

        # 恢复（进程重启后）
        saved_state = await checkpointer.load("session_123")
    """

    def __init__(self, pool, table_name: str = "qd_checkpoints"):
        """
        Args:
            pool: psycopg2 连接池（复用 db_multi 的 MarketPool）
            table_name: checkpoint 表名
        """
        self._pool = pool
        self._table = table_name
        self._table_ready = False

    def _sql_table(self):
        """安全的表名标识符"""
        from psycopg2 import sql
        return sql.Identifier(self._table)

    def _ensure_table(self):
        """确保表存在（幂等）。"""
        if self._table_ready:
            return
        try:
            from psycopg2 import sql
            with self._pool.cursor() as cur:
                cur.execute(sql.SQL(
                    "CREATE TABLE IF NOT EXISTS {} ("
                    "id SERIAL PRIMARY KEY, thread_id VARCHAR(64) NOT NULL, "
                    "node VARCHAR(64) NOT NULL, state JSONB NOT NULL, "
                    "created_at TIMESTAMP DEFAULT NOW())"
                ).format(self._sql_table()))
                cur.execute(sql.SQL(
                    "CREATE INDEX IF NOT EXISTS {} ON {} (thread_id, created_at DESC)"
                ).format(
                    sql.Identifier(f"idx_{self._table}_thread"), self._sql_table(),
                ))
            # MarketPool.cursor() 上下文管理器已自动 commit
            self._table_ready = True
            logger.info("[Checkpointer] %s 表就绪", self._table)
        except Exception as e:
            logger.warning("[Checkpointer] 建表失败: %s", e)

    async def save(self, state: dict, node: str):
        """保存当前状态。

        Args:
            state: 完整状态字典
            node: 刚执行完的节点名
        """
        self._ensure_table()
        thread_id = state.get("session_id", "default")

        # 序列化状态（处理不可 JSON 化的对象）
        try:
            state_json = json.dumps(state, default=str, ensure_ascii=False)
        except (TypeError, ValueError) as e:
            logger.warning("[Checkpointer] 状态序列化失败: %s", e)
            state_json = json.dumps({"error": f"序列化失败: {e}"}, ensure_ascii=False)

        try:
            from psycopg2 import sql
            with self._pool.cursor() as cur:
                cur.execute(sql.SQL(
                    "INSERT INTO {} (thread_id, node, state, created_at) VALUES (%s, %s, %s::jsonb, %s)"
                ).format(self._sql_table()),
                    (thread_id, node, state_json, datetime.now()))
            # MarketPool.cursor() 上下文管理器已自动 commit
            logger.debug("[Checkpointer] 保存: thread=%s node=%s", thread_id, node)
        except Exception as e:
            logger.warning("[Checkpointer] 保存失败: %s", e)

    async def load(self, thread_id: str) -> Optional[dict]:
        """恢复最近一次 checkpoint。

        Args:
            thread_id: 会话 ID

        Returns:
            最近的状态字典，无记录返回 None
        """
        self._ensure_table()
        try:
            from psycopg2 import sql
            with self._pool.cursor() as cur:
                cur.execute(sql.SQL(
                    "SELECT state FROM {} WHERE thread_id = %s ORDER BY created_at DESC LIMIT 1"
                ).format(self._sql_table()),
                    (thread_id,))
                row = cur.fetchone()
                if row:
                    state = row[0]
                    if isinstance(state, str):
                        state = json.loads(state)
                    logger.info("[Checkpointer] 恢复: thread=%s", thread_id)
                    return state
        except Exception as e:
            logger.warning("[Checkpointer] 恢复失败: %s", e)
        return None

    async def cleanup(self, days: int = 7):
        """清理过期 checkpoint。

        Args:
            days: 保留天数
        """
        self._ensure_table()
        cutoff = datetime.now() - timedelta(days=days)
        try:
            from psycopg2 import sql
            with self._pool.cursor() as cur:
                cur.execute(sql.SQL(
                    "DELETE FROM {} WHERE created_at < %s"
                ).format(self._sql_table()),
                    (cutoff,))
                deleted = cur.rowcount
            # MarketPool.cursor() 上下文管理器已自动 commit
            if deleted > 0:
                logger.info("[Checkpointer] 清理 %d 条过期记录", deleted)
        except Exception as e:
            logger.warning("[Checkpointer] 清理失败: %s", e)

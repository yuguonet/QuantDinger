"""
PostgreSQL 全文搜索检索器

使用 PostgreSQL 内置 tsvector/tsquery 做中文检索，不需要额外扩展。
配合 KeywordRetriever 组成多路召回。
"""
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)


def _tokenize_chinese(text: str) -> str:
    """简单中文分词：单字 + 双字 gram，用空格连接。

    PostgreSQL 'simple' 配置按空格分词，所以需要手动预处理。
    """
    text = (text or "").strip()
    if not text:
        return ""
    # 提取中文字符
    cjk = re.findall(r"[\u4e00-\u9fff]", text)
    # 提取英文单词和数字
    words = re.findall(r"[a-zA-Z0-9]+", text.lower())
    # 中文：单字 + 双字 gram
    grams = list(cjk)  # 单字
    for i in range(len(cjk) - 1):
        grams.append(cjk[i] + cjk[i + 1])  # 双字
    return " ".join(grams + words)


class PostgresFTSRetriever:
    """
    PostgreSQL 全文搜索检索器。

    使用 tsvector + tsquery 实现中文检索，不需要 zhparser 或 pg_jieba。
    适合和 KeywordRetriever 组成 MultiRouteRetriever 多路召回。

    使用前提：
      - qd_analysis_memory 表有数据
      - 首次调用时自动添加 tsvector 列和 GIN 索引
    """

    def __init__(
        self,
        dsn: str,
        top_k: int = 5,
        table: str = "qd_analysis_memory",
    ):
        self.dsn = dsn
        self.top_k = top_k
        self.table = table
        self._initialized = False

    def _ensure_schema(self, conn):
        """确保 tsvector 列和索引存在。"""
        if self._initialized:
            return
        try:
            cur = conn.cursor()
            # 添加 tsvector 列（如果不存在）
            cur.execute(f"""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = '{self.table}' AND column_name = 'fts_vector'
                    ) THEN
                        ALTER TABLE {self.table} ADD COLUMN fts_vector tsvector;
                    END IF;
                END $$;
            """)
            # 填充 tsvector（只更新 NULL 的行）
            cur.execute(f"""
                UPDATE {self.table}
                SET fts_vector = to_tsvector('simple',
                    COALESCE(symbol, '') || ' ' ||
                    COALESCE(decision, '') || ' ' ||
                    COALESCE(summary, '')
                )
                WHERE fts_vector IS NULL;
            """)
            # 创建 GIN 索引（如果不存在）
            cur.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_{self.table}_fts
                ON {self.table} USING gin(fts_vector);
            """)
            conn.commit()
            self._initialized = True
            logger.info("[PostgresFTS] schema 初始化完成")
        except Exception as e:
            logger.warning("[PostgresFTS] schema 初始化失败: %s", e)
            conn.rollback()

    async def retrieve(self, query: str, top_k: Optional[int] = None, filter: Optional[dict] = None) -> list[dict]:
        """执行全文搜索。"""
        k = top_k or self.top_k
        tokens = _tokenize_chinese(query)
        if not tokens:
            return []

        try:
            import psycopg2
            conn = psycopg2.connect(self.dsn, connect_timeout=5)
            self._ensure_schema(conn)

            cur = conn.cursor()

            # 构建 tsquery（simple 配置，OR 逻辑）
            # tokens 已经是空格分隔的 gram，直接用
            tsquery = " | ".join(tokens.split())

            sql = f"""
                SELECT
                    symbol, decision, summary, created_at,
                    ts_rank(fts_vector, q) AS rank
                FROM {self.table}, to_tsquery('simple', %s) AS q
                WHERE fts_vector @@ q
                ORDER BY rank DESC, created_at DESC
                LIMIT %s
            """
            cur.execute(sql, (tsquery, k))
            rows = cur.fetchall()

            docs = []
            for row in rows:
                symbol, decision, summary, created_at, rank = row
                content = f"{decision or ''} {symbol or ''} {summary or ''}"
                docs.append({
                    "content": content[:500],
                    "metadata": {
                        "source": "postgres_fts",
                        "symbol": symbol or "",
                        "decision": decision or "",
                        "date": str(created_at) if created_at else "",
                    },
                    "score": float(rank) if rank else 0,
                    "retrieval_route": "postgres_fts",
                })

            cur.close()
            conn.close()

            logger.info("[PostgresFTS] 查询 '%s' → %d 条结果", query[:30], len(docs))
            return docs

        except Exception as e:
            logger.warning("[PostgresFTS] 检索失败: %s", e)
            return []

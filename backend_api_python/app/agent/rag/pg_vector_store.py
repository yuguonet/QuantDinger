"""
PostgreSQL 向量存储

不依赖 pgvector 扩展，向量存在 JSONB 列，检索时 Python 计算余弦相似度。
适合中小规模（<10万条），大规模请用 pgvector。
"""
import json
import logging
import re
import uuid
from typing import Optional

logger = logging.getLogger(__name__)

# SQL 标识符白名单：表名 / JSONB 键名在 psycopg2 里无法用 %s 占位
# （`metadata ->> %s` 不是合法 PG 语法），只能拼接 ⇒ 必须在拼接前校验字符集。
# 只允许不含引号、点号、空白的纯标识符，任何越界输入直接 ValueError（不静默放行）。
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


def _assert_ident(name: str, what: str) -> str:
    """校验 SQL 标识符合法性，非法即抛（不做静默清理，避免掩盖调用方 bug）。"""
    s = str(name or "")
    if not _IDENT_RE.match(s):
        raise ValueError(f"非法的{what}（只允许字母/数字/下划线且不以数字开头）: {name!r}")
    return s


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """计算余弦相似度。"""
    import math
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class PgVectorStore:
    """
    PostgreSQL 向量存储（不依赖 pgvector）。

    向量存在 JSONB 列，检索时 Python 计算余弦相似度。
    自动建表、自动索引。

    使用前提：
      - PostgreSQL 已运行
      - DATABASE_URL 环境变量已设置
    """

    def __init__(
        self,
        dsn: str,
        embedding,
        table: str = "rag_vectors",
        score_threshold: float = 0.3,
    ):
        self.dsn = dsn
        self.embedding = embedding
        # 表名会 f-string 拼进 CREATE TABLE / INSERT / SELECT，必须先过标识符白名单
        self.table = _assert_ident(table, "表名")
        self.score_threshold = score_threshold
        self._initialized = False

    def _ensure_schema(self, conn):
        """确保表和索引存在。"""
        if self._initialized:
            return
        try:
            cur = conn.cursor()
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {self.table} (
                    id VARCHAR(64) PRIMARY KEY,
                    content TEXT NOT NULL,
                    metadata JSONB DEFAULT '{{}}',
                    embedding JSONB NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                );
            """)
            cur.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_{self.table}_created
                ON {self.table} (created_at DESC);
            """)
            conn.commit()
            self._initialized = True
            logger.info("[PgVectorStore] schema 初始化完成")
        except Exception as e:
            logger.warning("[PgVectorStore] schema 初始化失败: %s", e)
            conn.rollback()

    async def add_texts(self, texts: list[str], metadatas: Optional[list[dict]] = None) -> list[str]:
        """添加文本到向量存储。"""
        try:
            import psycopg2
            conn = psycopg2.connect(self.dsn, connect_timeout=5)
            self._ensure_schema(conn)

            vectors = self.embedding.embed_documents(texts)
            if not vectors:
                conn.close()
                return []

            cur = conn.cursor()
            ids = []
            for idx, (text, vector) in enumerate(zip(texts, vectors)):
                # 空文本 / 该条向量化失败：embed_documents 在该位置返回 []，
                # 必须跳过 —— 写进去就是「内容与向量不一致」的脏数据。
                if not vector:
                    continue
                doc_id = str(uuid.uuid4())
                metadata = metadatas[idx] if metadatas else {}
                cur.execute(
                    f"INSERT INTO {self.table} (id, content, metadata, embedding) VALUES (%s, %s, %s, %s)",
                    (doc_id, text, json.dumps(metadata, ensure_ascii=False), json.dumps(vector)),
                )
                ids.append(doc_id)

            conn.commit()
            cur.close()
            conn.close()
            logger.info("[PgVectorStore] 添加 %d 条文档", len(ids))
            return ids
        except Exception as e:
            logger.error("[PgVectorStore] 添加失败: %s", e)
            return []

    async def similarity_search(self, query: str, k: int = 5, filter: Optional[dict] = None) -> list[dict]:
        """相似度检索。"""
        try:
            import psycopg2
            conn = psycopg2.connect(self.dsn, connect_timeout=5)
            self._ensure_schema(conn)

            query_vector = self.embedding.embed_query(query)
            if not query_vector:
                conn.close()
                return []

            cur = conn.cursor()
            # 加载全部向量（小规模可行，大规模应改用 pgvector）
            sql = f"SELECT id, content, metadata, embedding FROM {self.table}"
            params = []
            if filter:
                conditions = []
                for key, value in filter.items():
                    # key 拼进 SQL（`metadata->>'...'`），必须校验；value 走 %s 占位
                    _assert_ident(key, "metadata 过滤键")
                    conditions.append(f"metadata->>'{key}' = %s")
                    params.append(str(value))
                sql += " WHERE " + " AND ".join(conditions)
            sql += " ORDER BY created_at DESC LIMIT 2000"  # 限制扫描量

            cur.execute(sql, params)
            rows = cur.fetchall()

            # 计算余弦相似度
            scored = []
            for doc_id, content, metadata, embedding_json in rows:
                try:
                    doc_vector = json.loads(embedding_json) if isinstance(embedding_json, str) else embedding_json
                    score = _cosine_similarity(query_vector, doc_vector)
                    if score >= self.score_threshold:
                        scored.append({
                            "content": content,
                            "metadata": metadata if isinstance(metadata, dict) else json.loads(metadata) if metadata else {},
                            "score": round(score, 4),
                        })
                except Exception:
                    continue

            # 按相似度排序
            scored.sort(key=lambda x: x["score"], reverse=True)
            cur.close()
            conn.close()

            logger.info("[PgVectorStore] 查询 '%s' → %d 条结果", query[:30], len(scored[:k]))
            return scored[:k]

        except Exception as e:
            logger.warning("[PgVectorStore] 检索失败: %s", e)
            return []

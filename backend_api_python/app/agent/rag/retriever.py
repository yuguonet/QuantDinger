"""
检索器

封装向量检索 + 上下文构建逻辑。
"""
import asyncio
import logging
import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Optional

from rag.vector_store import VectorStoreBase

logger = logging.getLogger(__name__)


def _doc_key(doc: dict) -> str:
    """为去重生成稳定 key，优先使用元数据 ID，其次使用内容片段。"""
    metadata = doc.get("metadata") or {}
    for key in ("id", "doc_id", "source_id", "url", "source"):
        value = metadata.get(key)
        if value:
            return f"{key}:{value}"
    return (doc.get("content") or "")[:300]


def _tokenize(text: str) -> list[str]:
    """轻量关键词切分，兼容英文单词和中文 2-gram。"""
    text = (text or "").lower()
    words = re.findall(r"[a-z0-9_]+", text)
    cjk = re.findall(r"[\u4e00-\u9fff]", text)
    grams = ["".join(cjk[i:i + 2]) for i in range(max(0, len(cjk) - 1))]
    return words + grams


class Retriever:
    """
    检索器

    封装向量检索，提供格式化的上下文文本供 LLM 使用。

    使用示例：
        retriever = Retriever(vector_store, top_k=5)
        docs = await retriever.retrieve("Python 异步编程")
        context = retriever.format_context(docs)
    """

    def __init__(
        self,
        vector_store: VectorStoreBase,
        top_k: int = 5,
        score_threshold: Optional[float] = None,
        embedding=None,
    ):
        self.vector_store = vector_store
        self.top_k = top_k
        self.score_threshold = score_threshold
        self.embedding = embedding

    async def retrieve(
        self,
        query: str,
        top_k: Optional[int] = None,
        filter: Optional[dict] = None,
    ) -> list[dict]:
        """
        执行检索

        :param query: 查询文本
        :param top_k: 返回数量
        :param filter: 元数据过滤条件
        :return: 文档列表 [{"content": ..., "metadata": ..., "score": ...}]
        """
        k = top_k or self.top_k
        docs = await self.vector_store.similarity_search(query=query, k=k, filter=filter)
        if self.score_threshold is not None:
            docs = [d for d in docs if d.get("score", 0) >= self.score_threshold]
        return docs

    @staticmethod
    def format_context(docs: list[dict], max_length: int = 8000) -> str:
        """
        将检索结果格式化为上下文文本

        :param docs: 检索结果列表
        :param max_length: 上下文最大长度
        :return: 格式化的上下文文本
        """
        if not docs:
            return ""

        parts = []
        total_len = 0
        for i, doc in enumerate(docs):
            content = doc.get("content", "")
            score = doc.get("score", 0)
            source = doc.get("metadata", {}).get("source", "unknown")

            part = f"[参考{i + 1}] (相关度: {score:.2f}, 来源: {source})\n{content}"
            if total_len + len(part) > max_length:
                break
            parts.append(part)
            total_len += len(part)

        return "\n\n---\n\n".join(parts)


class KeywordRetriever:
    """
    轻量关键词召回器。

    用于和向量召回组成多路召回，不依赖额外服务。适合小型知识库、
    FAQ、术语表，或作为向量检索漏召时的补充。
    """

    def __init__(self, documents: list[dict | str], top_k: int = 5):
        self.top_k = top_k
        self._docs = []
        self._doc_freq = Counter()

        for idx, item in enumerate(documents):
            if isinstance(item, str):
                doc = {"content": item, "metadata": {"id": str(idx)}}
            else:
                doc = {
                    "content": item.get("content") or item.get("text") or "",
                    "metadata": item.get("metadata") or {"id": str(idx)},
                }
            tokens = set(_tokenize(doc["content"]))
            for token in tokens:
                self._doc_freq[token] += 1
            self._docs.append((doc, tokens))

    async def retrieve(
        self,
        query: str,
        top_k: Optional[int] = None,
        filter: Optional[dict] = None,
    ) -> list[dict]:
        query_tokens = _tokenize(query)
        if not query_tokens:
            return []

        q_counter = Counter(query_tokens)
        total_docs = max(len(self._docs), 1)
        scored = []

        for doc, tokens in self._docs:
            metadata = doc.get("metadata") or {}
            if filter and any(metadata.get(k) != v for k, v in filter.items()):
                continue

            score = 0.0
            for token, q_count in q_counter.items():
                if token not in tokens:
                    continue
                idf = math.log((total_docs + 1) / (self._doc_freq[token] + 1)) + 1
                score += q_count * idf

            if score > 0:
                scored.append({
                    "content": doc["content"],
                    "metadata": metadata,
                    "score": round(score, 4),
                    "retrieval_route": "keyword",
                })

        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:top_k or self.top_k]


@dataclass
class RetrieverRoute:
    """多路召回中的单条检索路线"""

    name: str
    retriever: object
    weight: float = 1.0
    top_k: Optional[int] = None


class MultiRouteRetriever:
    """
    多路召回 + RRF 融合检索器。

    支持把向量召回、关键词召回、不同 collection、不同过滤条件等多条路线
    并发执行，然后用 Reciprocal Rank Fusion 合并排序。
    """

    def __init__(
        self,
        routes: list[RetrieverRoute],
        top_k: int = 5,
        rrf_k: int = 60,
        query_variants: Optional[list[str]] = None,
    ):
        if not routes:
            raise ValueError("MultiRouteRetriever 至少需要一条检索路线")
        self.routes = routes
        self.top_k = top_k
        self.rrf_k = rrf_k
        self.query_variants = query_variants or []

    async def retrieve(
        self,
        query: str,
        top_k: Optional[int] = None,
        filter: Optional[dict] = None,
    ) -> list[dict]:
        queries = [query, *[q for q in self.query_variants if q and q != query]]
        tasks = []
        task_meta = []

        for route in self.routes:
            route_top_k = route.top_k or top_k or self.top_k
            for q in queries:
                tasks.append(route.retriever.retrieve(q, top_k=route_top_k, filter=filter))
                task_meta.append((route.name, route.weight, q))

        route_results = await asyncio.gather(*tasks, return_exceptions=True)
        fused: dict[str, dict] = {}

        for docs, (route_name, weight, q) in zip(route_results, task_meta):
            if isinstance(docs, Exception):
                logger.warning(f"检索路线失败: {route_name}, error={docs}")
                continue

            for rank, doc in enumerate(docs, start=1):
                key = _doc_key(doc)
                rrf_score = weight / (self.rrf_k + rank)

                if key not in fused:
                    fused[key] = {
                        "content": doc.get("content", ""),
                        "metadata": doc.get("metadata", {}),
                        "score": 0.0,
                        "routes": [],
                        "raw_scores": [],
                    }

                fused[key]["score"] += rrf_score
                fused[key]["raw_scores"].append(doc.get("score", 0))
                fused[key]["routes"].append({
                    "route": route_name,
                    "query": q,
                    "rank": rank,
                    "score": doc.get("score", 0),
                })

        docs = sorted(fused.values(), key=lambda x: x["score"], reverse=True)
        for doc in docs:
            doc["score"] = round(doc["score"], 6)
            doc["metadata"] = {
                **(doc.get("metadata") or {}),
                "retrieval_routes": doc.pop("routes", []),
                "raw_scores": doc.pop("raw_scores", []),
            }
        return docs[:top_k or self.top_k]

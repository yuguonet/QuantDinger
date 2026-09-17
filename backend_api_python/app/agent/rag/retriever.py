"""
检索器

封装向量检索 + 上下文构建逻辑。
"""
import asyncio
import logging
import math
import os
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

    # 非金融意图集合（2026-09-16 下沉自 nodes：意图×语料域不匹配的判断属于 RAG 层——
    # 它拥有语料域知识；node 只传意图信号，不替 RAG 做过滤决策）。
    # 命中即跳过检索（省一次向量查询），返回空列表由调用方按「无参考资料」处理。
    NON_FINANCE_INTENTS = frozenset({"code", "general", "explain"})

    async def retrieve(
        self,
        query: str,
        top_k: Optional[int] = None,
        filter: Optional[dict] = None,
        intent: str = "",
    ) -> list[dict]:
        """
        执行检索

        :param query: 查询文本
        :param top_k: 返回数量
        :param filter: 元数据过滤条件
        :param intent: 用户意图分类（task 子类型，如 code/screen/analysis）。
            非金融意图（code/general/explain）与本库语料域不匹配 → 直接返回 []。
        :return: 文档列表 [{"content": ..., "metadata": ..., "score": ...}]
        """
        if intent and intent in self.NON_FINANCE_INTENTS:
            logger.info("[Retriever] 意图=%s 与本库语料域不匹配，跳过检索", intent)
            return []
        k = top_k or self.top_k
        docs = await self.vector_store.similarity_search(query=query, k=k, filter=filter)
        if self.score_threshold is not None:
            docs = [d for d in docs if d.get("score", 0) >= self.score_threshold]
        return docs

    @staticmethod
    def format_context(docs: list[dict], max_length: int = 8000) -> str:
        """将检索结果格式化为上下文文本（**只保留有效相关度的文档**）。

        Args:
            docs: 检索结果列表
            max_length: 上下文最大长度（按字符软截断）

        相关性过滤（2026-09-16 用户硬规则：**相关度 <0.3 一律拦截，严禁进入下一轮**）：
        绝对阈值 RAG_REF_ABS_FLOOR（默认 0.3）对所有召回源统一生效——
        Reranker 输出 [0,1] 直接可比；RRF 融合分（上限约 0.016）全数低于 0.3，
        视为「未证明相关」整体拦截（RRF 分数只反映排名不反映相关性，
        记忆库里没有相关内容时它照样返回 topN——实测「跑马灯」捞到西安银行 SELL）。
        整批被拦 → 返回空串，下游以「无参考资料」处理，不让弱相关内容污染任务。
        如需恢复历史记忆召回，请先接入 Reranker 精排（rerank_score >= 0.3 的文档可通过）。
        """
        if not docs:
            return ""

        # 2026-09-16 硬规则：score < 0.3 一律拦截（含原「best 兜底」例外也取消——
        # 整批弱相关时留一条兜底仍会污染任务，用户明确要求严禁进入）。
        _abs_floor = float(os.getenv("RAG_REF_ABS_FLOOR", "0.3"))

        parts = []
        total_len = 0
        kept = 0
        dropped = 0
        for doc in docs:
            content = doc.get("content", "")
            score = float(doc.get("score", 0) or 0)
            source = doc.get("metadata", {}).get("source", "unknown")
            if score < _abs_floor:
                dropped += 1
                continue
            part = f"[参考{kept + 1}] (相关度: {score:.2f}, 来源: {source})\n{content}"
            if total_len + len(part) > max_length:
                break
            parts.append(part)
            total_len += len(part)
            kept += 1
        if dropped:
            logger.info("[Retriever] 相关性硬拦截：%d → %d 条 (floor=%.2f)",
                        dropped + kept, kept, _abs_floor)

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


class ChatHistoryRetriever:
    """聊天历史全文检索器。

    封装 PostgresMemory.search()，作为 MultiRouteRetriever 的一路召回。
    使用 PostgreSQL tsvector/tsquery 做中文关键词检索，不依赖 Embedding。

    使用方式：
        from memory.postgres_memory import PostgresMemory
        memory = PostgresMemory()
        history_retriever = ChatHistoryRetriever(memory, top_k=5)
        docs = await history_retriever.retrieve("茅台")
    """

    def __init__(self, memory, top_k: int = 5, weight: float = 0.5):
        """
        Args:
            memory: PostgresMemory 实例
            top_k: 默认返回条数
            weight: RRF 融合权重（聊天记录权重应低于知识文档）
        """
        self.memory = memory
        self.top_k = top_k
        self.weight = weight

    async def retrieve(
        self,
        query: str,
        top_k: Optional[int] = None,
        filter: Optional[dict] = None,
    ) -> list[dict]:
        """搜索历史聊天记录。

        Args:
            query: 搜索关键词
            top_k: 返回数量（覆盖默认值）
            filter: 可选过滤条件（如 {"session_id": "xxx"}）

        Returns:
            list[dict]: 与 MultiRouteRetriever 兼容的文档格式
        """
        k = top_k or self.top_k
        session_id = (filter or {}).get("session_id")

        results = await self.memory.search(query, limit=k, session_id=session_id)

        # 转换为 MultiRouteRetriever 兼容的文档格式
        docs = []
        for r in results:
            docs.append({
                "content": r["content"],
                "metadata": {
                    "source": "chat_history",
                    "role": r["role"],
                    "session_id": r["session_id"],
                    "date": r["created_at"],
                },
                "score": r["score"],
                "retrieval_route": "chat_history",
            })
        return docs


@dataclass
class RetrieverRoute:
    """多路召回中的单条检索路线"""

    name: str
    retriever: object
    weight: float = 1.0
    top_k: Optional[int] = None


class BGEReranker:
    """
    BGE-reranker 精排模型。

    使用 Cross-Encoder 对检索结果进行重排序，显著提升精度。
    支持本地 sentence-transformers 或远程 API。

    使用方式：
        reranker = BGEReranker()
        reranked = reranker.rerank(query, docs, top_k=5)
    """

    def __init__(
        self,
        model_path: str = "BAAI/bge-reranker-v2-m3",
        use_api: bool = False,
        api_url: str = "",
        api_key: str = "",
    ):
        self.model_path = model_path
        self.use_api = use_api
        self.api_url = api_url
        self.api_key = api_key
        self._model = None

    def _get_model(self):
        """懒加载模型"""
        if self._model is None:
            try:
                from sentence_transformers import CrossEncoder
                self._model = CrossEncoder(self.model_path)
                logger.info(f"[BGEReranker] 模型加载完成: {self.model_path}")
            except ImportError:
                logger.error("sentence_transformers 未安装，请执行: pip install sentence-transformers")
                raise
            except Exception as e:
                logger.error(f"[BGEReranker] 模型加载失败: {e}")
                raise
        return self._model

    def rerank(
        self,
        query: str,
        docs: list[dict],
        top_k: int = 5,
    ) -> list[dict]:
        """
        对检索结果重排序。

        :param query: 查询文本
        :param docs: 文档列表 [{"content": ..., "score": ...}]
        :param top_k: 返回数量
        :return: 重排序后的文档列表
        """
        if not docs:
            return []

        try:
            if self.use_api:
                return self._rerank_api(query, docs, top_k)
            return self._rerank_local(query, docs, top_k)
        except Exception as e:
            logger.error(f"[BGEReranker] 重排序失败: {e}")
            return docs[:top_k]

    def _rerank_local(
        self,
        query: str,
        docs: list[dict],
        top_k: int,
    ) -> list[dict]:
        """本地模型重排序"""
        model = self._get_model()
        pairs = [(query, d.get("content", "")) for d in docs]
        scores = model.predict(pairs)

        for doc, score in zip(docs, scores):
            doc["rerank_score"] = float(score)

        reranked = sorted(docs, key=lambda x: x.get("rerank_score", 0), reverse=True)
        return reranked[:top_k]

    def _rerank_api(
        self,
        query: str,
        docs: list[dict],
        top_k: int,
    ) -> list[dict]:
        """远程 API 重排序（兼容 jina/cohere/siliconflow 等）"""
        import requests

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        documents = [d.get("content", "") for d in docs]
        payload = {
            "model": self.model_path,
            "query": query,
            "documents": documents,
            "top_n": top_k,
        }

        resp = requests.post(self.api_url, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        result = resp.json()

        # 兼容不同 API 格式
        reranked_docs = []
        for item in result.get("results", result.get("data", [])):
            idx = item.get("index", item.get("document_index", 0))
            score = item.get("relevance_score", item.get("score", 0))
            if 0 <= idx < len(docs):
                doc = docs[idx].copy()
                doc["rerank_score"] = float(score)
                reranked_docs.append(doc)

        return reranked_docs[:top_k]


class MultiRouteRetriever:
    """
    多路召回 + RRF 融合 + 精排检索器。

    支持把向量召回、关键词召回、不同 collection、不同过滤条件等多条路线
    并发执行，然后用 Reciprocal Rank Fusion 合并排序，最后用 Reranker 精排。

    使用示例：
        retriever = MultiRouteRetriever(
            routes=[...],
            top_k=5,
            reranker=BGEReranker(),
        )
        docs = await retriever.retriever("查询内容")
    """

    def __init__(
        self,
        routes: list[RetrieverRoute],
        top_k: int = 5,
        rrf_k: int = 60,
        query_variants: Optional[list[str]] = None,
        reranker: Optional[object] = None,
        rerank_top_k: int = 20,
    ):
        if not routes:
            raise ValueError("MultiRouteRetriever 至少需要一条检索路线")
        self.routes = routes
        self.top_k = top_k
        self.rrf_k = rrf_k
        self.query_variants = query_variants or []
        self.reranker = reranker
        self.rerank_top_k = rerank_top_k  # RRF 后送入 reranker 的数量

    # 非金融意图集合：与 Retriever.NON_FINANCE_INTENTS 同一清单（意图×语料域
    # 不匹配的判断属于 RAG 层；MultiRouteRetriever 作为组合器同步下沉，2026-09-17）。
    NON_FINANCE_INTENTS = Retriever.NON_FINANCE_INTENTS

    async def retrieve(
        self,
        query: str,
        top_k: Optional[int] = None,
        filter: Optional[dict] = None,
        intent: str = "",
    ) -> list[dict]:
        """执行多路检索 + RRF 融合 + 可选精排。

        :param intent: 用户意图分类（task 子类型，如 code/general/explain）。
            命中 NON_FINANCE_INTENTS → 与本库语料域不匹配，直接返回 []（不发起
            任何路线的检索调用），与 Retriever.retrieve 的门控语义一致。
        """
        if intent and intent in self.NON_FINANCE_INTENTS:
            logger.info("[MultiRouteRetriever] 意图=%s 与本库语料域不匹配，跳过检索", intent)
            return []
        # 1. 多路召回
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

        # 2. RRF 融合
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

        fused_docs = sorted(fused.values(), key=lambda x: x["score"], reverse=True)

        # 3. Reranker 精排（可选）
        if self.reranker and fused_docs:
            try:
                rerank_k = min(self.rerank_top_k, len(fused_docs))
                docs_to_rerank = fused_docs[:rerank_k]
                fused_docs = self.reranker.rerank(
                    query=query,
                    docs=docs_to_rerank,
                    top_k=top_k or self.top_k,
                )
            except Exception as e:
                logger.warning(f"[MultiRouteRetriever] Reranker 失败，回退到 RRF 排序: {e}")

        # 4. 格式化输出
        result_docs = fused_docs[:top_k or self.top_k]
        for doc in result_docs:
            doc["score"] = round(doc.get("rerank_score", doc.get("score", 0)), 6)
            doc["metadata"] = {
                **(doc.get("metadata") or {}),
                "retrieval_routes": doc.pop("routes", []),
                "raw_scores": doc.pop("raw_scores", []),
            }
        return result_docs
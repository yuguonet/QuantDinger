# -*- coding: utf-8 -*-
"""
Core Router — 语义路由引擎核心。

移植自 aurelio-labs/semantic-router 的路由内核，精简为：
- Route 定义（name + utterances + metadata）
- 向量索引（numpy，支持增删改查）
- cosine similarity 匹配 + 阈值过滤
- 多 utterance 聚合评分（mean/max/sum）

移除了：pydantic 依赖、异步、Pinecone/Qdrant/BM25/CLIP、
         动态路由、配置文件序列化等非核心功能。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from numpy.linalg import norm

from app.agent.router.encoder import BaseEncoder, create_encoder

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 1. 数据结构
# ═══════════════════════════════════════════════════════════════

@dataclass
class Route:
    """一条路由规则。

    Attributes:
        name: 路由名称，如 "finance/stock_analysis"
        utterances: 示例语句列表，用于构建向量空间
        description: 路由描述（可选，用于日志/调试）
        score_threshold: 该路由的最低置信度阈值（None 使用全局默认）
        metadata: 附加元数据（如 domain、intent、tools 等）
    """
    name: str
    utterances: List[str]
    description: str = ""
    score_threshold: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def domain(self) -> str:
        """从 name 提取 domain（如 'finance/stock_analysis' → 'finance'）"""
        return self.name.split("/")[0] if "/" in self.name else self.name

    @property
    def intent(self) -> str:
        """从 name 提取 intent（如 'finance/stock_analysis' → 'stock_analysis'）"""
        return self.name.split("/", 1)[1] if "/" in self.name else self.name


@dataclass
class RouteResult:
    """路由结果。

    Attributes:
        route_name: 匹配到的路由名（None 表示未命中）
        domain: 领域
        intent: 意图
        confidence: 置信度 (0~1)
        metadata: 路由附带的元数据
        all_scores: 所有路由的得分（调试用）
        elapsed_ms: 路由耗时（毫秒）
    """
    route_name: Optional[str] = None
    domain: str = "chat"
    intent: str = ""
    confidence: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)
    all_scores: Dict[str, float] = field(default_factory=dict)
    elapsed_ms: float = 0.0

    @property
    def matched(self) -> bool:
        return self.route_name is not None


# ═══════════════════════════════════════════════════════════════
# 2. 向量索引（LocalIndex 精简版）
# ═══════════════════════════════════════════════════════════════

class LocalIndex:
    """基于 numpy 的本地向量索引。"""

    def __init__(self):
        self.embeddings: Optional[np.ndarray] = None   # (N, dim)
        self.route_names: Optional[np.ndarray] = None   # (N,)
        self.utterances: Optional[np.ndarray] = None    # (N,)

    def add(
        self,
        embeddings: np.ndarray,
        route_names: List[str],
        utterances: List[str],
    ):
        """添加向量到索引。"""
        emb = np.array(embeddings, dtype=np.float32)
        names = np.array(route_names)
        utts = np.array(utterances)

        if self.embeddings is None:
            self.embeddings = emb
            self.route_names = names
            self.utterances = utts
        else:
            self.embeddings = np.concatenate([self.embeddings, emb])
            self.route_names = np.concatenate([self.route_names, names])
            self.utterances = np.concatenate([self.utterances, utts])

    def query(
        self,
        vector: np.ndarray,
        top_k: int = 5,
    ) -> Tuple[np.ndarray, List[str]]:
        """查询最相似的 top_k 条记录。

        Returns:
            (scores, route_names) — 分数和对应的路由名
        """
        if self.embeddings is None:
            return np.array([]), []

        # cosine similarity
        sim = self._similarity(vector, self.embeddings)
        # top-k
        k = min(top_k, sim.shape[0])
        idx = np.argpartition(sim, -k)[-k:]
        scores = sim[idx]
        # 按分数降序
        order = np.argsort(-scores)
        return scores[order], [self.route_names[i] for i in idx[order]]

    def is_ready(self) -> bool:
        return self.embeddings is not None and len(self.embeddings) > 0

    def __len__(self) -> int:
        return len(self.embeddings) if self.embeddings is not None else 0

    @staticmethod
    def _similarity(xq: np.ndarray, index: np.ndarray) -> np.ndarray:
        """计算 xq 与 index 中每个向量的 cosine similarity。"""
        xq_norm = norm(xq)
        if xq_norm == 0:
            return np.zeros(len(index))
        index_norms = norm(index, axis=1)
        # 避免除零
        denom = index_norms * xq_norm
        denom[denom == 0] = 1e-10
        return np.dot(index, xq) / denom


# ═══════════════════════════════════════════════════════════════
# 3. 语义路由器
# ═══════════════════════════════════════════════════════════════

class SemanticIntentRouter:
    """语义意图路由器。

    核心流程：
    1. 初始化时将所有 Route 的 utterances 编码为向量，存入 LocalIndex
    2. 路由时将用户消息编码，与索引做 cosine similarity
    3. 按 Route 聚合分数（mean），取最高分
    4. 高于阈值 → 返回匹配结果；低于阈值 → 返回未命中

    Args:
        encoder: 编码器实例或配置
        routes: Route 列表
        default_threshold: 默认置信度阈值
        aggregation: 多 utterance 聚合方式 ("mean" | "max" | "sum")
        top_k: 查询时保留的 top-k 候选数
        context_boost: 上下文加成系数 (0~1)
    """

    def __init__(
        self,
        encoder: BaseEncoder = None,
        routes: List[Route] = None,
        default_threshold: float = 0.45,
        aggregation: str = "mean",
        top_k: int = 10,
        context_boost: float = 0.1,
        encoder_backend: str = "auto",
        encoder_model: str = None,
    ):
        self.default_threshold = default_threshold
        self.aggregation = aggregation
        self.top_k = top_k
        self.context_boost = context_boost
        self.routes: List[Route] = []
        self.index = LocalIndex()
        self._route_map: Dict[str, Route] = {}

        # 编码器
        if encoder is not None:
            self.encoder = encoder
        else:
            self.encoder = create_encoder(
                backend=encoder_backend, model_name=encoder_model
            )

        # 加载路由
        if routes:
            self.add_routes(routes)

    def add_routes(self, routes: List[Route]):
        """批量添加路由规则并构建索引。"""
        t0 = time.time()
        all_utterances = []
        all_route_names = []

        for route in routes:
            self.routes.append(route)
            self._route_map[route.name] = route
            for utt in route.utterances:
                all_utterances.append(utt)
                all_route_names.append(route.name)

        if not all_utterances:
            logger.warning("[Router] 没有 utterance 可索引")
            return

        # 批量编码
        embeddings = self.encoder(all_utterances)
        self.index.add(embeddings, all_route_names, all_utterances)

        elapsed = (time.time() - t0) * 1000
        logger.info(
            "[Router] 已索引 %d 条路由，%d 条 utterance，耗时 %.0fms",
            len(routes), len(all_utterances), elapsed,
        )

    def route(
        self,
        query: str,
        session_id: str = None,
        context_domain: str = None,
    ) -> RouteResult:
        """对用户消息进行语义路由。

        Args:
            query: 用户消息
            session_id: 会话 ID（用于上下文加成）
            context_domain: 上一轮对话的领域（用于上下文加成）

        Returns:
            RouteResult
        """
        if not query or not query.strip():
            return RouteResult(domain="chat", intent="empty", confidence=1.0)

        if not self.index.is_ready():
            logger.warning("[Router] 索引未就绪，降级")
            return RouteResult()

        t0 = time.time()

        # 1. 编码查询
        query_vec = self.encoder([query.strip()])[0]

        # 2. 向量检索 top-k
        scores, route_names = self.index.query(query_vec, top_k=self.top_k)

        if len(scores) == 0:
            return RouteResult(elapsed_ms=(time.time() - t0) * 1000)

        # 3. 按 Route 聚合分数
        scored_routes = self._aggregate_scores(scores, route_names)

        # 4. 上下文加成
        if context_domain and self.context_boost > 0:
            scored_routes = self._apply_context_boost(scored_routes, context_domain)

        # 5. 取最高分，检查阈值
        scored_routes.sort(key=lambda x: x[1], reverse=True)
        best_name, best_score = scored_routes[0]

        route = self._route_map.get(best_name)
        threshold = route.score_threshold if route and route.score_threshold else self.default_threshold

        elapsed = (time.time() - t0) * 1000

        # 构建所有分数字典（调试用）
        all_scores = {name: round(float(score), 4) for name, score in scored_routes[:5]}

        if best_score >= threshold and route:
            logger.info(
                "[Router] 命中: %s (%.3f >= %.3f) %.0fms | %s",
                best_name, best_score, threshold, elapsed, query[:50],
            )
            return RouteResult(
                route_name=best_name,
                domain=route.domain,
                intent=route.intent,
                confidence=float(best_score),
                metadata=route.metadata,
                all_scores=all_scores,
                elapsed_ms=elapsed,
            )
        else:
            logger.info(
                "[Router] 未命中: best=%.3f < %.3f (%s) %.0fms | %s",
                best_score, threshold, best_name, elapsed, query[:50],
            )
            return RouteResult(
                all_scores=all_scores,
                elapsed_ms=elapsed,
            )

    def _aggregate_scores(
        self, scores: np.ndarray, route_names: List[str]
    ) -> List[Tuple[str, float]]:
        """将 utterance 级别的分数按 Route 聚合。"""
        by_route: Dict[str, List[float]] = {}
        for score, name in zip(scores, route_names):
            by_route.setdefault(name, []).append(float(score))

        result = []
        for name, route_scores in by_route.items():
            if self.aggregation == "mean":
                agg = sum(route_scores) / len(route_scores)
            elif self.aggregation == "max":
                agg = max(route_scores)
            elif self.aggregation == "sum":
                agg = sum(route_scores)
            else:
                agg = sum(route_scores) / len(route_scores)
            result.append((name, agg))
        return result

    def _apply_context_boost(
        self,
        scored_routes: List[Tuple[str, float]],
        context_domain: str,
    ) -> List[Tuple[str, float]]:
        """对与上一轮同 domain 的路由施加分数加成。

        避免上下文切换时的误分类：如果用户刚在聊股票，
        接下来的消息更可能延续该话题。
        """
        boosted = []
        for name, score in scored_routes:
            route = self._route_map.get(name)
            if route and route.domain == context_domain:
                score = min(score + self.context_boost, 1.0)
            boosted.append((name, score))
        return boosted

    def get_route(self, name: str) -> Optional[Route]:
        """按名称获取路由定义。"""
        return self._route_map.get(name)

    def list_routes(self) -> List[Dict[str, Any]]:
        """列出所有路由（调试用）。"""
        return [
            {
                "name": r.name,
                "domain": r.domain,
                "intent": r.intent,
                "utterances": len(r.utterances),
                "threshold": r.score_threshold,
            }
            for r in self.routes
        ]

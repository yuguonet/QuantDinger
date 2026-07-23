"""
RAG 检索增强层

提供向量化存储和语义检索能力。
"""
from rag.embeddings import EmbeddingBase, DashScopeEmbedding, OpenAIEmbedding, LlamaCppEmbedding, EmbeddingModel
from rag.vector_store import QdrantVectorStore, VectorStoreBase
from rag.pg_vector_store import PgVectorStore
from rag.retriever import KeywordRetriever, MultiRouteRetriever, Retriever, RetrieverRoute, BGEReranker, ChatHistoryRetriever
from rag.postgres_fts import PostgresFTSRetriever

__all__ = [
    "EmbeddingBase",
    "DashScopeEmbedding",
    "OpenAIEmbedding",
    "LlamaCppEmbedding",
    "EmbeddingModel",
    "VectorStoreBase",
    "QdrantVectorStore",
    "PgVectorStore",
    "Retriever",
    "KeywordRetriever",
    "RetrieverRoute",
    "MultiRouteRetriever",
    "BGEReranker",
    "PostgresFTSRetriever",
    "ChatHistoryRetriever",
]

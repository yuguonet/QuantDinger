# -*- coding: utf-8 -*-
"""
Encoder — embedding 编码器，零新增依赖。

优先级（自动选择，按精度排序）：
1. 远程 Embedding API（复用已有 LLM provider 的 api_key/base_url，零新增依赖）
2. HashEncoder（纯 Python + numpy，零依赖，精度一般）

注意：sentence-transformers 本地模式已移除（需 2GB+ torch，太重）。
如需本地离线，建议用 ONNX Runtime 方案（见末尾注释）。
"""
from __future__ import annotations

import hashlib
import logging
import os
from typing import List, Optional

import numpy as np

logger = logging.getLogger(__name__)


class BaseEncoder:
    """编码器基类。"""
    dimension: int = 384

    def encode(self, texts: List[str]) -> np.ndarray:
        """将文本列表编码为向量矩阵。shape: (len(texts), dimension)"""
        raise NotImplementedError

    def __call__(self, texts: List[str]) -> np.ndarray:
        return self.encode(texts)


# ═══════════════════════════════════════════════════════════════
# 1. 远程 Embedding API（推荐 — 零新增依赖，高精度）
# ═══════════════════════════════════════════════════════════════

# 各 provider 的默认 embedding 模型和维度
_PROVIDER_EMBED_DEFAULTS = {
    "openai": {"model": "text-embedding-3-small", "dimension": 1536},
    "deepseek": {"model": "text-embedding-v3", "dimension": 1024},
    "openrouter": {"model": "openai/text-embedding-3-small", "dimension": 1536},
    "google": {"model": "text-embedding-004", "dimension": 768},
    "ollama": {"model": "nomic-embed-text", "dimension": 768},
}

# OpenAI 兼容的 provider（走 /v1/embeddings）
_OPENAI_COMPAT_PROVIDERS = {"openai", "deepseek", "openrouter", "grok", "x-ai", "ollama"}


class RemoteEmbeddingEncoder(BaseEncoder):
    """通过 OpenAI 兼容的 /v1/embeddings 接口获取向量。

    支持：OpenAI、DeepSeek、OpenRouter、Google、任何 OpenAI 兼容端点。
    复用 QuantDinger 已有的 LLMService 配置，零新增依赖（只用 requests）。
    """

    def __init__(
        self,
        api_key: str = None,
        base_url: str = None,
        model: str = None,
        dimension: int = None,
    ):
        import requests as _requests
        self._requests = _requests

        # base_url 先检测（Ollama 判断需要）
        self.base_url = (base_url or self._detect_base_url()).rstrip("/")
        # api_key 后检测（Ollama 不需要 key）
        self.api_key = api_key or self._detect_api_key()
        self.model = model or self._detect_model()
        self.dimension = dimension or self._detect_dimension()

        if not self.api_key:
            raise ValueError(
                "RemoteEmbeddingEncoder 需要 api_key。"
                "请设置 OPENAI_API_KEY / DEEPSEEK_API_KEY，"
                "或确保 Ollama 在 localhost:11434 运行。"
            )

        logger.info(
            "[Encoder] 远程 embedding: %s/%s (dim=%d)",
            self._resolve_embedding_url(), self.model, self.dimension,
        )

    def _detect_api_key(self) -> str:
        """从环境变量检测 API key。Ollama 不需要 key，返回占位值。"""
        # Ollama 不需要 API key
        url = self.base_url.lower()
        if any(k in url for k in ("localhost:11434", "127.0.0.1:11434", "ollama")):
            return "ollama"  # 占位值，Ollama 不校验
        for key in ["OPENAI_API_KEY", "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY"]:
            val = os.getenv(key, "")
            if val:
                return val
        # 尝试从 LLMService 获取
        try:
            from app.services.llm import LLMService
            svc = LLMService()
            return svc.get_api_key()
        except Exception:
            return ""

    def _detect_base_url(self) -> str:
        """从环境变量或 LLMService 检测 base URL。"""
        for key in ["OPENAI_BASE_URL", "LLM_BASE_URL"]:
            val = os.getenv(key, "")
            if val:
                return val
        try:
            from app.services.llm import LLMService
            svc = LLMService()
            return svc.get_base_url()
        except Exception:
            return "https://api.openai.com/v1"

    def _resolve_embedding_url(self) -> str:
        """构造 embedding API 的完整 URL。

        Ollama: base_url 可能是 http://localhost:11434，需要拼 /v1/embeddings
        其他:   base_url 通常已含 /v1，直接拼 /embeddings
        """
        url = self.base_url.lower()
        # Ollama: 确保有 /v1 前缀
        if any(k in url for k in ("localhost:11434", "127.0.0.1:11434", "ollama")):
            if not url.endswith("/v1") and not url.endswith("/v1/"):
                return f"{self.base_url}/v1/embeddings"
        return f"{self.base_url}/embeddings"

    def _detect_provider(self) -> str:
        """检测当前 provider。"""
        try:
            from app.services.llm import LLMService
            svc = LLMService()
            return svc.provider.value if hasattr(svc.provider, 'value') else str(svc.provider)
        except Exception:
            url = self.base_url.lower()
            if any(k in url for k in ("localhost:11434", "127.0.0.1:11434", "ollama")):
                return "ollama"
            if "deepseek" in url:
                return "deepseek"
            if "openrouter" in url:
                return "openrouter"
            if "google" in url or "generativelanguage" in url:
                return "google"
            return "openai"

    def _detect_model(self) -> str:
        """检测默认 embedding 模型。"""
        provider = self._detect_provider()
        defaults = _PROVIDER_EMBED_DEFAULTS.get(provider, _PROVIDER_EMBED_DEFAULTS["openai"])
        return os.getenv("EMBEDDING_MODEL", defaults["model"])

    def _detect_dimension(self) -> int:
        """检测默认维度。"""
        provider = self._detect_provider()
        defaults = _PROVIDER_EMBED_DEFAULTS.get(provider, _PROVIDER_EMBED_DEFAULTS["openai"])
        return int(os.getenv("EMBEDDING_DIMENSION", str(defaults["dimension"])))

    def encode(self, texts: List[str]) -> np.ndarray:
        """调用 /v1/embeddings 接口。支持自动分批（API 通常限制 2048 条/次）。"""
        all_embeddings = []
        batch_size = 512
        url = self._resolve_embedding_url()

        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            resp = self._requests.post(
                url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={"model": self.model, "input": batch},
                timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json()["data"]
            data.sort(key=lambda x: x["index"])
            all_embeddings.extend(item["embedding"] for item in data)

        emb = np.array(all_embeddings, dtype=np.float32)
        # L2 归一化（与 HashEncoder 一致，方便统一 cosine similarity 计算）
        norms = np.linalg.norm(emb, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        emb /= norms
        # 更新实际维度
        if len(emb) > 0:
            self.dimension = emb.shape[1]
        return emb


# ═══════════════════════════════════════════════════════════════
# 2. HashEncoder（降级 — 纯 numpy，零新增依赖）
# ═══════════════════════════════════════════════════════════════

class HashEncoder(BaseEncoder):
    """基于字符 n-gram 哈希的降级编码器。

    零外部依赖（只用 hashlib + numpy），精度低于语义编码器，
    但对中文关键词匹配仍有不错的效果。
    """

    def __init__(self, dimension: int = 384, ngram_range: tuple = (2, 4)):
        self.dimension = dimension
        self.ngram_range = ngram_range
        logger.info("[Encoder] HashEncoder (dim=%d, ngram=%s) — 降级模式", dimension, ngram_range)

    def _text_to_ngrams(self, text: str) -> List[str]:
        text = text.lower().strip()
        ngrams = []
        for n in range(self.ngram_range[0], self.ngram_range[1] + 1):
            for i in range(len(text) - n + 1):
                ngrams.append(text[i:i + n])
        return ngrams

    def encode(self, texts: List[str]) -> np.ndarray:
        vectors = np.zeros((len(texts), self.dimension), dtype=np.float32)
        for i, text in enumerate(texts):
            ngrams = self._text_to_ngrams(text)
            for ng in ngrams:
                h = int(hashlib.md5(ng.encode("utf-8")).hexdigest(), 16)
                idx = h % self.dimension
                sign = 1.0 if (h // self.dimension) % 2 == 0 else -1.0
                vectors[i, idx] += sign
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        vectors /= norms
        return vectors


# ═══════════════════════════════════════════════════════════════
# 工厂函数
# ═══════════════════════════════════════════════════════════════

def create_encoder(
    backend: str = "auto",
    model_name: str = None,
    api_key: str = None,
    base_url: str = None,
) -> BaseEncoder:
    """创建编码器实例。

    Args:
        backend:
            "auto"    — 优先远程 API，无 key 则降级 HashEncoder
            "remote"  — 强制远程 API（无 key 则报错）
            "hash"    — 强制 HashEncoder
        model_name: 远程 embedding 模型名
        api_key: 远程 API key
        base_url: 远程 API base URL

    Returns:
        BaseEncoder 实例
    """
    if backend == "hash":
        return HashEncoder()

    if backend in ("auto", "remote"):
        try:
            return RemoteEmbeddingEncoder(
                api_key=api_key, base_url=base_url, model=model_name,
            )
        except (ValueError, ImportError) as e:
            if backend == "remote":
                raise
            logger.warning(
                "[Encoder] 远程 embedding 不可用 (%s)，降级到 HashEncoder", e
            )
            return HashEncoder()

    raise ValueError(f"未知的 encoder backend: {backend}")


# ═══════════════════════════════════════════════════════════════
# 注：如需本地离线 embedding（不调 API），有两个轻量方案：
#
# 方案 A — ONNX Runtime（推荐，~65MB，无 torch）
#   pip install onnxruntime tokenizers
#   模型: sentence-transformers/all-MiniLM-L6-v2 的 ONNX 量化版
#   从 HuggingFace 下载 onnx/ 目录即可
#
# 方案 B — fastembed（~50MB，基于 ONNX）
#   pip install fastembed
#   from fastembed import TextEmbedding
#   model = TextEmbedding("sentence-transformers/all-MiniLM-L6-v2")
#   embeddings = list(model.embed(texts))
#
# 两种方案都不需要 PyTorch，比 sentence-transformers 轻 30 倍。
# ═══════════════════════════════════════════════════════════════

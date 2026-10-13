"""
Embedding 抽象层

支持多种 Embedding 模型：
- DashScope text-embedding-v2
- OpenAI text-embedding-ada-002 / text-embedding-3-small
"""
import logging
from abc import ABC, abstractmethod
from typing import Optional

logger = logging.getLogger(__name__)


def _align_vectors(texts: list[str], vectors: list[list[float]],
                   valid_texts: list[str]) -> list[list[float]]:
    """把「过滤空文本后」的向量结果填回**原始入参的位置**。

    ⚠️ 契约（务必保持）：`embed_documents` 的返回长度必须等于入参长度，
    空文本 / 失败位置返回 `[]`。
    早期实现先 `if t and t.strip()` 过滤再请求，返回条数少于入参 ⇒ 下游
    `zip(texts, vectors)` 按下标配对时**整体错位**，文档与向量静默错配
    （连 metadata 一起错），且不报任何错。

    若上游返回条数与请求条数不一致，说明发生了无法归因的错位 —— 宁可全空
    也不按猜测对齐（避免「编造数据」）。
    """
    src = list(texts or [])
    if len(vectors) != len(valid_texts):
        logger.error(
            "[Embedding] 返回条数 %d 与请求条数 %d 不一致，放弃本批（不按猜测对齐）",
            len(vectors), len(valid_texts))
        return [[] for _ in src]
    it = iter(vectors)
    return [next(it) if (t and str(t).strip()) else [] for t in src]


class EmbeddingBase(ABC):
    """Embedding 基类"""

    @abstractmethod
    def embed_query(self, text: str) -> list[float]:
        """单条文本向量化"""
        ...

    @abstractmethod
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """批量文本向量化。

        Returns:
            与 `texts` **等长**的向量列表；空文本或失败位置为 `[]`。
            下游必须跳过空向量，不得按下标盲配。
        """
        ...


class DashScopeEmbedding(EmbeddingBase):
    """
    DashScope Embedding 实现

    使用阿里云 text-embedding-v2 模型，向量维度 1536。
    """

    def __init__(self, model: str = "text-embedding-v2", api_key: str = ""):
        self.model = model
        self.api_key = api_key

    def embed_query(self, text: str) -> list[float]:
        """单条文本向量化"""
        text = (text or "").strip()
        if not text:
            return []

        # 限制长度
        if len(text) > 2048:
            text = text[:2048]

        try:
            import dashscope
            from dashscope import TextEmbedding

            dashscope.api_key = self.api_key
            response = TextEmbedding.call(model=self.model, input=text)

            if response.status_code == 200:
                return response.output["embeddings"][0]["embedding"]
            else:
                logger.error(f"Embedding 失败: {getattr(response, 'message', response)}")
                return []
        except ImportError:
            logger.error("dashscope 未安装")
            return []
        except Exception as e:
            logger.error(f"Embedding 异常: {e}")
            return []

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """批量文本向量化（返回与 texts 等长，空/失败位置为 []）。"""
        src = list(texts or [])
        valid_texts = [t.strip()[:4096] for t in src if t and t.strip()]
        if not valid_texts:
            return [[] for _ in src]

        try:
            import dashscope
            from dashscope import TextEmbedding

            dashscope.api_key = self.api_key
            response = TextEmbedding.call(model=self.model, input=valid_texts)

            if response.status_code == 200:
                vecs = [item["embedding"] for item in response.output["embeddings"]]
                return _align_vectors(src, vecs, valid_texts)
            else:
                logger.error(f"批量 Embedding 失败: {getattr(response, 'message', response)}")
                return [[] for _ in src]
        except Exception as e:
            logger.error(f"批量 Embedding 异常: {e}")
            return [[] for _ in src]


class OpenAIEmbedding(EmbeddingBase):
    """
    OpenAI Embedding 实现

    支持 text-embedding-3-small / text-embedding-ada-002 等模型。
    也兼容其他支持 OpenAI Embedding API 的服务。
    """

    def __init__(self, model: str = "text-embedding-3-small", api_key: str = "", base_url: Optional[str] = None):
        self.model = model
        self.api_key = api_key
        self.base_url = base_url

    def _get_client(self):
        from openai import OpenAI
        kwargs = {"api_key": self.api_key}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        return OpenAI(**kwargs)

    def embed_query(self, text: str) -> list[float]:
        """单条文本向量化"""
        try:
            client = self._get_client()
            response = client.embeddings.create(model=self.model, input=text)
            return response.data[0].embedding
        except Exception as e:
            logger.error(f"OpenAI Embedding 异常: {e}")
            return []

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """批量文本向量化（返回与 texts 等长，空/失败位置为 []）。"""
        src = list(texts or [])
        try:
            client = self._get_client()
            response = client.embeddings.create(model=self.model, input=src)
            vecs = [item.embedding for item in response.data]
            # 请求未过滤空文本，向量应与入参一一对应；不对应则不猜、不配。
            return _align_vectors(src, vecs, src)
        except Exception as e:
            logger.error(f"OpenAI 批量 Embedding 异常: {e}")
            return [[] for _ in src]


class LlamaCppEmbedding(OpenAIEmbedding):
    """
    llama.cpp 本地 Embedding 实现。

    复用 OpenAIEmbedding，base_url 指向 llama.cpp 的 /v1 端点。
    llama.cpp 加载 embedding 模型后，/v1/embeddings 接口兼容 OpenAI API。

    使用方式：
        # llama.cpp 启动 embedding 模型（和 chat 模型共用端口或独立端口）
        # ./llama-server --model bge-m3-Q8_0.gguf --port 8080 --embedding
        embedding = LlamaCppEmbedding(
            model="bge-m3-Q8_0",
            base_url="http://localhost:8080/v1",
        )
    """

    def __init__(self, model: str = "", base_url: str = ""):
        super().__init__(model=model, api_key="not-needed", base_url=base_url)


class EmbeddingModel:
    """
    Embedding 工厂兼容层。

    README 和示例可以使用 EmbeddingModel(provider=...) 创建具体实现，
    实际返回对象实现 EmbeddingBase 接口。
    """

    def __new__(
        cls,
        provider: str = "dashscope",
        model: str = "",
        api_key: str = "",
        base_url: Optional[str] = None,
        **kwargs,
    ) -> EmbeddingBase:
        provider = (provider or "dashscope").lower()
        if provider == "dashscope":
            return DashScopeEmbedding(
                model=model or "text-embedding-v2",
                api_key=api_key,
            )
        if provider == "openai":
            return OpenAIEmbedding(
                model=model or "text-embedding-3-small",
                api_key=api_key,
                base_url=base_url,
            )
        if provider == "llamacpp":
            return LlamaCppEmbedding(
                model=model or "bge-m3-Q8_0",
                base_url=base_url or "http://localhost:8080/v1",
            )
        raise ValueError(f"不支持的 Embedding Provider: {provider}")

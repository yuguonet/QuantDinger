"""
示例 4: RAG 知识库问答 Agent

演示如何创建带多路召回的 RAG 检索增强 Agent。
运行: python -m examples.rag_example
"""

import asyncio
from llm import LLMFactory
from memory import LocalMemory
from rag import (
    EmbeddingModel,
    KeywordRetriever,
    MultiRouteRetriever,
    QdrantVectorStore,
    Retriever,
    RetrieverRoute,
)
from agents import ChatAgent
from config.loader import get_settings


async def main():
    settings = get_settings(config_dir="config")

    # 1. 创建 LLM
    llm = LLMFactory.create(
        provider=settings.llm.provider,
        model=settings.llm.model,
        api_key=settings.llm.api_key,
    )

    # 2. 创建 Embedding + 向量库 + 检索器
    embedding = EmbeddingModel(
        provider=settings.rag.embedding_provider,
        model=settings.rag.embedding_model,
        api_key=settings.rag.embedding_api_key,
    )

    vector_store = QdrantVectorStore(
        collection_name=settings.rag.collection_name,
        embedding=embedding,
        host=settings.rag.qdrant_host,
        port=settings.rag.qdrant_port,
        grpc_port=settings.rag.qdrant_grpc_port,
        prefer_grpc=settings.rag.qdrant_use_grpc,
        dimension=settings.rag.embedding_dimension,
        score_threshold=settings.rag.score_threshold,
    )

    # 3. 准备一些文档 (实际项目中从文件/数据库加载)
    docs = [
        {
            "text": "Python 3.12 引入了类型参数语法 (PEP 695)，使泛型定义更简洁。",
            "metadata": {"source": "python_docs"},
        },
        {
            "text": "FastAPI 是一个现代化的 Python Web 框架，基于 Starlette 和 Pydantic。",
            "metadata": {"source": "fastapi_docs"},
        },
        {
            "text": "Qdrant 是一个高性能的向量数据库，支持过滤搜索和 gRPC 协议。",
            "metadata": {"source": "qdrant_docs"},
        },
    ]

    print("正在写入文档到向量库...")
    await vector_store.add_texts(
        texts=[doc["text"] for doc in docs],
        metadatas=[doc["metadata"] for doc in docs],
    )
    print(f"已写入 {len(docs)} 条文档\n")

    vector_retriever = Retriever(
        vector_store=vector_store,
        top_k=settings.rag.top_k,
        score_threshold=settings.rag.score_threshold,
    )
    keyword_retriever = KeywordRetriever(
        documents=[
            {"content": doc["text"], "metadata": doc["metadata"]}
            for doc in docs
        ],
        top_k=settings.rag.top_k,
    )

    retriever = MultiRouteRetriever(
        routes=[
            RetrieverRoute("vector", vector_retriever, weight=1.0),
            RetrieverRoute("keyword", keyword_retriever, weight=0.6),
        ],
        top_k=settings.rag.top_k,
    )

    # 4. 创建带 RAG 的 Agent
    memory = LocalMemory(max_history=10)
    agent = ChatAgent(
        llm=llm,
        memory=memory,
        retriever=retriever,
        system_prompt="你是一个知识库助手。根据检索到的上下文回答问题，如果上下文不包含答案就如实说明。",
    )

    # 5. 问答
    questions = [
        "Python 3.12 有什么新特性？",
        "FastAPI 是基于什么框架构建的？",
        "Qdrant 支持什么协议？",
    ]

    session_id = "rag_demo"
    for q in questions:
        print(f"问: {q}")
        resp = await agent.chat(q, session_id=session_id)
        print(f"答: {resp.content}\n")
        print(f"来源: {resp.sources}\n")


if __name__ == "__main__":
    asyncio.run(main())

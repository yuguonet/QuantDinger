"""
Memory 记忆系统

管理 Agent 的对话历史，支持多种后端。
"""
from .base import MemoryBase, MemoryMessage
from .local_memory import LocalMemory
from .redis_memory import RedisMemory
from .postgres_memory import PostgresMemory

__all__ = ["MemoryBase", "MemoryMessage", "LocalMemory", "RedisMemory", "PostgresMemory"]

"""
Memory 记忆系统

管理 Agent 的对话历史，支持多种后端。
"""
from memory.base import MemoryBase, MemoryMessage
from memory.local_memory import LocalMemory
from memory.redis_memory import RedisMemory

__all__ = ["MemoryBase", "MemoryMessage", "LocalMemory", "RedisMemory"]

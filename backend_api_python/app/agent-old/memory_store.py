# -*- coding: utf-8 -*-
"""
Memory Store — 长期记忆存储（Agent-Template memory.md 模式）。

职责：
  - 读写 memory.md（长期知识：用户偏好、项目约束、历史教训）
  - 提供结构化的 section 管理（按类别组织知识）
  - 支持追加、更新、查询

存储位置：
  {WORKSPACE_ROOT}/memory/{user_id}/memory.md

用法：
  from app.agent.memory_store import get_memory
  memory = get_memory("1")
  memory.add_preference("偏好简短回复")
  memory.add_lesson("茅台适合技术面分析")
  content = memory.get_content()  # 注入 prompt
"""
from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from app.agent.log import logger


MEMORY_ROOT = os.getenv(
    "MEMORY_ROOT",
    os.path.join(os.path.dirname(__file__), "..", "..", "workspaces", "memory"),
)

# 每个 section 最大条目数
MAX_ENTRIES_PER_SECTION = 20
# memory.md 最大字符数
MAX_MEMORY_CHARS = 5000


class MemoryStore:
    """单用户长期记忆存储。"""

    SECTIONS = {
        "preferences": "用户偏好",
        "lessons": "历史教训",
        "constraints": "项目约束",
        "stocks": "关注的股票",
        "patterns": "分析模式",
    }

    def __init__(self, user_id: str = "1"):
        self.user_id = user_id
        self.dir = Path(MEMORY_ROOT) / user_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "memory.md"
        self._lock = threading.Lock()
        self._cache: Optional[str] = None
        self._cache_ts: float = 0

    def _read_raw(self) -> str:
        """读取原始 markdown 内容。"""
        if not self.path.exists():
            return ""
        try:
            return self.path.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning("[Memory] 读取失败: %s", e)
            return ""

    def _write_raw(self, content: str) -> None:
        """写入 markdown 内容。"""
        try:
            self.path.write_text(content, encoding="utf-8")
            self._cache = content
            self._cache_ts = time.time()
        except Exception as e:
            logger.error("[Memory] 写入失败: %s", e)

    def _parse_sections(self, content: str) -> Dict[str, List[str]]:
        """解析 markdown 为 {section_key: [entries]}。"""
        sections: Dict[str, List[str]] = {}
        current_key = None

        for line in content.split("\n"):
            line = line.strip()
            if not line:
                continue

            # 匹配 ## 标题
            m = re.match(r'^##\s+(.+)$', line)
            if m:
                title = m.group(1).strip()
                # 反向查找 section key
                current_key = None
                for key, name in self.SECTIONS.items():
                    if name in title or key in title:
                        current_key = key
                        break
                if current_key and current_key not in sections:
                    sections[current_key] = []
                continue

            # 匹配 - 条目
            if current_key and line.startswith("- "):
                entry = line[2:].strip()
                if entry:
                    sections.setdefault(current_key, []).append(entry)

        return sections

    def _build_markdown(self, sections: Dict[str, List[str]]) -> str:
        """从 sections 构建 markdown。"""
        parts = ["# 长期记忆", ""]
        for key, name in self.SECTIONS.items():
            entries = sections.get(key, [])
            parts.append(f"## {name}")
            parts.append("")
            if entries:
                for entry in entries:
                    parts.append(f"- {entry}")
            else:
                parts.append(f"- （暂无）")
            parts.append("")
        return "\n".join(parts)

    def _load_sections(self) -> Dict[str, List[str]]:
        """加载并解析 sections。"""
        content = self._read_raw()
        if not content:
            return {}
        return self._parse_sections(content)

    def _save_sections(self, sections: Dict[str, List[str]]) -> None:
        """保存 sections 到文件。"""
        content = self._build_markdown(sections)
        # 截断检查
        if len(content) > MAX_MEMORY_CHARS:
            # 从每个 section 尾部删减
            for key in sections:
                while len(sections[key]) > 3 and len(self._build_markdown(sections)) > MAX_MEMORY_CHARS:
                    sections[key].pop(0)
        self._write_raw(self._build_markdown(sections))

    def _add_entry(self, section_key: str, entry: str) -> bool:
        """向指定 section 追加条目。去重，限制条目数。"""
        if not entry or not entry.strip():
            return False

        with self._lock:
            sections = self._load_sections()
            entries = sections.get(section_key, [])

            # 去重
            entry_clean = entry.strip()
            if entry_clean in entries:
                return False

            # 追加
            entries.append(entry_clean)

            # 限制条目数
            if len(entries) > MAX_ENTRIES_PER_SECTION:
                entries = entries[-MAX_ENTRIES_PER_SECTION:]

            sections[section_key] = entries
            self._save_sections(sections)

            logger.info("[Memory] %s → %s: %s", self.user_id, section_key, entry_clean[:50])
            return True

    # ── 公开接口 ──────────────────────────────────────────────

    def get_content(self) -> str:
        """获取 memory.md 全文（注入 prompt 用）。带缓存。"""
        now = time.time()
        if self._cache and now - self._cache_ts < 60:
            return self._cache
        with self._lock:
            content = self._read_raw()
            self._cache = content
            self._cache_ts = now
            return content

    def get_summary(self) -> str:
        """获取精简摘要（用于 prompt 注入，节省 token）。"""
        sections = self._load_sections()
        parts = []
        for key, entries in sections.items():
            if entries:
                name = self.SECTIONS.get(key, key)
                items = "; ".join(entries[-5:])  # 只取最近 5 条
                parts.append(f"{name}: {items}")
        return "\n".join(parts) if parts else ""

    def add_preference(self, preference: str) -> bool:
        """添加用户偏好。"""
        return self._add_entry("preferences", preference)

    def add_lesson(self, lesson: str) -> bool:
        """添加历史教训。"""
        return self._add_entry("lessons", lesson)

    def add_constraint(self, constraint: str) -> bool:
        """添加项目约束。"""
        return self._add_entry("constraints", constraint)

    def add_stock(self, stock_info: str) -> bool:
        """添加关注的股票信息。"""
        return self._add_entry("stocks", stock_info)

    def add_pattern(self, pattern: str) -> bool:
        """添加分析模式。"""
        return self._add_entry("patterns", pattern)

    def get_section(self, section_key: str) -> List[str]:
        """获取指定 section 的条目。"""
        sections = self._load_sections()
        return sections.get(section_key, [])

    def clear(self) -> None:
        """清空所有记忆。"""
        with self._lock:
            self._write_raw("")
            self._cache = None
            logger.info("[Memory] %s: 已清空", self.user_id)


# ── 单例缓存 ──────────────────────────────────────────────────
_stores: Dict[str, MemoryStore] = {}
_stores_lock = threading.Lock()


def get_memory(user_id: str = "1") -> MemoryStore:
    """获取用户 MemoryStore 单例。"""
    with _stores_lock:
        if user_id not in _stores:
            _stores[user_id] = MemoryStore(user_id)
        return _stores[user_id]

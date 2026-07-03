# -*- coding: utf-8 -*-
"""
skill_loader.py — Anthropic SKILL 标准渐进式披露加载器 (Progressive Disclosure)

三级加载机制:
  Level 1: 元数据 (name + description)     — 始终在上下文，~100 tokens/skill
  Level 2: SKILL.md body (执行指令)         — 意图匹配后按需加载，≤5000 词
  Level 3: references/ scripts/ assets/     — 执行时按需读取，无限制

分段加载:
  SKILL.md body 按 ## 标题分段，planner 可按步骤加载对应段落，
  避免一次性塞入整个 SKILL.md。

依赖:
  - qd_skills.QDSkillAdapter (已有) — 负责 Level 1 扫描和元数据管理
  - 本模块负责 Level 2/3 的加载逻辑

使用方式:
    from app.agent.skills.skill_loader import SkillLoader

    loader = SkillLoader(adapter)

    # Level 1: system prompt 注入（始终在上下文）
    catalog = loader.get_catalog_for_prompt()

    # Level 2: 按需加载完整 body 或分段
    body = loader.load_body("market-screener")
    sections = loader.load_sections("market-screener")
    section = loader.load_section("market-screener", "Phase 1")

    # Level 3: 加载引用文件
    content = loader.load_resource("market-screener", "references/xxx.md")
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
#  数据结构
# ═══════════════════════════════════════════════════════════════

@dataclass
class SkillSection:
    """SKILL.md 中的一个段落（按 ## 标题切分）。"""
    heading: str           # 段落标题（不含 ##）
    level: int             # markdown 标题层级 (2=##, 3=###)
    content: str           # 段落内容（含标题行）
    char_offset: int       # 在原始 body 中的字符偏移


@dataclass
class SkillDocument:
    """Level 2 加载后的完整 skill 文档。"""
    name: str
    body: str                              # SKILL.md 完整正文
    base_path: Path                        # skill 目录路径（Level 3 相对路径基准）
    sections: List[SkillSection] = field(default_factory=list)
    _resource_cache: Dict[str, str] = field(default_factory=dict)

    def get_section(self, heading_keyword: str) -> Optional[SkillSection]:
        """按标题关键词模糊匹配段落。"""
        kw = heading_keyword.lower()
        for sec in self.sections:
            if kw in sec.heading.lower():
                return sec
        return None

    def get_section_by_index(self, index: int) -> Optional[SkillSection]:
        """按索引获取段落。"""
        if 0 <= index < len(self.sections):
            return self.sections[index]
        return None


# ═══════════════════════════════════════════════════════════════
#  加载器
# ═══════════════════════════════════════════════════════════════

class SkillLoader:
    """
    Anthropic SKILL 标准渐进式披露加载器。

    与 QDSkillAdapter 的关系:
      - QDSkillAdapter 负责扫描发现 + Level 1 元数据
      - SkillLoader 负责 Level 2/3 的加载和分段逻辑
    """

    def __init__(self, adapter=None, skills_dirs: List[str] = None):
        """
        Args:
            adapter: QDSkillAdapter 实例（复用已有的技能发现）
            skills_dirs: 技能目录列表（adapter 为 None 时使用）
        """
        self._adapter = adapter
        self._docs: Dict[str, SkillDocument] = {}

        # 从 adapter 获取技能目录
        if adapter is None:
            from app.agent.llm.qd_skills import QDSkillAdapter
            self._adapter = QDSkillAdapter(skills_dirs)

    # ── Level 1: 元数据（始终在上下文）─────────────────────────

    def get_catalog_for_prompt(self) -> str:
        """
        Level 1: 生成技能目录 XML，注入 system prompt。
        只包含 name + description，约 100 tokens/skill。
        """
        if self._adapter is None:
            return ""
        return self._adapter.get_catalog_text()

    def get_skill_descriptions(self) -> List[Dict[str, str]]:
        """Level 1: 返回所有 skill 的 name + description 列表。"""
        if self._adapter is None:
            return []
        return self._adapter.list_skills()

    # ── Level 2: SKILL.md body（按需加载）───────────────────────

    def load_body(self, skill_name: str) -> Optional[str]:
        """
        Level 2: 加载 SKILL.md 完整正文。
        触发时机: planner 选中 skill 后，agent 需要执行指令时。
        """
        doc = self._load_doc(skill_name)
        return doc.body if doc else None

    def load_sections(self, skill_name: str) -> List[SkillSection]:
        """
        Level 2 分段: 返回 SKILL.md 按 ## 标题切分的所有段落。
        planner 可根据当前步骤选择加载哪个段落。
        """
        doc = self._load_doc(skill_name)
        return doc.sections if doc else []

    def load_section(self, skill_name: str, heading_keyword: str) -> Optional[str]:
        """
        Level 2 分段: 按标题关键词加载单个段落。
        用于 planner→agent 循环中按步骤加载。

        Args:
            skill_name: 技能名称
            heading_keyword: 标题关键词（模糊匹配，如 "Phase 1"、"调用方式"）

        Returns:
            段落内容（含标题），未找到返回 None
        """
        doc = self._load_doc(skill_name)
        if not doc:
            return None
        section = doc.get_section(heading_keyword)
        return section.content if section else None

    def load_section_by_index(self, skill_name: str, index: int) -> Optional[str]:
        """
        Level 2 分段: 按索引加载段落。
        用于顺序执行场景。

        Args:
            skill_name: 技能名称
            index: 段落索引（从 0 开始）

        Returns:
            段落内容，越界返回 None
        """
        doc = self._load_doc(skill_name)
        if not doc:
            return None
        section = doc.get_section_by_index(index)
        return section.content if section else None

    def get_section_headings(self, skill_name: str) -> List[str]:
        """返回 skill 的所有段落标题，供 planner 选择。"""
        doc = self._load_doc(skill_name)
        if not doc:
            return []
        return [s.heading for s in doc.sections]

    # ── Level 3: 引用文件（按需加载）────────────────────────────

    def load_resource(self, skill_name: str, resource_path: str) -> Optional[str]:
        """
        Level 3: 按需加载 skill 目录下的引用文件。
        支持相对路径（基于 skill 的 base_path）。

        Args:
            skill_name: 技能名称
            resource_path: 相对路径，如 "references/style-guide.md"、"scripts/lint.sh"

        Returns:
            文件内容，不存在返回 None
        """
        doc = self._load_doc(skill_name)
        if not doc:
            return None

        # 缓存检查
        cache_key = f"{skill_name}:{resource_path}"
        if cache_key in doc._resource_cache:
            return doc._resource_cache[cache_key]

        full_path = doc.base_path / resource_path
        if not full_path.exists():
            logger.warning("[SkillLoader] Level 3 资源不存在: %s", full_path)
            return None

        try:
            content = full_path.read_text(encoding="utf-8")
            doc._resource_cache[cache_key] = content
            return content
        except Exception as e:
            logger.warning("[SkillLoader] Level 3 读取失败: %s, error=%s", full_path, e)
            return None

    def list_resources(self, skill_name: str) -> List[str]:
        """列出 skill 目录下的 Level 3 资源文件（references/ scripts/ assets/）。"""
        doc = self._load_doc(skill_name)
        if not doc:
            return []

        resource_dirs = {"references", "scripts", "assets"}
        skip_ext = {".pyc", ".pyo", ".pyd"}
        resources = []
        for d in resource_dirs:
            dir_path = doc.base_path / d
            if not dir_path.exists():
                continue
            for p in sorted(dir_path.rglob("*")):
                if p.is_file() and p.suffix not in skip_ext:
                    resources.append(str(p.relative_to(doc.base_path)))
        return resources

    # ── 内部实现 ────────────────────────────────────────────────

    def _load_doc(self, skill_name: str) -> Optional[SkillDocument]:
        """加载并缓存 SkillDocument。"""
        if skill_name in self._docs:
            return self._docs[skill_name]

        # 从 adapter 获取元数据和 body
        info = self._adapter.get(skill_name)
        if not info:
            logger.warning("[SkillLoader] skill 不存在: %s", skill_name)
            return None

        # Python 类型 skill 没有 SKILL.md body
        if info.skill_type == "python" and not info.body:
            logger.info("[SkillLoader] skill %s 是 python 类型，无 SKILL.md body", skill_name)
            # 尝试查找同目录下的 SKILL.md
            skill_dir = Path(info.dir_path) if info.dir_path else None
            if skill_dir:
                skill_md = skill_dir / "SKILL.md"
                if skill_md.exists():
                    try:
                        content = skill_md.read_text(encoding="utf-8")
                        _, body = self._parse_frontmatter(content)
                        info.body = body
                    except Exception:
                        pass

        if not info.body:
            logger.warning("[SkillLoader] skill %s 无 body 内容", skill_name)
            return None

        base_path = Path(info.dir_path) if info.dir_path else Path(".")
        sections = self._split_sections(info.body)

        doc = SkillDocument(
            name=skill_name,
            body=info.body,
            base_path=base_path,
            sections=sections,
        )
        self._docs[skill_name] = doc
        return doc

    @staticmethod
    def _parse_frontmatter(content: str) -> tuple:
        """解析 YAML frontmatter → (metadata_dict, body_str)。"""
        meta = {}
        body = content
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                try:
                    import yaml
                    meta = yaml.safe_load(parts[1]) or {}
                except Exception:
                    for line in parts[1].strip().split("\n"):
                        if ":" in line:
                            k, v = line.split(":", 1)
                            meta[k.strip()] = v.strip()
                body = parts[2].strip()
        return meta, body

    @staticmethod
    def _split_sections(body: str) -> List[SkillSection]:
        """
        按 ## 标题切分 SKILL.md body 为多个段落。
        保留 ### 等子标题在父段落内。
        """
        sections: List[SkillSection] = []
        # 匹配 ## 或 ### 等标题行
        pattern = re.compile(r"^(#{1,4})\s+(.+)$", re.MULTILINE)

        matches = list(pattern.finditer(body))
        if not matches:
            # 无标题，整体作为一个段落
            sections.append(SkillSection(
                heading="(全文)",
                level=1,
                content=body,
                char_offset=0,
            ))
            return sections

        # 只切 ## 级别标题，### 保留在父段落内
        h2_matches = [m for m in matches if len(m.group(1)) == 2]

        if not h2_matches:
            # 没有 ## 标题，用所有标题切分
            h2_matches = matches

        for i, match in enumerate(h2_matches):
            heading = match.group(2).strip()
            start = match.start()
            end = h2_matches[i + 1].start() if i + 1 < len(h2_matches) else len(body)
            content = body[start:end].strip()

            sections.append(SkillSection(
                heading=heading,
                level=len(match.group(1)),
                content=content,
                char_offset=start,
            ))

        # 标题前的内容（frontmatter 之后、第一个标题之前）
        first_heading_start = h2_matches[0].start()
        if first_heading_start > 0:
            preamble = body[:first_heading_start].strip()
            if preamble:
                sections.insert(0, SkillSection(
                    heading="(前言)",
                    level=0,
                    content=preamble,
                    char_offset=0,
                ))

        return sections

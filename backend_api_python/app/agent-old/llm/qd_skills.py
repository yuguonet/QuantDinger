# -*- coding: utf-8 -*-
"""
QuantDinger 技能桥接层 — 不依赖 smolagents。

支持两种技能格式：
  1. Python 类技能（skills/*.py，继承 Skill 基类）
  2. SKILL.md 技能（skills/*/SKILL.md，Anthropic 标准）

使用方式：
    from app.agent.llm.qd_skills import QDSkillAdapter

    adapter = QDSkillAdapter()
    catalog = adapter.get_catalog_text()   # 注入 system prompt
    body = adapter.get_body("market-screener")  # Agent 按需加载
    skill = adapter.get_skill("json_extractor")  # 获取 Python 技能实例
"""
from __future__ import annotations

import importlib
import inspect
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

try:
    import yaml
except ImportError:
    yaml = None


@dataclass
class SkillInfo:
    """技能元数据。"""
    name: str                    # 标识符
    display_name: str            # 显示名
    description: str = ""
    tags: List[str] = field(default_factory=list)
    priority: int = 5
    tools: List[str] = field(default_factory=list)
    body: str = ""               # SKILL.md body（Markdown 技能才有）
    dir_path: str = ""
    skill_type: str = "python"   # "python" 或 "markdown"
    skill_cls: Any = None        # Python 技能的类引用


@dataclass
class SkillSection:
    """SKILL.md 中的一个段落（按 ## 标题切分）。"""
    heading: str           # 段落标题（不含 ##）
    level: int             # markdown 标题层级 (2=##, 3=###)
    content: str           # 段落内容（含标题行）
    char_offset: int       # 在原始 body 中的字符偏移


@dataclass
class SkillDocument:
    """完整 skill 文档（缓存加载结果）。"""
    name: str
    body: str                              # SKILL.md 完整正文
    base_path: Path                        # skill 目录路径
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


def _parse_skill_md(content: str) -> tuple:
    """解析 SKILL.md → (metadata_dict, body_str)。"""
    meta = {}
    body = content
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            if yaml:
                try:
                    meta = yaml.safe_load(parts[1]) or {}
                except Exception:
                    meta = {}
            else:
                for line in parts[1].strip().split("\n"):
                    if ":" in line:
                        k, v = line.split(":", 1)
                        meta[k.strip()] = v.strip()
            body = parts[2].strip()
    return meta, body


def _split_sections(body: str) -> List[SkillSection]:
    """按 ## 标题切分 SKILL.md body 为多个段落。"""
    sections: List[SkillSection] = []
    pattern = re.compile(r"^(#{1,4})\s+(.+)$", re.MULTILINE)
    matches = list(pattern.finditer(body))
    if not matches:
        sections.append(SkillSection(
            heading="(全文)", level=1, content=body, char_offset=0,
        ))
        return sections
    h2_matches = [m for m in matches if len(m.group(1)) == 2]
    if not h2_matches:
        h2_matches = matches
    for i, match in enumerate(h2_matches):
        heading = match.group(2).strip()
        start = match.start()
        end = h2_matches[i + 1].start() if i + 1 < len(h2_matches) else len(body)
        sections.append(SkillSection(
            heading=heading,
            level=len(match.group(1)),
            content=body[start:end].strip(),
            char_offset=start,
        ))
    first_heading_start = h2_matches[0].start()
    if first_heading_start > 0:
        preamble = body[:first_heading_start].strip()
        if preamble:
            sections.insert(0, SkillSection(
                heading="(前言)", level=0, content=preamble, char_offset=0,
            ))
    return sections


class QDSkillAdapter:
    """QuantDinger 技能适配器 — 发现和管理技能。"""

    def __init__(self, skills_dirs: List[str] = None):
        self._skills: Dict[str, SkillInfo] = {}
        self._display_to_name: Dict[str, str] = {}
        self._docs: Dict[str, SkillDocument] = {}  # SkillDocument 缓存
        dirs = skills_dirs or self._default_dirs()
        for d in dirs:
            self._scan_python(d)
        for d in dirs:
            self._scan_markdown(d)  # SKILL.md 最终覆盖

    @staticmethod
    def _default_dirs() -> List[str]:
        here = Path(__file__).resolve().parent.parent
        dirs = []
        d1 = here / "skills"
        if d1.exists():
            dirs.append(str(d1))
        d2 = here.parent / "agent-nold" / "skills"
        if d2.exists():
            dirs.append(str(d2))
        return dirs

    def _scan_markdown(self, skills_dir: str):
        """扫描 SKILL.md 技能。"""
        base = Path(skills_dir)
        if not base.exists():
            return
        for skill_dir in sorted(base.iterdir()):
            if not skill_dir.is_dir() or skill_dir.name.startswith("_"):
                continue
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.exists():
                continue
            try:
                content = skill_md.read_text(encoding="utf-8")
                meta, body = _parse_skill_md(content)
            except Exception:
                continue
            display_name = meta.get("name", skill_dir.name)
            info = SkillInfo(
                name=skill_dir.name,
                display_name=display_name,
                description=meta.get("description", ""),
                tags=meta.get("tags", []),
                priority=meta.get("priority", 5),
                tools=meta.get("tools", []),
                body=body,
                dir_path=str(skill_dir),
                skill_type="markdown",
            )
            self._skills[info.name] = info
            self._display_to_name[display_name] = info.name

    def _scan_python(self, skills_dir: str):
        """扫描 Python 类技能（继承 Skill 基类）。"""
        base = Path(skills_dir)
        if not base.exists():
            return

        # 确保 Skill 基类可用
        try:
            from skills.base import Skill as SkillBase
        except ImportError:
            return

        for py_file in sorted(base.glob("*.py")):
            module_name = py_file.stem
            if module_name.startswith("_") or module_name in ("base", "registry", "call_skill_tool"):
                continue

            try:
                # 用完整包路径导入
                if "agent-nold" in str(base):
                    mod = importlib.import_module(f"app.agent-nold.skills.{module_name}")
                else:
                    mod = importlib.import_module(f"skills.{module_name}")
            except Exception:
                continue

            for attr_name in dir(mod):
                obj = getattr(mod, attr_name)
                if not (inspect.isclass(obj) and issubclass(obj, SkillBase) and obj is not SkillBase):
                    continue
                # 跳过抽象类
                if getattr(obj, "run", None) and inspect.isabstract(obj):
                    continue

                name = getattr(obj, "name", "") or attr_name
                description = getattr(obj, "description", "") or obj.__doc__ or ""
                description = description.strip().split("\n")[0][:200]

                info = SkillInfo(
                    name=name,
                    display_name=name,
                    description=description,
                    skill_type="python",
                    skill_cls=obj,
                )
                self._skills[info.name] = info
                self._display_to_name[name] = info.name

        # 也扫描子目录（如 market_screener）
        for sub_dir in sorted(base.iterdir()):
            if not sub_dir.is_dir() or sub_dir.name.startswith("_"):
                continue
            run_py = sub_dir / "run.py"
            if not run_py.exists():
                continue
            try:
                if "agent-nold" in str(base):
                    mod = importlib.import_module(f"app.agent-nold.skills.{sub_dir.name}.run")
                else:
                    mod = importlib.import_module(f"skills.{sub_dir.name}.run")
            except Exception:
                continue

            # 检查是否有 run() 函数或 Skill 类
            if hasattr(mod, "run") and callable(mod.run):
                name = sub_dir.name
                doc = inspect.getdoc(mod.run) or ""
                description = doc.split("\n")[0][:200] if doc else name
                info = SkillInfo(
                    name=name,
                    display_name=name,
                    description=description,
                    skill_type="python",
                )
                self._skills[info.name] = info
                self._display_to_name[name] = info.name
            else:
                for attr_name in dir(mod):
                    obj = getattr(mod, attr_name)
                    if inspect.isclass(obj) and hasattr(obj, "run"):
                        name = getattr(obj, "name", "") or attr_name
                        description = getattr(obj, "description", "") or ""
                        info = SkillInfo(
                            name=name,
                            display_name=name,
                            description=description,
                            skill_type="python",
                            skill_cls=obj,
                        )
                        self._skills[info.name] = info
                        self._display_to_name[name] = info.name

        logger.info("[QDSkills] 已发现 %d 个 Skill", len(self._skills))

    def get(self, name: str) -> Optional[SkillInfo]:
        info = self._skills.get(name)
        if info:
            return info
        real_name = self._display_to_name.get(name)
        return self._skills.get(real_name) if real_name else None

    def get_skill(self, name: str) -> Optional[Any]:
        """获取 Python 技能实例（需要 LLM 时传入）。"""
        info = self.get(name)
        if info and info.skill_cls:
            return info.skill_cls()
        return None

    def get_body(self, name: str) -> Optional[str]:
        """返回 SKILL.md body。"""
        info = self.get(name)
        return info.body if info else None

    def get_catalog_text(self) -> str:
        """生成技能目录 XML，注入 system prompt。"""
        if not self._skills:
            return ""
        lines = ["<available_skills>"]
        for info in sorted(self._skills.values(), key=lambda x: x.priority, reverse=True):
            stype = f" [{info.skill_type}]" if info.skill_type == "python" else ""
            lines.append(f'  <skill name="{info.display_name}">')
            lines.append(f'    <description>{info.description}{stype}</description>')
            if info.body:
                lines.append(f'    <location>{info.dir_path}/SKILL.md</location>')
            lines.append(f'  </skill>')
        lines.append("</available_skills>")
        return "\n".join(lines)

    def list_skills(self) -> List[Dict[str, str]]:
        return [
            {
                "name": info.display_name,
                "description": info.description[:200],
                "tags": info.tags,
                "type": info.skill_type,
            }
            for info in sorted(self._skills.values(), key=lambda x: x.priority, reverse=True)
        ]

    def __len__(self):
        return len(self._skills)

    # ── Level 2: SKILL.md body（按需加载）─────────────────────────

    def load_body(self, skill_name: str) -> Optional[str]:
        """加载 SKILL.md 完整正文（缓存）。"""
        doc = self._load_doc(skill_name)
        return doc.body if doc else None

    def load_sections(self, skill_name: str) -> List[SkillSection]:
        """返回 SKILL.md 按 ## 标题切分的所有段落。"""
        doc = self._load_doc(skill_name)
        return doc.sections if doc else []

    def load_section(self, skill_name: str, heading_keyword: str) -> Optional[str]:
        """按标题关键词加载单个段落。"""
        doc = self._load_doc(skill_name)
        if not doc:
            return None
        section = doc.get_section(heading_keyword)
        return section.content if section else None

    def load_section_by_index(self, skill_name: str, index: int) -> Optional[str]:
        """按索引加载段落。"""
        doc = self._load_doc(skill_name)
        if not doc:
            return None
        section = doc.get_section_by_index(index)
        return section.content if section else None

    def get_section_headings(self, skill_name: str) -> List[str]:
        """返回 skill 的所有段落标题。"""
        doc = self._load_doc(skill_name)
        if not doc:
            return []
        return [s.heading for s in doc.sections]

    # ── Level 3: 引用文件（按需加载）────────────────────────────

    def load_resource(self, skill_name: str, resource_path: str) -> Optional[str]:
        """按需加载 skill 目录下的引用文件。"""
        doc = self._load_doc(skill_name)
        if not doc:
            return None
        cache_key = f"{skill_name}:{resource_path}"
        if cache_key in doc._resource_cache:
            return doc._resource_cache[cache_key]
        full_path = doc.base_path / resource_path
        if not full_path.exists():
            logger.warning("[QDSkills] Level 3 资源不存在: %s", full_path)
            return None
        try:
            content = full_path.read_text(encoding="utf-8")
            doc._resource_cache[cache_key] = content
            return content
        except Exception as e:
            logger.warning("[QDSkills] Level 3 读取失败: %s, error=%s", full_path, e)
            return None

    def list_resources(self, skill_name: str) -> List[str]:
        """列出 skill 目录下的资源文件。"""
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
        info = self._skills.get(skill_name) or self._skills.get(self._display_to_name.get(skill_name))
        if not info:
            logger.warning("[QDSkills] skill 不存在: %s", skill_name)
            return None
        # Python 类型 skill 尝试找同目录下的 SKILL.md
        if info.skill_type == "python" and not info.body:
            skill_dir = Path(info.dir_path) if info.dir_path else None
            if skill_dir:
                skill_md = skill_dir / "SKILL.md"
                if skill_md.exists():
                    try:
                        content = skill_md.read_text(encoding="utf-8")
                        _, body = _parse_skill_md(content)
                        info.body = body
                    except Exception:
                        pass
        if not info.body:
            logger.warning("[QDSkills] skill %s 无 body 内容", skill_name)
            return None
        base_path = Path(info.dir_path) if info.dir_path else Path(".")
        sections = _split_sections(info.body)
        doc = SkillDocument(
            name=skill_name, body=info.body,
            base_path=base_path, sections=sections,
        )
        self._docs[skill_name] = doc
        return doc

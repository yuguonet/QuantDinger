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


class QDSkillAdapter:
    """QuantDinger 技能适配器 — 发现和管理技能。"""

    def __init__(self, skills_dirs: List[str] = None):
        self._skills: Dict[str, SkillInfo] = {}
        self._display_to_name: Dict[str, str] = {}
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

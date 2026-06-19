# -*- coding: utf-8 -*-
"""
Skill Registry — 发现和管理 skills/*/SKILL.md。

遵循 Anthropic Agent Skills 标准：
  - 每个 Skill 是一个目录，核心文件为 SKILL.md
  - SKILL.md 包含 YAML frontmatter（元数据）+ Markdown body（指令）
  - 目录下有 run.py 作为执行入口

职责：
  - 扫描 skills/*/SKILL.md，解析元数据
  - 提供按名称查找、列出全部的能力
  - 为 call_skill 工具提供执行入口
"""
from __future__ import annotations

import importlib
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

_SKILLS_DIR = Path(__file__).parent


@dataclass
class SkillInfo:
    """Skill 元数据（从 SKILL.md frontmatter 解析）。"""
    name: str                    # 目录名（下划线，Python 模块名）
    display_name: str            # SKILL.md 中的 name（连字符，Anthropic 标准）
    description: str = ""
    tags: List[str] = field(default_factory=list)
    priority: int = 5
    tools: List[str] = field(default_factory=list)
    body: str = ""               # SKILL.md 的 Markdown body（执行指令）
    dir_path: str = ""           # 目录绝对路径


# 缓存：name（下划线）→ SkillInfo
_skills: Dict[str, SkillInfo] = {}
_loaded = False


def _parse_skill_md(content: str) -> tuple:
    """解析 SKILL.md，分离 YAML frontmatter 和 Markdown body。"""
    meta = {}
    body = content
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            try:
                meta = yaml.safe_load(parts[1]) or {}
            except yaml.YAMLError:
                meta = {}
            body = parts[2].strip()
    return meta, body


def discover():
    """扫描 skills/*/SKILL.md，加载元数据。幂等，只加载一次。"""
    global _loaded
    if _loaded:
        return
    _loaded = True

    for skill_dir in sorted(_SKILLS_DIR.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_md = skill_dir / "SKILL.md"
        run_py = skill_dir / "run.py"
        if not skill_md.exists() or not run_py.exists():
            continue

        try:
            content = skill_md.read_text(encoding="utf-8")
            meta, body = _parse_skill_md(content)
            if not meta.get("name"):
                continue

            info = SkillInfo(
                name=skill_dir.name,                          # 目录名（下划线）
                display_name=meta.get("name", skill_dir.name),  # SKILL.md name（连字符）
                description=meta.get("description", ""),
                tags=meta.get("tags", []),
                priority=meta.get("priority", 5),
                tools=meta.get("tools", []),
                body=body,
                dir_path=str(skill_dir),
            )
            _skills[info.name] = info
            logger.debug("[SkillRegistry] 发现: %s (%s)", info.name, info.display_name)
        except Exception as e:
            logger.warning("[SkillRegistry] 加载 %s 失败: %s", skill_dir.name, e)

    logger.info("[SkillRegistry] 已发现 %d 个 Skill", len(_skills))


def get(name: str) -> Optional[SkillInfo]:
    """按名称查找 Skill。"""
    discover()
    return _skills.get(name)


def all_skills() -> Dict[str, SkillInfo]:
    """返回全部已发现的 Skill。"""
    discover()
    return dict(_skills)


def all_names() -> List[str]:
    """返回全部 Skill 名称列表。"""
    discover()
    return list(_skills.keys())


def run_skill(skill_name: str, stock_code: str = "", stock_name: str = "",
              context: dict = None) -> dict:
    """执行指定 Skill，返回标准化结果 dict。

    通过 importlib 动态加载 skills/<name>/run.py 的 run() 函数。
    """
    info = _skills.get(skill_name)
    if not info:
        discover()
        info = _skills.get(skill_name)
    if not info:
        return {
            "skill": skill_name, "status": "failed",
            "error": f"未知 Skill: {skill_name}",
            "score": 0, "direction": "neutral", "confidence": 0, "factors": [],
        }

    module_path = f"app.agent.skills.{skill_name}.run"
    t0 = time.time()
    try:
        mod = importlib.import_module(module_path)
        result = mod.run(stock_code, stock_name or "", context)
    except Exception as e:
        elapsed = (time.time() - t0) * 1000
        logger.warning("[SkillRegistry] %s 异常: %.0fms %s", skill_name, elapsed, e)
        return {
            "skill": skill_name, "status": "failed", "error": str(e),
            "score": 0, "direction": "neutral", "confidence": 0, "factors": [],
        }

    elapsed = (time.time() - t0) * 1000
    if not isinstance(result, dict):
        return {
            "skill": skill_name, "status": "failed",
            "error": f"输出格式错误: {type(result)}",
            "score": 0, "direction": "neutral", "confidence": 0, "factors": [],
        }

    result.setdefault("skill", skill_name)
    result.setdefault("status", "ok")
    logger.info("[SkillRegistry] %s | score=%s dir=%s | %.0fms",
                skill_name, result.get("score"), result.get("direction"), elapsed)
    return result

# -*- coding: utf-8 -*-
"""
Skill Registry — 发现和管理 skills/*/SKILL.md。

遵循 Anthropic Agent Skills 标准：
  - 每个 Skill 是一个目录，核心文件为 SKILL.md
  - SKILL.md 包含 YAML frontmatter（元数据）+ Markdown body（指令）
  - 支持两种类型：
    - code: 目录下有 run.py 作为执行入口
    - instruction: 纯 SKILL.md，agent 按指令用已有工具执行

职责：
  - 扫描 skills/*/SKILL.md，解析元数据
  - 校验 skill 合规性（name 格式、description、run.py 可导入）
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
    skill_type: str = "code"     # "code"（有 run.py）或 "instruction"（纯指令）


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


def _validate_skill(skill_dir: Path, meta: dict) -> Optional[str]:
    """校验 skill 是否合规。返回 None 表示通过，否则返回拒绝原因。

    支持两种类型：
      - code: 有 run.py，必须有 callable 的 run() 函数
      - instruction: 纯 SKILL.md，无 run.py，body 非空即可
    """
    import re

    # 1. name 必填
    name = meta.get("name", "")
    if not name:
        return "缺少 name 字段"

    # 2. name 格式：小写字母+数字+连字符，1-64字符，不以连字符开头/结尾，无连续连字符
    if len(name) > 64:
        return f"name 超过64字符: {len(name)}"
    if not re.match(r'^[a-z0-9]+(-[a-z0-9]+)*$', name):
        return f"name 格式不合规（需小写+数字+连字符）: {name}"

    # 3. description 必填
    desc = meta.get("description", "")
    if not desc:
        return "缺少 description 字段"
    if len(desc) > 1024:
        return f"description 超过1024字符: {len(desc)}"

    # 4. run.py 存在时校验（code 型）
    run_py = skill_dir / "run.py"
    if run_py.exists():
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("_skill_validate", str(run_py))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            if not hasattr(mod, "run"):
                return "run.py 缺少 run() 函数"
            if not callable(mod.run):
                return "run.py 的 run 不可调用"
        except Exception as e:
            return f"run.py 导入失败: {e}"

    return None


def discover():
    """扫描 skills/*/SKILL.md，加载元数据。幂等，只加载一次。

    校验规则（不合规的 skill 自动排除，不报错）：
      1. 目录下必须有 SKILL.md 和 run.py
      2. SKILL.md 必须有合法的 YAML frontmatter
      3. name 必填，格式：小写+数字+连字符，1-64字符
      4. description 必填，最长1024字符
      5. run.py 可导入且有 callable 的 run() 函数
    """
    global _loaded
    if _loaded:
        return
    _loaded = True

    for skill_dir in sorted(_SKILLS_DIR.iterdir()):
        if not skill_dir.is_dir():
            continue

        skill_md = skill_dir / "SKILL.md"
        run_py = skill_dir / "run.py"

        # 基础文件检查
        if not skill_md.exists():
            logger.debug("[SkillRegistry] 跳过 %s: 缺少 SKILL.md", skill_dir.name)
            continue

        # 解析 frontmatter
        try:
            content = skill_md.read_text(encoding="utf-8")
            meta, body = _parse_skill_md(content)
        except Exception as e:
            logger.warning("[SkillRegistry] 跳过 %s: SKILL.md 解析失败: %s", skill_dir.name, e)
            continue

        # 校验
        reason = _validate_skill(skill_dir, meta)
        if reason:
            logger.warning("[SkillRegistry] 跳过 %s: %s", skill_dir.name, reason)
            continue

        # 注册
        info = SkillInfo(
            name=skill_dir.name,
            display_name=meta.get("name", skill_dir.name),
            description=meta.get("description", ""),
            tags=meta.get("tags", []),
            priority=meta.get("priority", 5),
            tools=meta.get("tools", []),
            body=body,
            dir_path=str(skill_dir),
            skill_type="code" if run_py.exists() else "instruction",
        )
        _skills[info.name] = info
        logger.debug("[SkillRegistry] 发现: %s (%s)", info.name, info.display_name)

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

    两种执行方式：
      - code 型：importlib 加载 run.py 的 run() 函数
      - instruction 型：返回 SKILL.md body 作为指令，由 agent 自行执行
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

    # ── 纯指令型：返回 SKILL.md body 作为指令 ──
    if info.skill_type == "instruction":
        logger.info("[SkillRegistry] %s (instruction) | 返回指令", skill_name)
        return {
            "skill": skill_name,
            "status": "instruction",
            "instruction": info.body,
            "description": info.description,
            "score": 0,
            "direction": "neutral",
            "confidence": 0,
            "factors": [],
        }

    # ── 代码型：importlib 加载 run.py ──
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

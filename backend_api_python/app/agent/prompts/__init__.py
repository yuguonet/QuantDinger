# -*- coding: utf-8 -*-
"""prompts/ — 提示词外置加载器（2026-10-14）。

【为什么外置】提示词原先硬编码散落在 tool_preselect / qd_agent / c3p / resolver /
skill_brewer / func_overlap 等模块常量里：改一字要动 .py、`prompt_sha` 追溯不到、
与 `prompts/qd_system.txt` 一派的外置约定不一致（项目纪律：不硬编码）。

【格式约定】
  · 带 `{占位符}` 的长模板 → **.md**（可读性最好，与 skill_brew 同风格）
  · 多段（system + user）或键值短句 → **.yaml**（一文件多键，避免碎文件）
  · 存量 .txt（qd_core_base / qd_system / skill_brew）**不改后缀**，照常 load_prompt

【占位符】模板内用 `str.format` 风格 `{name}`；字面花括号写 `{{` `}}`
  （tool_preselect user 模板里的 JSON 示例即如此）。

【缓存】模块级 dict 缓存；改文件后需重启进程才生效（与旧行为一致：常量在 import 时定死）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

__all__ = ["PROMPTS_DIR", "load_prompt", "load_prompt_doc"]

PROMPTS_DIR = Path(__file__).resolve().parent
_SUFFIXES = (".md", ".yaml", ".yml", ".txt")
_cache: Dict[str, Any] = {}


def _resolve(name: str) -> Path:
    """`name` 可带可不带后缀；按 _SUFFIXES 顺序试。"""
    raw = Path(name).name  # 禁止路径穿越
    p = PROMPTS_DIR / raw
    if p.suffix in _SUFFIXES and p.is_file():
        return p
    for suf in _SUFFIXES:
        cand = PROMPTS_DIR / f"{raw}{suf}"
        if cand.is_file():
            return cand
    raise FileNotFoundError(f"prompts/ 下不存在: {name}")


def load_prompt(name: str) -> str:
    """加载单段提示词正文（.md/.txt 的全文；.yaml 的 `body` 或唯一长字符串键）。"""
    key = f"body:{name}"
    if key in _cache:
        return _cache[key]
    path = _resolve(name)
    text = path.read_text(encoding="utf-8")
    if path.suffix in (".yaml", ".yml"):
        import yaml
        doc = yaml.safe_load(text) or {}
        if isinstance(doc, str):
            body = doc
        elif isinstance(doc, dict):
            body = doc.get("body") or doc.get("prompt") or ""
            if not body:
                # 单键 dict 时取该值（如 {text: "..."})
                vals = [v for v in doc.values() if isinstance(v, str) and len(v) > 20]
                body = vals[0] if vals else ""
            body = str(body)
        else:
            body = str(doc)
    else:
        body = text
    _cache[key] = body
    return body


def load_prompt_doc(name: str) -> Dict[str, Any]:
    """加载多段提示词（yaml 映射；.md/.txt 包成 {"body": 全文}）。"""
    key = f"doc:{name}"
    if key in _cache:
        return _cache[key]
    path = _resolve(name)
    text = path.read_text(encoding="utf-8")
    if path.suffix in (".yaml", ".yml"):
        import yaml
        doc = yaml.safe_load(text) or {}
        if not isinstance(doc, dict):
            doc = {"body": str(doc)}
    else:
        doc = {"body": text}
    _cache[key] = doc
    return doc

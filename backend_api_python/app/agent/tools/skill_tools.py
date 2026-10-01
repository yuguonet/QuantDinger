# -*- coding: utf-8 -*-
"""list_skills / read_skill —— 技能读取工具（skills/ 域保留件的模型侧入口）。

旧核心用 _SkillSectionTool/_SkillResourceTool/_SkillFuncTool（smolagents 载体）把技能
喂给模型；mimoagent 范式下技能走统一函数工具通道，语义不变：
  目录 → list_skills；正文/小节/资源 → read_skill。
技能的 run.py 类执行入口（market_screener 等）由任务方按需直接调用，暂不入工具面。
"""
from __future__ import annotations

from typing import Any, Dict


def _get_skills():
    try:
        from app.agent.agent import skills as _s
    except ImportError:
        from agent import skills as _s
    return _s


def list_skills() -> Dict[str, Any]:
    """列出全部可用技能（名称 + 简介）。

    Returns:
        {"count": N, "skills": [{name, description}, ...]}
    """
    try:
        return {"count": len(_get_skills()), "skills": _get_skills().list_skills()}
    except Exception as e:
        return {"error": str(e)}


def read_skill(name: str, heading: str = "", resource: str = "") -> Dict[str, Any]:
    """读取技能内容。三选一：全文（都不传）/ 按小节标题关键词 / 按资源相对路径。

    Args:
        name: 技能名（list_skills 返回的 name）
        heading: 小节标题关键词（如 "分析框架"），优先级高于 resource
        resource: 资源相对路径（如 "references/analysis-framework.md"）

    Returns:
        {"name": ..., "content": ...} 或 {"error": ...}
    """
    sk = _get_skills()
    try:
        if heading:
            text = sk.load_section(name, heading)
            if text is None:
                return {"error": f"技能 {name} 无匹配小节: {heading}",
                        "headings": sk.get_section_headings(name)}
        elif resource:
            text = sk.load_resource(name, resource)
            if text is None:
                return {"error": f"技能 {name} 无资源: {resource}",
                        "resources": sk.list_resources(name)}
        else:
            text = sk.load_body(name)
        if text is None:
            return {"error": f"技能不存在: {name}"}
        return {"name": name, "content": text[:20000]}
    except Exception as e:
        return {"error": str(e)}

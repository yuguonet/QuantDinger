# -*- coding: utf-8 -*-
"""预扫模块（2026-09-12，Q4 token 优化）：规划前的代码级接口盘点。

两个入口（均纯静态读取，零执行、零网络）：
  - prescan_skills(skill_adapter) → 每个技能：名称/描述/阶段流/函数签名+文档首行
  - prescan_tools(provider, limit) → 工具紧凑清单：name(param: type, …) — 文档首行

供 _plan 注入规划提示：让外部 planner 不靠"名字猜参数"，直接按真实签名
编排工作流（命中率↑、试错步↓、token↓）。
"""
from __future__ import annotations

import ast
import importlib
import inspect
from pathlib import Path

from app.utils.logger import get_logger

logger = get_logger(__name__)

_SKILLS_DIR = Path(__file__).resolve().parents[1] / "skills"


def _first_doc(fn) -> str:
    """docstring 首行（≤80 字符）。"""
    doc = (inspect.getdoc(fn) or "").strip()
    return doc.split("\n")[0][:80] if doc else ""


def _sig_str(fn) -> str:
    """紧凑签名：name(a, b=1)（默认值简示）。"""
    try:
        ps = []
        for p in inspect.signature(fn).parameters.values():
            if p.name.startswith("_"):
                continue
            if p.default is inspect.Parameter.empty:
                ps.append(p.name)
            else:
                d = repr(p.default)
                ps.append(f"{p.name}={d}" if len(d) <= 12 else f"{p.name}={d[:9]}…")
        return f"{fn.__name__}({', '.join(ps)})"
    except (ValueError, TypeError):
        return fn.__name__ + "(…)"


def _return_hint(fn) -> str:
    """返回结构提示：从 docstring 里抓 Returns 段首行（≤70 字符）。"""
    doc = (inspect.getdoc(fn) or "")
    m = re.search(r"Returns:\s*\n\s*(.+)", doc)
    if m:
        return m.group(1).strip()[:70]
    return ""


import re  # noqa: E402  （放底部避免顶部拥挤）


def prescan_skill_funcs(module_name: str) -> list:
    """静态解析 skills/<module>/run.py：公开函数的签名+文档（不 import，无副作用）。

    Returns:
        [{"name", "sig", "doc", "ret"}]，解析失败返回 []
    """
    path = _SKILLS_DIR / module_name / "run.py"
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.debug("[prescan] %s 解析失败: %s", path, e)
        return []
    out = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or node.name.startswith("_"):
            continue
        # 签名（ast 层面提取，等价 inspect 但无需 import）
        try:
            sig = ast.unparse(node.args)
            sig = f"{node.name}({sig})"
        except Exception:
            sig = node.name + "(…)"
        doc = ""
        if (node.body and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)):
            doc = node.body[0].value.value.strip().split("\n")[0][:80]
        ret = ""
        ret_node = next((n for n in node.body if isinstance(n, ast.Return)), None)
        if ret_node and ret_node.value is not None:
            ret = ast.unparse(ret_node.value)[:60]
        out.append({"name": node.name, "sig": sig, "doc": doc, "ret": ret})
    return out


def prescan_skills(skill_adapter) -> str:
    """技能预扫文本：每技能一段（名称+描述+阶段流+函数签名表）。

    注入规划提示的 {skills_text} 槽位——替代"名字+描述"薄信息。
    """
    if not skill_adapter:
        return "(无可用技能)"
    parts = []
    try:
        skills = skill_adapter.list_skills()
    except Exception as e:
        logger.debug("[prescan] list_skills 失败: %s", e)
        return "(无可用技能)"
    for s in skills:
        name = s.get("name", "")
        desc = s.get("description", "")[:120]
        mod = name.replace("-", "_")
        parts.append(f"- {name}: {desc}")
        for f in prescan_skill_funcs(mod)[:8]:
            line = f"    · {f['sig']} — {f['doc']}"
            if f["ret"]:
                line += f"（返回 {f['ret']}）"
            parts.append(line)
    return "\n".join(parts) if parts else "(无可用技能)"


def prescan_tools(provider, limit: int = 60, per_item: int = 110) -> str:
    """工具预扫文本：紧凑签名清单（替代裸名字列表）。"""
    if not provider:
        return ""
    lines = []
    for n in provider.get_tool_names(limit=limit):
        fn = provider.get(n)
        sig = _sig_str(fn) if fn else n
        doc = _first_doc(fn) if fn else ""
        # 批量标注（2026-09-12）：codes 参数=支持逗号分隔多标的——
        # 规划器/执行器据此一次拉全，避免逐只分步（run4 实证烧 8 步）
        try:
            _params = [p.name for p in inspect.signature(fn).parameters.values()] if fn else []
        except Exception:
            _params = []
        mark = " [支持批量]" if "codes" in _params else ""
        line = f"  {sig}{mark}" + (f" — {doc}" if doc else "")
        lines.append(line[:per_item])
    return "\n".join(lines)

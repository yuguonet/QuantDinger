# -*- coding: utf-8 -*-
"""预扫模块（2026-09-12，Q4 token 优化）：规划前的代码级接口盘点。

两个入口（均纯静态读取，零执行、零网络）：
  - prescan_skills(skill_adapter) → 每个技能：名称/描述/阶段流/函数签名+文档首行
  - prescan_tools(provider, limit, query) → 工具紧凑清单：name(param: type, …) — 文档首行
    （可选 query：超出 limit 时按相关度裁剪而非字母序，见 rank_tool_names）

供 _plan 注入规划提示：让外部 planner 不靠"名字猜参数"，直接按真实签名
编排工作流（命中率↑、试错步↓、token↓）。
"""
from __future__ import annotations

import ast
import importlib
import inspect
import re
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


# ═══════════════════════════════════════════════════════════════
#  相关性排序（2026-09-13）：工具清单按"与本次需求的相关度"裁剪
# ═══════════════════════════════════════════════════════════════
#  背景（审计 L7）：原实现按字母序截断（get_tool_names(limit=60)），工具数超上限时
#  被砍掉的是"字母序靠后"的工具，与需求无关——核心工具可能整批消失；且能力段
#  不截断，形成"低阶能力可见、语义化工具不可见"的误导。
#  现改为"先按相关度排序再截断"：排序键相同则按名称稳定排序（结果可复现）。

_ASCII_TERM = re.compile(r"[a-zA-Z][a-zA-Z0-9]{1,}")
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")

_W_NAME = 6  # 命中工具名（下划线分词的整词）
_W_DOC = 2   # 命中文档首行


def _terms(text: str) -> set:
    """把文本切成检索词：ASCII 词 + 中文 2-gram（不引入分词依赖）。"""
    t = (text or "").lower()
    out = set(_ASCII_TERM.findall(t))
    for run in _CJK_RUN.findall(t):
        if len(run) == 1:
            out.add(run)
        else:
            out.update(run[i:i + 2] for i in range(len(run) - 1))
    return out


def _relevance(name: str, doc: str, terms: set) -> int:
    """工具与查询的相关度打分（0=无关）。"""
    if not terms:
        return 0
    score = _W_NAME * len(terms & set(name.lower().split("_")))
    if doc:
        doc_low = doc.lower()
        score += _W_DOC * sum(1 for t in terms if t in doc_low)
    return score


def rank_tool_names(provider, query: str, limit: int = 0,
                    domain: str | None = None) -> tuple:
    """按相关度排序工具名，返回 (names, hidden_count)。

    limit<=0 或工具数不超上限 → 不裁剪（只按名称稳定排序）。
    无任何相关信号（如纯闲聊）→ 退化为字母序，避免随机丢弃工具。

    Args:
        query: 相关性依据（用户消息 / 任务描述 / 实体信息），中文可用。
        limit: 最多返回多少个。
        domain: 只统计该来源域的工具（2026-09-18 新增）。能力层（CAPABILITY_DOMAIN）
            从 20 项扩至 70 项后，plan 提示的能力段必须按相关度裁剪，否则全量注入
            会让 plan 输入膨胀 ~10k 字符——补 L7 审计遗留的"能力段不截断"缺口。
    """
    names = provider.get_tool_names()
    if domain:
        names = [n for n in names if provider.get_domain(n) == domain]
    if limit <= 0 or len(names) <= limit:
        return names, 0
    terms = _terms(query)
    if not terms:
        return names[:limit], len(names) - limit
    docs = {}
    for n in names:
        fn = provider.get(n)
        docs[n] = ((inspect.getdoc(fn) or "").split("\n")[0][:200]) if fn else ""
    scores = {n: _relevance(n, docs[n], terms) for n in names}
    if not any(scores.values()):
        return names[:limit], len(names) - limit
    ordered = sorted(names, key=lambda n: (-scores[n], n))
    return ordered[:limit], len(names) - limit


def prescan_tools(provider, limit: int = 60, per_item: int = 110,
                  query: str = "") -> str:
    """工具预扫文本：紧凑签名清单（替代裸名字列表）。

    Args:
        limit: 最多列出多少个工具（<=0 不限）。超出上限时按 query 相关度裁剪，
            而不是按字母序（2026-09-13）。
        per_item: 每行最大字符数。
        query: 相关性排序依据；为空则退化为名称排序。
    """
    if not provider:
        return ""
    names, hidden = rank_tool_names(provider, query, limit)
    lines = []
    for n in names:
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
    if hidden > 0:
        lines.append(f"  …另有 {hidden} 个工具与本次需求相关度较低未列出"
                     f"（确需时可用 list_tools/search_tools 查询全量）")
    return "\n".join(lines)

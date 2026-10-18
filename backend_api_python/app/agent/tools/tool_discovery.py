# -*- coding: utf-8 -*-
"""工具发现元工具 —— list_tools / search_tools / activate_tools（分层注入的按需通道）。

分层口径（2026-10-01，对齐旧系统 tools/ 顶层必注入的口径）：
  - **必注入层**：mimo 原生工具 + tools/ 顶层（common 域）+ 本模块的三个元工具
    → 每轮都在工具清单里。
  - **按需层**：tools/finance/*（61 个）、tools/knowledge/* 等子域工具
    → **默认不在工具清单里**，模型先用 search_tools 检索、activate_tools 点名激活，
    之后这些工具的完整 schema 才出现在后续请求中。

为什么要分层：全量 73 个工具的 schema 约 12.8k tokens，mimo 每一轮请求都整包下发
（DefaultAgent.get_model_query_kwargs 返回 registry 全量定义），多轮对话下 token 爆炸。
分层后每轮只下发「必注入层 + 已激活的少量领域工具」。

【契约】三个函数的 `_context` 参数由 FnToolAdapter 注入（下划线开头，
func_to_openai_schema 不把它写进 schema，模型看不到也传不了），用于拿到当前 agent
并登记激活。没有 agent 上下文时（被单独直接调用）退化为"只返回清单、不激活"。
"""
from __future__ import annotations

from typing import Any, Dict, List

try:  # 生产：backend_api_python 为根
    from ._base import ToolProvider
except ImportError:  # cli/测试：app/agent 在 sys.path
    from ._base import ToolProvider


# 一次 search_tools 最多自动激活的工具数（防止"搜一次把整个域全激活"，
# 那样就等于没分层）。超出部分只列名字，模型要用再 activate_tools 点名。
_AUTO_ACTIVATE_MAX = int(__import__("os").getenv("QD_AUTO_ACTIVATE_MAX", "8"))


def _provider() -> ToolProvider:
    """取全局 provider（与 QDAgent 扫描同源同口径 —— 否则元工具/子域工具会对不上）。"""
    return ToolProvider.get_or_build()


def _agent_of(_context: Any):
    return (_context or {}).get("agent") if isinstance(_context, dict) else None


def _domains_overview(p: ToolProvider, agent: Any = None) -> str:
    """按需域概览：只给"域名 + 数量"，不给全量名单（省 token 且引导检索）。

    【T1·2026-10-16】agent 在会话中时，改按**注册表全量口径**列层（provider 之外
    还有 mimo 原生的 read/write/edit/glob/grep/task/bash/compact）。原先只列 provider
    域 ⇒ 原生工具在 `list_tools(domain='all')` 里**根本不出现**，一旦被
    on_demand_tools 移出必注入就是"静默消失"（实测确认）。无 agent 时保持原样。
    """
    lines = []
    covered = set()
    if agent is not None and hasattr(agent, "_deferred_names"):
        try:
            core = set(getattr(agent, "_core_names", None) or ())
            covered = set(agent.tool_registry.list_tools())
            deferred = [n for n in sorted(agent._deferred_names()) if n not in core]
            lines.append(f"  - 必注入层: {len(core)} 个（可直接调用）")
            if deferred:
                lines.append(f"  - 延迟层: {len(deferred)} 个（需 activate_tools 激活）")
        except Exception:
            covered = set()
    for dom in p.get_domains():
        names = [n for n in p.list_by_domain(dom) if n not in covered]
        if names:
            lines.append(f"  - {dom}: {len(names)} 个（用 search_tools 检索）")
    return "\n".join(lines) or "  （无）"


def list_tools(domain: str = "", query: str = "", limit: int = 40, _context: Any = None) -> str:
    """列出可用工具（按需层需要先用本工具或 search_tools 找到，再 activate_tools 激活）。

    Args:
        domain: 域名过滤。空=必注入层(common)工具；"all"=全部；或指定子域如 finance。
        query: 可选关键词，只列名称/简介命中的工具。
        limit: 最多列多少条，默认 40（domain='all' 是"看全量"的显式请求，忽略本上限）。

    Returns:
        工具名(参数签名) + 一行简介的清单文本；末尾附按需域概览。
    """
    p = _provider()
    agent = _agent_of(_context)
    reg = getattr(agent, "tool_registry", None) if agent is not None else None
    if domain == "all":
        names = list(p.get_tool_names())
        # 【T1·2026-10-16】注册表里但不在 provider 的工具（mimo 原生）也属于"全部"
        if reg is not None:
            names = names + sorted(n for n in reg.list_tools() if n not in set(names))
    elif domain:
        names = p.list_by_domain(domain)
        if domain in ("native", "other") and reg is not None:
            names = sorted(set(names) | {
                n for n in reg.list_tools()
                if getattr(reg.get(n), "domain", "other") == domain})
    else:
        names = p.list_by_domain("common")

    if query:
        import inspect as _ins
        q = query.lower()
        kept = []
        for n in names:
            fn = p.get(n)
            if fn is not None:
                desc = _ins.getdoc(fn) or ""
            else:
                desc = str(getattr(reg.get(n), "description", "") or "") if reg else ""
            if q in n.lower() or q in str(desc).lower():
                kept.append(n)
        names = kept

    total = len(names)
    # domain='all' 是"看全量"的显式请求（qd_system.txt 亦如此承诺），不做截断，
    # 否则会静默藏掉一半工具、与提示词自相矛盾；其余域/关键词浏览仍受 limit 约束。
    if domain != "all":
        names = names[: max(1, limit)]
    import inspect
    core_names = set(getattr(agent, "_core_names", None) or ()) if agent else set()
    lines = [f"工具清单（{len(names)}/{total}，domain='{domain or 'common'}'）："]
    for n in names:
        fn = p.get(n)
        if fn is None:
            # 【T1·2026-10-16】非 provider 工具（mimo 原生）：注册表取描述与参数名
            obj = reg.get(n) if reg is not None else None
            if obj is None:
                continue
            desc = str(getattr(obj, "description", "") or "").strip().split("\n")[0][:100]
            sig_str = ""
            try:
                params = (obj.get_function_definition() or {}).get(
                    "function", {}).get("parameters", {})
                sig_str = ", ".join((params.get("properties") or {}).keys())
            except Exception:
                sig_str = ""
            mark = "" if n in core_names else "[未激活] "
            lines.append(f"  - {mark}{n}({sig_str}) — {desc}")
            continue
        desc = (inspect.getdoc(fn) or "").strip().split("\n")[0][:100]
        sig_str = ""
        try:
            sig = inspect.signature(fn)
            sig_str = ", ".join(
                pname for pname in sig.parameters if not pname.startswith("_"))
        except Exception:
            sig_str = ""
        lines.append(f"  - {n}({sig_str}) — {desc}")
    lines.append("按需域（默认未激活，需 search_tools/activate_tools）：")
    lines.append(_domains_overview(p, agent))
    if domain != "all":
        lines.append("提示：domain='all' 可看全部；按需层工具须 activate_tools 激活后才能调用。")
    return "\n".join(lines)


def search_tools(query: str, domain: str = "", count: int = 8, _context: Any = None) -> str:
    """按关键词检索工具，并**自动激活**命中的前 N 个（之后即可直接调用）。

    与 list_tools 的区别：本工具会激活（把 schema 加进后续请求的工具清单），
    list_tools 只列名不激活。

    Args:
        query: 关键词（中英文均可，如 "资金流"、"龙虎榜"、"realtime"）。
        domain: 限定域（空=全部域）。
        count: 最多返回/激活条数，默认 8，上限 20。

    Returns:
        命中工具名 + 签名 + 简介，并注明已激活数量。
    """
    if not query:
        return "请提供搜索关键词。"
    count = max(1, min(int(count), 20))
    p = _provider()
    matched = p.search(query, domain=domain)
    names = [n for n, _ in matched[:count]]

    # 【T1·2026-10-16】provider 之外还有注册表工具（mimo 原生 read/write/edit/
    #  glob/grep/task/bash/compact 以及 update_plan）；它们不在 provider.search
    #  的检索范围内 ⇒ 一旦被移出必注入就是"搜也搜不到"。这里按同一关键词补检。
    agent0 = _agent_of(_context)
    reg0 = getattr(agent0, "tool_registry", None) if agent0 is not None else None
    if reg0 is not None:
        q = str(query).lower()
        extra = []
        for n in reg0.list_tools():
            try:
                d = str(getattr(reg0.get(n), "description", "") or "")
            except Exception:
                d = ""
            if q in n.lower() or q in d.lower():
                extra.append(n)
        names = names + [n for n in sorted(extra) if n not in set(names)]

    if not names:
        return (f"未找到匹配 '{query}' 的工具。"
                f"可用域：{', '.join(p.get_domains()) or '无'}；"
                f"也可 list_tools(domain='all') 全量查看。")

    agent = agent0
    activated: List[str] = []
    if agent is not None and hasattr(agent, "activate_tools"):
        res = agent.activate_tools(names[:_AUTO_ACTIVATE_MAX], source="auto")
        activated = list(res.get("activated", []))

    import inspect
    lines = [f"命中 {len(names)} 个，展示 {len(names)} 个，已激活 {len(activated)} 个："]
    for n in names:
        # 【T1·2026-10-16】provider 没有的工具走注册表取描述（mimo 原生的
        #  read/write/edit/glob/grep/task/bash/compact 等）。原先只遍历 provider
        #  命中 ⇒ 注册表命中即便找得到也不显示，等于白补。
        fn = p.get(n)
        sig_str = ""
        if fn is not None:
            desc = str(inspect.getdoc(fn) or "").strip().split("\n")[0][:120]
            try:
                sig = inspect.signature(fn)
                sig_str = ", ".join(
                    pname for pname in sig.parameters if not pname.startswith("_"))
            except Exception:
                sig_str = ""
        else:
            obj = reg0.get(n) if reg0 is not None else None
            if obj is None:
                continue
            desc = str(getattr(obj, "description", "") or "").strip().split("\n")[0][:120]
            try:
                params = (obj.get_function_definition() or {}).get(
                    "function", {}).get("parameters", {})
                sig_str = ", ".join((params.get("properties") or {}).keys())
            except Exception:
                sig_str = ""
        mark = "[已激活]" if n in activated else "[未激活]"
        lines.append(f"  {mark} {n}({sig_str}) — {desc}")
    # 仅当本轮一个都没激活时才提示（原先误写成 for-else：循环内无 break ⇒ 恒执行，
    # 与上方 [已激活] 标记自相矛盾）。
    if not activated:
        lines.append("未自动激活：请用 activate_tools 点名激活后再调用。")
    return "\n".join(lines)


def activate_tools(names: str, _context: Any = None) -> str:
    """点名激活按需层工具（激活后其完整 schema 才进入后续请求的工具清单）。

    Args:
        names: 工具名，逗号/空格分隔，如 "get_realtime_quote,get_fund_flow"。

    Returns:
        激活结果：已激活 / 本就激活 / 未找到。
    """
    import re
    raw = re.split(r"[,\s]+", (names or "").strip())
    want = [n for n in raw if n]
    if not want:
        return "请给出要激活的工具名（逗号或空格分隔）。"
    agent = _agent_of(_context)
    if agent is None or not hasattr(agent, "activate_tools"):
        return f"当前无活动会话，无法激活：{', '.join(want)}（请通过 agent 调用本工具）。"
    res = agent.activate_tools(want)
    activated = res.get("activated", [])
    already = res.get("already", [])
    unknown = res.get("unknown", [])
    off = res.get("disabled", [])
    lines = [f"激活结果：新激活 {len(activated)}，已激活 {len(already)}，未找到 {len(unknown)}"
             + (f"，已停用 {len(off)}" if off else "") + "。"]
    if activated:
        lines.append("新激活: " + ", ".join(activated))
    if unknown:
        lines.append("未找到: " + ", ".join(unknown) + "（用 search_tools 查准确名字）")
    if off:
        # ★ T7（2026-10-17）：disabled 不是"未找到"——静默归零会让模型反复重试。
        #   明示停用状态；直调仍可回退执行（execute_action fail-open），但不会
        #   进工具面，别把任务押在它上面。
        lines.append("已停用: " + ", ".join(off)
                     + "（当前被 tool_connect 禁用——不会进工具清单；如确需调用请直呼其名走回退通道）")
    lines.append(f"当前已激活按需工具 {res.get('active_total', 0)}/{res.get('cap', 0)} 个。")
    return "\n".join(lines)

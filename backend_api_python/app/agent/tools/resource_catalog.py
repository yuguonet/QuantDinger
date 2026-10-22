# -*- coding: utf-8 -*-
"""list_resources / search_resources —— 统一发现层（v52，2026-10-22 方案 §1-§8 + §12 v2）。

【定位】把 list_tools/search_tools/list_skills/list_capabilities/search_capabilities
五套发现动词收敛为 2 个统一口：LLM 事先不必决定调的是 skill/tool/capability 层，
由本模块内部级联 skill→tool→capability。

【边界（方案裁定，不得越界）】
  - 执行层动词完全不动：run_skill/read_skill/call_capability/activate_tools 照旧
    （已合并至 tools/exec_tools.py，AP.7）；
  - 各层内部排序/评分不动：只**消费**各层既有已排序输出（provider 原评分域序 /
    skills priority 降序 / capability admission 序），绝不二次排序；
  - 注册表原生工具、admission.json 准入语义、能力层三闸门均不动。

【级联语义】
  - search_resources：依次 skill→tool→capability，命中即返回首非空层（不跨层混排）；
    kind 显式指定单层时只查该层（不下探）。query 空/全空白 → 直接返回用法提示
    （P0-2 fail-fast，不级联）——否则 skill 层恒非空，tool/capability 永不可达。
  - list_resources：三层按 skill→tool→capability 合并浏览；每层先截 top-N
    （QD_DISCOVERY_LAYER_TOPN，默认 20，P0-3 防三层量级悬殊把 capability 挤出
    分页视野）再对合并扁平序列统一分页；头部标注各层总数与是否截断。

【P0-1 skill 层过滤职责】skills_adapter.list_skills() 无 query 参数 ⇒ skill 层的
query 过滤由本模块做：对 name/description/tags 子串匹配、大小写不敏感。
过滤≠排序：priority 序原样保留（对拍守卫 tests/test_v52_unified_discovery_20261022.py 锁死）。

【同名去重（唯一新增逻辑）】capability 的 name 已存在于工具层 provider ⇒ 丢弃该
capability 条目 —— 与「工具层>能力层」同名遮蔽裁定一致（exec_tools.call_capability
的 F4 同名直执行），堵住 list_capabilities 从 admission.json 直读、被遮蔽名仍残留的裂缝。

【激活纪律下沉（§5）】tool 层进入结果集时复用与 search_tools 同一机制同一上限：
agent.activate_tools(names[:_AUTO_ACTIVATE_MAX], source="auto")（env
QD_AUTO_ACTIVATE_MAX）。activate_tools 本身幂等（已激活进 already 桶、不重复登记），
重复 search 不产生重复激活（P1-5）。无 _context/agent 时退化为"只列不激活"
（与 search_tools 行为一致）。_AUTO_ACTIVATE_MAX 模块级取值（调用时读取），
env 调整即时生效。

【AP.7·2026-10-22 旧发现动词并入】list_tools/search_tools/list_skills/
list_capabilities/search_capabilities 从 LLM 工具面摘除后，唯一程序化消费方就是
本模块，故直接并入为 `_` 前缀内部函数（_list_tools/_search_tools/_list_skills/
_list_capabilities/_search_capabilities），连同其辅助 _provider/_agent_of/
_registry_supplement/_AUTO_ACTIVATE_MAX 一并收拢。原 tools/tool_discovery.py、
tools/skill_tools.py、tools/capability_tools.py 三个文件已删除（备份见
del/tools_merge_20261022/）。函数体逐字保真，仅改名加下划线。

【易错点】
  - 依赖方向单向：本模块 → exec_tools（执行层动词实现），**不得反向**
    import（防环）。tool 层的注册表原生工具补检与 search_tools T1 同口径：mimo 原生
    read/write/edit/... 不在 provider.search 检索范围，漏补检 = 移出必注入层后
    "搜也搜不到"。
  - kind 非法值 fail-fast 返回合法值列表（P1-4，报错风格对齐 skill_run 的白名单）。
  - 本文件被 tools/scan_directory 自动注册为普通工具：公开函数只有
    list_resources/search_resources 两个，其余一律 _ 前缀。
  - `_context` 由 FnToolAdapter 注入（下划线前缀不进 schema，模型看不到），用于
    拿 agent（激活登记 / tool 条目 state 判定 / 注册表补检）；直接脚本调用时可省略。
"""
from __future__ import annotations

import inspect
import os
from typing import Any, List, Optional, Tuple

# ═══════════════════════════════════════════════════════════
#  AP.7 并入：旧 5 发现动词内部实现（原 tool_discovery / skill_tools /
#  capability_tools，函数体逐字保真，仅改 _ 前缀）
# ═══════════════════════════════════════════════════════════

def _provider():
    """取全局 provider（与 QDAgent 扫描同源同口径 —— 否则元工具/子域工具会对不上）。"""
    from ._base import ToolProvider
    return ToolProvider.get_or_build()


def _agent_of(_context: Any):
    return (_context or {}).get("agent") if isinstance(_context, dict) else None


#: 一次 search_tools 最多自动激活的工具数（防止"搜一次把整个域全激活"，
#: 那样就等于没分层）。超出部分只列名字，模型要用再 activate_tools 点名。
_AUTO_ACTIVATE_MAX = int(os.getenv("QD_AUTO_ACTIVATE_MAX", "8"))


def _domains_overview(p, agent: Any = None) -> str:
    """按需域概览：只给"域名 + 数量"，不给全量名单（省 token 且引导检索）。

    【T1·2026-10-16】agent 在会话中时，改按**注册表全量口径**列层（provider 之外
    还有 mimo 原生的 read/write/edit/glob/grep/task/bash/compact）。原先只列 provider
    域 ⇒ 原生工具在 `_list_tools(domain='all')` 里**根本不出现**，一旦被
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
            lines.append(f"  - {dom}: {len(names)} 个（用 search_resources 检索）")
    return "\n".join(lines) or "  （无）"


def _list_tools(domain: str = "", query: str = "", limit: int = 40, _context: Any = None) -> str:
    """列出可用工具（按需层需要先用本工具或 search_resources 找到，再 activate_tools 激活）。

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
    lines.append("按需域（默认未激活，需 search_resources/activate_tools）：")
    lines.append(_domains_overview(p, agent))
    if domain != "all":
        lines.append("提示：domain='all' 可看全部；按需层工具须 activate_tools 激活后才能调用。")
    return "\n".join(lines)


def _registry_supplement(query: str, names: List[str], _context: Any) -> List[str]:
    """【T1·2026-10-16】注册表原生工具补检 —— search_tools 与统一口共用的单一真源。

    【T1 事故来源】2026-10-16：mimo 原生 read/write/edit/glob/grep/task/bash/compact
    及 update_plan 等注册表工具不在 provider.search 检索范围内 ⇒ 一旦被移出必注入层
    就是"搜也搜不到"（T1）。本函数按同一关键词对注册表补检：小写子串匹 name+
    description，sorted 去重合并进 names（provider 原序在前、补检按名排序在后）。

    【易错点】search_tools（渲染文本）与统一口结构化名单的语义差异只在下游渲染——
    补检口径必须且只能在这里维护一份，改两份必漂移。
    """
    agent = _agent_of(_context)
    reg = getattr(agent, "tool_registry", None) if agent is not None else None
    if reg is None:
        return names
    q = str(query).lower()
    extra = []
    for n in reg.list_tools():
        try:
            d = str(getattr(reg.get(n), "description", "") or "")
        except Exception:
            d = ""
        if q in n.lower() or q in d.lower():
            extra.append(n)
    return names + [n for n in sorted(extra) if n not in set(names)]


def _search_tools(query: str, domain: str = "", count: int = 8, _context: Any = None) -> str:
    """按关键词检索工具，并**自动激活**命中的前 N 个（之后即可直接调用）。

    与 _list_tools 的区别：本工具会激活（把 schema 加进后续请求的工具清单），
    _list_tools 只列名不激活。

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
    #  的检索范围内 ⇒ 一旦被移出必注入就是"搜也搜不到"。这里按同一关键词补检
    # （单一真源：_registry_supplement，统一口同用）。
    agent0 = _agent_of(_context)
    names = _registry_supplement(query, names, _context)

    if not names:
        return (f"未找到匹配 '{query}' 的工具。"
                f"可用域：{', '.join(p.get_domains()) or '无'}；"
                f"也可 _list_tools(domain='all') 全量查看。")

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
            # 注：原 search_tools 此处误用未定义变量 reg0（有 agent 上下文且命中
            # 注册表原生工具时 NameError）。并入内部接口时修正为经 agent 推导 reg。
            reg = getattr(agent, "tool_registry", None) if agent is not None else None
            obj = reg.get(n) if reg is not None else None
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


def _get_skills():
    from ..agent import skills as _s
    return _s


def _list_skills() -> Dict[str, Any]:
    """列出全部可用技能（名称 + 简介）。

    Returns:
        {"count": N, "skills": [{name, description}, ...]}
    """
    try:
        return {"count": len(_get_skills()), "skills": _get_skills().list_skills()}
    except Exception as e:
        return {"error": str(e)}


def _load_loader():
    from ..capabilities import loader as _l
    return _l


def _admitted_meta() -> List[dict]:
    try:
        return _load_loader().load_admitted_meta() or []
    except Exception:
        return []


def _list_capabilities() -> Dict[str, Any]:
    """列出全部准入能力（名称 + 模块 + 简介）。低优先级：仅按需调用。

    Returns:
        {"count": N, "capabilities": [{name, module, description}, ...]}
    """
    metas = _admitted_meta()
    out = []
    for m in metas:
        out.append({
            "name": m.get("name", ""),
            "module": m.get("module", m.get("mod_path", "")),
            "description": (m.get("description") or m.get("doc") or "")[:200],
        })
    return {"count": len(out), "capabilities": out}


def _search_capabilities(query: str, count: int = 8) -> Dict[str, Any]:
    """按关键词搜索能力层函数（名称/模块/简介模糊匹配）。

    Args:
        query: 搜索关键词
        count: 最多返回条数

    Returns:
        {"count": N, "matches": [{name, module, description}, ...]}
    """
    metas = _admitted_meta()
    keys = [k.lower() for k in (query or "").split() if k]
    scored = []
    for m in metas:
        hay = f"{m.get('name', '')} {m.get('module', m.get('mod_path', ''))} {m.get('description', m.get('doc', ''))}".lower()
        score = sum(1 for k in keys if k in hay)
        if score or not keys:
            scored.append((score, m))
    scored.sort(key=lambda x: -x[0])
    matches = [{
        "name": m.get("name", ""),
        "module": m.get("module", m.get("mod_path", "")),
        "description": (m.get("description") or m.get("doc") or "")[:200],
    } for _, m in scored[:count]]
    return {"count": len(matches), "matches": matches}


# ═══════════════════════════════════════════════════════════════
#  常量与统一口（v52 原实现）
# ═══════════════════════════════════════════════════════════════

#: kind 合法取值白名单（P1-4；"" 与 "all" 等价 = 三层级联/合并）
_KINDS = ("", "all", "skill", "tool", "capability")

#: 层级联固定顺序（方案 §4：skill→tool→capability，不得改写）
_ORDER = ("skill", "tool", "capability")

#: list_resources 每层截断上限（P0-3）。env 可调，模式对齐 QD_AUTO_ACTIVATE_MAX
#:（模块常量 + env，不硬编码）。
_LAYER_TOPN = int(os.getenv("QD_DISCOVERY_LAYER_TOPN", "20"))

#: search_resources 单层返回条数上限（与 search_tools 的 count 上限同口径）
_SEARCH_COUNT_MAX = 20

#: search_resources 空 query 的用法提示（P0-2 fail-fast：不级联、不静默）
_USAGE_HINT = (
    "用法提示（query 为空，未级联）：\n"
    "- search_resources(query, kind='', count=8)：按关键词检索，命中即返回首非空层"
    "（kind 为 ''/'all' 时按 skill→tool→capability 级联；显式 kind 只查该层）。\n"
    "- list_resources(kind='', query='', page=1, limit=40)：三层合并浏览"
    "（每层截 top-N 后统一分页）。\n"
    "- query 不能为空：空 query 时 skill 层恒非空 ⇒ tool/capability 永不可达，"
    "故 fail-fast。kind 合法值：" + str(list(_KINDS))
)


# ═══════════════════════════════════════════════════════════════
#  三层数据源（委派，不重写：只消费各层既有已排序输出）
# ═══════════════════════════════════════════════════════════════

def _skill_entries(query: str) -> Tuple[List[dict], int, Optional[str]]:
    """skill 层条目（P0-1：过滤在本模块做，priority 序原样保留）。

    数据源 _list_skills()（priority 降序信封 {"count","skills"}），
    对 name/description/tags 做大小写不敏感子串过滤；**过滤≠排序**，不改写原序。
    返回 (过滤后条目, 过滤后总数, 层错误提示或 None)。
    """
    data = _list_skills()
    if not isinstance(data, dict):
        return [], 0, f"skill 层返回异常类型 {type(data).__name__}"
    if data.get("error"):
        return [], 0, f"skill 层获取失败: {data['error']}"
    skills = data.get("skills") or []
    q = (query or "").strip().lower()
    if not q:
        return list(skills), len(skills), None
    kept = []
    for s in skills:
        tags = s.get("tags")
        tags_txt = " ".join(map(str, tags)) if isinstance(tags, (list, tuple)) else str(tags or "")
        hay = " ".join((str(s.get("name", "")),
                        str(s.get("description", "")), tags_txt)).lower()
        if q in hay:
            kept.append(s)
    return kept, len(kept), None


def _tool_search(query: str, count: int, _context: Any) -> Tuple[List[str], int]:
    """tool 层检索：provider.search 原评分域序 + 注册表原生工具补检（与 search_tools 同口径）。

    【为什么不直接调 _search_tools】它返回渲染文本，统一口需要结构化名单拼统一格式；
    这里复用同一数据源（provider.search）与同一补检单一真源
    （_registry_supplement，T1 口径只此一份）及同一激活机制（模块头 §5），
    不重写评分、不另写激活分支。返回 (工具名列表[已截 count], provider 命中总数)。
    """
    p = _provider()
    matched = p.search(query, domain="")
    names = [n for n, _ in matched[:count]]
    names = _registry_supplement(query, names, _context)
    return names, len(matched)


def _tool_browse(query: str, _context: Any) -> Tuple[List[str], int]:
    """tool 层浏览：list_by_domain 全量 + 注册表原生补并（与 _list_tools(domain='all') 同口径）
    + query 过滤（与 _list_tools 同判据：名称/简介子串）。返回 (名单, 过滤后总数)。
    """
    p = _provider()
    agent = _agent_of(_context)
    reg = getattr(agent, "tool_registry", None) if agent is not None else None
    names = list(p.list_by_domain(""))
    if reg is not None:
        names = names + sorted(n for n in reg.list_tools() if n not in set(names))
    q = (query or "").strip().lower()
    if q:
        kept = []
        for n in names:
            fn = p.get(n)
            if fn is not None:
                desc = inspect.getdoc(fn) or ""
            elif reg is not None:
                obj = reg.get(n)
                desc = str(getattr(obj, "description", "") or "") if obj is not None else ""
            else:
                desc = ""
            if q in n.lower() or q in str(desc).lower():
                kept.append(n)
        names = kept
    return names, len(names)


def _capability_items(query: str, count: int,
                      search_mode: bool) -> Tuple[List[dict], int, Optional[str]]:
    """capability 层条目 + 同名去重（工具层>能力层）。

    search_mode=True → _search_capabilities（自带打分序）；False → _list_capabilities
    （admission 序）。输出原样消费不重排；name 已存在于 provider 工具注册表的条目
    丢弃（admission.json 直读会残留被同名工具遮蔽的能力，见模块头「同名去重」）。
    """
    if search_mode:
        data = _search_capabilities(query, count=count)
        items = data.get("matches") or [] if isinstance(data, dict) else []
    else:
        data = _list_capabilities()
        items = data.get("capabilities") or [] if isinstance(data, dict) else []
    err = None
    if isinstance(data, dict) and data.get("error"):
        err = f"capability 层获取失败: {data['error']}"
    p = _provider()
    shadowed = set(p.get_tool_names()) if p is not None else set()
    kept = [m for m in items if str(m.get("name", "")) not in shadowed]
    return kept, len(kept), err


# ═══════════════════════════════════════════════════════════════
#  统一渲染（每条带 kind 标识 + next_action，方案 §4 统一记录结构）
# ═══════════════════════════════════════════════════════════════

def _tool_meta(name: str, agent: Any) -> Tuple[str, str, bool]:
    """tool 条目元数据：(参数签名, 一行描述, 是否已在工具面)。

    provider 函数用 inspect.signature（跳过 _ 前缀参数，与 _list_tools 同判据）；
    注册表原生工具走 get_function_definition。state 判定：core/已激活 → active；
    无 agent 时一律 deferred（与 _list_tools 无 agent 全标 [未激活] 同口径）。
    """
    p = _provider()
    reg = getattr(agent, "tool_registry", None) if agent is not None else None
    sig_str, desc = "", ""
    fn = p.get(name)
    if fn is not None:
        desc = str(inspect.getdoc(fn) or "").strip().split("\n")[0][:120]
        try:
            sig_str = ", ".join(pn for pn in inspect.signature(fn).parameters
                                if not pn.startswith("_"))
        except Exception:
            sig_str = ""
    elif reg is not None:
        obj = reg.get(name)
        if obj is not None:
            desc = str(getattr(obj, "description", "") or "").strip().split("\n")[0][:120]
            try:
                params = (obj.get_function_definition() or {}).get(
                    "function", {}).get("parameters", {})
                sig_str = ", ".join((params.get("properties") or {}).keys())
            except Exception:
                sig_str = ""
    active = False
    if agent is not None:
        active = (name in set(getattr(agent, "_core_names", None) or ())
                  or name in set(getattr(agent, "_activated", None) or ()))
    return sig_str, desc, active


def _tool_line(name: str, agent: Any) -> str:
    sig_str, desc, active = _tool_meta(name, agent)
    hint = "已激活，直接调用" if active else f"activate_tools('{name}') 后直调"
    return (f"[tool] {name}({sig_str}) — {desc} ｜ "
            f"state={'active' if active else 'deferred'} ｜ "
            f"next_action: activate_and_call（{hint}）")


def _skill_line(s: dict) -> str:
    return (f"[skill] {s.get('name', '')} — {str(s.get('description', ''))[:160]} ｜ "
            f"next_action: run_skill(name='{s.get('name', '')}')")


def _cap_line(m: dict) -> str:
    return (f"[capability] {m.get('name', '')} — {str(m.get('description', ''))[:160]} ｜ "
            f"next_action: call_capability(name='{m.get('name', '')}')")


def _finish(header: str, rows: List[str], notes: List[str]) -> str:
    """头部 + 条目 + 层错误注记（错误不静默，但也不让单层故障遮蔽命中层结果）。"""
    lines = [header] + rows
    lines.extend("注：" + n for n in notes)
    return "\n".join(lines)


def _kind_reject(kind: str, semantic: str) -> str:
    """P1-4：kind 非法值 fail-fast，白名单报错风格对齐 skill_run（未知X。可执行: [...]）。"""
    return (f"未知 kind='{kind}'。可执行: {list(_KINDS)}（{semantic}）")


# ═══════════════════════════════════════════════════════════════
#  统一口（唯一两个公开函数，随 scan_directory 自动注册）
# ═══════════════════════════════════════════════════════════════

def list_resources(kind: str = "", query: str = "", page: int = 1, limit: int = 40,
                   _context: Any = None) -> str:
    """统一发现口：列出技能/工具/能力（仅发现层，执行层动词不动）。

    kind: ""/"all" → 三层按 skill→tool→capability 顺序合并浏览；
          "skill"/"tool"/"capability" → 只取该层。
    query: 关键词过滤（各层内部检索/排序不变；skill 层过滤由本模块做）。
    page/limit: 合并后的扁平序列分页（每层先截 top-N 再合并）。

    Returns:
        统一格式文本：头部含各层 总数/截断标记，每条带 [kind] 标识 + next_action。
    """
    kind = "" if kind is None else str(kind).strip().lower()
    if kind not in _KINDS:
        return _kind_reject(kind, "''/all=三层合并浏览；显式 kind=只取该层")
    agent = _agent_of(_context)
    want = set(_ORDER) if kind in ("", "all") else {kind}
    layers: List[Tuple[str, List[str], int, bool]] = []  # (kind, 行, 过滤后总数, 是否截断)
    notes: List[str] = []
    for layer in _ORDER:
        if layer not in want:
            continue
        if layer == "skill":
            entries, total, err = _skill_entries(query)
            if err:
                notes.append(err)
            rows = [_skill_line(s) for s in entries[:_LAYER_TOPN]]
        elif layer == "tool":
            names, total = _tool_browse(query, _context)
            rows = [_tool_line(n, agent) for n in names[:_LAYER_TOPN]]
        else:
            items, total, err = _capability_items(query, count=_LAYER_TOPN, search_mode=False)
            if err:
                notes.append(err)
            rows = [_cap_line(m) for m in items[:_LAYER_TOPN]]
        layers.append((layer, rows, total, total > _LAYER_TOPN))

    flat = [row for _, rows, _, _ in layers for row in rows]
    grand = len(flat)
    if grand == 0:
        msg = (f"未找到匹配的资源（kind='{kind or 'all'}'"
               + (f"，query='{query}'" if str(query).strip() else "") + "）。"
                 "可 list_resources() 全量浏览。")
        return msg + ("".join("\n注：" + n for n in notes))
    limit_i = max(1, int(limit))
    pages = max(1, (grand + limit_i - 1) // limit_i)
    page_i = min(max(1, int(page)), pages)
    shown = flat[(page_i - 1) * limit_i: page_i * limit_i]
    summary = " · ".join(
        f"{name} {len(rows)}/{total}" + (f"（已截 top {_LAYER_TOPN}）" if trunc else "")
        for name, rows, total, trunc in layers)
    lines = [f"资源清单（第 {page_i}/{pages} 页，本页 {len(shown)}/{grand} 条 ｜ {summary}"]
    lines.extend(shown)
    lines.append("提示：检索用 search_resources(query, kind=)；执行层动词不变"
                 "（run_skill / read_skill / call_capability / activate_tools）。")
    lines.extend("注：" + n for n in notes)
    return "\n".join(lines)


def search_resources(query: str, kind: str = "", count: int = 8,
                     _context: Any = None) -> str:
    """统一发现口：按关键词检索并自动级联 skill→tool→capability。

    命中即返回首非空层结果（不跨层混排）；tool 命中走自动激活纪律
    （与 search_tools 同一机制同一上限）。
    query 为空/全空白 → 返回用法提示（fail-fast，不级联）。

    Returns:
        统一格式文本：头部注明命中层与已激活数，每条带 [kind] 标识 + next_action。
    """
    kind = "" if kind is None else str(kind).strip().lower()
    if kind not in _KINDS:
        return _kind_reject(kind, "''/all=按 skill→tool→capability 级联；显式 kind=只查该层")
    q = str(query or "").strip()
    if not q:
        return _USAGE_HINT  # P0-2：fail-fast，不级联
    count = max(1, min(int(count), _SEARCH_COUNT_MAX))
    agent = _agent_of(_context)
    order = list(_ORDER) if kind in ("", "all") else [kind]
    notes: List[str] = []
    for layer in order:
        if layer == "skill":
            entries, total, err = _skill_entries(q)
            if err:
                notes.append(err)
            if entries:
                head = (f"统一检索「{q}」→ 命中层：skill"
                        f"（{min(len(entries), count)}/{total} 条）")
                return _finish(head, [_skill_line(s) for s in entries[:count]], notes)
        elif layer == "tool":
            names, total = _tool_search(q, count, _context)
            if names:
                activated: List[str] = []
                if agent is not None and hasattr(agent, "activate_tools"):
                    # §5 激活纪律下沉：与 search_tools 同一机制、同一上限。
                    res = agent.activate_tools(names[:_AUTO_ACTIVATE_MAX], source="auto")
                    activated = list(res.get("activated", []))
                rows = [_tool_line(n, agent) for n in names]
                if not activated:
                    rows.append("未自动激活：请用 activate_tools 点名激活后再调用。")
                rows.append("提示：级联顺序 skill→tool→capability，仅展示首非空层"
                            "（要看下层用 kind= 显式指定）。")
                head = (f"统一检索「{q}」→ 命中层：tool"
                        f"（{len(names)}/{total} 条，已激活 {len(activated)}）")
                return _finish(head, rows, notes)
        else:
            items, total, err = _capability_items(q, count=count, search_mode=True)
            if err:
                notes.append(err)
            if items:
                head = (f"统一检索「{q}」→ 命中层：capability"
                        f"（{min(len(items), count)}/{total} 条）")
                return _finish(head, [_cap_line(m) for m in items[:count]], notes)
    msg = (f"未找到匹配 '{q}' 的资源（kind='{kind or 'all'}'，"
           f"已按 {'→'.join(order)} 级联）。可 list_resources() 全量浏览，"
           "或换关键词/指定 kind 重试。")
    return msg + ("".join("\n注：" + n for n in notes))
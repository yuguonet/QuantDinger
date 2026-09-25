# -*- coding: utf-8 -*-
"""Plan Linter（提智方案 二波 B1 · R1~R4 确定性部分，2026-09-24）——规划产物的产前检查。

职责（**只做确定性判断，不含 LLM**；LLM critic / Best-of-N 在 utils/plan_critic.py）：
  · **R1 工具覆盖**：从 task 正文识别"数据域需求"（行情/资金流/财务/板块/龙虎榜…），逐个断言
    「交付承诺的信息必须有对应取数工具」落在本轮**沙箱工具面**内；命中缺失 → 告警 + 留痕，
    词典高置信时补进点名通道。词典为主，2-gram 倒排索引兜底长尾。
  · **R2 隐式依赖**：下游阶段消费"上游阶段产出的代码清单/股票池"却没标 `barrier` 时，自动补
    `barrier=True`（WARN 不阻塞）。这是并行化（远期 F1）的地基——barrier 不对，"同批并行"
    会把有依赖的阶段塞进同一个 CodeAgent 会话。
  · **R3 预算-粒度失配**（2026-09-24 B1-B 补齐）：单阶段预算顶格且验收 >3 条 → "粒度过粗"；
    两相邻阶段各 ≤2 步且无 barrier → "建议合并"。**只出信号不机械拆合**（裁决：粒度信号
    只回炉让 planner 重出，防 acceptance 语义脱绑），信号进 LintReport.granularity →
    plan_critic.format_defects → 回炉一次。
  · **R4 工具面裁剪**（2026-09-24 B1-B 补齐）：planner 声明的工具里"与任务零相关"的条目
    （正文没提、倒排索引零命中、非清单保护名单）→ 裁掉，削减误用面、降幻觉调用概率。
    裁剪口径是**零相关**而非原文的"裁到并集"（本契约 phases[].tools 即执行面，无独立
    "声明 vs 引用"两层；误杀比漏杀烦人——裁决原则），保留数不低于 R4_MIN_FACE。

关键设计点：
  · **单一数据表** `_DATA_DOMAINS`（域 → 关键词 + 候选工具 + 首选工具）：关键词与工具同表，
    避免两处漂移；`DATA_DOMAIN_TOOLS` 由它派生，供 C7 的 CI 断言「词典工具名 ⊆ provider 注册表」。
  · **决策与应用分离**：`lint_plan()` 是纯函数，只产出 `LintReport`（含"该往哪儿加"）；
    `apply_report()` 按报告就地改 `phases`/`plan_tools`。分成两步是为了可单测、可审计。
  · **置信度分层**：① 词典命中 = 高置信 → 允许补工具；② 仅 2-gram 相关 = 低置信 → 只告警
    不动工具面（用相关性去吹工具面，token 成本立刻上升；本项目对 token 敏感）。
  · **不越权**：阶段白名单只在"该阶段自身也命中同一数据域"时才补，否则只告警——不因一棵树的
    需要就放宽整片林子（既有设计：顶层 tools 不静默并进每个阶段）。

易错点（改本文件前先读）：
  · `phases[].tools` 是**严格白名单**；`tools_declared=False` 才是"未声明 → 回退域全集"。覆盖
    检查必须按 `tools_declared` 分支，否则对"未声明"的阶段会整片误报缺失。
  · **只往 `tools_declared=True` 的阶段补工具**：给"未声明"的阶段加 tools 会把它的语义从
    "回退域全集"改成"严格白名单"，工具面**反而缩小**——这是静默变窄的坑。
  · 能力层（capability 域）工具**不在** selected_domain 基调里，必须被点名才注入。R1 最有价值
    的命中场景正是它（历史"能力层断链"：planner 提示展示了能力，执行时无从调用）。
  · `names_in_text()` 与 `agents/task_agent.py::_salvage_tools_from_text` 同源但**本模块自持**
    一份：task_agent 依赖本模块，反向 import 会成环，故不以"复用"名义反向依赖。
  · R2 只对**声明了 tools 的阶段**判依赖：未声明的阶段回退域全集，若两阶段被并进同一批，本就
    在同一个 CodeAgent 会话里顺序执行，跨会话依赖不存在，加了 barrier 反而是假告警。
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence, Tuple

__all__ = [
    "DATA_DOMAIN_TOOLS", "LIST_PRODUCERS", "LIST_CONSUMERS",
    "detect_domains", "names_in_text", "build_tool_index", "index_candidates",
    "list_tools", "granularity_hints", "LintReport", "lint_plan", "apply_report",
]

# R4 裁剪后的最小工具面（低于此数不再裁——空工具面比冗余更致命）
R4_MIN_FACE = 3

# ═══════════════════════════════════════════════════════════════════════════
#  数据域登记表（R1 词典：域 → 触发关键词 + 候选工具）
# ═══════════════════════════════════════════════════════════════════════════
# 2026-09-25：词典已抽到 tools/<domain>/domain_meta.py（DomainSpec.data_domains），
# 本模块只做拼接与派生视图，核心不再写死金融关键词/工具名。
# 每行 = (域, 关键词元组, 候选工具元组)。候选工具**首元素 = 首选**（补点名时用它）。
# 工具名必须逐字来自 provider 注册表（CI 断言见 tests/test_wiring.py 的 plan_linter 段）。
from domain_registry import (
    iter_data_domains as _iter_data_domains,
    list_consumers as _list_consumers,
    list_producers as _list_producers,
)

_DATA_DOMAINS: Tuple[Tuple[str, Tuple[str, ...], Tuple[str, ...]], ...] = _iter_data_domains()

# 域 → 候选工具（派生视图，供调用方/CI 断言使用；勿单独维护）
DATA_DOMAIN_TOOLS: Dict[str, Tuple[str, ...]] = {d: t for d, _kw, t in _DATA_DOMAINS}
# 域 → 首选工具
_PREFERRED: Dict[str, str] = {d: t[0] for d, _kw, t in _DATA_DOMAINS}

# ── R2 依赖登记表 ─────────────────────────────────────────────
# 生产者：调用后产出"一批代码/一个股票池"（下游必须等它跑完才知道做什么）。
LIST_PRODUCERS = _list_producers()
# 消费者：以"单票 / codes 清单"为单位取数或分析（输入依赖上一步的清单）。
LIST_CONSUMERS = _list_consumers()


def detect_domains(text: str) -> List[str]:
    """从文本识别数据域需求（按登记表顺序，去重）。无命中返回 []。"""
    s = str(text or "").lower()
    if not s:
        return []
    out: List[str] = []
    for dom, kws, _tools in _DATA_DOMAINS:
        for kw in kws:
            if kw in s:
                out.append(dom)
                break
    return out


def names_in_text(text: str, names) -> List[str]:
    """回收文本中**逐字出现**的工具名（词边界扫描，避免 get_fund_flow 命中 _daily 变体）。"""
    if not text or not names:
        return []
    hits = []
    for n in sorted(names):
        if re.search(r"(?<![A-Za-z0-9_])" + re.escape(n) + r"(?![A-Za-z0-9_])", text):
            hits.append(n)
    return hits


# ═══════════════════════════════════════════════════════════════════════════
#  2-gram 倒排索引（R1 兜底：词典覆盖不到的长尾找候选，能力层入同一索引）
# ═══════════════════════════════════════════════════════════════════════════
_INDEX_STOP = frozenset({
    # 高频无信息词（命中它们不代表相关性）
    "get", "data", "stock", "stocks", "list", "query", "info", "by", "code", "codes",
    "the", "and", "for", "with", "from", "day", "daily", "all", "of", "to", "in",
    "返回", "获取", "数据", "股票", "查询", "列表", "信息",
})

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*")


def _tokens(text: str) -> List[str]:
    """分词：ASCII 词 + 中文 2-gram（中文无空格，2-gram 是够用的相关性近似）。"""
    from collections import Counter
    cnt = Counter()
    for w in _TOKEN_RE.findall(str(text or "").lower()):
        if w not in _INDEX_STOP and len(w) > 2:
            cnt[w] += 1
    for seg in re.findall(r"[\u4e00-\u9fff]+", str(text or "")):
        for i in range(len(seg) - 1):
            g = seg[i:i + 2]
            if g not in _INDEX_STOP:
                cnt[g] += 1
    return list(cnt)


def build_tool_index(provider) -> Dict[str, set]:
    """建立 token → {工具名} 倒排索引（工具名 + docstring 首段）。"""
    import inspect
    idx: Dict[str, set] = {}
    if provider is None:
        return idx
    for name, func in (provider.get_functions() or {}).items():
        desc = ""
        try:
            desc = (inspect.getdoc(func) or "")[:300]
        except Exception:
            desc = ""
        for tk in set(_tokens(name + " " + desc)):
            idx.setdefault(tk, set()).add(name)
    return idx


# 进程级索引缓存：tools/ 目录运行期不变，扫描一次全程复用（与 nodes._SHARED_TOOL_PROVIDER 同理由）
_INDEX_CACHE: Dict[int, Dict[str, set]] = {}


def get_tool_index(provider) -> Dict[str, set]:
    """取（并缓存）provider 的工具倒排索引。"""
    if provider is None:
        return {}
    key = id(provider)
    if key not in _INDEX_CACHE:
        _INDEX_CACHE[key] = build_tool_index(provider)
    return _INDEX_CACHE[key]


def index_candidates(text: str, index: Dict[str, set], available_names,
                     *, min_hits: int = 3, limit: int = 8) -> List[str]:
    """按 token 重合度给候选工具（低置信，仅用于告警，不自动改工具面）。"""
    if not text or not index:
        return []
    avail = set(available_names or ())
    score: Dict[str, int] = {}
    for tk in set(_tokens(text)):
        for n in index.get(tk, ()):  # type: ignore[arg-type]
            if avail and n not in avail:
                continue
            score[n] = score.get(n, 0) + 1
    ranked = sorted((n for n, c in score.items() if c >= min_hits),
                    key=lambda n: (-score[n], n))
    return ranked[:limit]


# ═══════════════════════════════════════════════════════════════════════════
#  Lint 报告 + 检查 + 应用
# ═══════════════════════════════════════════════════════════════════════════
class LintReport:
    """检查结果（纯数据）。`coverage` 记录"哪个域缺覆盖"，`*_additions` 记录"该往哪加"。
    `granularity` = R3 粒度信号（回炉拆合提示）；`*_pruned*` = R4 工具面裁剪记录。"""

    __slots__ = ("coverage", "phase_tool_additions", "plan_tools_additions",
                 "barriers_added", "index_candidates", "warnings",
                 "granularity", "tool_pruned", "plan_tools_pruned")

    def __init__(self) -> None:
        self.coverage: List[dict] = []                    # {domain, tool, source, auto_added}
        self.phase_tool_additions: Dict[int, List[str]] = {}   # phase_id → [tool]
        self.plan_tools_additions: List[str] = []
        self.barriers_added: List[int] = []               # 自动补 barrier 的 phase_id
        self.index_candidates: List[str] = []             # 低置信候选（仅告警）
        self.warnings: List[str] = []
        self.granularity: List[str] = []                  # R3：粒度信号（回炉提示）
        self.tool_pruned: Dict[int, List[str]] = {}       # R4：phase_id → 裁掉的工具
        self.plan_tools_pruned: List[str] = []            # R4：顶层裁掉的工具

    @property
    def dirty(self) -> bool:
        return bool(self.coverage or self.barriers_added or self.warnings
                    or self.index_candidates or self.granularity
                    or self.tool_pruned or self.plan_tools_pruned)

    def to_trace(self) -> dict:
        """trace 载荷（键名稳定，供周报/评测聚合）。"""
        return {
            "coverage_missing": self.coverage,
            "phase_tool_additions": {str(k): v for k, v in self.phase_tool_additions.items()},
            "plan_tools_additions": self.plan_tools_additions,
            "barriers_added": self.barriers_added,
            "index_candidates": self.index_candidates,
            "warnings": self.warnings,
            "granularity": self.granularity,
            "tool_pruned": {str(k): v for k, v in self.tool_pruned.items()},
            "plan_tools_pruned": self.plan_tools_pruned,
        }


def _phase_face(phase: dict, base_tools) -> set:
    """单个阶段的**有效工具面**：声明了就用白名单；未声明 = 回退域全集。"""
    if phase.get("tools_declared"):
        return set(phase.get("tools") or ())
    return set(base_tools or ())


def _declared_face(phases: Sequence[dict], plan_tools, base_tools) -> set:
    """本轮实际注入的工具面（覆盖检查的比对基准）。"""
    face = set(plan_tools or ())
    if phases:
        for p in phases:
            face |= _phase_face(p, base_tools)
    else:
        face |= set(base_tools or ())
    return face


def granularity_hints(phases: Optional[Sequence[dict]]) -> List[str]:
    """R3 预算-粒度失配信号（纯函数；phases 可为**原始承诺**或规格化结构）。

    判据 A（过粗）：单阶段预算顶到 PLAN_PHASE_MAX_STEPS 上限且验收 >3 条 → 拆分。
    判据 B（过细）：两相邻阶段各 ≤2 步且无 barrier/replan 边界 → 合并。
    只出信号不机械拆合（裁决：回炉让 planner 重出，防 acceptance 语义脱绑）。
    阶段标识容忍缺 id（预选期传的是原始 phases，用序号代）。
    """
    out: List[str] = []
    phases = list(phases or [])
    if not phases:
        return out
    _phase_cap = 12  # 与 agents/task_agent.PLAN_PHASE_MAX_STEPS 对齐（惰性取真值，防漂移）
    try:
        from agents.task_agent import PLAN_PHASE_MAX_STEPS as _pms
        _phase_cap = _pms
    except Exception:
        pass
    for i, p in enumerate(phases):
        pid = p.get("id", i + 1)
        if int(p.get("step_budget") or 0) >= _phase_cap and len(p.get("acceptance") or ()) > 3:
            out.append("阶段 %s 粒度过粗（预算顶格 %d 步且验收 %d 条）——建议拆分"
                       % (pid, _phase_cap, len(p.get("acceptance") or ())))
    for i in range(1, len(phases)):
        up, cur = phases[i - 1], phases[i]
        if cur.get("barrier") or cur.get("replan") or up.get("replan"):
            continue
        ub = int(up.get("step_budget") or 0)
        cb = int(cur.get("step_budget") or 0)
        if 0 < ub <= 2 and 0 < cb <= 2:
            out.append("阶段 %s/%s 各 ≤2 步且无边界——建议合并"
                       % (up.get("id", i), cur.get("id", i + 1)))
    return out


def lint_plan(
    task: str,
    phases: Optional[Sequence[dict]] = None,
    plan_tools: Optional[Sequence[str]] = None,
    *,
    base_tools: Sequence[str] = (),
    available_names: Sequence[str] = (),
    index: Optional[Dict[str, set]] = None,
    protected_names: Sequence[str] = (),
) -> LintReport:
    """对规划产物做确定性检查（纯函数，不改入参）。

    Args:
        task: 规划器产出的任务书全文（数据域需求从这里识别）。
        phases: `_normalize_phases` 之后的结构（None/[] = 单段路径）。
        plan_tools: 顶层附加点名单。
        base_tools: selected_domain 的域基调工具名（空 = 无域，仅通用工具面）。
        available_names: provider 注册表全部工具名（校验词典/索引候选是否真实存在）。
        index: 工具倒排索引（None = 跳过兜底与 R4）。
        protected_names: R4 永不裁剪名单（技能工具/数据能力点名单——能力层断链教训：
            能力名恰是最脆弱的点名通道，宁可冗余也不裁）。

    Returns:
        LintReport。
    """
    phases = list(phases or [])
    plan_tools = list(plan_tools or [])
    rep = LintReport()
    # R2 depends_on（远期 F1 前置）：环/自依赖/未知 id 一律致命
    try:
        from app.agent.utils.phase_graph import validate_depends_on
        for _err in validate_depends_on(phases or []):
            rep.warnings.append(f"depends_on:{_err}")
    except Exception as _e:
        rep.warnings.append(f'depends_on_check_failed:{_e}')
    avail = set(available_names or ())
    face = _declared_face(phases, plan_tools, base_tools)

    # ── R1：数据域覆盖（词典，高置信）──
    for dom in detect_domains(task):
        cands = DATA_DOMAIN_TOOLS[dom]
        if set(cands) & face:
            continue                                   # 该域已有工具落在工具面内 → 覆盖 OK
        pick = _PREFERRED[dom]
        item = {"domain": dom, "tool": pick, "source": "dict"}
        if avail and pick not in avail:
            # 词典条目陈旧（工具被改名/下架）——报出来，别静默（C7 的运行时孪生信号）
            item["auto_added"] = False
            item["reason"] = "tool_not_registered"
            rep.warnings.append("数据域 %s 首选工具 %s 不在注册表（词典条目陈旧）" % (dom, pick))
            rep.coverage.append(item)
            continue
        added = _add_for_domain(rep, phases, plan_tools, dom, pick)
        item["auto_added"] = added
        if not added:
            rep.warnings.append(
                "数据域 %s 覆盖缺失（候选 %s）但无阶段 goal 命中该域，"
                "按「不越权」原则仅告警、不改阶段白名单" % (dom, "/".join(cands)))
        rep.coverage.append(item)

    # ── R1 兜底：2-gram 索引（低置信，仅告警）──
    if index:
        named = set(names_in_text(task, avail)) if avail else set()
        for n in index_candidates(task, index, avail):
            if n in face or n in named:
                continue
            rep.index_candidates.append(n)
        if rep.index_candidates:
            rep.warnings.append(
                "索引兜底候选未在工具面内（低置信，仅告警）：%s" % rep.index_candidates[:5])

    # ── R2：隐式依赖 → 自动补 barrier ──
    for i in range(1, len(phases)):
        up, cur = phases[i - 1], phases[i]
        if cur.get("barrier") or cur.get("replan"):
            continue                                   # 已标边界，无需干预
        if not (up.get("tools_declared") and cur.get("tools_declared")):
            continue                                   # 未声明 → 域全集，同批即同会话，无跨会话依赖
        if (set(cur.get("tools") or ()) & LIST_CONSUMERS) and \
           (set(up.get("tools") or ()) & LIST_PRODUCERS):
            rep.barriers_added.append(cur["id"])
    if rep.barriers_added:
        rep.warnings.append(
            "阶段 %s 消费上游产出的清单却未标 barrier → 自动补（WARN 不阻塞）"
            % rep.barriers_added)

    # ── R3：预算-粒度失配（只出信号，回炉让 planner 重出，不机械拆合）──
    rep.granularity = granularity_hints(phases)
    if rep.granularity:
        rep.warnings.append("R3 粒度信号 %d 条（仅回炉提示，不机械拆合）" % len(rep.granularity))

    # ── R4：工具面裁剪（只裁"零相关"条目，保留面不低于 R4_MIN_FACE）──
    # 相关性判定：正文逐字命中 ∣ 倒排索引 token 命中 ∣ 保护名单（清单候选/清单产消/
    # 技能工具/能力点名）。误杀比漏杀烦人（裁决原则）——有任何相关信号就不裁。
    if index:
        rel = set(protected_names or ()) | LIST_PRODUCERS | LIST_CONSUMERS
        for _tools in DATA_DOMAIN_TOOLS.values():
            rel |= set(_tools)
        rel |= _relevant_names(task, index, available_names)
        if phases:
            for p in phases:
                if not p.get("tools_declared"):
                    continue
                face = list(p.get("tools") or ())
                _prel = rel | _relevant_names(
                    " ".join(str(p.get(k) or "") for k in ("name", "goal", "deliverable")),
                    index, available_names)
                pruned = [t for t in face if t not in _prel]
                if pruned and len(face) - len(pruned) >= R4_MIN_FACE:
                    rep.tool_pruned[p["id"]] = pruned
        else:
            pruned = [t for t in plan_tools if t not in rel]
            if pruned and len(plan_tools) - len(pruned) >= R4_MIN_FACE:
                rep.plan_tools_pruned = pruned
        if rep.tool_pruned or rep.plan_tools_pruned:
            rep.warnings.append(
                "R4 工具面裁剪：零相关工具 %d 个（逐字/索引均未命中，误用面收缩）"
                % (sum(len(v) for v in rep.tool_pruned.values())
                   + len(rep.plan_tools_pruned)))

    return rep


def _relevant_names(text: str, index: Dict[str, set], available_names) -> set:
    """与文本相关的工具名集合：正文逐字命中 + 倒排索引 token 命中。"""
    rel = set(names_in_text(text, available_names)) if available_names else set()
    for tk in set(_tokens(text or "")):
        rel |= set(index.get(tk, ()))
    return rel


def _add_for_domain(rep: LintReport, phases, plan_tools, dom: str, tool: str) -> bool:
    """把某域的补位工具落到"自己也需要该域"的载体上。返回是否真的落了。"""
    if not phases:
        if tool not in plan_tools and tool not in rep.plan_tools_additions:
            rep.plan_tools_additions.append(tool)
            return True
        return False
    targets = [p["id"] for p in phases
               if p.get("tools_declared") and _phase_hits_domain(p, dom) and tool not in p.get("tools", ())]
    for pid in targets:
        bucket = rep.phase_tool_additions.setdefault(pid, [])
        if tool not in bucket:
            bucket.append(tool)
    return bool(targets)


def _phase_hits_domain(phase: dict, dom: str) -> bool:
    """该阶段自身的文本是否也命中该数据域（补白名单的前置条件：它自己需要）。"""
    text = " ".join(str(phase.get(k) or "") for k in ("name", "goal", "deliverable"))
    return dom in detect_domains(text)


def apply_report(phases, plan_tools, report: LintReport) -> Tuple[list, list]:
    """按报告就地落实（改的是入参对象；返回同一对引用，便于链式书写）。

    R1/R2 落**加法**（补工具/补 barrier）；R4 落**减法**（裁零相关工具）。
    加法先于减法：同名工具被补过就不再是“零相关”（补位源于域命中，本就不会进裁剪名单）。
    """
    for pid, tools in (report.phase_tool_additions or {}).items():
        for p in phases or ():
            if p.get("id") != pid:
                continue
            for t in tools:
                if t not in p.setdefault("tools", []):
                    p["tools"].append(t)
    for pid in (report.barriers_added or ()):
        for p in phases or ():
            if p.get("id") == pid:
                p["barrier"] = True
    for t in (report.plan_tools_additions or ()):
        if t not in (plan_tools or []):
            plan_tools.append(t)
    # R4：减法（只动 tools_declared=True 的阶段，与补位同一选面原则）
    for pid, pruned in (report.tool_pruned or {}).items():
        for p in phases or ():
            if p.get("id") == pid and p.get("tools_declared"):
                p["tools"] = [t for t in (p.get("tools") or []) if t not in set(pruned)]
    if report.plan_tools_pruned:
        _drop = set(report.plan_tools_pruned)
        plan_tools[:] = [t for t in plan_tools if t not in _drop]
    return phases, plan_tools
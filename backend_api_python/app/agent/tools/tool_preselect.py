# -*- coding: utf-8 -*-
"""工具预选（Tool Preselection）——外置 plan 式工具面声明 + 确定性 lint 校正。

## 它解决什么

工具分层把 88 个工具切成「必注入 25 + 按需 63」，token 从 21.2k/轮降到 8.7k/轮，
但代价转嫁到了**轮次**：模型要用 `search_tools` → `list_tools` → `activate_tools`
三轮才能摸到一个金融工具。实测一条选股任务 21 次工具调用里 **13 次（62%）是发现轮**，
真正取数只有 6 次。

## 方向（2026-10-01 重构：对齐旧系统外置 plan，非"LLM 路由器"）

【首版错在哪】首版让 LLM 直接回答"你会用哪些工具？输出 JSON 数组"——这是**在真空里
猜**，没有任务锚定，只能靠"宁可多选 1-2 个"的宽松指令兜底。实测结果：
选对省 25%，**选错费 136%**（天气任务被硬塞 8 个股票工具，30k→70.9k），
三次同任务实测 +21%/−25%/+31%，**方差大于效应**。

【旧系统怎么做】见 `app/agent_smolagents/prompts/plan_system.txt` + `utils/plan_linter.py`：
1. **工具选择是"任务书的副产品"**：planner 一次输出 `{task, tools, step_budget, phases}`，
   工具字段 = 「完成这份任务书的**最小工具面**」。先写清目标与交付要素，工具被锚定。
2. **双向标准**：「选主链路必须经过的工具，不凑数」+「关键链路不能断（宁多不可断）」。
   判据是**交付承诺的信息必须有对应取数工具**——可反向校验。
3. **确定性 Linter 兜底（稳定性的真正来源）**：LLM 选完还有一层不调 LLM 的校正——
   词典识别数据域 → 缺覆盖则补首选工具（R1，加法）；零相关条目裁掉（R4，减法）。
   **LLM 不稳定没关系，确定性层纠正它。**

故本模块重构为「LLM 出 plan → 确定性 lint 校正 → 落工具面」两阶段。

## 设计红线

1. **纯 fail-open**：预选失败/超时/选空 → 返回空，模型仍可走 search_tools 老路，
   绝不能因为预选出错就丢工具。
2. **只做加法**：不清理已激活项（多轮会话里上一轮的工具还可能用得上）。
3. **名字必须校验**：模型会幻觉出不存在的工具名，一律对照注册表过滤掉。
4. **确定性优先**：能被词典/索引查出来的不劳 LLM（提智原则 1）；LLM 只做
   确定性查不了的语义匹配。
5. **置信度分层**：词典命中 = 高置信（可补可裁）；仅倒排索引相关 = 低置信
   （**只告警不动工具面**——用弱相关性去吹工具面，token 成本立刻上升）。
6. **误杀比漏杀烦人**（旧系统裁决原则，沿用）：只要有任何相关信号就不裁。
7. **本模块只放纯函数**，不碰 model、不碰 DB；LLM 调用由
   `QDAgent._maybe_preselect_tools` 发起。

作者: QuantDinger / 2026-10-01（首版 LLM 路由器）→ 同日重构为 plan + lint
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "DATA_DOMAINS", "DATA_DOMAIN_TOOLS", "MIN_FACE",
    "detect_domains", "names_in_text", "build_tool_index", "index_candidates",
    "build_catalog", "build_catalog_grouped", "has_domain_hint",
    "build_preselect_messages", "parse_selection", "parse_plan",
    "LintReport", "lint_selection", "apply_lint",
    "capability_domain", "CAP_SECTION_NOTE", "fallback_domain_tools",
]

# ★ 本模块是「工具预选的管线」，不是工具面本身（2026-10-01）
# 上面这些公开函数是给 QDAgent / smoke 直接 import 调用的，模型不需要也不应该
# 调它们。此前因"公开+有 docstring"被全部注册进 common 域（必注入层）⇒ 每轮
# 白下发 13 份 schema，且模型可能真去调 apply_lint / lint_selection 这类内部函数。
# 声明 `_NOT_TOOLS` 后由 base._is_tool_function 统一排除（判据只有一处，不漂移）。
# ⚠ 本模块**新增公开函数时必须同步加进来**，否则它会静默变成"必选工具"
# （smoke test_24 会抓：必选层 ∩ _NOT_TOOLS 必须为空；但**没进 _NOT_TOOLS 的
#   新函数**只能靠肉眼/code review，加完请跑一次 test_24 看 common 名单）。
_NOT_TOOLS = frozenset({
    "check_domain_dict", "detect_domains", "names_in_text",
    "build_tool_index", "get_tool_index", "index_candidates",
    "build_catalog", "has_domain_hint", "build_preselect_messages",
    "parse_selection", "parse_plan", "lint_selection", "apply_lint",
    "build_catalog_grouped", "capability_domain", "fallback_domain_tools",
})


# ═══════════════════════════════════════════════════════════════════════════
#  一、数据域登记表（R1 词典：域 → 触发关键词 + 候选工具）
# ═══════════════════════════════════════════════════════════════════════════
# 每行 = (域, 关键词元组, 候选工具元组)。候选工具**首元素 = 首选**（补位时用它）。
#
# ⚠️ 硬约束：工具名必须**逐字**来自 provider 注册表，否则补位补出一个不存在的名字、
#    后续 activate_tools 收到 unknown、白跑一轮（且静默）。
#    `check_domain_dict()` 提供断言，由 smoke 的 test_23 每次跑——
#    旧系统踩过的坑正是"词典陈旧无人报警"（plan_linter.py 头部易错点），此处堵死。
#
# 关键词要用**用户会怎么写**的词（问法），不是工具的字段名。
DATA_DOMAINS: Tuple[Tuple[str, Tuple[str, ...], Tuple[str, ...]], ...] = (
    ("quote", ("行情", "现价", "报价", "涨跌", "涨幅", "跌幅", "价格", "收盘", "实时",
               "quote", "最新"),
     ("get_realtime_quote", "get_index_quote", "get_index_etf_quote")),

    ("kline", ("k线", "K线", "日线", "周线", "历史", "走势", "均线", "ma5", "ma20",
               "ma60", "kline", "蜡烛"),
     ("agent_get_kline", "get_index_kline", "daily", "calculate_ma")),

    ("trend", ("技术面", "趋势", "形态", "支撑", "压力", "突破", "macd", "kdj", "rsi",
               "指标", "多头", "空头", "背离"),
     ("technical_analysis", "analyze_trend", "analyze_pattern",
      "analyze_chart_patterns", "get_indicator_snapshot", "indicator_analysis",
      "run_indicator_signal", "list_indicators", "get_indicator_params")),

    ("fundflow", ("资金流", "主力", "净流入", "净流出", "大单", "超大单", "北向",
                  "融资", "主力净额"),
     ("get_fund_flow", "get_fund_flow_daily", "get_market_fund_flow",
      "get_sector_fund_flow", "get_concept_fund_flow")),

    ("sector", ("板块", "题材", "概念", "行业", "龙头", "赛道"),
     ("get_sector_board", "get_hot_sectors", "get_sector_stocks",
      "get_sector_fund_flow", "get_sector_trend_analysis", "get_stock_sector_info",
      "get_stock_concept_blocks", "get_industry_ranking", "get_sector_history_data")),

    ("screen", ("选股", "筛选", "强势", "涨停", "跌停", "榜单", "排行", "热榜",
                "哪些股", "几只", "排名", "涨幅榜"),
     ("search_stocks", "get_limit_pool", "get_hot_stocks_with_reason",
      "get_hot_rank", "get_screener_presets", "build_keyword_from_filters",
      "get_market_overview")),

    ("valuation", ("估值", "市盈率", "pe", "pb", "市净率", "财报", "业绩", "营收",
                   "净利润", "基本面", "ROE", "roe"),
     ("batch_valuation_compare", "get_stock_info")),

    ("chip", ("筹码", "成本", "套牢", "获利盘", "集中度"),
     ("get_chip_distribution",)),

    ("volume", ("成交量", "量能", "放量", "缩量", "换手", "obv", "成交额", "量比"),
     ("get_volume_analysis", "get_obv_analysis", "get_order_book")),

    ("market", ("大盘", "指数", "上证", "深证", "创业板", "沪深", "市场", "两市",
                "科创", "北证"),
     ("get_market_overview", "get_market_indices", "get_index_quote",
      "get_index_kline", "get_capital_summary")),

    ("dragon", ("龙虎", "席位", "游资", "机构专用"),
     ("get_dragon_tiger", "get_dragon_tiger_detail")),

    ("strategy", ("策略", "回测", "启动策略", "停止策略", "信号", "交易记录"),
     ("list_strategies", "get_strategy_detail", "get_strategy_trades",
      "start_strategy", "stop_strategy", "run_backtest", "get_backtest_history")),

    ("intel", ("新闻", "公告", "研报", "消息", "舆情", "政策", "公告", "为什么",
               "有什么利", "最新动态"),
     ("search_intel", "search_stock_intel", "search_policy_intel",
      "search_sector_intel", "search_comprehensive_intel")),

    ("capital", ("账户", "可用资金", "持仓", "仓位", "市值", "盈亏"),
     ("get_capital_summary",)),

    ("knowledge", ("知识库", "文档", "资料", "笔记", "之前记录", "咱们的"),
     ("search_knowledge",)),
)

# 派生视图：域 → 候选工具 / 域 → 首选工具（勿单独维护）
DATA_DOMAIN_TOOLS: Dict[str, Tuple[str, ...]] = {d: t for d, _kw, t in DATA_DOMAINS}
_PREFERRED: Dict[str, str] = {d: t[0] for d, _kw, t in DATA_DOMAINS}

# 裁剪后的最小工具面（低于此数不再裁——空工具面比冗余更致命）
MIN_FACE = 3

# 「几乎全被判零相关」的比例阈值：达到则说明这次预选整体误判，全部作废
# （旧系统 R4 只判 `剩余 >= MIN_FACE` 才裁，遇到"8 个全无关"会一刀不切地全留，
#   正是天气任务 +136% 的成因——此处补上这条）
MISSELECT_RATIO = 0.7


def check_domain_dict(available_names: Iterable[str]) -> List[str]:
    """词典自检：返回「词典引用了但注册表里不存在」的工具名（应恒为空）。

    旧系统教训：词典与注册表两处漂移 ⇒ 归组摘名后词典失效**无人报警**。
    由 smoke test_23 每次跑，非空即 FAIL。
    """
    avail = set(available_names or ())
    return sorted({t for tools in DATA_DOMAIN_TOOLS.values() for t in tools
                   if t not in avail})


def detect_domains(text: str) -> List[str]:
    """从文本识别数据域需求（按登记表顺序，去重）。无命中返回 []。"""
    s = str(text or "").lower()
    if not s:
        return []
    out: List[str] = []
    for dom, kws, _tools in DATA_DOMAINS:
        for kw in kws:
            if kw.lower() in s:
                out.append(dom)
                break
    return out


def names_in_text(text: str, names: Iterable[str]) -> List[str]:
    """回收文本中**逐字出现**的工具名（词边界扫描，避免 get_fund_flow 命中其变体）。"""
    if not text or not names:
        return []
    hits = []
    for n in sorted(set(names)):
        if re.search(r"(?<![A-Za-z0-9_])" + re.escape(n) + r"(?![A-Za-z0-9_])", text):
            hits.append(n)
    return hits


# ═══════════════════════════════════════════════════════════════════════════
#  二、倒排索引（低置信兜底：词典覆盖不到的长尾找候选）
# ═══════════════════════════════════════════════════════════════════════════
_INDEX_STOP = frozenset({
    "get", "data", "stock", "stocks", "list", "query", "info", "by", "code",
    "codes", "the", "and", "for", "with", "from", "day", "daily", "all", "of",
    "to", "in", "返回", "获取", "数据", "股票", "查询", "列表", "信息",
})

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*")


def _tokens(text: str) -> List[str]:
    """分词：ASCII 词 + 中文 2-gram（中文无空格，2-gram 是够用的相关性近似）。"""
    from collections import Counter
    cnt: Counter = Counter()
    for w in _TOKEN_RE.findall(str(text or "").lower()):
        if w not in _INDEX_STOP and len(w) > 2:
            cnt[w] += 1
    for seg in re.findall(r"[\u4e00-\u9fff]+", str(text or "")):
        for i in range(len(seg) - 1):
            g = seg[i:i + 2]
            if g not in _INDEX_STOP:
                cnt[g] += 1
    return list(cnt)


def build_tool_index(defs_by_name: Dict[str, dict]) -> Dict[str, Set[str]]:
    """建立 token → {工具名} 倒排索引（工具名 + 描述首段）。"""
    idx: Dict[str, Set[str]] = {}
    for name, d in (defs_by_name or {}).items():
        fn = (d or {}).get("function") or {}
        desc = str(fn.get("description") or "")[:300]
        for tk in set(_tokens(name + " " + desc)):
            idx.setdefault(tk, set()).add(name)
    return idx


# 进程级索引缓存：tools/ 目录运行期不变，建一次全程复用
_INDEX_CACHE: Dict[int, Dict[str, Set[str]]] = {}


def get_tool_index(defs_by_name: Dict[str, dict]) -> Dict[str, Set[str]]:
    """取（并缓存）工具倒排索引。"""
    if not defs_by_name:
        return {}
    key = id(defs_by_name)
    if key not in _INDEX_CACHE:
        _INDEX_CACHE[key] = build_tool_index(defs_by_name)
    return _INDEX_CACHE[key]


def index_candidates(text: str, index: Dict[str, Set[str]], available_names,
                     *, min_hits: int = 3, limit: int = 8) -> List[str]:
    """按 token 重合度给候选工具（**低置信，仅用于相关性判定，不自动改工具面**）。"""
    if not text or not index:
        return []
    avail = set(available_names or ())
    score: Dict[str, int] = {}
    for tk in set(_tokens(text)):
        for n in index.get(tk, ()):
            if avail and n not in avail:
                continue
            score[n] = score.get(n, 0) + 1
    return sorted((n for n, c in score.items() if c >= min_hits),
                  key=lambda n: (-score[n], n))[:limit]


def fallback_domain_tools(text: str, available_names: Iterable[str],
                          limit: int = 4) -> Tuple[List[str], List[str]]:
    """词典兜底：预选**空手而归**时，按命中的数据域补一个「核心子集」。

    【为什么需要·旧系统 P0 教训】`agents/task_agent.py` 里写得明明白白：域选择
    失效属退化，绝不能降成"只有通用工具"的裸沙箱——那是 2026-09-19 故障的直接
    机制；并为此配了三层兜底网（从 planner 原文回收工具名 → 域兜底 fallback_domain
    → 空域不许静默）。本系统预选一旦空手（闸门拦截 / 模型未点名 / lint 整体作废），
    工具面就只剩必选层，模型只能靠 `search_tools` 自救一轮。

    【为什么不整域注入】旧系统是 CodeAgent，注入的是函数名 + Returns 契约文本；
    本系统是 tool-calling，整域（finance 61 个）≈ 15k tokens/轮，抄不起。故只补
    词典登记的**核心子集**（默认 4 个）：关键链路不断，代价可控。

    【成本与克制】纯字符串匹配，零 LLM 调用；与 lint 同源（`DATA_DOMAIN_TOOLS`），
    命中不了就一个不补——宁可不补，不硬塞（"天气"不会命中任何域）。

    Returns:
        (工具名列表, 命中的域列表)
    """
    avail = set(available_names or ())
    if not avail:
        return [], []
    doms = detect_domains(text)
    if not doms:
        return [], []
    out: List[str] = []
    for dom in doms:
        for t in DATA_DOMAIN_TOOLS.get(dom, ()):
            if t in avail and t not in out:
                out.append(t)
            if len(out) >= limit:
                break
        if len(out) >= limit:
            break
    return out[:limit], doms


# ═══════════════════════════════════════════════════════════════════════════
#  三、极简目录（给 LLM 看的候选清单）
# ═══════════════════════════════════════════════════════════════════════════
_CATALOG_LINE = "{name} | {desc} | 参数: {params}"


def build_catalog(
    defs_by_name: Dict[str, dict],
    only_names: Optional[Iterable[str]] = None,
    max_desc: int = 100,
) -> tuple:
    """把工具定义压成「一行一个」的极简目录。

    Args:
        defs_by_name: {工具名: OpenAI function definition}
        only_names: 只收这些工具（传 None = 全部）。生产只传**按需层**。
        max_desc: 单条描述截断长度（字符）。

    Returns:
        (catalog_text, names_in_catalog)
    """
    names = list(only_names) if only_names is not None else list(defs_by_name)
    lines: List[str] = []
    kept: List[str] = []
    for name in names:
        d = defs_by_name.get(name)
        if not d:
            continue
        fn = d.get("function") or {}
        desc = _one_line(str(fn.get("description") or ""), max_desc)
        params = list(((fn.get("parameters") or {}).get("properties") or {}).keys())
        lines.append(_CATALOG_LINE.format(
            name=name,
            desc=desc or "（无说明）",
            params=",".join(params[:8]) if params else "-",
        ))
        kept.append(name)
    return "\n".join(lines), kept


def _one_line(text: str, limit: int) -> str:
    """描述压成单行（docstring 首行/首句），去掉换行与多余空白。"""
    s = re.sub(r"\s+", " ", (text or "").strip())
    if not s:
        return ""
    m = re.match(r"^(.{6,}?[。．.!！?？;；])", s)
    if m and len(m.group(1)) <= limit:
        s = m.group(1)
    return s[:limit]


# ═══════════════════════════════════════════════════════════════════════════
#  三之二、能力层（第三级：域内工具无覆盖时才用）
# ═══════════════════════════════════════════════════════════════════════════
# 分级口径（2026-10-01 用户裁定）：
#   一级 必选 = tools/ 顶层（common 域）+ mimo 原生 + 元工具，每轮全量下发；
#   二级 域内 = tools/<子目录>（finance / knowledge…），按任务筛选后激活；
#   三级 能力层 = capabilities/admission.json 准入函数，**域内工具优先**——
#                只有域内确实没有同功能工具时才轮到能力层。
# 让位判据复用能力层唯一实现（near_dup_tool_names），不在这里另写一套。

CAP_SECTION_NOTE = (
    "## 能力层（低优先级）\n"
    "仅当上面的域内工具**确实无法覆盖**某条交付信息时才从这里选；"
    "域内已有同功能工具时**不要重复选**。"
)


def capability_domain() -> str:
    """能力层的来源域名（唯一事实源：`capabilities/loader.CAPABILITY_DOMAIN`）。

    硬编码兜底只为 capabilities 包不可导入时仍能跑（此时能力层本就是空的）。
    """
    try:
        from app.agent.capabilities.loader import CAPABILITY_DOMAIN as _D
    except Exception:
        try:
            from capabilities.loader import CAPABILITY_DOMAIN as _D
        except Exception:
            return "capability"
    return _D


def _near_dup(a: str, b: str) -> bool:
    """近重名判定（让位用）——复用能力层实现，失败退化为同名判定。"""
    try:
        from app.agent.capabilities.loader import near_dup_tool_names as _f
    except Exception:
        try:
            from capabilities.loader import near_dup_tool_names as _f
        except Exception:
            return a == b
    try:
        return bool(_f(a, b))
    except Exception:
        return a == b


def build_catalog_grouped(
    defs_by_name: Dict[str, dict],
    domain_names: Sequence[str],
    capability_names: Sequence[str] = (),
    max_desc: int = 100,
) -> tuple:
    """两段目录：**域内在前（优先），能力层在后（兜底）**。

    能力层为空（admission.json 缺失 ⇒ 0 准入）时**不生成第二段**——
    既不浪费 token，也不给模型凭空多一段可幻觉的空间。
    """
    dom_txt, dom_kept = build_catalog(defs_by_name, only_names=domain_names,
                                      max_desc=max_desc)
    caps = [n for n in (capability_names or ())]
    if not caps:
        return dom_txt, dom_kept
    cap_txt, cap_kept = build_catalog(defs_by_name, only_names=caps, max_desc=max_desc)
    if not cap_kept:
        return dom_txt, dom_kept
    return (dom_txt + "\n\n" + CAP_SECTION_NOTE + "\n" + cap_txt,
            dom_kept + cap_kept)


# ═══════════════════════════════════════════════════════════════════════════
#  四、域特征闸门（成本闸：要不要花这次 LLM）
# ═══════════════════════════════════════════════════════════════════════════
_CODE_HINT_RE = re.compile(r"(?<!\d)\d{6}(?!\d)")

_DEFAULT_HINTS = (
    "股", "股票", "个股", "A股", "港股", "美股", "行情", "K线", "k线", "涨停", "跌停",
    "板块", "题材", "概念", "资金流", "主力", "龙虎", "技术面", "均线", "MACD", "KDJ",
    "估值", "财报", "业绩", "选股", "龙头", "强势", "持仓", "仓位", "止损", "止盈",
    "策略", "回测", "大盘", "指数", "涨幅", "跌幅", "反弹", "回调", "突破",
    "stock", "quote", "sector", "ticker",
    "数据", "统计", "查一下", "对比",
)


def has_domain_hint(query: str, hints: Optional[Sequence[str]] = None) -> bool:
    """问题是否带有"需要专业工具"的领域特征。

    【定位·2026-10-01 重构后】这只是**成本闸**：决定要不要为这个任务花一次
    LLM 调用（+ 3k 目录 token）。它**不负责**预选质量——质量由 `lint_selection`
    负责。首版把质量寄托在它身上（没特征就不预选）是方向错误：
    那等于"为了不出错干脆不做"，把收益一起扔了。

    真正的误选防线是 linter 的「零相关裁剪 + 整体作废」：直接判"选出来的工具
    跟这个问题有没有关系"，比"猜这个问题像不像金融问题"更准。
    """
    q = (query or "").strip()
    if not q:
        return False
    if _CODE_HINT_RE.search(q):
        return True
    words = [w.strip() for w in (hints if hints is not None else _DEFAULT_HINTS) if w.strip()]
    return any(w in q for w in words)


# ═══════════════════════════════════════════════════════════════════════════
#  五、提示词 —— 外置 plan（对齐 plan_system.txt 的"工具面 = 任务书副产品"）
# ═══════════════════════════════════════════════════════════════════════════
PRESELECT_SYSTEM = (
    "你是外部规划器，为执行器挑选本轮**沙箱工具面**。\n"
    "先想清楚这个任务要交付什么信息，再据此声明**最小工具面**——工具是目标的副产品，"
    "不是凭感觉罗列。\n"
    "工具面标准（两条同时成立，缺一不可）：\n"
    "1. 只选**主链路必须经过**的工具，不凑数；\n"
    "2. **关键链路不能断**：你承诺交付的每一条信息，都必须有对应取数工具支撑"
    "（宁多不可断）。\n"
    "若某条交付信息在候选目录里**找不到**对应工具，就在 deliverables 里删掉它——"
    "不要为了兑现承诺硬塞无关工具。\n"
    "工具名必须**逐字来自候选目录**，禁止编造（编造的名字会被丢弃并留痕）。\n"
    "「已常驻」清单里的工具无需再选。"
)

PRESELECT_PROMPT = """用户问题：
{query}

已常驻可直接调用、**不需要再选**的工具：
{already}

候选工具目录（每行：工具名 | 说明 | 参数）：
{catalog}
{history}
只输出 JSON，不要解释、不要代码块：
{{
  "goal": "可执行翻译后的目标（不是复述原话）",
  "deliverables": ["交付要素1", "交付要素2"],
  "tools": ["工具名", ...（最多 {max_tools} 个；确实无关则 []）]
}}"""


def build_preselect_messages(
    query: str,
    catalog: str,
    max_tools: int,
    already: Optional[Sequence[str]] = None,
    history_hint: str = "",
) -> list:
    """构造预选请求（无 tools，纯文本回合）。

    Args:
        already: 已常驻（必注入层）工具名。**必须传**：不告诉模型"web_search 已经
            有了"，它遇到"今天天气"这种需要联网、但目录里全是金融工具的任务时，
            就会从目录里硬凑无关工具（首版实测如此）。
        history_hint: 历史表现提示（2026-10-01 闭环消费端）。来自
            `chain/weight_hints.hint_text()`——低权重工具/链路 + 领域命中率。
            **只提示不过滤**：低权重不等于这次不需要，硬过滤会让模型在真需要时
            拿不到工具。空串时该段不出现（不占位、不打扰）。
    """
    already_txt = ", ".join(already) if already else "（无）"
    hist_txt = ("\n历史表现（仅供参考，不是禁令）：\n" + history_hint
                if history_hint else "")
    return [
        {"role": "system", "content": PRESELECT_SYSTEM},
        {"role": "user", "content": PRESELECT_PROMPT.format(
            query=(query or "")[:800], already=already_txt,
            catalog=catalog, max_tools=max_tools, history=hist_txt)},
    ]


# ═══════════════════════════════════════════════════════════════════════════
#  六、结果解析
# ═══════════════════════════════════════════════════════════════════════════
_JSON_ARRAY_RE = re.compile(r"\[[^\]]*\]", re.S)
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.S)


def _strip_fence(text: str) -> str:
    body = (text or "").strip()
    return re.sub(r"^```(?:json)?|```$", "", body, flags=re.M).strip()


def parse_selection(text: str, valid_names: Sequence[str], max_tools: int) -> List[str]:
    """从 LLM 返回文本里解析工具名列表（兼容裸数组与 plan JSON 两种输出）。

    容错链：整体 JSON → 提取首个 `[...]`/`{...}` → 目录逐字扫描兜底。
    **一律对照 valid_names 过滤**（模型幻觉出的名字直接丢弃）。
    """
    valid = set(valid_names)
    raw_items: List[Any] = []
    body = _strip_fence(text)

    parsed = None
    try:
        parsed = json.loads(body)
    except Exception:
        for pat in (_JSON_OBJECT_RE, _JSON_ARRAY_RE):
            m = pat.search(body)
            if m:
                try:
                    parsed = json.loads(m.group(0))
                    break
                except Exception:
                    parsed = None

    if isinstance(parsed, list):
        raw_items = parsed
    elif isinstance(parsed, dict):
        for key in ("tools", "selected", "names", "result"):
            if isinstance(parsed.get(key), list):
                raw_items = parsed[key]
                break
    else:
        raw_items = [n for n in valid_names if n and n in body]

    out: List[str] = []
    seen: Set[str] = set()
    for item in raw_items:
        name = item if isinstance(item, str) else str((item or {}).get("name", ""))
        name = name.strip().strip("\"'")
        if not name or name in seen or name not in valid:
            continue                     # ★ 幻觉名/已下线工具：丢弃
        seen.add(name)
        out.append(name)
        if len(out) >= max_tools:
            break
    return out


def parse_plan(text: str, valid_names: Sequence[str], max_tools: int) -> dict:
    """解析外置 plan → {"goal", "deliverables", "tools"}。

    与 `parse_selection` 的区别：本函数**保留 goal/deliverables**，因为 lint 的
    R1 覆盖检查要看"交付承诺了什么信息"（判据来源）。退化路径（模型只回了裸数组）
    仍然可用：此时 goal/deliverables 为空，linter 改用**用户问题**做域识别。
    """
    body = _strip_fence(text)
    parsed = None
    try:
        parsed = json.loads(body)
    except Exception:
        m = _JSON_OBJECT_RE.search(body)
        if m:
            try:
                parsed = json.loads(m.group(0))
            except Exception:
                parsed = None

    goal, deliverables = "", []
    if isinstance(parsed, dict):
        goal = str(parsed.get("goal") or parsed.get("task") or "")[:600]
        dv = parsed.get("deliverables")
        if isinstance(dv, (list, tuple)):
            deliverables = [str(x)[:200] for x in dv if str(x).strip()][:12]
        elif isinstance(dv, str) and dv.strip():
            deliverables = [dv.strip()[:200]]

    return {
        "goal": goal,
        "deliverables": deliverables,
        "tools": parse_selection(text, valid_names, max_tools),
        "parsed": isinstance(parsed, dict),
    }


# ═══════════════════════════════════════════════════════════════════════════
#  七、Lint —— 确定性校正（不调 LLM，稳定性的真正来源）
# ═══════════════════════════════════════════════════════════════════════════
class LintReport:
    """校正结果（纯数据）。`coverage` 记录"哪个域缺覆盖"，`additions`/`pruned`
    记录"该往哪加/该裁什么"，`misselected` = 整体误判作废标记。"""

    __slots__ = ("coverage", "additions", "pruned", "misselected",
                 "index_candidates", "capability_yield", "warnings")

    def __init__(self) -> None:
        self.coverage: List[dict] = []      # {domain, tool, auto_added}
        self.additions: List[str] = []      # 高置信补位（宁多不可断）
        self.pruned: List[str] = []         # 零相关裁剪
        self.misselected: bool = False      # 整体误判 → 全部作废
        self.index_candidates: List[str] = []   # 低置信候选（仅告警）
        # 能力层让位：{capability, yielded_to} —— 域内已有同功能工具（域内优先）
        self.capability_yield: List[dict] = []
        self.warnings: List[str] = []

    @property
    def dirty(self) -> bool:
        return bool(self.coverage or self.pruned or self.warnings
                    or self.misselected or self.index_candidates
                    or self.capability_yield)

    def to_trace(self) -> dict:
        return {
            "coverage": self.coverage, "additions": self.additions,
            "pruned": self.pruned, "misselected": self.misselected,
            "index_candidates": self.index_candidates,
            "capability_yield": self.capability_yield,
            "warnings": self.warnings,
        }


def _relevant_names(text: str, index: Dict[str, Set[str]], available_names) -> Set[str]:
    """与文本相关的工具名集合：正文逐字命中 ∪ 倒排索引 token 命中。"""
    rel: Set[str] = set(names_in_text(text, available_names)) if available_names else set()
    for tk in set(_tokens(text or "")):
        rel |= set(index.get(tk, ()) or ())
    return rel


def lint_selection(
    query: str,
    selected: Sequence[str],
    *,
    available_names: Sequence[str],
    index: Optional[Dict[str, Set[str]]] = None,
    plan_text: str = "",
    protected_names: Sequence[str] = (),
    parsed: bool = True,
    capability_names: Sequence[str] = (),
    domain_names: Sequence[str] = (),
) -> LintReport:
    """对预选结果做确定性校正（纯函数，不改入参）。

    Args:
        query: 用户问题（plan 不可用时的退化来源）。
        selected: LLM 点名的工具。
        available_names: 注册表全部工具名（校验词典/索引候选真实性）。
        index: 工具倒排索引（None = 跳过低置信兜底与裁剪）。
        plan_text: plan 的 goal + deliverables 拼接文本 —— **首选判据来源**。
        protected_names: 永不裁剪名单。
        parsed: LLM 是否真的输出了结构化 plan。False ⇒ 无可靠语义基准，
            走**保守**路径（见下）。
        capability_names: 能力层工具名（三级）。域内已有同功能工具 ⇒ 让位裁掉。
        domain_names: 域内工具名全集（二级），作为让位的比对基准。

    【判据来源为什么是 plan 正文而不是用户问题·2026-10-01】
    与旧系统 `lint_plan(task, ...)` 同源：以**模型自述的目标**校验**模型自选的
    工具**，判的是"它有没有自相矛盾"，而不是"我猜这个任务该用什么"。
    天气任务的模型会写 goal="查询北京今日天气" 却塞 8 个股票工具——这个矛盾
    用 plan 一判即中；若拿用户问题去比对，只能靠"天气不像金融"这种弱猜测。

    【保守原则】`parsed=False`（模型只回了裸数组）时**不裁**——没有语义基准就
    没有证据说某个工具无关，"判不了"≠"判了是无关"（误杀比漏杀烦人）。

    三步（加法先于减法，与旧系统 apply_report 同序）：
      R1 覆盖补位（高置信·加法）：域需求无工具落在工具面 → 补首选工具。
      R4 零相关裁剪（减法）：非词典命中、非索引命中、非保护名单 → 裁。
      R4b 整体作废：零相关占比 ≥ MISSELECT_RATIO 且**无补位** → 全部作废。
          ★ "有补位"说明词典确认了任务确有工具需求，此时不得作废——
            否则会把 R1 刚补上的正确工具一起丢掉（实测：资金流任务被误清空）。
    """
    rep = LintReport()
    avail = set(available_names or ())
    face = set(selected or ())
    src = " ".join(x for x in (plan_text, query) if x).strip()

    # ── R0：能力层让位（**域内工具优先**，2026-10-01 用户裁定）──
    # 能力层是三级兜底：只要域内（二级）存在同功能工具，模型却选了能力层，就让位。
    # 判据复用能力层自己的 near_dup_tool_names（功能等价，不只看名字），与
    # capabilities/func_overlap.py 的"工具层 > 能力层"同一口径——区别是那里在
    # **注册期**去重（能力根本不注册），这里在**预选期**去重（已注册但本轮不该选）。
    caps = [t for t in (selected or []) if t in set(capability_names or ())]
    if caps:
        dom_all = [n for n in (domain_names or []) if n in avail and n not in caps]
        for c in caps:
            shadow = next((d for d in dom_all if _near_dup(c, d)), None)
            if shadow:
                rep.capability_yield.append({"capability": c, "yielded_to": shadow})
        if rep.capability_yield:
            rep.warnings.append(
                "R0 能力层让位 %d 个（域内已有同功能工具，域内优先）：%s"
                % (len(rep.capability_yield),
                   [f"{x['capability']}→{x['yielded_to']}" for x in rep.capability_yield]))

    # ── R1：数据域覆盖（词典，高置信·加法）──
    for dom in detect_domains(src):
        cands = DATA_DOMAIN_TOOLS.get(dom) or ()
        if set(cands) & face:
            continue                                  # 已有工具覆盖 → OK
        pick = _PREFERRED.get(dom)
        if not pick:
            continue
        item = {"domain": dom, "tool": pick, "auto_added": False}
        if pick not in avail:
            # 词典条目陈旧（工具改名/下架）——报出来，别静默
            item["reason"] = "tool_not_registered"
            rep.warnings.append(
                "数据域 %s 首选工具 %s 不在注册表（词典条目陈旧，请修 DATA_DOMAINS）"
                % (dom, pick))
        else:
            rep.additions.append(pick)
            item["auto_added"] = True
        rep.coverage.append(item)
    if rep.additions:
        rep.warnings.append("R1 覆盖补位 %d 个（宁多不可断）：%s" % (
            len(rep.additions), rep.additions))

    # ── R4：零相关裁剪（减法）──
    if index and face and parsed and plan_text.strip():
        rel: Set[str] = set(protected_names or ())
        # 词典命中域的候选工具全集（本任务相关的域 ⇒ 其工具一律视为相关）
        for dom in detect_domains(src):
            rel |= set(DATA_DOMAIN_TOOLS.get(dom) or ())
        rel |= _relevant_names(plan_text, index, avail)
        pruned = [t for t in (selected or []) if t not in rel]
        if pruned:
            remain = (len(face) - len(pruned)) + len(rep.additions)
            if (len(pruned) / max(len(face), 1) >= MISSELECT_RATIO
                    and remain < MIN_FACE and not rep.additions):
                # ★ R4b：几乎全零相关且词典也没确认任何需求 → 整体误判，作废
                rep.misselected = True
                rep.pruned = list(selected or [])
                rep.warnings.append(
                    "R4b 预选整体误判：%d/%d 个工具与 plan 零相关（正文/词典/索引均未命中）"
                    " → 全部作废，回退 search_tools 老路" % (len(pruned), len(face)))
            elif remain >= MIN_FACE:
                rep.pruned = pruned
                rep.warnings.append(
                    "R4 零相关裁剪 %d 个（保留面 %d）" % (len(pruned), remain))

        # 低置信兜底：仅索引相关 → 只告警，不动工具面
        for n in index_candidates(plan_text, index, avail):
            if n not in face and n not in rel:
                rep.index_candidates.append(n)
        if rep.index_candidates:
            rep.warnings.append(
                "索引兜底候选未在工具面内（低置信，仅告警不动）：%s"
                % rep.index_candidates[:5])
    elif face:
        rep.warnings.append(
            "无可靠语义基准（plan 未解析出 goal）→ 保守不裁，原样放行 %d 个" % len(face))

    return rep


def apply_lint(selected: List[str], report: LintReport,
               capability_names: Sequence[str] = ()) -> List[str]:
    """按报告就地落实：先加法后减法，整体作废时返回空。

    加法先于减法：被补位的工具源于域命中，本就不会进裁剪名单。

    Args:
        capability_names: 能力层工具名。用于**排序**：域内工具在前、能力层在后
            （域内优先不只是"该不该选"，也体现在工具面的呈现顺序上——模型按
            目录顺序取用，把能力层放最后等于把它当兜底）。
    """
    if report.misselected:
        return []
    out = list(selected or [])
    for t in (report.additions or []):
        if t not in out:
            out.append(t)
    drop = set(report.pruned or ())
    drop |= {x.get("capability") for x in (report.capability_yield or [])}
    kept = [t for t in out if t not in drop]
    # 兜底：减法不得把工具面清空（空面比冗余更致命）
    kept = kept or out
    cap_set = set(capability_names or ())
    if cap_set:
        kept = [t for t in kept if t not in cap_set] + [t for t in kept if t in cap_set]
    return kept

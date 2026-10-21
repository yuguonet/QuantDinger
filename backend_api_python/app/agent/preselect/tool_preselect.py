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
import os
import re
import time
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "DATA_DOMAINS", "DATA_DOMAIN_TOOLS", "MIN_FACE",
    "detect_domains", "names_in_text", "build_tool_index", "index_candidates",
    "build_catalog", "build_catalog_grouped", "has_domain_hint",
    "build_preselect_messages", "parse_selection", "parse_plan",
    "LintReport", "lint_selection", "apply_lint",
    "capability_domain", "CAP_SECTION_NOTE", "fallback_domain_tools",
    # T5 确定性快路由
    "fast_route_tools", "single_target_anchor", "_FAST_ROUTE_DOMAINS",
    "shrink_catalog", "_preselect_catalog_limit",
    # F14（E3）：O-14 分级触发改判 schema 总字符数
    "schema_chars_of", "_preselect_tier_chars", "tier_catalog_enabled",
    "single_target_default_tools", "_SINGLE_STOCK_DOMAINS",
    "_FAST_ROUTE_INDEX_ONLY",
]

# ★ 本模块是「工具预选的管线」，不是工具面本身（2026-10-01）
# 上面这些公开函数是给 QDAgent / smoke 直接 import 调用的，模型不需要也不应该
# 调它们。此前它住在 `tools/`（自发现目录）下，因"公开+有 docstring"被全部注册
# 进 common 域（必注入层）⇒ 每轮白下发 13 份 schema，且模型可能真去调
# `apply_lint` / `lint_selection` 这类内部函数。
#
# ★★ 2026-10-13 P9：本模块已迁出 `tools/` 扫描目录（现 `app/agent/preselect/`），
#    不再需要 `_NOT_TOOLS` 名单——压根不会被扫到。名单方案的病根是"新增公开
#    函数必须手工同步登记"，这条约定必被遗忘且忘了是静默的；移出目录才是根治。


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
#
# ⚠️ 归组后的旧工具名**不得出现在候选里**：它们已被 `_SUPERSEDED_BY_MERGE` 摘出
# 注册表（如 analyze_trend → technical_analysis、search_stock_intel → search_intel），
# 写旧名 = 补位补出一个不存在的名字、白跑一轮且静默。改名一律查
# `tools/_base.py::TOOL_ALIAS`（旧名 → 新名的权威映射），不要凭印象写。
DATA_DOMAINS: Tuple[Tuple[str, Tuple[str, ...], Tuple[str, ...]], ...] = (
    ("quote", ("行情", "现价", "报价", "涨跌", "涨幅", "跌幅", "价格", "收盘", "实时",
               "quote", "最新"),
     ("get_realtime_quote", "get_index_quote")),   # get_index_etf_quote → get_index_quote

    ("kline", ("k线", "K线", "日线", "周线", "历史", "走势", "均线", "ma5", "ma20",
               "ma60", "kline", "蜡烛"),
     ("agent_get_kline", "get_index_kline", "technical_analysis")),  # calculate_ma → technical_analysis

    ("trend", ("技术面", "趋势", "形态", "支撑", "压力", "突破", "macd", "kdj", "rsi",
               "指标", "多头", "空头", "背离"),
    # analyze_trend / get_indicator_snapshot → technical_analysis；
    # indicator_tools 模块已并入 technical_analysis（run_indicator_signal /
    # list_indicators 不再注册），指标类查询一律走 technical_analysis
    # ★ 2026-10-19 用户裁定（附录 AN·分支 A）：indicator_tools.py 于 10-18 17:02
    #   恢复（用户确认有意），run_indicator_signal / list_indicators /
    #   get_indicator_params / indicator_analysis 现**已在工具面内**（仅按需通道
    #   可达，本域词典未纳管）。本条为 10-16 退休决定的历史陈述；后续评估：
    #   4 个指标工具是否纳管本词典（技术分析域），见附录 AN §5 后续清单。
     ("technical_analysis", "analyze_pattern",
      "analyze_chart_patterns")),

    ("fundflow", ("资金流", "主力", "净流入", "净流出", "大单", "超大单", "北向",
                  "融资", "主力净额"),
     # get_market_fund_flow / get_concept_fund_flow → get_fund_flow
     ("get_fund_flow", "get_fund_flow_daily", "get_sector_fund_flow")),

    ("sector", ("板块", "题材", "概念", "行业", "龙头", "赛道"),
     # get_sector_trend_analysis / get_sector_history_data / get_industry_ranking
     #   → get_sector_board；get_stock_concept_blocks → get_stock_sector_info
     ("get_sector_board", "get_hot_sectors", "get_sector_stocks",
      "get_sector_fund_flow", "get_stock_sector_info")),

    ("screen", ("选股", "筛选", "强势", "涨停", "跌停", "榜单", "排行", "热榜",
                "哪些股", "几只", "排名", "涨幅榜"),
     ("search_stocks", "get_limit_pool", "get_hot_stocks_with_reason",
      "get_hot_rank", "get_screener_presets",
      "get_market_overview")),

    ("valuation", ("估值", "市盈率", "pe", "pb", "市净率", "财报", "业绩", "营收",
                   "净利润", "基本面", "ROE", "roe"),
     ("batch_valuation_compare", "get_stock_info")),

    ("chip", ("筹码", "成本", "套牢", "获利盘", "集中度"),
     ("get_chip_distribution",)),

    ("volume", ("成交量", "量能", "放量", "缩量", "换手", "obv", "成交额", "量比"),
     # get_volume_analysis / get_obv_analysis → technical_analysis
     ("technical_analysis", "get_order_book")),

    ("market", ("大盘", "指数", "上证", "深证", "创业板", "沪深", "市场", "两市",
                "科创", "北证"),
     # get_market_indices → get_index_quote
     ("get_market_overview", "get_index_quote",
      "get_index_kline", "get_capital_summary")),

    ("dragon", ("龙虎", "席位", "游资", "机构专用"),
     ("get_dragon_tiger",)),   # get_dragon_tiger_detail → get_dragon_tiger

    # ⚠️ 2026-10-17 审计修复：原关键词含裸"信号"，预选 deliverables 里的
    # "技术面综合评分与趋势**信号**"会误命中本域 ⇒ 误加 list_strategies 等
    # 策略管理工具（与个股分析无关，白占 schema 还稀释注意力）。改为带
    # 交易/策略定语的具体词，保住"交易信号/策略信号"的真实召回。
    ("strategy", ("策略", "回测", "启动策略", "停止策略",
                  "交易信号", "策略信号", "买入信号", "卖出信号", "交易记录"),
     # run_backtest / get_backtest_history 已下线：回测桥工具更名为
     # agent_backtest_strategy（auto 泳道）/ agent_backtest_svc（指标·脚本泳道）
     # ★ 2026-10-19 用户裁定（附录 AN·分支 A）：backtest_tools.py 于 10-18 17:02
     #   恢复（用户确认有意），run_backtest / get_backtest_history 现**已在工具
     #   面内**（仅按需通道可达，本域词典未纳管）。本条为 10-16 退休决定的历史
     #   陈述；后续评估：与 agent_backtest_* 双泳道的去留，见附录 AN §5 后续清单。
     ("list_strategies", "get_strategy_detail", "get_strategy_trades",
      "start_strategy", "stop_strategy",
      "agent_backtest_strategy", "agent_backtest_svc")),

    ("intel", ("新闻", "公告", "研报", "消息", "舆情", "政策", "公告", "为什么",
               "有什么利", "最新动态"),
     # search_stock_intel / search_policy_intel / search_sector_intel
     #   / search_comprehensive_intel → search_intel
     ("search_intel",)),

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


# 进程级索引缓存：tools/ 目录运行期不变，建一次全程复用。
# 有界（8 份）：多会话/多 agent 场景下各持一份 defs，不让它无限长。
_INDEX_CACHE_MAX = 8
_INDEX_CACHE: Dict[int, Tuple[dict, Dict[str, Set[str]]]] = {}


def get_tool_index(defs_by_name: Dict[str, dict]) -> Dict[str, Set[str]]:
    """取（并缓存）工具倒排索引。

    ★★ 缓存命中必须校验**对象身份**（`is`），不能只认 `id()`
    （2026-10-13 P9 修：`id()` 是内存地址，旧 defs 被回收后新 defs 完全可能
    拿到同一地址 ⇒ 命中上一份工具面的索引。症状极隐蔽：新激活的工具不在索引里
    ⇒ lint 判它"零相关"⇒ 被裁掉，而工具面本身变了，索引却还停在旧版本。
    旧代码 `if key not in _INDEX_CACHE` 正是只认 id。）
    """
    if not defs_by_name:
        return {}
    hit = _INDEX_CACHE.get(id(defs_by_name))
    if hit is not None and hit[0] is defs_by_name:
        return hit[1]
    idx = build_tool_index(defs_by_name)
    if len(_INDEX_CACHE) >= _INDEX_CACHE_MAX:
        _INDEX_CACHE.pop(next(iter(_INDEX_CACHE)), None)
    _INDEX_CACHE[id(defs_by_name)] = (defs_by_name, idx)
    return idx


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
#  二·补 确定性快路由（2026-10-16 T5）
# ═══════════════════════════════════════════════════════════════════════════
# 动机：闸门只判"要不要花这次 LLM"，但**每次都给**仍要花 1 次模型调用 + ≈3k
#   目录 token。凡"单标的 + 单域意图"这种词典能定的场景，没必要劳 LLM ——
#   正是 `qd_preselect` docstring 那条"确定性校正优先：能被词典/索引查出来的
#   不劳 LLM"的直接落地。
#
# 【为什么只放行这些域】它们是"看某一只"的**明确**意图；而 sector/fundflow/
#   screen/market/capital/knowledge 等常出现在**多标的或全市场**语境
#   （"今天主力资金流入哪些板块"），一旦按单标的收紧反而会漏工具 ⇒ 交给 LLM。
#
# 【为什么把 resolve_stock 前置】域首选工具（如 get_realtime_quote）要求代码入参，
#   而用户给的是名称（"贵州茅台"）⇒ 先解析再取数，与人工走法一致。
_FAST_ROUTE_DOMAINS: Tuple[str, ...] = (
    "quote", "kline", "trend", "chip", "volume", "valuation", "dragon", "capital",
)
_FAST_ROUTE_MAX: int = 4
# 单标的（个股）路径不该捎带"指数/ETF 专用"工具：它们只在市场级问法里有用
# （实测 "600519 现价" 原先会带上 get_index_quote，白挂一份 schema）。
# 名单与 T2 的静态度量口径同源（tmp/_core_breakdown.py 里的 INDEX_ONLY_NAMES），
# 属显式登记而非猜测；新指数工具出现时在此登记一行即可。
_FAST_ROUTE_INDEX_ONLY: Tuple[str, ...] = (
    "get_index_quote", "get_index_kline", "index_fflow",
)


def _stock_codes_in(text: str) -> List[str]:
    """文本里的 6 位标的代码（去重，按出现顺序）。"""
    out: List[str] = []
    for m in re.finditer(r"(?<!\d)\d{6}(?!\d)", str(text or "")):
        if m.group(0) not in out:
            out.append(m.group(0))
    return out


def single_target_anchor(text: str) -> Tuple[str, List[str], List[str]]:
    """判定"单一标的"锚点：返回 (类型, 全部锚点, 全部股票名)。

    类型 ∈ {"code", "name", "code+name", ""}；`""` 表示非单标的（0 个或多个）。
    全部锚点为空/1 个时才可能判为单标的 —— 2 个以上一律不当高置信。
    """
    codes = _stock_codes_in(text)
    names = _hit_stock_names_all(text)
    if len(codes) > 1 or len(names) > 1:
        return "", codes + names, names
    if len(codes) == 1 and len(names) == 1:
        return "code+name", codes + names, names
    if len(codes) == 1:
        return "code", codes, names
    if len(names) == 1:
        return "name", names, names
    return "", [], names


def fast_route_tools(text: str, available_names: Iterable[str]) -> Tuple[List[str], str]:
    """确定性快路由：命中则返回 (工具名列表, 理由)，未命中返回 ([], 理由)。

    判据见模块注释；**纯字符串/词典计算，零 LLM、零网络**。
    """
    kind, anchors, names = single_target_anchor(text)
    if not kind:
        return [], "非单一标的" if anchors else "无标的锚点"
    doms = detect_domains(text)
    if len(doms) != 1:
        return [], ("无域命中" if not doms else "多域命中(%d)" % len(doms))
    dom = doms[0]
    if dom not in _FAST_ROUTE_DOMAINS:
        return [], "域未纳入快路由(%s)" % dom
    avail = set(available_names or ())
    if not avail:
        return [], "无可用候选"
    out: List[str] = []
    # 名称锚点：必须先解析代码，否则取数工具拿不到入参
    if "name" in kind and "resolve_stock" in avail:
        out.append("resolve_stock")
    for t in DATA_DOMAIN_TOOLS.get(dom, ()):
        if t not in avail or t in out:
            continue
        if t in _FAST_ROUTE_INDEX_ONLY:
            continue                     # 指数专用：单标的路径不捎带
        out.append(t)
        if len(out) >= _FAST_ROUTE_MAX:
            break
    if not out:
        return [], "域首选不在按需层(%s)" % dom
    return out[:_FAST_ROUTE_MAX], "ok:%s/%s" % (dom, kind)


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
    """描述压成单行：取**首段首行**（docstring 首行 = 写给预选看的短契约）。

    【F8（2026-10-17）】原实现把整段折叠成一行后按 `limit` 硬切（技能目录用 60），
    会把作者写下的首句契约从**中间砍断** ⇒ 预选看到的是半句话。改为**按段落边界
    截断**：取首段（首个空行之前）的首行，行内空白折叠；仅当首行仍超 `limit` 时
    才截断兜底 —— `limit` 降为安全上限，不再是主截断口径。零新组件：就地改本函数，
    `build_catalog` / `build_skill_catalog` 两个调用点不动（同源同口径）。
    """
    s = (text or "").strip()
    if not s:
        return ""
    paragraph = re.split(r"\n\s*\n", s, maxsplit=1)[0]
    first = (paragraph.splitlines()[0].strip() if paragraph else "")
    first = re.sub(r"\s+", " ", first)
    if not first:
        return ""
    return first[:limit]


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


def _capability_symbol(name: str):
    """取能力层符号（**唯一导入点**），不可用时返回 None。

    ★ 2026-10-13 P9：此前 `capability_domain` 与 `_near_dup` 各写了一份
    **逐字相同的双层 try/except**（复制残留，第二层永远不会走到不同结果）。
    收敛成一个入口：导入只有一处，退化语义由调用方各自声明。
    """
    try:
        from ..capabilities import loader as _loader
    except Exception as exc:                      # 能力层不可导入 ⇒ 本就是空的
        logger.debug("[preselect] 能力层不可用(fail-open): %s: %s",
                     type(exc).__name__, exc)
        return None
    return getattr(_loader, name, None)


def capability_domain() -> str:
    """能力层的来源域名（唯一事实源：`capabilities/loader.CAPABILITY_DOMAIN`）。

    硬编码兜底只为 capabilities 包不可导入时仍能跑（此时能力层本就是空的）。
    """
    dom = _capability_symbol("CAPABILITY_DOMAIN")
    return dom if isinstance(dom, str) and dom else "capability"


def _near_dup(a: str, b: str) -> bool:
    """近重名判定（让位用）——复用能力层实现，失败退化为同名判定。"""
    fn = _capability_symbol("near_dup_tool_names")
    if fn is None:
        return a == b
    try:
        return bool(fn(a, b))
    except Exception as exc:
        logger.debug("[preselect] near_dup 失败(退化为同名判定): %s: %s",
                     type(exc).__name__, exc)
        return a == b


def _capability_folded_text(caps: Sequence[str], reps: int = 6) -> str:
    """能力层**组面折叠**（2026-10-17，对齐 OpenAI namespace 目录形态）。

    动机（O2 残留）：能力层 40 行全列 ≈2k 字符，工具数涨到 300 时爆的就是它；
    但 2026-10-11 试过缩成"另有 N 个"裸指针行 ⇒ 功能性断裂被 P14 回退
    （`PRESELECT_SYSTEM` 要求"目录里找不到工具就删交付要素"，指针行不列名字
    ⇒ 模型既无法按名调用、又会删掉只能由能力层支撑的交付要素）。

    ★ 组面形态是两端之间的第三解：组名+计数+描述+**代表名仍列**（模型可点）
    + **可行动检索指针**（list_tools/search_tools 真实存在，按需取全量）。
      P14 不变量由此保住：名字可见 + 兜底可达。
    ★ 代表取排序前 N（确定性，守卫测试可钉）；真正发现路径是指针+search_tools。
    ★ env `QD_PRESELECT_CAP_FOLD=0` 整体回退全列（回退路径有守卫测试）。
    """
    shown = ", ".join(sorted(caps)[:reps])
    return (f"capability（{len(caps)} 个）外部能力包——仅当上面的域内工具确实无法"
            f"覆盖某条交付信息时才从这里选；例：{shown}；完整名单用 "
            f"list_tools(domain='capability') 或 search_tools 检索后点名。")


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
    # 与全列路径同口径：只收 defs 里真实存在的能力层名（上游可能给过期名单）
    caps = [n for n in (capability_names or ()) if n in defs_by_name]
    if not caps:
        return dom_txt, dom_kept
    fold = (os.getenv("QD_PRESELECT_CAP_FOLD") or "1").strip() not in ("0", "false", "False")
    if fold:
        # 组面折叠：只缩**呈现**，合法点名集（cap_kept）保持全量 ⇒ lint R0/R1、
        # parse_plan、catalog_size 口径全不变（守卫测试钉这两条）。
        return (dom_txt + "\n\n" + CAP_SECTION_NOTE + "\n"
                + _capability_folded_text(caps), dom_kept + list(caps))
    cap_txt, cap_kept = build_catalog(defs_by_name, only_names=caps, max_desc=max_desc)
    if not cap_kept:
        return dom_txt, dom_kept
    return (dom_txt + "\n\n" + CAP_SECTION_NOTE + "\n" + cap_txt,
            dom_kept + cap_kept)


# ═══════════════════════════════════════════════════════════════════════════
#  四、域特征闸门（成本闸：要不要花这次 LLM）
# ═══════════════════════════════════════════════════════════════════════════
# 【T4·2026-10-16 扩面】原实现只有「6 位数字」匹配（**匹配股票代码**），
# 变量名 `_CODE_HINT_RE` 与实际语义不符 ⇒ **纯编码请求一律不过闸**
# （实测漏判："python 调试一下这个文件"、"帮我把这段代码改成异步"、
#  "看看 app/agent/qd_agent.py 里 _sync_tool_tiers 怎么实现的"）⇒ 不预选 ⇒
# T2 已移出必注入的编码工具（read/edit/write/glob/grep/task）不会被首轮激活。
# 现按"编码意图"重写，四类信号：
#   ① 6 位标的代码（原信号，保留）；
#   ② 源码文件名（文件扩展名 ⇒ 任务在动工作区文件）；
#   ③ 语言/运行时/工具链名（python / pytest / npm / docker / git …）；
#   ④ 异常名 与 代码动作词/关键字。
# 仍可用 `QD_PRESELECT_HINTS` 覆盖域词、`QD_PRESELECT_GATE=0` 整体关闸。
_CODE_HINT_RE = re.compile(
    r"(?<!\d)\d{6}(?!\d)"                                    # 6 位标的代码
    r"|\.(?:py|js|ts|tsx|jsx|vue|md|json|ya?ml|toml|ini|cfg|sql|sh|ps1|bat)(?![0-9A-Za-z_])"
    r"|(?:backend_api_python|QuantDinger-Vue|app/agent|app[\\/]routes|tests?[\\/][\\w\\-]+)"
    r"|\b(?:python|python3|pytest|npm|pnpm|yarn|node|npx|git|ruff|mypy|flake8|"
    r"black|isort|docker|compose|venv|pip|conda)\b"
    r"|\b(?:traceback|syntaxerror|importerror|modulenotfounderror|typeerror|"
    r"valueerror|keyerror|attributeerror|nameerror|indexerror|runtimeerror|"
    r"zerodivisionerror|exception)\b"
    # 【T4 v2 收窄】`写`/`改` 单字过宽（实测 "帮我写一首诗" 也过闸 ⇒ 白花一次
    # LLM），改为**带宾语**的编码短语；`文件` 单字也挪走（"把这个文件发我"这类
    # 非编码请求会误收），保留"目录/工作区"这类强场景词。
    r"|(?:写个|写一个|写一段|写点|改写|重写|改成|改个|改一下|改这段|加个|加一个|"
    r"重构|调试|报错|异常|脚本|代码|函数|模块|单元测试|跑一下|跑个|跑一遍|"
    r"装包|依赖|编译|部署|崩溃|堆栈|工作区|目录)"
    r"|\b(?:def|class|import|return|async|await|lambda|self)\b"
    r"|\b(?:build|install|compile|lint|typecheck|unittest|refactor|debug)\b",
    re.I,
)

# 通用词（不含领域专有名词）：判断"像不像要动专业工具"的弱特征。
_GENERIC_HINTS = (
    "股", "股票", "个股", "A股", "港股", "美股", "行情", "K线", "k线", "涨停", "跌停",
    "板块", "题材", "概念", "资金流", "主力", "龙虎", "技术面", "均线", "MACD", "KDJ",
    "估值", "财报", "业绩", "选股", "龙头", "强势", "持仓", "仓位", "止损", "止盈",
    "策略", "回测", "大盘", "指数", "涨幅", "跌幅", "反弹", "回调", "突破",
    "stock", "quote", "sector", "ticker",
    "数据", "统计", "查一下", "对比",
    # 【T4】编码/运维域词：与 _CODE_HINT_RE 互补（正则可覆盖的形态走正则，
    # 这里补"词面"信号）。刻意保持精简——过宽会让成本闸失去意义（每轮多花 LLM）。
    "代码", "脚本", "函数", "模块", "文件", "目录", "报错", "异常", "调试", "日志",
    "测试", "用例", "依赖", "环境", "配置", "接口", "命令", "终端", "版本",
)

# ★ 2026-10-14 G5：**单源派生**，闸门词表 = 通用词 ∪ 域词典关键字。
# 此前两套词表手工并行维护，实测域词典 129 词里有 **100 个**不在闸门里
# （现价 / 北向 / 收盘 / 形态 / 支撑 / macd / 净流入 …）⇒ 这些再明确不过的金融
# 查询**根本进不了** plan+lint 质量路径，只能吃盲兜底（词典首选 ≤4 个）。
# 改为派生后：新增域词自动进闸门，不会再出现"词典认得、闸门不认"的漂移。
#
# ⚠ 行为变更（需知悉）：闸门放行面扩大 ⇒ 更多查询会花一次预选 LLM。
#   这是**回到设计**而非新增行为：`has_domain_hint` 的定位是**成本闸**不是质量闸
#   （见其 docstring："为了不出错干脆不做，把收益一起扔了"是方向错误），
#   真正的误选防线是 lint 的零相关裁剪/整体作废。仍可用 `QD_PRESELECT_HINTS`
#   整体覆盖，或 `QD_PRESELECT_GATE=0` 关闸。
_DEFAULT_HINTS = tuple(dict.fromkeys(
    _GENERIC_HINTS + tuple(w for _d, kws, _t in DATA_DOMAINS for w in kws)))


# ═══════════════════════════════════════════════════════════════════════════
#  四·补 标的名闸门（2026-10-16 新增，用户要求"用现有 resolve_stock/all_codes 数据"）
# ═══════════════════════════════════════════════════════════════════════════
# 动机：用户按**公司中文名**提问是常见形态（"分析一下贵州茅台"），但域词表与
# `_CODE_HINT_RE` 都覆盖不到 ⇒ 不过闸 ⇒ 不预选。实测该形态改前同样不过闸。
#
# 【为什么是"名单子串扫"而不是逐次调 resolve_stock】
#   · `resolve_stock` 走 basicinfo_db 模糊搜索（单次调用 + 建结果），不适合在
#     闸门这种**同步热路径**里对每个问题都调；
#   · 名单一次加载实测 77ms、5236 只；之后每次匹配 ~0ms（3 字以上子串扫）。
#
# 【为什么最小 3 字】名称分布实测：2 字 1 个、3 字 669、4 字 4463、5 字 101。
#   3 字起可避开绝大多数常用词；已知残留（"农产品""太阳能"等 3 字名）在
#   非股票语境里会误过闸 —— 这正是 docstring 说的"宁可宽"：代价只是一次 LLM
#   + 3k 目录，而**质量**由 lint 的零相关裁剪兜底。可用环境变量调高。
#
# 【通用性】名单来自本地库，不硬编码；库不可用/导入失败 ⇒ 空名单 ⇒ 退回原行为。
_NAME_LIST_TTL_S = 1800.0          # 名单缓存有效期（秒）；0 表示不缓存
_NAME_MIN_LEN = max(2, int(os.getenv("QD_PRESELECT_NAME_MIN_LEN", "3")))
_NAME_GATE_ON = os.getenv("QD_PRESELECT_NAME_GATE", "1") not in ("0", "false", "False")

# 口语简称：用户常省略地域/国别/修饰前缀（"贵州茅台"→"茅台"）。这里的不变量是
# **唯一性**——只有剥离后在**全市场唯一**才纳入别名集合，否则会误判（例："平安"
# 同时命中平安银行与中国平安 ⇒ 丢弃；"茅台"唯一 ⇒ 纳入）。
_NAME_MODIFIER_PREFIXES = (
    "贵州", "中国", "上海", "深圳", "北京", "广州", "天津", "重庆", "新疆", "西藏",
    "内蒙古", "云南", "四川", "湖南", "湖北", "河南", "河北", "山东", "山西", "江苏",
    "浙江", "安徽", "福建", "江西", "陕西", "甘肃", "青海", "宁夏", "海南", "吉林",
    "辽宁", "黑龙江", "广西", "青岛", "厦门", "大连", "宁波", "苏州", "无锡", "杭州",
    "南京", "武汉", "成都", "西安", "济南", "合肥", "长沙", "郑州", "昆明", "太原",
)
_NAME_ALIAS_MIN_LEN = 2

_NAME_CACHE: dict = {"names": None, "aliases": None, "at": 0.0}


def _stock_name_sets() -> tuple:
    """本地库里的 (股票中文名集合, 唯一·口语简称集合) —— 惰性加载 + 进程内缓存。

    失败一律返回两个空集（fail-open，退回原行为）。别名见
    `_NAME_MODIFIER_PREFIXES`：只有剥离前缀后在**全市场唯一**才纳入。
    """
    if not _NAME_GATE_ON:
        return frozenset(), frozenset()
    import time as _t
    now = _t.time()
    cached = _NAME_CACHE.get("names")
    if (cached is not None and _NAME_LIST_TTL_S > 0
            and (now - _NAME_CACHE.get("at", 0.0)) < _NAME_LIST_TTL_S):
        return cached, (_NAME_CACHE.get("aliases") or frozenset())
    names = set()
    alias_count: dict = {}
    try:
        from app.utils.basicinfo_db import get_stock_basic_db
        for row in (get_stock_basic_db().get_all_stocks() or []):
            nm = str((row or {}).get("name") or "").strip()
            if len(nm) >= _NAME_MIN_LEN:
                names.add(nm)
            for pfx in _NAME_MODIFIER_PREFIXES:
                if nm.startswith(pfx) and len(nm) - len(pfx) >= _NAME_ALIAS_MIN_LEN:
                    alias_count[nm[len(pfx):]] = alias_count.get(nm[len(pfx):], 0) + 1
                    break
    except Exception as e:                       # fail-open：库/依赖不可用即退回原行为
        logger.debug("标的名闸门名单加载失败（退回原行为）: %s", e)
        names = set()
        alias_count = {}
    # 唯一性过滤：同一简称对应多只股票 ⇒ 有歧义 ⇒ 整体丢弃（宁可不过闸也不误判）
    aliases = frozenset(a for a, c in alias_count.items() if c == 1)
    frozen = frozenset(names)
    _NAME_CACHE["names"] = frozen
    _NAME_CACHE["aliases"] = aliases
    _NAME_CACHE["at"] = now
    return frozen, aliases


def _hit_stock_names_all(query: str) -> List[str]:
    """问题里出现的**全部**已知股票名（最长优先、不重叠；按出现顺序去重）。

    【为什么返回全部】快路由（T5）要判"是否单一标的"：只返回首个命中的写法会把
    "茅台和五粮液哪个估值便宜"这类**双标的**查询误判成单标的 ⇒ 路由到错误的窄工具集。
    """
    q = (query or "").strip()
    if not q:
        return []
    names, aliases = _stock_name_sets()
    if not names and not aliases:
        return []
    out: List[str] = []
    i, n = 0, len(q)
    while i < n:
        hit = ""
        length = min(12, n - i)                   # 名字最长 6 字；留余量
        while length >= _NAME_ALIAS_MIN_LEN:
            sub = q[i:i + length]
            if sub in names or sub in aliases:    # 全名 / 唯一简称 同权
                hit = sub
                break
            length -= 1
        if hit:
            if hit not in out:
                out.append(hit)
            i += len(hit)                         # 不重叠：跳过已消费片段
        else:
            i += 1
    return out


def _hit_stock_name(query: str) -> str:
    """首个命中的股票名（未命中返回空串）—— 兼容原调用方。"""
    hits = _hit_stock_names_all(query)
    return hits[0] if hits else ""


def clear_name_cache() -> None:
    """清空名单缓存（数据库同步后或测试用）。"""
    _NAME_CACHE["names"] = None
    _NAME_CACHE["aliases"] = None
    _NAME_CACHE["at"] = 0.0


def has_domain_hint(query: str, hints: Optional[Sequence[str]] = None) -> bool:
    """问题是否带有"需要专业工具"的领域特征。

    【定位·2026-10-01 重构后】这只是**成本闸**：决定要不要为这个任务花一次
    LLM 调用（+ 3k 目录 token）。它**不负责**预选质量——质量由 `lint_selection`
    负责。首版把质量寄托在它身上（没特征就不预选）是方向错误：
    那等于"为了不出错干脆不做"，把收益一起扔了。

    真正的误选防线是 linter 的「零相关裁剪 + 整体作废」：直接判"选出来的工具
    跟这个问题有没有关系"，比"猜这个问题像不像金融问题"更准。

    【2026-10-16 扩面（三路）】
      ① 编码意图：`_CODE_HINT_RE` 重写（原先只匹配 6 位股票代码，纯编码请求全漏）；
      ② 域词表补编码/运维词；
      ③ 标的名兜底：名单来自本地库（`_stock_name_set`），覆盖"按中文名提问"。
    三者都是**放宽**闸门 —— 与本函数"成本闸"定位一致：多花一次 LLM 是可控代价，
    漏掉专业任务（首轮缺工具）才是真损失。`QD_PRESELECT_NAME_GATE=0` 可关第③路。
    """
    q = (query or "").strip()
    if not q:
        return False
    if _CODE_HINT_RE.search(q):
        return True
    words = [w.strip() for w in (hints if hints is not None else _DEFAULT_HINTS) if w.strip()]
    if any(w in q for w in words):
        return True
    # 【2026-10-16】标的名兜底：按公司中文名提问（"分析一下贵州茅台"）既不含 6 位
    # 代码也不含域词 ⇒ 原先不过闸。名单来自本地库，见 `_stock_name_set`。
    return bool(_hit_stock_name(q))


# ═══════════════════════════════════════════════════════════════════════════
#  五、提示词 —— 外置 plan（对齐 plan_system.txt 的"工具面 = 任务书副产品"）
#  2026-10-14 外置到 prompts/tool_preselect.yaml（system + user 两段，勿再写回常量）
# ═══════════════════════════════════════════════════════════════════════════
from ..prompts import load_prompt_doc as _load_prompt_doc

_preselect_prompts = _load_prompt_doc("tool_preselect")
PRESELECT_SYSTEM = _preselect_prompts["system"]
PRESELECT_PROMPT = _preselect_prompts["user"]

#: §14.4 固定模板骨架 = system + user 模板空槽（query/already/catalog/history/
#: skills/max_tools 全空）的字符数。`preselect_catalog_chars` 用它补足"预选
#: 那一轮输入"里与目录无关的固定部分；口径单一可复现（同模板同值）。
_PRESELECT_FIXED_CHARS = len(PRESELECT_SYSTEM) + len(PRESELECT_PROMPT.format(
    query="", already="", catalog="", max_tools=0, history="", skills=""))


def shrink_catalog(query: str, names: Sequence[str],
                   defs_by_name: Dict[str, dict],
                   limit: int = 0,
                   per_domain_keep: int = 0,
                   domain_of=None,
                   defaults: Sequence[str] = ()) -> List[str]:
    """按查询把候选目录**预过滤**到 limit 个（保持原顺序；limit<=0 时原样返回）。

    【2026-10-16 实测动机】预选那一次 LLM 调用实测 37~50s，输入本体 = 86 个工具的目录
    （≈6–7k tokens）。该网关对大输入极敏感 ⇒ 缩输入是最直接的提速手段。
    本函数是**纯字符串打分**（零 LLM / 零网络），命中三条线索：
      ① 工具名（含下划线拆词）出现在查询里   —— 最强
      ② 工具描述里的词出现在查询里           —— 中（描述截前 120 字，避免长尾噪声）
      ③ detect_domains 命中的域 → 该域候选加分
    并**保底**保留每个域前 `per_domain_keep` 个，防"没命中但确实需要"。

    【2026-10-17 接线修复】本函数自 2026-10-16 写好后**在 app/ 全仓零调用**——
    预选每次仍把 87 个工具全量目录塞进 prompt（实测 7197 字符）。三个失效点：
      ① `domain_of` 没人传对：`qd_tool_face._tool_domain` 返回粗粒度来源域
         （finance），与 detect_domains 的细粒度域（quote/kline…）对不上
         ⇒ `hit` 恒 False ⇒ keep 空 ⇒ 原样返回（不缩）。现 `domain_of=None`
         时内部用 `DATA_DOMAIN_TOOLS` 反转的细粒度映射（唯一事实源，不另维护）。
      ② 无域词查询（最常见问法「分析600519」——"分析"不在任何域关键词里）
         旧逻辑 `if not doms: return list(names)` 原样返回 ⇒ 不缩。
         新增 `defaults` 形参：调用方给单标的默认代表集（见
         `single_target_default_tools`），无域词时走它。
      ③ 目录缩了 ≠ 不可达：调用方必须把 `parse_plan`/`lint_selection` 的
         合法点名集保持**全量按需层**（qd_preselect._maybe_preselect_tools
         已分离 `names`（目录呈现）与 `valid`（合法点名）两个集合）——
         缩的只是 LLM 能看到什么，不是它能激活什么。

    成本账（300 工具场景）：目录 ≤ limit（默认 24）行 ⇒ 预选输入恒定 ≈2k 字符，
    与工具总数脱钩；未命中工具仍可由 search_tools/list_tools 发现（fail-open）。
    """
    if limit <= 0 or not names:
        return list(names)
    q = str(query or "").lower()
    if not q:
        return list(names)
    # ── 域优先（2026-10-16 二次修正）──────────────────────────────────
    # 首版用"工具名/描述关键词"打分 ⇒ 对 "分析002343" 这类**无关键词**的查询
    # 完全无差异（46 个候选原样返回，四个不同 query 输出一模一样，实测发现）。
    # 改为先按 detect_domains 命中的域分组：命中域**全收**（语义主力），
    # 未命中域只保代表作。
    doms = set(detect_domains(q))
    if not doms:
        # 【2026-10-17】无域词：单标的默认代表集（"分析600519" 的唯一收窄信号）。
        # 连默认集都没有（非单标的、无锚点）⇒ 不缩，行为与改造前一致（fail-open：
        # 宁可全量也不要空目录——目录空 = 模型没有任何候选可选）。
        default_set = [n for n in (defaults or ()) if n in set(names)]
        if default_set:
            # defaults 是调用方按域优先级排好的优选序（quote/kline/trend…），
            # **不按 names 字典序重排**——域序即语义序，重排等于把 quote 和
            # dragon 混同。只做 limit 截断。
            return list(default_set)[:max(0, limit)]
        return list(names)
    dom_map = domain_of if callable(domain_of) else _tool_domain_map()
    keep: List[str] = []
    generic: List[str] = []
    for n in names:
        try:
            nd = str(dom_map(n) or "") if callable(dom_map) else ""
        except Exception:
            nd = ""
        hit = bool(nd) and any(d and (d in nd or nd in d) for d in doms)
        if hit:
            keep.append(n)
        else:
            generic.append(n)
    if per_domain_keep > 0 and len(keep) < limit:
        keep.extend(generic[:max(0, limit - len(keep))])
    if not keep:
        return list(names)
    return _cap_keep(keep, names, limit)


def _cap_keep(keep: Sequence[str], names: Sequence[str], limit: int) -> List[str]:
    """按 limit 截断并保持 `names` 原顺序（裁剪不改呈现次序，模型按目录序取用）。"""
    keep = list(keep)[:max(0, limit)]
    order = {n: i for i, n in enumerate(names)}
    keep.sort(key=lambda n: order.get(n, 1 << 30))
    return keep


# 工具名 → 细粒度数据域 的反转映射（DATA_DOMAIN_TOOLS 唯一事实源派生，勿单独维护）。
# 2026-10-17：shrink_catalog 的 domain_of 默认值——此前没人传对（粗粒度 finance
# 对不上细粒度 quote），导致缩目录恒不生效。
_TOOL_DOMAIN_MAP: Dict[str, str] = {}


def _tool_domain_map():
    """返回 工具名→细粒度域 的查找函数（懒建 + 进程级缓存）。"""
    global _TOOL_DOMAIN_MAP
    if not _TOOL_DOMAIN_MAP:
        for dom, tools in DATA_DOMAIN_TOOLS.items():
            for t in tools:
                _TOOL_DOMAIN_MAP.setdefault(t, dom)
    return lambda n: _TOOL_DOMAIN_MAP.get(n, "")


# 单标的（个股）分析的代表域：域词典里**面向个股**的细粒度域。
# 出处 = auto-agent SKILL.md 单股分析任务书的取数维度（行情/K线/技术面/资金流/
# 估值/筹码/量能/龙虎/情报/资本面）；不含 market/sector/screen/strategy/
# knowledge——那些是多标的或非取数语境（"分析大盘"不会走单标的路径）。
# 可用 QD_PRESELECT_SINGLE_DOMAINS 覆盖（逗号分隔）。
_SINGLE_STOCK_DOMAINS: Tuple[str, ...] = (
    "quote", "kline", "trend", "fundflow", "valuation",
    "chip", "volume", "dragon", "intel", "capital",
)


def single_target_default_tools(text: str, available_names: Iterable[str]) -> List[str]:
    """单标的 + 无域词问法（「分析600519」）的默认代表集：每域首选工具。

    纯确定性（复用 `single_target_anchor` 锚点判定 + DATA_DOMAINS），零 LLM。
    非单标的（0/多个锚点）返回 [] ⇒ 调用方走原路径（不缩或域全收）。

    【为什么需要】「分析600519」是 auto-agent 最标准的问法，但：
      · detect_domains 无「分析」这个词 ⇒ 域词典不命中；
      · 倒排索引 min_hits=3，短查询 token 太少 ⇒ 恒空；
      · 快路由要求恰好单域 ⇒ 不过。
    三个确定性信号全失效 ⇒ 预选目录 87 个全量。本函数补上第四条信号：
    有唯一标的锚点就是"分析某一只"，其取数维度 = 单股任务书那十域的首选。
    """
    kind, _anchors, _names = single_target_anchor(text)
    if not kind:
        return []
    avail = set(available_names or ())
    override = (os.getenv("QD_PRESELECT_SINGLE_DOMAINS") or "").strip()
    doms = tuple(d.strip() for d in override.split(",") if d.strip()) if override \
        else _SINGLE_STOCK_DOMAINS
    out: List[str] = []
    for dom in doms:
        pick = _PREFERRED.get(dom)
        if pick and pick in avail and pick not in out:
            out.append(pick)
    return out


def _preselect_catalog_limit() -> int:
    """目录上限（`QD_PRESELECT_CATALOG_MAX`，默认 24；0/负 = 关闭预过滤）。"""
    import os as _os
    raw = (_os.getenv("QD_PRESELECT_CATALOG_MAX") or "24").strip()
    try:
        return int(raw)
    except ValueError:
        return 24


def schema_chars_of(defs_by_name) -> int:
    """工具面 schema 的**总字符数**（成本真变量，E3）。

    为什么不用工具**个数**：真实成本随描述厚度漂移——实测工具 92→101 仅 +10%，
    而 schema 49,507→75,299 字符 +52%；「工具没涨、描述涨胖」是按个数触发
    （O-14 原文「工具面破 120 个」）的盲区。
    口径：与探针 `tmp/_schema_stats.py` 一致，即 `json.dumps(defs, ensure_ascii=False)`
    的长度（同一份 dict 稳定可复现）。

    【§14.4 口径边界（2026-10-19）】本函数是**工具 schema 专用**度量，回答
    "工具面本身多胖"；预选那一轮输入的**总字符预算**（工具目录 + 技能候选段 +
    固定模板，不含选中后才加载的正文）由 `preselect_catalog_chars` 度量。
    两者分工明确、不得混称：`QD_PRESELECT_TIER_CHARS` 仍是唯一阈值 env，
    不新增平行的技能字符阈值。
    """
    try:
        return len(json.dumps(defs_by_name, ensure_ascii=False))
    except Exception:
        return 0


def _preselect_tier_chars() -> int:
    """O-14 分级目录的触发阈值（**schema 总字符数**）。

    env `QD_PRESELECT_TIER_CHARS`，默认 60000（源报告 `:41-45` 建议值）。
    0/负 = 恒触发分级（等价改造前「无条件收窄」的行为，回退开关）。

    【§14.4】本阈值只管**工具面分级触发**；技能候选的规模由 Top-K 计数
    （`QD_PRESELECT_SKILL_TOPK`）治理、总输入由 `preselect_catalog_chars`
    观测——三者各司其职，不存在第二个字符阈值。
    """
    raw = (os.getenv("QD_PRESELECT_TIER_CHARS") or "60000").strip()
    try:
        return int(raw)
    except ValueError:
        return 60000


def tier_catalog_enabled(defs_by_name) -> bool:
    """按 schema 字符数决定是否启用分级目录（O-14 触发条件，E3）。

    高于阈值 ⇒ 启用（只展开命中域，省预选那一轮的目录 token）；
    低于阈值 ⇒ 不分级（面本就便宜，全量目录给足命中率）。

    【§14.4】触发判据保持 F14 冻结口径（工具 schema 字符数）；技能段是否
    膨胀不在本函数判据内——技能侧由 Top-K 从结构上封顶（段长与技能总数
    脱钩），无需第二个字符触发器。
    """
    thr = _preselect_tier_chars()
    if thr <= 0:
        return True
    return schema_chars_of(defs_by_name) > thr


# ═══════════════════════════════════════════════════════════════════════════
#  五之二、技能目录（2026-10-17：skill 进预选层，排在工具之前）
# ═══════════════════════════════════════════════════════════════════════════
# 【为什么要有这一段】技能选择与工具选择原本是**两条互不知情的平行链**：
#   技能 = prefetch 里 `_rank_skills` 关键词 top-1（qd_service.py）
#   工具 = 预选器 LLM 点名（本模块，输入刻意剥掉 prefetch 注入块）
# 结果不是"并行"而是**技能被工具面过滤**：预选在不知道本轮用哪套方法论的情况下
# 选完工具即成既定工具面 ⇒ 技能正文要求的工具根本不在面里，步骤被静默跳过
# （run8 实证：auto-agent 步骤 1 要 `agent_get_kline`，预选没给 ⇒ 该步消失）。
# 更硬的证据：7 个技能里有 3 个都声明适用"分析某只股票"（auto-agent /
# auto-finance-analysis-stock / auto-stock-analyze），关键词 top-1 在真歧义上
# 是硬伤——这正是语义判断（LLM）比 2-gram 打分强的地方。
#
# 【为什么不改模板】`PRESELECT_PROMPT` 是 v9 逐字对拍契约；条件段按 T6 先例
# （coding_hint）**完全在 Python 侧组装**：关掉时零痕迹，打开时拼进 catalog
# 入参、位于工具目录**之前**（方法论先行）。契约字段（`"skill"`）由本段自带
# 说明，不落模板 ⇒ 关停路径与改造前**逐位一致**。
_SKILL_LINE = "{name} | {desc} | 声明工具: {tools}"

#: 技能段段头（全量与 Top-K 两条路径共用同一契约文案——分叉必然漂移）。
_SKILL_CATALOG_HEAD = (
    "【技能候选·先选技能，再选工具】每行：技能名 | 适用场景 | 声明工具\n"
    "技能是方法论，优先于工具：先挑最匹配本任务的一个技能；没有匹配的就把\n"
    "JSON 的 skill 字段填 null（技能名必须逐字来自本段，禁止编造）。\n"
    "选了某技能后，它「声明工具」里你没点到的会自动补进工具面（按声明顺序，"
    "受工具面容量约束），无需重复点名。\n"
    "输出 JSON 时新增 \"skill\" 字段：填上面某个技能名，或 null。\n"
)

#: §14.1 可见性兜底指针：截断后告知尚有多少可用技能 + 发现路径 + 禁编造。
#: 【为什么不列名】列了未展示的名 ⇒ 模型照抄幻觉名与真名无从分辨（P14 事故
#: 同型：指针行只说"另有 N 个"不列名，正是当年能力层的修法）；只给计数与路径。
_SKILL_POINTER = (
    "另有 {n} 个可用技能未在本段展示（与本任务相关度未过门槛或探索轮换未至）。"
    "可用 list_skills() 查看全部可用技能、read_skill() 读取其正文；"
    "技能名必须逐字来自本段，不得编造未展示名称。"
)


def _preselect_skill_on() -> bool:
    """技能目录是否进预选（`QD_PRESELECT_SKILL`，默认开；"0"/"false" 关）。

    关掉 ⇒ 无技能段、无 skill 字段、无补位、无 hint ⇒ 与 2026-10-17 前逐位
    一致（`_rank_skills` 仍是技能选择的唯一路径）。守卫见 tests/test_v17j_*。
    """
    return (os.getenv("QD_PRESELECT_SKILL") or "1").strip().lower() \
        not in ("0", "false", "no", "off")


def _preselect_skill_topk() -> int:
    """技能目录 Top-K（`QD_PRESELECT_SKILL_TOPK`，默认 4；0/负 = 旧全量路径）。

    §15.2：4 = 3 常规排序位 + 1 合格新技能探索位。0/负 时
    `build_skill_catalog_topk` 内部委托旧 `build_skill_catalog`（逐位一致）。
    """
    from .candidate_index import skill_topk
    return skill_topk()


def _preselect_skill_tools_max() -> int:
    """技能声明工具的补位**下限基数**（`QD_PRESELECT_SKILL_TOOLS_MAX`，默认 4）。

    【F6（2026-10-17）：由"上限"改判为"下限基数"】`_merge_skill_declared` 的
    room = `max(本值, 该技能声明数)` —— 技能声明工具是「方法论合同」（market-screener
    12 个、auto-stock-analyze 15 个），**不再按固定值截断**，本值只保证其声明量
    不足时的最小 room。整体仍受 `QD_MAX_ACTIVE_TOOLS` 硬闸约束（F6 已从 12 抬至 20，
    正是为容纳"点名 ≤8 + 技能声明 ≤15"）；真正的淘汰优先级由批 3 的 F24 优先级栈接管。
    """
    try:
        return max(0, int((os.getenv("QD_PRESELECT_SKILL_TOOLS_MAX") or "4").strip()))
    except ValueError:
        return 4


def build_skill_catalog(skills) -> Tuple[str, Dict[str, List[str]]]:
    """技能目录文本 + {技能显示名: [声明工具名]}（确定性，零 LLM）。

    【单一事实源】成员与声明工具全部来自 `skills.list_skills()`（frontmatter
    `tools:` 早已解析进 `SkillInfo.tools`，2026-10-17 起由 list_skills 暴露）
    ——不另维护名单，与 `tool_groups.data 组 ← DATA_DOMAIN_TOOLS 派生`同型。

    【fail-open】skills 为 None / 取不到 / 无技能 ⇒ ("", {}) ⇒ 整段不出现，
    预选输入与改造前**一字不差**（不是"空目录"，是"没有这一段"）。

    Returns:
        (目录文本, 技能→声明工具映射)。映射供 `parse_plan(valid_skills=)`
        与 `qd_preselect._merge_skill_declared` 消费，二者共用同一份字典，
        不存在"目录里写了 A、补位按 B"的漂移。
    """
    if skills is None:
        return "", {}
    try:
        infos = skills.list_skills() or []
    except Exception as e:      # 技能系统不可用 ≠ 预选不可用（fail-open）
        logger.debug("[preselect] 技能目录获取失败(fail-open): %s: %s",
                     type(e).__name__, e)
        return "", {}
    lines: List[str] = []
    tmap: Dict[str, List[str]] = {}
    for info in infos:
        if not isinstance(info, dict):
            continue
        name = str(info.get("name") or "").strip()
        if not name:
            continue
        desc = _one_line(str(info.get("description") or ""), 60) or "（无说明）"
        tools = [str(t).strip() for t in (info.get("tools") or []) if str(t).strip()]
        tmap[name] = tools
        lines.append(_SKILL_LINE.format(
            name=name, desc=desc,
            tools=",".join(tools) if tools else "（未声明）"))
    if not lines:
        return "", {}
    # 尾随空行：模板里 {skills} 与"候选工具目录"标题**同行**，段后必须换行
    # 才能让标题起新行（空串时整行与改造前逐字一致）。
    return _SKILL_CATALOG_HEAD + "\n".join(lines) + "\n\n", tmap


def _render_skill_section(shown, total: int) -> Tuple[str, Dict[str, List[str]]]:
    """展示集 → (段文本, 声明工具映射)。`shown` 为 Candidate 序列（含 name/desc/tools）。

    与 `build_skill_catalog` 同行格式（`_SKILL_LINE`）同口径；差异只在**展示集
    来源**（Top-K 截断 + 探索位）与截断后的可见性兜底指针（§14.1）。
    """
    lines: List[str] = []
    tmap: Dict[str, List[str]] = {}
    for c in shown:
        tools = [str(t).strip() for t in (c.declared_tools or []) if str(t).strip()]
        tmap[c.name] = tools
        desc = _one_line(c.description, 60) or "（无说明）"
        lines.append(_SKILL_LINE.format(
            name=c.name, desc=desc,
            tools=",".join(tools) if tools else "（未声明）"))
    hidden = max(0, int(total) - len(lines))
    if hidden > 0:
        lines.append(_SKILL_POINTER.format(n=hidden))
    if not lines:
        return "", {}
    return _SKILL_CATALOG_HEAD + "\n".join(lines) + "\n\n", tmap


# ═══════════════════════════════════════════════════════════════════════════
#  五之三、预选补充域中文标签（2026-10-19 自 qd_preselect.py 迁入）
# ═══════════════════════════════════════════════════════════════════════════
# 【为什么迁这里】域词汇表是**预选管线知识**（DATA_DOMAINS 同家的中文面），
# 不属于 qd_preselect 的 mixin 编排职责；qd_preselect 因此守住 ≤600 行单职责
# 预算（tests/test_v6_qd_agent_split）。纯函数 + 常量，零行为变化。

#: 预选补充域 → 模型面中文标签（2026-10-17 审计 T3）：lint 覆盖补位加进来的
#: 工具，工具地图里必须带原因，否则模型不知道"为什么多出一个没点过的工具"，
#: 实测导致预选 deliverables 要的估值口径被白白放着（报告留"未获取 PE/PB"）。
PRESELECT_DOMAIN_CN = {
    "kline": "K线/走势", "trend": "技术面/趋势", "fundflow": "资金流",
    "sector": "板块/行业", "screen": "选股/筛选", "valuation": "估值/基本面",
    "chip": "筹码", "volume": "量能", "market": "大盘/市场",
    "dragon": "龙虎榜", "strategy": "策略/回测", "intel": "情报/新闻",
    "capital": "两融/资本面", "knowledge": "知识库",
}


def preselect_additions(report: dict) -> dict:
    """从预选报告里取出「lint 覆盖补位」加进来的工具 → {工具名: 域中文标签}。

    只认 `lint.coverage` 中 `auto_added=True` 的条目（R1 高置信补位）；
    模型自己点名的不算——那些没有"为什么多出来"的解释负担。
    """
    out: dict = {}
    lint = (report or {}).get("lint") or {}
    for item in lint.get("coverage") or []:
        if not isinstance(item, dict) or not item.get("auto_added"):
            continue
        tool = item.get("tool")
        dom = item.get("domain") or ""
        if tool:
            out[tool] = PRESELECT_DOMAIN_CN.get(dom, dom or "相关维度")
    return out


def build_skill_catalog_topk(skills, query: str, *, topk: Optional[int] = None,
                             now: Optional[float] = None,
                             exposure: Optional[dict] = None
                             ) -> Tuple[str, Dict[str, List[str]], dict]:
    """Top-K 渐进披露技能目录（2026-10-19 阶段 3 §5/§14.1/§14.2/§14.4/§15.2；
    阶段 5 增反馈软重排 §14.3/§15.4）。

    【单一入口】`QD_PRESELECT_SKILL_TOPK ≤ 0` ⇒ 内部委托旧 `build_skill_catalog`
    全量路径并返回 `mode="full"`（回退逐位一致由构造保证）；>0 ⇒ Top-K 路径。

    Top-K 路径：
      ① 候选构建（candidate_index，只带轻量字段，不常驻正文）+ 硬约束过滤
         （非 active 剔除；registry 不可用 fail-open 保留全集）；
      ② `rank_skills_scored` 词面分池：≥ `QD_SKILL_MIN_SCORE` 才 eligible，
         无达标 ⇒ 空池（无匹配不强配，§15.2 允许 planner 输出 null）；
      ②b `apply_feedback_rerank`（阶段 5）：final = base ×
         `skill_events.weight_of(skill_id, version)` 轻量重排——新反馈链是
         候选排序唯一反馈来源（§14.3），只调序不过滤，冷启动/失败恒 1.0
         逐位一致；`QD_SKILL_FEEDBACK_RANK=0` 回退纯词面（影子留痕）；
      ③ 常规位 = 分数序前 `K-1`（K≥2）；探索位 = `select_explore` 从落选
         eligible 里挑 TTL 内新发布且未近期曝光者（有界轮换，§14.2）；
      ④ 渲染同旧行格式 + 可见性兜底指针（§14.1：另有 N 个可用技能，经
         `list_skills()` 发现、`read_skill()` 读取；不得编造未展示名称）。

    Args:
        exposure: 曝光状态注入（测试用；None = 进程级全局，加锁）。
        now: 当前时间戳（测试注入；None = time.time()）。

    Returns:
        (段文本, 技能→声明工具映射, 元数据)。元数据供 report 影子对照：
        {mode, topk, total, size, shown, regular, hidden, chars,
         explore:{picked, candidates, exhausted, ttl_days,
                  recent_published_known},
         funnel:{eligible, exposed, eligible_but_not_exposed},
         feedback:{enabled, would_reorder, weights}}。
        `candidates` 为 eligible 全集快照（阶段 4 事件链消费）：每条含
        skill_id/version/base_score/fb_weight/fb_samples/final_score/
        exposed/reasons。
    """
    from .candidate_index import (build_skill_candidates, filter_available,
                                  rank_skills_scored, select_explore,
                                  explore_ttl_days, skill_topk,
                                  apply_feedback_rerank)
    from ..chain import skill_registry as _sr

    k = skill_topk() if topk is None else max(0, int(topk))
    now_ts = time.time() if now is None else float(now)
    if k <= 0:
        sec, tmap = build_skill_catalog(skills)
        return sec, tmap, {"mode": "full", "topk": 0, "size": len(tmap)}

    meta: Dict[str, Any] = {
        "mode": "topk", "topk": k, "total": 0, "size": 0, "shown": [],
        "regular": [], "hidden": 0, "chars": 0,
        "explore": {"picked": "", "candidates": 0, "exhausted": False,
                    "ttl_days": explore_ttl_days(),
                    "recent_published_known": False},
        "funnel": {"eligible": 0, "exposed": 0,
                   "eligible_but_not_exposed": 0},
    }
    if skills is None:
        return "", {}, meta
    try:
        cands = build_skill_candidates(skills)
    except Exception as e:      # 技能系统不可用 ≠ 预选不可用（fail-open）
        logger.debug("[preselect] Top-K 技能候选构建失败(fail-open): %s: %s",
                     type(e).__name__, e)
        return "", {}, meta
    skill_cands = [c for c in filter_available(cands) if c.kind == "skill"]
    meta["total"] = len(skill_cands)
    if not skill_cands:
        return "", {}, meta

    eligible = rank_skills_scored(query, skill_cands)
    meta["funnel"]["eligible"] = len(eligible)
    # ★ 阶段5（2026-10-19）：技能效果反馈软重排（§14.3：新链是技能候选
    #   排序唯一反馈来源）。final = base × skill_events.weight_of(skill_id,
    #   version)；只调序不过滤（eligible 集合/注入门槛/探索位资格不变）；
    #   冷启动/低于门槛/DB 不可用 ⇒ 权重 1.0 ⇒ 与纯词面逐位一致；
    #   QD_SKILL_FEEDBACK_RANK=0 ⇒ 回退纯词面（meta 仍记影子 would_reorder）。
    eligible, fb_meta = apply_feedback_rerank(eligible)
    meta["feedback"] = fb_meta
    explore_slots = 1 if k >= 2 else 0
    regular = eligible[:max(0, k - explore_slots)]
    meta["regular"] = [c.name for c in regular]

    shown = list(regular)
    rest = eligible[len(regular):]
    if explore_slots and rest:
        ttl = explore_ttl_days()
        try:
            recent = _sr.recent_published_skill_ids(ttl)
        except Exception as e:      # 双保险：查询本身已 fail-soft，这里防接口变更
            logger.debug("[preselect] 探索位发布资格查询异常(fail-closed): %s", e)
            recent = None
        pick, exp_meta = select_explore(
            rest, recent_published=recent, now=now_ts, ttl_days=ttl,
            exposure=exposure)
        meta["explore"].update(exp_meta)
        if pick is not None:
            shown.append(pick)

    section, tmap = _render_skill_section(shown, meta["total"])
    meta["shown"] = [c.name for c in shown]
    meta["size"] = len(tmap)
    meta["hidden"] = max(0, meta["total"] - len(shown))
    meta["chars"] = len(section)
    meta["funnel"]["exposed"] = len(shown)
    meta["funnel"]["eligible_but_not_exposed"] = max(0, len(eligible) - len(shown))
    # 候选快照（阶段 4 事件链消费：eligible 全集 + 分数 + 是否曝光 + 理由；
    # 阶段 5 补反馈重排痕迹：基础分/反馈权重/样本数/最终分——排序可按
    # ranker/policy 版本复现，§14.3）。版本经 registry 单一真源
    # （None = 不可用，留空不猜）。
    _shown_ids = {c.id for c in shown}

    def _fb(c) -> dict:
        return (c.rank_metadata or {}).get("feedback") or {}

    meta["candidates"] = [{
        "skill_id": c.id, "name": c.name,
        "version": c.skill_version or "",
        "base_score": round(float(c.base_score), 4),
        "fb_weight": _fb(c).get("weight"),
        "fb_samples": _fb(c).get("samples"),
        "final_score": _fb(c).get("final"),
        "exposed": c.id in _shown_ids,
        "reasons": list((c.rank_metadata or {}).get("reasons") or []),
    } for c in eligible]
    return section, tmap, meta


def preselect_catalog_chars(tool_catalog: str, skill_section: str) -> int:
    """§14.4 单一预选目录字符预算：工具目录 + 技能候选段 + 固定模板骨架。

    【口径】= len(工具目录) + len(技能段) + `_PRESELECT_FIXED_CHARS`
    （PRESELECT_SYSTEM + PRESELECT_PROMPT 空槽 format 长度）。**不包含**
    选中后才加载的技能正文/资源，也不含 query/already/history 等会话变量
    （它们不是目录侧成本，Top-K/预算治理管不到也不该管）。
    与 `schema_chars_of`（工具 schema 专用口径，F14 冻结）分工：后者回答
    "工具面本身多胖"，本函数回答"预选那一轮输入总共多大"；`QD_PRESELECT_TIER_CHARS`
    仍是唯一阈值 env，不新增平行的技能字符阈值。
    """
    return (len(tool_catalog or "") + len(skill_section or "")
            + _PRESELECT_FIXED_CHARS)


def parse_skill_field(parsed, body: str, valid_skills: Sequence[str]) -> str:
    """从 plan JSON 里取技能名（2026-10-17 skill 进预选）。

    与 `parse_selection` 同三条哲学：
      ① 名字必须**逐字来自目录**（valid_skills），编造 ⇒ 丢弃 + debug 留痕；
      ② **显式优先**：给了 skill 字段（含 null）就不再扫正文——模型明说"没有
         匹配"时，不得从它自己写的 goal 里捞名字硬塞（对照 test_v9 的
         `test_parse_respects_explicit_empty_tools`）；
      ③ 字段**整体缺失**才回退正文逐字扫描（模型把技能名写进了 goal 却漏了
         字段 ⇒ 白丢一次召回，这是容错链，不是兜底补丁）。

    Returns:
        技能显示名；无需/不可用/幻觉 一律 "" ⇒ 调用方回落 `_rank_skills`。
    """
    valid = {str(s).strip() for s in (valid_skills or ()) if str(s).strip()}
    if not valid:
        return ""
    if isinstance(parsed, dict) and "skill" in parsed:
        v = parsed.get("skill")
        raw = v.strip() if isinstance(v, str) else ""
        if raw and raw in valid:
            return raw
        if raw:
            logger.debug("[preselect] 技能名 %r 不在目录（幻觉？）⇒ 丢弃，回落关键词选择", raw)
        return ""
    for s in sorted(valid, key=len, reverse=True):
        if re.search(r"(?<![A-Za-z0-9_-])" + re.escape(s) + r"(?![A-Za-z0-9_-])", body or ""):
            return s
    return ""


def build_preselect_messages(
    query: str,
    catalog: str,
    max_tools: int,
    already: Optional[Sequence[str]] = None,
    history_hint: str = "",
    coding_hint: str = "",
    skills: str = "",
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
        coding_hint: 【T6·2026-10-16】编码类工具自 T2 起进入延迟层，目录里有了但
            模型未必认得出用途 ⇒ 给一句"需要读写文件/跑脚本/调试代码时优先选这些"。
            同样**只提示不过滤**，空串时不出现（不改 system、不占 token）。
            ★ 2026-10-21 用户裁定（方案 A）：**参数与拼装能力保留**（设计文档
            §3.3 第 5 步把 coding_hint 登记为 plan 契约，删它=破坏契约+降智），
            但**生产调用方（qd_preselect）已不再传值** ⇒ 预选这一轮不再注入。
            撤回理由（实测 tmp/seq1_payload_dump.txt 第 59-60 行 + 
            tmp/probe_coding_hint_impact2_20261021.py 复刻真实裁剪链路）：
              ① 判据是"全网合法点名集"⇒ cli 模式**每轮必注入**，纯股票任务纯属
                 无关 token；
              ② 它作为**末段**追加在模板最后一行「用户"{query}" →」之后 ⇒ 把
                 续写引导顶断，模型看到的是半句示例 + 无关提示。
            能力损失（已核对，不阻断）：编码类任务目录**本来就含**全部编码工具
            （纯编码 query 实测目录 48 条含 7 个编码工具）⇒ 模型照常点名；
            目录被裁掉编码工具的混合任务，主循环系统提示的"延迟层清单"仍列着
            这些名字，模型可自行 activate_tools 补回。
            回滚方式：qd_preselect 调用处补回 `coding_hint=...` 一行即可。
        skills: 【2026-10-17 skill 进预选】技能目录段（build_skill_catalog 产出），
            排在**工具目录之前**（方法论优先于工具）。空串时模板该行与改造前
            逐字一致 ⇒ 关停路径零痕迹；非空时由调用方保证带尾换行（段后自然
            换行接工具目录标题）。技能契约字段（"skill"）的说明写在段内，
            与段同生共死——关停时不留下"要求输出一个不存在的字段"的死指令。
    """
    already_txt = ", ".join(already) if already else "（无）"
    hist_txt = ("\n历史表现（仅供参考，不是禁令）：\n" + history_hint
                if history_hint else "")
    # 【T6·2026-10-16 最终形态】编码线索**完全在 Python 侧追加**：
    # 模板（PRESELECT_PROMPT）是 v9 逐字对拍契约 ⇒ 不放条件占位（空串会多一个空行）。
    # 空 hint 时**零痕迹**，非空时作为末段追加。
    # ⚠ 末段追加会顶断模板末行「用户"{query}" →」的续写引导（2026-10-21 实测）；
    #   故当前**无生产调用方传值**（见上 coding_hint 说明）。若将来要重新启用，
    #   应改插在「【示例】」之前（工具目录之后），而不是追加在末尾。
    body = PRESELECT_PROMPT.format(
        query=(query or "")[:800], already=already_txt,
        catalog=catalog, max_tools=max_tools, history=hist_txt,
        skills=skills or "")
    if coding_hint:
        body = body + "\n" + coding_hint
    return [
        {"role": "system", "content": PRESELECT_SYSTEM},
        {"role": "user", "content": body},
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

    found_list = False
    if isinstance(parsed, list):
        raw_items = parsed
        found_list = True
    elif isinstance(parsed, dict):
        for key in ("tools", "selected", "names", "result"):
            if isinstance(parsed.get(key), list):
                raw_items = parsed[key]
                found_list = True
                break
    if not raw_items and not found_list:
        # ★ 2026-10-13 P9 补齐容错链最后一环：结构化 JSON 里**根本没有**工具字段
        #   （模型把工具名写进了 goal/deliverables 正文）时，旧实现直接返回空
        #   ⇒ 与 docstring 声明的"目录逐字扫描兜底"不符，白丢一次召回。
        #   反之，模型**显式**给了空列表（`"tools": []`）时必须尊重：那是
        #   "这任务不用工具"的判断，从正文捞名字硬塞就是自作主张（宁可不补）。
        # ★ 2026-10-14 G6：原为裸子串 `n in body` ⇒ 前缀名会误捞：正文只提
        #   `get_fund_flow_daily` 时，`get_fund_flow`（它的前缀且在目录里）一起被捞进。
        #   噪声被推给下游 lint。改成标识符词边界匹配（`(?<![\w])n(?![\w])`）。
        raw_items = [n for n in valid_names if n and re.search(
            r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % re.escape(n), body)]

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


def parse_plan(text: str, valid_names: Sequence[str], max_tools: int,
               *, valid_skills: Sequence[str] = ()) -> dict:
    """解析外置 plan → {"goal", "deliverables", "tools"[, "skill"]}。

    与 `parse_selection` 的区别：本函数**保留 goal/deliverables**，因为 lint 的
    R1 覆盖检查要看"交付承诺了什么信息"（判据来源）。退化路径（模型只回了裸数组）
    仍然可用：此时 goal/deliverables 为空，linter 改用**用户问题**做域识别。

    ★ `valid_skills`（2026-10-17 skill 进预选）：**关键字参数且默认空**——
    不传时返回字典**不含 "skill" 键**，与 v9 基线逐键对拍的调用形态一字不差
    （tests/_p9_preselect_baseline 用 `parse_plan(v, NAMES, 5)` 三参调用）。
    传了才解析技能名，语义见 `parse_skill_field`。
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

    out = {
        "goal": goal,
        "deliverables": deliverables,
        "tools": parse_selection(text, valid_names, max_tools),
        "parsed": isinstance(parsed, dict),
    }
    if valid_skills:
        out["skill"] = parse_skill_field(parsed, body, valid_skills)
    return out


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
            else:
                # ★ 2026-10-14 G2：中间分支（remain < MIN_FACE，但 ratio 未达
                #   R4b 门槛、或 R1 已补位）原先**什么都不做** —— 不裁、不记、不告警。
                #   后果：trace 里 `pruned: []` 会被读成"本轮没发现零相关工具"，
                #   而事实是"发现了但决策被丢弃" ⇒ 观测与行为脱钩，预选质量统计失真。
                #   行为维持**不裁**（保守原则：裁了会低于 MIN_FACE），只把决策落痕。
                rep.warnings.append(
                    "R4 保留 %d 个零相关不裁（remain=%d < MIN_FACE=%d，未达 R4b 作废门槛）：%s"
                    % (len(pruned), remain, MIN_FACE, pruned[:5]))

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

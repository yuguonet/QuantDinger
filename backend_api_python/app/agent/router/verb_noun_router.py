# -*- coding: utf-8 -*-
"""
VerbNoun Router — 动作-对象 两阶段意图路由。

设计思路：
  旧结构：平铺式 Route（finance/stock_analysis, coding/code_modify...），
         "分析"在多个 Route 的 utterance 中重叠，导致误分类。
  新结构：先识别动作（看/分析/修改/创建...），再识别对象（股票/代码/项目...），
         组合得到 domain/intent，消除歧义。

路由流程：
  1. 动作层（verb）：正则/关键词匹配，识别用户要做什么
  2. 对象层（noun）：正则/关键词匹配，识别用户要对什么做
  3. 组合层：verb + noun → domain/intent
  4. 降级：未命中时走语义路由兜底
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class VerbNounResult:
    """路由结果。"""
    domain: str = "chat"
    intent: str = ""
    verb: str = ""
    noun: str = ""
    confidence: float = 0.0
    source: str = ""  # "verb_noun" | "semantic_fallback"
    params: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    elapsed_ms: float = 0.0
    # 语义路由降级时的附加信息
    all_scores: Dict[str, float] = field(default_factory=dict)

    @property
    def matched(self) -> bool:
        return bool(self.domain and self.intent)


# ═══════════════════════════════════════════════════════════════
# 1. 动作定义 (Verb)
# ═══════════════════════════════════════════════════════════════

# 动作类别 → 匹配关键词/正则
# 优先级从上到下，先匹配到的优先
VERB_PATTERNS: List[Tuple[str, List[str]]] = [
    # ── 查看类 ──
    ("view", [
        r"^看(看|一下|下|了)?",
        r"^查看",
        r"^显示",
        r"^展示",
        r"^看看",
        r"^瞅(瞅|一下)?",
        r"^出(个|一下)?",
        r"^给我(看|展示|显示)",
        r"^打开",
        r"^浏览",
    ]),

    # ── 分析类 ──
    ("analyze", [
        r"分析",
        r"诊断",
        r"研判",
        r"评估",
        r"判断",
        r"研究",
        r"解读",
        r"剖析",
        r"梳理",
        r"怎么样",
        r"什么(情况|状态|位置)",
        r"能(买|卖|持有)吗",
        r"(涨|跌)了吗",
        r"走势(如何|怎么样|分析)",
    ]),

    # ── 修改类 ──
    ("modify", [
        r"修改",
        r"修复",
        r"改(一下|改)?",
        r"重构",
        r"优化",
        r"调整",
        r"fix",
        r"bug",
        r"有(问题|bug|错误)",
        r"怎么优化",
    ]),

    # ── 创建类 ──
    ("create", [
        r"创建",
        r"写(一个|个|一下)?",
        r"新建",
        r"生成",
        r"编写",
        r"实现",
        r"帮我写",
        r"帮我创建",
        r"帮我实现",
        r"写个",
    ]),

    # ── 筛选/选股类 ──
    ("filter", [
        r"筛选",
        r"选(几只|几只|几个|股)?",
        r"找(几只|几个|到)?",
        r"推荐",
        r"有没有好的",
        r"帮我选",
        r"帮我找",
        r"帮我筛选",
        r"条件选股",
        r"指标选股",
    ]),

    # ── 回测类 ──
    ("backtest", [
        r"回测",
        r"验证(一下)?",
        r"测试(策略|一下)?",
        r"跑(个|一下)?回测",
        r"历史(绩效|表现|数据)",
        r"收益率",
        r"胜率",
        r"回撤",
        r"夏普比率",
        r"最大回撤",
    ]),

    # ── 执行/交易类 ──
    ("execute", [
        r"启动",
        r"停止",
        r"暂停",
        r"执行",
        r"运行",
        r"买入",
        r"卖出",
        r"下单",
        r"持仓",
        r"交易记录",
        r"账户余额",
        r"策略运行",
    ]),

    # ── 解释类 ──
    ("explain", [
        r"^什么是",
        r"^解释",
        r"^介绍",
        r"^怎么理解",
        r"^什么意思",
        r"^是什么",
        r"^怎么用",
        r"^如何理解",
    ]),

    # ── 查询类（基本面、数据查询）──
    ("query", [
        r"查询",
        r"看看.*数据",
        r"多少",
        r"市值",
        r"市盈率",
        r"PE",
        r"PB",
        r"ROE",
        r"基本面",
        r"公司简介",
        r"行业分类",
    ]),
]


# ═══════════════════════════════════════════════════════════════
# 2. 对象定义 (Noun)
# ═══════════════════════════════════════════════════════════════

# 对象类别 → 匹配关键词/正则
NOUN_PATTERNS: List[Tuple[str, List[str]]] = [
    # ── K线/图表 ──
    ("chart", [
        r"K线",
        r"蜡烛图",
        r"走势图",
        r"图表",
        r"日K",
        r"周K",
        r"分钟K",
        r"分时",
        r"出个图",
        r"画个",
    ]),

    # ── 市场/大盘 ──
    ("market", [
        r"涨停",
        r"跌停",
        r"龙虎榜",
        r"板块",
        r"大盘",
        r"市场",
        r"沪指",
        r"创业板",
        r"连板",
        r"炸板",
        r"破板率",
        r"涨幅榜",
        r"热点",
        r"强势股",
        r"市场情绪",
        r"复盘",
    ]),

    # ── 资金流向 ──
    ("fund_flow", [
        r"资金(流向|流入|流出|动向|面)?",
        r"主力",
        r"北向",
        r"外资",
        r"融资融券",
        r"大单",
        r"板块资金",
    ]),

    # ── 技术指标 ──
    ("indicator", [
        r"MACD",
        r"RSI",
        r"KDJ",
        r"布林带",
        r"均线",
        r"技术指标",
        r"成交量",
        r"量价",
        r"换手率",
        r"金叉",
        r"死叉",
        r"超买",
        r"超卖",
        r"量能",
    ]),

    # ── 策略 ──
    ("strategy", [
        r"策略",
        r"网格",
        r"双均线",
        r"量化",
        r"交易系统",
    ]),

    # ── 项目/代码结构 ──
    ("project", [
        r"项目",
        r"结构",
        r"模块",
        r"目录",
        r"文件(结构|组织|梳理)?",
        r"依赖(关系)?",
        r"架构",
        r"代码组织",
        r"代码结构",
        r"功能",  # "分析XX功能" → 代码功能分析
    ]),

    # ── 代码/脚本 ──
    ("code", [
        r"代码",
        r"脚本",
        r"函数",
        r"方法",
        r"类",
        r"接口",
        r"API",
        r"bug",
        r"错误",
        r"异常",
        r"python",
        r"java(script)?",
        r"\.py",
        r"\.js",
    ]),

    # ── 交易/持仓 ──
    ("trading", [
        r"持仓",
        r"账户",
        r"余额",
        r"交易(记录|历史)",
        r"盈亏",
        r"仓位",
        r"买入(记录|历史)?",
        r"卖出(记录|历史)?",
    ]),

    # ── 个股/股票 ──
    ("stock", [
        r"\d{6}",           # 6位股票代码
        r"股[票市份]",
        r"个股",
        r"自选",
        r"沪深",
        r"A股",
        # 常见股票名称（示例，实际可通过外部词表扩展）
        r"茅台",
        r"比亚迪",
        r"宁德时代",
        r"招商银行",
        r"中芯国际",
        r"平安银行",
        r"沪指",
        r"深成指",
    ]),

    # ── 选股/筛选结果 ──
    ("screener", [
        r"选股",
        r"筛选",
        r"低估值",
        r"蓝筹",
        r"潜力股",
        r"好票",
        r"标的",
    ]),

    # ── 概念/术语（排在 code/project 之后，避免 "这个函数什么意思" 误判）──
    ("concept", [
        r"^什么是",
        r"^怎么理解",
        r"^什么意思",
        r"^概念",
        r"^术语",
        r"^知识",
    ]),
]


# ═══════════════════════════════════════════════════════════════
# 3. 组合映射：verb + noun → domain/intent
# ═══════════════════════════════════════════════════════════════

# (verb, noun) → (domain, intent, tool_categories)
# 未列出的组合走降级逻辑
COMBO_MAP: Dict[Tuple[str, str], Tuple[str, str, List[str]]] = {
    # ── 查看类 ──
    ("view", "chart"):      ("finance", "chart_view",     ["名称查询", "行情数据", "K线图表"]),
    ("view", "market"):     ("finance", "market_scan",    ["行情数据", "龙虎榜/热榜"]),
    ("view", "fund_flow"):  ("finance", "fund_flow",      ["名称查询", "行情数据"]),
    ("view", "indicator"):  ("finance", "indicator",      ["名称查询", "行情数据", "技术分析", "指标策略"]),
    ("view", "strategy"):   ("finance", "trading",        ["交易", "指标策略"]),
    ("view", "stock"):      ("finance", "stock_analysis", ["名称查询", "行情数据", "技术分析", "情报搜索"]),
    ("view", "project"):    ("coding",  "project_scan",   ["工作区"]),
    ("view", "code"):       ("coding",  "project_scan",   ["工作区"]),
    ("view", "screener"):   ("finance", "screener",       ["名称查询", "选股", "指标策略"]),
    ("view", "trading"):    ("finance", "trading",        ["交易", "指标策略"]),

    # ── 分析类 ──
    ("analyze", "stock"):      ("finance", "stock_analysis", ["名称查询", "行情数据", "技术分析", "情报搜索"]),
    ("analyze", "chart"):      ("finance", "chart_view",     ["名称查询", "行情数据", "K线图表"]),
    ("analyze", "market"):     ("finance", "market_scan",    ["行情数据", "龙虎榜/热榜"]),
    ("analyze", "indicator"):  ("finance", "indicator",      ["名称查询", "行情数据", "技术分析", "指标策略"]),
    ("analyze", "fund_flow"):  ("finance", "fund_flow",      ["名称查询", "行情数据"]),
    ("analyze", "strategy"):   ("finance", "backtest",       ["名称查询", "行情数据", "回测", "指标策略"]),
    ("analyze", "project"):    ("coding",  "project_scan",   ["工作区"]),
    ("analyze", "code"):       ("coding",  "code_modify",    ["工作区"]),
    ("analyze", "screener"):   ("finance", "screener",       ["名称查询", "选股", "指标策略"]),
    ("analyze", "trading"):    ("finance", "trading",        ["交易", "指标策略"]),

    # ── 修改类 ──
    ("modify", "code"):      ("coding",  "code_modify",  ["工作区"]),
    ("modify", "project"):   ("coding",  "code_modify",  ["工作区"]),
    ("modify", "strategy"):  ("finance", "trading",      ["交易", "指标策略"]),
    ("modify", "indicator"): ("finance", "indicator",    ["名称查询", "行情数据", "技术分析", "指标策略"]),
    ("modify", "trading"):   ("finance", "trading",      ["交易", "指标策略"]),

    # ── 创建类 ──
    ("create", "code"):      ("coding",  "code_create",  ["工作区"]),
    ("create", "project"):   ("coding",  "code_create",  ["工作区"]),
    ("create", "strategy"):  ("finance", "backtest",     ["名称查询", "行情数据", "回测", "指标策略"]),

    # ── 筛选类 ──
    ("filter", "stock"):     ("finance", "screener",     ["名称查询", "选股", "指标策略"]),
    ("filter", "screener"):  ("finance", "screener",     ["名称查询", "选股", "指标策略"]),
    ("filter", "indicator"): ("finance", "screener",     ["名称查询", "选股", "指标策略"]),

    # ── 回测类 ──
    ("backtest", "strategy"):  ("finance", "backtest", ["名称查询", "行情数据", "回测", "指标策略"]),
    ("backtest", "stock"):     ("finance", "backtest", ["名称查询", "行情数据", "回测", "指标策略"]),
    ("backtest", "indicator"): ("finance", "backtest", ["名称查询", "行情数据", "回测", "指标策略"]),

    # ── 执行/交易类 ──
    ("execute", "strategy"): ("finance", "trading", ["交易", "指标策略"]),
    ("execute", "stock"):    ("finance", "trading", ["交易", "指标策略"]),
    ("execute", "trading"):  ("finance", "trading", ["交易", "指标策略"]),

    # ── 解释类 ──
    ("explain", "indicator"): ("finance", "concept_explain", []),
    ("explain", "concept"):   ("finance", "concept_explain", []),
    ("explain", "stock"):     ("finance", "concept_explain", []),
    ("explain", "strategy"):  ("finance", "concept_explain", []),
    ("explain", "market"):    ("finance", "concept_explain", []),
    ("explain", "fund_flow"): ("finance", "concept_explain", []),

    # ── 查询类 ──
    ("query", "stock"):      ("finance", "stock_info", ["名称查询", "行情数据"]),
    ("query", "indicator"):  ("finance", "indicator",  ["名称查询", "行情数据", "技术分析", "指标策略"]),
    ("query", "trading"):    ("finance", "trading",    ["交易", "指标策略"]),
}

# ═══════════════════════════════════════════════════════════════
# 4. 降级规则：当 verb 或 noun 不完整时
# ═══════════════════════════════════════════════════════════════

# 只有 verb，没有 noun → 根据 verb 推断默认 domain
VERB_ONLY_DEFAULT: Dict[str, Tuple[str, str, List[str]]] = {
    "view":     ("finance", "stock_analysis", ["名称查询", "行情数据", "技术分析", "情报搜索"]),
    "analyze":  ("finance", "stock_analysis", ["名称查询", "行情数据", "技术分析", "情报搜索"]),
    "modify":   ("coding",  "code_modify",    ["工作区"]),
    "create":   ("coding",  "code_create",    ["工作区"]),
    "filter":   ("finance", "screener",       ["名称查询", "选股", "指标策略"]),
    "backtest": ("finance", "backtest",       ["名称查询", "行情数据", "回测", "指标策略"]),
    "execute":  ("finance", "trading",        ["交易", "指标策略"]),
    "explain":  ("finance", "concept_explain", []),
    "query":    ("finance", "stock_info",     ["名称查询", "行情数据"]),
}

# 只有 noun，没有 verb → 根据 noun 推断默认动作
NOUN_ONLY_DEFAULT: Dict[str, Tuple[str, str, List[str]]] = {
    "chart":     ("finance", "chart_view",     ["名称查询", "行情数据", "K线图表"]),
    "market":    ("finance", "market_scan",    ["行情数据", "龙虎榜/热榜"]),
    "fund_flow": ("finance", "fund_flow",      ["名称查询", "行情数据"]),
    "indicator": ("finance", "indicator",      ["名称查询", "行情数据", "技术分析", "指标策略"]),
    "strategy":  ("finance", "trading",        ["交易", "指标策略"]),
    "stock":     ("finance", "stock_analysis", ["名称查询", "行情数据", "技术分析", "情报搜索"]),
    "trading":   ("finance", "trading",        ["交易", "指标策略"]),
    "project":   ("coding",  "project_scan",   ["工作区"]),
    "code":      ("coding",  "code_modify",    ["工作区"]),
    "screener":  ("finance", "screener",       ["名称查询", "选股", "指标策略"]),
    "concept":   ("finance", "concept_explain", []),
}


# ═══════════════════════════════════════════════════════════════
# 5. 核心匹配逻辑
# ═══════════════════════════════════════════════════════════════

# 编译正则（一次性）
_COMPILED_VERBS: List[Tuple[str, List[re.Pattern]]] = [
    (name, [re.compile(p) for p in patterns])
    for name, patterns in VERB_PATTERNS
]

_COMPILED_NOUNS: List[Tuple[str, List[re.Pattern]]] = [
    (name, [re.compile(p, re.IGNORECASE) for p in patterns])
    for name, patterns in NOUN_PATTERNS
]


def _match_verb(message: str) -> Optional[str]:
    """匹配动作类别。"""
    for name, patterns in _COMPILED_VERBS:
        for pat in patterns:
            if pat.search(message):
                return name
    return None


def _match_noun(message: str) -> Optional[str]:
    """匹配对象类别。"""
    for name, patterns in _COMPILED_NOUNS:
        for pat in patterns:
            if pat.search(message):
                return name
    return None


def _extract_stock_code(message: str) -> Optional[str]:
    """提取股票代码。"""
    match = re.search(r'\b(\d{6})\b', message)
    if match:
        return match.group(1)
    return None


def _extract_stock_name(message: str) -> Optional[str]:
    """提取股票名称（通过市场接口验证）。"""
    stopwords = {"帮我", "分析", "查看", "看看", "查询", "怎么样", "什么", "如何",
                 "的", "了", "吗", "吧", "呢", "啊", "一下", "最近", "今天", "昨天",
                 "修改", "修复", "创建", "写", "生成", "筛选", "选择", "回测", "启动",
                 "停止", "看看", "显示", "展示", "项目", "代码", "文件", "目录"}
    match = re.search(r'[\u4e00-\u9fff]{2,6}', message)
    if match:
        candidate = match.group(0)
        if candidate not in stopwords:
            try:
                from app.utils.basicinfo_db import get_stock_basic_db
                matches = get_stock_basic_db().search_stocks(candidate, limit=1)
                if matches:
                    return matches[0].get("symbol")
            except Exception:
                pass
    return None


# ═══════════════════════════════════════════════════════════════
# 6. 主路由器
# ═══════════════════════════════════════════════════════════════

class VerbNounRouter:
    """动作-对象两阶段路由器。

    优先级：
    1. verb + noun 组合匹配（最精确）
    2. 只有 verb（降级到 verb 默认）
    3. 只有 noun（降级到 noun 默认）
    4. 都没命中 → 语义路由兜底

    工具链：
    路由命中后，自动查 tool_chains 配置获取建议执行步骤，
    注入到结果的 metadata 中，agent 会优先按此顺序执行。
    """

    def __init__(self, semantic_router=None, context_boost: float = 0.1):
        self.semantic_router = semantic_router
        self.context_boost = context_boost
        logger.info("[VerbNounRouter] 初始化完成")

    def route(
        self,
        query: str,
        session_id: str = None,
        context_domain: str = None,
    ) -> VerbNounResult:
        """路由用户消息。"""
        if not query or not query.strip():
            return VerbNounResult(domain="chat", intent="empty", confidence=1.0, source="verb_noun")

        t0 = time.time()
        message = query.strip()

        # ── Step 1: 匹配动作 ──
        verb = _match_verb(message)

        # ── Step 2: 匹配对象 ──
        noun = _match_noun(message)

        # ── Step 2.5: 中文股票名数据库兜底 ──
        # _match_noun 只能匹配硬编码的股票名（茅台/比亚迪等）和数字代码，
        # 用户输入"分析宇通客车"时 noun 为 None，导致链路无法触发。
        # 这里用 DB 查询兜底：提取中文词 → search_stocks → 命中则 noun="stock"
        if not noun:
            _stock_stopwords = {
                "帮我", "分析", "查看", "看看", "查询", "怎么样", "什么", "如何",
                "的", "了", "吗", "吧", "呢", "啊", "一下", "最近", "今天", "昨天",
                "修改", "修复", "创建", "写", "生成", "筛选", "选择", "回测", "启动",
                "停止", "显示", "展示", "项目", "代码", "文件", "目录", "评估",
                "判断", "研究", "解读", "情况", "状态", "走势", "趋势", "行情",
                "策略", "指标", "信号", "买入", "卖出", "持有", "观望", "建议",
                "推荐", "潜力", "风险", "机会", "分析一下", "怎么样",
            }
            _cn_match = re.search(r'[\u4e00-\u9fff]{2,8}', message)
            if _cn_match:
                _candidate = _cn_match.group(0)
                if _candidate not in _stock_stopwords:
                    try:
                        from app.utils.basicinfo_db import get_stock_basic_db
                        _matches = get_stock_basic_db().search_stocks(_candidate, limit=1)
                        if _matches:
                            noun = "stock"
                            logger.info("[VerbNoun] 中文名 '%s' → noun=stock (code=%s)",
                                        _candidate, _matches[0].get("symbol", ""))
                    except Exception:
                        pass

        # ── Step 3: 组合 ──
        result = None

        if verb and noun:
            combo = COMBO_MAP.get((verb, noun))
            if combo:
                domain, intent, tool_cats = combo
                tool_chain = _get_tool_chain(verb, noun)
                result = VerbNounResult(
                    domain=domain, intent=intent,
                    verb=verb, noun=noun,
                    confidence=0.95,
                    source="verb_noun",
                    metadata={"tool_categories": tool_cats, "tool_chain": tool_chain},
                )
            else:
                # 组合未定义，降级到 verb 默认
                default = VERB_ONLY_DEFAULT.get(verb)
                if default:
                    domain, intent, tool_cats = default
                    result = VerbNounResult(
                        domain=domain, intent=intent,
                        verb=verb, noun=noun,
                        confidence=0.75,
                        source="verb_noun_fallback",
                        metadata={"tool_categories": tool_cats},
                    )

        elif verb and not noun:
            default = VERB_ONLY_DEFAULT.get(verb)
            if default:
                domain, intent, tool_cats = default
                result = VerbNounResult(
                    domain=domain, intent=intent,
                    verb=verb, noun="",
                    confidence=0.70,
                    source="verb_only",
                    metadata={"tool_categories": tool_cats},
                )

        elif noun and not verb:
            default = NOUN_ONLY_DEFAULT.get(noun)
            if default:
                domain, intent, tool_cats = default
                result = VerbNounResult(
                    domain=domain, intent=intent,
                    verb="", noun=noun,
                    confidence=0.70,
                    source="noun_only",
                    metadata={"tool_categories": tool_cats},
                )

        # ── Step 4: 提取参数 ──
        if result:
            stock_code = _extract_stock_code(message)
            if stock_code:
                result.params["stock"] = stock_code
            else:
                stock_name = _extract_stock_name(message)
                if stock_name:
                    result.params["stock"] = stock_name

            # 上下文加成
            if context_domain and result.domain == context_domain:
                result.confidence = min(result.confidence + self.context_boost, 1.0)

            result.elapsed_ms = (time.time() - t0) * 1000
            logger.info(
                "[VerbNoun] 命中: verb=%s noun=%s → %s/%s (%.2f) %.0fms | %s",
                verb, noun, result.domain, result.intent,
                result.confidence, result.elapsed_ms, message[:50],
            )
            return result

        # ── Step 5: 语义路由兜底 ──
        if self.semantic_router:
            semantic_result = self.semantic_router.route(
                query=message,
                session_id=session_id,
                context_domain=context_domain,
            )
            if semantic_result.matched:
                tool_cats = semantic_result.metadata.get("tool_categories", [])
                result = VerbNounResult(
                    domain=semantic_result.domain,
                    intent=semantic_result.intent,
                    verb="", noun="",
                    confidence=semantic_result.confidence * 0.9,  # 降级折扣
                    source="semantic_fallback",
                    params=_extract_params_from_message(message),
                    metadata=semantic_result.metadata,
                    all_scores=semantic_result.all_scores,
                )
                result.elapsed_ms = (time.time() - t0) * 1000
                logger.info(
                    "[VerbNoun] 语义降级: %s/%s (%.2f) %.0fms | %s",
                    result.domain, result.intent,
                    result.confidence, result.elapsed_ms, message[:50],
                )
                return result

        # ── 完全未命中 ──
        result = VerbNounResult(
            domain="chat", intent="unmatched",
            confidence=0.0, source="none",
            params=_extract_params_from_message(message),
        )
        result.elapsed_ms = (time.time() - t0) * 1000
        logger.info("[VerbNoun] 未命中: %.0fms | %s", result.elapsed_ms, message[:50])
        return result


def _extract_params_from_message(message: str) -> Dict[str, Any]:
    """从消息中提取参数。"""
    params = {}
    code_match = re.search(r'\b(\d{6})\b', message)
    if code_match:
        params["stock"] = code_match.group(1)
    return params


def _get_tool_chain(verb: str, noun: str) -> List[Dict[str, str]]:
    """从 tool_chains 配置获取工具链。"""
    try:
        from app.agent.router.tool_chains import get_tool_chain
        return get_tool_chain(verb, noun)
    except Exception:
        return []

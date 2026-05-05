"""
规则引擎评分模块 — rule_engine.py

将关键词评分从扁平字典升级为声明式规则引擎。

设计原则:
  - 规则即数据: 每条规则独立声明, 可按类别/市场/场景组合
  - 支持正则: 不只是子串匹配, 支持 regex 模式
  - 上下文感知: 标题权重 > 摘要权重 (默认 2:1)
  - 多维信号: 利好/利空/否决 三通道独立评估
  - 可扩展: 新增规则只需往列表追加, 不改逻辑

规则结构:
  Rule(
      pattern: str          # 匹配模式 (正则, 自动编译)
      score: float          # 分值 (正=利好, 负=利空)
      category: str         # 分类标签 (policy/stock/market/event)
      label: str            # 可读名称
      weight: float = 1.0   # 权重乘数 (默认1.0)
      veto: bool = False    # 是否一票否决
      title_only: bool = False  # 仅匹配标题
      require_context: str = "" # 上下文前置条件 (正则, 需在同一文本中同时命中)
  )

评分流程:
  1. 遍历所有规则, 对标题和摘要分别匹配
  2. 标题命中的 score × title_weight, 摘要命中的 score × snippet_weight
  3. 同一规则标题+摘要都命中只计一次 (取较高权重)
  4. veto 规则命中直接返回 -999
  5. 利好/利空通道各自聚合, 按 category 去重取最强信号
  6. 最终得分 = 利好总分 - 利空总分, 裁剪到 [-10, +10]
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple


# ═══════════════════════════════════════════════════════════════
# 规则数据结构
# ═══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class Rule:
    """单条评分规则"""
    pattern: str              # 正则表达式
    score: float              # 分值
    category: str             # 分类
    label: str = ""           # 可读标签
    weight: float = 1.0       # 权重乘数
    veto: bool = False        # 一票否决
    title_only: bool = False  # 仅匹配标题
    require_context: str = "" # 上下文前置条件 (正则)
    _compiled: re.Pattern = field(default=None, repr=False, compare=False)
    _ctx_compiled: re.Pattern = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        # frozen=True 下需要 object.__setattr__
        object.__setattr__(self, '_compiled', re.compile(self.pattern, re.IGNORECASE))
        if self.require_context:
            object.__setattr__(self, '_ctx_compiled',
                               re.compile(self.require_context, re.IGNORECASE))


# ═══════════════════════════════════════════════════════════════
# 规则库 — 按类别组织
# ═══════════════════════════════════════════════════════════════

# ── 一票否决规则 (最高优先级, 命中即终止) ──
VETO_RULES: List[Rule] = [
    # 财务造假 / 违规
    Rule(r"财务造假|虚假陈述|虚增(?:收入|利润)", -999, "event", "财务造假", veto=True),
    Rule(r"立案调查|监管调查|证监会.*调查", -999, "event", "监管调查", veto=True),
    Rule(r"退市|暂停上市|终止上市", -999, "event", "退市", veto=True),
    Rule(r"破产|清算|资不抵债", -999, "event", "破产", veto=True),
    # 重大事故
    Rule(r"重大事故|安全事故|质量事故", -999, "event", "重大事故", veto=True),
    # 极端行情
    Rule(r"连续跌停|[2345]连板跌停", -999, "market", "连续跌停", veto=True),
    Rule(r"天地板", -999, "market", "天地板", veto=True),
    Rule(r"闪崩", -999, "market", "闪崩", veto=True),
    # 重大利空
    Rule(r"资金链断裂", -999, "event", "资金链断裂", veto=True),
    Rule(r"债务违约|债务危机", -999, "event", "债务危机", veto=True),
    Rule(r"黑天鹅", -999, "event", "黑天鹅", veto=True),
]

# ── 政策/宏观利好 ──
POLICY_BULLISH: List[Rule] = [
    Rule(r"降准", 8.0, "policy", "降准"),
    Rule(r"降息", 8.0, "policy", "降息"),
    Rule(r"LPR.*下调|下调.*LPR", 8.0, "policy", "LPR下调"),
    Rule(r"MLF.*投放|投放.*MLF", 7.0, "policy", "MLF投放"),
    Rule(r"逆回购", 6.5, "policy", "逆回购"),
    Rule(r"货币(?:政策)?(?:宽松|放松|转向宽松)", 7.5, "policy", "货币宽松"),
    Rule(r"财政(?:刺激|发力|扩张)", 7.5, "policy", "财政刺激"),
    Rule(r"补贴|扶持", 7.0, "policy", "补贴/扶持"),
    Rule(r"减税|免税|降税", 7.0, "policy", "减税"),
    Rule(r"政策利好|重大利好", 8.0, "policy", "政策利好"),
    Rule(r"政策支持|政策扶持", 7.0, "policy", "政策支持"),
    Rule(r"产业政策", 7.0, "policy", "产业政策"),
    Rule(r"定向降准", 8.5, "policy", "定向降准"),
    Rule(r"降准降息", 9.0, "policy", "降准降息"),
    Rule(r"国务院.*(?:意见|方案|规划)", 7.0, "policy", "国务院文件"),
    Rule(r"(?:中共中央|国务院).*(?:印发|发布|出台)", 7.5, "policy", "高层发文"),
]

# ── 政策/宏观利空 (score 为负数) ──
POLICY_BEARISH: List[Rule] = [
    Rule(r"调控", -3.0, "policy", "调控"),
    Rule(r"监管(?:加强|趋严|收紧)", -3.5, "policy", "监管趋严"),
    Rule(r"收紧", -2.5, "policy", "收紧"),
    Rule(r"加息", -2.0, "policy", "加息"),
    Rule(r"制裁", -2.0, "policy", "制裁"),
    Rule(r"限制.*(?:融资|信贷|贷款)", -2.5, "policy", "限制融资"),
    Rule(r"整顿", -3.0, "policy", "整顿"),
    Rule(r"去杠杆", -2.5, "policy", "去杠杆"),
    Rule(r"提高.*(?:准备金|准备金率)", -2.0, "policy", "提准"),
    Rule(r"关税.*(?:加征|提高|升级)", -2.0, "policy", "加征关税"),
    Rule(r"贸易.*(?:摩擦|战|争端)", -2.5, "policy", "贸易摩擦"),
]

# ── 行业利好 ──
INDUSTRY_BULLISH: List[Rule] = [
    Rule(r"新基建", 7.5, "industry", "新基建"),
    Rule(r"新能源", 7.0, "industry", "新能源"),
    Rule(r"(?:AI|人工智能)", 7.0, "industry", "AI"),
    Rule(r"芯片|半导体", 6.5, "industry", "芯片"),
    Rule(r"碳中和|碳达峰", 6.5, "industry", "碳中和"),
    Rule(r"RCEP", 6.5, "industry", "RCEP"),
    Rule(r"一带一路", 6.5, "industry", "一带一路"),
    Rule(r"共同富裕", 6.0, "industry", "共同富裕"),
    Rule(r"数字经济", 6.5, "industry", "数字经济"),
    Rule(r"国产替代|自主可控", 7.0, "industry", "国产替代"),
    Rule(r"大模型|LLM|ChatGPT", 7.0, "industry", "大模型"),
    Rule(r"低空经济", 7.0, "industry", "低空经济"),
    Rule(r"人形机器人", 6.5, "industry", "人形机器人"),
]

# ── 个股利好 ──
STOCK_BULLISH: List[Rule] = [
    # 行情类
    Rule(r"涨停", 8.0, "stock", "涨停"),
    Rule(r"封板", 8.0, "stock", "封板"),
    Rule(r"连板", 8.5, "stock", "连板"),
    Rule(r"一字涨停", 9.0, "stock", "一字涨停"),
    Rule(r"大涨|暴涨|飙升", 7.5, "stock", "大涨"),
    Rule(r"创新高|历史新高", 7.0, "stock", "创新高"),
    Rule(r"放量上涨", 7.0, "stock", "放量上涨"),
    Rule(r"突破.*(?:压力|平台|箱体)", 6.5, "stock", "突破"),
    Rule(r"金叉", 6.0, "stock", "金叉"),
    Rule(r"牛市", 7.0, "market", "牛市"),
    Rule(r"反弹|回升", 6.0, "stock", "反弹"),
    Rule(r"领涨", 6.5, "stock", "领涨"),
    # 业绩类
    Rule(r"业绩增长|利润增长|营收增长", 7.0, "stock", "业绩增长"),
    Rule(r"超预期|大超预期", 8.0, "stock", "超预期"),
    Rule(r"翻倍|倍增", 8.0, "stock", "翻倍"),
    Rule(r"高增长", 7.0, "stock", "高增长"),
    Rule(r"扭亏", 7.0, "stock", "扭亏"),
    Rule(r"净利润.*(?:大增|暴增|增长\d)", 7.5, "stock", "净利润大增"),
    Rule(r"营收.*(?:大增|突破|创新高)", 7.0, "stock", "营收大增"),
    # 资本运作
    Rule(r"重大合同|中标", 7.0, "stock", "重大合同"),
    Rule(r"战略合作", 6.5, "stock", "战略合作"),
    Rule(r"增持", 7.0, "stock", "增持"),
    Rule(r"回购", 7.0, "stock", "回购"),
    Rule(r"大股东增持", 7.5, "stock", "大股东增持"),
    Rule(r"分红.*(?:提高|增加|派息)", 6.5, "stock", "分红增加"),
    # 综合
    Rule(r"利好", 6.5, "stock", "利好"),
    Rule(r"重大利好", 8.0, "stock", "重大利好"),
]

# ── 个股利空 (score 为负数) ──
STOCK_BEARISH: List[Rule] = [
    # 行情类
    Rule(r"跌停", -2.0, "stock", "跌停"),
    Rule(r"一字跌停", -1.0, "stock", "一字跌停"),
    Rule(r"大跌|暴跌", -2.0, "stock", "大跌"),
    Rule(r"破位", -3.0, "stock", "破位"),
    Rule(r"新低|历史新低", -2.0, "stock", "新低"),
    Rule(r"熊市", -2.0, "market", "熊市"),
    Rule(r"死叉", -3.5, "stock", "死叉"),
    Rule(r"缩量下跌", -3.0, "stock", "缩量下跌"),
    # 业绩类
    Rule(r"净利.*(?:大跌|下滑|下降|亏损)", -2.0, "stock", "净利下滑"),
    Rule(r"业绩暴雷|暴雷", -1.5, "stock", "暴雷"),
    Rule(r"业绩变脸", -2.0, "stock", "业绩变脸"),
    Rule(r"巨亏|大幅亏损", -1.0, "stock", "巨亏"),
    Rule(r"由盈转亏", -1.5, "stock", "由盈转亏"),
    Rule(r"商誉减值", -1.5, "stock", "商誉减值"),
    Rule(r"营收.*(?:下滑|萎缩|腰斩)", -2.5, "stock", "营收下滑"),
    # 资本运作
    Rule(r"减持", -3.0, "stock", "减持"),
    Rule(r"清仓(?:式)?减持", -1.5, "stock", "清仓减持"),
    Rule(r"大股东减持", -1.5, "stock", "大股东减持"),
    Rule(r"违规减持", -1.5, "stock", "违规减持"),
    Rule(r"定增.*(?:终止|撤回)", -3.0, "stock", "定增终止"),
    # 综合
    Rule(r"利空", -3.0, "stock", "利空"),
    Rule(r"重大利空", -1.5, "stock", "重大利空"),
]


# ═══════════════════════════════════════════════════════════════
# 规则集组装 — 按场景组合
# ═══════════════════════════════════════════════════════════════

# 默认规则集 (全量)
_ALL_BULLISH = POLICY_BULLISH + INDUSTRY_BULLISH + STOCK_BULLISH
_ALL_BEARISH = POLICY_BEARISH + STOCK_BEARISH


def get_ruleset(news_type: str = "stock") -> Tuple[List[Rule], List[Rule], List[Rule]]:
    """
    根据新闻类型返回对应的规则集

    Args:
        news_type: "policy" / "stock" / "market" / "general"

    Returns:
        (veto_rules, bullish_rules, bearish_rules)
    """
    if news_type == "policy":
        return VETO_RULES, POLICY_BULLISH, POLICY_BEARISH
    elif news_type == "market":
        return VETO_RULES, STOCK_BULLISH + INDUSTRY_BULLISH, STOCK_BEARISH
    else:
        return VETO_RULES, _ALL_BULLISH, _ALL_BEARISH


# ═══════════════════════════════════════════════════════════════
# 规则引擎评分器
# ═══════════════════════════════════════════════════════════════

# 标题/摘要权重比
_TITLE_WEIGHT = 2.0
_SNIPPET_WEIGHT = 1.0


@dataclass
class RuleMatch:
    """单次规则命中记录"""
    rule: Rule
    source: str       # "title" / "snippet"
    weight: float     # 实际权重 (score × source_weight × rule.weight)


def _match_rules(
    text: str,
    rules: List[Rule],
    source: str,
    source_weight: float,
) -> List[RuleMatch]:
    """对一段文本执行规则匹配, 返回命中列表"""
    matches = []
    for rule in rules:
        if rule.title_only and source != "title":
            continue
        m = rule._compiled.search(text)
        if m:
            # 上下文前置条件检查
            if rule._ctx_compiled:
                if not rule._ctx_compiled.search(text):
                    continue
            matches.append(RuleMatch(
                rule=rule,
                source=source,
                weight=rule.score * source_weight * rule.weight,
            ))
    return matches


def _dedupe_by_category(matches: List[RuleMatch], positive: bool = True) -> List[RuleMatch]:
    """
    按 category 去重: 同一 category 只保留最强信号

    正分: 取 score 最高的; 负分: 取 score 最低的 (绝对值最大)
    """
    best: Dict[str, RuleMatch] = {}
    for m in matches:
        cat = m.rule.category
        if cat not in best:
            best[cat] = m
        else:
            if positive:
                if abs(m.weight) > abs(best[cat].weight):
                    best[cat] = m
            else:
                if abs(m.weight) > abs(best[cat].weight):
                    best[cat] = m
    return list(best.values())


def rule_engine_score(
    title: str,
    snippet: str = "",
    news_type: str = "stock",
) -> Dict[str, Any]:
    """
    规则引擎评分 — 替代原 keyword_score_article()

    流程:
      1. 获取规则集 (按 news_type 筛选)
      2. 标题匹配 (权重 ×2) + 摘要匹配 (权重 ×1)
      3. 同一规则标题+摘要都命中只计一次 (取较高权重)
      4. veto 命中直接返回 -999
      5. 利好/利空通道各自按 category 去重取最强信号
      6. 求和裁剪到 [-10, +10]

    Args:
        title: 文章标题
        snippet: 文章摘要
        news_type: "policy" / "stock" / "market" / "general"

    Returns:
        {
            "score": 7.5,
            "sentiment": "positive",
            "veto": False,
            "veto_keyword": None,
            "bullish_hits": {"涨停": 8.0, ...},
            "bearish_hits": {},
            "keywords": ["涨停", ...],
            "match_details": [...],  # 调试用: 所有命中详情
        }
    """
    veto_rules, bullish_rules, bearish_rules = get_ruleset(news_type)

    # ── 标题 + 摘要匹配 ──
    title = title or ""
    snippet = snippet or ""

    all_matches: List[RuleMatch] = []
    all_matches.extend(_match_rules(title, veto_rules, "title", _TITLE_WEIGHT))
    all_matches.extend(_match_rules(title, bullish_rules, "title", _TITLE_WEIGHT))
    all_matches.extend(_match_rules(title, bearish_rules, "title", _TITLE_WEIGHT))
    all_matches.extend(_match_rules(snippet, veto_rules, "snippet", _SNIPPET_WEIGHT))
    all_matches.extend(_match_rules(snippet, bullish_rules, "snippet", _SNIPPET_WEIGHT))
    all_matches.extend(_match_rules(snippet, bearish_rules, "snippet", _SNIPPET_WEIGHT))

    # ── 一票否决检测 ──
    for m in all_matches:
        if m.rule.veto:
            return {
                "score": -999.0,
                "sentiment": "negative",
                "veto": True,
                "veto_keyword": m.rule.label or m.rule.pattern,
                "bullish_hits": {},
                "bearish_hits": {m.rule.label or m.rule.pattern: m.weight},
                "keywords": [m.rule.label or m.rule.pattern],
                "match_details": [m.rule.label for m in all_matches if m.rule.veto],
            }

    # ── 去重: 同一规则只取最高权重 (标题 > 摘要) ──
    seen_rules: Set[str] = set()
    deduped: List[RuleMatch] = []
    for m in sorted(all_matches, key=lambda x: abs(x.weight), reverse=True):
        rule_id = m.rule.pattern
        if rule_id not in seen_rules:
            seen_rules.add(rule_id)
            deduped.append(m)

    # ── 分通道 ──
    bullish_matches = [m for m in deduped if m.weight > 0]
    bearish_matches = [m for m in deduped if m.weight < 0]

    # ── 按 category 去重取最强信号 ──
    bullish_matches = _dedupe_by_category(bullish_matches, positive=True)
    bearish_matches = _dedupe_by_category(bearish_matches, positive=False)

    # ── 汇总 ──
    bullish_total = sum(m.weight for m in bullish_matches)
    bearish_total = sum(m.weight for m in bearish_matches)

    raw_score = bullish_total + bearish_total  # bearish_total 已是负数
    score = round(max(-10.0, min(10.0, raw_score)), 1)

    # 情感标签
    if score > 1.0:
        sentiment = "positive"
    elif score < -1.0:
        sentiment = "negative"
    else:
        sentiment = "neutral"

    # 命中详情
    bullish_hits = {m.rule.label or m.rule.pattern: round(abs(m.weight), 1) for m in bullish_matches}
    bearish_hits = {m.rule.label or m.rule.pattern: round(abs(m.weight), 1) for m in bearish_matches}
    keywords = list(set(
        [m.rule.label or m.rule.pattern for m in bullish_matches] +
        [m.rule.label or m.rule.pattern for m in bearish_matches]
    ))

    return {
        "score": score,
        "sentiment": sentiment,
        "veto": False,
        "veto_keyword": None,
        "bullish_hits": bullish_hits,
        "bearish_hits": bearish_hits,
        "keywords": keywords,
        "match_details": [m.rule.label for m in bullish_matches + bearish_matches],
    }

"""
新闻分析评分引擎 — news_analysis.py

职责:
  1. keyword_score_article()  → 单篇关键词评分 (-10 ~ +10, 纯算法, 无外部依赖)
  2. ai_analyze_article()     → 单篇 AI 分析评分 (LLM, 允许改写/降级/加关键词)
  3. composite_score()        → 多篇综合评分 (RMS 聚合 + 非对称时间衰减)

调用方:
  - news.py NewsCacheManager.calc_score() → composite_score()
  - news_processor.py → 可被本模块替代
  - 上层路由/服务 → 直接调用单篇评分

设计原则:
  - 单篇评分: 每篇文章独立打分, 范围 -10 ~ +10, 0 = 中性, -999 = 一票否决
  - AI 分析: 允许 LLM 改写文章内容, 增加关键词, 降级为通俗易懂版本
  - 综合评分: RMS 聚合 (强信号不被弱/中性稀释), 非对称时间衰减
    好消息 10 天衰减至 0, 坏消息 15 天衰减至 0
"""
import json
import math
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.utils.logger import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════
#  1. 关键词评分 (单篇, 0-10, 无需 LLM)
# ═══════════════════════════════════════════════════════════════

# ── 利好关键词 → 分值 (越高越利好) ──
_BULLISH_KEYWORDS: Dict[str, float] = {
    # 政策类
    "降准": 8.0, "降息": 8.0, "LPR下调": 8.0, "MLF投放": 7.0,
    "逆回购": 6.5, "货币宽松": 7.5, "财政刺激": 7.5,
    "补贴": 7.0, "扶持": 7.0, "减税": 7.0, "免税": 7.0,
    "政策利好": 8.0, "政策支持": 7.0, "产业政策": 7.0,
    # 行业类
    "新基建": 7.5, "新能源": 7.0, "AI": 7.0, "人工智能": 7.0,
    "芯片": 6.5, "半导体": 6.5, "碳中和": 6.5,
    "RCEP": 6.5, "一带一路": 6.5, "共同富裕": 6.0,
    # 个股类
    "涨停": 8.0, "封板": 8.0, "连板": 8.5, "一字涨停": 9.0,
    "大涨": 7.0, "暴涨": 8.0, "飙升": 7.5, "创新高": 7.0,
    "业绩增长": 7.0, "超预期": 8.0, "大超预期": 8.5,
    "翻倍": 8.0, "高增长": 7.0, "扭亏": 7.0,
    "重大合同": 7.5, "中标": 7.0, "战略合作": 6.5,
    "增持": 7.0, "回购": 7.0, "大股东增持": 7.5,
    "利好": 6.5, "重大利好": 8.0,
    # 市场类
    "突破": 6.5, "放量上涨": 7.0, "领涨": 6.5, "反弹": 6.0,
    "金叉": 6.0, "牛市": 7.0,
}

# ── 利空关键词 → 分值 (越低越利空) ──
_BEARISH_KEYWORDS: Dict[str, float] = {
    # 政策类
    "调控": 3.0, "监管": 3.5, "收紧": 2.5, "加息": 2.0,
    "制裁": 2.0, "限制": 3.0, "整顿": 3.0, "去杠杆": 2.5,
    # 个股类
    "跌停": 2.0, "一字跌停": 1.0, "连续跌停": 0.5, "天地板": 1.0,
    "闪崩": 1.5, "崩盘": 1.0, "大跌": 2.5, "暴跌": 1.5,
    "净利大跌": 2.0, "业绩暴雷": 1.5, "暴雷": 1.5, "业绩变脸": 2.0,
    "巨亏": 1.0, "大幅亏损": 1.0, "由盈转亏": 1.5,
    "财务造假": 0.5, "立案调查": 1.0, "退市": 0.5,
    "破产": 0.5, "清算": 0.5, "债务危机": 1.0,
    "减持": 3.0, "清仓": 1.5, "大股东减持": 1.5,
    "利空": 3.0, "重大利空": 1.5, "黑天鹅": 1.0,
    # 市场类
    "破位": 3.0, "新低": 2.5, "历史新低": 1.5,
    "熊市": 2.0, "资金链断裂": 1.0,
}

# ── 一票否决关键词 (命中任一 → score = -999) ──
_VETO_KEYWORDS = {
    "跌停", "一字跌停", "连续跌停", "天地板",
    "闪崩", "崩盘", "历史新低", "净利大跌",
    "业绩暴雷", "暴雷", "业绩变脸",
    "巨亏", "大幅亏损", "由盈转亏",
    "商誉减值", "财务造假", "清仓",
    "大股东减持", "违规减持",
    "破产", "清算", "质量事故", "安全事故",
    "监管调查", "立案调查", "违法",
    "退市", "暂停上市",
    "债务危机", "债务违约", "资金链断裂",
    "巨额索赔", "重大利空", "黑天鹅",
}


def keyword_score_article(title: str, snippet: str = "") -> Dict[str, Any]:
    """
    单篇关键词评分 (纯算法, -10 ~ +10)

    评分逻辑:
      1. 一票否决: 命中 veto 关键词 → score = -999
      2. 分别匹配利好/利空关键词, 取各自最高分
      3. 利好分 > 利空分 → 正分区间, 反之亦然
      4. 无命中 → 中性 0

    Args:
        title: 文章标题
        snippet: 文章摘要/正文片段 (可选)

    Returns:
        {
            "score": 7.5,              # 评分 (-10 ~ +10, 一票否决时为 -999)
            "sentiment": "positive",    # positive / negative / neutral
            "keywords": ["降准", "央行"],  # 命中的关键词
            "veto": False,             # 是否触发一票否决
            "veto_keyword": None,      # 一票否决的关键词
            "bullish_hits": {"降准": 8.0},  # 命中的利好关键词及分值
            "bearish_hits": {},        # 命中的利空关键词及分值
        }
    """
    text = f"{title} {snippet}"

    # ── 一票否决 ──
    for kw in _VETO_KEYWORDS:
        if kw in text:
            return {
                "score": -999.0,
                "sentiment": "negative",
                "keywords": [kw],
                "veto": True,
                "veto_keyword": kw,
                "bullish_hits": {},
                "bearish_hits": {kw: 0.0},
            }

    # ── 匹配利好/利空 ──
    bullish_hits: Dict[str, float] = {}
    bearish_hits: Dict[str, float] = {}

    for kw, score in _BULLISH_KEYWORDS.items():
        if kw in text:
            bullish_hits[kw] = score

    for kw, score in _BEARISH_KEYWORDS.items():
        if kw in text:
            bearish_hits[kw] = score

    all_keywords = list(set(list(bullish_hits.keys()) + list(bearish_hits.keys())))

    # ── 无命中 → 中性 ──
    if not bullish_hits and not bearish_hits:
        return {
            "score": 0.0,
            "sentiment": "neutral",
            "keywords": [],
            "veto": False,
            "veto_keyword": None,
            "bullish_hits": {},
            "bearish_hits": {},
        }

    # ── 计算综合分 (-10 ~ +10) ──
    best_bull = max(bullish_hits.values()) if bullish_hits else 0.0
    best_bear = min(bearish_hits.values()) if bearish_hits else 10.0

    bull_count = len(bullish_hits)
    bear_count = len(bearish_hits)

    # 将原始 0-10 分映射到 -10 ~ +10:
    #   原始 10 → +10, 原始 0 → -10, 原始 5 → 0
    if bull_count > bear_count:
        # 偏利好: 以最高利好分为基础, 利空越多越往下拉
        base = best_bull
        penalty = bear_count * 0.5
        raw_score = max(5.0, base - penalty)
        # 映射: raw_score ∈ [5,10] → score ∈ [0, +10]
        score = (raw_score - 5.0) * 2.0
    elif bear_count > bull_count:
        # 偏利空: 以最高利空分为基础, 利好越多越往上抬
        base = best_bear
        bonus = bull_count * 0.5
        raw_score = min(5.0, base + bonus)
        # 映射: raw_score ∈ [0,5] → score ∈ [-10, 0]
        score = (raw_score - 5.0) * 2.0
    else:
        # 数量相当: 取两者的中点, 映射到 -10 ~ +10
        raw_mid = (best_bull + best_bear) / 2
        score = (raw_mid - 5.0) * 2.0

    score = round(max(-10.0, min(10.0, score)), 1)

    # 情感标签
    if score > 1.0:
        sentiment = "positive"
    elif score < -1.0:
        sentiment = "negative"
    else:
        sentiment = "neutral"

    return {
        "score": score,
        "sentiment": sentiment,
        "keywords": all_keywords,
        "veto": False,
        "veto_keyword": None,
        "bullish_hits": bullish_hits,
        "bearish_hits": bearish_hits,
    }


# ═══════════════════════════════════════════════════════════════
#  2. AI 分析评分 (单篇, LLM, 允许改写/降级/加关键词)
# ═══════════════════════════════════════════════════════════════

_ARTICLE_ANALYSIS_SYSTEM_PROMPT = """你是一位资深金融分析师兼科普作者。你的任务是分析一篇财经新闻，完成以下工作：

1. **评分**：根据文章内容对投资者的影响，给出 -10 到 +10 的评分
   - -10 ~ -1: 利空（负面消息，对投资者不利，越低越严重）
   - 0: 中性（信息性内容，无明显方向）
   - +1 ~ +10: 利好（正面消息，对投资者有利，越高越利好）

2. **提取关键词**：从文章中提取所有金融/政策/行业关键词

3. **改写降级**：将专业术语改写为通俗易懂的版本，让普通投资者也能看懂
   - 保留核心信息不变
   - 用日常用语替代专业术语
   - 必要时增加简短解释

4. **补充关键词**：在不改变原意的前提下，补充文章隐含但未明说的关键词

输出严格 JSON：
{
  "score": 7.5,
  "sentiment": "positive",
  "keywords": ["关键词1", "关键词2"],
  "original_summary": "原文摘要（50字内）",
  "simplified_text": "改写后的通俗版本（200字内）",
  "added_keywords": ["补充的关键词1"],
  "impact_level": "high",
  "reasoning": "评分理由（30字内）"
}"""


def ai_analyze_article(
    title: str,
    snippet: str = "",
    source: str = "",
    published_date: str = "",
    timeout: float = 30.0,
) -> Optional[Dict[str, Any]]:
    """
    用 LLM 对单篇文章做深度分析评分

    特性:
      - 评分 -10 ~ +10 (0 = 中性, 正 = 利好, 负 = 利空)
      - 允许 LLM 改写文章为通俗版本 (simplified_text)
      - 允许补充关键词 (added_keywords)
      - 返回结构化 JSON

    Args:
        title: 文章标题
        snippet: 文章摘要/正文
        source: 来源 (如 "东方财富")
        published_date: 发布时间
        timeout: LLM 调用超时秒数

    Returns:
        {
            "score": 7.5,
            "sentiment": "positive",
            "keywords": [...],
            "original_summary": "...",
            "simplified_text": "通俗易懂版本",
            "added_keywords": [...],
            "impact_level": "high/medium/low",
            "reasoning": "...",
            "llm_provider": "deepseek",
            "analysis_time": 2.3,
        }
        或 None (LLM 不可用时)
    """
    if not title:
        return None

    # 构建文章信息
    article_info = f"标题: {title}"
    if snippet:
        article_info += f"\n摘要: {snippet[:800]}"
    if source:
        article_info += f"\n来源: {source}"
    if published_date:
        article_info += f"\n发布时间: {published_date}"

    messages = [
        {"role": "system", "content": _ARTICLE_ANALYSIS_SYSTEM_PROMPT},
        {"role": "user", "content": f"请分析以下财经新闻：\n\n{article_info}"},
    ]

    t0 = time.time()
    try:
        from app.services.llm import LLMService
        llm = LLMService()
        content = llm.call_llm_api(
            messages=messages,
            temperature=0.3,
            use_json_mode=True,
        )
    except ValueError:
        logger.info("[AI分析] 无可用 LLM API Key, 跳过")
        return None
    except Exception as e:
        logger.error("[AI分析] LLM 调用失败: %s", e)
        return None

    elapsed = round(time.time() - t0, 1)
    result = _parse_json(content)
    if result:
        # 标准化字段: 评分范围 -10 ~ +10
        result["score"] = max(-10.0, min(10.0, float(result.get("score", 0.0))))
        result["sentiment"] = _score_to_sentiment(result["score"])
        result["keywords"] = result.get("keywords", [])
        result["simplified_text"] = result.get("simplified_text", "")
        result["added_keywords"] = result.get("added_keywords", [])
        result["impact_level"] = result.get("impact_level", "medium")
        result["reasoning"] = result.get("reasoning", "")
        result["llm_provider"] = getattr(llm.provider, "value", "unknown")
        result["analysis_time"] = elapsed
        logger.info(f"[AI分析] 完成, 评分={result['score']}, 耗时={elapsed}s")
    return result


# ═══════════════════════════════════════════════════════════════
#  工具函数
# ═══════════════════════════════════════════════════════════════

def _score_to_sentiment(score: float) -> str:
    """评分 → 情感标签 (适用于 -10 ~ +10 分制)"""
    if score > 1.0:
        return "positive"
    elif score < -1.0:
        return "negative"
    return "neutral"



def _parse_json(content: str) -> Optional[Dict]:
    """解析 LLM 返回的 JSON, 兼容 markdown 包裹"""
    if not content:
        return None
    json_str = content
    if "```json" in content:
        json_str = content.split("```json")[1].split("```")[0]
    elif "```" in content:
        json_str = content.split("```")[1].split("```")[0]
    try:
        return json.loads(json_str.strip())
    except json.JSONDecodeError:
        pass
    try:
        match = re.search(r'\{.*\}', content, re.DOTALL)
        if match:
            return json.loads(match.group())
    except Exception:
        pass
    return None


# ═══════════════════════════════════════════════════════════════
#  3. 综合评分 (多篇聚合, RMS + 非对称时间衰减)
# ═══════════════════════════════════════════════════════════════

# ── 衰减参数 ──
GOOD_NEWS_HALF_LIFE_DAYS = 10.0   # 好消息半衰期 10 天
BAD_NEWS_HALF_LIFE_DAYS  = 15.0   # 坏消息半衰期 15 天

# ── 综合评分输出范围 ──
COMPOSITE_MAX =  5.0
COMPOSITE_MIN = -5.0


def _time_decay_factor(hours_old: float, is_negative: bool) -> float:
    """
    计算时间衰减因子

    使用指数衰减: weight = 0.5^(t / half_life)
      - 好消息 (positive): 半衰期 10 天 → ~10 天影响力减半, ~30 天基本归零
      - 坏消息 (negative): 半衰期 15 天 → ~15 天影响力减半, ~45 天基本归零

    Args:
        hours_old: 文章发布后经过的小时数
        is_negative: 是否为负面消息 (负面消息衰减更慢)

    Returns:
        衰减权重 (0.0 ~ 1.0)
    """
    half_life = BAD_NEWS_HALF_LIFE_DAYS if is_negative else GOOD_NEWS_HALF_LIFE_DAYS
    half_life_hours = half_life * 24.0
    return math.pow(0.5, hours_old / half_life_hours)


def composite_score(
    articles: List[Dict[str, Any]],
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    多篇新闻综合评分 (RMS 聚合 + 非对称时间衰减)

    核心算法:
      1. 每篇文章 score ∈ [-10, +10], 经时间衰减后得到 weighted_score
      2. 分为正分组和负分组, 各自用加权 RMS 聚合
         → 强信号自权重高, 中性/微偏差文章几乎不影响结果
      3. 正分 RMS 减去负分 RMS, 得到 raw_composite ∈ [-10, +10]
      4. 线性映射到 [-5, +5] 输出范围
      5. 一票否决文章单独处理 (score=-999 触发否决)

    特性:
      - 一篇 10 分 + 五篇 2 分 → 满分 5 (强信号主导, 弱信号不稀释)
      - 一篇 -10 分 + 五篇 -2 分 → -5 (对称)
      - 中性文章 (0 分) 不影响结果 (权重为 0)
      - 好消息 10 天归零, 坏消息 15 天归零

    Args:
        articles: 文章列表, 每个元素需包含:
            - "score": float  (单篇评分, -10 ~ +10 或 -999)
            - "published_date": str  (ISO 格式时间, 可选)
        now: 当前时间 (默认 datetime.now())

    Returns:
        {
            "composite_score": 3.7,     # 综合评分 (-5 ~ +5, 一票否决时为 -5)
            "direction": "利好",        # 利好/偏利好/中性/偏利空/利空
            "positive_count": 5,        # 利好文章数
            "negative_count": 2,        # 利空文章数
            "neutral_count": 1,         # 中性文章数
            "veto": False,              # 是否触发一票否决
            "veto_article": None,       # 一票否决的文章信息
            "positive_rms": 7.2,        # 正分 RMS (调试用)
            "negative_rms": 1.5,        # 负分 RMS (调试用)
            "total_articles": 8,        # 总文章数
        }
    """
    if now is None:
        now = datetime.now()

    pos_weighted: List[float] = []   # 正分 (已衰减)
    neg_weighted: List[float] = []   # 负分 (已衰减, 取绝对值)
    pos_count = 0
    neg_count = 0
    neu_count = 0
    veto_info = None

    for art in articles:
        score = art.get("score", 0.0)
        if score is None:
            score = 0.0  # 无评分视为中性
        pub_date_str = art.get("published_date", "")

        # ── 一票否决检测 ──
        if score == -999.0:
            veto_info = art
            continue

        # ── 计算时间衰减 ──
        hours_old = 0.0
        if pub_date_str:
            try:
                pub_dt = datetime.fromisoformat(pub_date_str)
                hours_old = max(0.0, (now - pub_dt).total_seconds() / 3600.0)
            except (ValueError, TypeError):
                pass

        is_neg = score < 0
        decay = _time_decay_factor(hours_old, is_neg)
        weighted = score * decay

        # ── 分组 ──
        if weighted > 0.01:
            pos_weighted.append(weighted)
            pos_count += 1
        elif weighted < -0.01:
            neg_weighted.append(abs(weighted))
            neg_count += 1
        else:
            neu_count += 1

    # ── 一票否决: 直接返回 -5 ──
    if veto_info is not None:
        return {
            "composite_score": COMPOSITE_MIN,
            "direction": "利空",
            "positive_count": pos_count,
            "negative_count": neg_count,
            "neutral_count": neu_count,
            "veto": True,
            "veto_article": veto_info,
            "positive_rms": 0.0,
            "negative_rms": 10.0,
            "total_articles": len(articles),
        }

    # ── 加权 RMS 聚合 (quartic weighting, n=4) ──
    # 公式: sqrt(sum(x^2 * |x|^4) / sum(|x|^4))
    # 特性: 强信号自权重极高, 微弱信号几乎不影响结果
    #   例: score=10 权重=10000, score=2 权重=16 → 强信号完全主导
    #   10 + 5×2 → 4.98 ≈ 5.0 (满分)
    def _weighted_rms(values: List[float]) -> float:
        if not values:
            return 0.0
        weights = [abs(v) ** 4 for v in values]
        total_weight = sum(weights)
        if total_weight == 0:
            return 0.0
        return math.sqrt(sum(v * v * w for v, w in zip(values, weights)) / total_weight)

    pos_rms = _weighted_rms(pos_weighted)  # ∈ [0, 10]
    neg_rms = _weighted_rms(neg_weighted)  # ∈ [0, 10]

    # ── 合成: 正分 RMS - 负分 RMS → raw ∈ [-10, +10] ──
    raw_composite = pos_rms - neg_rms

    # ── 映射到 [-5, +5] ──
    # 线性映射: raw ∈ [-10, +10] → composite ∈ [-5, +5]
    composite = raw_composite * 0.5
    composite = round(max(COMPOSITE_MIN, min(COMPOSITE_MAX, composite)), 1)

    # ── 方向标签 ──
    if composite >= 3.0:
        direction = "利好"
    elif composite >= 1.0:
        direction = "偏利好"
    elif composite <= -3.0:
        direction = "利空"
    elif composite <= -1.0:
        direction = "偏利空"
    else:
        direction = "中性"

    return {
        "composite_score": composite,
        "direction": direction,
        "positive_count": pos_count,
        "negative_count": neg_count,
        "neutral_count": neu_count,
        "veto": False,
        "veto_article": None,
        "positive_rms": round(pos_rms, 2),
        "negative_rms": round(neg_rms, 2),
        "total_articles": len(articles),
    }

# -*- coding: utf-8 -*-
"""
新闻分析评分引擎 — news_analysis.py

职责:
  1. keyword_score_article()  → 单篇规则引擎评分 (-10 ~ +10, 纯算法, 无外部依赖)
  2. composite_score()        → 多篇综合评分 (RMS 聚合 + 非对称时间衰减)

调用方:
  - news_search.py NewsCacheManager.calc_score() → composite_score()
  - 上层路由/服务 → 直接调用单篇评分

设计原则:
  - 单篇评分: 每篇文章独立打分, 范围 -10 ~ +10, 0 = 中性, -999 = 一票否决
  - 规则引擎: 声明式规则 (正则/分类/权重/上下文), 替代原扁平字典匹配
  - 综合评分: RMS 聚合 (强信号不被弱/中性稀释), 非对称时间衰减
    好消息 10 天衰减至 0, 坏消息 15 天衰减至 0
"""
import math
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.utils.logger import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════
#  1. 规则引擎评分 (单篇, -10 ~ +10, 无需 LLM)
# ═══════════════════════════════════════════════════════════════

from app.services.rule_engine import rule_engine_score as _rule_engine_score


def keyword_score_article(title: str, snippet: str = "", news_type: str = "stock") -> Dict[str, Any]:
    """
    单篇规则引擎评分 (纯算法, -10 ~ +10)

    Args:
        title: 文章标题
        snippet: 文章摘要/正文 (可选)
        news_type: "policy" / "stock" / "market" / "general"

    Returns:
        {
            "score": 7.5,              # 评分 (-10 ~ +10, 一票否决时为 -999)
            "sentiment": "positive",   # 情感标签
            "veto": False,             # 是否一票否决
            "veto_keyword": None,      # 否决关键词
            "bullish_hits": {...},     # 利好命中
            "bearish_hits": {...},     # 利空命中
            "keywords": [...],         # 命中关键词
        }
    """
    return _rule_engine_score(title=title, snippet=snippet, news_type=news_type)


# ═══════════════════════════════════════════════════════════════
#  2. 综合评分 (多篇聚合, RMS + 非对称时间衰减)
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

    Args:
        articles: 文章列表, 每个元素需包含:
            - "score": float  (单篇评分, -10 ~ +10 或 -999)
            - "published_date": str  (ISO 格式时间, 可选)
        now: 当前时间 (默认 datetime.now())

    Returns:
        {
            "composite_score": 3.7,     # 综合评分 (-5 ~ +5)
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
        now = datetime.utcnow()

    pos_weighted: List[float] = []
    neg_weighted: List[float] = []
    pos_count = 0
    neg_count = 0
    neu_count = 0
    veto_info = None

    for art in articles:
        score = art.get("score", 0.0)
        if score is None:
            score = 0.0
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
    def _weighted_rms(values: List[float]) -> float:
        if not values:
            return 0.0
        weights = [abs(v) ** 4 for v in values]
        total_weight = sum(weights)
        if total_weight == 0:
            return 0.0
        return math.sqrt(sum(v * v * w for v, w in zip(values, weights)) / total_weight)

    pos_rms = _weighted_rms(pos_weighted)
    neg_rms = _weighted_rms(neg_weighted)

    raw_composite = pos_rms - neg_rms
    composite = raw_composite * 0.5
    composite = round(max(COMPOSITE_MIN, min(COMPOSITE_MAX, composite)), 1)

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

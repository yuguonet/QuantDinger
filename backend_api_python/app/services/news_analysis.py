"""
新闻分析评分引擎 — news_analysis.py

职责:
  1. keyword_score_article()  → 单篇关键词评分 (0-10, 纯算法, 无外部依赖)
  2. ai_analyze_article()     → 单篇 AI 分析评分 (LLM, 允许改写/降级/加关键词)
  3. composite_score()        → 多篇综合评分 (时间衰减加权, 从 news.py 迁入)

调用方:
  - news.py NewsCacheManager.calc_score() → composite_score()
  - news_processor.py → 可被本模块替代
  - 上层路由/服务 → 直接调用单篇评分

设计原则:
  - 单篇评分: 每篇文章独立打分, 返回结构化结果
  - AI 分析: 允许 LLM 改写文章内容, 增加关键词, 降级为通俗易懂版本
  - 综合评分: 保留原有时间衰减 + 一票否决逻辑
"""
import json
import re
import time
from typing import Any, Dict, Optional

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
    单篇关键词评分 (纯算法, 0-10)

    评分逻辑:
      1. 一票否决: 命中 veto 关键词 → score = -999
      2. 分别匹配利好/利空关键词, 取各自最高分
      3. 利好分 > 利空分 → 偏向利好区间, 反之亦然
      4. 无命中 → 中性 5.0

    Args:
        title: 文章标题
        snippet: 文章摘要/正文片段 (可选)

    Returns:
        {
            "score": 7.5,              # 评分 (0-10, 一票否决时为 -999)
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
            "score": 5.0,
            "sentiment": "neutral",
            "keywords": [],
            "veto": False,
            "veto_keyword": None,
            "bullish_hits": {},
            "bearish_hits": {},
        }

    # ── 计算综合分 ──
    best_bull = max(bullish_hits.values()) if bullish_hits else 5.0
    best_bear = min(bearish_hits.values()) if bearish_hits else 5.0

    bull_count = len(bullish_hits)
    bear_count = len(bearish_hits)

    if bull_count > bear_count:
        # 偏利好: 以最高利好分为基础, 利空越多越往下拉
        base = best_bull
        penalty = bear_count * 0.5
        score = max(5.0, base - penalty)
    elif bear_count > bull_count:
        # 偏利空: 以最高利空分为基础, 利好越多越往上抬
        base = best_bear
        bonus = bull_count * 0.5
        score = min(5.0, base + bonus)
    else:
        # 数量相当: 取两者的中点
        score = (best_bull + best_bear) / 2

    score = round(max(0.0, min(10.0, score)), 1)

    # 情感标签
    if score >= 7:
        sentiment = "positive"
    elif score <= 3:
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

1. **评分**：根据文章内容对投资者的影响，给出 0-10 的评分
   - 0-3: 利空（负面消息，对投资者不利）
   - 4-6: 中性（信息性内容，无明显方向）
   - 7-10: 利好（正面消息，对投资者有利）

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
      - 评分 0-10 (与 keyword_score_article 一致的区间)
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
        # 标准化字段
        result["score"] = max(0.0, min(10.0, float(result.get("score", 5.0))))
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
    """评分 → 情感标签"""
    if score >= 7:
        return "positive"
    elif score <= 3:
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

"""
新闻前处理层 — 纯算法, 无状态, 不碰 DB

职责:
  1. keyword_score()      → 关键词评分
  2. ai_analyze_policy()  → AI 解读 (系统 LLMService)

调用方: news.py 中的 process_policy_news()
位置:   search.py → news.py → ★ news_preprocessor.py ★ → 上层
"""
import json
import re
import time
from typing import Any, Dict, List, Optional

from app.utils.logger import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════
#  1. 关键词评分 (无需 LLM)
# ═══════════════════════════════════════════════════════════════

POLICY_KEYWORDS = [
    "央行", "降准", "降息", "LPR", "MLF", "逆回购",
    "国务院", "国常会", "发改委", "财政部", "证监会",
    "政策", "监管", "改革", "调控", "扶持", "补贴",
    "产业政策", "财政", "货币", "金融", "房地产",
    "新基建", "新能源", "芯片", "AI", "人工智能",
    "碳中和", "共同富裕", "一带一路", "RCEP",
]

_BULLISH_KEYWORDS = {
    "降准": 8.0, "降息": 8.0, "补贴": 7.0, "扶持": 7.0,
    "新基建": 7.5, "新能源": 7.0, "AI": 7.0, "人工智能": 7.0,
    "碳中和": 6.5, "RCEP": 6.5, "改革": 6.0, "开放": 6.5,
}

_BEARISH_KEYWORDS = {
    "调控": 3.0, "监管": 3.5, "收紧": 2.5, "加息": 2.0,
    "制裁": 2.0, "限制": 3.0, "整顿": 3.0,
}

_VETO_KEYWORDS = {
    "跌停", "闪崩", "崩盘", "巨亏", "暴雷", "财务造假",
    "立案调查", "退市", "债务危机", "黑天鹅",
}


def keyword_score(title: str, snippet: str = "") -> Dict[str, Any]:
    """
    关键词评分

    Returns:
        {"score": 7.5, "sentiment": "positive", "keywords": ["降准", "央行"], "veto": False}
    """
    text = f"{title} {snippet}"

    for kw in _VETO_KEYWORDS:
        if kw in text:
            return {"score": -999.0, "sentiment": "negative", "keywords": [kw], "veto": True}

    matched = [kw for kw in POLICY_KEYWORDS if kw in text]

    score = 5.0
    for kw, s in _BULLISH_KEYWORDS.items():
        if kw in text:
            score = max(score, s)
    for kw, s in _BEARISH_KEYWORDS.items():
        if kw in text:
            score = min(score, s)

    sentiment = "neutral"
    if score >= 7:
        sentiment = "positive"
    elif score <= 3:
        sentiment = "negative"

    return {"score": round(score, 1), "sentiment": sentiment, "keywords": matched, "veto": False}


def keyword_score_batch(items: List[Dict]) -> List[Dict]:
    """批量关键词评分, 原地更新"""
    for item in items:
        result = keyword_score(item.get("title", ""), item.get("snippet", ""))
        item["keywords"] = result["keywords"]
        if result["veto"]:
            item["sentiment_score"] = -999.0
            item["sentiment"] = "negative"
        else:
            item["sentiment_score"] = result["score"]
            item["sentiment"] = result["sentiment"]
    return items


# ═══════════════════════════════════════════════════════════════
#  2. AI 解读评分 (需要系统 LLMService)
# ═══════════════════════════════════════════════════════════════

_POLICY_SYSTEM_PROMPT = """你是一位资深的中国宏观策略分析师，擅长解读政策对金融市场的影响。

分析要求：
1. 准确识别实质性政策变动（非泛泛而谈）
2. 判断政策方向：货币宽松/紧缩、财政扩张/收缩、产业扶持/限制
3. 量化影响程度：重大(★★★) / 中等(★★) / 轻微(★)
4. 关联板块：利好和利空的行业/板块
5. 时间维度：短期/中期/长期
6. 风险提示

输出严格 JSON：
{
  "summary": "政策基调概要",
  "overall_sentiment": "宽松/中性/偏紧",
  "composite_score": 5.0,
  "policies": [
    {
      "headline": "政策标题",
      "direction": "利好/利空/中性",
      "impact_level": "★★★/★★/★",
      "keywords": ["关键词"],
      "core_analysis": "核心解读",
      "bullish_sectors": ["板块"],
      "bearish_sectors": ["板块"],
      "time_horizon": "短期/中期/长期",
      "score": 7.5,
      "risks": "风险"
    }
  ],
  "market_outlook": "市场展望",
  "actionable_advice": "策略建议"
}"""


def ai_analyze_policy(news_list: List[Dict]) -> Optional[Dict[str, Any]]:
    """
    用系统 LLMService 对政策新闻做深度分析

    Args:
        news_list: [{"title": "...", "source": "...", "time": "..."}]

    Returns:
        AI 分析结果 dict, 或 None (LLM 不可用时)
    """
    if not news_list:
        return None

    try:
        from app.services.llm import LLMService
        llm = LLMService()
    except Exception as e:
        logger.warning("[前处理-AI] LLMService 初始化失败: %s", e)
        return None

    news_text = "\n".join(
        f"[{n.get('source', '财经')} | {n.get('time', '')}] {n['title']}"
        for n in news_list if n.get("title")
    )

    messages = [
        {"role": "system", "content": _POLICY_SYSTEM_PROMPT},
        {"role": "user", "content": f"以下是今日财经新闻，请筛选政策相关内容做深度解读：\n\n---\n{news_text}\n---\n\n请严格输出 JSON。"},
    ]

    t0 = time.time()
    try:
        content = llm.call_llm_api(messages=messages, temperature=0.3, use_json_mode=True)
    except ValueError:
        logger.info("[前处理-AI] 无可用 LLM API Key, 跳过")
        return None
    except Exception as e:
        logger.error("[前处理-AI] LLM 调用失败: %s", e)
        return None

    elapsed = round(time.time() - t0, 1)
    result = _parse_json(content)
    if result:
        result["llm_provider"] = getattr(llm.provider, "value", "unknown")
        result["analysis_time"] = elapsed
        result["news_count"] = len(news_list)
        logger.info(f"[前处理-AI] 分析完成, 耗时 {elapsed}s")
    return result


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

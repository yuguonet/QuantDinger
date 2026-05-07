"""
国内市场宏观数据 — 精简版

仅保留 macro_backend.py 依赖的接口。
原 china_market.py 的其他功能已由 MacroCNBackend 统一接管。

用法:
    from app.market_cn.china_market import get_policy
    data = get_policy()
"""
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def get_policy() -> dict:
    """AI政策解读 — 调用 news_service 获取政策新闻。"""
    try:
        from app.services.news_service import fetch_financial_news, get_news_cache_manager
        from app.services.news_analysis import composite_score
    except ImportError as e:
        logger.error("news_service 导入失败: %s", e)
        return {"code": 0, "msg": f"依赖缺失: {e}", "data": {}}

    news_list = []
    try:
        resp = fetch_financial_news(lang="all", market="CNStock", symbol="POLICY")
        news_list = resp.get("cn", []) + resp.get("en", [])
    except Exception as e:
        logger.error("fetch_financial_news(POLICY) 异常: %s", e)

    if not news_list:
        try:
            cache_mgr = get_news_cache_manager()
            cached_items = cache_mgr.get_items("POLICY", "CNStock")
            if cached_items:
                news_list = [
                    {
                        "title": r.get("title", ""),
                        "link": r.get("url", ""),
                        "snippet": r.get("snippet", ""),
                        "source": r.get("source", ""),
                        "published": r.get("published_date", ""),
                        "sentiment": r.get("sentiment", "neutral"),
                        "sentiment_score": r.get("sentiment_score"),
                        "category": "政策/宏观:CNStock", "lang": "cn",
                    }
                    for r in cached_items
                ]
        except Exception as e:
            logger.error("DB 缓存降级失败: %s", e)

    if not news_list:
        return {"code": 0, "msg": "暂无政策数据", "data": {}}

    score_articles = [
        {"score": item.get("sentiment_score", 0.0) or 0.0,
         "published_date": item.get("published", "")}
        for item in news_list
    ]
    try:
        score_info = composite_score(score_articles)
    except Exception:
        score_info = {}

    return {
        "code": 1, "msg": "success",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "data": {
            "news": news_list,
            "items": news_list,
            "score": {
                "composite_score": score_info.get("composite_score", 0.0),
                "direction": score_info.get("direction", "中性"),
                "positive": score_info.get("positive_count", 0),
                "negative": score_info.get("negative_count", 0),
                "neutral": score_info.get("neutral_count", 0),
                "veto": score_info.get("veto", False),
                "veto_count": 1 if score_info.get("veto") else 0,
            },
        },
    }

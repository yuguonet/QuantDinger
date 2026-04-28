"""
Financial news data provider — 新闻数据生命周期管理

职责:
1. 新闻缓存 (PostgreSQL): 24h去重, 15天过期, 自选股时间窗口
2. 情感评分: 关键词评分 / AI 评分 / RMS 综合评分
3. 路由分发: 全部委托 search.py search_news_dispatch()

数据流:
  上层调用 → fetch_financial_news() → 缓存检查 → search.py → 评分 → 写缓存 → 返回
"""
from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.utils.logger import get_logger
from app.utils.db import get_db_connection
from app.services.search import SearchResult, SearchResponse, get_search_service, _safe_encode

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════
# 常量
# ═══════════════════════════════════════════════════════════════

POLICY_NEWS_SYMBOL = "POLICY"
GENERAL_NEWS_SYMBOL = "GENERAL_NEWS"


def get_news_type(symbol: str, market: str) -> str:
    """
    通过 symbol 判断新闻类型 (用于选择评分策略)

    规则:
      - symbol == POLICY_NEWS_SYMBOL → "policy"   政策/宏观 (AI 评分)
      - symbol == GENERAL_NEWS_SYMBOL → "general"  通用 (关键词评分)
      - symbol == market             → "market"    市场新闻 (关键词评分)
      - 其它                         → "stock"     个股新闻 (关键词评分)
    """
    if symbol == POLICY_NEWS_SYMBOL:
        return "policy"
    if symbol == GENERAL_NEWS_SYMBOL:
        return "general"
    if symbol == market:
        return "market"
    return "stock"


# ═══════════════════════════════════════════════════════════════
# 新闻缓存配置
# ═══════════════════════════════════════════════════════════════

NEWS_CACHE_DEDUP_HOURS = 24
NEWS_CACHE_EXPIRY_DAYS = 15

_MARKET_TRADING_WINDOWS = {
    "CNStock":  {"pre_open": (7, 30, 9, 30),  "midday": (12, 0, 13, 0)},
    "USStock":  {"pre_open": (20, 30, 21, 30), "midday": (1, 0, 2, 0)},
    "Crypto":   {"pre_open": None, "midday": None},
    "Forex":    {"pre_open": (5, 30, 6, 0),   "midday": (12, 0, 13, 0)},
}

# 宏观市场映射 (API 层 → POLICY 路由)
_MACRO_MARKET_MAP = {
    "MacroCN": "CNStock",
    "MacroIntl": "USStock",
}


# ═══════════════════════════════════════════════════════════════
# PostgreSQL 新闻缓存管理器
# ═══════════════════════════════════════════════════════════════

class NewsCacheManager:
    """
    新闻缓存管理器 (纯 DB 比对, 无内存状态)

    存储: qd_news_cache_items (每条一行, UNIQUE(symbol, market, title))
    策略: 24h去重, 15天过期, 自选股时间窗口, 评分写入
    """

    def __init__(self):
        pass

    def _purge_expired(self, conn) -> int:
        """清除超过 NEWS_CACHE_EXPIRY_DAYS 天的过期记录"""
        try:
            cursor = conn.cursor()
            cursor.execute(
                f"DELETE FROM qd_news_cache_items WHERE created_at < NOW() - INTERVAL '{NEWS_CACHE_EXPIRY_DAYS} days'"
            )
            deleted = cursor.rowcount
            if deleted > 0:
                logger.info(f"[过期清理] 已清除 {deleted} 条超过 {NEWS_CACHE_EXPIRY_DAYS} 天的新闻缓存")
            return deleted
        except Exception as e:
            logger.warning(f"过期清理异常(非致命): {e}")
            return 0

    def _get_last_search_time(self, symbol: str, market: str) -> Optional[datetime]:
        """从 DB 查询该 symbol 最后一条缓存的入库时间"""
        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT MAX(created_at) AS last_time FROM qd_news_cache_items WHERE symbol = %s AND market = %s",
                    (symbol, market)
                )
                row = cursor.fetchone()
                if row and row.get('last_time'):
                    return row['last_time']
                return None
        except Exception as e:
            logger.warning(f"查询最后搜索时间异常: {e}")
            return None

    def get_items(self, symbol: str, market: str = "CNStock") -> List[Dict[str, Any]]:
        """从 DB 查询缓存新闻, 同时清理过期记录"""
        try:
            with get_db_connection() as conn:
                self._purge_expired(conn)
                conn.commit()
                cursor = conn.cursor()
                cursor.execute(
                    """SELECT title, snippet, url, source, published_date, sentiment, sentiment_score
                       FROM qd_news_cache_items
                       WHERE symbol = %s AND market = %s
                       ORDER BY published_date DESC""",
                    (symbol, market)
                )
                return [dict(r) for r in cursor.fetchall()] or []
        except Exception as e:
            logger.warning(f"查询新闻缓存异常: {e}")
            return []

    def should_search(self, symbol: str, market: str = "CNStock",
                      is_watchlist: bool = False) -> tuple:
        """
        判断是否需要搜索

        判断顺序:
          1. 自选股 + 开盘前窗口 + 今日未搜 → 搜索
          2. 自选股 + 午间窗口 + 午间未搜 → 搜索
          3. DB中距上次搜索 <24h → 跳过
          4. 其他 → 搜索

        Returns:
            (should_search: bool, reason: str)
        """
        last_search = self._get_last_search_time(symbol, market)
        now = datetime.now()

        if is_watchlist:
            windows = _MARKET_TRADING_WINDOWS.get(market)
            if windows:
                pre = windows.get("pre_open")
                if pre:
                    sh, sm, eh, em = pre
                    in_pre = (now.hour > sh or (now.hour == sh and now.minute >= sm)) and \
                             (now.hour < eh or (now.hour == eh and now.minute < em))
                    if in_pre:
                        if last_search and last_search.date() == now.date():
                            return False, f"自选股今日已搜({last_search.strftime('%H:%M')}), 开盘前无需重复"
                        else:
                            return True, f"自选股开盘前刷新({market}, 今日未搜索)"

                mid = windows.get("midday")
                if mid:
                    mh, mm, meh, mem = mid
                    in_mid = (now.hour > mh or (now.hour == mh and now.minute >= mm)) and \
                             (now.hour < meh or (now.hour == meh and now.minute < mem))
                    if in_mid:
                        if last_search and last_search.date() == now.date() and last_search.hour >= mh:
                            return False, f"自选股午间已搜({last_search.strftime('%H:%M')})"
                        else:
                            return True, f"自选股午间刷新({market})"

        if last_search:
            hours_since = (now - last_search).total_seconds() / 3600
            if hours_since < NEWS_CACHE_DEDUP_HOURS:
                return False, f"距上次搜索仅{hours_since:.1f}h(<24h), 使用缓存"

        return True, "需要搜索" if not last_search else f"距上次搜索超过{NEWS_CACHE_DEDUP_HOURS}h"

    def calc_dynamic_days(self, symbol: str, market: str, default_days: int = 3) -> int:
        """根据最后搜索时间动态缩减搜索天数"""
        last_search = self._get_last_search_time(symbol, market)
        if not last_search:
            return default_days

        hours_since = (datetime.now() - last_search).total_seconds() / 3600
        if hours_since < 1:
            return 1
        elif hours_since < 6:
            return 2
        elif hours_since < 12:
            return 3
        elif hours_since < 24:
            return 4
        return default_days

    def save_items(self, symbol: str, market: str, results: List[SearchResult]) -> bool:
        """
        将搜索结果写入明细表, ON CONFLICT 更新

        评分策略 (按 news_type):
          - policy:  AI 分析评分 (LLM 改写+评分), 失败降级为关键词
          - 其他:    关键词评分 (纯算法), 一票否决 (-999)
        """
        if not symbol or not market or not results:
            return False
        try:
            with get_db_connection() as conn:
                self._purge_expired(conn)
                cursor = conn.cursor()

                news_type = get_news_type(symbol, market)
                from app.services.news_analysis import keyword_score_article, ai_analyze_article

                if news_type == "policy":
                    logger.info(f"[评分] 政策/宏观新闻, 启用 AI 分析: {len(results)}条")
                else:
                    logger.debug(f"[评分] {news_type}新闻, 关键词评分: {len(results)}条")

                rows = []
                for r in results:
                    title = _safe_encode(r.title, 500)
                    snippet = _safe_encode(r.snippet, 2000)
                    url = _safe_encode(r.url, 1000)
                    source = _safe_encode(r.source, 100)
                    pub_date = _safe_encode(r.published_date or '', 40)

                    if news_type == "policy":
                        ai_result = ai_analyze_article(
                            title=title, snippet=snippet,
                            source=source, published_date=pub_date,
                        )
                        if ai_result:
                            score = ai_result["score"]
                            sentiment = ai_result["sentiment"]
                            simplified = ai_result.get("simplified_text", "")
                            if simplified:
                                snippet = _safe_encode(simplified, 2000)
                        else:
                            kw_result = keyword_score_article(title, snippet)
                            score = kw_result["score"]
                            sentiment = kw_result["sentiment"]
                    else:
                        kw_result = keyword_score_article(title, snippet)
                        if kw_result["veto"]:
                            score = -999.0
                            sentiment = "negative"
                            logger.warning(f"[一票否决] 检测到重大负面: {title[:60]}")
                        else:
                            score = kw_result["score"]
                            sentiment = kw_result["sentiment"]

                    rows.append((symbol, market, title, snippet, url, source,
                                 pub_date, sentiment, score))

                cursor.executemany(
                    """INSERT INTO qd_news_cache_items
                       (symbol, market, title, snippet, url, source, published_date, sentiment, sentiment_score)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (symbol, market, title) DO UPDATE SET
                           snippet = EXCLUDED.snippet,
                           url = EXCLUDED.url,
                           source = EXCLUDED.source,
                           published_date = EXCLUDED.published_date,
                           sentiment = EXCLUDED.sentiment,
                           sentiment_score = EXCLUDED.sentiment_score,
                           created_at = NOW()""",
                    rows
                )
                conn.commit()
                logger.info(f"新闻缓存已保存: {symbol}({market}) {news_type} {len(results)}条")
                return True
        except Exception as e:
            logger.error(f"保存新闻缓存异常: {e}")
            return False

    @staticmethod
    def calc_score(symbol: str, market: str) -> Dict[str, Any]:
        """
        综合评分 — 委托 news_analysis.composite_score() (RMS + 非对称时间衰减)

        输出分值范围: -5 ~ +5
        """
        from app.services.news_analysis import composite_score as _composite_score

        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """SELECT sentiment_score, created_at
                       FROM qd_news_cache_items
                       WHERE symbol = %s AND market = %s""",
                    (symbol, market)
                )
                rows = cursor.fetchall()
        except Exception as e:
            logger.warning(f"calc_score 查询异常: {e}")
            rows = []

        if not rows:
            return {"composite_score": 0.0, "direction": "中性",
                    "positive": 0, "negative": 0, "neutral": 0,
                    "veto": False, "veto_count": 0}

        articles = []
        for row in rows:
            raw_score = row.get("sentiment_score")
            created_at = row.get("created_at")
            articles.append({
                "score": float(raw_score) if raw_score is not None else 0.0,
                "published_date": created_at.isoformat() if created_at else "",
            })

        result = _composite_score(articles)

        return {
            "composite_score": result["composite_score"],
            "direction": result["direction"],
            "positive": result.get("positive_count", 0),
            "negative": result.get("negative_count", 0),
            "neutral": result.get("neutral_count", 0),
            "veto": result.get("veto", False),
            "veto_count": 1 if result.get("veto") else 0,
        }


# 单例
_news_cache_manager: Optional[NewsCacheManager] = None


def get_news_cache_manager() -> NewsCacheManager:
    """获取新闻缓存管理器单例"""
    global _news_cache_manager
    if _news_cache_manager is None:
        _news_cache_manager = NewsCacheManager()
    return _news_cache_manager


# ═══════════════════════════════════════════════════════════════
# 格式化工具
# ═══════════════════════════════════════════════════════════════

def _results_to_list(
    results: List[SearchResult],
    lang: str,
    category: str,
) -> List[Dict[str, Any]]:
    """SearchResult → 统一 dict 格式"""
    return [
        {
            "title": r.title, "link": r.url,
            "snippet": r.snippet, "source": r.source,
            "published": r.published_date or "",
            "sentiment": r.sentiment,
            "sentiment_score": r.sentiment_score,
            "category": category, "lang": lang,
        }
        for r in results
    ]


def _detect_lang(results: List[SearchResult], default: str = "cn") -> str:
    """根据结果内容推断实际语言"""
    sample = " ".join(r.title for r in results[:5])
    if not sample:
        return default
    cn_chars = len(re.findall(r'[\u4e00-\u9fff]', sample))
    total = len(sample) or 1
    return "cn" if cn_chars / total > 0.15 else "en"


def _format_cached(items: List[Dict[str, Any]], lang: str) -> Dict[str, List[Dict[str, Any]]]:
    """缓存命中 → 格式化为 {"cn": [...], "en": [...]}"""
    result: Dict[str, List[Dict[str, Any]]] = {"cn": [], "en": []}
    for item in items:
        lang_key = "cn"  # 默认中文
        result[lang_key].append({
            "title": item["title"], "link": item.get("url", ""),
            "snippet": item.get("snippet", ""), "source": item.get("source", ""),
            "published": item.get("published_date", ""),
            "sentiment": item.get("sentiment", "neutral"),
            "sentiment_score": item.get("sentiment_score"),
            "category": "", "lang": lang_key,
        })
    return result


# ═══════════════════════════════════════════════════════════════
# 统一入口 — 缓存 → search.py → 评分 → 返回
# ═══════════════════════════════════════════════════════════════

def fetch_financial_news(
    lang: str = "all",
    market: str = "all",
    symbol: str = "",
    name: str = "",
    keywords: str = "",
) -> Dict[str, List[Dict[str, Any]]]:
    """
    财经新闻统一入口 — 唯一对外接口

    内部流程:
      1. 规范化 (MacroCN/MacroIntl → POLICY)
      2. 缓存检查 (NewsCacheManager.should_search)
      3. 缓存命中 → 直接返回
      4. 缓存未命中 → search.py search_news_dispatch() (路由全交出去)
      5. 评分 + 写缓存 (NewsCacheManager.save_items)
      6. 格式化返回

    路由规则 (由 search.py 内部处理):
      symbol=="POLICY"                        → 政策/宏观新闻
      symbol+market 都空 + keywords 非空      → 纯关键词搜索 (keywords 必填)
      symbol+market 都空 + 无 keywords        → 通用财经新闻
      symbol==空 + 具体 market                → 该市场新闻
      symbol==market                          → 该市场新闻
      symbol 是股票代码                       → 个股新闻

    Args:
        lang:      "cn"/"en"/"all"
        market:    "CNStock"/"USStock"/"Crypto"/"Forex"/"all"/"MacroCN"/"MacroIntl"
        symbol:    股票代码 ("600519") 或 "POLICY" 或 ""
        name:      股票名称 (仅个股, 提升搜索精度)
        keywords:  自定义搜索关键词 (可选, symbol+market 都为空时必须非空)

    返回格式:
      {"cn": [...], "en": [...], "_meta": {...}(仅个股)}
      每条: {title, link, snippet, source, published, sentiment, sentiment_score, category, lang}
    """
    result: Dict[str, List[Dict[str, Any]]] = {"cn": [], "en": []}

    try:
        # ── 1. 规范化 ──
        effective_market = market if market != "all" else "CNStock"
        is_watchlist = False  # TODO: 上层可传入

        if market in _MACRO_MARKET_MAP:
            symbol = "POLICY"
            effective_market = _MACRO_MARKET_MAP[market]

        # 确定缓存用的 symbol/market
        cache_symbol = symbol or (effective_market if market != "all" else GENERAL_NEWS_SYMBOL)
        cache_market = effective_market
        if not symbol and market == "all":
            cache_market = "General"

        # ── 2. 缓存检查 ──
        cache_mgr = get_news_cache_manager()
        should, reason = cache_mgr.should_search(cache_symbol, cache_market, is_watchlist)

        if not should:
            cached = cache_mgr.get_items(cache_symbol, cache_market)
            if cached:
                logger.info(f"[缓存命中] {cache_symbol}({cache_market}): {reason}, {len(cached)}条")
                return _format_cached(cached, lang)
            logger.info(f"[缓存降级] {cache_symbol}({cache_market}): {reason}, 但无数据, 重新搜索")

        logger.info(f"[需要搜索] {cache_symbol}({cache_market}): {reason}")

        # ── 3. 调 search.py — 路由全交出去 ──
        # 动态缩减搜索天数 (仅个股)
        is_stock = bool(symbol) and symbol not in ("POLICY", GENERAL_NEWS_SYMBOL) and symbol != effective_market
        if is_stock:
            days = cache_mgr.calc_dynamic_days(symbol, effective_market, default_days=3)
        elif symbol == "POLICY":
            days = 1
        else:
            days = 1

        svc = get_search_service()
        resp = svc.search_news_dispatch(
            symbol=symbol,
            market=effective_market,
            lang=lang,
            days=days,
            max_web_results=5,
            name=name,
            keywords=keywords,
        )

        if not resp.success or not resp.results:
            logger.info(f"[搜索无结果] {cache_symbol}({cache_market})")
            return result

        # ── 4. 评分 + 写缓存 ──
        cache_mgr.save_items(cache_symbol, cache_market, resp.results)

        # ── 5. 格式化返回 ──
        news_type = resp.metadata.get("news_type", "stock")
        actual_lang = _detect_lang(resp.results, "cn" if effective_market == "CNStock" else "en")
        target_lang = lang if lang != "all" else actual_lang

        category_map = {
            "policy": f"政策/宏观:{effective_market}",
            "general": "通用",
            "market": f"市场:{effective_market}",
            "stock": f"个股:{symbol}",
        }
        category = category_map.get(news_type, "")

        items = _results_to_list(resp.results, target_lang, category)
        result[target_lang].extend(items)

        # 个股附加评分元数据
        if news_type == "stock" and symbol:
            score_info = cache_mgr.calc_score(cache_symbol, cache_market)
            result["_meta"] = {
                "composite_score": score_info.get("composite_score", 0),
                "direction": score_info.get("direction", "中性"),
                "from_cache": False,
                "total": len(resp.results),
            }

        logger.info(f"[完成] {cache_symbol}({cache_market}) {news_type}: {len(resp.results)}条")
        return result

    except Exception as e:
        logger.error("Failed to fetch financial news: %s", e)

    return result

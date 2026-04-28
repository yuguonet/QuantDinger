"""
Financial news data provider — 新闻数据生命周期管理

职责:
1. 新闻缓存 (PostgreSQL): 24h去重, 15天过期, 自选股时间窗口
2. 情感评分: 加权求和 + 时间衰减 + 重大负面一票否决
3. 市场新闻 vs 个股新闻 区分:
   - 市场新闻: news_fetcher.py 直爬 (A股优先) + Web 补充, 预定义 symbol
   - 个股新闻: stock_news.py 5路源 + Web 并行, symbol=股票代码
4. 通用财经新闻: fetch_financial_news() 按语言/市场/个股分发

数据流:
  市场新闻 → news_fetcher.py (A股直爬, 优先) + search.py (Web 补充)
  个股新闻 → stock_news.py (5路源) + search.py (Web 补充)
  上层调用 → policy_analysis_ai.py / indicator_review.py → 本模块
"""
from __future__ import annotations

import hashlib
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.utils.logger import get_logger
from app.utils.db import get_db_connection
from app.services.search import SearchResult, SearchResponse, SearchService, get_search_service, _safe_encode

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════
# 预定义市场新闻 symbol (用于 qd_news_cache_items 表)
# ═══════════════════════════════════════════════════════════════
#
# 市场新闻不针对个股, 需要统一的 symbol 标识
# 个股新闻直接用股票代码作为 symbol
#
POLICY_NEWS_SYMBOL = "POLICY"      # 政策/宏观新闻 symbol


def get_news_type(symbol: str, market: str) -> str:
    """
    通过 market + symbol 判断新闻类型

    规则:
      - symbol == market          → "market"     市场新闻 (symbol 直接等于 market)
      - symbol == POLICY_NEWS_SYMBOL → "policy"  政策/宏观新闻
      - 其它                       → "stock"      个股新闻 (symbol = 股票代码)
    """
    if symbol == market:
        return "market"
    if symbol == POLICY_NEWS_SYMBOL:
        return "policy"
    return "stock"


# ═══════════════════════════════════════════════════════════════
# 新闻缓存配置
# ═══════════════════════════════════════════════════════════════

NEWS_CACHE_DEDUP_HOURS = 24  # 24小时内不重复搜索
NEWS_CACHE_EXPIRY_DAYS = 15  # 15天后自动清除过期新闻

# ── 各市场交易时间窗口 (用于自选股刷新判断) ─────────────────
# 格式: (start_hour, start_minute, end_hour, end_minute)
_MARKET_TRADING_WINDOWS = {
    "CNStock":  {"pre_open": (7, 30, 9, 30),  "midday": (12, 0, 13, 0)},   # A股 9:30开盘
    "USStock":  {"pre_open": (20, 30, 21, 30), "midday": (1, 0, 2, 0)},     # 美股 21:30开盘 (北京时间)
    "Crypto":   {"pre_open": None, "midday": None},                          # 7x24 无特殊窗口
    "Forex":    {"pre_open": (5, 30, 6, 0),   "midday": (12, 0, 13, 0)},    # 外汇 6:00开盘
}

# ── 一票否决逻辑已迁移至 app.services.news_analysis ──


# ═══════════════════════════════════════════════════════════════
# PostgreSQL 新闻缓存管理器
# ═══════════════════════════════════════════════════════════════

class NewsCacheManager:
    """
    新闻缓存管理器 (纯 DB 比对, 无内存状态)

    存储:
    - qd_news_cache_items: 新闻明细 (每条一行, UNIQUE(symbol, market, title))

    策略:
    - 24h去重: 查 DB MAX(created_at) 判断上次搜索时间
    - 自选股: 7:30-9:30 / 12:00-13:00 按最后搜索时间判断是否需要刷新
    - 无结果不写库, 下次会重新搜索 (无额外成本)
    - sentiment_score: 数值评分, 重大负面=-999 一票否决
    """

    def __init__(self):
        pass

    def _purge_expired(self, conn) -> int:
        """清除超过 NEWS_CACHE_EXPIRY_DAYS 天的过期记录, 返回删除行数"""
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
        """从 DB 查询该股票最后一条缓存的入库时间"""
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
        """从 DB 查询该股票的缓存新闻, 同时清理过期记录"""
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

        # 自选股特殊策略 (支持多市场)
        if is_watchlist:
            windows = _MARKET_TRADING_WINDOWS.get(market)
            if windows:
                # 开盘前窗口
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

                # 午间窗口
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

        # 24小时去重
        if last_search:
            hours_since = (now - last_search).total_seconds() / 3600
            if hours_since < NEWS_CACHE_DEDUP_HOURS:
                return False, f"距上次搜索仅{hours_since:.1f}h(<24h), 使用缓存"

        return True, "需要搜索" if not last_search else f"距上次搜索超过{NEWS_CACHE_DEDUP_HOURS}h"

    def calc_dynamic_days(self, symbol: str, market: str, default_days: int = 3) -> int:
        """
        根据最后搜索时间动态缩减搜索天数, 降低 API 调用

        规则:
        - 从未搜索 → 返回 default_days
        - 距上次搜索 < 1h → 1 天 (只补最新)
        - 距上次搜索 < 6h → 2 天
        - 距上次搜索 < 12h → 3 天
        - 距上次搜索 < 24h → 4 天
        - 超过 24h → default_days
        """
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
        将搜索结果写入明细表, ON CONFLICT 更新已有标题

        流程: 去重 → 按类型评分 → 存 DB
          - 政策/宏观新闻 (symbol=POLICY): AI 分析评分 (LLM 改写+评分)
          - 市场/个股新闻: 关键词评分 (纯算法)
        """
        if not symbol or not market or not results:
            return False
        try:
            with get_db_connection() as conn:
                self._purge_expired(conn)
                cursor = conn.cursor()

                # 判断新闻类型, 选择评分方式
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
                        # 政策/宏观: AI 分析评分
                        ai_result = ai_analyze_article(
                            title=title, snippet=snippet,
                            source=source, published_date=pub_date,
                        )
                        if ai_result:
                            score = ai_result["score"]
                            sentiment = ai_result["sentiment"]
                            # 用 AI 改写的通俗版本覆盖 snippet
                            simplified = ai_result.get("simplified_text", "")
                            if simplified:
                                snippet = _safe_encode(simplified, 2000)
                        else:
                            # AI 不可用, 降级为关键词评分
                            kw_result = keyword_score_article(title, snippet)
                            score = kw_result["score"]
                            sentiment = kw_result["sentiment"]
                    else:
                        # 市场/个股: 关键词评分
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

        输出分值范围: -5 ~ +5 (与 composite_score 一致)
        外部接口:
          输入: symbol, market
          输出: {"composite_score": -5~+5, "direction": ..., "positive": ..., ...}
        """
        from app.services.news_analysis import composite_score as _composite_score

        # ── 从 DB 读取 ──
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

        # ── 构造 composite_score() 所需的 articles 列表 ──
        articles = []
        for row in rows:
            raw_score = row.get("sentiment_score")
            created_at = row.get("created_at")
            articles.append({
                "score": float(raw_score) if raw_score is not None else 0.0,
                "published_date": created_at.isoformat() if created_at else "",
            })

        # ── 调用 composite_score() ──
        result = _composite_score(articles)

        return {
            "composite_score": result["composite_score"],   # -5 ~ +5
            "direction": result["direction"],
            "positive": result.get("positive_count", 0),
            "negative": result.get("negative_count", 0),
            "neutral": result.get("neutral_count", 0),
            "veto": result.get("veto", False),
            "veto_count": 1 if result.get("veto") else 0,
        }


# 全局缓存管理器单例
_news_cache_manager: Optional[NewsCacheManager] = None


def get_news_cache_manager() -> NewsCacheManager:
    """获取新闻缓存管理器单例"""
    global _news_cache_manager
    if _news_cache_manager is None:
        _news_cache_manager = NewsCacheManager()
    return _news_cache_manager


# ═══════════════════════════════════════════════════════════════
# 统一新闻搜索 (个股 + 市场)
# ═══════════════════════════════════════════════════════════════

def _is_stock_code(symbol: str) -> bool:
    """判断 symbol 是股票代码还是市场名"""
    if not symbol:
        return False
    # 纯数字 (A股/港股)、含点的代码 (BRK.A)、1-5位字母+数字混合 (AAPL, TSLA)
    if symbol in ("CNStock", "USStock", "Crypto", "Forex", "HKStock", "Futures"):
        return False
    return True


def search_news(
    symbol: str,
    market: str = "CNStock",
    lang: str = "all",
    days: int = 3,
    max_web_results: int = 5,
    is_watchlist: bool = False,
    name: str = "",
) -> SearchResponse:
    """
    统一新闻搜索 — 个股 + 市场, 带缓存 + 评分

    内部按 symbol 类型自动选数据源:
      symbol 是股票代码 → StockNews 5路 (A股) + Web (按 lang 选中/英文)
      symbol 是市场名   → StockNews(A股) + Web (按 lang 选中/英文)

    Args:
        symbol:    股票代码 ("600519", "AAPL") 或 市场名 ("CNStock", "USStock")
        market:    市场标识
        lang:      "cn"=仅中文源 | "en"=仅英文源 | "all"=全部
        days:      搜索天数 (仅个股, 市场固定1天)
        max_web_results: Web 搜索最大条数
        is_watchlist: 是否自选股 (启用时间窗口策略)
        name:      股票名称 (仅个股, 提升搜索精度)
    """
    start_time = time.time()
    is_stock = _is_stock_code(symbol)
    cache_mgr = get_news_cache_manager()

    # ── 动态缩减搜索天数 (仅个股) ──
    if is_stock:
        effective_days = cache_mgr.calc_dynamic_days(symbol, market, default_days=days)
    else:
        effective_days = 1  # 市场新闻固定1天

    # ── 缓存检查 ──
    should, reason = cache_mgr.should_search(symbol, market, is_watchlist and is_stock)

    if not should:
        items = cache_mgr.get_items(symbol, market)
        if items:
            score_info = cache_mgr.calc_score(symbol, market)
            results = [
                SearchResult(
                    title=i["title"], snippet=i.get("snippet", ""),
                    url=i.get("url", ""), source=i.get("source", ""),
                    published_date=i.get("published_date", ""),
                    sentiment=i.get("sentiment", "neutral"),
                    sentiment_score=i.get("sentiment_score"),
                ) for i in items
            ]
            score_info["from_cache"] = True
            provider = f"Cache({score_info['direction']} {score_info['composite_score']}/10)"
            label = f"{symbol}({market})" if is_stock else f"市场:{market}"
            logger.info(f"[缓存命中] {label}: {reason}")
            return SearchResponse(
                query=f"{symbol} {name}".strip() if is_stock else f"市场新闻({market})",
                results=results, provider=provider,
                success=True, search_time=round(time.time() - start_time, 2),
                metadata=score_info,
            )
        logger.info(f"[缓存降级] {symbol}({market}): {reason}, 但DB无数据, 重新搜索")

    label = f"{symbol}({market})" if is_stock else f"市场:{market}"
    logger.info(f"[需要搜索] {label}: {reason}")

    # ═══════════════════════════════════════════════
    # 搜索: 按 symbol 类型 + lang 选择数据源
    # ═══════════════════════════════════════════════
    search_svc = get_search_service()
    source_results: List[SearchResult] = []  # 优先源 (StockNews)
    web_results: List[SearchResult] = []     # Web 补充

    def _title_key(r: SearchResult) -> str:
        norm = re.sub(r'[\s\u3000\uff0c\u3001\u3002\uff01\uff1f\u2014]', '', r.title)
        return hashlib.md5(norm.encode()).hexdigest()[:16]

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {}

        if is_stock:
            # ── 个股: Web 搜索 ──
            if lang in ("cn", "all"):
                cn_q = f"{name} {symbol} A股新闻" if name else f"{symbol} 股票新闻"
                futures[pool.submit(search_svc.search_with_fallback, cn_q, max_web_results, effective_days)] = "cn_web"
            if lang in ("en", "all"):
                en_q = f"{name or symbol} stock news analysis" if name else f"{symbol} stock financial news"
                futures[pool.submit(search_svc.search_with_fallback, en_q, max_web_results, effective_days)] = "en_web"

        else:
            # ── 市场: Web 搜索 (按 lang 选查询词) ──
            cn_query_map = {
                "USStock": "美股市场新闻 股票",
                "Crypto": "加密货币新闻 比特币",
                "Forex": "外汇市场分析 汇率",
                "CNStock": "A股市场 财经新闻 股票",
            }
            en_query_map = {
                "USStock": "US stock market news today",
                "Crypto": "cryptocurrency market news bitcoin",
                "Forex": "forex market analysis news",
                "CNStock": "China A-share stock market news",
            }
            if lang in ("cn", "all"):
                cn_q = cn_query_map.get(market, f"{market} 市场新闻")
                futures[pool.submit(search_svc.search_with_fallback, cn_q, max_web_results, effective_days)] = "cn_web"
            if lang in ("en", "all"):
                en_q = en_query_map.get(market, f"{market} market news")
                futures[pool.submit(search_svc.search_with_fallback, en_q, max_web_results, effective_days)] = "en_web"

        # ── 收集结果 ──
        for future in as_completed(futures):
            src = futures[future]
            try:
                resp = future.result()
                if src == "StockNews":
                    source_results.extend(resp.results if isinstance(resp, SearchResponse) else [])
                elif src == "cn_news":
                    for item in (resp or []):
                        source_results.append(SearchResult(
                            title=item.get("title", ""), snippet=item.get("title", ""),
                            url=item.get("url", ""), source=item.get("source", "财经"),
                            published_date=item.get("time", ""),
                        ))
                else:
                    if isinstance(resp, SearchResponse) and resp.success:
                        web_results.extend(resp.results)
            except Exception as e:
                logger.warning(f"{src} 搜索异常: {e}")

    # ── 合并去重: 优先源 > Web ──
    seen: set = set()
    merged: List[SearchResult] = []
    for r in source_results:
        k = _title_key(r)
        if k not in seen:
            seen.add(k)
            merged.append(r)

    web_added = 0
    for r in web_results:
        k = _title_key(r)
        if k not in seen:
            seen.add(k)
            merged.append(r)
            web_added += 1

    merged.sort(key=lambda r: r.published_date or "", reverse=True)

    # ── 写入缓存 ──
    if merged:
        cache_mgr.save_items(symbol, market, merged)

    # ── 构造返回 ──
    elapsed = round(time.time() - start_time, 2)
    metadata = {
        "source_count": len(source_results),
        "web_results_added": web_added,
        "total_merged": len(merged),
        "from_cache": False,
        "effective_days": effective_days,
        "is_stock": is_stock,
    }
    # 个股附加 StockNews 评分元数据
    if is_stock and source_results:
        # 从 StockNews 的 metadata 取评分 (如果有)
        pass  # 评分已在 save_items 里完成, calc_score 在缓存读取时返回

    provider = "StockNews+Web" if is_stock else ("NewsFetcher+Web" if source_results else "Web")
    return SearchResponse(
        query=f"{symbol} {name}".strip() if is_stock else f"市场新闻({market})",
        results=merged, provider=provider,
        success=len(merged) > 0, search_time=elapsed, metadata=metadata,
    )


# ── 向后兼容别名 ──
def search_cn_stock_news(
    stock_code: str, stock_name: str = "",
    days: int = 3, max_web_results: int = 5,
    market: str = "CNStock", is_watchlist: bool = False,
    lang: str = "all",
) -> SearchResponse:
    """兼容旧调用 → 委托 search_news"""
    return search_news(
        symbol=stock_code, market=market, lang=lang,
        days=days, max_web_results=max_web_results,
        is_watchlist=is_watchlist, name=stock_name,
    )


def search_market_news(
    market: str = "CNStock", symbol: str = "",
    days: int = 1, max_web_results: int = 5,
    max_fetcher_per_source: int = 10,
    lang: str = "all",
) -> SearchResponse:
    """兼容旧调用 → 委托 search_news"""
    return search_news(
        symbol=symbol or market, market=market, lang=lang,
        days=days, max_web_results=max_web_results,
    )


# ═══════════════════════════════════════════════════════════════
# 政策/宏观新闻 (独立路由, 非股市)
# ═══════════════════════════════════════════════════════════════

# ── 政策/宏观专用搜索关键词 (按市场区分) ──
_POLICY_QUERIES: Dict[str, Dict[str, List[str]]] = {
    "CNStock": {
        "cn": [
            "国务院政策 最新",
            "宏观经济分析 GDP CPI PPI",
            "央行货币政策 降准 降息 LPR",
            "财政政策 减税 专项债",
            "产业政策 新能源 芯片 制造业",
            "经济数据 社融 M2 进出口",
        ],
        "en": [
            "China economic policy latest",
            "China central bank monetary policy",
            "China fiscal policy stimulus GDP",
            "China industry policy manufacturing",
        ],
    },
    "USStock": {
        "cn": [
            "美联储货币政策 利率决议",
            "美国经济数据 非农 CPI GDP",
            "美国财政政策 减税 刺激计划",
            "美国贸易政策 关税 制裁",
            "美国产业政策 芯片法案 科技监管",
        ],
        "en": [
            "Federal Reserve interest rate decision",
            "US economic data nonfarm CPI GDP",
            "US fiscal policy tax cut stimulus",
            "US trade policy tariff sanctions",
            "US industrial policy CHIPS act regulation",
        ],
    },
    "Crypto": {
        "cn": [
            "加密货币监管政策 各国",
            "数字货币政策 央行数字货币",
            "区块链监管 Web3 政策",
        ],
        "en": [
            "cryptocurrency regulation policy",
            "digital currency CBDC policy",
            "crypto regulation Web3 government",
        ],
    },
    "Forex": {
        "cn": [
            "外汇政策 央行干预 汇率",
            "货币政策 利率决议 各国央行",
            "国际贸易收支 外汇储备",
        ],
        "en": [
            "forex policy central bank intervention",
            "monetary policy interest rate decision",
            "trade balance foreign exchange reserves",
        ],
    },
}


def _fetch_policy_news(
    market: str = "CNStock",
    days: int = 1,
    max_per_query: int = 5,
) -> Dict[str, List[Dict[str, Any]]]:
    """
    政策/宏观新闻 — 按市场区分, 带缓存 + AI 评分

    与市场新闻的区别:
      - 市场新闻: 行情、板块轮动、个股涨跌、资金流向
      - 政策/宏观: 央行政策、财政政策、产业规划、经济数据、监管动态

    Args:
        market: CNStock=中国政策, USStock=美国政策, Crypto=加密监管, Forex=外汇政策
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    cache_mgr = get_news_cache_manager()
    result: Dict[str, List[Dict[str, Any]]] = {"cn": [], "en": []}

    market_queries = _POLICY_QUERIES.get(market, _POLICY_QUERIES["CNStock"])

    # ── 缓存检查 ──
    should, reason = cache_mgr.should_search("POLICY", market, is_watchlist=False)
    if not should:
        cached = cache_mgr.get_items("POLICY", market)
        if cached:
            for item in cached:
                lang_key = "cn"  # 政策新闻默认中文, 英文结果较少
                result[lang_key].append({
                    "title": item["title"], "link": item.get("url", ""),
                    "snippet": item.get("snippet", ""), "source": item.get("source", ""),
                    "published": item.get("published_date", ""),
                    "sentiment": item.get("sentiment", "neutral"),
                    "sentiment_score": item.get("sentiment_score"),
                    "category": f"政策/宏观:{market}", "lang": lang_key,
                })
            logger.info(f"[政策新闻缓存命中] {market} {reason}, {len(cached)}条")
            return result
        logger.info(f"[政策新闻缓存降级] {market} {reason}, 但无数据, 重新搜索")

    logger.info(f"[政策新闻] {market} 缓存未命中, 并行搜索")

    # ── Web 搜索 ──
    search = get_search_service()
    all_results: List[SearchResult] = []

    queries = []
    for lang_key in ("cn", "en"):
        for q in market_queries.get(lang_key, []):
            queries.append((lang_key, q))

    def _do_search(lang_key: str, query: str) -> List[SearchResult]:
        try:
            resp = search.search_with_fallback(query, max_per_query, days)
            return resp.results if resp.success else []
        except Exception:
            return []

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_do_search, lk, q): (lk, q) for lk, q in queries}
        for future in as_completed(futures):
            lk, q = futures[future]
            try:
                items = future.result()
                all_results.extend(items)
            except Exception as e:
                logger.warning(f"政策新闻搜索异常({q}): {e}")

    # ── 去重 ──
    seen: set = set()
    deduped: List[SearchResult] = []
    for r in all_results:
        norm = re.sub(r'[\s\u3000]', '', r.title)
        key = hashlib.md5(norm.encode()).hexdigest()[:16]
        if key not in seen:
            seen.add(key)
            deduped.append(r)

    # ── 写缓存 (save_items 自动触发 AI 评分: get_news_type="policy") ──
    if deduped:
        cache_mgr.save_items("POLICY", market, deduped)

    # ── 构造返回 (从缓存重新读取, 带评分) ──
    saved = cache_mgr.get_items("POLICY", market)
    for item in saved:
        lang_key = "cn"
        result[lang_key].append({
            "title": item["title"], "link": item.get("url", ""),
            "snippet": item.get("snippet", ""), "source": item.get("source", ""),
            "published": item.get("published_date", ""),
            "sentiment": item.get("sentiment", "neutral"),
            "sentiment_score": item.get("sentiment_score"),
            "category": f"政策/宏观:{market}", "lang": lang_key,
        })

    logger.info(f"[政策新闻] {market} 搜索完成, {len(deduped)}条去重, 评分后{len(saved)}条入库")
    return result


# ═══════════════════════════════════════════════════════════════
# 通用财经新闻
# ═══════════════════════════════════════════════════════════════

def _fetch_general_news_cached(
    lang: str = "all",
) -> Dict[str, List[Dict[str, Any]]]:
    """
    通用财经新闻 — 带缓存 + 情感评分

    流程:
      1. 查 NewsCacheManager 缓存 (symbol=GENERAL_NEWS, 24h 去重)
      2. 缓存命中 → 直接返回
      3. 缓存未命中 → 并行搜索中英文关键词 → 关键词评分 → 写缓存 → 返回
    """
    from app.services.news_analysis import keyword_score_article
    from concurrent.futures import ThreadPoolExecutor, as_completed

    cache_mgr = get_news_cache_manager()
    result: Dict[str, List[Dict[str, Any]]] = {"cn": [], "en": []}

    # ── 缓存检查 ──
    should, reason = cache_mgr.should_search("GENERAL_NEWS", "General", is_watchlist=False)
    if not should:
        cached_cn = cache_mgr.get_items("GENERAL_NEWS", "CN")
        cached_en = cache_mgr.get_items("GENERAL_NEWS", "EN")
        if cached_cn or cached_en:
            for item in cached_cn:
                result["cn"].append({
                    "title": item["title"], "link": item.get("url", ""),
                    "snippet": item.get("snippet", ""), "source": item.get("source", ""),
                    "published": item.get("published_date", ""),
                    "sentiment": item.get("sentiment", "neutral"),
                    "sentiment_score": item.get("sentiment_score"),
                    "category": "通用", "lang": "cn",
                })
            for item in cached_en:
                result["en"].append({
                    "title": item["title"], "link": item.get("url", ""),
                    "snippet": item.get("snippet", ""), "source": item.get("source", ""),
                    "published": item.get("published_date", ""),
                    "sentiment": item.get("sentiment", "neutral"),
                    "sentiment_score": item.get("sentiment_score"),
                    "category": "通用", "lang": "en",
                })
            if result["cn"] or result["en"]:
                logger.info(f"[通用新闻缓存命中] {reason}")
                return result
        # 缓存为空, 降级搜索
        logger.info(f"[通用新闻缓存降级] {reason}, 但无数据, 重新搜索")

    logger.info(f"[通用新闻] 缓存未命中, 并行搜索中英文")

    # ── 搜索关键词 ──
    cn_queries = [
        "加密货币新闻", "美联储利率", "美股市场最新消息",
        "外汇市场分析", "全球经济数据", "期货市场动态",
    ]
    en_queries = [
        "stock market news today", "cryptocurrency bitcoin news",
        "forex market analysis", "federal reserve interest rate",
        "global economic outlook", "S&P 500 market update",
    ]

    search = SearchService()

    def _search_and_score(query: str, num: int = 5) -> List[SearchResult]:
        """搜索 + 关键词评分, 返回 SearchResult 列表"""
        try:
            raw = search.search(query, num_results=num, date_restrict="d1")
        except Exception:
            return []
        items: List[SearchResult] = []
        for r in raw:
            title = r.get("title", "")
            snippet = r.get("snippet", "")
            kw = keyword_score_article(title, snippet)
            items.append(SearchResult(
                title=title, snippet=snippet,
                url=r.get("link", ""), source=r.get("source", ""),
                published_date=r.get("published", ""),
                sentiment=kw["sentiment"],
                sentiment_score=kw["score"],
            ))
        return items

    # ── 并行搜索 ──
    cn_results: List[SearchResult] = []
    en_results: List[SearchResult] = []

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {}
        if lang in ("all", "cn"):
            for q in cn_queries:
                futures[pool.submit(_search_and_score, q, 5)] = ("cn", q)
        if lang in ("all", "en"):
            for q in en_queries:
                futures[pool.submit(_search_and_score, q, 5)] = ("en", q)

        for future in as_completed(futures):
            lang_key, q = futures[future]
            try:
                items = future.result()
                if lang_key == "cn":
                    cn_results.extend(items)
                else:
                    en_results.extend(items)
            except Exception as e:
                logger.warning(f"通用新闻搜索异常({q}): {e}")

    # ── 去重 + 填充 ──
    def _dedup_and_fill(items: List[SearchResult], lang_key: str):
        seen: set = set()
        for r in items:
            if r.url and r.url not in seen:
                seen.add(r.url)
                result[lang_key].append({
                    "title": r.title, "link": r.url,
                    "snippet": r.snippet, "source": r.source,
                    "published": r.published_date or "",
                    "sentiment": r.sentiment,
                    "sentiment_score": r.sentiment_score,
                    "category": "通用", "lang": lang_key,
                })
        result[lang_key] = result[lang_key][:15]

    _dedup_and_fill(cn_results, "cn")
    _dedup_and_fill(en_results, "en")

    # ── 写入缓存 ──
    if cn_results:
        cache_mgr.save_items("GENERAL_NEWS", "CN", cn_results)
    if en_results:
        cache_mgr.save_items("GENERAL_NEWS", "EN", en_results)

    return result


def _results_to_list(
    results: List[SearchResult],
    lang: str,
    category: str,
) -> List[Dict[str, Any]]:
    """SearchResult → 统一 dict 格式 (含情感评分)"""
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
    """根据结果内容推断实际语言 (简单启发: 中文字符占比)"""
    sample = " ".join(r.title for r in results[:5])
    if not sample:
        return default
    cn_chars = len(re.findall(r'[\u4e00-\u9fff]', sample))
    total = len(sample) or 1
    return "cn" if cn_chars / total > 0.15 else "en"


def fetch_financial_news(
    lang: str = "all",
    market: str = "all",
    symbol: str = "",
    name: str = "",
) -> Dict[str, List[Dict[str, Any]]]:
    """
    财经新闻统一入口 v2.1 — 统一 search_news 路由

    路由规则 (严格按条件匹配):
      0. symbol=="POLICY"              → 政策/宏观新闻, 按 market 区分国家/市场
      1. symbol 有值 + symbol == market → 市场新闻 (如 symbol=CNStock, market=CNStock)
      2. symbol 有值 + symbol != market → 个股新闻 (如 symbol=600519, market=CNStock)
      3. market != "all", 无 symbol    → 市场新闻 (默认该市场)
      4. 都没有                        → 通用财经新闻

    所有路径统一返回格式:
      {"cn": [...], "en": [...]}
      每条: {title, link, snippet, source, published, sentiment, sentiment_score, category, lang}
    """
    result: Dict[str, List[Dict[str, Any]]] = {"cn": [], "en": []}

    try:
        effective_market = market if market != "all" else "CNStock"

        # ── 规范化: API 层 MacroCN/MacroIntl 映射为 POLICY 路由 ──
        _MACRO_MARKET_MAP = {
            "MacroCN": "CNStock",
            "MacroIntl": "USStock",
        }
        if market in _MACRO_MARKET_MAP:
            symbol = "POLICY"
            effective_market = _MACRO_MARKET_MAP[market]

        # ═══════════════════════════════════════════════
        # 路由 0: 政策/宏观新闻 (symbol == POLICY)
        # ═══════════════════════════════════════════════
        if symbol == "POLICY":
            return _fetch_policy_news(market=effective_market)

        # ═══════════════════════════════════════════════
        # 路由 1: 市场新闻 (symbol == market, 如 symbol=CNStock, market=CNStock)
        # ═══════════════════════════════════════════════
        if symbol and symbol == market:
            resp = search_news(
                symbol=market, market=market, lang=lang,
                days=1, max_web_results=10,
            )
            if resp.success:
                actual_lang = _detect_lang(resp.results, "cn" if market == "CNStock" else "en")
                target_lang = lang if lang != "all" else actual_lang
                items = _results_to_list(resp.results, target_lang, f"市场:{market}")
                result[target_lang].extend(items)
            return result

        # ═══════════════════════════════════════════════
        # 路由 2: 个股新闻 (symbol 有值 且 symbol != market)
        # ═══════════════════════════════════════════════
        if symbol:
            resp = search_news(
                symbol=symbol, market=effective_market, lang=lang,
                days=3, max_web_results=5, name=name or "",
            )
            if resp.success:
                actual_lang = _detect_lang(resp.results, "cn" if effective_market == "CNStock" else "en")
                target_lang = lang if lang != "all" else actual_lang
                items = _results_to_list(resp.results, target_lang, f"个股:{symbol}")
                result[target_lang].extend(items)

                # 附加评分元数据
                score_info = resp.metadata
                if score_info:
                    result["_meta"] = {
                        "composite_score": score_info.get("composite_score", 0),
                        "direction": score_info.get("direction", "中性"),
                        "from_cache": score_info.get("from_cache", False),
                        "total": len(resp.results),
                    }
            return result

        # ═══════════════════════════════════════════════
        # 路由 3: 市场新闻 (market 有值, 无 symbol, 默认该市场)
        # ═══════════════════════════════════════════════
        if market != "all":
            resp = search_news(
                symbol=market, market=market, lang=lang,
                days=1, max_web_results=10,
            )
            if resp.success:
                actual_lang = _detect_lang(resp.results, "cn" if market == "CNStock" else "en")
                target_lang = lang if lang != "all" else actual_lang
                items = _results_to_list(resp.results, target_lang, f"市场:{market}")
                result[target_lang].extend(items)
            return result

        # ═══════════════════════════════════════════════
        # 路由 4: 通用财经新闻 (无 symbol, 无 market)
        # ═══════════════════════════════════════════════
        return _fetch_general_news_cached(lang=lang)

    except Exception as e:
        logger.error("Failed to fetch financial news: %s", e)

    return result


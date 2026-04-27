"""
Financial news data provider — 新闻数据生命周期管理

职责:
1. 新闻缓存 (PostgreSQL): 24h去重, 15天过期, 自选股时间窗口
2. 情感评分: 加权求和 + 时间衰减 + 重大负面一票否决
3. 市场新闻 vs 个股新闻 区分:
   - 市场新闻: news_fetcher.py 直爬 (A股优先) + Web 补充, 预定义 symbol
   - 个股新闻: stock_news.py 5路源 + Web 并行, symbol=股票代码
4. 通用财经新闻: fetch_financial_news() 按语言分类
5. 经济日历: get_economic_calendar() 模板化事件

数据流:
  市场新闻 → news_fetcher.py (A股直爬, 优先) + search.py (Web 补充)
  个股新闻 → stock_news.py (5路源) + search.py (Web 补充)
  上层调用 → policy_analysis_ai.py / indicator_review.py → 本模块
"""
from __future__ import annotations

import hashlib
import re
import random
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
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
MARKET_NEWS_SYMBOLS = {
    "CNStock":  "MARKET_CN",       # A股市场综合新闻
    "USStock":  "MARKET_US",       # 美股市场综合新闻
    "Crypto":   "MARKET_CRYPTO",   # 加密货币市场新闻
    "Forex":    "MARKET_FOREX",    # 外汇市场新闻
    "Futures":  "MARKET_FUTURES",  # 期货市场新闻
}


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

# ── 重大负面新闻一票否决关键词 (命中任一 → sentiment_score = -999) ──
_VETO_KEYWORDS = {
    "跌停", "一字跌停", "连续跌停", "天地板",
    "闪崩", "崩盘",
    "历史新低",
    "净利大跌",
    "业绩暴雷", "暴雷", "业绩变脸",
    "巨亏", "大幅亏损", "由盈转亏",
    "商誉减值",
    "财务造假",
    "清仓",
    "大股东减持", "违规减持",
    "破产", "清算",
    "质量事故", "安全事故",
    "监管调查", "立案调查", "违法",
    "退市", "暂停上市",
    "债务危机", "债务违约", "资金链断裂",
    "巨额索赔",
    "重大利空", "黑天鹅",
}


def _check_veto(title: str, snippet: str = "") -> bool:
    """检查是否触发重大负面一票否决"""
    text = f"{title} {snippet}"
    for kw in _VETO_KEYWORDS:
        if kw in text:
            return True
    return False


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
        - 写入前强制 utf-8 编码清洗
        - 每条先做情感评分 (sentiment + sentiment_score)
        - 重大负面一票否决 → sentiment_score = -999
        """
        if not symbol or not market or not results:
            return False
        try:
            with get_db_connection() as conn:
                self._purge_expired(conn)
                cursor = conn.cursor()
                rows = []
                for r in results:
                    title = _safe_encode(r.title, 500)
                    snippet = _safe_encode(r.snippet, 2000)
                    url = _safe_encode(r.url, 1000)
                    source = _safe_encode(r.source, 100)
                    pub_date = _safe_encode(r.published_date or '', 40)
                    sentiment = _safe_encode(r.sentiment, 20)

                    # 计算数值评分
                    score = r.sentiment_score
                    if score is None:
                        score = {"positive": 7.5, "negative": 2.5, "neutral": 5.0}.get(sentiment, 5.0)

                    # 重大负面一票否决检测
                    if _check_veto(title, snippet):
                        score = -999.0
                        sentiment = "negative"
                        logger.warning(f"[一票否决] 检测到重大负面: {title[:60]}")

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
                logger.info(f"新闻缓存已保存: {symbol}({market}) {len(results)}条")
                return True
        except Exception as e:
            logger.error(f"保存新闻缓存异常: {e}")
            return False

    @staticmethod
    def calc_score(items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        从新闻列表实时计算综合评分 (复用 stock_news.py 的衰减逻辑)

        一票否决规则:
        - 任一条 sentiment_score == -999 → 直接参与加权求和
        - -999 乘以权重后, 不管多少篇正面文章, 累计分永远 < 0
        """
        if not items:
            return {"composite_score": 5.0, "direction": "中性",
                    "positive": 0, "negative": 0, "neutral": 0}

        now = datetime.now()
        pos = neg = neu = 0
        total_weighted = 0.0
        total_weight = 0.0
        has_veto = False

        for item in items:
            s = item.get("sentiment", "neutral")
            raw_score = item.get("sentiment_score", None)

            if s == "positive":
                pos += 1
            elif s == "negative":
                neg += 1
            else:
                neu += 1

            # 一票否决检测
            if raw_score is not None and float(raw_score) == -999.0:
                has_veto = True

            # 时间衰减
            pub = item.get("published_date", "")
            try:
                pub_dt = datetime.fromisoformat(pub) if pub else now
                hours = max(0, (now - pub_dt).total_seconds() / 3600)
            except (ValueError, TypeError):
                hours = 24
            decay = 0.8 ** (hours / 24)
            boost = 1.5 if s != "neutral" else 1.0
            weight = decay * boost

            # -999 直接参与加权, 确保累计分 < 0
            if raw_score is not None and float(raw_score) == -999.0:
                score = -999.0
            else:
                score = float(raw_score) if raw_score is not None else \
                    {"positive": 7.5, "negative": 2.5, "neutral": 5.0}.get(s, 5.0)

            total_weighted += score * weight
            total_weight += weight

        # 一票否决: 任何重大负面 → 综合分 < 0
        if has_veto:
            veto_score = round(total_weighted / total_weight, 1) if total_weight > 0 else -999.0
            return {"composite_score": veto_score, "direction": "重大利空",
                    "positive": pos, "negative": neg, "neutral": neu,
                    "veto": True}

        composite = round(total_weighted / total_weight, 1) if total_weight > 0 else 5.0
        composite = max(0.0, min(10.0, composite))

        if composite >= 7:
            direction = "利好"
        elif composite <= 3:
            direction = "利空"
        elif composite >= 6:
            direction = "偏利好"
        elif composite <= 4:
            direction = "偏利空"
        else:
            direction = "中性"

        return {"composite_score": composite, "direction": direction,
                "positive": pos, "negative": neg, "neutral": neu}


# 全局缓存管理器单例
_news_cache_manager: Optional[NewsCacheManager] = None


def get_news_cache_manager() -> NewsCacheManager:
    """获取新闻缓存管理器单例"""
    global _news_cache_manager
    if _news_cache_manager is None:
        _news_cache_manager = NewsCacheManager()
    return _news_cache_manager


# ═══════════════════════════════════════════════════════════════
# A股个股新闻搜索 (带缓存)
# ═══════════════════════════════════════════════════════════════

def _fetch_stock_news_search(query: str, max_results: int = 20, days: int = 7) -> SearchResponse:
    """调用 stock_news.py 获取A股个股新闻 (5路数据源)"""
    try:
        from app.services.stock_news import fetch_stock_news
        parts = query.strip().split(None, 1)
        stock_code = parts[0]
        stock_name = parts[1] if len(parts) > 1 else ""
        resp = fetch_stock_news(
            stock_code=stock_code, days=min(days, 30),
            stock_name=stock_name, max_results=max_results or 20,
        )
        direction = resp.metadata.get("direction", "")
        score = resp.metadata.get("composite_score", "")
        resp.provider = f"StockNews({direction} {score}/10)" if direction else "StockNews"
        return resp
    except Exception as e:
        return SearchResponse(query=query, results=[], provider="StockNews",
                              success=False, error_message=str(e))


def search_cn_stock_news(
    stock_code: str, stock_name: str = "",
    days: int = 3, max_web_results: int = 5,
    market: str = "CNStock", is_watchlist: bool = False,
) -> SearchResponse:
    """
    A股个股新闻搜索 — 带 PostgreSQL 持久化缓存 v2.2

    缓存策略:
      1. 查 DB 判断是否需要搜索 (24h去重 / 自选股时间窗口)
      2. 需要搜索 → 并行 Web + StockNews → 写入明细表
      3. 不需要搜索 → 从明细表取 items → 实时算评分返回
      4. 无结果不写库, 下次重新搜索
      5. 重大负面一票否决 → sentiment_score=-999 → 综合分归零

    外部接口参数保持不变
    """
    start_time = time.time()
    query = f"{stock_code} {stock_name}".strip()
    search_svc = get_search_service()

    # ── 第零步: 动态缩减搜索天数 ──
    cache_mgr = get_news_cache_manager()
    effective_days = cache_mgr.calc_dynamic_days(stock_code, market, default_days=days)

    # ── 第一步: 判断是否需要搜索 ──
    should, reason = cache_mgr.should_search(stock_code, market, is_watchlist)

    if not should:
        # 从缓存取 items, 实时算评分
        items = cache_mgr.get_items(stock_code, market)
        if items:
            score_info = cache_mgr.calc_score(items)
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
            logger.info(f"[缓存命中] {stock_code}({market}) {stock_name}: {reason}")
            return SearchResponse(
                query=query, results=results, provider=provider,
                success=True, search_time=round(time.time() - start_time, 2),
                metadata=score_info,
            )
        # DB说不搜但实际没数据 (可能数据被手动删了), 降级为搜索
        logger.info(f"[缓存降级] {stock_code}({market}): {reason}, 但DB无数据, 重新搜索")

    logger.info(f"[需要搜索] {stock_code}({market}) {stock_name}: {reason}")

    # ── 第二步: 并行搜索 ──
    def _title_key(r: SearchResult) -> str:
        norm = re.sub(r'[\s\u3000\uff0c\u3001\u3002\uff01\uff1f\u2014]', '', r.title)
        return hashlib.md5(norm.encode()).hexdigest()[:16]

    web_query = f"{stock_name} {stock_code} A股新闻" if stock_name else f"{stock_code} 股票新闻"

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_web = pool.submit(search_svc.search_with_fallback, web_query, max_web_results, effective_days)
        f_news = pool.submit(_fetch_stock_news_search, query, 20, effective_days)

        try:
            web_resp = f_web.result()
        except Exception as e:
            logger.warning(f"Web搜索异常: {e}")
            web_resp = SearchResponse(query=web_query, results=[], provider="Web",
                                      success=False, error_message=str(e))
        try:
            news_resp = f_news.result()
        except Exception as e:
            logger.warning(f"StockNews搜索异常: {e}")
            news_resp = SearchResponse(query=query, results=[], provider="StockNews",
                                       success=False, error_message=str(e))

    # 合并去重: stock_news 优先
    seen = set()
    merged: List[SearchResult] = []
    for r in news_resp.results:
        k = _title_key(r)
        if k not in seen:
            seen.add(k)
            merged.append(r)

    web_added = 0
    if web_resp.success:
        for r in web_resp.results:
            k = _title_key(r)
            if k not in seen:
                seen.add(k)
                merged.append(r)
                web_added += 1

    merged.sort(key=lambda r: r.published_date or "", reverse=True)

    # ── 第三步: 写入缓存 ──
    if merged:
        cache_mgr.save_items(stock_code, market, merged)

    elapsed = round(time.time() - start_time, 2)
    metadata = dict(news_resp.metadata) if news_resp.metadata else {}
    metadata["web_results_added"] = web_added
    metadata["stock_news_count"] = len(news_resp.results)
    metadata["total_merged"] = len(merged)
    metadata["from_cache"] = False
    metadata["effective_days"] = effective_days

    return SearchResponse(
        query=query, results=merged, provider="StockNews+Web",
        success=len(merged) > 0, search_time=elapsed, metadata=metadata,
    )


# ═══════════════════════════════════════════════════════════════
# 市场新闻 (news_fetcher.py 直爬, A股优先)
# ═══════════════════════════════════════════════════════════════
#
# 市场新闻 vs 个股新闻:
#   市场新闻 → symbol = MARKET_NEWS_SYMBOLS[market] (预定义)
#   个股新闻 → symbol = 股票代码 (search_cn_stock_news)
#
# A股市场新闻优先使用 news_fetcher.py 直爬 (5路数据源, 无需搜索引擎)
# 其他市场走 search_with_fallback
#

def search_market_news(
    market: str = "CNStock",
    days: int = 1, max_web_results: int = 5,
    max_fetcher_per_source: int = 10,
) -> SearchResponse:
    """
    市场新闻搜索 — 带缓存, 区分数据源

    - A股: news_fetcher.py 直爬 (优先) + Web 补充
    - 其他市场: Web 搜索

    缓存 symbol 使用 MARKET_NEWS_SYMBOLS 中的预定义值
    """
    start_time = time.time()
    symbol = MARKET_NEWS_SYMBOLS.get(market, f"MARKET_{market}")
    cache_mgr = get_news_cache_manager()

    # ── 判断是否需要搜索 ──
    should, reason = cache_mgr.should_search(symbol, market, is_watchlist=False)

    if not should:
        items = cache_mgr.get_items(symbol, market)
        if items:
            score_info = cache_mgr.calc_score(items)
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
            return SearchResponse(
                query=f"市场新闻({market})", results=results, provider=provider,
                success=True, search_time=round(time.time() - start_time, 2),
                metadata=score_info,
            )

    logger.info(f"[市场新闻] {symbol}({market}): {reason}")

    # ── 数据源: A股用 news_fetcher 直爬, 其他走 Web ──
    fetcher_results: List[SearchResult] = []
    web_results: List[SearchResult] = []

    if market == "CNStock":
        # A股: news_fetcher 直爬 (并行)
        def _do_fetch():
            from app.services.cn_news_provider import fetch_all_news
            return fetch_all_news(max_per_source=max_fetcher_per_source)

        with ThreadPoolExecutor(max_workers=2) as pool:
            f_fetcher = pool.submit(_do_fetch)
            f_web = pool.submit(
                get_search_service().search_with_fallback,
                "A股市场 财经新闻 股票", max_web_results, days,
            )
            try:
                raw_fetcher = f_fetcher.result()
                for item in raw_fetcher:
                    fetcher_results.append(SearchResult(
                        title=item.get("title", ""),
                        snippet=item.get("title", ""),
                        url=item.get("url", ""),
                        source=item.get("source", "财经"),
                        published_date=item.get("time", ""),
                    ))
            except Exception as e:
                logger.warning(f"news_fetcher 异常: {e}")

            try:
                web_resp = f_web.result()
                web_results = web_resp.results if web_resp.success else []
            except Exception as e:
                logger.warning(f"Web搜索异常: {e}")
    else:
        # 其他市场: Web 搜索
        query_map = {
            "USStock": "US stock market news today",
            "Crypto": "cryptocurrency market news bitcoin",
            "Forex": "forex market analysis news",
        }
        query = query_map.get(market, f"{market} market news")
        try:
            web_resp = get_search_service().search_with_fallback(query, max_web_results, days)
            web_results = web_resp.results if web_resp.success else []
        except Exception as e:
            logger.warning(f"Web搜索异常: {e}")

    # ── 合并去重: news_fetcher 优先 ──
    seen = set()
    merged: List[SearchResult] = []

    def _title_key(r: SearchResult) -> str:
        norm = re.sub(r'[\s\u3000\uff0c\u3001\u3002\uff01\uff1f\u2014]', '', r.title)
        return hashlib.md5(norm.encode()).hexdigest()[:16]

    for r in fetcher_results:
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

    elapsed = round(time.time() - start_time, 2)
    metadata = {
        "fetcher_count": len(fetcher_results),
        "web_results_added": web_added,
        "total_merged": len(merged),
        "from_cache": False,
        "symbol": symbol,
    }

    return SearchResponse(
        query=f"市场新闻({market})", results=merged,
        provider="NewsFetcher+Web" if fetcher_results else "Web",
        success=len(merged) > 0, search_time=elapsed, metadata=metadata,
    )


# ═══════════════════════════════════════════════════════════════
# 通用财经新闻
# ═══════════════════════════════════════════════════════════════

def fetch_financial_news(lang: str = "all") -> Dict[str, List[Dict[str, Any]]]:
    """Fetch financial news using search service — separated by language."""
    result: Dict[str, List[Dict[str, Any]]] = {"cn": [], "en": []}

    try:
        search = SearchService()

        cn_queries = [
            "加密货币新闻", "美联储利率", "美股市场最新消息",
            "外汇市场分析", "全球经济数据", "期货市场动态",
        ]
        en_queries = [
            "stock market news today", "cryptocurrency bitcoin news",
            "forex market analysis", "federal reserve interest rate",
            "global economic outlook", "S&P 500 market update",
        ]

        if lang in ("all", "cn"):
            for query in cn_queries:
                try:
                    results = search.search(query, num_results=5, date_restrict="d1")
                    for r in results:
                        result["cn"].append({
                            "title": r.get("title", ""), "link": r.get("link", ""),
                            "snippet": r.get("snippet", ""), "source": r.get("source", ""),
                            "published": r.get("published", ""), "category": query, "lang": "cn",
                        })
                except Exception:
                    pass

        if lang in ("all", "en"):
            for query in en_queries:
                try:
                    results = search.search(query, num_results=5, date_restrict="d1")
                    for r in results:
                        result["en"].append({
                            "title": r.get("title", ""), "link": r.get("link", ""),
                            "snippet": r.get("snippet", ""), "source": r.get("source", ""),
                            "published": r.get("published", ""), "category": query, "lang": "en",
                        })
                except Exception:
                    pass

        for lang_key in ["cn", "en"]:
            seen: set = set()
            unique = []
            for news in result[lang_key]:
                link = news.get("link", "")
                if link and link not in seen:
                    seen.add(link)
                    unique.append(news)
            result[lang_key] = unique[:15]

    except Exception as e:
        logger.error("Failed to fetch financial news: %s", e)

    return result


# ---------------------------------------------------------------------------
# Economic calendar (template-based, no real API yet)
# ---------------------------------------------------------------------------

_SAMPLE_EVENTS = [
    {"name": "美国非农就业数据", "name_en": "US Non-Farm Payrolls", "country": "US", "importance": "high", "forecast": "180K", "previous": "175K", "impact_if_above": "bullish", "impact_if_below": "bearish", "impact_desc": "高于预期利多美元/美股，低于预期利空", "impact_desc_en": "Above forecast: bullish USD/stocks; Below: bearish"},
    {"name": "美联储利率决议", "name_en": "Fed Interest Rate Decision", "country": "US", "importance": "high", "forecast": "5.25%", "previous": "5.25%", "impact_if_above": "bearish", "impact_if_below": "bullish", "impact_desc": "加息利空股市/加密货币，降息利多", "impact_desc_en": "Rate hike: bearish stocks/crypto; Cut: bullish"},
    {"name": "美国CPI月率", "name_en": "US CPI m/m", "country": "US", "importance": "high", "forecast": "0.3%", "previous": "0.4%", "impact_if_above": "bearish", "impact_if_below": "bullish", "impact_desc": "CPI高于预期增加加息预期，利空股市", "impact_desc_en": "Higher CPI increases rate hike expectations, bearish stocks"},
    {"name": "欧洲央行利率决议", "name_en": "ECB Interest Rate Decision", "country": "EU", "importance": "high", "forecast": "4.50%", "previous": "4.50%", "impact_if_above": "bearish", "impact_if_below": "bullish", "impact_desc": "加息利空欧股，利多欧元", "impact_desc_en": "Rate hike: bearish EU stocks, bullish EUR"},
    {"name": "日本央行利率决议", "name_en": "BoJ Interest Rate Decision", "country": "JP", "importance": "high", "forecast": "0.10%", "previous": "0.10%", "impact_if_above": "bullish", "impact_if_below": "bearish", "impact_desc": "加息预期利多日元，利空日股", "impact_desc_en": "Rate hike expectation: bullish JPY, bearish Nikkei"},
    {"name": "美国初请失业金人数", "name_en": "US Initial Jobless Claims", "country": "US", "importance": "medium", "forecast": "215K", "previous": "212K", "impact_if_above": "bearish", "impact_if_below": "bullish", "impact_desc": "失业人数上升利空美元，利多黄金", "impact_desc_en": "Rising claims: bearish USD, bullish gold"},
    {"name": "英国央行利率决议", "name_en": "BoE Interest Rate Decision", "country": "UK", "importance": "high", "forecast": "5.25%", "previous": "5.25%", "impact_if_above": "bullish", "impact_if_below": "bearish", "impact_desc": "加息利多英镑，利空英股", "impact_desc_en": "Rate hike: bullish GBP, bearish UK stocks"},
    {"name": "美国零售销售月率", "name_en": "US Retail Sales m/m", "country": "US", "importance": "medium", "forecast": "0.4%", "previous": "0.6%", "impact_if_above": "bullish", "impact_if_below": "bearish", "impact_desc": "零售数据强劲利多美元和美股", "impact_desc_en": "Strong retail: bullish USD and stocks"},
    {"name": "OPEC月度报告", "name_en": "OPEC Monthly Report", "country": "INTL", "importance": "medium", "forecast": "-", "previous": "-", "impact_if_above": "bullish", "impact_if_below": "bearish", "impact_desc": "减产预期利多原油，增产预期利空", "impact_desc_en": "Production cut: bullish oil; Increase: bearish"},
]


def get_economic_calendar() -> List[Dict[str, Any]]:
    """Generate economic calendar events with impact indicators."""
    today = datetime.now()
    events = []

    for i, evt in enumerate(_SAMPLE_EVENTS):
        days_offset = i % 14 - 5
        event_date = today + timedelta(days=days_offset)
        hour = (8 + (i * 3)) % 24

        is_released = event_date.date() < today.date() or (
            event_date.date() == today.date() and hour < today.hour
        )

        actual_value = None
        actual_impact = None
        expected_impact = evt["impact_if_above"]

        if is_released:
            forecast_num = "".join(filter(lambda x: x.isdigit() or x == ".", evt["forecast"]))
            if forecast_num:
                try:
                    base = float(forecast_num)
                    variation = random.uniform(-0.15, 0.15)
                    actual_num = base * (1 + variation)
                    if "K" in evt["forecast"]:
                        actual_value = f"{actual_num:.0f}K"
                    elif "%" in evt["forecast"]:
                        actual_value = f"{actual_num:.2f}%"
                    else:
                        actual_value = f"{actual_num:.2f}"
                    if actual_num > base:
                        actual_impact = evt["impact_if_above"]
                    elif actual_num < base:
                        actual_impact = evt["impact_if_below"]
                    else:
                        actual_impact = "neutral"
                except Exception:
                    actual_value = evt["forecast"]
                    actual_impact = "neutral"
            else:
                actual_value = evt["forecast"]
                actual_impact = "neutral"

        events.append({
            "id": i + 1,
            "name": evt["name"], "name_en": evt["name_en"],
            "country": evt["country"],
            "date": event_date.strftime("%Y-%m-%d"),
            "time": f"{hour:02d}:30",
            "importance": evt["importance"],
            "actual": actual_value, "forecast": evt["forecast"], "previous": evt["previous"],
            "impact_if_above": evt["impact_if_above"], "impact_if_below": evt["impact_if_below"],
            "impact_desc": evt["impact_desc"], "impact_desc_en": evt["impact_desc_en"],
            "expected_impact": expected_impact, "actual_impact": actual_impact,
            "is_released": is_released,
        })

    events.sort(key=lambda x: (x["date"], x["time"]))
    return events

"""
龙虎榜 / 涨跌停池 / 炸板池 — 统一数据层

4 个核心接口:
  - get_dragon_tiger(start_date, end_date)  龙虎榜
  - get_zt_pool(trade_date)                 涨停池
  - get_dt_pool(trade_date)                 跌停池
  - get_broken_board(trade_date)            炸板池

数据源: 一律经 app/data_sources/coord_pool（五族协调器），本文件不再直连远端。
  - 龙虎榜：coord_pool("dragon_tiger")           → provider_pool/akshare（+ 东财搜索补位，待补 provider）
  - 涨停/跌停/炸板池：coord_pool(<kind>, trade_date=)  → provider_pool/akshare
  - 热榜：coord_pool("hot_rank")                  → provider_pool/sina + akshare
  本文件只剩内存缓存与对外只读接口；取数/选源/熔断全在 data_sources 层。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

from app.utils.logger import get_logger

logger = get_logger(__name__)


# ══════════════════════════════════════════════════════════════
#  HTTP 东财搜索 — 主数据源 (正本: eastmoney_search.py)
# ══════════════════════════════════════════════════════════════
# ⚠ 待补 provider（2026-10-28 登记，勿误删）：
#   本函数是 coord_pool."dragon_tiger" 尚未覆盖的**独立真源**（coord 现仅 akshare）。
#   收敛顺序固定为：先在 provider_pool/ 新增东财搜索 provider（成为 dragon_tiger 的
#   第 N 个源）→ 逐键对拍 → **再**删本处自拉。在此之前**保留**，且不得在其上再叠源。
#   依据：docs/数据源外围收敛改造方案_20261028.md §1.5 结论 B。

def _em_search(keyword: str, page_size: int = 200) -> List[Dict[str, Any]]:
    """调东财智能选股搜索，返回股票列表（失败返回空列表）"""
    from app.market_cn.eastmoney_search import search_stocks
    try:
        raw = search_stocks(keyword=keyword, page_size=page_size)
        return raw.get("stocks", []) if raw.get("code") == 1 else []
    except Exception as e:
        logger.warning("[东财搜索] '%s' 失败: %s", keyword, e)
        return []


# ══════════════════════════════════════════════════════════════
#  统一接口 — HTTP 优先，AkShare 兜底
# ══════════════════════════════════════════════════════════════

def get_dragon_tiger(start_date: str = "", end_date: str = "") -> List[Dict[str, Any]]:
    """获取龙虎榜数据（只读: 返回缓存，不触发拉取）

    缓存存于 app/data_sources/dragon_tiger_local（2026-10-28 下沉），正向读取。
    """
    from app.data_sources.dragon_tiger_local import get_cached_dragon_tiger
    cached = get_cached_dragon_tiger()
    if cached is not None:
        return cached
    return []


def get_zt_pool(trade_date: str = "") -> List[Dict[str, Any]]:
    """获取涨停池。

    ⚠ 2026-09-30 修正：旧实现忽略 trade_date、任何日期都返回当日实时快照——
    fetch_recent_zt_pools(8) 会把今天这份池子当成 8 天记录（伪造历史日期维度）。
    现：当日/未指定 → 实时缓存（零网络，不触发拉取）；历史日期 → coord_pool 按日拉取。

    Returns:
        list[dict]: 涨停池行（含 stock_code/stock_name/price/change_percent/
        zt_time/turnover_rate/amount/continuous_zt_days/seal_amount/reason）；无数据 []。
    """
    _day = (trade_date or "").strip()
    if _day and _day != datetime.now().strftime("%Y-%m-%d"):
        return _fetch_pool_by_date("limit_up_pool", _day)
    if _rt_zt_pool is not None:
        return _rt_zt_pool
    return []


def get_dt_pool(trade_date: str = "") -> List[Dict[str, Any]]:
    """获取跌停池（日期语义同 get_zt_pool：历史按日拉取，当日走缓存）。

    Returns:
        list[dict]: 跌停池行（键同涨停池）；无数据 []。
    """
    _day = (trade_date or "").strip()
    if _day and _day != datetime.now().strftime("%Y-%m-%d"):
        return _fetch_pool_by_date("limit_down_pool", _day)
    if _rt_dt_pool is not None:
        return _rt_dt_pool
    return []


def get_broken_board(trade_date: str = "") -> List[Dict[str, Any]]:
    """获取炸板池（日期语义同 get_zt_pool：历史按日拉取，当日走缓存）。

    Returns:
        list[dict]: 炸板池行（键同涨停池）；无数据 []。
    """
    _day = (trade_date or "").strip()
    if _day and _day != datetime.now().strftime("%Y-%m-%d"):
        return _fetch_pool_by_date("broken_board_pool", _day)
    if _rt_broken_board is not None:
        return _rt_broken_board
    return []


def get_hot_rank() -> List[Dict[str, Any]]:
    """获取热榜（只读: 返回缓存，不触发拉取）"""
    if _rt_hot_rank is not None:
        return _rt_hot_rank
    return []


# ═══ 内存缓存 + refresh（scheduler 调用）═══

# 2026-10-28 正向化：龙虎榜缓存所有权已下沉至 app/data_sources/dragon_tiger_local.py
# （起因：cn_stock_putdb 落库后要刷新缓存 ⇒ 反依赖 market_cn.dragon_limit）。
# 读写一律经 dragon_tiger_local 的 get/set，本模块不再持有该全局。
# zt/dt/broken/hot 四个池的缓存无跨层消费者，保留在本模块，不做过度移植。
_rt_zt_pool = None
_rt_dt_pool = None
_rt_broken_board = None
_rt_hot_rank = None

def _fetch_dragon_tiger() -> List[Dict[str, Any]]:
    """拉取龙虎榜数据（2026-10-26 Phase C: 改走 coord_pool 多源调度）"""
    from app.data_sources.coord_pool import coordinate
    out = coordinate("dragon_tiger")
    if out.get("ok") and out.get("data", {}).get("rows"):
        rows = out["data"]["rows"]
        logger.info("[coord_pool] dragon_tiger: %d stocks (source=%s)", len(rows), out.get("source", ""))
        return rows
    logger.warning("[coord_pool] dragon_tiger: 所有数据源均无结果 (%s)", out.get("error", ""))
    return []


def _fetch_zt_pool() -> List[Dict[str, Any]]:
    """拉取涨停池数据（Phase C: 改走 coord_pool）"""
    from app.data_sources.coord_pool import coordinate
    out = coordinate("limit_up_pool")
    if out.get("ok") and out.get("data", {}).get("rows"):
        rows = out["data"]["rows"]
        logger.info("[coord_pool] limit_up_pool: %d stocks (source=%s)", len(rows), out.get("source", ""))
        return rows
    logger.info("[coord_pool] limit_up_pool: 无结果 (%s)", out.get("error", ""))
    return []


def _fetch_dt_pool() -> List[Dict[str, Any]]:
    """拉取跌停池数据（Phase C: 改走 coord_pool）"""
    from app.data_sources.coord_pool import coordinate
    out = coordinate("limit_down_pool")
    if out.get("ok") and out.get("data", {}).get("rows"):
        rows = out["data"]["rows"]
        logger.info("[coord_pool] limit_down_pool: %d stocks (source=%s)", len(rows), out.get("source", ""))
        return rows
    logger.info("[coord_pool] limit_down_pool: 无结果 (%s)", out.get("error", ""))
    return []


def _fetch_broken_board() -> List[Dict[str, Any]]:
    """拉取炸板池数据（Phase C: 改走 coord_pool）"""
    from app.data_sources.coord_pool import coordinate
    out = coordinate("broken_board_pool")
    if out.get("ok") and out.get("data", {}).get("rows"):
        rows = out["data"]["rows"]
        logger.info("[coord_pool] broken_board_pool: %d stocks (source=%s)", len(rows), out.get("source", ""))
        return rows
    logger.info("[coord_pool] broken_board_pool: 无结果 (%s)", out.get("error", ""))
    return []


def _fetch_pool_by_date(kind: str, trade_date: str) -> List[Dict[str, Any]]:
    """按日拉取历史榜单池 —— 走 coord_pool 多源调度。

    2026-10-28 Phase 1 收敛：原实现各 `get_*_pool` 的历史分支直连 `_ak_zt_pool` 等
    （外围自拉 akshare），改为与当日缓存**同一条取数链**（coord_pool）。
    选源/排序/超时/熔断/空信封判定全在 data_sources 层，外围不再持有远端调用。

    Returns:
        list[dict]: 行契约与 provider_pool 的 `_normalize_*_pool` 一致（stock_code/
        stock_name/trade_date/price/change_percent/... 同键）；无数据 []。
    """
    from app.data_sources.coord_pool import coordinate
    out = coordinate(kind, trade_date=trade_date)
    rows = ((out.get("data") or {}).get("rows") or []) if out.get("ok") else []
    if rows:
        logger.info("[coord_pool] %s %s: %d stocks (source=%s)",
                    kind, trade_date, len(rows), out.get("source", ""))
    else:
        logger.info("[coord_pool] %s %s: 无结果 (%s)", kind, trade_date, out.get("error", ""))
    return rows


def _fetch_hot_rank() -> List[Dict[str, Any]]:
    """拉取热榜数据（Phase C: 改走 coord_pool）"""
    from app.data_sources.coord_pool import coordinate
    out = coordinate("hot_rank")
    if out.get("ok") and out.get("data", {}).get("rows"):
        rows = out["data"]["rows"]
        logger.info("[coord_pool] hot_rank: %d stocks (source=%s)", len(rows), out.get("source", ""))
        return rows
    logger.warning("[coord_pool] hot_rank: 所有数据源均无结果 (%s)", out.get("error", ""))
    return []


def refresh_dragon_tiger():
    """拉取远端龙虎榜并写入内存缓存（缓存在 dragon_tiger_local，正向写入）"""
    try:
        from app.data_sources.dragon_tiger_local import set_cached_dragon_tiger
        set_cached_dragon_tiger(_fetch_dragon_tiger())
    except Exception as e:
        logger.warning("[refresh] refresh_dragon_tiger 失败: %s", e)


# load_dragon_tiger_from_db 已下沉至 app/data_sources/dragon_tiger_local.py（2026-10-28）。
# 此处 re-export 仅为保持对外 API 不变（调用方 cn_stock_putdb 已改调新位置）。
from app.data_sources.dragon_tiger_local import (  # noqa: E402,F401  (对外 re-export)
    load_dragon_tiger_from_db,
)

def load_hot_rank_from_db(trade_date: str = ""):
    """从 CNStock_db 加载热榜数据到缓存"""
    global _rt_hot_rank
    try:
        from app.data_sources.dragon_tiger_local import query_hot_rank
        data = query_hot_rank(trade_date=trade_date)
        if data:
            _rt_hot_rank = data
            logger.info("[db] 热榜从DB加载: %d 条", len(data))
    except Exception as e:
        logger.warning("[db] 从DB加载热榜失败: %s", e)

def refresh_zt_pool():
    global _rt_zt_pool
    try:
        _rt_zt_pool = _fetch_zt_pool()
    except Exception as e:
        logger.warning("[refresh] refresh_zt_pool 失败: %s", e)

def refresh_dt_pool():
    global _rt_dt_pool
    try:
        _rt_dt_pool = _fetch_dt_pool()
    except Exception as e:
        logger.warning("[refresh] refresh_dt_pool 失败: %s", e)

def refresh_broken_board():
    global _rt_broken_board
    try:
        _rt_broken_board = _fetch_broken_board()
    except Exception as e:
        logger.warning("[refresh] refresh_broken_board 失败: %s", e)

def refresh_hot_rank():
    global _rt_hot_rank
    try:
        _rt_hot_rank = _fetch_hot_rank()
    except Exception as e:
        logger.warning("[refresh] refresh_hot_rank 失败: %s", e)


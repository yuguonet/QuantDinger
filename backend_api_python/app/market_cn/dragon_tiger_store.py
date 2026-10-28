"""
龙虎榜 & 热榜 — 落库调度入口（取数经 coord_pool，本模块不持有本地库读写）

调度: scheduler.py 工作日 18:00 调用 save_daily()
数据源: 一律经 app/data_sources/coord_pool（akshare / sina 等由 provider_pool 承载）

职责边界:
  · 取数    = `_coord_pool_rows()` → coord_pool，选源/排序/重试/熔断全在 data_sources 层
              （2026-10-28 Phase 1 收敛：已删本文件原有的 akshare 直拉 + 东财搜索兜底链）
  · 落库    = `_save_dragon_tiger` / `_save_hot_rank`（经 dragon_tiger_local）
  · 本地库  = app/data_sources/dragon_tiger_local.py（DDL / 查询 / UPSERT），本文件 re-export
  · 方向    = market_cn → data_sources（正向）；data_sources 不得反依赖 market_cn

唯一保留的外围直连：`_fetch_hot_rank_from_pywencai`（同花顺问财）——
  coord_pool.hot_rank 只有"当日实时"，**无历史**；历史热榜暂只此一源。
  补齐 provider_pool 的历史热榜 provider 后应改为 `_coord_pool_rows`。

表结构（见 dragon_tiger_local）:
  - cnd_dragon_tiger_list  龙虎榜
  - cnd_hot_rank_list      热榜
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

from app.utils.logger import get_logger

logger = get_logger(__name__)

# ══════════════════════════════════════════════════════════════
#  本地库读写 —— 已下沉至 app/data_sources/dragon_tiger_local.py
# ══════════════════════════════════════════════════════════════
# 2026-10-28 Phase 0 正向化：
#   本文件原先混装"本地库读写"与"远端拉取"，导致 app/data_sources/ 两个模块
#   反依赖 app.market_cn（下层调上层，违反方向红线）。现本地库段整体下沉到
#   data_sources 层，此处仅 re-export，**对外 API 与行为完全不变**。
#   远端拉取 / save_daily / import_history 仍在本文件，方向为
#   market_cn → data_sources（正向）。
from app.data_sources.dragon_tiger_local import (  # noqa: F401  (对外 re-export)
    _ensure_tables,
    _get_cnstock_pool,
    _save_dragon_tiger,
    _save_hot_rank,
    query_dragon_tiger,
    query_dragon_tiger_latest,
    query_hot_rank,
)


# ══════════════════════════════════════════════════════════════
#  统一取数 —— 一律经 coord_pool（本文件不再直连远端）
# ══════════════════════════════════════════════════════════════

def _coord_pool_rows(kind: str, **params) -> List[Dict[str, Any]]:
    """经 coord_pool 取榜单行；失败/空返回 []。

    2026-10-28 Phase 1 收敛：原先本文件直连 akshare（`_fetch_*_from_akshare`，
    自带 3 次重试 + 递增 sleep）并以东财搜索兜底。现统一改由 coord_pool 调度：
    选源 / 排序 / 超时 / 重试 / 熔断 / 空信封判定全在 data_sources 层，
    本文件只消费结果（避免"外围再写一份降级链"）。
    """
    from app.data_sources.coord_pool import coordinate
    out = coordinate(kind, **params)
    rows = ((out.get("data") or {}).get("rows") or []) if out.get("ok") else []
    if rows:
        logger.info("[coord_pool] %s %s: %d 条 (source=%s)",
                    kind, params or "", len(rows), out.get("source", ""))
    else:
        logger.info("[coord_pool] %s %s: 无结果 (%s)", kind, params or "", out.get("error", ""))
    return rows


# ══════════════════════════════════════════════════════════════
#  历史数据获取（独立运行用）
# ══════════════════════════════════════════════════════════════

def _fetch_hot_rank_from_pywencai(trade_date: str = "") -> List[Dict[str, Any]]:
    """从同花顺问财获取历史热榜数据
    
    使用 pywencai 库，支持按日期查询。
    """
    from datetime import datetime
    
    try:
        import pywencai
        import pandas as pd
        
        if not trade_date:
            trade_date = datetime.now().strftime("%Y-%m-%d")
        
        # 格式化查询语句
        date_fmt = trade_date.replace("-", "年", 1).replace("-", "月", 1) + "日"
        query = f"{date_fmt} 热门个股排名"
        
        df = pywencai.get(query=query, loop=True)
        
        if not isinstance(df, pd.DataFrame):
            logger.warning("[pywencai] 热榜 %s: 返回类型 %s，非 DataFrame", trade_date, type(df).__name__)
            return []
        if df.empty:
            logger.warning("[pywencai] 热榜 %s: DataFrame 为空", trade_date)
            return []
        
        # 解析同花顺返回的 DataFrame
        result = []
        for idx, row in df.iterrows():
            row_dict = row.to_dict()
            
            # 提取股票代码（可能在不同列名中）
            stock_code = ""
            stock_name = ""
            for col in row_dict:
                col_lower = str(col).lower()
                if "代码" in col_lower or "code" in col_lower:
                    val = str(row_dict[col] or "")
                    # 提取纯数字代码
                    import re
                    match = re.search(r"(\d{6})", val)
                    if match:
                        stock_code = match.group(1)
                if "简称" in col_lower or "名称" in col_lower or "name" in col_lower:
                    stock_name = str(row_dict[col] or "")
            
            if not stock_code:
                continue
            
            normalized = {
                "trade_date": trade_date,
                "rank": len(result) + 1,
                "stock_code": stock_code,
                "stock_name": stock_name,
                "price": 0,
                "change_percent": 0,
                "popularity_score": 0,
                "current_rank_change": "",
            }
            result.append(normalized)
        
        logger.info("[pywencai] 热榜 %s: %d stocks", trade_date, len(result))
        return result
    except ImportError:
        logger.warning("[pywencai] 未安装，跳过")
        return []
    except Exception as e:
        logger.warning("[pywencai] 获取热榜失败: %s", e)
        return []


# ══════════════════════════════════════════════════════════════
#  历史数据导入（独立运行入口）
# ══════════════════════════════════════════════════════════════

def import_history(
    start_date: str = "",
    end_date: str = "",
    include_hot_rank: bool = True,
    source: str = "auto",
) -> Dict[str, Any]:
    """导入历史龙虎榜和热榜数据到 PostgreSQL
    
    独立运行入口，可命令行调用。
    
    Args:
        start_date: 开始日期 YYYY-MM-DD，默认30天前
        end_date: 结束日期 YYYY-MM-DD，默认今天
        include_hot_rank: 是否导入热榜（仅当天数据）
        source: 数据源 "auto" | "akshare" | "em"
            - auto: 东财搜索优先，AkShare 兜底
            - akshare: 仅用 AkShare
            - em: 仅用东财搜索
    
    Returns:
        {
            "dragon_tiger": {"written": int, "total": int, "date_range": str},
            "hot_rank": {"written": int, "total": int},
            "status": "ok" | "error",
        }
    """
    from datetime import timedelta
    from app.utils.db import get_db_connection
    
    # 默认日期范围
    if not end_date:
        end_date = datetime.now().strftime("%Y-%m-%d")
    if not start_date:
        start_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    
    result: Dict[str, Any] = {
        "dragon_tiger": {"written": 0, "total": 0, "date_range": f"{start_date} ~ {end_date}"},
        "hot_rank": {"written": 0, "total": 0},
        "status": "ok",
    }
    
    # 获取龙虎榜数据（经 coord_pool 多源调度，支持日期区间）
    # 说明：source 参数保留以兼容既有命令行调用，但**不再影响选源** ——
    # 选源/排序/重试/熔断统一由 data_sources 层决定（2026-10-28 收敛）。
    dt_data: List[Dict[str, Any]] = []
    if source in ("auto", "akshare", "em"):
        dt_data = _coord_pool_rows("dragon_tiger", start_date=start_date, end_date=end_date)
    
    result["dragon_tiger"]["total"] = len(dt_data)
    
    # 获取热榜数据（按天获取历史热榜）
    hr_data: List[Dict[str, Any]] = []
    if include_hot_rank:
        # 按天循环获取热榜
        from datetime import timedelta
        current = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")
        while current <= end:
            date_str = current.strftime("%Y-%m-%d")
            # 历史热榜唯一源 = 同花顺问财（coord_pool.hot_rank 只有当日实时，无历史）。
            day_data = _fetch_hot_rank_from_pywencai(date_str)
            if not day_data and date_str == datetime.now().strftime("%Y-%m-%d"):
                # 问财当天不可用时，当日快照由 coord 补 —— 这是"历史源 / 当日源"的
                # 能力分工，不是同源多级兜底。
                day_data = _coord_pool_rows("hot_rank")
            hr_data.extend(day_data)
            current += timedelta(days=1)
        result["hot_rank"]["total"] = len(hr_data)
    
    # 写入 CNStock_db
    try:
        pool = _get_cnstock_pool()
        with pool.connection() as conn:
            _ensure_tables(conn)
            
            if dt_data:
                written = _save_dragon_tiger(conn, dt_data, start_date)
                result["dragon_tiger"]["written"] = written
                logger.info("[import_history] 龙虎榜写入 %d/%d 条", written, len(dt_data))
            
            if hr_data:
                trade_date = datetime.now().strftime("%Y-%m-%d")
                written = _save_hot_rank(conn, hr_data, trade_date)
                result["hot_rank"]["written"] = written
                logger.info("[import_history] 热榜写入 %d/%d 条", written, len(hr_data))
    except Exception as e:
        logger.error("[import_history] 数据库写入失败: %s", e)
        result["status"] = "error"
    
    return result


# ══════════════════════════════════════════════════════════════
#  每日保存入口（scheduler 调用）
# ══════════════════════════════════════════════════════════════

def save_daily(include_dragon_tiger: bool = True) -> Dict[str, Any]:
    """每日保存龙虎榜 + 热榜到 CNStock_db

    由 scheduler.py 在工作日 17:00+ 调用。
    2026-10-27 起龙虎榜写链收口 app/data_sources/cn_stock_putdb.sync_dragon_tiger
    （coord_pool 多源），scheduler 以 include_dragon_tiger=False 只跑热榜 ——
    保留参数为 True 的旧形以兼容独立调用/import_history 场景。

    Args:
        include_dragon_tiger: False 时跳过龙虎榜拉取/落库（scheduler 路径，
            LHB 由 cn_stock_putdb.sync_dragon_tiger 负责）。

    Returns:
        {
            "dragon_tiger": {"written": int, "total": int},
            "hot_rank": {"written": int, "total": int},
            "trade_date": str,
            "status": "ok" | "error",
        }
    """
    trade_date = datetime.now().strftime("%Y-%m-%d")

    result: Dict[str, Any] = {
        "trade_date": trade_date,
        "dragon_tiger": {"written": 0, "total": 0},
        "hot_rank": {"written": 0, "total": 0},
        "status": "ok",
    }

    # 获取龙虎榜（经 coord_pool；scheduler 路径以 include_dragon_tiger=False 跳过，
    # 龙虎榜写链由 cn_stock_putdb.sync_dragon_tiger 负责）
    dt_data = []
    if include_dragon_tiger:
        dt_data = _coord_pool_rows("dragon_tiger", start_date=trade_date, end_date=trade_date)

    # 获取热榜（当日快照，coord_pool 内置 sina + akshare 多源）
    hr_data = _coord_pool_rows("hot_rank")
    
    result["dragon_tiger"]["total"] = len(dt_data) if dt_data else 0
    result["hot_rank"]["total"] = len(hr_data) if hr_data else 0

    # 写入 CNStock_db
    try:
        pool = _get_cnstock_pool()
        with pool.connection() as conn:
            _ensure_tables(conn)

            if dt_data:
                written = _save_dragon_tiger(conn, dt_data, trade_date)
                result["dragon_tiger"]["written"] = written
                logger.info("[dragon_tiger_store] 龙虎榜写入 %d/%d 条", written, len(dt_data))

            if hr_data:
                written = _save_hot_rank(conn, hr_data, trade_date)
                result["hot_rank"]["written"] = written
                logger.info("[dragon_tiger_store] 热榜写入 %d/%d 条", written, len(hr_data))

    except Exception as e:
        logger.error("[dragon_tiger_store] 数据库写入失败: %s", e)
        result["status"] = "error"

    # 写入成功后刷新内存缓存
    if result["status"] == "ok":
        try:
            from app.market_cn.dragon_limit import load_dragon_tiger_from_db, load_hot_rank_from_db
            load_dragon_tiger_from_db(trade_date)
            load_hot_rank_from_db(trade_date)
        except Exception as e:
            logger.warning("[dragon_tiger_store] 刷新缓存失败: %s", e)

    return result

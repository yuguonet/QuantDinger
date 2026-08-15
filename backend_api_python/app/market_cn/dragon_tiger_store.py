"""
龙虎榜 & 热榜 持久化层 — 每日写入 PostgreSQL (CNStock_db)

调度: scheduler.py 工作日 18:00 调用 save_daily()
数据源: dragon_limit (HTTP 东财搜索 + AkShare 兜底)

表结构:
  - cnd_dragon_tiger_list  龙虎榜
  - cnd_hot_rank_list      热榜
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Tuple

from app.utils.logger import get_logger

logger = get_logger(__name__)

# 建表标记，避免重复执行 DDL
_tables_ensured = False


# ══════════════════════════════════════════════════════════════
#  建表（首次使用时自动执行）
# ══════════════════════════════════════════════════════════════

_CREATE_DRAGON_TIGER = """
CREATE TABLE IF NOT EXISTS cnd_dragon_tiger_list (
    id              SERIAL PRIMARY KEY,
    trade_date      VARCHAR(10) NOT NULL,
    stock_code      VARCHAR(10) NOT NULL,
    stock_name      VARCHAR(50) DEFAULT '',
    reason          VARCHAR(200) DEFAULT '',
    buy_amount      DOUBLE PRECISION DEFAULT 0,
    sell_amount     DOUBLE PRECISION DEFAULT 0,
    net_amount      DOUBLE PRECISION DEFAULT 0,
    change_percent  DOUBLE PRECISION DEFAULT 0,
    close_price     DOUBLE PRECISION DEFAULT 0,
    turnover_rate   DOUBLE PRECISION DEFAULT 0,
    amount          DOUBLE PRECISION DEFAULT 0,
    buy_seat_count  INTEGER DEFAULT 0,
    sell_seat_count INTEGER DEFAULT 0,
    UNIQUE(trade_date, stock_code, reason)
)
"""

_CREATE_HOT_RANK = """
CREATE TABLE IF NOT EXISTS cnd_hot_rank_list (
    id                  SERIAL PRIMARY KEY,
    trade_date          VARCHAR(10) NOT NULL,
    rank                INTEGER DEFAULT 0,
    stock_code          VARCHAR(10) NOT NULL,
    stock_name          VARCHAR(50) DEFAULT '',
    price               DOUBLE PRECISION DEFAULT 0,
    change_percent      DOUBLE PRECISION DEFAULT 0,
    popularity_score    DOUBLE PRECISION DEFAULT 0,
    rank_change         VARCHAR(20) DEFAULT '',
    UNIQUE(trade_date, stock_code)
)
"""

_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_dt_trade_date ON cnd_dragon_tiger_list(trade_date)",
    "CREATE INDEX IF NOT EXISTS idx_dt_stock_code ON cnd_dragon_tiger_list(stock_code)",
    "CREATE INDEX IF NOT EXISTS idx_dt_date_code ON cnd_dragon_tiger_list(trade_date, stock_code)",
    "CREATE INDEX IF NOT EXISTS idx_hr_trade_date ON cnd_hot_rank_list(trade_date)",
    "CREATE INDEX IF NOT EXISTS idx_hr_stock_code ON cnd_hot_rank_list(stock_code)",
    "CREATE INDEX IF NOT EXISTS idx_hr_rank ON cnd_hot_rank_list(trade_date, rank)",
]


def _ensure_tables(conn) -> None:
    """建表 + 索引（幂等，进程内只执行一次）"""
    global _tables_ensured
    if _tables_ensured:
        return

    raw_cur = conn.cursor()
    try:
        raw_cur.execute(_CREATE_DRAGON_TIGER)
        raw_cur.execute(_CREATE_HOT_RANK)
        for idx_sql in _CREATE_INDEXES:
            raw_cur.execute(idx_sql)
        conn.commit()
        _tables_ensured = True
        logger.info("[dragon_tiger_store] 建表完成")
    except Exception as e:
        conn.rollback()
        logger.warning("[dragon_tiger_store] 建表失败: %s", e)
    finally:
        raw_cur.close()


# ══════════════════════════════════════════════════════════════
#  数据库写入
# ══════════════════════════════════════════════════════════════

_INSERT_DRAGON_TIGER = """
INSERT INTO cnd_dragon_tiger_list (
    trade_date, stock_code, stock_name, reason,
    buy_amount, sell_amount, net_amount,
    change_percent, close_price, turnover_rate, amount,
    buy_seat_count, sell_seat_count
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (trade_date, stock_code, reason) DO UPDATE SET
    stock_name     = EXCLUDED.stock_name,
    buy_amount     = EXCLUDED.buy_amount,
    sell_amount    = EXCLUDED.sell_amount,
    net_amount     = EXCLUDED.net_amount,
    change_percent = EXCLUDED.change_percent,
    close_price    = EXCLUDED.close_price,
    turnover_rate  = EXCLUDED.turnover_rate,
    amount         = EXCLUDED.amount,
    buy_seat_count = EXCLUDED.buy_seat_count,
    sell_seat_count= EXCLUDED.sell_seat_count
"""

_INSERT_HOT_RANK = """
INSERT INTO cnd_hot_rank_list (
    trade_date, rank, stock_code, stock_name,
    price, change_percent, popularity_score, rank_change
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (trade_date, stock_code) DO UPDATE SET
    rank             = EXCLUDED.rank,
    stock_name       = EXCLUDED.stock_name,
    price            = EXCLUDED.price,
    change_percent   = EXCLUDED.change_percent,
    popularity_score = EXCLUDED.popularity_score,
    rank_change      = EXCLUDED.rank_change
"""


def _save_dragon_tiger(conn, data: List[Dict[str, Any]], trade_date: str) -> int:
    """写入龙虎榜数据，返回写入条数"""
    if not data:
        return 0

    raw_cur = conn.cursor()
    written = 0
    try:
        for row in data:
            stock_code = row.get("stock_code", "")
            if not stock_code:
                continue
            params = (
                row.get("trade_date", trade_date),
                stock_code,
                row.get("stock_name", ""),
                (row.get("reason", "") or "")[:200],
                row.get("buy_amount", 0),
                row.get("sell_amount", 0),
                row.get("net_amount", 0),
                row.get("change_percent", 0),
                row.get("close_price", 0),
                row.get("turnover_rate", 0),
                row.get("amount", 0),
                row.get("buy_seat_count", 0),
                row.get("sell_seat_count", 0),
            )
            raw_cur.execute(_INSERT_DRAGON_TIGER, params)
            written += 1
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error("[dragon_tiger_store] 龙虎榜写入失败: %s", e)
        return 0
    finally:
        raw_cur.close()

    return written


def _save_hot_rank(conn, data: List[Dict[str, Any]], trade_date: str) -> int:
    """写入热榜数据，返回写入条数"""
    if not data:
        return 0

    raw_cur = conn.cursor()
    written = 0
    try:
        for row in data:
            stock_code = row.get("stock_code", "")
            if not stock_code:
                continue
            params = (
                trade_date,
                row.get("rank", 0),
                stock_code,
                row.get("stock_name", ""),
                row.get("price", 0),
                row.get("change_percent", 0),
                row.get("popularity_score", 0),
                row.get("current_rank_change", row.get("rank_change", "")),
            )
            raw_cur.execute(_INSERT_HOT_RANK, params)
            written += 1
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error("[dragon_tiger_store] 热榜写入失败: %s", e)
        return 0
    finally:
        raw_cur.close()

    return written


# ══════════════════════════════════════════════════════════════
#  数据库连接（使用 CNStock_db）
# ══════════════════════════════════════════════════════════════

def _get_cnstock_pool():
    """获取 CNStock_db 连接池"""
    from app.utils.db_market import get_market_db_manager
    mgr = get_market_db_manager()
    return mgr._get_pool("CNStock")


# ══════════════════════════════════════════════════════════════
#  数据库读取（供 Agent 工具和前端卡片使用）
# ══════════════════════════════════════════════════════════════

def query_dragon_tiger(trade_date: str = "", stock_code: str = "", days: int = 30) -> List[Dict[str, Any]]:
    """查询龙虎榜数据

    Args:
        trade_date: 指定日期，空=最近
        stock_code: 指定股票代码，空=全部
        days: 回溯天数（当 trade_date 为空时生效）

    Returns:
        龙虎榜记录列表
    """
    from app.utils.db import get_db_connection

    conditions = []
    params: List[Any] = []

    if trade_date:
        conditions.append("trade_date = %s")
        params.append(trade_date)
    elif stock_code:
        pass
    else:
        # 默认最近 N 天
        from datetime import timedelta
        start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        conditions.append("trade_date >= %s")
        params.append(start)

    if stock_code:
        conditions.append("stock_code = %s")
        params.append(stock_code)

    where = " AND ".join(conditions) if conditions else "1=1"
    sql = f"""
        SELECT trade_date, stock_code, stock_name, reason,
               buy_amount, sell_amount, net_amount,
               change_percent, close_price, turnover_rate, amount,
               buy_seat_count, sell_seat_count
        FROM cnd_dragon_tiger_list
        WHERE {where}
        ORDER BY trade_date DESC, net_amount DESC
        LIMIT 500
    """

    try:
        pool = _get_cnstock_pool()
        with pool.connection() as conn:
            cur = conn.cursor()
            cur.execute(sql, params)
            columns = [desc[0] for desc in cur.description]
            rows = cur.fetchall()
            cur.close()
            return [dict(zip(columns, row)) for row in rows]
    except Exception as e:
        logger.warning("[dragon_tiger_store] 查询龙虎榜失败: %s", e)
        return []


def query_hot_rank(trade_date: str = "", stock_code: str = "") -> List[Dict[str, Any]]:
    """查询热榜数据

    Args:
        trade_date: 指定日期，空=最近交易日
        stock_code: 指定股票代码，空=全部

    Returns:
        热榜记录列表
    """
    from app.utils.db import get_db_connection

    conditions = []
    params: List[Any] = []

    if trade_date:
        conditions.append("trade_date = %s")
        params.append(trade_date)

    if stock_code:
        conditions.append("stock_code = %s")
        params.append(stock_code)

    where = " AND ".join(conditions) if conditions else "1=1"
    sql = f"""
        SELECT trade_date, rank, stock_code, stock_name,
               price, change_percent, popularity_score, rank_change
        FROM cnd_hot_rank_list
        WHERE {where}
        ORDER BY trade_date DESC, rank ASC
        LIMIT 200
    """

    try:
        pool = _get_cnstock_pool()
        with pool.connection() as conn:
            cur = conn.cursor()
            cur.execute(sql, params)
            columns = [desc[0] for desc in cur.description]
            rows = cur.fetchall()
            cur.close()
            return [dict(zip(columns, row)) for row in rows]
    except Exception as e:
        logger.warning("[dragon_tiger_store] 查询热榜失败: %s", e)
        return []


# ══════════════════════════════════════════════════════════════
#  归一化处理（复用 dragon_limit.py 的标准化逻辑）
# ══════════════════════════════════════════════════════════════

def _normalize_dragon_tiger_record(raw: Dict[str, Any], source: str = "akshare") -> Dict[str, Any]:
    """归一化一条龙虎榜记录
    
    复用 dragon_limit.py 的标准化逻辑，确保数据格式一致。
    
    source:
        "akshare" - 旧版 AkShare 列名 (中文)
        "custom"  - 新版列名映射后 (已有英文字段)
        "em"      - 东财搜索返回
    """
    from app.data_sources.normalizer import safe_float as _sf, safe_int as _si
    
    def _ss(v, default="") -> str:
        if v is None:
            return default
        return str(v).strip() if v else default
    
    # custom source: 已通过列名映射，直接取英文字段
    if source == "custom":
        return {
            "stock_code": _ss(raw.get("stock_code", "")),
            "stock_name": _ss(raw.get("stock_name", "")),
            "trade_date": _ss(raw.get("trade_date", ""))[:10],
            "reason": _ss(raw.get("reason", ""))[:200],
            "buy_amount": _sf(raw.get("buy_amount", 0)),
            "sell_amount": _sf(raw.get("sell_amount", 0)),
            "net_amount": _sf(raw.get("net_amount", 0)),
            "change_percent": _sf(raw.get("change_percent", 0)),
            "close_price": _sf(raw.get("close_price", 0)),
            "turnover_rate": _sf(raw.get("turnover_rate", 0)),
            "amount": _sf(raw.get("amount", 0)),
            "buy_seat_count": _si(raw.get("buy_seat_count", 0)),
            "sell_seat_count": _si(raw.get("sell_seat_count", 0)),
        }
    
    if source == "akshare" or "代码" in raw:
        return {
            "stock_code": _ss(raw.get("代码", raw.get("code", raw.get("stock_code")))),
            "stock_name": _ss(raw.get("名称", raw.get("name", raw.get("stock_name")))),
            "trade_date": _ss(raw.get("上榜日", raw.get("trade_date", "")))[:10],
            "reason": _ss(raw.get("解读", raw.get("reason", raw.get("EXPLANATION"))))[:200],
            "buy_amount": _sf(raw.get("龙虎榜买入额", raw.get("buy_amount", raw.get("BUY")))),
            "sell_amount": _sf(raw.get("龙虎榜卖出额", raw.get("sell_amount", raw.get("SELL")))),
            "net_amount": _sf(raw.get("龙虎榜净额", raw.get("net_amount", raw.get("NET_BUY")))),
            "change_percent": _sf(raw.get("涨跌幅", raw.get("change_percent", raw.get("CHANGE_RATE")))),
            "close_price": _sf(raw.get("收盘价", raw.get("close_price", raw.get("CLOSE_PRICE")))),
            "turnover_rate": _sf(raw.get("换手率", raw.get("turnover_rate", raw.get("TURNOVERRATE")))),
            "amount": _sf(raw.get("成交额", raw.get("amount", raw.get("ACCUM_AMOUNT")))),
            "buy_seat_count": _si(raw.get("买入席位数", raw.get("buy_seat_count", raw.get("BUYER_NUM", 0)))),
            "sell_seat_count": _si(raw.get("卖出席位数", raw.get("sell_seat_count", raw.get("SELLER_NUM", 0)))),
        }
    
    # em source: 东财搜索
    return {
        "stock_code": _ss(raw.get("stock_code", raw.get("SECURITY_CODE"))),
        "stock_name": _ss(raw.get("stock_name", raw.get("SECURITY_NAME_ABBR"))),
        "trade_date": _ss(raw.get("trade_date", raw.get("TRADE_DATE", "")))[:10],
        "reason": _ss(raw.get("reason", raw.get("EXPLANATION"))),
        "buy_amount": _sf(raw.get("buy_amount", raw.get("BUY"))),
        "sell_amount": _sf(raw.get("sell_amount", raw.get("SELL"))),
        "net_amount": _sf(raw.get("net_amount", raw.get("NET_BUY"))),
        "change_percent": _sf(raw.get("change_percent", raw.get("CHANGE_RATE"))),
        "close_price": _sf(raw.get("close_price", raw.get("CLOSE_PRICE"))),
        "turnover_rate": _sf(raw.get("turnover_rate", raw.get("TURNOVERRATE"))),
        "amount": _sf(raw.get("amount", raw.get("ACCUM_AMOUNT"))),
        "buy_seat_count": _si(raw.get("buy_seat_count", raw.get("BUYER_NUM", 0))),
        "sell_seat_count": _si(raw.get("sell_seat_count", raw.get("SELLER_NUM", 0))),
    }


def _normalize_hot_rank_record(raw: Dict[str, Any], source: str = "akshare") -> Dict[str, Any]:
    """归一化一条热榜记录"""
    from app.data_sources.normalizer import safe_float as _sf, safe_int as _si
    
    def _ss(v, default="") -> str:
        if v is None:
            return default
        return str(v).strip() if v else default
    
    if source == "akshare" or "股票代码" in raw:
        return {
            "rank": _si(raw.get("当前排名", raw.get("rank", raw.get("RANK")))),
            "stock_code": _ss(raw.get("股票代码", raw.get("code", raw.get("SECURITY_CODE")))),
            "stock_name": _ss(raw.get("股票名称", raw.get("name", raw.get("SECURITY_NAME_ABBR")))),
            "price": _sf(raw.get("最新价", raw.get("price", raw.get("NEWEST_PRICE")))),
            "change_percent": _sf(raw.get("涨跌幅", raw.get("change_percent", raw.get("CHANGE_RATE")))),
            "popularity_score": _sf(raw.get("人气值", raw.get("popularity", raw.get("HOT_NUM", raw.get("SCORE"))))),
            "current_rank_change": _ss(raw.get("排名变化", raw.get("rank_change", raw.get("RANK_CHANGE")))),
        }
    return {
        "rank": _si(raw.get("rank", raw.get("RANK"))),
        "stock_code": _ss(raw.get("stock_code", raw.get("SECURITY_CODE"))),
        "stock_name": _ss(raw.get("stock_name", raw.get("SECURITY_NAME_ABBR"))),
        "price": _sf(raw.get("price", raw.get("NEWEST_PRICE"))),
        "change_percent": _sf(raw.get("change_percent", raw.get("CHANGE_RATE"))),
        "popularity_score": _sf(raw.get("popularity_score", raw.get("HOT_NUM", raw.get("SCORE")))),
        "current_rank_change": _ss(raw.get("current_rank_change", raw.get("RANK_CHANGE"))),
    }


# ══════════════════════════════════════════════════════════════
#  历史数据获取（独立运行用）
# ══════════════════════════════════════════════════════════════

def _fetch_dragon_tiger_from_akshare(start_date: str, end_date: str, max_retries: int = 3) -> List[Dict[str, Any]]:
    """从 AkShare 获取历史龙虎榜数据，带重试机制
    
    使用 stock_lhb_detail_em API（AkShare >= 1.18.x）
    """
    import time as _time
    
    # 格式转换: YYYY-MM-DD -> YYYYMMDD
    start_fmt = start_date.replace("-", "")
    end_fmt = end_date.replace("-", "")
    
    for attempt in range(1, max_retries + 1):
        try:
            import akshare as ak
            from app.data_sources.rate_limiter import get_akshare_limiter
            
            get_akshare_limiter().wait()
            df = ak.stock_lhb_detail_em(start_date=start_fmt, end_date=end_fmt)
            
            if df is None or df.empty:
                return []
            
            # 列名映射 (AkShare -> 标准字段)
            col_map = {
                "代码": "stock_code",
                "名称": "stock_name",
                "上榜日": "trade_date",
                "解读": "reason",
                "龙虎榜买入额": "buy_amount",
                "龙虎榜卖出额": "sell_amount",
                "龙虎榜净买额": "net_amount",
                "涨跌幅": "change_percent",
                "收盘价": "close_price",
                "换手率": "turnover_rate",
                "龙虎榜成交额": "amount",
                "上榜原因": "reason_extra",
            }
            
            result = []
            for _, row in df.iterrows():
                raw = row.to_dict()
                record = {}
                for cn_col, en_col in col_map.items():
                    if cn_col in raw:
                        record[en_col] = raw[cn_col]
                
                # 标准化处理
                normalized = _normalize_dragon_tiger_record(record, source="custom")
                
                # 日期格式标准化 (可能带时间，截取前10位)
                if normalized.get("trade_date"):
                    normalized["trade_date"] = str(normalized["trade_date"])[:10]
                
                # 补充上榜原因 (reason_extra -> reason)
                if not normalized.get("reason") and record.get("reason_extra"):
                    normalized["reason"] = str(record["reason_extra"])[:200]
                
                if normalized.get("stock_code") and normalized.get("trade_date"):
                    result.append(normalized)
            
            logger.info("[AkShare] 历史龙虎榜 %s~%s: %d records", start_date, end_date, len(result))
            return result
        except Exception as e:
            if attempt < max_retries:
                wait_sec = attempt * 3
                logger.warning("[AkShare] 龙虎榜获取失败 (第%d次): %s, %ds后重试", attempt, e, wait_sec)
                _time.sleep(wait_sec)
            else:
                logger.error("[AkShare] 龙虎榜获取失败 (已重试%d次): %s", max_retries, e)
                return []


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
        
        if not isinstance(df, pd.DataFrame) or df.empty:
            logger.warning("[pywencai] 热榜 %s: 无数据", trade_date)
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


def _fetch_hot_rank_from_em(page_size: int = 100, trade_date: str = "") -> List[Dict[str, Any]]:
    """从东财搜索获取热榜数据（仅当天，兜底用）"""
    from datetime import datetime
    
    try:
        from app.market_cn.eastmoney_search import search_stocks
        
        if not trade_date:
            trade_date = datetime.now().strftime("%Y-%m-%d")
        
        keyword = f"{trade_date} 热门股票"
        raw = search_stocks(keyword=keyword, page_size=page_size)
        if raw.get("code") != 1:
            return []
        
        result = []
        for idx, s in enumerate(raw.get("stocks", []), 1):
            normalized = {
                "trade_date": trade_date,
                "rank": idx,
                "stock_code": str(s.get("code", "")),
                "stock_name": str(s.get("name", "")),
                "price": float(s.get("new_price", 0) or 0),
                "change_percent": float(s.get("change_rate", 0) or 0),
                "popularity_score": 0,
                "current_rank_change": "",
            }
            if normalized.get("stock_code"):
                result.append(normalized)
        
        logger.info("[东财搜索] 热榜 %s: %d stocks", trade_date, len(result))
        return result
    except Exception as e:
        logger.error("[东财搜索] 获取热榜失败: %s", e)
        return []


def _fetch_hot_rank_from_akshare(max_retries: int = 3) -> List[Dict[str, Any]]:
    """从 AkShare 获取热榜数据（仅当天），带重试机制"""
    import time as _time
    
    for attempt in range(1, max_retries + 1):
        try:
            import akshare as ak
            from app.data_sources.rate_limiter import get_akshare_limiter
            
            get_akshare_limiter().wait()
            df = ak.stock_hot_rank_em()
            
            if df is None or df.empty:
                return []
            
            result = []
            for _, row in df.iterrows():
                raw = row.to_dict()
                normalized = _normalize_hot_rank_record(raw, source="akshare")
                if normalized.get("stock_code"):
                    result.append(normalized)
            
            logger.info("[AkShare] 热榜: %d stocks", len(result))
            return result
        except Exception as e:
            if attempt < max_retries:
                wait_sec = attempt * 3
                logger.warning("[AkShare] 热榜获取失败 (第%d次): %s, %ds后重试", attempt, e, wait_sec)
                _time.sleep(wait_sec)
            else:
                logger.warning("[AkShare] 热榜获取失败，回退东财搜索: %s", e)
                return _fetch_hot_rank_from_em()


def _fetch_dragon_tiger_from_em(keyword: str = "龙虎榜", page_size: int = 200) -> List[Dict[str, Any]]:
    """从东财搜索获取龙虎榜数据"""
    try:
        from app.market_cn.eastmoney_search import search_stocks
        
        raw = search_stocks(keyword=keyword, page_size=page_size)
        if raw.get("code") != 1:
            return []
        
        stocks = raw.get("stocks", [])
        result = []
        for s in stocks:
            normalized = _normalize_dragon_tiger_record(s, source="em")
            if normalized.get("stock_code"):
                result.append(normalized)
        
        logger.info("[东财搜索] 龙虎榜: %d stocks", len(result))
        return result
    except Exception as e:
        logger.error("[东财搜索] 获取龙虎榜失败: %s", e)
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
    
    # 获取龙虎榜数据
    dt_data: List[Dict[str, Any]] = []
    if source in ("auto", "em"):
        dt_data = _fetch_dragon_tiger_from_em("龙虎榜", 200)
    if not dt_data and source in ("auto", "akshare"):
        dt_data = _fetch_dragon_tiger_from_akshare(start_date, end_date)
    
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
            day_data = _fetch_hot_rank_from_pywencai(date_str)
            if not day_data and date_str == datetime.now().strftime("%Y-%m-%d"):
                # 当天数据用东财兜底
                day_data = _fetch_hot_rank_from_em(trade_date=date_str)
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

def save_daily() -> Dict[str, Any]:
    """每日保存龙虎榜 + 热榜到 CNStock_db

    由 scheduler.py 在工作日 18:00 调用。
    数据源: dragon_limit (HTTP 东财搜索 + AkShare 兜底)

    Returns:
        {
            "dragon_tiger": {"written": int, "total": int},
            "hot_rank": {"written": int, "total": int},
            "trade_date": str,
            "status": "ok" | "error",
        }
    """
    from app.market_cn.dragon_limit import (
        get_dragon_tiger, get_hot_rank,
        refresh_dragon_tiger, refresh_hot_rank,
    )

    trade_date = datetime.now().strftime("%Y-%m-%d")

    result: Dict[str, Any] = {
        "trade_date": trade_date,
        "dragon_tiger": {"written": 0, "total": 0},
        "hot_rank": {"written": 0, "total": 0},
        "status": "ok",
    }

    # 先刷新内存缓存
    try:
        refresh_dragon_tiger()
        refresh_hot_rank()
    except Exception as e:
        logger.warning("[dragon_tiger_store] 刷新缓存失败: %s", e)

    # 读取缓存数据
    dt_data = get_dragon_tiger()
    
    # 获取热榜（pywencai 优先，东财兜底）
    hr_data = _fetch_hot_rank_from_pywencai(trade_date)
    if not hr_data:
        hr_data = _fetch_hot_rank_from_em(trade_date=trade_date)
    
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

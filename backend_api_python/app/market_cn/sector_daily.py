"""
板块热度每日统计（行业 + 概念）

每日收盘后预计算各板块的涨停数、涨跌比、平均涨幅、热度分，
存入 sector_daily_stats 表。

数据源:
  - stock_basic_info（industry + concepts）→ 股票-板块映射
  - kline_1D_YYYY（CNStock_db）→ 日K线（涨跌幅、成交量、涨跌停判定）

用法:
  from app.market_cn.sector_daily import sync_single_date, ensure_table
  sync_single_date()                           # 计算最近一个交易日
  sync_single_date("2026-05-26")               # 指定日期
"""

import logging
import datetime
from collections import defaultdict
from typing import Dict, List, Tuple

logger = get_logger = logging.getLogger(__name__)

# ============================================================
# 建表 DDL
# ============================================================

DDL = """
CREATE TABLE IF NOT EXISTS sector_daily_stats (
    date            VARCHAR(10)  NOT NULL,
    sector_type     VARCHAR(10)  NOT NULL,
    sector_name     VARCHAR(50)  NOT NULL,
    stock_count     INT DEFAULT 0,
    limit_up_count  INT DEFAULT 0,
    limit_down_count INT DEFAULT 0,
    advance_count   INT DEFAULT 0,
    decline_count   INT DEFAULT 0,
    total_volume    DOUBLE PRECISION DEFAULT 0,
    avg_return      DOUBLE PRECISION DEFAULT 0,
    advance_pct     DOUBLE PRECISION DEFAULT 0,
    heat_score      DOUBLE PRECISION DEFAULT 0,
    updated_at      TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (date, sector_type, sector_name)
);

CREATE INDEX IF NOT EXISTS idx_sds_date ON sector_daily_stats (date);
CREATE INDEX IF NOT EXISTS idx_sds_heat ON sector_daily_stats (date, heat_score DESC);
"""


def _get_pool():
    """获取 CNStock 连接池"""
    from app.utils.db_market import get_market_db_manager
    mgr = get_market_db_manager()
    mgr.ensure_market_db("CNStock")
    return mgr._get_pool("CNStock")


def ensure_table(pool=None):
    """确保 sector_daily_stats 表存在"""
    pool = pool or _get_pool()
    with pool.connection() as conn:
        cur = conn.cursor()
        cur.execute(DDL)
        conn.commit()
        cur.close()


# ============================================================
# 加载股票-板块映射
# ============================================================

def load_stock_sector_map(pool) -> Tuple[Dict[str, List[str]], Dict[str, List[str]]]:
    """
    从 stock_basic_info 加载映射:
      industry_map: {sector_name: [symbol, ...]}
      concept_map:  {sector_name: [symbol, ...]}
    """
    industry_map = defaultdict(list)
    concept_map = defaultdict(list)

    with pool.cursor() as cur:
        cur.execute(
            "SELECT symbol, industry, concepts FROM stock_basic_info "
            "WHERE status = 'active' AND (industry IS NOT NULL OR concepts IS NOT NULL)"
        )
        for row in cur.fetchall():
            symbol = row[0]
            industry = (row[1] or "").strip()
            concepts_raw = (row[2] or "").strip()

            if industry:
                industry_map[industry].append(symbol)

            if concepts_raw:
                for c in concepts_raw.split(","):
                    c = c.strip()
                    if c:
                        concept_map[c].append(symbol)

    return dict(industry_map), dict(concept_map)


# ============================================================
# 获取某日全部股票 K 线
# ============================================================

def load_kline_for_date(target_date: str, symbols: List[str] = None, pool=None) -> Dict[str, dict]:
    """
    读取指定日期的全部股票日K线。
    返回: {symbol: {"open", "high", "low", "close", "volume", "pre_close"}}
    """
    from app.utils.db_market import get_market_kline_writer
    from app.data_sources.normalizer import strip_market_prefix
    from app.utils.trading_calendar import prev_trading_day

    writer = get_market_kline_writer()

    if symbols is None:
        from app.utils.basicinfo_db import get_stock_basic_db
        db = get_stock_basic_db()
        p = db._get_pool()
        with p.cursor() as cur:
            cur.execute("SELECT symbol FROM stock_basic_info WHERE status = 'active'")
            symbols = [row[0] for row in cur.fetchall()]

    prev_date = prev_trading_day(target_date, n=1)
    today_data = {}

    for sym in symbols:
        db_symbol = strip_market_prefix(sym)
        try:
            rows = writer.query("CNStock", db_symbol, "1D",
                                start_time=prev_date, end_time=target_date, limit=5)
        except Exception:
            continue
        if not rows or len(rows) < 2:
            continue

        target_bar = None
        prev_close = None
        for i, r in enumerate(rows):
            t = r.get("time")
            if hasattr(t, 'strftime'):
                date_str = t.strftime("%Y-%m-%d")
            else:
                date_str = str(t)[:10]
            if date_str == target_date:
                target_bar = r
                if i > 0:
                    prev_close = float(rows[i - 1].get("close", 0))
                break

        if target_bar is None or prev_close is None or prev_close <= 0:
            continue

        today_data[sym] = {
            "open": float(target_bar.get("open", 0)),
            "high": float(target_bar.get("high", 0)),
            "low": float(target_bar.get("low", 0)),
            "close": float(target_bar.get("close", 0)),
            "volume": float(target_bar.get("volume", 0)),
            "pre_close": prev_close,
        }

    return today_data


# ============================================================
# 涨跌停判定
# ============================================================

def _get_limit_ratio(symbol: str) -> float:
    """获取涨跌停幅度: 主板10%/创业科创20%/北交30%/ST5%"""
    code = symbol.split(".")[0] if "." in symbol else symbol
    if code.startswith(("300", "301", "688", "689")):
        return 0.20
    if code.startswith(("43", "82", "83", "87", "88")):
        return 0.30
    return 0.10


def _is_limit_up(close, high, pre_close, ratio):
    if pre_close <= 0:
        return False
    lp = round(pre_close * (1 + ratio), 2)
    return abs(close - lp) < 0.02 and abs(high - lp) < 0.02


def _is_limit_down(close, low, pre_close, ratio):
    if pre_close <= 0:
        return False
    lp = round(pre_close * (1 - ratio), 2)
    return abs(close - lp) < 0.02 and abs(low - lp) < 0.02


# ============================================================
# 计算单日板块统计
# ============================================================

def calc_sector_stats(
    target_date: str,
    sector_type: str,
    sector_map: Dict[str, List[str]],
    kline_data: Dict[str, dict],
) -> List[dict]:
    """计算指定日期、指定类型的所有板块统计。"""
    results = []

    for sector_name, symbols in sector_map.items():
        stock_count = limit_up_count = limit_down_count = 0
        advance_count = decline_count = 0
        total_volume = 0.0
        returns = []

        for sym in symbols:
            kd = kline_data.get(sym)
            if not kd:
                continue
            pre_close = kd.get("pre_close", 0)
            if pre_close <= 0:
                continue

            close = kd["close"]
            high = kd["high"]
            low = kd["low"]
            volume = kd["volume"]
            ret = (close - pre_close) / pre_close * 100

            stock_count += 1
            total_volume += volume
            returns.append(ret)
            if ret > 0:
                advance_count += 1
            elif ret < 0:
                decline_count += 1

            ratio = _get_limit_ratio(sym)
            if _is_limit_up(close, high, pre_close, ratio):
                limit_up_count += 1
            if _is_limit_down(close, low, pre_close, ratio):
                limit_down_count += 1

        if stock_count == 0:
            continue

        avg_return = sum(returns) / len(returns) if returns else 0.0
        advance_pct = (advance_count / stock_count * 100) if stock_count > 0 else 0.0
        heat_score = limit_up_count * 3 + advance_pct * 0.5 + avg_return * 0.2

        results.append({
            "date": target_date,
            "sector_type": sector_type,
            "sector_name": sector_name,
            "stock_count": stock_count,
            "limit_up_count": limit_up_count,
            "limit_down_count": limit_down_count,
            "advance_count": advance_count,
            "decline_count": decline_count,
            "total_volume": round(total_volume, 2),
            "avg_return": round(avg_return, 4),
            "advance_pct": round(advance_pct, 2),
            "heat_score": round(heat_score, 4),
        })

    return results


# ============================================================
# 写库
# ============================================================

def write_stats(pool, stats: List[dict]):
    """批量写入 sector_daily_stats（UPSERT）"""
    if not stats:
        return 0

    with pool.connection() as conn:
        cur = conn.cursor()
        batch = []
        now = datetime.datetime.now()

        for s in stats:
            batch.append((
                s["date"], s["sector_type"], s["sector_name"],
                s["stock_count"], s["limit_up_count"], s["limit_down_count"],
                s["advance_count"], s["decline_count"],
                s["total_volume"], s["avg_return"], s["advance_pct"],
                s["heat_score"], now,
            ))
            if len(batch) >= 500:
                _flush_batch(cur, batch)
                batch = []

        if batch:
            _flush_batch(cur, batch)

        conn.commit()
        cur.close()

    return len(stats)


def _flush_batch(cur, batch):
    args_str = ",".join(
        cur.mogrify(
            "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", row
        ).decode() for row in batch
    )
    cur.execute(f"""
        INSERT INTO sector_daily_stats
            (date, sector_type, sector_name, stock_count,
             limit_up_count, limit_down_count, advance_count, decline_count,
             total_volume, avg_return, advance_pct, heat_score, updated_at)
        VALUES {args_str}
        ON CONFLICT (date, sector_type, sector_name) DO UPDATE SET
            stock_count     = EXCLUDED.stock_count,
            limit_up_count  = EXCLUDED.limit_up_count,
            limit_down_count = EXCLUDED.limit_down_count,
            advance_count   = EXCLUDED.advance_count,
            decline_count   = EXCLUDED.decline_count,
            total_volume    = EXCLUDED.total_volume,
            avg_return      = EXCLUDED.avg_return,
            advance_pct     = EXCLUDED.advance_pct,
            heat_score      = EXCLUDED.heat_score,
            updated_at      = EXCLUDED.updated_at
    """)


# ============================================================
# 主入口
# ============================================================

def sync_single_date(target_date: str = None, pool=None, dry_run: bool = False) -> int:
    """计算并写入单日板块统计。返回写入条数。"""
    import datetime as _dt

    if target_date is None:
        target_date = _dt.date.today().strftime("%Y-%m-%d")

    pool = pool or _get_pool()
    ensure_table(pool)

    logger.info("[sector_daily] 加载 stock_basic_info 映射...")
    industry_map, concept_map = load_stock_sector_map(pool)
    logger.info("[sector_daily] 行业=%d, 概念=%d", len(industry_map), len(concept_map))

    logger.info("[%s] 加载 K 线...", target_date)
    kline_data = load_kline_for_date(target_date, pool=pool)
    logger.info("[%s] %d 只股票", target_date, len(kline_data))

    if not kline_data:
        logger.warning("[%s] 无 K 线数据，跳过", target_date)
        return 0

    ind_stats = calc_sector_stats(target_date, "industry", industry_map, kline_data)
    con_stats = calc_sector_stats(target_date, "concept", concept_map, kline_data)
    all_stats = ind_stats + con_stats

    logger.info("[%s] 行业=%d 板块, 概念=%d 板块", target_date, len(ind_stats), len(con_stats))

    if dry_run:
        return len(all_stats)

    written = write_stats(pool, all_stats)
    logger.info("[%s] 写入 %d 条", target_date, written)
    return written


def get_sector_stats_from_db(target_date: str, sector_type: str = None, pool=None) -> List[dict]:
    """从 DB 读取某日板块统计。"""
    pool = pool or _get_pool()
    with pool.cursor() as cur:
        if sector_type:
            cur.execute(
                "SELECT * FROM sector_daily_stats WHERE date=%s AND sector_type=%s ORDER BY heat_score DESC",
                (target_date, sector_type)
            )
        else:
            cur.execute(
                "SELECT * FROM sector_daily_stats WHERE date=%s ORDER BY heat_score DESC",
                (target_date,)
            )
        cols = [desc[0] for desc in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_sector_history_from_db(
    sector_type: str = "industry",
    start_date: str = None,
    end_date: str = None,
    days: int = 30,
    pool=None,
) -> List[dict]:
    """从 DB 读取板块历史统计（按日期范围）。"""
    import datetime as _dt

    pool = pool or _get_pool()
    if end_date is None:
        end_date = _dt.date.today().strftime("%Y-%m-%d")
    if start_date is None:
        start_date = (_dt.date.today() - _dt.timedelta(days=days)).strftime("%Y-%m-%d")

    with pool.cursor() as cur:
        cur.execute(
            "SELECT * FROM sector_daily_stats "
            "WHERE sector_type=%s AND date>=%s AND date<=%s "
            "ORDER BY date, heat_score DESC",
            (sector_type, start_date, end_date)
        )
        cols = [desc[0] for desc in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

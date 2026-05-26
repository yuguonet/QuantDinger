#!/usr/bin/env python3
"""
sync_sector_daily.py — 板块热度每日统计（行业 + 概念）

V1 策略（追连板）需要板块热度作为过滤维度。
每日收盘后预计算各板块的涨停数、涨跌比、平均涨幅、热度分，存入 sector_daily_stats 表。

数据源:
  - stock_basic_info（industry + concepts）→ 股票-板块映射
  - kline_1D_YYYY（CNStock_db）→ 日K线（涨跌幅、成交量、涨跌停判定）

用法:
  python scripts/sync_sector_daily.py                # 计算最近一个交易日
  python scripts/sync_sector_daily.py --date 2026-05-26
  python scripts/sync_sector_daily.py --backfill 2024-01-01   # 回填历史
  python scripts/sync_sector_daily.py --dry-run               # 只看不写
"""

import sys
import argparse
import datetime
from pathlib import Path
from collections import defaultdict
from typing import Dict, List, Tuple

# ============================================================
# 路径 & 环境
# ============================================================
_root = Path(__file__).resolve().parent.parent  # scripts/ → QuantDinger/
sys.path.insert(0, str(_root / "backend_api_python"))

try:
    from dotenv import load_dotenv
    load_dotenv(_root / "backend_api_python" / ".env")
    load_dotenv(_root / ".env")
except ImportError:
    pass

sys.path.insert(0, str(_root))
sys.path.insert(0, str(_root / "backend_api_python"))


# ============================================================
# 数据库连接
# ============================================================

def _get_pools():
    """获取 CNStock 连接池（stock_basic_info + kline 共用）"""
    from app.utils.db_market import get_market_db_manager
    mgr = get_market_db_manager()
    mgr.ensure_market_db("CNStock")
    pool = mgr._get_pool("CNStock")
    return pool, mgr


# ============================================================
# 建表
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


def ensure_table(pool):
    """确保 sector_daily_stats 表存在"""
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

    一只股票可能属于多个概念，概念用逗号分隔。
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

def load_kline_for_date(target_date: str) -> Dict[str, dict]:
    """
    通过 MarketKlineWriter 读取指定日期的全部股票日K线。
    模式与 cn_stock.py 的 _read_db 一致：全量读取，内存过滤。

    返回: {symbol: {"open": float, "high": float, "low": float,
                     "close": float, "volume": float, "pre_close": float}}
    """
    from app.utils.db_market import get_market_kline_writer
    from app.data_sources.normalizer import strip_market_prefix

    writer = get_market_kline_writer()

    # 从 stock_basic_info 拿全量 symbol
    from app.utils.basicinfo_db import get_stock_basic_db
    db = get_stock_basic_db()
    pool = db._get_pool()
    with pool.cursor() as cur:
        cur.execute("SELECT symbol FROM stock_basic_info WHERE status = 'active'")
        symbols = [row[0] for row in cur.fetchall()]

    today_data = {}
    pre_close_map = {}

    for sym in symbols:
        db_symbol = strip_market_prefix(sym)
        try:
            rows = writer.query("CNStock", db_symbol, "1D",
                                start_time=None, end_time=None, limit=10000)
        except Exception:
            continue
        if not rows:
            continue

        # 内存里找 target_date 和前一日
        target_bar = None
        prev_close = None
        for i, r in enumerate(rows):
            t = r.get("time")
            # time 可能是 datetime 或 timestamp
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

def _get_limit_ratio(symbol: str, name: str = "") -> float:
    """获取涨跌停幅度

    主板(60/00): 10%
    创业板(30)/科创板(68): 20%
    ST: 5%
    北交所(43/82/83/87/88): 30%
    """
    code = symbol.split(".")[0] if "." in symbol else symbol
    if name and "ST" in name.upper():
        return 0.05
    if code.startswith(("300", "301", "688", "689")):
        return 0.20
    if code.startswith(("43", "82", "83", "87", "88")):
        return 0.30
    return 0.10


def is_limit_up(close: float, high: float, pre_close: float, limit_ratio: float) -> bool:
    """判断是否涨停：收盘价等于涨停价且最高价等于涨停价"""
    if pre_close <= 0:
        return False
    limit_price = round(pre_close * (1 + limit_ratio), 2)
    return abs(close - limit_price) < 0.02 and abs(high - limit_price) < 0.02


def is_limit_down(close: float, low: float, pre_close: float, limit_ratio: float) -> bool:
    """判断是否跌停：收盘价等于跌停价且最低价等于跌停价"""
    if pre_close <= 0:
        return False
    limit_price = round(pre_close * (1 - limit_ratio), 2)
    return abs(close - limit_price) < 0.02 and abs(low - limit_price) < 0.02


# ============================================================
# 计算单日板块统计
# ============================================================

def calc_sector_stats(
    target_date: str,
    sector_type: str,
    sector_map: Dict[str, List[str]],
    kline_data: Dict[str, dict],
) -> List[dict]:
    """
    计算指定日期、指定类型（industry/concept）的所有板块统计。

    返回: [{date, sector_type, sector_name, stock_count, limit_up_count, ...}, ...]
    """
    results = []

    for sector_name, symbols in sector_map.items():
        stock_count = 0
        limit_up_count = 0
        limit_down_count = 0
        advance_count = 0
        decline_count = 0
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
            ret = (close - pre_close) / pre_close * 100  # 涨跌幅%

            stock_count += 1
            total_volume += volume
            returns.append(ret)

            if ret > 0:
                advance_count += 1
            elif ret < 0:
                decline_count += 1

            limit_ratio = _get_limit_ratio(sym)
            if is_limit_up(close, high, pre_close, limit_ratio):
                limit_up_count += 1
            if is_limit_down(close, low, pre_close, limit_ratio):
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
    """批量 UPSERT"""
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
# 获取交易日列表（回填用）
# ============================================================

def get_trading_dates(start_date: str, end_date: str) -> List[str]:
    """获取有数据的交易日列表（用一只股票采样）"""
    from app.utils.db_market import get_market_kline_writer
    from app.data_sources.normalizer import strip_market_prefix

    writer = get_market_kline_writer()

    from app.utils.basicinfo_db import get_stock_basic_db
    db = get_stock_basic_db()
    pool = db._get_pool()
    with pool.cursor() as cur:
        cur.execute("SELECT symbol FROM stock_basic_info WHERE status='active' LIMIT 1")
        row = cur.fetchone()
        if not row:
            return []

    db_symbol = strip_market_prefix(row[0])
    rows = writer.query("CNStock", db_symbol, "1D",
                        start_time=None, end_time=None, limit=10000)
    dates = set()
    for r in rows:
        t = r.get("time")
        if hasattr(t, 'strftime'):
            d = t.strftime("%Y-%m-%d")
        else:
            d = str(t)[:10]
        if d >= start_date and d <= end_date:
            dates.add(d)
    return sorted(dates)


# ============================================================
# 主流程
# ============================================================

def run_single_date(pool, target_date: str, industry_map, concept_map, dry_run=False):
    """计算并写入单日统计"""
    print(f"\n[{target_date}] 加载 K 线...", end="", flush=True)
    kline_data = load_kline_for_date(target_date)
    print(f" {len(kline_data)} 只股票", flush=True)

    if not kline_data:
        print(f"  ⚠️  无 K 线数据，跳过")
        return 0

    # 计算行业板块
    ind_stats = calc_sector_stats(target_date, "industry", industry_map, kline_data)
    # 计算概念板块
    con_stats = calc_sector_stats(target_date, "concept", concept_map, kline_data)

    all_stats = ind_stats + con_stats
    print(f"  行业: {len(ind_stats)} 个板块, 概念: {len(con_stats)} 个板块")

    # 打印 top 10
    top = sorted(all_stats, key=lambda x: -x["heat_score"])[:10]
    print(f"  🔥 Top 10 热度:")
    for s in top:
        tag = "行业" if s["sector_type"] == "industry" else "概念"
        print(f"    [{tag}] {s['sector_name']}: "
              f"热度={s['heat_score']:.1f} "
              f"涨停={s['limit_up_count']} "
              f"涨比={s['advance_pct']:.0f}% "
              f"均涨幅={s['avg_return']:.2f}%")

    if dry_run:
        print(f"  [Dry-Run] 不写库")
        return len(all_stats)

    written = write_stats(pool, all_stats)
    print(f"  ✅ 写入 {written} 条")
    return written


def main():
    parser = argparse.ArgumentParser(description="板块热度每日统计（行业+概念）")
    parser.add_argument("--date", type=str, help="指定日期 YYYY-MM-DD")
    parser.add_argument("--backfill", type=str, help="回填起始日期 YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true", help="只看不写")
    args = parser.parse_args()

    print(f"[开始] {datetime.datetime.now().strftime('%H:%M:%S')}")

    pool, _ = _get_pools()
    ensure_table(pool)

    # 加载股票-板块映射
    print("[加载] stock_basic_info 映射...", end="", flush=True)
    industry_map, concept_map = load_stock_sector_map(pool)
    print(f" 行业={len(industry_map)} 个, 概念={len(concept_map)} 个")

    if args.backfill:
        # 回填模式
        end_date = args.date or datetime.date.today().strftime("%Y-%m-%d")
        dates = get_trading_dates(args.backfill, end_date)
        print(f"[回填] {args.backfill} → {end_date}, 共 {len(dates)} 个交易日")
        total = 0
        for d in dates:
            n = run_single_date(pool, d, industry_map, concept_map, args.dry_run)
            total += n
        print(f"\n[完成] 共写入 {total} 条")
    else:
        # 单日模式
        target = args.date or datetime.date.today().strftime("%Y-%m-%d")
        run_single_date(pool, target, industry_map, concept_map, args.dry_run)

    print(f"[完成] {datetime.datetime.now().strftime('%H:%M:%S')}")


if __name__ == "__main__":
    main()

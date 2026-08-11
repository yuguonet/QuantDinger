#!/usr/bin/env python3
"""
fix_kline_1d_time.py — 存量 kline_1D_YYYY 数据时间归一化

背景: 历史上存在两条写入路径以 00:00:00 写入 1D K 线
  - backfill_db（mootdx 盘后覆写）
  - index_daily / sync_index_daily（指数日线）
而 cn_stock 路径写 15:00:00，导致同一天 00:00 与 15:00 两条记录并存。

本脚本将 kline_1D_YYYY 中所有非 15:00:00 的记录统一为 15:00:00：
  1. 若同 symbol 同日期已有 15:00:00 记录 → 删除非 15:00 的记录（去重）
  2. 剩余非 15:00:00 记录 → 更新为当天 15:00:00

用法:
  python scripts/fix_kline_1d_time.py                 # 只统计，不写库（默认）
  python scripts/fix_kline_1d_time.py --apply         # 真正执行修正
  python scripts/fix_kline_1d_time.py --year 2025     # 只处理指定年份
  python scripts/fix_kline_1d_time.py --apply --year 2025
"""
import argparse
import sys
from pathlib import Path

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
# 数据库访问
# ============================================================

def get_pool():
    from app.utils.db_market import get_market_db_manager
    mgr = get_market_db_manager()
    mgr.ensure_market_db("CNStock")
    return mgr._get_pool("CNStock")


def list_1d_tables(pool, year: int = None):
    """列出 CNStock 库中所有 kline_1D_YYYY 表。"""
    with pool.cursor() as cur:
        cur.execute("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_type = 'BASE TABLE'
              AND table_name LIKE 'kline_1D_%'
            ORDER BY table_name
        """)
        tables = [r[0] for r in cur.fetchall()]
    if year:
        tables = [t for t in tables if t.endswith(str(year))]
    return tables


def table_stats(pool, table):
    """返回 (总行数, 非15:00行数, 00:00行数)。"""
    with pool.connection() as conn:
        conn.set_client_encoding("UTF8")
        cur = conn.cursor()
        cur.execute(f'SELECT COUNT(*) FROM "{table}"')
        total = cur.fetchone()[0]
        cur.execute(f'SELECT COUNT(*) FROM "{table}" WHERE EXTRACT(HOUR FROM time) != 15')
        bad = cur.fetchone()[0]
        cur.execute(f'SELECT COUNT(*) FROM "{table}" WHERE EXTRACT(HOUR FROM time) = 0')
        midnight = cur.fetchone()[0]
    return total, bad, midnight


def fix_table(pool, table):
    """修正单张表，返回 (删除条数, 更新条数)。"""
    with pool.connection() as conn:
        conn.set_client_encoding("UTF8")
        cur = conn.cursor()
        # 1. 同 symbol 同日期已有 15:00 记录 → 删除非 15:00 的重复
        cur.execute(f"""
            DELETE FROM "{table}" d
            USING "{table}" k
            WHERE d.symbol = k.symbol
              AND d.time::date = k.time::date
              AND EXTRACT(HOUR FROM d.time) != 15
              AND EXTRACT(HOUR FROM k.time) = 15
        """)
        deleted = cur.rowcount
        # 2. 剩余非 15:00 记录 → 归一到当天 15:00:00
        cur.execute(f"""
            UPDATE "{table}"
            SET time = date_trunc('day', time) + interval '15 hours'
            WHERE EXTRACT(HOUR FROM time) != 15
        """)
        updated = cur.rowcount
        conn.commit()
    return deleted, updated


# ============================================================
# 主逻辑
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="存量 kline_1D_YYYY 时间归一化到 15:00:00")
    parser.add_argument("--apply", action="store_true", help="真正执行修正（默认只统计）")
    parser.add_argument("--year", type=int, default=None, help="只处理指定年份")
    args = parser.parse_args()

    pool = get_pool()
    tables = list_1d_tables(pool, args.year)
    if not tables:
        print("未找到 kline_1D_* 表")
        sys.exit(1)

    print("=" * 62)
    print(f"  kline_1D 时间归一化  |  {'APPLY' if args.apply else '统计模式（加 --apply 执行）'}")
    print("=" * 62)

    total_bad = 0
    for table in tables:
        total, bad, midnight = table_stats(pool, table)
        if bad == 0:
            print(f"  {table:<20} {total:>9,} 行  |  非15:00: {bad:>8,}  00:00: {midnight:>8,}  (已一致)")
            continue
        total_bad += bad
        print(f"  {table:<20} {total:>9,} 行  |  非15:00: {bad:>8,}  00:00: {midnight:>8,}")

    if total_bad == 0:
        print("\n  无需修正：所有 1D K 线时间已统一为 15:00:00")
        return

    if not args.apply:
        print(f"\n  共 {total_bad:,} 条非 15:00 记录待修正。加 --apply 执行。")
        return

    print("\n  开始修正 ...")
    total_deleted = 0
    total_updated = 0
    for table in tables:
        total, bad, midnight = table_stats(pool, table)
        if bad == 0:
            continue
        deleted, updated = fix_table(pool, table)
        total_deleted += deleted
        total_updated += updated
        new_total, new_bad, _ = table_stats(pool, table)
        print(f"  {table:<20} 删除重复 {deleted:>7,} | 更新 {updated:>7,} | 剩余非15:00 {new_bad:>6,}")

    print("=" * 62)
    print(f"  完成: 删除 {total_deleted:,} 条重复, 更新 {total_updated:,} 条为 15:00:00")
    print("=" * 62)


if __name__ == "__main__":
    main()

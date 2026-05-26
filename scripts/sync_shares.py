#!/usr/bin/env python3
"""
sync_shares.py
从东财 push2 API 批量抓取全 A 股的 总股本 / 流通股本 / 总市值 / 流通市值，
写入 stock_basic_info 表。

数据源:
  东财 push2 API（批量分页，约 60 页，2~3 分钟完成）
  字段: f38=总股本, f20=总市值, f21=流通市值
  流通股本 = 流通市值 ÷ (总市值 ÷ 总股本) 推算

用法:
  python scripts/sync_shares.py              # 抓取 + 写库
  python scripts/sync_shares.py --dry-run    # 只看不写
"""

import sys
import os
import json
import time
import random
import datetime
import argparse
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
# 东财 push2 批量拉取
# ============================================================

def _detect_market(code: str) -> str:
    c = (code or "").strip()
    if not c.isdigit() or len(c) != 6:
        return ""
    if c.startswith(("600", "601", "603", "605", "688", "689", "900")):
        return "SH"
    if c.startswith(("000", "001", "002", "003", "300", "301", "200")):
        return "SZ"
    if c.startswith(("43", "82", "83", "87", "88")):
        return "BJ"
    return ""


def _safe_float(v):
    """东财返回 '-' / None / 负数 都视为无效，返回 0.0"""
    if v is None or v == "-" or v == "":
        return 0.0
    try:
        val = float(v)
        return val if val > 0 else 0.0
    except (ValueError, TypeError):
        return 0.0


def fetch_eastmoney_shares():
    """
    从东财 push2 API 分页拉取全 A 股的 股本/市值 数据。

    可靠字段:
      f12  = 股票代码
      f14  = 股票名称
      f38  = 总股本（股）—— 可靠
      f20  = 总市值（元）—— 可靠
      f21  = 流通市值（元）—— 可靠

    流通股本 = 流通市值 ÷ 股价 = f21 ÷ (f20 ÷ f38)
    弃用字段: f84(流通股本, 大量负值) f116(返回"-") f85(比例非股数)

    Returns:
      [{"symbol": "600519", "name": "贵州茅台", "market_cn": "SH",
        "total_shares": 1258000000, "circ_shares": 1258000000}, ...]
    """
    import urllib.request
    import urllib.parse

    print("[抓取] 东财 push2 API 分页拉取股本/市值...", file=sys.stderr)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/131.0.0.0 Safari/537.36",
        "Referer": "https://quote.eastmoney.com/",
    }

    base_url = "https://push2.eastmoney.com/api/qt/clist/get"
    all_items = []
    page = 1
    page_size = 100  # 东财实际每页上限约 100

    while True:
        # f38=总股本(可靠) f20=总市值(可靠) f21=流通市值(可靠)
        # 弃用: f84(流通股本,负值) f116(返回"-") f85(比例非股数)
        params = {
            "pn": page,
            "pz": page_size,
            "po": 1,
            "np": 1,
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": 2,
            "invt": 2,
            "fid": "f3",
            "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
            "fields": "f12,f14,f38,f20,f21",
        }

        # 用 urllib.request 每次新建连接，避免 requests.Session 复用被东财断开
        # 东财限流较严，需要较多重试 + 指数退避
        data = None
        full_url = f"{base_url}?{urllib.parse.urlencode(params)}"
        for attempt in range(6):
            try:
                req = urllib.request.Request(full_url, headers=headers)
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                break
            except Exception as e:
                if attempt < 5:
                    wait = 3 * (2 ** attempt) + random.uniform(1, 3)
                    print(f"  [重试] 第 {page} 页 (第{attempt+1}次)，{wait:.1f}s 后重试: {e}", file=sys.stderr)
                    time.sleep(wait)
                else:
                    raise

        if data is None:
            break

        items = ((data or {}).get("data") or {}).get("diff")
        if not items:
            break

        all_items.extend(items)
        total = ((data or {}).get("data") or {}).get("total", 0)
        if page % 10 == 0:
            print(f"  进度 {len(all_items)}/{total}...", file=sys.stderr)
        if len(all_items) >= total:
            break
        page += 1
        time.sleep(random.uniform(1.0, 2.5))  # 东财限流严，页间多等一会

    if not all_items:
        print("[错误] 东财返回空数据", file=sys.stderr)
        return []

    result = []
    for item in all_items:
        if not isinstance(item, dict):
            continue
        code = str(item.get("f12", "")).strip()
        name = str(item.get("f14", "")).strip()
        if not code or not name or len(code) != 6 or not code.isdigit():
            continue

        total_shares = _safe_float(item.get("f38"))
        total_mv = _safe_float(item.get("f20"))
        circ_mv = _safe_float(item.get("f21"))

        # 推算流通股本: 流通市值 ÷ 股价, 股价 = 总市值 ÷ 总股本
        circ_shares = 0.0
        if total_shares > 0 and total_mv > 0 and circ_mv > 0:
            price = total_mv / total_shares  # 股价（元）
            if price > 0:
                circ_shares = circ_mv / price

        result.append({
            "symbol": code,
            "name": name,
            "market_cn": _detect_market(code),
            "total_shares": total_shares,
            "circ_shares": circ_shares,
        })

    print(f"[完成] 东财返回 {len(result)} 只股票（共 {page} 页）", file=sys.stderr)
    return result


# ============================================================
# 写库
# ============================================================

def write_to_db(stocks: list, dry_run=False):
    """将总股本数据写入 stock_basic_info 表

    逻辑:
      1. 读取 DB 已有数据，只更新 DB 中已存在的记录
      2. 非零覆盖：新值 > 0 时才覆盖，否则保留旧值
    """

    if dry_run:
        print(f"\n[Dry-Run] 将更新 {len(stocks)} 只股票:", file=sys.stderr)
        for s in stocks[:10]:
            ts = s["total_shares"]
            cs = s["circ_shares"]
            print(f"  {s['symbol']} {s['name']}: "
                  f"总股本={ts/1e8:.2f}亿 流通股本={cs/1e8:.2f}亿",
                  file=sys.stderr)
        print(f"  ... 共 {len(stocks)} 只", file=sys.stderr)
        return

    from app.utils.basicinfo_db import get_stock_basic_db
    db = get_stock_basic_db()
    db.ensure_table()
    pool = db._get_pool()

    # ── 确保列存在 ──
    with pool.connection() as conn:
        cur = conn.cursor()
        for col, typ in [("total_shares", "float8"), ("circ_shares", "float8")]:
            cur.execute(f"""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'stock_basic_info' AND column_name = '{col}'
                    ) THEN
                        ALTER TABLE stock_basic_info ADD COLUMN {col} {typ} DEFAULT 0;
                    END IF;
                END $$;
            """)
        conn.commit()
        cur.close()
    print("[DB] 表结构已就绪", file=sys.stderr)

    # ── 获取 DB 中已有的 symbol ──
    db_codes = set()
    with pool.cursor() as cur:
        cur.execute("SELECT symbol FROM stock_basic_info")
        db_codes = {row[0] for row in cur.fetchall()}

    target = [s for s in stocks if s["symbol"] in db_codes]
    skipped = len(stocks) - len(target)
    if skipped:
        print(f"[跳过] {skipped} 只股票不在 DB 中", file=sys.stderr)

    if not target:
        print("[完成] 无需更新（DB 中无匹配记录）", file=sys.stderr)
        return

    # ── 3. 批量 UPDATE ──
    print(f"[写库] 更新 {len(target)} 只股票...", file=sys.stderr)
    now = datetime.datetime.now()
    updated = 0

    with pool.connection() as conn:
        cur = conn.cursor()
        for s in target:
            ts = s["total_shares"]
            cs = s["circ_shares"]
            cur.execute("""
                UPDATE stock_basic_info SET
                    total_shares = CASE WHEN %s > 0 THEN %s ELSE total_shares END,
                    circ_shares  = CASE WHEN %s > 0 THEN %s ELSE circ_shares  END,
                    updated_at   = %s
                WHERE symbol = %s
            """, (ts, ts, cs, cs, now, s["symbol"]))
            updated += cur.rowcount
        conn.commit()
        cur.close()

    print(f"✅ 已更新 {updated} 只股票的股本/市值数据", file=sys.stderr)


# ============================================================
# main
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="从东财批量抓取总股本/流通股本，写入 stock_basic_info")
    parser.add_argument("--dry-run", action="store_true", help="只看不写")
    args = parser.parse_args()

    print(f"[开始] {datetime.datetime.now().strftime('%H:%M:%S')}", file=sys.stderr)

    stocks = fetch_eastmoney_shares()
    if not stocks:
        print("[错误] 未获取到数据", file=sys.stderr)
        sys.exit(1)

    write_to_db(stocks, dry_run=args.dry_run)

    print(f"\n[完成] {datetime.datetime.now().strftime('%H:%M:%S')}", file=sys.stderr)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# bat_in_kline.py — 全市场K线批量拉取 → 比对 → 存库
# ============================================================================
#
# 数据源:
#   日K:  东方财富 clist 接口 (1次HTTP拉全市场当天快照)
#   15min: 新浪财经 (逐只拉取, 30并发)
#
# 流程:
#   1. 东财 clist 一次HTTP拉全市场日K快照
#   2. 新浪 30并发拉全市场15min
#   3. 写入DB
#
# 用法:
#   python bat_in_kline.py                      # 全量拉取+存库
#   python bat_in_kline.py --dry-run            # 只拉取不写DB
#   python bat_in_kline.py --symbol 600519      # 单只股票
#   python bat_in_kline.py --update             # 含时间窗口检查
#   python bat_in_kline.py --update --force-full
#
# 修改时间: 2026-05-07
# ============================================================================

from __future__ import annotations

import os
import sys
import csv
import json
import math
import signal
import logging
import argparse
import traceback
import requests
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple, Set
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 路径 & 环境
# ---------------------------------------------------------------------------

def _find_project_root() -> str:
    d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(5):
        if os.path.isdir(os.path.join(d, "backend_api_python")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    cwd = os.getcwd()
    if os.path.isdir(os.path.join(cwd, "backend_api_python")):
        return cwd
    parent_cwd = os.path.dirname(cwd)
    if os.path.isdir(os.path.join(parent_cwd, "backend_api_python")):
        return parent_cwd
    return cwd

PROJECT_ROOT = _find_project_root()
sys.path.insert(0, os.path.join(PROJECT_ROOT, "backend_api_python"))


def _load_env():
    try:
        from dotenv import load_dotenv
        for p in [
            os.path.join(PROJECT_ROOT, "backend_api_python", ".env"),
            os.path.join(PROJECT_ROOT, ".env"),
        ]:
            if os.path.isfile(p):
                load_dotenv(p, override=False)
                break
    except Exception:
        pass

_load_env()

# ---------------------------------------------------------------------------
# 时间 & 工具
# ---------------------------------------------------------------------------

TZ_SH = timezone(timedelta(hours=8))
_TRADING_DAY_SET: frozenset[str] | None = None
_TRADING_DAY_SILENT: bool = False

def _build_trading_day_cache(market: str = "CNStock", silent: bool = False):
    global _TRADING_DAY_SET
    if _TRADING_DAY_SET is not None:
        return
    from app.utils.trading_calendar import trade_date_range
    end_year = datetime.now(TZ_SH).year + 1
    dates = trade_date_range("2015-01-01", f"{end_year}-12-31")
    _TRADING_DAY_SET = frozenset(dates)
    if not silent:
        print(f"📅 交易日历: {len(_TRADING_DAY_SET)} 天")

def _is_trading_day(d: str) -> bool:
    if _TRADING_DAY_SET is None:
        _build_trading_day_cache(silent=_TRADING_DAY_SILENT)
    return d in _TRADING_DAY_SET

def _ts_to_date(ts) -> str:
    if isinstance(ts, datetime):
        dt = ts if ts.tzinfo else ts.replace(tzinfo=TZ_SH)
        return dt.strftime("%Y-%m-%d")
    if isinstance(ts, str):
        return ts[:10]
    return datetime.fromtimestamp(ts, tz=TZ_SH).strftime("%Y-%m-%d")

def _date_to_ts_midnight(d: str) -> datetime:
    return datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=TZ_SH)

def _safe_float(v, default: float = 0.0) -> float:
    if v is None:
        return default
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except (ValueError, TypeError):
        return default

def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# HTTP 会话
# ---------------------------------------------------------------------------

_HTTP = requests.Session()
_HTTP.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
})

MAX_WORKERS = 30


# ===================================================================
#  日K: 东方财富 clist (1次HTTP拉全市场)
# ===================================================================

def fetch_daily_snapshot() -> Dict[str, Dict[str, Any]]:
    """
    东财 clist 接口, 1次HTTP拉全市场当天日K快照。

    返回: {code: {"open": float, "high": float, "low": float,
                   "close": float, "volume": float, "name": str,
                   "pct_chg": float, "amount": float, "turnover": float}}
    """
    log("[东财] 1次HTTP拉全市场日K快照...")
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": 1, "pz": 6000,
        "po": 1, "np": 1,
        "fltt": 2, "invt": 2,
        "fid": "f3",
        "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
        # f2=最新价 f3=涨跌幅 f5=成交量 f6=成交额 f7=振幅
        # f10=量比 f15=最高 f16=最低 f17=今开 f18=昨收
        "fields": "f2,f3,f5,f6,f7,f10,f12,f14,f15,f16,f17,f18",
    }
    r = _HTTP.get(url, params=params, timeout=15)
    r.raise_for_status()
    data = r.json()

    items = data.get("data", {}).get("diff", [])
    if not items:
        log("[东财] 返回数据为空!")
        return {}

    result = {}
    for it in items:
        code = it.get("f12", "")
        name = it.get("f14", "")
        if not code:
            continue

        # 东财沪深代码 → sh/sz 前缀格式
        if code.startswith("6") or code.startswith("9"):
            sym = f"sh{code}"
        else:
            sym = f"sz{code}"

        result[sym] = {
            "code": code,
            "name": name,
            "open": _safe_float(it.get("f17")),      # 今开
            "high": _safe_float(it.get("f15")),       # 最高
            "low": _safe_float(it.get("f16")),        # 最低
            "close": _safe_float(it.get("f2")),       # 最新价
            "volume": _safe_float(it.get("f5")) * 100,  # 成交量: 手→股
            "amount": _safe_float(it.get("f6")),      # 成交额(元)
            "pct_chg": _safe_float(it.get("f3")),     # 涨跌幅(%)
            "amplitude": _safe_float(it.get("f7")),   # 振幅(%)
            "turnover": _safe_float(it.get("f10")),   # 量比
        }

    log(f"[东财] 获取 {len(result)} 只股票")
    return result


# ===================================================================
#  15min: 新浪财经 (逐只, 并发)
# ===================================================================

def fetch_15min_sina(sym: str, datalen: int = 100) -> Optional[List[Dict[str, Any]]]:
    """
    新浪15分钟K线, 单只股票。

    返回: [{"datetime": "2026-05-06 14:00:00", "open": float, ...}, ...]
    """
    url = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
    params = {"symbol": sym, "scale": 15, "ma": "no", "datalen": datalen}
    try:
        r = _HTTP.get(url, params=params, timeout=8)
        if r.status_code != 200:
            return None
        data = json.loads(r.text)
        if not data:
            return None
        bars = []
        for row in data:
            bars.append({
                "datetime": row["day"],
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
            })
        return bars
    except Exception:
        return None


def batch_fetch_15min(codes: List[str]) -> Dict[str, List[Dict[str, Any]]]:
    """并发拉取全市场15min K线"""
    log(f"\n[新浪-15min] 启动 ({len(codes)}只, {MAX_WORKERS}并发)")
    results = {}
    ok = fail = 0
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futs = {pool.submit(fetch_15min_sina, c): c for c in codes}
        for f in as_completed(futs):
            c = futs[f]
            try:
                bars = f.result()
                if bars:
                    results[c] = bars
                    ok += 1
                else:
                    fail += 1
            except Exception:
                fail += 1
            total = ok + fail
            if total % 500 == 0:
                log(f"  [15min] {total}/{len(codes)} ok={ok} {time.time()-t0:.0f}s")

    log(f"[15min] ok:{ok} fail:{fail} {time.time()-t0:.0f}s")
    return results


# ===================================================================
#  DB 写入
# ===================================================================

def write_snapshot_to_db(
    writer,
    market: str,
    snapshot: Dict[str, Dict[str, Any]],
    today: str,
    dry_run: bool = False,
) -> int:
    """
    将东财快照数据写入DB (1D)。
    每只股票写入1条当天数据。
    """
    if dry_run:
        log(f"[写入1D] dry-run, 跳过 ({len(snapshot)} 只)")
        return len(snapshot)

    log(f"[写入1D] {len(snapshot)} 只...")
    dt = _date_to_ts_midnight(today)
    records = []
    for sym, bar in snapshot.items():
        records.append({
            "symbol": bar["code"],
            "timeframe": "1D",
            "time": dt,
            "open": bar["open"],
            "high": bar["high"],
            "low": bar["low"],
            "close": bar["close"],
            "volume": bar["volume"],
        })

    try:
        result = writer.bulk_write(market, records, on_conflict="update", batch_size=5000)
        n = result.get("inserted", 0)
        log(f"[写入1D] 完成: {n} 条")
        return n
    except Exception as e:
        logger.warning(f"写入1D失败: {e}")
        return 0


def write_15min_to_db(
    writer,
    market: str,
    data: Dict[str, List[Dict[str, Any]]],
    dry_run: bool = False,
) -> int:
    """
    将新浪15min数据写入DB。
    每只股票写入多条15min数据。
    """
    if dry_run:
        total_bars = sum(len(bars) for bars in data.values())
        log(f"[写入15min] dry-run, 跳过 ({len(data)} 只, {total_bars} 条)")
        return total_bars

    total_written = 0
    total = len(data)
    t0 = time.time()

    for i, (sym, bars) in enumerate(data.items()):
        code = sym[2:]  # sh600000 → 600000
        records = []
        for bar in bars:
            try:
                dt = datetime.strptime(bar["datetime"][:16], "%Y-%m-%d %H:%M").replace(tzinfo=TZ_SH)
            except ValueError:
                continue
            records.append({
                "symbol": code,
                "timeframe": "15m",
                "time": dt,
                "open": bar["open"],
                "high": bar["high"],
                "low": bar["low"],
                "close": bar["close"],
                "volume": bar["volume"],
            })
        if records:
            try:
                result = writer.bulk_write(market, records, on_conflict="update", batch_size=5000)
                total_written += result.get("inserted", 0)
            except Exception as e:
                logger.warning(f"写入15min失败 {code}: {e}")

        if (i + 1) % 200 == 0 or i + 1 == total:
            log(f"  [写入15min] {i+1}/{total} written={total_written} {time.time()-t0:.0f}s")

    log(f"[写入15min] 完成: {total_written} 条")
    return total_written


# ===================================================================
#  质量检查 (保留)
# ===================================================================

def classify_bar_quality(bar: Dict[str, Any]) -> str:
    o = _safe_float(bar.get("open"))
    h = _safe_float(bar.get("high"))
    lo = _safe_float(bar.get("low"))
    c = _safe_float(bar.get("close"))
    v = _safe_float(bar.get("volume"))

    if o == 0 and h == 0 and lo == 0 and c == 0:
        return "bad"
    if v == 0 and o == h == lo == c and o > 0:
        return "suspended"
    if h > 0 and lo > 0 and (h < lo or (o > 0 and (o > h or o < lo)) or (c > 0 and (c > h or c < lo))):
        return "incomplete"
    if v == 0 and not (o == h == lo == c):
        return "incomplete"
    if (o == 0) != (h == 0) or (h == 0) != (lo == 0) or (lo == 0) != (c == 0):
        return "incomplete"
    return "ok"


def load_db_data(writer, market: str, code: str, timeframe: str) -> List[Dict[str, Any]]:
    try:
        records = writer.query(market, code, timeframe, limit=0)
        return records or []
    except Exception as e:
        logger.warning(f"DB查询失败 {code}/{timeframe}: {e}")
        return []


def check_15m_quality_db(writer, market: str, codes: List[str]) -> Dict[str, Any]:
    """从DB读取15m数据做质量检查"""
    log(f"\n── 15m DB 质量检查 ({len(codes)} 只) ──")
    results = []
    stats = {"total": 0, "quality_issues": 0}

    for i, code in enumerate(codes):
        stats["total"] += 1
        try:
            db_15m = load_db_data(writer, market, code, "15m")
            if not db_15m:
                continue
            issues = []
            for r in db_15m:
                q = classify_bar_quality(r)
                if q != "ok":
                    issues.append({
                        "date": _ts_to_date(r["time"]),
                        "quality": q,
                        "db_ohlc": {
                            "open": _safe_float(r.get("open")),
                            "high": _safe_float(r.get("high")),
                            "low": _safe_float(r.get("low")),
                            "close": _safe_float(r.get("close")),
                            "volume": _safe_float(r.get("volume")),
                        },
                    })
            if issues:
                stats["quality_issues"] += len(issues)
                results.append({"timeframe": "15m", "code": code, "quality_issues": issues})
        except Exception as e:
            logger.warning(f"15m质量检查失败 {code}: {e}")

        if (i + 1) % 500 == 0 or i + 1 == len(codes):
            log(f"  [15m质量] {i+1}/{len(codes)} 问题={stats['quality_issues']}")

    return {"results": results, "stats": stats}


def get_db_symbols(writer, market: str) -> Set[str]:
    try:
        from app.utils.db_market import get_market_db_manager
        mgr = get_market_db_manager()
        pool = mgr._get_pool(market)
        with pool.cursor() as cur:
            cur.execute("""
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_type = 'BASE TABLE'
                  AND table_name LIKE 'kline_1D_%'
                  AND table_name NOT LIKE '%%_from_%%'
            """)
            tables = [r[0] for r in cur.fetchall()]
        if not tables:
            return set()
        syms: Set[str] = set()
        with pool.cursor() as cur:
            for tbl in tables:
                try:
                    cur.execute(f'SELECT DISTINCT symbol FROM "{tbl}"')
                    for r in cur.fetchall():
                        syms.add(r[0])
                except Exception:
                    pass
        return syms
    except Exception as e:
        logger.warning(f"获取DB symbol列表失败: {e}")
        return set()


# ===================================================================
#  时间窗口 & last_update
# ===================================================================

def _check_time_window() -> Tuple[bool, str]:
    now = datetime.now(TZ_SH)
    today = now.strftime("%Y-%m-%d")
    is_td = _is_trading_day(today)
    minutes = now.hour * 60 + now.minute

    if not is_td:
        return True, f"非交易日 {today}，允许运行"
    if minutes < 510:
        return True, f"交易日 {today} 08:30前，允许运行"
    if minutes >= 1020:
        return True, f"交易日 {today} 17:00后，允许运行"
    return False, f"交易日 {today} {now.strftime('%H:%M')}，不在允许窗口"


def _ensure_last_update_table(pool) -> None:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS last_update (
                    id          SERIAL PRIMARY KEY,
                    updated_at  TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                    report      TEXT,
                    batch_size  INTEGER NOT NULL DEFAULT 0
                )
            """)
    print("✅ last_update 表已就绪")


def _insert_last_update(pool, report: str, batch_size: int) -> None:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO last_update (updated_at, report, batch_size) VALUES (NOW(), %s, %s)",
                (report, batch_size),
            )
    log(f"📝 last_update: {report}")


# ===================================================================
#  CSV 导出
# ===================================================================

def export_csv(all_results: List[Dict[str, Any]], path: str):
    rows = []
    for r in all_results:
        code = r["code"]
        for qi in r.get("quality_issues", []):
            rows.append({
                "code": code, "timeframe": r.get("timeframe", "15m"),
                "issue": f"quality_{qi['quality']}",
                "date": qi["date"],
                "detail": json.dumps(qi, ensure_ascii=False, separators=(",", ":")),
            })

    if not rows:
        log("无差异数据，跳过CSV导出")
        return

    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["code", "timeframe", "issue", "date", "detail"])
        w.writeheader()
        w.writerows(rows)
    log(f"✅ CSV报告: {path} ({len(rows)}条)")


# ===================================================================
#  中断信号
# ===================================================================

_INTERRUPTED = False

def _signal_handler(signum, frame):
    global _INTERRUPTED
    if _INTERRUPTED:
        print("\n⚡ 强制退出")
        sys.exit(1)
    _INTERRUPTED = True
    print("\n⚠️  收到中断信号...")


# ===================================================================
#  核心: 全量拉取 + 存库
# ===================================================================

def do_full_fetch(
    market: str = "CNStock",
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    全量拉取 + 存库。

    流程:
      1. 东财 clist 1次HTTP拉全市场日K快照
      2. 新浪 30并发拉全市场15min
      3. 写入DB
    """
    from app.utils.db_market import get_market_kline_writer, get_market_db_manager

    writer = get_market_kline_writer()
    mgr = get_market_db_manager()
    pool = mgr._get_pool(market)

    try:
        _ensure_last_update_table(pool)

        now = datetime.now(TZ_SH)
        today = now.strftime("%Y-%m-%d")

        log(f"\n{'='*60}")
        log(f"🚀 全市场K线拉取 + 存库")
        log(f"   市场: {market}")
        log(f"   日期: {today}")
        log(f"   模式: {'dry-run' if dry_run else '写入DB'}")
        log(f"{'='*60}")

        # ── Step 1: 东财 1次HTTP 日K快照 ──
        snapshot = fetch_daily_snapshot()
        if not snapshot:
            log("❌ 东财日K拉取失败!")
            return {"error": "东财拉取失败"}

        codes = sorted(snapshot.keys())
        log(f"   有效股票: {len(codes)}")

        # ── Step 2: 新浪 30并发 15min ──
        data_15m = batch_fetch_15min(codes)

        # ── Step 3: 写入DB ──
        log(f"\n{'='*40} 写入DB {'='*40}")
        written_1d = write_snapshot_to_db(writer, market, snapshot, today, dry_run=dry_run)
        written_15m = write_15min_to_db(writer, market, data_15m, dry_run=dry_run)

        # ── Step 4: last_update ──
        if not dry_run:
            report = (
                f"全量更新 | {today} | "
                f"1D={written_1d} 15m={written_15m} | "
                f"股票={len(codes)}"
            )
            _insert_last_update(pool, report, 5000)

        log(f"\n{'='*60}")
        log(f"✅ 全量完成")
        log(f"   股票: {len(codes)}")
        log(f"   1D写入: {written_1d}")
        log(f"   15m写入: {written_15m}")
        log(f"{'='*60}")

        return {
            "stocks": len(codes),
            "written_1d": written_1d,
            "written_15m": written_15m,
        }

    finally:
        mgr.close_all_pools()


# ===================================================================
#  主程序
# ===================================================================

def main():
    global _INTERRUPTED

    parser = argparse.ArgumentParser(
        description="全市场K线批量拉取 → 存库",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("-T", "--type",
        choices=["1D", "15m", "all"], default="all",
        help="操作类型 (默认 all)")
    parser.add_argument("--symbol", help="只处理指定股票")
    parser.add_argument("--market", default="CNStock", help="市场")
    parser.add_argument("--dry-run", action="store_true", help="只拉取不写DB")
    parser.add_argument("--csv", help="导出CSV报告")
    parser.add_argument("--update", action="store_true", help="全量拉取+存库 (1D+15m, 忽略-T)")
    parser.add_argument("--force-full", action="store_true", help="跳过时间窗口")
    parser.add_argument("-w", "--workers", type=int, help="HTTP并发数")
    args = parser.parse_args()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    global MAX_WORKERS
    if args.workers:
        MAX_WORKERS = args.workers

    # ── 更新模式 ──
    if args.update:
        _build_trading_day_cache(args.market)

        if not args.force_full:
            allowed, msg = _check_time_window()
            if not allowed:
                log(f"❌ {msg}")
                log(f"   --force-full 可跳过")
                return 1
            log(f"✅ {msg}")
        else:
            log("⚡ 强制模式")

        from app.utils.db_market import get_market_db_manager
        mgr = get_market_db_manager()
        if not mgr.market_db_exists(args.market):
            log(f"❌ {args.market}_db 不存在")
            return 1

        try:
            do_full_fetch(
                market=args.market,
                dry_run=args.dry_run,
            )
        except Exception as e:
            log(f"❌ 失败: {e}")
            traceback.print_exc()
            return 1
        return 0

    # ── DB质量检查模式 ──
    from app.utils.db_market import get_market_kline_writer, get_market_db_manager
    writer = get_market_kline_writer()
    mgr = get_market_db_manager()
    market = args.market

    if not mgr.market_db_exists(market):
        log(f"❌ {market}_db 不存在")
        return 1

    db_syms = get_db_symbols(writer, market)
    if not db_syms:
        log(f"❌ {market}_db 中无数据")
        return 1

    syms = [args.symbol] if args.symbol else sorted(db_syms)
    log(f"\n{'='*60}")
    log(f"📊 DB 质量检查 | {market} | {len(syms)} 只")
    log(f"{'='*60}")

    _build_trading_day_cache(market)

    all_results = []
    agg_stats = {"quality_issues": 0}

    if args.type in ("15m", "all"):
        qr = check_15m_quality_db(writer, market, syms)
        all_results.extend(qr["results"])
        agg_stats["quality_issues"] += qr["stats"]["quality_issues"]

    log(f"\n{'='*60}")
    log(f"检查完成: 质量问题={agg_stats['quality_issues']}")
    log(f"{'='*60}")

    if args.csv:
        export_csv(all_results, args.csv)

    mgr.close_all_pools()
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""
cn_stock_putdb — 指数 5m K线落库（coord_index → kline_index_5m）

═══════════════════════════════════════════════════════════════
  用途: 写侧作业层 —— 直接调 coord_* 多源调度取数，再 UPSERT 落库
        （sync / sync_index_fflow / sync_stock_kline / run_1d / run_1m /
        write_to_db / write_index_fflow / write_stock_kline_bulk /
        sync_dragon_tiger）。读侧（读库 / 聚合 / 新鲜度闸门 / 远端兜底）在
        app/data_sources/cn_stock_ext.py，两侧职责不混。

  读写分离（2026-10-29 用户裁定）:
  - 写 = 本模块 → coord_* → 库。「已在目标日收官」的幂等跳过判据（_index_day_complete）
    只服务于写作业本身（重启不重复写库），不是对外读接口。
  - 读 = 外围 → cn_stock_ext（读库优先，缺/陈旧才走远端）。

  收口的写链（2026-10-27 起）:
  - sync()              → kline_index_5m    （coord_index index_kline，5m）
  - sync_index_fflow()  → kline_index_fflow （coord_index index_fflow，当日 240 根）
  - sync_stock_kline()  → kline_1D/kline_1m （coordinator 个股 K线，含覆盖率补齐）
  - sync_dragon_tiger() → cnd_dragon_tiger_list（coord_pool dragon_tiger）

  设计点:
  - 数据源一律走 coord_* 多源串行调度；
  - 🚫 方向红线（2026-10-28 固化并达成）：本文件属 data_sources 层（最底层），
    **禁止 import app.market_cn.***——下层不得反依赖上层。
    本地库读写与龙虎榜内存缓存一律经同层 app/data_sources/dragon_tiger_local.py。
    （本文件原有 3 处反依赖已全部消除：2 处改调同层本地库读，
      1 处龙虎榜缓存刷新改调同层——缓存所有权已下沉，market_cn 侧正向读取。）
  - UPSERT 幂等，重复跑不重不漏；
  - 指数分钟/资金流不可回补只能向前攒：默认拉新浪上限 5001 根（≈104 交易日）
    滑动覆盖，断采一周仍可自动补回；资金流仅当日 240 根，每日增量即全量。

  易错点:
  - 各源 volume 单位不一致风险：sina 5m=股（官方口径，写入即正确）；
    mootdx 5m vol=成交额/100（错位）——只在 sina 全挂时兜底，后续 sina
    同步覆盖同一窗口时会归正（与 sync_index_minute 同一容忍策略）。
  - 指数间必须 sleep：新浪限流会断连（2026-09-12 实测）。
═══════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

from app.utils.logger import get_logger
from psycopg2.extras import execute_values

logger = get_logger(__name__)

TABLE = "kline_index_5m"

# 与 sync_index_daily.INDICES / sync_index_minute.INDICES 完全同键
INDICES: Dict[str, str] = {
    "000001.SH": "上证指数",
    "000016.SH": "上证50",
    "000300.SH": "沪深300",
    "000905.SH": "中证500",
    "000852.SH": "中证1000",
    "399001.SZ": "深证成指",
    "399006.SZ": "创业板指",
    "399005.SZ": "中小板指",
    "000688.SH": "科创50",
}

SINA_BARS = 5001       # 新浪服务器硬顶 ≈104 个交易日
# 码间限频 pause / 重试池已收口 dispatch.run_batch（coord_index.BATCH_PAUSE 等）


def get_pool():
    from app.utils.db_market import get_market_db_manager
    mgr = get_market_db_manager()
    mgr.ensure_market_db("CNStock")
    return mgr._get_pool("CNStock")


def write_to_db(pool, symbol: str, rows: Iterable[Dict[str, Any]],
                dry_run: bool = False) -> int:
    """UPSERT 幂等写入。rows: [{time, open, high, low, close, volume, up_count?, down_count?}]。

    time 接受 "YYYY-MM-DD HH:MM[:SS]" / datetime；up/down_count 缺省 None
    （COALESCE 保留库内旧值）。
    """
    data = [r for r in rows if r and r.get("time") is not None and r.get("close") is not None]
    if not data:
        return 0
    if dry_run:
        logger.info("[putdb][dry-run] %s %s: %d 条 (%s ~ %s)",
                    TABLE, symbol, len(data),
                    data[0].get("time"), data[-1].get("time"))
        return len(data)
    table = '"%s"' % TABLE
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    CREATE TABLE IF NOT EXISTS {table} (
                        symbol     VARCHAR(20) NOT NULL,
                        time       TIMESTAMP   NOT NULL,
                        open       DOUBLE PRECISION,
                        high       DOUBLE PRECISION,
                        low        DOUBLE PRECISION,
                        close      DOUBLE PRECISION,
                        volume     DOUBLE PRECISION,
                        up_count   INTEGER,
                        down_count INTEGER,
                        PRIMARY KEY (symbol, time)
                    )
                """)
                for row in data:
                    cur.execute(f"""
                        INSERT INTO {table}
                            (symbol, time, open, high, low, close, volume,
                             up_count, down_count)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (symbol, time) DO UPDATE SET
                            open       = EXCLUDED.open,
                            high       = EXCLUDED.high,
                            low        = EXCLUDED.low,
                            close      = EXCLUDED.close,
                            volume     = EXCLUDED.volume,
                            up_count   = COALESCE(EXCLUDED.up_count,   {table}.up_count),
                            down_count = COALESCE(EXCLUDED.down_count, {table}.down_count)
                    """, (
                        symbol, row["time"],
                        float(row.get("open") or 0), float(row.get("high") or 0),
                        float(row.get("low") or 0), float(row.get("close") or 0),
                        float(row.get("volume") or 0),
                        _opt_int(row.get("up_count")), _opt_int(row.get("down_count")),
                    ))
            conn.commit()
        return len(data)
    except Exception as e:
        logger.error("[putdb] %s %s 写入失败: %s", TABLE, symbol, e)
        return 0


def _opt_int(v) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ================================================================
# 指数 5m K线同步作业（coord_index index_kline → kline_index_5m）
# ────────────────────────────────────────────────────────────────
# 读写分离（2026-10-29 用户裁定）：本模块是**写侧作业**，直接调 coord_* 取数后
# 落库；读侧（读库 / 聚合 / 新鲜度闸门 / 远端兜底）在 cn_stock_ext.py。
# 因此「库里这一天有没有 15:00 收官根」这种幂等跳过判据留在这里——它只服务于
# 写作业本身（重启不重复写库），不是对外读接口的一部分。
# ================================================================


def _index_day_complete(table: str, symbol: str, target_td: str) -> bool:
    """该 symbol 是否已写到目标日 **15:00 收官 bar**（重启不再重复写库）。

    判据 = **目标日当天**的末条 time ≥ 15:00（kline_index_5m 的 bar 结束时刻 /
    kline_index_fflow 的 15:00 日累计行）。盘中只写到 14:xx 的**半日数据不算**
    —— 指数 5m/资金流不可回补，把半日当收官会永久丢尾段（2026-10-27）。

    ★ 必须带 `time::date = target_td`（2026-10-29 修）：只取 max(time) 再比字符串
    时，库里一旦有**今天**的 bar（盘中写入），`"今天 09:34" >= "昨天 15:00"` 恒真
    ⇒ 目标日被误判收官、全天不再刷新（000852.SH 实测：09:33 写今日 4 根后
    09:38 起永久跳过）。
    查库失败 ⇒ False（保守重写，幂等无害）。
    """
    try:
        pool = get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    'SELECT max(time) FROM "%s" WHERE symbol = %%s '
                    'AND time::date = %%s::date' % table,
                    (symbol, target_td),
                )
                row = cur.fetchone()
        if not row or row[0] is None:
            return False
        last = row[0]
        last_s = last.strftime("%Y-%m-%d %H:%M") if hasattr(last, "strftime") else str(last)[:16]
        return last_s >= ("%s 15:00" % target_td)
    except Exception as e:
        logger.warning("[putdb] %s %s 收官检查失败（保守重写）: %s", table, symbol, e)
        return False


def _sync_index_kind(*, kind: str, table: str, target_td: str,
                     symbols: Optional[List[str]], dry_run: bool, verbose: bool,
                     force: bool, coord_params: Dict[str, Any],
                     writer) -> Dict[str, int]:
    """指数同步作业内核：**库内收官裁决 → 一次打包 coordinate → 逐码落库**。

    指数码一次打包交 `coordinate(kind, codes=[...])`，由 dispatch.run_batch 逐码
    多源分发（主池+重试池、码间限频 pause），不逐码循环 coordinate（2026-10-27
    用户裁定，对齐 coordinator.coordinate_market_kline 的批量形态）。

    目标日已有 15:00 收官根 ⇒ 跳过该码（重启不重复写库，2026-10-27 用户报障）；
    未收官（盘中）⇒ 每轮全量重写，UPSERT 幂等、累计值逐轮归正。force 跳过裁决。
    断采/限频无需单独补数：下一轮拿到的是当日全量累计，重写即自愈；真正补不上
    的只有跨日缺口（源不给历史），属上游限制。
    """
    from app.data_sources.coord_index import coordinate

    syms = list(symbols) if symbols else list(INDICES.keys())
    if not syms:
        return {"written": 0, "failed": 0}
    if not force:
        pending = [s for s in syms if not _index_day_complete(table, s, target_td)]
        if not pending:
            logger.info("[putdb] %s 目标=%s 已收官 %d/%d，跳过",
                        table, target_td, len(syms), len(syms))
            return {"written": 0, "failed": 0}
        if len(pending) < len(syms):
            logger.info("[putdb] %s 目标=%s 收官 %d/%d，仅补 %d 个缺口",
                        table, target_td, len(syms) - len(pending),
                        len(syms), len(pending))
        syms = pending
    code2sym = {s.split(".")[0]: s for s in syms}
    logger.info("[putdb] %s 批量同步 %d 个 %s", table, len(syms), coord_params)
    out = coordinate(kind, codes=list(code2sym), **coord_params)
    entries = (out.get("data") or {}).get("rows") or []
    pool = get_pool()
    total, failed = 0, []
    for e in entries:
        code = str(e.get("code", ""))
        sym = code2sym.get(code, code)
        rows = e.get("rows") or []
        if not rows:
            err = (e.get("error") or "").strip()
            if err:
                failed.append(sym)
                logger.warning("[putdb] %s %s 拉取失败: %s", table, sym, err)
            else:
                # NotSupported / 空信封无 error ⇒ 跳过，不进失败表（dispatch 同语义）
                logger.info("[putdb] %s %s 无数据/无源支持（跳过，不计失败）", table, sym)
            continue
        if verbose:
            logger.info("[putdb] %s %s source=%s %d 根 (%s ~ %s)",
                        table, sym, e.get("source"), len(rows),
                        rows[0].get("time"), rows[-1].get("time"))
        total += writer(pool, sym, rows, dry_run=dry_run)
    logger.info("[putdb] %s 完成: %d 条写入, %d 个失败%s", table, total,
                len(failed), f" ({', '.join(failed)})" if failed else "")
    return {"written": total, "failed": len(failed)}


def sync(symbols: Optional[List[str]] = None, dry_run: bool = False,
         verbose: bool = False, bars: Optional[int] = None,
         force: bool = False) -> Dict[str, int]:
    """指数 5m K线同步 → kline_index_5m（scheduler / CLI 的唯一写入口）。

    返回 {written, failed}。目标日 = `_target_trading_day()` = 最近一个已收盘
    交易日（5m bar 以 15:00 收官根判完整）。默认拉新浪上限 SINA_BARS
    （≈104 交易日）滑动覆盖：断采一周仍可自动补回。
    """
    return _sync_index_kind(
        kind="index_kline", table=TABLE, target_td=_target_trading_day(),
        symbols=symbols, dry_run=dry_run, verbose=verbose, force=force,
        coord_params={"timeframe": "5m", "limit": int(bars or SINA_BARS)},
        writer=write_to_db,
    )


# ================================================================
# 个股 1D / 1m K线盘后覆写（coordinate_market_kline → kline_1D/kline_1m）
# ────────────────────────────────────────────────────────────────
# 2026-10-27 从 app/data_sources/backfill_db 迁入：原 mootdx 直连通达信已不可用，
# 改为走 coordinator.coordinate_market_kline（多源并发/重试/熔断，mootdx 坏了自动绕开）。
# 写入口径与原 backfill_db._batch_insert 完全一致（不复权原始价，volume 单位=股）。
# ================================================================

TZ_CN = timezone(timedelta(hours=8))

# 每标的一个完整交易日应有的 bar 数（**完整性校验基准**，不含任何余量）
TF_BAR_COUNT = {"1D": 1, "1m": 240}

# 实际**拉取**条数：必须严格大于 TF_BAR_COUNT。
# 若拉取数恰等于应有根数（原实现 count=240），源返回里混入任何非交易时段点
# 都会按"取最近 N 条"砍掉当日头部 —— 实测 2026-10-29：
#   · sohu 一次给 272 点（09:30 + 上午120 + 13:00 + 下午120 + 盘后15:01~15:30 共30）
#     ⇒ 截尾 240 后起点变 10:02，679 只票缺 9:31~10:00；
#   · tencent 给 241 点（含 09:30 竞价成交点）；
#   · sina 的 240 根跨日（前日 2 根 + 当日 238 根）。
# 余量 60 覆盖盘后 30 + 竞价 1 + 跨日残留，并留出源侧偶发多返的缓冲。
TF_FETCH_COUNT = {"1D": 1, "1m": 300}

# 1m 有效 bar 时段（库表口径）：09:31~11:30 + 13:01~15:00 = 240 根。
# 库表不含 09:30 集合竞价成交点与 15:01~15:30 盘后固定价格交易点；
# 拉取余量放大后源侧会带出这些点，须在此统一剔除（跨源同一口径，勿逐 provider 各写一份）。
_INTRADAY_1M_RANGES = ((9 * 60 + 31, 11 * 60 + 30), (13 * 60 + 1, 15 * 60))


def _in_1m_session(dt: datetime) -> bool:
    """bar 时刻是否落在 1m 有效时段内（分钟粒度，秒/微秒忽略）。"""
    mins = dt.hour * 60 + dt.minute
    return any(lo <= mins <= hi for lo, hi in _INTRADAY_1M_RANGES)


def _stock_kline_sync_timeout() -> int:
    """一次 sync_stock_kline 全集抓取的总预算（秒），超预算未返回的源按失败计。"""
    try:
        v = int(os.getenv("BACKFILL_SYNC_TIMEOUT", "1800"))
        return v if v > 0 else 1800
    except Exception:
        return 1800


def _target_trading_day() -> str:
    """计算目标交易日（最近一个已收盘的交易日）。

    供 K线族（5m/1D）用：它们的 bar 以 15:00 收官根判完整性。
    """
    from app.utils.trading_calendar import last_finish_trading_day
    return last_finish_trading_day()


def _get_all_stock_codes() -> List[str]:
    """获取全市场活跃股票代码（纯码，如 "000001"）。"""
    try:
        from app.utils.basicinfo_db import get_stock_basic_db
        return get_stock_basic_db().market_all_codes(status="active")
    except Exception as e:
        logger.error("[putdb] 获取股票列表失败: %s", e)
        return []


def _count_existing_kline(tf: str, bar_time: datetime) -> int:
    """查询目标日期已写入的 symbol 数量（用于完整性校验）。"""
    table = f'"kline_{tf}_{bar_time.year}"'
    naive = bar_time.replace(tzinfo=None) if bar_time.tzinfo else bar_time
    start_of_day = naive.replace(hour=0, minute=0, second=0, microsecond=0)
    start_of_next_day = start_of_day + timedelta(days=1)
    try:
        pool = get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT COUNT(DISTINCT symbol) FROM {table}
                    WHERE time >= %s AND time < %s
                """, (start_of_day, start_of_next_day))
                return cur.fetchone()[0]
    except Exception as e:
        logger.warning("[putdb] 查询 kline_%s 已有数据失败: %s", tf, e)
        return 0


def _missing_symbols(tf: str, bar_time: datetime, symbols: List[str]) -> List[str]:
    """目标交易日尚无任何 bar 的 symbol（增量缺口）。

    只拉缺失标的 ⇒ 重启/重试不再重跑已完成部分（2026-10-27 用户报障：
    全量重拉已写 >95% 的当日 = 重复回填）。查库失败时返回全量（fail-open，
    宁可多拉不漏拉）。
    """
    table = f'"kline_{tf}_{bar_time.year}"'
    naive = bar_time.replace(tzinfo=None) if bar_time.tzinfo else bar_time
    start_of_day = naive.replace(hour=0, minute=0, second=0, microsecond=0)
    start_of_next_day = start_of_day + timedelta(days=1)
    try:
        pool = get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT DISTINCT symbol FROM {table}
                    WHERE time >= %s AND time < %s
                """, (start_of_day, start_of_next_day))
                have = {r[0] for r in cur.fetchall()}
    except Exception as e:
        logger.warning("[putdb] 查询 kline_%s 缺口失败（全量拉取兜底）: %s", tf, e)
        return list(symbols)
    return [s for s in symbols if s not in have]


def _delete_kline_day(bar_time: datetime, tf: str, symbols: Optional[List[str]] = None) -> int:
    """删除指定日期的 bar 数据，覆盖式覆写的前置步骤。

    symbols 给定时**只删这些 symbol**（作用域=本次写入集）：
    先全量删当日再写的顺序在崩溃/重启时会丢掉整日数据（未写完的全没了），
    2026-10-27 用户报障「>95% 重启后又重新回填」即此窗口；限定作用域后
    未覆盖的 symbol 不受影响，重跑幂等。
    """
    table = f'"kline_{tf}_{bar_time.year}"'
    naive = bar_time.replace(tzinfo=None) if bar_time.tzinfo else bar_time
    start_of_day = naive.replace(hour=0, minute=0, second=0, microsecond=0)
    start_of_next_day = start_of_day + timedelta(days=1)
    try:
        pool = get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                if symbols:
                    cur.execute(f"""
                        DELETE FROM {table}
                        WHERE time >= %s AND time < %s AND symbol = ANY(%s)
                    """, (start_of_day, start_of_next_day, list(symbols)))
                else:
                    cur.execute(f"""
                        DELETE FROM {table}
                        WHERE time >= %s AND time < %s
                    """, (start_of_day, start_of_next_day))
                deleted = cur.rowcount
                conn.commit()
                return deleted
    except Exception as e:
        logger.warning("[putdb] 删除 kline_%s %s 失败: %s", tf, naive, e)
        return 0


def stock_kline_coverage(tf: str = "1D") -> tuple:
    """目标交易日 K线覆盖 (have, total)：已有 bar 的 symbol 数 / 全市场活跃数。

    scheduler 补拉闭环据此判「数据是否到位」；total=0 时查询失败（fail-open）。
    """
    total = len(_get_all_stock_codes())
    if total == 0:
        return 0, 0
    bar_time = datetime.strptime(_target_trading_day(), "%Y-%m-%d").replace(
        hour=15, minute=0, second=0, tzinfo=TZ_CN)
    return _count_existing_kline(tf, bar_time), total


def write_stock_kline_bulk(pool, tf: str, records: list, dry_run: bool = False) -> int:
    """原生批量 INSERT 个股 K线（kline_{tf}_YYYY），按年分表，UPSERT 幂等。

    records: [{symbol, time, open, high, low, close, volume}]，time 须为 datetime。
    与 backfill_db._batch_insert 同口径（不复权原始价，volume=股）。
    """
    if not records:
        return 0
    from app.utils.db_market import _ensure_datetime, normalize_1d_time
    from app.utils.db_multi import _table_name

    # 1D bar 时间归一到 15:00:00（收盘），避免 00:00/15:00 并存
    for r in records:
        r["time"] = _ensure_datetime(r["time"])
        if tf == "1D":
            r["time"] = normalize_1d_time(r["time"])

    if dry_run:
        logger.info("[putdb][dry-run] kline_%s: %d 条 (%s ~ %s)",
                    tf, len(records), records[0]["time"], records[-1]["time"])
        return len(records)

    from app.utils.db_market import get_market_db_manager

    mgr = get_market_db_manager()
    # 按 (symbol, year) 分组
    by_sym_year: Dict[Any, list] = {}
    for r in records:
        sym = r.get("symbol", "_unknown")
        year = r["time"].year
        by_sym_year.setdefault((sym, year), []).append(r)

    total = 0
    with pool.connection() as conn:
        cur = conn.cursor()
        for (sym, year), recs in by_sym_year.items():
            table = _table_name(tf, year)
            mgr.ensure_year_table("CNStock", tf, year)
            sql = f"""
                INSERT INTO "{table}" (symbol, time, open, high, low, close, volume)
                VALUES %s
                ON CONFLICT (symbol, time) DO UPDATE SET
                    open = EXCLUDED.open,
                    high = EXCLUDED.high,
                    low = EXCLUDED.low,
                    close = EXCLUDED.close,
                    volume = EXCLUDED.volume
            """
            values = [
                (sym, _ensure_datetime(r["time"]),
                 float(r.get("open") or 0), float(r.get("high") or 0),
                 float(r.get("low") or 0), float(r.get("close") or 0),
                 float(r.get("volume") or 0))
                for r in recs
            ]
            try:
                execute_values(cur, sql, values, page_size=5000)
                total += len(values)
                conn.commit()
            except Exception as e:
                conn.rollback()
                logger.error("[putdb] 写入 kline_%s %s 失败: %s", tf, table, e)
        cur.close()
    return total


_last_stock_sync = {}

def sync_stock_kline(tf: str = "1D", symbols: Optional[List[str]] = None,
                     dry_run: bool = False, force: bool = False) -> Dict[str, Any]:
    """盘后覆写个股 K线（coordinate_market_kline 多源批量 → kline_{tf}）。

    替代原 backfill_db.sync_tf（mootdx 直连通达信，已不可用）。流程:
      1. 目标交易日（最近已收盘）
      2. 拉取集：显式 symbols=强制覆写这些票；None=**增量缺口**（只拉目标日
         尚无 bar 的票，重启/重试不重跑已完成部分）
      3. coordinate_market_kline 拉取集（多源并发/重试/熔断，含 count 根）
      4. time 归一成 datetime + 仅保留目标交易日的 bar
      5. 写前清场仅限本次写入集 → 批量 UPSERT（幂等）

    Args:
        tf: "1D" / "1m"
        symbols: 纯码列表（如 ["000001"]）；None=全市场活跃（增量缺口）
        dry_run: 只看不写
        force: 跳过 600s 防重守卫（scheduler 缺口补拉重试用）

    Returns:
        {"status", "written", "failed", "report"}
    """
    if tf not in TF_BAR_COUNT:
        return {"status": "error", "written": 0, "failed": 0, "report": f"不支持的周期: {tf}"}

    target_td = _target_trading_day()
    bar_time = datetime.strptime(target_td, "%Y-%m-%d").replace(
        hour=15, minute=0, second=0, tzinfo=TZ_CN)

    count = TF_FETCH_COUNT[tf]
    key = f"{tf}:{target_td}"
    now = datetime.now(TZ_CN)
    last = _last_stock_sync.get(key)
    if not force and last and (now - last).total_seconds() < 600:
        logger.info("[putdb] kline_%s 目标=%s 最近已触发，跳过（防重）", tf, target_td)
        return {"status": "ok", "written": 0, "failed": 0, "report": "防重跳过"}

    # 拉取集选择：
    #   · 显式 symbols = 手动指定，强制覆写这些票；
    #   · 全市场（symbols=None）= **增量缺口** —— 只拉目标日尚无 bar 的票，
    #     重启/重试不再重跑已完成部分（2026-10-27 用户报障）。
    if symbols is not None:
        fetch_symbols = list(symbols)
        total = len(fetch_symbols)
    else:
        all_codes = _get_all_stock_codes()
        if not all_codes:
            return {"status": "error", "written": 0, "failed": 0, "report": "无股票列表"}
        fetch_symbols = _missing_symbols(tf, bar_time, all_codes)
        total = len(all_codes)
        if not fetch_symbols:
            logger.info("[putdb] kline_%s 目标=%s 已齐 %d/%d，跳过", tf, target_td, total, total)
            _last_stock_sync[key] = now
            return {"status": "ok", "written": 0, "failed": 0,
                    "report": f"已齐 {total}/{total}，跳过"}
        logger.info("[putdb] kline_%s 目标=%s 缺口 %d/%d 开始补拉", tf, target_td,
                    len(fetch_symbols), total)
    if not fetch_symbols:
        return {"status": "error", "written": 0, "failed": 0, "report": "无股票列表"}

    from app.data_sources.coordinator import get_coordinator
    bars = get_coordinator().coordinate_market_kline(
        market="CNStock", timeframe=tf, count=count,
        timeout=_stock_kline_sync_timeout(), symbols=fetch_symbols)
    if not bars:
        logger.error("[putdb] kline_%s 拉取到 0 条记录，跳过删除和写入（目标 %s）", tf, target_td)
        return {"status": "error", "written": 0, "failed": len(fetch_symbols),
                "report": f"拉取到 0 条数据（目标 {target_td}）"}

    # time 归一成 datetime + 仅保留目标交易日 + 1m 仅保留交易时段
    from app.utils.db_market import _ensure_datetime
    target_date = target_td
    all_records: List[Dict[str, Any]] = []
    bars_by_symbol: Dict[str, int] = {}
    skipped = 0
    off_session = 0
    for b in bars:
        sym = b.get("symbol")
        if not sym:
            continue
        try:
            dt = _ensure_datetime(b.get("time"))
        except Exception:
            skipped += 1
            continue
        if dt.strftime("%Y-%m-%d") != target_date:
            continue
        if tf == "1m" and not _in_1m_session(dt):
            off_session += 1
            continue
        b["symbol"] = sym
        b["time"] = dt
        all_records.append(b)
        bars_by_symbol[sym] = bars_by_symbol.get(sym, 0) + 1
    if off_session:
        logger.info("[putdb] kline_1m 剔除非交易时段 bar %d 条（09:30 竞价 / 13:00 / 盘后）",
                    off_session)

    if not all_records:
        logger.error("[putdb] kline_%s 目标 %s 过滤后 0 条，跳过删除和写入", tf, target_td)
        return {"status": "error", "written": 0, "failed": len(fetch_symbols),
                "report": f"过滤后 0 条（目标 {target_td}）"}
    if skipped:
        logger.warning("[putdb] kline_%s 跳过 %d 条无法解析时间的 bar", tf, skipped)

    got_symbols = set(bars_by_symbol.keys())
    pool = get_pool()
    # 写前清场仅限**本次写入集**（00:00/15:00 混存等旧 bar 归一）；
    # 绝不整日全删 —— 崩溃/重启窗口会丢掉未重写的票（2026-10-27 报障根因）。
    if not dry_run:
        deleted = _delete_kline_day(bar_time, tf, symbols=sorted(got_symbols))
        if deleted > 0:
            logger.info("[putdb] kline_%s 已清场 %d 条旧数据（范围=%d 标的）", tf, deleted, len(got_symbols))
    written = write_stock_kline_bulk(pool, tf, all_records, dry_run=dry_run)

    failed = [s for s in fetch_symbols if s not in bars_by_symbol]
    ok = len(fetch_symbols) - len(failed)
    sync_rate = ok / len(fetch_symbols) if fetch_symbols else 0
    status = "ok" if sync_rate > 0.9 else "error"
    report = f"已同步 {ok}/{len(fetch_symbols)}（全市场 {total}）"
    if failed:
        report += f"; 失败 {len(failed)}: {','.join(failed[:20])}"
    _last_stock_sync[key] = datetime.now(TZ_CN)
    logger.info("[putdb] kline_%s 完成: written=%d failed=%d status=%s", tf, written, len(failed), status)
    return {"status": status, "written": written, "failed": len(failed), "report": report}


def run_1d(symbols: Optional[List[str]] = None, force: bool = False) -> dict:
    """覆写 1D，返回 {status, written, skipped}。force=跳过防重守卫（补拉重试用）。"""
    r = sync_stock_kline("1D", symbols, force=force)
    return {"status": r["status"], "written": r["written"],
            "skipped": r["written"] == 0 and r["status"] == "ok"}


def run_1m(symbols: Optional[List[str]] = None, force: bool = False) -> dict:
    """覆写 1m，返回 {status, written, skipped}。force=跳过防重守卫（补拉重试用）。"""
    r = sync_stock_kline("1m", symbols, force=force)
    return {"status": r["status"], "written": r["written"],
            "skipped": r["written"] == 0 and r["status"] == "ok"}


# ================================================================
# 指数资金流落库（coord_index index_fflow → kline_index_fflow）
# ================================================================

FFLOW_TABLE = "kline_index_fflow"
# f52~f56 净流入字段 → 库列名（当日累计值、元；主力=大单+超大单，四类之和=0）
FFLOW_NET_COLS = ("main_net", "small_net", "mid_net", "big_net", "super_net")


def write_index_fflow(pool, symbol: str, rows: Iterable[Dict[str, Any]],
                      dry_run: bool = False) -> int:
    """UPSERT 幂等写入资金流。rows: [{time, main_net, ..., super_net}]。

    time 接受 "YYYY-MM-DD HH:MM[:SS]" / datetime；净额=当日累计（元），
    全列覆盖更新（EM 分钟接口每日增量即全量，末段盘中抖动逐根归正）。
    """
    data = [r for r in rows if r and r.get("time") is not None]
    if not data:
        return 0
    if dry_run:
        logger.info("[putdb][dry-run] %s %s: %d 条 (%s ~ %s)",
                    FFLOW_TABLE, symbol, len(data),
                    data[0].get("time"), data[-1].get("time"))
        return len(data)
    table = '"%s"' % FFLOW_TABLE
    cols = ", ".join(FFLOW_NET_COLS)
    ph = ", ".join(["%s"] * (2 + len(FFLOW_NET_COLS)))
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    CREATE TABLE IF NOT EXISTS {table} (
                        symbol VARCHAR(20) NOT NULL,
                        time   TIMESTAMP   NOT NULL,
                        {", ".join(f"{c} DOUBLE PRECISION" for c in FFLOW_NET_COLS)},
                        PRIMARY KEY (symbol, time)
                    )
                """)
                for r in data:
                    cur.execute(f"""
                        INSERT INTO {table} (symbol, time, {cols})
                        VALUES ({ph})
                        ON CONFLICT (symbol, time) DO UPDATE SET
                            {", ".join(f"{c} = EXCLUDED.{c}" for c in FFLOW_NET_COLS)}
                    """, (symbol, r["time"], *[float(r.get(c) or 0) for c in FFLOW_NET_COLS]))
            conn.commit()
        return len(data)
    except Exception as e:
        logger.error("[putdb] %s %s 写入失败: %s", FFLOW_TABLE, symbol, e)
        return 0


def _fflow_target_day() -> str:
    """指数资金流的目标交易日 —— **上游源会返回的那一天**。

    `fflow_em` 无日期参数、只给当日累计（provider_index/fflow_em.py:50），所以
    目标日必须是「今天」（今天为交易日时）；非交易日源返上一交易日，故回退
    `last_finish_trading_day()`。

    与 `_target_trading_day()` 的区别（易错点）：盘中 10:00 时本函数=今天、
    后者=昨天。资金流若用后者，每轮都在问「昨天是否收官」→ 恒 True → 全天空转
    （2026-10-29 实测 `已收官 9/9，跳过` 每 5 分钟一次，`kline_index_fflow`
    当日 0 行）。交易日历走本地 pkl 缓存，零外网。
    """
    from app.utils.trading_calendar import is_trading_day_today, last_finish_trading_day
    if is_trading_day_today():
        return datetime.now().strftime("%Y-%m-%d")
    return last_finish_trading_day()


def sync_index_fflow(symbols: Optional[List[str]] = None, dry_run: bool = False,
                     verbose: bool = False, limit: int = 240,
                     force: bool = False) -> Dict[str, int]:
    """指数分钟资金流同步 → kline_index_fflow（scheduler / CLI 的唯一写入口）。

    返回 {written, failed}。目标日 = `_fflow_target_day()`（源无日期参数、只给
    当日）。盘中每轮全日重写（240 根/指数，累计值逐轮归正），15:30 盘后批次再
    终校准。EM delay host 滞后 ~15 分钟属上游行为；EM push2 IP 限频 RST 突发窗
    由 dispatch.run_batch 的码间 pause + 重试池覆盖，长关窗由下一轮调度收敛。
    """
    return _sync_index_kind(
        kind="index_fflow", table=FFLOW_TABLE, target_td=_fflow_target_day(),
        symbols=symbols, dry_run=dry_run, verbose=verbose, force=force,
        coord_params={"limit": int(limit)},
        writer=write_index_fflow,
    )


# ================================================================
# 龙虎榜落库（coord_pool dragon_tiger → cnd_dragon_tiger_list）
# ================================================================

def sync_dragon_tiger(trade_date: Optional[str] = None, dry_run: bool = False,
                      verbose: bool = False) -> Dict[str, Any]:
    """龙虎榜同步入口（scheduler._save_dragon_hot_daily 调用）。

    coord_pool("dragon_tiger") 多源拉当日（或指定日）榜单 → 复用
    dragon_tiger_store._save_dragon_tiger UPSERT 进 cnd_dragon_tiger_list，
    成功后刷新 dragon_limit 内存缓存。返回
    {"written", "total", "trade_date", "status"}。

    时效纪律（与 hub.lhb 配套，决策不可违）：LHB(D) 交易所 ~17:00-17:30 发布，
    本入口安排在 17:00 后跑；调用方判定日只允许消费 trade_date ≤ D-1 的事件。
    """
    from datetime import datetime
    from app.data_sources.coord_pool import coordinate

    trade_date = trade_date or datetime.now().strftime("%Y-%m-%d")
    result: Dict[str, Any] = {"written": 0, "total": 0, "trade_date": trade_date,
                              "status": "ok"}
    out = coordinate("dragon_tiger", start_date=trade_date, end_date=trade_date)
    rows = (out.get("data") or {}).get("rows") if out.get("ok") else None
    if not rows:
        result["status"] = "error"
        logger.warning("[putdb] 龙虎榜 %s 拉取失败: %s", trade_date, out.get("error", "无数据"))
        return result
    result["total"] = len(rows)
    if dry_run:
        result["written"] = len(rows)
        logger.info("[putdb][dry-run] 龙虎榜 %s: %d 条 (source=%s)",
                    trade_date, len(rows), out.get("source"))
        return result
    try:
        from app.data_sources.dragon_tiger_local import (
            _ensure_tables, _get_cnstock_pool, _save_dragon_tiger,
        )
        pool = _get_cnstock_pool()
        with pool.connection() as conn:
            _ensure_tables(conn)
            result["written"] = _save_dragon_tiger(conn, rows, trade_date)
        logger.info("[putdb] 龙虎榜 %s: %d/%d 条 (source=%s)",
                    trade_date, result["written"], len(rows), out.get("source"))
    except Exception as e:
        result["status"] = "error"
        logger.error("[putdb] 龙虎榜 %s 写入失败: %s", trade_date, e)
        return result
    # 写入成功后刷新内存缓存（dragon_monitor 经 dragon_limit.get_dragon_tiger 读热数据）。
    # 缓存所有权 2026-10-28 已下沉至同层 dragon_tiger_local ⇒ 同层调用，无反依赖。
    try:
        from app.data_sources.dragon_tiger_local import load_dragon_tiger_from_db
        load_dragon_tiger_from_db(trade_date)
    except Exception as e:
        logger.warning("[putdb] 龙虎榜缓存刷新失败: %s", e)
    return result


def main():
    """CLI 便捷入口：指数 5m 同步。"""
    import argparse
    parser = argparse.ArgumentParser(description="A股主要指数 5m K线同步 → kline_index_5m")
    parser.add_argument("--indices", help="指定指数, 逗号分隔 (如 000001.SH,000300.SH)")
    parser.add_argument("--bars", type=int, default=SINA_BARS,
                        help=f"拉取根数 (sina 上限 {SINA_BARS})")
    parser.add_argument("--dry-run", action="store_true", help="只看不写")
    parser.add_argument("--verbose", "-v", action="store_true", help="详细输出")
    args = parser.parse_args()

    symbols = None
    if args.indices:
        symbols = [s.strip() for s in args.indices.split(",") if s.strip() in INDICES]
        if not symbols:
            logger.error("无有效指数代码")
            raise SystemExit(1)
    sync(symbols, dry_run=args.dry_run, verbose=args.verbose, bars=args.bars)


if __name__ == "__main__":
    main()

"""
realtime_snapshot.py — 全市场实时行情快照原始数据采集

═══════════════════════════════════════════════════════════════
  本文件负责盘中原始快照存储，不负责 K 线转换。
  精确 1m K 线由 backfill_db.run_1m() 盘后回填。
═══════════════════════════════════════════════════════════════

在架构中的位置:
  scheduler.py (interval=60, trading_only=True)
    → collect_realtime_snapshot()          ← 本文件
      → basicinfo_db.market_all_codes()    ← 股票列表
      → coordinator.coordinate_batch_quotes() ← 多源并发拉取
      → UPSERT 到 realtime_snapshot 单表

  scheduler.py (post_market_batch)
    → backfill_db.run_1m()                 ← 盘后精确1m K线

核心职责:
  1. 盘中每分钟拉取全市场实时行情快照 (原始数据)
     - 9:15~9:25 集合竞价段 / 9:30~11:31 与 13:00~15:01 连续竞价段
     (2026-10-17 由"9:26 单拍 + 9:30 起"扩到 9:15~9:25 整段, 见 _is_auction_time)
  2. 存入独立表 realtime_snapshot (单表，不按年分表)
  3. 供盘中 VWAP/换手率等指标直接读取，不经过 1m K 线中转

设计原则:
  1. 存原始数据，不做任何 K 线转换或指标计算
  2. ON CONFLICT (symbol, time) DO UPDATE 幂等写入，重复跑不产生重复数据
  3. 独立表，不与 kline 表混用
  4. 表自动创建 (CREATE TABLE IF NOT EXISTS)，无需手动建表
  5. 单表，保留 KEEP_DAYS 天历史（默认 8 天），过期自动清理
  6. **空拍不入库** (2026-10-17): open<=0 且 volume<=0 的记录是"无成交前收报价",
     竞价段扩窗后这类会大量出现, 落库必污染下游 (见 _extract_snapshot_records)

表结构 (realtime_snapshot):
  核心字段 — 与 coordinator 返回的行情 dict 字段一一对应:
    symbol, time, "last", open, high, low, "previousClose", volume
  扩展字段 — extras JSONB，各源返回的额外数据 (amount/change/changePercent 等)，
    有就存，没有就是 NULL，不同源返回内容不同，不保证一致性。

⚠️ 容易踩坑的点:
  1. "last" 和 "previousClose" 是 PostgreSQL 保留字/特殊名，SQL 中必须加双引号
  2. coordinator 返回的字段名是 camelCase (previousClose)，不是 snake_case
  3. volume 单位是"股"，由各 Provider 自行归一化——**不是本模块保证的**。
     ⚠️ 2026-10-23 事故：tencent 对科创板 parts[6] 返回"股"却按"手"×100，
     叠加 coordinator 逐拍换源 ⇒ 累计量在真实值与 ×100 之间跳变，下游资金流
     据此算出 688787 主力净额 1349 亿。根因已在 provider 层修复
     （`tencent.py::_volume_to_shares`）；本层另加 `_mark_volume_jumps`
     打 `extras.vol_jump` 标记作为第二道观测（**只标记不丢弃**）。
  4. extras 内容因源而异，不要在业务逻辑中依赖 extras 的具体字段
  5. 与 backfill_db 的 kline 表是两套独立数据，不要混用
  6. **落库 time 用的是"数据源返回的行情时间"**（q.time 优先，缺失才用整分钟兜底），
     所以同一次调度写出的行 time 会秒级错开（实测 9:26 那次写出 09:25:00~09:26:22），
     别误以为是多次调度; 也因此"按分钟整点"去数拍数会低估真实调度频次。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List

from app.utils.logger import get_logger

logger = get_logger(__name__)

TZ_CN = timezone(timedelta(hours=8))

# 单表名 — 2026-09-26 由按年分表改为单表（KEEP_DAYS 清理机制已控量）
_TABLE = "realtime_snapshot"


# ================================================================
# 建表
# ================================================================

def _ensure_snapshot_table():
    """确保 realtime_snapshot 单表存在 (idempotent)"""
    from app.utils.db_market import get_market_db_manager
    mgr = get_market_db_manager()
    pool = mgr._get_pool("CNStock")
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS "{_TABLE}" (
                    symbol           VARCHAR(16) NOT NULL,
                    time             TIMESTAMP WITHOUT TIME ZONE NOT NULL,
                    name             VARCHAR(64),
                    "last"           DOUBLE PRECISION,
                    open             DOUBLE PRECISION,
                    high             DOUBLE PRECISION,
                    low              DOUBLE PRECISION,
                    "previousClose" DOUBLE PRECISION,
                    change           DOUBLE PRECISION,
                    "changePercent"  DOUBLE PRECISION,
                    volume           DOUBLE PRECISION,
                    extras           JSONB,
                    PRIMARY KEY (symbol, time)
                )
            """)
            conn.commit()


# ================================================================
# 股票列表
# ================================================================

def _load_all_codes() -> List[str]:
    """从 basicinfo_db 获取全市场活跃股票代码 (纯6位数字)"""
    from app.utils.basicinfo_db import get_stock_basic_db
    db = get_stock_basic_db()
    return db.market_all_codes(status="active")


# ================================================================
# 快照数据提取
# ================================================================

# 核心字段集合 — 在这些范围内的 key 存独立列，其余打入 extras JSONB
# 注意: "close" 和 "price" 是部分源对 "last" 的别名，归入核心字段避免重复存
_CORE_KEYS = {"symbol", "last", "open", "high", "low", "previousClose", "prev_close", "volume", "close", "price", "name", "change", "changePercent", "time"}


def _extract_snapshot_records(quotes: List[Dict], fallback_ts: str):
    """
    coordinator 返回的行情 dict → 快照记录列表。

    核心字段存独立列，其余字段打包到 extras JSONB (有就存，没有忽略)。
    time 字段优先使用数据源返回的 time，若无则使用 fallback_ts。

    两道丢弃闸门（2026-10-17 竞价段扩窗配套）:
      1. last <= 0 —— 无效报价（停牌/源未返回），历史行为不变;
      2. **空拍**: open<=0 且 volume<=0 —— 日内既无开盘价又无成交，纯"前收报价",
         对下游全是负资产（cn_stock 会拿它拼今日 1D bar、fund_flow 会按 last 的
         涨跌方向伪造资金流）。集合竞价 9:15~9:25 正是这类报价的高发区，
         扩窗采集后必须挡掉；实测全库该类记录约占每天 0.2%~0.28%。

    ⚠️ 竞价段的代价：9:15-9:25 若数据源只给前收报价，则这 11 分钟每分钟一次
       的全市场拉取都不会产生有效行（白跑）。日志里的 empty_pick 计数可直接
       看出源在竞价段到底给不给数据（若恒为全量 => 竞价段数据全被过滤）。

    Returns:
        (records, skipped_empty) —— skipped_empty 为被空拍闸门丢弃的条数
    """
    records = []
    skipped_empty = 0
    for q in quotes:
        sym = q.get("symbol", "")
        last = q.get("last") or q.get("close") or q.get("price") or 0
        if last <= 0:
            continue

        # 核心字段
        open_px = q.get("open") or 0
        cum_vol = q.get("volume") or 0
        if open_px <= 0 and cum_vol <= 0:
            skipped_empty += 1
            continue

        rec = {
            "symbol": sym,
            "time": q.get("time") or fallback_ts,
            "name": q.get("name") or "",
            "last": last,
            "open": open_px,
            "high": q.get("high") or 0,
            "low": q.get("low") or 0,
            "previousClose": q.get("previousClose") or q.get("prev_close") or 0,
            "change": q.get("change") or 0,
            "changePercent": q.get("changePercent") or 0,
            "volume": cum_vol,
        }

        # 扩展字段 → extras (有就存，没有不加)
        extras = {k: v for k, v in q.items() if k not in _CORE_KEYS and v is not None and v != ""}
        if extras:
            rec["extras"] = extras

        records.append(rec)
    return records, skipped_empty


# ================================================================
# DB 写入 (UPSERT)
# ================================================================

def _bulk_upsert(records: List[Dict]) -> int:
    """
    批量 UPSERT 快照到 realtime_snapshot (单表)。

    ON CONFLICT (symbol, time) DO UPDATE — 幂等，同一 symbol+time 覆盖更新。
    按 symbol 分组写入，单个 symbol 失败不影响其他。

    Returns:
        成功写入的记录数
    """
    if not records:
        return 0

    import json as _json
    from psycopg2.extras import execute_values
    from app.utils.db_market import get_market_db_manager, _ensure_datetime

    mgr = get_market_db_manager()
    pool = mgr._get_pool("CNStock")

    # 按 symbol 分组
    by_symbol: Dict[str, List[Dict]] = {}
    for rec in records:
        sym = rec["symbol"]
        by_symbol.setdefault(sym, []).append(rec)

    total = 0
    with pool.connection() as conn:
        cur = conn.cursor()
        for sym, recs in by_symbol.items():
            values = [
                (r["symbol"], _ensure_datetime(r["time"]),
                 r["name"], r["last"], r["open"], r["high"], r["low"],
                 r["previousClose"], r["change"], r["changePercent"],
                 r["volume"],
                 _json.dumps(r["extras"]) if r.get("extras") else None)
                for r in recs
            ]
            sql = f"""
                INSERT INTO "{_TABLE}"
                    (symbol, time, name, "last", open, high, low,
                     "previousClose", change, "changePercent", volume, extras)
                VALUES %s
                ON CONFLICT (symbol, time) DO UPDATE SET
                    name            = EXCLUDED.name,
                    "last"          = EXCLUDED."last",
                    open            = EXCLUDED.open,
                    high            = EXCLUDED.high,
                    low             = EXCLUDED.low,
                    "previousClose" = EXCLUDED."previousClose",
                    change          = EXCLUDED.change,
                    "changePercent"  = EXCLUDED."changePercent",
                    volume          = EXCLUDED.volume,
                    extras          = EXCLUDED.extras
            """
            try:
                execute_values(cur, sql, values, page_size=5000)
                total += len(values)
                conn.commit()
            except Exception as e:
                conn.rollback()
                logger.warning("[realtime_snapshot] 写入 %s 失败: %s", sym, e)
        cur.close()

    return total


# ================================================================
# volume 跳变标记
# ================================================================

# 2026-10-23 事故配套：coordinator 是「主池 + 重试池、失败即换源」，同一 symbol
# 逐拍可能来自不同源；而**各源 volume 单位口径并非天然一致**——tencent 对科创板
# parts[6] 返回"股"、对其余板块返回"手"，历史代码无条件 ×100 ⇒ 科创板累计量在
# 真实值与 ×100 之间来回跳变；下游资金流按正增量累加 ⇒ 688787 算出主力净额 1349 亿。
#
# 这里**只打标记、不过滤**：
#   - 开盘瞬时真实增长可达数倍（实测 688787 09:25→09:30: 13.2 万 → 52.5 万，约 4x），
#     而单位跳变是 100x 量级 ⇒ 阈值 20x 足以分离常态与跳变；
#   - 但极端小成交量票（竞价几百股 → 开盘几十万股）仍可能误标，
#     故不在写入端丢弃，改由消费侧按标记决策，避免误杀真实数据。
_VOL_JUMP_RATIO = 20.0


def _mark_volume_jumps(records: List[Dict], day_start: str) -> int:
    """给疑似单位跳变的拍打 `extras.vol_jump` 标记（原地修改）。

    判据：本次 volume ≥ 该 symbol 当日已入库最大累计量 × `_VOL_JUMP_RATIO`。
    累计量在日内单调不减，正常单拍增量不可能到 20 倍。

    Args:
        records:   `_extract_snapshot_records` 产出的记录列表
        day_start: 当日 00:00:00，用于限定"当日已入库累计量"的查询范围

    Returns:
        被打标记的条数
    """
    if not records:
        return 0

    from app.utils.db_market import get_market_db_manager

    pool = get_market_db_manager()._get_pool("CNStock")
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f'SELECT symbol, MAX(volume) FROM "{_TABLE}" '
                "WHERE time >= %s::timestamp GROUP BY symbol",
                (day_start,),
            )
            prev_max = {r[0]: (r[1] or 0) for r in cur.fetchall()}

    marked = 0
    samples: List[str] = []
    for rec in records:
        vol = rec.get("volume") or 0
        pv = prev_max.get(rec["symbol"])
        if pv and pv > 0 and vol >= pv * _VOL_JUMP_RATIO:
            extras = rec.get("extras") or {}
            extras["vol_jump"] = True
            extras["vol_jump_prev_max"] = pv
            rec["extras"] = extras
            marked += 1
            if len(samples) < 5:
                samples.append(f"{rec['symbol']} {vol:,.0f} vs 前值 {pv:,.0f}")

    if marked:
        logger.warning(
            "[realtime_snapshot] volume 跳变拍 %d 条（疑似源单位不一致，已打 "
            "extras.vol_jump 标记，不丢弃）样例: %s",
            marked, "; ".join(samples),
        )
    return marked


# ================================================================
# 交易时间判断
# ================================================================

def _is_auction_time(now: datetime) -> bool:
    """集合竞价段: 9:15~9:25 整段 (2026-10-17 由"仅 9:26 一拍"扩到整段、右端收到 9:25)。

    为什么要整段而不是单拍：
      9:15-9:20 可撤单、9:20-9:25 不可撤单 —— 两段的竞价博弈状态完全不同，
      单拍只拿到撮合后的结果，等于放弃了盘前竞价的过程数据。
      撮合前的报价多为"无成交前收"（open=0/volume=0），由 `_extract_snapshot_records`
      的空拍闸门挡在库外，不会污染下游。

    为什么右端是 9:25 而不是 9:29：
      9:25 集合竞价一次性撮合出开盘价，此后到 9:30 开盘前**不会有新成交**，
      9:26~9:29 每分钟再拉一次拿到的都是同一份撮合结果，属纯开销+限流风险。
      ⚠️ 代价：9:25 那一拍若源尚未刷新出撮合结果（拉到的仍是撮合前报价），
         会全被空拍闸门拦下 → 当日就完全没有开盘价快照。
         实测 10-15/10-16 的 9:26 拍才是稳定拿到撮合结果的一拍（09:25:00 那批
         2830/1050 条都是 9:26 调度写的，落库 time 用的是源返回的行情时间）。
         所以若首日上线后日志显示 9:25 拍 empty_pick 接近全量，就把右端
         放回 926 做兜底 —— 这是"源何时刷新"的外部事实，代码无法预知。
    """
    t = now.hour * 100 + now.minute
    return 915 <= t <= 925


def _is_trading_collect_time(now: datetime) -> bool:
    """连续竞价可采集时段: 9:30~11:31, 13:00~15:01 (与竞价段 9:15~9:25 相接不重叠)"""
    t = now.hour * 100 + now.minute
    return (930 <= t <= 1131) or (1300 <= t <= 1501)


# ================================================================
# 数据清理: 删除超过 KEEP_DAYS 天的快照数据
# ================================================================

# 快照保留天数（2026-09-26 由 5→8）。
# 盘中策略回看窗口、资金流近似计算、hub minute_live 当日回看均依赖此数据，
# 保留 8 天可覆盖完整两周工作日（含一个周末）。
KEEP_DAYS = 8


def _cleanup_old_snapshots(keep_days: int = KEEP_DAYS):
    """删除 realtime_snapshot 单表中超过 keep_days 天的数据（天然跨年）"""
    from app.utils.db_market import get_market_db_manager
    mgr = get_market_db_manager()
    pool = mgr._get_pool("CNStock")
    cutoff = datetime.now(TZ_CN) - timedelta(days=keep_days)
    cutoff_str = cutoff.strftime("%Y-%m-%d 00:00:00")
    try:
        with pool.connection() as conn:
            cur = conn.cursor()
            cur.execute(f'DELETE FROM "{_TABLE}" WHERE time < %s', (cutoff_str,))
            deleted = cur.rowcount
            conn.commit()
            cur.close()
            if deleted > 0:
                logger.info("[realtime_snapshot] 清理 %d 条过期数据 (< %s)", deleted, cutoff_str)
    except Exception as e:
        logger.warning("[realtime_snapshot] 清理过期数据失败: %s", e)


# ================================================================
# 主入口 — scheduler 调用
# ================================================================

def collect_realtime_snapshot() -> Dict:
    """
    全市场实时行情快照采集。

    流程: 交易时间检查 → 拉代码 → coordinator 批量拉取 → 原始数据 UPSERT 写入 → 清理过期数据

    交易时间:
      9:15~9:25 → 集合竞价段采集(撮合前的无成交报价由空拍闸门丢弃, 只留有成交的拍;
                9:25 撮合后到 9:30 无新成交, 故 9:26~9:29 不采)
      9:30~11:31 → 正常采集
      13:00~15:01 → 正常采集
      其他时段   → 跳过

    Returns:
        {"status": "ok"|"skip"|"error", "stocks": int, "written": int,
         "failed": int, "elapsed": float, "empty_pick": int}
    """
    t0 = time.time()
    now = datetime.now(TZ_CN)

    # ── 0. 交易时间检查 ──
    if now.weekday() >= 5:
        return {"status": "skip", "stocks": 0, "written": 0,
                "failed": 0, "elapsed": 0, "reason": "周末"}

    if not (_is_trading_collect_time(now) or _is_auction_time(now)):
        return {"status": "skip", "stocks": 0, "written": 0,
                "failed": 0, "elapsed": 0, "reason": "非交易时段"}

    # ── 1. 获取全市场代码 ──
    try:
        codes = _load_all_codes()
    except Exception as e:
        logger.error("[realtime_snapshot] 获取股票列表失败: %s", e)
        return {"status": "error", "stocks": 0, "written": 0,
                "failed": 0, "elapsed": time.time() - t0}

    if not codes:
        logger.warning("[realtime_snapshot] 股票列表为空")
        return {"status": "error", "stocks": 0, "written": 0,
                "failed": 0, "elapsed": time.time() - t0}

    # ── 2. coordinator 批量拉取 ──
    from app.data_sources.coordinator import get_coordinator
    coord = get_coordinator()
    quotes = coord.coordinate_batch_quotes(
        symbols=codes, market="CNStock", timeout=60,
    )

    if not quotes:
        logger.warning("[realtime_snapshot] 拉取 0 条行情 (全市场 %d 只)", len(codes))
        return {"status": "error", "stocks": len(codes), "written": 0,
                "failed": len(codes), "elapsed": time.time() - t0}

    # ── 3. 提取原始快照（丢弃 last<=0 与空拍 open<=0 AND volume<=0）──
    ts_str = now.strftime("%Y-%m-%d %H:%M:00")
    records, empty_pick = _extract_snapshot_records(quotes, ts_str)
    if not records:
        # 竞价段常态：源此时只给"前收报价"（无开盘价无成交）→ 全部被空拍闸门拦下。
        # 不视为错误（源本身在竞价段就没数据），记 info 便于次日核对源行为。
        logger.info(
            "[realtime_snapshot] %s 全部为空拍/无效报价 (拉取 %d 只, 空拍 %d), 跳过写入",
            now.strftime("%H:%M"), len(quotes), empty_pick,
        )
        return {"status": "ok", "stocks": len(codes), "written": 0,
                "failed": 0, "elapsed": time.time() - t0,
                "empty_pick": empty_pick}

    # ── 4. 建表 + volume 跳变标记 + UPSERT ──
    #    标记必须在建表之后（要查当日已入库累计量做对比）、写入之前。
    _ensure_snapshot_table()
    vol_jumps = _mark_volume_jumps(records, now.strftime("%Y-%m-%d 00:00:00"))
    written = _bulk_upsert(records)

    elapsed = time.time() - t0
    fetched = len(quotes)
    total = len(codes)
    failed = total - fetched
    success_rate = fetched / total * 100 if total > 0 else 0

    # 找出缺失的股票 (采样前20个)
    quote_symbols = {q.get("symbol", "") for q in quotes}
    missing = [c for c in codes if c not in quote_symbols]
    missing_sample = missing[:20]

    logger.info(
        "[realtime_snapshot] 完成: %d/%d 只 (%.1f%%), %d 条写入, %d 缺失, %d 空拍丢弃, "
        "%d 跳变标记, %.2fs",
        fetched, total, success_rate, written, failed, empty_pick, vol_jumps, elapsed,
    )
    if failed > 0:
        logger.info(
            "[realtime_snapshot] 缺失采样 (%d只): %s",
            len(missing_sample), ", ".join(missing_sample),
        )

    # ── 5. 清理过期数据（保留 KEEP_DAYS 天） ──
    _cleanup_old_snapshots(keep_days=KEEP_DAYS)

    return {
        "status": "ok" if success_rate > 80 else "error",
        "stocks": total,
        "written": written,
        "failed": failed,
        "elapsed": round(elapsed, 2),
        "empty_pick": empty_pick,
        "vol_jumps": vol_jumps,
    }

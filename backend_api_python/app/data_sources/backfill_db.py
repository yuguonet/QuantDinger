"""
backfill_db.py — A 股 K 线增量同步 + 后台调度

═══════════════════════════════════════════════════════════════
  架构位置: backfill_db → coordinator(kline/15m) / coordinator(1D)
═══════════════════════════════════════════════════════════════

核心职责:
  1. 交易日 15:05 后同步当日 15m bar（16 条，走 kline API）
  2. 交易日 17:00 后同步当日 1D bar
  3. 首次运行时做历史回填
  4. 后台自动调度，不影响主线程

数据流:
  15m → coordinator.coordinate_market_kline() → 16 bars/标的 → bulk_write
  1D  → coordinator.coordinate_batch_quotes() → 重试+去重 → bulk_write
  ↓
  db_market.upsert() → PostgreSQL
  ↓
  cn_last_update 记录同步状态

cn_last_update 表结构:
    CREATE TABLE public.cn_last_update (
        id int4 DEFAULT nextval('cn_last_update_new_id_seq'::regclass) NOT NULL,
        tf varchar(10) NOT NULL,
        last_bar_time timestamp NOT NULL,
        status varchar(20) DEFAULT 'ok'::character varying NULL,
        report text NULL,
        failed_count int4 DEFAULT 0 NULL,
        synced_count int4 DEFAULT 0 NULL,
        written_count int4 DEFAULT 0 NULL,
        CONSTRAINT cn_last_update_new_pkey PRIMARY KEY (id, last_bar_time),
        CONSTRAINT cn_last_update_new_status_check CHECK (((status)::text = ANY ((ARRAY['ok'::character varying, 'error'::character varying, 're'::character varying])::text[])))
    );
设计原则:
  1. cn_last_update 是唯一的调度控制表
  2. 15m 每个交易日 15:05 后同步一次（kline API，当天 16 条 bar）
     盘中无法获取分时线 OHLCV 中的 HL 值，故不在盘中拉取
  3. 1D 每个交易日 17:00 后同步一次
  4. 非交易日不执行
  5. 后台 daemon 线程自动运行，fire-and-forget
  6. 所有数据源走内联 provider，不依赖外部 API
"""

import re as _re
import threading
from datetime import datetime, timedelta, timezone, time as dt_time

from app.utils.db_market import get_market_db_manager, get_market_kline_writer
from app.utils.trading_calendar import is_trading_day, prev_trading_day, next_trading_day, last_finish_trading_day
from app.utils.logger import get_logger

logger = get_logger(__name__)

TZ_CN = timezone(timedelta(hours=8))

# 下载超时（秒）
_BATCH_TIMEOUT = 300

# 15m 每个交易日拉取的 bar 数量（9:30-15:00 共 16 根 15m bar）
_15M_BARS_PER_DAY = 16

# 15m 标准 bar 结束时间有序列表（用于归一化查找）
_VALID_15M_BAR_TIMES_SORTED = sorted({
    (9, 45), (10, 0), (10, 15), (10, 30),
    (10, 45), (11, 0),  (11, 15), (11, 30),
    (13, 15), (13, 30), (13, 45), (14, 0),
    (14, 15), (14, 30), (14, 45), (15, 0),
})

# 各周期的触发截止时间和 bar 结束时间
_TF_CONFIG = {
    "15m": {"cutoff": (15, 5),  "bar_hour": 15, "bar_minute": 0},
    "1D":  {"cutoff": (17, 0),  "bar_hour": 0,  "bar_minute": 0},
}

# 调度器常量
_INITIAL_DELAY = 300          # 进程启动后首次执行延迟（秒）
_RETRY_INTERVAL = 120         # 修复轮次间等待（秒）
_MIN_DELAY = 30               # 最小调度延迟（秒），防止 0 延迟
_MAX_REPAIR_ATTEMPTS = 9      # 循环<10次修复（初始1次 + 最多9次重试）


# ================================================================
# cn_last_update 表 — 同步的唯一控制机制（PostgreSQL）
# ================================================================

def _get_last_update(tf: str, pool_name: str = "CNStock") -> dict | None:
    """查询 cn_last_update 最新记录（按 tf 匹配最新一条）。"""
    try:
        mgr = get_market_db_manager()
        pool = mgr._get_pool(pool_name)
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT last_bar_time, status, report, failed_count, synced_count, written_count "
                    "FROM cn_last_update "
                    "WHERE tf = %s "
                    "ORDER BY last_bar_time DESC LIMIT 1",
                    (tf,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return {
                    "last_bar_time": row[0],
                    "status": row[1],
                    "report": row[2],
                    "failed_count": row[3] or 0,
                    "synced_count": row[4] or 0,
                    "written_count": row[5] or 0,
                }
    except Exception as e:
        logger.error(f"[同步] 查询 cn_last_update 失败: {e}")
        return None


def _insert_record(tf: str, status: str, report: str,
                   last_bar_time: datetime | None = None,
                   synced_count: int | None = None,
                   failed_count: int | None = None,
                   pool_name: str = "CNStock",
                   written_count: int | None = None):
    """INSERT 新行到 cn_last_update。"""
    try:
        naive_lbt = last_bar_time.replace(tzinfo=None) if last_bar_time and last_bar_time.tzinfo else last_bar_time
        mgr = get_market_db_manager()
        pool = mgr._get_pool(pool_name)
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO cn_last_update
                        (tf, last_bar_time, status, report,
                         synced_count, failed_count, written_count)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (tf, naive_lbt, status, report,
                      synced_count, failed_count, written_count))
                conn.commit()
    except Exception as e:
        logger.error(f"[同步] INSERT cn_last_update 失败: {e}")


def _update_record(tf: str, last_bar_time: datetime,
                   status: str | None = None, report: str | None = None,
                   synced_count: int | None = None,
                   failed_count: int | None = None,
                   written_count: int | None = None,
                   pool_name: str = "CNStock"):
    """UPDATE 已有行，按 (tf, last_bar_time) 定位。只更新传入的非 None 字段。"""
    naive_lbt = last_bar_time.replace(tzinfo=None) if last_bar_time.tzinfo else last_bar_time
    sets: list[str] = []
    params: list = []
    for field, val in [("status", status), ("report", report),
                       ("synced_count", synced_count), ("failed_count", failed_count),
                       ("written_count", written_count)]:
        if val is not None:
            sets.append(f"{field} = %s")
            params.append(val)
    if not sets:
        return
    params.extend([tf, naive_lbt])
    try:
        mgr = get_market_db_manager()
        pool = mgr._get_pool(pool_name)
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    UPDATE cn_last_update
                    SET {', '.join(sets)}
                    WHERE tf = %s AND last_bar_time = %s
                """, params)
                conn.commit()
                if cur.rowcount == 0:
                    logger.warning(f"[同步] UPDATE cn_last_update 未匹配: tf={tf}, last_bar_time={naive_lbt}")
    except Exception as e:
        logger.error(f"[同步] UPDATE cn_last_update 失败: {e}")


# ================================================================
# 判断逻辑
# ================================================================

def _parse_db_timestamp(ts) -> datetime | None:
    """将 DB 返回的时间戳统一为带 TZ_CN 的 datetime。"""
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts.astimezone(TZ_CN) if ts.tzinfo else ts.replace(tzinfo=TZ_CN)
    if isinstance(ts, str):
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(ts, fmt).replace(tzinfo=TZ_CN)
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(ts).replace(tzinfo=TZ_CN) if not ts.endswith("Z") else \
                   datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(TZ_CN)
        except ValueError:
            logger.warning(f"[同步] 无法解析时间戳: {ts}")
            return None
    return None


def _same_trading_day(dt1: datetime, dt2: datetime) -> bool:
    """判断两个时间点是否在同一个交易日。"""
    d1, d2 = dt1.strftime("%Y-%m-%d"), dt2.strftime("%Y-%m-%d")
    if d1 == d2:
        return True
    def _own_td(d: str) -> str:
        return d if is_trading_day(d) else prev_trading_day(d)
    return _own_td(d1) == _own_td(d2)


def _normalize_15m_bar_time(dt_obj: datetime) -> datetime | None:
    """将任意时间标准化到其所属 15m bar 的标准结束时间。"""
    t = dt_obj.astimezone(TZ_CN) if dt_obj.tzinfo else dt_obj.replace(tzinfo=TZ_CN)
    t_time = t.time()
    if t_time < dt_time(9, 30):
        return None
    if dt_time(11, 30) < t_time < dt_time(13, 0):
        return None
    for h, m in _VALID_15M_BAR_TIMES_SORTED:
        if t_time <= dt_time(h, m):
            return datetime(t.year, t.month, t.day, h, m, 0, tzinfo=TZ_CN)
    return datetime(t.year, t.month, t.day, 15, 0, 0, tzinfo=TZ_CN)


def _compute_target_td(tf: str) -> str:
    """根据当前时间和周期，计算目标交易日（已结束的交易日）。"""
    cutoff = _TF_CONFIG[tf]["cutoff"]
    cutoff_str = f"{cutoff[0]:02d}:{cutoff[1]:02d}"
    return last_finish_trading_day(cutoff_str)


def _make_bar_time(target_td: str, tf: str) -> datetime:
    """根据目标交易日和周期，生成 bar_time。"""
    cfg = _TF_CONFIG[tf]
    return datetime.strptime(target_td, "%Y-%m-%d").replace(
        hour=cfg["bar_hour"], minute=cfg["bar_minute"], second=0, tzinfo=TZ_CN
    )


def _extract_failed_symbols_from_report(report_text: str) -> list[str]:
    """从 cn_last_update.report 中提取失败标的代码列表。"""
    if not report_text:
        return []
    m = _re.search(r"失败标的\(\d+\):\s*([^\s;]+)", report_text)
    if m:
        return [s.strip() for s in m.group(1).split(",") if s.strip()]
    return []


def _compute_is_update(task: str, last_bar_time: datetime) -> bool:
    """计算 is_update: 是否需要全新拉取。

    is_update = ((当前时间 - (date(db最后时间)+cutoff)) > (下一交易日 - 当前交易日))
    当前交易日 = last_finish_trading_day(cutoff)
    """
    now = datetime.now(TZ_CN)
    cutoff_h, cutoff_m = _TF_CONFIG[task]["cutoff"]
    cutoff_str = f"{cutoff_h:02d}:{cutoff_m:02d}"

    # 当前交易日（已结束的交易日）
    current_td = last_finish_trading_day(cutoff_str)

    # db最后时间对应的交易日 + cutoff
    db_date = last_bar_time.astimezone(TZ_CN).strftime("%Y-%m-%d") if last_bar_time.tzinfo else last_bar_time.strftime("%Y-%m-%d")
    db_cutoff = datetime.strptime(db_date, "%Y-%m-%d").replace(
        hour=cutoff_h, minute=cutoff_m, second=0, tzinfo=TZ_CN
    )

    # 下一交易日 - 当前交易日（天数差）
    next_td = datetime.strptime(next_trading_day(current_td), "%Y-%m-%d").replace(tzinfo=TZ_CN)
    current_td_dt = datetime.strptime(current_td, "%Y-%m-%d").replace(tzinfo=TZ_CN)

    return (now - db_cutoff) > (next_td - current_td_dt)


def _read_sync_progress(task: str, pool_name: str = "CNStock") -> tuple[dict | None, str, int, int, int, float]:
    """读取同步进度，返回 (doc, status, synced, failed, written, sync_rate)。"""
    doc = _get_last_update(task, pool_name=pool_name)
    if not doc:
        return None, "", 0, 0, 0, 0.0
    synced = doc.get("synced_count") or 0
    failed = doc.get("failed_count") or 0
    written = doc.get("written_count") or 0
    sync_rate = (synced - failed) / synced if synced > 0 else 0.0
    return doc, doc.get("status", ""), synced, failed, written, sync_rate


# ================================================================
# 数据源配置
# ================================================================

class BackfillSource:
    """数据源配置。"""
    def __init__(self, name: str, market: str, timeframe: str, db_pool: str = "CNStock"):
        self.name = name
        self.market = market
        self.timeframe = timeframe
        self.db_pool = db_pool


# ================================================================
# 同步执行器
# ================================================================

class BackfillDB:
    """全盘批量同步工具。"""

    def __init__(self, source: BackfillSource):
        self.source = source
        self._writer = get_market_kline_writer()

    # ── OHLCV 提取 + 校验 ──

    @staticmethod
    def _parse_ohlcv(record: dict) -> tuple[float, float, float, float, float] | None:
        """从 quote/bar dict 提取 OHLCV，校验后返回 (o, h, l, c, v)。"""
        o = float(record.get("open", 0) or 0)
        h = float(record.get("high", 0) or 0)
        l = float(record.get("low", 0) or 0)
        c = float(record.get("close", 0) or 0)
        v = float(record.get("volume", 0) or 0)
        if c <= 0:
            c = float(record.get("last", 0) or 0)
        if o == 0 and h == 0 and l == 0 and c == 0:
            return None
        if c <= 0 or o <= 0:
            return None
        if h > 0 and l > 0 and h < l:
            h, l = l, h
        return o, h, l, c, v

    @staticmethod
    def _parse_bar_time(bar: dict, fallback: datetime) -> datetime:
        """从 kline bar dict 解析时间戳，失败则返回 fallback。"""
        bt = bar.get("time")
        if isinstance(bt, str):
            try:
                return datetime.strptime(bt, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ_CN)
            except ValueError:
                return fallback
        if isinstance(bt, datetime):
            return bt.astimezone(TZ_CN) if bt.tzinfo else bt.replace(tzinfo=TZ_CN)
        return fallback

    @staticmethod
    def _make_bar_record(symbol: str, tf: str, bar_time: datetime,
                         ohlcv: tuple[float, float, float, float, float]) -> dict:
        """构造标准 bar record dict。"""
        o, h, l, c, v = ohlcv
        return {
            "symbol": symbol, "timeframe": tf, "time": bar_time,
            "open": o, "high": h, "low": l, "close": c, "volume": v,
        }

    # ── run_once 入口 ──

    def run_once(self, tf: str | None = None, symbols: list | None = None,
                 skip_repair: bool = False, force_refetch: bool = False) -> dict:
        """执行一次同步。"""
        tf = tf or self.source.timeframe
        pool = self.source.db_pool

        existing_doc = _get_last_update(tf, pool_name=pool)
        existing_lbt = _parse_db_timestamp(existing_doc.get("last_bar_time")) if existing_doc else None

        try:
            is_first_run = not (existing_lbt and _same_trading_day(existing_lbt, datetime.now(TZ_CN)))
            written, failed = self._sync(tf, symbols, is_first_run=is_first_run,
                                          skip_repair=skip_repair, force_refetch=force_refetch)
        except Exception as e:
            error_msg = f"同步异常: {e}"
            logger.error(f"[同步] {self.source.name} tf={tf} {error_msg}")
            existing_report = existing_doc.get("report", "") if existing_doc else ""
            existing_failed_syms = _extract_failed_symbols_from_report(existing_report)
            report = f"{error_msg}; 失败标的({len(existing_failed_syms)}): {','.join(existing_failed_syms[:50])}" if existing_failed_syms else error_msg
            if existing_lbt:
                _update_record(tf, existing_lbt, status="re", report=report, pool_name=pool,
                              failed_count=(existing_doc.get("failed_count") or 0) + 1 if existing_doc else None,
                              synced_count=existing_doc.get("synced_count") if existing_doc else None,
                              written_count=existing_doc.get("written_count") if existing_doc else None)
            else:
                _insert_record(tf, "re", report,
                              last_bar_time=datetime.now(TZ_CN).replace(hour=0, minute=0, second=0, microsecond=0),
                              synced_count=0, failed_count=1, pool_name=pool, written_count=0)
            return {"source": self.source.name, "tf": tf, "written": 0, "status": "error", "report": report}

        if failed:
            logger.warning(f"[同步] {self.source.name} tf={tf} {len(failed)} 只标的失败，下次同步自然重试")

        doc = _get_last_update(tf, pool_name=pool)
        return {
            "source": self.source.name, "tf": tf, "written": written,
            "status": doc.get("status", "ok") if doc else "ok",
            "report": doc.get("report", f"写入 {written} 条") if doc else f"写入 {written} 条",
        }

    # ── 数据拉取 ──

    def _fetch_data(self, tf: str, symbols: list) -> list[dict]:
        """统一数据拉取入口。15m 走 kline API，1D 走 batch_quotes。"""
        from app.data_sources.coordinator import get_coordinator
        coord = get_coordinator()

        if tf == "15m":
            try:
                klines = coord.coordinate_market_kline(
                    symbols=symbols, market=self.source.market,
                    timeframe="15m", count=_15M_BARS_PER_DAY, timeout=float(_BATCH_TIMEOUT),
                )
                if klines:
                    return klines
            except Exception as e:
                logger.warning(f"[同步] {self.source.name} kline 拉取失败: {e}")
            return []

        try:
            quotes = coord.coordinate_batch_quotes(
                symbols=symbols, market=self.source.market, timeout=float(_BATCH_TIMEOUT),
            )
            if quotes:
                return quotes
        except Exception as e:
            logger.warning(f"[同步] {self.source.name} batch_quotes 拉取失败: {e}")
        return []

    # ── 记录构建 ──

    def _build_records(self, tf: str, symbols: list, raw_data: list[dict],
                       bar_time: datetime, failed_reasons: dict[str, str]) -> list[dict]:
        """统一记录构建入口。"""
        from app.data_sources.normalizer import strip_market_prefix

        if tf == "15m":
            return self._build_records_15m(symbols, raw_data, bar_time, failed_reasons)

        # 1D
        quote_map = {q["symbol"]: q for q in raw_data if q.get("symbol")}
        records: list[dict] = []
        for symbol in symbols:
            clean = strip_market_prefix(symbol)
            quote = quote_map.get(symbol) or quote_map.get(clean)
            if not quote:
                failed_reasons.setdefault(symbol, "无行情数据")
                continue
            ohlcv = self._parse_ohlcv(quote)
            if ohlcv is None:
                failed_reasons.setdefault(symbol, "OHLCV 无效(停牌/退市/价格异常)")
                continue
            records.append(self._make_bar_record(clean, tf, bar_time, ohlcv))
        return records

    def _build_records_15m(self, symbols: list, raw_data: list[dict],
                           bar_time: datetime, failed_reasons: dict[str, str]) -> list[dict]:
        """将 kline 扁平列表转换为 bulk_write 记录（15m）。"""
        from app.data_sources.normalizer import strip_market_prefix

        kline_map: dict[str, list[dict]] = {}
        for bar in raw_data:
            sym = bar.get("symbol", "")
            if sym:
                kline_map.setdefault(sym, []).append(bar)

        records: list[dict] = []
        for symbol in symbols:
            clean = strip_market_prefix(symbol)
            bars = kline_map.get(symbol) or kline_map.get(clean) or []
            if not bars:
                failed_reasons.setdefault(symbol, "无 kline 数据")
                continue

            seen: dict[tuple[str, datetime], dict] = {}
            for bar in bars:
                raw_time = self._parse_bar_time(bar, bar_time)
                bar_time_dt = _normalize_15m_bar_time(raw_time)
                if bar_time_dt is None:
                    logger.debug(f"[同步] {clean} 过滤非交易时段 bar: {raw_time:%Y-%m-%d %H:%M}")
                    continue
                ohlcv = self._parse_ohlcv(bar)
                if ohlcv is None:
                    continue
                seen[(clean, bar_time_dt)] = self._make_bar_record(clean, "15m", bar_time_dt, ohlcv)

            if not seen:
                failed_reasons.setdefault(symbol, "无有效 15m bar")
                continue
            records.extend(seen.values())

        return records

    # ── 统一同步主逻辑 ──

    def _sync(self, tf: str, symbols: list | None = None, is_first_run: bool = True,
              skip_repair: bool = False, force_refetch: bool = False) -> tuple[int, list[str]]:
        """统一同步逻辑，15m 和 1D 共用。"""
        from app.data_sources.normalizer import strip_market_prefix

        if not symbols:
            from app.utils.basicinfo_db import get_stock_basic_db
            symbols = get_stock_basic_db().market_all_codes(status="active")
        if not symbols:
            logger.warning(f"[同步] {self.source.name} 获取股票列表失败")
            return 0, []

        total_symbols = len(symbols)
        target_td = _compute_target_td(tf)
        bar_time = _make_bar_time(target_td, tf)
        pool = self.source.db_pool

        # ── 新交易日首次写入: INSERT 初始记录 ──
        existing = _get_last_update(tf, pool_name=pool)
        existing_lbt = _parse_db_timestamp(existing.get("last_bar_time")) if existing else None
        is_new_day = not existing_lbt or not _same_trading_day(existing_lbt, bar_time)
        if is_new_day:
            _insert_record(tf, "re", f"开始同步 {tf} ({total_symbols} 只标的)",
                          last_bar_time=bar_time, synced_count=total_symbols,
                          failed_count=0, pool_name=pool, written_count=0)
        elif existing:
            # 修复场景: 使用 DB 中的总股票数，而非本次传入的子集大小
            total_symbols = existing.get("synced_count") or total_symbols

        # ── 循环拉取 ──
        total_written = 0
        round_num = 0
        first_write_done = False
        failed_reasons: dict[str, str] = {}

        while True:
            round_num += 1
            remaining = self._get_remaining(tf, bar_time, symbols, pool, force_refetch)
            force_refetch = False

            if not remaining:
                logger.info(f"[同步] {self.source.name} {tf} 所有 {total_symbols} 只已同步")
                break

            logger.info(f"[同步] {self.source.name} {tf} 第 {round_num} 轮，待拉取 {len(remaining)}")

            raw_data = self._fetch_data(tf, remaining)
            if not raw_data:
                logger.warning(f"[同步] {self.source.name} {tf} 第 {round_num} 轮返回空数据，停止")
                for symbol in remaining:
                    failed_reasons.setdefault(symbol, "返回空数据")
                break

            records = self._build_records(tf, remaining, raw_data, bar_time, failed_reasons)
            if not records:
                logger.info(f"[同步] {self.source.name} {tf} 第 {round_num} 轮 0 新增，停止")
                break

            if is_first_run and not first_write_done:
                deleted = self._delete_bars(bar_time, tf=tf)
                if deleted > 0:
                    logger.info(f"[同步] {self.source.name} {tf} 首次运行，已清除当日 {deleted} 条旧 bar")
                first_write_done = True

            try:
                r = self._writer.bulk_write(self.source.market, records)
                round_written = r.get("inserted", 0) + r.get("skipped", 0)
            except Exception as e:
                logger.error(f"[同步] {self.source.name} {tf} 第 {round_num} 轮写入失败: {e}")
                break

            total_written += round_written
            synced_now = self._get_synced_symbols(bar_time, tf=tf)
            logger.info(f"[同步] {self.source.name} {tf} 第 {round_num} 轮 写入 {round_written}，累计 {len(synced_now)}/{total_symbols}")

            if len(synced_now) >= total_symbols:
                break

        # ── 阶段一统计 ──
        final_synced = self._get_synced_symbols(bar_time, tf=tf)
        final_count = len(final_synced)
        failed = [s for s in symbols if strip_market_prefix(s) not in final_synced]

        # ── 阶段二: 内部修复循环 ──
        if failed and not skip_repair:
            failed, final_count, total_written = self._internal_repair_loop(
                tf, bar_time, pool, symbols, failed, failed_reasons, total_symbols, total_written)

        # ── 最终落盘 ──
        self._finalize_sync(tf, bar_time, pool, total_symbols, final_count, failed, failed_reasons, total_written)
        return total_written, failed

    def _get_remaining(self, tf: str, bar_time: datetime, symbols: list,
                       pool: str, force_refetch: bool) -> list[str]:
        """计算待拉取的 symbols 列表。"""
        from app.data_sources.normalizer import strip_market_prefix
        if force_refetch:
            deleted = self._delete_symbols_bars(bar_time, symbols, tf=tf)
            if deleted > 0:
                logger.info(f"[同步] {self.source.name} {tf} force_refetch: 已清除 {deleted} 条旧 bar")
            return list(symbols)
        synced = self._get_synced_symbols(bar_time, tf=tf)
        return [s for s in symbols if strip_market_prefix(s) not in synced]

    def _internal_repair_loop(self, tf: str, bar_time: datetime, pool: str,
                              symbols: list, failed: list[str],
                              failed_reasons: dict[str, str],
                              total_symbols: int, total_written: int
                              ) -> tuple[list[str], int, int]:
        """run_once 内部的修复循环（skip_repair=False 时执行）。"""
        from app.data_sources.normalizer import strip_market_prefix

        repair_round = 0
        while failed:
            repair_round += 1
            logger.info(f"[同步] {self.source.name} {tf} 修复第 {repair_round} 轮，重拉 {len(failed)} 只失败标的")

            raw_data = self._fetch_data(tf, failed)
            if not raw_data:
                logger.info(f"[同步] {self.source.name} {tf} 修复第 {repair_round} 轮返回空，停止")
                break

            reasons: dict[str, str] = {}
            records = self._build_records(tf, failed, raw_data, bar_time, reasons)
            if not records:
                logger.info(f"[同步] {self.source.name} {tf} 修复第 {repair_round} 轮 0 有效，停止")
                break

            try:
                r = self._writer.bulk_write(self.source.market, records)
                total_written += r.get("inserted", 0) + r.get("skipped", 0)
            except Exception as e:
                logger.error(f"[同步] {self.source.name} {tf} 修复第 {repair_round} 轮写入失败: {e}")
                break

            final_synced = self._get_synced_symbols(bar_time, tf=tf)
            failed = [s for s in symbols if strip_market_prefix(s) not in final_synced]
            logger.info(f"[同步] {self.source.name} {tf} 修复第 {repair_round} 轮 累计 {len(final_synced)}/{total_symbols}，剩余失败 {len(failed)}")

            _update_record(tf, bar_time, status="re",
                          report=f"修复中: {len(final_synced)}/{total_symbols}, 失败 {len(failed)}",
                          synced_count=total_symbols, failed_count=len(failed),
                          pool_name=pool, written_count=len(final_synced))

        final_synced = self._get_synced_symbols(bar_time, tf=tf)
        return failed, len(final_synced), total_written

    def _finalize_sync(self, tf: str, bar_time: datetime, pool: str,
                       total_symbols: int, final_count: int,
                       failed: list[str], failed_reasons: dict[str, str], total_written: int):
        """最终落盘 cn_last_update + 日志输出。"""
        report_parts = [f"已同步 {final_count}/{total_symbols}"]
        if failed:
            reason_counts: dict[str, int] = {}
            for sym in failed:
                reason = failed_reasons.get(sym, "未返回数据")
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
            reason_summary = ", ".join(f"{r}({c})" for r, c in sorted(reason_counts.items(), key=lambda x: -x[1]))
            report_parts.append(f"失败原因: {reason_summary}")
            report_parts.append(f"失败标的({len(failed)}): {','.join(failed[:50])}")

        report = "; ".join(report_parts)
        sync_rate = final_count / total_symbols if total_symbols > 0 else 0
        final_status = "ok" if sync_rate > 0.9 and not failed else "re"

        _update_record(tf, bar_time, status=final_status, report=report,
                      synced_count=total_symbols, failed_count=len(failed),
                      pool_name=pool, written_count=final_count)

        if failed:
            logger.warning(f"[同步] {self.source.name} {tf} 完成，写入 {total_written}，同步 {final_count}/{total_symbols}，失败 {len(failed)}")
            for sym in failed[:20]:
                logger.warning(f"  ✗ {sym}: {failed_reasons.get(sym, '未返回数据')}")
            if len(failed) > 20:
                logger.warning(f"  ... 共 {len(failed)} 只失败，详见 cn_last_update.report")
        else:
            logger.info(f"[同步] {self.source.name} {tf} 完成，写入 {total_written}，同步 {final_count}/{total_symbols}，无失败")

    # ── DB 辅助方法 ──

    def _kline_table(self, bar_time: datetime, tf: str = "1D") -> str:
        """返回 kline_{tf}_{年} 表名。"""
        return f'"kline_{tf}_{bar_time.year}"'

    def _delete_bars(self, bar_time: datetime, tf: str = "1D", symbols: list[str] | None = None) -> int:
        """删除 bar 数据。symbols=None 删除当日全部，否则只删除指定 symbols。"""
        table = self._kline_table(bar_time, tf=tf)
        try:
            mgr = get_market_db_manager()
            pool = mgr._get_pool(self.source.db_pool)
            with pool.connection() as conn:
                with conn.cursor() as cur:
                    if symbols:
                        from app.data_sources.normalizer import strip_market_prefix
                        naive_lbt = bar_time.replace(tzinfo=None) if bar_time.tzinfo else bar_time
                        clean = [strip_market_prefix(s) for s in symbols]
                        cur.execute(f"DELETE FROM {table} WHERE time = %s AND symbol = ANY(%s)",
                                   (naive_lbt, clean))
                    else:
                        cur.execute(f"DELETE FROM {table} WHERE time::date = %s::date",
                                   (bar_time.strftime("%Y-%m-%d"),))
                    deleted = cur.rowcount
                    conn.commit()
                    return deleted
        except Exception as e:
            logger.warning(f"[同步] {self.source.name} {tf} 清除旧 bar 失败 (可忽略): {e}")
            return 0

    def _delete_symbols_bars(self, bar_time: datetime, symbols: list[str], tf: str = "1D") -> int:
        """删除指定 symbols 的 bar 数据。"""
        return self._delete_bars(bar_time, tf=tf, symbols=symbols)

    def _get_synced_symbols(self, bar_time: datetime, tf: str = "1D") -> set[str]:
        """查询指定 bar_time 已写入的 symbols 集合。"""
        table = self._kline_table(bar_time, tf=tf)
        naive_lbt = bar_time.replace(tzinfo=None) if bar_time.tzinfo else bar_time
        try:
            mgr = get_market_db_manager()
            pool = mgr._get_pool(self.source.db_pool)
            with pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(f"SELECT DISTINCT symbol FROM {table} WHERE time = %s", (naive_lbt,))
                    return {row[0] for row in cur.fetchall()}
        except Exception as e:
            logger.warning(f"[同步] {self.source.name} {tf} 查询已同步 symbols 失败: {e}")
            return set()


# ================================================================
# 预定义数据源实例
# ================================================================

stock_daily_k = BackfillDB(BackfillSource(
    name="stock_daily_k", market="CNStock", timeframe="15m",
))


# ================================================================
# 统一调度器 — threading.Timer 自调度，15m / 1D 各自独立
# ================================================================

_timers: dict[str, threading.Timer] = {}
_running = False
_repair_attempt: dict[str, int] = {}  # task → 当前修复轮次（跨 _run_repair 调用持久化）


def _next_trigger_time(task: str) -> datetime:
    """返回指定任务的下次触发时间，跳过非交易日。"""
    now = datetime.now(TZ_CN)
    today_str = now.strftime("%Y-%m-%d")
    trigger_h, trigger_m = _TF_CONFIG[task]["cutoff"]
    if is_trading_day(today_str) and now.time() < dt_time(trigger_h, trigger_m):
        return datetime(now.year, now.month, now.day, trigger_h, trigger_m, 0, tzinfo=TZ_CN)
    next_td = next_trading_day(today_str)
    dt_obj = datetime.strptime(next_td, "%Y-%m-%d")
    return datetime(dt_obj.year, dt_obj.month, dt_obj.day, trigger_h, trigger_m, 0, tzinfo=TZ_CN)


def _check_and_set_completion(task: str, synced: int, current_failed: int,
                               lbt: datetime | None, pool_name: str = "CNStock") -> bool:
    """检查完成度，设置 status，返回 True 表示应正常退出。"""
    sync_rate = (synced - current_failed) / synced if synced > 0 else 0
    if sync_rate > 0.9:
        logger.info(f"[调度] {task} 完成度 {sync_rate:.0%} > 90%, status=ok, 正常退出")
        if lbt:
            _update_record(task, lbt, status="ok", pool_name=pool_name)
        return True
    logger.info(f"[调度] {task} 完成度 {sync_rate:.0%} <= 90%, status=error, 正常退出")
    if lbt:
        _update_record(task, lbt, status="error", pool_name=pool_name)
    return True


def _merge_failed_report(task: str, doc: dict, failed_symbols: list[str],
                         saved_synced_count: int) -> tuple[list[str], int, int]:
    """合并本次修复结果与之前已知失败列表。"""
    lbt = _parse_db_timestamp(doc.get("last_bar_time"))
    new_failed_set = set(_extract_failed_symbols_from_report(doc.get("report", "") or ""))

    # 读取 kline 表确认实际已写入
    final_synced: set[str] = set()
    if lbt:
        try:
            table = f'"kline_{task}_{lbt.year}"'
            naive_lbt = lbt.replace(tzinfo=None) if lbt.tzinfo else lbt
            mgr = get_market_db_manager()
            pool = mgr._get_pool("CNStock")
            with pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(f"SELECT DISTINCT symbol FROM {table} WHERE time = %s", (naive_lbt,))
                    final_synced = {row[0] for row in cur.fetchall()}
        except Exception:
            pass

    from app.data_sources.normalizer import strip_market_prefix
    for prev_sym in failed_symbols:
        clean = strip_market_prefix(prev_sym)
        if clean not in new_failed_set and clean not in final_synced:
            new_failed_set.add(clean)

    remaining_failed = sorted(new_failed_set)
    current_failed = len(remaining_failed)
    current_written = len(final_synced)

    if lbt and remaining_failed:
        merged_report = f"已同步 {current_written}/{saved_synced_count}; 失败标的({len(remaining_failed)}): {','.join(remaining_failed[:50])}"
        _update_record(task, lbt, report=merged_report,
                      failed_count=current_failed, written_count=current_written, pool_name="CNStock")
    elif lbt and not remaining_failed:
        current_failed = 0

    return remaining_failed, current_failed, current_written


def _run_task(task: str):
    """执行一次同步，按调度协议决定后续动作。"""
    global _running
    if not _running:
        return

    try:
        now = datetime.now(TZ_CN)
        today_str = now.strftime("%Y-%m-%d")
        if is_trading_day(today_str) and dt_time(9, 15) <= now.time() <= dt_time(15, 0, 59):
            logger.info(f"[调度] {task} 盘中，正常退出")
            _schedule_next(task, _next_trigger_time(task))
            return

        doc, last_status, *_ = _read_sync_progress(task)
        last_bar_time = _parse_db_timestamp(doc.get("last_bar_time")) if doc else None

        if not last_bar_time:
            _run_fresh_pull(task, last_status)
            return

        is_update = _compute_is_update(task, last_bar_time)

        if is_update:
            _run_fresh_pull(task, last_status)
        elif last_status == "re":
            _run_repair(task)
        elif last_status == "error":
            _repair_attempt.pop(task, None)
            logger.info(f"[调度] {task} status=error (终态), 正常退出")
            _schedule_next(task, _next_trigger_time(task))
        else:
            _repair_attempt.pop(task, None)
            logger.info(f"[调度] {task} is_update={is_update}, status={last_status}, 正常退出")
            _schedule_next(task, _next_trigger_time(task))

    except Exception as e:
        logger.error(f"[调度] {task} 异常: {e}", exc_info=True)
        _schedule_next(task, delay_seconds=_RETRY_INTERVAL)


def _run_fresh_pull(task: str, last_status: str):
    """全新拉取: ok → 正常退出; re → 等待120s后进入修复。"""
    _repair_attempt[task] = 0  # 重置修复计数器
    logger.info(f"[调度] {task} 全新拉取 (上次状态={last_status or '无记录'})")
    # skip_repair=True: 全新拉取不做内部修复，由调度器 _run_repair 统一控制修复循环
    result = stock_daily_k.run_once(task, skip_repair=True)
    logger.info(f"[调度] {task} 本轮写入: {result.get('written', 0)}")

    doc, status, synced, failed, written, sync_rate = _read_sync_progress(task)
    if not doc:
        _schedule_next(task, _next_trigger_time(task))
        return

    logger.info(f"[调度] {task} 进度: 写入{written}/同步{synced} ({sync_rate:.0%}), 失败 {failed}, status={status}")

    if status == "ok":
        logger.info(f"[调度] {task} 全新拉取完成 (status=ok), 正常退出")
        _schedule_next(task, _next_trigger_time(task))
    else:
        logger.info(f"[调度] {task} 全新拉取未全部完成 (status={status}), {_RETRY_INTERVAL}s 后进入修复")
        _schedule_next(task, delay_seconds=_RETRY_INTERVAL)


def _run_repair(task: str):
    """report 修复流程（调度器控制循环，每次调用执行一轮）。

    设计说明:
      每次调用执行一轮修复，通过 _repair_attempt 全局计数器跨调用持久化轮次。
      循环<10次:
        全部完成 → status=ok; 正常退出
        部分完成(本次修复>0) → 删除report已写入的代码 → 等待120s
        未完成(本次修复=0) → 完成度>90% → status=ok; 否则 → status=error
      循环用尽 → 完成度>90% → status=ok; 否则 → status=error
    """
    doc, last_status, synced_total, failed_count, *_ = _read_sync_progress(task)
    if not doc:
        _schedule_next(task, _next_trigger_time(task))
        return

    if last_status == "ok":
        # 修复已全部完成（可能在上一轮 _sync 中全部成功），正常退出
        logger.info(f"[调度] {task} 修复: status=ok, 正常退出")
        _repair_attempt.pop(task, None)
        _schedule_next(task, _next_trigger_time(task))
        return

    if last_status == "error":
        # error 是终态，正常退出
        logger.info(f"[调度] {task} 修复: status=error (终态), 正常退出")
        _repair_attempt.pop(task, None)
        _schedule_next(task, _next_trigger_time(task))
        return

    if last_status != "re":
        # 未知状态 → 兜底全新拉取
        logger.info(f"[调度] {task} 上次 status={last_status or '无记录'}, 尝试全新拉取")
        _run_fresh_pull(task, last_status)
        return

    if failed_count == 0 and synced_total > 0:
        logger.info(f"[调度] {task} 修复: 已全部完成, 正常退出")
        _repair_attempt.pop(task, None)
        _schedule_next(task, _next_trigger_time(task))
        return

    # 从 report 提取失败标的
    failed_symbols = _extract_failed_symbols_from_report(doc.get("report", "") or "")
    if not failed_symbols:
        lbt = _parse_db_timestamp(doc.get("last_bar_time"))
        _check_and_set_completion(task, synced_total, failed_count, lbt)
        _repair_attempt.pop(task, None)
        _schedule_next(task, _next_trigger_time(task))
        return

    # 获取当前轮次（跨调用持久化）
    attempt = _repair_attempt.get(task, 0) + 1
    _repair_attempt[task] = attempt

    # 超过最大轮次 → 完成度判断退出
    if attempt > _MAX_REPAIR_ATTEMPTS:
        logger.info(f"[调度] {task} 循环 {_MAX_REPAIR_ATTEMPTS} 次修复用尽")
        doc_final, _, synced_f, failed_f, *_ = _read_sync_progress(task)
        lbt = _parse_db_timestamp(doc_final.get("last_bar_time")) if doc_final else None
        _check_and_set_completion(task, synced_f or synced_total, failed_f, lbt)
        _repair_attempt.pop(task, None)
        _schedule_next(task, _next_trigger_time(task))
        return

    logger.info(f"[调度] {task} 修复第 {attempt}/{_MAX_REPAIR_ATTEMPTS} 轮, 重拉 {len(failed_symbols)} 只失败标的")

    stock_daily_k.run_once(task, symbols=failed_symbols, skip_repair=True, force_refetch=True)

    # 重新读取修复后的状态
    doc = _get_last_update(task, pool_name="CNStock")
    if not doc:
        _repair_attempt.pop(task, None)
        _schedule_next(task, _next_trigger_time(task))
        return

    remaining_failed, current_failed, current_written = _merge_failed_report(
        task, doc, failed_symbols, synced_total)

    synced = synced_total
    lbt = _parse_db_timestamp(doc.get("last_bar_time"))

    logger.info(f"[调度] {task} 修复第 {attempt} 轮后: 写入{current_written}/同步{synced}, 失败 {current_failed}")

    # 全部完成 → status=ok; 正常退出
    if current_failed == 0 and synced > 0:
        logger.info(f"[调度] {task} 修复第 {attempt} 轮全部完成, status=ok, 正常退出")
        if lbt:
            _update_record(task, lbt, status="ok", pool_name="CNStock")
        _repair_attempt.pop(task, None)
        _schedule_next(task, _next_trigger_time(task))
        return

    # 部分完成(本次修复>0) → 等待120s → 继续下一轮
    if remaining_failed and len(remaining_failed) < len(failed_symbols):
        logger.info(f"[调度] {task} 修复第 {attempt} 轮部分完成: {len(failed_symbols)}→{len(remaining_failed)}, {_RETRY_INTERVAL}s 后重试")
        if lbt:
            _update_record(task, lbt, status="re",
                          report=f"修复中: 写入{current_written}/同步{synced}, 失败 {current_failed}, 剩余失败标的({len(remaining_failed)}): {','.join(remaining_failed[:50])}",
                          synced_count=synced, failed_count=current_failed,
                          written_count=current_written, pool_name="CNStock")
        _schedule_next(task, delay_seconds=_RETRY_INTERVAL)
        return

    # 未完成(本次修复=0) → 完成度判断
    logger.info(f"[调度] {task} 修复第 {attempt} 轮无进展 (本次修复=0)")
    sync_rate = (synced - current_failed) / synced if synced > 0 else 0
    if sync_rate > 0.9:
        logger.info(f"[调度] {task} 完成度 {sync_rate:.0%} > 90%, status=ok, 正常退出")
        if lbt:
            _update_record(task, lbt, status="ok",
                          report=f"完成度 {sync_rate:.0%} > 90%, 无需继续修复",
                          synced_count=synced, failed_count=current_failed,
                          written_count=current_written, pool_name="CNStock")
    else:
        logger.info(f"[调度] {task} 完成度 {sync_rate:.0%} <= 90%, status=error, 正常退出")
        if lbt:
            _update_record(task, lbt, status="error",
                          report=f"修复无进展: 写入{current_written}/同步{synced}, 失败 {current_failed}",
                          synced_count=synced, failed_count=current_failed,
                          written_count=current_written, pool_name="CNStock")
    _repair_attempt.pop(task, None)
    _schedule_next(task, _next_trigger_time(task))


def _schedule_next(task: str, trigger_at: datetime = None, delay_seconds: float = None):
    """为指定任务安排下次执行。"""
    global _running
    if not _running:
        return

    old = _timers.pop(task, None)
    if old:
        old.cancel()

    if delay_seconds is not None:
        delay = max(_MIN_DELAY, delay_seconds)
    elif trigger_at is not None:
        delay = max(_MIN_DELAY, (trigger_at - datetime.now(TZ_CN)).total_seconds())
    else:
        delay = _INITIAL_DELAY

    timer = threading.Timer(delay, _run_task, args=[task])
    timer.daemon = True
    timer.name = f"backfill-{task}"
    timer.start()
    _timers[task] = timer

    run_at = datetime.now(TZ_CN) + timedelta(seconds=delay)
    logger.info(f"[调度] {task} 下次执行: {run_at:%Y-%m-%d %H:%M:%S} (延迟 {delay:.0f}s)")


def start_scheduler():
    """启动统一调度器（幂等，重复调用安全）。"""
    global _running
    if _running:
        return
    _running = True

    logger.info(f"[调度] 启动（15m@15:05 + 1D@17:00 + {_RETRY_INTERVAL}s 重试）")

    now = datetime.now(TZ_CN)
    today_str = now.strftime("%Y-%m-%d")
    if is_trading_day(today_str) and dt_time(9, 15) <= now.time() <= dt_time(15, 0, 59):
        logger.info("[调度] 启动检查: 盘中，正常退出")
        for task in ("15m", "1D"):
            _schedule_next(task, _next_trigger_time(task))
        return

    for task in ("15m", "1D"):
        doc = _get_last_update(task, pool_name="CNStock")
        last_bar_time = _parse_db_timestamp(doc.get("last_bar_time")) if doc else None
        last_status = doc.get("status", "") if doc else ""

        if not last_bar_time:
            logger.info(f"[调度] {task} 启动检查: 无记录，{_INITIAL_DELAY}s 后核心启动")
            _schedule_next(task, delay_seconds=_INITIAL_DELAY)
            continue

        is_update = _compute_is_update(task, last_bar_time)
        db_date = last_bar_time.astimezone(TZ_CN).strftime("%Y-%m-%d") if last_bar_time.tzinfo else last_bar_time.strftime("%Y-%m-%d")

        if is_update or last_status == "re":
            logger.info(f"[调度] {task} 启动检查: db最后时间={db_date}, is_update={is_update}, status={last_status}, {_INITIAL_DELAY}s 后核心启动")
            _schedule_next(task, delay_seconds=_INITIAL_DELAY)
        else:
            logger.info(f"[调度] {task} 启动检查: db最后时间={db_date}, is_update={is_update}, status={last_status}, 正常退出")
            _schedule_next(task, _next_trigger_time(task))


def stop_scheduler():
    """停止调度器，取消所有待执行 timer。"""
    global _running
    _running = False
    for timer in _timers.values():
        timer.cancel()
    _timers.clear()
    logger.info("[调度] 调度器已停止")

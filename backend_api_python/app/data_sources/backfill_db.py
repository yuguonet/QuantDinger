"""
backfill_db.py — A 股 K 线增量同步 + 后台调度

═══════════════════════════════════════════════════════════════
  架构位置: backfill_db → provider(15m直调) / coordinator(1D)
═══════════════════════════════════════════════════════════════

核心职责:
  1. 盘中增量同步当日 15m bar（精确到具体 bar 时间点）
  2. 17:00 后同步当日 1D bar
  3. 首次运行时做历史回填
  4. 后台自动调度，不影响主线程

数据流:
  15m → get_providers("batch_quote") → _batch_fetch_quotes_by_codes(500/组)
        → 多 provider 并发 → 合并去重 → bulk_write
  1D  → coordinator.coordinate_batch_quotes() → 重试+去重 → bulk_write
  ↓
  db_market.upsert() → PostgreSQL
  ↓
  cn_last_update 记录同步状态

设计原则:
  1. cn_last_update 是唯一的调度控制表
  2. 15m 在 bar 结束前 30s 触发下载（如 9:44:30, 9:59:30 ...）
  3. 1D 每个交易日 17:00 后同步一次，<90% 则重试（最多 5 次，两次比对去重）
  4. 非交易日不执行
  5. 后台 daemon 线程自动运行，fire-and-forget
  6. 所有数据源走内联 provider，不依赖外部 API
"""

import logging
import threading
import time
from datetime import datetime, timedelta, timezone, time as dt_time

from app.utils.db_market import get_market_db_manager, get_market_kline_writer
from app.utils.trading_calendar import is_trading_day, prev_trading_day, next_trading_day
from app.utils.logger import get_logger

logger = get_logger(__name__)

TZ_CN = timezone(timedelta(hours=8))

# ── 功能开关 ──────────────────────────────────────────────
# 设为 False 即关闭对应周期的下载-保存全流程（仅本文件内生效）
ENABLE_15M = True   # 15 分钟线开关
ENABLE_1D  = True   # 日线开关
# ──────────────────────────────────────────────────────────

# 15m 下载超时（秒）
_BATCH_TIMEOUT_15M = 300

# 1D 下载超时（秒）
_BATCH_TIMEOUT_1D = 300

# 1D 无需内部重试 — 调度器 17:00 后每轮自动重试
# 增量同步: 首次全量拉取+写入，后续只补拉缺失 symbols


# ================================================================
# cn_last_update 表 — 同步的唯一控制机制（PostgreSQL）
# ================================================================

_ensure_table_lock = threading.Lock()
_tables_ensured: set[str] = set()


def _ensure_cn_last_update_table(pool_name: str = "CNStock"):
    """确保 cn_last_update 表存在。"""
    if pool_name in _tables_ensured:
        return
    with _ensure_table_lock:
        if pool_name in _tables_ensured:
            return
        try:
            mgr = get_market_db_manager()
            pool = mgr._get_pool(pool_name)
            with pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS cn_last_update (
                            id VARCHAR(64) PRIMARY KEY,
                            tf VARCHAR(10) NOT NULL,
                            last_updated TIMESTAMP NOT NULL,
                            last_bar_time TIMESTAMP,
                            status VARCHAR(20) DEFAULT 'ok',
                            report TEXT,
                            failed_count INT DEFAULT 0,
                            synced_count INT DEFAULT 0
                        )
                    """)
                    # 兼容旧表: 加缺失列（如果不存在）
                    for col, col_def in [
                        ("last_bar_time", "TIMESTAMP"),
                        ("failed_count", "INT DEFAULT 0"),
                        ("synced_count", "INT DEFAULT 0"),
                    ]:
                        cur.execute(f"""
                            DO $$
                            BEGIN
                                IF NOT EXISTS (
                                    SELECT 1 FROM information_schema.columns
                                    WHERE table_name = 'cn_last_update'
                                      AND column_name = '{col}'
                                ) THEN
                                    ALTER TABLE cn_last_update
                                        ADD COLUMN {col} {col_def};
                                END IF;
                            END $$;
                        """)
                    conn.commit()
            _tables_ensured.add(pool_name)
        except Exception as e:
            logger.error(f"[同步] 创建 cn_last_update 表失败: {e}")


def _get_last_update(source_name: str, tf: str, pool_name: str = "CNStock") -> dict | None:
    """查询 cn_last_update 记录。"""
    _ensure_cn_last_update_table(pool_name)
    try:
        mgr = get_market_db_manager()
        pool = mgr._get_pool(pool_name)
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT last_updated, last_bar_time, status, report, failed_count, synced_count "
                    "FROM cn_last_update WHERE id = %s",
                    (f"{source_name}_{tf}",),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return {
                    "last_updated": row[0],
                    "last_bar_time": row[1],
                    "status": row[2],
                    "report": row[3],
                    "failed_count": row[4] or 0,
                    "synced_count": row[5] or 0,
                }
    except Exception as e:
        logger.error(f"[同步] 查询 cn_last_update 失败: {e}")
        return None


def _record_update(source_name: str, tf: str, status: str, report: str,
                   last_bar_time: datetime | None = None,
                   synced_count: int | None = None,
                   failed_count: int | None = None,
                   pool_name: str = "CNStock"):
    """写入同步记录到 cn_last_update。synced_count/failed_count 用于增量同步进度追踪。"""
    _ensure_cn_last_update_table(pool_name)
    try:
        mgr = get_market_db_manager()
        pool = mgr._get_pool(pool_name)
        with pool.connection() as conn:
            with conn.cursor() as cur:
                # 使用北京时间存储，避免 PG 服务器时区不一致导致的比较错误
                cur.execute("""
                    INSERT INTO cn_last_update
                        (id, tf, last_updated, last_bar_time, status, report,
                         synced_count, failed_count)
                    VALUES (%s, %s, NOW() AT TIME ZONE 'Asia/Shanghai', %s, %s, %s,
                            %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        last_updated  = NOW() AT TIME ZONE 'Asia/Shanghai',
                        last_bar_time = COALESCE(EXCLUDED.last_bar_time, cn_last_update.last_bar_time),
                        status        = EXCLUDED.status,
                        report        = EXCLUDED.report,
                        synced_count  = COALESCE(EXCLUDED.synced_count, cn_last_update.synced_count),
                        failed_count  = COALESCE(EXCLUDED.failed_count, cn_last_update.failed_count)
                """, (f"{source_name}_{tf}", tf, last_bar_time, status, report,
                      synced_count, failed_count))
                conn.commit()
    except Exception as e:
        logger.error(f"[同步] 写入 cn_last_update 失败: {e}")


# ================================================================
# 15m bar 时间表 — 固定时间点 + 提前 30s 触发
# ================================================================
#
# A 股交易时段: 9:30-11:30, 13:00-15:00
# 15m bar 的结束时间（北京时间）:
#   09:45, 10:00, 10:15, 10:30,
#   10:45, 11:00, 11:15, 11:30,
#   13:15, 13:30, 13:45, 14:00,
#   14:15, 14:30, 14:45, 15:00
#
# 触发时间 = bar 结束前 30s:
#   9:44:30, 9:59:30, 10:14:30, 10:29:30,
#   10:44:30, 10:59:30, 11:14:30, 11:29:30,
#   13:14:30, 13:29:30, 13:44:30, 13:59:30,
#   14:14:30, 14:29:30, 14:44:30, 14:59:30
#

# 15m bar 结束时间（时, 分）— 北京时间
_BAR_END_TIMES = [
    (9, 45),  (10, 0),  (10, 15), (10, 30),
    (10, 45), (11, 0),  (11, 15), (11, 30),
    (13, 15), (13, 30), (13, 45), (14, 0),
    (14, 15), (14, 30), (14, 45), (15, 0),
]


def _latest_finished_bar_time() -> datetime | None:
    """返回当前时间之前最近一根已结束的 15m bar 的标准结束时间。

    不用 delay，只看 bar 结束时间是否已过。
    """
    now = datetime.now(TZ_CN)
    latest = None
    for h, m in _BAR_END_TIMES:
        bar_end = datetime(now.year, now.month, now.day, h, m, 0, tzinfo=TZ_CN)
        if bar_end <= now:
            latest = bar_end
        else:
            break
    return latest


def _normalize_to_bar_time(dt_obj: datetime) -> datetime:
    """将任意时间标准化到其所属 15m bar 的标准结束时间。

    例: 10:05:00 → 属于 10:00~10:15 这根 bar → 返回 10:15
        09:29:00 → 盘前，不属于任何 bar → 返回 None（由调用方处理）
        12:00:00 → 午休，不属于任何 bar → 返回 None
    """
    t = dt_obj.astimezone(TZ_CN) if dt_obj.tzinfo else dt_obj.replace(tzinfo=TZ_CN)
    t_time = t.time()

    # 盘前 (09:30 前) → 不属于任何 bar
    if t_time < dt_time(9, 30):
        return None

    # 午休 (11:30 ~ 13:00) → 不属于任何 bar
    if dt_time(11, 30) < t_time < dt_time(13, 0):
        return None

    # 从 _BAR_END_TIMES 中找到第一个结束时间 >= t_time 的 bar
    for h, m in _BAR_END_TIMES:
        bar_end_time = dt_time(h, m)
        if t_time <= bar_end_time:
            return datetime(t.year, t.month, t.day, h, m, 0, tzinfo=TZ_CN)

    # 超过 15:00 → 归到 15:00
    return datetime(t.year, t.month, t.day, 15, 0, 0, tzinfo=TZ_CN)


# ================================================================
# 判断逻辑
# ================================================================

def _parse_db_timestamp(ts) -> datetime | None:
    """将 DB 返回的时间戳统一为带 TZ_CN 的 datetime。

    处理三种情况:
    1. datetime with tzinfo → 直接转北京时间
    2. datetime naive → 视为北京时间（修复后 NOW() AT TIME ZONE 存储的就是 naive 北京时间）
    3. str → 按常见格式解析后视为北京时间
    """
    if ts is None:
        return None
    if isinstance(ts, datetime):
        if ts.tzinfo:
            return ts.astimezone(TZ_CN)
        return ts.replace(tzinfo=TZ_CN)
    if isinstance(ts, str):
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(ts, fmt).replace(tzinfo=TZ_CN)
            except ValueError:
                continue
        # ISO format fallback
        try:
            return datetime.fromisoformat(ts).replace(tzinfo=TZ_CN) if not ts.endswith("Z") else \
                   datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(TZ_CN)
        except ValueError:
            logger.warning(f"[同步] 无法解析时间戳: {ts}")
            return None
    return None


def _same_trading_day(dt1: datetime, dt2: datetime) -> bool:
    """判断两个时间点是否在同一个交易日。"""
    d1 = dt1.strftime("%Y-%m-%d")
    d2 = dt2.strftime("%Y-%m-%d")
    if d1 == d2:
        return True

    def _own_trading_day(d: str) -> str:
        return d if is_trading_day(d) else prev_trading_day(d)

    return _own_trading_day(d1) == _own_trading_day(d2)


# ================================================================
# 数据源配置
# ================================================================

class BackfillSource:
    """数据源配置。"""

    def __init__(self, name: str, market: str, timeframe: str,
                 db_pool: str = "CNStock"):
        self.name = name
        self.market = market
        self.timeframe = timeframe
        self.db_pool = db_pool


# ================================================================
# 同步执行器
# ================================================================

class BackfillDB:
    """全盘批量同步工具。

    A 股 15m: 直调 provider.fetch_batch_quotes（透传，无重试）
    A 股 1D: coordinator.coordinate_batch_quotes（含重试）
    基金/债: Dinger API
    """

    def __init__(self, source: BackfillSource):
        self.source = source
        self._writer = get_market_kline_writer()

    def run_once(self, tf: str | None = None, symbols: list | None = None) -> dict:
        """执行一次同步。tf 默认取 source.timeframe。"""
        tf = tf or self.source.timeframe

        pool = self.source.db_pool
        latest_bar = _latest_finished_bar_time()

        try:
            if tf == "15m":
                written, failed = self._sync_15m(symbols)
            elif tf == "1D":
                # 判断是否当天首次运行（用于决定是否清除当日旧 bar）
                doc = _get_last_update(self.source.name, "1D", pool_name=pool)
                is_first_run = True
                if doc:
                    lu = _parse_db_timestamp(doc.get("last_updated"))
                    if lu and _same_trading_day(lu, datetime.now(TZ_CN)):
                        is_first_run = False
                written, failed = self._sync_1d(symbols, is_first_run=is_first_run)
            else:
                return {
                    "source": self.source.name, "tf": tf,
                    "written": 0, "status": "ok", "report": f"不支持的周期: {tf}",
                }
        except Exception as e:
            report = f"同步异常: {e}"
            logger.error(f"[同步] {self.source.name} tf={tf} {report}")
            _record_update(self.source.name, tf, "error", report,
                           last_bar_time=latest_bar, pool_name=pool)
            return {
                "source": self.source.name, "tf": tf,
                "written": 0, "status": "error", "report": report,
            }

        if failed:
            logger.warning(
                f"[同步] {self.source.name} tf={tf} "
                f"{len(failed)} 只标的失败，下次同步自然重试"
            )

        # 1D: _sync_1d 内部已落盘 cn_last_update，直接读取返回
        if tf == "1D":
            doc = _get_last_update(self.source.name, "1D", pool_name=pool)
            status = doc.get("status", "ok") if doc else "ok"
            report = doc.get("report", f"写入 {written} 条") if doc else f"写入 {written} 条"
            return {
                "source": self.source.name, "tf": tf,
                "written": written, "status": status, "report": report,
            }

        if written == 0:
            report = "未获取到数据"
            logger.warning(f"[同步] {self.source.name} tf={tf} {report}")
            # 即使无数据也要记录，防止同一 bar 被重复触发
            _record_update(self.source.name, tf, "empty", report,
                           last_bar_time=latest_bar, pool_name=pool)
            return {
                "source": self.source.name, "tf": tf,
                "written": 0, "status": "empty", "report": report,
            }

        report = f"写入 {written} 条"
        _record_update(self.source.name, tf, "ok", report,
                       last_bar_time=latest_bar, pool_name=pool)
        logger.info(f"[同步] {self.source.name} tf={tf} {report}")

        return {
            "source": self.source.name, "tf": tf,
            "written": written, "status": "ok", "report": report,
        }

    # ── 15m 同步: 直调 provider.fetch_batch_quotes + 并发分组 + 合并去重 ──

    def _sync_15m(self, symbols: list | None = None) -> tuple[int, list[str]]:
        """15m 同步: 直接调用各 provider 的 fetch_batch_quotes，并发分组拉取，合并去重后写入 DB。

        不经过 coordinator，直接透传 provider。
        每个 provider 内部按 batch_size=500 分组并发（如新浪/腾讯每组500只，11组）。
        多个 provider 之间也并发执行。
        返回: (写入条数, 失败symbols列表)
        """
        import concurrent.futures
        from app.data_sources.normalizer import strip_market_prefix
        from app.data_sources.provider import get_providers, _batch_fetch_quotes_by_codes

        if not symbols:
            from app.utils.basicinfo_db import get_stock_basic_db
            symbols = get_stock_basic_db().market_all_codes(status="active")
        if not symbols:
            logger.warning(f"[同步] {self.source.name} 获取股票列表失败")
            return 0, []

        # 获取所有支持 batch_quote 的 provider
        providers = get_providers(capability="batch_quote", market=self.source.market)
        if not providers:
            logger.warning(f"[同步] {self.source.name} 无可用 batch_quote provider")
            return 0, list(symbols)

        provider_names = [getattr(p, "name", "?") for p in providers]
        logger.info(
            f"[同步] {self.source.name} tf=15m 直调 provider，"
            f"标的数={len(symbols)}，providers={provider_names}"
        )

        # 多 provider 并发: 每个 provider 在独立线程中分组拉取
        quotes: dict[str, dict] = {}
        quotes_lock = threading.Lock()

        def _fetch_provider(p):
            pname = getattr(p, "name", p.__class__.__name__)
            try:
                prepare_fn = getattr(p, 'prepare', None)
                if prepare_fn and not prepare_fn():
                    logger.warning(f"[同步] {pname} prepare() 返回 False，跳过")
                    return pname, {}
                result = _batch_fetch_quotes_by_codes(
                    p, batch_size=500,
                    timeout=int(_BATCH_TIMEOUT_15M),
                    symbols=list(symbols),
                )
                return pname, result or {}
            except Exception as e:
                logger.warning(f"[同步] {pname} 拉取失败: {e}")
                return pname, {}

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(providers)) as pool:
            futures = {pool.submit(_fetch_provider, p): p for p in providers}
            for fut in concurrent.futures.as_completed(futures):
                pname, result = fut.result()
                if not result:
                    logger.info(f"[同步] {pname} 返回 0 条")
                    continue
                # 合并去重: 已有数据的 symbol 不覆盖（先到先得，priority 高的先完成不保证）
                with quotes_lock:
                    new_count = sum(1 for s in result if s not in quotes)
                    quotes.update(result)
                logger.info(f"[同步] {pname} 返回 {len(result)} 条，新增 {new_count}")

        if not quotes:
            logger.warning(f"[同步] {self.source.name} 所有 provider 均返回空数据")
            return 0, list(symbols)

        logger.info(f"[同步] {self.source.name} 合并后共 {len(quotes)} 只标的")

        # 确定当前 bar 的标准结束时间
        now_cn = datetime.now(TZ_CN)
        bar_time = _normalize_to_bar_time(now_cn)
        if bar_time is None:
            logger.info(f"[同步] {self.source.name} 当前时间 {now_cn:%H:%M} 不在交易时段，跳过")
            return 0, []

        all_records = []
        failed_set = set()       # 用 set 去重，避免同一 symbol 被记多次
        success_digits = set()

        for symbol in symbols:
            quote = quotes.get(symbol)
            if not quote:
                digits = strip_market_prefix(symbol)
                quote = quotes.get(digits)

            if not quote:
                failed_set.add(symbol)
                continue

            o = float(quote.get("open", 0) or 0)
            h = float(quote.get("high", 0) or 0)
            l = float(quote.get("low", 0) or 0)
            c = float(quote.get("last", 0) or quote.get("close", 0) or 0)
            v = float(quote.get("volume", 0) or 0)

            if o == 0 and h == 0 and l == 0 and c == 0:
                failed_set.add(symbol)
                continue
            if c <= 0 or o <= 0:
                failed_set.add(symbol)
                continue
            if h > 0 and l > 0 and h < l:
                h, l = l, h

            clean_symbol = strip_market_prefix(symbol)
            all_records.append({
                "symbol": clean_symbol,
                "timeframe": "15m",
                "time": bar_time,
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": v,
            })
            success_digits.add(clean_symbol)

        # 统计缺失（quotes 里没有、且未成功处理的）
        requested_digits = set(strip_market_prefix(s) for s in symbols)
        fetched_set = set(quotes.keys())
        missing_digits = requested_digits - fetched_set - success_digits
        digit_to_symbol = {strip_market_prefix(s): s for s in symbols}
        for d in missing_digits:
            if d in digit_to_symbol:
                failed_set.add(digit_to_symbol[d])

        failed_symbols = list(failed_set)

        if failed_symbols:
            logger.warning(
                f"[同步] {self.source.name} 15m "
                f"{len(failed_symbols)}/{len(symbols)} 只标的失败"
            )

        if not all_records:
            return 0, failed_symbols

        try:
            r = self._writer.bulk_write(self.source.market, all_records)
            total = r.get("inserted", 0) + r.get("skipped", 0)
            logger.info(f"[同步] {self.source.name} 15m 批量写入 {total} 条")
            return total, failed_symbols
        except Exception as e:
            logger.error(f"[同步] {self.source.name} 15m 批量写入失败: {e}")
            return 0, list(symbols)

    # ── 1D 同步: batch_quotes + 重试 + 去重 ──────────────────

    def _sync_1d(self, symbols: list | None = None, is_first_run: bool = True) -> tuple[int, list[str]]:
        """1D 同步: 循环增量 batch_quotes，直到全部同步或 0 新增为止。

        - 首次运行 (is_first_run=True):  删除当日 bar → 全量拉取 → 写入
        - 后续重试 (is_first_run=False): 跳过已写入 symbols → 只补拉缺失部分
        - 每轮写入后立即落盘 cn_last_update，重启可续
        - 某轮 0 新增 → 说明剩余 symbols 已失效，停止

        返回: (总写入条数, 失败symbols列表)
        """
        from app.data_sources.coordinator import get_coordinator
        from app.data_sources.normalizer import strip_market_prefix

        if not symbols:
            from app.utils.basicinfo_db import get_stock_basic_db
            symbols = get_stock_basic_db().market_all_codes(status="active")
        if not symbols:
            logger.warning(f"[同步] {self.source.name} 获取股票列表失败")
            return 0, []

        coord = get_coordinator()
        total_symbols = len(symbols)

        # 日线 bar 时间: 目标交易日 17:00:00 (北京时间)
        # 17:00 后 → 今天, 08:00 前 → 上一个交易日
        now_cn = datetime.now(TZ_CN)
        today_str = now_cn.strftime("%Y-%m-%d")
        if now_cn.time() >= dt_time(17, 0) and is_trading_day(today_str):
            target_td = today_str
        else:
            target_td = prev_trading_day(today_str)
        bar_time = datetime.strptime(target_td, "%Y-%m-%d").replace(
            hour=17, minute=0, second=0, tzinfo=TZ_CN
        )

        pool = self.source.db_pool

        # ── 首次运行: 清除当日 bar，防止盘中数据污染 ──
        if is_first_run:
            deleted = self._delete_1d_bars(bar_time)
            if deleted > 0:
                logger.info(f"[同步] {self.source.name} 1D 首次运行，已清除当日 {deleted} 条旧 bar")

        # ── 循环拉取，直到全部同步或 0 新增 ──
        total_written = 0
        round_num = 0
        failed_reasons: dict[str, str] = {}  # symbol → 失败原因

        while True:
            round_num += 1

            # 查已同步 symbols，计算待拉取
            synced = self._get_synced_symbols(bar_time)
            remaining = [s for s in symbols if strip_market_prefix(s) not in synced]

            if not remaining:
                logger.info(f"[同步] {self.source.name} 1D 所有 {total_symbols} 只已同步")
                break

            logger.info(
                f"[同步] {self.source.name} 1D 第 {round_num} 轮，"
                f"已同步 {len(synced)}/{total_symbols}，待拉取 {len(remaining)}"
            )

            quotes = coord.coordinate_batch_quotes(
                symbols=remaining,
                market=self.source.market,
                timeout=float(_BATCH_TIMEOUT_1D),
            )

            if not quotes:
                logger.warning(
                    f"[同步] {self.source.name} 1D 第 {round_num} 轮返回空数据，停止"
                )
                for symbol in remaining:
                    failed_reasons.setdefault(symbol, "provider 返回空数据")
                break

            # List[Dict] → 按 symbol 索引（每条 quote 含 symbol 字段）
            quote_map = {q["symbol"]: q for q in quotes if q.get("symbol")}

            # 转换本轮结果
            records: list[dict] = []
            for symbol in remaining:
                quote = quote_map.get(symbol)
                if not quote:
                    quote = quote_map.get(strip_market_prefix(symbol))

                if not quote:
                    failed_reasons.setdefault(symbol, "无行情数据")
                    continue

                o = float(quote.get("open", 0) or 0)
                h = float(quote.get("high", 0) or 0)
                l = float(quote.get("low", 0) or 0)
                c = float(quote.get("last", 0) or quote.get("close", 0) or 0)
                v = float(quote.get("volume", 0) or 0)

                if o == 0 and h == 0 and l == 0 and c == 0:
                    failed_reasons.setdefault(symbol, "OHLCV 全零(停牌/退市)")
                    continue
                if c <= 0 or o <= 0:
                    failed_reasons.setdefault(symbol, f"价格异常(o={o},c={c})")
                    continue
                if h > 0 and l > 0 and h < l:
                    h, l = l, h

                records.append({
                    "symbol": strip_market_prefix(symbol),
                    "timeframe": "1D",
                    "time": bar_time,
                    "open": o,
                    "high": h,
                    "low": l,
                    "close": c,
                    "volume": v,
                })

            # 本轮 0 新增 → 剩余的拉不到了，停止
            if not records:
                logger.info(
                    f"[同步] {self.source.name} 1D 第 {round_num} 轮 0 新增，停止"
                )
                break

            # 写入
            try:
                r = self._writer.bulk_write(self.source.market, records)
                round_written = r.get("inserted", 0) + r.get("skipped", 0)
            except Exception as e:
                logger.error(f"[同步] {self.source.name} 1D 第 {round_num} 轮写入失败: {e}")
                break

            total_written += round_written
            synced_now = self._get_synced_symbols(bar_time)
            logger.info(
                f"[同步] {self.source.name} 1D 第 {round_num} 轮 "
                f"写入 {round_written}，累计 {len(synced_now)}/{total_symbols}"
            )

            # 全部完成
            if len(synced_now) >= total_symbols:
                break

        # ── 阶段一统计 ──
        final_synced = self._get_synced_symbols(bar_time)
        final_count = len(final_synced)
        failed = [s for s in symbols if strip_market_prefix(s) not in final_synced]

        # ── 阶段二: 失败修复 ──
        # synced + failed >= total 且 failed > 0 → 重拉失败标的
        if final_count < total_symbols and final_count + len(failed) >= total_symbols and len(failed) > 0:
            repair_round = 0

            while len(failed) > 0:
                repair_round += 1

                logger.info(
                    f"[同步] {self.source.name} 1D 修复第 {repair_round} 轮，"
                    f"重拉 {len(failed)} 只失败标的"
                )

                raw_quotes = coord.coordinate_batch_quotes(
                    symbols=failed,
                    market=self.source.market,
                    timeout=float(_BATCH_TIMEOUT_1D),
                )

                if not raw_quotes:
                    logger.info(f"[同步] {self.source.name} 1D 修复第 {repair_round} 轮返回空，停止")
                    break

                quote_map = {q["symbol"]: q for q in raw_quotes if q.get("symbol")}

                repair_records: list[dict] = []
                for symbol in failed:
                    quote = quote_map.get(symbol)
                    if not quote:
                        quote = quote_map.get(strip_market_prefix(symbol))
                    if not quote:
                        continue

                    o = float(quote.get("open", 0) or 0)
                    h = float(quote.get("high", 0) or 0)
                    l = float(quote.get("low", 0) or 0)
                    c = float(quote.get("last", 0) or quote.get("close", 0) or 0)
                    v = float(quote.get("volume", 0) or 0)

                    if o == 0 and h == 0 and l == 0 and c == 0:
                        continue
                    if c <= 0 or o <= 0:
                        continue
                    if h > 0 and l > 0 and h < l:
                        h, l = l, h

                    repair_records.append({
                        "symbol": strip_market_prefix(symbol),
                        "timeframe": "1D",
                        "time": bar_time,
                        "open": o,
                        "high": h,
                        "low": l,
                        "close": c,
                        "volume": v,
                    })

                # 拉不到有效数据 → 修不好了，不修了
                if not repair_records:
                    logger.info(f"[同步] {self.source.name} 1D 修复第 {repair_round} 轮 0 有效，停止")
                    break

                try:
                    r = self._writer.bulk_write(self.source.market, repair_records)
                    rw = r.get("inserted", 0) + r.get("skipped", 0)
                    total_written += rw
                except Exception as e:
                    logger.error(f"[同步] {self.source.name} 1D 修复第 {repair_round} 轮写入失败: {e}")
                    break

                # 重新统计
                final_synced = self._get_synced_symbols(bar_time)
                final_count = len(final_synced)
                failed = [s for s in symbols if strip_market_prefix(s) not in final_synced]

                logger.info(
                    f"[同步] {self.source.name} 1D 修复第 {repair_round} 轮 "
                    f"写入 {rw}，累计 {final_count}/{total_symbols}，剩余失败 {len(failed)}"
                )

                # 写一轮落一次盘，重启可续
                _record_update(
                    self.source.name, "1D", "partial",
                    f"修复中: {final_count}/{total_symbols}, 失败 {len(failed)}",
                    last_bar_time=bar_time, synced_count=final_count,
                    failed_count=len(failed), pool_name=pool,
                )

        # ── 最终落盘 cn_last_update ──
        report_parts = [f"已同步 {final_count}/{total_symbols}"]
        if failed:
            reason_counts: dict[str, int] = {}
            for sym in failed:
                reason = failed_reasons.get(sym, "未返回行情")
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
            reason_summary = ", ".join(f"{r}({c})" for r, c in sorted(reason_counts.items(), key=lambda x: -x[1]))
            report_parts.append(f"失败原因: {reason_summary}")
            sample = failed[:50]
            report_parts.append(f"失败标的({len(failed)}): {','.join(sample)}")

        report = "; ".join(report_parts)

        if final_count >= total_symbols or len(failed) == 0:
            _record_update(
                self.source.name, "1D", "ok", report,
                last_bar_time=bar_time, synced_count=final_count,
                failed_count=0, pool_name=pool,
            )
        else:
            # 修复后仍有失败 → 设 ok，失败明细保留在 report
            _record_update(
                self.source.name, "1D", "ok", report,
                last_bar_time=bar_time, synced_count=final_count,
                failed_count=len(failed), pool_name=pool,
            )

        if failed:
            logger.warning(
                f"[同步] {self.source.name} 1D 完成，"
                f"共 {round_num} 轮，写入 {total_written}，"
                f"同步 {final_count}/{total_symbols}，失败 {len(failed)}"
            )
            for sym in failed[:20]:
                reason = failed_reasons.get(sym, "未返回行情")
                logger.warning(f"  ✗ {sym}: {reason}")
            if len(failed) > 20:
                logger.warning(f"  ... 共 {len(failed)} 只失败，详见 cn_last_update.report")
        else:
            logger.info(
                f"[同步] {self.source.name} 1D 完成，"
                f"共 {round_num} 轮，写入 {total_written}，"
                f"同步 {final_count}/{total_symbols}，无失败"
            )

        return total_written, failed

    def _kline_table(self, bar_time: datetime) -> str:
        """返回 kline_1D_{年} 表名（加引号保留大小写）。"""
        return f'"kline_1D_{bar_time.year}"'

    def _delete_1d_bars(self, bar_time: datetime) -> int:
        """删除指定日期的 1D 数据，返回删除条数。按日期匹配，不依赖具体时间点。"""
        table = self._kline_table(bar_time)
        # 按日期匹配: 用 DATE() 比较，避免因时间部分不同导致漏删
        target_date = bar_time.strftime("%Y-%m-%d")
        try:
            mgr = get_market_db_manager()
            pool = mgr._get_pool(self.source.db_pool)
            with pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(f"""
                        DELETE FROM {table}
                        WHERE time::date = %s::date
                    """, (target_date,))
                    deleted = cur.rowcount
                    conn.commit()
                    return deleted
        except Exception as e:
            logger.warning(f"[同步] {self.source.name} 1D 清除旧 bar 失败 (可忽略): {e}")
            return 0

    def _get_synced_symbols(self, bar_time: datetime) -> set[str]:
        """查询指定 bar_time 已写入的 symbols 集合。"""
        table = self._kline_table(bar_time)
        # strip tzinfo: PG timestamp 列存储为 naive，aware datetime 匹配不到
        naive_bar_time = bar_time.replace(tzinfo=None) if bar_time.tzinfo else bar_time
        try:
            mgr = get_market_db_manager()
            pool = mgr._get_pool(self.source.db_pool)
            with pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(f"""
                        SELECT DISTINCT symbol FROM {table}
                        WHERE time = %s
                    """, (naive_bar_time,))
                    return {row[0] for row in cur.fetchall()}
        except Exception as e:
            logger.warning(f"[同步] {self.source.name} 1D 查询已同步 symbols 失败: {e}")
            return set()



# ================================================================
# 预定义数据源实例
# ================================================================

stock_daily_k = BackfillDB(BackfillSource(
    name="stock_daily_k", market="CNStock", timeframe="15m",
))


# ================================================================
# 统一调度器 — timer-based，同时管理 15m 和 1D 任务
# ================================================================
#
# 设计模式: 与 emotion_scheduler / sector_history 一致
#   - 单守护线程，sleep 到最近触发点 → 执行任务 → 计算下次触发 → 继续 sleep
#   - 15m: 盘中每 15 分钟触发一次（bar 结束前 30s），交易日共 16 次
#   - 1D:  每个交易日 17:05 触发一次（cn_last_update 内置重试/续传逻辑）
#   - 非交易日: 整天 sleep，不执行任何任务
#

_scheduler_started = False
_stop_event = threading.Event()

# 15m 提前预热秒数（cookie/服务器探测等耗时操作提前执行）
_PREPARE_LEAD_SECONDS = 60


def _pre_prepare_providers():
    """提前调用所有 provider 的 prepare()（cookie 刷新、服务器探测等）。"""
    try:
        from app.data_sources.provider import get_providers
        providers = get_providers(capability="batch_quote", market="CNStock")
        if not providers:
            return
        for p in providers:
            pname = getattr(p, "name", p.__class__.__name__)
            try:
                prepare_fn = getattr(p, 'prepare', None)
                if prepare_fn:
                    ok = prepare_fn()
                    logger.info(f"[15m预热] {pname} prepare() → {ok}")
            except Exception as e:
                logger.warning(f"[15m预热] {pname} prepare() 失败（忽略）: {e}")
    except Exception as e:
        logger.warning(f"[15m预热] 获取 provider 失败（忽略）: {e}")


def _next_bar_trigger_time() -> datetime:
    """返回下一个 15m bar 的触发时间（bar 结束前 30s）。"""
    now = datetime.now(TZ_CN)
    for h, m in _BAR_END_TIMES:
        bar_end = datetime(now.year, now.month, now.day, h, m, 0, tzinfo=TZ_CN)
        trigger = bar_end - timedelta(seconds=30)
        if trigger > now:
            return trigger
    # 今天的 bar 都过了 → 明天第一个 bar
    tomorrow = now + timedelta(days=1)
    h, m = _BAR_END_TIMES[0]
    return datetime(tomorrow.year, tomorrow.month, tomorrow.day, h, m, 0,
                    tzinfo=TZ_CN) - timedelta(seconds=30)


def _next_1d_trigger_time() -> datetime:
    """返回下一个 1D 任务的触发时间（17:05，跳过非交易日）。"""
    now = datetime.now(TZ_CN)
    today_str = now.strftime("%Y-%m-%d")

    # 今天是交易日且还没到 17:05 → 今天 17:05
    if is_trading_day(today_str) and now.time() < dt_time(17, 5):
        return datetime(now.year, now.month, now.day, 17, 5, 0, tzinfo=TZ_CN)

    # 找下一个交易日的 17:05
    from app.utils.trading_calendar import next_trading_day
    if now.time() >= dt_time(17, 5):
        next_td = next_trading_day(today_str)
    else:
        next_td = today_str if is_trading_day(today_str) else next_trading_day(today_str)

    dt_obj = datetime.strptime(next_td, "%Y-%m-%d")
    return datetime(dt_obj.year, dt_obj.month, dt_obj.day, 17, 5, 0, tzinfo=TZ_CN)


def _scheduler_loop():
    """统一调度主循环 — 一个线程管理 15m + 1D，sleep 到最近触发点执行。

    每轮:
      1. 非交易日 → sleep 到明天 09:00 重新判断
      2. 计算 15m 和 1D 各自的下次触发时间，取较近者
      3. sleep 到点 → 执行对应任务 → 计算新的触发时间 → 循环
    """
    # 确保 cn_last_update 表存在
    _ensure_cn_last_update_table()

    while not _stop_event.is_set():
        try:
            now = datetime.now(TZ_CN)
            today_str = now.strftime("%Y-%m-%d")

            # ── 非交易日: sleep 到明天 09:00 ──
            if not is_trading_day(today_str):
                tomorrow = now + timedelta(days=1)
                wake = datetime(tomorrow.year, tomorrow.month, tomorrow.day,
                                9, 0, 0, tzinfo=TZ_CN)
                sleep_sec = max((wake - now).total_seconds(), 60)
                logger.info(f"[调度] 非交易日，sleep {sleep_sec/3600:.1f}h 到明天 09:00")
                _stop_event.wait(timeout=sleep_sec)
                continue

            # ── 计算两个任务的下次触发时间 ──
            next_15m = _next_bar_trigger_time()
            next_1d = _next_1d_trigger_time()

            # 取较近者；15m 需要提前预热，所以用 (trigger - lead) 作为唤醒时间
            if next_15m <= next_1d:
                target_task = "15m"
                trigger_time = next_15m
                # 提前 _PREPARE_LEAD_SECONDS 唤醒做预热
                wake_time = trigger_time - timedelta(seconds=_PREPARE_LEAD_SECONDS)
                now = datetime.now(TZ_CN)
                if wake_time <= now:
                    # 已过了预热时间 → 直接触发
                    wake_time = trigger_time
            else:
                target_task = "1D"
                trigger_time = next_1d
                wake_time = trigger_time

            sleep_sec = max((wake_time - datetime.now(TZ_CN)).total_seconds(), 0)
            logger.info(
                f"[调度] 下次触发: {target_task} @ {trigger_time:%H:%M:%S}，"
                f"sleep {sleep_sec:.0f}s"
            )

            if _stop_event.wait(timeout=sleep_sec):
                break  # 收到停止信号

            # ── 执行对应任务 ──
            if target_task == "15m":
                # 两阶段: 如果提前醒来，先预热再等到点
                now = datetime.now(TZ_CN)
                if now < trigger_time:
                    logger.info("[调度] 15m 预热: cookie/服务器探测")
                    _pre_prepare_providers()
                    remaining = max((trigger_time - datetime.now(TZ_CN)).total_seconds(), 0)
                    if remaining > 0:
                        logger.info(f"[调度] 15m 预热完成，sleep {remaining:.0f}s 到正式触发")
                        if _stop_event.wait(timeout=remaining):
                            break

                logger.info("[调度] 15m 开始同步")
                result = stock_daily_k.run_once("15m")
                logger.info(
                    f"[调度] 15m 完成: written={result.get("written", 0)} "
                    f"status={result.get("status", "")}"
                )
            else:
                logger.info("[调度] 1D 开始同步")
                result = stock_daily_k.run_once("1D")
                logger.info(
                    f"[调度] 1D 完成: written={result.get("written", 0)} "
                    f"status={result.get("status", "")}"
                )

        except Exception as e:
            logger.error(f"[调度] 异常: {e}")
            _stop_event.wait(timeout=10)

    logger.info("[调度] 调度器已停止")


def start_scheduler():
    """启动统一调度器（幂等，重复调用安全）。"""
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True
    _stop_event.clear()
    t = threading.Thread(target=_scheduler_loop, daemon=True,
                         name="backfill-scheduler")
    t.start()
    logger.info("[调度] 统一调度器已启动（15m + 1D）")


def stop_scheduler():
    """停止调度器。"""
    _stop_event.set()
    logger.info("[调度] 调度器停止信号已发送")

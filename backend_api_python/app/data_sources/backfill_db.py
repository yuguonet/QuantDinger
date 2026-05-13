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


def _should_run_15m(pool_name: str = "CNStock") -> tuple[bool, str]:
    """15m 调度: 非交易日不跑 → 查表 → 判断是否有新 bar 触发。"""
    if not ENABLE_15M:
        return False, "15m 已关闭 (ENABLE_15M=False)"

    # 非交易日不跑
    today_str = datetime.now(TZ_CN).strftime("%Y-%m-%d")
    if not is_trading_day(today_str):
        return False, "非交易日"

    now = datetime.now(TZ_CN)
    doc = _get_last_update("stock_daily_k", "15m", pool_name=pool_name)

    # ── 首次同步 ──
    if not doc:
        latest_bar = _latest_finished_bar_time()
        if not latest_bar:
            return True, "首次同步，盘前执行历史回填"
        return True, "首次同步，无历史记录"

    # 15m 不重试: 无论成功/失败/空数据，只要本 bar 已尝试过就跳过
    last_bar = doc.get("last_bar_time")
    last_updated = doc.get("last_updated")

    if not last_bar and not last_updated:
        return True, "无时间记录，重新同步"

    ref_time = last_bar or last_updated
    ref_cn = _parse_db_timestamp(ref_time)
    if not ref_cn:
        return True, "时间戳无法解析，重新同步"

    # 跨交易日 → 直接同步
    if not _same_trading_day(ref_cn, now):
        return True, f"上次 {ref_cn:%Y-%m-%d}，跨交易日"

    # ── 看是否有新 bar 到了 ──
    latest_bar = _latest_finished_bar_time()

    if not latest_bar:
        # 盘前（9:45 前）→ 没有可同步的 bar
        if not last_bar:
            return True, "盘前，从未同步过，执行历史回填"
        return False, "盘前，无可同步 bar"

    # 精确比较: last_bar_time < 最新已结束 bar → 有新数据
    if ref_cn < latest_bar:
        return True, f"last_bar={ref_cn:%H:%M}, 新 bar 到 {latest_bar:%H:%M}"

    return False, f"已同步到 {ref_cn:%H:%M}，最新可用 {latest_bar:%H:%M}"


def _should_run_1d(pool_name: str = "CNStock") -> tuple[bool, str]:
    """1D 调度: 交易日 17:00 ~ 下一个交易日 08:00 可更新。"""
    if not ENABLE_1D:
        return False, "1D 已关闭 (ENABLE_1D=False)"

    now = datetime.now(TZ_CN)
    today_str = now.strftime("%Y-%m-%d")

    # 08:00-17:00 不更新
    if dt_time(8, 0) <= now.time() < dt_time(17, 0):
        return False, "不在 1D 更新窗口 (08:00-17:00)"

    # 确定目标交易日 + 窗口校验
    if now.time() >= dt_time(17, 0):
        # 17:00 后: 目标=今天, 今天必须是交易日
        if not is_trading_day(today_str):
            return False, "今天非交易日"
    else:
        # 08:00 前: 目标=上一个交易日, 必须在下一个交易日 08:00 之前
        prev_td = prev_trading_day(today_str)
        next_td = next_trading_day(prev_td)
        next_td_open = datetime.strptime(next_td, "%Y-%m-%d").replace(
            hour=8, minute=0, second=0, tzinfo=TZ_CN
        )
        if now >= next_td_open:
            return False, "已过下一个交易日 08:00，不在窗口内"

    doc = _get_last_update("stock_daily_k", "1D", pool_name=pool_name)

    # 首次同步
    if not doc:
        return True, "首次 1D 同步"

    status = doc.get("status", "")
    if status == "error":
        return True, f"上次 1D 失败: {doc.get('report', '')}，重试"

    last_updated = doc.get("last_updated")
    if last_updated:
        last_cn = _parse_db_timestamp(last_updated)
        if not last_cn:
            return True, "上次 1D 时间戳无法解析，重新同步"
        if not _same_trading_day(last_cn, now):
            return True, f"上次 1D {last_cn:%Y-%m-%d}，跨交易日"

        # 同一交易日内: 检查增量进度
        if status == "partial":
            synced = doc.get("synced_count", 0) or 0
            return True, f"本交易日 1D 部分完成 ({synced} 只)，继续补拉"

        if status == "ok":
            synced = doc.get("synced_count", 0) or 0
            from app.utils.basicinfo_db import get_stock_basic_db
            total = len(get_stock_basic_db().market_all_codes(status="active"))
            if synced < total:
                return True, f"本交易日 1D 已同步 {synced}/{total}，继续补拉"
            return False, "本交易日 1D 已全部同步"

        return False, "本交易日 1D 已同步"

    return True, "本交易日 1D 待同步"


# ================================================================
# 数据源配置
# ================================================================

class BackfillSource:
    """数据源配置。"""

    def __init__(self, name: str, market: str, timeframe: str,
                 dinger_url: str = "", db_pool: str = "CNStock"):
        self.name = name
        self.market = market
        self.timeframe = timeframe
        self.dinger_url = dinger_url
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

        # 查表: 该不该干
        pool = self.source.db_pool
        if tf == "15m":
            should, reason = _should_run_15m(pool_name=pool)
        elif tf == "1D":
            should, reason = _should_run_1d(pool_name=pool)
        else:
            should, reason = _should_run_generic(tf, pool_name=pool)

        if not should:
            return {
                "source": self.source.name, "tf": tf,
                "written": 0, "status": "ok", "report": reason,
            }

        # 执行同步
        latest_bar = _latest_finished_bar_time()

        try:
            if self.source.dinger_url:
                written, failed = self._sync_via_api(tf)
            elif tf == "15m":
                written, failed = self._sync_15m(symbols)
            elif tf == "1D":
                # 判断是否当天首次运行
                doc = _get_last_update(self.source.name, "1D", pool_name=pool)
                is_first_run = True
                if doc:
                    lu = _parse_db_timestamp(doc.get("last_updated"))
                    if lu and _same_trading_day(lu, datetime.now(TZ_CN)):
                        is_first_run = False
                written, failed = self._sync_1d(symbols, is_first_run=is_first_run)
            else:
                written, failed = 0, []
        except Exception as e:
            report = f"同步异常: {e}"
            logger.error(f"[同步] {self.source.name} tf={tf} {report}")
            # 异常也记录 bar 时间，防止 15m 同一 bar 重试
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
                break

            # 转换本轮结果
            records: list[dict] = []
            for symbol in remaining:
                quote = quotes.get(symbol)
                if not quote:
                    quote = quotes.get(strip_market_prefix(symbol))

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

        # 最终统计 + 落盘 cn_last_update
        final_synced = self._get_synced_symbols(bar_time)
        final_count = len(final_synced)
        failed = [s for s in symbols if strip_market_prefix(s) not in final_synced]

        # 构建失败报告: 原因分组统计 + 明细（最多 50 条）
        report_parts = [f"已同步 {final_count}/{total_symbols}"]
        if failed:
            # 按原因分组统计
            reason_counts: dict[str, int] = {}
            for sym in failed:
                reason = failed_reasons.get(sym, "未返回行情")
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
            reason_summary = ", ".join(f"{r}({c})" for r, c in sorted(reason_counts.items(), key=lambda x: -x[1]))
            report_parts.append(f"失败原因: {reason_summary}")
            # 明细: 最多 50 个 symbol
            sample = failed[:50]
            report_parts.append(f"失败标的({len(failed)}): {','.join(sample)}")

        report = "; ".join(report_parts)

        if final_count >= total_symbols:
            _record_update(
                self.source.name, "1D", "ok", report,
                last_bar_time=bar_time, synced_count=final_count,
                failed_count=len(failed), pool_name=pool,
            )
        else:
            _record_update(
                self.source.name, "1D", "partial", report,
                last_bar_time=bar_time, synced_count=final_count,
                failed_count=len(failed), pool_name=pool,
            )

        if failed:
            logger.warning(
                f"[同步] {self.source.name} 1D 完成，"
                f"共 {round_num} 轮，写入 {total_written}，"
                f"同步 {final_count}/{total_symbols}，失败 {len(failed)}"
            )
            # 打印前 20 个失败 symbol 及原因，便于快速排查
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
        """删除指定 bar_time 的 1D 数据，返回删除条数。"""
        table = self._kline_table(bar_time)
        # strip tzinfo: PG timestamp 列存储为 naive，aware datetime 匹配不到
        naive_bar_time = bar_time.replace(tzinfo=None) if bar_time.tzinfo else bar_time
        try:
            mgr = get_market_db_manager()
            pool = mgr._get_pool(self.source.db_pool)
            with pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(f"""
                        DELETE FROM {table}
                        WHERE time = %s
                    """, (naive_bar_time,))
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

    # ── Dinger API (基金/债) ────────────────────────────────

    def _sync_via_api(self, tf: str) -> tuple[int, list[str]]:
        """基金/债: 通过 Dinger API 拉取并写入。返回 (写入条数, 失败symbols列表)。"""
        import requests

        url = self.source.dinger_url.format(tf=tf, count=300)
        logger.info(f"[同步] {self.source.name} tf={tf} 请求 {url}")

        try:
            resp = requests.get(url, timeout=60)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error(f"[同步] {self.source.name} HTTP 请求失败: {e}")
            raise

        items = data.get("data", [])
        if not items or not isinstance(items, list):
            return 0, []

        ts_field = "navDate" if "fund" in self.source.name else "date"
        by_symbol: dict[str, list] = {}
        for item in items:
            sym = item.get("symbol", "")
            if not sym:
                continue
            by_symbol.setdefault(sym, []).append(item)

        all_records = []
        for symbol, records in by_symbol.items():
            for rec in records:
                ts_str = rec.get(ts_field)
                if not ts_str:
                    continue
                try:
                    ts = (
                        datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        if isinstance(ts_str, str) else ts_str
                    )
                except ValueError:
                    continue
                all_records.append({
                    "symbol": symbol,
                    "timeframe": tf,
                    "time": ts,
                    "open": float(rec.get("open", 0)),
                    "high": float(rec.get("high", 0)),
                    "low": float(rec.get("low", 0)),
                    "close": float(rec.get("close", 0)),
                    "volume": float(rec.get("volume", 0)),
                })

        if not all_records:
            return 0, []

        all_records = [
            r for r in all_records
            if r["open"] != 0 or r["high"] != 0 or r["low"] != 0 or r["close"] != 0
        ]
        if not all_records:
            return 0, []

        try:
            r = self._writer.bulk_write(self.source.market, all_records)
            total = r.get("inserted", 0) + r.get("skipped", 0)
            logger.info(f"[同步] {self.source.name} tf={tf} 批量写入 {total} 条")
            return total, []
        except Exception as e:
            logger.error(f"[同步] {self.source.name} 批量写入失败: {e}")
            return 0, []


# ================================================================
# 预定义数据源实例
# ================================================================

DINGER_BASE_URL = "https://api.quantdinger.com/v1"

stock_daily_k = BackfillDB(BackfillSource(
    name="stock_daily_k", market="CNStock", timeframe="15m",
))

fund_nav_daily = BackfillDB(BackfillSource(
    name="fund_nav_daily", market="CNStock", timeframe="1D",
    dinger_url=f"{DINGER_BASE_URL}/fund/nav_daily?tf={{tf}}&count={{count}}",
))

bond_daily_k = BackfillDB(BackfillSource(
    name="bond_daily_k", market="CNStock", timeframe="1D",
    dinger_url=f"{DINGER_BASE_URL}/bond/daily_k?tf={{tf}}&count={{count}}",
))


# ================================================================
# 通用调度 fallback
# ================================================================

def _should_run_generic(tf: str, pool_name: str = "CNStock") -> tuple[bool, str]:
    """非 15m/1D 的通用调度逻辑（基金/债等）。"""
    doc = _get_last_update("fund_nav_daily" if tf == "1D" else "unknown", tf,
                           pool_name=pool_name)
    if not doc:
        return True, "首次同步"
    status = doc.get("status", "")
    if status in ("error", "partial"):
        return True, "上次失败，重试"
    last = doc.get("last_updated")
    if last and not _same_trading_day(last, datetime.now(TZ_CN)):
        return True, "跨交易日"
    return False, "同交易日已同步"


# ================================================================
# 15m 自驱动调度器 — 独立线程，按 bar 时间点自动触发
# ================================================================
#
# 工作模式:
#   1. start_scheduler() 启动守护线程
#   2. 线程计算下一个 bar 触发时间（bar 结束前 30s），sleep 到点
#   3. 到点 → 执行一次 run_once("15m") → 计算下一个 bar → 继续 sleep
#   4. 完全自驱动，不依赖外部调用
#

_scheduler_started = False


def _next_bar_trigger() -> datetime:
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


def _scheduler_15m_loop():
    """15m 调度主循环 — 到点执行，永不退出。"""
    while True:
        try:
            # 非交易日 → sleep 到明天开盘前
            today_str = datetime.now(TZ_CN).strftime("%Y-%m-%d")
            if not is_trading_day(today_str):
                tomorrow = datetime.now(TZ_CN) + timedelta(days=1)
                h, m = _BAR_END_TIMES[0]
                wake = datetime(tomorrow.year, tomorrow.month, tomorrow.day,
                                h, m, 0, tzinfo=TZ_CN) - timedelta(seconds=30)
                sleep_sec = max((wake - datetime.now(TZ_CN)).total_seconds(), 60)
                logger.info(f"[15m调度] 非交易日，sleep {sleep_sec/3600:.1f}h 到明天")
                time.sleep(sleep_sec)
                continue

            trigger = _next_bar_trigger()
            sleep_sec = max((trigger - datetime.now(TZ_CN)).total_seconds(), 0)
            logger.info(f"[15m调度] 下次触发 {trigger:%H:%M:%S}，sleep {sleep_sec:.0f}s")
            time.sleep(sleep_sec)

            if not ENABLE_15M:
                continue

            logger.info("[15m调度] 到点，开始同步")
            result = stock_daily_k.run_once("15m")
            written = result.get("written", 0)
            status = result.get("status", "")
            logger.info(f"[15m调度] 完成: written={written} status={status}")

        except Exception as e:
            logger.error(f"[15m调度] 异常: {e}")
            time.sleep(10)


def start_scheduler():
    """启动 15m 自驱动调度器（幂等，重复调用安全）。"""
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True
    t = threading.Thread(target=_scheduler_15m_loop, daemon=True,
                         name="scheduler-15m")
    t.start()
    logger.info("[15m调度] 守护线程已启动")


# 模块加载时自动启动（幂等，重复 import 安全）
start_scheduler()


# ================================================================
# 1D + 基金/债 触发式同步
# ================================================================

_sync_running = threading.Lock()


def trigger_sync():
    """触发 1D / 基金 / 债同步（不含 15m）。"""
    if not _sync_running.acquire(blocking=False):
        return

    t = threading.Thread(target=_sync_worker, daemon=True, name="backfill-sync")
    t.start()


def _sync_worker():
    try:
        _run_all_sync()
    except Exception as e:
        logger.error(f"[后台同步] 异常: {e}")
    finally:
        _sync_running.release()


def _run_all_sync():
    """1D + 基金/债同步（15m 由独立调度器处理）。"""
    now = datetime.now(TZ_CN)
    today_str = now.strftime("%Y-%m-%d")

    if not is_trading_day(today_str):
        logger.info("[后台同步] 非交易日，跳过")
        return

    # ── 1D 同步 ──
    if not ENABLE_1D:
        logger.info("[后台同步] 1D 已关闭，跳过")
    else:
        try:
            result = stock_daily_k.run_once("1D")
            if result.get("written", 0) > 0:
                logger.info(f"[后台同步] 1D stock: {result.get('written')} 条")
        except Exception as e:
            logger.error(f"[后台同步] 1D stock 异常: {e}")

    # fund + bond — 17:00 后
    if now.time() >= dt_time(17, 0):
        for src in (fund_nav_daily, bond_daily_k):
            try:
                result = src.run_once("1D")
                if result.get("written", 0) > 0:
                    logger.info(f"[后台同步] 1D {src.source.name}: {result.get('written')} 条")
            except Exception as e:
                logger.error(f"[后台同步] 1D {src.source.name} 异常: {e}")


# ================================================================
# 全盘同步入口（保留兼容性）
# ================================================================

def run_once(tf: str | None = None, symbols: list | None = None) -> list[dict]:
    """全盘同步入口 — 三个数据源依次执行。"""
    if tf == "15m" and not ENABLE_15M:
        logger.info("[全盘同步] 15m 已关闭 (ENABLE_15M=False)，跳过")
        return [{"source": s.source.name, "tf": "15m", "written": 0,
                 "status": "ok", "report": "15m 已关闭"} for s in (stock_daily_k,)]
    if tf == "1D" and not ENABLE_1D:
        logger.info("[全盘同步] 1D 已关闭 (ENABLE_1D=False)，跳过")
        return [{"source": s.source.name, "tf": "1D", "written": 0,
                 "status": "ok", "report": "1D 已关闭"} for s in (stock_daily_k, fund_nav_daily, bond_daily_k)]

    results = []
    for source in (stock_daily_k, fund_nav_daily, bond_daily_k):
        try:
            r = source.run_once(tf, symbols=symbols)
            results.append(r)
        except Exception as e:
            logger.error(f"[全盘同步] {source.source.name} 异常: {e}")
            results.append({
                "source": source.source.name, "tf": tf or "?",
                "written": 0, "status": "error", "report": str(e),
            })

    ok = sum(1 for r in results if r["status"] == "ok")
    errors = sum(1 for r in results if r["status"] == "error")
    logger.info(f"[全盘同步] 完成: {ok} 成功, {errors} 失败")

    return results

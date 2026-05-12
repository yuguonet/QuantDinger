"""
backfill_db.py — A 股 K 线增量同步 + 后台调度

═══════════════════════════════════════════════════════════════
  架构位置: cn_stock → backfill_db → coordinator → 数据源 API
═══════════════════════════════════════════════════════════════

核心职责:
  1. 盘中增量同步当日 15m bar（精确到具体 bar 时间点）
  2. 17:00 后同步当日 1D bar
  3. 首次运行时做历史回填
  4. 后台自动调度，不影响主线程

数据流:
  15m → coordinator.coordinate_batch_quotes() → 标准化 time → bulk_write
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
from app.utils.trading_calendar import is_trading_day, prev_trading_day
from app.utils.logger import get_logger

logger = get_logger(__name__)

TZ_CN = timezone(timedelta(hours=8))

# ── 功能开关 ──────────────────────────────────────────────
# 设为 False 即关闭对应周期的下载-保存全流程（仅本文件内生效）
ENABLE_15M = False   # 15 分钟线开关
ENABLE_1D  = True   # 日线开关
# ──────────────────────────────────────────────────────────

# 15m 下载超时（秒）
_BATCH_TIMEOUT_15M = 300

# 1D 下载超时（秒）
_BATCH_TIMEOUT_1D = 300

# 1D 重试配置
_1D_MIN_RATIO = 0.9       # 90% 阈值
_1D_MAX_RETRIES = 5        # 最大重试次数


# ================================================================
# cn_last_update 表 — 同步的唯一控制机制（PostgreSQL）
# ================================================================

_ensure_table_lock = threading.Lock()
_table_ensured = False


def _ensure_cn_last_update_table():
    """确保 cn_last_update 表存在。"""
    global _table_ensured
    if _table_ensured:
        return
    with _ensure_table_lock:
        if _table_ensured:
            return
        try:
            mgr = get_market_db_manager()
            pool = mgr._get_pool("CNStock")
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
                            failed_count INT DEFAULT 0
                        )
                    """)
                    # 兼容旧表: 加缺失列（如果不存在）
                    for col, col_def in [
                        ("last_bar_time", "TIMESTAMP"),
                        ("failed_count", "INT DEFAULT 0"),
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
            _table_ensured = True
        except Exception as e:
            logger.error(f"[同步] 创建 cn_last_update 表失败: {e}")


def _get_last_update(source_name: str, tf: str) -> dict | None:
    """查询 cn_last_update 记录。"""
    _ensure_cn_last_update_table()
    try:
        mgr = get_market_db_manager()
        pool = mgr._get_pool("CNStock")
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT last_updated, last_bar_time, status, report, failed_count "
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
                }
    except Exception as e:
        logger.error(f"[同步] 查询 cn_last_update 失败: {e}")
        return None


def _record_update(source_name: str, tf: str, status: str, report: str,
                   last_bar_time: datetime | None = None):
    """写入同步记录到 cn_last_update。"""
    _ensure_cn_last_update_table()
    try:
        mgr = get_market_db_manager()
        pool = mgr._get_pool("CNStock")
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO cn_last_update
                        (id, tf, last_updated, last_bar_time, status, report)
                    VALUES (%s, %s, NOW(), %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        last_updated  = NOW(),
                        last_bar_time = COALESCE(EXCLUDED.last_bar_time, cn_last_update.last_bar_time),
                        status        = EXCLUDED.status,
                        report        = EXCLUDED.report
                """, (f"{source_name}_{tf}", tf, last_bar_time, status, report))
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

def _same_trading_day(dt1: datetime, dt2: datetime) -> bool:
    """判断两个时间点是否在同一个交易日。"""
    d1 = dt1.strftime("%Y-%m-%d")
    d2 = dt2.strftime("%Y-%m-%d")
    if d1 == d2:
        return True

    def _own_trading_day(d: str) -> str:
        return d if is_trading_day(d) else prev_trading_day(d)

    return _own_trading_day(d1) == _own_trading_day(d2)


def _should_run_15m() -> tuple[bool, str]:
    """15m 调度: 非交易日不跑 → 查表 → 判断是否有新 bar 触发。"""
    if not ENABLE_15M:
        return False, "15m 已关闭 (ENABLE_15M=False)"

    # 非交易日不跑
    today_str = datetime.now(TZ_CN).strftime("%Y-%m-%d")
    if not is_trading_day(today_str):
        return False, "非交易日"

    now = datetime.now(TZ_CN)
    doc = _get_last_update("stock_daily_k", "15m")

    # ── 首次同步 ──
    if not doc:
        latest_bar = _latest_finished_bar_time()
        if not latest_bar:
            return True, "首次同步，盘前执行历史回填"
        return True, "首次同步，无历史记录"

    status = doc.get("status", "")
    if status == "error":
        return True, f"上次失败: {doc.get('report', '')}，重试"

    last_bar = doc.get("last_bar_time")
    last_updated = doc.get("last_updated")

    if not last_bar and not last_updated:
        return True, "无时间记录，重新同步"

    ref_time = last_bar or last_updated
    ref_cn = ref_time.astimezone(TZ_CN) if ref_time.tzinfo else ref_time.replace(tzinfo=TZ_CN)

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


def _should_run_1d() -> tuple[bool, str]:
    """1D 调度: 非交易日不跑 → 查表 → 17:00 后判断。"""
    if not ENABLE_1D:
        return False, "1D 已关闭 (ENABLE_1D=False)"

    now = datetime.now(TZ_CN)
    today_str = now.strftime("%Y-%m-%d")

    # 非交易日不跑
    if not is_trading_day(today_str):
        return False, "非交易日"

    # 17:00 前不跑
    if now.time() < dt_time(17, 0):
        return False, "17:00 前不更新 1D"

    doc = _get_last_update("stock_daily_k", "1D")

    # 首次同步
    if not doc:
        return True, "首次 1D 同步"

    status = doc.get("status", "")
    if status == "error":
        return True, f"上次 1D 失败: {doc.get('report', '')}，重试"

    last_updated = doc.get("last_updated")
    if last_updated:
        last_cn = last_updated.astimezone(TZ_CN) if last_updated.tzinfo else last_updated.replace(tzinfo=TZ_CN)
        if not _same_trading_day(last_cn, now):
            return True, f"上次 1D {last_cn:%Y-%m-%d}，跨交易日"
        return False, "本交易日 1D 已同步"

    return True, "本交易日 1D 待同步"


# ================================================================
# 数据源配置
# ================================================================

class BackfillSource:
    """数据源配置。"""

    def __init__(self, name: str, market: str, timeframe: str, dinger_url: str = ""):
        self.name = name
        self.market = market
        self.timeframe = timeframe
        self.dinger_url = dinger_url


# ================================================================
# 同步执行器
# ================================================================

class BackfillDB:
    """全盘批量同步工具。

    A 股 15m/1D: coordinator.coordinate_batch_quotes()
    基金/债: Dinger API
    """

    def __init__(self, source: BackfillSource):
        self.source = source
        self._writer = get_market_kline_writer()

    def run_once(self, tf: str | None = None, symbols: list | None = None) -> dict:
        """执行一次同步。tf 默认取 source.timeframe。"""
        tf = tf or self.source.timeframe

        # 查表: 该不该干
        if tf == "15m":
            should, reason = _should_run_15m()
        elif tf == "1D":
            should, reason = _should_run_1d()
        else:
            should, reason = _should_run_generic(tf)

        if not should:
            return {
                "source": self.source.name, "tf": tf,
                "written": 0, "status": "ok", "report": reason,
            }

        # 执行同步
        try:
            if self.source.dinger_url:
                written, failed = self._sync_via_api(tf)
            elif tf == "15m":
                written, failed = self._sync_15m(symbols)
            elif tf == "1D":
                written, failed = self._sync_1d(symbols)
            else:
                written, failed = 0, []
        except Exception as e:
            report = f"同步异常: {e}"
            logger.error(f"[同步] {self.source.name} tf={tf} {report}")
            _record_update(self.source.name, tf, "error", report)
            return {
                "source": self.source.name, "tf": tf,
                "written": 0, "status": "error", "report": report,
            }

        if failed:
            logger.warning(
                f"[同步] {self.source.name} tf={tf} "
                f"{len(failed)} 只标的失败，下次同步自然重试"
            )

        if written == 0:
            report = "未获取到数据"
            logger.warning(f"[同步] {self.source.name} tf={tf} {report}")
            return {
                "source": self.source.name, "tf": tf,
                "written": 0, "status": "empty", "report": report,
            }

        latest_bar = _latest_finished_bar_time()
        report = f"写入 {written} 条"
        _record_update(self.source.name, tf, "ok", report,
                       last_bar_time=latest_bar)
        logger.info(f"[同步] {self.source.name} tf={tf} {report}")

        return {
            "source": self.source.name, "tf": tf,
            "written": written, "status": "ok", "report": report,
        }

    # ── 15m 同步: batch_quotes + time 标准化 ──────────────────

    def _sync_15m(self, symbols: list | None = None) -> tuple[int, list[str]]:
        """15m 同步: 调用 batch_quotes 下载行情快照，标准化 time 后写入 DB。

        返回: (写入条数, 失败symbols列表)
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

        logger.info(f"[同步] {self.source.name} tf=15m batch_quotes 开始，标的数={len(symbols)}")

        quotes = coord.coordinate_batch_quotes(
            symbols=symbols,
            market=self.source.market,
            timeout=float(_BATCH_TIMEOUT_15M),
        )

        if not quotes:
            logger.warning(f"[同步] {self.source.name} batch_quotes 返回空数据")
            return 0, list(symbols)

        logger.info(f"[同步] {self.source.name} batch_quotes 拉到 {len(quotes)} 只标的")

        # 确定当前 bar 的标准结束时间
        now_cn = datetime.now(TZ_CN)
        bar_time = _normalize_to_bar_time(now_cn)
        if bar_time is None:
            logger.info(f"[同步] {self.source.name} 当前时间 {now_cn:%H:%M} 不在交易时段，跳过")
            return 0, []

        all_records = []
        failed_symbols = []
        success_digits = set()

        for symbol in symbols:
            quote = quotes.get(symbol)
            if not quote:
                digits = strip_market_prefix(symbol)
                quote = quotes.get(digits)

            if not quote:
                failed_symbols.append(symbol)
                continue

            o = float(quote.get("open", 0) or 0)
            h = float(quote.get("high", 0) or 0)
            l = float(quote.get("low", 0) or 0)
            c = float(quote.get("last", 0) or quote.get("close", 0) or 0)
            v = float(quote.get("volume", 0) or 0)

            if o == 0 and h == 0 and l == 0 and c == 0:
                failed_symbols.append(symbol)
                continue
            if c <= 0 or o <= 0:
                failed_symbols.append(symbol)
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

        # 统计缺失
        requested_digits = set(strip_market_prefix(s) for s in symbols)
        fetched_set = set(quotes.keys())
        missing_digits = requested_digits - fetched_set - success_digits
        digit_to_symbol = {strip_market_prefix(s): s for s in symbols}
        failed_symbols.extend(digit_to_symbol[d] for d in missing_digits if d in digit_to_symbol)

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

    def _sync_1d(self, symbols: list | None = None) -> tuple[int, list[str]]:
        """1D 同步: batch_quotes 下载日线快照，<90% 则重试（最多 5 次），两次比对去重。

        返回: (写入条数, 失败symbols列表)
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

        # 日线 bar 时间: 当日 17:00:00 (北京时间)
        now_cn = datetime.now(TZ_CN)
        today_str = now_cn.strftime("%Y-%m-%d")
        if is_trading_day(today_str):
            last_td_str = today_str
        else:
            last_td_str = prev_trading_day(today_str)
        bar_time = datetime.strptime(last_td_str, "%Y-%m-%d").replace(
            hour=17, minute=0, second=0, tzinfo=TZ_CN
        )

        total_symbols = len(symbols)
        threshold = int(total_symbols * _1D_MIN_RATIO)

        # 收集所有成功记录（跨重试去重）
        all_records_map: dict[str, dict] = {}  # symbol → record

        for attempt in range(1 + _1D_MAX_RETRIES):
            # 第一次 + 最多重试 5 次 = 最多 6 次调用
            is_retry = attempt > 0
            label = f"第 {attempt + 1} 次" if attempt == 0 else f"重试 {attempt}/{_1D_MAX_RETRIES}"

            logger.info(
                f"[同步] {self.source.name} 1D {label} batch_quotes 开始，"
                f"标的数={total_symbols}"
            )

            quotes = coord.coordinate_batch_quotes(
                symbols=symbols,
                market=self.source.market,
                timeout=float(_BATCH_TIMEOUT_1D),
            )

            if not quotes:
                logger.warning(f"[同步] {self.source.name} 1D {label} batch_quotes 返回空数据")
                if attempt >= _1D_MAX_RETRIES:
                    break
                continue

            logger.info(f"[同步] {self.source.name} 1D {label} 拉到 {len(quotes)} 只标的")

            # 转换本次结果
            batch_records: dict[str, dict] = {}
            batch_failed: list[str] = []

            for symbol in symbols:
                quote = quotes.get(symbol)
                if not quote:
                    digits = strip_market_prefix(symbol)
                    quote = quotes.get(digits)
                if not quote:
                    batch_failed.append(symbol)
                    continue

                o = float(quote.get("open", 0) or 0)
                h = float(quote.get("high", 0) or 0)
                l = float(quote.get("low", 0) or 0)
                c = float(quote.get("last", 0) or quote.get("close", 0) or 0)
                v = float(quote.get("volume", 0) or 0)

                if o == 0 and h == 0 and l == 0 and c == 0:
                    batch_failed.append(symbol)
                    continue
                if c <= 0 or o <= 0:
                    batch_failed.append(symbol)
                    continue
                if h > 0 and l > 0 and h < l:
                    h, l = l, h

                clean_symbol = strip_market_prefix(symbol)
                record = {
                    "symbol": clean_symbol,
                    "timeframe": "1D",
                    "time": bar_time,
                    "open": o,
                    "high": h,
                    "low": l,
                    "close": c,
                    "volume": v,
                }
                batch_records[clean_symbol] = record

            # 合并到总记录（去重: 同一 symbol 取最新一次）
            all_records_map.update(batch_records)

            success_count = len(all_records_map)
            ratio = success_count / total_symbols if total_symbols else 0
            logger.info(
                f"[同步] {self.source.name} 1D {label} "
                f"本次 {len(batch_records)}，累计去重 {success_count}/{total_symbols} "
                f"({ratio:.1%})"
            )

            # 达到 90% → 不再重试
            if success_count >= threshold:
                logger.info(f"[同步] {self.source.name} 1D 已达 {ratio:.1%} ≥ {_1D_MIN_RATIO:.0%}，停止重试")
                break

            # 还没到最后一次 → 继续重试
            if attempt < _1D_MAX_RETRIES:
                logger.info(
                    f"[同步] {self.source.name} 1D {ratio:.1%} < {_1D_MIN_RATIO:.0%}，"
                    f"准备重试 ({attempt + 1}/{_1D_MAX_RETRIES})"
                )
                time.sleep(5)  # 等待 5s 后重试，给数据源恢复时间

        # 最终统计
        all_records = list(all_records_map.values())
        if not all_records:
            logger.warning(f"[同步] {self.source.name} 1D 所有尝试均无数据")
            return 0, list(symbols)

        final_count = len(all_records)
        final_ratio = final_count / total_symbols if total_symbols else 0
        failed_symbols = [s for s in symbols if strip_market_prefix(s) not in all_records_map]

        logger.info(
            f"[同步] {self.source.name} 1D 最终 "
            f"{final_count}/{total_symbols} ({final_ratio:.1%})，"
            f"失败 {len(failed_symbols)} 只"
        )

        try:
            r = self._writer.bulk_write(self.source.market, all_records)
            total = r.get("inserted", 0) + r.get("skipped", 0)
            logger.info(f"[同步] {self.source.name} 1D 批量写入 {total} 条")
            return total, failed_symbols
        except Exception as e:
            logger.error(f"[同步] {self.source.name} 1D 批量写入失败: {e}")
            return 0, list(symbols)

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

def _should_run_generic(tf: str) -> tuple[bool, str]:
    """非 15m/1D 的通用调度逻辑（基金/债等）。"""
    doc = _get_last_update("fund_nav_daily" if tf == "1D" else "unknown", tf)
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
# 触发式后台同步 — 保证唯一 + 自动结束
# ================================================================
#
# 工作模式:
#   1. cn_stock 调用 trigger_sync() → 新建线程
#   2. 同一时间只允许一个线程运行（_sync_running 原子锁）
#   3. 线程执行完所有同步逻辑后自动退出
#   4. 下次 cn_stock 调用时再触发新线程
#

_sync_running = threading.Lock()


def trigger_sync():
    """由 cn_stock 调用，触发一次后台同步。

    保证同一时间只有一个线程在运行:
      - 已有线程在跑 → 立即返回（不阻塞调用方）
      - 没有线程 → 新建一个，执行完自动退出
    """
    if not _sync_running.acquire(blocking=False):
        return  # 已有线程在跑，跳过

    t = threading.Thread(target=_sync_worker, daemon=True, name="backfill-sync")
    t.start()


def _sync_worker():
    """同步工作线程 — 执行完所有同步逻辑后自动退出。"""
    try:
        _run_all_sync()
    except Exception as e:
        logger.error(f"[后台同步] 异常: {e}")
    finally:
        _sync_running.release()


def _run_all_sync():
    """执行所有需要的同步任务。

    调度逻辑:
      1. 非交易日 → 全部跳过
      2. 15m → 按 bar 触发时间点判断
      3. 1D → 17:00 后执行，含重试
      4. 基金/债 → 17:00 后执行
    """
    now = datetime.now(TZ_CN)
    today_str = now.strftime("%Y-%m-%d")

    # 非交易日不跑
    if not is_trading_day(today_str):
        logger.info("[后台同步] 非交易日，跳过全部同步")
        return

    # ── 15m 同步 ──
    if not ENABLE_15M:
        logger.info("[后台同步] 15m 已关闭 (ENABLE_15M=False)，跳过")
    else:
        try:
            result = stock_daily_k.run_once("15m")
            if result.get("written", 0) > 0:
                logger.info(f"[后台同步] 15m: {result.get('written')} 条 — {result.get('report', '')}")
        except Exception as e:
            logger.error(f"[后台同步] 15m 异常: {e}")

    # ── 1D 同步 ──
    if not ENABLE_1D:
        logger.info("[后台同步] 1D 已关闭 (ENABLE_1D=False)，跳过")
    else:
        try:
            result = stock_daily_k.run_once("1D")
            if result.get("written", 0) > 0:
                logger.info(f"[后台同步] 1D stock: {result.get('written')} 条")
        except Exception as e:
            logger.error(f"[后台同步] 1D stock 异常: {e}")

    # fund + bond (Dinger API) — 只在 17:00 后跑
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

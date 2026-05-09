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
  15m 盘中 → coordinator.coordinate_market_kline(count=None, start_date="") → 批量行情快照
  15m 首次 → coordinator.coordinate_market_kline(count=None, start_date=回溯日期) → 并发逐只
  1D 每日  → coordinator.coordinate_market_kline(count=None, start_date="") → 批量行情快照
  ↓
  db_market.upsert() → PostgreSQL
  ↓
  cn_last_update 记录同步状态

设计原则:
  1. cn_last_update 是唯一的调度控制表
  2. 15m 按 bar 完成时间精确调度，不用简单间隔
  3. 1D 每个交易日只同步一次（17:00 后）
  4. 数据校验: 缺 volume 时保留旧值
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

# 首次同步（cn_last_update 无记录时）的最大回溯天数
MAX_15M_DAYS = 3
MAX_1D_DAYS = 10

# 后台调度循环间隔（秒）
_SCHEDULER_TICK = 30

# 15m bar 完成后的延迟（秒）— 等数据源准备好
# 16:00 的 bar 在 ~16:10~16:17 才能从源拿到
_BAR_READY_DELAY = 70


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
                            source_name VARCHAR(64) NOT NULL,
                            tf VARCHAR(10) NOT NULL,
                            last_updated TIMESTAMP NOT NULL,
                            last_bar_time TIMESTAMP,
                            status VARCHAR(20) DEFAULT 'ok',
                            report TEXT
                        )
                    """)
                    # 兼容旧表: 加 last_bar_time 列（如果不存在）
                    cur.execute("""
                        DO $$
                        BEGIN
                            IF NOT EXISTS (
                                SELECT 1 FROM information_schema.columns
                                WHERE table_name = 'cn_last_update'
                                  AND column_name = 'last_bar_time'
                            ) THEN
                                ALTER TABLE cn_last_update
                                    ADD COLUMN last_bar_time TIMESTAMP;
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
                    "SELECT last_updated, last_bar_time, status, report "
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
                        (id, source_name, tf, last_updated, last_bar_time, status, report)
                    VALUES (%s, %s, %s, NOW(), %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        last_updated  = NOW(),
                        last_bar_time = COALESCE(EXCLUDED.last_bar_time, cn_last_update.last_bar_time),
                        status        = EXCLUDED.status,
                        report        = EXCLUDED.report
                """, (f"{source_name}_{tf}", source_name, tf, last_bar_time, status, report))
                conn.commit()
    except Exception as e:
        logger.error(f"[同步] 写入 cn_last_update 失败: {e}")


# ================================================================
# 15m bar 时间表 — 固定时间点，不是简单间隔
# ================================================================
#
# A 股交易时段: 9:30-11:30, 13:00-15:00
# 15m bar 的结束时间（北京时间）:
#   09:45, 10:00, 10:15, 10:30, 10:45, 11:00, 11:15, 11:30,
#   13:15, 13:30, 13:45, 14:00, 14:15, 14:30, 14:45, 15:00
#
# 每根 bar 结束后需要等数据源准备好（~60-70s 延迟）才能同步。
#

# 15m bar 结束时间（时, 分）— 北京时间
_BAR_END_TIMES = [
    (9, 45),  (10, 0),  (10, 15), (10, 30),
    (10, 45), (11, 0),  (11, 15), (11, 30),
    (13, 15), (13, 30), (13, 45), (14, 0),
    (14, 15), (14, 30), (14, 45), (15, 0),
]


def _bar_ready_times() -> list[datetime]:
    """返回今天所有 15m bar 可供同步的时间点（bar 结束 + 延迟）。"""
    today = datetime.now(TZ_CN).date()
    return [
        datetime(today.year, today.month, today.day, h, m, 0, tzinfo=TZ_CN)
        + timedelta(seconds=_BAR_READY_DELAY)
        for h, m in _BAR_END_TIMES
    ]


def _latest_available_bar_time() -> datetime | None:
    """返回当前时间之前最近一根已就绪的 15m bar 的结束时间。

    例: 现在 10:18, bar_ready_delay=70s
      09:45 → ready 09:46:10  ✓ (已就绪)
      10:00 → ready 10:01:10  ✓ (已就绪)
      10:15 → ready 10:16:10  ✓ (已就绪)
      10:30 → ready 10:31:10  ✗ (还没到)
    → 返回 10:15 的 bar_end_time = 10:30 (这根 bar 覆盖 10:15-10:30)

    Wait, 逻辑需要理清:
    bar 结束时间是指 bar 覆盖的最后一分钟。
    09:30-09:45 这根 bar，结束时间是 09:45。
    数据源在 09:45 + delay 后才准备好。
    所以 "最新可用 bar" = 结束时间 <= now - delay 的最大那根。
    """
    now = datetime.now(TZ_CN)
    cutoff = now - timedelta(seconds=_BAR_READY_DELAY)

    latest = None
    for h, m in _BAR_END_TIMES:
        bar_end = datetime(now.year, now.month, now.day, h, m, 0, tzinfo=TZ_CN)
        if bar_end <= cutoff:
            latest = bar_end
        else:
            break  # 后面的都更大，不用看了

    return latest


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
    """15m 调度: 先查表 → 再看盘中 → 再看时间点。"""
    doc = _get_last_update("stock_daily_k", "15m")

    # ── 第一步: 查表 ──
    if not doc:
        return True, "首次同步，无历史记录"

    status = doc.get("status", "")
    if status == "error":
        return True, f"上次失败: {doc.get('report', '')}，重试"

    last_bar = doc.get("last_bar_time")
    last_updated = doc.get("last_updated")

    if not last_bar and not last_updated:
        return True, "无时间记录，重新同步"

    # ── 第二步: 看是不是盘中 ──
    now = datetime.now(TZ_CN)
    ref_time = last_bar or last_updated
    if ref_time:
        ref_cn = ref_time.astimezone(TZ_CN) if ref_time.tzinfo else ref_time.replace(tzinfo=TZ_CN)
    else:
        ref_cn = None

    # 跨交易日 → 直接同步
    if ref_cn and not _same_trading_day(ref_cn, now):
        return True, f"上次 {ref_cn:%Y-%m-%d}，跨交易日"

    # last_bar_time 异常检测: 不在交易时段的 bar 时间 → 数据有问题，重跑
    if ref_cn:
        bar_time = ref_cn.time()
        is_valid_bar_time = any(
            bar_time >= dt_time(h, m) and bar_time <= dt_time(h, m + 1)
            for h, m in _BAR_END_TIMES
        )
        if not is_valid_bar_time and bar_time < dt_time(9, 30):
            return True, f"last_bar_time={ref_cn:%H:%M} 不在交易时段，数据异常，重跑"

    # ── 第三步: 看时间点 ──
    latest_bar = _latest_available_bar_time()

    # 盘前（9:45+70s 前）→ 没有可同步的 bar
    if not latest_bar:
        # 但如果是首次同步（数据量可能不足），盘前也跑一次历史回填
        report = doc.get("report", "")
        if "首次" in report or "条" not in report:
            return True, "盘前，首次/数据不足，执行历史回填"
        return False, "盘前，无可同步 bar"

    # 有 last_bar_time → 精确比较
    if ref_cn:
        if ref_cn >= latest_bar:
            return False, f"已同步到 {ref_cn:%H:%M}，最新可用 {latest_bar:%H:%M}"
        return True, f"last_bar={ref_cn:%H:%M}, 有新 bar 到 {latest_bar:%H:%M}"

    # 有记录但无 last_bar_time（旧格式）→ 按间隔判断
    if last_updated:
        elapsed = (now - (last_updated.astimezone(TZ_CN) if last_updated.tzinfo else last_updated.replace(tzinfo=TZ_CN))).total_seconds()
        if elapsed < 300:
            return False, f"距上次 {elapsed:.0f}s，暂不更新"
        return True, f"距上次 {elapsed:.0f}s，检查新 bar"

    return True, "记录不完整，重新同步"


def _should_run_1d() -> tuple[bool, str]:
    """1D 调度: 先查表 → 没记录直接干 → 再看时间点。"""
    now = datetime.now(TZ_CN)
    doc = _get_last_update("stock_daily_k", "1D")

    # ── 第一步: 查表 ──
    if not doc:
        return True, "首次 1D 同步"

    status = doc.get("status", "")
    if status == "error":
        return True, f"上次 1D 失败: {doc.get('report', '')}，重试"

    last_updated = doc.get("last_updated")
    if last_updated:
        last_cn = last_updated.astimezone(TZ_CN) if last_updated.tzinfo else last_updated.replace(tzinfo=TZ_CN)
        # 跨交易日 → 待同步
        if not _same_trading_day(last_cn, now):
            return True, f"上次 1D {last_cn:%Y-%m-%d}，跨交易日"
        # 同交易日已同步 → 不重复
        return False, "本交易日 1D 已同步"

    # ── 第二步: 看时间点（首次已处理，这里是有记录但无 last_updated 的异常情况）──
    if now.time() < dt_time(17, 0):
        return False, "17:00 前不更新 1D"

    from app.utils.trading_calendar import is_trading_day_today
    if not is_trading_day_today():
        return False, "非交易日"

    return True, "本交易日 1D 待同步"


# ================================================================
# 数据校验
# ================================================================

def _validate_bars(bars: list[dict]) -> list[dict]:
    """校验 K 线数据，修正明显问题。

    规则:
    - volume 缺失或为 0 → 保留（有的源确实没有 volume）
    - OHLC 全为 0 → 丢弃（无效数据）
    - high < low → 交换
    """
    validated = []
    for bar in bars:
        o = float(bar.get("open", 0))
        h = float(bar.get("high", 0))
        l = float(bar.get("low", 0))
        c = float(bar.get("close", 0))

        # OHLC 全为 0 → 跳过
        if o == 0 and h == 0 and l == 0 and c == 0:
            continue

        # 极端负数/零价 → 跳过
        if c <= 0 or o <= 0:
            continue

        # high < low → 交换
        if h > 0 and l > 0 and h < l:
            bar["high"], bar["low"] = l, h

        validated.append(bar)

    return validated


# ================================================================
# 日期范围计算
# ================================================================

def _date_range_15m(last_bar_time: datetime | None) -> tuple[str, str]:
    """15m 增量同步的日期范围。

    有 last_bar → start_date = last_bar 所在交易日（从那天开始补）
    无 last → start_date = 往前推 MAX_15M_DAYS 天
    end_date = 今天
    """
    end_date = datetime.now(TZ_CN).strftime("%Y-%m-%d")

    if not last_bar_time:
        start = (datetime.now(TZ_CN) - timedelta(days=MAX_15M_DAYS)).strftime("%Y-%m-%d")
    else:
        # last_bar_time 可能是带时区的
        if last_bar_time.tzinfo:
            start = last_bar_time.astimezone(TZ_CN).strftime("%Y-%m-%d")
        else:
            start = last_bar_time.strftime("%Y-%m-%d")

    return start, end_date


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

    A 股:  coordinator.coordinate_market_kline(count=None, ...)
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
                written = self._sync_via_api(tf)
            else:
                written = self._sync_via_coordinator(tf, symbols=symbols)
        except Exception as e:
            report = f"同步异常: {e}"
            logger.error(f"[同步] {self.source.name} tf={tf} {report}")
            _record_update(self.source.name, tf, "error", report)
            return {
                "source": self.source.name, "tf": tf,
                "written": 0, "status": "error", "report": report,
            }

        if written == 0:
            # 没写入数据 → 不记录 ok，下次还会重试
            report = "未获取到数据"
            logger.warning(f"[同步] {self.source.name} tf={tf} {report}")
            return {
                "source": self.source.name, "tf": tf,
                "written": 0, "status": "empty", "report": report,
            }

        # 有实际写入 → 记录成功
        latest_bar = _latest_available_bar_time()
        report = f"写入 {written} 条"
        _record_update(self.source.name, tf, "ok", report,
                       last_bar_time=latest_bar)
        logger.info(f"[同步] {self.source.name} tf={tf} {report}")

        return {
            "source": self.source.name, "tf": tf,
            "written": written, "status": "ok", "report": report,
        }

    def _sync_via_coordinator(self, tf: str, symbols: list | None = None) -> int:
        """A 股: 通过 coordinator 全市场批量同步。

        关键: 区分"增量快照"和"历史回填"两种路径。
          - 增量快照: count=None, start_date="" → 走 batch_quotes（1 HTTP，快）
          - 历史回填: count=None, start_date=过去日期 → 走并发 fetch_kline（N HTTP）
        """
        from app.data_sources.coordinator import get_coordinator
        from app.data_sources.circuit_breaker import get_realtime_circuit_breaker
        from app.data_sources.kline_clean import clean_klines

        if not symbols:
            from app.utils.basicinfo_db import get_stock_basic_db
            symbols = get_stock_basic_db().market_all_codes(status="active")
        if not symbols:
            logger.warning(f"[同步] {self.source.name} 获取股票列表失败")
            return 0

        coord = get_coordinator()
        cb = get_realtime_circuit_breaker()

        doc = _get_last_update(self.source.name, tf)
        last_bar_time = doc.get("last_bar_time") if doc else None
        last_updated = doc.get("last_updated") if doc else None

        is_incremental = self._is_incremental(tf, last_bar_time, last_updated)

        if is_incremental:
            # 增量快照: count=None + start_date="" → 批量行情（1 HTTP，每只 1 bar）
            start_date = ""
            end_date = ""
            logger.info(f"[同步] {self.source.name} tf={tf} 增量快照模式")
        else:
            # 历史回填: count=None + start_date=回溯日期 → 并发逐只
            ref_time = last_bar_time or last_updated
            start_date, end_date = _date_range_15m(ref_time)
            logger.info(
                f"[同步] {self.source.name} tf={tf} "
                f"历史回填模式 {start_date} ~ {end_date}"
            )

        result = coord.coordinate_market_kline(
            cb=cb,
            market=self.source.market,
            timeframe=tf,
            count=None,
            start_date=start_date,
            end_date=end_date,
            timeout=300,
            symbols=symbols,
        )

        if not result:
            logger.warning(f"[同步] {self.source.name} coordinator 返回空数据")
            return 0

        logger.info(f"[同步] {self.source.name} tf={tf} 拉到 {len(result)} 只标的")

        # 收集所有标的的数据，一次性批量写入
        all_records = []
        for symbol, bars in result.items():
            if not bars:
                continue
            cleaned = clean_klines(bars, tf)
            if not cleaned:
                continue
            cleaned = _validate_bars(cleaned)
            if not cleaned:
                continue
            for bar in cleaned:
                all_records.append({
                    "symbol": symbol,
                    "timeframe": tf,
                    "time": bar["time"],
                    "open": bar["open"],
                    "high": bar["high"],
                    "low": bar["low"],
                    "close": bar["close"],
                    "volume": bar.get("volume", 0),
                })

        if not all_records:
            return 0

        # 记录成功获取的 symbol 数量 vs 总数
        fetched_symbols = set(r["symbol"] for r in all_records)
        total_symbols = len(result)
        if len(fetched_symbols) < total_symbols:
            missing = total_symbols - len(fetched_symbols)
            logger.warning(
                f"[同步] {self.source.name} tf={tf} "
                f"部分失败: {total_symbols}只中{missing}只未获取到数据"
            )

        try:
            r = self._writer.bulk_write(self.source.market, all_records)
            total = r.get("inserted", 0) + r.get("skipped", 0)
            logger.info(f"[同步] {self.source.name} tf={tf} 批量写入 {total} 条")
            return total
        except Exception as e:
            logger.error(f"[同步] {self.source.name} 批量写入失败: {e}")
            return 0

    @staticmethod
    def _is_incremental(tf: str, last_bar_time, last_updated) -> bool:
        """判断应该走增量快照还是历史回填。

        增量快照: count=None, start_date="" → batch_quotes（1 HTTP，1 bar/只）
        历史回填: count=None, start_date=过去日期 → 并发 fetch_kline（多 bar/只）

        首次（无记录）→ False，走历史回填补齐数据
        有记录且同交易日 → True，只拿最新 bar
        有记录但跨交易日 → False，走历史回填补齐当天数据
        """
        ref_time = last_bar_time or last_updated
        if not ref_time:
            return False  # 首次 → 历史回填

        now = datetime.now(TZ_CN)
        if _same_trading_day(ref_time, now):
            return True   # 同交易日 → 增量快照（只要最新 bar）

        # 跨交易日 → 历史回填（需要补齐当天从开盘到现在的 bar）
        return False

    def _sync_via_api(self, tf: str) -> int:
        """基金/债: 通过 Dinger API 拉取并写入。"""
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
            return 0

        # 按 symbol 分组
        ts_field = "navDate" if "fund" in self.source.name else "date"
        by_symbol = {}
        for item in items:
            sym = item.get("symbol", "")
            if not sym:
                continue
            by_symbol.setdefault(sym, []).append(item)

        # 收集所有数据，一次性批量写入
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
            return 0

        all_records = [
            r for r in all_records
            if r["open"] != 0 or r["high"] != 0 or r["low"] != 0 or r["close"] != 0
        ]
        if not all_records:
            return 0

        # 记录成功获取的 symbol 数量 vs 总数
        fetched_symbols = set(r["symbol"] for r in all_records)
        total_symbols = len(result)
        if len(fetched_symbols) < total_symbols:
            missing = total_symbols - len(fetched_symbols)
            logger.warning(
                f"[同步] {self.source.name} tf={tf} "
                f"部分失败: {total_symbols}只中{missing}只未获取到数据"
            )

        try:
            r = self._writer.bulk_write(self.source.market, all_records)
            total = r.get("inserted", 0) + r.get("skipped", 0)
            logger.info(f"[同步] {self.source.name} tf={tf} 批量写入 {total} 条")
            return total
        except Exception as e:
            logger.error(f"[同步] {self.source.name} 批量写入失败: {e}")
            return 0


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
    if status == "error":
        return True, f"上次失败，重试"
    last = doc.get("last_updated")
    if last and not _same_trading_day(last, datetime.now(TZ_CN)):
        return True, "跨交易日"
    return False, "同交易日已同步"


# ================================================================
# 触发式后台同步 — 保证唯一 + 自动结束
# ================================================================
#
# 工作模式（你提的方案）:
#   1. cn_stock 调用 trigger_sync() → 新建线程
#   2. 同一时间只允许一个线程运行（_sync_running 原子锁）
#   3. 线程执行完所有同步逻辑后自动退出
#   4. 下次 cn_stock 调用时再触发新线程
#
# 不是常驻 daemon，没有轮询循环，用完即走。
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

    调度优先级（每个任务内部自行判断）:
      1. 查 cn_last_update → 没记录就直接干（首次回填）
      2. 有记录 → 看是不是盘中
      3. 盘中 → 看是不是到了 bar 完成时间点
    """
    now = datetime.now(TZ_CN)

    # ── 15m 同步 ──
    # _should_run_15m() 内部已包含完整判断链:
    #   查表 → 首次/失败直接干 → 跨交易日直接干 → 盘中按 bar 时间点判断
    try:
        result = stock_daily_k.run_once("15m")
        if result.get("written", 0) > 0:
            logger.info(f"[后台同步] 15m: {result.get('written')} 条 — {result.get('report', '')}")
    except Exception as e:
        logger.error(f"[后台同步] 15m 异常: {e}")

    # ── 1D 同步 ──
    # _should_run_1d() 内部已包含完整判断: 首次立即干，之后 17:00 后每交易日一次
    try:
        result = stock_daily_k.run_once("1D")
        if result.get("written", 0) > 0:
            logger.info(f"[后台同步] 1D stock: {result.get('written')} 条")
    except Exception as e:
        logger.error(f"[后台同步] 1D stock 异常: {e}")

    # fund + bond (Dinger API) — 只在 17:00 后跑
    if now.time() >= dt_time(17, 0):
        from app.utils.trading_calendar import is_trading_day_today
        if is_trading_day_today():
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

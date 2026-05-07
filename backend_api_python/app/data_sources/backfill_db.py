"""
backfill_db.py — 全盘批量同步 K 线到 PostgreSQL

═══════════════════════════════════════════════════════════════
  架构位置: cn_stock → backfill_db → coordinator → 数据源
═══════════════════════════════════════════════════════════════

核心职责: 全盘同步最新 K 线，写入 PostgreSQL（db_market）

数据流:
  A 股:  coordinator.coordinate_market_kline(count=None) → db_market.upsert()
  基金/债: Dinger API → db_market.upsert()

决策依据: cn_last_update 表（PostgreSQL）
  该不该干 → 查 last_updated，判断间隔
  干了什么 → 记录 status / report

设计原则:
  1. 只做全盘同步，不做单只
  2. 下游根据 start_date/end_date 自行决定返回多少 bar
  3. 先删后写，保证数据干净
  4. 所有 DB 读写通过 db_market
"""

import logging
import threading
from datetime import datetime, timedelta, timezone

from app.utils.db_market import get_market_db_manager, get_market_kline_writer
from app.utils.trading_calendar import is_trading_day, prev_trading_day
from app.utils.logger import get_logger

logger = get_logger(__name__)

TZ_CN = timezone(timedelta(hours=8))

# 首次同步（cn_last_update 无记录时）的最大回溯天数
MAX_15M_DAYS = 10
MAX_1D_DAYS = 30


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
                            status VARCHAR(20) DEFAULT 'ok',
                            report TEXT
                        )
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
                    "SELECT last_updated, status, report "
                    "FROM cn_last_update WHERE id = %s",
                    (f"{source_name}_{tf}",),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return {"last_updated": row[0], "status": row[1], "report": row[2]}
    except Exception as e:
        logger.error(f"[同步] 查询 cn_last_update 失败: {e}")
        return None


def _record_update(source_name: str, tf: str, status: str, report: str):
    """写入同步记录到 cn_last_update。"""
    _ensure_cn_last_update_table()
    try:
        mgr = get_market_db_manager()
        pool = mgr._get_pool("CNStock")
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO cn_last_update (id, source_name, tf, last_updated, status, report)
                    VALUES (%s, %s, %s, NOW(), %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        last_updated = NOW(),
                        status = EXCLUDED.status,
                        report = EXCLUDED.report
                """, (f"{source_name}_{tf}", source_name, tf, status, report))
                conn.commit()
    except Exception as e:
        logger.error(f"[同步] 写入 cn_last_update 失败: {e}")


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


def _should_run(source_name: str, tf: str) -> tuple[bool, str]:
    """查 cn_last_update，判断是否需要同步。"""
    doc = _get_last_update(source_name, tf)
    if not doc:
        return True, "首次同步，无历史记录"

    status = doc.get("status", "")
    if status == "error":
        return True, f"上次失败: {doc.get('report', '')}，重试"
    if status != "ok":
        return True, f"上次 status={status}，需要同步"

    last = doc.get("last_updated")
    if not last:
        return True, "无 last_updated，重新同步"

    # 1D: 同交易日不干，跨交易日干
    if tf == "1D":
        if _same_trading_day(last, datetime.utcnow()):
            return False, "本交易日 1D 已同步"
        return True, f"上次 1D 是 {last:%Y-%m-%d}，跨交易日了"

    # 15m: 跨交易日直接干，同交易日 5 分钟节流
    if not _same_trading_day(last, datetime.utcnow()):
        return True, f"15m 上次是 {last:%Y-%m-%d}，跨交易日了"

    elapsed = (datetime.utcnow() - last).total_seconds()
    if elapsed < 300:
        return False, f"15m 距上次 {elapsed:.0f}s < 300s，跳过"
    return True, f"15m 距上次 {elapsed:.0f}s，盘中有新 bar"


# ================================================================
# 日期范围计算
# ================================================================

def _date_range(tf: str, last: datetime | None) -> tuple[str, str]:
    """根据上次同步时间计算 start_date / end_date。

    end_date 固定为今天。
    有 last → start_date = last 所在交易日
    无 last → start_date = 往前推 MAX 天
    """
    end_date = datetime.now(TZ_CN).strftime("%Y-%m-%d")

    if not last:
        days = MAX_15M_DAYS if tf == "15m" else MAX_1D_DAYS
        start_date = (datetime.now(TZ_CN) - timedelta(days=days)).strftime("%Y-%m-%d")
    else:
        start_date = last.astimezone(TZ_CN).strftime("%Y-%m-%d")

    return start_date, end_date


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

    A 股:  coordinator.coordinate_market_kline(count=None, start_date, end_date)
    基金/债: Dinger API
    """

    def __init__(self, source: BackfillSource):
        self.source = source
        self._writer = get_market_kline_writer()

    def run_once(self, tf: str | None = None) -> dict:
        """执行一次全盘同步。"""
        tf = tf or self.source.timeframe

        # 查表: 该不该干
        should, reason = _should_run(self.source.name, tf)
        if not should:
            return {
                "source": self.source.name, "tf": tf,
                "written": 0, "status": "ok", "report": reason,
            }

        # 算日期范围
        doc = _get_last_update(self.source.name, tf)
        last = doc.get("last_updated") if doc else None
        start_date, end_date = _date_range(tf, last)

        # 执行同步
        try:
            if self.source.dinger_url:
                written = self._sync_via_api(tf, start_date, end_date)
            else:
                written = self._sync_via_coordinator(tf, start_date, end_date)
        except Exception as e:
            report = f"同步异常: {e}"
            logger.error(f"[同步] {self.source.name} tf={tf} {report}")
            _record_update(self.source.name, tf, "error", report)
            return {
                "source": self.source.name, "tf": tf,
                "written": 0, "status": "error", "report": report,
            }

        report = f"{start_date} ~ {end_date} 写入 {written} 条"
        _record_update(self.source.name, tf, "ok", report)
        logger.info(f"[同步] {self.source.name} tf={tf} {report}")

        return {
            "source": self.source.name, "tf": tf,
            "written": written, "status": "ok", "report": report,
        }

    def _sync_via_coordinator(self, tf: str, start_date: str, end_date: str) -> int:
        """A 股: 通过 coordinator 全市场批量同步。"""
        from app.data_sources.coordinator import get_coordinator
        from app.data_sources.circuit_breaker import get_realtime_circuit_breaker
        from app.data_sources.kline_clean import clean_klines

        coord = get_coordinator()
        cb = get_realtime_circuit_breaker()

        logger.info(f"[同步] {self.source.name} tf={tf} {start_date} ~ {end_date} 全市场拉取中...")

        result = coord.coordinate_market_kline(
            cb=cb,
            market=self.source.market,
            timeframe=tf,
            count=None,
            start_date=start_date,
            end_date=end_date,
            timeout=120,
        )

        if not result:
            logger.warning(f"[同步] {self.source.name} coordinator 返回空数据")
            return 0

        logger.info(f"[同步] {self.source.name} tf={tf} 拉到 {len(result)} 只标的")

        total_written = 0
        for symbol, bars in result.items():
            if not bars:
                continue
            cleaned = clean_klines(bars, tf)
            if not cleaned:
                continue
            try:
                r = self._writer.upsert(self.source.market, symbol, tf, cleaned)
                total_written += r.get("inserted", 0) + r.get("updated", 0)
            except Exception as e:
                logger.warning(f"[同步] {self.source.name} {symbol} 写入失败: {e}")

        return total_written

    def _sync_via_api(self, tf: str, start_date: str, end_date: str) -> int:
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

        total_written = 0
        for symbol, records in by_symbol.items():
            bars = []
            for rec in records:
                ts_str = rec.get(ts_field)
                if not ts_str:
                    continue
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")) if isinstance(ts_str, str) else ts_str
                except ValueError:
                    continue
                bars.append({
                    "time": ts,
                    "open": float(rec.get("open", 0)),
                    "high": float(rec.get("high", 0)),
                    "low": float(rec.get("low", 0)),
                    "close": float(rec.get("close", 0)),
                    "volume": float(rec.get("volume", 0)),
                })
            if not bars:
                continue
            try:
                r = self._writer.upsert(self.source.market, symbol, tf, bars)
                total_written += r.get("inserted", 0) + r.get("updated", 0)
            except Exception as e:
                logger.warning(f"[同步] {self.source.name} {symbol} 写入失败: {e}")

        return total_written


# ================================================================
# 预定义数据源实例
# ================================================================

DINGER_BASE_URL = "https://api.quantdinger.com/v1"

stock_daily_k = BackfillDB(BackfillSource(
    name="stock_daily_k", market="CNStock", timeframe="1D",
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
# 全盘同步入口
# ================================================================

def run_once(tf: str | None = None) -> list[dict]:
    """全盘同步入口 — 三个数据源依次执行。"""
    results = []
    for source in (stock_daily_k, fund_nav_daily, bond_daily_k):
        try:
            r = source.run_once(tf)
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

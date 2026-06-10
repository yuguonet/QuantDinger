"""
backfill_db.py — A 股 K 线增量同步 + 后台同步

═══════════════════════════════════════════════════════════════
  架构位置: backfill_db → coordinator(kline/15m) / coordinator(1D)
═══════════════════════════════════════════════════════════════

核心职责:
  1. 交易日 15:05 后同步当日 15m bar（16 条，走 kline API）
  2. 交易日 17:00 后同步当日 1D bar
  3. 首次运行时做历史回填
  4. 后台自动同步，不影响主线程

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
  1. cn_last_update 是唯一的同步控制表
  2. 15m 每个交易日 15:05 后同步一次（kline API，当天 16 条 bar）
     盘中无法获取分时线 OHLCV 中的 HL 值，故不在盘中拉取
  3. 1D 每个交易日 17:00 后同步一次
  4. 非交易日不执行
  5. 后台 daemon 线程自动运行，fire-and-forget
  6. 所有数据源走内联 provider，不依赖外部 API
"""

import os as _os
import re as _re
import subprocess
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

# 1D 无需内部重试 — 同步器 17:00 后每轮自动重试
# 增量同步: 首次全量拉取+写入，后续只补拉缺失 symbols


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
    """INSERT 新行到 cn_last_update。

    用于: 新交易日首次写入（全新拉取），(tf, last_bar_time) 不存在时。
    如果 (tf, last_bar_time) 已存在（唯一约束冲突），降级为 UPDATE。
    """
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
                    VALUES (%s, %s, %s, %s,
                            %s, %s, %s)
                    ON CONFLICT (tf, last_bar_time) DO UPDATE SET
                        status = EXCLUDED.status,
                        report = EXCLUDED.report,
                        synced_count = EXCLUDED.synced_count,
                        failed_count = EXCLUDED.failed_count,
                        written_count = EXCLUDED.written_count
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
    """UPDATE 已有行，按 (tf, last_bar_time) 定位。

    用于: 同一交易日修复/重试后更新 status、report 等字段。
    只更新传入的非 None 字段。
    """
    naive_lbt = last_bar_time.replace(tzinfo=None) if last_bar_time.tzinfo else last_bar_time
    sets: list[str] = []
    params: list = []
    if status is not None:
        sets.append("status = %s")
        params.append(status)
    if report is not None:
        sets.append("report = %s")
        params.append(report)
    if synced_count is not None:
        sets.append("synced_count = %s")
        params.append(synced_count)
    if failed_count is not None:
        sets.append("failed_count = %s")
        params.append(failed_count)
    if written_count is not None:
        sets.append("written_count = %s")
        params.append(written_count)
    if not sets:
        return  # 无字段需要更新
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
                updated = cur.rowcount
                conn.commit()
                if updated == 0:
                    logger.warning(f"[同步] UPDATE cn_last_update 未匹配: tf={tf}, last_bar_time={naive_lbt}")
    except Exception as e:
        logger.error(f"[同步] UPDATE cn_last_update 失败: {e}")


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


def _normalize_15m_bar_time(dt_obj: datetime) -> datetime | None:
    """将任意时间标准化到其所属 15m bar 的标准结束时间。

    规则:
      - 盘前 (< 09:30) → None
      - 午休 (11:30 ~ 13:00) → None
      - 盘后 (> 15:00) → 归到 15:00
      - 交易时段内 → 找到所属 bar 的标准结束时间

    例: 09:30:00 → 09:45（第一根 bar）
        09:44:59 → 09:45
        09:45:00 → 09:45（精确命中 bar 结束时间）
        09:45:01 → 10:00（属于 09:45~10:00 这根 bar）
        10:05:00 → 10:15
        12:00:00 → None（午休）
        13:00:00 → 13:15（下午第一根 bar）
        15:30:00 → 15:00（盘后归到最后一根 bar）
    """
    t = dt_obj.astimezone(TZ_CN) if dt_obj.tzinfo else dt_obj.replace(tzinfo=TZ_CN)
    t_time = t.time()

    # 盘前 (< 09:30) → 不属于任何 bar
    if t_time < dt_time(9, 30):
        return None

    # 午休 (11:30 < t < 13:00) → 不属于任何 bar
    if dt_time(11, 30) < t_time < dt_time(13, 0):
        return None

    # 从有序列表中找到第一个结束时间 >= t_time 的 bar
    for h, m in _VALID_15M_BAR_TIMES_SORTED:
        bar_end_time = dt_time(h, m)
        if t_time <= bar_end_time:
            return datetime(t.year, t.month, t.day, h, m, 0, tzinfo=TZ_CN)

    # 超过 15:00 → 归到最后一根 bar (15:00)
    return datetime(t.year, t.month, t.day, 15, 0, 0, tzinfo=TZ_CN)


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

    A 股 15m: 15:05 后通过 coordinator.coordinate_market_kline 拉取当天 16 条 bar
    A 股 1D:  coordinator.coordinate_batch_quotes（含重试）
    基金/债:  Dinger API
    """

    def __init__(self, source: BackfillSource):
        self.source = source
        self._writer = get_market_kline_writer()

    # ── OHLCV 提取 + 校验（15m / 1D 共用） ──

    @staticmethod
    def _parse_ohlcv(record: dict, field_map: dict | None = None) -> tuple[float, float, float, float, float] | None:
        """从 quote/bar dict 提取 OHLCV，校验后返回 (o, h, l, c, v)。

        field_map: 自定义字段映射，默认 {"open":"open", "high":"high", "low":"low",
                   "close":"close", "volume":"volume"}，1D 的 close 字段可传 "last"。
        返回 None 表示数据无效（应跳过/记为失败）。
        """
        fm = field_map or {}
        o = float(record.get(fm.get("open", "open"), 0) or 0)
        h = float(record.get(fm.get("high", "high"), 0) or 0)
        l = float(record.get(fm.get("low", "low"), 0) or 0)
        c = float(record.get(fm.get("close", "close"), 0) or 0)
        v = float(record.get(fm.get("volume", "volume"), 0) or 0)

        # 1D 兼容: close 可能在 "last" 字段
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

    def run_once(self, tf: str | None = None, symbols: list | None = None,
                 skip_repair: bool = False, force_refetch: bool = False) -> dict:
        """执行一次同步。tf 默认取 source.timeframe。

        职责: 调用 _sync_* 拉数据，然后 INSERT/UPDATE cn_last_update。
        skip_repair=True: 由同步器控制修复循环。
        force_refetch=True: 跳过"已同步"检查，强制重新拉取所有传入的 symbols。
        """
        tf = tf or self.source.timeframe
        pool = self.source.db_pool

        # 提前读取已有记录，异常时用于 update
        existing_doc = _get_last_update(tf, pool_name=pool)
        existing_lbt = _parse_db_timestamp(existing_doc.get("last_bar_time")) if existing_doc else None
        existing_status = existing_doc.get("status", "") if existing_doc else ""
        # 计算 bar_time（与 _sync_* 内部逻辑一致）
        now_cn = datetime.now(TZ_CN)
        today_str = now_cn.strftime("%Y-%m-%d")
        if tf == "15m":
            if now_cn.time() >= dt_time(15, 5) and is_trading_day(today_str):
                target_td = today_str
            else:
                target_td = prev_trading_day(today_str)
            bar_time = datetime.strptime(target_td, "%Y-%m-%d").replace(
                hour=15, minute=0, second=0, tzinfo=TZ_CN
            )
        elif tf == "1D":
            if now_cn.time() >= dt_time(17, 0) and is_trading_day(today_str):
                target_td = today_str
            else:
                target_td = prev_trading_day(today_str)
            bar_time = datetime.strptime(target_td, "%Y-%m-%d").replace(
                hour=0, minute=0, second=0, tzinfo=TZ_CN
            )
        else:
            return {
                "source": self.source.name, "tf": tf,
                "written": 0, "status": "ok", "report": f"不支持的周期: {tf}",
            }

        # 判断是否当天首次运行
        is_first_run = True
        if existing_lbt and _same_trading_day(existing_lbt, now_cn):
            is_first_run = False

        # ── 2. ok 保护：同交易日已完成 → 跳过 ──
        if (existing_lbt and existing_status in ("ok", "error")
                and _same_trading_day(existing_lbt, bar_time)
                and not force_refetch):
            return {
                "source": self.source.name, "tf": tf,
                "written": 0, "status": "ok",
                "report": f"该交易日已同步完成(status=ok)，跳过",
            }

        # ── 新交易日首次写入: INSERT 初始记录 ──
        if not existing_lbt or not _same_trading_day(existing_lbt, bar_time):
            total_syms = len(symbols) if symbols else 0
            if not symbols:
                try:
                    from app.utils.basicinfo_db import get_stock_basic_db
                    total_syms = len(get_stock_basic_db().market_all_codes(status="active"))
                except Exception:
                    pass
            _insert_record(
                tf, "re",
                f"开始同步 {tf} ({total_syms} 只标的)",
                last_bar_time=bar_time, synced_count=total_syms,
                failed_count=0, pool_name=pool, written_count=0,
            )

        # ── 调用 _sync_* 拉数据 ──
        try:
            if tf == "15m":
                written, failed, final_count, failed_reasons = self._sync_15m(
                    symbols, is_first_run=is_first_run,
                    force_refetch=force_refetch)
            elif tf == "1D":
                written, failed, final_count, failed_reasons = self._sync_1d(
                    symbols, is_first_run=is_first_run,
                    force_refetch=force_refetch)
            else:
                written, failed, final_count, failed_reasons = 0, [], 0, {}
        except Exception as e:
            error_msg = f"同步异常: {e}"
            logger.error(f"[同步] {self.source.name} tf={tf} {error_msg}")
            existing_report = existing_doc.get("report", "") if existing_doc else ""
            existing_failed_syms = _extract_failed_symbols_from_report(existing_report)
            if existing_failed_syms:
                report = f"{error_msg}; 失败标的({len(existing_failed_syms)}): {','.join(existing_failed_syms)}"
            else:
                report = error_msg
            if existing_lbt:
                _update_record(tf, existing_lbt,
                              status="re", report=report, pool_name=pool,
                              failed_count=(existing_doc.get("failed_count") or 0) + 1 if existing_doc else None,
                              synced_count=existing_doc.get("synced_count") if existing_doc else None,
                              written_count=existing_doc.get("written_count") if existing_doc else None)
            else:
                _insert_record(tf, "re", report,
                              last_bar_time=bar_time,
                              synced_count=0, failed_count=1, pool_name=pool, written_count=0)
            return {
                "source": self.source.name, "tf": tf,
                "written": 0, "status": "error", "report": report,
            }

        # ── 落盘 cn_last_update ──
        total_symbols = final_count + len(failed)
        report_parts = [f"已同步 {final_count}/{total_symbols}"]
        if failed:
            reason_counts: dict[str, int] = {}
            for sym in failed:
                reason = failed_reasons.get(sym, "未返回数据")
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
            reason_summary = ", ".join(f"{r}({c})" for r, c in sorted(reason_counts.items(), key=lambda x: -x[1]))
            report_parts.append(f"失败原因: {reason_summary}")
            report_parts.append(f"失败标的({len(failed)}): {','.join(failed)}")

        report = "; ".join(report_parts)
        sync_rate = final_count / total_symbols if total_symbols > 0 else 0
        final_status = "ok" if sync_rate > 0.9 and not failed else "re"

        _update_record(
            tf, bar_time,
            status=final_status, report=report,
            synced_count=total_symbols,
            failed_count=len(failed), pool_name=pool,
            written_count=final_count,
        )

        return {
            "source": self.source.name, "tf": tf,
            "written": written, "status": final_status, "report": report,
        }

    # ── 15m kline 数据拉取 ──

    def _fetch_15m_klines(self, symbols: list) -> list[dict]:
        """拉取 15m kline 数据。通过 Coordinator.coordinate_market_kline 批量拉取。"""
        from app.data_sources.coordinator import get_coordinator

        coord = get_coordinator()
        try:
            klines = coord.coordinate_market_kline(
                symbols=symbols,
                market=self.source.market,
                timeframe="15m",
                count=_15M_BARS_PER_DAY,
                timeout=float(_BATCH_TIMEOUT),
            )
            if klines:
                return klines
        except AttributeError:
            logger.info(f"[同步] {self.source.name} coordinator 无 kline 方法，回退到逐 symbol 拉取")
        except Exception as e:
            logger.warning(f"[同步] {self.source.name} coordinator kline 失败: {e}，回退到逐 symbol 拉取")

        return self._fetch_15m_via_provider(symbols)

    def _fetch_15m_via_provider(self, symbols: list) -> list[dict]:
        """回退方案: 通过 Coordinator.coordinate_market_kline 批量拉取 15m 数据。"""
        from app.data_sources.coordinator import get_coordinator

        coord = get_coordinator()
        logger.info(f"[同步] {self.source.name} 回退: 通过 Coordinator.coordinate_market_kline 批量拉取 15m kline")

        try:
            klines = coord.coordinate_market_kline(
                symbols=symbols,
                market=self.source.market,
                timeframe="15m",
                count=_15M_BARS_PER_DAY,
                timeout=float(_BATCH_TIMEOUT),
            )
            if klines:
                return klines
        except Exception as e:
            logger.warning(f"[同步] {self.source.name} coordinator market_kline 回退失败: {e}")

        return []

    def _klines_to_records(self, symbols: list, klines: list[dict],
                           fallback_time: datetime, failed_reasons: dict[str, str]) -> list[dict]:
        """将 kline 扁平列表转换为 bulk_write 记录。

        处理:
        1. 按 symbol 分组
        2. 时间归一化: 将任意时间戳映射到标准 15m bar 结束时间
        3. 过滤非交易时段（盘前、午休）
        4. 按 (symbol, time) 去重，保留最后一条
        5. OHLCV 校验

        同时更新 failed_reasons（无数据 / 无有效 bar 的 symbol）。
        """
        from app.data_sources.normalizer import strip_market_prefix

        kline_map: dict[str, list[dict]] = {}
        for bar in klines:
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

            # 按 (symbol, normalized_time) 去重，保留最后一条
            seen: dict[tuple[str, datetime], dict] = {}

            for bar in bars:
                # 时间归一化: kline 时间戳 → 标准 15m bar 结束时间
                raw_time = self._parse_bar_time(bar, fallback_time)
                bar_time_dt = _normalize_15m_bar_time(raw_time)
                if bar_time_dt is None:
                    # 盘前或午休，不属于任何 bar
                    logger.debug(
                        f"[同步] {clean} 过滤非交易时段 bar: {raw_time:%Y-%m-%d %H:%M}"
                    )
                    continue

                ohlcv = self._parse_ohlcv(bar)
                if ohlcv is None:
                    continue
                o, h, l, c, v = ohlcv

                seen[(clean, bar_time_dt)] = {
                    "symbol": clean,
                    "timeframe": "15m",
                    "time": bar_time_dt,
                    "open": o, "high": h, "low": l, "close": c, "volume": v,
                }

            if not seen:
                failed_reasons.setdefault(symbol, "无有效 15m bar")
                continue

            records.extend(seen.values())

        return records

    # ── 15m 同步主逻辑 ──

    def _sync_15m(self, symbols: list | None = None, is_first_run: bool = True,
                  force_refetch: bool = False) -> tuple[int, list[str], int, dict[str, str]]:
        """15m 同步: 单次拉取 + 写 kline 表，不操作 cn_last_update。

        1. 首次运行清除当日旧 15m bar
        2. 通过 kline API 单次拉取（16 bars/标的）
        3. 返回 (写入条数, 失败symbols列表, 最终同步数, 失败原因dict)

        force_refetch=True: 跳过"已同步"检查，强制重新拉取所有传入的 symbols。
        未覆盖的 symbols 由外层 _run_repair 循环补齐。
        """
        from app.data_sources.normalizer import strip_market_prefix

        if not symbols:
            from app.utils.basicinfo_db import get_stock_basic_db
            symbols = get_stock_basic_db().market_all_codes(status="active")
        if not symbols:
            logger.warning(f"[同步] {self.source.name} 获取股票列表失败")
            return 0, [], 0, {}

        total_symbols = len(symbols)

        # 目标交易日: 15:05 后 → 今天, 之前 → 上一个交易日
        now_cn = datetime.now(TZ_CN)
        today_str = now_cn.strftime("%Y-%m-%d")
        if now_cn.time() >= dt_time(15, 5) and is_trading_day(today_str):
            target_td = today_str
        else:
            target_td = prev_trading_day(today_str)

        # 15m 最后一根 bar 的结束时间: 15:00
        bar_time = datetime.strptime(target_td, "%Y-%m-%d").replace(
            hour=15, minute=0, second=0, tzinfo=TZ_CN
        )

        pool = self.source.db_pool

        failed_reasons: dict[str, str] = {}

        # ── 确定待拉取 symbols ──
        if force_refetch:
            remaining = list(symbols)
            deleted = self._delete_symbols_bars(bar_time, remaining, tf="15m")
            if deleted > 0:
                logger.info(f"[同步] {self.source.name} 15m force_refetch: 已清除 {deleted} 条旧 bar")
        else:
            synced = self._get_synced_symbols(bar_time, tf="15m")
            remaining = [s for s in symbols if strip_market_prefix(s) not in synced]

        if not remaining:
            logger.info(f"[同步] {self.source.name} 15m 所有 {total_symbols} 只已同步")
            return 0, [], total_symbols, {}

        logger.info(f"[同步] {self.source.name} 15m 待拉取 {len(remaining)}")

        # ── 单次拉取 ──
        klines = self._fetch_15m_klines(remaining)
        if not klines:
            logger.warning(f"[同步] {self.source.name} 15m 拉取返回空数据")
            for symbol in remaining:
                failed_reasons.setdefault(symbol, "kline 返回空数据")
            final_synced = self._get_synced_symbols(bar_time, tf="15m")
            failed = [s for s in symbols if strip_market_prefix(s) not in final_synced]
            return 0, failed, len(final_synced), failed_reasons

        records = self._klines_to_records(remaining, klines, bar_time, failed_reasons)

        if not records:
            logger.info(f"[同步] {self.source.name} 15m 无有效记录")
            final_synced = self._get_synced_symbols(bar_time, tf="15m")
            failed = [s for s in symbols if strip_market_prefix(s) not in final_synced]
            return 0, failed, len(final_synced), failed_reasons

        # 首次运行: 写入前删除当日旧数据（删和写紧挨，最小化丢数据窗口）
        if is_first_run:
            deleted = self._delete_bars(bar_time, tf="15m")
            if deleted > 0:
                logger.info(f"[同步] {self.source.name} 15m 首次运行，已清除当日 {deleted} 条旧 bar")

        # 写入
        try:
            r = self._writer.bulk_write(self.source.market, records)
            total_written = r.get("inserted", 0) + r.get("skipped", 0)
        except Exception as e:
            logger.error(f"[同步] {self.source.name} 15m 写入失败: {e}")
            final_synced = self._get_synced_symbols(bar_time, tf="15m")
            failed = [s for s in symbols if strip_market_prefix(s) not in final_synced]
            return 0, failed, len(final_synced), failed_reasons

        # ── 统计 ──
        final_synced = self._get_synced_symbols(bar_time, tf="15m")
        final_count = len(final_synced)
        failed = [s for s in symbols if strip_market_prefix(s) not in final_synced]

        # 补全 failed_reasons: 没被 _klines_to_records 处理到的 symbol 也要记录原因
        for sym in failed:
            failed_reasons.setdefault(sym, "未返回 kline 数据")

        if failed:
            logger.warning(
                f"[同步] {self.source.name} 15m 完成，"
                f"写入 {total_written}，"
                f"同步 {final_count}/{total_symbols}，失败 {len(failed)}"
            )
            for sym in failed[:20]:
                logger.warning(f"  ✗ {sym}: {failed_reasons.get(sym, '未知')}")
            if len(failed) > 20:
                logger.warning(f"  ... 共 {len(failed)} 只失败")
        else:
            logger.info(
                f"[同步] {self.source.name} 15m 完成，"
                f"写入 {total_written}，"
                f"同步 {final_count}/{total_symbols}，无失败"
            )

        return total_written, failed, final_count, failed_reasons

    # ── 1D 同步: batch_quotes 单次拉取 ──────────────────

    def _sync_1d(self, symbols: list | None = None, is_first_run: bool = True,
                 force_refetch: bool = False) -> tuple[int, list[str], int, dict[str, str]]:
        """1D 同步: 单次拉取 + 写 kline 表，不操作 cn_last_update。

        - 首次运行 (is_first_run=True):  删除当日 bar → 全量拉取 → 写入
        - 后续重试 (is_first_run=False): 跳过已写入 symbols → 只补拉缺失部分
        - 返回 (写入条数, 失败symbols列表, 最终同步数, 失败原因dict)

        force_refetch=True: 跳过"已同步"检查，强制重新拉取所有传入的 symbols。
        未覆盖的 symbols 由外层 _run_repair 循环补齐。
        """
        from app.data_sources.coordinator import get_coordinator
        from app.data_sources.normalizer import strip_market_prefix

        if not symbols:
            from app.utils.basicinfo_db import get_stock_basic_db
            symbols = get_stock_basic_db().market_all_codes(status="active")
        if not symbols:
            logger.warning(f"[同步] {self.source.name} 获取股票列表失败")
            return 0, [], 0, {}

        coord = get_coordinator()
        total_symbols = len(symbols)

        # 日线 bar 时间: 目标交易日 00:00:00 (北京时间)
        # 17:00 后 → 今天, 08:00 前 → 上一个交易日
        now_cn = datetime.now(TZ_CN)
        today_str = now_cn.strftime("%Y-%m-%d")
        if now_cn.time() >= dt_time(17, 0) and is_trading_day(today_str):
            target_td = today_str
        else:
            target_td = prev_trading_day(today_str)
        bar_time = datetime.strptime(target_td, "%Y-%m-%d").replace(
            hour=0, minute=0, second=0, tzinfo=TZ_CN
        )

        pool = self.source.db_pool

        failed_reasons: dict[str, str] = {}

        # ── 确定待拉取 symbols ──
        if force_refetch:
            remaining = list(symbols)
            deleted = self._delete_symbols_bars(bar_time, remaining, tf="1D")
            if deleted > 0:
                logger.info(f"[同步] {self.source.name} 1D force_refetch: 已清除 {deleted} 条旧 bar")
        else:
            synced = self._get_synced_symbols(bar_time, tf="1D")
            remaining = [s for s in symbols if strip_market_prefix(s) not in synced]

        if not remaining:
            logger.info(f"[同步] {self.source.name} 1D 所有 {total_symbols} 只已同步")
            return 0, [], total_symbols, {}

        logger.info(f"[同步] {self.source.name} 1D 待拉取 {len(remaining)}")

        # ── 单次拉取 ──
        quotes = coord.coordinate_batch_quotes(
            symbols=remaining,
            market=self.source.market,
            timeout=float(_BATCH_TIMEOUT),
        )

        if not quotes:
            logger.warning(f"[同步] {self.source.name} 1D 拉取返回空数据")
            for symbol in remaining:
                failed_reasons.setdefault(symbol, "provider 返回空数据")
            final_synced = self._get_synced_symbols(bar_time, tf="1D")
            failed = [s for s in symbols if strip_market_prefix(s) not in final_synced]
            return 0, failed, len(final_synced), failed_reasons

        # List[Dict] → 按 symbol 索引
        quote_map = {q["symbol"]: q for q in quotes if q.get("symbol")}

        # 转换
        records: list[dict] = []
        for symbol in remaining:
            quote = quote_map.get(symbol)
            if not quote:
                quote = quote_map.get(strip_market_prefix(symbol))

            if not quote:
                failed_reasons.setdefault(symbol, "无行情数据")
                continue

            ohlcv = self._parse_ohlcv(quote)
            if ohlcv is None:
                failed_reasons.setdefault(symbol, "OHLCV 无效(停牌/退市/价格异常)")
                continue
            o, h, l, c, v = ohlcv

            records.append({
                "symbol": strip_market_prefix(symbol),
                "timeframe": "1D",
                "time": bar_time,
                "open": o, "high": h, "low": l, "close": c, "volume": v,
            })

        if not records:
            logger.info(f"[同步] {self.source.name} 1D 无有效记录")
            final_synced = self._get_synced_symbols(bar_time, tf="1D")
            failed = [s for s in symbols if strip_market_prefix(s) not in final_synced]
            return 0, failed, len(final_synced), failed_reasons

        # 首次运行: 写入前删除当日旧数据
        if is_first_run:
            deleted = self._delete_bars(bar_time, tf="1D")
            if deleted > 0:
                logger.info(f"[同步] {self.source.name} 1D 首次运行，已清除当日 {deleted} 条旧 bar")

        # 写入
        try:
            r = self._writer.bulk_write(self.source.market, records)
            total_written = r.get("inserted", 0) + r.get("skipped", 0)
        except Exception as e:
            logger.error(f"[同步] {self.source.name} 1D 写入失败: {e}")
            final_synced = self._get_synced_symbols(bar_time, tf="1D")
            failed = [s for s in symbols if strip_market_prefix(s) not in final_synced]
            return 0, failed, len(final_synced), failed_reasons

        # ── 统计 ──
        final_synced = self._get_synced_symbols(bar_time, tf="1D")
        final_count = len(final_synced)
        failed = [s for s in symbols if strip_market_prefix(s) not in final_synced]

        # 补全 failed_reasons: 没被处理到的 symbol 也要记录原因
        for sym in failed:
            failed_reasons.setdefault(sym, "未返回行情数据")

        if failed:
            logger.warning(
                f"[同步] {self.source.name} 1D 完成，"
                f"写入 {total_written}，"
                f"同步 {final_count}/{total_symbols}，失败 {len(failed)}"
            )
            for sym in failed[:20]:
                logger.warning(f"  ✗ {sym}: {failed_reasons.get(sym, '未知')}")
            if len(failed) > 20:
                logger.warning(f"  ... 共 {len(failed)} 只失败")
        else:
            logger.info(
                f"[同步] {self.source.name} 1D 完成，"
                f"写入 {total_written}，"
                f"同步 {final_count}/{total_symbols}，无失败"
            )

        return total_written, failed, final_count, failed_reasons

    # ── 通用辅助方法 ──────────────────

    def _kline_table(self, bar_time: datetime, tf: str = "1D") -> str:
        """返回 kline_{tf}_{年} 表名（加引号保留大小写）。"""
        return f'"kline_{tf}_{bar_time.year}"'

    def _delete_bars(self, bar_time: datetime, tf: str = "1D") -> int:
        """删除指定日期的 bar 数据，返回删除条数。按日期匹配，不依赖具体时间点。"""
        table = self._kline_table(bar_time, tf=tf)
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
            logger.warning(f"[同步] {self.source.name} {tf} 清除旧 bar 失败 (可忽略): {e}")
            return 0

    def _delete_symbols_bars(self, bar_time: datetime, symbols: list[str], tf: str = "1D") -> int:
        """删除指定日期+指定 symbols 的 bar 数据，返回删除条数。

        用于 force_refetch: 先删旧记录，再重新拉取，确保修复状态准确。
        注意: kline 表存的是 strip 后的代码，入参加了前缀需先 strip。
        """
        from app.data_sources.normalizer import strip_market_prefix
        table = self._kline_table(bar_time, tf=tf)
        naive_bar_time = bar_time.replace(tzinfo=None) if bar_time.tzinfo else bar_time
        clean_symbols = [strip_market_prefix(s) for s in symbols]
        try:
            mgr = get_market_db_manager()
            pool = mgr._get_pool(self.source.db_pool)
            with pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(f"""
                        DELETE FROM {table}
                        WHERE time = %s AND symbol = ANY(%s)
                    """, (naive_bar_time, clean_symbols))
                    deleted = cur.rowcount
                    conn.commit()
                    return deleted
        except Exception as e:
            logger.warning(f"[同步] {self.source.name} {tf} 清除旧 bar 失败 (可忽略): {e}")
            return 0

    def _get_synced_symbols(self, bar_time: datetime, tf: str = "1D") -> set[str]:
        """查询指定 bar_time 已写入的 symbols 集合。

        1D: 检查 bar_time (00:00) 对应的那条 bar
        15m: 检查 bar_time (15:00, 最后一根 bar) 对应的那条 bar
        """
        table = self._kline_table(bar_time, tf=tf)
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
            logger.warning(f"[同步] {self.source.name} {tf} 查询已同步 symbols 失败: {e}")
            return set()



# ================================================================
# 预定义数据源实例
# ================================================================

stock_daily_k = BackfillDB(BackfillSource(
    name="stock_daily_k", market="CNStock", timeframe="15m",
))


# ================================================================
# 统一同步器 — threading.Timer 自同步，15m / 1D 各自独立
# ================================================================
#
# 设计模式: 与 SectorHistoryScheduler 一致
#   - 每个任务一个 threading.Timer，到期 → 执行 → 自同步下次
#   - 不用 while-loop + sleep，避免线程卡死无法恢复
#   - 进程启动后延迟 _INITIAL_DELAY 秒再执行首次（等 DB/依赖就绪）
#   - 非交易日不执行，跳到下一个交易日
#   - 未完成的任务（re）每 120s 自动重试，循环<10次
#
# 同步协议:
#   正常退出 = 完成 + 下一次同步任务
#   15m 触发时间: 15:05, 1D 触发时间: 17:00
#
#   启动:
#     盘前 (0:00~9:14:59) → date(db最后时间) >= 前一交易日 → 正常退出
#     盘后 (15:01~23:59:59) → date(db最后时间) == 当前交易日 → 正常退出
#     盘中或其它 → 正常退出
#     否则 → 等待300s后核心启动
#
#   任务流程:
#     读 cn_last_update 最后时间 > 前一交易日 15:05 / 17:00
#       → 全新拉取 → 全部成功写 status=ok 正常退出, 否则写 status=re + report
#     读 cn_last_update 最后时间 <= 前一交易日 15:05 / 17:00 且 status=re
#       → report 修复 → 循环<10次:
#         全部完成 → status=ok 正常退出
#         部分完成(本次修复>0) → 修改 report + 等待120s
#         未完成(本次修复=0) → 完成度>90% → status=ok 正常退出
#         否则 → status=error 正常退出
#

_INITIAL_DELAY = 300          # 进程启动后首次执行延迟（秒）— 等待300s后核心启动
_RETRY_INTERVAL = 120         # 修复轮次间等待（秒）— 每次修复等待120s
_MIN_DELAY = 30               # 最小同步延迟（秒），防止 0 延迟

_timers: dict[str, threading.Timer] = {}
_running = False
_MAX_REPAIR_ATTEMPTS = 9                 # 循环<10次修复（初始1次 + 最多9次重试），超过标记 error 退出


def _next_trigger_time(task: str) -> datetime:
    """返回指定任务的下次触发时间，跳过非交易日。

    task: "15m" → 15:05, "1D" → 17:00
    """
    now = datetime.now(TZ_CN)
    today_str = now.strftime("%Y-%m-%d")
    trigger_h, trigger_m = (15, 5) if task == "15m" else (17, 0)

    # 今天是交易日且还没到触发时间 → 今天
    if is_trading_day(today_str) and now.time() < dt_time(trigger_h, trigger_m):
        return datetime(now.year, now.month, now.day, trigger_h, trigger_m, 0, tzinfo=TZ_CN)

    # 找下一个交易日
    next_td = next_trading_day(today_str)
    dt_obj = datetime.strptime(next_td, "%Y-%m-%d")
    return datetime(dt_obj.year, dt_obj.month, dt_obj.day, trigger_h, trigger_m, 0, tzinfo=TZ_CN)


def _extract_failed_symbols_from_report(report_text: str) -> list[str]:
    """从 cn_last_update.report 中提取失败标的代码列表。

    report 格式示例:
      "已同步 4900/5000; 失败原因: 无kline数据(50); 失败标的(50): 000001,000002,..."
    """
    if not report_text:
        return []
    m = _re.search(r"失败标的\(\d+\):\s*([^\s;]+)", report_text)
    if m:
        return [s.strip() for s in m.group(1).split(",") if s.strip()]
    return []


def _compute_is_update(task: str, last_bar_time: datetime) -> bool:
    """db 里记录的日期是否早于当前应同步的目标交易日。

    目标交易日的计算与 _sync_15m / _sync_1d 完全一致:
      15m: 15:05 后 → 今天, 否则 → 上一个交易日
      1D:  17:00 后 → 今天, 否则 → 上一个交易日
    """
    now = datetime.now(TZ_CN)
    today_str = now.strftime("%Y-%m-%d")

    if task == "15m":
        if now.time() >= dt_time(15, 5) and is_trading_day(today_str):
            target_td = today_str
        else:
            target_td = prev_trading_day(today_str)
    elif task == "1D":
        if now.time() >= dt_time(17, 0) and is_trading_day(today_str):
            target_td = today_str
        else:
            target_td = prev_trading_day(today_str)
    else:
        return True

    db_date = (last_bar_time.astimezone(TZ_CN).strftime("%Y-%m-%d")
               if last_bar_time.tzinfo
               else last_bar_time.strftime("%Y-%m-%d"))
    return db_date < target_td

def _run_post_script(task: str):
    import sys as _sys

    script_dir = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))), "scripts")
    script_map = {"15m": "after_15m.py", "1D": "after_1d.py"}
    script_name = script_map.get(task)
    if not script_name:
        return
    script_path = _os.path.join(script_dir, script_name)
    if not _os.path.isfile(script_path):
        logger.warning(f"[同步] {task} 批处理脚本不存在: {script_path}")
        return
    try:
        subprocess.Popen(
            [_sys.executable, script_path],
            cwd=script_dir,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        logger.info(f"[同步] {task} 已启动批处理脚本: {script_path}")
    except Exception as e:
        logger.error(f"[同步] {task} 批处理脚本执行失败: {e}")


def _run_task(task: str):
    """执行一次同步，按同步协议决定后续动作。

    设计说明:
      is_update = ((当前时间 - (date(db最后时间)+cutoff)) > (下一交易日 - 当前交易日))
      15m cutoff=15:05, 1D cutoff=17:00

      if(is_update) → 全新拉取:
        启动全新拉取(是正常完成){是-->写新记录insert到db+全部成功写status=ok;正常退出}else{写新记录insert到db+report+status=re}
        -->等待120s-->
      elif(status==re) → 修复循环:
        for<10次{
          启动report修复(读取report中代码进行远端拉取):
          全部完成{是-->修改status=ok;正常退出};
          部分完成(本次修复>0){是-->删除report已写入的代码 --> 等待120s};
          未完成(本次修复=0){是-->(完成度>90%){是-->修改status=ok;正常退出}else{修改status=error;正常退出}
        }
        (完成度>90%){是-->修改status=ok;正常退出;}else{修改status=error;正常退出}
      else → 正常退出
    """
    global _running
    if not _running:
        return

    try:
        # 盘中不执行
        now = datetime.now(TZ_CN)
        today_str = now.strftime("%Y-%m-%d")
        if is_trading_day(today_str) and dt_time(9, 15) <= now.time() <= dt_time(15, 0, 59):
            logger.info(f"[同步] {task} 盘中，正常退出")
            _schedule_next(task, _next_trigger_time(task))
            return

        doc = _get_last_update(task, pool_name="CNStock")
        last_bar_time = _parse_db_timestamp(doc.get("last_bar_time")) if doc else None
        last_status = doc.get("status", "") if doc else ""

        # 无记录 → 全新拉取
        if not last_bar_time:
            final_status = _run_fresh_pull(task, doc, last_status)
            return final_status
        # 计算 is_update
        is_update = _compute_is_update(task, last_bar_time)
        if is_update:
            # is_update → 全新拉取
            final_status = _run_fresh_pull(task, doc, last_status)
        elif last_status == "re" and last_bar_time:
            # status==re → 修复循环
            final_status = _run_repair(task, doc, last_status)
        else:
            # 已是最新，无需操作
            logger.info(f"[同步] {task} is_update={is_update}, status={last_status}, 正常退出")
            _schedule_next(task, _next_trigger_time(task))
            final_status = None

        # 统一出口: status=ok 时触发批处理脚本
        if final_status == "ok":
            _run_post_script(task)

    except Exception as e:
        logger.error(f"[同步] {task} 异常: {e}", exc_info=True)
        _schedule_next(task, delay_seconds=_RETRY_INTERVAL)


def _run_fresh_pull(task: str, doc: dict | None, last_status: str):
    """全新拉取: 全部成功 → status=ok 正常退出; 否则 → status=re + report，等待120s后进入修复循环。"""
    logger.info(f"[同步] {task} 全新拉取 (上次状态={last_status or '无记录'})")
    result = stock_daily_k.run_once(task)
    written = result.get("written", 0)
    logger.info(f"[同步] {task} 本轮写入: {written}")

    doc = _get_last_update(task, pool_name="CNStock")
    if not doc:
        # 无记录 → 同步下一个交易日
        _schedule_next(task, _next_trigger_time(task))
        return "unknown"

    synced = doc.get("synced_count") or 0
    failed = doc.get("failed_count") or 0
    written = doc.get("written_count") or 0
    sync_rate = (synced - failed) / synced if synced > 0 else 0
    status = doc.get("status", "ok")
    lbt = _parse_db_timestamp(doc.get("last_bar_time"))
    logger.info(f"[同步] {task} 进度: 写入{written}/同步{synced} ({sync_rate:.0%}), 失败 {failed}, status={status}")

    if status == "ok":
        # 全部成功 → 正常退出，同步下一个交易日
        logger.info(f"[同步] {task} 全新拉取完成 (status=ok), 正常退出")
        _schedule_next(task, _next_trigger_time(task))
    else:
        # 未全部完成 → status=re + report（run_once 内部已写），等待120s后进入修复循环
        logger.info(f"[同步] {task} 全新拉取未全部完成 (status={status}), {_RETRY_INTERVAL}s 后进入修复循环")
        _schedule_next(task, delay_seconds=_RETRY_INTERVAL)

    return status


def _run_repair(task: str, doc: dict | None, last_status: str):
    """report 修复流程（同步器控制循环）。

    设计说明:
      for<10次 {
        启动report修复(读取report中代码进行远端拉取):
        全部完成{是-->修改status=ok;正常退出};
        部分完成(本次修复>0){是-->删除report已写入的代码 --> 等待120s};
        未完成(本次修复=0){是-->(完成度>90%){是-->修改status=ok;正常退出}else{修改status=error;正常退出}
      }
      (完成度>90%){是-->修改status=ok;正常退出;}else{修改status=error;正常退出}

    error 是终态，不自动重试 — 正常退出，等下个触发时间重新评估。
    """
    if last_status == "error":
        logger.info(f"[同步] {task} status=error (终态), 不重试, 正常退出")
        _schedule_next(task, _next_trigger_time(task))
        return "error"
    if last_status != "re":
        # 无记录等异常状态 → 视为需要全新拉取（兜底）
        logger.info(f"[同步] {task} 上次 status={last_status or '无记录'}, 尝试全新拉取")
        return _run_fresh_pull(task, doc, last_status)

    synced_total = doc.get("synced_count") or 0
    failed = doc.get("failed_count") or 0
    written_db = doc.get("written_count") or 0

    # 已全部完成 → status=ok 正常退出
    if failed == 0 and synced_total > 0:
        logger.info(f"[同步] {task} 修复: 已全部完成, 正常退出")
        _schedule_next(task, _next_trigger_time(task))
        return "ok"

    logger.info(f"[同步] {task} 启动修复 (写入{written_db}/同步{synced_total}, 失败 {failed})")

    # 从 report 中提取失败标的
    report_text = doc.get("report", "") or ""
    failed_symbols = _extract_failed_symbols_from_report(report_text)

    if not failed_symbols:
        # report 中无失败标的 → 无法修复，判断完成度
        sync_rate = (synced_total - failed) / synced_total if synced_total > 0 else 0
        if sync_rate > 0.9:
            logger.info(f"[同步] {task} 无失败标的且完成度 {sync_rate:.0%} > 90%, status=ok, 正常退出")
            lbt = _parse_db_timestamp(doc.get("last_bar_time"))
            if lbt:
                _update_record(task, lbt, status="ok", pool_name="CNStock")
            _schedule_next(task, _next_trigger_time(task))
            return "ok"
        else:
            logger.info(f"[同步] {task} 无失败标的且完成度 {sync_rate:.0%} <= 90%, status=error, 正常退出")
            lbt = _parse_db_timestamp(doc.get("last_bar_time"))
            if lbt:
                _update_record(task, lbt, status="error", pool_name="CNStock")
            _schedule_next(task, _next_trigger_time(task))
            return "error"

    # 修复循环: for<10次
    for attempt in range(1, _MAX_REPAIR_ATTEMPTS + 1):
        logger.info(f"[同步] {task} 修复第 {attempt}/{_MAX_REPAIR_ATTEMPTS} 轮, 重拉 {len(failed_symbols)} 只失败标的")

        # 读取当前 synced_count（run_once 会覆盖，需要保存）
        doc_before = _get_last_update(task, pool_name="CNStock")
        saved_synced_count = (doc_before.get("synced_count") or synced_total) if doc_before else synced_total

        result = stock_daily_k.run_once(task, symbols=failed_symbols, skip_repair=True,
                                        force_refetch=True)

        # run_once 内部会设置 synced_count=len(failed_symbols)，恢复为总股票数
        doc_after = _get_last_update(task, pool_name="CNStock")
        if doc_after and (doc_after.get("synced_count") or 0) != saved_synced_count:
            lbt = _parse_db_timestamp(doc_after.get("last_bar_time"))
            if lbt:
                _update_record(task, lbt, synced_count=saved_synced_count, pool_name="CNStock")

        # 重新读取修复后的状态
        doc = _get_last_update(task, pool_name="CNStock")
        if not doc:
            _schedule_next(task, _next_trigger_time(task))
            return "unknown"

        synced = doc.get("synced_count") or saved_synced_count
        current_failed = doc.get("failed_count") or 0
        current_written = doc.get("written_count") or 0
        status = doc.get("status", "")
        lbt = _parse_db_timestamp(doc.get("last_bar_time"))

        # ── 合并 report: 本次 run_once 的失败 + 不在本次 batch 中的之前已知失败 ──
        # run_once 只处理了 failed_symbols 子集，report 中只有该子集的失败。
        # 不在本次 batch 中的 symbols（已跳过/未知）如果也没写入 kline 表，
        # 必须保留在 report 中，确保下次修复循环能继续处理。
        new_report_text = doc.get("report", "") or ""
        new_failed_set = set(_extract_failed_symbols_from_report(new_report_text))
        # 读取 kline 表确认哪些 symbols 实际已写入
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
                final_synced = set()
        else:
            final_synced = set()
        from app.data_sources.normalizer import strip_market_prefix
        # 不在本次 batch 中、且不在 kline 表中的 symbols → 未写入，必须保留
        for prev_sym in failed_symbols:
            clean = strip_market_prefix(prev_sym)
            if clean not in new_failed_set and clean not in final_synced:
                new_failed_set.add(clean)
        remaining_failed = sorted(new_failed_set)

        # 用合并后的失败列表覆盖 report
        if lbt and remaining_failed:
            report_parts = [f"已同步 {len(final_synced)}/{synced}"]
            report_parts.append(f"失败标的({len(remaining_failed)}): {','.join(remaining_failed[:50])}")
            merged_report = "; ".join(report_parts)
            _update_record(
                task, lbt, report=merged_report,
                failed_count=len(remaining_failed),
                written_count=len(final_synced), pool_name="CNStock",
            )
            current_failed = len(remaining_failed)
            current_written = len(final_synced)
        elif lbt and not remaining_failed:
            # 无失败 → 清理 report
            current_failed = 0
            current_written = len(final_synced)

        # 合并后重新计算 sync_rate
        sync_rate = (synced - current_failed) / synced if synced > 0 else 0

        logger.info(f"[同步] {task} 修复第 {attempt} 轮后: 写入{current_written}/同步{synced} ({sync_rate:.0%}), 失败 {current_failed}")

        # 全部完成 → 修改status=ok; 正常退出
        if status == "ok" or (current_failed == 0 and synced > 0):
            logger.info(f"[同步] {task} 修复第 {attempt} 轮全部完成, status=ok, 正常退出")
            if lbt and status != "ok":
                _update_record(task, lbt, status="ok", pool_name="CNStock")
            _schedule_next(task, _next_trigger_time(task))
            return "ok"

        # 本次修复>0（有进展）→ 删除report已写入的代码 --> 等待120s
        if remaining_failed and len(remaining_failed) < len(failed_symbols):
            logger.info(f"[同步] {task} 修复第 {attempt} 轮部分完成: {len(failed_symbols)}→{len(remaining_failed)}, {_RETRY_INTERVAL}s 后重试")
            failed_symbols = remaining_failed
            # 修改 report（删除已写入的代码）
            if lbt:
                _update_record(
                    task, lbt, status="re",
                    report=f"修复中: 写入{current_written}/同步{synced}, 失败 {current_failed}, 剩余失败标的({len(remaining_failed)}): {','.join(remaining_failed[:50])}",
                    synced_count=synced, failed_count=current_failed,
                    written_count=current_written, pool_name="CNStock",
                )
            _schedule_next(task, delay_seconds=_RETRY_INTERVAL)
            return "re"

        # 未完成(本次修复=0) → 完成度>90% → status=ok; 否则 → status=error
        logger.info(f"[同步] {task} 修复第 {attempt} 轮无进展 (本次修复=0)")
        if sync_rate > 0.9:
            logger.info(f"[同步] {task} 完成度 {sync_rate:.0%} > 90%, status=ok, 正常退出")
            if lbt:
                _update_record(
                    task, lbt, status="ok",
                    report=f"完成度 {sync_rate:.0%} > 90%",
                    synced_count=synced, failed_count=current_failed,
                    written_count=current_written, pool_name="CNStock",
                )
            _schedule_next(task, _next_trigger_time(task))
            return "ok"
        else:
            logger.info(f"[同步] {task} 完成度 {sync_rate:.0%} <= 90%, status=error, 正常退出")
            if lbt:
                _update_record(
                    task, lbt, status="error",
                    report=f"修复无进展: 写入{current_written}/同步{synced}, 失败 {current_failed}",
                    synced_count=synced, failed_count=current_failed,
                    written_count=current_written, pool_name="CNStock",
                )
            _schedule_next(task, _next_trigger_time(task))
            return "error"

    # 循环<10次用尽仍未完成 → 完成度>90% → status=ok; 否则 → status=error
    doc_final = _get_last_update(task, pool_name="CNStock")
    if doc_final:
        synced = doc_final.get("synced_count") or synced_total
        written_db = doc_final.get("written_count") or 0
        current_failed = doc_final.get("failed_count") or 0
        sync_rate = (synced - current_failed) / synced if synced > 0 else 0
        lbt = _parse_db_timestamp(doc_final.get("last_bar_time"))
    else:
        sync_rate = 0
        lbt = None

    if sync_rate > 0.9:
        logger.info(f"[同步] {task} 循环 {_MAX_REPAIR_ATTEMPTS} 次修复后完成度 {sync_rate:.0%} > 90%, status=ok, 正常退出")
        if lbt:
            _update_record(
                task, lbt, status="ok",
                report=f"循环 {_MAX_REPAIR_ATTEMPTS} 次修复后完成度 {sync_rate:.0%} > 90%",
                pool_name="CNStock",
            )
    else:
        logger.info(f"[同步] {task} 循环 {_MAX_REPAIR_ATTEMPTS} 次修复后完成度 {sync_rate:.0%} <= 90%, status=error, 正常退出")
        if lbt:
            _update_record(
                task, lbt, status="error",
                report=f"循环 {_MAX_REPAIR_ATTEMPTS} 次修复未完成: 写入{written_db}/同步{synced}, 失败 {current_failed}",
                synced_count=synced, failed_count=current_failed,
                written_count=written_db, pool_name="CNStock",
            )
    _schedule_next(task, _next_trigger_time(task))
    return "ok" if sync_rate > 0.9 else "error"


def _schedule_next(task: str, trigger_at: datetime = None, delay_seconds: float = None):
    """为指定任务安排下次执行。

    两种模式:
      - trigger_at: 绝对时间（datetime），计算 delay
      - delay_seconds: 相对秒数
    """
    global _running
    if not _running:
        return

    # 取消旧 timer
    old = _timers.pop(task, None)
    if old:
        old.cancel()

    if delay_seconds is not None:
        delay = max(_MIN_DELAY, delay_seconds)
    elif trigger_at is not None:
        now = datetime.now(TZ_CN)
        delay = max(_MIN_DELAY, (trigger_at - now).total_seconds())
    else:
        delay = _INITIAL_DELAY

    timer = threading.Timer(delay, _run_task, args=[task])
    timer.daemon = True
    timer.name = f"backfill-{task}"
    timer.start()
    _timers[task] = timer

    run_at = datetime.now(TZ_CN) + timedelta(seconds=delay)
    logger.info(f"[同步] {task} 下次执行: {run_at:%Y-%m-%d %H:%M:%S} (延迟 {delay:.0f}s)")


def start_scheduler():
    """启动统一同步器（幂等，重复调用安全）。

    启动协议（设计说明）:
      is_update = ((当前时间 - (date(db最后时间)+cutoff)) > (下一交易日 - 当前交易日))
      15m cutoff=15:05, 1D cutoff=17:00
      if(is_update 或 status==re) → 延迟启动 _run_task
      else → 正常退出（同步到下一触发时间）

      _run_task 内部按 is_update 区分:
        is_update → 全新拉取
        status==re → 修复循环
    """
    global _running
    if _running:
        return
    _running = True

    logger.info(f"[同步] 启动（15m@15:05 + 1D@17:00 + {_RETRY_INTERVAL}s 重试）")

    # 盘中不执行
    now = datetime.now(TZ_CN)
    today_str = now.strftime("%Y-%m-%d")
    if is_trading_day(today_str) and dt_time(9, 15) <= now.time() <= dt_time(15, 0, 59):
        logger.info("[同步] 启动检查: 盘中，正常退出")
        for task in ("15m", "1D"):
            _schedule_next(task, _next_trigger_time(task))
        return

    for task in ("15m", "1D"):
        doc = _get_last_update(task, pool_name="CNStock")
        last_bar_time = _parse_db_timestamp(doc.get("last_bar_time")) if doc else None
        last_status = doc.get("status", "") if doc else ""

        if not last_bar_time:
            # 无记录 → 需要核心启动
            logger.info(f"[同步] {task} 启动检查: 无记录，{_INITIAL_DELAY}s 后核心启动")
            _schedule_next(task, delay_seconds=_INITIAL_DELAY)
            continue

        is_update = _compute_is_update(task, last_bar_time)
        db_date = last_bar_time.astimezone(TZ_CN).strftime("%Y-%m-%d") if last_bar_time.tzinfo else last_bar_time.strftime("%Y-%m-%d")

        if is_update or last_status == "re":
            logger.info(f"[同步] {task} 启动检查: db最后时间={db_date}, is_update={is_update}, status={last_status}, {_INITIAL_DELAY}s 后核心启动")
            _schedule_next(task, delay_seconds=_INITIAL_DELAY)
        else:
            logger.info(f"[同步] {task} 启动检查: db最后时间={db_date}, is_update={is_update}, status={last_status}, 正常退出")
            _schedule_next(task, _next_trigger_time(task))

    # 启动前复权因子全量更新（交易日 6:00）
    threading.Thread(target=_schedule_adj_update, daemon=True, name="adj-factors-scheduler").start()


def _schedule_adj_update():
    """交易日 6:00 全量更新前复权因子（失败不重试）。"""
    import time as _time
    import os as _os

    while _running:
        now = datetime.now(TZ_CN)
        today_str = now.strftime("%Y-%m-%d")

        if is_trading_day(today_str):
            target = now.replace(hour=6, minute=0, second=0, microsecond=0)
            if now < target:
                wait = (target - now).total_seconds()
                logger.info(f"[复权因子] 今日交易日，等待 {wait:.0f}s 至 06:00")
                _time.sleep(wait)

            if not _running:
                break

            # 检查今天是否已更新过（文件修改时间）
            try:
                from app.data_sources.provider.adjustment import _CACHE_FILE
                if _os.path.exists(_CACHE_FILE):
                    mtime = datetime.fromtimestamp(_os.path.getmtime(_CACHE_FILE), tz=TZ_CN)
                    if mtime.strftime("%Y-%m-%d") == today_str:
                        logger.info(f"[复权因子] 今日已更新，跳过")
                        next_day = target + timedelta(days=1)
                        _time.sleep((next_day - datetime.now(TZ_CN)).total_seconds())
                        continue
            except Exception:
                pass

            try:
                from app.data_sources.provider.adjustment import update_all_factors
                count = update_all_factors()
                logger.info(f"[复权因子] 全量更新完成: {count} 只股票")
            except Exception as e:
                logger.error(f"[复权因子] 更新失败: {e}")

            # 等到下一天再检查
            next_day = target + timedelta(days=1)
            _time.sleep((next_day - datetime.now(TZ_CN)).total_seconds())
        else:
            # 非交易日，找下一个交易日 6:00
            next_td = next_trading_day(today_str)
            dt_obj = datetime.strptime(next_td, "%Y-%m-%d").replace(hour=6, minute=0, second=0, tzinfo=TZ_CN)
            wait = (dt_obj - now).total_seconds()
            logger.info(f"[复权因子] 非交易日，等待至 {next_td} 06:00 ({wait:.0f}s)")
            _time.sleep(wait)


def stop_scheduler():
    """停止同步器，取消所有待执行 timer。"""
    global _running
    _running = False
    for task, timer in list(_timers.items()):
        timer.cancel()
    _timers.clear()
    logger.info("[同步] 同步器已停止")

# -*- coding: utf-8 -*-
"""
cn_stock_ext — 指数扩展数据源（kline_index_5m 底仓的读/聚合层）

═══════════════════════════════════════════════════════════════
  🚫 方向红线（2026-10-28 固化）：本文件属 data_sources 层（最底层），
     **禁止 import app.market_cn.***——下层不得反依赖上层。
     远端取数一律走 coord_* / provider_*；本地库读一律走同层 *_local 模块
     （龙虎榜/热榜：app/data_sources/dragon_tiger_local.py）。

  设计思路（作业抄 cn_stock.py：本地优先 + 闸门 + 远端兜底）：

  1. 底仓 = kline_index_5m（5m、股、bar 结束时刻 09:35 起、含 up/down_count）。
     由 app/data_sources/cn_stock_putdb.py 盘后经 coord_index 同步；
     语义「不可回补，只能向前攒」⇒ 新鲜度判据是本模块的核心闸门。

  2. 新鲜度闸门（trading_calendar）——**远端兜底的真正作用**：
     · 锚点 fresh_anchor(as_of)：
       - as_of=None → last_finish_trading_day()（最近一个已完结交易日）
       - as_of=D    → last_finish_trading_day(D 15:00)（D 含以前最后已完结交易日）
     · 库中末条日期 < 锚点 ⇒ 判陈旧（盘后同步未跑 / 同步停摆 / 窗口右沿缺日）
       → 走 coord_index 远端兜底；命中则零外网。
     · 盘中当天未入库属设计行为：此时锚点=昨天，库有昨天即判新鲜。
     · 交易日历异常 ⇒ 保守判陈旧、回落远端（不猜日期）。

  3. 聚合：5m 为唯一底仓粒度，向上聚合 **15m/30m/1H/2H/4H/1D/1W/1M**
     （aggregate_5m_bars 是全周期聚合的唯一实现，其它周期一律由此归并）。
     · 分钟族按**交易时段槽位**归并（bar 结束时刻口径，09:35 起）：
       上午 09:30-11:30 / 下午 13:00-15:00 各自对齐 N 分钟网格；
     · 1D 按日归并（SQL GROUP BY time::date，与原 index._kline_local_agg 同构）；
     · 1W 由 1D 按 ISO 周归并（周线 time=周一 00:00，抄 cn_stock._aggregate_weekly）；
     · 1M 由 1D 按自然月归并（月线 time=月初）。
     · 1m 无 5m 底仓可下钻 ⇒ 不聚合，远端 mootdx 原生直拉即终点。

  4. 兜底 = coord_index("index_kline") 统 kind + timeframe 参数
     （对齐 coordinator 的 fetch_kline(symbol, timeframe, limit) 语义）：
     · 1D → timeframe="1D"（深度 ~800 日）；
     · 5m/15m/… → timeframe="5m"（sina≤5001 根，mootdx 价格兜底），必要时远端聚合。

  5. 出口契约（与被替换点逐字段对齐，勿漂移）：
     · index_minute()     = market_cn/auto/core/data/hub.index_minute 形态
                            （time "YYYY-MM-DD HH:MM" + up/down_count，days=交易日数）
     · aggregate_daily_df() = market_cn/index._kline_local_agg 形态
                            （DataFrame date/OHLCV，volume=手（库内股÷100），三重门控→None）
     · get_index_kline()  = market_cn/index.get_index_kline 形态（全周期聚合出口：
                            [{date, o,h,l,c,v}]，1D date="YYYY-MM-DD"、其余带时分秒）
     · index_fflow()      = hub.index_fflow 形态（kline_index_fflow，净额当日累计、元、
                            bar 起始时刻；新鲜度不过闸走 coord_index index_fflow 补当日）
     · lhb()              = hub.lhb 形态（cnd_dragon_tiger_list 名单型事件，不 OHLVC 化；
                            store 优先，空窗走 coord_pool dragon_tiger；时效纪律见其 docstring）

═══════════════════════════════════════════════════════════════
  易错点：
  - kline_index_5m 的 time 是 **bar 结束时刻**（09:35 首根），volume=股；
    aggregate_daily_df 出口 volume=手（÷100）是历史契约（test_index_local_agg 锁定），
    而 index_kline 远端各源 volume 单位不统一（mootdx=手/sina·baostock=股/
    tencent=手×100→股）——远端行按原样透传，不做静默换算（已知限度，勿"顺手统一"）。
  - 聚合分钟槽位：`(分钟数-1)//N` 归桶（bar 结束时刻），09:35/09:40/09:45 同属
    09:30-09:45 的 15m 桶；直接 `//N` 会把 09:45 错推进下一桶。
  - 新鲜度锚点比较的是**日期**（date），不是 timestamp；库 time 是 naive 北京时间。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.data_sources.normalizer import normalize_chart_timeframe
from app.utils.logger import get_logger

logger = get_logger(__name__)

TABLE = "kline_index_5m"

# 库内 9 指数（899050/399303 未落库）。白名单用于「日线本地聚合」门控——
# 名单外查库必空，提前短路直接走远端，勿按前缀猜 symbol（原 index.py 口径）。
LOCAL_AGG_INDEX_SYMBOLS = {
    "000001": "000001.SH", "000016": "000016.SH", "000300": "000300.SH",
    "000688": "000688.SH", "000852": "000852.SH", "000905": "000905.SH",
    "399001": "399001.SZ", "399005": "399005.SZ", "399006": "399006.SZ",
}

# 目标周期 ← 5m bar 数（仅语义标注；分钟族用槽位归并，不用这个乘数硬切）
_TF_LABEL = {
    "5m": "5m", "15m": "15m", "30m": "30m", "1H": "1H", "2H": "2H", "4H": "4H",
}

# 交易时段（分钟数）：bar 结束时刻 09:35..11:30 / 13:05..15:00
_AM_START, _AM_END = 9 * 60 + 30, 11 * 60 + 30
_PM_START, _PM_END = 13 * 60, 15 * 60


# ================================================================
# 符号/时间口径
# ================================================================

def to_symbol(code: str) -> str:
    """6 位指数码 → 库表 symbol（399*→.SZ / 其余→.SH，与 sync_index_daily 同键）。

    已带 .SH/.SZ 后缀的原样返回（大小写归一）。
    """
    c = str(code or "").strip()
    if not c:
        return c
    if "." in c:
        return c.upper()
    return "%s.%s" % (c, "SZ" if c.startswith("399") else "SH")


def _bar_date(bar: Dict[str, Any]) -> str:
    """bar 的日期串 "YYYY-MM-DD"（time 可为 datetime/str）。"""
    t = bar.get("time")
    if isinstance(t, datetime):
        return t.strftime("%Y-%m-%d")
    return str(t)[:10]


def _bar_dt(bar: Dict[str, Any]) -> datetime:
    """bar time → naive datetime（北京时间，库口径）。"""
    t = bar.get("time")
    if isinstance(t, datetime):
        return t.replace(tzinfo=None) if t.tzinfo else t
    s = str(t).replace("T", " ")[:19]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError("无法解析 bar time: %r" % (t,))


def _fmt_hm(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def _norm_tf(timeframe: str) -> str:
    """周期归一化。月线 "1M" 与 1 分钟 "1m" 仅差大小写，而
    normalize_chart_timeframe 查表前会 lower()（"1M"→"1m" 撞车成分钟线）
    ⇒ 此处在归一前特判月线；其余周期交回原归一器。"""
    t = (timeframe or "").strip()
    if t == "1M" or t.lower() in ("1month", "month", "mon"):
        return "1M"
    return normalize_chart_timeframe(t)


# ================================================================
# 新鲜度闸门（trading_calendar）—— 远端兜底的判据
# ================================================================

def fresh_anchor(as_of: Optional[str] = None) -> str:
    """新鲜度锚点：库中末条至少要覆盖到的交易日。

    · as_of=None → last_finish_trading_day()：最近一个**已完结**交易日
      （kline_index_5m 盘后同步，盘中当天未入库属设计行为，锚点=昨天）；
    · as_of=D    → last_finish_trading_day(D 15:00)：D 含以前最后已完结交易日
      （历史回放窗口右沿；D 为周末时自动回退到上一交易日）。

    交易日历异常原样抛出，调用方保守判陈旧、回落远端。
    """
    from app.utils.trading_calendar import last_finish_trading_day
    if as_of:
        return last_finish_trading_day(ref_dt="%s 15:00:00" % str(as_of)[:10])
    return last_finish_trading_day()


def freshness_ok(last_bar: Optional[Dict[str, Any]], as_of: Optional[str] = None) -> bool:
    """末条 bar 是否新鲜（≥ fresh_anchor）。空/日历异常 ⇒ False。

    这是「本地可用」的核心判据：False 时调用方必须走 coord_index 远端兜底，
    不得把陈旧库数据当最新返回。
    """
    if not last_bar:
        return False
    try:
        anchor = fresh_anchor(as_of)
    except Exception as e:
        logger.warning("[cn_stock_ext] 交易日历不可用，保守判陈旧回落远端: %s", e)
        return False
    return _bar_date(last_bar) >= anchor


# ================================================================
# DB 直读（kline_index_5m）
# ================================================================

def _rows_to_bars(rows: List[tuple]) -> List[Dict[str, Any]]:
    """DB 行 (time,o,h,l,c,v,up,down) → bar dict 列表，time 保持 naive datetime。"""
    out = []
    for r in rows or []:
        out.append({
            "time": r[0], "open": float(r[1] or 0), "high": float(r[2] or 0),
            "low": float(r[3] or 0), "close": float(r[4] or 0),
            "volume": float(r[5] or 0),
            "up_count": r[6], "down_count": r[7],
        })
    return out


def read_db_5m(
    code: str,
    limit: Optional[int] = None,
    as_of: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """读 kline_index_5m（time 升序）。失败记 warning 返回 []（不抛）。

    limit=最近 N 根（SQL DESC LIMIT 下推）；as_of 只保留该日（含）以前。
    """
    symbol = to_symbol(code)
    sql = (
        'SELECT time, open, high, low, close, volume, up_count, down_count '
        'FROM "%s" WHERE symbol = %%s' % TABLE
    )
    params: list = [symbol]
    if as_of:
        sql += " AND time::date <= %s"
        params.append(str(as_of)[:10])
    sql += " ORDER BY time DESC"
    if limit:
        sql += " LIMIT %s"
        params.append(int(limit))
    try:
        from app.utils.db_market import get_market_db_manager
        with get_market_db_manager()._get_pool("CNStock").cursor() as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
    except Exception as e:
        logger.warning("[cn_stock_ext] %s 5m 读取失败: %s", symbol, e)
        return []
    return list(reversed(_rows_to_bars(rows)))


# ================================================================
# 聚合：5m → 15m/30m/1H/2H/4H/1D/1W
# ================================================================

def _merge_group(group: List[Dict[str, Any]], label) -> Dict[str, Any]:
    """一组 5m bar → 单根（open=首, high=最大, low=最小, close=末, volume=和）。"""
    out = {
        "time": label,
        "open": group[0]["open"],
        "high": max(b["high"] for b in group),
        "low": min(b["low"] for b in group),
        "close": group[-1]["close"],
        "volume": round(sum(b["volume"] for b in group), 2),
    }
    # up/down_count 是市场宽度快照，求和无意义 ⇒ 取组内末根（收盘口径）；
    # 1D 聚合走 SQL 路径不带这两列，此处仅分钟族多周期聚合会带上。
    for k in ("up_count", "down_count"):
        v = group[-1].get(k)
        if v is not None:
            out[k] = v
    return out


def _session_slot(dt: datetime, n_min: int) -> Optional[Tuple[str, int]]:
    """bar 结束时刻 → (session, bucket_idx)。场外（竞价等）返回 None。

    归桶公式：`(分钟数-1)//N` —— bar 结束时刻口径下，09:35/09:40/09:45 同属
    09:30-09:45 的 15m 桶（elapsed 5/10/15 → (5-1)//15=(10-1)//15=(15-1)//15=0）。
    """
    hm = dt.hour * 60 + dt.minute
    if _AM_START < hm <= _AM_END:
        return ("AM", (hm - _AM_START - 1) // n_min)
    if _PM_START < hm <= _PM_END:
        return ("PM", (hm - _PM_START - 1) // n_min)
    return None


def _bucket_label(date_str: str, session: str, idx: int, n_min: int) -> datetime:
    """桶的 bar 结束时刻 = 时段起点 + (idx+1)*N 分钟（对齐 5m bar 结束口径）。"""
    start = _AM_START if session == "AM" else _PM_START
    hm = start + (idx + 1) * n_min
    return datetime.strptime(date_str, "%Y-%m-%d").replace(hour=hm // 60, minute=hm % 60)


def aggregate_5m_bars(bars: List[Dict[str, Any]], timeframe: str) -> List[Dict[str, Any]]:
    """5m bar 列表 → 目标周期 bar 列表（升序）。全周期聚合**唯一实现**：
    5m/15m/30m/1H/2H/4H/1D/1W/1M（1m 无 5m 底仓可下钻，不在此列）。

    · 5m：原样返回（拷贝）；
    · 15m/30m/1H/2H/4H：按交易时段槽位归并，label=桶的 bar 结束时刻；
    · 1D：按日归并，label=当日 15:00；
    · 1W/1M：先归并 1D 再归周/月（1W label=ISO 周一，1M label=月初）。
    场外时刻的 bar（如 09:25 竞价）不参与分钟族归并（不属 48 槽网格）。
    """
    tf = _norm_tf(timeframe)
    if not bars:
        return []
    if tf == "5m":
        return [dict(b) for b in bars]

    if tf in ("1W", "1M"):
        daily = aggregate_5m_bars(bars, "1D")
        return aggregate_weekly(daily) if tf == "1W" else aggregate_monthly(daily)

    if tf == "1D":
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for b in bars:
            groups.setdefault(_bar_date(b), []).append(b)
        out = []
        for d in sorted(groups):
            g = sorted(groups[d], key=_bar_dt)
            out.append(_merge_group(g, datetime.strptime(d, "%Y-%m-%d").replace(hour=15)))
        return out

    n_map = {"15m": 15, "30m": 30, "1H": 60, "2H": 120, "4H": 240}
    n_min = n_map.get(tf)
    if n_min is None:
        raise ValueError("cn_stock_ext 聚合不支持的周期: %s" % timeframe)

    buckets: Dict[Tuple[str, str, int], List[Dict[str, Any]]] = {}
    for b in bars:
        dt = _bar_dt(b)
        slot = _session_slot(dt, n_min)
        if slot is None:
            continue
        d = dt.strftime("%Y-%m-%d")
        buckets.setdefault((d,) + slot, []).append(b)

    out = []
    for key in sorted(buckets, key=lambda k: (k[0], k[1], k[2])):
        d, session, idx = key
        g = sorted(buckets[key], key=_bar_dt)
        out.append(_merge_group(g, _bucket_label(d, session, idx, n_min)))
    return out


def aggregate_weekly(daily: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """日线 → ISO 周线（time=周一 00:00，抄 cn_stock._aggregate_weekly 口径）。"""
    if not daily:
        return []
    weeks: Dict[Tuple[int, int], List[Dict[str, Any]]] = {}
    for b in daily:
        dt = _bar_dt(b)
        iso = dt.isocalendar()
        weeks.setdefault((iso[0], iso[1]), []).append(b)
    out = []
    for key in sorted(weeks):
        g = sorted(weeks[key], key=_bar_dt)
        year, week = key
        monday = datetime.fromisocalendar(year, week, 1)
        out.append(_merge_group(g, monday))
    return out


def aggregate_monthly(daily: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """日线 → 月线（time=月初 00:00）。"""
    if not daily:
        return []
    months: Dict[Tuple[int, int], List[Dict[str, Any]]] = {}
    for b in daily:
        dt = _bar_dt(b)
        months.setdefault((dt.year, dt.month), []).append(b)
    out = []
    for key in sorted(months):
        g = sorted(months[key], key=_bar_dt)
        out.append(_merge_group(g, datetime(key[0], key[1], 1)))
    return out


def aggregate_daily_df(code: str, days: int):
    """5m→1D 本地聚合（index._kline_local_agg 同构契约）。门控不过返回 None。

    门控（三重，全过才出 DataFrame）：
      ① 白名单 —— 库内只有 9 指数；
      ② 深度 —— 聚合行数 ≥ days；
      ③ 新鲜度 —— 末条日期 ≥ fresh_anchor()（trading_calendar）；
    DB/日历异常 ⇒ None（保守回落，调用方走远端）。

    Returns:
        pandas.DataFrame [date, open, high, low, close, volume]（升序）| None
        date="YYYY-MM-DD"；volume=手（库内股÷100，历史契约，test 锁定）。
    """
    symbol = LOCAL_AGG_INDEX_SYMBOLS.get(str(code).strip())
    if symbol is None:
        return None
    try:
        from app.utils.db_market import get_market_db_manager
        with get_market_db_manager()._get_pool("CNStock").cursor() as cur:
            cur.execute(
                """
                SELECT time::date AS d,
                       (array_agg(open  ORDER BY time))[1]    AS open,
                       max(high)                              AS high,
                       min(low)                               AS low,
                       (array_agg(close ORDER BY time DESC))[1] AS close,
                       sum(volume)                            AS volume
                FROM "kline_index_5m"
                WHERE symbol = %s
                GROUP BY time::date
                ORDER BY d DESC
                LIMIT %s
                """,
                (symbol, days),
            )
            rows = cur.fetchall()
    except Exception as e:
        logger.warning("[cn_stock_ext] 日K线聚合失败(%s): %s", code, e)
        return None

    # ② 深度
    if not rows or len(rows) < days:
        return None

    # ③ 新鲜度: 末条（DESC 首行）≥ trading_calendar 锚点
    if not freshness_ok({"time": rows[0][0]}):
        logger.info("[cn_stock_ext] 日K线 %s 末条 %s 未过新鲜度锚点，回落远端",
                    code, rows[0][0])
        return None

    out = [
        {"date": str(r[0]), "open": float(r[1]), "high": float(r[2]),
         "low": float(r[3]), "close": float(r[4]),
         "volume": float(r[5]) / 100.0}   # 股 → 手
        for r in reversed(rows)
    ]
    logger.info("[cn_stock_ext] 日K线 %s: %d 条（kline_index_5m 聚合，零外网）", code, len(out))
    import pandas as pd
    return pd.DataFrame(out)


# ================================================================
# coord_index 远端兜底
# ================================================================

def _coord_kline(code: str, timeframe: str, limit: int) -> List[Dict[str, Any]]:
    """coord_index index_kline（timeframe 统 kind）→ rows 列表。

    1D rows: [{date, open, high, low, close, volume}]; 5m rows: [{time, ...}]。
    """
    from app.data_sources.coord_index import coordinate
    out = coordinate("index_kline", code=str(code).split(".")[0],
                     timeframe=timeframe, limit=int(limit))
    if not (out.get("ok") and (out.get("data") or {}).get("rows")):
        logger.warning("[cn_stock_ext] 远端 %s 兜底失败(%s): %s",
                       timeframe, code, out.get("error", ""))
        return []
    rows = out["data"]["rows"]
    logger.info("[cn_stock_ext] 远端 %s %s: %d 条 (source=%s)",
                timeframe, code, len(rows), out.get("source", ""))
    return rows


# ================================================================
# 对外接口
# ================================================================

def index_minute(code: str = "000300", days: int = 800,
                 as_of: Optional[str] = None) -> List[Dict[str, Any]]:
    """指数 5m K线（hub.index_minute 同构契约）。

    Returns:
        list[dict]: [{time, open, high, low, close, volume, up_count, down_count}]
                    time="YYYY-MM-DD HH:MM"（bar 结束时刻），升序；volume=股。

    days=**交易日数**（不是 bar 行数）。as_of: 只返回该交易日（含）以前。
    本地 DB 优先；**新鲜度不过闸**（trading_calendar 锚点）⇒ coord_index 远端兜底。
    """
    bars = read_db_5m(code, as_of=as_of)
    if not bars or not freshness_ok(bars[-1], as_of=as_of):
        remote = _coord_kline(code, "5m", max(int(days) * 48, 200))
        remote = [r for r in remote
                  if not as_of or str(r.get("time", ""))[:10] <= str(as_of)[:10]]
        bars = _remote_5m_to_bars(remote)
    out = []
    for b in bars:
        row = {
            "time": _fmt_hm(_bar_dt(b)), "open": b["open"], "high": b["high"],
            "low": b["low"], "close": b["close"], "volume": b["volume"],
            "up_count": b.get("up_count"), "down_count": b.get("down_count"),
        }
        out.append(row)
    return _tail_trading_days(out, days)


def _remote_5m_to_bars(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """coord 5m 行 → bar dict（time 统一 datetime，升序）。"""
    out = []
    for r in rows or []:
        try:
            out.append({
                "time": _bar_dt(r), "open": float(r.get("open", 0)),
                "high": float(r.get("high", 0)), "low": float(r.get("low", 0)),
                "close": float(r.get("close", 0)), "volume": float(r.get("volume", 0)),
                "up_count": r.get("up_count"), "down_count": r.get("down_count"),
            })
        except (ValueError, TypeError):
            continue
    out.sort(key=lambda b: b["time"])
    return out


def _tail_trading_days(bars: List[Dict[str, Any]], days: int) -> List[Dict[str, Any]]:
    """按**交易日**取尾部 N 日（非「最后 N 根 bar」；hub._tail_trading_days 同口径）。"""
    if not days or days <= 0:
        return bars
    dates: List[str] = []
    for b in bars:
        d = str(b.get("time", ""))[:10]
        if not dates or dates[-1] != d:
            dates.append(d)
    if len(dates) <= days:
        return bars
    cut = dates[-days]
    return [b for b in bars if str(b.get("time", ""))[:10] >= cut]


def get_index_kline(code: str = "000001", timeframe: str = "1D", limit: int = 200,
                    as_of: Optional[str] = None) -> List[Dict[str, Any]]:
    """指数 K线统一出口（扩展+聚合+兜底）——**全周期聚合的唯一入口**。

    流程（抄 cn_stock.get_kline 的本地优先骨架）：
      1) 本地 kline_index_5m 直读（5m）或聚合（15m/30m/1H/2H/4H/1D/1W/1M，
         聚合实现见 aggregate_5m_bars —— 其它周期一律由此归并，勿另写聚合）；
      2) 闸门：白名单（日线族强制）∧ 深度 ≥ limit ∧ 新鲜度 ≥ fresh_anchor(as_of)；
      3) 不过闸 → coord_index 兜底（1m 无 5m 底仓，只走远端）。

    Returns:
        [{date, open, high, low, close, volume}, ...] date 升序
        （1D: date="YYYY-MM-DD"；其余周期: date="YYYY-MM-DD HH:MM:SS"，
        与 market_cn.index.get_index_kline 历史契约一致）。
        volume 为各源原口径（本地=股；远端按原样透传，见文件头易错点）。
    """
    tf = _norm_tf(timeframe)
    limit = max(int(limit or 200), 1)
    daily_like = tf in ("1D", "1W", "1M")

    # ── 本地路径（1m 无 5m 底仓可下钻，跳过）──
    if tf != "1m":
        need_bars = _tf_5m_bars(tf, limit)
        local = read_db_5m(code, limit=need_bars + 48, as_of=as_of)
        if local and freshness_ok(local[-1], as_of=as_of):
            if not daily_like or str(code).split(".")[0] in LOCAL_AGG_INDEX_SYMBOLS:
                agg = aggregate_5m_bars(local, tf)
                if len(agg) >= limit:
                    return _fmt_exit_rows(agg, tf, limit, as_of)

    # ── 远端兜底 ──
    if daily_like:
        bars = _remote_daily_to_bars(_coord_kline(code, "1D", _tf_1d_days(tf, limit)))
    elif tf == "1m":
        # 1m 不可由 5m 聚合（coord 亦无 1m kind），mootdx 原生直拉即终点
        bars = _remote_tdx_minute(code, "1m", limit)
    else:
        bars = _remote_5m_to_bars(_coord_kline(code, "5m", _tf_5m_bars(tf, limit) + 48))
    agg = bars if tf == "1m" else aggregate_5m_bars(bars, tf)
    return _fmt_exit_rows(agg, tf, limit, as_of)


def _tf_5m_bars(tf: str, limit: int) -> int:
    """目标周期 limit 根 ≈ 多少根 5m（读库/远端拉取量估算）。"""
    per = {"5m": 1, "15m": 3, "30m": 6, "1H": 12, "2H": 24, "4H": 48}.get(tf)
    if per:
        return limit * per
    days = _tf_1d_days(tf, limit)
    return days * 48


def _tf_1d_days(tf: str, limit: int) -> int:
    """日线族 limit 根 ≈ 多少个交易日（1W≈5 日/周，1M≈21 日/月，留冗余）。"""
    return {"1D": limit, "1W": limit * 7, "1M": limit * 31}.get(tf, limit)


def _remote_daily_to_bars(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """coord 1D 行 [{date,...}] → bar dict（time 统一 datetime，升序）。"""
    out = []
    for r in rows or []:
        try:
            out.append({
                "time": _bar_dt({"time": r.get("date", r.get("time"))}),
                "open": float(r.get("open", 0)), "high": float(r.get("high", 0)),
                "low": float(r.get("low", 0)), "close": float(r.get("close", 0)),
                "volume": float(r.get("volume", 0)),
            })
        except (ValueError, TypeError):
            continue
    out.sort(key=lambda b: b["time"])
    return out


def _fmt_exit_rows(bars: List[Dict[str, Any]], tf: str,
                   limit: int, as_of: Optional[str]) -> List[Dict[str, Any]]:
    """聚合 bar → 出口行（date 形状对齐 market_cn.get_index_kline 历史契约）。"""
    out = []
    for b in bars:
        dt = _bar_dt(b)
        d = dt.strftime("%Y-%m-%d") if tf == "1D" else dt.strftime("%Y-%m-%d %H:%M:%S")
        if as_of and d[:10] > str(as_of)[:10]:
            continue
        out.append({"date": d, "open": b["open"], "high": b["high"],
                    "low": b["low"], "close": b["close"], "volume": b["volume"]})
    return out[-limit:]


def _remote_tdx_minute(code: str, tf: str, limit: int) -> List[Dict[str, Any]]:
    """1m 原生直拉（mootdx TDX frequency=7）——1m 不可由 5m 聚合，兜底即终点。

    与 market_cn.index.get_index_kline 旧路径同源同口径（date 保留时分秒）。
    """
    from app.utils.mootdx_client import get_client
    cli = get_client()
    if cli is None:
        logger.warning("[cn_stock_ext] mootdx 不可用，1m %s 无源", code)
        return []
    try:
        df = cli.index_bars(symbol=str(code).split(".")[0], frequency=7,
                            start=0, offset=min(limit, 800))
        if df is None or df.empty:
            return []
        rows = []
        for _, r in df.iterrows():
            rows.append({
                "time": str(r.get("datetime", r.get("date", "")))[:19],
                "open": float(r.get("open", 0)), "high": float(r.get("high", 0)),
                "low": float(r.get("low", 0)), "close": float(r.get("close", 0)),
                "volume": float(r.get("vol", r.get("volume", 0)) or 0),
            })
        return _remote_5m_to_bars(rows)
    except Exception as e:
        logger.warning("[cn_stock_ext] 1m %s mootdx 失败: %s", code, e)
        return []


def get_index_daily(code: str = "000001", days: int = 200,
                    as_of: Optional[str] = None) -> List[Dict[str, Any]]:
    """指数日线出口：本地聚合（三重门控）→ coord_index 兜底。

    Returns:
        [{date, open, high, low, close, volume}, ...] date 升序；失败 []。
    """
    df = aggregate_daily_df(code, days)
    if df is not None:
        return df.to_dict("records")
    remote = _coord_kline(code, "1D", days)
    if as_of:
        remote = [r for r in remote
                  if str(r.get("date", r.get("time", "")))[:10] <= str(as_of)[:10]]
    return remote[-days:]


# ================================================================
# 指数资金流（kline_index_fflow — hub.index_fflow 同构契约）
# ================================================================

FFLOW_TABLE = "kline_index_fflow"
FFLOW_NET_COLS = ("main_net", "small_net", "mid_net", "big_net", "super_net")


def read_db_fflow(code: str, as_of: Optional[str] = None) -> List[Dict[str, Any]]:
    """读 kline_index_fflow（time 升序）。失败记 warning 返回 []。

    行口径：{time: "YYYY-MM-DD HH:MM", main_net, small_net, mid_net, big_net,
    super_net}；净额=**当日累计值**（元，bar 起始时刻 09:31 起）。
    """
    symbol = to_symbol(code)
    sql = (
        'SELECT time, main_net, small_net, mid_net, big_net, super_net '
        'FROM "%s" WHERE symbol = %%s' % FFLOW_TABLE
    )
    params: list = [symbol]
    if as_of:
        sql += " AND time::date <= %s"
        params.append(str(as_of)[:10])
    sql += " ORDER BY time ASC"
    try:
        from app.utils.db_market import get_market_db_manager
        with get_market_db_manager()._get_pool("CNStock").cursor() as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
    except Exception as e:
        logger.warning("[cn_stock_ext] %s 资金流读取失败: %s", symbol, e)
        return []
    out = []
    for r in rows or []:
        t = r[0]
        row = {"time": t.strftime("%Y-%m-%d %H:%M") if isinstance(t, datetime) else str(t)[:16]}
        for i, c in enumerate(FFLOW_NET_COLS):
            row[c] = float(r[i + 1] or 0)
        out.append(row)
    return out


def _coord_fflow(code: str, limit: int = 240) -> List[Dict[str, Any]]:
    """coord_index index_fflow → 当日分钟资金流行（仅当日，断采不可回补）。"""
    from app.data_sources.coord_index import coordinate
    out = coordinate("index_fflow", code=str(code).split(".")[0], limit=int(limit))
    if not (out.get("ok") and (out.get("data") or {}).get("rows")):
        logger.warning("[cn_stock_ext] 远端资金流兜底失败(%s): %s",
                       code, out.get("error", ""))
        return []
    rows = out["data"]["rows"]
    logger.info("[cn_stock_ext] 远端资金流 %s: %d 根 (source=%s)",
                code, len(rows), out.get("source", ""))
    return rows


def index_fflow(code: str = "000300", days: int = 800,
                as_of: Optional[str] = None) -> List[Dict[str, Any]]:
    """指数大盘资金流（hub.index_fflow 同构契约）。

    Returns:
        list[dict]: [{time, main_net, small_net, mid_net, big_net, super_net}]
                    time="YYYY-MM-DD HH:MM"（bar 起始时刻），升序，净额单位元、
                    **当日累计值语义**（15:00 累计 = 日级值，特征工程用时差分）。

    days=**交易日数**。as_of: 只返回该交易日（含）以前。
    本地 DB 优先；**新鲜度不过闸**（trading_calendar 锚点）⇒ coord_index 兜底
    —— 远端仅当日 240 根（断采不可回补），故兜底只补当日缺日，历史缺口不造。
    ⚠ time 为 bar 起始时刻（09:31 起），与 index_minute 的 bar 结束时刻差 1 根，
      跨表对齐特征时注意（hub 原契约，勿改）。
    """
    local = read_db_fflow(code, as_of=as_of)
    if not local or not freshness_ok({"time": local[-1]["time"]}, as_of=as_of):
        remote = _coord_fflow(code, limit=240)
        remote = [r for r in remote
                  if not as_of or str(r.get("time", ""))[:10] <= str(as_of)[:10]]
        if remote:
            today = str(remote[-1]["time"])[:10]
            history = [r for r in local if str(r.get("time", ""))[:10] < today]
            local = history + remote
    return _tail_trading_days(local, days)


# ================================================================
# 龙虎榜（cnd_dragon_tiger_list — hub.lhb 同构契约）
# ================================================================

def lhb(trade_date: str = "", stock_code: str = "", days: int = 30) -> List[Dict[str, Any]]:
    """龙虎榜事件（名单型数据，不 OHLVC 化）。hub.lhb 同构契约。

    本地 dragon_tiger_local 优先；空窗/缺日时 coord_pool("dragon_tiger") 兜底。

    Returns:
        list[dict]: 龙虎榜事件行（含 stock_code/stock_name/trade_date/net_amount 等）；
                    无数据 []。

    🔴 发布时效纪律 (决策不可违): LHB(D) 交易所 ~17:00-17:30 才发布 → 任何 D 日
    判定 (盘后扫描 / 盘中窗口) 只允许消费 trade_date ≤ D-1 的事件, 本接口
    不代为裁剪 —— 调用方必须显式传 ≤D-1 的 trade_date/days 窗口。
    (2026-09-18 调度重排: 龙虎榜落库 17:00+重试 → 盘后扫描 17:25, 顺序上当日榜
    已可查; 但纪律仍保留 —— 同日调用返回空 vs 发布后重跑返回当日榜即隐性未来
    函数; 2026-09-12 偏差审计 A2 固化。)
    若策略真消费本通道, 须 data_needs 声明加 "lhb"。
    """
    try:
        from app.data_sources.dragon_tiger_local import query_dragon_tiger
        rows = query_dragon_tiger(trade_date=trade_date, stock_code=stock_code, days=days)
    except Exception as e:
        logger.warning("[cn_stock_ext] 龙虎榜本地读取失败: %s", e)
        rows = []
    if rows:
        return rows
    # 本地空窗 → coord_pool 兜底（store 未落库/当日尚未跑 save 时补读）
    try:
        from datetime import datetime, timedelta
        from app.data_sources.coord_pool import coordinate
        end = trade_date or datetime.now().strftime("%Y-%m-%d")
        start = trade_date or (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        out = coordinate("dragon_tiger", start_date=start, end_date=end)
        remote = (out.get("data") or {}).get("rows") if out.get("ok") else None
        if not remote:
            return []
        if stock_code:
            remote = [r for r in remote if r.get("stock_code") == stock_code]
        logger.info("[cn_stock_ext] 远端龙虎榜兜底 %s~%s: %d 条 (source=%s)",
                    start, end, len(remote), out.get("source", ""))
        return remote
    except Exception as e:
        logger.warning("[cn_stock_ext] 龙虎榜远端兜底失败: %s", e)
        return []

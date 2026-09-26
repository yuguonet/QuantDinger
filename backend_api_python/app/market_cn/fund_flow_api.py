# -*- coding: utf-8 -*-
"""
fund_flow_api.py — 通用资金流 API (2026-09-26)

覆盖三类对象 × 实时/历史:
  1. 大盘   market_fund_flow_*
  2. 个股   stock_fund_flow_*
  3. 行业/概念  sector_fund_flow_*   (board_type = industry | concept)

设计要点
--------
- **门面层**: 取数仍走既有实现 (index / tape / fund_flow_local / hub.index_fflow),
  本模块只做: 统一 schema → 历史入库 → 5~30 日裁剪 → 实时/历史读口。\n- **方向优先** (2026-09-26): 用户裁定不关心主力/全量分类, 只要流入/流出方向;\n  snapshot 汇总已清洗 688/北交所/脏量, 输出 net_yi + direction。
- **历史表** (market DB):
    qd_fund_flow_market   日度大盘 (remote EM 日线 + 我们快照可补)
    qd_fund_flow_stock    日度个股
    qd_fund_flow_sector   日度行业/概念 **快照累积** (上游只给 今日/3/5/10 日聚合,
                          没有逐日序列 → 历史只能靠每日落一行)
- **口径标注** (不编造):
    source = eastmoney | sina | snapshot_approx | hub_index_fflow
    approx = True 时为量价方向近似 (realtime_snapshot), 非主力/超大单分类
- **保留天数** KEEP_DAYS 默认 30,  clamp 到 [5, 30]; sync 时删更旧的行。

用法
----
    from app.market_cn.fund_flow_api import (
        market_fund_flow_realtime, market_fund_flow_history,
        stock_fund_flow_realtime, stock_fund_flow_history,
        sector_fund_flow_realtime, sector_fund_flow_history,
        sync_fund_flow_history,
    )

    sync_fund_flow_history(days=30)          # 取数 + 入库 + 裁剪
    market_fund_flow_history(days=10)
    stock_fund_flow_realtime("600519")
    sector_fund_flow_realtime("industry")
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from app.utils.logger import get_logger

logger = get_logger(__name__)

KEEP_DAYS_DEFAULT = 30
# 盘中派生节流: 全市场聚合 ~10s+, 勿每 60s 跑
INTRADAY_MIN_INTERVAL_SEC = 180
_LAST_INTRADAY_TS = 0.0
KEEP_DAYS_MIN = 5
KEEP_DAYS_MAX = 30

T_MARKET = "qd_fund_flow_market"
T_STOCK = "qd_fund_flow_stock"
T_SECTOR = "qd_fund_flow_sector"


# ================================================================
# 0. 表
# ================================================================

def _pool():
    from app.utils.db_market import get_market_db_manager
    mgr = get_market_db_manager()
    mgr.ensure_market_db("CNStock")
    return mgr._get_pool("CNStock")


def ensure_fund_flow_tables() -> None:
    """幂等建三张资金流历史表。"""
    with _pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {T_MARKET} (
                    trade_date  DATE PRIMARY KEY,
                    main_net    DOUBLE PRECISION,
                    super_net   DOUBLE PRECISION,
                    large_net   DOUBLE PRECISION,
                    mid_net     DOUBLE PRECISION,
                    small_net   DOUBLE PRECISION,
                    main_pct    DOUBLE PRECISION,
                    turnover    DOUBLE PRECISION,
                    inflow      DOUBLE PRECISION,
                    outflow     DOUBLE PRECISION,
                    source      TEXT,
                    raw         JSONB,
                    updated_at  TIMESTAMPTZ DEFAULT NOW()
                )""")
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {T_STOCK} (
                    trade_date  DATE NOT NULL,
                    code        VARCHAR(16) NOT NULL,
                    main_net    DOUBLE PRECISION,
                    super_net   DOUBLE PRECISION,
                    large_net   DOUBLE PRECISION,
                    mid_net     DOUBLE PRECISION,
                    small_net   DOUBLE PRECISION,
                    source      TEXT,
                    approx      BOOLEAN DEFAULT FALSE,
                    updated_at  TIMESTAMPTZ DEFAULT NOW(),
                    PRIMARY KEY (trade_date, code)
                )""")
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {T_SECTOR} (
                    trade_date   DATE NOT NULL,
                    board_type   VARCHAR(16) NOT NULL,
                    code         VARCHAR(32) NOT NULL,
                    name         TEXT,
                    change_pct   DOUBLE PRECISION,
                    main_net     DOUBLE PRECISION,
                    main_pct     DOUBLE PRECISION,
                    in_net       DOUBLE PRECISION,
                    out_net      DOUBLE PRECISION,
                    turnover     DOUBLE PRECISION,
                    lead_stock   TEXT,
                    lead_pct     DOUBLE PRECISION,
                    source       TEXT,
                    updated_at   TIMESTAMPTZ DEFAULT NOW(),
                    PRIMARY KEY (trade_date, board_type, code)
                )""")
        conn.commit()
        try:
            with _pool().connection() as conn2:
                with conn2.cursor() as cur2:
                    cur2.execute(f"ALTER TABLE {T_MARKET} ADD COLUMN IF NOT EXISTS turnover DOUBLE PRECISION")
                    cur2.execute(f"ALTER TABLE {T_MARKET} ADD COLUMN IF NOT EXISTS inflow DOUBLE PRECISION")
                    cur2.execute(f"ALTER TABLE {T_MARKET} ADD COLUMN IF NOT EXISTS outflow DOUBLE PRECISION")
                    try:
                        # 2026-09-26 由按年分表改为单表
                        cur2.execute(
                            f"CREATE INDEX IF NOT EXISTS idx_rtsnapshot_sym_time "
                            f'ON realtime_snapshot (symbol, time)')
                    except Exception:
                        pass
                conn2.commit()
        except Exception:
            pass


def _clamp_days(days: int) -> int:
    try:
        d = int(days)
    except Exception:
        d = KEEP_DAYS_DEFAULT
    return max(KEEP_DAYS_MIN, min(KEEP_DAYS_MAX, d))


def _prune(cur, table: str, keep_days: int, extra_where: str = "",
           extra_params: tuple = ()) -> int:
    cutoff = (datetime.now() - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    cur.execute(f"DELETE FROM {table} WHERE trade_date < %s{extra_where}",
                (cutoff,) + tuple(extra_params))
    return cur.rowcount


# ================================================================
# 1. 大盘
# ================================================================

def market_fund_flow_realtime(force: bool = False) -> Dict[str, Any]:
    """大盘实时资金流 (远端多源)。

    Returns:
        {trade_date, main_net, super_net, large_net, mid_net, small_net,
         main_pct, source, raw}  失败时 {source:"none", error}
    """
    from app.market_cn.index import get_market_fund_flow_realtime as _rt
    d = _rt(force=force) or {}
    if not d or d.get("source") == "none" or d.get("error"):
        return {"source": d.get("source", "none"), "error": d.get("error", "empty"),
                "trade_date": datetime.now().strftime("%Y-%m-%d")}
    return {
        "trade_date": str(d.get("date") or datetime.now().strftime("%Y-%m-%d")),
        "main_net": _f(d.get("main_net")),
        "super_net": _f(d.get("super_net")),
        "large_net": _f(d.get("large_net")),
        "mid_net": _f(d.get("mid_net")),
        "small_net": _f(d.get("small_net")),
        "main_pct": _f(d.get("main_pct")),
        "source": d.get("source") or "eastmoney",
        "raw": {k: v for k, v in d.items() if k != "raw"},
    }


def market_fund_flow_history(days: int = 30, as_of: Optional[str] = None,
                             prefer_db: bool = True) -> List[Dict[str, Any]]:
    """大盘历史日线 (默认读库; 缺则拉远端并回填)。

    Args:
        days: 5~30
        as_of: 截止日 YYYY-MM-DD (含)
        prefer_db: True=库优先; False=强制远端拉取后写库再读
    """
    days = _clamp_days(days)
    ensure_fund_flow_tables()
    if prefer_db:
        rows = _load_market_db(days, as_of)
        if rows:
            return rows
    # 优先级: 1m OHLCV → 远端 EM → snapshot 派生 (2026-09-26)
    rows = _market_from_kline1m(days, as_of)
    if not rows:
        rows = _fetch_market_daily(days)
    if not rows:
        rows = _market_from_snapshot(days, as_of)
    if rows:
        upsert_market(rows)
    return _load_market_db(days, as_of) or rows


def _fetch_market_daily(days: int) -> List[Dict[str, Any]]:
    from app.market_cn.index import get_market_fund_flow_daily
    try:
        raw = get_market_fund_flow_daily(days=days, force=True) or []
    except Exception as e:
        logger.warning("[fund_flow_api] 大盘日线远端失败: %s", e)
        return []
    out = []
    for r in raw:
        out.append({
            "trade_date": str(r.get("date") or "")[:10],
            "main_net": _f(r.get("main_net")),
            "super_net": _f(r.get("super_net")),
            "large_net": _f(r.get("large_net")),
            "mid_net": _f(r.get("mid_net")),
            "small_net": _f(r.get("small_net")),
            "main_pct": _f(r.get("main_pct")),
            "source": "eastmoney",
        })
    return [r for r in out if r["trade_date"]]


def upsert_market(rows: List[Dict[str, Any]]) -> int:
    if not rows:
        return 0
    ensure_fund_flow_tables()
    n = 0
    with _pool().connection() as conn:
        with conn.cursor() as cur:
            for r in rows:
                cur.execute(
                    f"""INSERT INTO {T_MARKET}
                        (trade_date, main_net, super_net, large_net, mid_net,
                         small_net, main_pct, turnover, inflow, outflow, source, updated_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, NOW())
                        ON CONFLICT (trade_date) DO UPDATE SET
                          main_net=EXCLUDED.main_net, super_net=EXCLUDED.super_net,
                          large_net=EXCLUDED.large_net, mid_net=EXCLUDED.mid_net,
                          small_net=EXCLUDED.small_net, main_pct=EXCLUDED.main_pct, turnover=EXCLUDED.turnover,
                          inflow=EXCLUDED.inflow, outflow=EXCLUDED.outflow,
                          source=EXCLUDED.source, updated_at=NOW()""",
                    (r["trade_date"], r.get("main_net"), r.get("super_net"),
                     r.get("large_net"), r.get("mid_net"), r.get("small_net"),
                     r.get("main_pct"),
                     r.get("turnover"), r.get("inflow"), r.get("outflow"),
                     r.get("source")),
                )
                n += 1
            _prune(cur, T_MARKET, KEEP_DAYS_DEFAULT)
        conn.commit()
    return n


def market_flow_summary(days: int = 5, as_of: Optional[str] = None) -> Dict[str, Any]:
    """大盘资金方向摘要 —— **只关心流入/流出**, 不区分主力/全量。

    Returns:
        {trade_date, direction, net_yi, turnover_yi, pct, history:[...],
         note}
    """
    hist = market_fund_flow_history(days=days, as_of=as_of)
    if not hist:
        return {"direction": "unknown", "history": [],
                "note": "no data (snapshot/remote empty)"}
    last = hist[-1]
    net = _f(last.get("main_net")) or 0.0
    to = _f(last.get("turnover_yi"))
    if to is None:
        to = abs(_f(last.get("main_net")) or 0)  # 远端日线可能无 turnover
    direction = last.get("direction") or (
        "inflow" if net > 0 else ("outflow" if net < 0 else "flat"))
    # 近 N 日方向序列 (便于判断是否持续流出)
    seq = [r.get("direction") or ("inflow" if (r.get("main_net") or 0) > 0 else
           ("outflow" if (r.get("main_net") or 0) < 0 else "flat"))
           for r in hist]
    def _yi(x):
        x = _f(x)
        return None if x is None else round(x / 1e8, 2)

    # 兼容: 库里 turn/inflow/outflow 可能是元; hist 项也可能已带 *_yi
    inflow_yi = last.get("inflow_yi")
    if inflow_yi is None and _f(last.get("inflow")) is not None:
        inflow_yi = _yi(last.get("inflow"))
    outflow_yi = last.get("outflow_yi")
    if outflow_yi is None and _f(last.get("outflow")) is not None:
        outflow_yi = _yi(last.get("outflow"))
    return {
        "trade_date": last.get("trade_date"),
        "direction": direction,
        "inflow_yi": inflow_yi,
        "outflow_yi": outflow_yi,
        "net_yi": last.get("net_yi") if last.get("net_yi") is not None
                  else round(net / 1e8, 2),
        "turnover_yi": last.get("turnover_yi"),
        "net_pct": last.get("net_pct") if last.get("net_pct") is not None
                   else last.get("main_pct"),
        "pct": last.get("main_pct"),
        "direction_seq": seq,
        "history": hist,
        "note": last.get("note") or "量价方向近似: 流入/流出/净额/净占比, 非主力单型",
    }


def _load_market_db(days: int, as_of: Optional[str]) -> List[Dict[str, Any]]:
    sql = (f"SELECT trade_date, main_net, super_net, large_net, mid_net, small_net, "
           f"main_pct, source, turnover, inflow, outflow FROM {T_MARKET} WHERE 1=1")
    args: list = []
    if as_of:
        sql += " AND trade_date <= %s"
        args.append(str(as_of)[:10])
    sql += " ORDER BY trade_date DESC LIMIT %s"
    args.append(days)
    with _pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(args))
            rows = cur.fetchall()
    out = []
    for r in rows:
        to = _f(r[8])
        inflow = _f(r[9])
        outflow = _f(r[10])
        net = _f(r[1]) or 0.0
        out.append({
            "trade_date": str(r[0])[:10], "main_net": net,
            "super_net": _f(r[2]), "large_net": _f(r[3]), "mid_net": _f(r[4]),
            "small_net": _f(r[5]), "main_pct": _f(r[6]), "source": r[7],
            "turnover": to, "inflow": inflow, "outflow": outflow,
            "turnover_yi": round(to / 1e8, 2) if to else None,
            "inflow_yi": round(inflow / 1e8, 2) if inflow else None,
            "outflow_yi": round(outflow / 1e8, 2) if outflow else None,
            "net_yi": round(net / 1e8, 2),
            "net_pct": _f(r[6]),
            "direction": ("inflow" if net > 0 else ("outflow" if net < 0 else "flat")),
        })
    return list(reversed(out))


# ================================================================
# 2. 个股
# ================================================================

def stock_fund_flow_realtime(code: str, date: Optional[str] = None,
                             prefer: str = "snapshot") -> Dict[str, Any]:
    """个股实时/当日资金流。

    prefer:
      snapshot = 本地 realtime_snapshot 量价方向近似 (零外网, approx=True)
      remote   = 远端 (东财/新浪等, 有单型分类时更准)
    """
    code = (code or "").strip()
    if not code:
        return {"code": "", "error": "empty code"}
    if prefer != "remote":
        try:
            from app.market_cn.fund_flow_local import get_fund_flow_from_snapshot
            d = get_fund_flow_from_snapshot(code, date=date)
            if d and d.get("points", 0) > 0:
                d["scope"] = "stock"
                d["trade_date"] = (date or datetime.now().strftime("%Y-%m-%d"))
                return d
        except Exception as e:
            logger.warning("[fund_flow_api] snapshot 资金流失败(%s): %s", code, e)
    # 远端
    try:
        from app.market_cn.tape import get_fund_flow_realtime
        d = get_fund_flow_realtime(code) or {}
        d.update({"code": code, "scope": "stock", "approx": False,
                  "trade_date": (date or datetime.now().strftime("%Y-%m-%d"))})
        return d
    except Exception as e:
        return {"code": code, "error": str(e), "approx": True,
                "trade_date": date or datetime.now().strftime("%Y-%m-%d")}


def stock_fund_flow_history(code: str, days: int = 30,
                            prefer_db: bool = True) -> List[Dict[str, Any]]:
    """个股历史日线 (5~30)。库优先, 缺则远端 EM fflow 日线回填。"""
    code = (code or "").strip()
    days = _clamp_days(days)
    ensure_fund_flow_tables()
    if prefer_db:
        rows = _load_stock_db(code, days)
        if rows:
            return rows
    rows = _stock_from_kline1m(code, days)
    if not rows:
        rows = _fetch_stock_daily(code, days)
    if not rows:
        rows = _stock_from_snapshot(code, days)
    if rows:
        upsert_stock(rows)
    return _load_stock_db(code, days) or rows


def _fetch_stock_daily(code: str, days: int) -> List[Dict[str, Any]]:
    from app.market_cn.tape import get_fund_flow_daily
    try:
        d = get_fund_flow_daily(code, days=days) or {}
    except Exception as e:
        logger.warning("[fund_flow_api] 个股日线远端失败(%s): %s", code, e)
        return []
    items = d.get("data") or d.get("rows") or d.get("list") or []
    if isinstance(items, dict):
        items = items.get("data") or []
    out = []
    for r in items:
        if not isinstance(r, dict):
            continue
        out.append({
            "trade_date": str(r.get("date") or r.get("trade_date") or "")[:10],
            "code": code,
            "main_net": _f(r.get("main_net")),
            "super_net": _f(r.get("super_net")),
            "large_net": _f(r.get("large_net")),
            "mid_net": _f(r.get("mid_net")),
            "small_net": _f(r.get("small_net")),
            "source": "eastmoney",
            "approx": False,
        })
    return [r for r in out if r["trade_date"]]


def upsert_stock(rows: List[Dict[str, Any]]) -> int:
    if not rows:
        return 0
    ensure_fund_flow_tables()
    n = 0
    with _pool().connection() as conn:
        with conn.cursor() as cur:
            for r in rows:
                if not r.get("code") or not r.get("trade_date"):
                    continue
                cur.execute(
                    f"""INSERT INTO {T_STOCK}
                        (trade_date, code, main_net, super_net, large_net,
                         mid_net, small_net, source, approx, updated_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s, NOW())
                        ON CONFLICT (trade_date, code) DO UPDATE SET
                          main_net=EXCLUDED.main_net, super_net=EXCLUDED.super_net,
                          large_net=EXCLUDED.large_net, mid_net=EXCLUDED.mid_net,
                          small_net=EXCLUDED.small_net, source=EXCLUDED.source,
                          approx=EXCLUDED.approx, updated_at=NOW()""",
                    (r["trade_date"], r["code"], r.get("main_net"), r.get("super_net"),
                     r.get("large_net"), r.get("mid_net"), r.get("small_net"),
                     r.get("source"), bool(r.get("approx"))),
                )
                n += 1
            _prune(cur, T_STOCK, KEEP_DAYS_DEFAULT)
        conn.commit()
    return n


def _load_stock_db(code: str, days: int) -> List[Dict[str, Any]]:
    with _pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT trade_date, main_net, super_net, large_net, mid_net, "
                f"small_net, source, approx FROM {T_STOCK} "
                f"WHERE code=%s ORDER BY trade_date DESC LIMIT %s",
                (code, days))
            rows = cur.fetchall()
    out = []
    for r in rows:
        out.append({
            "trade_date": str(r[0])[:10], "code": code,
            "main_net": _f(r[1]), "super_net": _f(r[2]), "large_net": _f(r[3]),
            "mid_net": _f(r[4]), "small_net": _f(r[5]),
            "source": r[6], "approx": bool(r[7]),
        })
    return list(reversed(out))


# ================================================================
# 3. 行业 / 概念
# ================================================================

def sector_fund_flow_realtime(board_type: str = "industry",
                              indicator: str = "今日") -> List[Dict[str, Any]]:
    """行业/概念板块资金流排名 (远端聚合)。

    board_type: industry | concept
    indicator: 今日 / 3日 / 5日 / 10日
    """
    from app.market_cn.index import get_sector_fund_flow
    board_type = (board_type or "industry").lower()
    if board_type not in ("industry", "concept"):
        board_type = "industry"
    try:
        rows = get_sector_fund_flow(indicator=indicator, board_type=board_type) or []
    except Exception as e:
        logger.warning("[fund_flow_api] 板块资金流失败(%s): %s", board_type, e)
        return []
    today = datetime.now().strftime("%Y-%m-%d")
    out = []
    for r in rows:
        out.append({
            "trade_date": today,
            "board_type": board_type,
            "code": str(r.get("code") or ""),
            "name": r.get("name") or "",
            "change_pct": _f(r.get("change_pct")),
            "main_net": _f(r.get("main_net")),
            "main_pct": _f(r.get("main_pct")),
            "in_net": _f(r.get("in_net")),
            "out_net": _f(r.get("out_net")),
            "turnover": _f(r.get("turnover")),
            "lead_stock": r.get("lead_stock") or "",
            "lead_pct": _f(r.get("lead_pct")),
            "source": "eastmoney_or_sina",
            "indicator": indicator,
        })
    return out


def upsert_sector(rows: List[Dict[str, Any]]) -> int:
    if not rows:
        return 0
    ensure_fund_flow_tables()
    n = 0
    with _pool().connection() as conn:
        with conn.cursor() as cur:
            for r in rows:
                if not r.get("code"):
                    continue
                cur.execute(
                    f"""INSERT INTO {T_SECTOR}
                        (trade_date, board_type, code, name, change_pct,
                         main_net, main_pct, in_net, out_net, turnover,
                         lead_stock, lead_pct, source, updated_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, NOW())
                        ON CONFLICT (trade_date, board_type, code) DO UPDATE SET
                          name=EXCLUDED.name, change_pct=EXCLUDED.change_pct,
                          main_net=EXCLUDED.main_net, main_pct=EXCLUDED.main_pct,
                          in_net=EXCLUDED.in_net, out_net=EXCLUDED.out_net,
                          turnover=EXCLUDED.turnover, lead_stock=EXCLUDED.lead_stock,
                          lead_pct=EXCLUDED.lead_pct, source=EXCLUDED.source,
                          updated_at=NOW()""",
                    (r.get("trade_date"), r.get("board_type") or "industry",
                     r.get("code"), r.get("name"), r.get("change_pct"),
                     r.get("main_net"), r.get("main_pct"), r.get("in_net"),
                     r.get("out_net"), r.get("turnover"), r.get("lead_stock"),
                     r.get("lead_pct"), r.get("source")),
                )
                n += 1
            _prune(cur, T_SECTOR, KEEP_DAYS_DEFAULT)
        conn.commit()
    return n


def sector_fund_flow_history(board_type: str = "industry", days: int = 30,
                             top_n: int = 0,
                             prefer_db: bool = True) -> List[Dict[str, Any]]:
    """行业/概念历史 —— **来自每日快照累积** (上游无逐日序列)。

    返回按 trade_date 升序的板块行; top_n>0 时每个交易日只留 main_net 前 N。
    """
    board_type = (board_type or "industry").lower()
    days = _clamp_days(days)
    ensure_fund_flow_tables()
    if not prefer_db:
        rows = sector_fund_flow_realtime(board_type=board_type, indicator="今日")
        upsert_sector(rows)
    sql = (f"SELECT trade_date, board_type, code, name, change_pct, main_net, "
           f"main_pct, in_net, out_net, turnover, lead_stock, lead_pct, source "
           f"FROM {T_SECTOR} WHERE board_type=%s ORDER BY trade_date DESC, main_net DESC NULLS LAST")
    with _pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (board_type,))
            raw = cur.fetchall()
    out = [{
        "trade_date": str(r[0])[:10], "board_type": r[1], "code": r[2], "name": r[3],
        "change_pct": _f(r[4]), "main_net": _f(r[5]), "main_pct": _f(r[6]),
        "in_net": _f(r[7]), "out_net": _f(r[8]), "turnover": _f(r[9]),
        "lead_stock": r[10], "lead_pct": _f(r[11]), "source": r[12],
    } for r in raw]
    # 按日截 top_n / 只要最近 days 个日期
    dates: List[str] = []
    seen = set()
    for r in out:
        d = r["trade_date"]
        if d not in seen:
            seen.add(d)
            dates.append(d)
        if len(dates) >= days:
            break
    keep_dates = set(dates)
    filtered = [r for r in out if r["trade_date"] in keep_dates]
    if top_n and top_n > 0:
        by_date: Dict[str, List] = {}
        for r in filtered:
            by_date.setdefault(r["trade_date"], []).append(r)
        filtered = []
        for d, rows in by_date.items():
            filtered.extend(rows[:top_n])
    return sorted(filtered, key=lambda x: (x["trade_date"], -(x["main_net"] or 0)))


# ================================================================
# 4. 一键同步
# ================================================================

def sync_fund_flow_history(days: int = 30,
                           codes: Optional[List[str]] = None,
                           board_types: Optional[List[str]] = None,
                           with_sector_today: bool = True) -> Dict[str, Any]:
    """取数 + 入库 + 裁剪。可被 scheduler / CLI 调用。

    Args:
        days: 5~30
        codes: 个股列表 (空则不同步个股)
        board_types: ["industry","concept"]; 空则不刷板块
        with_sector_today: True 时若 board_types 非空则先拉「今日」再入库

    Returns:
        {"days", "market", "stock", "sector", "pruned_note"}
    """
    days = _clamp_days(days)
    ensure_fund_flow_tables()
    summary = {"days": days, "market": 0, "stock": 0, "sector": 0}

    # 大盘
    mrows = _fetch_market_daily(days)
    if mrows:
        summary["market"] = upsert_market(mrows)

    # 个股
    codes = list(codes or [])
    scount = 0
    for c in codes:
        rows = _fetch_stock_daily(c, days)
        if rows:
            scount += upsert_stock(rows)
    summary["stock"] = scount

    # 板块 (历史靠快照; 今日拉一遍)
    btypes = list(board_types or [])
    qcount = 0
    if with_sector_today and btypes:
        for bt in btypes:
            rows = sector_fund_flow_realtime(board_type=bt, indicator="今日")
            qcount += upsert_sector(rows)
    summary["sector"] = qcount
    summary["pruned_note"] = f"history keep last {days} days (5~30)"
    return summary



# ================================================================
# 5. Snapshot 回退 (EM 远端不可用时, realtime_snapshot 近似日汇总)
# ================================================================

def _snap_days(limit: int) -> List[str]:
    """snapshot 表最近 N 个日期 (降序)。"""
    try:
        with _pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT DISTINCT time::date FROM realtime_snapshot "
                    f"ORDER BY 1 DESC LIMIT %s", (limit,))
                return [str(r[0])[:10] for r in cur.fetchall()]
    except Exception as e:
        logger.warning("[fund_flow_api] snapshot 日期列表失败: %s", e)
        return []


def _market_from_snapshot(days: int, as_of: Optional[str] = None) -> List[Dict[str, Any]]:
    """全市场按日净流向 (量价方向近似, **方向优先**, 不做主力分类)。

    清洗 (2026-09-26, 对齐 Sina 量级):
      - 剔除 688* (科创板 volume 脏数据, 会把成交额放大百倍);
      - 剔除北交所/老三板 8*/4*/92* (非沪深两市, 与 Sina 口径不一致);
      - 成交额 sanity: 单票当日 turnover > 2000 亿 → 丢弃 (脏量)。
    输出含 net_yi / turnover_yi (亿元) 与 direction (inflow|outflow|flat)。
    """
    days = _clamp_days(days)
    dts = _snap_days(days)
    if as_of:
        dts = [d for d in dts if d <= str(as_of)[:10]]
    out = []
    # 2026-09-26 由按年分表改为单表 realtime_snapshot
    sql = (
        "WITH t AS ("
        "  SELECT symbol, time, \"last\", volume,"
        "         lag(volume) OVER (PARTITION BY symbol ORDER BY time) AS pvol,"
        "         lag(\"last\") OVER (PARTITION BY symbol ORDER BY time) AS plast"
        f"  FROM realtime_snapshot WHERE time::date=%s"
        "    AND symbol NOT LIKE '688%%'"          # 脏 volume
        "    AND symbol NOT LIKE '8%%'"
        "    AND symbol NOT LIKE '4%%'"
        "    AND symbol NOT LIKE '92%%'"
        "), s AS ("
        "  SELECT symbol,"
        "         sum(CASE WHEN \"last\" > plast THEN (volume-pvol)*\"last\""
        "                  WHEN \"last\" < plast THEN -(volume-pvol)*\"last\""
        "                  ELSE 0 END) AS net,"
        "         sum((volume-pvol)*\"last\") AS turnover"
        "  FROM t WHERE pvol IS NOT NULL AND volume > pvol AND plast IS NOT NULL"
        "  GROUP BY symbol"
        ") SELECT coalesce(sum(net),0), coalesce(sum(turnover),0), count(*),"
        "         coalesce(sum(CASE WHEN net > 0 THEN net ELSE 0 END),0),"
        "         coalesce(sum(CASE WHEN net < 0 THEN -net ELSE 0 END),0) FROM s"
        " WHERE turnover < 200000000000"           # 2000 亿 sanity
    )
    try:
        with _pool().connection() as conn:
            with conn.cursor() as cur:
                for d in dts:
                    cur.execute(sql, (d,))
                    net, turnover, nsym, inflow, outflow = cur.fetchone()
                    net = float(net or 0)
                    turnover = float(turnover or 0)
                    inflow = float(inflow or 0)
                    outflow = float(outflow or 0)
                    pct = (net / turnover * 100.0) if turnover > 0 else 0.0
                    if net > 0:
                        direction = "inflow"
                    elif net < 0:
                        direction = "outflow"
                    else:
                        direction = "flat"
                    out.append({
                        "trade_date": d,
                        "main_net": round(net, 2),
                        "super_net": None, "large_net": None,
                        "mid_net": None, "small_net": None,
                        "main_pct": round(pct, 2),
                        "inflow_yi": round(inflow / 1e8, 2),
                        "outflow_yi": round(outflow / 1e8, 2),
                        "net_yi": round(net / 1e8, 2),
                        "turnover_yi": round(turnover / 1e8, 2),
                        "net_pct": round(pct, 2),
                        "turnover": round(turnover, 2),
                        "inflow": round(inflow, 2),
                        "outflow": round(outflow, 2),
                        "direction": direction,
                        "source": "snapshot_approx",
                        "approx": True,
                        "note": f"量价方向近似(非主力分类) nsym={nsym}; 已剔688/北交所/脏量",
                    })
    except Exception as e:
        logger.warning("[fund_flow_api] 大盘 snapshot 汇总失败: %s", e)
    return out


def _stock_from_snapshot(code: str, days: int) -> List[Dict[str, Any]]:
    """个股按日 snapshot 近似 (fund_flow_local 同口径)。"""
    from app.market_cn.fund_flow_local import get_fund_flow_from_snapshot
    out = []
    for d in _snap_days(days):
        try:
            r = get_fund_flow_from_snapshot(code, date=d)
        except Exception:
            continue
        if not r or not r.get("points"):
            continue
        out.append({
            "trade_date": d, "code": code,
            "main_net": float(r.get("total_main_net") or 0),
            "super_net": None, "large_net": None,
            "mid_net": None, "small_net": None,
            "source": "snapshot_approx", "approx": True,
        })
    return out


# ================================================================
# helpers
# ================================================================





# ================================================================
# 7. 1m OHLCV 资金流 (2026-09-26: 日度/历史主源, 优先于 realtime_snapshot)
# ================================================================
# 口径: 额 ≈ volume * close; 方向 = sign(close - prev_close) (首根用 close vs open)。
# volume 为分钟成交量(股)。已剔 688*/北交所脏样本与极端成交额。
# kline_1m 多为**盘后回填** —— 盘中当日可能尚无 1m, 由 snapshot 派生兜底。

def _kline_1m_year_table(year: int) -> str:
    return f"kline_1m_{year}"


def _flow_from_1m_day(day: str, code: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """单日 1m OHLCV → 流入/流出/净额/净占比。code=None 全市场。"""
    year = int(str(day)[:4])
    table = _kline_1m_year_table(year)
    where = "time::date = %s"
    args: list = [day]
    if code:
        where += " AND symbol = %s"
        args.append(code)
    else:
        # 清洗: 与 snapshot 同口径
        # psycopg2: 参数化查询里 LIKE 字面量 % 必须写成 %%
        where += (" AND symbol NOT LIKE '688%%'"
                  " AND symbol NOT LIKE '8%%'"
                  " AND symbol NOT LIKE '4%%'"
                  " AND symbol NOT LIKE '92%%'")
    sql = (
        "WITH t AS ("
        "  SELECT symbol, time, open, close, volume,"
        "         lag(close) OVER (PARTITION BY symbol ORDER BY time) AS pc"
        f"  FROM {table} WHERE {where}"
        "), s AS ("
        "  SELECT symbol,"
        "         CASE WHEN pc IS NOT NULL THEN close ELSE open END AS ref,"
        "         close, volume"
        "  FROM t"
        "), d AS ("
        "  SELECT symbol,"
        "         CASE WHEN close > ref THEN volume * close"
        "              WHEN close < ref THEN -(volume * close)"
        "              ELSE 0 END AS net,"
        "         volume * close AS amt"
        "  FROM s WHERE volume > 0 AND close > 0"
        "), p AS ("
        "  SELECT symbol, sum(net) AS net, sum(amt) AS amt FROM d GROUP BY symbol"
        ") SELECT coalesce(sum(net),0), coalesce(sum(amt),0), count(*),"
        "        coalesce(sum(CASE WHEN net>0 THEN net ELSE 0 END),0),"
        "        coalesce(sum(CASE WHEN net<0 THEN -net ELSE 0 END),0) "
        " FROM p WHERE amt < 200000000000"
    )
    try:
        with _pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(args))
                net, amt, nsym, inflow, outflow = cur.fetchone()
    except Exception as e:
        logger.warning("[fund_flow_api] 1m 资金流失败(%s): %s", day, e)
        return None
    net = float(net or 0)
    amt = float(amt or 0)
    inflow = float(inflow or 0)
    outflow = float(outflow or 0)
    pct = (net / amt * 100.0) if amt > 0 else 0.0
    direction = "inflow" if net > 0 else ("outflow" if net < 0 else "flat")
    out = {
        "trade_date": str(day)[:10],
        "main_net": round(net, 2),
        "super_net": None, "large_net": None, "mid_net": None, "small_net": None,
        "main_pct": round(pct, 2),
        "net_pct": round(pct, 2),
        "inflow": round(inflow, 2), "outflow": round(outflow, 2),
        "turnover": round(amt, 2),
        "inflow_yi": round(inflow / 1e8, 2),
        "outflow_yi": round(outflow / 1e8, 2),
        "net_yi": round(net / 1e8, 2),
        "turnover_yi": round(amt / 1e8, 2),
        "direction": direction,
        "source": "kline_1m",
        "approx": True,
        "note": f"1m OHLCV 量价方向近似 nsym={nsym} (非主力单型)",
    }
    return out


def _market_from_kline1m(days: int, as_of: Optional[str] = None) -> List[Dict[str, Any]]:
    """最近 N 个交易日 (从 kline_1m)。"""
    days = _clamp_days(days)
    cutoff = (datetime.now() - timedelta(days=days * 3)).strftime("%Y-%m-%d")
    try:
        with _pool().connection() as conn:
            with conn.cursor() as cur:
                year = datetime.now().year
                for yt in (year, year - 1):
                    cur.execute(
                        f"SELECT DISTINCT time::date FROM kline_1m_{yt} "
                        f"WHERE time::date >= %s ORDER BY 1 DESC LIMIT %s",
                        (cutoff, days))
                    dts = [str(r[0])[:10] for r in cur.fetchall()]
                    if dts:
                        break
    except Exception as e:
        logger.warning("[fund_flow_api] 1m 日期列表失败: %s", e)
        return []
    if as_of:
        dts = [d for d in dts if d <= str(as_of)[:10]]
    rows = []
    for d in dts[:days]:
        r = _flow_from_1m_day(d)
        if r:
            rows.append(r)
    return list(reversed(rows))


def _stock_from_kline1m(code: str, days: int) -> List[Dict[str, Any]]:
    """个股最近 N 日 1m 资金流。"""
    days = _clamp_days(days)
    cutoff = (datetime.now() - timedelta(days=days * 3)).strftime("%Y-%m-%d")
    try:
        with _pool().connection() as conn:
            with conn.cursor() as cur:
                year = datetime.now().year
                cur.execute(
                    f"SELECT DISTINCT time::date FROM kline_1m_{year} "
                    f"WHERE symbol=%s AND time::date >= %s ORDER BY 1 DESC LIMIT %s",
                    (code, cutoff, days))
                dts = [str(r[0])[:10] for r in cur.fetchall()]
    except Exception as e:
        logger.warning("[fund_flow_api] 1m 个股日期失败(%s): %s", code, e)
        return []
    out = []
    for d in dts[:days]:
        r = _flow_from_1m_day(d, code=code)
        if not r:
            continue
        out.append({
            "trade_date": r["trade_date"], "code": code,
            "main_net": r["main_net"],
            "super_net": None, "large_net": None,
            "mid_net": None, "small_net": None,
            "main_pct": r["main_pct"], "net_pct": r["net_pct"],
            "inflow": r["inflow"], "outflow": r["outflow"],
            "turnover": r["turnover"],
            "inflow_yi": r["inflow_yi"], "outflow_yi": r["outflow_yi"],
            "net_yi": r["net_yi"], "turnover_yi": r["turnover_yi"],
            "direction": r["direction"],
            "source": "kline_1m", "approx": True,
        })
    return list(reversed(out))



# ================================================================
# 6. 盘中跟拍 (snapshot 采集后调用) / 收盘定稿
# ================================================================

def update_intraday_fund_flow(codes: Optional[List[str]] = None) -> Dict[str, Any]:
    """**每拍 snapshot 之后**刷新「今日」资金流行 (幂等 UPSERT)。

    设计 (2026-09-26 用户裁定):
      - 不另开分时落库任务 —— 快照已是 60s 原始序列, 资金流是它的派生视图;
      - 每次全量重算当日 (清洗 688/北交所/脏量), 写 qd_fund_flow_market 一行;
      - codes 非空时顺带刷新这些票的今日个股行 (默认只刷大盘, 控制成本)。
      - 任何异常只打日志, **绝不拖垮 snapshot 采集**。

    Returns:
        {"ok", "trade_date", "direction", "net_yi", "net_pct", "elapsed"}
    """
    import time as _time
    t0 = _time.time()
    global _LAST_INTRADAY_TS
    if (_time.time() - _LAST_INTRADAY_TS) < INTRADAY_MIN_INTERVAL_SEC and not codes:
        return {"ok": True, "skipped": True,
                "reason": f"throttle {INTRADAY_MIN_INTERVAL_SEC}s",
                "elapsed": 0.0}
    day = datetime.now().strftime("%Y-%m-%d")
    try:
        rows = _market_from_kline1m(1, as_of=day)
        if not rows:
            rows = _market_from_snapshot(1, as_of=day)
        # _snap_days 可能无今日(开盘前) → 空
        row = next((r for r in rows if r.get("trade_date") == day), None)
        if row is None and rows:
            row = rows[0]
        if row:
            upsert_market([row])
            _LAST_INTRADAY_TS = _time.time()
            if codes:
                srows = []
                for c in codes:
                    srows.extend(_stock_from_snapshot(c, 1))
                if srows:
                    upsert_stock(srows)
            return {
                "ok": True,
                "trade_date": row.get("trade_date"),
                "direction": row.get("direction"),
                "net_yi": row.get("net_yi"),
                "net_pct": row.get("net_pct"),
                "elapsed": round(_time.time() - t0, 2),
            }
        return {"ok": False, "trade_date": day, "error": "no snapshot today"}
    except Exception as e:
        logger.warning("[fund_flow_api] 盘中资金流刷新失败: %s", e)
        return {"ok": False, "error": str(e)}


def finalize_fund_flow_day(codes: Optional[List[str]] = None,
                           board_types: Optional[List[str]] = None) -> Dict[str, Any]:
    """收盘后定稿: 今日行重算 + 板块快照入库 + 5~30 日裁剪 + 可选远端补历史。"""
    ensure_fund_flow_tables()
    r1 = update_intraday_fund_flow(codes=codes)
    n_sec = 0
    for bt in (board_types or []):
        try:
            n_sec += upsert_sector(sector_fund_flow_realtime(board_type=bt, indicator="今日"))
        except Exception as e:
            logger.warning("[fund_flow_api] 板块定稿失败(%s): %s", bt, e)
    # 可选: 远端日线补历史 (失败忽略, snapshot 已够近几日)
    n_mkt = 0
    try:
        mrows = _fetch_market_daily(KEEP_DAYS_DEFAULT)
        if mrows:
            n_mkt = upsert_market(mrows)
    except Exception as e:
        logger.warning("[fund_flow_api] 远端大盘日线补写失败: %s", e)
    return {"intraday": r1, "sector_rows": n_sec, "market_rows": n_mkt}



def _f(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except Exception:
        return None


# ================================================================
# CLI
# ================================================================
if __name__ == "__main__":
    import json
    import sys

    def _load_env():
        try:
            from dotenv import load_dotenv
            import os as _os
            for _p in (_os.path.join(_os.getcwd(), ".env"),
                       _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.dirname(
                           _os.path.abspath(__file__)))), ".env")):
                if _os.path.isfile(_p):
                    load_dotenv(_p, override=False)
                    break
        except Exception:
            pass

    _load_env()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    if cmd == "market":
        days = int(sys.argv[2]) if len(sys.argv) > 2 else 10
        print(json.dumps(market_fund_flow_history(days=days), ensure_ascii=False, indent=2, default=str))
    elif cmd == "stock":
        code = sys.argv[2] if len(sys.argv) > 2 else "600519"
        days = int(sys.argv[3]) if len(sys.argv) > 3 else 10
        print(json.dumps(stock_fund_flow_history(code, days=days), ensure_ascii=False, indent=2, default=str))
    elif cmd == "sector":
        bt = sys.argv[2] if len(sys.argv) > 2 else "industry"
        rows = sector_fund_flow_realtime(board_type=bt, indicator="今日")
        print(json.dumps(rows[:15], ensure_ascii=False, indent=2, default=str))
    elif cmd == "rt":
        print(json.dumps({"market": market_fund_flow_realtime()}, ensure_ascii=False, indent=2, default=str))
    elif cmd == "sync":
        days = int(sys.argv[2]) if len(sys.argv) > 2 else 30
        codes = sys.argv[3].split(",") if len(sys.argv) > 3 and sys.argv[3] else []
        bts = (sys.argv[4].split(",") if len(sys.argv) > 4 and sys.argv[4]
               else ["industry", "concept"])
        print(json.dumps(sync_fund_flow_history(days=days, codes=codes, board_types=bts),
                         ensure_ascii=False, indent=2))
    else:
        print("usage: python -m app.market_cn.fund_flow_api "
              "[market N|stock CODE N|sector industry|rt|sync DAYS codes bt1,bt2]")

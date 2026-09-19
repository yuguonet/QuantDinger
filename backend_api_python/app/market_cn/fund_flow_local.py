# -*- coding: utf-8 -*-
"""fund_flow_local.py — 由本地 realtime_snapshot 派生分钟级「近似资金流」(2026-09-19)

背景:
  个股分钟资金流原走东财 push2 远程（tape.get_fund_flow_realtime），盘中频繁
  `RemoteDisconnected`，且外网依赖重。本模块改为**从本地 DB 派生**，盘中零外网。
  远程仅在本地无数据时兜底（见 tape.get_fund_flow_realtime）。

数据来源:
  realtime_snapshot_YYYY（scheduler 每 60s/拍全市场快照，见 realtime_snapshot.py）
  可用字段: time / "last" / volume（**当日累计量**）/ open / high / low / "previousClose"

口径（用户 2026-09-19 定：量价方向近似 —— 明确是近似值，**非真实主力分类**）:
  对相邻两拍：Δvol = vol(t) - vol(t-1)；Δlast = last(t) - last(t-1)
  方向 = sign(Δlast)（涨计流入 +、跌计流出 -、平 0）
  分钟净额 = Δvol * last(t) * 方向      # Δvol × 价 ≈ 该拍成交额
  累计 total_main_net = Σ 分钟净额
  ⚠ 真实「超大单/大单/中单/小单」需要逐笔成交分类，60s 快照拍不到，
    故 small/mid/large/super 一律 None（不编造）。返回值带 approx=True 显式标注。

易错点:
  1. volume 是**当日累计**，必须差分；集合竞价→开盘首拍跳变由累计差分自然消化。
  2. 表按年分表 realtime_snapshot_YYYY；"last"/"previousClose" 是 PostgreSQL 特殊名，
     SQL 中必须加双引号。
  3. 非交易时段/当日无数据 → 回退该股**最新有数据的交易日**（周六日自动落到周五）。
  4. 指数（如 000001）通常不在快照表（快照只采股票），会返回空 → 由上层回退远程。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from app.utils.logger import get_logger

logger = get_logger(__name__)

_TABLE_PREFIX = "realtime_snapshot"

_NOTE = ("由 realtime_snapshot 量价方向近似（涨计流入/跌计流出），非真实主力分类；"
         "small/mid/large/super 无数据")


def _snapshot_pool():
    from app.utils.db_market import get_market_db_manager
    return get_market_db_manager()._get_pool("CNStock")


def _table(year: int) -> str:
    return f"{_TABLE_PREFIX}_{year}"


def _empty(code: str) -> Dict[str, Any]:
    return {"code": code, "points": 0, "total_main_net": 0.0, "data": [],
            "source": "realtime_snapshot", "approx": True, "note": _NOTE}


def _resolve_date(cur, code: str, year: int, date: Optional[str]) -> Optional[str]:
    """确定取数日期：显式 date > 当日(该股有数据) > 该股最新有数据的交易日。"""
    d = date or datetime.now().strftime("%Y-%m-%d")
    cur.execute(f'SELECT 1 FROM "{_table(year)}" WHERE symbol=%s AND time::date=%s LIMIT 1',
                (code, d))
    if cur.fetchone():
        return d
    if date:  # 显式指定却无数据 → 不回退，返回空
        return None
    cur.execute(f'SELECT max(time)::date FROM "{_table(year)}" WHERE symbol=%s', (code,))
    row = cur.fetchone()
    return str(row[0]) if row and row[0] else None


def get_fund_flow_from_snapshot(code: str, date: Optional[str] = None) -> Dict[str, Any]:
    """从 realtime_snapshot 派生分钟级近似资金流（量价方向法）。

    Args:
        code: 6 位股票代码
        date: "YYYY-MM-DD"；None = 当日（无数据则回退最近交易日）

    Returns:
        {code, points, total_main_net, data:[{time, main_net, small_net, mid_net,
         large_net, super_net}], source:"realtime_snapshot", approx:True, note}
        无数据时 data=[]（调用方据此回退远程）。
    """
    if not code:
        return _empty(code)
    year = int((date or datetime.now().strftime("%Y-%m-%d"))[:4])
    try:
        pool = _snapshot_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                d = _resolve_date(cur, code, year, date)
                if not d:
                    return _empty(code)
                cur.execute(
                    f'SELECT time, "last", volume FROM "{_table(year)}" '
                    f'WHERE symbol = %s AND time >= %s AND time < %s ORDER BY time',
                    (code, f"{d} 09:00:00", f"{d} 16:00:00"),
                )
                rows = cur.fetchall()
    except Exception as e:
        logger.warning("[fund_flow_local] 快照读取失败(%s): %s", code, e)
        return _empty(code)

    return _derive(code, rows)


def _derive(code: str, rows: List) -> Dict[str, Any]:
    """累计量快照序列 → 分钟级近似净额（差分 + 价格方向）。"""
    data: List[Dict[str, Any]] = []
    prev = None
    for r in rows:
        t, last, vol = r[0], r[1], r[2]
        if prev is not None and last is not None and vol is not None:
            _, p_last, p_vol = prev
            dvol = float(vol) - float(p_vol)
            if dvol > 0 and p_last is not None:
                dlast = float(last) - float(p_last)
                direction = 1 if dlast > 0 else (-1 if dlast < 0 else 0)
                data.append({
                    "time": t.strftime("%Y-%m-%d %H:%M") if hasattr(t, "strftime") else str(t),
                    "main_net": round(dvol * float(last) * direction, 2),
                    "small_net": None, "mid_net": None,
                    "large_net": None, "super_net": None,
                })
        prev = (t, last, vol)

    total = round(sum(d["main_net"] for d in data), 2)
    return {"code": code, "points": len(data), "total_main_net": total, "data": data,
            "source": "realtime_snapshot", "approx": True, "note": _NOTE}

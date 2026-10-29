# -*- coding: utf-8 -*-
"""fund_flow_local.py — 由本地 realtime_snapshot 派生分钟级「近似资金流」(2026-09-19)

背景:
  个股分钟资金流原走东财 push2 远程（tape.get_fund_flow_realtime），盘中频繁
  `RemoteDisconnected`，且外网依赖重。本模块改为**从本地 DB 派生**，盘中零外网。
  2026-10-28 远端源整族下线后，本模块即该能力的唯一实现。

数据来源:
  realtime_snapshot（scheduler 每 60s/拍全市场快照，见 realtime_snapshot.py）
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
  2. 表按年分表 realtime_snapshot；"last"/"previousClose" 是 PostgreSQL 特殊名，
     SQL 中必须加双引号。
  3. 非交易时段/当日无数据 → 回退该股**最新有数据的交易日**（周六日自动落到周五）。
  4. 指数（如 000001）通常不在快照表（快照只采股票），会返回空 → 由上层回退远程。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from app.utils.logger import get_logger

logger = get_logger(__name__)

# 2026-09-26 由按年分表改为单表
_TABLE = "realtime_snapshot"

_NOTE = ("由 realtime_snapshot 量价方向近似（涨计流入/跌计流出），非真实主力分类；"
         "small/mid/large/super 无数据")


def _snapshot_pool():
    from app.utils.db_market import get_market_db_manager
    return get_market_db_manager()._get_pool("CNStock")


def _table() -> str:
    return _TABLE


def _empty(code: str) -> Dict[str, Any]:
    return {"code": code, "points": 0, "total_main_net": 0.0, "total_amount": 0.0,
            "main_pct": None, "data": [],
            "source": "realtime_snapshot", "approx": True, "note": _NOTE}


def _resolve_date(cur, code: str, date: Optional[str]) -> Optional[str]:
    """确定取数日期：显式 date > 当日(该股有数据) > 该股最新有数据的交易日。"""
    d = date or datetime.now().strftime("%Y-%m-%d")
    cur.execute(f'SELECT 1 FROM "{_table()}" WHERE symbol=%s AND time::date=%s LIMIT 1',
                (code, d))
    if cur.fetchone():
        return d
    if date:  # 显式指定却无数据 → 不回退，返回空
        return None
    cur.execute(f'SELECT max(time)::date FROM "{_table()}" WHERE symbol=%s', (code,))
    row = cur.fetchone()
    return str(row[0]) if row and row[0] else None


def get_fund_flow_from_snapshot(code: str, date: Optional[str] = None) -> Dict[str, Any]:
    """从 realtime_snapshot 派生分钟级近似资金流（量价方向法）。

    Args:
        code: 6 位股票代码
        date: "YYYY-MM-DD"；None = 当日（无数据则回退最近交易日）

    Returns:
        {code, points, total_main_net, total_amount, main_pct,
         data:[{time, main_net, small_net, mid_net,
         large_net, super_net}], source:"realtime_snapshot", approx:True, note}
        无数据时 data=[]（调用方据此回退远程）。
    """
    if not code:
        return _empty(code)
    try:
        pool = _snapshot_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                d = _resolve_date(cur, code, date)
                if not d:
                    return _empty(code)
                cur.execute(
                    f'SELECT time, "last", volume FROM "{_table()}" '
                    f'WHERE symbol = %s AND time >= %s AND time < %s ORDER BY time',
                    (code, f"{d} 09:00:00", f"{d} 16:00:00"),
                )
                rows = cur.fetchall()
    except Exception as e:
        logger.warning("[fund_flow_local] 快照读取失败(%s): %s", code, e)
        return _empty(code)

    return _derive(code, rows)


# ★ 单拍增量异常判据（2026-10-23 事故修复）：
#   `realtime_snapshot` 的累计量在日内单调不减。但各源 volume 单位口径并非
#   天然一致（tencent 对科创板 parts[6] 返回"股"却按"手"×100），而 coordinator
#   逐拍换源 ⇒ 累计量在真实值与 ×100 之间跳变。若下游只累加正增量、把回落
#   （dvol<0）静默丢弃，62 次跳变累加即 688787 主力净额 1349 亿。
#   单拍（≈60s）增量不可能达到前一拍累计量的 20 倍（实测开盘瞬时最大约 4x），
#   故以此为界丢弃异常拍。与 `realtime_snapshot._VOL_JUMP_RATIO` 同量级但
#   基准不同（那边比当日 MAX、这边比前一拍），不共用常量。
_DVOL_JUMP_RATIO = 20.0


def _derive(code: str, rows: List) -> Dict[str, Any]:
    """累计量快照序列 → 分钟级近似净额（差分 + 价格方向）。

    ⚠️ 数据有效性闸门（2026-10-23 事故修复）。累计量在日内单调不减，故：
      - `dvol < 0`  ⇒ 累计量倒退，该拍口径与前后不一致，丢弃；
      - `dvol > 前值 × _DVOL_JUMP_RATIO` ⇒ 疑似源单位跳变，丢弃；
      - `dvol == 0` ⇒ 无增量，不产生点（非异常）。
    丢弃数计入返回值的 `dropped`，**不静默**：异常拍数会在日志告警，
    避免"数据错了但没人知道"重演（provider 层"允许空、不允许错"的延伸）。

    Returns:
        {"code", "points", "total_main_net", "total_amount", "main_pct",
         "data", "dropped", "source", "approx", "note"}
    """
    data: List[Dict[str, Any]] = []
    dropped = {"negative": 0, "jump": 0}
    amt_sum = 0.0
    prev = None
    for r in rows:
        t, last, vol = r[0], r[1], r[2]
        if prev is not None and last is not None and vol is not None:
            _, p_last, p_vol = prev
            dvol = float(vol) - float(p_vol)
            if dvol < 0:
                # 累计量倒退 ⇒ 源返回了不一致的口径，不是"卖出了负量"
                dropped["negative"] += 1
            elif dvol == 0:
                pass  # 该分钟无成交增量，正常不产生点
            elif p_vol and dvol > float(p_vol) * _DVOL_JUMP_RATIO:
                # 单拍增量远超已累计量 ⇒ 单位跳变（如"股"被当成"手"×100）
                dropped["jump"] += 1
            elif p_last is not None:
                dlast = float(last) - float(p_last)
                direction = 1 if dlast > 0 else (-1 if dlast < 0 else 0)
                amt = dvol * float(last)
                amt_sum += amt
                data.append({
                    "time": t.strftime("%Y-%m-%d %H:%M") if hasattr(t, "strftime") else str(t),
                    "main_net": round(amt * direction, 2),
                    "small_net": None, "mid_net": None,
                    "large_net": None, "super_net": None,
                })
        prev = (t, last, vol)

    total = round(sum(d["main_net"] for d in data), 2)
    if dropped["negative"] or dropped["jump"]:
        logger.warning(
            "[fund_flow_local] %s 丢弃异常拍: 累计量倒退 %d、单位跳变 %d "
            "（保留 %d 拍，净额 %s）",
            code, dropped["negative"], dropped["jump"], len(data), total,
        )
    amt_sum = round(amt_sum, 2)
    return {"code": code, "points": len(data), "total_main_net": total,
            "total_amount": amt_sum,
            "main_pct": (round(total / amt_sum * 100.0, 2) if amt_sum else None),
            "data": data,
            "dropped": dropped,
            "source": "realtime_snapshot", "approx": True, "note": _NOTE}

# -*- coding: utf-8 -*-
"""单票诊断的板块内龙头 / 跟风判定（stock-analyzer 本地实现，与 market-screener 完全独立）。

逻辑来源：market-screener/_topdown.py 的 board_members / limit_stats / follow_filter，
按「两技能完全独立」的用户裁定复制纯计算部分到本模块；不 import 任何 market-screener
模块，不改其文件，也不改 screen_query 共享层。取数只调 app.data_sources.screen_query
的被动只读接口（_tag_map / cross_section / board_series / ticks）。

口径：盘中与历史都用 realtime_snapshot 分钟拍（60s 一拍、保留 20 个交易日），不碰 kline_1m
——后者盘后回填、盘中当日 0 行，且与 snapshot 的同刻量差约 7%、净额差 55%~78%，
两端不得拼接。龙头 = 板块内最早触及涨停价者；跟随 = 龙头触板后 10 分钟内方向与
强度都跟上的成员。缺数显式 None / ok=False，不补默认值、不给方向票。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from . import _inner
from ._inner import env_float as _env_f, env_int as _env_i, num as _num


def _now_hhmm() -> str:
    """分时截断时刻（Asia/Shanghai 墙上钟，与日线/快照口径同源）；非交易时段取 15:00。

    显式钉时区：宿主机/docker 为 UTC 时，naive now() 会把盘中判成凌晨 ⇒ 永远取 15:00，
    板块排名与跟随判定会拿全天口径冒充盘中口径。
    """
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    t = now.strftime("%H:%M")
    return t if ("09:30" <= t <= "11:30" or "13:00" <= t <= "15:00") else "15:00"


def _members(board_name: str, track: str) -> List[str]:
    from ....data_sources import screen_query as sq
    if not board_name:
        return []
    tmap = sq._tag_map("industry" if track == "industry" else "concepts", None)
    return sorted(str(c) for c, tags in tmap.items() if board_name in (tags or []))


def _median(vals: List[float]) -> Optional[float]:
    if not vals:
        return None
    v = sorted(vals)
    n = len(v)
    return round(v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2, 3)


def _limit_leader(limit_rows: List[Dict[str, Any]], day: str,
                  t_to: Optional[str]) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    from ....data_sources import screen_query as sq
    if not limit_rows:
        return None, []
    codes = [str(r["code"]).split(".")[0] for r in limit_rows]
    got = sq.ticks(codes, day=day, t_to=t_to)
    best = None
    for code in codes:
        box = got.get(code) or {}
        pc = _num(box.get("prev_close"))
        if not pc:
            continue
        band = _inner.limit_pct(code)
        if band <= 0:
            continue
        target = pc * (1 + band / 100)
        for hhmm, px, _cum in box.get("ticks") or []:
            if _num(px) is not None and float(px) >= target * 0.999:
                if best is None or hhmm < best["zt_time"]:
                    best = {"code": code, "zt_time": hhmm, "limit_pct": band}
                break
    return best, codes


def _follow(target: str, leader_time: str, limit_codes: List[str], members: List[str],
            day: str, t_to: Optional[str]) -> Dict[str, Any]:
    from ....data_sources import screen_query as sq
    sync_min = _env_f("QD_STOCK_ANALYZER_FOLLOW_SYNC", 0.6)
    ratio_min = _env_f("QD_STOCK_ANALYZER_FOLLOW_RATIO", 0.5)
    window = _env_i("QD_STOCK_ANALYZER_FOLLOW_WINDOW_MIN", 10)
    pool = [c for c in members if c not in set(limit_codes)]
    if not pool:
        return {"ok": False, "reason": "板块内除涨停票外无成员"}
    bs = sq.board_series(pool, day=day, t_to=t_to)
    times, pct = bs.get("times") or [], bs.get("pct") or []
    if not leader_time or not times:
        return {"ok": False, "reason": "缺龙头时刻或板块序列"}
    try:
        i0 = min(i for i, t in enumerate(times) if t >= str(leader_time)[:5])
    except ValueError:
        i0 = len(times) - 1
    idx = [i for i in range(i0, min(len(times), i0 + window + 1)) if pct[i] is not None]
    if len(idx) < 2:
        return {"ok": False, "reason": "窗口内板块序列样本不足"}
    t1 = times[idx[-1]]
    r_s = pct[idx[-1]] - pct[idx[0]]
    box = (sq.ticks([target], day=day, t_to=t1).get(target) or {})
    pc = _num(box.get("prev_close"))
    by_t = {t: _num(px) for t, px, _c in box.get("ticks") or []}
    seq = [(times[i], by_t[times[i]]) for i in idx
           if times[i] in by_t and by_t[times[i]] is not None]
    if not pc or len(seq) < 2:
        return {"ok": False, "reason": "目标票窗口内分时不足"}
    r_i = (seq[-1][1] / seq[0][1] - 1) * 100
    same = tot = 0
    for k in range(1, len(seq)):
        d_i = seq[k][1] - seq[k - 1][1]
        j = times.index(seq[k][0])
        d_s = (pct[j] - pct[j - 1]) if (j > 0 and pct[j] is not None
                                        and pct[j - 1] is not None) else 0.0
        if d_i == 0 and d_s == 0:
            continue
        tot += 1
        if (d_i > 0 and d_s > 0) or (d_i < 0 and d_s < 0):
            same += 1
    sync = same / tot if tot else 0.0
    follow = sync >= sync_min and r_i >= r_s * ratio_min and r_i > 0
    reverse = sync >= sync_min and r_i < 0 and r_s > 0
    return {"ok": True, "t0": str(leader_time)[:5], "t1": t1,
            "board_pct": round(r_s, 3), "r_i": round(r_i, 3),
            "sync": round(sync, 3), "classification": "follow" if follow else
            ("reverse" if reverse else "none")}


def build(code: str, sector: Dict[str, Any]) -> Dict[str, Any]:
    """产出板块内排名、强度与角色；失败返回 ok=False 与 reason。"""
    from ....data_sources import screen_query as sq
    from ....utils.trading_calendar import current_data_day
    code = str(code or "").split(".")[0]
    board = (sector or {}).get("industry_board")
    track = "industry"
    if not board:
        tags = (sector or {}).get("concept_tags") or []
        board = tags[0] if tags else None
        track = "concepts"
    members = _members(board, track)
    min_members = _env_i("QD_STOCK_ANALYZER_BOARD_MIN_MEMBERS", 5)
    if len(members) < min_members:
        return {"ok": False, "reason": f"板块成员不足 {min_members} 只", "board": board,
                "role": "未知"}
    day = current_data_day()
    t_to = _now_hhmm()
    cross = sq.cross_section(as_of=t_to, end_day=day, codes=members,
                             features=["change_pct", "fund_circ_ratio"],
                             order_by="-change_pct")
    rows = [r for r in (cross.get("rows") or []) if r.get("change_pct") is not None]
    ranked = sorted(rows, key=lambda r: -r["change_pct"])
    mine = next((r for r in ranked if str(r.get("code", "")).split(".")[0] == code), None)
    pcts = [r["change_pct"] for r in rows]
    band = _inner.limit_pct(code)
    limit_rows = [r for r in ranked if band > 0 and r["change_pct"] >= band - 0.3]
    leader, limit_codes = _limit_leader(limit_rows, day, t_to)
    follow = _follow(code, leader["zt_time"], limit_codes, members, day, t_to) \
        if leader and code not in limit_codes else {"ok": False, "reason": "目标票为龙头或无龙头"}
    rank = next((i + 1 for i, r in enumerate(ranked)
                 if str(r.get("code", "")).split(".")[0] == code), None)

    role = "未知"
    if leader and code == leader["code"]:
        role = "龙头"
    elif code in limit_codes:
        role = "同板涨停"
    elif follow.get("ok"):
        role = {"follow": "跟风", "reverse": "反向"}.get(
            follow.get("classification"), "未跟随")
    elif ranked:
        role = "无涨停龙头"
    return {"ok": True, "board": board, "track": track, "n": len(ranked),
            "rank": rank, "percentile": (round((1 - rank / len(ranked)) * 100, 1)
                                         if rank and ranked else None),
            "change_pct": mine.get("change_pct") if mine else None,
            "board_pct_median": _median(pcts),
            "advance_n": sum(1 for p in pcts if p > 0),
            "decline_n": sum(1 for p in pcts if p < 0),
            "limit_up_n": len(limit_codes),
            "leader": leader, "role": role, "follow": follow,
            "fund_circ_ratio": mine.get("fund_circ_ratio") if mine else None,
            "caliber": "realtime_snapshot", "as_of": cross.get("as_of"),
            "missing": cross.get("missing") or []}
